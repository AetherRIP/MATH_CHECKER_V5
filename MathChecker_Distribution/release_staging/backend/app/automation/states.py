"""The automation workflow STATE MACHINE (spec §13, §38, §39).

A job is only ever COMPLETE when every step has been individually performed
AND verified. The states below are persisted verbatim in
``automation_jobs.status`` so a restart can resume from the exact step that
was last reached.

Happy path (§1):

    NEW -> CORRECTING -> RESULT_VALIDATED -> REPORT_GENERATED ->
    REPORT_VALIDATED -> REPORT_UPLOADED -> REPORT_ATTACHED ->
    ATTACHMENT_VERIFIED -> GRADE_DRAFTED -> GRADE_VERIFIED -> RETURNING ->
    RETURNED -> COMPLETE

Failure/recovery states (§39) name the step that must be retried, so a retry
NEVER re-runs an earlier step (the AI marking, the report render or a grade
write) that already succeeded.

:func:`retry_step` is the single source of truth for what a manual Retry may
resume at. The API advertises it as ``resumable`` and the runner enforces it,
so the button and the backend can never disagree, and a job that needs a
DECISION (a review-mode hold) is never offered — or accepted as — a retry.
"""

from __future__ import annotations

# -- happy path states -------------------------------------------------------

NEW = "NEW"
CORRECTING = "CORRECTING"
RESULT_VALIDATED = "RESULT_VALIDATED"
REPORT_GENERATED = "REPORT_GENERATED"
REPORT_VALIDATED = "REPORT_VALIDATED"
REPORT_UPLOADED = "REPORT_UPLOADED"
REPORT_ATTACHED = "REPORT_ATTACHED"
ATTACHMENT_VERIFIED = "ATTACHMENT_VERIFIED"
GRADE_DRAFTED = "GRADE_DRAFTED"
GRADE_VERIFIED = "GRADE_VERIFIED"
RETURNING = "RETURNING"
RETURNED = "RETURNED"
COMPLETE = "COMPLETE"

# -- failure / recovery states ----------------------------------------------

CORRECTION_FAILED = "CORRECTION_FAILED"
REPORT_FAILED = "REPORT_FAILED"
REPORT_UPLOAD_FAILED = "REPORT_UPLOAD_FAILED"
ATTACHMENT_FAILED = "ATTACHMENT_FAILED"
GRADE_FAILED = "GRADE_FAILED"
GRADE_VERIFICATION_FAILED = "GRADE_VERIFICATION_FAILED"
RETURN_FAILED = "RETURN_FAILED"
CLEANUP_PENDING = "CLEANUP_PENDING"

#: Terminal-ish states that need a human decision (not a blind retry).
ATTENTION_REQUIRED = "ATTENTION_REQUIRED"
BLOCKED_NOT_MODIFIABLE = "BLOCKED_NOT_MODIFIABLE"
CANCELLED = "CANCELLED"

# -- sets --------------------------------------------------------------------

#: States whose step still needs to run (the runner advances from these).
PENDING_STATES = (
    NEW, CORRECTING, RESULT_VALIDATED, REPORT_GENERATED, REPORT_VALIDATED,
    REPORT_UPLOADED, REPORT_ATTACHED, ATTACHMENT_VERIFIED, GRADE_DRAFTED,
    GRADE_VERIFIED, RETURNING, CLEANUP_PENDING,
)

#: States a retry is allowed on, mapped to the step to resume from.
RETRYABLE_FAILURES = (
    CORRECTION_FAILED, REPORT_FAILED, REPORT_UPLOAD_FAILED, ATTACHMENT_FAILED,
    GRADE_FAILED, GRADE_VERIFICATION_FAILED, RETURN_FAILED, CLEANUP_PENDING,
)

#: The step each failure resumes at (never an earlier, already-done step).
RESUME_FROM = {
    CORRECTION_FAILED: CORRECTING,
    REPORT_FAILED: REPORT_GENERATED,
    REPORT_UPLOAD_FAILED: REPORT_VALIDATED,
    ATTACHMENT_FAILED: REPORT_VALIDATED,
    GRADE_FAILED: ATTACHMENT_VERIFIED,
    GRADE_VERIFICATION_FAILED: GRADE_DRAFTED,
    RETURN_FAILED: GRADE_VERIFIED,
    CLEANUP_PENDING: CLEANUP_PENDING,
}

#: A job in one of these will not advance without teacher action.
NEEDS_ATTENTION = (ATTENTION_REQUIRED, BLOCKED_NOT_MODIFIABLE,
                   GRADE_VERIFICATION_FAILED)

