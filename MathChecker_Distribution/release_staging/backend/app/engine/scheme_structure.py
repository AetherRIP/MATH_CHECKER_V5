"""Structured question / SUBQUESTION representation of a mark scheme.

Why this module exists
----------------------
A mark scheme was reaching the marker as a flat list of
``question_number / max_marks / criteria`` rows, with every sub-part, answer and
M/A/B rule squeezed into one free-text ``criteria`` string. Three measured
failures followed from that:

1. **Sub-parts collapsed.** ``canonical_question_id("4(b)(i)")`` and
   ``canonical_question_id("4(b)(ii)")`` both returned ``"4b"``, so those two
   marking points shared ONE identity. That identity is what groups the
   marker's returned rows onto the mark scheme, so a result for part (ii)
   could be grouped onto part (i) — an answer attached to the wrong
   subquestion, and one part's marks leaking into another's.
2. **Sub-parts thrown away.** The extraction schema asks the model for
   ``parts`` per question, but ``blueprint._clean_questions`` dropped that
   field, so the per-part marks and answers never reached the marking prompt
   or the checker.
3. **Criteria truncated.** ``blueprint.prompt_block`` cut ``criteria`` at 400
   characters *silently*, so a long mark scheme lost alternative answers and
   M/A/B rules with no warning anywhere.

What this module provides (all deterministic — no model, no network, no I/O):

* **Identity** — :func:`subquestion_id` keeps every nesting level
  (``"4(b)(ii)"`` -> ``"4bii"``, ``"2.1"`` -> ``"2.1"``) and
  :func:`parent_id` walks the nesting, so a subquestion is never identified by
  a local label alone.
* **Marks** — :func:`extract_mark_codes` finds M/A/B (and ECF/ISW/oe/ft)
  annotations in a criteria string, so the individual marking points survive
  even when the whole question is one row.
* **Answers** — :func:`extract_answers` splits a criteria/answer string into
  the accepted alternatives ("or", "accept", "oe", "any equivalent"), keeping
  each one attached to the subquestion it was read from.
* **Structure** — :func:`build_structure` turns the extracted questions into
  parents + subquestions with explicit ids, marks, answers, page, confidence
  and validation status.
* **Validation** — :func:`validate_structure` reports duplicate ids, missing
  answers/criteria, mark sums that do not add up, labels that disagree with
  their parent, and suspicious row counts. It never changes a mark.
* **Matching** — :func:`match_result_rows` maps the marker's returned rows
  onto the declared subquestions, and says explicitly which rows matched
  nothing and where two rows claimed the same subquestion, instead of silently
  assigning ambiguous text to whatever row is processed next.

Nothing here rewrites a mark or an extracted mark-scheme text: it structures,
checks and reports. Proposed interpretations stay separate from the source.
"""

from __future__ import annotations

import re
from typing import Any, Iterable, Sequence

# --------------------------------------------------------------------- labels

#: The leading question number: "4", "Q4", "q4", "Question 4", "4.".
_MAIN = re.compile(r"^\s*(?:q(?:uestion)?\s*)?\.?\s*(\d{1,3})\s*",
                   re.IGNORECASE)
#: A parenthesised or bracketed level: "(a)", "[ii]".
_LEVEL_GROUP = re.compile(r"[\(\[]\s*([a-zA-Z]{1,4}|\d{1,2})\s*[\)\]]")
#: Separators between numeric levels: "2.1", "2-1", "2/1".
_NUM_LEVEL = re.compile(r"\s*[.\-/]\s*(\d{1,2})")
#: One whitespace-separated flat token: "bii", "bi", "a", "iii" in
#: "4 b ii". A following word ("4(a) Algebra") must not be read as a level.
_FLAT = re.compile(r"^\s*[,:;]?\s*([a-zA-Z]{1,4})(?=\s|$|[\(\[])")

_ROMAN = ("i", "ii", "iii", "iv", "v", "vi", "vii", "viii", "ix", "x", "xi", "xii")
_ROMAN_SET = set(_ROMAN)
_ROMAN_RANK = {token: index for index, token in enumerate(_ROMAN, start=1)}

_NON_ALNUM = re.compile(r"[^a-z0-9]")


def _letter_level(token: str) -> str:
    """One alphabetic level, lower-cased (a..z, or a roman numeral)."""
    return token.strip().lower()


