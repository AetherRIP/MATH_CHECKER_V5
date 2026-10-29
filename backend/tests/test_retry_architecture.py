"""Retry/failover architecture tests (spec §17) — MOCKED failures only.

No real model/API quota is consumed anywhere in this file: provider
failures are simulated with scripted fake clients and pure classification
checks. Covered (per §17):

* 429 → key cooldown + failover to a healthy key
* 503 → key cooldown + failover + provider gate
* authentication error → NO retry loop, key marked invalid
* timeout / invalid request → exactly one request (never a blind re-send)
* success on first attempt → zero retries
* failure after the retry budget → FAILED (never an infinite loop)
* submission downloaded once, mark scheme prepared once, PDF normalized
  once — repeated retries never repeat downloads/preparation
* successful results are never overwritten by duplicate corrections
* Clear Log never destroys API usage statistics
"""

import io
import json
import threading
import time
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

import app.config as config
import app.db as db
from app import retry_policy as rp


@pytest.fixture(autouse=True)
def _clean_retry_state():
    """No gate/stats leakage between tests (fresh provider + counters)."""
    from app import api_stats
    from app.gemini.provider_health import provider_health
    provider_health().reset()
    api_stats.reset()
    yield
    provider_health().reset()
    api_stats.reset()


# ============================================================ classification

class TestFailureClassification:
    def test_rate_limit_parks(self):
        f = rp.classify_failure(Exception("429 Resource Exhausted: quota exceeded"))
        assert f.cls == rp.RATE_LIMIT and f.park and f.retryable

    def test_high_demand_parks(self):
        f = rp.classify_failure(Exception("503 The service is currently unavailable — high demand"))
        assert f.cls == rp.HIGH_DEMAND and f.park

    def test_timeout_limited_to_one_retry(self):
        f = rp.classify_failure(Exception("request timed out"))
        assert f.cls == rp.TIMEOUT and f.max_retries == 1

    def test_network_error_limited(self):
        f = rp.classify_failure(Exception("connection reset by peer"))
        assert f.cls == rp.NETWORK_ERROR and f.max_retries == 1

    def test_authentication_never_retries(self):
        for msg in ("401 Unauthorized: API key not valid",
                    "403 permission denied"):
            f = rp.classify_failure(Exception(msg))
            assert f.cls == rp.AUTHENTICATION_ERROR
            assert not f.retryable and f.max_retries == 0

    def test_invalid_request_never_retries(self):
        f = rp.classify_failure(Exception("400 invalid argument: malformed request"))
        assert f.cls == rp.INVALID_REQUEST and not f.retryable

    def test_context_too_large_never_identical_retry(self):
        f = rp.classify_failure(Exception("context window exceeded: too many tokens"))
        assert f.cls == rp.CONTEXT_TOO_LARGE and not f.retryable

    def test_parsing_error_gets_one_controlled_retry(self):
        f = rp.classify_failure(Exception("failed to parse response: expecting value"))
        assert f.cls == rp.PARSING_ERROR and f.max_retries == 1

    def test_validation_error_one_retry_not_zero(self):
        # Spec: a validation error must NOT become a zero — at most one
        # controlled correction retry with the already-prepared input.
        f = rp.classify_failure(Exception("response format validation failed: schema"))
        assert f.cls == rp.VALIDATION_ERROR
        assert f.max_retries == 1 and f.retryable

    def test_unknown_very_limited(self):
        f = rp.classify_failure(Exception("something nobody has ever seen"))
        assert f.cls == rp.UNKNOWN_ERROR and f.max_retries == 1

    def test_all_keys_cooling_is_a_park_not_a_failure(self):
        f = rp.classify_failure(Exception("All Gemini keys are cooling down after failures."))
        assert f.cls == rp.API_UNAVAILABLE and f.park

    def test_no_keys_configured_fails_fast(self):
        f = rp.classify_failure(Exception("No Gemini API keys are configured."))
        assert f.cls == rp.AUTHENTICATION_ERROR and not f.retryable

    def test_staging_config_problem_fails_fast(self):
        f = rp.classify_failure(Exception(
            "No mark scheme has been uploaded for this assignment yet."))
        assert f.cls == rp.INVALID_REQUEST and not f.retryable

    def test_worker_child_kind_mapping(self):
        assert rp.failure_for_kind("invalid_key").cls == rp.AUTHENTICATION_ERROR
        assert rp.failure_for_kind("rate_limited").cls == rp.RATE_LIMIT
        assert rp.failure_for_kind("timeout").cls == rp.TIMEOUT
        assert rp.failure_for_kind("temporary", "503 unavailable").cls == rp.HIGH_DEMAND
        assert rp.failure_for_kind("worker_crash").cls == rp.WORKER_CRASH
        assert rp.failure_for_kind("unknown", "connection refused").cls == rp.NETWORK_ERROR

    def test_provider_cooling_error_is_a_park(self):
        from app.gemini.client_proxy import ProviderCoolingError
        f = rp.classify_failure(ProviderCoolingError(
            "Gemini provider is cooling down after high demand — request "
            "not sent (30s cooldown remaining)."))
        assert f.cls == rp.HIGH_DEMAND and f.park


# ================================================================= backoff

