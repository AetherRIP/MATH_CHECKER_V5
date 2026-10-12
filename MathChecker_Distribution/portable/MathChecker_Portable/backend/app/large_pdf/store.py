"""Persistent store for the Large PDF Worker.

Everything a resumable long-document correction needs lives in SQLite:

* ``large_pdf_jobs``    — one row per student-answer-PDF version;
* ``large_pdf_batches``  — one row per batch, with its own state and result.

Claims are atomic UPDATEs guarded by the previous status, so two runners can
never mark the same batch simultaneously. Nothing is held only in memory: a
browser refresh or an application restart cannot lose or repeat work.
"""

from __future__ import annotations

import time
import uuid

from .. import db
from . import states


# ------------------------------------------------------------------ jobs

def get_job(job_id: str) -> dict | None:
    row = db.query_one("SELECT * FROM large_pdf_jobs WHERE id = ?", (job_id,))
    return dict(row) if row else None


def find_job(coursework_id: str | None, submission_id: str | None,
             processing_version: str) -> dict | None:
    """An existing job for the same submission VERSION (idempotency)."""
    if coursework_id and submission_id:
        row = db.query_one(
            "SELECT * FROM large_pdf_jobs WHERE coursework_id = ? "
            "AND submission_id = ? AND processing_version = ?",
            (coursework_id, submission_id, processing_version))
        return dict(row) if row else None
    row = db.query_one(
        "SELECT * FROM large_pdf_jobs WHERE coursework_id IS NULL "
        "AND submission_id IS NULL AND processing_version = ?",
        (processing_version,))
    return dict(row) if row else None


def find_active_for_submission(coursework_id: str | None,
                               submission_id: str | None) -> dict | None:
    """Any moveable job for a submission (duplicate-launch protection)."""
    if not submission_id:
        return None
    placeholders = ",".join("?" * len(states.IN_FLIGHT))
    row = db.query_one(
        f"SELECT * FROM large_pdf_jobs WHERE coursework_id = ? "
        f"AND submission_id = ? AND status IN ({placeholders}) "
        f"ORDER BY created_at DESC LIMIT 1",
        (coursework_id, submission_id, *states.IN_FLIGHT))
    return dict(row) if row else None


def create_job(*, submission_id: str | None, coursework_id: str | None,
               course_id: str | None, student_id: str | None,
               student_name: str | None, student_pdf_path: str,
               mark_scheme_path: str, processing_version: str,
               student_pdf_sha: str | None = None,
               mark_scheme_sha: str | None = None,
               max_attempts: int = 3, job_id: str | None = None) -> dict:
    """Create a job row. The partial unique index makes this idempotent for
    Classroom submissions: a racing insert for the same version returns the
existing row instead of duplicating work.

    ``job_id`` lets the router pin a delegated long-document job to the SAME id
    as the correction job it was routed from, so one submission keeps one job
    row and one result all the way through the approval pipeline.
    """
    job_id = job_id or uuid.uuid4().hex
    now_ts = time.time()
    try:
        with db.transaction() as conn:
            conn.execute(
                """
                INSERT INTO large_pdf_jobs(
                    id, submission_id, coursework_id, course_id, student_id,
                    student_name, student_pdf_path, mark_scheme_path,
                    processing_version, student_pdf_sha, mark_scheme_sha,
                    status, step_detail, attempts, max_attempts,
                    created_at, updated_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,0,?,?,?)
                """,
                (job_id, submission_id, coursework_id, course_id, student_id,
                 student_name, student_pdf_path, mark_scheme_path,
                 processing_version, student_pdf_sha, mark_scheme_sha,
                 states.QUEUED, "Preparing document", max_attempts,
                 now_ts, now_ts),
            )
    except Exception:
        existing = find_job(coursework_id, submission_id, processing_version)
        if existing:
            return existing
        raise
    return get_job(job_id)


#: How a Large PDF Worker state reads on the shared ``correction_jobs`` row.
#: QUEUED is deliberately avoided — that status is claimable by the normal
#: JobManager, and a delegated job must never be re-claimed by it.
_CORRECTION_STATUS = {
    states.QUEUED: "PREPARING",
    states.INSPECTING: "PREPARING",
    states.PLANNING: "PREPARING",
    states.READY: "PREPARING",
    states.PROCESSING: "PROCESSING",
    states.VALIDATING: "PROCESSING",
    states.MERGING: "PROCESSING",
    states.AWAITING_REVIEW: "COMPLETED",
    states.COMPLETED: "COMPLETED",
    states.FAILED: "FAILED",
    states.CANCELLED: "CANCELLED",
}
_TERMINAL_STATUSES = (states.COMPLETED, states.AWAITING_REVIEW, states.FAILED,
                      states.CANCELLED)


