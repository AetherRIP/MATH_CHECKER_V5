"""Remark (re-mark) for a single student correction (§Remark).

What a remark is
----------------
Re-running the AI marking for ONE already-corrected submission. It must never:

* delete the previous correction,
* create a new Classroom submission / student / assignment,
* silently overwrite a valid result with a worse or invalid one.

How that is guaranteed
----------------------
A remark job is flagged ``correction_jobs.is_remark = 1``. When the worker saves
its attempt, :func:`versions.record_pending` stores the full attempt — marks,
question detail, model, validation, confidence — but leaves the **active**
correction untouched. So after a remark:

* the teacher still sees the original 68/75,
* the new attempt is there in full, side by side,
* nothing has been applied, and nothing can have been destroyed.

Only :func:`decide` applies anything, and only when the teacher asks. That is why
"re-mark" can never become a way to accidentally lose a valid correction.
"""

from __future__ import annotations

from typing import Any

from .. import db
from . import versions
from .result_guard import percentage_of

# Teacher decisions after a remark.
DECISION_KEEP_PREVIOUS = "KEEP_PREVIOUS"
DECISION_USE_NEW = "USE_NEW"
DECISIONS = (DECISION_KEEP_PREVIOUS, DECISION_USE_NEW)

NOTE_REMARK = "Remark attempt"

# Confidence values that mean "do not trust this without looking".
_UNTRUSTED = ("LOW", "VALIDATION_FAILED", "LARGE_SCORE_CHANGE",
              "SUBMISSION_REVIEW_REQUIRED")



def _questions(result_json: Any) -> dict[str, dict]:
    """``{question_number: {awarded, max, status}}`` from a stored result."""
    payload = db.loads(result_json, result_json) if isinstance(result_json, str) \
        else (result_json or {})
    out: dict[str, dict] = {}
    for q in (payload or {}).get("questions") or []:
        if not isinstance(q, dict):
            continue
        number = str(q.get("question_number", "")).strip()
        if number:
            out[number] = {"awarded": q.get("awarded_marks"),
                           "max": q.get("max_marks"), "status": q.get("status")}
    return out


def _side(version: dict | None) -> dict:
    if not version:
        return {"version": None, "earned": None, "available": None,
                "percentage": 0.0, "provisional_grade": None,
                "marking_model": None, "confidence": None, "verified": None,
                "created_at": None, "pending": False, "reliable": False}
    verification = db.loads(version.get("verification_json"), {}) or {}
    confidence = version.get("confidence")
    return {
        "version": version.get("version"),
        "earned": version.get("earned"),
        "available": version.get("available"),
        "percentage": round(percentage_of(version.get("earned"), version.get("available")), 2),
        "provisional_grade": version.get("provisional_grade"),
        "marking_model": version.get("marking_model"),
        "confidence": confidence,
        "verified": verification.get("status"),
        "created_at": version.get("created_at"),
        "pending": bool(version.get("pending")),
        # An attempt that failed validation is never offered as a sensible
        # default - the teacher can still pick it, but it is labelled first.
        "reliable": confidence not in _UNTRUSTED,
    }


def compare(previous: dict | None, new: dict | None) -> dict:
    """The "Remark Comparison" payload: totals plus every changed question.

    Only questions whose awarded marks actually moved are listed, which is what
    makes it obvious at a glance what the re-mark changed.
    """
    prev_q = _questions((previous or {}).get("result_json"))
    new_q = _questions((new or {}).get("result_json"))

    changed: list[dict] = []
    for number in sorted(set(prev_q) | set(new_q), key=lambda n: (len(n), n)):
        before, after = prev_q.get(number), new_q.get(number)
        if before and after and before["awarded"] == after["awarded"]:
            continue
        changed.append({
            "question_number": number,
            "previous": (before or {}).get("awarded"),
            "new": (after or {}).get("awarded"),
            "max": (after or before or {}).get("max"),
            "kind": "added" if not before else "removed" if not after else "changed",
        })

    prev_side, new_side = _side(previous), _side(new)
    delta = None
    if new_side["earned"] is not None and prev_side["earned"] is not None:
        delta = (new_side["earned"] or 0) - (prev_side["earned"] or 0)
    return {
        "previous": prev_side,
        "new": new_side,
        "delta_points": delta,
        "percentage_delta": round(new_side["percentage"] - prev_side["percentage"], 2),
        "questions": changed,
        "question_changes": len(changed),
    }


