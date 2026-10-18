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

import contextlib
import contextvars
import logging
import time
import uuid

from .. import db
from ..logging_setup import log_event
from . import operations, states

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
              limit: int = 200, include_hidden: bool = False) -> list[dict]:
    where, params = [], []
    if not include_hidden:
        where.append("hidden_at IS NULL")
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


def execution_counts(now_ts: float | None = None) -> dict[str, int]:
    """Count logical active and waiting jobs, not pipeline-stage rows.

    An automation workflow is active while its lease is live, or while its
    child correction job is genuinely executing in the worker pool. NEW jobs,
    backoff retries and correction jobs queued/waiting for API capacity are
    waiting, not in flight. DISTINCT workflow ids ensure a job moving through
    multiple states is counted once.
    """
    now_ts = time.time() if now_ts is None else now_ts
    lease_cutoff = now_ts - CLAIM_LEASE_SECONDS
    active_engine = ("STARTING", "PREPARING", "DOWNLOADING", "UPLOADING",
                     "MARKING", "VERIFYING", "SAVING", "RETRYING")
    active_workflows = states.PENDING_STATES
    waiting_engine = ("QUEUED", "WAITING_FOR_API")
    workflow_marks = ",".join("?" * len(active_workflows))
    active_marks = ",".join("?" * len(active_engine))
    waiting_marks = ",".join("?" * len(waiting_engine))
    row = db.query_one(
        f"""
        SELECT
          COUNT(DISTINCT CASE WHEN aj.status IN ({workflow_marks}) AND (
            (aj.claimed_by IS NOT NULL AND aj.claimed_at >= ? AND cj.id IS NULL)
            OR cj.status IN ({active_marks})) THEN aj.id END) AS active,
          COUNT(DISTINCT CASE WHEN aj.status = ? OR cj.status IN ({waiting_marks})
            OR aj.status IN ({','.join('?' * len(RETRYABLE_STATES))})
            THEN aj.id END) AS waiting
        FROM automation_jobs aj
        LEFT JOIN correction_jobs cj ON cj.id = aj.correction_job_id
        """,
        (*active_workflows, lease_cutoff, *active_engine,
         states.NEW, *waiting_engine, *RETRYABLE_STATES))
    return {"active": int(row["active"] or 0),
            "waiting": int(row["waiting"] or 0)}


RETRYABLE_STATES = states.RETRYABLE_FAILURES


def clear_recent_jobs() -> int:
    """Hide safely terminal history without deleting job or result records."""
    now_ts = time.time()
    marks = ",".join("?" * len(states.TERMINAL))
    with db.transaction() as conn:
        cur = conn.execute(
            f"UPDATE automation_jobs SET hidden_at = ?, updated_at = ? "
            f"WHERE hidden_at IS NULL AND status IN ({marks}) "
            "AND (claimed_by IS NULL OR claimed_at < ?)",
            (now_ts, now_ts, *states.TERMINAL,
             now_ts - CLAIM_LEASE_SECONDS))
        return int(cur.rowcount)


def newer_finished_job(job: dict) -> dict | None:
    """A LATER job for the same submission that already finished its work.

    One job exists per submission VERSION, so a resubmission (or a
    re-correction) legitimately creates a new job while the older one keeps its
    own history. When the newer job COMPLETED, the older job's failure is
    history rather than a live problem — this is the lookup that lets
    reconciliation say so instead of leaving a false "failed" row on screen.
    """
    row = db.query_one(
        "SELECT * FROM automation_jobs WHERE coursework_id = ? "
        "AND submission_id = ? AND id != ? AND status = ? "
        "AND created_at > ? ORDER BY created_at DESC LIMIT 1",
        (job.get("coursework_id"), job.get("submission_id"), job.get("id"),
         states.COMPLETE, job.get("created_at") or 0))
    return dict(row) if row else None


def older_parked_jobs(job: dict) -> list[dict]:
    """Earlier jobs for the SAME submission that are parked with an error.

    Those are the rows that read as live failures on the dashboard while a
    newer attempt has already done the work. Jobs that are still progressing
    are deliberately NOT returned: superseding them would abandon work that is
    genuinely in flight.
    """
    parked = tuple(states.RETRYABLE_FAILURES) + tuple(states.UNRESOLVED_TERMINAL)
    marks = ",".join("?" * len(parked))
    rows = db.query(
        f"SELECT * FROM automation_jobs WHERE coursework_id = ? "
        f"AND submission_id = ? AND id != ? AND created_at < ? "
        f"AND hidden_at IS NULL AND status IN ({marks}) "
        "ORDER BY created_at ASC",
        (job.get("coursework_id"), job.get("submission_id"), job.get("id"),
         job.get("created_at") or 0, *parked))
    return [dict(r) for r in rows]


