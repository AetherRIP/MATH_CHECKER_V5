"""Regression tests: a failed API request is NOT proof that a key is invalid.

The reported bug: keys that had been working were shown as INVALID, and the app
then reported "No usable Gemini keys remain (all invalid or disabled)".

Two independent causes are pinned here:

1. ``KeyRecord.load`` derived INVALID from the ``removed`` column, which the
   "delete this key from the list" flow also sets. A key that had EVER recorded
   a failure (even a plain 429) and was later restored — backup restore, a
   re-pasted ``secrets.toml``, a re-added key — came back INVALID with
   ``auth_errors == 0``: the provider had never rejected it.
2. The worker pool released a key with the engine's AGGREGATED error
   ("All configured Gemini models failed. model-a: 429 … | model-b: 401 …").
   ``classify_gemini_error`` saw the digits ``401`` anywhere in that text and
   stamped the healthy leased key INVALID permanently.

Everything here is deterministic and offline: fake credentials, scripted
responses, no network, no quota, no student data.
"""

from __future__ import annotations

import time
from types import SimpleNamespace

import pytest

from app import config
from app.errors import classify_gemini_error, http_status_of
from app.gemini.key_manager import (
    KeyManager,
    WaitForApiError,
    fingerprint,
)

# Fake credentials only — never a real key.
K1 = "AIzaSyFAKEKEYFORREGRESSIONTESTS000000000001"
K2 = "AIzaSyFAKEKEYFORREGRESSIONTESTS000000000002"

#: What Google actually returns when the credential is the problem.
REJECTED = ("401 UNAUTHENTICATED: API key not valid. Please pass a valid API key.")

#: The engine's aggregate, exactly as it is built in legacy_marking: several
#: model failures joined with " | ". One sub-error belongs to a DIFFERENT key.
AGGREGATE = (
    "All configured Gemini models failed. gemini-3.8-flash: 429 "
    "RESOURCE_EXHAUSTED. You exceeded your current quota | gemini-3.6-flash: "
    "401 UNAUTHENTICATED API key not valid"
)


@pytest.fixture
def km(fresh_state):
    config.get_secrets().set_gemini_keys([K1, K2])
    manager = KeyManager()
    manager.sync_with_secret_store()
    return manager


def _seed(*keys):
    config.get_secrets().set_gemini_keys(list(keys))


def _status(manager, fp):
    return next(k for k in manager.status()["keys"] if k["fingerprint"] == fp)


# ---------------------------------------------------------------------------
# §4 the classification table: what each failure MEANS
# ---------------------------------------------------------------------------

class TestClassificationTable:
    def test_provider_rejecting_the_credential_is_invalid(self):
        assert classify_gemini_error(Exception(REJECTED))["kind"] == "invalid_key"

    def test_a_leaked_key_names_invalid(self):
        assert classify_gemini_error(
            Exception("API key reported as leaked"))["kind"] == "invalid_key"

    def test_digits_inside_a_payload_are_not_a_credential_rejection(self):
        """A malformed request is a request error, never a dead key.

        The old check was ``"401" in message``, so a 400 whose body happened to
        contain the digits 401 permanently invalidated a working key.
        """
        kind = classify_gemini_error(Exception(
            "400 INVALID_ARGUMENT: request contains 401 bytes of inline data"))
        assert kind["kind"] != "invalid_key"

    def test_model_unavailable_is_a_model_problem_not_a_rate_limit(self):
        """``limit: 0`` is "this project has no access to this model".

        The branch existed but sat AFTER the 429/quota branch, so the word
        "quota" always won and a model with no free-tier access was reported
        (and parked) as a rate-limited key.
        """
        for message in ("429 RESOURCE_EXHAUSTED: quotaValue 0 (limit: 0)",
                        "400 INVALID_ARGUMENT: model has limit:0 for this project",
                        "404 NOT_FOUND: model gemini-9.9-flash is not supported",
                        # The exact body the real provider returned for a model
                        # that does not exist (observed in the V5 failover run).
                        "404 NOT_FOUND. {'error': {'code': 404, 'message': "
                        "'models/gemini-9.9-nonexistent is not found for API "
                        "version v1beta, or is not supported for generateContent. '"
                        ", 'status': 'NOT_FOUND'}}"):
            assert classify_gemini_error(Exception(message))["kind"] == "model_no_access", \
                message

    def test_quota_exhausted(self):
        assert classify_gemini_error(
            Exception("429 RESOURCE_EXHAUSTED: You exceeded your current quota")
        )["kind"] == "rate_limited"

    def test_rate_limited(self):
        assert classify_gemini_error(
            Exception("429 Too Many Requests: rate limit exceeded, retry in 30s")
        )["kind"] == "rate_limited"

    def test_temporary_provider_failure(self):
        assert classify_gemini_error(Exception(
            "503 UNAVAILABLE. This model is currently experiencing high demand."
        ))["kind"] == "temporary"

    def test_timeout_in_every_spelling(self):
        for message in ("request timed out", "504 DEADLINE_EXCEEDED",
                        "Deadline expired before operation could complete."):
            assert classify_gemini_error(Exception(message))["kind"] == "timeout", message

    def test_network_failure_is_a_connection_error(self):
        for message in ("Connection reset by peer",
                        "getaddrinfo failed for generativelanguage.googleapis.com",
                        "Cannot connect to host"):
            kind = classify_gemini_error(Exception(message))["kind"]
            assert kind in ("network", "timeout"), message

    def test_malformed_response_is_a_validation_error(self):
        kind = classify_gemini_error(Exception(
            "Expecting value: line 1 column 1 (char 0)"))["kind"]
        assert kind not in ("invalid_key", "forbidden"), kind

    def test_an_unfamiliar_provider_message_is_unknown_never_invalid(self):
        for message in ("Quota policy rejected the request with an unknown code",
                        "Provider returned status code 599 from a new gateway"):
            assert classify_gemini_error(Exception(message))["kind"] != "invalid_key", \
                message

    def test_an_aggregated_multi_key_error_is_never_a_credential_verdict(self):
        """The aggregate is several failures joined, not one verdict about one key."""
        kind = classify_gemini_error(Exception(AGGREGATE))["kind"]
        assert kind != "invalid_key", AGGREGATE
        assert kind != "forbidden", AGGREGATE


