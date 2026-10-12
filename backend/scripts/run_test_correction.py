"""Run the app's REAL correction for the test submission.

Uses the production path end to end: sync -> enqueue -> worker pool (which
downloads the student's work, marks it with Gemini, validates and stores the
result). Nothing is mocked. Then the caller can attach the report.

Prints IDs and counts only - no student names, emails or report contents.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))

COURSE_ID = "886515702341"
COURSEWORK_ID = "873785733817"
SUBMISSION_ID = "Cg4IqOb1jpESELnlwI23GQ"
TIMEOUT_SECONDS = 1500


def main() -> int:
    from app import db
    from app.classroom import sync as classroom_sync
    db.init_db()

    print("[run] syncing coursework + submissions")
    classroom_sync.sync_coursework(COURSE_ID, force=True)
    classroom_sync.sync_submissions(COURSE_ID, COURSEWORK_ID, force=True)

    existing = db.query_one(
        "SELECT * FROM results WHERE coursework_id = ? AND submission_id = ?",
        (COURSEWORK_ID, SUBMISSION_ID))
    if existing:
        print(f"[run] result already exists: {existing['id']}")
        return 0

    from app.engine.service import CorrectionService
    from app.jobs.manager import JobManagerHolder
    from app.workers.pool import WorkerPoolHolder

    job = JobManagerHolder.instance().enqueue(
        submission_id=SUBMISSION_ID, coursework_id=COURSEWORK_ID,
        course_id=COURSE_ID, student_id=None, student_name=None)
    job_id = job.get("id")
    print(f"[run] enqueued job={job_id}")

    pool = WorkerPoolHolder.instance()
    pool.start()
    deadline = time.time() + TIMEOUT_SECONDS
    last = ""
    try:
        while time.time() < deadline:
            row = db.query_one("SELECT status, error_code, stage_detail, "
                               "attempts FROM correction_jobs WHERE id = ?",
                               (job_id,))
            if row:
                state = (f"{row['status']} error={row['error_code']} "
                         f"stage={str(row['stage_detail'])[:60]}")
                if state != last:
                    print(f"[run] {state}")
                    last = state
                if row["status"] in ("COMPLETED", "FAILED", "CANCELLED"):
                    break
            time.sleep(3)
    finally:
        pool.shutdown(drain=False)

    result = db.query_one(
        "SELECT * FROM results WHERE coursework_id = ? AND submission_id = ?",
        (COURSEWORK_ID, SUBMISSION_ID))
    if result:
        print(f"\n[run] RESULT id={result['id']}")
        print(f"[run] earned={result['earned']}/{result['available']} "
              f"({result['percentage']:.1f}%) grade={result['provisional_grade']}")
        print(f"[run] marking_model={result['marking_model']} "
              f"submission_id={result['submission_id']}")
        return 0
    print("[run] no result produced (see job status above)")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())