def supersede(job_id: str, *, by_job_id: str | None = None,
              reason: str | None = None) -> dict | None:
    """Mark a job SUPERSEDED (history, never a live failure).

    Nothing is deleted: the job keeps its correction, result, report and grade
    references so the audit trail and the duplicate-upload guards still work.
    """
    job = get(job_id)
    if not job or job["status"] == states.SUPERSEDED:
        return job
    if job["status"] not in states.PENDING_STATES and job["status"] not in \
            states.RETRYABLE_FAILURES and job["status"] not in \
            states.UNRESOLVED_TERMINAL and job["status"] != states.NEW:
        return job
    detail = reason or states.SUPERSEDED_NOTE
    # Every operation still owed by this attempt is CANCELLED (the newer
    # attempt took it over), which resolves its error without pretending this
    # attempt succeeded at anything it did not.
    for op in operations.for_job(job_id):
        if op["state"] in operations.UNRESOLVED_STATES:
            operations.cancel(job_id, op["op_key"],
                              note=states.SUPERSEDED_NOTE)
    db.execute(
        "UPDATE automation_jobs SET status = ?, error = NULL, error_code = NULL, "
        "step_detail = ?, next_attempt_at = ?, claimed_by = NULL, "
        "claimed_at = NULL, finished_at = COALESCE(finished_at, ?), "
        "updated_at = ? WHERE id = ?",
        (states.SUPERSEDED, detail, time.time() + RECOVER_HORIZON,
         time.time(), time.time(), job_id))
    log_job("info", "superseded", detail, job_id=job_id)
    log_event("automation", "job superseded by a newer attempt",
              job_id=job_id, by=by_job_id)
    return get(job_id)


def begin_bulk_upload_run(result_ids: list[str], *, lease_seconds: float = 900.0) -> str | None:
    """Atomically lease a set of result uploads; reject overlapping active runs."""
    now_ts = time.time()
    run_id = uuid.uuid4().hex
    result_ids = list(dict.fromkeys(result_ids))
    if not result_ids:
        return None
    with db.transaction() as conn:
        marks = ",".join("?" * len(result_ids))
        conn.execute(
            "DELETE FROM bulk_upload_claims WHERE lease_until <= ?", (now_ts,))
        conflict = conn.execute(
            f"SELECT result_id FROM bulk_upload_claims WHERE result_id IN ({marks}) LIMIT 1",
            result_ids).fetchone()
        if conflict:
            return None
        conn.executemany(
            "INSERT INTO bulk_upload_claims(result_id, run_id, lease_until, updated_at) "
            "VALUES(?,?,?,?)",
            [(result_id, run_id, now_ts + lease_seconds, now_ts)
             for result_id in result_ids])
    return run_id


def end_bulk_upload_run(run_id: str) -> None:
    db.execute("DELETE FROM bulk_upload_claims WHERE run_id = ?", (run_id,))


def release_bulk_upload_claim(run_id: str, result_id: str) -> None:
    """Free ONE finished item so a later run can retry it immediately.

    A claim exists to stop two overlapping runs touching the same submission.
    Once the item's outcome is recorded the claim has done its job, so holding
    it until the whole (possibly long) run ends would only block the teacher
    from retrying the few items that actually failed.
    """
    db.execute("DELETE FROM bulk_upload_claims WHERE run_id = ? AND result_id = ?",
               (run_id, result_id))


def bulk_upload_component(result_id: str) -> dict | None:
    row = db.query_one("SELECT * FROM bulk_upload_items WHERE result_id = ?",
                       (result_id,))
    return dict(row) if row else None


