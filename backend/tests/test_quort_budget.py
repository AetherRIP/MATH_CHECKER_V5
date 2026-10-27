"""Fewer provider requests per correction — measured, not asserted by vibes.

The production log shows why this matters: 3,922 failed ``generate_content``
attempts against 456 successes over one day of corrections (89.6 % of all
requests were failures), because every worker process re-learned an outage
from scratch and every overload failure was immediately resent.

These tests pin the mechanisms that remove that waste:

1. the overload gate is SHARED between worker processes (a fresh child starts
   closed instead of paying the discovery cost again);
2. a model the gate has parked is not sent a request at all;
3. an overload failure is not retried on the SAME model two seconds later;
4. a per-correction request budget stops a storm with a readable reason;
5. the ledger counts every attempt by stage, including failures.
"""
from __future__ import annotations

import pytest

from app.retry_policy import HIGH_DEMAND

OVERLOAD = (
    "503 UNAVAILABLE. {'error': {'code': 503, 'message': 'This model is "
    "currently experiencing high demand. Spikes in demand are usually "
    "temporary.', 'status': 'UNAVAILABLE'}}"
)


class _CountingModels:
    def __init__(self, calls: list, fail: bool) -> None:
        self.calls = calls
        self.fail = fail

    def generate_content(self, **kwargs):
        self.calls.append(kwargs.get("model"))
        if self.fail:
            raise RuntimeError(OVERLOAD)

        class _Response:
            text = '{"questions": []}'
        return _Response()


class _Counter:
    """A plain client the legacy loop can call (no proxy in the way)."""

    def __init__(self, fail: bool = True) -> None:
        self.calls: list = []
        self.models = _CountingModels(self.calls, fail)


@pytest.fixture(autouse=True)
def _fresh_gate():
    from app.gemini.provider_health import provider_health
    provider_health().reset()
    yield
    provider_health().reset()


class TestSharedGateAcrossProcesses:
    def test_a_new_process_adopts_the_cooldown(self):
        """The child that starts next must not re-discover the outage.

        Six workers each paid "3 failures before the gate closes" because the
        gate lived only in memory. Here the first gate writes the cooldown and
        a brand-new instance (a fresh worker process, its own memory) reads it.
        """
        from app.gemini.provider_health import (
            ProviderHealth, provider_health,
        )
        health = provider_health()
        for _ in range(3):
            health.record_failure(HIGH_DEMAND, "503", "gemini-3.8-flash")
        assert health.available("gemini-3.8-flash")[0] is False

        # A new process: empty memory, same database. The singleton is the only
        # instance that reads shared state, so drive the check through it.
        health._buckets.clear()
        health._last_refresh = 0.0
        allowed, wait = health.available("gemini-3.8-flash")
        assert allowed is False, "a fresh worker must start with the gate closed"
        assert wait > 0
        assert ProviderHealth().available("gemini-3.8-flash")[0] is True, (
            "a hermetic instance must not read shared state")

    def test_reset_clears_the_shared_state(self):
        from app.gemini.provider_health import provider_health
        health = provider_health()
        for _ in range(3):
            health.record_failure(HIGH_DEMAND, "503", "gemini-3.8-flash")
        health.reset()
        assert health.available("gemini-3.8-flash")[0] is True
        health._last_refresh = 0.0
        assert health.available("gemini-3.8-flash")[0] is True


class TestNoWastedRequests:
    def test_a_parked_model_is_not_sent_a_request(self, monkeypatch):
        from app.engine import legacy_marking as legacy
        from app.gemini.provider_health import provider_health

        for _ in range(3):
            provider_health().record_failure(HIGH_DEMAND, "503",
                                             "gemini-3.8-flash")
        client = _Counter(fail=False)
        with pytest.raises(RuntimeError) as exc:
            legacy.grade_with_model(
                client=client, model_names=["gemini-3.8-flash"],
                student_file="s", mark_scheme_file="m")
        # The only configured model is parked, so nothing was sent and the
        # failure says WHY instead of hammering it.
        assert client.calls == []
        assert "cooling down" in str(exc.value)

    def test_overload_is_not_retried_on_the_same_model(self, monkeypatch):
        from app.engine import legacy_marking as legacy
        from app.gemini.provider_health import provider_health

        client = _Counter(fail=True)
        with pytest.raises(RuntimeError):
            legacy.grade_with_model(
                client=client, model_names=["gemini-3.8-flash"],
                student_file="s", mark_scheme_file="m")
        # One attempt, not the old "fail, sleep 2 s, send the whole paper
        # again" pair: the gate carries the cooldown instead.
        assert client.calls == ["gemini-3.8-flash"]
        # The failure was fed to the shared gate, so the NEXT paper starts
        # from a known-bad model instead of learning it again.
        streak = provider_health().snapshot()["models"]["gemini-3.8-flash"]
        assert streak["consecutive"] == 1

    def test_a_parked_first_model_still_lets_the_next_one_mark(self):
        from app.engine import legacy_marking as legacy
        from app.gemini.provider_health import provider_health

        for _ in range(3):
            provider_health().record_failure(HIGH_DEMAND, "503",
                                             "gemini-3.8-flash")
        client = _Counter(fail=False)
        result, model = legacy.grade_with_model(
            client=client, model_names=["gemini-3.8-flash", "gemini-3.6-flash"],
            student_file="s", mark_scheme_file="m")
        assert model == "gemini-3.6-flash"
        assert client.calls == ["gemini-3.6-flash"]


