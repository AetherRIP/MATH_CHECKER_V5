"""No layer may multiply one failed request into a storm.

Three retry owners exist, and each has its own budget:

* the proxy (`client_proxy`, up to 8 key attempts),
* the model walk (`legacy_marking`, 1-2 attempts per model),
* the job (`pool`, 3 attempts, with parks outside that budget).

Multiplying them is how a single 503 became an hour of re-sent 41-page
requests. These tests pin the bounds that stop it:

* a provider-wide 503 is re-sent through at most two keys, because it is a
  property of the model, not of the credential;
* a genuinely key-scoped failure (rate limit) still rotates;
* repeated overload parks progressively longer, so a fleet does not re-probe an
  outage in lockstep every minute.
"""

from __future__ import annotations

import pytest


class _Sdk:
    """A stub SDK whose `generate_content` always raises ``error``."""

    def __init__(self, error: Exception, calls: list):
        self.error = error
        outer = self

        class _Models:
            def generate_content(self, **kwargs):
                calls.append(kwargs.get("model"))
                raise outer.error

        self.models = _Models()


def _client_with_keys(monkeypatch, error: Exception, keys: int = 4):
    from app.gemini import client_proxy
    from app.gemini.key_manager import KeyManager

    calls: list = []
    fingerprint = {"n": 0}

    def acquire():
        fingerprint["n"] += 1
        return f"fp-{fingerprint['n']}"

    client = client_proxy.KeyAwareClient(KeyManager())
    sdk = _Sdk(error, calls)
    monkeypatch.setattr(client, "_acquire_for_call", acquire)
    monkeypatch.setattr(client, "_configured_keys",
                        lambda: [f"AIzaFake{i:022d}" for i in range(keys)])
    monkeypatch.setattr(client, "_key_value", lambda fp: f"AIzaFake{fp}")
    monkeypatch.setattr(client, "_sdk_client_for_key", lambda key: sdk)
    monkeypatch.setattr(client, "_sdk_clients", {})
    monkeypatch.setattr(client, "_needs_reupload", lambda contents, fp: False)
    monkeypatch.setattr(client, "_reupload_for_key", lambda c, fp: (c, []))
    return client, calls


def _send(client):
    return client.models.generate_content(model="gemini-3.6-flash",
                                         contents=["a"])


class TestOverloadDoesNotMultiplyRequests:
    def test_a_provider_wide_503_is_resent_through_at_most_two_keys(
            self, monkeypatch):
        from app.gemini.client_proxy import MAX_OVERLOAD_KEY_ATTEMPTS
        client, calls = _client_with_keys(
            monkeypatch,
            RuntimeError("503 UNAVAILABLE: this model is experiencing high "
                         "demand"))
        with pytest.raises(Exception):
            _send(client)
        assert len(calls) == MAX_OVERLOAD_KEY_ATTEMPTS
        assert MAX_OVERLOAD_KEY_ATTEMPTS <= 2, (
            "each extra key attempt re-sends the whole paper")

    def test_a_rate_limited_key_still_rotates_to_another(self, monkeypatch):
        """429 is per key, so rotation must keep working."""
        client, calls = _client_with_keys(
            monkeypatch,
            RuntimeError("429 RESOURCE_EXHAUSTED: quota exceeded"))
        with pytest.raises(Exception):
            _send(client)
        assert len(calls) >= 2


class TestParkBackoffGrows:
    def _no_jitter(self, monkeypatch):
        from app import retry_policy as rp
        monkeypatch.setattr(rp.random, "uniform", lambda *a: 1.0)

    def test_the_first_park_is_at_least_a_minute(self, monkeypatch):
        from app import retry_policy as rp
        self._no_jitter(monkeypatch)
        assert rp.compute_park_backoff(1) >= rp.HIGH_DEMAND_MIN_PARK_SECONDS

    def test_repeated_parks_wait_longer(self, monkeypatch):
        from app import retry_policy as rp
        self._no_jitter(monkeypatch)
        delays = [rp.compute_park_backoff(n) for n in (1, 2, 3, 4)]
        assert delays == sorted(delays)
        assert delays[3] > delays[0]

    def test_the_park_wait_is_capped(self, monkeypatch):
        from app import retry_policy as rp
        self._no_jitter(monkeypatch)
        assert rp.compute_park_backoff(50) == rp.HIGH_DEMAND_BACKOFF_CAP_SECONDS

    def test_jitter_never_drops_below_the_minimum(self, monkeypatch):
        from app import retry_policy as rp
        monkeypatch.setattr(rp.random, "uniform", lambda *a: 0.01)
        assert rp.compute_park_backoff(1) >= rp.HIGH_DEMAND_MIN_PARK_SECONDS

    def test_decide_uses_the_park_count(self, monkeypatch):
        from app import retry_policy as rp
        self._no_jitter(monkeypatch)
        failure = rp.failure_for(rp.HIGH_DEMAND)
        first = rp.decide(failure, attempts=1, max_attempts=3,
                          cfg_max_retries=2, parks=0)
        later = rp.decide(failure, attempts=1, max_attempts=3,
                          cfg_max_retries=2, parks=3)
        assert first.action == later.action == rp.ACTION_PARK
        assert later.delay > first.delay

    def test_escalating_parks_end_in_a_clean_failure_within_the_budget(
            self, monkeypatch):
        """Growing waits must still terminate: an outage is not waited out forever."""
        from app import retry_policy as rp
        self._no_jitter(monkeypatch)
        failure = rp.failure_for(rp.HIGH_DEMAND)
        waited, parks, actions = 0.0, 0, []
        for _ in range(12):
            decision = rp.decide(failure, attempts=1, max_attempts=3,
                                 cfg_max_retries=2, waited_seconds=waited,
                                 parks=parks)
            actions.append(decision.action)
            if decision.action != rp.ACTION_PARK:
                break
            waited += decision.delay
            parks += 1
        assert actions[-1] == rp.ACTION_FAIL
        assert parks < 6, "a job must not park indefinitely"
        assert waited <= rp._WAIT_BUDGET_SECONDS

    def test_a_longer_provider_hint_still_wins(self, monkeypatch):
        from app import retry_policy as rp
        self._no_jitter(monkeypatch)
        decision = rp.decide(rp.failure_for(rp.HIGH_DEMAND), attempts=1,
                             max_attempts=3, cfg_max_retries=2,
                             park_hint_delay=900.0, parks=1)
        assert decision.delay == 900.0
