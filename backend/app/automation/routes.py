"""REST API for the automated correction workflow (§7, §43, §44, §45, §46)."""

from __future__ import annotations

import logging
import time

from fastapi import APIRouter, Request

from .. import auth_gate, db, testing_guard
from ..config import AUTOMATION_INTERVALS, get_config, save_config
from ..errors import AppError
from ..classroom import client as gc
from ..logging_setup import log_event
from . import operations, states, store
from .scheduler import AutomationSchedulerHolder

router = APIRouter(prefix="/api/automation")


def _scheduler():
    return AutomationSchedulerHolder.instance()


def _batched_keys() -> tuple[set[tuple], set[str]]:
    """Which submissions the LARGE-PDF pipeline actually handled (§8).

    Read from the real ``large_pdf_jobs`` rows rather than inferred: a
    submission that the router sent to the batched worker has a row there, and
    nothing else does. Two keys are collected because a Classroom job and a
    manual upload are identified differently.
    """
    rows = db.query(
        "SELECT coursework_id, submission_id, result_id FROM large_pdf_jobs "
        "WHERE status != 'CANCELLED'")
    pairs = {(r["coursework_id"], r["submission_id"]) for r in rows
             if r["coursework_id"] and r["submission_id"]}
    results = {r["result_id"] for r in rows if r["result_id"]}
    return pairs, results


def _pipeline_of(job: dict, batched: tuple[set[tuple], set[str]]) -> str | None:
    """The pipeline that handled this job: 'batched', 'standard' or unknown.

    ``None`` means the job has not reached correction yet, so claiming either
    pipeline would be a guess.
    """
    pairs, results = batched
    if job.get("result_id") and job["result_id"] in results:
        return "batched"
    key = (job.get("coursework_id"), job.get("submission_id"))
    if key[0] and key[1] and key in pairs:
        return "batched"
    if job.get("correction_job_id") or job.get("result_id"):
        return "standard"
    return None


def _operation_public(op: dict) -> dict:
    """One operation as the page shows it: CURRENT state + error HISTORY.

    The state is what is true now; ``error``/``error_resolved`` are the history.
    A resolved error is sent as history so the UI can show "recovered" instead
    of an active failure — and can never merge the two back together.
    """
    return {
        "op_key": op["op_key"],
        "label": operations.LABELS.get(op["op_key"], op["op_key"]),
        "state": op["state"],
        "state_label": operations.STATE_LABELS.get(op["state"], op["state"]),
        "attempt": op["attempt"],
        "verified": bool(op["verified"]),
        "recovered": bool(op["recovered"]),
        "note": op["note"],
        "error": op["error"],
        "error_code": op["error_code"],
        "error_at": op["error_at"],
        "error_attempt": op["error_attempt"],
        "error_resolved": bool(op["resolved"]),
        "error_resolved_at": op["resolved_at"],
        "error_dismissed": bool(op["dismissed_at"]),
        "last_attempt_at": op["last_attempt_at"],
        "succeeded_at": op["succeeded_at"],
        "active": bool(op["error"] and not op["resolved"]
                       and not op["dismissed_at"]),
        "dismissible": bool(op["error"] and not op["dismissed_at"]
                            and (op["resolved"]
                                 or op["state"] == operations.NEEDS_VERIFICATION)),
    }


def _job_public(job: dict, batched: tuple[set[tuple], set[str]] | None = None) -> dict:
    # One shared decision: a retry is possible exactly when the runner says so.
    resume = states.retry_step(job)
    resumable = resume is not None
    if batched is None:
        batched = _batched_keys()
    ops = [_operation_public(op) for op in operations.for_job(job["id"])]
    active_errors = [op for op in ops if op["active"]]
    # A DISMISSED error is acknowledged, so it is neither active nor offered
    # as history again — otherwise dismissing it would do nothing on screen.
    resolved_errors = [op for op in ops
                       if op["error"] and not op["active"]
                       and not op["error_dismissed"]]
    return {
        **job,
        #: Which REAL pipeline corrected (or is correcting) this submission.
        "pipeline": _pipeline_of(job, batched),
        "pipeline_label": {"batched": "Batched large-PDF pipeline",
                           "standard": "Standard single-pass pipeline"
                           }.get(_pipeline_of(job, batched), None),
        "status_label": states.label(job["status"]),
        "stage_index": states.stage_index(job["status"]),
        # The SAME rule the runner enforces, so the Retry button and the
        # backend can never disagree about whether a retry is possible.
        "resumable": resumable,
        #: Where a retry would resume, so the UI can say WHAT it is retrying.
        "retry_from": states.label(resume) if resume else None,
        # The report is only "uploaded" once a verified Drive file id is on
        # the job - the UI must never show a tick without that evidence.
        "has_result": bool(job.get("result_id")),
        "report_uploaded": bool(job.get("drive_file_id")),
        # Review mode (and engine review flags) park a job with a verified
        # result + grade, waiting for a teacher. These two flags drive the
        # Approve button in the UI.
        "awaiting_approval": (
            job["status"] == states.ATTENTION_REQUIRED
            and job.get("error_code") == "review_mode_await_teacher"),
        "approvable": bool(
            job.get("result_id") and job.get("grade")
            and job["status"] in (states.ATTENTION_REQUIRED,
                                  states.GRADE_VERIFIED,
                                  states.GRADE_DRAFTED)),
        # -- per-operation truth (report / grade / return) -----------------
        #: Each operation's CURRENT state and its error history. The dashboard
        #: reads these instead of inferring everything from the job's own
        #: status + single error column, which is what let a resolved failure
        #: keep looking like an active one.
        "operations": ops,
        "operation_states": {op["op_key"]: op["state"] for op in ops},
        "active_errors": active_errors,
        "resolved_errors": resolved_errors,
        "has_active_error": bool(active_errors),
        #: An outcome Google never confirmed: the safe action is to CHECK (or
        #: refresh) rather than to upload/write again.
        "needs_verification": any(
            op["state"] == operations.NEEDS_VERIFICATION for op in ops),
        #: The job is history: a newer attempt finished this submission's work.
        "superseded": job["status"] == states.SUPERSEDED,
        "reconcilable": job["status"] not in (states.CANCELLED, states.SUPERSEDED),
    }


