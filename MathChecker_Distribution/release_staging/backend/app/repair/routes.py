"""Read-only repair API: assignment-wide repair dashboard data + previews.

Nothing here writes anything: the repair session writes its test records, and
this module serves them inside the application labelled
"TESTING ONLY · REPAIRED RESULTS NOT PUBLISHED".
"""

from __future__ import annotations

import json
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse

from . import pipeline, store
from .audit import summarise


def _safe(name: str) -> str:
    if "/" in name or "\\" in name or name.startswith(".") or ".." in name:
        raise HTTPException(status_code=400, detail="Invalid name.")
    return name


def register(app: FastAPI) -> None:
    @app.get("/api/repair/summary")
    def repair_summary(coursework_id: str) -> dict:
        from .. import db

        inventory = pipeline.assignment_inventory(coursework_id)
        rows = [db.row_to_dict(r) for r in db.query(
            "SELECT * FROM results WHERE coursework_id = ?", (coursework_id,))]
        blueprint = blueprint_for_coursework(coursework_id)
        audits = []
        for row in rows:
            if blueprint is None:
                continue
            findings = pipeline.audit_stored_result(row, blueprint)
            audits.append({
                "result_id": row["id"],
                "student_name": row["student_name"],
                "earned": row["earned"],
                "available": row["available"],
                "percentage": row["percentage"],
                "review_required": row["review_required"],
                "findings": [{"code": f.code, "severity": f.severity,
                              "question": f.question, "detail": f.detail}
                             for f in findings],
                "summary": summarise(findings),
            })
        stored = store.load_summary(coursework_id)
        return {
            "banner": store.BANNER,
            "assignment": {
                "coursework_id": coursework_id,
                "title": _coursework_title(coursework_id),
            },
            "inventory": inventory,
            "audits": audits,
            "repair": stored,
            "scheme": ({
                "questions": len(blueprint.get("questions") or []),
                "total_marks": sum(int(q.get("max_marks") or 0)
                                   for q in (blueprint.get("questions") or [])),
            } if blueprint else None),
        }

    @app.get("/api/repair/item/{run_id}/{item_id}")
    def repair_item(run_id: str, item_id: str) -> dict:
        _safe(run_id)
        _safe(item_id)
        path = store.item_path(run_id, item_id)
        if not path.is_file():
            raise HTTPException(status_code=404, detail="Repair item not found.")
        return json.loads(path.read_text(encoding="utf-8"))

    @app.get("/api/repair/report/{run_id}/{name}")
    def repair_report(run_id: str, name: str) -> FileResponse:
        _safe(run_id)
        _safe(name)
        if not name.lower().endswith(".pdf"):
            raise HTTPException(status_code=400, detail="Not a PDF.")
        path = store.repairs_dir() / run_id / "reports" / name
        if not path.is_file():
            raise HTTPException(status_code=404, detail="Report not found.")
        return FileResponse(str(path), media_type="application/pdf",
                            filename=name)


def _coursework_title(coursework_id: str) -> str | None:
    from .. import db
    row = db.query_one("SELECT title FROM coursework WHERE id = ?",
                       (coursework_id,))
    return row["title"] if row else None


def blueprint_for_coursework(coursework_id: str) -> dict | None:
    """The AUTHORITATIVE mark-scheme structure for an assignment.

    Keyed by the CURRENT ``mark_schemes/<coursework_id>.pdf`` SHA-256 and its
    cached (teacher-verified) blueprint — never by whatever extraction
    happened to be cached when an old result was stored.
    """
    import hashlib

    from ..config import DATA_DIR

    scheme = Path(DATA_DIR) / "mark_schemes" / f"{coursework_id}.pdf"
    if not scheme.is_file():
        return None
    sha = hashlib.sha256(scheme.read_bytes()).hexdigest()
    path = Path(DATA_DIR) / "blueprints" / f"{sha}.json"
    if not path.is_file():
        return None
    wrapper = json.loads(path.read_text(encoding="utf-8"))
    blueprint = wrapper.get("blueprint") if isinstance(wrapper, dict) else None
    return blueprint if isinstance(blueprint, dict) else None
