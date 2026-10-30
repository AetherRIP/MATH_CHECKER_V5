"""Accuracy, coverage and disagreement metrics for a marking evaluation.

Every metric states its denominator. Two rules matter more than the arithmetic:

**Missing is not agreement.** A question the marker never returned is counted
as missing and reported; it is not dropped to improve the rate.

**Partial verification cannot produce a whole-paper total.** A script-level
total is compared only when every reference item on that paper is eligible and
was matched. Comparing a fully-marked AI total with a half-verified teacher
total would manufacture a large "error" that is really a coverage gap.
"""

from __future__ import annotations

import math
import re
import statistics
from typing import Any, Iterable

from .dataset import ReferenceDataset, ReferenceItem
from .store import ResolvedReference

Z_95 = 1.959963984540054

CATEGORY_HYPOTHESES: dict[str, tuple[str, bool]] = {
    "alternative-method": ("mark-scheme rejected or mishandled an alternative method", True),
    "equivalent-expression": ("equivalent expression treated as a different answer", True),
    "follow-through": ("follow-through marking not applied to the candidate's own work", True),
    "nested-subquestion": ("question/subquestion matching or nested-part attribution", True),
    "missing-answer": ("blank or missing answer handling", False),
    "partial-credit": ("partial-credit decision", True),
    "method-and-accuracy": ("method/accuracy mark split", True),
    "ambiguous": ("ambiguous answer handling", True),
    "incorrect": ("incorrect answer accepted or a correct rejection disputed", True),
    "scheme-total-disagrees": ("mark-scheme extraction or total reconstruction", True),
}

DEFINITIONS = {
    "exact_item_agreement": {
        "numerator": "matched reference items whose AI mark equals the teacher mark",
        "denominator": "matched reference items with a valid AI mark (question and subquestion levels combined)",
    },
    "exact_question_agreement": {
        "numerator": "matched question-level references with an exact mark",
        "denominator": "matched question-level references with a valid AI mark",
    },
    "exact_subquestion_agreement": {
        "numerator": "matched subquestion-level references with an exact mark",
        "denominator": "matched subquestion-level references with a valid AI mark",
    },
    "exact_total_agreement": {
        "numerator": "scripts whose AI total equals the teacher total",
        "denominator": "scripts where every reference item is eligible, matched and valid, and both totals exist",
    },
    "mean_absolute_error": {
        "numerator": "sum of |AI mark - teacher mark|",
        "denominator": "matched reference items with a valid AI mark",
    },
    "normalized_mae": {
        "numerator": "sum of |AI mark - teacher mark| / maximum marks for the item",
        "denominator": "matched reference items with a valid AI mark and maximum marks > 0",
    },
    "overmarking_rate": {
        "numerator": "matched items where AI mark > teacher mark",
        "denominator": "matched reference items with a valid AI mark",
    },
    "undermarking_rate": {
        "numerator": "matched items where AI mark < teacher mark",
        "denominator": "matched reference items with a valid AI mark",
    },
    "average_overmarking_magnitude": {
        "numerator": "sum of (AI mark - teacher mark) over overmarked items",
        "denominator": "overmarked items",
    },
    "average_undermarking_magnitude": {
        "numerator": "sum of (teacher mark - AI mark) over undermarked items",
        "denominator": "undermarked items",
    },
    "question_coverage": {
        "numerator": "question-level references matched to a valid AI mark",
        "denominator": "eligible question-level references expected in the run",
    },
    "subquestion_coverage": {
        "numerator": "subquestion-level references matched to a valid AI mark",
        "denominator": "eligible subquestion-level references expected in the run",
    },
    "review_flag_rate": {
        "numerator": "scripts the AI flagged for review",
        "denominator": "scripts processed with a usable outcome",
    },
    "human_review_rate": {
        "numerator": "matched items where AI and teacher marks disagree",
        "denominator": "matched reference items with a valid AI mark",
    },
}

_WS = re.compile(r"\s+")


def canonical_label(label: Any) -> str:
    return _WS.sub("", str(label or "")).lower()


def question_stem(label: Any) -> str:
    match = re.match(r"\s*(\d+)", str(label or ""))
    return match.group(1) if match else canonical_label(label)


