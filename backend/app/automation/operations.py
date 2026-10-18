"""Per-OPERATION state for an automation job (report upload / grade / return).

A job row has ONE ``status`` and ONE ``error``. That is the state-machine
position, and it is not enough: a report upload that Google actually accepted
while the response timed out left the job parked as ``REPORT_UPLOAD_FAILED``
with the report and the returned work already on the submission, and the stale
error then outlived every retry.

This module keeps the two ideas apart:

* **current operation state** — ``PENDING``, ``IN_PROGRESS``, ``SUCCEEDED``,
  ``FAILED``, ``NEEDS_VERIFICATION``, ``CANCELLED``: what is true NOW.
* **error history** — what failed, when, on which attempt, and whether it was
  later ``resolved`` (recovered by a successful retry or by reconciling the
  real Google Classroom state). History is kept for diagnosis but can never
  make a succeeded operation read as failed.

An operation is identified by ``(job_id, op_key)``, so a retry RESOLVES the
same logical operation instead of creating a new one, and the row doubles as
the idempotency record that stops a second report attachment or a second grade
write.

Everything here is a small, explicit SQLite write; nothing is inferred from a
200 and nothing is ever deleted by clearing an error — clearing removes only
the resolved MESSAGE, never the record that prevents a duplicate write.
"""

from __future__ import annotations

import logging
import time

from .. import db

log = logging.getLogger("automation.operations")

# -- operation keys ----------------------------------------------------------

REPORT = "report"
GRADE = "grade"
RETURN = "return"

#: The canonical order the UI shows the operations in.
OP_KEYS = (REPORT, GRADE, RETURN)

KIND_OF = {REPORT: "report_upload", GRADE: "grade", RETURN: "return"}
KEY_OF_KIND = {kind: key for key, kind in KIND_OF.items()}

LABELS = {REPORT: "Report upload", GRADE: "Grade", RETURN: "Return"}

# -- operation states --------------------------------------------------------

PENDING = "PENDING"
IN_PROGRESS = "IN_PROGRESS"
SUCCEEDED = "SUCCEEDED"
FAILED = "FAILED"
NEEDS_VERIFICATION = "NEEDS_VERIFICATION"
CANCELLED = "CANCELLED"

STATE_LABELS = {
    PENDING: "Pending",
    IN_PROGRESS: "In progress",
    SUCCEEDED: "Succeeded",
    FAILED: "Failed",
    NEEDS_VERIFICATION: "Needs verification",
    CANCELLED: "Cancelled",
}

#: States that mean "the operation is done and nothing is owed".
DONE_STATES = (SUCCEEDED,)

#: States whose error is still ACTIVE (never auto-cleared by a later success).
UNRESOLVED_STATES = (FAILED, NEEDS_VERIFICATION)

#: Workflow states that name an operation, so a job failure can be attributed.
STATE_TO_KEY = {
    "REPORT_FAILED": REPORT,
    "REPORT_UPLOAD_FAILED": REPORT,
    "ATTACHMENT_FAILED": REPORT,
    "GRADE_FAILED": GRADE,
    "GRADE_VERIFICATION_FAILED": GRADE,
    "RETURN_FAILED": RETURN,
    "CLEANUP_PENDING": RETURN,
}


def key_for_status(status: str | None) -> str | None:
    """Which operation a FAILURE state belongs to (``None`` when it names none,
    e.g. a correction failure)."""
    return STATE_TO_KEY.get(status or "")


def kind(key: str) -> str:
    return KIND_OF.get(key, key)


# ------------------------------------------------------------------ reading

def _row(job_id: str, op_key: str):
    return db.query_one(
        "SELECT * FROM automation_operations WHERE job_id = ? AND op_key = ?",
        (job_id, op_key))


def get(job_id: str, op_key: str) -> dict | None:
    row = _row(job_id, op_key)
    return dict(row) if row else None


def _blank(job_id: str, op_key: str) -> dict:
    """The row an operation has before anything has been attempted.

    Returned (never written) so a job with no history still reports the honest
    ``Pending`` state instead of a missing operation.
    """
    return {
        "job_id": job_id, "op_key": op_key, "kind": kind(op_key),
        "state": PENDING, "attempt": 0, "error": None, "error_code": None,
        "error_at": None, "error_attempt": None, "resolved": 0,
        "resolved_at": None, "recovered": 0, "verified": 0, "note": None,
        "dismissed_at": None, "first_attempt_at": None, "last_attempt_at": None,
        "succeeded_at": None, "updated_at": None,
    }


def for_job(job_id: str) -> list[dict]:
    """Every operation of a job, in canonical order (missing ones = Pending)."""
    rows = {r["op_key"]: dict(r) for r in db.query(
        "SELECT * FROM automation_operations WHERE job_id = ?", (job_id,))}
    return [rows.get(key) or _blank(job_id, key) for key in OP_KEYS]