# ---------------------------------------------------------------------------
# §2/§4 the manager: transient failures must not invalidate a key
# ---------------------------------------------------------------------------

class TestTransientFailuresDoNotInvalidate:
    def test_a_quota_exhaustion_cools_down_and_recovers(self, km):
        fp = fingerprint(K1)
        km.record_failure(fp, Exception("429 RESOURCE_EXHAUSTED: quota"))
        shown = _status(km, fp)
        assert shown["status"] == "COOLDOWN"
        assert shown["auth_errors"] == 0
        km._records[fp].cooldown_until = time.time() - 1
        assert _status(km, fp)["status"] == "HEALTHY"

    def test_a_provider_overload_cools_down_but_never_invalidates(self, km):
        fp = fingerprint(K1)
        km.record_failure(fp, Exception("503 UNAVAILABLE: high demand"))
        assert _status(km, fp)["status"] == "COOLDOWN"
        assert _status(km, fp)["auth_errors"] == 0

    def test_a_timeout_is_counted_but_never_invalidates(self, km):
        fp = fingerprint(K1)
        km.record_failure(fp, Exception("504 DEADLINE_EXCEEDED"))
        assert km._records[fp].timeout_errors == 1
        assert km._records[fp].invalid is False
        assert km._records[fp].cooldown_until == 0.0

    def test_network_and_parse_failures_never_invalidate(self, km):
        fp = fingerprint(K1)
        for message in ("Connection reset by peer",
                        "Expecting value: line 1 column 1 (char 0)"):
            km.record_failure(fp, Exception(message))
        assert km._records[fp].invalid is False

    def test_one_failing_key_does_not_block_the_others(self, km):
        bad, good = fingerprint(K1), fingerprint(K2)
        km.record_failure(bad, Exception(REJECTED))
        assert _status(km, bad)["status"] == "INVALID"
        assert _status(km, good)["status"] == "HEALTHY"
        for _ in range(3):
            assert km.acquire()[0] == good

    def test_rotation_after_a_transient_failure_uses_another_key(self, km):
        first = km.acquire()[0]
        km.record_failure(first, Exception("429 RESOURCE_EXHAUSTED: quota"))
        assert km.acquire()[0] != first

    def test_an_unsupported_model_does_not_corrupt_the_key_status(self, km):
        fp = fingerprint(K1)
        km.record_failure(fp, Exception(
            "404 NOT_FOUND: model gemini-9.9-flash is not supported for this API version"))
        assert km._records[fp].invalid is False
        assert km._records[fp].cooldown_until == 0.0
        assert km.acquire()[0] in (fp, fingerprint(K2))


# ---------------------------------------------------------------------------
# §4/§7 persistence: remove, restore, restart
# ---------------------------------------------------------------------------

