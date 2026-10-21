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

# The safety cap on a single targeted-recovery round. A healthy blueprint
# should never miss this many questions in one pass; when the inventory
# reports more, the problem is almost certainly the BLUEPRINT (a bad
# extraction), not the marker, and no number of retries can fix it. The
# affected questions stay unresolved (NEEDS_REVIEW / teacher review) instead
# of burning unbounded API quota.
MAX_MISSING_QUESTIONS = 12

def canonical_question_id(text: str) -> str:
    """Normalise an id to ONE canonical form so equivalent labels match.

    "62(a)" / "Q62(a)" / "62 a" / " 62 (a)"  -> "62a"
    "14" / "Q14" / " q14. "                  -> "14"
    "4(b)(ii)" / "4 b ii" / "4bii"          -> "4bii"
    "2.1" / "2.1.3"                          -> "2.1" / "2.1.3"

    The NESTING is preserved, because a subquestion is identified by its full
    label. Collapsing "4(b)(i)" and "4(b)(ii)" to one id ("4b") gave two
    different marking points a shared identity, so an answer or a mark for one
    could be read as belonging to the other. Anything that does not parse
    returns the low-cased alphanumerics, so an exotic id can never collide
    with an unrelated question number.
    """
    from . import scheme_structure as st
    return st.subquestion_id(text)


def parent_canonical(canonical: str) -> str:
    """The TOP-LEVEL id of a (sub)question id: "62a" -> "62", "4bii" -> "4".

    Kept as a public helper because other modules ask "which question does this
    sub-part belong to?"; the nesting-aware walk itself lives in
    :mod:`app.engine.scheme_structure`.
    """
    from . import scheme_structure as st
    return st.root_id(canonical)


def subquestion_ids(row: dict) -> list[dict]:
    """The declared subquestions of one scheme row (or ``[]``)."""
    if not isinstance(row, dict):
        return []
    from . import scheme_structure as st
    out: list[dict] = []
    for part in (row.get("subquestions") or row.get("parts") or []):
        if not isinstance(part, dict):
            continue
        label = str(part.get("number") or part.get("question_number")
                    or part.get("label") or "").strip()
        if not label:
            continue
        parent = st.subquestion_id(row.get("question_number"))
        if st.parse_label(label)[0] is None:
            # A locally-labelled part ("b", "(ii)") belongs to this question.
            _canonical, label = st.local_subquestion(parent, label)
        try:
            max_marks = int(part.get("max_marks"))
        except (TypeError, ValueError):
            max_marks = 0
        out.append({
            "question_number": label,
            "id": st.subquestion_id(label),
            "max_marks": max_marks,
            "criteria": str(part.get("criteria") or "")[:800],
        })
    return out


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

    A question that declares its OWN subquestions (``parts``) is the opposite
    case: those subquestions are the marking points, so each is expected in its
    own right and a sub-part the marker never returned is reported rather than
    absorbed into the parent (§4).
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
        declared = subquestion_ids(row)
        if declared:
            # The subquestions ARE the marking points: each one is expected in
            # its own right, so a sub-part the marker never returned is
            # reported, not absorbed into its parent (§4).
            for sub in declared:
                by_canonical.setdefault(sub["id"], {
                    "question_number": sub["question_number"],
                    "max_marks": sub["max_marks"] or max_marks,
                    "criteria": sub["criteria"],
                    "parent_question": number,
                })
            continue
        by_canonical.setdefault(canonical, {
            "question_number": number,
            "max_marks": max_marks,
            "criteria": str(row.get("criteria") or "")[:800],
        })

    # A row that labels a SUB-PART of another row the scheme also declares is
    # that row's sub-part, not a second required question with its own marks:
    # the parent's allocation already includes it. It is attached as declared
    # subquestion evidence and drops out of the expected list (the pre-existing
    # "62(a)" + "62" rule, applied to any level of nesting).
    collapsing = {canonical: nearest_declared(canonical, by_canonical)
                  for canonical in by_canonical}
    out: dict[str, dict] = {}
    for canonical, row in by_canonical.items():
        if collapsing.get(canonical):
            continue
        out[canonical] = row
    for canonical, row in by_canonical.items():
        ancestor = collapsing.get(canonical)
        if not ancestor:
            continue
        # Attach to the nearest ancestor that is itself a required question
        # ("4" declared, "4b" and "4bii" declared as rows -> both land on 4).
        walk = ancestor
        while collapsing.get(walk):
            walk = collapsing.get(walk) or ""
            if not walk:
                break
        if not walk:
            continue
        out.setdefault(walk, {
            "question_number": walk,
            "max_marks": 0,
            "criteria": "",
        }).setdefault("subquestions", []).append({
            "id": canonical,
            "question_number": row["question_number"],
            "max_marks": row["max_marks"],
            "criteria": row["criteria"],
        })
    return out


