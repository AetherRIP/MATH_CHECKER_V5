"""Temporary Drive file lifecycle for correction reports.

WHAT CHANGED, AND WHY (the "document has been deleted" bug)
-----------------------------------------------------------
A Classroom submission attachment is a REFERENCE to a Google Drive file.
Deleting the Drive file afterwards leaves Classroom holding a dangling
reference, so the student is told "The document couldn't be opened. It may
have been deleted." The old behaviour deleted the temporary file as soon as
the attach API returned success - which is exactly that bug.

The file is therefore **RETAINED** once the attachment has been verified:
made accessible to the attached student and kept alive for as long as
Classroom references it. We only ever delete a temporary file that was never
attached (an orphan), and even then only when every ownership/marker proof
holds (§17).

The lifecycle
-------------
    CREATED -> UPLOADED -> ATTACHED -> RETAINED       (kept; Classroom needs it)
    CREATED -> UPLOADED -> ...        -> orphan sweep  (never attached; reaped)

The teacher's Drive is not littered because each correction reuses ONE file:
a re-mark PATCHes the existing file rather than creating another copy, and
only files stamped with this app's own temporary markers are ever considered
for deletion (§14, §17).

Why cleanup can never touch a user's file (§17)
------------------------------------------------
Deletion requires PROOF on four independent conditions:

  1. the file carries ``mathcorrectTemp=true`` (stamped at upload);
  2. it carries ``mathcorrectResultId`` equal to the correction it belongs to;
  3. it carries the per-upload ``mathcorrectToken`` we generated;
  4. we still OWN it, so the delete is even permitted.

A filename is never evidence. A teacher has files called "Report.pdf" too, and
the app only ever sees files created with the ``drive.file`` scope, so a
foreign file cannot even be read - let alone deleted.

Cleanup failure is NOT a correction failure (§12): the report was attached and
the grade was returned; only a best-effort housekeeping step failed. That is
recorded as a separate, retryable state.
"""

from __future__ import annotations

import logging
import time
import uuid
from typing import Any

from ..db import execute, query, query_one
from . import client as gc

log = logging.getLogger("classroom.report_cleanup")

# --- lifecycle states (§11) -------------------------------------------------
LIFECYCLE_NONE = "NONE"
LIFECYCLE_CREATED = "CREATED"
LIFECYCLE_UPLOADED = "UPLOADED"
LIFECYCLE_ATTACHED = "ATTACHED"
LIFECYCLE_CONFIRMED = "CONFIRMED"
LIFECYCLE_CLEANUP_PENDING = "CLEANUP_PENDING"
LIFECYCLE_CLEANED = "CLEANED"
LIFECYCLE_CLEANUP_FAILED = "CLEANUP_FAILED"
#: The report is attached to a Classroom submission and the Drive file is
#: deliberately KEPT because Classroom references it. Never delete one of
#: these - doing so is the "document couldn't be opened" bug.
LIFECYCLE_RETAINED = "RETAINED"

#: Houskeeping still outstanding for a file that is safe to delete (never an
#: attached file). Only these are ever swept or retried.
PENDING_CLEANUP = (LIFECYCLE_CLEANUP_PENDING, LIFECYCLE_CLEANUP_FAILED)

#: Lifecycles whose Drive file is referenced by a live Classroom attachment
#: and must therefore NEVER be deleted by any housekeeping path.
RETAINED_LIFECYCLES = (LIFECYCLE_RETAINED, LIFECYCLE_CONFIRMED,
                       LIFECYCLE_ATTACHED)

#: A temporary report older than this with no owning correction is an orphan.
DEFAULT_ORPHAN_AGE_SECONDS = 24 * 60 * 60

MAX_CLEANUP_ATTEMPTS = 5


def new_temp_token() -> str:
    """A fresh, unguessable marker proving a file is ours."""
    return uuid.uuid4().hex


def stamp_properties(result_id: str, token: str) -> dict[str, str]:
    """The appProperties stamped on every temporary report we create."""
    return {
        gc.APP_PROP_TEMP: "true",
        gc.APP_PROP_RESULT: str(result_id),
        gc.APP_PROP_TOKEN: token,
        gc.APP_PROP_CREATED: str(int(time.time())),
    }


# ------------------------------------------------------------- persistence

_LIFECYCLE_COLUMNS = (
    "lifecycle", "owner_email", "is_temp", "temp_token", "cleanup_attempts",
    "cleanup_error", "created_at", "previous_drive_file_id",
)


def set_lifecycle(result_id: str, lifecycle: str, **fields: Any) -> None:
    """Advance (or record) the file lifecycle for one correction."""
    allowed = {k: v for k, v in fields.items() if k in _LIFECYCLE_COLUMNS}
    if not allowed:
        execute("UPDATE report_attachments SET lifecycle = ? WHERE result_id = ?",
                (lifecycle, result_id))
        return
    sets = ", ".join(f"{k} = ?" for k in allowed)
    execute(
        f"UPDATE report_attachments SET lifecycle = ?, {sets} WHERE result_id = ?",
        (lifecycle, *allowed.values(), result_id))