def _mirror_correction_job(job_id: str, status: str, detail: str | None,
                           fields: dict) -> None:
    """Keep a DELEGATED correction job's row live.

    A long document routed from the normal pipeline shares its id with a
    ``correction_jobs`` row (same id). Mirroring progress here is what makes the
    Corrections page and every existing surface show the large job's real state
    without a second source of truth. Standalone Large PDF Worker uploads have
    no such row until finalize, so this is a no-op for them.
    """
    mapped = _CORRECTION_STATUS.get(status)
    if mapped is None:
        return
    row = db.query_one("SELECT id FROM correction_jobs WHERE id = ?", (job_id,))
    if row is None:
        return
    now_ts = time.time()
    sets = ["status = ?", "updated_at = ?"]
    params: list = [mapped, now_ts]
    if detail is not None:
        sets.append("stage_detail = ?")
        params.append(detail)
    if "result_id" in fields and fields["result_id"]:
        sets.append("result_id = ?")
        params.append(fields["result_id"])
    if status == states.FAILED:
        sets.append("error = ?")
        params.append(fields.get("error") or "Large PDF Worker failed")
        sets.append("error_code = ?")
        params.append(fields.get("error_code") or "large_pdf_failed")
    if status in _TERMINAL_STATUSES:
        sets.append("finished_at = COALESCE(finished_at, ?)")
        params.append(now_ts)
    params.append(job_id)
    db.execute(f"UPDATE correction_jobs SET {', '.join(sets)} WHERE id = ?",
               params)


def set_status(job_id: str, status: str, *, detail: str | None = None,
               **fields) -> dict | None:
    sets = ["status = ?", "updated_at = ?"]
    params: list = [status, time.time()]
    if detail is not None:
        sets.append("step_detail = ?")
        params.append(detail)
    for key in ("page_count", "batch_count", "plan_json", "result_id", "error",
                "error_code", "started_at", "finished_at"):
        if key in fields and fields[key] is not None:
            sets.append(f"{key} = ?")
            params.append(fields[key])
    if status == states.FAILED and "finished_at" not in fields:
        sets.append("finished_at = ?")
        params.append(time.time())
    if status == states.COMPLETED and "finished_at" not in fields:
        sets.append("finished_at = ?")
        params.append(time.time())
    if status not in (states.FAILED,) and "error_code" not in fields:
        sets.append("error = NULL")
        sets.append("error_code = NULL")
    params.append(job_id)
    db.execute(f"UPDATE large_pdf_jobs SET {', '.join(sets)} WHERE id = ?",
               params)
    _mirror_correction_job(job_id, status, detail, fields)
    return get_job(job_id)


def claim_job(job_id: str, *, worker: str = "large-pdf-runner") -> dict | None:
    """Atomically move QUEUED -> INSPECTING. ``None`` if another runner won."""
    now_ts = time.time()
    with db.transaction() as conn:
        cur = conn.execute(
            "UPDATE large_pdf_jobs SET status = ?, claimed_by = ?, "
            "claimed_at = ?, attempts = attempts + 1, "
            "started_at = COALESCE(started_at, ?), updated_at = ? "
            "WHERE id = ? AND status = ?",
            (states.INSPECTING, worker, now_ts, now_ts, now_ts, job_id,
             states.QUEUED),
        )
        if cur.rowcount == 0:
            return None
    return get_job(job_id)


def requeue(job_id: str) -> None:
    """Return a claimed-but-unfinished job to QUEUED (boot recovery)."""
    db.execute(
        "UPDATE large_pdf_jobs SET status = ?, claimed_by = NULL, "
        "claimed_at = NULL, updated_at = ? WHERE id = ?",
        (states.QUEUED, time.time(), job_id))


def list_jobs(*, statuses: list[str] | None = None, limit: int = 200) -> list[dict]:
    where, params = [], []
    if statuses:
        where.append("status IN (%s)" % ",".join("?" * len(statuses)))
        params.extend(statuses)
    clause = ("WHERE " + " AND ".join(where)) if where else ""
    rows = db.query(
        f"SELECT * FROM large_pdf_jobs {clause} ORDER BY updated_at DESC LIMIT ?",
        (*params, limit))
    return [dict(r) for r in rows]


def counts() -> dict:
    rows = db.query("SELECT status, COUNT(*) AS n FROM large_pdf_jobs GROUP BY status")
    return {r["status"]: r["n"] for r in rows}


def in_flight_jobs() -> list[dict]:
    placeholders = ",".join("?" * len(states.IN_FLIGHT))
    rows = db.query(
        f"SELECT * FROM large_pdf_jobs WHERE status IN ({placeholders})",
        states.IN_FLIGHT)
    return [dict(r) for r in rows]


# --------------------------------------------------------------- batches

def get_batch(job_id: str, batch_index: int) -> dict | None:
    row = db.query_one(
        "SELECT * FROM large_pdf_batches WHERE job_id = ? AND batch_index = ?",
        (job_id, batch_index))
    return dict(row) if row else None


def list_batches(job_id: str) -> list[dict]:
    rows = db.query(
        "SELECT * FROM large_pdf_batches WHERE job_id = ? ORDER BY batch_index",
        (job_id,))
    return [dict(r) for r in rows]


