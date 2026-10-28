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

from pathlib import Path

from .. import auth_gate, db
from ..errors import AppError
from ..engine import scheme_audit, scheme_decisions, scheme_exclusions
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


def _scheme_blueprint(coursework_id: str | None) -> tuple[dict | None, dict | None]:
    """The cached mark-scheme record and blueprint for an assignment.

    Read-only: this never extracts a scheme, it only reads what an earlier
    correction already cached. A missing scheme answers (None, None) and the
    caller says so, rather than inventing a question list.
    """
    from ..config import data_dir
    from ..engine import blueprint as bp_mod

    if not coursework_id:
        return None, None
    path = Path(data_dir()) / "mark_schemes" / f"{coursework_id}.pdf"
    if not path.exists():
        pending = db.query_one(
            "SELECT path FROM pending_mark_schemes WHERE coursework_id = ?",
            (coursework_id,))
        if not pending or not Path(pending["path"]).exists():
            return None, None
        path = Path(pending["path"])
    try:
        record = bp_mod.load_record(bp_mod.file_hash(path)) or {}
    except OSError:
        return None, None
    blueprint = record.get("blueprint") if isinstance(record, dict) else None
    return record, (blueprint if isinstance(blueprint, dict) else None)


def _question_context(record: dict | None, blueprint: dict | None,
                      question_number: str | None) -> dict:
    """Where a question is in the ACTUAL mark scheme, and what it says.

    ``page`` is the page the blueprint found the question on, so the review
    screen can open that page of the uploaded PDF instead of describing it.
    """
    if not blueprint or not question_number:
        return {}
    question = scheme_exclusions.find(blueprint, question_number)
    if question is None:
        return {}
    page = question.get("page")
    coursework_id = str((record or {}).get("coursework_id") or "")
    return {
        "question": question.get("question_number") or question.get("number"),
        "criteria": question.get("criteria"),
        "max_marks": question.get("max_marks"),
        "printed_label": question.get("printed_label"),
        "page": page,
        # The page image is rendered from the uploaded file by the existing
        # assignment route; nothing here re-draws or rewrites the scheme.
        "page_image": (f"/api/assignments/{coursework_id}/mark-scheme/page/{page}.png"
                       if page and coursework_id else None),
    }


# ================================================================ discrepancies

@router.get("/discrepancies")
def list_discrepancies(coursework_id: str | None = None,
                       review_status: str | None = None):
    items = scheme_audit.list_discrepancies(
        coursework_id=coursework_id, review_status=review_status)
    # One blueprint read per assignment on the page, so every row can say
    # which page of the real mark scheme its question lives on — including
    # when the list spans several assignments.
    schemes: dict[str, dict | None] = {}
    ignored_cache: dict[str, dict] = {}
    for item in items:
        number = str(item.get("question_number") or "")
        cw = item.get("coursework_id") or coursework_id
        if cw not in schemes:
            schemes[cw] = _scheme_blueprint(cw)[1]
        if cw not in ignored_cache:
            ignored_cache[cw] = scheme_exclusions.ignored(cw)
        blueprint = schemes.get(cw)
        question = scheme_exclusions.find(blueprint, number) if blueprint else None
        item["scheme_page"] = (question or {}).get("page")
        item["scheme_criteria"] = (question or {}).get("criteria")
        item["printed_label"] = (question or {}).get("printed_label")
        item["ignored"] = bool(ignored_cache[cw].get(
            scheme_exclusions.canonical(number)))
    return {"items": items, "total": len(items)}


@router.get("/discrepancies/{discrepancy_id}")
def get_discrepancy(discrepancy_id: str):
    row = scheme_audit.get_discrepancy(discrepancy_id)
    if row is None:
        raise _not_found("That discrepancy does not exist.")
    coursework_id = row.get("coursework_id")
    record, blueprint = _scheme_blueprint(coursework_id)
    context = _question_context(
        {**(record or {}), "coursework_id": coursework_id},
        blueprint, row.get("question_number"))
    return {
        "discrepancy": row,
        "scheme": context,
        # What the student actually wrote, read from the result this row points
        # at. The review screen cannot judge the scheme without it, and nothing
        # here is invented: no stored answer simply means no answer to show.
        "answer": _stored_answer(row.get("student_result_id"),
                                 row.get("question_number")),
        "ignored": _is_ignored(coursework_id, row.get("question_number")),
        "review_history": scheme_audit.review_history(discrepancy_id),
        "proposals": [scheme_audit.proposal_detail(p["id"])
                      for p in scheme_audit.list_proposals(limit=100)
                      if p["discrepancy_id"] == discrepancy_id],
    }