def state_for(result_id: str) -> dict | None:
    row = query_one("SELECT * FROM report_attachments WHERE result_id = ?",
                    (result_id,))
    return dict(row) if row else None


def record_upload(result_id: str, *, drive_file_id: str, owner_email: str | None,
                  temp_token: str, previous_drive_file_id: str | None = None,
                  content_bytes: bytes | None = None) -> None:
    """Record a freshly created temporary file for this correction."""
    execute(
        """
        UPDATE report_attachments SET
            lifecycle = ?, drive_file_id = ?, owner_email = ?, is_temp = 1,
            temp_token = ?, previous_drive_file_id = ?, created_at = ?,
            content_bytes = COALESCE(?, content_bytes)
        WHERE result_id = ?
        """,
        (LIFECYCLE_UPLOADED, drive_file_id, (owner_email or None), temp_token,
         previous_drive_file_id, time.time(), content_bytes, result_id))


def record_retained(result_id: str) -> None:
    """Mark a report's Drive file as RETAINED for its Classroom attachment.

    The file id is KEPT (Classroom references it); only the retry buffer is
    dropped, since the PDF can always be re-rendered from the stored result.
    This is the state a verified attachment ends in - not CLEANED.
    """
    execute(
        "UPDATE report_attachments SET lifecycle = ?, content_bytes = NULL, "
        "cleanup_error = NULL WHERE result_id = ?",
        (LIFECYCLE_RETAINED, result_id))


# ------------------------------------------------------------------ cleanup

def cleanup_result(result_id: str, token: str, *,
                   account_email: str | None = None) -> dict:
    """Delete the temporary Drive file for one CONFIRMED attachment.

    Returns a small report; never raises for an expected condition. A refusal
    to delete is recorded (``cleanup_attempts``/``cleanup_error``) so it can be
    retried by :func:`retry_pending_cleanup` without touching the PDF again.
    """
    state = state_for(result_id)
    if not state:
        return {"ok": False, "skipped": "no_state", "result_id": result_id}

    lifecycle = state.get("lifecycle")
    # A file Classroom depends on is NEVER deleted. This is the fix for the
    # "document couldn't be opened / may have been deleted" bug: the attachment
    # is a reference to this Drive object, so removing it breaks the student.
    if lifecycle in RETAINED_LIFECYCLES:
        if lifecycle != LIFECYCLE_RETAINED:
            record_retained(result_id)
        return {"ok": True, "skipped": "retained", "result_id": result_id,
                "reason": ("The report is attached to a Classroom submission; "
                           "its Drive file is kept so the student can open it.")}
    if lifecycle in (LIFECYCLE_CLEANED, LIFECYCLE_NONE, None) and not \
            state.get("drive_file_id"):
        return {"ok": True, "skipped": "nothing_to_clean", "result_id": result_id}

    file_id = state.get("drive_file_id")
    if not file_id:
        execute(
            "UPDATE report_attachments SET lifecycle = ?, cleanup_error = NULL "
            "WHERE result_id = ?", (LIFECYCLE_CLEANED, result_id))
        return {"ok": True, "skipped": "no_file", "result_id": result_id}

    # Refuse unless we can PROVE the file is ours and temporary (§17).
    try:
        meta = gc.get_drive_file(token, file_id)
    except Exception as exc:  # noqa: BLE001
        # Any failure - a transient Drive error OR a dead authorization - is
        # recorded as a retryable cleanup failure, never raised at the teacher.
        return _cleanup_failed(result_id, state,
                               f"drive_lookup_failed: {_reason(exc)}")

    if meta is None:
        execute(
            "UPDATE report_attachments SET lifecycle = ?, cleanup_error = NULL "
            "WHERE result_id = ?", (LIFECYCLE_CLEANED, result_id))
        return {"ok": True, "skipped": "already_deleted", "result_id": result_id}

    if not gc.is_our_temp_report(
            meta, result_id=result_id, token_value=state.get("temp_token")):
        return _cleanup_failed(
            result_id, state,
            "refused: the Drive file is not provably a temporary report of "
            "this correction (missing app marker); it was left untouched")

    owners = gc.drive_file_owners(meta)
    account = (account_email or "").strip().lower()
    if account and account not in owners:
        return _cleanup_failed(
            result_id, state,
            f"refused: this account no longer owns the file (owner="
            f"{owners or 'unknown'}); the file was left untouched")

    try:
        gc.delete_drive_file(token, file_id)
    except Exception as exc:  # noqa: BLE001
        return _cleanup_failed(result_id, state,
                               f"drive_delete_failed: {_reason(exc)}")

    # The bytes go too: a report is a transport artifact, not an archive (§10).
    execute(
        "UPDATE report_attachments SET lifecycle = ?, content_bytes = NULL, "
        "drive_file_id = NULL, cleanup_error = NULL, temp_token = NULL "
        "WHERE result_id = ?", (LIFECYCLE_CLEANED, result_id))
    log.info("report temp file deleted: result=%s file=%s", result_id, file_id)
    return {"ok": True, "deleted": file_id, "result_id": result_id}


