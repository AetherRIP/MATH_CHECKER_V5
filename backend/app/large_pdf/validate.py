"""Per-batch validation and canonical merge for the Large PDF Worker.

The rules enforced here are the difference between "corrected 100 pages
quickly" and "corrected 100 pages correctly":

* A question the marker did NOT return is **missing**, never a silent 0.
* A confirmed blank answer (the marker returned the question with no student
  answer, no breakdown and 0 marks) is distinguished from a missing result.
* Awarded marks must lie in ``0..max`` and the maximum must agree with the
  official mark scheme.
* A question is awarded exactly ONE authoritative result. Overlapping context
  pages never produce duplicate marks; a genuine conflict is flagged for
  teacher review instead of picking whichever response arrived last.

All totals are recomputed deterministically from the merged question list by
the preserved engine (``legacy_marking.calculate_totals``), so the overview,
the expanded view, the stored result and the report can never disagree.
"""

from __future__ import annotations

from ..engine import legacy_marking as legacy
from .states import BATCH_COMPLETED, BATCH_REVIEW_REQUIRED

#: Questions the batch planner could NOT keep whole (a boundary split them and
#: no safe shift existed). A batch only sees part of such a question's answer,
#: so its fragment is NOT authoritative and must never silently read as 0.
SPLIT_QUESTION_REVIEW_REASON = (
    "This question continues across a batch boundary that could not be kept "
    "whole, so the marker may only have seen part of the student's answer. "
    "The unmarked continuation is NOT a confirmed 0 — review before trusting "
    "this result.")


