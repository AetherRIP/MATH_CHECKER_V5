"""Key manager tests: rotation, cooldown, invalid keys, persistence."""

import time

import pytest

from app.config import get_secrets
from app.errors import classify_gemini_error
from app.gemini.key_manager import (
    KeyManager, KeyRecord, WaitForApiError, NoKeysError, fingerprint,
)


@pytest.fixture
def two_keys(fresh_state):
    get_secrets().set_gemini_keys(["AIzaTestKEYAAAA1111", "AIzaTestKEYBBBB2222"])
    km = KeyManager()
    km.sync_with_secret_store()
    return km


class TestRotation:
    def test_round_robin_balances_requests(self, two_keys):
        km = two_keys
        picks = [km.acquire()[0] for _ in range(6)]
        # Alternating pattern (not key1 until exhaustion)
        assert picks[0] != picks[1]
        counts = {fp: picks.count(fp) for fp in set(picks)}
        assert set(counts.values()) == {3}

    def test_no_keys_raises(self, fresh_state):
        km = KeyManager()
        km.sync_with_secret_store()
        with pytest.raises(NoKeysError):
            km.acquire()

    def test_all_cooling_raises_wait_for_api(self, two_keys):
        km = two_keys
        for fp, _ in [(km.acquire()[0], None) for _ in range(2)]:
            pass
        for fp in list(km._records.keys()):
            km._records[fp].cooldown_until = time.time() + 300
        with pytest.raises(WaitForApiError):
            km.acquire()

    def test_cooldown_redirects_traffic_to_healthy_key(self, two_keys):
        km = two_keys
        fps = [km.acquire()[0] for _ in range(2)]
        km.record_failure(fps[0], Exception("429 Resource Exhausted: quota"))
        # The rate-limited key is cooling down; all traffic goes to the other.
        picks = {km.acquire()[0] for _ in range(5)}
        assert picks == {fps[1]}

    def test_cooldown_expires_and_key_returns(self, two_keys):
        km = two_keys
        fp, _ = km.acquire()
        km.record_failure(fp, Exception("429 quota"))
        assert km.status()["cooldown"] == 1
        # simulate cooldown expiry
        km._records[fp].cooldown_until = time.time() - 1
        status = km.status()
        assert status["cooldown"] == 0
        assert status["healthy"] == 2


class TestFailureKinds:
    def test_invalid_key_marked_invalid(self, two_keys):
        km = two_keys
        # Exhaust every key with auth failures.
        for _ in range(2):
            fp, _rec = km.acquire()
            km.record_failure(fp, Exception("401 Unauthorized: API key not valid"))
        assert km.status()["invalid"] == 2
        with pytest.raises(WaitForApiError):
            km.acquire()  # nothing healthy left

    def test_one_invalid_key_leaves_the_other_usable(self, two_keys):
        km = two_keys
        fp, _ = km.acquire()
        km.record_failure(fp, Exception("401 Unauthorized: API key not valid"))
        assert km.status()["invalid"] == 1
        other_fp, _ = km.acquire()
        assert other_fp != fp

    def test_leaked_key_is_invalid(self, fresh_state):
        result = classify_gemini_error(Exception("API key reported as leaked"))
        assert result["kind"] == "invalid_key"

    def test_rate_limit_classification(self, fresh_state):
        assert classify_gemini_error(Exception("429 quota exceeded"))["kind"] == "rate_limited"

    def test_timeout_classification(self, fresh_state):
        assert classify_gemini_error(Exception("request timed out"))["kind"] == "timeout"

    def test_a_504_deadline_is_a_timeout_in_every_spelling(self, fresh_state):
        """The provider's own status text must not land in ``unknown``.

        An ``unknown`` kind let the job policy retry a dead 504 twice; the
        timeout budget is one retry.
        """
        payload = ("504 DEADLINE_EXCEEDED. {'error': {'code': 504, 'message': "
                   "'Deadline expired before operation could complete.', "
                   "'status': 'DEADLINE_EXCEEDED'}}")
        for message in (payload, "DEADLINE_EXCEEDED", "Deadline Exceeded",
                        "deadline exceeded"):
            assert classify_gemini_error(Exception(message))["kind"] == "timeout", \
                message

    def test_timeout_counted_not_cooldown(self, two_keys):
        km = two_keys
        fp, _ = km.acquire()
        km.record_failure(fp, Exception("deadline exceeded"))
        rec = km._records[fp]
        assert rec.timeout_errors == 1
        assert rec.cooldown_until == 0.0  # model fallback handles timeouts

    def test_exponential_cooldown_capped(self, two_keys):
        km = two_keys
        fp, _ = km.acquire()
        rec = km._records[fp]
        for _ in range(12):
            km.record_failure(fp, Exception("429 quota"))
        assert rec.cooldown_until - time.time() <= 601


class TestPersistence:
    def test_stats_survive_new_manager(self, two_keys):
        km = two_keys
        fp, _ = km.acquire()
        km.record_success(fp)
        km.record_failure(km.acquire()[0], Exception("429 quota"))

        km2 = KeyManager()
        km2.sync_with_secret_store()
        status = km2.status()
        by_fp = {k["fingerprint"]: k for k in status["keys"]}
        assert by_fp[fp]["successes"] == 1
        total_requests = sum(k["requests"] for k in status["keys"])
        assert total_requests == 2

    def test_fingerprint_never_reverses_key(self, fresh_state):
        fp = fingerprint("AIzaSomeSecretKeyMaterial")
        assert "AIza" not in fp
        assert len(fp) == 12

    def test_re_enable_clears_invalid(self, two_keys):
        km = two_keys
        fp, _ = km.acquire()
        km.record_failure(fp, Exception("401 invalid api key"))
        assert km.status()["invalid"] == 1
        km.re_enable(fp)
        assert km.status()["invalid"] == 0
        km.acquire()  # usable again


class TestStatus:
    def test_status_never_contains_key_material(self, two_keys):
        km = two_keys
        km.acquire()
        import json
        text = json.dumps(km.status())
        assert "AIzaTestKEY" not in text

    def test_a_stored_provider_error_carries_no_key(self, fresh_state):
        """``last_error`` is the one error string this app keeps.

        It is written to ``gemini_keys.last_error`` and returned by
        ``/api/keys``, so a provider error that echoes the request URL (the
        SDK does exactly that for some 400s) must be redacted on the way in.
        """
        key = "AIzaSyC0de" + "x" * 28          # a realistic key length
        get_secrets().set_gemini_keys([key])
        km = KeyManager()
        fp = fingerprint(key)
        km.record_failure(fp, RuntimeError(
            "400 INVALID_ARGUMENT https://generativelanguage.googleapis.com/"
            "v1beta/models/gemini-3.6-flash:generateContent?key=" + key))

        shown = {k["fingerprint"]: k for k in km.status()["keys"]}[fp]
        assert key not in (shown["last_error"] or "")
        assert "[REDACTED]" in shown["last_error"]