@router.get("/status")
def automation_status():
    """Everything the Automation page needs to render (§7, §43)."""
    scheduler = _scheduler()
    status = scheduler.status()
    return {
        **status,
        "intervals": list(AUTOMATION_INTERVALS),
        "pipeline": [{"state": s, "label": states.label(s)} for s in states.PIPELINE],
    }


@router.post("/worker")
def worker_control(payload: dict, request: Request):
    """Background Worker ON/OFF (§3) — the real backend switch.

    Turning the worker OFF stops NEW automatic correction jobs from starting
    (detection stops and never-started jobs stay queued) while jobs already
    in flight finish normally; the website, manual AI correction and the
    tiny networking agent are untouched. The preference is persisted to
    ``config.toml`` before anything else, so an ordinary restart cannot
    silently turn the worker back on.

    Turning it ON re-checks that the scheduler thread is actually alive and
    reports the verified state — the response is always the backend's real
    state, never an echo of the requested value.
    """
    auth_gate.require_permission(request, "automation.manage")
    desired = bool(payload.get("enabled"))
    cfg = get_config()
    changed = bool(cfg.automation.enabled) != desired
    if changed:
        cfg.automation.enabled = desired
        save_config(cfg)          # persist BEFORE acting (survives restart)

    scheduler = _scheduler()
    if desired:
        # Wake immediately: queued (NEW) jobs resume without waiting a cycle.
        store.set_meta("next_check", 0.0)
        scheduler.start()         # idempotent — never a second thread
        scheduler.wake()
        if not scheduler.running():
            raise AppError(
                "The background worker did not start. Check the server log "
                "(Diagnostics → Logs) — automation is enabled but no scheduler "
                "thread is running.", status=500, code="worker_start_failed")
    else:
        # Keep the thread alive so in-flight jobs advance to a safe stop.
        store.set_meta("next_check", time.time())
        scheduler.wake()

    state = scheduler.status()
    log_event("automation", "background worker " + ("turned on" if desired else "turned off"),
              state=state.get("worker_state"),
              pending=state.get("pending_jobs"), active=state.get("active_jobs"))
    return {**state, "updated": ["enabled"] if changed else [],
            "requested": desired}


@router.post("/settings")
def update_settings(payload: dict, request: Request):
    """Enable/disable automation and choose the polling interval (§4, §45).

    Disabling automation stops future polling but NEVER destroys queued jobs
    or completed results.
    """
    auth_gate.require_permission(request, "automation.manage")
    cfg = get_config()
    changed = []
    if "enabled" in payload:
        cfg.automation.enabled = bool(payload["enabled"])
        changed.append("enabled")
    if "interval_minutes" in payload:
        try:
            interval = int(payload["interval_minutes"])
        except (TypeError, ValueError):
            raise AppError("interval_minutes must be a number.", status=400,
                           code="bad_interval")
        if interval not in AUTOMATION_INTERVALS:
            raise AppError(
                "interval_minutes must be one of "
                f"{', '.join(str(i) for i in AUTOMATION_INTERVALS)}.",
                status=400, code="bad_interval")
        cfg.automation.interval_minutes = interval
        changed.append("interval_minutes")
    for key in ("attach_report", "submit_grade", "return_work", "draft_only",
                "review_mode"):
        if key in payload:
            setattr(cfg.automation, key, bool(payload[key]))
            changed.append(key)
    if payload.get("report_detail") in ("summary", "standard", "detailed"):
        cfg.automation.report_detail = payload["report_detail"]
        changed.append("report_detail")
    save_config(cfg)
    if "interval_minutes" in changed or "enabled" in changed:
        # Recompute the next check from now so a changed interval takes effect
        # immediately instead of after the old (possibly long) interval.
        import time
        store.set_meta("next_check", 0.0 if cfg.automation.enabled
                       else time.time())
    _scheduler().wake()
    return {**_scheduler().status(), "updated": changed}


@router.get("/assignments")
def list_assignments():
    """Per-assignment automation selection + live statistics (§43, §46)."""
    enabled = store.enabled_map()
    rows = db.query(
        "SELECT id, course_id, title, max_points, allowlisted, sync_state, "
        "custom_title, classroom_title FROM coursework ORDER BY title")
    stats = _assignment_stats()
    out = []
    for row in rows:
        cw = row["id"]
        setting = enabled.get(cw)
        stat = stats.get(cw, {"submitted": 0, "completed": 0, "processing": 0,
                              "waiting": 0, "failed": 0, "total": 0,
                              "superseded": 0})
        deleted = (row["sync_state"] == "DELETED_IN_CLASSROOM")
        out.append({
            "coursework_id": cw,
            "course_id": row["course_id"],
            "title": row["title"],
            "classroom_title": row["classroom_title"],
            "custom_title": row["custom_title"],
            "sync_state": row["sync_state"] or "ACTIVE",
            # Never selectable/monitored once deleted in Classroom.
            "deleted_in_classroom": deleted,
            "max_points": row["max_points"],
            "enabled": bool(setting and setting["enabled"]) and not deleted,
            "last_checked_at": setting["last_checked_at"] if setting else None,
            "last_error": setting["last_error"] if setting else None,
            "markscheme": _has_mark_scheme(cw),
            "stats": stat,
        })
    return {"assignments": out, "enabled_count":
            sum(1 for a in out if a["enabled"])}


