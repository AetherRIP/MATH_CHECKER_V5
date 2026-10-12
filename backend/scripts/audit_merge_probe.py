"""Replay REAL stored batch results through the merge/reconcile path.

Deterministic (no AI). Proves where the phantom 'missing questions' come from.

Reads the isolated audit test DB produced by scripts/audit_run.py.
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
ROOT = BACKEND.parent
sys.path.insert(0, str(BACKEND))

TEST_DATA = Path(os.environ.get("MC5_AUDIT_DATA_DIR",
                                Path(os.environ.get("TEMP", "/tmp")) / "mc5_audit_test"))
os.environ["MC5_DATA_DIR"] = str(TEST_DATA)

from app import db  # noqa: E402
from app.engine.blueprint import apply_blueprint, blueprint_for_mark_scheme  # noqa: E402
from app.engine import batching  # noqa: E402  (plan reconstruction)
from app.large_pdf.validate import merge_batches  # noqa: E402

job = db.row_to_dict(db.query_one(
    "SELECT * FROM large_pdf_jobs ORDER BY created_at DESC LIMIT 1"))
job_id = job["id"]
print("job:", job_id, "status:", job["status"])
print("recovery_json:", job.get("recovery_json"))

plan_raw = job.get("plan_json")
print("plan_json:", str(plan_raw)[:200])
plan = None
if plan_raw:
    try:
        blob = json.loads(plan_raw)
        blob = blob.get("plan", blob)
        plan = batching.BatchPlan.from_dict(blob)
    except Exception as exc:  # noqa: BLE001
        print("plan_json is not a BatchPlan dict:", exc)
if plan is None:
    # Fall back to rebuilding the plan from the persisted batch rows.
    rows = [dict(b) for b in db.query(
        "SELECT batch_index,page_start,page_end,question_ids FROM large_pdf_batches "
        "WHERE job_id=? ORDER BY batch_index", (job_id,))]
    print("plan_json empty -> rebuilding a plan stub from batch rows")
    plan = batching.BatchPlan(
        page_count=int(job["page_count"]), single_pass=False,
        batches=[batching.Batch(index=r["batch_index"], start=r["page_start"],
                                end=r["page_end"],
                                question_ids=tuple(json.loads(r["question_ids"] or "[]")))
                 for r in rows],
    )

batch_rows = [dict(b) for b in db.query(
    "SELECT batch_index,question_ids,result_json FROM large_pdf_batches "
    "WHERE job_id=? ORDER BY batch_index", (job_id,))]
batches = [dict(b) for b in db.query(
    "SELECT * FROM large_pdf_batches WHERE job_id=? ORDER BY batch_index", (job_id,))]
batch_results = {}
for b in batch_rows:
    payload = json.loads(b["result_json"]) if b["result_json"] else None
    if payload is not None:
        batch_results[int(b["batch_index"])] = payload
        print(f"  batch {b['batch_index']}: {len(payload.get('questions') or [])} rows from the model")

blueprint, meta = blueprint_for_mark_scheme(job["mark_scheme_path"])
bp_questions = (blueprint or {}).get("questions")
print("blueprint source:", meta.get("source"), "| questions:", len(bp_questions or []))

merged = merge_batches(plan, batches, batch_results, blueprint_questions=bp_questions)
rep = merged["report"]
print("\n--- merge_batches report ---")
print("merged_questions:", rep["merged_questions"])
print("missing_questions:", len(rep["missing_questions"]), rep["missing_questions"])
print("duplicate_questions:", len(rep.get("duplicate_questions") or []))
print("conflicts:", len(rep["conflicts"]))
print("expected_questions:", len(rep.get("expected_questions") or []))
print("needs_review:", merged["needs_review"])

result, bp_report = apply_blueprint(merged["result"], blueprint)
print("\n--- apply_blueprint (authoritative) report ---")
print("missing_questions:", len(bp_report["missing_questions"]), bp_report["missing_questions"])
print("extra_questions:", bp_report["extra_questions"])
print("max_marks_corrected:", len(bp_report["max_marks_corrected"]))
print("total questions in result:", len(result.get("questions") or []))