def as_mark(value) -> int | None:
    """A model-supplied mark as a whole number, or ``None`` if it is not one.

    Providers occasionally return ``"7"``, ``7.0``, ``"n/a"``, ``null`` or a
    nested object where a mark belongs. Validation must never raise on that:
    an unreadable mark is a REPORTED problem, never a crash and never a guess.
    A fractional value is not a whole mark either, so it reads as unreadable
    rather than being silently truncated.
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            return None
        return int(value) if value.is_integer() else None
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            return int(text)
        except ValueError:
            try:
                number = float(text)
            except ValueError:
                return None
            if number != number or number in (float("inf"), float("-inf")):
                return None
            return int(number) if number.is_integer() else None
    return None


def _rows_of(result) -> list[dict]:
    """The object rows of a model result. Anything else is not a question.

    ``questions`` may be absent, a string, or a list containing scalars when a
    provider truncates or wraps its JSON. Callers must see only real rows and
    report the rest, so every reader of a model response goes through here.
    """
    if not isinstance(result, dict):
        return []
    questions = result.get("questions")
    if not isinstance(questions, list):
        return []
    return [q for q in questions if isinstance(q, dict)]


def scheme_total_marks(blueprint) -> int | None:
    """The official paper total from an extracted mark-scheme structure.

    ``None`` when there is no usable structure — the batch coverage check then
    simply does not run rather than inventing an expectation.
    """
    if not isinstance(blueprint, dict):
        return None
    total = 0
    for q in blueprint.get("questions") or []:
        if not isinstance(q, dict):
            continue
        try:
            total += max(0, int(q.get("max_marks") or 0))
        except (TypeError, ValueError):
            continue
    if total > 0:
        return total
    try:
        declared = int(blueprint.get("total_marks") or 0)
    except (TypeError, ValueError):
        return None
    return declared or None


def _max_by_question(blueprint_questions) -> dict[str, int]:
    out: dict[str, int] = {}
    for q in blueprint_questions or []:
        try:
            number = str(q["question_number"]).strip()
            out[number] = int(q["max_marks"])
        except (KeyError, TypeError, ValueError):
            continue
    return out


#: Per-question states that mean "the marker never produced a result" (as
#: opposed to a confirmed, evidence-backed 0).
UNRESOLVED_STATUSES = ("REVIEW", "NEEDS_REVIEW", "UNMARKED")

#: A batch whose returned marks cover less than this share of the marks its
#: page range plausibly holds did not mark its pages — it returned a stub.
#: Real defect this guards: a 49-page submission whose second batch (pages
#: 25-49, ~136 expected marks) returned 8 sub-rows covering 9 marks, and the
#: pipeline accepted it as a COMPLETED batch, so 32 questions the mark scheme
#: requires (38-69, 128 marks) silently became zeros and the student's report
#: read 124/266 = 46.6% instead of 124/138 = 89.9% on the work that WAS marked.
STUB_COVERAGE_RATIO = 0.25

#: Below this many expected marks a coverage ratio is too noisy to judge.
MIN_EXPECTED_MARKS_FOR_COVERAGE = 8


def batch_coverage(result: dict | None, batch: dict, *,
                   paper_pages: int | None,
                   scheme_total_marks: int | None,
                   min_ratio: float = STUB_COVERAGE_RATIO,
                   min_expected_marks: int = MIN_EXPECTED_MARKS_FOR_COVERAGE
                   ) -> dict | None:
    """Does a batch's result plausibly cover the pages it owns?

    Deterministic, model-free completeness proxy: a batch that owns a share of
    the student's pages should return roughly that share of the official
    marks. Batching is not uniform, so the tolerance is generous
    (``min_ratio``); anything below it is a STUB response (the model answered a
    few questions and stopped, usually after provider failures), which must be
    re-marked rather than accepted. Returns ``None`` when there is not enough
    information to judge — never a fabricated verdict.
    """
    try:
        pages = int(paper_pages or 0)
        scheme_marks = int(scheme_total_marks or 0)
    except (TypeError, ValueError):
        return None
    if pages <= 0 or scheme_marks <= 0:
        return None
    try:
        owned = int(batch.get("pages") or 0)
    except (TypeError, ValueError):
        owned = 0
    if owned <= 0:
        try:
            owned = max(0, int(batch["page_end"]) - int(batch["page_start"]) + 1)
        except (KeyError, TypeError, ValueError):
            owned = 0
    if owned <= 0:
        return None
    expected = scheme_marks * (owned / pages)
    if expected < min_expected_marks:
        return None
    returned = 0
    for q in _rows_of(result):
        mark = as_mark(q.get("max_marks"))
        if mark is not None:
            returned += max(0, mark)
    ratio = returned / expected if expected else None
    stub = ratio is not None and ratio < min_ratio
    return {
        "owned_pages": owned,
        "paper_pages": pages,
        "expected_marks": round(expected, 1),
        "returned_marks": returned,
        "ratio": round(ratio, 3) if ratio is not None else None,
        "stub": bool(stub),
        "reason": (
            f"this batch owns {owned} of {pages} pages (~{expected:.0f} marks "
            f"expected) but returned only {returned} marks worth of "
            f"questions — the response is incomplete, not a full mark of "
            f"those pages" if stub else ""),
    }


def validate_batch(result: dict | None, batch: dict, *,
                   expected_questions: list[str] | None = None,
                   blueprint_max: dict[str, int] | None = None,
                   coverage: dict | None = None) -> dict:
    """Validate one batch's marks. Never raises; reports problems instead.

    ``coverage`` (from :func:`batch_coverage`) additionally reports whether the
    batch returned a STUB. A stub is deliberately NOT a plain "problem": the
    runner re-marks the same batch first, and only escalates to review when the
    retry budget is spent.
    """
    problems: list[str] = []
    warnings: list[str] = []
    returned: list[str] = []
    duplicates: list[str] = []
    invalid_marks: list[str] = []
    max_mismatch: list[str] = []
    blanks: list[str] = []

    if result is not None and not isinstance(result, dict):
        problems.append("The model response was not a JSON object.")
    questions = result.get("questions") if isinstance(result, dict) else None
    if questions is not None and not isinstance(questions, list):
        problems.append("The model response's 'questions' field was not a list.")
    seen: set[str] = set()
    for q in _rows_of(result):
        number = str(q.get("question_number") or "").strip()
        if not number:
            problems.append("A returned question had no question number.")
            continue
        if number in seen:
            duplicates.append(number)
        seen.add(number)
        returned.append(number)
        awarded = as_mark(q.get("awarded_marks"))
        max_marks = as_mark(q.get("max_marks"))
        if awarded is None or max_marks is None:
            # Never coerce an unreadable mark to 0: a teacher must decide.
            invalid_marks.append(f"{number}:unreadable")
            continue
        if awarded < 0 or (max_marks > 0 and awarded > max_marks):
            invalid_marks.append(f"{number}:{awarded}/{max_marks}")
        if blueprint_max and number in blueprint_max:
            official = blueprint_max[number]
            if max_marks and max_marks != official:
                max_mismatch.append(f"{number}:{max_marks}!={official}")
        # Confirmed blank: the marker saw the question, produced no evidence
        # and awarded nothing. This is a legitimate 0, NOT a missing result.
        if (awarded == 0 and max_marks > 0
                and not str(q.get("student_answer") or "").strip()
                and not (q.get("mark_breakdown") or [])):
            blanks.append(number)

    expected_source = expected_questions
    if expected_source is None:
        expected_source = batch.get("question_ids")
        if isinstance(expected_source, str):
            try:
                import json as _json
                expected_source = _json.loads(expected_source)
            except (ValueError, TypeError):
                expected_source = []
    expected = [str(q) for q in (expected_source or [])]
    missing = [q for q in expected if q not in seen]

    if invalid_marks:
        problems.append(
            "Awarded marks outside the permitted range for "
            + ", ".join(invalid_marks))
    if max_mismatch:
        problems.append(
            "Maximum marks disagree with the mark scheme for "
            + ", ".join(max_mismatch))
    if duplicates:
        warnings.append("Duplicate question identifiers: "
                        + ", ".join(sorted(set(duplicates))))

    stub = bool(coverage and coverage.get("stub"))
    if stub:
        # A stub is a heuristic, so it never blocks the batch on its own: the
        # runner re-marks the same batch, keeps whichever attempt marked more
        # of its pages, and the merge-level reconciliation still reports any
        # question the mark scheme requires but nobody returned.
        warnings.append(
            "INCOMPLETE RESPONSE: " + str(coverage.get("reason") or "")
            + " — the batch is re-marked, and its questions stay unmarked "
              "(never 0) if the retry cannot complete them.")

    status = BATCH_REVIEW_REQUIRED if problems else BATCH_COMPLETED
    return {
        "status": status,
        "stub": stub,
        "coverage": coverage or None,
        "returned_questions": sorted(set(returned)),
        "missing_questions": missing,
        "duplicate_questions": sorted(set(duplicates)),
        "invalid_marks": invalid_marks,
        "max_marks_mismatch": max_mismatch,
        "blank_questions": sorted(set(blanks)),
        "problems": problems,
        "warnings": warnings,
        "question_count": len(seen),
    }


def _as_list(value) -> list:
    """Coerce a stored ``question_ids`` value (JSON string or list) to a list."""
    if value is None:
        return []
    if isinstance(value, str):
        try:
            import json as _json
            parsed = _json.loads(value)
            return parsed if isinstance(parsed, list) else []
        except (ValueError, TypeError):
            return []
    return list(value)


def _owner_map(plan, batches: list[dict]) -> dict[str, int]:
    """Canonical owner batch for every known question id.

    Primary source: the plan's question->page evidence. Fallback: the first
    batch that lists the question in its expected set.
    """
    owners: dict[str, int] = {}
    for qid, pages in (plan.question_pages or {}).items():
        if not pages:
            continue
        owner = plan.owner_of(min(pages))
        if owner is not None:
            owners[str(qid)] = owner
    for batch in batches:
        for qid in _as_list(batch.get("question_ids")):
            owners.setdefault(str(qid), int(batch["batch_index"]))
    return owners


def merge_batches(plan, batches: list[dict], batch_results: dict[int, dict], *,
                  blueprint_questions=None) -> dict:
    """Merge validated batch results into ONE canonical, complete result.

    ``batch_results`` maps ``batch_index -> normalised result dict``.
    Returns ``{result, report, needs_review}`` where ``result`` is the merged
    legacy result (ready for ``apply_blueprint`` + ``verify_result``).
    """
    owners = _owner_map(plan, batches)
    blueprint_max = _max_by_question(blueprint_questions)

    contributions: dict[str, list[tuple[int, dict]]] = {}
    for batch in batches:
        index = int(batch["batch_index"])
        result = batch_results.get(index)
        if result is None:
            continue
        for q in _rows_of(result):
            qid = str(q.get("question_number") or "").strip()
            if qid:
                contributions.setdefault(qid, []).append((index, q))

    merged: dict[str, dict] = {}
    conflicts: list[dict] = []
    overlap_ignored: list[str] = []
    cross_owned: list[str] = []
    duplicates: list[str] = []
    blanks: list[str] = []
    split_flag_questions: set[str] = set()

    for qid, entries in contributions.items():
        owner = owners.get(qid)
        by_owner = [e for e in entries if e[0] == owner]
        # An unreadable award sorts below every real mark so two attempts can
        # never look like the "same" mark, and the row is held for review.
        distinct_marks = set()
        unreadable = False
        for entry in entries:
            mark = as_mark(entry[1].get("awarded_marks"))
            if mark is None:
                unreadable = True
                distinct_marks.add(-1)
            else:
                distinct_marks.add(mark)
        if len(entries) > 1:
            duplicates.append(qid)

        # ---- METHODOLOGICAL SAFEGUARDS (merge-time) -----------------------
        # 1. A question the planner FLAGGED as split must never finalize as a
        #    clean mark: flag NEEDS_REVIEW on the row itself so the UI block
        #    on finalization is structural, not convention.
        # 2. A piece of the question appearing in several batches is kept
        #    ONCE (owner-first) so no fragment is double-counted. Different
        #    fragment marks are a conflict, and a teacher must decide.
        if by_owner:
            chosen = by_owner[0][1]
            if len(entries) > len(by_owner):
                overlap_ignored.append(qid)
            if len(distinct_marks) > 1:
                conflicts.append({
                    "question": qid,
                    "owner_batch": owner,
                    "marks": sorted(distinct_marks),
                })
        elif len(entries) == 1:
            chosen = entries[0][1]
            if owner is not None and entries[0][0] != owner:
                cross_owned.append(qid)
        else:
            # Appears in several batches but none of them owns it and there
            # is no marked owner: an ambiguous match. Do not guess a mark.
            chosen = entries[0][1]
            conflicts.append({
                "question": qid,
                "owner_batch": owner,
                "marks": sorted(distinct_marks),
                "ambiguous": True,
            })

        question = dict(chosen)
        awarded = as_mark(question.get("awarded_marks"))
        official_max = as_mark(question.get("max_marks"))
        if unreadable or awarded is None:
            # A mark nobody can read is not a zero and not a mark: it is a
            # question a teacher has to settle.
            question["status"] = "NEEDS_REVIEW"
            question.setdefault(
                "review_reason",
                "The marker returned an unreadable mark for this question; "
                "it was not converted to 0 and needs review.")
        # A confirmed blank: the marker returned the question, found no work
        # and awarded nothing. Distinct from a MISSING result (not returned).
        if (awarded == 0 and official_max is not None and official_max > 0
                and not str(question.get("student_answer") or "").strip()
                and not (question.get("mark_breakdown") or [])):
            blanks.append(qid)
        if qid in {c["question"] for c in conflicts}:
            question["status"] = "REVIEW"
            question.setdefault(
                "review_reason",
                "Conflicting marks from overlapping batches — a teacher must "
                "review before this mark is trusted.")
        flagged_ids = {str(q).strip() for q in (plan.flagged_questions or [])}
        if qid in flagged_ids and qid not in \
                {c["question"] for c in conflicts}:
            # A split question whose continuation may not have been marked:
            # do NOT finalize the owning fragment as a clean result. Row-level
            # NEEDS_REVIEW makes the finalization block structural.
            question["status"] = "NEEDS_REVIEW"
            question.setdefault("review_reason", SPLIT_QUESTION_REVIEW_REASON)
            split_flag_questions.add(qid)
        merged[qid] = question

    questions = sorted(merged.values(),
                       key=lambda q: _question_sort_key(q.get("question_number")))

    result = {
        "paper_info": {"qualification": "Pearson Edexcel International GCSE Mathematics",
                       "paper": "Not identified", "session": "Not identified",
                       "max_marks_declared": None},
        "questions": questions,
        "overall_notes": {"summary": "", "strengths": [], "revision_areas": []},
        "uncertainties": [],
    }

    # NOTE: this gap is computed from RAW model ids, so it is an ADVISORY
    # pre-reconciliation signal only; the authoritative coverage check is the
    # blueprint reconciliation (``apply_blueprint``), which groups sub-part
    # rows ("1(a)", "1(b)(i)") onto the question the mark scheme declares.
    # Callers must not treat this list as the final missing-question set.
    expected = set(blueprint_max) if blueprint_max else (
        set(owners) | set(progressed_questions(batches)))
    missing = sorted(expected - set(merged), key=_question_sort_key)

    # ---- coverage that could NOT be verified ----------------------------
    # When no mark-scheme structure is available, nothing above can see a
    # question the marker simply never returned — yet a batch that answered a
    # fraction of the pages its siblings answered is strong evidence of an
    # incomplete response (the live case: batch 0 returned 34 questions over
    # 24 pages, batch 1 returned 8 rows over 25 pages). This compares the
    # batches with each other only, so it needs no scheme, and it never changes
    # a mark: an unbalanced batch puts the correction in the teacher's queue.
    thin_batches = _thin_batches(batches, batch_results)

    needs_review = (bool(conflicts) or bool(cross_owned)
                    or bool(thin_batches)
                    or bool(split_flag_questions))
    report = {
        "merged_questions": len(merged),
        "duplicate_questions": sorted(set(duplicates), key=_question_sort_key),
        "overlap_ignored": sorted(set(overlap_ignored), key=_question_sort_key),
        "cross_owned": sorted(set(cross_owned), key=_question_sort_key),
        "conflicts": conflicts,
        "blank_questions": sorted(set(blanks), key=_question_sort_key),
        "missing_questions": missing,
        "expected_questions": sorted(expected, key=_question_sort_key),
        "missing_questions_pre_reconciliation": missing,
        "split_flagged_questions": sorted(split_flag_questions,
                                           key=_question_sort_key),
        "thin_batches": thin_batches,
        "needs_review": needs_review,
    }
    return {"result": result, "report": report, "needs_review": needs_review}


#: A batch answering less than this share of its siblings' average question
#: density almost certainly stopped early. Deliberately loose: real packets are
#: uneven, and a false "review" costs a teacher a glance, never a mark.
THIN_BATCH_DENSITY_SHARE = 0.5


def _owned_pages(batch: dict) -> int:
    try:
        pages = int(batch.get("pages") or 0)
    except (TypeError, ValueError):
        pages = 0
    if pages > 0:
        return pages
    try:
        return max(0, int(batch["page_end"]) - int(batch["page_start"]) + 1)
    except (KeyError, TypeError, ValueError):
        return 0


def _thin_batches(batches: list[dict],
                  batch_results: dict[int, dict]) -> list[dict]:
    """Batches whose returned question density is far below their siblings'."""
    densities: list[tuple[int, int, float]] = []
    for batch in batches:
        try:
            index = int(batch["batch_index"])
        except (KeyError, TypeError, ValueError):
            continue
        result = batch_results.get(index)
        if result is None:
            continue
        owned = _owned_pages(batch)
        rows = len([q for q in result.get("questions") or []
                    if isinstance(q, dict)])
        if owned > 0:
            densities.append((index, rows, rows / owned))
    if len(densities) < 2:
        return []
    mean = sum(d for _i, _r, d in densities) / len(densities)
    if mean <= 0:
        return []
    thin = []
    for index, rows, density in densities:
        if density < THIN_BATCH_DENSITY_SHARE * mean:
            thin.append({
                "batch": index,
                "returned_questions": rows,
                "density": round(density, 3),
                "average_density": round(mean, 3),
            })
    return sorted(thin, key=lambda entry: entry["batch"])


