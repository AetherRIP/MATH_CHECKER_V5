"""How each question of an assignment's mark scheme is to be handled.

A mark scheme is evidence, not an instruction. Before a paper is marked the
teacher decides, per question and per sub-question, one of three things:

``AUTO``
    Mark it from the extracted scheme, under the normal marking safeguards.
``MANUAL``
    Do not trust it — a human checks this question independently. Chosen by the
    teacher, never imposed: marking is the proposed handling for every question,
    because an independent check is the exception the teacher asks for, not the
    state a scheme starts in.
``SKIP``
    Do not mark it at all.

Two rules drive every decision here:

* The blueprint and the uploaded PDF are never edited. A teacher's corrected
  answer lives in its own column, so "remove the edit" restores the extraction
  exactly, and re-extracting the scheme cannot be blocked by a decision.
* A decision is identified by the scheme's OWN id (canonical, so ``Q1(a)``,
  ``1 a`` and ``1(a)`` agree), never by position. Re-extracting a scheme that
  reorders its questions therefore cannot move a decision onto a different
  question; a decision whose question no longer exists is reported as
  orphaned instead of being reattached to whatever now sits at that index.

SKIP is applied through :mod:`app.engine.scheme_exclusions`, which the
correction worker already honours (the question is not asked for, its rows are
dropped before totals, and it raises no discrepancy). Writing the exclusion
from here means the marking path needs no parallel rule to stay in step.
"""

from __future__ import annotations

import time

from .. import db
from . import scheme_exclusions
from . import scheme_structure as st
from .question_inventory import canonical_question_id

__all__ = [
    "AUTO", "MANUAL", "SKIP", "DECISIONS", "LABELS",
    "answer_for", "decisions_for", "finalize", "history", "manual_keys",
    "mark_manual", "rows_for", "set_answer", "set_decision", "summary",
]

AUTO = "AUTO"
MANUAL = "MANUAL"
SKIP = "SKIP"

DECISIONS = (AUTO, MANUAL, SKIP)

#: Wording a teacher reads. The code travels to the API; the label never does.
LABELS = {
    AUTO: "Mark automatically",
    MANUAL: "Manual independent check",
    SKIP: "Skip question",
}

# Extraction states, worst last. They describe the SCHEME, not the teacher's
# choice: a reliable answer can still be checked by hand.
RELIABLE = "RELIABLE"
CRITERIA_ONLY = "CRITERIA_ONLY"
MISSING = "MISSING"

#: A structure Demo Mode built for exploration. It is NOT a read of the scheme:
#: it is labelled as such and counted as unreliable, so no row built from it can
#: pass for an extraction — including the rows proposed for automatic marking.
DEMO = "DEMO"


def _canon(value) -> str:
    return canonical_question_id(str(value or ""))


def _display_part(parent: str, label: str) -> str:
    """How a sub-question is printed: part "a" of question 1 is "1(a)".

    A part that already carries a full label ("4(b)(ii)") is shown as it is,
    so a scheme that prints the whole id is not given a doubled prefix.
    """
    label = str(label or "").strip()
    if not label:
        return parent
    if st.parse_label(label)[0] is not None:
        return label
    if label.startswith("(") and label.endswith(")"):
        return f"{parent}{label}"
    return f"{parent}({label})"


def _resolve(blueprint: dict | None, number: str) -> dict | None:
    """The question or sub-question row for an id as the teacher wrote it.

    Matching goes through the same flattening the page uses, so ``1(a)``,
    ``1 a`` and a blueprint that stores the part as a bare ``a`` all land on
    the same row. Existence is checked here rather than with
    :func:`scheme_exclusions.find`, which only knows a part's bare label.
    """
    wanted = _canon(number)
    if not wanted:
        return None
    rows, _orphans = rows_for(blueprint, None)
    for row in rows:
        if row["question_key"] == wanted:
            return row
    return None


def _answers(row: dict) -> list[str]:
    out = [str(a).strip() for a in (row.get("answers") or []) if str(a).strip()]
    single = str(row.get("answer") or "").strip()
    if single and single not in out:
        out.insert(0, single)
    return out


