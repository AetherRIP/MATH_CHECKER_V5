"""A transient 503 must not cost an hour of marking.

Measured 2026-10-30 (docs/results/CORRECTION_PERFORMANCE_RECOVERY.md): the 35
recorded correction jobs contain single ``gemini-3.8-flash`` marking calls of
720 s, 1 129 s, 2 451 s and 8 400 s. Each one was entered because a transient
503 hit the primary model, and the page-scaled budget (120 s + 15 s/page ≈ 12
min for a 41-page paper) allowed it to run to that length — twice per model and
again per job attempt.

These tests pin the two decisions that follow from that measurement:

* the default chain no longer contains the model that needs minutes per call;
* a fallback model gets a short rescue deadline, so a slow model is abandoned
  instead of holding the job, while the primary keeps the page-scaled budget a
  large paper needs.
"""

from __future__ import annotations

import pytest

from app.config import (DEFAULT_AUDIT_MODELS, DEFAULT_PRIMARY_MODELS,
                        is_trusted_marking_model)


class _Recorder:
    """A fake client that records every request config it is handed."""

    def __init__(self, behaviour):
        self.calls: list[tuple[str, dict]] = []
        self.models = self
        self._behaviour = behaviour

    def generate_content(self, *, model, contents, config=None, **_kwargs):
        self.calls.append((model, dict(config or {})))
        return self._behaviour(model, config)


def _reply():
    class _Response:
        text = ('{"questions": [{"question_number": "1", '
                '"awarded_marks": 1, "max_marks": 1}]}')
    return _Response()


@pytest.fixture(autouse=True)
def _clean_model_bench():
    """The bench is process-global; a bench left by another test would hide a
    model the test needs to see."""
    from app.engine import legacy_marking as legacy
    legacy.MODEL_HEALTH.reset()
    yield
    legacy.MODEL_HEALTH.reset()


class TestDefaultChain:
    def test_the_chain_is_the_measured_fast_model(self):
        assert DEFAULT_PRIMARY_MODELS == ("gemini-3.6-flash",)

    def test_every_default_is_trusted_for_marking(self):
        assert all(is_trusted_marking_model(m) for m in DEFAULT_PRIMARY_MODELS)
        assert all(is_trusted_marking_model(m) for m in DEFAULT_AUDIT_MODELS)

    def test_the_measured_slow_model_is_not_a_default(self):
        # 3.8 Flash: 53.6 s for one line of text, 240 s+ on another attempt,
        # 720-8 400 s for a real paper. It may be configured explicitly; it must
        # not be what an unconfigured installation silently uses.
        assert "gemini-3.8-flash" not in DEFAULT_PRIMARY_MODELS
        assert "gemini-3.8-flash" not in DEFAULT_AUDIT_MODELS

    def test_the_thinking_budget_round_trips_through_config(self, tmp_path,
                                                           monkeypatch):
        from app import config as cfg
        file = tmp_path / "config.toml"
        file.write_text('[gemini]\nthinking_budget = 0\n', encoding="utf-8")
        monkeypatch.setattr(cfg, "CONFIG_PATH", file)
        monkeypatch.setattr(cfg, "_config", None)
        try:
            assert cfg.get_config(reload=True).gemini.thinking_budget == 0
            # Saving keeps it, and an unset budget saves as an absent value
            # rather than as a made-up default.
            loaded = cfg.get_config()
            loaded.gemini.thinking_budget = None
            cfg.save_config(loaded)
            monkeypatch.setattr(cfg, "_config", None)
            assert cfg.get_config(reload=True).gemini.thinking_budget is None
        finally:
            monkeypatch.setattr(cfg, "_config", None)

    def test_the_rescue_deadline_is_far_above_a_healthy_call(self):
        from app.engine.legacy_marking import FALLBACK_MODEL_DEADLINE_MS
        # A healthy 41-page marking call measured 97 s; a "deadline" near that
        # would cut real work off. It must also be far below the ~12 minutes
        # the page-scaled budget allows, which is the defect being fixed.
        assert 180_000 <= FALLBACK_MODEL_DEADLINE_MS <= 600_000