def progressed_questions(batches: list[dict]) -> set[str]:
    """Every question id referenced anywhere in the plan."""
    out: set[str] = set()
    for batch in batches:
        out.update(str(q) for q in _as_list(batch.get("question_ids")))
    return out


def _question_sort_key(value) -> tuple:
    text = str(value).strip()
    lead = ""
    for ch in text:
        if ch.isdigit():
            lead += ch
        else:
            break
    try:
        return (0, int(lead), text)
    except ValueError:
        return (1, 0, text)


def totals_for(result: dict) -> tuple[int, int, float]:
    """Deterministic totals via the preserved legacy arithmetic."""
    return legacy.calculate_totals(result)


def score_breakdown(result: dict) -> dict:
    """Score the work that was ACTUALLY marked, and name what was not.

    A question the marker never returned is UNMARKED. Dividing by it would
    charge the student for the marker's failure — the live defect that showed
    124/266 = 46.6% (grade 4) for a submission whose marked questions were
    124/138 = 89.9%. So the reported percentage is computed over the marked
    questions only, and the unresolved remainder is reported separately and
    explicitly. Nothing here changes a mark, and a fully marked paper is
    accounted for exactly as before (``score_basis`` = COMPLETE and
    ``unresolved_marks`` = 0).
    """
    rows = [q for q in (result or {}).get("questions") or []
            if isinstance(q, dict)]
    earned = available = paper_available = 0
    unresolved: list[str] = []
    for q in rows:
        try:
            max_marks = int(q.get("max_marks") or 0)
        except (TypeError, ValueError):
            max_marks = 0
        paper_available += max_marks
        if str(q.get("status") or "").upper() in UNRESOLVED_STATUSES:
            unresolved.append(str(q.get("question_number")))
            continue
        available += max_marks
        # A single number is the score basis, so every row is validated before
        # it is summed: non-numeric, negative and above-maximum awards never
        # reach the total (the per-batch validator reports them too).
        try:
            awarded = int(q.get("awarded_marks") or 0)
        except (TypeError, ValueError):
            awarded = 0
        awarded = max(0, awarded)
        if max_marks > 0:
            awarded = min(awarded, max_marks)
        earned += awarded
    percentage = (earned / available * 100.0) if available else 0.0
    # A correction is PARTIAL whenever any expected mark has no outcome. It is
    # never presented as a score out of the paper: a 22/22 share of the seven
    # questions a failed run happened to mark reads to a teacher as full marks.
    paper_percentage = (earned / paper_available * 100.0) if paper_available else 0.0
    return {
        "earned": earned,
        "available": available,
        "paper_available": paper_available,
        "unresolved_questions": sorted(unresolved, key=_question_sort_key),
        "unresolved_count": len(unresolved),
        "unresolved_marks": max(0, paper_available - available),
        "percentage": percentage,
        "marked_percentage": percentage,
        "paper_percentage": paper_percentage,
        "partial": bool(unresolved) or paper_available > available,
        "score_basis": ("MARKED_ONLY" if unresolved else "COMPLETE"),
    }


