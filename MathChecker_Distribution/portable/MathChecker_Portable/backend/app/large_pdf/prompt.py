"""The DEDICATED marking prompt for the Large PDF Worker.

Why a separate prompt exists (real defect, 49-page O Level submission):

The long-document workflow marks ONE BATCH of student pages at a time, but it
was sending the ordinary single-file prompt. That prompt assumes the model
holds the whole script and has no notion of "your pages are one batch of a
longer paper", so a batch that ran while the provider was degraded answered a
few questions and stopped — and nothing in the prompt told the model that
stopping early is a failure, or that the mark scheme it was given covers the
WHOLE paper (not just the pages in front of it). The result was 32 questions
required by the mark scheme (128 marks) never marked, reported to the teacher
as a 46.6% score.

The rules the large-PDF prompt adds, on top of the standard marking rules:

* the attached official mark scheme is the COMPLETE scheme and is NEVER split;
* the student pages supplied are ONE BATCH of a longer script, so the batch
  owns only some of the paper's questions — absence from the batch is not
  absence from the paper, and must never be scored as 0;
* a question whose working continues onto another page of the same batch is
  ONE question and its continuation must be read before the mark is set;
* the response must be complete: every question visible in the supplied pages
  gets an entry, and the model must not stop early;
* anything not visible must be reported as an uncertainty, never invented.

The text is a module constant so it can be inspected and overridden through the
existing configuration path (``[large_pdf] system_prompt_override`` /
``user_prompt_override`` in ``data/config.toml``), independently of the normal
correction prompt.
"""

from __future__ import annotations

from ..config import get_config

LARGE_PDF_MARKING_SYSTEM = r"""
You are an automated Pearson Edexcel International GCSE Mathematics
marking assistant. You are NOT an official Pearson employee or examiner.

You are marking ONE BATCH of pages from a LONG student answer script. The same
student's script was split into batches only so it fits in a single request; the
official mark scheme you were given is the COMPLETE, UNSHORTENED scheme for the
whole paper. Treat it as the single authoritative reference.

STANDARD MARKING RULES
1. Match each question to the corresponding mark-scheme question and award
   marks using the supplied mark scheme, not generic intuition.
2. Respect method (M), accuracy (A), independent/basis (B), error-carried-
   forward (ECF), follow-through, implied answers and "ignore subsequent
   working" (ISW) where the mark scheme supports them.
3. Do not award a mark because a final answer merely looks plausible, and do
   not remove an earlier earned mark because of a later contradiction when the
   mark scheme says the earlier work stands (ISW).
4. Do not invent working that is not visible. If handwriting or a diagram is
   genuinely unreadable, say so in "uncertainties" instead of guessing.
5. Do not penalise a formatting difference when the mathematics is equivalent.
6. Use the mark scheme's exact maximum marks, award integer marks only, and
   never exceed a question's maximum.
7. For each lost mark, explain the precise mathematical/mark-scheme reason, and
   give a full-mark solution the student can study.

LONG-DOCUMENT RULES (these decide whether the marking is usable at all)

A. USE THE WHOLE OFFICIAL MARK SCHEME
   The mark scheme covers the entire paper. Use every part of it that is
   relevant to the questions in front of you — including questions whose
   numbers do not resemble this batch's page numbers. Never assume a question
   is absent from the assignment because it is not visible in these pages, and
   never copy the mark scheme's maximum mark for the wrong question.

B. ONLY THE SUPPLIED STUDENT PAGES MAY BE MARKED
   Identify which questions of the paper are actually present in the supplied
   pages and return an entry for each of them. A question the mark scheme
   declares but that is NOT visible in these pages must NOT be returned and
   must NOT be scored (not even 0): it belongs to another batch. List those
   top-level question numbers in "uncertainties" as
   "not in this batch: <numbers>" so the batch's coverage is auditable.

C. READ EVERY SUPPLIED PAGE
   Inspect all pages given to you: handwriting, algebraic working, tables,
   diagrams, graphs, Venn diagrams and intermediate calculations. Base every
   judgement on visible evidence, and distinguish a genuinely BLANK answer from
   one that is unreadable or missing from the batch. A genuinely blank answer
   gets 0 marks with a blank-answer explanation.

D. A QUESTION THAT CONTINUES IS ONE QUESTION
   Working often continues onto the next page (or the next batch's first
   pages) of the SAME question. Read the continuation before fixing the mark,
   keep the question's own numbering and sub-part structure
   ("14(a)(ii)" etc.) exactly as the paper and mark scheme label it, and never
   mark a question wrong merely because the final answer or later work appears
   on another page.

E. FINISH THE RESPONSE — DO NOT STOP EARLY
   Return one entry for EVERY question visible in the supplied pages, in the
   paper's order, before writing "overall_notes". Omitting questions that are
   clearly answered in the pages you were given makes the whole correction
   unusable, because an unreturned question cannot be scored and would be
   mistaken for missing work. If you are running out of room, shorten the
   explanations — never drop a question.

F. SUB-PART NUMBERING
   If the paper sub-divides a question (a)(i), (a)(ii), (b), return that
   sub-part as its own entry when the mark scheme assigns it separate marks;
   otherwise return one entry for the top-level question. Never merge two
   different questions into one entry and never invent a question number.

The application's Python code calculates the total score and percentage from
the individual question marks. Do not adjust marks to make the arithmetic sound
consistent, and do not produce a narrative total.
"""