def summary(job_id: str) -> dict:
    """``{op_key: state}`` for the job, with unrecorded operations as Pending."""
    return {op["op_key"]: op["state"] for op in for_job(job_id)}


def has_history(job_id: str) -> bool:
    row = db.query_one(
        "SELECT 1 AS n FROM automation_operations WHERE job_id = ? LIMIT 1",
        (job_id,))
    return row is not None


def active_errors(job_id: str) -> list[dict]:
    """Unresolved (or unresolved-and-not-dismissed) error history for a job."""
    return [op for op in for_job(job_id)
            if op["error"] and not op["resolved"] and not op["dismissed_at"]]


def resolved_errors(job_id: str) -> list[dict]:
    return [op for op in for_job(job_id)
            if op["error"] and (op["resolved"] or op["dismissed_at"])]


def resolved_error_count() -> int:
    row = db.query_one(
        "SELECT COUNT(*) AS n FROM automation_operations "
        "WHERE error IS NOT NULL AND resolved = 1")
    return int(row["n"] or 0)


# ------------------------------------------------------------------ writing

def _ensure(job_id: str, op_key: str) -> None:
    if op_key not in OP_KEYS:
        raise ValueError(f"Unknown automation operation '{op_key}'.")
    now_ts = time.time()
    db.execute(
        """INSERT INTO automation_operations(
               job_id, op_key, kind, state, attempt, updated_at)
           VALUES(?,?,?,?,0,?)
           ON CONFLICT(job_id, op_key) DO UPDATE SET updated_at = updated_at
        """,
        (job_id, op_key, kind(op_key), PENDING, now_ts))


def begin(job_id: str, op_key: str, *, note: str | None = None) -> dict:
    """Record that the operation is being attempted now (attempt += 1).

    A retry therefore lands on the SAME logical operation. An operation that
    already succeeded is never silently re-opened as pending: the caller has to
    ask for a real retry (``reset``), which is what makes a duplicate upload or
    a duplicate grade write impossible to reach by accident.
    """
    _ensure(job_id, op_key)
    now_ts = time.time()
    db.execute(
        """UPDATE automation_operations
              SET state = CASE WHEN state = ? THEN state ELSE ? END,
                  attempt = attempt + 1,
                  first_attempt_at = COALESCE(first_attempt_at, ?),
                  last_attempt_at = ?,
                  note = COALESCE(?, note),
                  updated_at = ?
            WHERE job_id = ? AND op_key = ?""",
        (SUCCEEDED, IN_PROGRESS, now_ts, now_ts, note, now_ts, job_id, op_key))
    return get(job_id, op_key) or _blank(job_id, op_key)


def succeed(job_id: str, op_key: str, *, verified: bool = True,
            recovered: bool = False, note: str | None = None) -> dict:
    """The operation SUCCEEDED — and any earlier error becomes history.

    ``recovered`` marks success that was established by VERIFYING the remote
    outcome (Google already had it) rather than by a successful response from
    this attempt. Either way the operation's state is ``SUCCEEDED`` and the old
    error is flagged resolved so it can never be shown as an active failure.
    """
    _ensure(job_id, op_key)
    now_ts = time.time()
    db.execute(
        """UPDATE automation_operations
              SET state = ?,
                  verified = ?,
                  recovered = CASE WHEN ? = 1 THEN 1 ELSE recovered END,
                  note = COALESCE(?, note),
                  resolved = CASE WHEN error IS NOT NULL THEN 1 ELSE resolved END,
                  resolved_at = CASE WHEN error IS NOT NULL AND resolved_at IS NULL
                                     THEN ? ELSE resolved_at END,
                  succeeded_at = COALESCE(succeeded_at, ?),
                  updated_at = ?
            WHERE job_id = ? AND op_key = ?""",
        (SUCCEEDED, 1 if verified else 0, 1 if recovered else 0, note,
         now_ts, now_ts, now_ts, job_id, op_key))
    return get(job_id, op_key) or _blank(job_id, op_key)


def fail(job_id: str, op_key: str, *, message: str, code: str,
         ambiguous: bool = False, note: str | None = None) -> dict:
    """Record a failure (or an outcome that cannot be established at all).

    ``ambiguous=True`` records ``NEEDS_VERIFICATION``: Google may or may not
    have accepted the operation, so it is never presented as a plain failure —
    the safe recovery path is to CHECK before retrying.
    """
    _ensure(job_id, op_key)
    row = get(job_id, op_key) or {}
    now_ts = time.time()
    state = NEEDS_VERIFICATION if ambiguous else FAILED
    db.execute(
        """UPDATE automation_operations
              SET state = ?, error = ?, error_code = ?, error_at = ?,
                  error_attempt = ?, resolved = 0, resolved_at = NULL,
                  dismissed_at = NULL, note = COALESCE(?, note), updated_at = ?
            WHERE job_id = ? AND op_key = ?""",
        (state, message[:500], code, now_ts, int(row.get("attempt") or 0),
         note, now_ts, job_id, op_key))
    return get(job_id, op_key) or _blank(job_id, op_key)