class TestPersistedStatusIsHonest:
    def test_a_removed_and_restored_working_key_stays_healthy(self, km):
        """The reported bug.

        A key that merely hit a transient 429, was deleted from the list, and
        later restored (backup restore / re-pasted secrets.toml) came back
        INVALID with zero auth errors and was refused by ``acquire``.
        """
        fp = fingerprint(K1)
        km.record_success(fp)
        km.record_failure(fp, Exception("429 RESOURCE_EXHAUSTED: quota"))

        km.mark_removed(fp)
        _seed(K2)                      # the teacher deleted it from the list
        assert km.status()["total"] == 1

        _seed(K1, K2)                  # …and later restored the same key
        restored = KeyManager()
        restored.sync_with_secret_store()
        shown = _status(restored, fp)
        assert shown["status"] != "INVALID", shown
        assert shown["auth_errors"] == 0
        assert restored.acquire()[0] in (fp, fingerprint(K2))

    def test_a_rejected_key_still_comes_back_invalid(self, km):
        """Genuine detection is preserved: a 401 is not erased by a restart."""
        fp = fingerprint(K1)
        km.record_failure(fp, Exception(REJECTED))
        assert _status(km, fp)["status"] == "INVALID"
        assert _status(km, fp)["auth_errors"] == 1

        restarted = KeyManager()
        restarted.sync_with_secret_store()
        assert _status(restarted, fp)["status"] == "INVALID"
        with pytest.raises(WaitForApiError):
            # only the rejected key is configured -> nothing to hand out
            _seed(K1)
            restarted2 = KeyManager()
            restarted2.sync_with_secret_store()
            restarted2.acquire()

    def test_restart_does_not_invalidate_previously_valid_keys(self, km):
        for fp in (fingerprint(K1), fingerprint(K2)):
            km.record_success(fp)
        km.record_failure(fingerprint(K2), Exception("429 RESOURCE_EXHAUSTED: quota"))

        restarted = KeyManager()
        restarted.sync_with_secret_store()
        for fp in (fingerprint(K1), fingerprint(K2)):
            assert _status(restarted, fp)["status"] != "INVALID"

    def test_a_stored_key_is_used_without_modification(self, km):
        stored = config.get_secrets().gemini_keys()
        assert stored == [K1, K2]                     # verbatim, not trimmed apart
        assert _status(km, fingerprint(K1))["fingerprint"] == fingerprint(K1)
        assert km._records[fingerprint(K1)].invalid is False

    def test_re_enable_clears_a_genuine_rejection(self, km):
        fp = fingerprint(K1)
        km.record_failure(fp, Exception(REJECTED))
        km.re_enable(fp)
        assert _status(km, fp)["status"] == "HEALTHY"
        km.acquire()


# ---------------------------------------------------------------------------
# §4 attribution: the aggregate error belongs to no single key
# ---------------------------------------------------------------------------

class TestAggregateErrorAttribution:
    def _leased_worker(self, key_manager):
        from app.workers.adapter import CorrectionWorker
        return CorrectionWorker("w-test", __import__("pathlib").Path("."), key_manager)

    def test_the_aggregate_error_from_a_child_cannot_invalidate_the_leased_key(self, fresh_state):
        """The child already recorded every real per-key outcome.

        Re-recording the job's aggregate against the leased key let ANOTHER
        key's 401 permanently invalidate a healthy key.
        """
        _seed(K1)
        manager = KeyManager()
        manager.sync_with_secret_store()
        fp = fingerprint(K1)
        worker = self._leased_worker(manager)
        worker.lease_key()

        worker.release_key(success=False, error=RuntimeError(AGGREGATE),
                           child_recorded=True)

        record = manager._records[fp]
        assert record.invalid is False, record.last_error
        assert record.auth_errors == 0, record.last_error
        assert manager.status()["healthy"] == 1

    def test_the_aggregate_error_alone_is_not_an_invalid_verdict(self):
        """Even without a child record, the aggregate must not read as "dead key"."""
        kind = classify_gemini_error(Exception(AGGREGATE))["kind"]
        assert kind != "invalid_key"


# ---------------------------------------------------------------------------
# §3/§5 the staged health probe
# ---------------------------------------------------------------------------

class _FakeProbeModels:
    def __init__(self, script):
        self._script = script

    def list(self):
        return [object()]

    def generate_content(self, *, model, contents, config=None, **kwargs):
        action = self._script()
        if isinstance(action, Exception):
            raise action
        return action


class _FakeProbeClient:
    def __init__(self, script):
        self.models = _FakeProbeModels(script)


