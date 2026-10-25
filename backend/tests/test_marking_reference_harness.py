"""The accuracy harness must count every marking point it was given.

The end-to-end test of paper B found the harness reporting ``compared 2/4
items`` and three "no mark returned" errors for a correction whose stored
result contained 1a 1/1, 1b 1/1, 1c 1/2. The marks were there; the comparison
only ever read each question's roll-up. These tests pin the corrected
behaviour — no network, no model, no quota: they feed the comparison the shape
the app actually stores.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module")
def harness():
    path = ROOT / "backend" / "scripts"
    sys.path.insert(0, str(path))
    try:
        import verify_marking_reference as module
        yield module
    finally:
        sys.path.remove(str(path))
        sys.modules.pop("verify_marking_reference", None)


def _paper(*items, total=None):
    """A manifest-shaped paper: items name questions OR marking points."""
    rows = [{"question": q, "marks_available": m, "expected": e,
             "categories": []} for q, m, e in items]
    return {"id": "paper-x", "items": rows,
            "expected_total": total if total is not None
            else sum(r["expected"] for r in rows),
            "marks_available": sum(r["marks_available"] for r in rows)}


def test_nested_subquestions_are_compared_item_by_item(harness):
    """The observed paper-b shape: one question with three marking points."""
    paper = _paper(("1a", 1, 1), ("1b", 1, 1), ("1c", 2, 1), ("2", 3, 3))
    comparison = harness.compare_paper(
        paper,
        {"1": {"awarded": 3, "max": 4}, "2": {"awarded": 3, "max": 3}},
        {"1a": {"awarded": 1, "max": 1, "question": "1"},
         "1b": {"awarded": 1, "max": 1, "question": "1"},
         "1c": {"awarded": 1, "max": 2, "question": "1"}})
    assert comparison["items_in_reference"] == 4
    assert comparison["items_compared"] == 4
    assert comparison["exact_agreement"] == 4
    assert comparison["over_marked"] == [] and comparison["under_marked"] == []
    # The roll-up is the same marks again, never an invented question.
    assert comparison["matching_errors"] == {"unmatched_model": [],
                                             "unmatched_manifest": []}
    assert comparison["rollup_labels"] == {"1": ["1a", "1b", "1c"]}
    assert comparison["subquestions_compared"] == 3


def test_a_mix_of_questions_with_and_without_subquestions(harness):
    paper = _paper(("3a", 1, 1), ("3b", 2, 2), ("4", 3, 2))
    comparison = harness.compare_paper(
        paper,
        {"3": {"awarded": 3, "max": 3}, "4": {"awarded": 2, "max": 3}},
        {"3a": {"awarded": 1, "max": 1, "question": "3"},
         "3b": {"awarded": 2, "max": 2, "question": "3"}})
    assert comparison["items_compared"] == 3
    assert comparison["exact_agreement"] == 3
    assert {row["question"] for row in comparison["rows"]} == {"3a", "3b", "4"}


def test_a_missing_mark_on_a_real_nested_item_is_named(harness):
    paper = _paper(("1a", 1, 1), ("1b", 1, 1), ("1c", 2, 1), ("2", 3, 3))
    comparison = harness.compare_paper(
        paper,
        {"1": {"awarded": 3, "max": 4}, "2": {"awarded": 3, "max": 3}},
        {"1a": {"awarded": 1, "max": 1, "question": "1"},
         "1b": {"awarded": 1, "max": 1, "question": "1"},
         "1c": {"awarded": 2, "max": 2, "question": "1"}})
    assert comparison["items_compared"] == 4
    assert comparison["exact_agreement"] == 3
    assert [row["question"] for row in comparison["over_marked"]] == ["1c"]
    assert comparison["over_marked"][0]["difference"] == 1


def test_missing_subquestion_rows_fall_back_to_the_group_without_counting_twice(
        harness):
    """An older result with no breakdown still compares, as a GROUP total."""
    paper = _paper(("1a", 1, 1), ("1b", 1, 1), ("1c", 2, 1))
    comparison = harness.compare_paper(paper, {"1": {"awarded": 3, "max": 4}})
    assert comparison["items_in_reference"] == 3
    assert comparison["items_compared"] == 1      # one group total, not three
    assert comparison["rows"][0]["matched_by"] == "group"
    assert comparison["matching_errors"]["unmatched_model"] == []
    # The group is NOT silently counted as three exact agreements.
    assert comparison["exact_agreement"] == 1


def test_a_top_level_only_paper_is_unchanged(harness):
    """The pre-existing behaviour this harness was written for."""
    paper = _paper(("1", 3, 3), ("2", 2, 2))
    comparison = harness.compare_paper(paper, {"1": {"awarded": 3, "max": 3},
                                               "2": {"awarded": 2, "max": 2}})
    assert comparison["items_compared"] == 2
    assert comparison["exact_agreement"] == 2
    assert comparison["subquestions_compared"] == 0
    assert comparison["rollup_labels"] == {}


def test_a_mislabelled_marking_point_is_reported_not_folded(harness):
    """The app returned 1b where the key says 1a: that is a mismatch twice."""
    paper = _paper(("1a", 1, 1), ("2", 3, 3))
    comparison = harness.compare_paper(
        paper,
        {"2": {"awarded": 3, "max": 3}},
        {"1b": {"awarded": 1, "max": 1, "question": "1"}})
    assert comparison["items_compared"] == 1
    assert comparison["matching_errors"]["unmatched_manifest"] == ["1a"]
    assert comparison["matching_errors"]["unmatched_model"] == ["1b"]


def test_the_same_marking_point_is_never_counted_twice(harness):
    """Adding subquestion rows must not change what the two questions add to."""
    paper = _paper(("1a", 1, 1), ("1b", 1, 1), ("2", 3, 3))
    comparison = harness.compare_paper(
        paper,
        {"1": {"awarded": 2, "max": 2}, "2": {"awarded": 3, "max": 3}},
        {"1a": {"awarded": 1, "max": 1, "question": "1"},
         "1b": {"awarded": 1, "max": 1, "question": "1"}})
    assert comparison["items_compared"] == 3
    assert sum(row["awarded"] for row in comparison["rows"]) == 5
    assert sum(row["expected"] for row in comparison["rows"]) == 5
    assert comparison["exact_agreement"] == 3