def _status(answers: list[str], criteria: str) -> str:
    """Whether the extracted answer can be relied on. Never guessed.

    An entry with marks and neither an answer nor criteria is MISSING: the
    extraction produced nothing a marker could check against.
    """
    if answers:
        return RELIABLE
    return CRITERIA_ONLY if criteria.strip() else MISSING


def decisions_for(coursework_id: str | None) -> dict[str, dict]:
    """Stored decisions for one assignment, keyed by canonical question id."""
    if not coursework_id:
        return {}
    rows = db.query(
        "SELECT question_key, question_label, decision, decided, answer_text, note, "
        "source, updated_by, updated_at FROM scheme_question_decisions "
        "WHERE coursework_id = ?", (coursework_id,))
    out: dict[str, dict] = {}
    for row in rows:
        key = _canon(row["question_key"])
        out[key] = {
            "question_key": key,
            "question_label": row["question_label"] or row["question_key"],
            "decision": row["decision"],
            "decided": bool(row["decided"] if row["decided"] is not None else 1),
            "answer_text": row["answer_text"],
            "note": row["note"] or "",
            "source": row["source"] or "HUMAN",
            "updated_by": row["updated_by"],
            "updated_at": row["updated_at"],
        }
    return out


def rows_for(blueprint: dict | None, coursework_id: str | None) -> tuple[list[dict], list[dict]]:
    """Every question and sub-question, with its state and current decision.

    Returns ``(rows, orphaned)``. A stored decision whose question is no longer
    in the scheme is returned in ``orphaned`` rather than folded into a row: a
    re-extraction that changed the structure must be looked at, not guessed at.
    """
    stored = decisions_for(coursework_id)
    ignored = scheme_exclusions.ignored(coursework_id)
    rows: list[dict] = []
    seen: set[str] = set()

    for question in (blueprint or {}).get("questions") or []:
        if not isinstance(question, dict):
            continue
        number = str(question.get("question_number") or question.get("number") or "").strip()
        if not number:
            continue
        parts = [p for p in (question.get("parts") or []) if isinstance(p, dict)]
        # A question that only groups sub-questions carries no answer of its
        # own: its parts do. Reporting the container as "answer missing" would
        # flag every multi-part question on every paper.
        container_resolved = any(_answers(part) for part in parts)
        rows.append(_row(number, number, question, stored, ignored, depth=0,
                         container_resolved=container_resolved))
        seen.add(_canon(number))

        for part in question.get("parts") or []:
            if not isinstance(part, dict):
                continue
            raw = str(part.get("number") or part.get("question_number") or "").strip()
            if not raw:
                continue
            display = _display_part(number, raw)
            key = _canon(display)
            if not key or key in seen:
                continue
            rows.append(_row(display, key, part, stored, ignored, depth=1,
                             parent=_canon(number)))
            seen.add(key)

    orphans = [item for key, item in stored.items() if key not in seen]
    return rows, orphans


def _row(label: str, key: str, source: dict, stored: dict, ignored: dict,
         depth: int, parent: str | None = None,
         container_resolved: bool = False) -> dict:
    answers = _answers(source)
    criteria = str(source.get("criteria") or "").strip()
    status = DEMO if source.get("demo") else _status(answers, criteria)
    if status not in (RELIABLE, DEMO) and container_resolved:
        # The parts answer for the question; nothing is missing at this level.
        status = RELIABLE
    record = stored.get(key)
    edited = (record or {}).get("answer_text")
    # A stored decision is the teacher's; otherwise the PROPOSED default is
    # shown, clearly marked as not yet decided.
    if record:
        decision = record["decision"]
        # A row created only by an answer edit is NOT a decision.
        decided = record.get("decided", True)
    else:
        # Marking is the job, so every question is proposed for automatic
        # marking. An independent check is opt-in: defaulting to it turned a
        # whole scheme into dozens of manual checks nobody chose, which is not a
        # review — it is a wall, and a wall gets clicked past. What the
        # extraction could not support is still on the page as its status, so a
        # teacher can filter "Untrusted extraction" and pick the rows to check.
        decision = AUTO
        decided = False

    try:
        marks = int(source.get("max_marks") or 0)
    except (TypeError, ValueError):
        marks = 0

    return {
        "question_key": key,
        "question_label": label,
        "depth": depth,
        "parent_key": parent,
        "max_marks": marks,
        "page": source.get("page"),
        "printed_label": source.get("printed_label"),
        "answers": answers,
        "answer_text": edited,
        "effective_answer": edited if edited is not None else (answers[0] if answers else ""),
        "criteria": criteria[:2000],
        "extraction_status": status,
        "reliable": status == RELIABLE,
        "decision": decision,
        "decided": decided,
        "label": LABELS.get(decision, decision),
        "skipped": key in ignored,
        "note": (record or {}).get("note") or "",
        "updated_by": (record or {}).get("updated_by"),
        "updated_at": (record or {}).get("updated_at"),
    }