class TestHealthProbeClassification:
    def _run(self, monkeypatch, script):
        from app.gemini import tester
        monkeypatch.setattr(tester, "_real_client",
                            lambda key: _FakeProbeClient(script))
        _seed(K1)
        return tester.test_key(1, K1, fingerprint(K1))

    def test_a_working_key_passes(self, monkeypatch, fresh_state):
        state = self._run(monkeypatch, lambda: SimpleNamespace(text="API_TEST_OK"))
        assert state.status == "HEALTHY"
        assert state.error_kind is None

    def test_a_rejected_credential_is_reported_invalid(self, monkeypatch, fresh_state):
        def script():
            raise RuntimeError(REJECTED)

        state = self._run(monkeypatch, script)
        assert state.status == "INVALID"
        assert state.error_kind == "invalid_key"

    def test_a_transient_failure_is_never_reported_invalid(self, monkeypatch, fresh_state):
        def script():
            raise RuntimeError("503 UNAVAILABLE: high demand")

        state = self._run(monkeypatch, script)
        assert state.status != "INVALID"
        assert state.status == "COOLDOWN"

    def test_an_unexpected_probe_reply_is_not_a_dead_key(self, monkeypatch, fresh_state):
        state = self._run(monkeypatch,
                          lambda: SimpleNamespace(text="I cannot help with that"))
        assert state.status != "INVALID"
        assert state.error_kind == "unexpected_response"

    def test_a_local_client_failure_is_not_a_credential_verdict(self, monkeypatch, fresh_state):
        """Constructing the SDK client is a LOCAL step.

        It used to be reported as ``invalid_key``, which the batch "Test All
        Keys" run then persisted — a local/config problem permanently struck a
        working credential off the rotation.
        """
        from app.gemini import tester

        def boom(key):
            raise TypeError("unsupported keyword argument 'http_options'")

        monkeypatch.setattr(tester, "_real_client", boom)
        _seed(K1)
        state = tester.test_key(1, K1, fingerprint(K1))

        assert state.status != "INVALID"
        assert state.error_kind != "invalid_key"
        # The message must not claim the credential was tested or rate limited.
        assert "not tested" in (state.error_message or "")
        # …and nothing was written to the key's stored status.
        manager = KeyManager()
        manager.sync_with_secret_store()
        assert _status(manager, fingerprint(K1))["status"] == "HEALTHY"


# ---------------------------------------------------------------------------
# §5 diagnostics never carry credentials
# ---------------------------------------------------------------------------

class TestDiagnosticStatus:
    def test_the_provider_status_is_read_from_a_real_status_position(self):
        assert http_status_of("401 UNAUTHENTICATED: API key not valid") == 401
        assert http_status_of(
            "{'error': {'code': 429, 'message': 'quota'}}") == 429
        assert http_status_of("HTTP 503 Service Unavailable") == 503

    def test_a_number_in_prose_is_not_reported_as_a_status(self):
        """Diagnostics must not invent a status out of prose.

        Reporting "401" for a 400 whose body mentioned 401 bytes is how a
        person reading the log concludes a working key is dead.
        """
        assert http_status_of("request contains 401 bytes of inline data") is None
        assert http_status_of("Connection reset by peer") is None
        assert http_status_of("") is None

    def test_classification_reports_the_status_alongside_the_category(self):
        rejected = classify_gemini_error(Exception(REJECTED))
        assert rejected["kind"] == "invalid_key" and rejected["status"] == 401
        assert classify_gemini_error(
            Exception("429 RESOURCE_EXHAUSTED: quota"))["status"] == 429
        assert classify_gemini_error(Exception(REJECTED)).get("status") is not None

    def test_logs_record_category_and_status_without_key_material(self, km, caplog):
        import logging as _logging
        fp = fingerprint(K1)
        with caplog.at_level(_logging.WARNING):
            km.record_failure(fp, Exception(REJECTED))
            km.record_failure(fingerprint(K2), Exception(
                "429 RESOURCE_EXHAUSTED: quota https://generativelanguage."
                "googleapis.com/v1beta/models/gemini-3.8-flash:generateContent?key="
                + K2))
        text = caplog.text
        assert "key marked INVALID" in text
        assert K1 not in text and K2 not in text
        assert "[REDACTED]" in text or "key=" not in text


class TestDiagnosticsStayClean:
    def test_status_and_stored_errors_never_carry_key_material(self, km):
        import json
        fp = fingerprint(K1)
        km.record_failure(fp, RuntimeError(
            "400 INVALID_ARGUMENT request failed for https://generativelanguage."
            "googleapis.com/v1beta/models/gemini-3.8-flash:generateContent?key=" + K1))
        blob = json.dumps(km.status())
        assert K1 not in blob and K2 not in blob
        assert "[REDACTED]" in (_status(km, fp)["last_error"] or "")