class TestRequestBudget:
    def test_budget_stops_a_storm_before_anything_is_sent(self, monkeypatch):
        from app.gemini import call_audit
        from app.gemini.client_proxy import (
            KeyAwareClient, ProviderBudgetExceeded,
        )
        from app.gemini.key_manager import KeyManager

        call_audit.begin("job-budget")
        monkeypatch.setattr(call_audit, "max_requests", lambda: 1)

        client = KeyAwareClient(KeyManager())
        client._configured_keys = lambda: ["AIzaFakeKeyForTests0000000000"]
        client._acquire_for_call = lambda: "fp-budget"
        client._key_value = lambda fp: "AIzaFakeKeyForTests0000000000"
        client._needs_reupload = lambda contents, fp: False
        client._sdk_clients["fp-budget"] = _CounterSDK()
        call_audit.note_send(call_audit.KIND_GENERATE, model="m")
        with pytest.raises(ProviderBudgetExceeded) as exc:
            client.generate_content(model="gemini-3.8-flash", contents=["x"])
        assert "budget of 1" in str(exc.value)
        assert "re-run the paper" in str(exc.value)

    def test_the_budget_is_not_a_marking_limit(self):
        """A real paper costs 1-4 requests; the cap only catches a storm."""
        from app.gemini.call_audit import DEFAULT_MAX_REQUESTS
        assert DEFAULT_MAX_REQUESTS >= 8

    def test_a_spent_budget_does_not_charge_another_job(self, monkeypatch):
        """The ledger is per PROCESS; the budget is per CORRECTION.

        A worker child arms the budget for its own job. The same interpreter
        then serves an unrelated caller (the next job, or the backend's
        in-process mark-scheme recheck) and that caller must not inherit what
        the first job already spent — it would fail on work the budget was
        never meant to govern.
        """
        from app.gemini import call_audit

        call_audit.begin("job-first")
        monkeypatch.setattr(call_audit, "max_requests", lambda: 1)
        call_audit.note_send(call_audit.KIND_GENERATE, model="m",
                             job_id="job-first")

        client = _stubbed_client(job_id="job-second")
        response = client.generate_content(model="gemini-3.8-flash",
                                           contents=["x"])
        assert response.text == "{}", "the next job's request must go out"

        snap = call_audit.snapshot()
        assert snap["requests"] == 1, "only job-first's request is in the ledger"
        assert snap["by_kind"] == {call_audit.KIND_GENERATE: 1}

    def test_the_jobs_own_client_is_still_charged(self, monkeypatch):
        """Scoping the budget must not switch the safeguard off.

        The worker child begins the ledger for its job and hands the same id
        to its client (``workers/child.py``); that pair still spends the
        budget, which is what stops a request storm inside a correction.
        """
        from app.gemini import call_audit
        from app.gemini.client_proxy import ProviderBudgetExceeded

        call_audit.begin("job-worker")
        monkeypatch.setattr(call_audit, "max_requests", lambda: 1)
        client = _stubbed_client(job_id="job-worker")
        call_audit.note_send(call_audit.KIND_GENERATE, model="m",
                             job_id="job-worker")
        with pytest.raises(ProviderBudgetExceeded):
            client.generate_content(model="gemini-3.8-flash", contents=["x"])

    def test_a_budget_nobody_armed_never_blocks_the_api_process(
            self, monkeypatch):
        """The backend's in-process callers begin no ledger.

        Mark-scheme recheck and the developer diagnostics send provider
        requests from the API process, where no correction is running. Counting
        them into a process-global total made the 13th one fail with
        "this correction has already used its budget" although no correction
        existed.
        """
        from app.gemini import call_audit

        call_audit.reset()
        monkeypatch.setattr(call_audit, "max_requests", lambda: 2)

        client = _stubbed_client(job_id=None)
        for _ in range(3):
            assert client.generate_content(model="gemini-3.8-flash",
                                           contents=["x"]).text == "{}"
        assert call_audit.snapshot()["requests"] == 0


