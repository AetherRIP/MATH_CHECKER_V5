"""Dedicated TEST RECORDS for repair sessions.

A repair session never touches the live correction records: originals stay in
``results`` untouched, and every repaired result lands here — in the
``repair_runs`` / ``repair_items`` tables plus JSON artifacts under
``data/repairs/<run_id>/``. Nothing in the correction, grading or publication
flows reads these tables.
"""

from __future__ import annotations

import json
import time
import uuid
from pathlib import Path

BANNER = "TESTING ONLY · REPAIRED RESULTS NOT PUBLISHED"


def _db():
    from .. import db
    return db


def repairs_dir() -> Path:
    from ..config import DATA_DIR
    return Path(DATA_DIR) / "repairs"


def item_path(run_id: str, item_id: str) -> Path:
    return repairs_dir() / run_id / f"{item_id}.json"


def create_run(*, coursework_id: str, course_id: str | None = None,
               assignment_title: str | None = None,
               class_name: str | None = None,
               notes: str | None = None) -> dict:
    db = _db()
    run_id = uuid.uuid4().hex
    db.execute(
        "INSERT INTO repair_runs (id, coursework_id, course_id, "
        "assignment_title, class_name, banner, status, started_at, notes) "
        "VALUES (?, ?, ?, ?, ?, ?, 'RUNNING', ?, ?)",
        (run_id, coursework_id, course_id, assignment_title, class_name,
         BANNER, time.time(), notes))
    return {"id": run_id, "coursework_id": coursework_id, "status": "RUNNING",
            "banner": BANNER}


def add_item(run_id: str, item: dict) -> dict:
    """Persist one repaired (or blocked) submission as a test record."""
    db = _db()
    item_id = item.get("id") or uuid.uuid4().hex
    payload = {
        "id": item_id,
        "run_id": run_id,
        "coursework_id": item["coursework_id"],
        "submission_id": item.get("submission_id"),
        "student_id": item.get("student_id"),
        "student_name": item.get("student_name"),
        "mode": item["mode"],
        "status": item["status"],
        "original_result_id": item.get("original_result_id"),
        "original_earned": item.get("original_earned"),
        "original_available": item.get("original_available"),
        "original_percentage": item.get("original_percentage"),
        "repaired_earned": item.get("repaired_earned"),
        "repaired_available": item.get("repaired_available"),
        "repaired_percentage": item.get("repaired_percentage"),
        "repaired_grade": item.get("repaired_grade"),
        "score_basis": item.get("score_basis"),
        "questions_total": item.get("questions_total") or 0,
        "questions_added": item.get("questions_added") or 0,
        "questions_changed": item.get("questions_changed") or 0,
        "questions_flagged": item.get("questions_flagged") or 0,
        "missing_fixed": item.get("missing_fixed") or 0,
        "zeros_fixed": item.get("zeros_fixed") or 0,
        "unresolved_count": item.get("unresolved_count") or 0,
        "diff_json": json.dumps(item.get("diff") or [], ensure_ascii=False),
        "audit_findings_json": json.dumps(item.get("audit_findings") or [],
                                          ensure_ascii=False),
        "validation_json": json.dumps(item.get("validation") or {},
                                      ensure_ascii=False),
        "artifact_path": None,
        "report_filename": item.get("report_filename"),
        "error": item.get("error"),
        "created_at": time.time(),
    }
    # Full original + repaired payloads go to the artifact file (they can hold
    # student answers); the table keeps the diff and the numbers.
    artifact = item_path(run_id, item_id)
    artifact.parent.mkdir(parents=True, exist_ok=True)
    artifact.write_text(json.dumps(item.get("artifact") or item,
                                   ensure_ascii=False, default=str),
                        encoding="utf-8")
    payload["artifact_path"] = str(artifact)

    cols = ", ".join(payload)
    marks = ", ".join("?" for _ in payload)
    db.execute(f"INSERT INTO repair_items ({cols}) VALUES ({marks})",
               tuple(payload.values()))
    return {"id": item_id, **{k: v for k, v in payload.items()
                              if k not in ("diff_json", "audit_findings_json",
                                           "validation_json")}}


def finish_run(run_id: str, *, summary: dict, status: str = "COMPLETE") -> None:
    _db().execute(
        "UPDATE repair_runs SET status = ?, finished_at = ?, summary_json = ? "
        "WHERE id = ?",
        (status, time.time(), json.dumps(summary, ensure_ascii=False), run_id))


def load_run(run_id: str) -> dict | None:
    row = _db().query_one("SELECT * FROM repair_runs WHERE id = ?", (run_id,))
    return _db().row_to_dict(row) if row else None


def load_summary(coursework_id: str) -> dict:
    """Everything the assignment-wide repair dashboard needs."""
    db = _db()
    runs = [db.row_to_dict(r) for r in db.query(
        "SELECT * FROM repair_runs WHERE coursework_id = ? "
        "ORDER BY started_at DESC", (coursework_id,))]
    items = []
    for r in db.query("SELECT * FROM repair_items WHERE coursework_id = ? "
                      "ORDER BY created_at", (coursework_id,)):
        row = db.row_to_dict(r)
        for key, target in (("diff_json", "diff"),
                            ("audit_findings_json", "audit_findings"),
                            ("validation_json", "validation")):
            row[target] = db.loads(row.pop(key) or "[]") or ({} if target == "validation" else [])
        items.append(row)
    counts = {
        "runs": len(runs),
        "items": len(items),
        "repaired": sum(1 for i in items if i["status"] == "REPAIRED"),
        "verified": sum(1 for i in items if i["status"] == "VERIFIED"),
        "match": sum(1 for i in items if i["status"] == "MATCH"),
        "needs_review": sum(1 for i in items if i["status"] == "NEEDS_REVIEW"),
        "unresolved": sum(1 for i in items if i["status"] == "UNRESOLVED"),
        "blocked": sum(1 for i in items if i["status"] == "BLOCKED"),
        "failed": sum(1 for i in items if i["status"] == "FAILED"),
    }
    return {"banner": BANNER, "runs": runs, "items": items, "counts": counts}
