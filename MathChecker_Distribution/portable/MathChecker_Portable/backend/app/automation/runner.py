"""The resumable workflow runner (spec §13-§18, §30, §33-§42).

One job is advanced ONE verified step at a time. Every step reads its inputs
from the database and writes its outcome back, so a crash/restart resumes from
the exact step that was reached — the AI marking, the report render and any
grade already submitted are NEVER redone (§14, §15, §16, §41).

The runner does not implement its own correction, report, Drive or Classroom
logic: each step calls the EXISTING systems

* correction      -> ``engine.service.CorrectionService`` (existing job queue)
* report render   -> ``classroom.report_attachment.render_report_pdf``
* upload + attach -> ``classroom.report_attachment.attach_report`` (verified)
* grade / return  -> ``classroom.managed.publish`` (honest, never fake success)
* cleanup         -> ``classroom.report_cleanup``

and only decides WHAT to call next and WHEN to retry.
"""

from __future__ import annotations

import json
import logging
import random
import time

from .. import db
from ..logging_setup import log_event
from . import grading, states, store

log = logging.getLogger("automation.runner")

#: Backoff for transient automation-step failures (exponential + jitter).
BACKOFF_BASE = 5.0
BACKOFF_CAP = 120.0

#: A permanent failure (or an exhausted retry budget) PARKS the job this far
#: into the future. The failure states are runnable, so a due timestamp would
#: re-run the same failing step on every scheduler tick forever (production
#: log: 209 "failed permanently" attempts on ONE job). Only a teacher's
#: retry()/retry_report() re-arms the job — both reset next_attempt_at.
PARK_SECONDS = 10 * 365 * 24 * 3600

#: Errors that a retry can never fix (must not be retried blindly, §40).
_PERMANENT_CODES = {
    400: "invalid_request",
    401: "auth_required",
    403: "permission_denied",
    404: "not_found",
}


class StepFailure(RuntimeError):
    """A step failed; ``transient`` decides whether a retry is warranted."""

    def __init__(self, message: str, *, transient: bool, state: str,
                 code: str = "step_failed", detail: str | None = None):
        super().__init__(message)
        self.transient = transient
        self.state = state
        self.code = code
        self.detail = detail


def classify_exception(exc: Exception, failure_state: str) -> StepFailure:
    """Map a step exception onto a retryable or permanent failure (§40)."""
    if isinstance(exc, StepFailure):
        return exc
    from ..classroom.client import ClassroomApiError
    if isinstance(exc, ClassroomApiError):
        code = _PERMANENT_CODES.get(exc.status)
        transient = exc.status == 429 or exc.status >= 500
        return StepFailure(
            str(exc)[:400], transient=transient, state=failure_state,
            code=code or ("rate_limited" if exc.status == 429 else "classroom_error"))
    if isinstance(exc, grading.GradeError):
        return StepFailure(str(exc), transient=False, state=failure_state,
                           code=exc.code)
    message = str(exc).lower()
    # A missing/invalid Google authorization is a config problem a retry can
    # never fix — surface it as needing attention, never as a transient loop.
    if ("not connected" in message or "reconnect" in message
            or "authorization" in message or "invalid_grant" in message):
        return StepFailure(str(exc)[:400], transient=False,
                           state=states.ATTENTION_REQUIRED,
                           code="requires_auth")
    transient_signals = (
        "timed out", "timeout", "connection", "temporarily", "503", "502",
        "504", "500", "rate limit", "too many requests", "overloaded",
        "network", "reset by peer", "unavailable",
    )
    transient = any(sig in message for sig in transient_signals)
    return StepFailure(str(exc)[:400], transient=transient, state=failure_state,
                       code="transient_error" if transient else "step_error")


def backoff_seconds(attempts: int) -> float:
    delay = min(BACKOFF_CAP, BACKOFF_BASE * (2 ** max(0, attempts - 1)))
    return max(1.0, delay * random.uniform(0.8, 1.2))


