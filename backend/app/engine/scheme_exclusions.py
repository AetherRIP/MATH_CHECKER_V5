"""Questions a teacher has taken out of marking, per assignment.

A mark scheme can contain a question that must not be marked: the printed paper
does not match it, the scheme's own answer is wrong beyond repair, or the class
never covered it. Rewriting the PDF is not an option, so the exclusion is kept
here and applied in three places, all of them derived from the same rows:

* the marking prompt and reconciliation (the question is not asked for),
* the saved result (its rows are dropped before totals are calculated),
* the discrepancy audit (no row is raised for a question nobody marks).

The blueprint FILE is never modified: removing the row restores the question
exactly, and a paper corrected meanwhile keeps the marks it was given.
"""

from __future__ import annotations

import time

from .. import db
from .question_inventory import canonical_question_id, parent_canonical

__all__ = [
    "add", "canonical", "drop_questions", "filter_blueprint", "find", "ignored",
    "remove",
]


def canonical(value) -> str:
    """One comparable id for a question label ("Q12" and " 12 " agree)."""
    return canonical_question_id(str(value or ""))


def _canon(value) -> str:
    return canonical(value)


def ignored(coursework_id: str | None) -> dict[str, dict]:
    """Excluded questions for one assignment, keyed by canonical id."""
    if not coursework_id:
        return {}
    rows = db.query(
        "SELECT question_number, note, created_at FROM scheme_ignored_questions "
        "WHERE coursework_id = ? ORDER BY question_number",
        (coursework_id,))
    out: dict[str, dict] = {}
    for row in rows:
        out[_canon(row["question_number"])] = {
            "question_number": str(row["question_number"]),
            "note": row["note"] or "",
            "created_at": row["created_at"],
        }
    return out


def find(blueprint: dict | None, question_number: str) -> dict | None:
    """The blueprint question (or part) whose id matches, or None.

    Matching is canonical, so "Q12", "12" and " 12 " all find the same row —
    the UI sends what it read from the blueprint, and a teacher's typed value
    still has to name a question that really exists.
    """
    wanted = _canon(question_number)
    if not blueprint or not wanted:
        return None
    for question in blueprint.get("questions") or []:
        if _canon(question.get("question_number")) == wanted:
            return question
        for part in question.get("parts") or []:
            if _canon(part.get("number")) == wanted:
                return part
    return None


def filter_blueprint(blueprint: dict | None,
                     coursework_id: str | None) -> tuple[dict | None, list[dict]]:
    """The blueprint without the excluded questions, plus what was removed.

    A whole question drops its parts with it. A single excluded PART keeps the
    question and lowers its maximum by that part's marks, so the paper total
    stays the sum of what is actually being marked.
    """
    exclusions = ignored(coursework_id)
    if not blueprint or not exclusions:
        return blueprint, []

    removed: list[dict] = []
    kept_questions: list[dict] = []
    for question in blueprint.get("questions") or []:
        number = str(question.get("question_number"))
        if _canon(number) in exclusions:
            removed.append({
                "question_number": number,
                "max_marks": int(question.get("max_marks") or 0),
                "note": exclusions[_canon(number)]["note"],
            })
            continue

        parts = question.get("parts") or []
        dropped_parts = [p for p in parts if _canon(p.get("number")) in exclusions]
        if not dropped_parts:
            kept_questions.append(question)
            continue

        kept_parts = [p for p in parts if _canon(p.get("number")) not in exclusions]
        dropped_marks = sum(int(p.get("max_marks") or 0) for p in dropped_parts)
        for part in dropped_parts:
            removed.append({
                "question_number": str(part.get("number")),
                "max_marks": int(part.get("max_marks") or 0),
                "note": exclusions[_canon(part.get("number"))]["note"],
            })
        adjusted = dict(question)
        adjusted["parts"] = kept_parts
        adjusted["max_marks"] = max(0, int(question.get("max_marks") or 0) - dropped_marks)
        kept_questions.append(adjusted)

    trimmed = dict(blueprint)
    trimmed["questions"] = kept_questions
    # The total is recomputed rather than copied: an excluded question's marks
    # must not stay in the denominator of every paper.
    trimmed["total_marks"] = sum(int(q.get("max_marks") or 0) for q in kept_questions)
    trimmed["excluded_questions"] = removed
    return trimmed, removed


def drop_questions(result: dict | None, excluded: list[dict] | None) -> dict:
    """Remove every row belonging to an excluded question from a result.

    Both the question itself and any subquestion row ("12(a)") go, so a marker
    that split the question into parts cannot leave marks behind in the total.
    """
    if not result or not excluded:
        return result or {}
    skip = {_canon(item.get("question_number")) for item in excluded}
    skip |= {_canon(str(item.get("question_number")).split("(")[0]) for item in excluded}

    kept: list[dict] = []
    for row in result.get("questions") or []:
        canon = _canon(row.get("question_number"))
        if canon in skip or parent_canonical(canon) in skip:
            continue
        kept.append(row)
    result["questions"] = kept
    for key in ("groups", "question_groups", "rows"):
        rows = result.get(key)
        if isinstance(rows, list):
            result[key] = [r for r in rows if isinstance(r, dict)
                           and _canon(r.get("question_number")) not in skip]
    return result


def add(coursework_id: str, question_number: str, note: str = "") -> dict:
    """Record one exclusion. Idempotent: the same question updates its note."""
    number = str(question_number or "").strip()
    if not coursework_id or not number:
        raise ValueError("An assignment and a question number are required.")
    db.execute(
        "INSERT INTO scheme_ignored_questions(coursework_id, question_number, note, created_at) "
        "VALUES(?,?,?,?) ON CONFLICT(coursework_id, question_number) "
        "DO UPDATE SET note = excluded.note",
        (coursework_id, number, note or "", time.time()))
    return ignored(coursework_id).get(_canon(number), {
        "question_number": number, "note": note or "", "created_at": time.time()})


def remove(coursework_id: str, question_number: str) -> bool:
    """Put one question back into marking. True when a row was removed."""
    number = str(question_number or "").strip()
    if not coursework_id or not number:
        return False
    row = db.query_one(
        "SELECT question_number FROM scheme_ignored_questions "
        "WHERE coursework_id = ? AND question_number = ?", (coursework_id, number))
    if row is None:
        # The UI may send the canonical form the blueprint matched; fall back to
        # a canonical comparison so "Q12" removes a stored "12".
        wanted = _canon(number)
        for stored, item in ignored(coursework_id).items():
            if stored == wanted:
                number = item["question_number"]
                break
        else:
            return False
    db.execute(
        "DELETE FROM scheme_ignored_questions WHERE coursework_id = ? AND question_number = ?",
        (coursework_id, number))
    return True