class TestBackoff:
    def test_exponential_with_jitter_bounds(self):
        d1 = [rp.compute_backoff(1) for _ in range(30)]
        d2 = [rp.compute_backoff(2) for _ in range(30)]
        d3 = [rp.compute_backoff(3) for _ in range(30)]
        assert all(1.4 <= d <= 2.7 for d in d1)     # ~2s
        assert all(2.9 <= d <= 5.5 for d in d2)     # ~4s
        assert all(5.9 <= d <= 11 for d in d3)      # ~8s
        assert min(d3) > max(d1)                    # always grows

    def test_backoff_is_capped(self):
        for i in range(4, 12):
            assert rp.compute_backoff(i) <= rp.BACKOFF_CAP_SECONDS * 1.2 + 0.01

    def test_retry_after_hint_wins_when_larger(self):
        d = rp.compute_backoff(1, retry_after=45)
        assert d >= 45

    def test_absurd_retry_after_is_clamped(self):
        assert rp.parse_retry_after("retry after 9999999s") is None

    def test_parse_retry_after_variants(self):
        assert rp.parse_retry_after("Please retry in 42s") == 42
        assert rp.parse_retry_after("retry-after: 30 seconds later") is not None
        assert rp.parse_retry_after("retry_delay { seconds: 12 }") == 12
        assert rp.parse_retry_after("no hints here") is None


# ================================================================ decisions

class TestDecision:
    def test_retryable_failure_gets_a_backoff_retry(self):
        d = rp.decide(rp.failure_for(rp.UNKNOWN_ERROR), attempts=1,
                      max_attempts=3, cfg_max_retries=2)
        assert d.action == rp.ACTION_RETRY and d.delay >= 0.5

    def test_retry_budget_exhausted_fails(self):
        d = rp.decide(rp.failure_for(rp.TIMEOUT), attempts=2,
                      max_attempts=3, cfg_max_retries=2)
        assert d.action == rp.ACTION_FAIL and d.error_code == "timeout"

    def test_authentication_fails_immediately_even_with_attempts_left(self):
        d = rp.decide(rp.failure_for(rp.AUTHENTICATION_ERROR), attempts=1,
                      max_attempts=3, cfg_max_retries=2)
        assert d.action == rp.ACTION_FAIL
        assert d.error_code == "authentication_error"

    def test_config_can_zero_the_retry_budget(self):
        d = rp.decide(rp.failure_for(rp.UNKNOWN_ERROR), attempts=1,
                      max_attempts=3, cfg_max_retries=0)
        assert d.action == rp.ACTION_FAIL

    def test_park_ignores_attempts_but_respects_wait_budget(self):
        ok = rp.decide(rp.failure_for(rp.RATE_LIMIT), attempts=9, max_attempts=3,
                       cfg_max_retries=2, park_hint_delay=20)
        assert ok.action == rp.ACTION_PARK and ok.delay == 20.0
        over = rp.decide(rp.failure_for(rp.RATE_LIMIT), attempts=1,
                         max_attempts=3, cfg_max_retries=2,
                         waited_seconds=14 * 60, park_hint_delay=60 * 60)
        assert over.action == rp.ACTION_FAIL
        assert over.error_code == "all_keys_unavailable"

    def test_job_cap_still_bounds_retries(self):
        d = rp.decide(rp.failure_for(rp.TRANSIENT_PROVIDER_ERROR),
                      attempts=3, max_attempts=3, cfg_max_retries=2)
        assert d.action == rp.ACTION_FAIL


# ============================================== key cooldowns + Retry-After

def _provider_429(quota_id: str, retry_delay: str) -> Exception:
    """A 429 shaped exactly like Google's own body, including the details the
    classification must read instead of guessing from the word "quota"."""
    class _ProviderError(Exception):
        def __init__(self, payload: dict):
            self.details = payload
            super().__init__(f"429 RESOURCE_EXHAUSTED. {json.dumps(payload)}")

    return _ProviderError({"error": {
        "code": 429,
        "message": "You exceeded your current quota, please check your plan and billing details.",
        "status": "RESOURCE_EXHAUSTED",
        "details": [
            {"@type": "type.googleapis.com/google.rpc.QuotaFailure",
             "violations": [{"quotaMetric": "generativelanguage.googleapis.com/generate_content_free_tier_requests",
                             "quotaId": quota_id, "quotaValue": "10"}]},
            {"@type": "type.googleapis.com/google.rpc.RetryInfo",
             "retryDelay": retry_delay},
        ]}})


class TestKeyCooldownPolicy:
    @pytest.fixture
    def km(self, fresh_state):
        config.get_secrets().set_gemini_keys(["K-alpha-000000000001",
                                              "K-beta-0000000000002"])
        from app.gemini.key_manager import KeyManager
        manager = KeyManager()
        manager.sync_with_secret_store()
        return manager

    def test_503_cools_the_key_and_traffic_moves_over(self, km):
        fp, _ = km.acquire()
        km.record_failure(fp, Exception("503 The service is currently unavailable"))
        status = km.status()
        assert status["cooldown"] == 1 and status["healthy"] == 1
        picks = {km.acquire()[0] for _ in range(4)}
        assert picks == {next(k for k in status["keys"]
                              if k["fingerprint"] != fp)["fingerprint"]}

    def test_per_minute_429_is_a_rate_limit_not_an_exhausted_day(self, km):
        fp, _ = km.acquire()
        km.record_failure(fp, _provider_429(
            "GenerateRequestsPerMinutePerProjectPerModel-FreeTier", "7s"))
        rec = km._records[fp]
        assert rec.rate_limit_events == 1
        assert rec.quota_errors == 0          # a burst is not quota exhaustion
        assert rec.cooldown_until - time.time() < 120

    def test_daily_quota_429_parks_the_key_for_much_longer(self, km):
        fp, _ = km.acquire()
        km.record_failure(fp, _provider_429(
            "GenerateRequestsPerDayPerProjectPerModel-FreeTier", "33s"))
        rec = km._records[fp]
        assert rec.quota_errors == 1
        assert rec.cooldown_until - time.time() >= 1_500

    def test_429_without_quota_evidence_is_not_reported_as_an_exhausted_day(self, km):
        fp, _ = km.acquire()
        km.record_failure(fp, Exception("429 RESOURCE_EXHAUSTED: slow down"))
        rec = km._records[fp]
        assert rec.quota_errors == 0
        assert rec.cooldown_until - time.time() < 120

    def test_retry_after_hint_extends_cooldown(self, km):
        fp, _ = km.acquire()
        km.record_failure(fp, Exception("429 quota — please retry in 120s"))
        rec = km._records[fp]
        assert rec.cooldown_until - time.time() >= 110

    def test_timeout_still_does_not_cool_the_key(self, km):
        fp, _ = km.acquire()
        km.record_failure(fp, Exception("request timed out"))
        assert km._records[fp].cooldown_until == 0.0