#: The step a MANUAL retry resumes at when the job was parked (rather than
#: failed) in ATTENTION_REQUIRED by one specific step. The recorded error code
#: says WHICH step was blocked, so the retry re-runs that one step: the AI
#: correction, the report render and every Classroom write that already
#: succeeded are never repeated (no second report, no second grade).
#: Codes that are absent here need a teacher DECISION (see DECISION_CODES) or
#: have no step to re-run, and are refused instead of guessed at.
BLOCKED_STEP_FROM_CODE = {
    "attach_disabled": REPORT_VALIDATED,        # report delivery was skipped
    "no_submission_target": REPORT_VALIDATED,   # nothing to attach to (yet)
    "grade_disabled": ATTACHMENT_VERIFIED,      # grade submission was skipped
    "return_disabled": GRADE_VERIFIED,          # the grade is done; return next
    "grade_unavailable": GRADE_DRAFTED,         # re-draft after fixing grades
    "requires_review": ATTACHMENT_VERIFIED,     # accept in Review Queue, retry
    "result_missing": CORRECTING,               # re-read the engine's result
    "correction_job_missing": NEW,              # the AI job itself is gone
}

#: Parked waiting for an explicit teacher DECISION, not for a retry. Re-running
#: the pipeline here would either duplicate Classroom work or hold the job
#: again, so a retry is refused and the real action is named instead.
DECISION_CODES = {
    "review_mode_await_teacher": "Approve",
}

#: The labelled teacher action that resolves a parked-with-no-step job.
APPROVE_ACTION = "Approve"


def _evidenced_step(job: dict) -> str:
    """The furthest step a job has EVIDENCE for.

    Used when the exact step that failed was not recorded (e.g. an expired
    Google authorization surfaced from an arbitrary step). It never returns a
    step that would re-run the AI or re-render the report: every value here is
    a step that only READS or re-attempts an operation whose inputs are already
    persisted.
    """
    if not job.get("result_id"):
        return CORRECTING if job.get("correction_job_id") else NEW
    if job.get("drive_file_id"):
        return ATTACHMENT_VERIFIED
    return REPORT_VALIDATED


def retry_step(job: dict) -> str | None:
    """The step a manual retry must resume from, or ``None`` when a retry
    cannot help this job.

    ``None`` means the job needs a human DECISION (approve it), is already
    finished, or is still running — the caller MUST refuse instead of
    reporting success for work that will never happen.
    """
    status = job.get("status")
    if status in RESUME_FROM:
        # A real failure: resume at the step that failed, never from NEW.
        return RESUME_FROM[status]
    code = str(job.get("error_code") or "")
    if status == ATTENTION_REQUIRED:
        if code in DECISION_CODES:
            return None
        if code in BLOCKED_STEP_FROM_CODE:
            return BLOCKED_STEP_FROM_CODE[code]
        if not code:
            # No failure recorded: the job is waiting to be approved.
            return None
        return _evidenced_step(job)
    if status == BLOCKED_NOT_MODIFIABLE:
        # Google refused the write. Retry the step that was refused: the report
        # stage until the report really is on the submission, then the grade.
        return ATTACHMENT_VERIFIED if job.get("drive_file_id") else REPORT_VALIDATED
    return None

#: Genuinely finished states (no further automatic work).
TERMINAL = (COMPLETE, CANCELLED, ATTENTION_REQUIRED, BLOCKED_NOT_MODIFIABLE)

#: Ordered list used by the UI to render the progress timeline.
PIPELINE = (
    NEW, CORRECTING, RESULT_VALIDATED, REPORT_GENERATED, REPORT_VALIDATED,
    REPORT_UPLOADED, REPORT_ATTACHED, ATTACHMENT_VERIFIED, GRADE_DRAFTED,
    GRADE_VERIFIED, RETURNING, RETURNED, COMPLETE,
)

LABELS = {
    NEW: "Detected",
    CORRECTING: "Correcting",
    RESULT_VALIDATED: "Result validated",
    REPORT_GENERATED: "Report generated",
    REPORT_VALIDATED: "Report validated",
    REPORT_UPLOADED: "Report uploaded",
    REPORT_ATTACHED: "Report attached",
    ATTACHMENT_VERIFIED: "Attachment verified",
    GRADE_DRAFTED: "Grade drafted",
    GRADE_VERIFIED: "Grade verified",
    RETURNING: "Returning",
    RETURNED: "Returned",
    COMPLETE: "Complete",
    CORRECTION_FAILED: "Correction failed",
    REPORT_FAILED: "Report failed",
    REPORT_UPLOAD_FAILED: "Upload failed",
    ATTACHMENT_FAILED: "Attachment failed",
    GRADE_FAILED: "Grade failed",
    GRADE_VERIFICATION_FAILED: "Grade verification failed",
    RETURN_FAILED: "Return failed",
    CLEANUP_PENDING: "Cleanup pending",
    ATTENTION_REQUIRED: "Needs attention",
    BLOCKED_NOT_MODIFIABLE: "Blocked by Google Classroom",
    CANCELLED: "Cancelled",
}


def label(status: str) -> str:
    return LABELS.get(status, status)


def is_complete(status: str) -> bool:
    return status == COMPLETE


def is_terminal(status: str) -> bool:
    return status in TERMINAL


def stage_index(status: str) -> int:
    """Position of a state in the happy-path pipeline (for progress display).

    A failure state reports the index of the step it must resume from, so the
    UI shows how far the job actually got.
    """
    if status in RESUME_FROM:
        status = RESUME_FROM[status]
    try:
        return PIPELINE.index(status)
    except ValueError:
        return -1