def summary(rows: list[dict], blueprint: dict | None) -> dict:
    """Counts and marks for the review header.

    Skipped marks are reported separately and removed from ``marked_total``,
    so the number shown is what the paper is actually out of.
    """
    # The blueprint's own total is authoritative: recomputing from question
    # maxima would disagree with the paper wherever a scheme prints a total
    # that is not the plain sum of its question maxima.
    declared = int((blueprint or {}).get("total_marks") or 0)
    total = declared or sum(int(r["max_marks"] or 0) for r in rows if r["depth"] == 0)
    skipped = [r for r in rows if r["skipped"]]
    manual = [r for r in rows if not r["skipped"] and r["decision"] == MANUAL]
    auto = [r for r in rows if not r["skipped"] and r["decision"] == AUTO]
    unresolved = [r for r in rows if not r["decided"]]
    unreliable = [r for r in rows if not r["reliable"] and not r["skipped"]]

    skipped_marks = sum(int(r["max_marks"] or 0) for r in skipped)
    marked = total
    return {
        "questions": sum(1 for r in rows if r["depth"] == 0),
        "subquestions": sum(1 for r in rows if r["depth"] > 0),
        "total": len(rows),
        "automatic": len(auto),
        "manual": len(manual),
        "skipped": len(skipped),
        "unresolved": len(unresolved),
        "unreliable": len(unreliable),
        "total_marks": total,
        "skipped_marks": skipped_marks,
        "marked_marks": max(0, marked - skipped_marks),
        "manual_marks": sum(int(r["max_marks"] or 0) for r in manual),
    }


def _record(coursework_id: str, key: str, label: str, action: str, *,
            previous_decision: str | None, new_decision: str | None,
            previous_answer: str | None, new_answer: str | None,
            reason: str, source: str, actor: str) -> None:
    db.execute(
        "INSERT INTO scheme_decision_history(coursework_id, question_key, "
        "question_label, action, previous_decision, new_decision, previous_answer, "
        "new_answer, reason, source, actor, at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        (coursework_id, key, label, action, previous_decision, new_decision,
         previous_answer, new_answer, reason, source, actor, time.time()))