# =========================================== scripted client (no network)

def _seed_keys(*keys):
    config.get_secrets().set_gemini_keys(list(keys))


class _ScriptedModels:
    """generate_content fake: script(call_number) → Exception to raise or a
    response to return. One instance is shared by every SDK client, so the
    call counter measures TOTAL requests sent across keys."""

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
    _seed_keys(*keys)
    manager = KeyManager()
    manager.sync_with_secret_store()
    client = KeyAwareClient(manager)
    models = _ScriptedModels(script)
    sdk = SimpleNamespace(models=models)
    monkeypatch.setattr(client, "_sdk_client_for_key", lambda key: sdk,
                        raising=False)
    return client, manager, models


class TestKeyFailoverMocked:
    def test_429_cools_key_and_fails_over_to_healthy_key(self, monkeypatch, fresh_state):
        client, km, models = _proxy(
            monkeypatch,
            lambda n: Exception("429 Resource Exhausted: quota exceeded")
            if n == 1 else "ok-response",
            "K-one-0000000000000001", "K-two-0000000000000002")
        assert client.generate_content(model="m", contents=["x"]) == "ok-response"
        assert models.calls == 2          # one failure + ONE failover
        status = km.status()
        assert status["cooldown"] == 1 and status["healthy"] == 1
        stats = _stats()
        assert stats["requests"] == 2 and stats["failures"] == 1
        assert stats["successes"] == 1 and stats["failovers"] == 1
        assert stats["cooldowns"] >= 1

    def test_503_cools_key_fails_over_and_provider_stays_healthy(
            self, monkeypatch, fresh_state):
        client, km, models = _proxy(
            monkeypatch,
            lambda n: Exception("503 high demand — service overloaded")
            if n == 1 else "ok-response",
            "K-one-0000000000000001", "K-two-0000000000000002")
        assert client.generate_content(model="m", contents=["x"]) == "ok-response"
        assert models.calls == 2
        assert km.status()["cooldown"] == 1
        from app.gemini.provider_health import provider_health
        snap = provider_health().snapshot()
        assert snap["available"] is True and snap["consecutive_high_demand"] == 0

    def test_authentication_error_never_loops(self, monkeypatch, fresh_state):
        client, km, models = _proxy(
            monkeypatch, lambda n: Exception("401 Unauthorized: API key not valid"),
            "K-solo-0000000000000001")
        with pytest.raises(Exception) as exc:
            client.generate_content(model="m", contents=["x"])
        assert "401" in str(exc.value)
        assert models.calls == 1              # NO repeated retry loop
        assert km.status()["invalid"] == 1    # key marked invalid
        stats = _stats()
        assert stats["requests"] == 1 and stats["failures"] == 1

    def test_timeout_sends_exactly_one_request(self, monkeypatch, fresh_state):
        client, _km, models = _proxy(
            monkeypatch, lambda n: Exception("request timed out"),
            "K-one-0000000000000001", "K-two-0000000000000002")
        with pytest.raises(Exception):
            client.generate_content(model="m", contents=["x"])
        assert models.calls == 1        # outcome unknown → do not duplicate

    def test_invalid_request_is_never_resent(self, monkeypatch, fresh_state):
        client, _km, models = _proxy(
            monkeypatch,
            lambda n: Exception("400 invalid argument: malformed request"),
            "K-one-0000000000000001", "K-two-0000000000000002")
        with pytest.raises(Exception):
            client.generate_content(model="m", contents=["x"])
        assert models.calls == 1

    def test_success_first_attempt_uses_zero_retries(self, monkeypatch, fresh_state):
        client, _km, models = _proxy(monkeypatch, lambda n: "ok-response",
                                     "K-one-0000000000000001")
        assert client.generate_content(model="m", contents=["x"]) == "ok-response"
        assert models.calls == 1
        stats = _stats()
        assert stats["requests"] == 1 and stats["failures"] == 0
        assert stats["failovers"] == 0 and stats["retries"] == 0

    def test_provider_gate_blocks_request_before_any_bytes_sent(
            self, monkeypatch, fresh_state):
        from app.gemini.provider_health import provider_health
        for _ in range(3):
            provider_health().record_failure(rp.HIGH_DEMAND, "503 high demand")
        assert provider_health().available()[0] is False
        client, _km, models = _proxy(
            monkeypatch, lambda n: "ok-response", "K-one-0000000000000001")
        with pytest.raises(Exception) as exc:
            client.generate_content(model="m", contents=["x"])
        assert "cooling down" in str(exc.value)
        assert models.calls == 0          # the giant request was NOT sent
        assert _stats()["requests"] == 0


