"""CANONICAL QUESTION INVENTORY — the expected-question list every checker
compares its returned results against.

Two production bugs motivated this module:

1. Questions the official mark scheme contains came back from the marker with
   "no answer" and silently looked like 0s (questions 62-69 class of failure).
2. A single long answer spanning several pages was partially assessed, and the
   un-assessed remainder quietly became a zero.

The guard-rail rules encoded here (and enforced by the pipelines that call us):

* An EXPECTED question the marker did not return is MISSING — never a score.
* A MISSING result must end in an explicit, valid state (NEEDS_REVIEW) with a
  useful reason, and finalization is blocked until it is resolved.
* A confirmed BLANK answer (the marker inspected the question and genuinely
  earned nothing) is a legitimate 0 — that state is CONFIRMED_ZERO.
* Question ids are normalised so equivalent formats ("62(a)", "Q62(a)",
  "62 a") match one canonical question and never create duplicates.

Everything here is a pure function of strings and dicts; no AI, no network,
no I/O. Nothing in this module marks, re-mark, or changes a mark: it decides
what is MISSING, what is BLANK, and what a retry may ask for.
"""

from __future__ import annotations

import re

# The safety cap on a single targeted-recovery round. A healthy blueprint
# should never miss this many questions in one pass; when the inventory
# reports more, the problem is almost certainly the BLUEPRINT (a bad
# extraction), not the marker, and no number of retries can fix it. The
# affected questions stay unresolved (NEEDS_REVIEW / teacher review) instead
# of burning unbounded API quota.
MAX_MISSING_QUESTIONS = 12

_TOKEN = re.compile(
    r"^\s*(?:[Qq](?:uestion)?\s*)?\.?\s*(\d{1,3})\s*"
    r"(?:[\.\)\:\-]?\s*\(?\s*([a-zA-Z]{1,2})\s*\)?)?"
)
_NON_ALNUM = re.compile(r"[^a-z0-9]")


def canonical_question_id(text: str) -> str:
    """Normalise an id to ONE canonical form so equivalent labels match.

    "62(a)" / "Q62(a)" / "62 a" / " 62 (a)"  -> "62a"
    "14" / "Q14" / " q14. "                  -> "14"
    Basis: main question number + (optionally) the first letter group.
    Anything that does not parse returns the low-cased alphanumerics, so an
    exotic id can never collide with an unrelated question number.
    """
    raw = str(text or "").strip()
    if not raw:
        return ""
    match = _TOKEN.match(raw)
    if match:
        number, part = match.group(1), match.group(2)
        return str(int(number)) + (part.lower() if part else "")
    return _NON_ALNUM.sub("", raw.lower())


def expected_inventory(blueprint: dict | None) -> dict[str, dict]:
    """The official question inventory keyed canonically.

    Returns {canonical_id: question_row} for every question the mark scheme
    declares, including alias metadata so reporting can show the original
    numbering. Blank / invalid rows are skipped, mirroring the strict rules
    ``blueprint.validate`` already enforces on the structure itself.

    A subpart row collapses into its parent row ("62(a)" -> "62") when the
    mark scheme ALSO declares the parent: the pipeline's reconciliation
    groups sub-part results onto that parent row, so the inventory must not
    manufacture a second required question for the same work. Subparts stay
    separate when no parent row exists (a part-only scheme).
    """
    if not isinstance(blueprint, dict):
        return {}
    by_canonical: dict[str, dict] = {}
    for row in blueprint.get("questions") or []:
        if not isinstance(row, dict):
            continue
        number = str(row.get("question_number", "")).strip()
        if not number:
            continue
        try:
            max_marks = int(row.get("max_marks") or 0)
        except (TypeError, ValueError):
            continue
        if max_marks <= 0:
            continue
        canonical = canonical_question_id(number)
        if not canonical:
            continue
        by_canonical.setdefault(canonical, {
            "question_number": number,
            "max_marks": max_marks,
            "criteria": str(row.get("criteria") or "")[:800],
        })
    # Collapse subpart rows whose parent row the scheme also declares.
    out: dict[str, dict] = {}
    for canonical, row in by_canonical.items():
        parent = _parent_canonical(canonical)
        if parent != canonical and parent in by_canonical:
            continue
        out[canonical] = row
    return out


def _returned_ids(result: dict) -> set[str]:
    ids: set[str] = set()
    for q in (result or {}).get("questions") or []:
        if isinstance(q, dict):
            ids.add(canonical_question_id(str(q.get("question_number", ""))))
    ids.discard("")
    return ids


def _looks_unresolved(q: dict) -> bool:
    status = str(q.get("status", "")).upper()
    return status in ("REVIEW", "NEEDS_REVIEW", "UNMARKED",)


