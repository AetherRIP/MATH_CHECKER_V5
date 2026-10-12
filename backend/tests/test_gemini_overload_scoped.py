"""Regression tests for the Gemini "cooling down after high demand" bug.

Evidence these lock in (see data/logs/app.log + scripts/probe_gemini.py):
Gemini returned a genuine ``503 UNAVAILABLE "This model is currently
experiencing high demand"`` for ONE model while 11 of 12 keys were serving
that same model normally in the same minute. The application treated that
single-model 503 as a provider-wide verdict, so its global cooldown gate
froze ALL keys and ALL models — the "every key is cooling down" symptom.

These tests pin the three defects that produced it:
  1. a 503 on model A closed the gate for models B..E as well;
  2. the streak counter was reset only by a success, which cannot arrive
     while the gate is closed → cooldown escalated 30→60→…→600s and stuck;
  3. one key's 503 blocked the other keys' requests before they were sent.
"""
from __future__ import annotations

import time

import pytest

from app.retry_policy import HIGH_DEMAND, TIMEOUT


@pytest.fixture(autouse=True)
def _fresh():
    from app.gemini.provider_health import provider_health
    provider_health().reset()
    yield
    provider_health().reset()


class TestOverloadIsScopedNotGlobal:
    def test_one_models_503_does_not_block_another_model(self):
        from app.gemini.provider_health import ProviderHealth
        health = ProviderHealth()
        for _ in range(5):
            health.record_failure(HIGH_DEMAND, "503 high demand",
                                  "gemini-3.8-flash")
        assert health.available("gemini-3.8-flash")[0] is False
        # The other models are untouched and still usable.
        for model in ("gemini-3.7-flash", "gemini-3.6-flash",
                      "gemini-3.5-flash-lite"):
            assert health.available(model)[0] is True, model

    def test_dispatch_only_pauses_when_everything_is_cooling(self):
        from app.gemini.provider_health import ProviderHealth
        health = ProviderHealth()
        for _ in range(5):
            health.record_failure(HIGH_DEMAND, "503", "gemini-3.8-flash")
        # One model cooling must not stop the whole batch: another model
        # in the fallback chain can still serve it.
        assert health.available()[0] is True
        for _ in range(5):
            health.record_failure(HIGH_DEMAND, "503", "gemini-3.7-flash")
        assert health.available()[0] is True

    def test_provider_wide_wildcard_still_blocks_everything(self):
        from app.gemini.provider_health import ProviderHealth
        health = ProviderHealth()
        for _ in range(5):
            health.record_failure(HIGH_DEMAND, "503")     # model unknown
        assert health.available()[0] is False
        assert health.available("gemini-3.6-flash")[0] is False

    def test_success_on_a_healthy_model_clears_only_that_streak(self):
        from app.gemini.provider_health import ProviderHealth
        health = ProviderHealth()
        for _ in range(5):
            health.record_failure(HIGH_DEMAND, "503", "gemini-3.8-flash")
        health.record_success("gemini-3.6-flash")
        assert health.available("gemini-3.6-flash")[0] is True
        # The failing model's own cooldown still holds until it expires.
        assert health.available("gemini-3.8-flash")[0] is False