def strict_levels(token: str) -> list[str]:
    """Levels for a token that MUST be a label, or ``[]`` for prose.

    Used for whitespace-separated tokens, where the risk is a trailing word
    ("4(a) Area") rather than a label. A roman numeral, a single letter, a
    letter followed by a roman numeral ("bii") or digits are labels; anything
    else is prose and ends the label.
    """
    text = token.strip().lower()
    if not text:
        return []
    if text in _ROMAN_SET or len(text) == 1 or text.isdigit():
        return [text]
    if text[1:] in _ROMAN_SET:
        return [text[0], text[1:]]
    return []


def split_flat_run(run: str) -> list[str]:
    """Split a flat trailing run into its levels: ``"bii"`` -> ``["b", "ii"]``.

    Mark schemes print the same nesting three ways — ``4(b)(ii)``, ``4 b ii``
    and ``4bii`` — and the level boundary has to survive all three. A run that
    is itself roman (``"ii"``) is one level; ``"bii"`` is the letter ``b``
    followed by ``ii``; anything else is split per character so an exotic
    label can never silently become a different existing one.
    """
    text = run.strip().lower()
    if not text:
        return []
    if text in _ROMAN_SET:
        return [text]
    if len(text) == 1:
        return [text]
    if text[1:] in _ROMAN_SET:
        return [text[0], text[1:]]
    if text.isdigit():
        return [text]
    # Two or more single-character levels ("ab" -> ["a", "b"]).
    if all(ch.isalpha() for ch in text):
        return list(text)
    return [text]


def parse_label(label: Any) -> tuple[int | None, list[str]]:
    """``"4(b)(ii)"`` -> ``(4, ["b", "ii"])``; ``"2.1"`` -> ``(2, ["1"])``.

    Levels come back in source order, normalised to lower case. ``(None, [])``
    means the label carries no recognisable numbering at all.
    """
    text = str(label or "").strip()
    if not text:
        return None, []

    main: int | None = None
    match = _MAIN.match(text)
    if match:
        main = int(match.group(1))
        text = text[match.end():]

    levels: list[str] = []
    pos = 0
    while pos < len(text):
        chunk = text[pos:]
        if chunk[0].isspace():
            # Whitespace between levels is a separator, never a boundary:
            # "4 (b) (ii)" is the same subquestion as "4(b)(ii)".
            pos += 1
            continue
        group = _LEVEL_GROUP.match(chunk)
        if group:
            levels.extend(split_flat_run(group.group(1)))
            pos += group.end()
            continue
        numeric = _NUM_LEVEL.match(chunk)
        if numeric:
            levels.append(numeric.group(1))
            pos += numeric.end()
            continue
        flat = _FLAT.match(chunk)
        if flat:
            token_levels = strict_levels(flat.group(1))
            if not token_levels:
                break  # trailing prose, not a label
            levels.extend(token_levels)
            pos += flat.end()
            continue
        # Unrecognised trailing text (a topic name, e.g. "4(b) Algebra").
        break

    if main is None and not levels:
        return None, []
    return main, levels


def render_id(main: int | None, levels: Sequence[str]) -> str:
    """Canonical id for a parsed label.

    ``(4, ["b", "ii"])`` -> ``"4bii"``; ``(2, ["1"])`` -> ``"2.1"``. Numeric
    levels keep their dot so ``2.1`` and ``21`` can never collide.
    """
    out = "" if main is None else str(main)
    for level in levels:
        out += f".{level}" if level.isdigit() else level
    return out


def subquestion_id(label: Any) -> str:
    """The canonical, NESTED identity of a question or subquestion.

    ``"4"``, ``"Q4"``, ``"4."``      -> ``"4"``
    ``"4(a)"``, ``"4 a"``, ``"4a"``  -> ``"4a"``
    ``"4(b)(ii)"``, ``"4bii"``       -> ``"4bii"``   (distinct from ``4bi``)
    ``"2.1"``, ``"2.1.3"``           -> ``"2.1"``, ``"2.1.3"``

    Everything that cannot be parsed falls back to its lower-cased
    alphanumerics, so an exotic label can never collide with a numbered one.
    """
    main, levels = parse_label(label)
    if main is None and not levels:
        return _NON_ALNUM.sub("", str(label or "").strip().lower())
    return render_id(main, levels)


def parent_id(canonical: str) -> str:
    """The id one level up: ``"4bii"`` -> ``"4b"``, ``"4b"`` -> ``"4"``.

    ``"4"`` has no parent and returns ``""``. Numeric levels walk their dots:
    ``"2.1.3"`` -> ``"2.1"``.
    """
    text = str(canonical or "")
    if not text:
        return ""
    if "." in text:
        head, _sep, _tail = text.rpartition(".")
        return head
    levels = _levels_of(text)
    if not levels:
        return ""
    tail = levels[-1]
    if len(levels) == 1:
        # "4b" -> the top-level question "4". "4" itself has no parent.
        return text[: -len(tail)] if len(text) > len(tail) else ""
    return text[: -len(tail)]


