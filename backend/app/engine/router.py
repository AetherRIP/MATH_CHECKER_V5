"""Central correction routing — ONE rule, in ONE place.

Every correction entry point in the app (manual upload, automatic homework,
Google Classroom submission jobs, bulk/assignment correction, API-triggered and
background jobs) funnels through ``CorrectionService.handle_job``. That handler
asks THIS module which pipeline should correct the submission:

* the STUDENT ANSWER PDF's page count is the only input that matters;
* at or below :func:`threshold_pages` the existing single-pass workflow runs
  unchanged;
* above it the Large PDF Worker inspects, plans and batches the document.

The mark scheme's page count is NEVER used to route — the two files' page
numbers are unrelated. The threshold itself lives in exactly one configuration
field (``config.large_pdf.threshold_pages``) so no entry point can disagree.

If the page count cannot be determined the router raises :class:`RoutingError`
rather than silently assuming the document is short: a long document must never
be handed to the single-pass pipeline as a fallback.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..config import get_config
from ..logging_setup import log_event
from . import pdf_inspect

#: Route the submission through the existing single-pass correction workflow.
NORMAL = "normal"
#: Route the submission through the Large PDF Worker (batched).
LARGE = "large"


class RoutingError(RuntimeError):
    """The student PDF's page count could not be determined reliably."""


@dataclass(frozen=True)
class RoutingDecision:
    kind: str                # NORMAL | LARGE
    page_count: int
    threshold: int
    reason: str

    @property
    def is_large(self) -> bool:
        return self.kind == LARGE


def threshold_pages() -> int:
    """The single, central routing threshold (default 30 pages)."""
    return int(get_config().large_pdf.threshold_pages)


def route_student_pdf(student_pdf_path: str, *,
                      threshold: int | None = None) -> RoutingDecision:
    """Decide the correction pipeline from the STUDENT answer PDF's size.

    Raises :class:`RoutingError` when the PDF cannot be read, so an unknown
    page count fails safely instead of defaulting to the short-document path.
    """
    limit = threshold_pages() if threshold is None else int(threshold)
    pages = pdf_inspect.page_count(student_pdf_path)
    if pages < 1:
        raise RoutingError(
            "The student answer PDF could not be read, so its page count is "
            "unknown. Nothing was sent to the AI. Check that the file is a "
            "valid, non-empty PDF and try again."
        )
    if pages > limit:
        decision = RoutingDecision(
            LARGE, pages, limit,
            f"{pages} pages exceeds the {limit}-page single-pass limit — "
            f"routed to the Large AI Worker and split into balanced batches.")
    else:
        decision = RoutingDecision(
            NORMAL, pages, limit,
            f"{pages} pages is within the {limit}-page single-pass limit — "
            f"the existing correction workflow is used.")
    log_event("router", "routed submission", route=decision.kind,
              pages=pages, threshold=limit)
    return decision


def delegate_long_document(job: dict, submission_paths: list[str] | None,
                           mark_scheme_path: str | None) -> bool:
    """Hand a long submission to the Large AI Worker; return True if handed off.

    This is the SHARED delegation used by every real executor (the WorkerPool
    that runs production jobs and the legacy JobManager path), so the routing
    decision and the hand-off can never diverge. Returns False when the
    existing single-pass workflow should continue.

    Never silently assumes an unknown page count is short: an unreadable PDF
    raises :class:`RoutingError`, and a long document with no mark scheme
    raises a plain ``RuntimeError`` with an actionable message.
    """
    paths = [p for p in (submission_paths or []) if p]
    if not paths:
        return False

    pages = sum(max(0, pdf_inspect.page_count(p)) for p in paths)
    if pages < 1:
        raise RoutingError(
            "The student answer PDF could not be read, so its page count is "
            "unknown. Nothing was sent to the AI. Check that the file is a "
            "valid, non-empty PDF and try again.")
    limit = threshold_pages()
    if pages <= limit:
        log_event("router", "routed submission", route=NORMAL, pages=pages,
                  threshold=limit)
        return False

    # The batched workflow needs ONE student PDF; combine multi-attachment
    # submissions deterministically so the page count that routed them is the
    # page count that gets batched.
    student_pdf = paths[0] if len(paths) == 1 else _merge_pdfs(paths, job)
    if not mark_scheme_path:
        raise RuntimeError(
            "This long submission has no mark scheme available locally, so it "
            "cannot be batched safely. Upload the assignment mark scheme and "
            "retry — nothing was sent to the AI.")

    from ..large_pdf.runner import LargePdfRunnerHolder
    runner = LargePdfRunnerHolder.instance()
    pinned = job.get("id")
    if job.get("submission_id"):
        large = runner.enqueue_classroom(
            submission_id=job["submission_id"],
            coursework_id=job.get("coursework_id"),
            course_id=job.get("course_id"),
            student_id=job.get("student_id"),
            student_name=job.get("student_name"),
            student_pdf_path=student_pdf, mark_scheme_path=mark_scheme_path,
            job_id=pinned)
    else:
        large = runner.enqueue_manual(
            student_name=job.get("student_name") or "Student",
            student_pdf_path=student_pdf, mark_scheme_path=mark_scheme_path,
            job_id=pinned)
    runner.prepare(large["id"])
    log_event("router", "routed submission", route=LARGE, pages=pages,
              threshold=limit, job_id=pinned)
    return True


def _merge_pdfs(paths: list[str], job: dict) -> str:
    """Concatenate a multi-attachment submission into one student PDF."""
    from pathlib import Path
    from pypdf import PdfReader, PdfWriter
    from ..config import DATA_DIR

    out_dir = Path(DATA_DIR) / "tmp" / "router_merged"
    out_dir.mkdir(parents=True, exist_ok=True)
    target = out_dir / f"{(job.get('id') or 'job')}_student.pdf"
    writer = PdfWriter()
    for path in paths:
        try:
            for page in PdfReader(path).pages:
                writer.add_page(page)
        except Exception:  # noqa: BLE001 — skip an unreadable part, keep the rest
            continue
    with open(target, "wb") as handle:
        writer.write(handle)
    return str(target)


__all__ = ["NORMAL", "LARGE", "RoutingError", "RoutingDecision",
           "threshold_pages", "route_student_pdf", "delegate_long_document"]