def _stored_answer(result_id: str | None, question_number: str | None) -> dict:
    """The student's stored answer and marks for one question, if any.

    Read-only. A missing result, an unreadable payload or a question the result
    does not contain all answer with empty fields rather than a guess.
    """
    import json

    if not result_id or not question_number:
        return {}
    row = db.query_one("SELECT result_json FROM results WHERE id = ?", (result_id,))
    if not row or not row["result_json"]:
        return {}
    try:
        result = json.loads(row["result_json"])
    except (TypeError, ValueError):
        return {}
    wanted = scheme_exclusions.canonical(question_number)
    for question in (result.get("questions") or []):
        if not isinstance(question, dict):
            continue
        if scheme_exclusions.canonical(question.get("question_number")) != wanted:
            continue
        return {
            "student_answer": question.get("student_answer") or "",
            "working": question.get("working_summary") or "",
            "loss_reason": question.get("loss_reason") or "",
            "awarded": question.get("awarded_marks"),
            "max_marks": question.get("max_marks"),
        }
    return {}


def _assignment_title(coursework_id: str) -> str:
    """The teacher's own name for the assignment, falling back to Google's.

    Read-only, and empty when the row is unknown: the page has to be able to
    say which assignment it is showing, but must never invent a title.
    """
    row = db.query_one(
        "SELECT COALESCE(NULLIF(custom_title, ''), NULLIF(classroom_title, ''), "
        "title) AS name FROM coursework WHERE id = ?", (coursework_id,))
    return str(row["name"] or "") if row else ""


def _is_ignored(coursework_id: str | None, question_number: str | None) -> bool:
    """Is this question already excluded from marking for the assignment?"""
    if not coursework_id or not question_number:
        return False
    return scheme_exclusions.canonical(question_number) in scheme_exclusions.ignored(coursework_id)


# ================================================================ exclusions

@router.get("/questions")
def list_questions(coursework_id: str):
    """The questions this assignment's mark scheme declares, and which are out."""
    record, blueprint = _scheme_blueprint(coursework_id)
    if blueprint is None:
        raise _not_found("No mark scheme is attached to this assignment yet.")
    ignored = scheme_exclusions.ignored(coursework_id)
    questions = []
    for question in blueprint.get("questions") or []:
        number = str(question.get("question_number"))
        questions.append({
            "question_number": number,
            "max_marks": question.get("max_marks"),
            "page": question.get("page"),
            "printed_label": question.get("printed_label"),
            "criteria": question.get("criteria"),
            "ignored": scheme_exclusions.canonical(number) in ignored,
            "parts": [{"number": p.get("number"), "max_marks": p.get("max_marks"),
                       "ignored": scheme_exclusions.canonical(p.get("number")) in ignored}
                      for p in question.get("parts") or []],
        })
    filtered, _ = scheme_exclusions.filter_blueprint(blueprint, coursework_id)
    return {"questions": questions, "items": list(ignored.values()),
            "total_marks": blueprint.get("total_marks"),
            "marked_total": (filtered or {}).get("total_marks")}


@router.get("/ignored")
def list_ignored(coursework_id: str):
    record, blueprint = _scheme_blueprint(coursework_id)
    filtered, removed = scheme_exclusions.filter_blueprint(blueprint, coursework_id)
    return {
        "items": list(scheme_exclusions.ignored(coursework_id).values()),
        "excluded": removed,
        "total_marks": (blueprint or {}).get("total_marks"),
        "marked_total": (filtered or {}).get("total_marks"),
    }


@router.post("/ignore")
def ignore_question(payload: dict, request: Request):
    """Take one question out of marking for this assignment.

    Nothing already stored changes: this is a rule for corrections that start
    after it, and the existing results keep the marks they were given until
    they are corrected again.
    """
    coursework_id = str((payload or {}).get("coursework_id") or "").strip()
    number = str((payload or {}).get("question_number") or "").strip()
    if not coursework_id or not number:
        raise _bad_request("An assignment and a question number are required.")
    _, blueprint = _scheme_blueprint(coursework_id)
    if blueprint is None:
        raise _not_found("Attach the mark scheme before excluding a question.")
    question = scheme_exclusions.find(blueprint, number)
    if question is None:
        raise _bad_request(f"Question {number} is not in this mark scheme.")
    stored = scheme_exclusions.add(coursework_id, number,
                                   str((payload or {}).get("note") or ""))
    log_event("api.scheme", "question excluded from marking",
              coursework_id=coursework_id, question=number, by=_actor(request))
    filtered, removed = scheme_exclusions.filter_blueprint(blueprint, coursework_id)
    return {"ignored": stored, "excluded": removed,
            "marked_total": (filtered or {}).get("total_marks")}


@router.delete("/ignored/{coursework_id}/{question_number}")
def restore_question(coursework_id: str, question_number: str, request: Request):
    """Put a question back into marking."""
    removed = scheme_exclusions.remove(coursework_id, question_number)
    if not removed:
        raise _not_found(f"Question {question_number} is not excluded.")
    log_event("api.scheme", "question restored to marking",
              coursework_id=coursework_id, question=question_number,
              by=_actor(request))
    _, blueprint = _scheme_blueprint(coursework_id)
    filtered, but_removed = scheme_exclusions.filter_blueprint(blueprint, coursework_id)
    return {"restored": question_number, "still_excluded": but_removed,
            "marked_total": (filtered or {}).get("total_marks")}


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

# ============================================== per-question handling decisions

