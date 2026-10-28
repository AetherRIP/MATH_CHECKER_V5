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


class TestRecovery:
    """A key sidelined by a failure must come back when it is proven to work.

    INVALID is meant to mean "a credential rejection was recorded". It used to
    be a one-way latch: ``acquire`` and ``available`` skip an invalid key, and
    neither success path cleared the flag, so a key that was rejected once —
    even by a misclassified transient error — never served another request and
    survived every restart as invalid. The only escape was the manual
    re-enable button. That is lost quota, and it is the reason a key that
    demonstrably works is still reported as invalid.

    A real success is stronger evidence than the failure that set the flag, so
    it clears it. A key that is genuinely rejected never records a success, so
    it stays invalid.
    """

    def test_a_verified_success_clears_a_stale_invalid_mark(self, two_keys):
        km = two_keys
        fp, _ = km.acquire()
        km.record_failure(fp, Exception("401 Unauthorized: API key not valid"))
        assert fp not in [r.fingerprint for r in km.available()]

        km.record_success(fp)

        assert fp in [r.fingerprint for r in km.available()], \
            "a key that just served a request must be usable again"
        shown = {k["fingerprint"]: k for k in km.status()["keys"]}[fp]
        assert shown["status"] == "HEALTHY"
        assert shown["auth_errors"] == 1, "the recorded rejection stays on the record"

    def test_a_staged_test_success_clears_a_stale_invalid_mark(self, two_keys):
        km = two_keys
        fp, _ = km.acquire()
        km.record_failure(fp, Exception("401 Unauthorized: API key not valid"))
        assert km.status()["invalid"] == 1

        km.register_manual_result(fp, ok=True, kind="ok", message="")

        assert km.status()["invalid"] == 0
        assert fp in [r.fingerprint for r in km.available()]

    def test_recovery_survives_a_restart(self, two_keys):
        km = two_keys
        fp, _ = km.acquire()
        km.record_failure(fp, Exception("401 Unauthorized: API key not valid"))
        km.record_success(fp)

        km2 = KeyManager()
        km2.sync_with_secret_store()

        assert fp in [r.fingerprint for r in km2.available()]

    def test_a_key_that_never_succeeds_stays_invalid(self, two_keys):
        km = two_keys
        fps = [km.acquire()[0] for _ in range(2)]
        for fp in fps:
            km.record_failure(fp, Exception("401 Unauthorized: API key not valid"))
            assert fp not in [r.fingerprint for r in km.available()]
        assert km.status()["invalid"] == 2
        assert km.available() == []

    def test_a_rate_limit_alone_never_marks_a_key_invalid(self, two_keys):
        km = two_keys
        fp, _ = km.acquire()
        km.record_failure(fp, Exception("429 Resource Exhausted: quota"))
        km.record_success(fp)

        shown = {k["fingerprint"]: k for k in km.status()["keys"]}[fp]
        assert shown["status"] == "HEALTHY"
        assert shown["auth_errors"] == 0