def set_decision(coursework_id: str, question_number: str, decision: str, *,
                 note: str = "", reason: str = "", actor: str = "teacher",
                 source: str = "HUMAN",
                 blueprint: dict | None = None) -> dict:
    """Record one handling decision, keeping the previous one in history.

    ``SKIP`` also writes the existing exclusion row, so the correction worker
    (which already filters and drops excluded questions) needs no new rule.
    Changing away from ``SKIP`` removes it again.
    """
    value = str(decision or "").strip().upper()
    if value not in DECISIONS:
        raise ValueError(
            f"Unknown decision {decision!r}. Use one of: {', '.join(DECISIONS)}.")
    if not coursework_id:
        raise ValueError("An assignment is required.")

    label = str(question_number or "").strip()
    key = _canon(label)
    if not key:
        raise ValueError("A question is required.")

    if blueprint is not None:
        resolved = _resolve(blueprint, label)
        if resolved is None:
            raise ValueError(f"Question {label} is not in this mark scheme.")
        # Use the scheme's own spelling for the display label, so history reads
        # "1(a)" even when the request sent "1 a".
        label = resolved["question_label"]

    before = decisions_for(coursework_id).get(key)
    previous = before["decision"] if before else None
    previous_answer = before["answer_text"] if before else None
    # An answer the teacher already typed is never dropped by a decision change.
    answer_text = before["answer_text"] if before else None

    if previous == value and before and before["answer_text"] == answer_text:
        # A no-op decision still updates the note, but records no history row:
        # a history full of "changed AUTO to AUTO" is not an audit trail.
        if before["note"] != (note or ""):
            _record(coursework_id, key, label, "NOTE",
                    previous_decision=previous, new_decision=value,
                    previous_answer=previous_answer, new_answer=answer_text,
                    reason=reason, source=source, actor=actor)
        db.execute(
            "UPDATE scheme_question_decisions SET note = ?, updated_by = ?, updated_at = ? "
            "WHERE coursework_id = ? AND question_key = ?",
            (note or "", actor, time.time(), coursework_id, key))
        return decisions_for(coursework_id)[key]

    db.execute(
        "INSERT INTO scheme_question_decisions(coursework_id, question_key, "
        "question_label, decision, decided, answer_text, note, source, updated_by, "
        "updated_at) VALUES(?,?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(coursework_id, question_key) DO UPDATE SET "
        "question_label = excluded.question_label, decision = excluded.decision, "
        "decided = 1, answer_text = excluded.answer_text, note = excluded.note, "
        "source = excluded.source, updated_by = excluded.updated_by, "
        "updated_at = excluded.updated_at",
        (coursework_id, key, label, value, 1, answer_text, note or "", source,
         actor, time.time()))
    _record(coursework_id, key, label, "DECISION",
            previous_decision=previous, new_decision=value,
            previous_answer=previous_answer, new_answer=answer_text,
            reason=reason, source=source, actor=actor)

    # Keep the marking path in step: SKIP is an exclusion, anything else is not.
    if value == SKIP:
        scheme_exclusions.add(coursework_id, label, note or "")
    else:
        scheme_exclusions.remove(coursework_id, label)

    return decisions_for(coursework_id)[key]


def set_answer(coursework_id: str, question_number: str, answer: str, *,
               reason: str = "", actor: str = "teacher",
               blueprint: dict | None = None) -> dict:
    """Replace the expected answer the marker uses for one question.

    Stored beside the blueprint, never in it: the extraction stays intact and
    clearing the edit (``answer=None``) restores it exactly. An empty string is
    a real value — "there is no acceptable answer" is a decision a teacher may
    need to record — so only ``None`` clears.
    """
    if not coursework_id:
        raise ValueError("An assignment is required.")
    label = str(question_number or "").strip()
    key = _canon(label)
    if not key:
        raise ValueError("A question is required.")
    if blueprint is not None:
        resolved = _resolve(blueprint, label)
        if resolved is None:
            raise ValueError(f"Question {label} is not in this mark scheme.")
        label = resolved["question_label"]

    before = decisions_for(coursework_id).get(key)
    previous_answer = before["answer_text"] if before else None
    new_value = None if answer is None else str(answer)
    if previous_answer == new_value:
        return before  # nothing changed, nothing to record

    # An answer edit must not silently create an AUTO decision: a question a
    # teacher just corrected still carries whatever decision it had, and an
    # undecided question stays proposed until it is decided.
    if before:
        db.execute(
            "UPDATE scheme_question_decisions SET answer_text = ?, updated_by = ?, "
            "updated_at = ? WHERE coursework_id = ? AND question_key = ?",
            (new_value, actor, time.time(), coursework_id, key))
    else:
        db.execute(
            "INSERT INTO scheme_question_decisions(coursework_id, question_key, "
            "question_label, decision, decided, answer_text, note, source, updated_by, "
            "updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
            # decided=0: an edited answer is not a handling decision, and the
            # row keeps the same proposed default it had before the edit.
            (coursework_id, key, label, AUTO, 0, new_value, "", "HUMAN", actor,
             time.time()))

    _record(coursework_id, key, (before or {}).get("question_label") or label,
            "ANSWER", previous_decision=(before or {}).get("decision"),
            new_decision=(before or {}).get("decision"),
            previous_answer=previous_answer, new_answer=new_value,
            reason=reason, source="HUMAN", actor=actor)
    return decisions_for(coursework_id)[key]