def _assignment_stats() -> dict[str, dict]:
    rows = db.query(
        "SELECT coursework_id, status, COUNT(*) AS n FROM automation_jobs "
        "GROUP BY coursework_id, status")
    out: dict[str, dict] = {}
    for row in rows:
        bucket = out.setdefault(row["coursework_id"], {
            "submitted": 0, "completed": 0, "processing": 0, "waiting": 0,
            "failed": 0, "total": 0, "superseded": 0})
        if row["status"] == states.SUPERSEDED:
            bucket["superseded"] += row["n"]
        status, n = row["status"], row["n"]
        bucket["total"] += n
        if status in (states.COMPLETE, states.SUPERSEDED):
            # Superseded means a NEWER attempt finished this submission's work,
            # so it is not outstanding work and not a failure: counting it as
            # "processing" was what made finished submissions look busy.
            bucket["completed"] += n
        elif status == states.CANCELLED:
            pass
        elif status in states.RETRYABLE_FAILURES:
            bucket["failed"] += n
            bucket["waiting"] += n
        elif status in states.NEEDS_ATTENTION:
            bucket["failed"] += n
        elif status == states.NEW:
            bucket["submitted"] += n
            bucket["waiting"] += n
        else:
            bucket["processing"] += n
    return out


def _has_mark_scheme(coursework_id: str) -> bool:
    from pathlib import Path
    from ..config import DATA_DIR
    if (DATA_DIR / "mark_schemes" / f"{coursework_id}.pdf").exists():
        return True
    row = db.query_one(
        "SELECT path FROM pending_mark_schemes WHERE coursework_id = ?",
        (coursework_id,))
    return bool(row and row["path"] and Path(row["path"]).exists())


@router.post("/assignments/{coursework_id}")
def set_assignment(coursework_id: str, payload: dict):
    """Enable or disable automatic correction for ONE assignment (§3, §46)."""
    cw = db.query_one("SELECT * FROM coursework WHERE id = ?", (coursework_id,))
    if not cw:
        raise AppError("Assignment not found. Sync Google Classroom first.",
                       status=404, code="not_found")
    enabled = bool(payload.get("enabled"))
    if enabled and (cw["sync_state"] or "ACTIVE") == "DELETED_IN_CLASSROOM":
        raise AppError(
            "This assignment was deleted in Google Classroom, so it cannot be "
            "monitored. Re-create it in Classroom to resume automation.",
            status=409, code="deleted_in_classroom")
    record = store.set_assignment_enabled(coursework_id, cw["course_id"], enabled)
    _scheduler().wake()
    return {"coursework_id": coursework_id, "enabled": enabled,
            "assignment": record}


@router.get("/jobs")
def list_jobs(status: str | None = None, coursework_id: str | None = None,
              limit: int = 100):
    statuses = [s.strip().upper() for s in status.split(",")] if status else None
    jobs = store.list_jobs(statuses=statuses, coursework_id=coursework_id,
                           limit=min(500, max(1, limit)))
    # ONE lookup for the whole page, not one query per row.
    batched = _batched_keys()
    return {"jobs": [_job_public(j, batched) for j in jobs]}


@router.get("/jobs/clearable-count")
def clearable_job_count():
    return {"count": store.clear_recent_count()}


@router.get("/errors/clearable-count")
def clearable_error_count():
    return {"count": operations.resolved_error_count()}


@router.post("/errors/clear")
def clear_resolved_errors(payload: dict, request: Request):
    """Clear the MESSAGES of resolved/recovered errors only.

    Unresolved errors stay, and every operation record is kept: that record is
    what stops a duplicate report attachment or a second grade write, so it is
    never part of "clearing errors".
    """
    auth_gate.require_permission(request, "automation.manage")
    if payload.get("confirm") is not True:
        raise AppError("Confirm clearing resolved errors.", status=400,
                       code="confirmation_required")
    cleared = store.clear_resolved_errors()
    return {"cleared": cleared, "operations_preserved": True,
            "remaining": operations.resolved_error_count()}


@router.post("/jobs/{job_id}/errors/{op_key}/dismiss")
def dismiss_operation_error(job_id: str, op_key: str, request: Request):
    """Dismiss ONE error line without changing the operation's real state.

    A genuinely failed operation stays failed — dismissing only stops the
    message being presented as an active error, so this control can never make
    a failure look like a success.
    """
    auth_gate.require_permission(request, "automation.manage")
    job = store.get(job_id)
    if not job:
        raise AppError("Automation job not found.", status=404, code="not_found")
    op, refusal = operations.dismiss(job_id, op_key)
    if refusal:
        raise AppError(refusal, status=409, code="not_dismissible")
    store.log_job("info", "error_dismissed",
                  f"{operations.LABELS.get(op_key, op_key)} error dismissed "
                  "(the operation's state is unchanged)", job_id=job_id)
    return _job_public(store.get(job_id) or job)


