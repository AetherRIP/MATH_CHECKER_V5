"""Coded review reasons, including for rows saved before codes existed.

New corrections write ``verification.review_reasons`` with a code, the stage
that raised it, the question and the action. Rows stored earlier only have the
prose in ``confidence_reasons``; the review queue would otherwise keep saying
"Verification requested review" for every one of them.

Deriving a code from that stored text only CLASSIFIES what was already recorded —
nothing is inferred about the marking and no field is rewritten. Each derived
entry carries ``derived: True`` so a reader can tell a recorded reason from a
recovered one.
"""

from __future__ import annotations

import re

#: Same vocabulary the live pipeline uses, so a teacher's filter does not
#: behave differently depending on when a paper was corrected.
CODE_MODEL_FALLBACK = "MODEL_FALLBACK"
CODE_QUESTION_FLAGGED = "QUESTION_FLAGGED"
CODE_VALIDATION_REVIEW = "VALIDATION_REVIEW"
CODE_SUBMISSION_QUALITY = "SUBMISSION_QUALITY"
CODE_MARK_ABOVE_MAXIMUM = "MARK_ABOVE_MAXIMUM"
CODE_CORRECTION_REVIEW = "CORRECTION_REVIEW"

_QUESTION_IN_REASON = re.compile(r"e\.g\.\s*([0-9]+[a-z]?(?:\([ivx]+\))?)", re.IGNORECASE)


def _entry(code: str, stage: str, severity: str, detail: str,
           question: str | None = None, action: str | None = None) -> dict:
    return {
        "code": code,
        "stage": stage,
        "severity": severity,
        "question": question,
        "detail": detail,
        "action": action,
        "derived": True,
    }


def from_confidence_reasons(reasons: list[str] | None,
                            verification: dict | None = None) -> list[dict]:
    """Turn stored confidence prose into coded reasons (never the reverse)."""
    out: list[dict] = []
    verification = verification or {}

    for text in reasons or []:
        line = str(text or "").strip()
        if not line:
            continue
        lower = line.lower()

        if "instead of the configured primary" in lower:
            out.append(_entry(
                CODE_MODEL_FALLBACK, "marking", "advisory", line,
                action="Re-run this paper once the primary model is healthy if "
                       "the grade must be final."))
            continue

        if "submission quality" in lower:
            out.append(_entry(
                CODE_SUBMISSION_QUALITY, "extraction", "review", line,
                action="Check that the scan is the right paper and is readable."))
            continue

        if "flagged for review" in lower:
            match = _QUESTION_IN_REASON.search(line)
            question = match.group(1) if match else None
            detail = line
            # The question's own recorded reason is more useful than the count.
            for recorded in verification.get("warnings") or []:
                if question and str(recorded).startswith(f"Question {question}"):
                    detail = str(recorded).split(":", 1)[-1].strip()
                    break
            out.append(_entry(
                CODE_QUESTION_FLAGGED, "marking", "review", detail,
                question=question,
                action="Check the question against the scan and set the mark by "
                       "hand if it is wrong."))
            continue

        if "exceed the maximum" in lower:
            out.append(_entry(
                CODE_MARK_ABOVE_MAXIMUM, "validation", "review", line,
                action="Award within the paper's maximum."))
            continue

        if "verification requested review" in lower:
            # Prefer the verification's own words, but only a PAPER-level one:
            # a "Question 16: …" line already has its own entry and repeating it
            # here would read as two separate findings.
            detail = line
            for recorded in (verification.get("anomalies") or []) + \
                    (verification.get("warnings") or []):
                if str(recorded).startswith("Question "):
                    continue
                detail = str(recorded)
                break
            out.append(_entry(
                CODE_VALIDATION_REVIEW, "validation", "review", detail,
                action="Open the result and read the recorded finding before "
                       "deciding."))
            continue

        out.append(_entry(CODE_CORRECTION_REVIEW, "validation", "review", line))

    # One entry per code+question: the same fact is often recorded twice.
    seen: set[tuple[str, str | None]] = set()
    unique: list[dict] = []
    for entry in out:
        key = (entry["code"], entry["question"])
        if key in seen:
            continue
        seen.add(key)
        unique.append(entry)
    return unique


__all__ = ["from_confidence_reasons"]