class TestProbation:
    """Nothing may be permanently dead without a human.

    ``acquire`` skips invalid keys, so an invalid key is never sent a request
    and therefore never records the success that would clear the flag. When
    every key is invalid the app can never correct anything again — the
    reported "No usable Gemini keys remain" dead end. A key that was flagged a
    while ago gets one bounded trial: if the credential really is dead the
    trial fails and re-parks it, and if it was flagged by mistake the trial
    restores it.
    """

    def _make_invalid(self, km):
        import app.gemini.key_manager as key_manager_module
        fps = [km.acquire()[0] for _ in range(2)]
        for fp in fps:
            km.record_failure(fp, Exception("401 Unauthorized: API key not valid"))
        assert km.available() == []
        # Age the failures past the probation window.
        aged = time.time() - (key_manager_module.PROBATION_SECONDS + 1)
        for fp in fps:
            km._records[fp].last_failure = aged
        return fps

    def test_all_invalid_is_not_a_permanent_dead_end(self, two_keys):
        km = two_keys
        fps = self._make_invalid(km)

        fp, _rec = km.acquire()

        assert fp in fps, "one flagged key must get a bounded trial"

    def test_a_key_that_just_failed_is_not_probed_again(self, two_keys):
        km = two_keys
        for fp in [km.acquire()[0] for _ in range(2)]:
            km.record_failure(fp, Exception("401 Unauthorized: API key not valid"))

        with pytest.raises(WaitForApiError):
            km.acquire()

    def test_each_key_gets_one_trial_and_no_more(self, two_keys):
        """One trial per KEY, then the pool stops asking.

        A pool of flagged keys is revalidated one key at a time — never the
        same key twice inside the window, and nothing at all once every key
        has had its turn.
        """
        km = two_keys
        fps = self._make_invalid(km)

        tried = [km.acquire()[0], km.acquire()[0]]

        assert sorted(tried) == sorted(fps)
        with pytest.raises(WaitForApiError):
            km.acquire()

    def test_a_trial_that_works_restores_the_key(self, two_keys):
        km = two_keys
        self._make_invalid(km)

        fp, _rec = km.acquire()
        km.record_success(fp)

        assert fp in [r.fingerprint for r in km.available()]
        assert km.status()["healthy"] == 1

    def test_a_trial_that_fails_puts_that_key_back_to_sleep(self, two_keys):
        km = two_keys
        self._make_invalid(km)

        failed, _rec = km.acquire()
        km.record_failure(failed, Exception("401 Unauthorized: API key not valid"))

        # The key that just failed is not offered again in this window; the
        # other flagged key takes its own turn, then the pool stops.
        other, _rec2 = km.acquire()
        assert other != failed
        with pytest.raises(WaitForApiError):
            km.acquire()
        assert km.available() == []

    def test_an_aged_key_is_tried_when_the_pool_is_only_cooling(self, two_keys):
        """A pool that is cooling must not fall back to nothing while flagged
        keys exist: a single usable key that rate-limits would otherwise stall
        the queue with a dozen recoverable keys sitting idle."""
        km = two_keys
        usable, _ = km.acquire()
        other = next(f for f in km._records if f != usable)
        rec = km._records[other]
        rec.invalid = True
        rec.last_failure = 0.0
        rec.save()
        km._records[usable].cooldown_until = time.time() + 600

        fp, _rec = km.acquire()

        assert fp == other

    def test_probation_never_touches_a_configured_healthy_key(self, two_keys):
        km = two_keys
        fp, _rec = km.acquire()
        km.record_failure(fp, Exception("401 Unauthorized: API key not valid"))

        # One key is still usable, so no trial is offered: the working key
        # serves every request.
        picks = {km.acquire()[0] for _ in range(5)}
        assert len(picks) == 1 and fp not in picks


class TestPermissionPark:
    """A 403 is not a rate limit, and it is not a bad credential.

    The key reaches Gemini and is refused: the project is not allowed to use
    the API/model. Retrying it on the next turn spends a real request on an
    answer that cannot change, so it is parked — for longer than a rate-limit
    cooldown, and still recoverable. It must never be marked INVALID: the
    credential was accepted, only its permissions were refused.
    """

    FORBIDDEN = "403 PERMISSION_DENIED: The caller does not have permission to use this API."

    def test_the_message_really_classifies_as_forbidden(self, fresh_state):
        assert classify_gemini_error(Exception(self.FORBIDDEN))["kind"] == "forbidden"

    def test_a_forbidden_key_is_taken_out_of_rotation(self, two_keys):
        km = two_keys
        fp = km.acquire()[0]
        km.record_failure(fp, Exception(self.FORBIDDEN))

        picks = {km.acquire()[0] for _ in range(6)}
        assert picks == {f for f in km._records if f != fp}, \
            "a refused key must not keep receiving requests"

    def test_a_forbidden_key_is_parked_longer_than_a_rate_limit(self, two_keys):
        km = two_keys
        limited_fp, forbidden_fp = km.acquire()[0], km.acquire()[0]
        km.record_failure(limited_fp, Exception("429 quota exceeded"))
        km.record_failure(forbidden_fp, Exception(self.FORBIDDEN))

        forbidden = km._records[forbidden_fp]
        limited = km._records[limited_fp]
        assert forbidden.cooldown_kind == "forbidden"
        assert forbidden.cooldown_until - time.time() >= 600, \
            "a permission refusal must not be retried a minute later"
        assert forbidden.cooldown_until > limited.cooldown_until

    def test_a_forbidden_key_is_never_marked_invalid(self, two_keys):
        km = two_keys
        fp = km.acquire()[0]
        km.record_failure(fp, Exception(self.FORBIDDEN))

        rec = km._records[fp]
        assert rec.invalid is False
        assert rec.auth_errors == 0
        assert km.status()["invalid"] == 0

    def test_a_parked_key_returns_when_its_window_expires(self, two_keys):
        km = two_keys
        fp = km.acquire()[0]
        km.record_failure(fp, Exception(self.FORBIDDEN))

        km._records[fp].cooldown_until = time.time() - 1

        assert fp in {km.acquire()[0] for _ in range(4)}


