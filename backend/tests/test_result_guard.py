"""Result guard + configurable grading tests (§7/§8/§13/§23).

These lock the two behaviours that caused the previous correction bugs:
a valid stored result must never be silently replaced by a suspicious one,
and a grade must be a deterministic lookup, never AI-invented.
"""

from app.engine import grading, result_guard as rg


def _result(questions):
    return {"questions": questions}


def _q(number, awarded, maximum):
    return {"question_number": number, "awarded_marks": awarded,
            "max_marks": maximum, "mark_breakdown": [{"awarded": True}]}


def _verification(status="VALID", review=None, anomalies=None, warnings=None):
    return {"status": status, "review_questions": review or [],
            "anomalies": anomalies or [], "warnings": warnings or []}


class TestConfidence:
    def test_full_marks_is_high(self):
        result = _result([_q(1, 5, 5), _q(2, 5, 5)])
        level, reasons = rg.assess_confidence(result, _verification(), 10, 10)
        assert level == rg.HIGH
        assert reasons

    def test_partial_marks_is_high(self):
        result = _result([_q(1, 3, 5), _q(2, 4, 5)])
        level, _ = rg.assess_confidence(result, _verification(), 7, 10)
        assert level == rg.HIGH

    def test_zero_score_is_valid_not_failed(self):
        """A genuine zero (answered nothing) must NOT be flagged as failure."""
        result = _result([_q(1, 0, 5), _q(2, 0, 5)])
        level, _ = rg.assess_confidence(result, _verification(), 0, 10)
        assert level == rg.HIGH

    def test_impossible_mark_fails(self):
        result = _result([_q(1, 8, 5)])
        level, reasons = rg.assess_confidence(result, _verification(), 8, 5)
        assert level == rg.VALIDATION_FAILED
        assert any("exceed" in r for r in reasons)

    def test_missing_questions_fails(self):
        level, _ = rg.assess_confidence(_result([]), _verification(), 0, 0)
        assert level == rg.VALIDATION_FAILED

    def test_review_signal_is_low(self):
        result = _result([_q(1, 2, 5)])
        verification = _verification(status="REVIEW", review=["3"])
        level, reasons = rg.assess_confidence(result, verification, 2, 5)
        assert level == rg.LOW
        assert any("review" in r.lower() for r in reasons)

    def test_warning_is_medium(self):
        result = _result([_q(1, 2, 5)])
        verification = _verification(warnings=["question 1 max corrected"])
        level, _ = rg.assess_confidence(result, verification, 2, 5)
        assert level == rg.MEDIUM

    def test_primary_model_result_stays_high(self):
        """Naming the model that marked must not weaken a normal result."""
        result = _result([_q(1, 5, 5)])
        level, reasons = rg.assess_confidence(
            result, _verification(), 5, 5,
            marking_model="gemini-3.8-flash",
            primary_model="gemini-3.8-flash")
        assert level == rg.HIGH
        assert not any("instead of the configured primary" in r for r in reasons)

    def test_fallback_model_is_never_a_routine_high(self):
        """A fallback model marks the same paper differently — say so.

        Measured on one 15-page scan against a 26-page scheme: the primary
        gave 70/103 and the fallback 76/103, differing on 10 of 31 questions.
        A repeat run on a healthy primary would therefore not reproduce this
        total, so the marks are kept and the result is never HIGH.

        It is MEDIUM rather than LOW: the validated marking produced no fault,
        and a model switch is a fact about the RUN, not evidence against these
        marks. Treating it as LOW queued 11 of 15 stored papers for a teacher
        who had nothing to decide. The full explanation still travels with the
        result, and the next step (re-run on the primary) is spelled out.
        """
        result = _result([_q(1, 5, 5)])
        level, reasons = rg.assess_confidence(
            result, _verification(), 5, 5,
            marking_model="gemini-3.6-flash",
            primary_model="gemini-3.8-flash")
        assert level == rg.MEDIUM
        assert any("instead of the configured primary" in r for r in reasons)
        assert any("model switch is the only caveat" in r for r in reasons)

    def test_model_switch_cannot_upgrade_a_low_result(self):
        result = _result([_q(1, 2, 5)])
        verification = _verification(status="REVIEW", review=["3"])
        level, _ = rg.assess_confidence(
            result, verification, 2, 5,
            marking_model="gemini-3.6-flash",
            primary_model="gemini-3.8-flash")
        assert level == rg.LOW


