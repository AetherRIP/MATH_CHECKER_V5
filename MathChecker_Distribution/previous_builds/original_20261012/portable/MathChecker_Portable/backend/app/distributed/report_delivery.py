"""Report delivery tracking (§5).

A synchronised GRADE is not proof the PDF arrived. The two are tracked
separately on purpose:

* the grade lives in ``correction_results_sync`` and is idempotent by
  (submission_id, submission_version, worker_id),
* the report lives here, keyed on the result record, with its own state
  machine, attempt count and next-retry time.

That split is what lets the system express the real-world cases the spec calls
out — "a report upload fails while the grade record succeeds", and "the report
is uploaded successfully after a retry" — without either one masking the other.

Retry policy reuses :func:`app.retry_policy.compute_backoff` (exponential, with
jitter, capped) so report transfers back off exactly like the rest of the app
and a fleet of assistants does not retry in lockstep.
"""
from __future__ import annotations

import time
import uuid

from .. import db
from ..retry_policy import compute_backoff

# State machine (§6 "Suggested states").
PENDING = "PENDING"
SENDING = "SENDING"
DELIVERED = "DELIVERED"
RETRY_SCHEDULED = "RETRY_SCHEDULED"
FAILED = "FAILED"
NEEDS_REVIEW = "NEEDS_REVIEW"

TERMINAL = (DELIVERED,)
# After this many failed attempts the item stops auto-retrying and asks a human
# to look, instead of hammering the endpoint forever.
DEFAULT_MAX_ATTEMPTS = 8
# Report files are larger than a heartbeat: give the transfer real headroom.
RETRY_CAP_SECONDS = 30 * 60


def _row(record_id: str) -> dict | None:
    r = db.query_one("SELECT * FROM report_deliveries WHERE result_record_id = ?",
                     (record_id,))
    return dict(r) if r else None


def _by_id(delivery_id: str) -> dict | None:
    r = db.query_one("SELECT * FROM report_deliveries WHERE id = ?", (delivery_id,))
    return dict(r) if r else None


def record_pending(*, result_record_id: str, submission_id: str | None = None,
                   report_filename: str | None = None,
                   report_sha256: str | None = None,
                   size_bytes: int | None = None,
                   max_attempts: int = DEFAULT_MAX_ATTEMPTS) -> dict:
    """Create (or refresh) the delivery row for a correction result.

    Idempotent: re-registering the same result never creates a second row and
    never rewinds an already-DELIVERED report back to PENDING — that is how
    "do not repeatedly upload already acknowledged reports" is enforced.
    """
    if not result_record_id:
        raise ValueError("result_record_id is required")
    existing = _row(result_record_id)
    now = time.time()
    if existing:
        if existing["state"] == DELIVERED:
            return existing
        db.execute(
            "UPDATE report_deliveries SET submission_id = COALESCE(?, submission_id),"
            " report_filename = COALESCE(?, report_filename),"
            " report_sha256 = COALESCE(?, report_sha256),"
            " size_bytes = COALESCE(?, size_bytes), updated_at = ?"
            " WHERE result_record_id = ?",
            (submission_id, report_filename, report_sha256, size_bytes, now,
             result_record_id))
        return _row(result_record_id)

    delivery_id = uuid.uuid4().hex
    db.execute(
        "INSERT INTO report_deliveries(id, result_record_id, submission_id,"
        " report_filename, report_sha256, size_bytes, state, attempts,"
        " max_attempts, next_retry_at, created_at, updated_at)"
        " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        (delivery_id, result_record_id, submission_id, report_filename,
         report_sha256, size_bytes, PENDING, 0, max_attempts, now, now, now))
    return _row(result_record_id)


def mark_sending(result_record_id: str) -> dict | None:
    db.execute(
        "UPDATE report_deliveries SET state = ?, updated_at = ?"
        " WHERE result_record_id = ? AND state != ?",
        (SENDING, time.time(), result_record_id, DELIVERED))
    return _row(result_record_id)


def mark_delivered(result_record_id: str, *,
                   report_sha256: str | None = None) -> dict | None:
    """Only call this after the receiving service CONFIRMED storage (§5/§6).

    An unacknowledged upload must never be recorded as delivered, so nothing
    here marks success speculatively.
    """
    now = time.time()
    db.execute(
        "UPDATE report_deliveries SET state = ?, delivered_at = ?, updated_at = ?,"
        " next_retry_at = NULL, last_error = NULL,"
        " report_sha256 = COALESCE(?, report_sha256)"
        " WHERE result_record_id = ?",
        (DELIVERED, now, now, report_sha256, result_record_id))
    return _row(result_record_id)


