"""Persistent store for the automation layer (spec §12, §42, §43, §44).

Everything the workflow needs to resume lives in SQLite, never in an
in-memory queue:

* ``automation_assignments`` — which assignments are monitored (§3, §46).
* ``automation_jobs``       — one persistent job per submission VERSION.
* ``automation_log``        — append-only recent-activity feed (§44).

Concurrency (§42) is handled with an atomic claim: a job is only moved out of
an unclaimed state by an UPDATE that requires the previous status to still
match, so a second worker sees rowcount 0 and leaves it alone.
"""

from __future__ import annotations

import logging
import time
import uuid

from .. import db
from ..logging_setup import log_event
from . import states

log = logging.getLogger("automation.store")


# ------------------------------------------------------------- assignments

def set_assignment_enabled(coursework_id: str, course_id: str, enabled: bool) -> dict:
    """Turn Automatic Correction on/off for ONE assignment (§3, §46)."""
    now_ts = time.time()
    db.execute(
        """
        INSERT INTO automation_assignments(
            coursework_id, course_id, enabled, created_at, updated_at)
        VALUES(?,?,?,?,?)
        ON CONFLICT(coursework_id) DO UPDATE SET
            course_id = excluded.course_id,
            enabled = excluded.enabled,
            updated_at = excluded.updated_at
        """,
        (coursework_id, course_id, 1 if enabled else 0, now_ts, now_ts),
    )
    log_event("automation", "assignment automation toggled",
              coursework_id=coursework_id, enabled=bool(enabled))
    return assignment(coursework_id) or {}


def assignment(coursework_id: str) -> dict | None:
    row = db.query_one(
        "SELECT * FROM automation_assignments WHERE coursework_id = ?",
        (coursework_id,))
    return dict(row) if row else None


def enabled_assignments() -> list[dict]:
    """Only assignments the teacher explicitly enabled (§3)."""
    rows = db.query(
        "SELECT * FROM automation_assignments WHERE enabled = 1 "
        "ORDER BY updated_at DESC")
    return [dict(r) for r in rows]


def all_assignments() -> list[dict]:
    rows = db.query(
        "SELECT * FROM automation_assignments ORDER BY updated_at DESC")
    return [dict(r) for r in rows]


def touch_assignment_check(coursework_id: str, *, error: str | None = None) -> None:
    db.execute(
        "UPDATE automation_assignments SET last_checked_at = ?, last_error = ? "
        "WHERE coursework_id = ?",
        (time.time(), error, coursework_id))


def enabled_map() -> dict[str, dict]:
    return {row["coursework_id"]: row for row in all_assignments()}


# ------------------------------------------------------------- grade boundaries

def set_grade_boundaries(coursework_id: str,
                         boundaries: list[tuple[str, float]],
                         session_label: str | None = None) -> None:
    import json
    db.execute(
        """
        INSERT INTO grade_boundaries(coursework_id, boundaries_json, session_label, updated_at)
        VALUES(?,?,?,?)
        ON CONFLICT(coursework_id) DO UPDATE SET
            boundaries_json = excluded.boundaries_json,
            session_label = excluded.session_label,
            updated_at = excluded.updated_at
        """,
        (coursework_id, json.dumps([[g, m] for g, m in boundaries]),
         session_label, time.time()),
    )


def grade_boundaries(coursework_id: str | None) -> tuple[list[tuple[str, float]], str]:
    """Assignment-specific boundaries, falling back to the global config (§20).

    Returns ``(boundaries, source)`` where ``source`` is ``assignment`` or
    ``global``. The AI never decides a grade — this table only supplies the
    boundary numbers the deterministic lookup uses.
    """
    if coursework_id:
        row = db.query_one(
            "SELECT boundaries_json, session_label FROM grade_boundaries "
            "WHERE coursework_id = ?", (coursework_id,))
        if row:
            parsed = _parse_boundaries(db.loads(row["boundaries_json"], []))
            if parsed:
                return parsed, "assignment"
    from ..config import get_config
    return list(get_config().grades.boundaries), "global"


def _parse_boundaries(raw) -> list[tuple[str, float]]:
    parsed: list[tuple[str, float]] = []
    if not isinstance(raw, list):
        return parsed
    for entry in raw:
        if not isinstance(entry, (list, tuple)) or len(entry) != 2:
            continue
        try:
            parsed.append((str(entry[0]).strip(), float(entry[1])))
        except (TypeError, ValueError):
            continue
    parsed.sort(key=lambda item: item[1])
    return parsed


# ------------------------------------------------------------------- jobs

def find_job(coursework_id: str, submission_id: str,
             submission_version: str) -> dict | None:
    row = db.query_one(
        "SELECT * FROM automation_jobs WHERE coursework_id = ? "
        "AND submission_id = ? AND submission_version = ?",
        (coursework_id, submission_id, submission_version))
    return dict(row) if row else None


