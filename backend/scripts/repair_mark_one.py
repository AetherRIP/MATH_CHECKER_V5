"""Mark ONE student PDF with the corrected pipeline in an ISOLATED data dir.

Spawned by ``scripts/repair_assignment.py``. Nothing here can touch the
production database or any Google write surface: ``MC5_DATA_DIR`` points at a
throwaway directory seeded with config/secrets/blueprints/mark scheme, and
``[testing] only`` stays on. The result is dumped as JSON to ``--out``.

    python scripts/repair_mark_one.py --data-dir <isolated> \
        --student-name "<label>" --student-pdf <pdf> --mark-scheme <pdf> \
        --out <result.json>
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-dir", required=True)
    ap.add_argument("--student-name", required=True)
    ap.add_argument("--student-pdf", required=True)
    ap.add_argument("--mark-scheme", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--max-seconds", type=int, default=2400)
    args = ap.parse_args()

    # Isolation MUST happen before the app package is imported.
    os.environ["MC5_DATA_DIR"] = args.data_dir
    os.environ["MC5_LOGIN_REQUIRED"] = "0"
    sys.path.insert(0, str(BACKEND))

    from app import config, db
    from app.config import DATA_DIR

    db.init_db()
    assert Path(DATA_DIR).resolve() != (BACKEND.parent / "data").resolve(), \
        "refusing to mark against the production data dir"
    cfg = config.get_config(reload=True)
    assert cfg.testing.only, "testing-only mode must be ON for a repair run"
    if cfg.mock_mode:
        print("mock_mode is ON — refusing to present this as a real repair")
        return 2

    from app.large_pdf import states as lstates
    from app.large_pdf.runner import LargePdfRunnerHolder

    runner = LargePdfRunnerHolder.instance()
    job = runner.enqueue_manual(student_name=args.student_name,
                                student_pdf_path=args.student_pdf,
                                mark_scheme_path=args.mark_scheme)
    job_id = job["id"]
    print("job:", job_id, flush=True)
    deadline = time.time() + args.max_seconds
    last = None
    while time.time() < deadline:
        row = db.query_one("SELECT status, step_detail FROM large_pdf_jobs "
                           "WHERE id=?", (job_id,))
        cur = (row["status"], row["step_detail"])
        if cur != last:
            print(f"  [{time.strftime('%H:%M:%S')}] {cur[0]} — {cur[1]}",
                  flush=True)
            last = cur
        if row["status"] in lstates.TERMINAL:
            break
        runner.process(job_id)
        time.sleep(1.0)

    job = db.row_to_dict(db.query_one("SELECT * FROM large_pdf_jobs WHERE id=?",
                                      (job_id,)))
    result = None
    if job.get("result_id"):
        row = db.query_one("SELECT * FROM results WHERE id=?",
                           (job["result_id"],))
        result = db.row_to_dict(row)
        result["result_json"] = db.loads(result.get("result_json") or "{}")
        result["verification_json"] = db.loads(
            result.get("verification_json") or "{}")
    batches = [dict(b) for b in db.query(
        "SELECT batch_index, page_start, page_end, status, attempts, "
        "error_code, validation_json FROM large_pdf_batches WHERE job_id=? "
        "ORDER BY batch_index", (job_id,))]
    recovery = db.loads(job.get("recovery_json") or "{}") or {}

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({
        "student_name": args.student_name,
        "job_id": job_id,
        "status": job.get("status"),
        "error": job.get("error"),
        "recovery": recovery,
        "batches": batches,
        "result": result,
    }, ensure_ascii=False, default=str), encoding="utf-8")
    print("wrote", out)
    return 0 if result else 1


if __name__ == "__main__":
    raise SystemExit(main())
