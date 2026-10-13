"""Teacher-facing mark-scheme review + approval-gated bulk correction routes.

These routes expose the engine's discrepancy records and its STAGED bulk
proposals. Creating a proposal changes nothing; the ONLY route that can change
previously graded marks is ``POST /api/scheme/proposals/{id}/approve`` and it
requires an explicit ``confirm: true`` in the request body (opening the review
screen, dismissing a notification or enabling automatic correction is NOT
approval). The original mark scheme is never modified by any of these routes.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter

from .. import db
from ..errors import AppError
from ..engine import scheme_audit
from ..logging_setup import log_event

router = APIRouter(prefix="/api/scheme")

log = logging.getLogger("api.scheme")


def register(app) -> None:
    app.include_router(router)


def _actor(payload: dict | None = None) -> str:
    """The authorised reviewer's name. App authentication gates who may reach
    these routes at all; the name is recorded for the audit trail."""
    if payload and str(payload.get("actor") or "").strip():
        return str(payload["actor"]).strip()[:200]
    try:
        from ..config import get_config
        return (get_config().teacher.teacher_name or "").strip() or "teacher"
    except Exception:  # noqa: BLE001 - identity must never block a decision
        return "teacher"


def _bad_request(message: str) -> AppError:
    return AppError(message, code="bad_request", status=400)


def _not_found(message: str) -> AppError:
    return AppError(message, code="not_found", status=404)


# ================================================================ discrepancies

@router.get("/discrepancies")
def list_discrepancies(coursework_id: str | None = None,
                       review_status: str | None = None):
    items = scheme_audit.list_discrepancies(
        coursework_id=coursework_id, review_status=review_status)
    return {"items": items, "total": len(items)}


@router.get("/discrepancies/{discrepancy_id}")
def get_discrepancy(discrepancy_id: str):
    row = scheme_audit.get_discrepancy(discrepancy_id)
    if row is None:
        raise _not_found("That discrepancy does not exist.")
    return {
        "discrepancy": row,
        "review_history": scheme_audit.review_history(discrepancy_id),
        "proposals": scheme_audit.list_proposals(limit=50),
    }


@router.post("/discrepancies/{discrepancy_id}/decision")
def decide(discrepancy_id: str, payload: dict):
    """Record an authorised reviewer's outcome on a discrepancy (section 8)."""
    decision = str((payload or {}).get("decision") or "").upper()
    if not decision:
        raise _bad_request("A decision is required.")
    try:
        row = scheme_audit.record_decision(
            discrepancy_id, decision=decision, decided_by=_actor(payload),
            note=str((payload or {}).get("note") or ""))
    except ValueError as exc:
        raise _bad_request(str(exc)) from exc
    except LookupError as exc:
        raise _not_found(str(exc)) from exc
    log_event("api.scheme", "discrepancy decision", discrepancy_id=discrepancy_id,
              decision=decision)
    return {"discrepancy": row}


# ================================================================ proposals

@router.get("/proposals")
def list_proposals(coursework_id: str | None = None, status: str | None = None):
    items = scheme_audit.list_proposals(coursework_id=coursework_id,
                                        status=status)
    return {"items": items, "total": len(items)}


@router.post("/discrepancies/{discrepancy_id}/propose")
def propose(discrepancy_id: str):
    """Stage a bulk correction. Changes NOTHING."""
    try:
        return scheme_audit.build_proposal(discrepancy_id)
    except LookupError as exc:
        raise _not_found(str(exc)) from exc


@router.get("/proposals/{proposal_id}")
def get_proposal(proposal_id: str):
    try:
        return scheme_audit.proposal_detail(proposal_id)
    except LookupError as exc:
        raise _not_found(str(exc)) from exc