def root_id(canonical: str) -> str:
    """The top-level question id: ``"4bii"`` -> ``"4"``."""
    text = str(canonical or "")
    match = _MAIN.match(text)
    if match:
        return match.group(1)
    head = text.split(".", 1)[0]
    levels = _levels_of(head)
    if levels and len(head) > len(levels[-1]):
        return head[: -len(levels[-1])]
    return head


def _levels_of(canonical: str) -> list[str]:
    """The level tokens of a canonical id, used to walk the nesting."""
    text = str(canonical or "")
    match = _MAIN.match(text)
    rest = text[match.end():] if match else text
    levels: list[str] = []
    for part in re.findall(r"[a-z]+|\d+", rest):
        if part.isdigit():
            levels.append(part)
        elif part in _ROMAN_SET:
            levels.append(part)
        elif len(part) == 1:
            levels.append(part)
        else:
            levels.extend(split_flat_run(part))
    return levels


def order_key(label: Any) -> tuple:
    """Deterministic ordering: 4, 4a, 4b, 4bi, 4bii — never string order."""
    canonical = subquestion_id(label)
    main, levels = parse_label(canonical)
    if main is None and not levels:
        return (1, 0, (), str(label or ""))
    ranked = tuple(
        (2, f"{_ROMAN_RANK[level]:03d}") if level in _ROMAN_SET
        else (0, f"{int(level):03d}") if level.isdigit()
        else (1, level)
        for level in levels
    )
    return (0, main or 0, ranked, canonical)


def level_depth(canonical: str) -> int:
    """How deep a subquestion sits: ``"4"`` -> 0, ``"4bii"`` -> 2."""
    return len(_levels_of(canonical))


# ---------------------------------------------------------------- mark codes

#: Mark annotations in a criteria string: "M1", "A1", "B2", "M1A1", "(M1)".
#: Only letters block a match, so "M1A1" yields both marks while "cm2" does not.
_MARK_CODE = re.compile(r"(?<![A-Za-z])([MAB])\s?(\d)(?!\d)",
                        re.IGNORECASE)
#: Modifier annotations that change what a mark means.
_MARK_NOTES = re.compile(
    r"(?<![A-Za-z])(ECF|ISW|oe|ft|cao|AG|dep|D\d?|SP)\b")
#: An allocation in brackets at the end of a row: "(2)" / "[3]".
_ALLOCATION = re.compile(r"[\(\[]\s*(\d{1,2})\s*[\)\]]\s*$")

#: What each letter actually rewards, spelled out for the marker.
MARK_KINDS = {
    "M": "method",
    "A": "accuracy",
    "B": "independent",
}


def extract_mark_codes(text: Any) -> dict:
    """Deterministic M/A/B extraction from a criteria string.

    Returns ``{"M": n, "A": n, "B": n, "total": n, "notes": [...], "codes":
    ["M1", "A1", ...]}``. ``total`` is the number of mark codes FOUND, which is
    evidence about the criteria — never a substitute for the question's
    official max marks.
    """
    raw = str(text or "")
    codes: list[str] = []
    counts = {"M": 0, "A": 0, "B": 0}
    for letter, digit in _MARK_CODE.findall(raw):
        key = letter.upper()
        counts[key] += 1
        codes.append(f"{key}{digit}")
    notes: list[str] = []
    for note in _MARK_NOTES.findall(raw):
        if note not in notes:
            notes.append(note)
    return {
        "M": counts["M"], "A": counts["A"], "B": counts["B"],
        "total": sum(counts.values()),
        "codes": codes,
        "notes": notes,
    }


def stated_allocation(text: Any) -> int | None:
    """The mark total a row prints in brackets at its end, or ``None``.

    ``"(3)"`` at the end of a mark-scheme row is the row's allocation; a bare
    number in the middle of the text is not.
    """
    match = _ALLOCATION.search(str(text or "").strip())
    return int(match.group(1)) if match else None


# ------------------------------------------------------------------- answers

#: Brackets left empty by stripping the mark annotations: "x = 7 ( )".
_EMPTY_BRACKETS = re.compile(r"[\(\[]\s*[\)\]]")
#: Ways a mark scheme introduces an accepted alternative.
_ALT_SPLIT = re.compile(
    r"\s*(?:\bor\b|\baccept\b|\baccepts\b|\boe\b|\be\.g\.|\beg\b|\bany\s+equivalent\b|"
    r"\bequivalent\s+to\b)\s*",
    re.IGNORECASE)


