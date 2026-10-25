"""Regression tests for the false-100% failure mode.

Live evidence (TEST 3, 2026-10-24): ``gemini-3.5-flash-lite`` returned 103/103
for a paper with wrong and blank answers.  Question 11 was returned as
``student_answer: "Blank on page, implied complete"`` with 4/4.  The
"independent" second correction also fell back to the SAME model and the pair
was stored as MATCHING / review_required=0.

These tests pin the three deterministic protections added for it:
1. an explicitly absent answer cannot be awarded marks;
2. a lightweight model cannot be the marking model;
3. a second pass using the first pass's model is never called independent.
"""

from __future__ import annotations

import pytest

from app.engine.mark_integrity import enforce_evidence


def _result(**overrides):
    question = {
        "question_number": "11",
        "max_marks": 4,
        "awarded_marks": 4,
        "student_answer": "Blank on page, implied complete",
        "working_summary": "Complete method following standard pattern",
        "loss_reason": "None",
        "mark_breakdown": [
            {"code": "M2", "awarded": True, "reason": "method"},
            {"code": "M1", "awarded": True, "reason": "method"},
            {"code": "A1", "awarded": True, "reason": "answer"},
        ],
    }
    question.update(overrides)
    return {"questions": [question], "uncertainties": []}


class TestAbsentAnswers:
    def test_a_blank_answer_cannot_earn_marks(self):
        result, report = enforce_evidence(_result())
        q = result["questions"][0]
        assert q["awarded_marks"] == 0
        assert q["status"] == "REVIEW"
        assert "Blank" in q["review_reason"]
        assert report["changed"][0]["from"] == 4
        assert report["review_questions"] == ["11"]

    @pytest.mark.parametrize("answer", [
        "Unattempted", "not attempted", "No answer", "illegible",
        "unreadable", "cannot be read", "omitted", "implied complete",
    ])
    def test_every_explicit_absence_phrase_is_recognised(self, answer):
        result, _ = enforce_evidence(_result(student_answer=answer))
        assert result["questions"][0]["awarded_marks"] == 0

    def test_a_blank_answer_with_zero_marks_is_unchanged(self):
        result, report = enforce_evidence(
            _result(student_answer="Unattempted", awarded_marks=0,
                    mark_breakdown=[]))
        q = result["questions"][0]
        assert q["awarded_marks"] == 0
        assert "status" not in q
        assert report["changed"] == []

    def test_a_parent_row_with_one_blank_part_keeps_the_other_parts_marks(self):
        result, report = enforce_evidence(_result(
            student_answer="16125 | Unattempted", awarded_marks=3,
            max_marks=6, loss_reason="part (b) blank",
            mark_breakdown=[],
            subquestions=[
                {"id": "15a", "awarded_marks": 3, "max_marks": 3},
                {"id": "15b", "awarded_marks": 0, "max_marks": 3},
            ]))
        q = result["questions"][0]
        assert q["awarded_marks"] == 3
        assert report["changed"] == []

    def test_empty_metadata_is_not_treated_as_a_blank_page(self):
        """An empty field is missing metadata, not proof the page is blank."""
        result, report = enforce_evidence(
            _result(student_answer="", mark_breakdown=[]))
        q = result["questions"][0]
        assert q["awarded_marks"] == 4
        assert "status" not in q
        assert report["changed"] == []


class TestNumericContradiction:
    def test_full_marks_contradicted_by_the_markers_own_solution(self):
        result, report = enforce_evidence(_result(
            student_answer="4780", awarded_marks=3, max_marks=3,
            loss_reason="None", correct_method="5200 * 1.025^4",
            full_mark_solution="5200 * 1.025^4 = 5739.81... ≈ 5740",
            mark_breakdown=[
                {"code": "M2", "awarded": True, "reason": "method"},
                {"code": "A1", "awarded": True, "reason": "answer"},
            ]))
        q = result["questions"][0]
        assert q["awarded_marks"] == 2
        assert q["status"] == "REVIEW"
        assert q["mark_breakdown"][-1]["awarded"] is False
        assert report["changed"][0]["from"] == 3
        assert report["changed"][0]["to"] == 2

    def test_a_small_answer_mismatch_is_flagged_but_not_reduced(self):
        result, report = enforce_evidence(_result(
            student_answer="8.6", awarded_marks=4, max_marks=4,
            loss_reason="None", full_mark_solution="16.28",
            mark_breakdown=[
                {"code": "M2", "awarded": True, "reason": "method"},
                {"code": "M1", "awarded": True, "reason": "method"},
                {"code": "A1", "awarded": True, "reason": "answer"},
            ]))
        q = result["questions"][0]
        assert q["awarded_marks"] == 4  # never penalise an equivalent form
        assert q["status"] == "REVIEW"
        assert "review" in q["review_reason"].lower()
        assert report["changed"] == []

    def test_rounding_within_tolerance_is_not_a_contradiction(self):
        result, report = enforce_evidence(_result(
            student_answer="8437", awarded_marks=3, max_marks=3,
            loss_reason="None", full_mark_solution="7500 * 1.04^3 = 8436.48 ≈ 8436",
            mark_breakdown=[
                {"code": "M2", "awarded": True, "reason": "method"},
                {"code": "A1", "awarded": True, "reason": "answer"},
            ]))
        assert result["questions"][0]["awarded_marks"] == 3
        assert report["changed"] == []

    def test_a_partial_award_is_never_reduced_by_this_check(self):
        result, report = enforce_evidence(_result(
            student_answer="44220", awarded_marks=2, max_marks=3,
            loss_reason="Arithmetic error.",
            full_mark_solution="40000 * 1.035^3 = 44349",
            mark_breakdown=[
                {"code": "M2", "awarded": True, "reason": "method"},
                {"code": "A1", "awarded": False, "reason": "answer"},
            ]))
        assert result["questions"][0]["awarded_marks"] == 2
        assert report["changed"] == []