def mark_failed(result_record_id: str, error: str | None = None, *,
                retry_after: float | None = None) -> dict | None:
    """Record a failed attempt and schedule the next retry.

    Transient failures become RETRY_SCHEDULED with exponential backoff + jitter;
    exhausting the attempt budget becomes NEEDS_REVIEW so a human decides,
    rather than retrying forever.
    """
    current = _row(result_record_id)
    if current is None:
        return None
    attempts = int(current["attempts"] or 0) + 1
    max_attempts = int(current["max_attempts"] or DEFAULT_MAX_ATTEMPTS)
    now = time.time()
    if attempts >= max_attempts:
        db.execute(
            "UPDATE report_deliveries SET state = ?, attempts = ?, last_error = ?,"
            " next_retry_at = NULL, updated_at = ? WHERE result_record_id = ?",
            (NEEDS_REVIEW, attempts, (error or "")[:500], now, result_record_id))
        return _row(result_record_id)

    delay = compute_backoff(attempts, retry_after=retry_after,
                            cap=RETRY_CAP_SECONDS)
    db.execute(
        "UPDATE report_deliveries SET state = ?, attempts = ?, last_error = ?,"
        " next_retry_at = ?, updated_at = ? WHERE result_record_id = ?",
        (RETRY_SCHEDULED, attempts, (error or "")[:500], now + delay, now,
         result_record_id))
    return _row(result_record_id)


def retry_now(result_record_id: str) -> dict | None:
    """Manual retry (§6): make an item eligible again immediately.

    Clears a FAILED/NEEDS_REVIEW item back to PENDING but never touches a
    DELIVERED report — an acknowledged transfer is not re-sent for no reason.
    """
    current = _row(result_record_id)
    if current is None:
        return None
    if current["state"] == DELIVERED:
        return current
    db.execute(
        "UPDATE report_deliveries SET state = ?, next_retry_at = ?, updated_at = ?"
        " WHERE result_record_id = ?",
        (PENDING, time.time(), time.time(), result_record_id))
    return _row(result_record_id)


def due_retries(now: float | None = None, limit: int = 50) -> list[dict]:
    """Everything the sender should attempt right now.

    Includes PENDING: a freshly queued report has never been attempted, so
    excluding it would mean a new delivery was never sent at all. Also
    includes RETRY_SCHEDULED items whose backoff has elapsed, and SENDING rows
    older than the retry cap — that last case is exactly "the upload succeeded
    but the acknowledgement was lost", where we must try again rather than
    leave the record stuck forever (§6).
    """
    now = now if now is not None else time.time()
    rows = db.query(
        "SELECT * FROM report_deliveries"
        " WHERE (state IN (?, ?) AND (next_retry_at IS NULL OR next_retry_at <= ?))"
        "    OR (state = ? AND updated_at <= ?)"
        " ORDER BY COALESCE(next_retry_at, updated_at) ASC LIMIT ?",
        (PENDING, RETRY_SCHEDULED, now, SENDING, now - RETRY_CAP_SECONDS,
         max(1, min(limit, 500))))
    return [dict(r) for r in rows]


def pending_count() -> int:
    row = db.query_one(
        "SELECT COUNT(*) n FROM report_deliveries WHERE state IN (?,?,?)",
        (PENDING, SENDING, RETRY_SCHEDULED))
    return int(row["n"]) if row else 0


def summary(*, class_ids: set[str] | None = None) -> dict:
    """Counts per state, for the teacher/developer overview cards."""
    rows = db.query("SELECT state, COUNT(*) n FROM report_deliveries GROUP BY state")
    counts = {r["state"]: int(r["n"]) for r in rows}
    return {
        "pending": counts.get(PENDING, 0),
        "sending": counts.get(SENDING, 0),
        "delivered": counts.get(DELIVERED, 0),
        "retry_scheduled": counts.get(RETRY_SCHEDULED, 0),
        "failed": counts.get(FAILED, 0),
        "needs_review": counts.get(NEEDS_REVIEW, 0),
        "total": sum(counts.values()),
    }


def list_deliveries(*, limit: int = 200,
                    result_ids: set[str] | None = None) -> list[dict]:
    """Deliveries, newest first.

    ``result_ids`` narrows the list to a caller's visible correction records
    (§48 scoping / §1 team sharing). ``None`` means unrestricted — used only by
    roles that already see everything (Teacher/Developer).
    """
    limit = max(1, min(limit, 1000))
    if result_ids is not None:
        ids = list(result_ids)
        if not ids:
            return []
        placeholders = ",".join("?" * len(ids))
        rows = db.query(
            f"SELECT * FROM report_deliveries WHERE result_record_id IN "
            f"({placeholders}) ORDER BY updated_at DESC LIMIT ?",
            (*ids, limit))
        return [dict(r) for r in rows]
    rows = db.query("SELECT * FROM report_deliveries ORDER BY updated_at DESC LIMIT ?",
                    (limit,))
    return [dict(r) for r in rows]


def delivery_for(result_record_id: str) -> dict | None:
    return _row(result_record_id)


def get(delivery_id: str) -> dict | None:
    return _by_id(delivery_id)
