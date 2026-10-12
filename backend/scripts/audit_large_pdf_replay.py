"""Replay a REAL Large PDF job's stored batch results through the current merge
pipeline — read-only, offline, no API calls.

Purpose: prove, on a live submission, what the marker actually returned per
batch, which questions were never marked, and what the score SHOULD be under
the corrected (marked-only) accounting. The live database is opened READ-ONLY
and nothing is written back to it.

    python scripts/audit_large_pdf_replay.py            # newest large job
    python scripts/audit_large_pdf_replay.py <job_id>

Writes ``data/audit_runs/large_pdf_replay_<job>.json`` (test artifact) and
prints a short summary. No student names, no answers, no grades are printed
beyond the internal audit record.
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

DATA = Path(os.environ.get("MC5_DATA_DIR") or (BACKEND.parent / "data")).resolve()
DB = DATA / "app.db"
OUT_DIR = DATA / "audit_runs"


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _load_blueprint(sha: str | None) -> dict | None:
    if not sha:
        return None
    path = DATA / "blueprints" / f"{sha}.json"
    if not path.is_file():
        return None
    wrapper = json.loads(path.read_text(encoding="utf-8"))
    blueprint = wrapper.get("blueprint") if isinstance(wrapper, dict) else None
    return blueprint if isinstance(blueprint, dict) else None


def main() -> int:
    from app.engine.blueprint import apply_blueprint
    from app.engine.verify import verify_result
    from app.engine import batching
    from app.engine.recover_missing import (
        MAXMISS_IN_PROMPT, need_recovery, plan_chunked_recovery,
    )
    from app.large_pdf.validate import (
        batch_coverage, merge_batches, scheme_total_marks, score_breakdown,
        totals_for,
    )

    wanted = sys.argv[1] if len(sys.argv) > 1 else None
    with _connect() as conn:
        if wanted:
            job = conn.execute("SELECT * FROM large_pdf_jobs WHERE id = ?",
                               (wanted,)).fetchone()
        else:
            job = conn.execute(
                "SELECT * FROM large_pdf_jobs ORDER BY created_at DESC "
                "LIMIT 1").fetchone()
        if job is None:
            print("no large PDF job found")
            return 2
        batches = conn.execute(
            "SELECT * FROM large_pdf_batches WHERE job_id = ? "
            "ORDER BY batch_index", (job["id"],)).fetchall()
        result_row = None
        if job["result_id"]:
            result_row = conn.execute("SELECT * FROM results WHERE id = ?",
                                      (job["result_id"],)).fetchone()
        assignment = None
        if job["coursework_id"]:
            assignment = conn.execute(
                "SELECT title FROM coursework WHERE id = ?",
                (job["coursework_id"],)).fetchone()

    plan_dict = (json.loads(job["plan_json"]) or {}).get("plan")
    plan = batching.BatchPlan.from_dict(plan_dict) if plan_dict else None
    blueprint = _load_blueprint(job["mark_scheme_sha"])

    batch_dicts = [dict(b) for b in batches]
    for b in batch_dicts:
        b["question_ids"] = b.get("question_ids") or "[]"
    batch_results = {}
    for b in batch_dicts:
        payload = json.loads(b.get("result_json") or "null")
        if payload:
            batch_results[int(b["batch_index"])] = payload

    report: dict = {
        "job_id": job["id"],
        "coursework_id": job["coursework_id"],
        "coursework_title": (assignment["title"] if assignment else None),
        "page_count": job["page_count"],
        "batch_count": job["batch_count"],
        "mark_scheme_sha": job["mark_scheme_sha"],
        "blueprint_available": blueprint is not None,
        "blueprint_questions": len((blueprint or {}).get("questions") or []),
        "batches": [],
    }
    scheme_marks = scheme_total_marks(blueprint)
    for b in batch_dicts:
        payload = batch_results.get(int(b["batch_index"])) or {}
        rows = payload.get("questions") or []
        coverage = batch_coverage(payload, b, paper_pages=job["page_count"],
                                  scheme_total_marks=scheme_marks)
        report["batches"].append({
            "batch": b["batch_index"],
            "pages": [b["page_start"], b["page_end"]],
            "status": b["status"],
            "attempts": b["attempts"],
            "returned_questions": len(rows),
            "returned_marks": sum(int(q.get("max_marks") or 0) for q in rows),
            "coverage": coverage,
            "question_labels": [str(q.get("question_number")) for q in rows],
        })

    merged = merge_batches(plan, batch_dicts, batch_results,
                           blueprint_questions=(blueprint or {}).get("questions"))
    result = merged["result"]
    result, blueprint_report = apply_blueprint(result, blueprint)
    verification = verify_result(result, blueprint_report)
    old_earned, old_available, old_percentage = totals_for(result)
    breakdown = score_breakdown(result)

    report["merge"] = {
        "merged_questions": merged["report"]["merged_questions"],
        "advisory_missing": merged["report"]["missing_questions"],
        "thin_batches": merged["report"]["thin_batches"],
        "cross_owned": merged["report"]["cross_owned"],
        "conflicts": merged["report"]["conflicts"],
        "needs_review": merged["needs_review"],
    }
    report["blueprint_report"] = {
        "applied": bool(blueprint_report.get("applied")),
        "missing_questions": blueprint_report.get("missing_questions") or [],
        "max_marks_corrected": blueprint_report.get("max_marks_corrected") or [],
        "source": blueprint_report.get("source"),
    }
    # What the recovery path now does for this gap (and what it used to do):
    # the same real gap is re-marked in bounded chunks instead of being refused
    # outright because of its size.
    missing_now = list(blueprint_report.get("missing_questions") or [])
    chunk_size = 6
    max_chunks = 8
    try:
        from app.config import get_config as _get_config
        _cfg = _get_config().large_pdf
        chunk_size = int(_cfg.recovery_chunk_questions)
        max_chunks = int(_cfg.recovery_max_chunks)
    except Exception:  # noqa: BLE001 — the defaults mirror the shipped config
        pass
    plan = plan_chunked_recovery(
        missing=missing_now, chunk_size=chunk_size, max_chunks=max_chunks,
        scheme_questions=len((blueprint or {}).get("questions") or []) or None)
    legacy_may_retry, legacy_reason = need_recovery(
        missing=missing_now, already_attempted=0)
    report["recovery"] = {
        "missing_questions": missing_now,
        "chunk_size": chunk_size,
        "max_chunks": max_chunks,
        "chunks": [len(chunk.questions) for chunk in plan.chunks],
        "chunks_planned": len(plan.chunks),
        "deferred": plan.deferred,
        "blocked_reason": plan.blocked_reason,
        "previous_single_round_allowed": legacy_may_retry,
        "previous_single_round_reason": legacy_reason,
        "note": (
            "The gap is re-marked in bounded chunks of at most "
            f"{chunk_size} questions ({len(plan.chunks)} request(s) planned, "
            f"ceiling {max_chunks}). Every question that survives the retries "
            "keeps that question's explicit unresolved reason."),
    }
    report["score_old_asis"] = {
        "earned": old_earned, "available": old_available,
        "percentage": round(old_percentage, 2),
        "note": "unmarked questions divided into the total (the reported 46.6%)",
    }
    report["score_marked_only"] = {
        "earned": breakdown["earned"],
        "available": breakdown["available"],
        "percentage": round(breakdown["percentage"], 2),
        "paper_available": breakdown["paper_available"],
        "unresolved_questions": breakdown["unresolved_questions"],
        "unresolved_count": breakdown["unresolved_count"],
        "unresolved_marks": breakdown["unresolved_marks"],
        "score_basis": breakdown["score_basis"],
    }
    if result_row is not None:
        stored = json.loads(result_row["result_json"] or "{}")
        report["stored_result"] = {
            "earned": result_row["earned"],
            "available": result_row["available"],
            "percentage": round(result_row["percentage"], 2),
            "review_required": result_row["review_required"],
            "questions_stored": len(stored.get("questions") or []),
        }

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out = OUT_DIR / f"large_pdf_replay_{job['id']}.json"
    out.write_text(json.dumps(report, indent=1, ensure_ascii=False),
                   encoding="utf-8")

    print(f"job {report['job_id']}  {report['page_count']} pages / "
          f"{report['batch_count']} batches  (blueprint: "
          f"{report['blueprint_questions']} questions)")
    for b in report["batches"]:
        coverage = b.get("coverage") or {}
        print(f"  batch {b['batch']}: pages {b['pages'][0]}-{b['pages'][1]}  "
              f"status={b['status']} attempts={b['attempts']}  "
              f"returned {b['returned_questions']} rows / "
              f"{b['returned_marks']} marks"
              + (f"  [expected ~{coverage.get('expected_marks')} marks, "
                 f"ratio {coverage.get('ratio')}"
                 f"{' — INCOMPLETE' if coverage.get('stub') else ''}]"
                 if coverage else ""))
    print(f"  thin batches (unbalanced responses): "
          f"{[t['batch'] for t in report['merge']['thin_batches']]}")
    print(f"  mark scheme missing after reconciliation: "
          f"{len(report['blueprint_report']['missing_questions'])} questions")
    recovery = report["recovery"]
    print(f"  retry plan: {recovery['chunks_planned']} chunk(s) "
          f"{recovery['chunks']} of max {recovery['chunk_size']} questions, "
          f"deferred {len(recovery['deferred'])}"
          + (f", blocked: {recovery['blocked_reason'][:80]}"
             if recovery["blocked_reason"] else "")
          + ("" if not recovery["missing_questions"] else
             "   [previous single-round gate: ALLOWED]"
             if recovery["previous_single_round_allowed"]
             else "   [previous single-round gate: REFUSED - the defect]"))
    print(f"  as-reported score : {report['score_old_asis']['earned']}"
          f"/{report['score_old_asis']['available']} = "
          f"{report['score_old_asis']['percentage']}%")
    print(f"  marked-only score : {report['score_marked_only']['earned']}"
          f"/{report['score_marked_only']['available']} = "
          f"{report['score_marked_only']['percentage']}%  "
          f"(unresolved {report['score_marked_only']['unresolved_count']} "
          f"questions / {report['score_marked_only']['unresolved_marks']} marks)")
    print(f"  -> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