class TestShedRequestWait:
    """A 503 "high demand" must not be re-run two seconds later."""

    def test_high_demand_parks_for_at_least_a_minute(self):
        from app import retry_policy as rp
        decision = rp.decide(rp.failure_for(rp.HIGH_DEMAND), attempts=1,
                             max_attempts=3, cfg_max_retries=2)
        assert decision.action == rp.ACTION_PARK
        assert decision.delay >= rp.HIGH_DEMAND_MIN_PARK_SECONDS

    def test_a_longer_provider_cooldown_still_wins(self):
        from app import retry_policy as rp
        decision = rp.decide(rp.failure_for(rp.HIGH_DEMAND), attempts=1,
                             max_attempts=3, cfg_max_retries=2,
                             park_hint_delay=300.0)
        assert decision.action == rp.ACTION_PARK
        assert decision.delay == 300.0

    def test_parking_does_not_consume_the_attempt_budget(self):
        from app import retry_policy as rp
        decision = rp.decide(rp.failure_for(rp.HIGH_DEMAND), attempts=3,
                             max_attempts=3, cfg_max_retries=2)
        assert decision.action == rp.ACTION_PARK


class TestFallbackBudget:
    def _chain(self, behaviour, models):
        from app.engine import legacy_marking as legacy
        client = _Recorder(behaviour)
        parsed, used = legacy.grade_with_model(
            client=client,
            model_names=models,
            student_file="student",
            mark_scheme_file="scheme",
        )
        return client, parsed, used

    def test_the_primary_keeps_the_page_scaled_budget(self):
        client, _parsed, used = self._chain(lambda m, c: _reply(),
                                            ["gemini-3.6-flash"])
        assert used == "gemini-3.6-flash"
        assert len(client.calls) == 1
        assert "http_options" not in client.calls[0][1]

    def test_a_fallback_is_given_the_short_rescue_deadline(self):
        from app.engine.legacy_marking import FALLBACK_MODEL_DEADLINE_MS

        def behaviour(model, _config):
            if model == "gemini-3.6-flash":
                raise RuntimeError("503 UNAVAILABLE: high demand")
            return _reply()

        client, _parsed, used = self._chain(
            behaviour, ["gemini-3.6-flash", "gemini-3.8-flash"])
        assert used == "gemini-3.8-flash"
        assert len(client.calls) == 2
        primary_config = client.calls[0][1]
        fallback_config = client.calls[1][1]
        assert "http_options" not in primary_config
        assert fallback_config["http_options"]["timeout"] == \
            FALLBACK_MODEL_DEADLINE_MS
        # The proxy must remain the only thing that retries, so the rescue
        # request pins its own single attempt too.
        assert fallback_config["http_options"]["retry_options"] == {
            "attempts": 1}

    def test_a_fallback_that_times_out_is_not_sent_twice(self):
        def behaviour(model, _config):
            if model == "gemini-3.6-flash":
                raise RuntimeError("503 UNAVAILABLE: high demand")
            raise RuntimeError("504 DEADLINE_EXCEEDED: deadline exceeded")

        client = _Recorder(behaviour)
        from app.engine import legacy_marking as legacy
        with pytest.raises(RuntimeError, match="All configured"):
            legacy.grade_with_model(
                client=client,
                model_names=["gemini-3.6-flash", "gemini-3.8-flash"],
                student_file="student",
                mark_scheme_file="scheme",
            )
        fallback_calls = [c for c in client.calls if c[0] == "gemini-3.8-flash"]
        assert len(fallback_calls) == 1, (
            "a model that already timed out must not spend the deadline again")

    def test_no_thinking_setting_is_sent_by_default(self):
        from app.config import get_config
        get_config().gemini.thinking_budget = None
        client, _parsed, _used = self._chain(lambda m, c: _reply(),
                                             ["gemini-3.6-flash"])
        assert "thinking_config" not in client.calls[0][1]

    def test_a_configured_budget_reaches_the_request(self):
        from app.config import get_config
        try:
            for budget in (0, 1024):
                get_config().gemini.thinking_budget = budget
                client, _parsed, _used = self._chain(
                    lambda m, c: _reply(), ["gemini-3.6-flash"])
                assert client.calls[0][1]["thinking_config"] == {
                    "thinking_budget": budget}
        finally:
            get_config().gemini.thinking_budget = None

    def test_a_fallback_still_marks_when_it_answers_in_time(self):
        def behaviour(model, _config):
            if model == "gemini-3.6-flash":
                raise RuntimeError("503 UNAVAILABLE: high demand")
            return _reply()

        _client, parsed, used = self._chain(
            behaviour, ["gemini-3.6-flash", "gemini-3.8-flash"])
        assert used == "gemini-3.8-flash"
        assert parsed["questions"][0]["awarded_marks"] == 1