def _num(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return None


def _rate(numerator: int, denominator: int) -> float | None:
    return round(numerator / denominator, 6) if denominator else None


def wilson_interval(successes: int, total: int,
                    z: float = Z_95) -> list[float] | None:
    """95% Wilson score interval, which behaves at small n and near 0/1."""
    if total <= 0:
        return None
    p = successes / total
    denominator = 1 + z * z / total
    centre = (p + z * z / (2 * total)) / denominator
    margin = (z * math.sqrt(p * (1 - p) / total + z * z / (4 * total * total))
              / denominator)
    return [round(max(0.0, centre - margin), 6),
            round(min(1.0, centre + margin), 6)]


def _agreement(exact: int, total: int) -> dict:
    return {
        "exact": exact,
        "compared": total,
        "rate": _rate(exact, total),
        "ci95": wilson_interval(exact, total),
        "denominator": "matched reference items with a valid AI mark",
    }


def _mark_entry(candidates: dict[str, Any], label: str) -> dict | None:
    return candidates.get(canonical_label(label))


def _awarded(entry: Any) -> int | None:
    if isinstance(entry, dict):
        return _num(entry.get("awarded", entry.get("awarded_marks")))
    return _num(entry)


def _declared_max(entry: Any) -> int | None:
    if isinstance(entry, dict):
        return _num(entry.get("max", entry.get("max_marks")))
    return None


def _failed(outcome: dict) -> bool:
    status = str(outcome.get("status") or "").lower()
    if status in {"error", "failed", "timeout", "invalid"}:
        return True
    if outcome.get("error"):
        return True
    return not (outcome.get("questions") or outcome.get("subquestions")
                or outcome.get("earned") is not None)


def _total_of(outcome: dict) -> tuple[int | None, int | None, str | None]:
    total = outcome.get("total") if isinstance(outcome.get("total"), dict) else {}
    earned = _num(total.get("earned", outcome.get("earned")))
    available = _num(total.get("available", outcome.get("available")))
    grade = total.get("grade", outcome.get("grade"))
    return earned, available, str(grade) if grade not in (None, "") else None


def _hypothesis(categories: Iterable[str]) -> tuple[str, bool]:
    for category in categories:
        if category in CATEGORY_HYPOTHESES:
            return CATEGORY_HYPOTHESES[category]
    return ("unclassified numerical disagreement", False)


def _select_rows(dataset: ReferenceDataset, resolved: dict[str, ResolvedReference],
                 *, partition: str, include_unverified: bool
                 ) -> tuple[list[tuple[ReferenceItem, int, str]], list[dict]]:
    """Return (item, reference mark, certification basis) plus exclusions."""
    rows: list[tuple[ReferenceItem, int, str]] = []
    excluded: list[dict] = []
    for item in dataset.items:
        if partition != "all" and item.partition != partition:
            excluded.append({"reference_id": item.reference_id,
                             "status": "out_of_partition",
                             "reason": f"partition={item.partition}"})
            continue
        row = resolved.get(item.reference_id)
        if row is not None and row.eligible:
            rows.append((item, int(row.teacher_mark or 0), "teacher_verified"))
            continue
        if include_unverified and item.provenance != "ai_generated":
            rows.append((item, item.scheme_mark, "scheme_reference"))
            continue
        excluded.append({
            "reference_id": item.reference_id,
            "status": row.status if row is not None else "unverified",
            "reason": row.reason if row is not None else "no_teacher_assessment",
        })
    return rows, excluded


def compute_metrics(dataset: ReferenceDataset,
                    resolved: dict[str, ResolvedReference],
                    outcomes: dict[str, dict],
                    *, partition: str = "all",
                    include_unverified: bool = False) -> dict:
    """Compute the benchmark metrics for one set of raw outcomes.

    ``include_unverified`` exists for measurement against scheme-derived
    references and for infrastructure checks. Such a run is never certified;
    the runner records the basis and the report labels it explicitly.
    """
    rows, excluded = _select_rows(dataset, resolved, partition=partition,
                                  include_unverified=include_unverified)
    by_paper: dict[str, list[tuple[ReferenceItem, int, str]]] = {}
    for row in rows:
        by_paper.setdefault(row[0].paper_id, []).append(row)
    for paper_rows in by_paper.values():
        paper_rows.sort(key=lambda r: r[0].question)

    matched: list[dict] = []
    missing: list[dict] = []
    invalid: list[dict] = []
    allocation: list[dict] = []
    unmatched_model: list[dict] = []
    rollups: list[dict] = []

    scripts_attempted = len(by_paper)
    scripts_processed = 0
    scripts_failed: list[dict] = []
    review_flagged = 0
    grade_compared = 0
    grade_exact = 0
    total_eligible = 0
    total_exact = 0
    total_rows: list[dict] = []

    for paper_id in sorted(by_paper):
        paper_rows = by_paper[paper_id]
        outcome = outcomes.get(paper_id) or {}
        if _failed(outcome):
            scripts_failed.append({
                "paper_id": paper_id,
                "error": str(outcome.get("error") or _outcome_status(outcome)),
            })
            for item, _mark, _basis in paper_rows:
                missing.append({"paper_id": paper_id,
                                "reference_id": item.reference_id,
                                "question": item.question,
                                "reason": "script_not_processed"})
            continue

        scripts_processed += 1
        if bool(outcome.get("review_required")):
            review_flagged += 1

        questions = {canonical_label(k): v for k, v in
                     (outcome.get("questions") or {}).items()}
        subs = {canonical_label(k): v for k, v in
                (outcome.get("subquestions") or {}).items()}
        label_display = {canonical_label(k): str(k) for k in
                         (outcome.get("questions") or {})}
        label_display.update({canonical_label(k): str(k) for k in
                              (outcome.get("subquestions") or {})})

        matched_labels: set[str] = set()
        paper_matched: list[dict] = []
        paper_complete = True

        for item, teacher_mark, basis in paper_rows:
            label = canonical_label(item.question)
            entry: Any = None
            matched_by = ""

            if item.is_subquestion:
                entry = subs.get(label)
                if entry is not None:
                    matched_by = "exact_subquestion"
                else:
                    # Some stored outcomes put a per-part mark in the questions
                    # map under its exact label (``1a``). That is unambiguous;
                    # a whole-question roll-up (``1``) is not, and is never
                    # split across sub-parts.
                    entry = questions.get(label)
                    if entry is not None:
                        matched_by = "exact_subquestion_label"
            else:
                entry = questions.get(label)
                if entry is not None:
                    matched_by = "exact_question"
                else:
                    children = [key for key in subs
                                if question_stem(key) == question_stem(label)]
                    # Only sum children when this reference has no sibling
                    # subquestion references; otherwise the marks are already
                    # being compared one part at a time.
                    siblings = [r for r in paper_rows
                                if r[0].is_subquestion
                                and question_stem(r[0].question)
                                == question_stem(item.question)]
                    if children and not siblings:
                        child_marks = [_awarded(subs[key]) for key in children]
                        if any(mark is None for mark in child_marks):
                            entry = None
                        else:
                            entry = {"awarded": sum(child_marks),
                                     "max": sum(_num(subs[key].get("max")) or 0
                                                for key in children
                                                if isinstance(subs[key], dict))}
                            matched_by = "subquestion_sum"

            if entry is None:
                paper_complete = False
                missing.append({
                    "paper_id": paper_id,
                    "reference_id": item.reference_id,
                    "question": item.question,
                    "reason": ("no_mark_returned" if item.is_subquestion
                               else "no_question_mark_returned"),
                })
                continue

            awarded = _awarded(entry)
            declared_max = _declared_max(entry)
            if awarded is None:
                paper_complete = False
                invalid.append({"paper_id": paper_id,
                                "reference_id": item.reference_id,
                                "reason": "awarded_mark_missing_or_not_integer"})
                continue
            if awarded < 0 or awarded > item.max_marks:
                paper_complete = False
                invalid.append({"paper_id": paper_id,
                                "reference_id": item.reference_id,
                                "reason": f"awarded {awarded} outside 0..{item.max_marks}"})
                continue
            if declared_max is not None and declared_max != item.max_marks:
                allocation.append({
                    "paper_id": paper_id,
                    "reference_id": item.reference_id,
                    "scheme_says": item.max_marks,
                    "app_read": declared_max,
                })

            diff = awarded - teacher_mark
            row = {
                "reference_id": item.reference_id,
                "paper_id": paper_id,
                "paper_title": item.paper_title,
                "question": item.question,
                "subquestion": item.subquestion,
                "level": item.level,
                "matched_by": matched_by,
                "max_marks": item.max_marks,
                "teacher_mark": teacher_mark,
                "ai_mark": awarded,
                "difference": diff,
                "abs_difference": abs(diff),
                "direction": ("over" if diff > 0 else
                              "under" if diff < 0 else "exact"),
                "categories": list(item.categories),
                "scheme": list(item.scheme),
                "basis": item.basis,
                "certification_basis": basis,
                "review_required": bool(outcome.get("review_required")),
            }
            matched.append(row)
            paper_matched.append(row)
            matched_labels.add(label)

        # A question roll-up is not "invented" when all of its parts were just
        # compared; recording it as a matching error would punish a correct
        # nested-subquestion run.
        sub_refs = [r for r in paper_rows if r[0].is_subquestion]
        matched_sub_stems = {question_stem(r[0].question) for r in sub_refs
                             if canonical_label(r[0].question) in matched_labels}
        # Labels belonging to reference items that were deliberately excluded
        # from this (possibly partially verified) run are not "invented" by
        # the marker. The paper-level item list is authoritative here.
        all_paper_refs = dataset.paper(paper_id)
        known_labels = {canonical_label(i.question)
                        for i in (all_paper_refs.items if all_paper_refs else ())}
        for key in questions:
            if key in matched_labels:
                continue
            if key in matched_sub_stems:
                rollups.append({"paper_id": paper_id, "label": key,
                                "reason": "roll-up of matched subquestions"})
                continue
            if key in subs or key in known_labels:
                continue
            unmatched_model.append({"paper_id": paper_id,
                                    "label": label_display.get(key, key)})
        for key in subs:
            if key in matched_labels or key in known_labels:
                continue
            unmatched_model.append({"paper_id": paper_id,
                                    "label": label_display.get(key, key)})

        # Script total: only when EVERY reference item on the paper is present
        # in this evaluation (not a verified subset), every item matched, and
        # both totals are usable.
        all_paper_items = dataset.paper(paper_id)
        expected_ids = {i.reference_id for i in (all_paper_items.items
                                                 if all_paper_items else ())}
        included_ids = {r[0].reference_id for r in paper_rows}
        earned, available, grade = _total_of(outcome)
        teacher_total = sum(r[1] for r in paper_rows)
        teacher_max = sum(r[0].max_marks for r in paper_rows)

        if expected_ids == included_ids and paper_complete and earned is not None:
            total_eligible += 1
            available_note = None
            if available is not None and available != teacher_max:
                available_note = {"scheme_says": teacher_max, "app_read": available}
            exact_total = earned == teacher_total
            if exact_total:
                total_exact += 1
            total_rows.append({
                "paper_id": paper_id,
                "teacher_total": teacher_total,
                "ai_total": earned,
                "max_marks": teacher_max,
                "difference": earned - teacher_total,
                "exact": exact_total,
                "available_disagreement": available_note,
                "grade": grade,
            })
        if (all_paper_items is not None
                and expected_ids == included_ids and paper_complete
                and getattr(all_paper_items, "teacher_grade", None)):
            if available is not None and grade is not None:
                grade_compared += 1
                if grade == str(all_paper_items.teacher_grade):
                    grade_exact += 1

    matched_count = len(matched)
    question_rows = [r for r in matched if r["level"] == "question"]
    sub_rows = [r for r in matched if r["level"] == "subquestion"]
    exact = sum(1 for r in matched if r["difference"] == 0)
    question_exact = sum(1 for r in question_rows if r["difference"] == 0)
    sub_exact = sum(1 for r in sub_rows if r["difference"] == 0)
    over = [r for r in matched if r["difference"] > 0]
    under = [r for r in matched if r["difference"] < 0]
    errors = [r["abs_difference"] for r in matched]
    normalized = [r["abs_difference"] / r["max_marks"] for r in matched
                  if r["max_marks"] > 0]

    question_expected = sum(1 for row in rows if not row[0].is_subquestion)
    sub_expected = sum(1 for row in rows if row[0].is_subquestion)

    disagreement_groups: dict[str, int] = {}
    disagreements = []
    for row in matched:
        if row["difference"] == 0:
            continue
        hypothesis, confirmed = _hypothesis(row["categories"])
        disagreement_groups[hypothesis] = disagreement_groups.get(hypothesis, 0) + 1
        disagreements.append({
            **row,
            "error_category": hypothesis,
            "hypothesis": hypothesis if not confirmed
            else f"possible {hypothesis}; not root-caused",
            "root_cause_confirmed": False,
            "requires_human_investigation": True,
            "scheme_criterion": "; ".join(row["scheme"]),
        })
    disagreements.sort(key=lambda r: (r["direction"] != "over",
                                      -abs(r["difference"]),
                                      r["reference_id"]))

    by_paper_matched: dict[str, list[dict]] = {}
    for row in matched:
        by_paper_matched.setdefault(row["paper_id"], []).append(row)

    flagged_with_disagreement = sum(
        1 for paper_id, rows_ in by_paper_matched.items()
        if outcomes.get(paper_id, {}).get("review_required")
        and any(r["difference"] != 0 for r in rows_))
    flagged_without_disagreement = sum(
        1 for paper_id, rows_ in by_paper_matched.items()
        if outcomes.get(paper_id, {}).get("review_required")
        and not any(r["difference"] != 0 for r in rows_))

    excluded_reasons: dict[str, int] = {}
    for row in excluded:
        key = f"{row['status']}:{row['reason']}"
        excluded_reasons[key] = excluded_reasons.get(key, 0) + 1

    return {
        "definitions": DEFINITIONS,
        "sample": {
            "papers_expected": len({r[0].paper_id for r in rows}),
            "papers_attempted": scripts_attempted,
            "papers_processed": scripts_processed,
            "references_expected": len(rows),
            "questions_expected": question_expected,
            "subquestions_expected": sub_expected,
            "marks_available": sum(r[0].max_marks for r in rows),
            "certification_basis": ("teacher_verified" if not include_unverified
                                    else "scheme_reference_not_certified"),
            "dataset_version": dataset.version,
            "dataset_sha256": dataset.sha256,
            "partition": partition,
        },
        "agreement": {
            "items": _agreement(exact, matched_count),
            "questions": _agreement(question_exact, len(question_rows)),
            "subquestions": _agreement(sub_exact, len(sub_rows)),
            "total": {
                "exact": total_exact,
                "compared": total_eligible,
                "rate": _rate(total_exact, total_eligible),
                "ci95": wilson_interval(total_exact, total_eligible),
                "denominator": DEFINITIONS["exact_total_agreement"]["denominator"],
                "scripts": total_rows,
            },
        },
        "error": {
            "mean_absolute_error": round(statistics.mean(errors), 6)
            if errors else None,
            "normalized_mae": round(statistics.mean(normalized), 6)
            if normalized else None,
            "overmarking": {
                "count": len(over),
                "rate": _rate(len(over), matched_count),
                "mean_magnitude": round(statistics.mean(
                    r["difference"] for r in over), 6) if over else None,
            },
            "undermarking": {
                "count": len(under),
                "rate": _rate(len(under), matched_count),
                "mean_magnitude": round(statistics.mean(
                    -r["difference"] for r in under), 6) if under else None,
            },
        },
        "coverage": {
            "questions_processed": len(question_rows),
            "question_coverage": _rate(len(question_rows), question_expected),
            "subquestions_processed": len(sub_rows),
            "subquestion_coverage": _rate(len(sub_rows), sub_expected),
            "items_processed": matched_count,
            "item_coverage": _rate(matched_count, len(rows)),
            "missing_mark_count": len(missing),
            "invalid_result_count": len(invalid),
            "scripts_failed": len(scripts_failed),
            "schema_validation_failure_rate": _rate(len(scripts_failed),
                                                    scripts_attempted),
            "mark_allocation_disagreements": allocation,
            "unresolved_reference_count": sum(
                1 for row in excluded
                if row["status"] in {"disputed", "incomplete"}),
        },
        "review": {
            "review_flag_rate": _rate(review_flagged, scripts_processed),
            "flagged_scripts": review_flagged,
            "flagged_with_disagreement": flagged_with_disagreement,
            "flagged_without_disagreement": flagged_without_disagreement,
            "human_review_rate": _rate(len(disagreements), matched_count),
            "items_requiring_investigation": len(disagreements),
            "note": ("A review flag is not evidence that the mark is wrong; "
                     "flagged and unflagged disagreements are reported separately."),
        },
        "grades": {
            "compared": grade_compared,
            "exact": grade_exact,
            "rate": _rate(grade_exact, grade_compared),
            "available": grade_compared > 0,
            "reason": (None if grade_compared else
                       "no teacher-verified grade is recorded for the evaluated papers"),
        },
        "matching": {
            "unmatched_reference_items": missing,
            "unmatched_model_labels": unmatched_model,
            "rollup_labels": rollups,
            "invalid_marks": invalid,
            "allocation_disagreements": allocation,
        },
        "disagreements": disagreements,
        "disagreement_groups": disagreement_groups,
        "excluded": excluded,
        "excluded_reasons": excluded_reasons,
        "rows": matched,
    }


def _outcome_status(outcome: dict) -> str:
    return str(outcome.get("status") or "no usable outcome")