@router.post("/jobs/{job_id}/reconcile")
def reconcile_job(job_id: str, request: Request):
    """Refresh ONE job from Google Classroom's real state (read-only)."""
    auth_gate.require_permission(request, "automation.manage")
    from .runner import runner
    job = store.get(job_id)
    if not job:
        raise AppError("Automation job not found.", status=404, code="not_found")
    fresh = runner().reconcile(job_id)
    _scheduler().wake()
    return _job_public(fresh or job)


@router.post("/jobs/reconcile")
def reconcile_jobs(request: Request, payload: dict | None = None):
    """Refresh the jobs whose recorded state may be stale (bounded + read-only).

    Only jobs parked in a failure/attention state are checked, at most 25 per
    call, so this is a deliberate refresh — never a background retry loop.
    """
    auth_gate.require_permission(request, "automation.manage")
    from .runner import runner
    limit = int((payload or {}).get("limit") or 25)
    summary = runner().reconcile_stale(limit=limit)
    _scheduler().wake()
    return {**summary, "jobs": [
        _job_public(j) for j in store.list_jobs(limit=100)]}


@router.post("/jobs/clear")
def clear_recent_jobs(payload: dict, request: Request):
    auth_gate.require_permission(request, "automation.manage")
    if payload.get("confirm") is not True:
        raise AppError("Confirm archiving visible terminal jobs.", status=400,
                       code="confirmation_required")
    hidden = store.clear_recent_jobs()
    return {"hidden": hidden, "history_preserved": True,
            "active_jobs": store.visible_active_count()}


@router.get("/jobs/{job_id}")
def job_detail(job_id: str):
    job = store.get(job_id)
    if not job:
        raise AppError("Automation job not found.", status=404, code="not_found")
    result = None
    if job.get("result_id"):
        result = db.row_to_dict(db.query_one(
            "SELECT id, earned, available, percentage, provisional_grade, "
            "review_required, manual_override, double_check_triggered, "
            "double_check_status, double_check_reason, disagreement_status, "
            "first_earned, first_available, second_earned, second_available, "
            "confidence, confidence_reasons FROM results WHERE id = ?",
            (job["result_id"],)))
    # The REAL per-stage report/attachment state (status, error, drive id,
    # lifecycle). The UI shows this rather than inferring from the job status.
    attachment = None
    if job.get("result_id"):
        try:
            from ..classroom import report_attachment as ra
            attachment = ra.get_state(job["result_id"])
        except Exception:  # noqa: BLE001 - detail must never break the page
            attachment = None
    return {**_job_public(job), "result": result, "attachment": attachment,
            "publication": _publication(job, attachment)}


def _publication(job: dict, attachment: dict | None) -> dict:
    """Honest per-stage publication status for the review UI (§6).

    Google Classroom operations are separate API calls, so each stage is
    reported individually: a teacher can see exactly which part succeeded and
    which still needs to run. Nothing here is inferred from an intention —
    only from the recorded job state and the verified attachment record.
    """
    status = job.get("status")
    op_states = operations.summary(job["id"])
    attached = bool(attachment and attachment.get("attached"))
    attach_status = (attachment or {}).get("status")
    failed_statuses = {"UPLOAD_FAILED", "DRIVE_VERIFY_FAILED",
                       "ATTACH_FAILED", "VERIFY_FAILED", "OWNERSHIP_ERROR"}
    # The operation ledger is the authority: an operation Google/Drive already
    # confirmed stays confirmed (recovered included) even when the JOB is still
    # parked, so a stage can never read "failed" for work that really happened.
    report_op = op_states.get(operations.REPORT)
    if report_op in operations.DONE_STATES or attached:
        report = "sent"
    elif report_op == operations.NEEDS_VERIFICATION:
        report = "unverified"
    elif report_op == operations.FAILED or attach_status in failed_statuses:
        report = "failed"
    elif attach_status:
        report = str(attach_status)
    else:
        report = "held_for_approval" if status == states.ATTENTION_REQUIRED \
            else "pending"
    grade_op = op_states.get(operations.GRADE)
    grade_published = (grade_op in operations.DONE_STATES
                       or status in (states.RETURNING, states.RETURNED,
                                     states.COMPLETE))
    return_op = op_states.get(operations.RETURN)
    returned = (return_op in operations.DONE_STATES
                or status in (states.RETURNED, states.COMPLETE))
    return {
        "report": report,
        "report_sent": attached or report_op in operations.DONE_STATES,
        "report_state": report_op,
        "grade": "published" if grade_published else
                 ("unverified" if grade_op == operations.NEEDS_VERIFICATION
                  else "pending" if status == states.ATTENTION_REQUIRED
                  else str(job.get("grade") or "pending")),
        "grade_published": grade_published,
        "grade_state": grade_op,
        "returned": returned,
        "return_state": return_op,
        "awaiting_approval": (
            status == states.ATTENTION_REQUIRED
            and job.get("error_code") == "review_mode_await_teacher"),
        "complete": status == states.COMPLETE,
        "superseded": status == states.SUPERSEDED,
    }


