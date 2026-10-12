"""Eligible-submission detection, stable identity and version handling.

Spec §8-§11. During each polling cycle every enabled assignment is examined and
only GENUINELY new / unprocessed submission versions are queued.

Identity is by stable Google Classroom ids only — never a student name, a PDF
filename, a nickname or an assignment title (§9):

    course_id + coursework_id + student_id + submission_id + submission_version

A student may submit, be corrected, then resubmit. The version identifier
distinguishes the two: the SAME version is never corrected twice, while a NEW
version legitimately queues a new correction, preserving the previous version's
history (§10, §48).
"""

from __future__ import annotations

import hashlib
import logging

from .. import db
from . import states, store

log = logging.getLogger("automation.detection")

#: Raw Classroom states that mean the student has handed work in and it has
#: NOT yet been returned. RETURNED is deliberately excluded: returning the work
#: is an action THIS app takes, so a returned submission must never be queued as
#: a new correction — only a genuine resubmission (which Classroom reports as
#: TURNED_IN / STUDENT_EDITED_AFTER_TURN_IN again) is (§10, §11).
HANDED_IN_STATES = {"TURNED_IN", "STUDENT_EDITED_AFTER_TURN_IN"}


#: States that mean a STUDENT handed the work in (their action, not ours).
_STUDENT_HANDED_IN = {"TURNED_IN", "STUDENT_EDITED_AFTER_TURN_IN"}


def submission_version(row: dict) -> str:
    """A stable, collision-resistant identifier for one submission VERSION.

    Built ONLY from student-driven facts: the newest state-history timestamp
    that records the work being handed in, plus the attachment ids. It
    deliberately does NOT use the row's raw state or ``updateTime`` when the
    state is RETURNED, because returning the work is an action THIS app takes —
    using it would make an already-corrected submission look like a fresh
    version and queue a duplicate correction (§10, §11).

    A genuine resubmission advances the student's turn-in timestamp (or swaps
    the attachment), so it produces a new version; nothing else does. Names and
    filenames are never used (§9).
    """
    attachments = row.get("attachments") or []
    ids = sorted(str(a.get("id")) for a in attachments
                 if isinstance(a, dict) and a.get("id"))
    digest = hashlib.sha1("|".join(ids).encode("utf-8")).hexdigest()[:12]

    stamp = ""
    for entry in (row.get("submission_history") or []):
        if not isinstance(entry, dict):
            continue
        if entry.get("state") in _STUDENT_HANDED_IN and entry.get("updateTime"):
            value = str(entry["updateTime"])
            if value > stamp:
                stamp = value
    if not stamp:
        stamp = str(row.get("update_time") or "")
    return f"{stamp}#{digest}" if stamp else digest


def is_eligible(row: dict) -> tuple[bool, str]:
    """Is this tracked submission a candidate for correction right now?"""
    if row.get("synthetic"):
        return False, "no_submission_resource"
    raw = row.get("raw_state")
    if raw not in HANDED_IN_STATES:
        return False, f"state_{raw or 'unknown'}"
    attachments = [a for a in (row.get("attachments") or [])
                   if isinstance(a, dict) and a.get("kind") == "drive" and a.get("id")]
    if not attachments:
        return False, "no_attachment"
    if not row.get("student_id"):
        return False, "no_student_id"
    return True, "ok"


def detect_assignment(course_id: str, coursework_id: str,
                      *, assignment_title: str | None = None,
                      max_attempts: int = 3) -> dict:
    """Detect and queue new submission versions for ONE assignment (§8).

    Pure decision-making over already-synced tracking data — no Classroom API
    calls, so it is cheap to run every cycle. Only submissions with a stable
    id, a handed-in state and a Drive attachment become jobs, and a version
    that already has a job is skipped (§11).
    """
    from ..classroom import submissions as cs

    tracking = cs.tracking_rows(course_id=course_id, coursework_id=coursework_id)
    queued: list[dict] = []
    skipped = 0
    for row in tracking["rows"]:
        ok, reason = is_eligible(row)
        if not ok:
            skipped += 1
            continue
        submission_id = row["submission_id"]
        version = submission_version(row)
        if store.find_job(coursework_id, submission_id, version) is not None:
            # Same submission version already queued/processed — never again.
            skipped += 1
            continue
        job = store.create_job(
            course_id=course_id, coursework_id=coursework_id,
            assignment_title=assignment_title or row.get("coursework_title"),
            student_id=row.get("student_id"),
            student_name=row.get("display_name") or row.get("student_name"),
            submission_id=submission_id, submission_version=version,
            max_attempts=max_attempts)
        if job:
            queued.append(job)

    if queued:
        store.log_event_entry(
            "success", "submissions_detected",
            f"Found {len(queued)} new submission(s)", course_id=course_id,
            coursework_id=coursework_id)
    return {"coursework_id": coursework_id, "queued": [j["id"] for j in queued],
            "queued_count": len(queued), "skipped": skipped}


def known_versions(coursework_id: str, submission_id: str) -> list[str]:
    rows = db.query(
        "SELECT submission_version FROM automation_jobs WHERE coursework_id = ? "
        "AND submission_id = ? ORDER BY created_at",
        (coursework_id, submission_id))
    return [r["submission_version"] for r in rows]


def job_progress(job: dict | None) -> str | None:
    if not job:
        return None
    return states.label(job["status"])