def _stats():
    from app import api_stats
    return api_stats.snapshot()


# ======================================================= provider gate

class TestProviderHealth:
    def test_gate_opens_only_after_consecutive_high_demand(self):
        from app.gemini.provider_health import ProviderHealth
        health = ProviderHealth()
        health.record_failure(rp.HIGH_DEMAND)
        health.record_failure(rp.HIGH_DEMAND)
        assert health.available()[0] is True     # failover first
        health.record_failure(rp.HIGH_DEMAND)
        ok, wait = health.available()
        assert not ok and 0 < wait <= 610

    def test_success_or_reset_reopens_the_gate(self):
        from app.gemini.provider_health import ProviderHealth
        health = ProviderHealth()
        for _ in range(4):
            health.record_failure(rp.HIGH_DEMAND)
        assert health.available()[0] is False
        health.reset()
        assert health.available()[0] is True
        assert health.snapshot()["cooldowns"] == 0

    def test_non_overload_failures_never_close_the_gate(self):
        from app.gemini.provider_health import ProviderHealth
        health = ProviderHealth()
        for _ in range(10):
            health.record_failure(rp.TIMEOUT, "request timed out")
        assert health.available()[0] is True


# ======================================================= key-availability gate

class TestKeyAvailabilityGate:
    """No key can serve a request => no worker, no child, no prompt.

    The recorded outage showed one paper starting 60 times because every
    capacity park requeued it and the dispatcher started a fresh child for a
    request the keys could only refuse.
    """

    _COOLING = {
        "total": 3, "healthy": 0, "cooldown": 3, "invalid": 0,
        "disabled": 0, "has_keys": True, "all_down": True,
        "keys": [{"status": "COOLDOWN", "cooldown_remaining": 30} for _ in range(3)],
    }

    _HEALTHY = {
        "total": 3, "healthy": 2, "cooldown": 1, "invalid": 0,
        "disabled": 0, "has_keys": True, "all_down": False,
        "keys": [{"status": "HEALTHY", "cooldown_remaining": 0}],
    }

    def test_dispatch_pauses_while_every_key_is_cooling(self, fresh_state, monkeypatch):
        pool = _pool()
        monkeypatch.setattr(pool.keys, "status", lambda: dict(self._COOLING))
        job_id = _insert_job(status="QUEUED", attempts=0)

        pool._keys_next_check = 0.0
        pool._dispatch_queued()

        row = _job_row(job_id)
        assert row["status"] == "QUEUED", "nothing may be launched without a key"
        assert pool._keys_blocked is True
        assert "Waiting for an API key" in (row["stage_detail"] or "")

        # The first key that frees up reopens the queue, and the message clears.
        monkeypatch.setattr(pool.keys, "status", lambda: dict(self._HEALTHY))
        pool._keys_next_check = 0.0
        assert pool._keys_ready() is True
        assert pool._keys_blocked is False

    def test_a_configured_but_empty_key_store_is_not_this_gate(self, fresh_state):
        """No keys at all is a setup problem, handled with an actionable error."""
        pool = _pool()
        pool.keys.status = lambda: {"total": 0, "healthy": 0, "cooldown": 0,
                                    "invalid": 0, "disabled": 0,
                                    "has_keys": False, "all_down": False,
                                    "keys": []}
        pool._keys_next_check = 0.0
        assert pool._keys_ready() is True

    def test_park_delay_follows_the_first_key_that_recovers(self, fresh_state):
        pool = _pool()
        pool.keys.status = lambda: {
            "total": 2, "healthy": 0, "cooldown": 2, "invalid": 0, "disabled": 0,
            "has_keys": True, "all_down": True,
            "keys": [{"cooldown_remaining": 40}, {"cooldown_remaining": 6}],
        }
        # Waiting the LATEST cooldown would sleep through the window in which
        # the second key is already usable.
        assert pool._capacity_delay() == 8.0


class TestStopForDiagnosis:
    """A retry condition stops the job and keeps the evidence.

    The whole point of this policy: no job may be sent again just because it
    failed again. Past the retry budget the job is FAILED with
    ``retry_suspended``, the diagnosis is in its stage history, and the teacher
    resumes it deliberately.
    """

    def test_capacity_parks_stop_a_job_instead_of_grinding_the_budget(
            self, fresh_state, monkeypatch):
        timers = _fake_timers(monkeypatch)
        pool = _pool()
        job_id = _insert_job(status="WAITING_FOR_API", attempts=0)

        for _ in range(pool.MAX_PARKS_BEFORE_SUSPEND):
            pool._park_job(job_id, 5.0, reason="RATE_LIMIT: waiting for API capacity")
        assert _job_row(job_id)["status"] == "WAITING_FOR_API"

        pool._park_job(job_id, 5.0, reason="RATE_LIMIT: waiting for API capacity")

        row = _job_row(job_id)
        assert row["status"] == "FAILED"
        assert row["error_code"] == "retry_suspended"
        assert "Stopped for diagnosis" in (row["stage_detail"] or "")
        # The evidence survives: stage history names the stop.
        stages = [r["stage"] for r in db.query(
            "SELECT stage FROM job_stage_events WHERE job_id = ? ORDER BY id", (job_id,))]
        assert any("Stopped for diagnosis" in (s or "") for s in stages)
        # And no further attempt was scheduled for it.
        assert not [t for t in timers if job_id in str(t)]

    def test_a_job_already_retried_past_budget_is_stopped_not_reissued(
            self, fresh_state, monkeypatch):
        _fake_timers(monkeypatch)
        pool = _pool()
        job_id = _insert_job(status="MARKING", attempts=3, max_attempts=3)

        pool._handle_failure(
            _worker(), _job_row(job_id),
            _worker_result(job_id, error="Gemini answered but the body was cut off",
                           error_kind="unexpected_response"))

        row = _job_row(job_id)
        assert row["status"] == "FAILED", "the attempt budget is a hard stop"
        assert row["attempts"] == 3, "history is preserved, never reset"
        stages = [r["stage"] for r in db.query(
            "SELECT stage FROM job_stage_events WHERE job_id = ? ORDER BY id", (job_id,))]
        assert any("Diagnosed" in (s or "") for s in stages), stages