#: The batch-specific user turn. The page range and the paper's total page
#: count are stated so the model knows it holds a SLICE of the script, and how
#: big that slice is relative to the whole paper.
_LARGE_PDF_USER = """
Mark the student pages supplied in the student PDF above against the complete
official mark scheme that is attached.

This request is {batch_label} of {paper_pages} page(s) in total: you have the
student's pages {page_start}-{page_end}{context_note}.

Remember, for this batch:
- the attached mark scheme is the COMPLETE official scheme for the whole paper
  ({scheme_note}) and has NOT been shortened for this batch;
- mark ONLY the questions whose working appears in pages {page_start}-{page_end};
- a question whose working continues onto a later page must be read as one
  question (say so in "uncertainties" if the continuation is not in this batch);
- do NOT return or score a question that is not visible in this batch;
- finish the response: an entry for every question visible in these pages,
  in the paper's order, then the summary.

Return ONLY the requested JSON structure.
"""


def _scheme_note(blueprint: dict | None, scheme_questions=None) -> str:
    """A short, factual description of the scheme actually supplied."""
    total = None
    count = None
    if isinstance(blueprint, dict):
        try:
            total = int(blueprint.get("total_marks") or 0) or None
        except (TypeError, ValueError):
            total = None
        rows = blueprint.get("questions")
        if isinstance(rows, list) and rows:
            count = len(rows)
            try:
                total = total or sum(int(q.get("max_marks") or 0)
                                     for q in rows if isinstance(q, dict))
            except (TypeError, ValueError):
                pass
    if count is None and scheme_questions:
        count = len(scheme_questions)
    parts = []
    if count:
        parts.append(f"{count} questions")
    if total:
        parts.append(f"{total} marks")
    return ", ".join(parts) if parts else "structure not re-extracted"


def large_pdf_user_prompt(*, batch: dict, paper_pages: int,
                          blueprint: dict | None = None,
                          scheme_questions=None) -> str:
    """The batch user-turn text (see the rules it enforces above)."""
    try:
        index = int(batch.get("batch_index", 0)) + 1
    except (TypeError, ValueError):
        index = 1
    total = int(batch.get("_batch_total") or 0) or _batch_total(batch)
    start = batch.get("page_start", "?")
    end = batch.get("page_end", "?")
    before = int(batch.get("context_before") or 0)
    after = int(batch.get("context_after") or 0)
    context = ""
    if before or after:
        context = (f" (with {before} page(s) before and {after} page(s) after "
                   f"for context only — context pages are NOT scored)")
    label = (f"batch {index} of {total}" if total and total > 1
             else "the complete script")
    return _LARGE_PDF_USER.format(
        batch_label=label, paper_pages=max(1, int(paper_pages or 0)),
        page_start=start, page_end=end, context_note=context,
        scheme_note=_scheme_note(blueprint, scheme_questions))


def _batch_total(batch: dict) -> int:
    try:
        return max(1, int(batch.get("batch_total") or 1))
    except (TypeError, ValueError):
        return 1


def effective_large_pdf_system_prompt() -> str:
    """The system instruction the Large PDF Worker actually sends.

    A Developer override in ``[large_pdf] system_prompt_override`` replaces the
    built-in text; the normal correction prompt is never used for a batched
    long document, because it cannot express the batch/continuation rules.
    """
    override = getattr(get_config().large_pdf, "system_prompt_override", "")
    return override if override and override.strip() else LARGE_PDF_MARKING_SYSTEM


def effective_large_pdf_user_prompt(**kwargs) -> str:
    """The user turn the Large PDF Worker actually sends (override-aware)."""
    override = getattr(get_config().large_pdf, "user_prompt_override", "")
    if override and override.strip():
        return override
    return large_pdf_user_prompt(**kwargs)


def prompts_for_batch(*, batch: dict, paper_pages: int,
                      blueprint: dict | None = None,
                      scheme_questions=None) -> tuple[str, str]:
    """``(system_prompt, user_prompt)`` for one batch request."""
    return (effective_large_pdf_system_prompt(),
            effective_large_pdf_user_prompt(
                batch=batch, paper_pages=paper_pages, blueprint=blueprint,
                scheme_questions=scheme_questions))
