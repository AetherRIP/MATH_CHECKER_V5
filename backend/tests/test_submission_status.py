"""Centralized submission-status logic (spec 31-35, §47).

One resolver decides what a submission MEANS - reports, dashboard, class
pages and student profiles all read its verdict, so this suite locks the
priority chain itself: MISSING/DRAFT/grade-0 are OUTSTANDING, grade 1 is
GRADE_ONE, and a submitted grade of 2+ is never missing.
"""

from __future__ import annotations

import pytest

from app.submission_status import (
    GRADE_ONE,
    OUTSTANDING,
    PENDING,
    SUBMITTED,
    grade_of,
    resolve,
    sql_outstanding,
)


class TestPriorityChain:
    """The §47 table, row by row."""

    @pytest.mark.parametrize("status", ["MISSING", "NOT_STARTED", "RECLAIMED"])
    def test_classroom_missing_states_are_outstanding(self, status):
        track = resolve(status, None)
        assert track.status == OUTSTANDING
        assert track.original_status == status
        assert track.outstanding

    def test_draft_is_outstanding_with_its_draft_origin(self):
        track = resolve("CREATED", None)
        assert track.status == OUTSTANDING
        assert track.original_status == "CREATED"
        assert track.label == "Draft"

    def test_absent_submission_is_outstanding(self):
        assert resolve(None, None).status == OUTSTANDING
        assert resolve("", None).status == OUTSTANDING

    def test_grade_zero_is_outstanding_and_keeps_the_grade(self):
        track = resolve("GRADED", 0)
        assert track.status == OUTSTANDING
        assert track.original_status == "GRADED"
        assert track.original_grade == 0.0
        assert track.label == "Grade 0"

    def test_grade_zero_on_a_corrected_result_is_outstanding(self):
        assert resolve("CORRECTED", 0.0).status == OUTSTANDING

    def test_grade_one_is_grade_one(self):
        track = resolve("GRADED", 1)
        assert track.status == GRADE_ONE
        assert track.label == "Grade 1"

    @pytest.mark.parametrize("grade", [2, 5, 9, 10, 7.5])
    def test_grade_two_plus_is_submitted_never_missing(self, grade):
        track = resolve("GRADED", grade)
        assert track.status == SUBMITTED
        assert not track.outstanding

    @pytest.mark.parametrize("grade", [1, 2, 3, 4, 5, 6, 7, 8, 9])
    def test_low_grades_are_not_missing(self, grade):
        # §33: only 0 is outstanding - a real grade of 1+ was submitted.
        assert not resolve("SUBMITTED", grade).outstanding

    def test_handed_in_without_a_grade_is_pending_not_missing(self):
        track = resolve("SUBMITTED", None)
        assert track.status == PENDING
        assert not track.outstanding
        assert track.label == "Submitted, not yet graded"

    def test_missing_wins_over_a_stale_grade(self):
        # §34 order: state is checked before the grade.
        track = resolve("MISSING", 0)
        assert track.status == OUTSTANDING
        assert track.original_status == "MISSING"

    def test_unverified_data_is_never_accused(self):
        # §35: do not invent missing from an absence of data.
        track = resolve("UNKNOWN", None)
        assert not track.outstanding
        assert track.status == PENDING

    @pytest.mark.parametrize("status", [
        "SUBMITTED", "SUBMITTED_LATE", "RETURNED", "EDITED_AFTER_SUBMISSION",
        "GRADED", "CORRECTED", "PROCESSING", "FAILED", "INVALID",
    ])
    def test_handed_in_family_never_reads_outstanding_without_reason(self, status):
        track = resolve(status, 4)
        assert track.status == SUBMITTED
        assert not track.outstanding


class TestGradeOf:
    """Math Correct's mark outranks Classroom's assigned grade (§35)."""

    def test_mc_result_wins(self):
        assert grade_of({"mc_earned": 7, "assigned_grade": 3}) == 7.0

    def test_result_dict_is_read(self):
        assert grade_of({"result": {"earned": 5}, "assigned_grade": 9}) == 5.0

    def test_classroom_grade_is_the_fallback(self):
        assert grade_of({"assigned_grade": 8}) == 8.0

    def test_nothing_recorded_is_none(self):
        assert grade_of({}) is None
        assert grade_of({"assigned_grade": None}) is None


class TestSqlTwin:
    """sql_outstanding must agree with resolve row-for-row (§48)."""

    def test_expression_mentions_both_rules(self):
        sql = sql_outstanding("cs")
        assert "CREATED" in sql and "RECLAIMED_BY_STUDENT" in sql
        assert "assigned_grade" in sql and "results" in sql
        assert "data_quality" in sql

    @pytest.mark.parametrize("status,grade,expected", [
        ("MISSING", None, True),
        ("CREATED", None, True),
        ("GRADED", 0, True),
        ("GRADED", 1, False),
        ("GRADED", 5, False),
        ("SUBMITTED", None, False),
    ])
    def test_resolver_table(self, status, grade, expected):
        assert resolve(status, grade).outstanding is expected
