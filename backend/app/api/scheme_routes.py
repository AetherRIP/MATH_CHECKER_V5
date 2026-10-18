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

from fastapi import APIRouter, Depends, Request

from .. import auth_gate, db
from ..errors import AppError
from ..engine import scheme_audit
from ..logging_setup import log_event

def _authorized_reviewer(request: Request) -> None:
    # These assignment-wide views are not class-scoped. Never expose them to
    # assistants/co-teachers until their scope can be enforced here.
    auth_gate.require_role(request, "TEACHER", "DEVELOPER")


router = APIRouter(prefix="/api/scheme", dependencies=[Depends(_authorized_reviewer)])

log = logging.getLogger("api.scheme")


def register(app) -> None:
    app.include_router(router)


def _actor(request: Request) -> str:
    """The authorised reviewer's name. App authentication gates who may reach
    these routes at all; the name is recorded for the audit trail."""
    identity = auth_gate.identity_of(request)
    if identity:
        return str(identity.get("account_id") or identity.get("username"))[:200]
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
        "proposals": [scheme_audit.proposal_detail(p["id"])
                      for p in scheme_audit.list_proposals(limit=100)
                      if p["discrepancy_id"] == discrepancy_id],
    }


@router.post("/discrepancies/{discrepancy_id}/decision")
def decide(discrepancy_id: str, payload: dict, request: Request):
    """Record an authorised reviewer's outcome on a discrepancy (section 8)."""
    decision = str((payload or {}).get("decision") or "").upper()
    if not decision:
        raise _bad_request("A decision is required.")
    try:
        row = scheme_audit.record_decision(
            discrepancy_id, decision=decision, decided_by=_actor(request),
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


def _classroom_publish(coursework_id: str | None, course_id: str | None,
                       items: list[dict]) -> dict:
    """Publish approved item grades via the existing Classroom managed flow.

    ``items`` are the APPLIED rows of a proposal (the exact shape the engine
    passes to its publish callback). Only called AFTER explicit approval has
    been recorded, and every write is the real Google response: nothing is
    reported as published without it. The returned dict is normalised so the
    engine's outcome accounting never has to guess at the provider's nesting.
    """
    from ..classroom import managed as managed_mod
    if not coursework_id or not course_id:
        return {"error": "This proposal is not linked to a Classroom assignment."}
    from ..automation.grading import compute_grade
    from ..automation.runner import WorkflowRunner
    operations = []
    for item in items or []:
        if not item.get("result_id") or not item.get("submission_id"):
            continue
        row = db.query_one("SELECT earned, available FROM results WHERE id = ?",
                           (item["result_id"],))
        if not row or not row["available"]:
            continue
        grade = int(compute_grade(row["earned"], row["available"],
                                  coursework_id=coursework_id).grade)
        operations.append({"type": "final_grade",
                           "submission_id": item["submission_id"],
                           "grade": grade})
    if not operations:
        return {"published": 0, "failed": 0, "not_supported": 0,
                "requires_auth": 0, "total": 0, "results": []}
    report = managed_mod.publish(coursework_id, course_id, operations) or {}
    summary = dict(report.get("summary") or {})
    # A PATCH/return response alone is not evidence of the resulting grade.
    # Verify the final assigned value AND returned state for each success.
    verifier = WorkflowRunner()
    for outcome in report.get("results") or []:
        if outcome.get("status") != managed_mod.STATUS_PUBLISHED:
            continue
        sid = outcome.get("submission_id")
        operation = next((op for op in operations if op["submission_id"] == sid), None)
        try:
            if operation is None:
                raise RuntimeError("No approved operation matched the publication outcome.")
            remote = verifier._read_submission({"course_id": course_id,
                                               "coursework_id": coursework_id,
                                               "submission_id": sid})
            if remote.get("assignedGrade") is None or \
                    abs(float(remote["assignedGrade"]) - operation["grade"]) > 1e-6 or \
                    remote.get("state") != "RETURNED":
                raise RuntimeError("Classroom did not confirm the approved grade and returned state.")
        except Exception:
            outcome["status"] = managed_mod.STATUS_FAILED
            outcome["message"] = "Grade return could not be verified; inspect Classroom before retrying."
            summary["published"] = max(0, int(summary.get("published") or 0) - 1)
            summary["failed"] = int(summary.get("failed") or 0) + 1
            if outcome.get("id"):
                db.execute("UPDATE classroom_publish_ops SET status='failed', message=? WHERE id=?",
                           (outcome["message"], outcome["id"]))
    return {
        "published": int(summary.get("published") or 0),
        "failed": int(summary.get("failed") or 0),
        "not_supported": int(summary.get("not_supported") or 0),
        "requires_auth": int(summary.get("requires_auth") or 0),
        "total": int(summary.get("total") or len(operations)),
        "results": report.get("results") or [],
    }


@router.post("/proposals/{proposal_id}/approve")
def approve(proposal_id: str, payload: dict, request: Request):
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
            # Local correction is independently valuable and Classroom may be
            # temporarily unavailable, so we apply the approved marks locally
            # and report the REAL write-back outcome honestly. The assignment's
            # Classroom identity comes from the proposal itself (the engine's
            # publish callback receives only the applied items).
            detail = scheme_audit.proposal_detail(proposal_id)
            cw_id = detail.get("coursework_id")
            course_id = detail.get("course_id")
            result = scheme_audit.apply_approved(
                proposal_id, approved_by=_actor(request),
                note=str(payload.get("note") or ""), publish=True,
                publish_fn=lambda applied: _classroom_publish(cw_id, course_id,
                                                              applied))
        else:
            result = scheme_audit.apply_approved(
                proposal_id, approved_by=_actor(request),
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
def reject(proposal_id: str, payload: dict, request: Request):
    payload = payload or {}
    try:
        return scheme_audit.reject_proposal(
            proposal_id, rejected_by=_actor(request),
            note=str(payload.get("note") or ""))
    except ValueError as exc:
        raise _bad_request(str(exc)) from exc
    except LookupError as exc:
        raise _not_found(str(exc)) from exc


@router.post("/proposals/{proposal_id}/retry")
def retry(proposal_id: str, payload: dict, request: Request):
    if (payload or {}).get("confirm") is not True:
        raise _bad_request("Confirm recovery of the failed items in this approved batch.")
    try:
        return scheme_audit.apply_approved(proposal_id, approved_by=_actor(request),
                                           retry_failed=True)
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