def missing_from_results(result: dict, blueprint: dict | None) -> list[str]:
    """Official questions (original labels, sorted canonically) NOT covered.

    Sub-parts are covered by their parent question (a result that returned
    "62(a)" satisfies an inventory row of "62"), which is exactly how the
    blueprint reconciliation groups rows.
    """
    inventory = expected_inventory(blueprint)
    if not inventory:
        return []
    covered = _returned_ids(result)
    # A returned subpart ("62a") covers its parent row ("62") too.
    covered_parents = {_parent_canonical(i) for i in covered}
    missing: list[str] = []
    for canonical, row in inventory.items():
        if canonical in covered or canonical in covered_parents:
            continue
        parent = _parent_canonical(canonical)
        if parent != canonical and parent in covered:
            continue
        missing.append(row["question_number"])
    from .batching import _question_sort_key
    return sorted(missing, key=_question_sort_key)


def _parent_canonical(canonical: str) -> str:
    """The top-level id of a subpart id ("62a" -> "62")."""
    digits = re.match(r"^(\d{1,3})", canonical)
    return digits.group(1) if digits else canonical


def mark_missing_results(result: dict) -> int:
    """Give every UNRESOLVED question an explicit NEEDS_REVIEW state.

    ``apply_blueprint`` adds missing questions as status=REVIEW placeholders.
    REVIEW happily also means "an extra question appeared" — so the teacher's
    queue could not distinguish "the marker omitted a question the mark
    scheme REQUIRES" from "an informational anomaly". This pass upgrades the
    MISSING rows to an explicit NEEDS_REVIEW state with an actionable reason,
    never altering a mark.

    Returns the number of rows upgraded.
    """
    upgraded = 0
    for q in (result or {}).get("questions") or []:
        if not isinstance(q, dict) or not _looks_unresolved(q):
            continue
        reason = str(q.get("review_reason") or "")
        if "did not return an answer" not in reason and \
                "present in the official mark scheme" not in reason:
            continue  # not a missing-result row (extra question etc.)
        q["status"] = "NEEDS_REVIEW"
        q["review_reason"] = (
            "The marker returned no result for this question, although it is "
            "required by the official mark scheme. It is UNMARKED (not a "
            "confirmed 0). A targeted retry was attempted where supported; "
            "the result is still unresolved and needs manual marking or a "
            "re-run.")
        upgraded += 1
    return upgraded


def confirmed_zero_status(q: dict) -> str | None:
    """'CONFIRMED_ZERO' when the row is a genuine, evidence-backed zero.

    Mirrors the deterministic blank rule already used by the merge layer: the
    marker returned the question, awarded 0, left no student answer and has no
    mark_breakdown to show — that is a legitimate zero rather than missing
    evidence. A NEEDS_REVIEW / REVIEW placeholder row (the marker returned no
    result at all) is exactly what this function must NOT call a confirmed
    zero. ``None`` when the row is anything else.
    """
    if not isinstance(q, dict):
        return None
    # An unresolved row is never a confirmed zero, whatever its numbers say.
    if _looks_unresolved(q):
        return None
    if int(q.get("awarded_marks", 0) or 0) != 0:
        return None
    if int(q.get("max_marks", 0) or 0) <= 0:
        return None
    if str(q.get("student_answer", "")).strip():
        return None
    if q.get("mark_breakdown") or []:
        return None
    return "CONFIRMED_ZERO"


def narrow_blueprint(mark_scheme_pdf: str | None, ids: list[str]) -> dict | None:
    """A prompt-only blueprint restricted to the given question ids.

    Drives the targeted recovery retry: the marker is asked to return
    EXACTLY the listed questions (same prompting path, same structure), no
    other question is scored, and the full result is never re-marked. The
    structure comes from the CACHED blueprint for the mark scheme — this
    never triggers an AI extraction call of its own. ``None`` when the cache
    has no usable structure (the retry then falls back to the batch's normal
    full prompt) or when the ids cannot be found.
    """
    if not mark_scheme_pdf or not ids:
        return None
    from .blueprint import blueprint_for_mark_scheme
    try:
        cached, _meta = blueprint_for_mark_scheme(mark_scheme_pdf)
    except Exception:  # noqa: BLE001 — the narrower is best-effort
        return None
    inventory = expected_inventory(cached)
    if not inventory:
        return None
    wanted = {canonical_question_id(i) for i in ids}
    wanted.discard("")
    rows: list[dict] = []
    for canonical, row in inventory.items():
        parent = _parent_canonical(canonical)
        if canonical in wanted or parent in wanted:
            rows.append({
                "question_number": row["question_number"],
                "max_marks": row["max_marks"],
                "criteria": row["criteria"],
            })
    if not rows:
        return None
    return {"questions": rows}