def _active(result_id: str) -> dict | None:
    """The attempt that is currently the correction.

    Falls back to the ``results`` row itself: corrections stored before the
    versioning feature existed (or written by any path that did not snapshot)
    have no ``is_current`` version row, and a remark against one of those must
    still have a baseline to compare against.
    """
    row = db.query_one(
        "SELECT * FROM result_versions WHERE result_id = ? AND is_current = 1 "
        "ORDER BY version DESC LIMIT 1", (result_id,))
    if row:
        return dict(row)
    current = db.query_one(
        "SELECT id, earned, available, percentage, provisional_grade, "
        "marking_model, confidence, verification_json, result_json, created_at "
        "FROM results WHERE id = ?", (result_id,))
    if not current:
        return None
    return {**dict(current), "version": None, "is_current": 1, "pending": 0}


def _pending(result_id: str) -> dict | None:
    row = db.query_one(
        "SELECT * FROM result_versions WHERE result_id = ? AND pending = 1 "
        "ORDER BY version DESC LIMIT 1", (result_id,))
    return dict(row) if row else None


def summary(result_id: str) -> dict:
    """Which attempt is active, and is a newer attempt awaiting a decision?"""
    active, pending = _active(result_id), _pending(result_id)
    count = int(db.query_one(
        "SELECT COUNT(*) AS n FROM result_versions WHERE result_id = ?",
        (result_id,))["n"])
    return {
        "result_id": result_id,
        "active": _side(active),
        "pending": _side(pending) if pending else None,
        "pending_version": pending["version"] if pending else None,
        "comparison": compare(active, pending) if pending else None,
        "can_use_new": bool(pending),
        "awaiting_decision": bool(pending),
        "attempt_count": count,
    }


def decide(result_id: str, decision: str, *, version: int | None = None,
           note: str = "Teacher decision after remark") -> dict:
    """Apply the teacher's choice of which attempt is the active correction.

    ``KEEP_PREVIOUS`` dismisses the pending attempt and leaves the correction
    exactly as it is. ``USE_NEW`` promotes it. No attempt is ever deleted, so
    the history stays browsable and the choice is reversible.
    """
    decision = (decision or "").upper()
    if decision not in DECISIONS:
        raise ValueError("decision must be KEEP_PREVIOUS or USE_NEW")

    active = _active(result_id)
    if active is None:
        raise LookupError("This correction has no attempt history.")

    if decision == DECISION_KEEP_PREVIOUS:
        db.execute("UPDATE result_versions SET pending = 0, note = ? "
                   "WHERE result_id = ? AND pending = 1",
                   (f"{note} — kept attempt {active['version']}", result_id))
        return {"result_id": result_id, "decision": decision,
                "active_version": active["version"], "forced_review": False,
                "summary": summary(result_id)}

    target = _pending(result_id) if version is None else (
        db.query_one("SELECT * FROM result_versions WHERE result_id = ? AND version = ?",
                     (result_id, int(version))))
    if target is None:
        raise ValueError("That attempt does not exist.")
    target = dict(target)
    if target["version"] == active["version"] and active.get("version") is not None:
        raise ValueError("That attempt is already the active correction.")

    # Promoting an attempt that failed validation is allowed - the teacher is in
    # charge - but it is flagged for review rather than silently trusted.
    forced = target.get("confidence") in _UNTRUSTED

    with db.transaction() as conn:
        conn.execute("UPDATE result_versions SET is_current = 0 WHERE result_id = ?",
                     (result_id,))
        conn.execute(
            "UPDATE result_versions SET is_current = 1, pending = 0, note = ? "
            "WHERE result_id = ? AND version = ?",
            (f"{note} — using attempt {target['version']}", result_id,
             target["version"]))
        conn.execute(
            """
            UPDATE results SET earned = ?, available = ?, percentage = ?,
                provisional_grade = ?, marking_model = ?, confidence = ?,
                verification_json = ?, result_json = ?, review_required = ?
            WHERE id = ?
            """,
            (target["earned"], target["available"], target["percentage"],
             target["provisional_grade"], target["marking_model"],
             target["confidence"], target["verification_json"],
             target["result_json"], 1 if forced else 0, result_id))

    return {"result_id": result_id, "decision": decision,
            "active_version": target["version"], "forced_review": forced,
            "summary": summary(result_id)}