"""Deterministic guard against marks the marker's own evidence contradicts.

The correction engine trusts Gemini for the mathematics, but it must never
trust a mark that the result itself says has nothing behind it.  A weak model
handed a 15-page scan and a 26-page scheme once returned:

    "student_answer": "Blank on page, implied complete",
    "awarded_marks": 4, "max_marks": 4

for a question that was genuinely blank.  Every downstream total was
arithmetically correct, so no existing check caught it.

This module applies only rules that need **no mathematical judgement** and
never invents a mark:

* an explicitly absent answer (blank / unattempted / unreadable / omitted)
  cannot receive credit — the award is withdrawn and the question is sent to
  review;
* a full-mark award whose own M/A/B breakdown says a mark was NOT awarded is
  contradictory — it is kept but sent to review;
* a partial award whose loss reason says "no marks lost" while every marking
  point was awarded is contradictory — it is kept but sent to review.

Everything else is left exactly as the marker produced it.  A legitimate
partial credit or a correct answer with no visible working is never changed
just because the wording looks unusual.
"""

from __future__ import annotations

import re

STATUS_REVIEW = "REVIEW"

#: Phrases that state the answer is not present in the submission.  They are
#: matched against ``student_answer`` only: a summary like "no working shown"
#: can still describe a correct final answer that earns an A1.
_ABSENT = re.compile(
    r"\b(?:"
    r"unattempted|not\s+attempted|not\s+answered|no\s+answer|"
    r"blank|empty|illegible|unreadable|"
    r"cannot\s+be\s+read|can't\s+be\s+read|not\s+visible|"
    r"not\s+shown|omitted|implied\s+complete"
    r")\b",
    re.IGNORECASE,
)

#: A loss reason that explicitly claims nothing was lost.
_NO_LOSS = re.compile(
    r"\b(?:none|no\s+marks?\s+lost|full\s+marks?\s+awarded)\b",
    re.IGNORECASE,
)


def _int(value) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _explicitly_absent(answer: str) -> str | None:
    """Return the absence phrase only when the WHOLE answer is absent.

    A reconciled parent row may read ``"16125 | Unattempted"`` when part (b)
    was blank; the question still earned part (a)'s marks.  Treating any
    occurrence of "unattempted" as the whole question being blank would wipe
    legitimate credit (observed on Q15: 3/3 for (a) became 0/6).
    """
    text = (answer or "").strip()
    if not text:
        return None
    parts = [p.strip() for p in re.split(r"[|;\n]", text) if p.strip()]
    if len(parts) > 1:
        if all(_ABSENT.search(part) for part in parts):
            match = _ABSENT.search(parts[0])
            return match.group(0) if match else None
        return None
    match = _ABSENT.search(text)
    return match.group(0) if match else None


_NUMBER = re.compile(r"(?<![\w.])-?\d[\d,]*(?:\.\d+)?(?![\w.])")


def _numbers(text: str) -> list[float]:
    out = []
    for raw in _NUMBER.findall(text or ""):
        try:
            out.append(float(raw.replace(",", "")))
        except ValueError:
            pass
    return out


def _same_number(a: float, b: float) -> bool:
    """Allow ordinary rounding, not a different value.

    0.5% (or one unit, for small amounts) covers "9826" vs "9825.6" and
    "43076" vs "43075.625", but not a genuinely different answer such as
    "4780" where the marker's own solution says "5740".
    """
    return abs(a - b) <= max(1.0, 0.005 * max(abs(a), abs(b)))


def _parts(q: dict) -> list[dict]:
    parts = q.get("subquestions")
    if not isinstance(parts, list):
        return []
    return [p for p in parts if isinstance(p, dict)]


def _part_allocated(q: dict) -> bool:
    """Does the mark scheme allocate this question's marks per subquestion?

    When the parts' maxima add up to the parent's maximum, the parent row is an
    AGGREGATE: its marks come from the parts, and the parent's own
    ``mark_breakdown`` / ``loss_reason`` / solution text describes only one part
    (usually the one the marker happened to quote). Judging the aggregate by
    that single fragment is what sent a correct 1/3 (1/1 for (b), 0/2 for (d))
    to review as "every listed point awarded but not full marks".
    """
    parts = _parts(q)
    if not parts:
        return False
    try:
        parts_max = sum(_int(p.get("max_marks")) for p in parts)
    except (TypeError, ValueError):
        return False
    return parts_max > 0 and parts_max == _int(q.get("max_marks"))