def test_concurrent_enqueues_create_exactly_one_job(fresh_state):
    """Two callers asking at the same moment get one correction, not two."""
    import threading
    from app.jobs.manager import DuplicateJobError, JobManager

    manager = JobManager()
    results: list[str] = []
    errors: list[Exception] = []
    barrier = threading.Barrier(6)

    def attempt() -> None:
        barrier.wait(timeout=5)
        try:
            results.append(manager.enqueue(
                submission_id="s-race", coursework_id="w-race", course_id="c",
                student_id="u-race", student_name="Race")["id"])
        except DuplicateJobError as exc:
            errors.append(exc)

    threads = [threading.Thread(target=attempt) for _ in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    rows = db.query("SELECT id, status FROM correction_jobs WHERE submission_id = 's-race'")
    active = [r for r in rows if r["status"] in ("QUEUED", "STARTING", "PREPARING")]
    assert len(active) == 1, rows
    assert len(rows) == 1, "one row per submission, not just one active"
    assert len(set(results)) == 1, results


def test_an_aggregate_quota_storm_is_never_a_credential_verdict():
    """A 429 storm across keys must stay retryable.

    The recorded failures carried a joined "all configured models failed"
    message with a 429 while `error_kind` said invalid_key: the job was
    recorded as authentication_error (permanent) and failed on the spot,
    though every key was merely out of quota.
    """
    storm = ("All configured Gemini models failed. gemini-3.6-flash: 429 "
             "RESOURCE_EXHAUSTED. {'error': {'code': 429, 'message': 'You "
             "exceeded your current quota, please check your plan'}}")
    for kind in ("invalid_key", "forbidden"):
        f = rp.failure_for_kind(kind, storm)
        assert f.cls == rp.RATE_LIMIT, (kind, f.cls)
        assert f.retryable is True

    # A verdict on ONE credential still fails fast — the rule is unchanged.
    f = rp.failure_for_kind("invalid_key", "API key not valid. Please pass a valid API key.")
    assert f.cls == rp.AUTHENTICATION_ERROR
    assert f.retryable is False


# ================================================= prepared-input cache

def _pool(worker_count: int = 1):
    from app.workers.pool import WorkerPool
    return WorkerPool(worker_count=worker_count)


def _pdf_bytes(pages: int = 1) -> bytes:
    """A REAL, readable PDF — routing must be able to read the page count."""
    import io
    from pypdf import PdfWriter
    writer = PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(width=200, height=200)
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


def _manual_files(job_id: str, tmp_dir) -> tuple[Path, Path]:
    sub = Path(tmp_dir) / f"{job_id}_sub.pdf"
    ms = Path(tmp_dir) / f"{job_id}_ms.pdf"
    sub.write_bytes(_pdf_bytes())
    ms.write_bytes(_pdf_bytes())
    for kind, path in (("submission", sub), ("mark_scheme", ms)):
        db.execute(
            "INSERT INTO pending_manual_files(job_id, kind, path, filename) "
            "VALUES(?,?,?,?)", (job_id, kind, str(path), path.name))
    return sub, ms


class TestPreparedInputCache:
    def test_prepared_input_reused_across_retries(self, fresh_state, tmp_path):
        from app.workers.pool import cleanup_job_files
        job_id = f"job-prep-{uuid.uuid4().hex[:8]}"
        sub, ms = _manual_files(job_id, tmp_path)
        job = {"id": job_id, "submission_id": None, "coursework_id": None,
               "student_name": "Prep"}
        pool = _pool()
        first = pool._stage_inputs(job)
        assert len(first["submission"]) == 1
        assert Path(first["submission"][0]).is_file()

        # Sources vanish (a later retry must still prepare nothing again).
        sub.unlink()
        ms.unlink()
        db.execute("DELETE FROM pending_manual_files WHERE job_id = ?", (job_id,))
        second = pool._stage_inputs(job)
        assert second["submission"] == first["submission"]
        assert second["mark_scheme"] == first["mark_scheme"]
        assert Path(second["submission"][0]).is_file()
        assert _stats()["avoided"]["reprepare"] >= 1

        # Terminal cleanup deletes the cache — student PDFs not retained.
        cleanup_job_files(job_id)
        assert not (config.DATA_DIR / "tmp" / f"job-{job_id}").exists()

    def test_classroom_submission_downloaded_and_prepared_once(
            self, fresh_state, monkeypatch):
        import app.classroom.auth as classroom_auth
        import app.classroom.client as classroom_client
        monkeypatch.setattr(classroom_auth, "get_access_token", lambda: "fake-token")
        pdf = _blank_pdf()
        downloads = {"n": 0}

        def fake_download(token, file_id):
            downloads["n"] += 1
            return ("homework.pdf", pdf, "application/pdf")

        monkeypatch.setattr(classroom_client, "download_drive_file", fake_download)

        now_ts = time.time()
        db.execute("INSERT INTO courses(id, name, fetched_at) VALUES('c1','Maths',?)",
                   (now_ts,))
        db.execute(
            "INSERT INTO coursework(id, course_id, title, fetched_at, allowlisted) "
            "VALUES('w1','c1','HW',?,1)", (now_ts,))
        db.execute(
            "INSERT INTO submissions(id, coursework_id, course_id, user_id, "
            "state, attachment_ids, fetched_at) VALUES(?,?,?,?,?,?,?)",
            ("s1", "w1", "c1", "u1", "TURNED_IN", json.dumps(["f1"]), now_ts))
        scheme_dir = config.DATA_DIR / "mark_schemes"
        scheme_dir.mkdir(parents=True, exist_ok=True)
        (scheme_dir / "w1.pdf").write_bytes(pdf)

        job = {"id": "job-cdl", "submission_id": "s1", "coursework_id": "w1",
               "student_name": "Kim"}
        from app.workers.pool import WorkerPool, cleanup_job_files
        pool = WorkerPool(worker_count=1)
        first = pool._stage_inputs(job)
        assert downloads["n"] == 1
        assert len(first["submission"]) == 1 and Path(first["submission"][0]).is_file()

        # Retry / key-failover attempt: no second download, no re-normalize.
        second = pool._stage_inputs(job)
        assert downloads["n"] == 1, "retry must NOT re-download the submission"
        assert second["submission"] == first["submission"]
        assert second["mark_scheme"] == first["mark_scheme"]
        stats = _stats()
        assert stats["avoided"]["downloads"] == 1
        assert stats["avoided"]["reprepare"] == 1

        cleanup_job_files("job-cdl")
        assert not (config.DATA_DIR / "tmp" / "job-cdl").exists()

    def test_partial_cache_falls_back_to_fresh_preparation(
            self, fresh_state, tmp_path):
        job_id = f"job-part-{uuid.uuid4().hex[:8]}"
        _manual_files(job_id, tmp_path)
        job = {"id": job_id, "submission_id": None, "coursework_id": None,
               "student_name": "Part"}
        pool = _pool()
        first = pool._stage_inputs(job)
        # Simulate a torn cache: delete one prepared file.
        Path(first["submission"][0]).unlink()
        db.execute("DELETE FROM pending_manual_files WHERE job_id = ?", (job_id,))
        with pytest.raises(RuntimeError):
            pool._stage_inputs(job)   # never reuse a broken manifest silently


def _blank_pdf() -> bytes:
    from pypdf import PdfWriter
    buf = io.BytesIO()
    writer = PdfWriter()
    writer.add_blank_page(width=200, height=200)
    writer.write(buf)
    return buf.getvalue()


# ================================================ pool failure handling

def _insert_job(**over) -> str:
    job_id = over.get("id") or f"job-{uuid.uuid4().hex[:8]}"
    now_ts = time.time()
    db.execute(
        "INSERT INTO correction_jobs(id, submission_id, coursework_id, "
        "course_id, student_id, student_name, status, created_at, updated_at, "
        "attempts, max_attempts) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (job_id, over.get("submission_id"), over.get("coursework_id"), None,
         None, over.get("student_name", "Retry Test"),
         over.get("status", "STARTING"), now_ts, now_ts,
         over.get("attempts", 1), over.get("max_attempts", 3)))
    return job_id