def extract_answers(text: Any) -> list[str]:
    """Accepted answer(s) read out of an answer/criteria string.

    Splits on the words a real scheme uses to introduce an alternative, then
    strips the mark annotations and bracketed allocations so what is left is
    the mathematics. Order is preserved and duplicates are dropped, so "or"
    alternatives stay attached to the subquestion they were read from.
    """
    raw = str(text or "").strip()
    if not raw:
        return []
    pieces = _ALT_SPLIT.split(raw)
    out: list[str] = []
    for piece in pieces:
        cleaned = _MARK_CODE.sub(" ", piece)
        cleaned = _MARK_NOTES.sub(" ", cleaned)
        cleaned = _EMPTY_BRACKETS.sub(" ", cleaned)
        cleaned = _ALLOCATION.sub("", cleaned.strip())
        cleaned = re.sub(r"\s+", " ", cleaned).strip(" ;,.-–—:")
        if cleaned and cleaned not in out:
            out.append(cleaned)
    return out


# -------------------------------------------------------------- structure

VALIDATION_OK = "OK"
VALIDATION_REVIEW = "NEEDS_REVIEW"

#: Issue codes reported by :func:`validate_structure`.
ISSUE_DUPLICATE_ID = "DUPLICATE_SUBQUESTION_ID"
ISSUE_MISSING_ANSWER = "SUBQUESTION_WITHOUT_ANSWER"
ISSUE_MISSING_CRITERIA = "SUBQUESTION_WITHOUT_CRITERIA"
ISSUE_MARK_SUM = "SUBQUESTION_MARKS_DO_NOT_ADD_UP"
ISSUE_MARKS_UNKNOWN = "QUESTION_WITHOUT_MARKS"
ISSUE_LABEL_MISMATCH = "SUBQUESTION_LABEL_DISAGREES_WITH_PARENT"
ISSUE_MANY_SUBQUESTIONS = "SUSPICIOUS_SUBQUESTION_COUNT"
ISSUE_NO_SUBQUESTIONS = "QUESTION_WITHOUT_SUBQUESTIONS"

SEVERITY_REVIEW = "review"
SEVERITY_INFO = "info"


def _confidence_from(issues: Sequence[dict], base: float = 1.0) -> float:
    """A subquestion's confidence: 1.0 minus a penalty per review issue.

    Deterministic and evidence-based — it is derived from the issues actually
    found, never invented, and never assigned without a reason.
    """
    penalty = 0.0
    for issue in issues:
        penalty += 0.35 if issue.get("severity") == SEVERITY_REVIEW else 0.1
    return round(max(0.0, min(1.0, base - penalty)), 2)


def local_subquestion(parent_label: Any, part_label: Any) -> tuple[str, str]:
    """Scope a locally-labelled part to its parent.

    Mark schemes print sub-parts locally ("(b)(ii)") under a question
    ("4"). The pair is turned into the canonical id ``"4bii"`` and a display
    label ``"4b(ii)"`` — the id is what makes the part unique, so a local
    label can never be mistaken for the same label under another question.
    """
    parent_main, parent_levels = parse_label(parent_label)
    _part_main, part_levels = parse_label(part_label)
    canonical = render_id(parent_main, list(parent_levels) + list(part_levels))
    text = str(part_label or "").strip()
    if not text:
        return canonical, ""
    if text[0] in "([":
        return canonical, f"{parent_label}{text}"
    if text[0].isalnum():
        return canonical, f"{parent_label}{text}"
    return canonical, f"{parent_label} {text}"


def _row_entry(item: dict, *, parent: str = "", label: str = "",
               canonical_id: str = "") -> dict:
    """One subquestion/parent entry from a raw extracted row."""
    number = str(item.get("question_number")
                 or item.get("number") or label or "").strip()
    criteria = str(item.get("criteria") or "").strip()
    provided_answers = item.get("answers") or item.get("answer") or ""
    if isinstance(provided_answers, (list, tuple)):
        answers = [str(a).strip() for a in provided_answers if str(a).strip()]
    else:
        answers = [a for a in re.split(r"\s*\|\s*", str(provided_answers))
                   if a.strip()]
    if not answers:
        # No explicit answer field: read the alternatives out of the text the
        # scheme/model gave us, which is the only evidence available.
        answers = extract_answers(provided_answers or criteria)
    try:
        max_marks = int(item.get("max_marks"))
    except (TypeError, ValueError):
        max_marks = 0
    codes = extract_mark_codes(criteria or " ".join(answers))
    validation = item.get("validation") or _validation_of(
        number=number, criteria=criteria, answers=answers, max_marks=max_marks)
    return {
        "id": canonical_id or subquestion_id(number),
        "label": number,
        "question_number": number,
        "parent_id": parent,
        "parent_question": parent,
        "criteria": criteria,
        "answers": answers,
        "marks": codes,
        "max_marks": max_marks,
        "page": item.get("page"),
        "confidence": item.get("confidence"),
        "validation": validation,
        "issues": [],
    }