def _explained_by_parts(q: dict) -> bool:
    """Are the parent's awarded marks exactly the sum of its parts' awards?"""
    parts = _parts(q)
    if not parts or not _part_allocated(q):
        return False
    return sum(_int(p.get("awarded_marks")) for p in parts) == _int(q.get("awarded_marks"))


def _full_mark_contradiction(q: dict) -> tuple[float, float, bool] | None:
    """A numeric final answer that the marker's own solution does not contain.

    Only FULL-MARK awards are checked, and only when both sides contain
    numbers.  A valid alternative form/rounding is accepted if it matches any
    number in the marker's solution; the check exists to catch the marker
    accepting an answer its own working contradicts, not to replace its
    mathematical judgement.
    """
    answer = str(q.get("student_answer") or "")
    solution = str(q.get("full_mark_solution") or "") or \
        str(q.get("correct_method") or "")
    got = _numbers(answer)
    wanted = _numbers(solution)
    if not got or not wanted:
        return None
    if any(_same_number(a, b) for a in got for b in wanted):
        return None
    student_final, solution_final = got[-1], wanted[-1]
    # Only a LARGE numeric mismatch withdraws a mark.  Small answers are left
    # alone (equivalent fractions/ratios and rounding would otherwise be
    # penalised); they are still reported for review below.
    large = (
        abs(student_final) >= 100 and abs(solution_final) >= 100
        and abs(student_final - solution_final) >
        max(1.0, 0.01 * max(abs(student_final), abs(solution_final)))
    )
    return student_final, solution_final, large