class TestBillingPark:
    """A project with no Gemini entitlement: valid key, no quota to give.

    Google reports it as 403 PERMISSION_DENIED, but the remedy is billing, not
    permissions — so it gets its own kind and a six-hour park. It must never be
    INVALID (the credential is fine) and must not be re-probed on the
    rate-limit cadence, which would spend a request an hour per such key.
    """

    # Verbatim payload from a real key whose project has no free tier.
    NO_FREE_TIER = (
        '403 PERMISSION_DENIED. {\'error\': {\'code\': 403, \'message\': "The '
        'Gemini API free tier is not available for this project. To use the '
        'Gemini API, please set up billing to upgrade to the paid tier at '
        'https://aistudio.google.com/billing.", \'status\': '
        '"PERMISSION_DENIED"}}')

    def test_the_message_classifies_as_billing_not_forbidden(self, fresh_state):
        verdict = classify_gemini_error(Exception(self.NO_FREE_TIER))
        assert verdict["kind"] == "billing_required"
        assert "billing" in verdict["message"].lower()

    def test_a_billing_blocked_key_is_never_marked_invalid(self, two_keys):
        km = two_keys
        fp = km.acquire()[0]
        km.record_failure(fp, Exception(self.NO_FREE_TIER))

        rec = km._records[fp]
        assert rec.invalid is False
        assert rec.auth_errors == 0
        assert km.status()["invalid"] == 0

    def test_a_billing_blocked_key_is_parked_for_hours(self, two_keys):
        km = two_keys
        limited_fp, billing_fp = km.acquire()[0], km.acquire()[0]
        km.record_failure(limited_fp, Exception("429 quota exceeded"))
        km.record_failure(billing_fp, Exception(self.NO_FREE_TIER))

        rec = km._records[billing_fp]
        assert rec.cooldown_kind == "billing_required"
        remaining = rec.cooldown_until - time.time()
        assert remaining >= 4 * 3600, \
            "a project without a Gemini entitlement must not be retried hourly"
        assert remaining > km._records[limited_fp].cooldown_until - time.time()

    def test_a_billing_blocked_key_leaves_the_rotation(self, two_keys):
        km = two_keys
        fp = km.acquire()[0]
        km.record_failure(fp, Exception(self.NO_FREE_TIER))

        picks = {km.acquire()[0] for _ in range(6)}
        assert picks == {f for f in km._records if f != fp}

    def test_a_project_that_gets_billing_back_returns(self, two_keys):
        km = two_keys
        fp = km.acquire()[0]
        km.record_failure(fp, Exception(self.NO_FREE_TIER))

        # The user enabled billing; the park simply expires.
        km._records[fp].cooldown_until = time.time() - 1
        assert fp in {km.acquire()[0] for _ in range(4)}
        km.record_success(fp)
        assert km._records[fp].cooldown_until == 0

    def test_a_test_all_verdict_parks_it_exactly_like_a_real_failure(self, two_keys):
        """Otherwise the next correction relearns it with a full marking call.

        The staged test used to record the verdict and drop the key straight
        back into rotation, so "Test All" on a pool with a dead project was
        followed by real marking requests being spent on that same key.
        """
        km = two_keys
        tested, spared = km.acquire()[0], km.acquire()[0]
        km.register_manual_result(
            tested, ok=False, kind="billing_required",
            message="free tier is not available for this project")

        rec = km._records[tested]
        assert rec.invalid is False, "the credential is fine, only the project"
        assert rec.cooldown_until - time.time() >= 4 * 3600
        picks = {km.acquire()[0] for _ in range(6)}
        assert picks == {spared}

    def test_a_test_all_verdict_parks_a_permission_refusal_too(self, two_keys):
        km = two_keys
        tested, spared = km.acquire()[0], km.acquire()[0]
        km.register_manual_result(
            tested, ok=False, kind="forbidden",
            message="403 PERMISSION_DENIED")

        rec = km._records[tested]
        assert rec.invalid is False
        assert rec.cooldown_until - time.time() >= 600
        assert {km.acquire()[0] for _ in range(6)} == {spared}

    def test_a_test_all_pass_still_clears_a_park(self, two_keys):
        km = two_keys
        fp = km.acquire()[0]
        km.record_failure(fp, Exception(self.NO_FREE_TIER))
        km.register_manual_result(fp, ok=True, kind="healthy", message="ok")

        assert km._records[fp].cooldown_until == 0
        assert km._records[fp].invalid is False


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
