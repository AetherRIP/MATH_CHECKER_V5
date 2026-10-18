"""Deterministic submission-quality validation (§9 — engine-side source of truth).

The correction engine judges only what it is given. Two of the most damaging
failure modes it CANNOT see are:

* the student submitted the WRONG assignment (the pipeline faithfully marks it
  and the maths class turns out to be a history essay → arbitrary marks), and
* the paper was never actually completed (a few-one-page scan worth of
  near-blank pages → "everything is unattempted" can be read as a
  genuine 0/150 instead of an incomplete submission), and
* the submission is unreadable/corrupted so no marks can be trusted.

This module checks the STRUCTURE of the marking result against the mark-scheme
blueprint in plain Python — no AI, no network — and produces a verdict that the
verification layer and the automation safety gate can act on. It never changes
a mark: it only flags.
"""

from __future__ import annotations

from . import legacy_marking as legacy

QUALITY_VALID = "VALID"
QUALITY_REVIEW = "SUBMISSION_REVIEW_REQUIRED"

# Percentages of the paper a real near-blank submission usually covers.
_BLANK_AWARDED_FRACTION_MIN = 1   # <= this many marks earned looks blank
_BLANK_MAX_FRACTION = 0.15        # <=15% of max answered looks near-blank



def assess_submission_quality(result: dict | None,
                              verification: dict | None) -> dict:
    """Classify the submission itself from the finished correction evidence.

    Returns ``{quality, review, reasons}`` where ``quality`` is
    :data:`QUALITY_VALID` or :data:`QUALITY_REVIEW`. Never raises.
    """
    reasons: list[str] = []
    result = result if isinstance(result, dict) else {}
    questions = result.get("questions") or []
    verification = verification if isinstance(verification, dict) else {}

    # calculate_totals requires the questions key to exist; an empty result is
    # a fully-blank signal handled below, not a crash.
    try:
        earned, available, percentage = legacy.calculate_totals(result)
    except (KeyError, TypeError, ValueError):
        earned, available, percentage = 0.0, 0.0, 0.0
    review_questions = set(verification.get("review_questions") or [])

    # unreadable / no-work-at-all
    if not questions:
        reasons.append("Nothing at all could be read from the submission.")
    unreadable = [
        str(q.get("question_number"))
        for q in questions
        if "unreadable" in (
            (str(q.get("student_answer", "")) + " "
             + str(q.get("working_summary", "")) + " "
             + str(q.get("loss_reason", "")) + " "
             + str(q.get("review_reason", "")))).lower()
    ]
    if unreadable:
        reasons.append(
            f"{len(unreadable)} answer(s) could not be read from the scan "
            f"(e.g. Q{unreadable[0]}).")

    # near-blank paper: almost nothing was recognised as attempted
    if available and percentage <= 100.0 * _BLANK_MAX_FRACTION:
        attempted = sum(
            1 for q in questions
            if str(q.get("student_answer") or "").strip())
        if attempted <= 1:
            reasons.append(
                f"Only {attempted} of {len(questions)} question(s) contain any "
                f"recognised answer and the score is {percentage:g}% (≤ "
                f"{_BLANK_MAX_FRACTION:.0%} of the paper) — this may be blank "
                "or the wrong assignment, not a genuine score.")

    # wrong assignment: most answers are not in the mark scheme
    blueprint = verification.get("blueprint") if isinstance(
        verification.get("blueprint"), dict) else {}
    extras = len(blueprint.get("extra_questions") or [])
    missing = len(verification.get("review_questions") or [])
    total_marked = len(questions)
    if total_marked and extras and extras >= 0.5 * total_marked:
        reasons.append(
            f"{extras} of {total_marked} answers do not match the mark "
            "scheme — the student may have submitted the wrong assignment.")

    # unmarked technical gaps in the result (marker never returned a question)
    if missing and total_marked and missing >= 0.5 * total_marked:
        reasons.append(
            f"{missing} of {total_marked} question(s) were returned UNMARKED "
            "(a technical gap) — the marks for them are not confirmed.")

    if reasons:
        return {"quality": QUALITY_REVIEW, "review": True, "reasons": reasons}
    return {"quality": QUALITY_VALID, "review": False, "reasons": []}


# Re-exported for convenience: the confidence value the guard/pool uses when
# a submission itself is suspect.
from .result_guard import (  # noqa: E402,F401
    SUBMISSION_REVIEW_REQUIRED,
)

__all__ = ["assess_submission_quality", "QUALITY_VALID", "QUALITY_REVIEW",
           "SUBMISSION_REVIEW_REQUIRED"]