class TestOverwriteProtection:
    def test_insert_when_nothing_stored(self):
        decision = rg.evaluate_overwrite(
            existing=None, earned=5, available=10, verification=_verification(),
            confidence=rg.HIGH, confidence_reasons=[])
        assert decision.action == rg.ACTION_INSERT

    def test_valid_replacement_updates(self):
        existing = {"id": "r1", "earned": 6, "available": 10, "manual_override": 0}
        decision = rg.evaluate_overwrite(
            existing=existing, earned=7, available=10, verification=_verification(),
            confidence=rg.HIGH, confidence_reasons=[])
        assert decision.action == rg.ACTION_UPDATE

    def test_zero_never_overwrites_valid(self):
        """The motivating bug: 67/103 must not become 0/103 silently."""
        existing = {"id": "r1", "earned": 67, "available": 103, "manual_override": 0}
        decision = rg.evaluate_overwrite(
            existing=existing, earned=0, available=103, verification=_verification(),
            confidence=rg.HIGH, confidence_reasons=[])
        assert decision.action == rg.ACTION_KEEP_EXISTING
        assert decision.review_required
        assert decision.confidence == rg.LARGE_SCORE_CHANGE

    def test_large_drop_keeps_existing(self):
        existing = {"id": "r1", "earned": 90, "available": 100, "manual_override": 0}
        decision = rg.evaluate_overwrite(
            existing=existing, earned=50, available=100, verification=_verification(),
            confidence=rg.HIGH, confidence_reasons=[])
        assert decision.action == rg.ACTION_KEEP_EXISTING

    def test_validation_failure_keeps_existing(self):
        existing = {"id": "r1", "earned": 40, "available": 100, "manual_override": 0}
        decision = rg.evaluate_overwrite(
            existing=existing, earned=45, available=100, verification=_verification(),
            confidence=rg.VALIDATION_FAILED, confidence_reasons=["bad"])
        assert decision.action == rg.ACTION_KEEP_EXISTING

    def test_manual_override_wins(self):
        existing = {"id": "r1", "earned": 40, "available": 100, "manual_override": 1}
        decision = rg.evaluate_overwrite(
            existing=existing, earned=42, available=100, verification=_verification(),
            confidence=rg.HIGH, confidence_reasons=[])
        assert decision.action == rg.ACTION_KEEP_EXISTING

    def test_force_overrides_manual(self):
        existing = {"id": "r1", "earned": 40, "available": 100, "manual_override": 1}
        decision = rg.evaluate_overwrite(
            existing=existing, earned=42, available=100, verification=_verification(),
            confidence=rg.HIGH, confidence_reasons=[], force=True)
        assert decision.action == rg.ACTION_UPDATE


class TestGrading:
    def test_default_boundaries_match_legacy(self):
        assert grading.grade_for_percentage(92, None) == "9"
        assert grading.grade_for_percentage(50, None) == "5"

    def test_configured_boundaries(self):
        bounds = [("1", 0), ("4", 45), ("7", 75), ("9", 95)]
        assert grading.grade_for_percentage(44, bounds) == "1"
        assert grading.grade_for_percentage(45, bounds) == "4"
        assert grading.grade_for_percentage(80, bounds) == "7"
        assert grading.grade_for_percentage(96, bounds) == "9"

    def test_table_without_a_zero_floor_cannot_gift_a_high_grade(self):
        """Regression: a low score must never inherit the table's lowest label.

        A teacher who lists ONLY the top bands (a realistic misconfiguration —
        the Settings page accepts any table) previously handed a 20% paper a
        "7", because a score below the lowest boundary simply kept the first
        entry in the list. Such a score now falls back to the preserved legacy
        percentage bands, so the error can only ever be too low, never a grade
        the student did not earn.
        """
        top_only = [("7", 75), ("8", 85), ("9", 95)]
        assert grading.grade_for_percentage(20, top_only) == "2"
        assert grading.grade_for_percentage(0, top_only) == "1"
        # At or above the lowest configured boundary the table still wins.
        assert grading.grade_for_percentage(75, top_only) == "7"
        assert grading.grade_for_percentage(96, top_only) == "9"

    def test_grade_for_totals(self):
        grade, pct = grading.grade_for_totals(67, 103)
        assert grade in "123456789"
        assert round(pct, 1) == 65.0

    def test_zero_available_is_safe(self):
        grade, pct = grading.grade_for_totals(0, 0)
        assert grade == "1"
        assert pct == 0.0

    def test_describe_boundaries(self):
        text = grading.describe_boundaries([("9", 90), ("1", 0)])
        assert "9>=90%" in text and "1>=0%" in text