@router.post("/jobs/{job_id}/approve")
def approve_job(job_id: str, request: Request):
    """Teacher approval: publish the FINAL grade and return the work.

    The explicit human decision that review mode holds a correction for. It
    reuses the same verified grade/return operations as the automatic path, so
    the AI is never re-run and the mark is never recomputed.
    """
    auth_gate.require_role(request, "TEACHER", "DEVELOPER")
    from .runner import StepFailure, runner
    job = store.get(job_id)
    if not job:
        raise AppError("Automation job not found.", status=404, code="not_found")
    try:
        updated = runner().approve(job_id)
    except StepFailure as exc:
        # Keep the runner's own code (no_result / grade_unavailable /
        # not_approvable) so the UI can say WHY, not just "failed".
        raise AppError(str(exc), status=409, code=exc.code)
    _scheduler().wake()
    return _job_public(updated or job)


@router.post("/jobs/{job_id}/retry")
def retry_job(job_id: str):
    """Retry a failed job from the step that failed — never from scratch.

    A job a retry cannot help (still running, already finished, or waiting for
    the teacher's decision) is REFUSED with 409 and the reason, so the button
    can never report a retry that did not happen.
    """
    from .runner import StepFailure, runner
    job = store.get(job_id)
    if not job:
        raise AppError("Automation job not found.", status=404, code="not_found")
    try:
        updated = runner().retry(job_id)
    except StepFailure as exc:
        raise AppError(str(exc), status=409, code=exc.code)
    _scheduler().wake()
    return _job_public(updated or job)


@router.post("/jobs/{job_id}/retry-report")
def retry_report(job_id: str):
    """Repair ONLY the report upload/attachment for a job.

    Never re-runs the AI correction and never re-computes the grade, so it is
    safe even on a job already marked COMPLETE whose report was not actually
    uploaded. Cheap enough to run synchronously, so the result comes straight
    back to the button that triggered it.
    """
    from .runner import runner
    job = store.get(job_id)
    if not job:
        raise AppError("Automation job not found.", status=404, code="not_found")
    if not job.get("result_id"):
        raise AppError("This job has no correction result to attach a report to.",
                       status=409, code="no_result")
    updated = runner().retry_report(job_id)
    _scheduler().wake()
    return _job_public(updated or job)


@router.get("/jobs/{job_id}/errors")
def job_errors(job_id: str):
    """One job's error HISTORY, split into active and resolved (audit view)."""
    job = store.get(job_id)
    if not job:
        raise AppError("Automation job not found.", status=404, code="not_found")
    public = _job_public(job)
    return {"job_id": job_id, "active_errors": public["active_errors"],
            "resolved_errors": public["resolved_errors"],
            "operations": public["operations"]}


@router.get("/bulk-upload/eligible")
def bulk_upload_eligible(request: Request):
    auth_gate.require_permission(request, "automation.manage")
    items = []
    from ..classroom import report_attachment as ra
    from .grading import grade_from_result
    detail = get_config().automation.report_detail
    for row in store.bulk_upload_items():
        try:
            row["prerequisite_error"] = None
            if not row.get("submission_id") or not row.get("course_id"):
                row["prerequisite_error"] = "Classroom submission identifiers are missing."
            elif row.get("review_required") and not row.get("manual_override"):
                row["prerequisite_error"] = "This correction requires teacher review."
            elif not row.get("synced_submission_id"):
                row["prerequisite_error"] = ("Sync Google Classroom before uploading "
                                             "this submission.")
            elif row.get("earned") is None or row.get("available") in (None, 0):
                row["prerequisite_error"] = "Corrected marks are missing or invalid."
            else:
                grade = grade_from_result(row, coursework_id=row.get("coursework_id"))
                row["provisional_grade"] = grade.grade
            state = ra.get_state(row["result_id"])
            row["report_attached"] = bool(state.get("attached"))
            row["report_error"] = state.get("error")
            row["report_status"] = state.get("status") or "PENDING"
            # The hash MUST be computed from the same full result row the
            # upload path uses, otherwise the id shown as "unchanged" here
            # would not match the id the upload itself records (§14).
            full_result = db.row_to_dict(db.query_one(
                "SELECT * FROM results WHERE id = ?", (row["result_id"],)))
            row["report_hash"] = ra._report_fingerprint(full_result or row,
                                                        detail=detail)
            upload_record = store.report_upload_record(row["result_id"])
            row["upload_status"] = upload_record.get("status") if upload_record else row["report_status"]
            row["remote_file_id"] = upload_record.get("remote_file_id") if upload_record else state.get("drive_file_id")
            row["grade_uploaded"] = row.get("assigned_grade") is not None
            row["grade_status"] = "UPLOADED" if row["grade_uploaded"] else (row.get("grade_status") or "PENDING")
            row["eligible"] = not row["prerequisite_error"]
        except Exception as exc:
            row["prerequisite_error"] = row.get("prerequisite_error") or "Upload prerequisites could not be verified."
            row["report_attached"] = False
            row["report_error"] = str(exc)[:200]
            row["eligible"] = False
        items.append(row)
    return {"items": items, "eligible_count": sum(bool(i["eligible"]) for i in items)}