def grade_component_evidence(coursework_id: str, submission_id: str,
                             grade: float) -> str:
    """Return published/unknown/none for this exact intended Classroom grade."""
    rows = db.query(
        "SELECT status, payload_json FROM classroom_publish_ops "
        "WHERE coursework_id = ? AND submission_id = ? "
        "AND op_type IN ('assign_grade','final_grade') ORDER BY id DESC",
        (coursework_id, submission_id))
    for row in rows:
        try:
            payload = db.loads(row["payload_json"], {}) or {}
            same_grade = abs(float(payload.get("grade")) - float(grade)) < 1e-6
        except (TypeError, ValueError):
            same_grade = False
        if same_grade:
            return "published" if row["status"] == "published" else "unknown"
    return "none"


def save_bulk_upload_component(*, result_id: str, coursework_id: str,
                               submission_id: str, component: str,
                               status: str, message: str | None = None,
                               grade: float | None = None,
                               report_hash: str | None = None) -> None:
    if component not in ("grade", "report"):
        raise ValueError("Unknown bulk upload component.")
    column = "grade_status" if component == "grade" else "report_status"
    message_column = "grade_message" if component == "grade" else "report_message"
    db.execute(
        f"""INSERT INTO bulk_upload_items(
            result_id, coursework_id, submission_id, grade, report_hash,
            {column}, {message_column}, updated_at)
        VALUES(?,?,?,?,?,?,?,?)
        ON CONFLICT(result_id) DO UPDATE SET
            coursework_id=excluded.coursework_id,
            submission_id=excluded.submission_id,
            grade=COALESCE(excluded.grade, bulk_upload_items.grade),
            report_hash=COALESCE(excluded.report_hash, bulk_upload_items.report_hash),
            {column}=excluded.{column},
            {message_column}=excluded.{message_column},
            updated_at=excluded.updated_at""",
        (result_id, coursework_id, submission_id, grade, report_hash,
         status, message, time.time()))


def report_upload_begin(result: dict, report_hash: str, *,
                        lease_seconds: float = 240.0) -> dict:
    """Durably claim one result's report version, preserving ambiguous outcomes."""
    now_ts = time.time()
    result_id = str(result["id"])
    with db.transaction() as conn:
        row = conn.execute(
            "SELECT status, report_hash, lease_until, remote_file_id FROM report_upload_records "
            "WHERE result_id = ?", (result_id,)).fetchone()
        if row and row["report_hash"] == report_hash:
            status = row["status"]
            if status == "UPLOADED":
                return {"claimed": False, "status": status,
                        "remote_file_id": row["remote_file_id"]}
            if status == "UPLOADING" and float(row["lease_until"] or 0) > now_ts:
                return {"claimed": False, "status": status,
                        "remote_file_id": row["remote_file_id"]}
            if status == "AMBIGUOUS":
                return {"claimed": False, "status": status,
                        "remote_file_id": row["remote_file_id"]}
        conn.execute(
            "INSERT INTO report_upload_records(result_id, coursework_id, submission_id, "
            "student_id, report_hash, status, lease_until, updated_at) "
            "VALUES(?,?,?,?,?,'UPLOADING',?,?) ON CONFLICT(result_id) DO UPDATE SET "
            "coursework_id=excluded.coursework_id, submission_id=excluded.submission_id, "
            "student_id=excluded.student_id, report_hash=excluded.report_hash, "
            "status='UPLOADING', lease_until=excluded.lease_until, "
            "remote_file_id=NULL, error=NULL, updated_at=excluded.updated_at",
            (result_id, result.get("coursework_id"), result.get("submission_id"),
             result.get("student_id"), report_hash, now_ts + lease_seconds, now_ts))
    return {"claimed": True, "status": "UPLOADING", "remote_file_id": None}


def report_upload_finish(result_id: str, report_hash: str, *, status: str,
                         remote_file_id: str | None = None,
                         error: str | None = None) -> None:
    if status not in ("UPLOADED", "RETRYABLE_FAILURE", "PERMANENT_FAILURE", "AMBIGUOUS"):
        raise ValueError("Invalid report upload state")
    db.execute(
        "UPDATE report_upload_records SET status=?, remote_file_id=?, error=?, "
        "lease_until=NULL, updated_at=? WHERE result_id=? AND report_hash=?",
        (status, remote_file_id, (error or None), time.time(), result_id, report_hash))


def report_upload_record(result_id: str) -> dict | None:
    row = db.query_one("SELECT * FROM report_upload_records WHERE result_id = ?",
                       (result_id,))
    return dict(row) if row else None


