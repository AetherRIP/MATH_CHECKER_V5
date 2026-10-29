"""A synthetic mark-scheme structure for Demo Mode only.

Demo Mode never reads a mark scheme: it has no model to read it with, and a
correction in Demo Mode marks by mock. That left the review page permanently
empty for anyone exploring the app without an API key — every row of the
per-question workflow (answers, handling decisions, decision history, finalising)
was unreachable.

This builds a structure the page CAN show, and labels it at every level so it can
never be mistaken for a real read:

* every question carries ``"demo": True``;
* the decision row reports the extraction state ``DEMO``, which is not
  ``RELIABLE``, so no row here can pass for an extraction and the review page
  carries a notice while the synthetic structure is in use;
* the record is stored with ``model="demo"``, and the review page shows a
  notice while that record is in use.

It is deterministic: the question numbers come from the document's own text
layer when it has one, so the same PDF always yields the same structure.

The SHAPE is a stand-in for a real paper, not a reading of your PDF's marks. A
paper with every question worth the same two marks, each split into two one-mark
parts, does not exercise the page: mark totals that do not add up the way you
expect, sub-questions labelled ``1(b)(ii)``, questions with no parts at all and a
pool of marks per question are exactly what the review workflow exists to handle.
So the shapes below are cycled over the numbers found, and the whole structure is
labelled synthetic.
"""

from __future__ import annotations

import re

#: The shapes a demo paper is built from, cycled over the question numbers the
#: document shows: ``(marks, ((part label, marks), ...))``. A real read takes
#: these from the scheme; here they are synthetic, which is why every row says so.
#: Part labels are printed the way a paper prints them — ``(a)``, ``(b)(ii)``,
#: ``(a)(i)`` — so the page's hierarchy and its id handling are both exercised.
QUESTION_SHAPES: tuple[tuple[int, tuple[tuple[str, int], ...]], ...] = (
    (5, (("(a)", 2), ("(b)(i)", 1), ("(b)(ii)", 2))),
    (3, ()),
    (4, (("(a)", 1), ("(b)", 3))),
    (6, ()),
    (2, (("(a)", 1), ("(b)", 1))),
    (4, (("(a)(i)", 1), ("(a)(ii)", 3))),
)

#: How many questions a demo structure will show at most.
MAX_QUESTIONS = 8

#: Used when the PDF has no usable text layer to find numbers in.
DEFAULT_NUMBERS = ("1", "2", "3")

#: Every demo answer carries this tag as its first word. It has to be obvious in
#: a narrow table cell that this is a placeholder and not a marked answer, so the
#: tag comes first and the sentence says what the text is.
DEMO_ANSWER_TAG = "[demo]"


def _answer(label: str) -> str:
    """The placeholder answer shown for one demo question."""
    return f"{DEMO_ANSWER_TAG} placeholder answer for question {label} — type over it to try editing"

_LEADING_NUMBER = re.compile(r"^\s*(\d{1,2})\s*$")


def _question_numbers(evidence: dict) -> list[str]:
    """Question numbers the document's text layer shows, in reading order."""
    numbers: list[str] = []
    for candidate in evidence.get("candidates") or []:
        if not isinstance(candidate, dict):
            continue
        match = _LEADING_NUMBER.match(str(candidate.get("value") or ""))
        if not match:
            continue
        number = str(int(match.group(1)))
        if number not in numbers:
            numbers.append(number)
        if len(numbers) >= MAX_QUESTIONS:
            break
    return numbers


def build(mark_scheme_path: str) -> dict | None:
    """A labelled demo blueprint for this file, or None if it cannot be built.

    The document is analysed with the same local, free reader a real extraction
    starts from, so the question numbers shown here correspond to what is
    actually printed. Nothing is read from a model and no answer is claimed as
    the scheme's: every question is marked ``demo`` and every answer says so.
    """
    from . import blueprint as bp_mod
    from . import mark_scheme

    try:
        evidence = mark_scheme.analyse_document(str(mark_scheme_path))
    except Exception:  # noqa: BLE001 - a demo structure must never fail a page
        evidence = {}

    numbers = _question_numbers(evidence) or list(DEFAULT_NUMBERS)
    pages = {
        str(candidate.get("value")): candidate.get("page")
        for candidate in (evidence.get("candidates") or [])
        if isinstance(candidate, dict)
    }

    questions: list[dict] = []
    for index, number in enumerate(numbers):
        page = pages.get(number) or 1
        marks, parts = QUESTION_SHAPES[index % len(QUESTION_SHAPES)]
        question = {
            "question_number": number,
            "max_marks": marks,
            "page": page,
            "answers": [_answer(number)],
            "criteria": "DEMO — synthetic marking guidance, not read from your PDF.",
            "demo": True,
        }
        if parts:
            question["parts"] = [
                {
                    "number": label,
                    "max_marks": part_marks,
                    "page": page,
                    "answers": [_answer(f"{number}{label}")],
                    "criteria": "DEMO — synthetic marking guidance, not read from your PDF.",
                    "demo": True,
                }
                for label, part_marks in parts
            ]
        questions.append(question)

    blueprint = {
        "questions": questions,
        "total_marks": sum(int(q["max_marks"]) for q in questions),
        "demo": True,
    }
    return blueprint if bp_mod.validate(blueprint) else None
