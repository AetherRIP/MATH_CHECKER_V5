"""State machine for the Large PDF Worker.

One job per (student answer PDF version + mark scheme version). The states
below are persisted verbatim in ``large_pdf_jobs.status`` so a restart resumes
from the exact step reached, and each batch carries its own state in
``large_pdf_batches.status`` so a single failed batch can be retried without
re-running the successful ones.

Happy path::

    QUEUED -> INSPECTING -> PLANNING -> READY -> PROCESSING -> VALIDATING
          -> MERGING -> AWAITING_REVIEW | COMPLETED

The AI marking NEVER starts before the plan exists: the runner refuses to
process a job whose status is not READY (or a batch whose job is not READY/
PROCESSING), which is what makes "inspect first, then mark" structural rather
than a convention.
"""

from __future__ import annotations

# -- job states --------------------------------------------------------------

QUEUED = "QUEUED"
INSPECTING = "INSPECTING"
PLANNING = "PLANNING"
READY = "READY"
PROCESSING = "PROCESSING"
VALIDATING = "VALIDATING"
MERGING = "MERGING"
AWAITING_REVIEW = "AWAITING_REVIEW"
COMPLETED = "COMPLETED"
FAILED = "FAILED"
CANCELLED = "CANCELLED"

#: States where marking may run (plan exists and is validated).
PROCESSABLE = (READY, PROCESSING, VALIDATING, MERGING, AWAITING_REVIEW)

#: States that are still moving (used by boot recovery).
IN_FLIGHT = (QUEUED, INSPECTING, PLANNING, READY, PROCESSING, VALIDATING,
             MERGING)

#: No further automatic work.
TERMINAL = (COMPLETED, AWAITING_REVIEW, FAILED, CANCELLED)

#: A failure the teacher (or an automatic retry) can resume from.
RETRYABLE = (FAILED, AWAITING_REVIEW)

PIPELINE = (QUEUED, INSPECTING, PLANNING, READY, PROCESSING, VALIDATING,
            MERGING, AWAITING_REVIEW, COMPLETED)

LABELS = {
    QUEUED: "Queued",
    INSPECTING: "Inspecting document",
    PLANNING: "Preparing batches",
    READY: "Ready to process",
    PROCESSING: "Processing batches",
    VALIDATING: "Validating results",
    MERGING: "Merging results",
    AWAITING_REVIEW: "Awaiting teacher review",
    COMPLETED: "Correction completed",
    FAILED: "Correction failed",
    CANCELLED: "Cancelled",
}

# -- batch states ------------------------------------------------------------

BATCH_PENDING = "PENDING"
BATCH_PROCESSING = "PROCESSING"
BATCH_COMPLETED = "COMPLETED"
BATCH_FAILED = "FAILED"
BATCH_VERIFICATION_REQUIRED = "VERIFICATION_REQUIRED"
BATCH_REVIEW_REQUIRED = "REVIEW_REQUIRED"

BATCH_TERMINAL = (BATCH_COMPLETED, BATCH_FAILED, BATCH_REVIEW_REQUIRED)


def label(status: str) -> str:
    return LABELS.get(status, status)


def is_terminal(status: str) -> bool:
    return status in TERMINAL


def is_processable(status: str) -> bool:
    return status in PROCESSABLE


def progress_message(status: str, *, batch_index: int | None = None,
                     batch_total: int | None = None,
                     pages: int | None = None) -> str:
    """Plain-language progress line for the teacher (no diagnostics)."""
    if status == INSPECTING and pages:
        return f"Inspecting {pages}-page PDF"
    if status == PLANNING:
        if batch_total:
            return f"Preparing {batch_total} batch{'es' if batch_total != 1 else ''}"
        return "Preparing document"
    if status == PROCESSING:
        if batch_index and batch_total:
            return f"Processing batch {batch_index} of {batch_total}"
        return "Processing batches"
    if status == VALIDATING:
        return "Validating question coverage"
    if status == MERGING:
        return "Merging results"
    return label(status)