def get(job_id: str) -> dict | None:
    row = db.query_one("SELECT * FROM automation_jobs WHERE id = ?", (job_id,))
    return dict(row) if row else None


def create_job(*, course_id: str, coursework_id: str, assignment_title: str | None,
               student_id: str | None, student_name: str | None,
               submission_id: str, submission_version: str,
               max_attempts: int = 3) -> dict | None:
    """Create a NEW workflow job for one submission VERSION.

    Returns ``None`` when a job already exists for this exact version — the
    duplicate-protection guarantee from §11 (a unique index backs this up, so
    even a racing insert cannot create a second job).
    """
    job_id = uuid.uuid4().hex
    now_ts = time.time()
    try:
        db.execute(
            """
            INSERT INTO automation_jobs(
                id, course_id, coursework_id, assignment_title, student_id,
                student_name, submission_id, submission_version, status,
                step_detail, attempts, max_attempts, created_at, updated_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,0,?,?,?)
            """,
            (job_id, course_id, coursework_id, assignment_title, student_id,
             student_name, submission_id, submission_version, states.NEW,
             "Queued for automatic correction", max_attempts, now_ts, now_ts),
        )
    except Exception as exc:  # noqa: BLE001 — unique index = duplicate
        log.debug("automation job not created (likely duplicate): %s", exc)
        return None
    log_job("info", "detected", "New submission detected", job_id=job_id)
    return get(job_id)


def list_jobs(*, statuses: list[str] | None = None, coursework_id: str | None = None,
              limit: int = 200) -> list[dict]:
    where, params = [], []
    if statuses:
        where.append("status IN (%s)" % ",".join("?" * len(statuses)))
        params.extend(statuses)
    if coursework_id:
        where.append("coursework_id = ?")
        params.append(coursework_id)
    clause = ("WHERE " + " AND ".join(where)) if where else ""
    rows = db.query(
        f"SELECT * FROM automation_jobs {clause} ORDER BY updated_at DESC LIMIT ?",
        (*params, limit))
    return [dict(r) for r in rows]


def counts() -> dict:
    rows = db.query("SELECT status, COUNT(*) AS n FROM automation_jobs GROUP BY status")
    return {r["status"]: r["n"] for r in rows}


RUNNABLE_STATES = states.PENDING_STATES + states.RETRYABLE_FAILURES

#: Normal due-times never exceed a couple of minutes (BACKOFF_CAP), so a job
#: due further ahead than this horizon was deliberately PARKED after a
#: permanent failure (see runner.PARK_SECONDS) and must stay parked across a
#: restart instead of being made due again by boot recovery.
RECOVER_HORIZON = 24 * 3600.0


def next_runnable(now_ts: float | None = None, *,
                  exclude_new: bool = False) -> dict | None:
    """The oldest job whose next step is due.

    Includes retryable failure states so a transient failure retries itself
    with exponential backoff (§39, §40) — but never an ATTENTION_REQUIRED or
    BLOCKED job, which needs a human.

    ``exclude_new`` is the background-worker OFF rule: a job that has not
    started yet (NEW) waits for the worker to be turned back on, while jobs
    already in flight keep advancing to a safe stopping point.
    """
    now_ts = now_ts if now_ts is not None else time.time()
    states_list = list(RUNNABLE_STATES)
    if exclude_new and states.NEW in states_list:
        states_list = [s for s in states_list if s != states.NEW]
    if not states_list:
        return None
    placeholders = ",".join("?" * len(states_list))
    row = db.query_one(
        f"SELECT * FROM automation_jobs WHERE status IN ({placeholders}) "
        f"AND next_attempt_at <= ? ORDER BY created_at ASC LIMIT 1",
        (*states_list, now_ts))
    return dict(row) if row else None


#: How long a claim is trusted before another worker may take the job over.
#: Shared with the runner so a manual retry drops only a claim that this lease
#: has already expired (a fresh claim means a live worker owns the job).
CLAIM_LEASE_SECONDS = 120.0


def claim(job_id: str, *, worker: str = "automation-runner") -> dict | None:
    """Atomically claim a job so two workers can never process it (§42).

    The UPDATE only succeeds while the job is in a runnable state AND was not
    already claimed recently; a racing caller gets ``None``.
    """
    now_ts = time.time()
    placeholders = ",".join("?" * len(RUNNABLE_STATES))
    with db.transaction() as conn:
        cur = conn.execute(
            f"UPDATE automation_jobs SET claimed_by = ?, claimed_at = ?, "
            f"updated_at = ? WHERE id = ? AND status IN ({placeholders}) "
            f"AND next_attempt_at <= ? "
            f"AND (claimed_by IS NULL OR claimed_at < ?)",
            (worker, now_ts, now_ts, job_id, *RUNNABLE_STATES, now_ts,
             now_ts - CLAIM_LEASE_SECONDS),
        )
        if cur.rowcount == 0:
            return None
    return get(job_id)