def _reason(exc: Exception) -> str:
    """A short, safe description of a Drive/Google failure.

    Only the status and the API's own message are used - never a token, and
    never a whole traceback, because this text is stored and shown to the
    teacher (§3, §21).
    """
    status = getattr(exc, "status", None)
    detail = getattr(exc, "detail", None) or str(exc)
    return f"{status} {detail}".strip()[:200]


def _cleanup_failed(result_id: str, state: dict, reason: str) -> dict:
    """Record a cleanup failure WITHOUT failing the correction (§12)."""
    attempts = int(state.get("cleanup_attempts") or 0) + 1
    execute(
        "UPDATE report_attachments SET lifecycle = ?, cleanup_attempts = ?, "
        "cleanup_error = ? WHERE result_id = ?",
        (LIFECYCLE_CLEANUP_FAILED, attempts, reason[:400], result_id))
    log.warning("report cleanup pending (result=%s): %s", result_id, reason)
    return {"ok": False, "skipped": "cleanup_failed", "reason": reason,
            "attempts": attempts, "result_id": result_id}


def retry_pending_cleanup(token: str, *, account_email: str | None = None,
                          limit: int = 50) -> dict:
    """Retry deletion of every temporary file still pending cleanup (§13).

    Never regenerates a report, never re-marks, never creates another Drive
    file - it only re-issues the delete for files we already own.
    """
    rows = query(
        "SELECT result_id FROM report_attachments "
        f"WHERE lifecycle IN ({','.join('?' * len(PENDING_CLEANUP))}) "
        "AND drive_file_id IS NOT NULL ORDER BY updated_at ASC LIMIT ?",
        (*PENDING_CLEANUP, limit))
    done, failed = [], []
    for row in rows:
        # One unreachable file must never abort the whole sweep: the rest are
        # still worth cleaning, and this one is retried next time (§13).
        try:
            outcome = cleanup_result(row["result_id"], token,
                                     account_email=account_email)
        except Exception as exc:  # noqa: BLE001
            outcome = {"ok": False, "skipped": "error",
                       "reason": _reason(exc), "result_id": row["result_id"]}
        (done if outcome.get("ok") else failed).append(outcome)
    if rows:
        log.info("report cleanup sweep: %d attempted, %d cleaned, %d pending",
                 len(rows), len(done), len(failed))
    return {"attempted": len(rows), "cleaned": len(done),
            "pending": len(failed), "results": failed}


def sweep_orphans(token: str, *, account_email: str | None = None,
                  older_than: int = DEFAULT_ORPHAN_AGE_SECONDS) -> dict:
    """Delete our own orphaned temporary reports (§16).

    Scans Drive for files stamped with our temporary marker that no live
    correction still needs, and older than ``older_than``. Only files carrying
    ALL of our markers are ever considered, and only ones we still own — a
    teacher's files, student files and mark schemes are untouchable (§17).
    """
    try:
        files = gc.find_temp_reports(token)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": _reason(exc), "scanned": 0,
                "deleted": [], "kept": []}

    live = {r["result_id"] for r in query(
        "SELECT result_id FROM report_attachments "
        f"WHERE lifecycle IN ({','.join('?' * len(PENDING_CLEANUP))}) "
        "AND drive_file_id IS NOT NULL", PENDING_CLEANUP)}
    owners = {r["drive_file_id"] for r in query(
        "SELECT drive_file_id FROM report_attachments "
        "WHERE drive_file_id IS NOT NULL")}

    account = (account_email or "").strip().lower()
    cutoff = time.time() - max(0, older_than)
    deleted, kept = [], []
    for meta in files:
        file_id = meta.get("id")
        props = meta.get("appProperties") or {}
        result_id = str(props.get(gc.APP_PROP_RESULT) or "")
        if file_id in owners or result_id in live:
            kept.append({"file_id": file_id, "why": "still referenced"})
            continue
        try:
            created = int(props.get(gc.APP_PROP_CREATED) or 0)
        except (TypeError, ValueError):
            created = 0
        if created and created > cutoff:
            kept.append({"file_id": file_id, "why": "too recent"})
            continue
        file_owners = gc.drive_file_owners(meta)
        if account and account not in file_owners:
            kept.append({"file_id": file_id, "why": "not owned by this account"})
            continue
        try:
            gc.delete_drive_file(token, file_id)
            deleted.append({"file_id": file_id, "result_id": result_id})
        except Exception as exc:  # noqa: BLE001
            kept.append({"file_id": file_id, "why": f"delete failed: {_reason(exc)}"})
    if deleted:
        log.info("report orphan sweep removed %d temporary file(s)", len(deleted))
    return {"ok": True, "scanned": len(files), "deleted": deleted, "kept": kept}