def _stubbed_client(*, job_id: str | None):
    """A KeyAwareClient whose "provider" is a counter: no keys, no network."""
    from app.gemini.client_proxy import KeyAwareClient
    from app.gemini.key_manager import KeyManager

    client = KeyAwareClient(KeyManager(), job_id=job_id)
    client._configured_keys = lambda: ["AIzaFakeKeyForTests0000000000"]
    client._acquire_for_call = lambda: "fp-budget"
    client._key_value = lambda fp: "AIzaFakeKeyForTests0000000000"
    client._needs_reupload = lambda contents, fp: False
    client._sdk_clients["fp-budget"] = _CounterSDK()
    return client


class _CounterSDK:
    def __init__(self) -> None:
        self.models = self
        self.calls = 0

    def generate_content(self, **kwargs):
        self.calls += 1

        class _R:
            text = "{}"
        return _R()


class TestCallLedger:
    def test_every_attempt_is_counted_by_stage_including_failures(self):
        from app.gemini import call_audit

        call_audit.begin("job-ledger")
        with call_audit.stage(call_audit.STAGE_MARKING):
            call_audit.note_send(call_audit.KIND_GENERATE, model="m")
            call_audit.note_result(ok=False, error="503")
            call_audit.note_send(call_audit.KIND_GENERATE, model="m")
            call_audit.note_result(ok=True)
        with call_audit.stage(call_audit.STAGE_UPLOAD):
            call_audit.note_send(call_audit.KIND_UPLOAD)
        call_audit.note_skip("cooling")
        snap = call_audit.snapshot()
        assert snap["requests"] == 2
        assert snap["failed"] == 1 and snap["succeeded"] == 1
        # Stages describe MARKING work only; the upload is not a marking
        # request and must not make the breakdown disagree with the count.
        assert snap["by_stage"] == {"marking": 2}
        assert snap["total_requests"] == 3
        assert snap["skipped"] == 1
        assert snap["by_kind"][call_audit.KIND_GENERATE] == 2

    def test_the_ledger_is_persisted_on_the_job(self):
        from app import api_stats
        from app.gemini import call_audit

        call_audit.begin("job-persist")
        with call_audit.stage(call_audit.STAGE_MARKING):
            call_audit.note_send(call_audit.KIND_GENERATE, model="m")
        api_stats.record_ai_usage("job-persist", call_audit.snapshot())
        row = api_stats.job_bandwidth("job-persist")
        assert row["ai_attempts"] == 1
        assert api_stats.job_ai_usage("job-persist")["by_stage"] == {"marking": 1}

    def test_a_retried_job_accumulates_its_attempts(self):
        from app import api_stats
        for _ in range(3):
            api_stats.record_ai_usage("job-retry", {"requests": 2})
        assert api_stats.job_bandwidth("job-retry")["ai_attempts"] == 6


class TestReusedUploadsAreNotReValidated:
    def test_a_remote_verified_in_this_process_is_reused_without_a_lookup(
            self, monkeypatch):
        from app.gemini import client_proxy, file_registry
        from app.gemini.key_manager import KeyManager

        client = client_proxy.KeyAwareClient(KeyManager())
        client._verified_remotes["files/abc"] = 10_000_000_000.0  # far future
        monkeypatch.setattr(file_registry, "content_sha256", lambda path: "sha")
        monkeypatch.setattr(file_registry, "lookup",
                            lambda fp, sha, purpose: {"remote_name": "files/abc"})
        monkeypatch.setattr(file_registry, "touch",
                            lambda fp, sha, purpose: None)
        monkeypatch.setattr(file_registry, "record_job_file",
                            lambda *a, **k: None)

        class _Files:
            lookups = 0

            def get(self, name):
                _Files.lookups += 1
                raise AssertionError("a verified remote must not be looked up")

        class _SDK:
            files = _Files()

        remote = client._reuse_registered(_SDK(), "fp", "ignored",
                                          "submission", ("fp", "path"))
        assert remote is not None
        assert remote.name == "files/abc"
        assert _Files.lookups == 0