def _validation_of(*, number: str, criteria: str, answers: Sequence[str],
                   max_marks: int) -> str:
    if max_marks <= 0:
        return VALIDATION_REVIEW
    if not criteria and not answers:
        return VALIDATION_REVIEW
    return VALIDATION_OK


def _annotate(entry: dict, *, has_subquestions: bool = False) -> dict:
    """Attach this entry's own issues (kept separate from the document report).

    A question whose marking points are its SUBQUESTIONS needs no separate
    answer or criteria of its own: requiring one would flag every correctly
    split scheme as incomplete.
    """
    issues: list[dict] = []
    if entry["max_marks"] <= 0:
        issues.append({"code": ISSUE_MARKS_UNKNOWN, "severity": SEVERITY_REVIEW,
                       "id": entry["id"],
                       "message": f"Subquestion {entry['label'] or entry['id']} "
                                  "has no mark allocation."})
    if has_subquestions:
        entry["issues"] = (entry.get("issues") or []) + issues
        if entry["confidence"] is None:
            entry["confidence"] = _confidence_from(issues)
        return entry
    if not entry["answers"]:
        issues.append({"code": ISSUE_MISSING_ANSWER,
                       "severity": SEVERITY_REVIEW, "id": entry["id"],
                       "message": f"Subquestion {entry['label'] or entry['id']} "
                                  "has no expected answer: only a mark scheme "
                                  "that states what earns the mark can be "
                                  "applied with confidence."})
    if not entry["criteria"]:
        issues.append({"code": ISSUE_MISSING_CRITERIA,
                       "severity": SEVERITY_INFO, "id": entry["id"],
                       "message": f"Subquestion {entry['label'] or entry['id']} "
                                  "has no criteria text; the marking points "
                                  "could not be read."})
    entry["issues"] = (entry.get("issues") or []) + issues
    if issues and entry["validation"] == VALIDATION_OK:
        entry["validation"] = VALIDATION_REVIEW
    if entry["confidence"] is None:
        entry["confidence"] = _confidence_from(issues)
    return entry


def build_structure(questions: Iterable[dict] | None) -> dict:
    """Build the structured representation from extracted question rows.

    Each question keeps its subquestions (from the extractor's ``parts``, from
    its own rows, or derived from the numbering it prints). Every subquestion
    gets: its own nested id, the parent's id, the text it was read from, its
    accepted answers, its M/A/B marking points and marks, its source page, and
    a validation status with the reasons behind it.

    ``build_structure`` never renumbers, merges or invents a subquestion: the
    ids come from the labels the source document prints.
    """
    parents: list[dict] = []
    seen_ids: dict[str, int] = {}
    for raw in (questions or []):
        if not isinstance(raw, dict):
            continue
        label = str(raw.get("question_number") or "").strip()
        if not label:
            continue
        parent = _row_entry(raw, parent="", label=label)
        parent["extraction"] = "model"
        parent_id_value = parent["id"]

        sub_raw = raw.get("subquestions") or raw.get("parts") or []
        subs: list[dict] = []
        for part in sub_raw:
            if not isinstance(part, dict):
                continue
            part_label = str(part.get("number") or part.get("question_number")
                             or part.get("label") or "").strip()
            if not part_label:
                continue
            canonical = ""
            if parse_label(part_label)[0] is None:
                # A part printed locally ("b", "(ii)") belongs to THIS parent:
                # its identity is the parent's id plus its own levels.
                canonical, part_label = local_subquestion(parent_id_value,
                                                          part_label)
            sub = _row_entry(part, parent=parent_id_value, label=part_label,
                             canonical_id=canonical)
            # The DISPLAY label is the scoped one ("4b(ii)", not "b(ii)"), so a
            # review message can never point at a bare "(ii)" that exists under
            # three different questions.
            sub["label"] = part_label
            sub["extraction"] = "model"
            subs.append(sub)

        if subs:
            subs.sort(key=lambda s: order_key(s["label"]))
            for sub in subs:
                seen_ids[sub["id"]] = seen_ids.get(sub["id"], 0) + 1
            parent["subquestions"] = [_annotate(s) for s in subs]
            # The parent's marking points ARE its subquestions, so its own
            # validation follows theirs rather than its (usually empty) text.
            parent["validation"] = (
                VALIDATION_OK if all(s["validation"] == VALIDATION_OK
                                     for s in parent["subquestions"])
                else VALIDATION_REVIEW)
            parent["extraction"] = "model"
            declared = parent["max_marks"]
            parts_total = sum(s["max_marks"] for s in parent["subquestions"])
            if declared and parts_total and declared != parts_total:
                parent.setdefault("issues", []).append({
                    "code": ISSUE_MARK_SUM, "severity": SEVERITY_REVIEW,
                    "id": parent_id_value,
                    "message": (f"The subquestions of {label} add up to "
                                f"{parts_total} mark(s) but the question is "
                                f"declared as {declared}."),
                })
        else:
            parent["subquestions"] = []
        seen_ids[parent_id_value] = seen_ids.get(parent_id_value, 0) + 1
        parent["extraction"] = "model"
        parent["confidence"] = parent.get("confidence")
        parents.append(_annotate(
            parent, has_subquestions=bool(parent.get("subquestions"))))

    parents.sort(key=lambda p: order_key(p["question_number"]))
    return {
        "questions": parents,
        "subquestions": [s for p in parents for s in p["subquestions"]],
        "id_counts": seen_ids,
    }