@router.post("/bulk-upload")
def bulk_upload(payload: dict, request: Request):
    auth_gate.require_permission(request, "automation.manage")
    if not isinstance(payload.get("result_ids"), list) or not payload["result_ids"]:
        raise AppError("Select at least one corrected submission.", status=400,
                       code="empty_selection")
    result_ids = list(dict.fromkeys(str(x) for x in payload["result_ids"]))
    if len(result_ids) > 100:
        raise AppError("Bulk uploads are limited to 100 submissions per run.",
                       status=400, code="batch_too_large")
    if len(result_ids) > 10 and payload.get("confirm") is not True:
        raise AppError("Confirm this bulk upload of more than 10 submissions.",
                       status=400, code="confirmation_required")
    run_id = store.begin_bulk_upload_run(result_ids)
    if not run_id:
        raise AppError("One or more selected submissions are already in an upload run. Refresh and retry.",
                       status=409, code="bulk_upload_in_progress")
    # The run row is written FIRST (as RUNNING) because the per-item outcome
    # rows reference it: the run must exist before its items can be recorded.
    # It is also what makes an interrupted run readable instead of invisible.
    db.execute(
        "INSERT INTO automation_bulk_runs(id,status,total,completed,failed,created_at,updated_at) "
        "VALUES(?,?,?,?,?,?,?)",
        (run_id, "RUNNING", len(result_ids), 0, 0, time.time(), time.time()))
    from .grading import grade_from_result
    from ..classroom import managed, report_attachment as ra
    from ..classroom.auth import get_access_token, account_email
    outcomes = []
    try:
        try:
            token = get_access_token()
        except Exception as exc:  # noqa: BLE001 - say WHY nothing was sent
            # Not connected (or the stored grant is dead): refuse the whole run
            # with an actionable message, and fall through to the finally below
            # so the run is closed and its leases are released - a failure here
            # must not lock the submissions out of a retry.
            raise AppError(
                "Google Classroom is not connected, so no grade or report could "
                f"be uploaded. Connect Google Classroom and try again ({exc})",
                status=409, code="classroom_not_connected") from exc
        for result_id in result_ids:
            item = _bulk_upload_one(result_id, grade_from_result, managed, ra,
                                    token, account_email)
            item["run_id"] = run_id
            outcomes.append(item)
            db.execute(
                "INSERT INTO automation_bulk_run_items(run_id,result_id,status,grade_status,report_status,message,updated_at) "
                "VALUES(?,?,?,?,?,?,?) ON CONFLICT(run_id,result_id) DO UPDATE SET "
                "status=excluded.status,grade_status=excluded.grade_status,report_status=excluded.report_status,"
                "message=excluded.message,updated_at=excluded.updated_at",
                (run_id, result_id, item["status"], item.get("grade"), item.get("report"),
                 item.get("error") or item.get("report_message") or item.get("grade_message"), time.time()))
            # Recorded outcome = the claim has done its job. Releasing it here
            # lets an immediate retry of a failed item start, without waiting
            # for the whole run (or its crash lease) to expire.
            store.release_bulk_upload_claim(run_id, result_id)
        summary = {"total": len(outcomes),
                   "complete": sum(x["status"] == "complete" for x in outcomes),
                   "partial": sum(x["status"] == "partial" for x in outcomes),
                   "failed": sum(x["status"] == "failed" for x in outcomes)}
        db.execute("UPDATE automation_bulk_runs SET status='COMPLETED', total=?, "
                   "completed=?, failed=?, updated_at=? WHERE id=?",
                   (summary["total"], summary["complete"], summary["failed"],
                    time.time(), run_id))
        return {"run_id": run_id, "items": outcomes, "summary": summary}
    except Exception:
        # The run is left recorded as FAILED with the outcomes reached so far,
        # so the teacher can see how far it got instead of losing the run.
        db.execute("UPDATE automation_bulk_runs SET status='FAILED', "
                   "completed=?, updated_at=? WHERE id=?",
                   (len(outcomes), time.time(), run_id))
        raise
    finally:
        store.end_bulk_upload_run(run_id)


