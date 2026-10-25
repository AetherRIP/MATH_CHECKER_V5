"""Provider outage (504 DEADLINE_EXCEEDED): bounded, failover, never silent.

The end-to-end test observed real 504 DEADLINE_EXCEEDED responses from the two
preferred models. This file does NOT claim another real outage: every provider
failure here is scripted. What it pins is the behaviour the live run relied on:

* a 504 is retryable but strictly bounded (never an unbounded resend loop);
* one failing request is never blindly duplicated on the same key;
* the same key is not disabled for a server-side deadline;
* the marking loop moves on to another model and still produces a result;
* when every model fails, the run RAISES — no partial or unverified result is
  returned as a success;
* the exhausted failure is surfaced by the job policy as a failure with an
  error code, never as a retry that could loop forever.

No network and no API quota is used anywhere in this module.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from app import retry_policy as rp
from app.errors import classify_gemini_error

#: The exact shape Google returned during the recorded run (docs/logs/
#: gemini_probe_38.txt), shortened but with its identifying text intact.
OUTAGE = ("504 DEADLINE_EXCEEDED. {'error': {'code': 504, 'message': "
          "'Deadline expired before operation could complete.', "
          "'status': 'DEADLINE_EXCEEDED'}}")


@pytest.fixture(autouse=True)
def _clean_state():
    from app import api_stats
    from app.gemini.provider_health import provider_health
    from app.engine import legacy_marking as legacy
    provider_health().reset()
    api_stats.reset()
    legacy.MODEL_HEALTH.reset()
    yield
    provider_health().reset()
    api_stats.reset()
    legacy.MODEL_HEALTH.reset()


# ------------------------------------------------------------- classification

def test_the_recorded_504_is_classified_as_a_timeout():
    """Google spells the status ``DEADLINE_EXCEEDED``; both spellings count."""
    assert classify_gemini_error(Exception(OUTAGE))["kind"] == "timeout"
    assert classify_gemini_error(Exception("Deadline Exceeded"))["kind"] == "timeout"
    assert classify_gemini_error(Exception("deadline_exceeded"))["kind"] == "timeout"


def test_a_504_costs_one_retry_and_is_not_parked():
    """The outage budget is one retry, not the two an UNKNOWN error allowed.

    A 504 means the provider already answered "this will not finish"; retrying
    it twice spends three full marking passes on the same dead request.
    """
    failure = rp.classify_failure(Exception(OUTAGE))
    assert failure.cls == rp.TIMEOUT
    assert failure.retryable
    assert failure.max_retries == 1          # one retry, then fail
    assert not failure.park                  # not a capacity wait
    first = rp.decide(failure, attempts=1, max_attempts=5, cfg_max_retries=5)
    second = rp.decide(failure, attempts=2, max_attempts=5, cfg_max_retries=5)
    assert first.action == rp.ACTION_RETRY
    assert second.action == rp.ACTION_FAIL and second.error_code == "timeout"
    # ... and the path the worker child actually takes (exception -> error_kind
    # -> job policy) reaches the same one-retry budget.
    kind = classify_gemini_error(Exception(OUTAGE))["kind"]
    worker_side = rp.failure_for_kind(kind, OUTAGE)
    assert worker_side.cls == rp.TIMEOUT and worker_side.max_retries == 1


def test_the_retry_budget_is_exhausted_and_reported_as_a_failure():
    failure = rp.classify_failure(Exception(OUTAGE))
    allowed = failure.max_retries
    for attempts in range(1, allowed + 1):
        decision = rp.decide(failure, attempts=attempts, max_attempts=10,
                             cfg_max_retries=5)
        assert decision.action == rp.ACTION_RETRY
        assert decision.delay > 0
    exhausted = rp.decide(failure, attempts=allowed + 1, max_attempts=10,
                          cfg_max_retries=5)
    assert exhausted.action == rp.ACTION_FAIL
    assert exhausted.error_code
    assert "budget exhausted" in exhausted.reason


def test_the_worker_kind_path_classifies_a_504_deadline_as_a_timeout():
    """The child reports ``error_kind: timeout`` for a deadline; the policy
    must not treat that as an unknown, endlessly-retryable error."""
    failure = rp.failure_for_kind("timeout", OUTAGE)
    assert failure.cls == rp.TIMEOUT
    assert failure.retryable and failure.max_retries == 1


# ---------------------------------------------------------------- key handling

def test_a_504_does_not_disable_or_park_the_key():
    from app.gemini.key_manager import KeyManager
    from app.config import get_secrets
    get_secrets().set_gemini_keys(["K-one-0000000000000001"])
    manager = KeyManager()
    manager.sync_with_secret_store()
    fingerprint, _ = manager.acquire()
    manager.record_failure(fingerprint, Exception(OUTAGE))
    record = manager._records[fingerprint]
    assert record.invalid is False
    assert record.cooldown_until == 0.0      # a server deadline is not capacity
    # Counted as the timeout it is, which is what keeps it out of cooldown.
    assert record.timeout_errors == 1
    assert record.failures == 1


# ------------------------------------------------------- one request per call

class _ScriptedModels:
    def __init__(self, script):
        self._script = script
        self.calls = 0

    def generate_content(self, *, model, contents, config=None, **kwargs):
        self.calls += 1
        action = self._script(self.calls)
        if isinstance(action, Exception):
            raise action
        return action


def _proxy(monkeypatch, script, *keys):
    from app.gemini.client_proxy import KeyAwareClient
    from app.gemini.key_manager import KeyManager
    from app.config import get_secrets
    get_secrets().set_gemini_keys(list(keys))
    manager = KeyManager()
    manager.sync_with_secret_store()
    client = KeyAwareClient(manager)
    models = _ScriptedModels(script)
    sdk = SimpleNamespace(models=models)
    monkeypatch.setattr(client, "_sdk_client_for_key", lambda key: sdk,
                        raising=False)
    return client, manager, models


# ------------------------------------------------------------- model failover

class _FakeModels:
    def __init__(self, responses):
        self.responses = responses
        self.calls: list[str] = []

    def generate_content(self, *, model, contents, config=None, **kwargs):
        self.calls.append(model)
        action = self.responses.get(model)
        if isinstance(action, Exception):
            raise action
        return SimpleNamespace(text=action)


class _FakeClient:
    def __init__(self, responses):
        self.models = _FakeModels(responses)


def test_a_504_on_the_preferred_model_fails_over_and_marks(monkeypatch):
    """The observed shape: preferred model 504s, the next one marks."""
    from app.engine import legacy_marking as legacy
    monkeypatch.setattr(legacy.time, "sleep", lambda _s: None)
    client = _FakeClient({
        "gemini-3.8-flash": Exception(OUTAGE),
        "gemini-3.6-flash": json.dumps({"questions": []}),
    })
    parsed, model = legacy.grade_with_model(
        client, ["gemini-3.8-flash", "gemini-3.6-flash"], ["f"], ["ms"])
    assert model == "gemini-3.6-flash"
    assert parsed == {"questions": []}
    # Bounded: the failing model is not retried indefinitely.
    assert client.models.calls.count("gemini-3.8-flash") <= 2
    assert client.models.calls[-1] == "gemini-3.6-flash"


def test_when_every_model_504s_the_run_fails_and_yields_no_result(monkeypatch):
    from app.engine import legacy_marking as legacy
    monkeypatch.setattr(legacy.time, "sleep", lambda _s: None)
    client = _FakeClient({
        "gemini-3.8-flash": Exception(OUTAGE),
        "gemini-3.6-flash": Exception(OUTAGE),
    })
    with pytest.raises(RuntimeError) as raised:
        legacy.grade_with_model(
            client, ["gemini-3.8-flash", "gemini-3.6-flash"], ["f"], ["ms"])
    # No result is fabricated; the failure names the models that failed.
    message = str(raised.value)
    assert "All configured Gemini models failed" in message
    assert "DEADLINE" in message or "504" in message
    assert client.models.calls == ["gemini-3.8-flash", "gemini-3.6-flash"]
    # A server-side deadline is not treated as a permanent model fault, so the
    # model stays in the chain for the next attempt instead of disappearing.
    assert legacy.MODEL_HEALTH.benched() == {}


def test_one_504_request_is_not_blindly_resent_on_the_same_key(
        monkeypatch, fresh_state):
    client, _km, models = _proxy(monkeypatch, lambda n: Exception(OUTAGE),
                                 "K-one-0000000000000001",
                                 "K-two-0000000000000002")
    with pytest.raises(Exception):
        client.generate_content(model="m", contents=["x"])
    assert models.calls == 1