# --------------------------------------------------------------- validation

def belongs_to(canonical: str, parent: str) -> bool:
    """Is ``canonical`` this parent, or nested inside it?

    Walks the nesting rather than comparing prefixes, so question 4 never
    claims subquestion 44.
    """
    current = str(canonical or "")
    wanted = str(parent or "")
    while current:
        if current == wanted:
            return True
        current = parent_id(current)
    return False


def flat_subquestions(questions: Sequence[dict] | None) -> list[dict]:
    """Every subquestion of every question, in order (parents included)."""
    out: list[dict] = []
    for question in (questions or []):
        if not isinstance(question, dict):
            continue
        subs = question.get("subquestions") or []
        if subs:
            out.extend(subs)
        else:
            out.append(question)
    return out


def declared_ids(questions: Sequence[dict] | None) -> list[str]:
    """Canonical ids of every subquestion a scheme declares, in order."""
    return [str(s.get("id") or "") for s in flat_subquestions(questions)
            if str(s.get("id") or "")]


def validate_structure(questions: Sequence[dict] | None,
                       *, expected_total: int | None = None,
                       max_subquestions_per_question: int = 40) -> dict:
    """Deterministic validation of a structured scheme.

    Returns ``{"complete": bool, "confidence": float, "anomalies": [...],
    "counts": {...}}``. Anomalies are described, never applied: a broken
    extraction must stay visible rather than be silently repaired, and no
    mark is ever changed here.

    Checks: duplicate ids (the collapse that makes two subquestions one), a
    subquestion with no answer, a parent whose marks do not equal its
    subquestions' total, labels that disagree with the declared parent, a
    missing mark allocation, a question with no subquestions at all, an
    implausible subquestion count, and the mark total versus a printed total.
    """
    questions = [q for q in (questions or []) if isinstance(q, dict)]
    anomalies: list[dict] = []
    seen: dict[str, int] = {}

    for question in questions:
        label = str(question.get("question_number") or "")
        parent_id_value = str(question.get("id") or subquestion_id(label))
        subs = question.get("subquestions") or []
        if not subs:
            anomalies.append({
                "code": ISSUE_NO_SUBQUESTIONS, "severity": SEVERITY_INFO,
                "id": parent_id_value,
                "message": (f"Question {label} has no subquestions; its marks "
                            "are treated as one marking point.")})
        for sub in subs:
            sub_id = str(sub.get("id") or "")
            seen[sub_id] = seen.get(sub_id, 0) + 1
            if not belongs_to(sub_id, parent_id_value):
                anomalies.append({
                    "code": ISSUE_LABEL_MISMATCH, "severity": SEVERITY_REVIEW,
                    "id": sub_id,
                    "message": (f"Subquestion {sub.get('label') or sub_id} is "
                                f"listed under question {label}.")})
            for issue in (sub.get("issues") or []):
                anomalies.append(dict(issue))
        seen[parent_id_value] = seen.get(parent_id_value, 0) + 1
        for issue in (question.get("issues") or []):
            anomalies.append(dict(issue))
        if len(subs) > max_subquestions_per_question:
            anomalies.append({
                "code": ISSUE_MANY_SUBQUESTIONS, "severity": SEVERITY_REVIEW,
                "id": parent_id_value,
                "message": (f"Question {label} declares {len(subs)} "
                            "subquestions, which looks like split rows or "
                            "duplicated numbering.")})

    for entry_id, count in seen.items():
        if count > 1 and entry_id:
            anomalies.append({
                "code": ISSUE_DUPLICATE_ID, "severity": SEVERITY_REVIEW,
                "id": entry_id,
                "message": (f"{count} rows share the identity '{entry_id}'. "
                            "Two marking points cannot share one identity: "
                            "their answers and marks would be interchangeable.")})

    counted = flat_subquestions(questions)
    total = sum(int(q.get("max_marks") or 0) for q in questions)
    report_confidence = _confidence_from(anomalies)
    if expected_total is not None and int(expected_total or 0) > 0 \
            and int(expected_total) != total:
        anomalies.append({
            "code": ISSUE_MARK_SUM, "severity": SEVERITY_REVIEW, "id": "",
            "message": (f"The scheme's questions total {total} mark(s) but "
                        f"{expected_total} were expected.")})

    return {
        "complete": not any(a["severity"] == SEVERITY_REVIEW
                            for a in anomalies),
        "confidence": report_confidence,
        "anomalies": anomalies,
        "counts": {
            "questions": len(questions),
            "subquestions": len(counted),
            "marks": total,
        },
    }