@router.get("/decisions")
def list_decisions(coursework_id: str):
    """Every question of this assignment's scheme, with its handling decision.

    The blueprint is read, never re-extracted: a review page must show exactly
    what the correction will use. A stored decision whose question has since
    disappeared from the scheme is returned under ``orphaned`` so a re-parse
    that changed the structure is visible instead of silently reattached.
    """
    record, blueprint = _scheme_blueprint(coursework_id)
    if blueprint is None:
        raise _not_found("No mark scheme is attached to this assignment yet.")
    rows, orphans = scheme_decisions.rows_for(blueprint, coursework_id)
    return {
        "coursework_id": coursework_id,
        "assignment_title": _assignment_title(coursework_id),
        "questions": rows,
        "orphaned": orphans,
        "summary": scheme_decisions.summary(rows, blueprint),
        "total_marks": blueprint.get("total_marks"),
        "labels": scheme_decisions.LABELS,
        "scheme": {
            "pages": (record or {}).get("blueprint", {}).get("paper")
                     if isinstance((record or {}).get("blueprint"), dict) else None,
            "verification": (record or {}).get("verification"),
        },
    }


@router.post("/decision")
def set_decision(payload: dict, request: Request):
    """Set one question's handling decision, keeping the previous one.

    ``SKIP`` is written through the existing exclusion table, so the marking
    worker (which already drops excluded questions before totals) needs no
    second rule that could disagree with this screen.
    """
    payload = payload or {}
    coursework_id = str(payload.get("coursework_id") or "").strip()
    number = str(payload.get("question_number") or "").strip()
    if not coursework_id or not number:
        raise _bad_request("An assignment and a question number are required.")
    _, blueprint = _scheme_blueprint(coursework_id)
    if blueprint is None:
        raise _not_found("Attach the mark scheme before deciding how to handle a question.")
    try:
        stored = scheme_decisions.set_decision(
            coursework_id, number, str(payload.get("decision") or ""),
            note=str(payload.get("note") or ""),
            reason=str(payload.get("reason") or ""),
            actor=_actor(request), blueprint=blueprint)
    except ValueError as exc:
        raise _bad_request(str(exc)) from exc
    log_event("api.scheme", "question decision", coursework_id=coursework_id,
              question=number, decision=stored.get("decision"), by=_actor(request))
    rows, orphans = scheme_decisions.rows_for(blueprint, coursework_id)
    return {"decision": stored, "questions": rows, "orphaned": orphans,
            "summary": scheme_decisions.summary(rows, blueprint)}


@router.post("/answer")
def set_answer(payload: dict, request: Request):
    """Replace the expected answer used for one question.

    The blueprint and the uploaded PDF are untouched: the edit lives in its own
    column, so clearing it restores the extraction exactly.
    """
    payload = payload or {}
    coursework_id = str(payload.get("coursework_id") or "").strip()
    number = str(payload.get("question_number") or "").strip()
    if not coursework_id or not number:
        raise _bad_request("An assignment and a question number are required.")
    if "answer" not in payload:
        raise _bad_request("Send answer (use null to restore the extracted answer).")
    _, blueprint = _scheme_blueprint(coursework_id)
    if blueprint is None:
        raise _not_found("Attach the mark scheme before editing an answer.")
    try:
        stored = scheme_decisions.set_answer(
            coursework_id, number, payload.get("answer"),
            reason=str(payload.get("reason") or ""),
            actor=_actor(request), blueprint=blueprint)
    except ValueError as exc:
        raise _bad_request(str(exc)) from exc
    log_event("api.scheme", "scheme answer edited", coursework_id=coursework_id,
              question=number, cleared=payload.get("answer") is None,
              by=_actor(request))
    rows, orphans = scheme_decisions.rows_for(blueprint, coursework_id)
    return {"question": stored, "questions": rows, "orphaned": orphans,
            "summary": scheme_decisions.summary(rows, blueprint)}


@router.get("/decision-history")
def decision_history(coursework_id: str, question_number: str | None = None):
    """Every decision and answer change made on this assignment, newest first."""
    if not coursework_id:
        raise _bad_request("An assignment is required.")
    items = scheme_decisions.history(coursework_id, question_number)
    return {"items": items, "total": len(items)}


@router.post("/finalize")
def finalize_review(payload: dict, request: Request):
    """Sign off the review. Undecided questions need explicit acknowledgement."""
    payload = payload or {}
    coursework_id = str(payload.get("coursework_id") or "").strip()
    if not coursework_id:
        raise _bad_request("An assignment is required.")
    _, blueprint = _scheme_blueprint(coursework_id)
    if blueprint is None:
        raise _not_found("There is no mark scheme to finalise.")
    try:
        result = scheme_decisions.finalize(
            coursework_id, actor=_actor(request),
            note=str(payload.get("note") or ""),
            acknowledge_unresolved=payload.get("acknowledge_unresolved") is True,
            blueprint=blueprint)
    except ValueError as exc:
        raise _bad_request(str(exc)) from exc
    log_event("api.scheme", "mark scheme review finalised",
              coursework_id=coursework_id, manual=result.get("manual"),
              skipped=result.get("skipped"),
              acknowledged=result.get("acknowledged_unresolved"))
    return result


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
