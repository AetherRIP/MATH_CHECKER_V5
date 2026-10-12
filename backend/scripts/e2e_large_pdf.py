"""Real end-to-end test of the Large PDF Worker on a REAL submission.

Runs the actual correction engine (real Gemini keys) on a long student answer
PDF + its official mark scheme, through the exact Large PDF Worker path:

    enqueue -> inspect -> plan -> mark each batch -> validate -> merge

It writes to an ISOLATED scratch data directory (keys copied from the app's
secret store, nothing else) so the live database/corrections are untouched.

Usage:
    python scripts/e2e_large_pdf.py "<student.pdf>" "<mark_scheme.pdf>" [name]
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import time
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

student = sys.argv[1] if len(sys.argv) > 1 else ""
scheme = sys.argv[2] if len(sys.argv) > 2 else ""
name = sys.argv[3] if len(sys.argv) > 3 else "Yousef Tamer"
if not student or not scheme:
    raise SystemExit("usage: e2e_large_pdf.py <student.pdf> <mark_scheme.pdf> [name]")

# Dev-only scratch lives under dev_local/ so the project root stays clean;
# override with LP_E2E_DATA. Never inside data/ (that tree is the real app data).
scratch = Path(os.environ.get(
    "LP_E2E_DATA", str(BACKEND.parent / "dev_local" / "runs" / "largepdf_e2e")))
scratch.mkdir(parents=True, exist_ok=True)
real_secrets = BACKEND.parent / "data" / "secrets.toml"
if real_secrets.exists() and not (scratch / "secrets.toml").exists():
    shutil.copy2(real_secrets, scratch / "secrets.toml")
os.environ["MC5_DATA_DIR"] = str(scratch)

import app.config as config  # noqa: E402
from app import db  # noqa: E402

config.get_config().mock_mode = False
db.init_db()

from app.large_pdf import states, store  # noqa: E402
from app.large_pdf.runner import LargePdfRunner  # noqa: E402

print(f"[e2e] data dir: {scratch}", flush=True)
print(f"[e2e] keys: {len(config.get_secrets().gemini_keys())}", flush=True)

runner = LargePdfRunner()
job = runner.enqueue_manual(student_name=name, student_pdf_path=student,
                           mark_scheme_path=scheme)
print(f"[e2e] job: {job['id']}", flush=True)

t0 = time.time()
prepared = runner.prepare(job["id"])
plan_raw = json.loads(prepared.get("plan_json") or "{}")
plan = plan_raw.get("plan", {})
print(f"[e2e] prepared: status={prepared['status']} pages={prepared['page_count']} "
      f"batches={prepared['batch_count']}", flush=True)
print(f"[e2e] plan: {[(b['start'], b['end']) for b in plan.get('batches', [])]}",
      flush=True)
print(f"[e2e] mark-scheme questions detected: "
      f"{len(plan_raw.get('scheme_questions') or [])}", flush=True)
if prepared["status"] == states.FAILED:
    print(f"[e2e] INSPECTION FAILED: {prepared.get('error')}", flush=True)
    raise SystemExit(1)

final = runner.process(job["id"])
elapsed = time.time() - t0
print(f"[e2e] FINAL status={final['status']} detail={final['step_detail']} "
      f"result_id={final['result_id']} elapsed={elapsed:.0f}s", flush=True)

for b in store.list_batches(job["id"]):
    validation = db.loads(b.get("validation_json"), {}) or {}
    print(f"[e2e]   batch {b['batch_index'] + 1}: pages {b['page_start']}-{b['page_end']} "
          f"status={b['status']} questions={validation.get('question_count')} "
          f"missing={validation.get('missing_questions')} "
          f"error={b.get('error')}", flush=True)

if final.get("result_id"):
    row = db.query_one("SELECT * FROM results WHERE id = ?", (final["result_id"],))
    if row:
        result = db.loads(row["result_json"], {}) or {}
        verification = db.loads(row["verification_json"], {}) or {}
        qs = result.get("questions", [])
        print(f"[e2e] RESULT earned={row['earned']}/{row['available']} "
              f"({row['percentage']:.2f}%) grade={row['provisional_grade']} "
              f"review_required={row['review_required']} "
              f"confidence={row['confidence']}", flush=True)
        print(f"[e2e]   questions={len(qs)} verification={verification.get('status')} "
              f"missing={verification.get('blueprint', {}) and verification['blueprint'].get('missing_questions')}",
              flush=True)
        for q in qs[:8]:
            print(f"[e2e]   Q{q['question_number']}: {q['awarded_marks']}/{q['max_marks']} "
                  f"status={q.get('status')}", flush=True)
print("[e2e] done", flush=True)