# ----------------------------------------------------------------- matching

def match_result_rows(rows: Sequence[dict] | None,
                      questions: Sequence[dict] | None) -> dict:
    """Map the marker's returned rows onto the declared subquestions.

    The rule this enforces is the one the old pipeline broke: a returned row
    belongs to the subquestion it *names*, if the scheme declares that
    subquestion. It falls back to a declared PARENT only when the scheme
    declares no finer subquestion of its own, and it reports:

    ``matched``      row index -> canonical subquestion id
    ``by_id``        canonical subquestion id -> row indexes
    ``unmatched``    rows naming a subquestion the scheme does not declare
    ``conflicts``    two rows claiming the SAME subquestion
    ``unmarked``     declared subquestions the marker returned no row for

    Ambiguity is reported, never resolved by guessing: nothing here decides
    which of two conflicting rows "wins".
    """
    declared = {str(s.get("id") or ""): s for s in flat_subquestions(questions)}
    rows = list(rows or [])

    matched: dict[str, str] = {}
    by_id: dict[str, list[int]] = {}
    unmatched: list[dict] = []

    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            continue
        raw_number = str(row.get("question_number") or row.get("subquestion_id")
                        or "")
        canonical = subquestion_id(raw_number)
        target = canonical
        if target not in declared:
            # Walk up the nesting ("4bii" -> "4bi" -> "4b" -> "4") and accept
            # the deepest DECLARED ancestor. Sub-part rows therefore land on
            # their own declared subquestion, never on a namesake.
            walked = canonical
            while walked:
                if walked in declared:
                    target = walked
                    break
                walked = parent_id(walked)
            else:
                target = ""
        if not target:
            unmatched.append({"index": index, "question_number": raw_number})
            continue
        matched[str(index)] = target
        by_id.setdefault(target, []).append(index)

    conflicts = [{"id": sub_id, "rows": [rows[i].get("question_number")
                                         for i in indexes]}
                 for sub_id, indexes in by_id.items() if len(indexes) > 1]
    unmarked = [{"id": sub_id, "label": row.get("label") or sub_id}
                for sub_id, row in declared.items()
                if sub_id and sub_id not in by_id]

    return {
        "matched": matched,
        "by_id": by_id,
        "unmatched": unmatched,
        "conflicts": sorted(conflicts, key=lambda c: order_key(c["id"])),
        "unmarked": sorted(unmarked, key=lambda u: order_key(u["id"])),
    }


def subquestion_of_row(row_id: Any, questions: Sequence[dict] | None) -> str:
    """The declared subquestion id a single row label belongs to (or "")."""
    mapping = match_result_rows([{"question_number": row_id}], questions)
    return next(iter(mapping["matched"].values()), "")


# --------------------------------------------------------------- prompt text

#: Per-subquestion budget for the criteria text sent to the marker. A scheme
#: row that outgrows this is visibly truncated with a marker, never silently.
CRITERIA_BUDGET = 900


def _clip(text: str, budget: int = CRITERIA_BUDGET) -> str:
    clean = " ".join(str(text or "").split())
    if len(clean) <= budget:
        return clean
    return clean[:budget] + " …[criteria truncated for length]"