def history(coursework_id: str, question_number: str | None = None,
            limit: int = 500) -> list[dict]:
    """Every decision and answer change, newest first."""
    if not coursework_id:
        return []
    sql = ("SELECT question_key, question_label, action, previous_decision, "
           "new_decision, previous_answer, new_answer, reason, source, actor, at "
           "FROM scheme_decision_history WHERE coursework_id = ?")
    params: list = [coursework_id]
    if question_number:
        sql += " AND question_key = ?"
        params.append(_canon(question_number))
    sql += " ORDER BY at DESC, id DESC LIMIT ?"
    params.append(max(1, min(int(limit), 2000)))
    out = []
    for row in db.query(sql, tuple(params)):
        out.append({
            "question_key": row["question_key"],
            "question_label": row["question_label"] or row["question_key"],
            "action": row["action"],
            "previous_decision": row["previous_decision"],
            "new_decision": row["new_decision"],
            "previous_answer": row["previous_answer"],
            "new_answer": row["new_answer"],
            "reason": row["reason"] or "",
            "source": row["source"] or "HUMAN",
            "actor": row["actor"] or "",
            "at": row["at"],
        })
    return out


def manual_keys(coursework_id: str | None,
                blueprint: dict | None = None) -> set[str]:
    """Questions that must be checked by a human rather than trusted.

    With a blueprint this is every row the teacher set to MANUAL, plus any
    stored decision whose question the scheme no longer contains. The proposed
    default is automatic, so an untouched row is not flagged: whether an answer
    could be extracted is reported on the row itself, where it is visible
    without pretending the question has been checked. SKIP is never included —
    those questions are not marked at all.
    """
    if blueprint is not None:
        rows, _orphans = rows_for(blueprint, coursework_id)
        return {r["question_key"] for r in rows
                if not r["skipped"] and r["decision"] == MANUAL}
    return {key for key, row in decisions_for(coursework_id).items()
            if row["decision"] == MANUAL}


def mark_manual(result: dict | None, keys: set[str]) -> tuple[dict, int]:
    """Flag the questions a human must check on a finished correction.

    Marked but not trusted: the marks stay (so totals are unchanged), but each
    row says it needs an independent check, and the result is marked for
    review. Silently dropping the marks would invent a zero the teacher never
    asked for; reporting them as confidently verified would be a lie.
    """
    if not result or not keys:
        return result or {}, 0
    wanted = {_canon(k) for k in keys}
    flagged = 0
    for row in result.get("questions") or []:
        if not isinstance(row, dict):
            continue
        key = _canon(row.get("question_number"))
        if key not in wanted and st.root_id(key) not in wanted:
            continue
        row["manual_check"] = True
        row["review_status"] = MANUAL
        flagged += 1
    return result, flagged


def finalize(coursework_id: str, *, actor: str = "teacher", note: str = "",
             acknowledge_unresolved: bool = False,
             blueprint: dict | None = None) -> dict:
    """Sign off the review for one assignment.

    A question nobody decided is NOT approved by silence: finalising with
    undecided questions is refused unless the caller explicitly acknowledges
    them, and the acknowledgement is written to the history so it is visible
    afterwards rather than being a dialog someone dismissed.
    """
    if not coursework_id:
        raise ValueError("An assignment is required.")
    rows, orphans = rows_for(blueprint, coursework_id)
    stats = summary(rows, blueprint)
    if stats["unresolved"] and not acknowledge_unresolved:
        raise ValueError(
            f"{stats['unresolved']} question(s) still have no decision. "
            "Decide them, or acknowledge them to finalise anyway.")

    _record(coursework_id, "*", "Assignment", "FINALIZED",
            previous_decision=None, new_decision=None,
            previous_answer=None, new_answer=None,
            reason=note or (f"{stats['unresolved']} unresolved question(s) "
                            "acknowledged at sign-off"),
            source="HUMAN", actor=actor)
    return {**stats, "orphaned": len(orphans), "finalized_at": time.time(),
            "acknowledged_unresolved": stats["unresolved"]
            if acknowledge_unresolved else 0}


def answer_for(coursework_id: str | None, question_number: str) -> str | None:
    """The teacher's edited answer for one question, or None when unedited."""
    if not coursework_id:
        return None
    record = decisions_for(coursework_id).get(_canon(question_number))
    return (record or {}).get("answer_text")