def nearest_declared(canonical: str, declared: dict[str, dict]) -> str:
    """The closest DECLARED ancestor of ``canonical``, or "".

    Walks the nesting ("4bii" -> "4bi" -> "4b" -> "4") so a nested label
    attaches to its real parent rather than to the top-level question.
    """
    from . import scheme_structure as st
    walk = st.parent_id(str(canonical or ""))
    while walk:
        if walk in declared:
            return walk
        walk = st.parent_id(walk)
    return ""


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

    A whole-question row is covered by a result for a sub-part nested inside
    it ("62(a)" satisfies an inventory row of "62"), which is exactly how the
    blueprint reconciliation groups rows. A declared SUBQUESTION is only ever
    covered by its own result: the parent's result does not prove the sub-part
    was assessed (§4).
    """
    inventory = expected_inventory(blueprint)
    if not inventory:
        return []
    covered = _returned_ids(result)
    missing: list[str] = []
    for canonical, row in inventory.items():
        # Covered by its own result, or by a result for a MORE SPECIFIC
        # subquestion nested inside it ("62a" covers the whole-question row
        # "62"). The reverse is deliberately NOT coverage: a result for the
        # parent does not prove that 4(b)(ii) was assessed, and treating it as
        # coverage is exactly how an unmarked sub-part becomes a silent zero.
        if _covers(canonical, covered):
            continue
        missing.append(row["question_number"])
    from .batching import _question_sort_key
    return sorted(missing, key=_question_sort_key)


def _covers(expected: str, covered: set[str]) -> bool:
    """Is ``expected`` accounted for by any covered id?

    True when the expected id itself was returned, or when a returned id is
    nested INSIDE it (a sub-part result answers the question it belongs to).
    """
    from . import scheme_structure as st
    if expected in covered:
        return True
    for returned in covered:
        walk = st.parent_id(returned)
        while walk:
            if walk == expected:
                return True
            walk = st.parent_id(walk)
    return False


def _parent_canonical(canonical: str) -> str:
    """The top-level id of a subpart id ("62a" -> "62", "4bii" -> "4")."""
    return parent_canonical(canonical_question_id(canonical))


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
    try:
        awarded = int(q.get("awarded_marks") or 0)
        maximum = int(q.get("max_marks") or 0)
    except (TypeError, ValueError):
        # An unreadable mark is never evidence of a genuine zero.
        return None
    if awarded != 0:
        return None
    if maximum <= 0:
        return None
    if str(q.get("student_answer", "")).strip():
        return None
    if q.get("mark_breakdown") or []:
        return None
    return "CONFIRMED_ZERO"


def narrow_blueprint(mark_scheme_pdf: str | None, ids: list[str],
                     *, blueprint: dict | None = None) -> dict | None:
    """A prompt-only blueprint restricted to the given question ids.

    Drives the targeted recovery retry: the marker is asked to return
    EXACTLY the listed questions (same prompting path, same structure), no
    other question is scored, and the full result is never re-marked. The
    structure comes from the CACHED blueprint for the mark scheme — this
    never triggers an AI extraction call of its own. ``None`` when the cache
    has no usable structure (the retry then falls back to the batch's normal
    full prompt) or when the ids cannot be found.

    ``blueprint`` lets a caller that ALREADY holds the reconciled in-memory
    structure pass it in directly. The disk cache is keyed by the mark-scheme
    file path, so a caller whose scheme arrived from Classroom/Drive (no local
    cached path) previously got ``None`` here and the retry was refused — the
    omitted question then stayed a persisted 0 forever. Supplying the live
    blueprint removes that dependency without changing the cached path's
    behaviour.
    """
    if not ids:
        return None
    if blueprint is not None:
        cached = blueprint
    else:
        if not mark_scheme_pdf:
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
    from . import scheme_structure as st
    rows: list[dict] = []
    for canonical, row in inventory.items():
        # Wanted by its own id, or by any ANCESTOR ("recover 4" recovers 4, 4a,
        # 4(b)(ii) ...) — walking the nesting rather than the root alone, so a
        # mid-level request is not silently widened or narrowed.
        walk = canonical
        wanted_here = False
        while walk:
            if walk in wanted:
                wanted_here = True
                break
            walk = st.parent_id(walk)
        if wanted_here:
            rows.append({
                "question_number": row["question_number"],
                "max_marks": row["max_marks"],
                "criteria": row["criteria"],
            })
    if not rows:
        return None
    return {"questions": rows}