def _job_row(job_id: str) -> dict:
    return db.row_to_dict(db.query_one(
        "SELECT * FROM correction_jobs WHERE id = ?", (job_id,)))


def _fake_timers(monkeypatch) -> list:
    """Record scheduled retries/parks without ever running them."""
    timers: list = []

    class _FakeTimer:
        def __init__(self, delay, fn, *args, **kwargs):
            self.delay = float(delay)
            self.fn = fn
            timers.append(self)

        def start(self):
            pass

    monkeypatch.setattr(threading, "Timer", _FakeTimer)
    return timers


def _worker():
    from app.config import DATA_DIR
    from app.gemini.tester import KeyManagerHolder
    from app.workers.adapter import CorrectionWorker
    return CorrectionWorker("WT", DATA_DIR / "tmp" / "worker-retry-test",
                            KeyManagerHolder.instance())


def _worker_result(job_id: str, **kw):
    from app.workers.adapter import WorkerResult
    payload = dict(job_id=job_id, worker_id="WT", status="FAILED", ok=False,
                   error="boom", error_kind="unknown")
    payload.update(kw)
    return WorkerResult(**payload)


class TestPoolFailurePolicy:
    def _pool(self):
        return _pool()

    def test_authentication_error_fails_fast_without_retry(
            self, fresh_state, monkeypatch):
        timers = _fake_timers(monkeypatch)
        job_id = _insert_job()
        self._pool()._handle_failure(
            _worker(), _job_row(job_id),
            _worker_result(job_id, error="401 Unauthorized: API key not valid",
                           error_kind="invalid_key"))
        row = _job_row(job_id)
        assert row["status"] == "FAILED"
        assert row["error_code"] == "authentication_error"
        assert row["finished_at"] is not None
        assert timers == []                       # nothing scheduled → no resend
        assert _stats()["retries"] == 0

    def test_timeout_retries_once_then_fails_at_budget(
            self, fresh_state, monkeypatch):
        timers = _fake_timers(monkeypatch)
        pool = self._pool()
        first = _insert_job(attempts=1)
        pool._handle_failure(
            _worker(), _job_row(first),
            _worker_result(first, error="request timed out", error_kind="timeout"))
        assert _job_row(first)["status"] == "RETRYING"
        assert len(timers) == 1 and 0 < timers[0].delay <= 30

        second = _insert_job(attempts=2)
        pool._handle_failure(
            _worker(), _job_row(second),
            _worker_result(second, error="request timed out", error_kind="timeout"))
        assert _job_row(second)["status"] == "FAILED"
        assert _job_row(second)["error_code"] == "timeout"
        assert _stats()["retries"] == 1          # exactly one retry overall

    def test_rate_limit_parks_the_job_instead_of_resending(
            self, fresh_state, monkeypatch):
        timers = _fake_timers(monkeypatch)
        job_id = _insert_job(attempts=1)
        self._pool()._handle_failure(
            _worker(), _job_row(job_id),
            _worker_result(job_id, error="429 quota exceeded",
                           error_kind="rate_limited"))
        row = _job_row(job_id)
        assert row["status"] == "WAITING_FOR_API"
        assert "no request sent" in (row["stage_detail"] or "")
        assert len(timers) == 1 and timers[0].delay >= 1
        assert _stats()["retries"] == 0          # parks are not retries

    def test_park_budget_ends_in_clean_failure(
            self, fresh_state, monkeypatch):
        timers = _fake_timers(monkeypatch)
        job_id = _insert_job(attempts=1)
        pool = self._pool()
        # The ledger is PERSISTED on the job row (a fresh pool must honour the
        # waiting already spent, including after a restart).
        db.execute("UPDATE correction_jobs SET waited_seconds = ? WHERE id = ?",
                   (15 * 60 - 5, job_id))
        pool._handle_failure(
            _worker(), _job_row(job_id),
            _worker_result(job_id, error="429 quota exceeded",
                           error_kind="rate_limited"))
        row = _job_row(job_id)
        assert row["status"] == "FAILED"
        assert row["error_code"] == "all_keys_unavailable"
        assert timers == []                      # bounded — never an infinite park

    def test_existing_result_completes_without_duplicate_correction(
            self, fresh_state, monkeypatch):
        timers = _fake_timers(monkeypatch)
        job_id = _insert_job(submission_id="s-dup", coursework_id="w-dup",
                             attempts=1)
        now_ts = time.time()
        db.execute(
            "INSERT INTO results(id, job_id, coursework_id, submission_id, "
            "student_name, earned, available, percentage, result_json, "
            "created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
            ("r-dup", job_id, "w-dup", "s-dup", "Retry Test", 9, 12, 75.0,
             '{"questions": []}', now_ts))
        self._pool()._handle_failure(
            _worker(), _job_row(job_id),
            _worker_result(job_id, error="503 high demand",
                           error_kind="temporary"))
        row = _job_row(job_id)
        assert row["status"] == "COMPLETED"
        assert row["result_id"] == "r-dup"
        assert timers == []                      # no duplicate correction
        assert _stats()["retries"] == 0

    def test_dispatch_pauses_during_provider_cooldown(self, fresh_state):
        from app.gemini.provider_health import provider_health
        for _ in range(3):
            provider_health().record_failure(rp.HIGH_DEMAND, "503 high demand")
        pool = self._pool()
        job_id = _insert_job(status="QUEUED", attempts=0)
        pool._dispatch_queued()
        assert _job_row(job_id)["status"] == "QUEUED"   # nothing launched
        assert pool._provider_blocked is True

        provider_health().reset()
        pool._dispatch_queued()
        row = _job_row(job_id)
        # Local, deterministic input problem — fails fast, zero API calls.
        assert row["status"] == "FAILED"
        assert row["error_code"] == "invalid_request"
        assert pool._provider_blocked is False

    def test_retry_dispatch_skips_job_with_existing_result(self, fresh_state):
        pool = self._pool()
        job_id = _insert_job(status="QUEUED", attempts=1,
                             submission_id="s-early", coursework_id="w-early")
        now_ts = time.time()
        db.execute(
            "INSERT INTO results(id, job_id, coursework_id, submission_id, "
            "student_name, earned, available, percentage, result_json, "
            "created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
            ("r-early", job_id, "w-early", "s-early", "Retry Test",
             7, 10, 70.0, '{"questions": []}', now_ts))
        pool._dispatch_queued()
        row = _job_row(job_id)
        assert row["status"] == "COMPLETED" and row["result_id"] == "r-early"

    def test_transient_staging_error_gets_one_bounded_retry(
            self, fresh_state, monkeypatch):
        import app.classroom.auth as classroom_auth
        import app.classroom.client as classroom_client
        monkeypatch.setattr(
            classroom_auth, "get_access_token",
            lambda: (_ for _ in ()).throw(ConnectionError(
                "connection reset by peer")))
        timers = _fake_timers(monkeypatch)
        job_id = _insert_job(status="QUEUED", attempts=0,
                             submission_id="s-net", coursework_id="w-net")
        pool = self._pool()
        pool._dispatch_queued()
        row = _job_row(job_id)
        assert row["status"] == "RETRYING"
        assert len(timers) == 1 and timers[0].delay <= 30
        assert _stats()["retries"] == 1


