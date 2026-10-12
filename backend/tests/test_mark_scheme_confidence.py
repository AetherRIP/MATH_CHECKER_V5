"""Mark-scheme verification confidence + automatic suspicion detection (§19/§26/§27).

The parser must never let a broken structure look authoritative. These tests
pin every deterministic trigger from the spec's suspicion list, and prove the
important negative: a clean structure does NOT ask Gemini for anything.
"""

from __future__ import annotations

from app.engine import blueprint as bp


def _report(**over):
    base = bp.structure_report({"questions": [
        {"question_number": str(n), "max_marks": 5} for n in range(1, 13)
    ]})
    base.update(over)
    return base


class TestCleanStructureNeedsNoVerification:
    """§27: a confident, consistent parse must not spend an API request."""

    def test_a_complete_paper_is_high_confidence(self):
        out = bp.mark_scheme_confidence(_report(), declared_total=60)
        assert out["confidence"] == bp.CONFIDENCE_HIGH
        assert out["requires_verification"] is False
        assert out["reasons"] == []

    def test_declared_total_matching_the_sum_is_accepted(self):
        report = bp.structure_report({"questions": [
            {"question_number": "1", "max_marks": 5},
            {"question_number": "2", "max_marks": 7},
        ]})
        out = bp.mark_scheme_confidence(report, declared_total=12)
        assert out["requires_verification"] is False


class TestSuspiciousStructureIsCaught:
    """§26: every listed symptom must raise a verification prompt."""

    def test_no_questions_detected(self):
        out = bp.mark_scheme_confidence(bp.structure_report(None))
        assert out["requires_verification"] is True
        assert any("No questions" in r for r in out["reasons"])

    def test_zero_total_marks(self):
        report = bp.structure_report(
            {"questions": [{"question_number": "1", "max_marks": 0}]})
        out = bp.mark_scheme_confidence(report)
        assert out["requires_verification"] is True
        assert any("zero or missing" in r for r in out["reasons"])

    def test_one_question_across_a_multi_mark_paper(self):
        report = bp.structure_report(
            {"questions": [{"question_number": "1", "max_marks": 75}]})
        out = bp.mark_scheme_confidence(report)
        assert out["requires_verification"] is True
        assert any("incomplete" in r for r in out["reasons"])

    def test_missing_question_numbers(self):
        report = bp.structure_report({"questions": [
            {"question_number": "1", "max_marks": 5},
            {"question_number": "4", "max_marks": 5},
        ]})
        out = bp.mark_scheme_confidence(report)
        assert out["requires_verification"] is True
        assert any("Missing question numbers" in r for r in out["reasons"])

    def test_duplicate_question_numbers(self):
        report = bp.structure_report({"questions": [
            {"question_number": "1", "max_marks": 5},
            {"question_number": "1", "max_marks": 5},
        ]})
        out = bp.mark_scheme_confidence(report)
        assert out["requires_verification"] is True
        assert any("Duplicate" in r for r in out["reasons"])

    def test_total_smaller_than_question_count_is_impossible(self):
        report = bp.structure_report({"questions": [
            {"question_number": str(n), "max_marks": 0} for n in range(1, 6)
        ]})
        out = bp.mark_scheme_confidence(report)
        assert out["requires_verification"] is True

    def test_detected_total_disagreeing_with_the_stated_total(self):
        """§20 cross-check: never silently continue on a mismatch."""
        out = bp.mark_scheme_confidence(_report(), declared_total=75)
        assert out["requires_verification"] is True
        assert any("stated total of 75" in r for r in out["reasons"])

    def test_disagreeing_verification_passes(self):
        """§21: two passes that disagree need a human."""
        out = bp.mark_scheme_confidence(_report(), declared_total=60,
                                        disagreement=True)
        assert out["requires_verification"] is True
        assert any("disagreed" in r for r in out["reasons"])