def release(job_id: str) -> None:
    db.execute("UPDATE automation_jobs SET claimed_by = NULL WHERE id = ?",
               (job_id,))


def set_status(job_id: str, status: str, *, detail: str | None = None,
               error: str | None = None, error_code: str | None = None,
               next_attempt_at: float | None = None, **fields) -> dict | None:
    """Persist an explicit state transition and log it (§13, §44)."""
    current = get(job_id)
    sets = ["status = ?", "updated_at = ?"]
    params: list = [status, time.time()]
    if detail is not None:
        sets.append("step_detail = ?")
        params.append(detail)
    if error is not None:
        sets.append("error = ?")
        params.append(error)
    if error_code is not None:
        sets.append("error_code = ?")
        params.append(error_code)
    if next_attempt_at is not None:
        sets.append("next_attempt_at = ?")
        params.append(next_attempt_at)
    if error_code is None and status not in states.RETRYABLE_FAILURES:
        # A step that moved forward clears the previous error.
        sets.append("error = NULL")
        sets.append("error_code = NULL")
    for key in ("correction_job_id", "result_id", "marks_obtained",
                "maximum_marks", "percentage", "grade", "report_filename",
                "drive_file_id"):
        if key in fields and fields[key] is not None:
            sets.append(f"{key} = ?")
            params.append(fields[key])
    if status in states.TERMINAL:
        sets.append("finished_at = ?")
        params.append(time.time())
    if status == states.COMPLETE:
        sets.append("completed_at = ?")
        params.append(time.time())
    params.append(job_id)
    db.execute(f"UPDATE automation_jobs SET {', '.join(sets)} WHERE id = ?", params)
    job = get(job_id)
    if current and current["status"] != status:
        log_job("success" if status == states.COMPLETE else "info",
                status.lower(),
                detail or (f"{states.label(current['status'])} → "
                           f"{states.label(status)}"), job_id=job_id)
    return job


def log_job(level: str, event: str, message: str | None = None, *,
            job_id: str | None = None) -> None:
    job = get(job_id) if job_id else None
    db.execute(
        """
        INSERT INTO automation_log(
            created_at, level, event, message, automation_job_id,
            course_id, coursework_id, submission_id)
        VALUES(?,?,?,?,?,?,?,?)
        """,
        (time.time(), level, event, message, job_id,
         job.get("course_id") if job else None,
         job.get("coursework_id") if job else None,
         job.get("submission_id") if job else None),
    )


def log_event_entry(level: str, event: str, message: str | None = None, *,
                    course_id: str | None = None,
                    coursework_id: str | None = None,
                    submission_id: str | None = None) -> None:
    db.execute(
        """
        INSERT INTO automation_log(
            created_at, level, event, message, course_id, coursework_id,
            submission_id)
        VALUES(?,?,?,?,?,?,?)
        """,
        (time.time(), level, event, message, course_id, coursework_id,
         submission_id),
    )


def recent_log(limit: int = 100) -> list[dict]:
    rows = db.query(
        "SELECT * FROM automation_log ORDER BY created_at DESC LIMIT ?", (limit,))
    return [dict(r) for r in rows]


def last_activity() -> float | None:
    """Timestamp of the most recent automation event (worker activity feed)."""
    row = db.query_one("SELECT MAX(created_at) AS t FROM automation_log")
    value = row["t"] if row else None
    return float(value) if value is not None else None


def clear_log() -> int:
    cur = db.execute("DELETE FROM automation_log")
    return cur.rowcount if cur else 0


# ------------------------------------------------------------------- meta

def get_meta(key: str) -> float | None:
    raw = db.get_meta(f"automation.{key}")
    try:
        return float(raw) if raw is not None else None
    except (TypeError, ValueError):
        return None


def set_meta(key: str, value: float) -> None:
    db.set_meta(f"automation.{key}", str(value))


def clear_claim_stale() -> int:
    """Release claims left behind by a crash so jobs can run again (§41)."""
    now_ts = time.time()
    cur = db.execute(
        "UPDATE automation_jobs SET claimed_by = NULL, claimed_at = NULL "
        "WHERE claimed_by IS NOT NULL AND claimed_at < ?",
        (now_ts - CLAIM_LEASE_SECONDS,))
    return cur.rowcount if cur else 0


def recover_in_flight() -> dict:
    """Restart-safety (§41): requeue claimed-but-not-finished jobs.

    A job never restarts from NEW — it keeps its exact status, so the runner
    resumes from the step it reached. Only the stale claim is released and a
    job stuck mid-step is made immediately due again.
    """
    claimed = clear_claim_stale()
    now_ts = time.time()
    db.execute(
        "UPDATE automation_jobs SET next_attempt_at = ?, updated_at = ? "
        "WHERE status IN (%s) AND next_attempt_at <= ?"
        % ",".join("?" * len(RUNNABLE_STATES)),
        (now_ts, now_ts, *RUNNABLE_STATES, now_ts + RECOVER_HORIZON))
    return {"claims_released": claimed}