# ============================================ JobManager decision path

class TestJobManagerPolicy:
    def _wait_for(self, manager, job_id, cond, timeout=15):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if cond(manager.get(job_id)):
                return manager.get(job_id)
            time.sleep(0.05)
        return manager.get(job_id)

    def test_authentication_error_never_reaches_attempt_two(self, fresh_state):
        from app.jobs.manager import JobManager
        manager = JobManager(worker_count=1, max_attempts=3)
        calls = {"n": 0}

        def handler(job, cancel):
            calls["n"] += 1
            raise RuntimeError("401 Unauthorized: API key not valid")

        manager.set_handler("classroom_correction", handler)
        manager.start()
        job = manager.enqueue(submission_id="s-auth", coursework_id="w-auth",
                              course_id="c1", student_id="u1", student_name="A")
        final = self._wait_for(manager, job["id"],
                               lambda r: r["status"] == "FAILED")
        manager.shutdown()
        assert final["status"] == "FAILED"
        assert final["error_code"] == "authentication_error"
        assert calls["n"] == 1                    # NO retry loop

    def test_all_keys_cooling_parks_then_recovers(self, fresh_state):
        from app.jobs.manager import JobManager
        manager = JobManager(worker_count=1, max_attempts=3)
        calls = {"n": 0}

        def handler(job, cancel):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError(
                    "All Gemini keys are cooling down after failures.")
            return None

        manager.set_handler("classroom_correction", handler)
        manager.start()
        job = manager.enqueue(submission_id="s-park", coursework_id="w-park",
                              course_id="c1", student_id="u1", student_name="B")
        final = self._wait_for(manager, job["id"],
                               lambda r: r["status"] == "COMPLETED",
                               timeout=20)
        manager.shutdown()
        assert final["status"] == "COMPLETED"
        assert calls["n"] == 2                    # parked once, then ran
        # The parked wait did not burn the whole attempt budget.
        assert final["attempts"] <= 2