#: Row states that mean a teacher (or a later run) still has to resolve it.
_REVIEW_STATES = ("REVIEW", "NEEDS_REVIEW", "UNMARKED", "FAILED")


def coverage_inventory(result: dict, blueprint_report: dict | None = None,
                       *, expected_questions: list[str] | None = None) -> dict:
    """One explicit state per expected question — the accountability record.

    Every expected question is counted exactly once, so a run can never claim
    coverage it does not have:

    * ``marked``          — a returned mark that passed reconciliation;
    * ``unanswered``      — the marker saw the answer and it is genuinely blank
      (``CONFIRMED_ZERO``): a legitimate 0, not a gap;
    * ``illegible``       — flagged as unreadable rather than scored;
    * ``requires_review`` — returned but unresolved (ambiguous sub-parts,
      conflicts, split questions) or never returned at all (``NEEDS_REVIEW``);
    * ``unaccounted``     — expected by the mark scheme but absent from the
      result entirely. Never silently dropped.

    ``verified`` is true only when every expected question has a definitive
    outcome (marked or a confirmed blank) and nothing is unaccounted for.
    """
    rows = [q for q in (result or {}).get("questions") or []
            if isinstance(q, dict)]
    report = blueprint_report or {}
    expected = [str(q) for q in (expected_questions or [])]
    numbers = [str(q.get("question_number")) for q in rows]
    if not expected and report.get("applied"):
        missing = {str(q) for q in (report.get("missing_questions") or [])}
        expected = numbers + [m for m in missing if m not in set(numbers)]

    seen: set[str] = set()
    marked = unanswered = illegible = requires_review = 0
    unmarked: list[str] = []
    for q in rows:
        number = str(q.get("question_number"))
        seen.add(number)
        state = str(q.get("status") or "").upper()
        reason = str(q.get("review_reason") or "").lower()
        if state == "CONFIRMED_ZERO":
            unanswered += 1
        elif state in _REVIEW_STATES:
            requires_review += 1
            unmarked.append(number)
            if "illegible" in reason or "unreadable" in reason:
                illegible += 1
        else:
            marked += 1

    unaccounted = sorted({e for e in expected if e not in seen},
                         key=_question_sort_key)
    total_expected = len(expected) if expected else len(seen)
    return {
        "expected": total_expected,
        "extracted": len(seen),
        "marked": marked,
        "unanswered": unanswered,
        "illegible": illegible,
        "requires_review": requires_review,
        "unaccounted": unaccounted,
        "unmarked": sorted(unmarked, key=_question_sort_key),
        "verified": (total_expected > 0
                     and not unaccounted
                     and marked + unanswered == total_expected),
    }