class TestCooldownCannotGetStuck:
    def test_cooldown_never_exceeds_the_cap(self):
        from app.gemini.provider_health import ProviderHealth
        health = ProviderHealth()
        for _ in range(50):        # far past any escalation
            health.record_failure(HIGH_DEMAND, "503", "gemini-3.8-flash")
        _, wait = health.available("gemini-3.8-flash")
        assert wait <= health_snapshot_cap()

    def test_cooldown_is_half_open_and_restarts_bounded(self):
        """The regression that caused the 578s lock-out.

        The old streak counter was only cleared by a success — impossible
        while the gate is closed. So each probe after expiry added to the
        same streak and escalated further (30→60→…→600s). Now expiry resets
        the streak, so every recovery cycle re-enters at the BASE window.

        Asserted against the base window (which jitter can only shrink or
        scale by 1.2), NOT by comparing two separately jittered samples —
        that comparison was itself flaky.
        """
        from app.gemini.provider_health import (
            COOLDOWN_BASE_SECONDS, COOLDOWN_JITTER, ProviderHealth,
        )
        base_ceiling = COOLDOWN_BASE_SECONDS * max(COOLDOWN_JITTER)
        health = ProviderHealth()
        # Every recovery cycle must land in the base window, never the
        # escalated 60/120/240/600s the old code produced.
        for cycle in range(6):
            for _ in range(3):
                health.record_failure(HIGH_DEMAND, "503", "gemini-3.8-flash")
            ok, wait = health.available("gemini-3.8-flash")
            assert ok is False, f"cycle {cycle} must gate the model"
            assert 0 < wait <= base_ceiling + 1, (
                f"cycle {cycle} cooldown {wait:.1f}s escaped the base window "
                f"({base_ceiling:.0f}s) — it is ratcheting again")
            # Pretend the window elapsed with no success in between.
            health._buckets["gemini-3.8-flash"].cooldown_until = time.time() - 1

    def test_half_open_re_arms_and_still_gates(self):
        """Regression in the first half-open draft.

        Re-arming the reset on *every* failure while the elapsed cooldown
        timestamp is still in the past pinned the streak at 1, so the model
        was never gated again after it recovered.
        """
        from app.gemini.provider_health import ProviderHealth
        health = ProviderHealth()
        for _ in range(3):
            health.record_failure(HIGH_DEMAND, "503", "gemini-3.8-flash")
        assert health.available("gemini-3.8-flash")[0] is False
        health._buckets["gemini-3.8-flash"].cooldown_until = time.time() - 1
        # Three fresh failures after expiry must gate it again — starting
        # from the base delay, not escalating and not being skipped.
        for i in range(1, 4):
            health.record_failure(HIGH_DEMAND, "503", "gemini-3.8-flash")
            assert health._buckets["gemini-3.8-flash"].consecutive == i
        ok, wait = health.available("gemini-3.8-flash")
        assert ok is False and 0 < wait <= 40

    def test_a_success_reopens_the_gate(self):
        from app.gemini.provider_health import ProviderHealth
        health = ProviderHealth()
        for _ in range(6):
            health.record_failure(HIGH_DEMAND, "503", "gemini-3.8-flash")
        assert health.available("gemini-3.8-flash")[0] is False
        health.record_success("gemini-3.8-flash")
        assert health.available("gemini-3.8-flash")[0] is True

    def test_non_overload_failures_never_close_the_gate(self):
        from app.gemini.provider_health import ProviderHealth
        health = ProviderHealth()
        for _ in range(10):
            health.record_failure(TIMEOUT, "timed out", "gemini-3.8-flash")
        assert health.available()[0] is True
        assert health.available("gemini-3.8-flash")[0] is True

    def test_snapshot_reports_per_model_state(self):
        from app.gemini.provider_health import ProviderHealth
        health = ProviderHealth()
        for _ in range(4):
            health.record_failure(HIGH_DEMAND, "503", "gemini-3.8-flash")
        snap = health.snapshot()
        assert snap["last_failure_model"] == "gemini-3.8-flash"
        assert snap["models"]["gemini-3.8-flash"]["consecutive"] >= 3
        assert snap["cooldowns"] >= 1


class TestProxyRespectsModelScope:
    def _client(self, monkeypatch, calls):
        """KeyAwareClient with one key and a stubbed SDK that always 503s."""
        from app.gemini import client_proxy
        from app.gemini.key_manager import KeyManager

        km = KeyManager()
        client = client_proxy.KeyAwareClient(km)

        class _Models:
            def generate_content(self, **kw):
                calls.append(kw.get("model"))
                raise RuntimeError(
                    "503 UNAVAILABLE. {'error': {'code': 503, 'message': "
                    "'This model is currently experiencing high demand. Spikes "
                    "in demand are usually temporary.', 'status': "
                    "'UNAVAILABLE'}}")

        class _SDK:
            models = _Models()

        monkeypatch.setattr(client, "_sdk_client_for_key",
                            lambda key: _SDK())
        monkeypatch.setattr(client, "_configured_keys",
                            lambda: ["AIzaFakeKeyForTests0000000000"])
        monkeypatch.setattr(client, "_acquire_for_call", lambda: "fp-test")
        client._sdk_clients["fp-test"] = _SDK()
        return client

    def test_cooling_model_blocks_only_that_model(self, monkeypatch):
        from app.gemini.client_proxy import ProviderCoolingError
        from app.gemini.provider_health import provider_health
        calls: list = []
        client = self._client(monkeypatch, calls)
        # Drive the gate onto ONE model only.
        for _ in range(5):
            provider_health().record_failure(
                HIGH_DEMAND, "503 high demand", "gemini-3.8-flash")
        # Blocked: nothing leaves the process.
        with pytest.raises(ProviderCoolingError) as exc:
            client.generate_content(model="gemini-3.8-flash", contents=["x"])
        assert "gemini-3.8-flash" in str(exc.value)
        # A DIFFERENT model is still allowed to be attempted.
        with pytest.raises(Exception) as exc2:
            client.generate_content(model="gemini-3.6-flash", contents=["x"])
        assert not isinstance(exc2.value, ProviderCoolingError)
        assert "gemini-3.6-flash" in calls

    def test_error_message_names_the_model_not_a_global_outage(
            self, monkeypatch):
        from app.gemini.client_proxy import ProviderCoolingError
        from app.gemini.provider_health import provider_health
        calls: list = []
        client = self._client(monkeypatch, calls)
        for _ in range(5):
            provider_health().record_failure(HIGH_DEMAND, "503",
                                             "gemini-3.8-flash")
        with pytest.raises(ProviderCoolingError) as exc:
            client.generate_content(model="gemini-3.8-flash", contents=["x"])
        message = str(exc.value)
        # It must not claim the whole provider/ every key is down.
        assert "every key" not in message.lower()
        assert "gemini-3.8-flash" in message


def health_snapshot_cap() -> float:
    from app.gemini.provider_health import COOLDOWN_CAP_SECONDS
    return COOLDOWN_CAP_SECONDS * 1.2 + 0.01