# ================================================ usage dashboard (§12/§15)

class TestUsageDashboard:
    @pytest.fixture
    def client(self, fresh_state, monkeypatch):
        from app.config import get_config, save_config
        cfg = get_config()
        cfg.mock_mode = True
        cfg.role = "developer"
        save_config(cfg)
        from app.config import get_secrets
        get_secrets().set_gemini_keys(["AIzaRetryStatsKey1234567890"])
        from fastapi.testclient import TestClient
        from app.main import app
        with TestClient(app) as test_client:
            yield test_client

    def test_usage_includes_local_api_stats(self, client):
        from app import api_stats
        api_stats.record_request(ok=True)
        api_stats.record_request(ok=False, error_class="RATE_LIMIT")
        api_stats.record_retry("RATE_LIMIT")
        api_stats.record_avoided("downloads", 2)
        api_stats.record_avoided("uploads", 1)

        data = client.get("/api/usage").json()
        stats = data["api"]
        assert stats["requests"] == 2 and stats["successes"] == 1
        assert stats["failures"] == 1 and stats["retries"] == 1
        assert stats["avoided"] == {"downloads": 2, "uploads": 1,
                                    "reprepare": 0}
        assert stats["by_class"]["RATE_LIMIT"] == 1
        assert stats["provider"]["available"] is True
        assert "keys" in stats

    def test_clear_log_never_destroys_api_stats(self, client):
        from app import api_stats
        api_stats.record_request(ok=False, error_class="TIMEOUT")
        assert client.post("/api/diagnostics/logs/clear",
                           json={"confirm": True}).status_code == 200
        # Stats live in the DB (app_meta), not in the truncated log file.
        stats = client.get("/api/usage").json()["api"]
        assert stats["failures"] == 1
        assert stats["by_class"]["TIMEOUT"] == 1


# ================================================ end-to-end (mock mode)

class TestEndToEndMock:
    def test_successful_job_completes_with_zero_retries(self, fresh_state):
        """Mock Gemini path: a healthy first attempt schedules no retries,
        records none, and the prepared-input cache is removed at the end."""
        cfg = config.get_config()
        cfg.mock_mode = True
        cfg.worker.concurrency = 2
        config.save_config(cfg)
        config.get_secrets().set_gemini_keys(["AIzaEndToEndRetryKey1234567890"])

        from app.workers.pool import WorkerPool
        pool = WorkerPool(worker_count=2)
        pool.start()
        try:
            job_id = _insert_job(status="QUEUED", attempts=0,
                                 student_name="ZeroRetry")
            staging = fresh_state / "staging"
            staging.mkdir(exist_ok=True)
            _manual_files(job_id, staging)
            pool._wake.set()
            deadline = time.time() + 90
            final = None
            while time.time() < deadline:
                final = _job_row(job_id)
                if final["status"] in ("COMPLETED", "FAILED", "CANCELLED"):
                    break
                time.sleep(0.3)
            assert final is not None and final["status"] == "COMPLETED", final
            assert final["result_id"]
            assert pool._retries_total == 0
            assert _stats()["retries"] == 0
            # Terminal cleanup removed the per-job prepared input (§10).
            assert not (config.DATA_DIR / "tmp" / f"job-{job_id}").exists()
        finally:
            pool.shutdown(drain=False)