class WorkflowRunner:
    """Advances automation jobs through the workflow state machine."""

    def __init__(self, *, correction_service=None, attach=None, publish=None,
                 render_pdf=None, pdf_validate=None, drive_verify=None,
                 submission_reader=None, account_email=None, cleanup=None,
                 config=None, token_fn=None):
        self._service = correction_service
        self._attach = attach
        self._publish = publish
        self._render = render_pdf
        self._validate = pdf_validate
        self._drive_verify = drive_verify
        self._submission_reader = submission_reader
        self._account_email = account_email
        self._cleanup = cleanup
        self._config = config
        self._token_fn = token_fn
        self._worker = "automation-runner"

    # -- dependency accessors (lazy, so tests can inject) --------------------

    def _cfg(self):
        if self._config is not None:
            return self._config
        from ..config import get_config
        return get_config().automation

    def service(self):
        if self._service is None:
            from ..engine.service import CorrectionService
            self._service = CorrectionService()
        return self._service

    def _attach_fn(self):
        if self._attach is None:
            from ..classroom.report_attachment import attach_report
            self._attach = attach_report
        return self._attach

    def _publish_fn(self):
        if self._publish is None:
            from ..classroom import managed
            self._publish = managed.publish
        return self._publish

    def _render_fn(self):
        if self._render is None:
            from ..classroom.report_attachment import render_report_pdf
            self._render = render_report_pdf
        return self._render

    def _validate_fn(self):
        if self._validate is None:
            from .pdf_validation import validate_report_pdf
            self._validate = validate_report_pdf
        return self._validate

    def _drive_verify_fn(self):
        if self._drive_verify is None:
            from .pdf_validation import verify_drive_report
            self._drive_verify = verify_drive_report
        return self._drive_verify

    def _read_submission(self, job: dict) -> dict:
        if self._submission_reader is not None:
            return self._submission_reader(job)
        from ..classroom.auth import get_access_token
        from ..classroom import client as gc
        return gc.get_student_submission(
            get_access_token(), job["course_id"], job["coursework_id"],
            job["submission_id"])

    def _cleanup_fn(self):
        if self._cleanup is None:
            from ..classroom import report_cleanup
            self._cleanup = report_cleanup.cleanup_result
        return self._cleanup

    def _token(self) -> str:
        if self._token_fn is not None:
            return self._token_fn()
        from ..classroom.auth import get_access_token
        return get_access_token()

    def _account(self):
        if self._account_email is not None:
            return self._account_email
        try:
            from ..classroom.auth import account_email
            return account_email()
        except Exception:  # noqa: BLE001
            return None

    # -- public entry points -------------------------------------------------

    def advance(self, job_id: str | None = None) -> dict | None:
        """Advance one job (or the next due one) until it waits or finishes."""
        if job_id is None:
            candidate = store.next_runnable()
            if not candidate:
                return None
            job_id = candidate["id"]
        job = store.claim(job_id, worker=self._worker)
        if job is None:
            return store.get(job_id)
        try:
            while True:
                job = store.get(job_id)
                if job is None or job["status"] in states.TERMINAL:
                    break
                try:
                    progressed = self._dispatch(job)
                except Exception as exc:  # noqa: BLE001
                    self._record_failure(job, exc)
                    break
                if not progressed:
                    break
            return store.get(job_id)
        finally:
            store.release(job_id)

    def run_pending(self, *, max_jobs: int = 25,
                    include_new: bool = True) -> int:
        """Advance every due job once (used by the scheduler each tick).

        ``include_new=False`` is the background-worker OFF rule: jobs that
        have not started yet (NEW) are left queued and resume when the worker
        is turned back on, while jobs already in flight are still advanced to
        a safe stopping point — completed work is never discarded.
        """
        processed = 0
        for _ in range(max_jobs):
            candidate = store.next_runnable(exclude_new=not include_new)
            if not candidate:
                break
            job = self.advance(candidate["id"])
            processed += 1
            if job and job["status"] in states.PENDING_STATES \
                    and (job.get("next_attempt_at") or 0) > time.time():
                break
        return processed

    # -- dispatch ------------------------------------------------------------

    def _dispatch(self, job: dict) -> bool:
        """Run the step for the job's current state.

        Returns True when the job moved forward (caller loops again), False
        when the runner must wait (a correction still running) or is done.
        """
        status = job["status"]
        if status in states.RESUME_FROM:
            # A retryable failure: resume from its step (never from NEW) and
            # clear the failure so the state machine reads as progress.
            resume = states.RESUME_FROM[status]
            # set_status already records this transition, so the activity feed
            # gets one meaningful line per retry instead of two.
            job = store.set_status(job["id"], resume,
                                   detail=f"Retrying from {states.label(resume)}")
            status = job["status"]
        handler = getattr(self, f"_step_{status.lower()}", None)
        if handler is None:
            # Unknown / attention state that still needs a human — stop.
            return False
        return handler(job)

    # -- NEW: enqueue onto the EXISTING correction queue --------------------

    def _step_new(self, job: dict) -> bool:
        service = self.service()
        cj = service.enqueue_classroom_submission(
            submission_id=job["submission_id"],
            coursework_id=job["coursework_id"], course_id=job["course_id"],
            student_id=job["student_id"],
            student_name=job.get("student_name") or job["submission_id"])
        store.set_status(
            job["id"], states.CORRECTING,
            detail="Correcting with the existing correction engine",
            correction_job_id=cj["id"])
        store.log_job("success", "correction_started",
                      f"Correction started for {job.get('student_name') or 'student'}",
                      job_id=job["id"])
        return True

    # -- CORRECTING: wait for the EXISTING pipeline, then validate result ---

    def _step_correcting(self, job: dict) -> bool:
        cj_id = job.get("correction_job_id")
        if not cj_id:
            # Recover a job that crashed between enqueue and the status write.
            existing = store.get(job["id"])
            if existing and not existing.get("correction_job_id"):
                row = db.query_one(
                    "SELECT id FROM correction_jobs WHERE submission_id = ? "
                    "ORDER BY created_at DESC LIMIT 1", (job["submission_id"],))
                if row:
                    db.execute("UPDATE automation_jobs SET correction_job_id = ?, "
                               "updated_at = ? WHERE id = ?",
                               (row["id"], time.time(), job["id"]))
                    return True
            return False

        cj = self.service().get_correction_status(cj_id)
        if cj is None:
            raise StepFailure(
                "The correction job this workflow was following no longer exists.",
                transient=False, state=states.ATTENTION_REQUIRED,
                code="correction_job_missing")
        status = (cj.get("status") or "").upper()
        if status in ("FAILED", "CANCELLED"):
            # The existing correction queue has ALREADY applied its own retry
            # policy (including API-capacity waits) before reaching FAILED, so
            # the automation must not blindly re-enqueue the same correction.
            # It surfaces the engine's verdict instead (§17, §40).
            raise StepFailure(
                cj.get("error") or "The correction engine failed this submission.",
                transient=False, state=states.CORRECTION_FAILED,
                code=cj.get("error_code") or "correction_failed")
        if status != "COMPLETED" or not cj.get("result_id"):
            return False          # still marking — wait, do not re-enqueue

        result = self.service().get_result(cj["result_id"])
        if result is None:
            raise StepFailure("The correction completed but its result is missing.",
                              transient=False, state=states.ATTENTION_REQUIRED,
                              code="result_missing")

        gr = grading.grade_from_result(result, coursework_id=job["coursework_id"])
        # Submission-quality flag (§9): surface WHY a result needs review when
        # the submission itself is suspect (blank / unreadable / wrong
        # assignment) rather than a generic "flagged" line.
        sq_note = ""
        try:
            from ..engine.submission_validation import QUALITY_REVIEW
            reasons = []
            for reason in (result.get("confidence_reasons") or []):
                if str(reason).startswith("Submission quality"):
                    reasons.append(str(reason))
            if (str(result.get("confidence") or "")
                    == "SUBMISSION_REVIEW_REQUIRED") or reasons:
                sq_note = " — SUBMISSION_REVIEW_REQUIRED"
                if reasons:
                    sq_note += f" ({reasons[0]})"
                sq_default = reasons[0] if reasons else "flagged for review"
                store.log_job(
                    "warning", "submission_review_required",
                    "The submission itself is suspect (blank / unreadable / "
                    f"wrong assignment): {sq_default}",
                    job_id=job["id"])
        except Exception:  # noqa: BLE001 — a display detail must never fail a step
            sq_note = ""
        # Persist the deterministic 1-9 grade on the result row so every
        # surface (results, reports, class reports) agrees (§20, §23).
        db.execute(
            "UPDATE results SET provisional_grade = ? WHERE id = ?",
            (gr.grade, result["id"]))
        # High-score double-check visibility (§8/§15): the grade is computed
        # from the FINAL result either way; the log records that an
        # independent verification happened and what it decided.
        dc_note = ""
        if int(result.get("double_check_triggered") or 0):
            dc_status = str(result.get("double_check_status") or "")
            dc_note = f" — double-checked ({dc_status})"
            store.log_job(
                "warning" if dc_status in ("REVIEW_REQUIRED",
                                           "DOUBLE_CHECK_FAILED") else "success",
                "double_check",
                f"High-score double-check {dc_status}: first "
                f"{result.get('first_earned')}/{result.get('first_available')}",
                job_id=job["id"])
        store.set_status(
            job["id"], states.RESULT_VALIDATED,
            detail=(f"Result validated: {gr.marks_obtained}/{gr.maximum_marks} "
                    f"→ {gr.percentage:g}% → Grade {gr.grade}{dc_note}{sq_note}"),
            result_id=result["id"], marks_obtained=gr.marks_obtained,
            maximum_marks=gr.maximum_marks, percentage=gr.percentage,
            grade=gr.grade)
        store.log_job("success", "result",
                      f"Result: {gr.marks_obtained}/{gr.maximum_marks} → "
                      f"Grade {gr.grade}{dc_note}{sq_note}", job_id=job["id"])
        with db.transaction() as conn:
            conn.execute(
                "UPDATE correction_jobs SET stage_detail = ? WHERE id = ?",
                (f"{gr.marks_obtained}/{gr.maximum_marks} → Grade {gr.grade}"
                 f"{dc_note}{sq_note}", job["id"]))
        return True

    def _step_result_validated(self, job: dict) -> bool:
        """Generate + validate the report PDF (existing generator)."""
        content = self._render_report(job)
        store.set_status(job["id"], states.REPORT_GENERATED,
                         detail="Report generated", report_filename=None)
        result = self._report_result(job) or {}
        must, any_of = self._expected_text(job, result)
        validation = self._validate_fn()(
            content, expected_text=must, expected_any=any_of)
        if not validation.get("ok"):
            raise StepFailure(
                validation.get("reason") or "The generated report is invalid.",
                transient=False, state=states.REPORT_FAILED,
                code="report_invalid")
        store.set_status(
            job["id"], states.REPORT_VALIDATED,
            detail=(f"Report validated ({validation.get('pages')} page(s), "
                    f"{validation.get('size')} bytes)"))
        store.log_job("success", "report_generated", "Report generated and validated",
                      job_id=job["id"])
        return True

    def _step_report_generated(self, job: dict) -> bool:
        # Crash between render and validate: re-render then validate (§41).
        return self._step_result_validated(job)

    def _step_report_validated(self, job: dict) -> bool:
        """Upload + attach via the EXISTING verified attachment path (§26).

        In REVIEW MODE the job stops here: the report is generated and
        validated locally, but NOTHING is sent to Google Classroom (no report
        attachment, no draft grade) until the teacher explicitly approves —
        sending is the approval's job, together with the grade.
        """
        cfg = self._cfg()
        if not cfg.attach_report:
            raise StepFailure(
                "Automatic report attachment is disabled in Automation settings.",
                transient=False, state=states.ATTENTION_REQUIRED,
                code="attach_disabled")
        result = self.service().get_result(job["result_id"])
        if result is None:
            raise StepFailure("The correction result is missing.",
                              transient=False, state=states.ATTENTION_REQUIRED,
                              code="result_missing")
        result = self._decorate(result)

        if getattr(cfg, "review_mode", False):
            # Await review: the report exists and is validated, but delivery
            # is held back together with the grade. No Classroom write has
            # happened at this point.
            store.set_status(
                job["id"], states.ATTENTION_REQUIRED,
                detail=("REVIEW MODE: report generated and validated, grade "
                        f"{job.get('grade')} ready — awaiting teacher approval "
                        "to send the grade and report together"),
                error_code="review_mode_await_teacher")
            store.log_job(
                "warning", "review_mode_hold",
                f"REVIEW MODE: report validated, grade {job.get('grade')} "
                "awaiting teacher approval — nothing sent to Classroom yet",
                job_id=job["id"])
            return False

        self._deliver_report(job)
        return True

    def _deliver_report(self, job: dict) -> None:
        """Attach the report through the verified path (shared by the normal
        step and teacher approval, so the two can never drift apart)."""
        cfg = self._cfg()
        result = self.service().get_result(job["result_id"])
        if result is None:
            raise StepFailure("The correction result is missing.",
                              transient=False, state=states.ATTENTION_REQUIRED,
                              code="result_missing")
        outcome = self._attach_fn()(
            self._token(), self._decorate(result), course_id=job["course_id"],
            assignment_title=job.get("assignment_title"),
            detail=cfg.report_detail, account_email=self._account())
        self._apply_report_outcome(job, result, outcome)

    def _apply_report_outcome(self, job: dict, result: dict, outcome) -> None:
        """Translate a verified attach outcome into the granular workflow
        states. Only ATTACHED_STATUSES become REPORT_UPLOADED onward; every
        failure keeps its own honest state and machine-readable code.

        Shared by the normal step and the report-only retry, so the two can
        never drift apart. Raises :class:`StepFailure` on any non-success.
        """
        from ..classroom import report_attachment as ra
        status = getattr(outcome, "status", None)
        extra = getattr(outcome, "extra", {}) or {}
        if status in ra.ATTACHED_STATUSES:
            # Verify the Drive file when it still exists (a retry keeps it).
            drive_file_id = extra.get("drive_file_id")
            self._verify_drive_if_present(job, result, drive_file_id)
            store.set_status(job["id"], states.REPORT_UPLOADED,
                             detail="Report uploaded to Google Drive",
                             drive_file_id=drive_file_id,
                             report_filename=extra.get("report_filename"))
            store.set_status(job["id"], states.REPORT_ATTACHED,
                             detail="Report attached to the student's submission")
            store.set_status(job["id"], states.ATTACHMENT_VERIFIED,
                             detail="Attachment verified by re-reading the submission")
            store.log_job("success", "report_attached", "Report attached and verified",
                          job_id=job["id"])
            return

        if status == ra.STATUS_BLOCKED:
            raise StepFailure(
                outcome.message, transient=False,
                state=states.BLOCKED_NOT_MODIFIABLE, code="not_modifiable")
        if status == ra.STATUS_SKIPPED:
            raise StepFailure(
                outcome.message, transient=False,
                state=states.ATTENTION_REQUIRED, code="no_submission_target")
        if status == ra.STATUS_PDF_INVALID:
            raise StepFailure(outcome.message, transient=False,
                              state=states.REPORT_FAILED, code="report_invalid")
        if status == ra.STATUS_TESTING_BLOCKED:
            # The local stop-the-line guard refused the write. NOTHING left the
            # machine, nothing is wrong with the report and no retry can change
            # the outcome, so the job waits for the teacher instead of burning
            # its retry budget and parking as a Drive failure.
            raise StepFailure(outcome.message, transient=False,
                              state=states.ATTENTION_REQUIRED,
                              code="testing_only_blocked")
        if status == ra.STATUS_UPLOAD_FAILED:
            raise StepFailure(outcome.message, transient=True,
                              state=states.REPORT_UPLOAD_FAILED,
                              code="report_upload_failed")
        if status == ra.STATUS_DRIVE_VERIFY_FAILED:
            raise StepFailure(outcome.message, transient=True,
                              state=states.REPORT_UPLOAD_FAILED,
                              code="report_upload_not_verified")
        if status == ra.STATUS_OWNERSHIP_ERROR:
            # Permanent for that file: the retry re-creates it under the
            # current identity, so it is a real (non-transient) attach failure.
            raise StepFailure(outcome.message, transient=False,
                              state=states.ATTACHMENT_FAILED,
                              code="report_ownership_error")
        raise StepFailure(outcome.message or "The report could not be attached.",
                          transient=True, state=states.ATTACHMENT_FAILED,
                          code="attachment_failed")

    def _step_report_uploaded(self, job: dict) -> bool:
        # Resume point after an attachment-phase crash: the file exists, so
        # re-run the (idempotent) attach which re-uses it (§14).
        return self._step_report_validated(job)

    _step_report_attached = _step_report_uploaded

    # -- ATTACHMENT_VERIFIED: draft grade -> verify -------------------------

    def _step_attachment_verified(self, job: dict) -> bool:
        cfg = self._cfg()
        if not cfg.submit_grade:
            raise StepFailure(
                "Automatic grade submission is disabled in Automation settings.",
                transient=False, state=states.ATTENTION_REQUIRED,
                code="grade_disabled")
        result = self.service().get_result(job["result_id"]) or {}
        # Safety gate (§34): never push ANY grade for a correction the engine
        # has not accepted. This runs BEFORE the review-mode draft so an
        # engine-flagged correction never even receives a draft grade.
        if int(result.get("review_required") or 0) and not int(
                result.get("manual_override") or 0):
            raise StepFailure(
                "This correction is flagged for teacher review, so no Classroom "
                "grade was submitted.", transient=False,
                state=states.ATTENTION_REQUIRED, code="requires_review")
        # TEST / REVIEW MODE (draft-grade safety gate): grades are left as
        # drafts that ONLY a teacher can finalise — never auto-published and
        # never auto-returned. The correction, report and proposed grade are
        # all still produced and verified end-to-end.
        if getattr(cfg, "review_mode", False):
            return self._grade_op_review_mode(job)

        grade = job.get("grade")
        if grade not in grading.VALID_GRADES:
            raise StepFailure(
                "No valid IGCSE 1-9 grade is available for this result.",
                transient=False, state=states.ATTENTION_REQUIRED,
                code="grade_unavailable")

        # Default automation writes the FINAL grade, not a draft: a successful
        # job must not sit in Classroom as an unpublished draft. The teacher can
        # still choose draft-only mode explicitly (see ``_cfg().draft_only``).
        op = "draft_grade" if getattr(cfg, "draft_only", False) else "assign_grade"
        self._grade_op(job, op, grade)
        store.set_status(job["id"], states.GRADE_DRAFTED,
                         detail=f"Final grade {grade} posted to Classroom")
        # Verify by reading the submission back — a 200 is NOT proof (§34/§35).
        self._verify_grade(job, grade)
        store.set_status(job["id"], states.GRADE_VERIFIED,
                         detail=f"Grade {grade} verified in Classroom")
        store.log_job("success", "grade_verified", f"Grade {grade} verified",
                      job_id=job["id"])
        return True

    def _step_grade_drafted(self, job: dict) -> bool:
        # A crash right after the draft write: verify it (idempotent).
        grade = job.get("grade")
        self._verify_grade(job, grade)
        store.set_status(job["id"], states.GRADE_VERIFIED,
                         detail=f"Grade {grade} verified in Classroom")
        return True

    def _grade_op_review_mode(self, job: dict) -> bool:
        """TEST/REVIEW MODE grade step: save the PROPOSED grade as a draft in
        Classroom and verify it, then STOP before any publish/return. The
        teacher finalises it after inspecting the correction report."""
        self._grade_op(job, "draft_grade", job.get("grade"))
        self._verify_grade(job, job.get("grade"))
        store.set_status(
            job["id"], states.GRADE_VERIFIED,
            detail=f"Review mode: proposed grade {job.get('grade')} saved as a "
                   "verified DRAFT")
        store.set_status(
            job["id"], states.ATTENTION_REQUIRED,
            detail=("REVIEW MODE: proposed grade " + str(job.get("grade"))
                    + " is a draft awaiting teacher approval — the work was "
                      "NOT returned and the grade was NOT published"),
            error_code="review_mode_await_teacher")
        store.log_job(
            "warning", "review_mode_hold",
            f"REVIEW MODE: proposed grade {job.get('grade')} is a draft "
            "awaiting teacher approval; work NOT returned", job_id=job["id"])
        return False

    # -- GRADE_VERIFIED: return + verify ------------------------------------

    def _step_grade_verified(self, job: dict) -> bool:
        cfg = self._cfg()
        # Review mode never re-publishes or re-returns a draft it already
        # parked — advancing past GRADE_VERIFIED now always requires a
        # teacher decision (the manual publish endpoint).
        if getattr(cfg, "review_mode", False):
            store.set_status(
                job["id"], states.ATTENTION_REQUIRED,
                detail="REVIEW MODE: draft grade is awaiting teacher approval "
                       "— publish/return will only happen with explicit "
                       "teacher action",
                error_code="review_mode_await_teacher")
            return False
        if getattr(cfg, "draft_only", False):
            # Explicit draft-only mode: the grade is saved but the teacher
            # decides when to return. This is the ONLY path that legitimately
            # leaves a successful correction unreturned.
            store.set_status(
                job["id"], states.ATTENTION_REQUIRED,
                detail="Draft grade saved (draft-only mode) — return manually "
                       "when ready")
            return False
        if getattr(cfg, "draft_only", False):
            # Explicit draft-only mode: the grade is saved but the teacher
            # decides when to return. This is the ONLY path that legitimately
            # leaves a successful correction unreturned.
            store.set_status(
                job["id"], states.ATTENTION_REQUIRED,
                detail="Draft grade saved (draft-only mode) — return manually "
                       "when ready")
            return False
        if not cfg.return_work:
            raise StepFailure(
                "Automatic return is disabled in Automation settings.",
                transient=False, state=states.ATTENTION_REQUIRED,
                code="return_disabled")
        store.set_status(job["id"], states.RETURNING,
                         detail="Returning the work to the student")
        self._grade_op(job, "return", None)
        self._verify_returned(job)
        store.set_status(job["id"], states.RETURNED,
                         detail="Return verified in Classroom")
        store.log_job("success", "returned", "Submission returned to the student",
                      job_id=job["id"])
        return True

    def _step_returning(self, job: dict) -> bool:
        return self._step_grade_verified(job)

    # -- teacher approval of a review-mode hold ------------------------------

    #: A job in one of these states has a grade ready and is only waiting for
    #: a teacher decision (review mode, or an engine review flag).
    APPROVABLE_STATES = (states.ATTENTION_REQUIRED, states.GRADE_VERIFIED,
                         states.GRADE_DRAFTED, states.RETURNING,
                         states.RETURNED, states.REPORT_UPLOAD_FAILED,
                         states.ATTACHMENT_FAILED)

    def approve(self, job_id: str) -> dict | None:
        """Teacher approval: send the report + grade, then return the work.

        The explicit human decision that review mode (and the engine review
        flag) hold a correction for. Publication runs as SEPARATE, individually
        verifiable stages:

            1. attach + verify the report (skipped if already attached)
            2. publish the FINAL grade and read it back
            3. return the work and read that back
            4. finalise

        Each stage records its outcome before the next begins, so a failure or
        a crash resumes at the FAILED stage on the next approval — the
        correction is never re-run, no duplicate report is created and a
        half-finished publication is completed rather than restarted.
        Re-approving a finished job is refused rather than duplicated.
        """
        job = store.get(job_id)
        if not job:
            return None
        if not job.get("result_id"):
            raise StepFailure(
                "There is no correction result to approve yet.",
                transient=False, state=states.ATTENTION_REQUIRED,
                code="no_result")
        if job["status"] not in self.APPROVABLE_STATES:
            raise StepFailure(
                f"This job is in '{job['status']}' — it is not waiting for "
                "approval.", transient=False, state=job["status"],
                code="not_approvable")
        if job["status"] == states.RETURNED:
            # Crashed between the verified return and the final state.
            self._finalize_cleanup(job)
            store.set_status(job["id"], states.COMPLETE,
                             detail="Teacher-approved: return already verified")
            return store.get(job_id)

        # Accept an engine-flagged result so the grade step cannot refuse it,
        # mirroring the Review Queue's "Accept" (a permanent human decision).
        result = self.service().get_result(job["result_id"])
        if result and int(result.get("review_required") or 0) and not int(
                result.get("manual_override") or 0):
            with db.transaction() as conn:
                conn.execute(
                    "UPDATE results SET manual_override = 1, review_required = 0, "
                    "confidence = 'HIGH', confidence_reasons = ? WHERE id = ?",
                    (json.dumps(["Approved by the teacher (from automation)."]),
                     job["result_id"]))
                conn.execute(
                    "INSERT INTO result_reviews(result_id, action, "
                    "previous_earned, new_earned, previous_confidence, note, "
                    "created_at) VALUES(?,?,?,?,?,?,?)",
                    (job["result_id"], "approved", result.get("earned"),
                     result.get("earned"), result.get("confidence"),
                     "Approved by the teacher (from automation)", time.time()))

        grade = job.get("grade")
        if grade not in grading.VALID_GRADES:
            raise StepFailure(
                "No valid IGCSE 1-9 grade is available to publish.",
                transient=False, state=states.ATTENTION_REQUIRED,
                code="grade_unavailable")

        cfg = self._cfg()
        stage = job["status"]
        #: Stages that still owe the report and/or the final grade: the hold
        #: itself, plus the two states a FAILED report stage parks in. Without
        #: these the approval used to fall straight through to COMPLETE and
        #: record success for a publication that never happened.
        needs_publication = (stage in (states.ATTENTION_REQUIRED,
                                      states.REPORT_UPLOAD_FAILED,
                                      states.ATTACHMENT_FAILED))

        # ---- stage 1: send the report (only when it has not been sent yet)
        if needs_publication and cfg.attach_report:
            # Raises StepFailure on failure: the job records the FAILED stage
            # and the next approval resumes right here — the correction is
            # never re-run and no second file is created.
            self._deliver_report(job)
            store.log_job("success", "approval_report",
                          "Approved: report attached and verified",
                          job_id=job["id"])

        # ---- stage 2: publish the FINAL grade and verify it ---------------
        # Runs for every stage that can still be holding only a DRAFT grade: the
        # review hold, a stage that failed before the grade was published, and
        # a crash right after a draft write. An approved job must never be
        # recorded as complete with nothing published.
        if needs_publication or stage == states.GRADE_DRAFTED:
            self._grade_op(job, "assign_grade", grade)
            self._verify_grade(job, grade)
            store.set_status(job["id"], states.GRADE_VERIFIED,
                             detail=f"Approved by teacher: grade {grade} published")
            store.log_job("success", "approval_grade",
                          f"Approved: grade {grade} published and verified",
                          job_id=job["id"])
            stage = states.GRADE_VERIFIED

        # ---- stage 3: return the work and verify it ----------------------
        if stage in (states.GRADE_VERIFIED, states.RETURNING):
            store.set_status(job["id"], states.RETURNING,
                             detail="Approved by teacher: returning the work")
            self._grade_op(job, "return", None)
            self._verify_returned(job)
            store.set_status(job["id"], states.RETURNED,
                             detail="Approved by teacher: return verified")

        # ---- stage 4: finalise -------------------------------------------
        self._finalize_cleanup(job)
        store.set_status(job["id"], states.COMPLETE,
                         detail=f"Teacher-approved: grade {grade} published and "
                                "work returned")
        store.log_job("success", "approved",
                      f"Teacher approved: grade {grade} published and returned",
                      job_id=job["id"])
        log_event("automation", "job approved by teacher", job_id=job["id"],
                  grade=grade)
        return store.get(job_id)

    # -- RETURNED: final cleanup -> COMPLETE --------------------------------

    def _step_returned(self, job: dict) -> bool:
        self._finalize_cleanup(job)
        store.set_status(job["id"], states.COMPLETE,
                         detail="Automated correction complete")
        store.log_job("success", "complete",
                      "COMPLETE — correction, report, grade and return verified",
                      job_id=job["id"])
        log_event("automation", "workflow complete", job_id=job["id"],
                  submission_id=job["submission_id"], grade=job.get("grade"))
        return False

    def _step_cleanup_pending(self, job: dict) -> bool:
        return self._finalize_cleanup(job, resume_state=states.RETURNED)

    # -- helpers -------------------------------------------------------------

    def _render_report(self, job: dict) -> bytes:
        result = self._report_result(job)
        if result is None:
            raise StepFailure("The correction result is missing.",
                              transient=False, state=states.ATTENTION_REQUIRED,
                              code="result_missing")
        try:
            return self._render_fn()(result, detail=self._cfg().report_detail)
        except Exception as exc:  # noqa: BLE001
            raise StepFailure(f"Report rendering failed: {exc}", transient=True,
                              state=states.REPORT_FAILED, code="report_failed")

    @staticmethod
    def _decorate(result: dict) -> dict:
        """Attach the canonical student identity to a result row.

        The report PDF prints ``display_name`` (nickname → Classroom name),
        and the results page, the CSV/Markdown exports and the Classroom
        attachment filename all read that SAME decorated row. The automation
        worker used to render the RAW row, so its report showed the stored
        snapshot name ("Yehia Nour") while every other surface showed the
        canonical one ("Yahia Mohamed Noureldin"): the automated report and the
        one a teacher downloads disagreed about who the report is about, and
        the PDF then failed its own content check against the Classroom name.
        Decorating here keeps ONE identity across every surface.
        """
        try:
            from .. import student_identity
            return student_identity.decorate(result)
        except Exception as exc:  # noqa: BLE001 — the report must still render
            log.warning("could not resolve the student identity: %s", exc)
            return result

    def _report_result(self, job: dict) -> dict | None:
        """The decorated result the report is rendered from (and validated
        against), or ``None`` when the correction result is missing."""
        result = self.service().get_result(job["result_id"])
        return self._decorate(result) if result is not None else None

    def _expected_text(self, job: dict, result: dict | None = None) -> tuple:
        """What the report MUST contain, and the names ANY of which is enough.

        Returns ``(must_contain, any_of)``.

        * must-contain: the assignment title the renderer will print (read
          from the SAME row it reads, so a renamed assignment cannot make a
          correct report fail).
        * any-of: the student's names. The report prints the canonical
          display name, but the Classroom roster name, the stored snapshot and
          the nickname are the same student — a report is about the right
          person when ANY of them is on it. Requiring the roster name in the
          PDF is what parked good corrections in REPORT_FAILED.
        """
        result = result if result is not None else (self._report_result(job) or {})
        title = str(job.get("assignment_title") or "").strip()
        if result.get("coursework_id"):
            row = db.query_one("SELECT title FROM coursework WHERE id = ?",
                               (result["coursework_id"],))
            printed = str(row["title"] or "").strip() if row else ""
            # The renderer prints the coursework row's title; prefer it.
            title = printed or title
        must = [title] if title else []
        names = [result.get("display_name"), result.get("nickname"),
                 result.get("classroom_name"), result.get("student_name"),
                 job.get("student_name")]
        any_of = []
        for name in names:
            text = str(name or "").strip()
            if text and text not in any_of:
                any_of.append(text)
        if not any_of and job.get("submission_id"):
            # Last resort: prove the report names SOMEBODY rather than
            # accepting a report that names nobody at all.
            must.append("Student")
        return must, any_of

    def _verify_drive_if_present(self, job: dict, result: dict,
                                 drive_file_id: str | None) -> None:
        """Verify an already-uploaded Drive file when one exists (§28).

        The happy-path upload inside ``attach_report`` performs its own
        upload + ownership + attachment verification; this check covers the
        retry path where a file was deliberately kept.
        """
        if not drive_file_id:
            return
        try:
            check = self._drive_verify_fn()(self._token(), drive_file_id)
        except Exception as exc:  # noqa: BLE001 — never block on a housekeeping read
            check = {"ok": False, "reason": str(exc)}
        if not check.get("ok"):
            log.warning("existing report file failed verification for job %s: %s",
                        job["id"], check.get("reason"))

    def _grade_op(self, job: dict, op_type: str, grade: str | None) -> dict:
        op = {"type": op_type, "submission_id": job["submission_id"]}
        if grade is not None:
            op["grade"] = float(grade)
        outcome = self._publish_fn()(
            job["coursework_id"], job["course_id"], [op])
        results = (outcome or {}).get("results") or []
        result = results[0] if results else {}
        status = result.get("status")
        from ..classroom import managed
        failure_state = (states.RETURN_FAILED if op_type == "return"
                        else states.GRADE_FAILED)
        if status == managed.STATUS_PUBLISHED:
            return result
        if status == managed.STATUS_NOT_SUPPORTED:
            raise StepFailure(result.get("message") or "Not supported by Classroom.",
                              transient=False,
                              state=states.BLOCKED_NOT_MODIFIABLE,
                              code="not_modifiable")
        if status == managed.STATUS_REQUIRES_AUTH:
            raise StepFailure(result.get("message") or "Google authorization expired.",
                              transient=False, state=states.ATTENTION_REQUIRED,
                              code="requires_auth")
        if status == managed.STATUS_REQUIRES_REVIEW:
            raise StepFailure(result.get("message") or "This correction requires review.",
                              transient=False, state=states.ATTENTION_REQUIRED,
                              code="requires_review")
        # A generic failure: 5xx/rate-limited are retryable, but publish()
        # already translates Google's response, so use the http status.
        transient = _transient_publish(result)
        raise StepFailure(result.get("message") or "The Classroom operation failed.",
                          transient=transient, state=failure_state,
                          code=result.get("error_code") or "grade_failed")

    def _verify_grade(self, job: dict, grade) -> None:
        try:
            submission = self._read_submission(job)
        except Exception as exc:  # noqa: BLE001
            raise classify_exception(exc, states.GRADE_VERIFICATION_FAILED)
        draft = submission.get("draftGrade")
        assigned = submission.get("assignedGrade")
        stored = draft if draft is not None else assigned
        if stored is None or abs(float(stored) - float(grade)) > 1e-6:
            raise StepFailure(
                f"Classroom reported draftGrade={stored!r} but Grade {grade} was "
                f"submitted.", transient=True,
                state=states.GRADE_VERIFICATION_FAILED,
                code="grade_verification_failed")
        # Record the write-back so the rest of the app sees it as graded.
        db.execute(
            "UPDATE classroom_submissions SET assigned_grade = ? WHERE submission_id = ?",
            (float(grade), job["submission_id"]))

    def _verify_returned(self, job: dict) -> None:
        try:
            submission = self._read_submission(job)
        except Exception as exc:  # noqa: BLE001
            raise classify_exception(exc, states.RETURN_FAILED)
        state = (submission.get("state") or "").upper()
        if state != "RETURNED" and not submission.get("assignedGrade"):
            raise StepFailure(
                f"Classroom did not report the submission as returned (state="
                f"{state or 'unknown'}).", transient=True,
                state=states.RETURN_FAILED, code="return_not_verified")
        # Only the state is updated — NEVER update_time: this app's own return
        # must not look like a student resubmission to version detection (§10).
        db.execute(
            "UPDATE classroom_submissions SET current_state = 'RETURNED', "
            "assigned_grade = COALESCE(?, assigned_grade) WHERE submission_id = ?",
            (job.get("grade"), job["submission_id"]))

    def _finalize_cleanup(self, job: dict, *, resume_state: str | None = None) -> bool:
        """Retry the temporary Drive cleanup only (§31). Never re-marks."""
        result_id = job.get("result_id")
        if not result_id:
            return True
        try:
            from ..classroom import report_attachment as ra
            state = ra.get_state(result_id)
            if state.get("drive_cleanup") in ("cleaned", "not_needed",
                                              "retained"):
                return True
            outcome = self._cleanup_fn()(result_id, self._token(),
                                         account_email=self._account())
        except Exception as exc:  # noqa: BLE001 — housekeeping is never fatal
            log.warning("report cleanup retry failed for job %s: %s", job["id"], exc)
            outcome = {"ok": False}
        if not outcome.get("ok") and outcome.get("skipped") == "cleanup_failed":
            if resume_state:
                store.set_status(
                    job["id"], states.RETURNED,
                    detail="Correction successful; temporary file cleanup pending")
            return True
        return True

    # -- failure handling ----------------------------------------------------

    def _record_failure(self, job: dict, exc: Exception) -> None:
        failure = classify_exception(exc, states.CORRECTION_FAILED)
        attempts = int(job.get("attempts") or 0) + 1
        max_attempts = int(job.get("max_attempts") or 3)
        if failure.state == states.CORRECTION_FAILED:
            # The existing correction queue owns its own retry budget; only
            # give up here once it has genuinely failed.
            pass
        terminal = (not failure.transient) or attempts >= max_attempts
        if terminal:
            # Park, never leave due: these states are runnable, so
            # next_attempt_at = 0 would have the scheduler re-run this same
            # failing step on every tick, forever. The status stays the
            # honest failure state for the UI (Retry buttons key off it);
            # the far-future due time is what says "wait for a human".
            db.execute(
                "UPDATE automation_jobs SET attempts = ?, updated_at = ? "
                "WHERE id = ?", (attempts, time.time(), job["id"]))
            store.set_status(
                job["id"], failure.state, detail=failure.detail or str(exc)[:300],
                error=str(exc)[:500], error_code=failure.code,
                next_attempt_at=time.time() + PARK_SECONDS)
            store.log_job("error", failure.state.lower(),
                          f"{failure.code}: {str(exc)[:200]}", job_id=job["id"])
            log_event("automation", "workflow step failed permanently",
                      level=logging.WARNING, job_id=job["id"],
                      state=failure.state, error_code=failure.code)
            return
        delay = backoff_seconds(attempts)
        db.execute(
            "UPDATE automation_jobs SET attempts = ?, updated_at = ? WHERE id = ?",
            (attempts, time.time(), job["id"]))
        store.set_status(
            job["id"], failure.state,
            detail=(f"{failure.code} — retry {attempts}/{max_attempts} in "
                    f"~{int(delay)}s"),
            error=str(exc)[:500], error_code=failure.code,
            next_attempt_at=time.time() + delay)
        store.log_job("warning", "retry",
                      f"{failure.code}: retrying in ~{int(delay)}s",
                      job_id=job["id"])

    def retry(self, job_id: str) -> dict | None:
        """Manually retry a failed job from the step that failed (§39).

        The resume point comes from :func:`states.retry_step`, so a retry NEVER
        re-runs the AI correction, re-renders the report or re-writes a grade
        that already succeeded. When the job is not one a retry can help — it
        is still running, it is already finished, or it is waiting for a
        teacher DECISION — the retry is REFUSED with the reason instead of
        returning success for work that never happened.
        """
        job = store.get(job_id)
        if job is None:
            return None
        resume = states.retry_step(job)
        if resume is None:
            raise StepFailure(self.retry_refusal(job), transient=False,
                              state=job["status"], code="not_retryable")
        # A manual retry is an explicit human action on a job that had stopped
        # by itself, so making it due NOW is what makes the retry take effect
        # on the next tick instead of silently doing nothing until the stale
        # claim expires. A claim is only dropped when it is STALE: a fresh one
        # means another worker is genuinely advancing the job at this moment,
        # and pulling the job out from under it would run the step twice.
        now_ts = time.time()
        db.execute(
            "UPDATE automation_jobs SET attempts = 0, next_attempt_at = 0, "
            "claimed_by = NULL, claimed_at = NULL, updated_at = ? "
            "WHERE id = ? AND claimed_at IS NOT NULL AND claimed_at < ?",
            (now_ts, job_id, now_ts - store.CLAIM_LEASE_SECONDS))
        db.execute(
            "UPDATE automation_jobs SET attempts = 0, next_attempt_at = 0, "
            "updated_at = ? WHERE id = ?", (now_ts, job_id))
        store.set_status(job_id, resume,
                         detail=f"Retrying from {states.label(resume)}")
        store.log_job("info", "retry_requested",
                      f"Retry requested from {states.label(resume)}", job_id=job_id)
        return store.get(job_id)

    @staticmethod
    def retry_refusal(job: dict) -> str:
        """Why this job cannot be retried, and what the teacher should do."""
        status = job["status"]
        code = str(job.get("error_code") or "")
        if code in states.DECISION_CODES:
            return ("This correction is waiting for your decision, not a "
                    "retry — use "
                    f"{states.DECISION_CODES[code]} to send the report, "
                    "publish the grade and return the work.")
        if status == states.ATTENTION_REQUIRED:
            return ("This correction is waiting for your approval, not a "
                    "retry — use Approve to publish the grade and return the "
                    "work.")
        if status == states.COMPLETE:
            return "This correction is already complete — there is nothing to retry."
        if status == states.CANCELLED:
            return "This correction was cancelled — there is nothing to retry."
        if status in states.PENDING_STATES:
            return (f"This correction is still in progress ({states.label(status)}). "
                    "Wait for it to finish, then retry if it fails.")
        return (f"There is no recorded step to retry for "
                f"'{states.label(status)}'.")

    def retry_report(self, job_id: str) -> dict | None:
        """Repair ONLY the report upload/attachment stage (§7).

        Unlike :meth:`retry` this works on ANY job - including one that already
        reached COMPLETE with a report that was never actually uploaded (the
        legacy ``CLEANED`` rows read VERIFIED with a NULL Drive file id). The
        AI correction, the raw score and the 1-9 grade are all REUSED: the
        student is never re-marked, the correction is never regenerated and no
        second grade is computed. Only the report is re-validated, re-uploaded
        (or its existing file reused) and re-attached, then verified.
        """
        job = store.get(job_id)
        if job is None:
            return None
        if not job.get("result_id"):
            return job
        result = self.service().get_result(job["result_id"])
        if result is None:
            store.set_status(job_id, states.ATTENTION_REQUIRED,
                             detail="The correction result is missing.",
                             error_code="result_missing")
            store.log_job("error", "retry_report",
                          "The correction result is missing.", job_id=job_id)
            return store.get(job_id)
        # The repair must produce the SAME report every other surface shows.
        result = self._decorate(result)

        previous = job["status"]
        db.execute(
            "UPDATE automation_jobs SET attempts = 0, next_attempt_at = 0, "
            "updated_at = ? WHERE id = ?", (time.time(), job_id))
        store.log_job("info", "retry_report_requested",
                      "Retrying the report upload/attachment only "
                      "(the AI correction is not re-run)", job_id=job_id)
        try:
            outcome = self._attach_fn()(
                self._token(), result, course_id=job["course_id"],
                assignment_title=job.get("assignment_title"),
                detail=self._cfg().report_detail,
                account_email=self._account())
            self._apply_report_outcome(job, result, outcome)
        except StepFailure as failure:
            # A synchronous repair action: record the failure and let the
            # teacher see the resulting state instead of raising a 500.
            self._record_failure(job, failure)
            return store.get(job_id)

        # The report is verified. When the grade and the return were ALREADY
        # done, keep the job COMPLETE - only the report needed repairing, so a
        # fresh grade must not be posted and the work must not be re-returned.
        if previous in (states.GRADE_VERIFIED, states.RETURNING, states.RETURNED,
                        states.COMPLETE):
            store.set_status(
                job_id, states.COMPLETE,
                detail="Report repaired; grade and return already verified")
            store.log_job("success", "report_repaired",
                          "Report uploaded, attached and verified", job_id=job_id)
        return store.get(job_id)


def _transient_publish(result: dict) -> bool:
    http = result.get("http_status")
    if isinstance(http, int):
        return http == 429 or http >= 500
    code = (result.get("error_code") or "")
    return code in ("rate_limited", "google_error")


_runner: WorkflowRunner | None = None


def runner() -> WorkflowRunner:
    global _runner
    if _runner is None:
        _runner = WorkflowRunner()
    return _runner


def reset_runner() -> None:
    global _runner
    _runner = None