def _classroom_publish(proposal: dict):
    """Publish approved grades via the existing Classroom managed flow.

    Only called AFTER explicit approval has been recorded. Every write is the
    real Google response; nothing is reported as published without it.
    """
    from ..classroom import managed as managed_mod
    coursework_id = proposal.get("coursework_id")
    course_id = proposal.get("course_id")
    if not coursework_id or not course_id:
        return {"error": "This proposal is not linked to a Classroom assignment."}
    cw = db.query_one("SELECT * FROM coursework WHERE id = ?", (coursework_id,))
    max_points = cw["max_points"] if cw else None
    operations = []
    for item in proposal.get("items") or []:
        if item.get("status") != scheme_audit.ITEM_APPLIED:
            continue
        row = db.query_one("SELECT earned, available FROM results WHERE id = ?",
                           (item["result_id"],))
        if not row or not row["available"]:
            continue
        if max_points:
            grade = round((row["earned"] / row["available"]) * max_points, 2)
        else:
            grade = round(float(row["earned"]), 2)
        operations.append({"type": "assign_grade",
                           "submission_id": item["submission_id"],
                           "grade": grade})
    if not operations:
        return {"published": 0, "failed": 0, "total": 0}
    return managed_mod.publish(coursework_id, course_id, operations)


@router.post("/proposals/{proposal_id}/approve")
def approve(proposal_id: str, payload: dict):
    """Apply an approved bulk correction. Requires explicit confirmation.

    ``confirm: true`` is the deliberate approval action; a missing/false value
    is refused so a stray click can never apply a bulk grade change.
    """
    payload = payload or {}
    if payload.get("confirm") is not True:
        raise _bad_request(
            "Bulk grade changes require explicit confirmation "
            "(send {\"confirm\": true} after reviewing the proposal).")
    if int(payload.get("affected_count") or -1) not in (-1,):
        # Optional optimistic-concurrency check: the client may echo the count
        # it reviewed; a mismatch means the proposal changed under it.
        current = db.query_one(
            "SELECT affected_count FROM bulk_correction_proposals WHERE id = ?",
            (proposal_id,))
        if current and payload.get("affected_count") is not None and \
                int(payload["affected_count"]) != int(current["affected_count"]):
            raise AppError(
                "The proposal changed since it was displayed; review it again.",
                code="conflict", status=409)
    publish = bool(payload.get("publish"))
    try:
        if publish:
            # Reject up-front if the assignment cannot be published, so the
            # local marks are not changed for a write-back that cannot happen?
            # No: local correction is independently valuable, and Classroom may
            # be temporarily unavailable. We apply locally, then report the
            # real publish outcome honestly.
            detail = scheme_audit.proposal_detail(proposal_id)
            result = scheme_audit.apply_approved(
                proposal_id, approved_by=_actor(payload),
                note=str(payload.get("note") or ""), publish=True,
                publish_fn=_classroom_publish)
        else:
            result = scheme_audit.apply_approved(
                proposal_id, approved_by=_actor(payload),
                note=str(payload.get("note") or ""))
    except ValueError as exc:
        raise _bad_request(str(exc)) from exc
    except LookupError as exc:
        raise _not_found(str(exc)) from exc
    log_event("api.scheme", "bulk proposal approved", proposal_id=proposal_id,
              status=result.get("status"), applied=result.get("applied_count"),
              failed=result.get("failed_count"))
    return result


@router.post("/proposals/{proposal_id}/reject")
def reject(proposal_id: str, payload: dict):
    payload = payload or {}
    try:
        return scheme_audit.reject_proposal(
            proposal_id, rejected_by=_actor(payload),
            note=str(payload.get("note") or ""))
    except ValueError as exc:
        raise _bad_request(str(exc)) from exc
    except LookupError as exc:
        raise _not_found(str(exc)) from exc


# ================================================================ summary

@router.get("/summary")
def summary(coursework_id: str | None = None):
    """Counts for the teacher dashboard / attention badge."""
    open_rows = scheme_audit.list_discrepancies(
        coursework_id=coursework_id, review_status=scheme_audit.REVIEW_AWAITING)
    proposals = scheme_audit.list_proposals(coursework_id=coursework_id,
                                            status=scheme_audit.PROPOSAL_PROPOSED)
    return {
        "open_discrepancies": len(open_rows),
        "confirmed": sum(1 for r in open_rows
                         if r["state"] == scheme_audit.STATE_CONFIRMED),
        "awaiting_approval": len(proposals),
        "items": open_rows,
    }