class TestContradictoryEvidence:
    def test_full_marks_with_a_missed_marking_point_needs_review(self):
        result, _ = enforce_evidence(_result(
            student_answer="16125", loss_reason="None",
            mark_breakdown=[
                {"code": "M1", "awarded": True, "reason": "method"},
                {"code": "A1", "awarded": False, "reason": "wrong"},
            ]))
        q = result["questions"][0]
        assert q["awarded_marks"] == 4  # the guard never invents a mark
        assert q["status"] == "REVIEW"
        assert "breakdown" in q["review_reason"]

    def test_partial_marks_described_as_lossless_needs_review(self):
        result, _ = enforce_evidence(_result(
            student_answer="16125", awarded_marks=2,
            loss_reason="None. Full marks awarded."))
        q = result["questions"][0]
        assert q["awarded_marks"] == 2
        assert q["status"] == "REVIEW"

    def test_a_consistent_result_is_untouched(self):
        result, report = enforce_evidence(_result(
            student_answer="16125", loss_reason="None",
            mark_breakdown=[
                {"code": "M1", "awarded": True, "reason": "method"},
                {"code": "A1", "awarded": True, "reason": "answer"},
            ]))
        q = result["questions"][0]
        assert q["awarded_marks"] == 4
        assert "status" not in q
        assert report["changed"] == []

    def test_a_legitimate_partial_with_reason_is_untouched(self):
        result, _ = enforce_evidence(_result(
            student_answer="8933", awarded_marks=2,
            loss_reason="Gave the value instead of the interest.",
            mark_breakdown=[
                {"code": "M2", "awarded": True, "reason": "method"},
                {"code": "A1", "awarded": False, "reason": "wrong"},
            ]))
        assert result["questions"][0]["awarded_marks"] == 2
        assert "status" not in result["questions"][0]


class TestModelTrust:
    def test_lightweight_models_are_not_trusted_for_marking(self):
        from app.config import is_trusted_marking_model

        assert not is_trusted_marking_model("gemini-3.5-flash-lite")
        assert not is_trusted_marking_model("gemini-3.8-flash-lite")
        assert is_trusted_marking_model("gemini-3.6-flash")
        assert not is_trusted_marking_model("")

    def test_grade_refuses_a_chain_of_only_lightweight_models(self):
        from app.engine import legacy_marking as legacy

        class _NeverCalled:
            class models:
                @staticmethod
                def generate_content(**kwargs):
                    raise AssertionError("a lightweight model must not be called")

        with pytest.raises(RuntimeError, match="No full-capability"):
            legacy.grade_with_model(
                client=_NeverCalled(),
                model_names=["gemini-3.5-flash-lite"],
                student_file="student",
                mark_scheme_file="scheme",
            )


class TestDoubleCheckIndependence:
    def _run(self, **kwargs):
        from app.engine import double_check
        return double_check.combine(
            {"questions": [{"question_number": "1", "awarded_marks": 3,
                            "max_marks": 3, "loss_reason": "None"}]},
            {"questions": [{"question_number": "1", "awarded_marks": 3,
                            "max_marks": 3, "loss_reason": "None"}]},
            **kwargs)

    def test_the_same_model_cannot_confirm_itself(self):
        from app.engine import double_check
        out = self._run(first_marking_model="m1",
                        second_marking_model="m1")
        assert out["review_required"] is True
        assert out["status"] == double_check.STATUS_REVIEW_REQUIRED
        assert out["disagreement_status"] == \
            double_check.DISAGREE_NOT_INDEPENDENT
        assert "same model" in out["reason"]
        # The marks are preserved; only the trust verdict changes.
        assert out["final"] == (3, 3, 100.0)

    def test_two_different_models_can_confirm(self):
        from app.engine import double_check
        out = self._run(first_marking_model="m1",
                        second_marking_model="m2")
        assert out["review_required"] is False
        assert out["disagreement_status"] == double_check.DISAGREE_MATCHING

    def test_a_lightweight_second_pass_is_not_independent(self):
        from app.engine import double_check
        out = self._run(first_marking_model="gemini-3.6-flash",
                        second_marking_model="gemini-3.5-flash-lite")
        assert out["review_required"] is True
        assert out["disagreement_status"] == \
            double_check.DISAGREE_NOT_INDEPENDENT