def reconcile_report_upload(result: dict, report_hash: str, remote_file_id: str,
                            *, attached: bool) -> None:
    """Record the verified outcome after observing Classroom's remote state."""
    report_upload_finish(
        str(result["id"]), report_hash,
        status="UPLOADED" if attached else "RETRYABLE_FAILURE",
        remote_file_id=remote_file_id,
        error=None if attached else "Google has not confirmed this report attachment.")


def clear_recent_count() -> int:
    """Number of terminal job rows that can safely be archived from the UI."""
    terminal = tuple(states.TERMINAL)
    marks = ",".join("?" * len(terminal))
    row = db.query_one(
        f"SELECT COUNT(*) AS n FROM automation_jobs WHERE hidden_at IS NULL "
        f"AND status IN ({marks}) AND (claimed_by IS NULL OR claimed_at < ?)",
        (*terminal, time.time() - CLAIM_LEASE_SECONDS))
    return int(row["n"] or 0)


def visible_active_count() -> int:
    """How many jobs are still on screen with work outstanding.

    Used by the "Clear history" confirmation to state what the archive did NOT
    touch: clearing only hides terminal history, so an active job must still be
    counted here afterwards.
    """
    terminal = tuple(states.TERMINAL)
    marks = ",".join("?" * len(terminal))
    row = db.query_one(
        f"SELECT COUNT(*) AS n FROM automation_jobs WHERE hidden_at IS NULL "
        f"AND status NOT IN ({marks})", (*terminal,))
    return int(row["n"] or 0)


def bulk_upload_items(coursework_id: str | None = None) -> list[dict]:
    where, params = [], []
    if coursework_id:
        where.append("r.coursework_id = ?")
        params.append(coursework_id)
    where.insert(0, "r.submission_id IS NOT NULL")
    clause = "WHERE " + " AND ".join(where)
    rows = db.query(
        f"""
        SELECT r.id AS result_id, r.coursework_id, r.submission_id,
               r.student_id, r.student_name, r.earned, r.available,
               r.percentage, r.provisional_grade, r.review_required,
               r.manual_override, cw.title AS assignment_title, cw.course_id,
               cw.max_points, cs.assigned_grade, cs.draft_grade,
               cs.submission_id AS synced_submission_id,
               b.grade_status, b.grade_message, b.report_status, b.report_message,
               b.report_hash, b.grade
        FROM results r
        JOIN coursework cw ON cw.id = r.coursework_id
        LEFT JOIN classroom_submissions cs ON cs.submission_id = r.submission_id
        LEFT JOIN bulk_upload_items b ON b.result_id = r.id
        {clause}
        ORDER BY r.created_at DESC
        """,
        params)
    return [dict(row) for row in rows]


def clear_claim_stale() -> int:
    """Release claims left behind by a crash so jobs can run again (§41)."""
    now_ts = time.time()
    cur = db.execute(
        "UPDATE automation_jobs SET claimed_by = NULL, claimed_at = NULL "
        "WHERE claimed_by IS NOT NULL AND claimed_at < ?",
        (now_ts - CLAIM_LEASE_SECONDS,))
    return cur.rowcount if cur else 0


def counts() -> dict:
    rows = db.query("SELECT status, COUNT(*) AS n FROM automation_jobs "
                    "WHERE hidden_at IS NULL GROUP BY status")
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


#: The claim token of the worker that is currently advancing a job, if any.
#: Set by :func:`claim_guard` around the runner's advance loop, so EVERY status
#: write made while a job is being advanced is conditional on this worker still
#: owning the job. A stale worker (whose claim was released, taken over by a
#: retry, or whose job was superseded meanwhile) then cannot write over a newer
#: successful state — its UPDATE simply matches no row.
_claim_guard: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "automation_claim_guard", default=None)


@contextlib.contextmanager
def claim_guard(worker: str):
    """Scope status writes to the worker that holds this job's claim."""
    token = _claim_guard.set(worker)
    try:
        yield
    finally:
        _claim_guard.reset(token)