def enforce_evidence(result: dict) -> tuple[dict, dict]:
    """Return ``(result, report)`` after evidence-consistency enforcement.

    The result is mutated in place (the database copy is built from it) and
    also returned for callers that prefer assignment.  The report names every
    change and every question sent to review, so the UI can explain it.
    """
    report = {
        "changed": [],
        "review_questions": [],
        "warnings": [],
    }
    questions = result.get("questions") if isinstance(result, dict) else None
    if not isinstance(questions, list):
        return result, report

    # Each subquestion is judged on its OWN answer and marks; the parent is
    # judged as an aggregate (below).  A part marked while its answer is
    # explicitly absent earns nothing, exactly as for a whole question.
    for parent in questions:
        if not isinstance(parent, dict):
            continue
        for part in _parts(parent):
            awarded = _int(part.get("awarded_marks"))
            answers = part.get("answers")
            text = (" | ".join(str(a) for a in answers)
                    if isinstance(answers, list) else str(answers or ""))
            absent = _explicitly_absent(text)
            if awarded <= 0 or not absent:
                continue
            label = str(part.get("label") or part.get("id") or "?")
            reason = (f"The marker recorded no readable answer for subquestion {label} "
                      f"({absent}) but awarded {awarded} mark(s). The award was "
                      f"withdrawn: a missing answer cannot earn credit. Re-check "
                      f"this part against the scan.")
            part["awarded_marks"] = 0
            report["changed"].append({
                "question_number": label,
                "from": awarded,
                "to": 0,
                "reason": reason,
            })
            report["review_questions"].append(label)
            report["warnings"].append(f"Subquestion {label}: {reason}")

    for q in questions:
        if not isinstance(q, dict):
            continue
        number = str(q.get("question_number", "?"))
        awarded = _int(q.get("awarded_marks"))
        maximum = _int(q.get("max_marks"))
        answer = str(q.get("student_answer") or "").strip()

        absent = _explicitly_absent(answer)
        if awarded > 0 and absent:
            reason = (f"The marker recorded no readable student answer "
                      f"({absent}) but awarded {awarded} mark(s). "
                      f"The award was withdrawn: a missing answer cannot earn "
                      f"credit. Re-check this question against the scan.")
            q["awarded_marks"] = 0
            q["status"] = STATUS_REVIEW
            q["review_reason"] = reason
            report["changed"].append({
                "question_number": number,
                "from": awarded,
                "to": 0,
                "reason": reason,
            })
            report["review_questions"].append(number)
            report["warnings"].append(f"Question {number}: {reason}")
            continue

        # An EMPTY answer field is a metadata gap, not proof that the page is
        # blank (method marks can be earned without a final answer line).  It
        # is deliberately left untouched; only an explicit absence is acted on.

        breakdown = q.get("mark_breakdown")
        if not isinstance(breakdown, list):
            breakdown = []

        # A per-subquestion aggregate carries only one part's evidence, so the
        # parent-level text checks below do not apply to it. Its marks are the
        # sum of its parts, and each part was checked above.
        aggregate = _part_allocated(q)
        if aggregate and _explained_by_parts(q):
            continue

        # A full-mark award whose own solution gives a different final number
        # is not a full-mark answer.  Withdraw the accuracy mark (never a
        # method mark) and send the question to review.  This caught a live
        # Q30 where the recorded answer was 4780 while the marker's own
        # working gave 5740.
        if maximum > 0 and awarded >= maximum and not aggregate:
            contradiction = _full_mark_contradiction(q)
            if contradiction is not None:
                got, wanted, large = contradiction
                if not large:
                    reason = (
                        f"Full marks were awarded although the marker's own "
                        f"solution gives {wanted:g} and the recorded student "
                        f"answer is {got:g}. The marks were kept; review this "
                        f"question before the grade is used.")
                    q["status"] = STATUS_REVIEW
                    q["review_reason"] = reason
                    report["review_questions"].append(number)
                    report["warnings"].append(f"Question {number}: {reason}")
                    continue
                new_award = max(0, maximum - 1)
                # Flip the last awarded accuracy code, when the breakdown names
                # one, so the evidence matches the mark that was withdrawn.
                for item in reversed(breakdown):
                    if not isinstance(item, dict) or not item.get("awarded"):
                        continue
                    code = str(item.get("code") or "").strip().upper()
                    if code.startswith("A"):
                        item["awarded"] = False
                        item["reason"] = (
                            "Withdrawn: the student's final answer "
                            f"({got:g}) differs from the marker's own "
                            f"solution ({wanted:g}).")
                        break
                reason = (
                    f"Full marks were awarded although the marker's own "
                    f"solution gives {wanted:g} and the recorded student "
                    f"answer is {got:g}. One accuracy mark was withheld; "
                    f"review the question before the grade is used.")
                q["awarded_marks"] = new_award
                q["loss_reason"] = reason
                q["status"] = STATUS_REVIEW
                q["review_reason"] = reason
                report["changed"].append({
                    "question_number": number,
                    "from": awarded,
                    "to": new_award,
                    "reason": reason,
                })
                report["review_questions"].append(number)
                report["warnings"].append(f"Question {number}: {reason}")
                continue

        # A full-mark award must not contain a marking point the marker itself
        # marked as missed.  Keep the award (the breakdown is metadata) but do
        # not let it pass as a routine, trusted result.
        if maximum > 0 and awarded >= maximum and not aggregate and any(
                isinstance(item, dict) and not bool(item.get("awarded"))
                for item in breakdown):
            reason = ("Full marks were awarded even though the marker's own "
                      "mark breakdown says a marking point was not awarded.")
            q["status"] = STATUS_REVIEW
            q["review_reason"] = reason
            report["review_questions"].append(number)
            report["warnings"].append(f"Question {number}: {reason}")
            continue

        # A partial award that claims no marks were lost, with every listed
        # marking point awarded, cannot be explained by its own evidence.
        loss_reason = str(q.get("loss_reason") or "").strip()
        if (0 < awarded < maximum and breakdown and not aggregate
                and all(bool(item.get("awarded")) for item in breakdown
                        if isinstance(item, dict))
                and _NO_LOSS.search(loss_reason)):
            reason = ("The marker awarded fewer than the maximum marks but "
                      "described the question as fully correct with every "
                      "listed marking point awarded. The award was kept; the "
                      "question needs a human check.")
            q["status"] = STATUS_REVIEW
            q["review_reason"] = reason
            report["review_questions"].append(number)
            report["warnings"].append(f"Question {number}: {reason}")

    report["review_questions"] = sorted(set(report["review_questions"]))
    return result, report


__all__ = ["enforce_evidence", "STATUS_REVIEW"]