def _bulk_upload_one(result_id, grade_from_result, managed, ra, token, account_email_fn):
    result = db.row_to_dict(db.query_one("SELECT * FROM results WHERE id = ?", (result_id,)))
    item = {"result_id": result_id, "grade": "skipped", "report": "skipped", "status": "failed"}
    grade_value = None
    try:
        if not result or not result.get("submission_id") or not result.get("coursework_id"):
            raise AppError("The corrected Classroom submission no longer exists.", status=409,
                           code="submission_missing")
        if int(result.get("review_required") or 0) and not int(result.get("manual_override") or 0):
            raise AppError("This result needs teacher review before upload.", status=409,
                           code="review_required")
        tracked = db.query_one("SELECT current_state,assigned_grade FROM classroom_submissions WHERE submission_id=?",
                               (result["submission_id"],))
        if not tracked:
            raise AppError("Sync Classroom before uploading this submission.", status=409,
                           code="submission_not_synced")
        cw = db.query_one("SELECT course_id,title,max_points FROM coursework WHERE id=?",
                          (result["coursework_id"],))
        if not cw:
            raise AppError("The assignment no longer exists locally.", status=409,
                           code="assignment_missing")
        grade_result = grade_from_result(result, coursework_id=result["coursework_id"])
        grade_value = round((grade_result.marks_obtained / grade_result.maximum_marks)
                            * float(cw["max_points"] or grade_result.maximum_marks), 2)
        if managed.managed_info(result["coursework_id"]).get("managed"):
            if tracked["assigned_grade"] is not None and abs(float(tracked["assigned_grade"]) - grade_value) < 1e-6:
                item["grade"] = "skipped"
                item["grade_message"] = "Unchanged grade is already present in the synced Classroom state."
                store.save_bulk_upload_component(result_id=result_id, coursework_id=result["coursework_id"],
                    submission_id=result["submission_id"], component="grade", status="PUBLISHED",
                    message=item["grade_message"], grade=grade_value)
            else:
                evidence = store.grade_component_evidence(result["coursework_id"], result["submission_id"], grade_value)
                remote = gc.get_student_submission(token, cw["course_id"], result["coursework_id"], result["submission_id"])
                vals = [v for v in (remote.get("draftGrade"), remote.get("assignedGrade")) if v is not None]
                present = any(abs(float(v) - grade_value) < 1e-6 for v in vals)
                if present and evidence != "none":
                    item["grade"] = "skipped"
                    item["grade_message"] = "Classroom already has the intended grade; reconciled without another write."
                    store.save_bulk_upload_component(result_id=result_id, coursework_id=result["coursework_id"],
                        submission_id=result["submission_id"], component="grade", status="PUBLISHED",
                        message=item["grade_message"], grade=grade_value)
                elif evidence == "unknown" and not vals:
                    item["grade"] = "retryable_failure"
                    item["grade_message"] = "The previous grade write has an ambiguous outcome; verify Classroom before retrying."
                else:
                    outcome = managed.publish(result["coursework_id"], cw["course_id"],
                        [{"type":"assign_grade","submission_id":result["submission_id"],"grade":grade_value}])
                    op = (outcome.get("results") or [{}])[0]
                    item["grade"] = op.get("status", "failed")
                    item["grade_message"] = op.get("message")
                    store.save_bulk_upload_component(result_id=result_id, coursework_id=result["coursework_id"],
                        submission_id=result["submission_id"], component="grade", status=item["grade"].upper(),
                        message=item["grade_message"], grade=grade_value)
        else:
            item["grade"] = "not_supported"
            item["grade_message"] = "Google permits grade changes only for assignments created by this integration."
        detail = get_config().automation.report_detail
        report_hash = ra._report_fingerprint(result, detail=detail)
        claim = store.report_upload_begin(result, report_hash)
        if not claim["claimed"]:
            if claim["status"] == "UPLOADED" and claim.get("remote_file_id"):
                remote = gc.get_student_submission(token, cw["course_id"], result["coursework_id"],
                                                   result["submission_id"])
                attached_ids = ra._attachment_ids(remote)
                attached = claim["remote_file_id"] in attached_ids
                if attached:
                    item["report"] = "uploaded"
                    item["report_message"] = "Previously uploaded report was verified on Classroom."
                else:
                    item["report"] = "failed"
                    item["report_message"] = "Saved report is no longer attached; reconcile it before retrying."
            else:
                item["report"] = claim["status"].lower()
                item["report_message"] = "Report upload is in progress or has an ambiguous remote outcome; no duplicate was sent."
        else:
            report = ra.attach_report(token, result, course_id=cw["course_id"], assignment_title=cw["title"],
                                      detail=detail, account_email=account_email_fn())
            item["report"] = "uploaded" if report.status in ra.ATTACHED_STATUSES else "failed"
            item["report_message"] = report.message
            state = ra.get_state(result_id)
            if item["report"] == "uploaded":
                store.report_upload_finish(result_id, report_hash, status="UPLOADED",
                                           remote_file_id=state.get("drive_file_id"))
                store.save_bulk_upload_component(result_id=result_id, coursework_id=result["coursework_id"],
                    submission_id=result["submission_id"], component="report", status="UPLOADED",
                    message=report.message, report_hash=report_hash)
            else:
                ambiguous = (report.status == ra.STATUS_PENDING
                             or state.get("error_code") in (
                                 "upload_outcome_ambiguous", "attachment_outcome_ambiguous",
                                 "remote_read_retryable"))
                if not ambiguous and state.get("drive_file_id"):
                    # Classroom is append-only for attachments. Before another
                    # modify call, reconcile whether this exact file is already
                    # attached despite a lost response.
                    try:
                        remote = gc.get_student_submission(token, cw["course_id"],
                            result["coursework_id"], result["submission_id"])
                        if state["drive_file_id"] in ra._attachment_ids(remote):
                            store.reconcile_report_upload(result_id, report_hash,
                                                          state["drive_file_id"])
                            item["report"] = "uploaded"
                            item["report_message"] = "Classroom confirmed the attachment after the response was lost."
                    except Exception:
                        ambiguous = True
                store.report_upload_finish(result_id, report_hash,
                    status="UPLOADED" if item["report"] == "uploaded" else
                           "AMBIGUOUS" if ambiguous else "RETRYABLE_FAILURE",
                    remote_file_id=state.get("drive_file_id"), error=report.message)
                store.save_bulk_upload_component(result_id=result_id, coursework_id=result["coursework_id"],
                    submission_id=result["submission_id"], component="report",
                    status="AMBIGUOUS" if ambiguous else "RETRYABLE_FAILURE",
                    message=report.message, report_hash=report_hash)
        item["status"] = "complete" if item["grade"] in ("published", "skipped") and item["report"] == "uploaded" else "partial"
        if item["grade"] not in ("published", "skipped") and item["report"] != "uploaded":
            item["status"] = "failed"

    except AppError as exc:
        item["error"] = str(exc)
        item["error_code"] = exc.code
    except Exception as exc:
        item["error"] = str(exc)[:300]
        if grade_value is not None:
            store.save_bulk_upload_component(result_id=result_id, coursework_id=result.get("coursework_id"),
                submission_id=result.get("submission_id"), component="grade", status="RETRYABLE_FAILURE",
                message=item["error"], grade=grade_value)
    return item