def create_batches(job_id: str, batches: list[dict]) -> None:
    """Persist the complete batch plan (called ONCE, at planning time)."""
    now_ts = time.time()
    import json
    with db.transaction() as conn:
        for batch in batches:
            conn.execute(
                """
                INSERT INTO large_pdf_batches(
                    id, job_id, batch_index, page_start, page_end,
                    context_before, context_after, question_ids, status,
                    attempts, max_attempts, created_at, updated_at)
                VALUES(?,?,?,?,?,?,?,?,?,0,?,?,?)
                ON CONFLICT(job_id, batch_index) DO NOTHING
                """,
                (uuid.uuid4().hex, job_id, int(batch["index"]),
                 int(batch["start"]), int(batch["end"]),
                 int(batch.get("context_before", 0)),
                 int(batch.get("context_after", 0)),
                 json.dumps(batch.get("question_ids", [])),
                 states.BATCH_PENDING,
                 int(batch.get("max_attempts", 3)), now_ts, now_ts),
            )


def claim_batch(job_id: str, batch_index: int,
                *, worker: str = "large-pdf-runner") -> dict | None:
    """Atomically claim a PENDING batch. ``None`` if already claimed/done.

    This is the concurrency guard: two workers cannot mark the same batch.
    """
    now_ts = time.time()
    with db.transaction() as conn:
        cur = conn.execute(
            "UPDATE large_pdf_batches SET status = ?, claimed_by = ?, "
            "claimed_at = ?, attempts = attempts + 1, updated_at = ? "
            "WHERE job_id = ? AND batch_index = ? AND status = ?",
            (states.BATCH_PROCESSING, worker, now_ts, now_ts, job_id,
             batch_index, states.BATCH_PENDING),
        )
        if cur.rowcount == 0:
            return None
    return get_batch(job_id, batch_index)


def complete_batch(job_id: str, batch_index: int, *, result: dict | None,
                   validation: dict | None, status: str) -> None:
    import json
    db.execute(
        "UPDATE large_pdf_batches SET status = ?, result_json = ?, "
        "validation_json = ?, error = NULL, error_code = NULL, "
        "finished_at = ?, updated_at = ? WHERE job_id = ? AND batch_index = ?",
        (status,
         json.dumps(result, ensure_ascii=False) if result is not None else None,
         json.dumps(validation, ensure_ascii=False) if validation is not None
         else None,
         time.time(), time.time(), job_id, batch_index),
    )


def fail_batch(job_id: str, batch_index: int, *, error: str,
               error_code: str | None = None) -> None:
    db.execute(
        "UPDATE large_pdf_batches SET status = ?, error = ?, error_code = ?, "
        "updated_at = ? WHERE job_id = ? AND batch_index = ?",
        (states.BATCH_FAILED, error[:800], error_code, time.time(), job_id,
         batch_index),
    )


def reset_batch(job_id: str, batch_index: int) -> None:
    """Return a FAILED batch to PENDING for an independent retry."""
    db.execute(
        "UPDATE large_pdf_batches SET status = ?, claimed_by = NULL, "
        "claimed_at = NULL, error = NULL, error_code = NULL, updated_at = ? "
        "WHERE job_id = ? AND batch_index = ? AND status = ?",
        (states.BATCH_PENDING, time.time(), job_id, batch_index,
         states.BATCH_FAILED),
    )


def recover_in_flight() -> dict:
    """Restart-safety: requeue jobs and batches interrupted by a crash."""
    now_ts = time.time()
    jobs = db.execute(
        "UPDATE large_pdf_jobs SET status = ?, claimed_by = NULL, "
        "claimed_at = NULL, updated_at = ? WHERE status IN (%s)"
        % ",".join("?" * len(states.IN_FLIGHT)),
        (states.QUEUED, now_ts, *states.IN_FLIGHT))
    batches = db.execute(
        "UPDATE large_pdf_batches SET status = ?, claimed_by = NULL, "
        "claimed_at = NULL, updated_at = ? WHERE status = ?",
        (states.BATCH_PENDING, now_ts, states.BATCH_PROCESSING))
    return {"jobs_requeued": jobs.rowcount if jobs else 0,
            "batches_requeued": batches.rowcount if batches else 0}


def summary(job_id: str) -> dict:
    """Batch-state rollup for the dashboard."""
    rows = list_batches(job_id)
    counts: dict[str, int] = {}
    for row in rows:
        counts[row["status"]] = counts.get(row["status"], 0) + 1
    return {
        "total": len(rows),
        "pending": counts.get(states.BATCH_PENDING, 0),
        "processing": counts.get(states.BATCH_PROCESSING, 0),
        "completed": counts.get(states.BATCH_COMPLETED, 0),
        "failed": counts.get(states.BATCH_FAILED, 0),
        "review": counts.get(states.BATCH_REVIEW_REQUIRED, 0)
        + counts.get(states.BATCH_VERIFICATION_REQUIRED, 0),
    }