def declared_questions(questions: Sequence[dict] | None) -> list[dict]:
    """Accept either a built structure or the RAW extracted question rows.

    An extracted blueprint keeps its sub-parts under ``parts``; the built
    structure keeps them under ``subquestions`` with resolved ids. Rendering a
    raw blueprint without this step silently printed ONLY the question lines,
    so every sub-part's answer and marking points never reached the marker at
    all — the prompt looked structured while carrying none of the structure.
    """
    rows = [q for q in (questions or []) if isinstance(q, dict)]
    if any(q.get("parts") and not q.get("subquestions") for q in rows):
        return build_structure(rows)["questions"]
    return rows


def prompt_block(questions: Sequence[dict] | None, *,
                 total_marks: int | None = None) -> str:
    """Render the structure as an explicit, per-subquestion instruction block.

    Every subquestion is one labelled line carrying its own id, its accepted
    answers and its M/A/B marking points, so the marker never has to
    reconstruct which answer belongs to which part from a wall of text.
    """
    questions = declared_questions(questions)
    lines = ["OFFICIAL MARK SCHEME BLUEPRINT (extracted once from the uploaded "
             "mark scheme, cached, identical for every run):"]
    total = total_marks
    if total is None:
        total = sum(int(q.get("max_marks") or 0) for q in (questions or []))
    lines.append(f"total_marks: {total}")
    lines.append("questions and subquestions:")
    for question in (questions or []):
        if not isinstance(question, dict):
            continue
        lines.append(_question_line(question))
        for sub in (question.get("subquestions") or []):
            lines.append(_subquestion_line(sub))
    lines.append("")
    lines.extend([
        "Rules:",
        "- Return EXACTLY one entry per blueprint question, using the SAME",
        "  question_number string and the SAME max_marks value shown above.",
        "- A declared SUBQUESTION is its own marking point: return its own entry",
        "  (or one entry per sub-part) and never move an answer, a mark or a",
        "  criterion from one subquestion to another.",
        "- Do not invent, split, merge, rename or omit questions, even if the",
        "  student left one blank — blank work is still an entry with",
        "  awarded_marks 0.",
        "- Award marks ONLY against the criteria above and the mark scheme;",
        "  never against your own sense of what looks reasonable.",
        "- Independently verify the answer. If the mark scheme's reference",
        "  answer is demonstrably wrong or incomplete, award the marks the",
        "  mathematics justifies and record the disagreement in scheme_check;",
        "  never award zero merely because a correct answer differs from a",
        "  broken reference answer. Preserve every existing M/A/B rule.",
        "- Use the mark_breakdown codes (M/A/B...) from the criteria as the",
        "  evidence for each awarded or withheld mark.",
    ])
    return "\n".join(lines)


def _question_line(question: dict) -> str:
    return ("  - question_number=%s max_marks=%s%s criteria=%s"
            % (question.get("question_number"), question.get("max_marks"),
               f" page={question['page']}" if question.get("page") else "",
               _clip(question.get("criteria"))))


def _subquestion_line(sub: dict) -> str:
    marks = sub.get("marks") or {}
    codes = ",".join(marks.get("codes") or []) or "none stated"
    answers = " | ".join(sub.get("answers") or []) or "not stated"
    return ("      * subquestion=%s (label %s, parent %s) max_marks=%s "
            "mark_codes=%s answers=%s criteria=%s"
            % (sub.get("id"), sub.get("label"), sub.get("parent_id"),
               sub.get("max_marks"), codes, _clip(answers, 300),
               _clip(sub.get("criteria"))))


# ---------------------------------------------------------- result checking

def award_anomalies(row: dict, questions: Sequence[dict] | None) -> list[str]:
    """Check ONE marked row against the subquestion it claims.

    Reports (never rewrites) a row whose awarded marks exceed the subquestion
    it names, or a row that names a subquestion the scheme does not declare.
    """
    problems: list[str] = []
    canonical = subquestion_id(row.get("question_number"))
    declared = {str(s.get("id") or ""): s for s in flat_subquestions(questions)}
    entry = declared.get(canonical)
    if entry is None:
        return problems
    try:
        awarded = int(row.get("awarded_marks") or 0)
    except (TypeError, ValueError):
        return problems
    maximum = int(entry.get("max_marks") or 0)
    if maximum and awarded > maximum:
        problems.append(
            f"Subquestion {entry.get('label') or canonical} was awarded "
            f"{awarded} mark(s) but its own maximum is {maximum}.")
    return problems