def cancel(job_id: str, op_key: str, *, note: str | None = None) -> dict:
    """The operation was handed over to a newer attempt, so it is no longer
    owed. Its error stops being ACTIVE but is kept as history."""
    _ensure(job_id, op_key)
    now_ts = time.time()
    db.execute(
        """UPDATE automation_operations
              SET state = ?, note = COALESCE(?, note),
                  resolved = CASE WHEN error IS NOT NULL THEN 1 ELSE resolved END,
                  resolved_at = CASE WHEN error IS NOT NULL AND resolved_at IS NULL
                                     THEN ? ELSE resolved_at END,
                  updated_at = ?
            WHERE job_id = ? AND op_key = ? AND state != ?""",
        (CANCELLED, note, now_ts, now_ts, job_id, op_key, SUCCEEDED))
    return get(job_id, op_key) or _blank(job_id, op_key)


def reset(job_id: str, op_key: str, *, note: str | None = None) -> dict:
    """Re-open a finished operation for an explicit retry.

    Used when a teacher retries a step that had already succeeded (e.g. re-run
    only the report after the marks changed). It never clears the error
    history: the next ``succeed``/``fail`` decides it.
    """
    _ensure(job_id, op_key)
    now_ts = time.time()
    db.execute(
        """UPDATE automation_operations
              SET state = ?, verified = 0, note = COALESCE(?, note), updated_at = ?
            WHERE job_id = ? AND op_key = ?""",
        (PENDING, note, now_ts, job_id, op_key))
    return get(job_id, op_key) or _blank(job_id, op_key)


#: Errors that a crash/restart leaves behind: the attempt was cut off mid
#: request, so the remote outcome is UNKNOWN until checked.
def abandon_in_flight(job_id: str | None = None) -> int:
    """Turn every interrupted attempt into ``NEEDS_VERIFICATION``.

    Called on restart: an operation recorded ``IN_PROGRESS`` by a process that
    died was never confirmed, so the next attempt verifies the remote state
    before it writes anything.
    """
    now_ts = time.time()
    if job_id:
        cur = db.execute(
            "UPDATE automation_operations SET state = ?, updated_at = ? "
            "WHERE state = ? AND job_id = ?",
            (NEEDS_VERIFICATION, now_ts, IN_PROGRESS, job_id))
    else:
        cur = db.execute(
            "UPDATE automation_operations SET state = ?, updated_at = ? "
            "WHERE state = ?", (NEEDS_VERIFICATION, now_ts, IN_PROGRESS))
    return cur.rowcount if cur else 0


def interrupted(job_id: str, op_key: str) -> bool:
    """Was this operation's last attempt cut off mid-request?"""
    row = get(job_id, op_key)
    return bool(row and row["state"] == NEEDS_VERIFICATION)


# ------------------------------------------------------- teacher controls

def dismiss(job_id: str, op_key: str) -> tuple[dict | None, str | None]:
    """Dismiss an error line WITHOUT touching the operation's state.

    Returns ``(op, refusal)``. A genuinely failed operation stays failed — the
    dismissal only stops it being presented as an active error, so the UI can
    never turn a failure into a success by hiding it.
    """
    op = get(job_id, op_key)
    if op is None or not op.get("error"):
        return op, "This operation has no error to dismiss."
    if not (op["resolved"] or op["state"] == NEEDS_VERIFICATION):
        return op, ("This operation genuinely failed, so it is kept visible "
                    "until it succeeds or you retry it.")
    db.execute(
        "UPDATE automation_operations SET dismissed_at = ?, updated_at = ? "
        "WHERE job_id = ? AND op_key = ?",
        (time.time(), time.time(), job_id, op_key))
    return get(job_id, op_key), None


def clear_resolved(job_id: str | None = None) -> int:
    """Drop the MESSAGE of every resolved error, keeping the record itself.

    The operation row is what stops a duplicate report attachment or a second
    grade write, so it is never deleted here — only the resolved diagnostic
    text is cleared. Unresolved errors are untouched.
    """
    now_ts = time.time()
    sql = ("UPDATE automation_operations SET error = NULL, error_code = NULL, "
           "dismissed_at = NULL, updated_at = ? "
           "WHERE error IS NOT NULL AND resolved = 1")
    params: list = [now_ts]
    if job_id:
        sql += " AND job_id = ?"
        params.append(job_id)
    cur = db.execute(sql, params)
    return cur.rowcount if cur else 0