@router.get("/bulk-upload/runs/{run_id}")
def bulk_upload_run(run_id: str, request: Request):
    auth_gate.require_permission(request, "automation.manage")
    run = db.query_one("SELECT * FROM automation_bulk_runs WHERE id=?", (run_id,))
    if not run:
        raise AppError("Bulk upload run not found.", status=404, code="not_found")
    items = [dict(row) for row in db.query("SELECT * FROM automation_bulk_run_items WHERE run_id=? ORDER BY updated_at",
                                           (run_id,))]
    return {"run": dict(run), "items": items}


@router.post("/run-now")
def run_now():
    """Run one detection cycle + advance jobs immediately (teacher-triggered)."""
    scheduler = _scheduler()
    summary = scheduler.run_cycle()
    from .runner import runner
    runner().run_pending(max_jobs=25)
    return {**scheduler.status(), "cycle": summary}


@router.get("/log")
def recent_log(limit: int = 100):
    """Recent automation activity (§44) — no secrets are ever recorded."""
    return {"entries": store.recent_log(limit=min(500, max(1, limit)))}


@router.post("/log/clear")
def clear_activity(payload: dict, request: Request):
    auth_gate.require_permission(request, "automation.manage")
    if payload.get("confirm") is not True:
        raise AppError("Confirm clearing the visible activity feed.", status=400, code="confirmation_required")
    return {"hidden": store.clear_log(), "audit_preserved": True}


@router.get("/assignment-naming")
def get_assignment_naming():
    """The OPTIONAL default display-name template for new assignments.

    All fields default to empty: the original Classroom name is preserved
    exactly unless the teacher deliberately configures a change.
    """
    from ..classroom import naming
    return {
        **naming.naming_defaults(),
        "reserved_text": naming.RESERVED_AUTO_TEXT,
        "note": ("No prefix or suffix is ever added automatically. With every "
                 "field empty the original Classroom name is kept exactly."),
    }


@router.post("/assignment-naming")
def set_assignment_naming(payload: dict):
    """Configure the default display-name template (nothing is forced)."""
    cfg = get_config()
    mode = str(payload.get("mode", cfg.assignment_naming.mode))
    cfg.assignment_naming.mode = mode if mode in ("original", "custom") \
        else "original"
    for key in ("custom_name", "prefix", "suffix"):
        if key in payload:
            setattr(cfg.assignment_naming, key, str(payload.get(key) or ""))
    save_config(cfg)
    from ..classroom import naming
    return {**naming.naming_defaults(), "updated": True}


@router.post("/assignments/{coursework_id}/name")
def rename_assignment(coursework_id: str, payload: dict):
    """Change ONE assignment's displayed Classroom name, verified.

    Internal identity (course/coursework id, original name) is untouched; only
    the display name the teacher sees is changed, and only if Google confirms.
    """
    row = db.query_one("SELECT course_id, title FROM coursework WHERE id = ?",
                       (coursework_id,))
    course_id = payload.get("course_id") or (row["course_id"] if row else None)
    if not course_id:
        raise AppError("Unknown assignment — sync Google Classroom first.",
                       status=404, code="not_found")
    display_name = payload.get("display_name")
    if not str(display_name or "").strip():
        raise AppError("A display name is required.", status=400,
                       code="empty_name")
    from ..classroom import client as gc
    from ..classroom import naming
    try:
        return naming.rename_coursework(coursework_id, course_id,
                                        str(display_name))
    except AppError:
        raise
    except testing_guard.PublicationBlocked:
        raise  # the testing-only gate's own 403 handler answers this one
    except ValueError as exc:
        raise AppError(str(exc), status=400, code="empty_name")
    except gc.ClassroomApiError as exc:
        raise AppError(
            f"Google refused the rename ({exc.status}). {exc.detail or ''}"[:400],
            status=502, code="rename_failed")
    except Exception as exc:  # noqa: BLE001 - reported, never hidden
        # Same hole as assignment creation: a rename that failed for a reason
        # OTHER than a Google error response (Google unreachable, a revoked
        # grant) used to escape to the global 500 and read as "Something went
        # wrong processing this request".
        log_event("automation", "rename Classroom assignment failed",
                  level=logging.ERROR, error=str(exc)[:500],
                  exc_type=type(exc).__name__, coursework_id=coursework_id)
        from ..api.routes_all import _classroom_error
        raise _classroom_error(exc) from exc


@router.get("/grade-boundaries/{coursework_id}")
def get_grade_boundaries(coursework_id: str):
    boundaries, source = store.grade_boundaries(coursework_id)
    return {"coursework_id": coursework_id, "source": source,
            "boundaries": [[g, m] for g, m in boundaries]}


@router.post("/grade-boundaries/{coursework_id}")
def set_assignment_grade_boundaries(coursework_id: str, payload: dict):
    """Set assignment-specific IGCSE 1-9 boundaries (§20, §21)."""
    raw = payload.get("boundaries")
    parsed: list[tuple[str, float]] = []
    if isinstance(raw, list):
        for entry in raw:
            if not isinstance(entry, (list, tuple)) or len(entry) != 2:
                continue
            try:
                parsed.append((str(entry[0]).strip(), float(entry[1])))
            except (TypeError, ValueError):
                continue
    from .grading import _valid_boundaries
    if not _valid_boundaries(parsed):
        raise AppError(
            "Boundaries must be a valid IGCSE 1-9 table: (grade, minimum "
            "percentage) pairs including a grade reachable at 0%.",
            status=400, code="invalid_boundaries")
    store.set_grade_boundaries(coursework_id, parsed,
                               payload.get("session_label"))
    return {"coursework_id": coursework_id, "boundaries": [[g, m] for g, m in parsed]}
