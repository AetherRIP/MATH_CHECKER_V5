"""Regression tests: the preserved legacy engine must behave identically to
the friend's original implementation (V5 §25)."""

import pytest

from app.engine import legacy_marking as legacy


class TestNormaliseResult:
    def test_full_pass_through(self):
        data = {
            "paper_info": {"qualification": "Q", "paper": "1H", "session": "May 2024", "max_marks": 10},
            "questions": [
                {"question_number": "1", "max_marks": 4, "awarded_marks": 3,
                 "topic": "Algebra", "student_answer": "x=2", "working_summary": "ok",
                 "loss_reason": "sign slip",
                 "mark_breakdown": [{"code": "M1", "awarded": True, "reason": "method"}],
                 "correct_method": "…", "full_mark_solution": "…"},
                {"question_number": "2", "max_marks": 6, "awarded_marks": 6,
                 "topic": "Geometry", "student_answer": "150", "working_summary": "ok",
                 "loss_reason": "", "mark_breakdown": [],
                 "correct_method": "…", "full_mark_solution": "…"},
            ],
            "overall_notes": {"summary": "good", "strengths": ["algebra"],
                              "revision_areas": ["care"]},
            "uncertainties": ["q2 handwriting"],
        }
        result = legacy.normalise_result(data)
        assert len(result["questions"]) == 2
        assert result["questions"][0]["awarded_marks"] == 3
        assert result["questions"][1]["awarded_marks"] == 6
        assert result["paper_info"]["max_marks_declared"] == 10
        assert result["overall_notes"]["strengths"] == ["algebra"]
        assert result["uncertainties"] == ["q2 handwriting"]

    def test_clamps_over_awarded_marks(self):
        data = {"questions": [{"question_number": "1", "max_marks": 3, "awarded_marks": 99}]}
        result = legacy.normalise_result(data)
        assert result["questions"][0]["awarded_marks"] == 3  # legacy clamp

    def test_clamps_negative_marks(self):
        data = {"questions": [{"question_number": "1", "max_marks": 3, "awarded_marks": -5}]}
        result = legacy.normalise_result(data)
        assert result["questions"][0]["awarded_marks"] == 0

    def test_non_integer_marks_become_zero(self):
        data = {"questions": [{"question_number": "1", "max_marks": "3", "awarded_marks": "lots"}]}
        result = legacy.normalise_result(data)
        assert result["questions"][0]["max_marks"] == 3
        assert result["questions"][0]["awarded_marks"] == 0

    def test_sorts_questions_numerically_as_strings(self):
        data = {"questions": [
            {"question_number": "10", "max_marks": 5, "awarded_marks": 5},
            {"question_number": "2", "max_marks": 5, "awarded_marks": 5},
        ]}
        result = legacy.normalise_result(data)
        numbers = [q["question_number"] for q in result["questions"]]
        assert numbers == sorted(numbers)  # legacy: string sort

    def test_malformed_questions_skipped(self):
        data = {"questions": ["garbage", {"question_number": "1", "max_marks": 2, "awarded_marks": 1}, 42]}
        result = legacy.normalise_result(data)
        assert len(result["questions"]) == 1

    def test_defaults_applied(self):
        result = legacy.normalise_result({})
        assert result["paper_info"]["qualification"] == "Pearson Edexcel International GCSE Mathematics"
        assert result["questions"] == []
        assert result["overall_notes"]["summary"] == ""


class TestTotals:
    def test_calculate_totals_legacy_arithmetic(self):
        result = {"questions": [
            {"awarded_marks": 3, "max_marks": 4},
            {"awarded_marks": 6, "max_marks": 6},
            {"awarded_marks": 0, "max_marks": 5},
        ]}
        earned, available, percentage = legacy.calculate_totals(result)
        assert (earned, available) == (9, 15)
        assert percentage == 60.0

    def test_empty_paper_zero_percentage(self):
        earned, available, percentage = legacy.calculate_totals({"questions": []})
        assert (earned, available, percentage) == (0, 0, 0.0)

    def test_percentage_band_grades(self):
        assert legacy.percentage_band_grade(92) == "9"
        assert legacy.percentage_band_grade(85) == "8"
        assert legacy.percentage_band_grade(70) == "7"
        assert legacy.percentage_band_grade(50) == "5"
        assert legacy.percentage_band_grade(15) == "1"

    def test_group_rows(self):
        questions = [{"question_number": str(i), "awarded_marks": 1, "max_marks": 2}
                     for i in range(1, 8)]
        groups = legacy.make_group_rows(questions)
        assert len(groups) == 2
        assert groups[0]["earned"] == 5 and groups[0]["available"] == 10
        assert groups[1]["earned"] == 2 and groups[1]["available"] == 4


class TestExtractJson:
    def test_plain_json(self):
        assert legacy.extract_json('{"a": 1}') == {"a": 1}

    def test_fenced_json(self):
        assert legacy.extract_json('```json\n{"a": 1}\n```') == {"a": 1}

    def test_json_with_surrounding_prose(self):
        assert legacy.extract_json('Here you go:\n{"a": {"b": 2}}\nThanks!') == {"a": {"b": 2}}

    def test_empty_raises(self):
        with pytest.raises(ValueError):
            legacy.extract_json("")