def set_status(job_id: str, status: str, *, detail: str | None = None,
               error: str | None = None, error_code: str | None = None,
               next_attempt_at: float | None = None, **fields) -> dict | None:
    """Persist an explicit state transition and log it (§13, §44).

    While an advance loop holds the job's claim the write is CONDITIONAL on
    ``claimed_by`` still being that worker, so an outdated worker response can
    never revert a newer successful state.
    """
    guard = _claim_guard.get()
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
    sql = f"UPDATE automation_jobs SET {', '.join(sets)} WHERE id = ?"
    if guard:
        sql += " AND claimed_by = ?"
        params.append(guard)
    with db.transaction() as conn:
        cur = conn.execute(sql, params)
    job = get(job_id)
    if guard and cur.rowcount == 0:
        # The claim moved on: this write is stale. Report the truth instead of
        # overwriting it, and let the caller re-read the job.
        log.info("stale %s write for job %s refused (claim lost)",
                 status, job_id)
        return job
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
        "SELECT * FROM automation_log WHERE id > ? ORDER BY created_at DESC LIMIT ?",
        (int(get_meta("log_hidden_through_id") or 0), limit))
    return [dict(r) for r in rows]


def last_activity() -> float | None:
    """Timestamp of the most recent automation event (worker activity feed)."""
    row = db.query_one("SELECT MAX(created_at) AS t FROM automation_log")
    value = row["t"] if row else None
    return float(value) if value is not None else None


def clear_log() -> int:
    # Clear the visible feed, never the append-only activity/audit records.
    with db.transaction() as conn:
        cutoff = int(get_meta("log_hidden_through_id") or 0)
        row = conn.execute("SELECT COUNT(*) AS n, COALESCE(MAX(id), 0) AS last_id "
                           "FROM automation_log WHERE id > ?", (cutoff,)).fetchone()
        conn.execute("INSERT INTO app_meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                     ("automation.log_hidden_through_id", str(max(cutoff, row["last_id"]))))
        return row["n"]


# ------------------------------------------------------------------- meta

def get_meta(key: str) -> float | None:
    raw = db.get_meta(f"automation.{key}")
    try:
        return float(raw) if raw is not None else None
    except (TypeError, ValueError):
        return None


def set_meta(key: str, value: float) -> None:
    db.set_meta(f"automation.{key}", str(value))


def recover_in_flight() -> dict:
    """Restart-safety (§41): requeue claimed-but-not-finished jobs.

    A job never restarts from NEW — it keeps its exact status, so the runner
    resumes from the step it reached. Only the stale claim is released and a
    job stuck mid-step is made immediately due again.

    An operation that was IN PROGRESS when the process died is recorded as
    NEEDS VERIFICATION: the request may have been accepted by Google, so the
    next attempt re-reads the remote state BEFORE writing anything instead of
    blindly repeating the call.
    """
    claimed = clear_claim_stale()
    interrupted = operations.abandon_in_flight()
    now_ts = time.time()
    db.execute(
        "UPDATE automation_jobs SET next_attempt_at = ?, updated_at = ? "
        "WHERE status IN (%s) AND next_attempt_at <= ?"
        % ",".join("?" * len(RUNNABLE_STATES)),
        (now_ts, now_ts, *RUNNABLE_STATES, now_ts + RECOVER_HORIZON))
    return {"claims_released": claimed, "operations_interrupted": interrupted}


def reset_for_retry(job_id: str, *, expected_status: str,
                    resume_state: str) -> bool:
    """Re-arm ONE failed job for a retry — atomically, and only from the state
    the caller actually saw.

    The compare-and-set is what makes two concurrent retries safe: the second
    request finds the row already moved on (or finished) and is refused instead
    of re-running the step a second time.

    A LIVE claim is deliberately kept: the worker that holds it re-reads the
    job at the top of its loop and picks the new state up there, so the retry
    takes effect immediately without the step ever running twice. Only a
    genuinely STALE claim (its lease expired, i.e. that worker died) is
    dropped, so the job can be claimed again right away.
    """
    now_ts = time.time()
    with db.transaction() as conn:
        cur = conn.execute(
            "UPDATE automation_jobs SET status = ?, attempts = 0, "
            "next_attempt_at = 0, updated_at = ? WHERE id = ? AND status = ?",
            (resume_state, now_ts, job_id, expected_status))
        if cur.rowcount == 0:
            return False
        conn.execute(
            "UPDATE automation_jobs SET claimed_by = NULL, claimed_at = NULL "
            "WHERE id = ? AND claimed_by IS NOT NULL AND claimed_at < ?",
            (job_id, now_ts - CLAIM_LEASE_SECONDS))
    return True


def clear_resolved_errors() -> int:
    """Clear the MESSAGES of resolved errors, keeping every operation record."""
    return operations.clear_resolved()
