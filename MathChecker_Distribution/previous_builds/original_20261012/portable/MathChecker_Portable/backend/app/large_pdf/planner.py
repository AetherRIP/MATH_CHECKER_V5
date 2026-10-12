"""Step 1 of the Large PDF Worker: inspect the document, build the plan.

This is deliberately separated from marking. The worker calls ``prepare`` and
only a fully validated plan lets marking begin — the AI is never handed a
long PDF to choke on.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..config import get_config
from ..engine import batching, pdf_inspect


@dataclass
class PreparedPlan:
    page_count: int
    plan: batching.BatchPlan | None
    scheme_questions: list[str]
    question_pages: dict[str, list[int]]
    notes: list[str] = field(default_factory=list)
    blocked: bool = False
    block_reason: str | None = None
    mark_scheme_pages: int = 0

    @property
    def batch_count(self) -> int:
        return len(self.plan.batches) if self.plan else 0


def prepare(student_pdf: str, mark_scheme_pdf: str) -> PreparedPlan:
    """Deterministically inspect both files and produce a safe batch plan."""
    cfg = get_config().large_pdf
    notes: list[str] = []

    # The mark scheme's page count is read independently: its page numbers are
    # NEVER assumed to line up with the student's PDF.
    mark_scheme_pages = pdf_inspect.page_count(mark_scheme_pdf)
    scheme_questions = pdf_inspect.expected_question_numbers(mark_scheme_pdf)

    page_count = pdf_inspect.page_count(student_pdf)
    if page_count < 1:
        return PreparedPlan(
            page_count=0, plan=None, scheme_questions=scheme_questions,
            question_pages={}, notes=notes, blocked=True,
            block_reason="The student answer PDF could not be read or has no "
                         "pages. Nothing was sent to the correction engine.",
            mark_scheme_pages=mark_scheme_pages)

    inspection = pdf_inspect.inspect_student_pdf(
        student_pdf, scheme_questions or None)
    question_pages = inspection.question_pages
    if inspection.mode != "TEXT":
        notes.append(
            "The student PDF has no usable text layer (scanned images only) — "
            "the plan is a balanced page-count partition; question boundaries "
            "could not be verified from the text.")
    if not question_pages and scheme_questions:
        notes.append(
            "No question numbers could be read from the student PDF — question "
            "coverage will be validated against the mark scheme at merge time.")

    # The one-time routing decision (centralised, configurable).
    if page_count <= cfg.threshold_pages and not _forced_batching(cfg):
        # Short document: the existing single-pass workflow is preserved.
        plan = batching.plan_batches(
            page_count, target_min=cfg.target_min, target_max=cfg.threshold_pages,
            question_pages=question_pages, overlap=cfg.context_overlap)
        notes.append(
            f"{page_count} pages is within the single-pass threshold "
            f"({cfg.threshold_pages}); no batching required.")
    else:
        plan = batching.plan_batches(
            page_count, target_min=cfg.target_min, target_max=cfg.target_max,
            question_pages=question_pages, overlap=cfg.context_overlap)

    problems = batching.validate_plan(plan)
    if problems:
        return PreparedPlan(
            page_count=page_count, plan=None, scheme_questions=scheme_questions,
            question_pages=question_pages,
            notes=notes + problems, blocked=True,
            block_reason=("A safe batch plan could not be built: "
                          + "; ".join(problems)),
            mark_scheme_pages=mark_scheme_pages)

    notes.extend(plan.notes)
    return PreparedPlan(
        page_count=page_count, plan=plan, scheme_questions=scheme_questions,
        question_pages=question_pages, notes=notes, mark_scheme_pages=mark_scheme_pages)


def _forced_batching(cfg) -> bool:
    """Batching is the norm for long docs; a short doc stays single-pass."""
    return False