class TestModelFallback:
    """grade_with_model's fallback semantics must match the legacy app."""

    class FakeResponse:
        def __init__(self, text):
            self.text = text

    def _make_client(self, responses):
        class FakeResponse:
            def __init__(self, text):
                self.text = text

        class FakeModels:
            def __init__(self):
                self.calls = []

            def generate_content(self, *, model, contents, config=None, **kw):
                self.calls.append(model)
                action = responses.get(model)
                if isinstance(action, Exception):
                    raise action
                return FakeResponse(action)

        class FakeClient:
            def __init__(self):
                self.models = FakeModels()

        return FakeClient()

    def setup_method(self):
        legacy.MODEL_HEALTH.reset()

    def teardown_method(self):
        legacy.MODEL_HEALTH.reset()

    def test_first_model_success(self):
        client = self._make_client({"m1": '{"ok": true}'})
        parsed, model = legacy.grade_with_model(client, ["m1"], ["f"], ["ms"])
        assert model == "m1" and parsed == {"ok": True}

    def test_429_falls_through_to_next_model(self):
        class Quota(Exception):
            def __str__(self):
                return "429 Resource Exhausted"

        client = self._make_client({"m1": Quota(), "m2": '{"ok": 1}'})
        parsed, model = legacy.grade_with_model(client, ["m1", "m2"], ["f"], ["ms"])
        assert model == "m2"
        # m1 benched as temporary (5 min)
        assert "m1" in legacy.MODEL_HEALTH.benched()

    def test_timeout_moves_straight_to_next_model(self):
        class Timeout(Exception):
            def __str__(self):
                return "Request timed out after 180000ms"

        client = self._make_client({"m1": Timeout(), "m2": '{"ok": 1}'})
        _, model = legacy.grade_with_model(client, ["m1", "m2"], ["f"], ["ms"])
        assert model == "m2"

    def test_permanent_error_benches_for_12h(self):
        class NotFound(Exception):
            def __str__(self):
                return "404 model m1 not_found"

        client = self._make_client({"m1": NotFound(), "m2": '{"ok": 1}'})
        _, model = legacy.grade_with_model(client, ["m1", "m2"], ["f"], ["ms"])
        kind, _ = legacy.MODEL_HEALTH.benched()["m1"]
        assert kind == "unavailable"

    def test_limit_zero_is_permanent(self):
        assert legacy.is_permanent_model_error(Exception("quota limit: 0 for this model"))

    def test_benched_model_skipped_on_next_call(self):
        legacy.MODEL_HEALTH.record_failure("m1", Exception("429 overloaded"))
        client = self._make_client({"m2": '{"ok": 1}'})
        _, model = legacy.grade_with_model(client, ["m1", "m2"], ["f"], ["ms"])
        assert model == "m2"
        assert client.models.calls == ["m2"]  # m1 never tried

    def test_all_models_fail_raises(self):
        class Boom(Exception):
            def __str__(self):
                return "some error"

        client = self._make_client({"m1": Boom(), "m2": Boom()})
        with pytest.raises(RuntimeError, match="All configured Gemini models failed"):
            legacy.grade_with_model(client, ["m1", "m2"], ["f"], ["ms"])

    def test_empty_response_retries_then_moves_on(self):
        # Legacy semantics: an empty (non-temporary) response records a
        # failure and breaks to the next model after the first attempt.
        client = self._make_client({"m1": "", "m2": '{"ok": 1}'})
        _, model = legacy.grade_with_model(client, ["m1", "m2"], ["f"], ["ms"])
        assert model == "m2"
        assert client.models.calls == ["m1", "m2"]

    def test_mark_one_submission_pipeline(self):
        # Answer whatever model the configured chain actually asks for: this
        # test is about the pipeline, not about which model is the default.
        from app.config import get_config
        configured_model = list(get_config().gemini.primary_models)[0]
        client = self._make_client({configured_model: '''
{
  "paper_info": {"qualification": "Q", "paper": "1H", "session": "S", "max_marks": 5},
  "questions": [
    {"question_number": "1", "max_marks": 2, "awarded_marks": 2, "topic": "t",
     "student_answer": "a", "working_summary": "w", "loss_reason": "",
     "mark_breakdown": [], "correct_method": "c", "full_mark_solution": "f"},
    {"question_number": "2", "max_marks": 3, "awarded_marks": 1, "topic": "t",
     "student_answer": "a", "working_summary": "w", "loss_reason": "arithmetic",
     "mark_breakdown": [], "correct_method": "c", "full_mark_solution": "f"}
  ],
  "overall_notes": {"summary": "s", "strengths": [], "revision_areas": []},
  "uncertainties": []
}
'''})
        row = legacy.mark_one_submission(client, "Alice", ["remote"], ["ms"])
        assert row["status"] == "Marked"
        assert row["totals"]["earned"] == 3
        assert row["totals"]["available"] == 5
        assert abs(row["totals"]["percentage"] - 60.0) < 1e-9
        assert row["totals"]["provisional_grade"] == "6"
