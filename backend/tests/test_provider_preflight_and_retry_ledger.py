"""Retry-ledger persistence, bounded manual retries and provider preflight.

The two defects these cover were both "the budget lived somewhere that forgot":

* capacity waiting was counted only in the scheduler's memory, so a restart (or
  a cancel/re-arm cycle) handed a stuck job a fresh 15-minute park budget and an
  unlimited number of parks;
* the Retry button reset ``attempts`` to 0, so it was an unlimited full-budget
  reset rather than one auditable grant.

The preflight tests pin the other half of the contract: a key being configured
is not evidence that inference works, a 503 is not an invalid credential, and a
health check never needs a student PDF.
"""

from __future__ import annotations

import json
import time

import pytest

from app import db
from app.jobs import manager as jm
from app.jobs.manager import (
    API_WAIT_BUDGET_SECONDS, MAX_MANUAL_RETRIES, JobManager,
)


# -- manual retry --------------------------------------------------------------

def _job(manager, submission="s-retry"):
    return manager.enqueue(
        submission_id=submission, coursework_id="w1", course_id="c1",
        student_id="u1", student_name="Student")


def _fail(job_id: str, attempts: int = 1, code: str = "high_demand") -> None:
    db.execute(
        "UPDATE correction_jobs SET status = 'FAILED', attempts = ?, "
        "error = 'provider failed', error_code = ?, finished_at = ? "
        "WHERE id = ?", (attempts, code, time.time(), job_id))


def test_a_manual_retry_grants_one_attempt_and_keeps_the_history(fresh_state):
    manager = JobManager()
    job = _job(manager)
    _fail(job["id"], attempts=3)

    again = manager.retry(job["id"])

    assert again is not None
    assert again["status"] == "QUEUED"
    # ONE grant: 3 attempts of history become a budget for a fourth request,
    # they are not erased back to zero.
    assert again["attempts"] == 2, "history preserved, exactly one attempt granted"
    assert again["manual_retries"] == 1
    assert again["error"] in (None, "")
    assert again["error_code"] in (None, "")

    # The decision is in the job's own history, not only in a log line.
    history = [row["stage"] for row in db.query(
        "SELECT stage FROM job_stage_events WHERE job_id = ? ORDER BY id",
        (job["id"],))]
    assert any("Manual retry 1/" in (stage or "") for stage in history), history


def test_manual_retries_are_capped_and_refused_audibly(fresh_state):
    manager = JobManager()
    job = _job(manager, submission="s-cap")
    attempts = 1

    for expected in range(1, MAX_MANUAL_RETRIES + 1):
        _fail(job["id"], attempts=attempts)
        granted = manager.retry(job["id"])
        assert granted is not None, expected
        assert granted["manual_retries"] == expected
        attempts = granted["attempts"]

    _fail(job["id"], attempts=attempts)
    refused = manager.retry(job["id"])

    assert refused is None
    assert "manual retries" in (manager.last_retry_refusal or "").lower()
    row = db.query_one("SELECT status, manual_retries FROM correction_jobs "
                       "WHERE id = ?", (job["id"],))
    assert row["status"] == "FAILED", "a refused retry must not touch the job"
    assert row["manual_retries"] == MAX_MANUAL_RETRIES
    history = [r["stage"] for r in db.query(
        "SELECT stage FROM job_stage_events WHERE job_id = ?", (job["id"],))]
    assert any("Manual retry refused" in (stage or "") for stage in history)


def test_a_manual_retry_clears_the_capacity_ledger_but_a_plain_rearm_does_not(
        fresh_state):
    manager = JobManager()
    job = _job(manager, submission="s-ledger")
    db.execute(
        "UPDATE correction_jobs SET status = 'CANCELLED', attempts = 2, "
        "waited_seconds = 600, park_count = 3, finished_at = ? WHERE id = ?",
        (time.time(), job["id"]))

    # An automatic re-arm of the same paper keeps what was really spent.
    rearmed = manager.enqueue(
        submission_id="s-ledger", coursework_id="w1", course_id="c1",
        student_id="u1", student_name="Student")
    assert rearmed["id"] == job["id"]
    assert rearmed["waited_seconds"] == 600
    assert rearmed["park_count"] == 3

    # Only the explicit, capped teacher action resets it.
    db.execute("UPDATE correction_jobs SET status = 'CANCELLED' WHERE id = ?",
               (job["id"],))
    granted = manager.retry(job["id"])
    assert granted is not None
    assert granted["waited_seconds"] == 0
    assert granted["park_count"] == 0


# -- persisted park ledger -----------------------------------------------------

class _NoopTimer:
    """Do not let a test's park timer requeue a job after the test ends."""

    def __init__(self, *args, **kwargs):
        self.args = args
        self.kwargs = kwargs

    def start(self) -> None:
        return None


def _pool():
    from app.workers.pool import WorkerPool
    return WorkerPool(worker_count=1)


def test_each_park_is_persisted_with_its_reason(fresh_state, monkeypatch):
    monkeypatch.setattr("threading.Timer", _NoopTimer)
    manager = JobManager()
    job = _job(manager, submission="s-park")
    pool = _pool()

    pool._park_job(job["id"], 42.0, reason="quota cooldown",
                   failure_class="RATE_LIMIT")

    row = db.query_one(
        "SELECT status, waited_seconds, park_count, last_failure_class, "
        "retry_due_at FROM correction_jobs WHERE id = ?", (job["id"],))
    assert row["status"] == "WAITING_FOR_API"
    assert row["waited_seconds"] == pytest.approx(42.0)
    assert row["park_count"] == 1
    assert row["last_failure_class"] == "RATE_LIMIT"
    assert row["retry_due_at"] > time.time()

    # A fresh pool (a restart) reads the same ledger instead of zero.
    assert _pool()._ledger(job["id"]) == (pytest.approx(42.0), 1)


def test_a_restart_cannot_reset_the_waiting_budget(fresh_state, monkeypatch):
    monkeypatch.setattr("threading.Timer", _NoopTimer)
    manager = JobManager()
    job = _job(manager, submission="s-budget")
    db.execute(
        "UPDATE correction_jobs SET waited_seconds = ? WHERE id = ?",
        (API_WAIT_BUDGET_SECONDS - 1, job["id"]))

    # This pool has never seen the job: only the persisted column can stop it.
    pool = _pool()
    pool._park_job(job["id"], 10.0, reason="cooldown", failure_class="HIGH_DEMAND")

    row = db.query_one(
        "SELECT status, error_code, waited_seconds, park_count "
        "FROM correction_jobs WHERE id = ?", (job["id"],))
    assert row["status"] == "FAILED"
    assert row["error_code"] == "all_keys_unavailable"
    assert row["park_count"] == 1, "the refused park is still counted"
    assert row["waited_seconds"] == pytest.approx(API_WAIT_BUDGET_SECONDS + 9)


def test_a_restart_cannot_reset_the_park_count(fresh_state, monkeypatch):
    monkeypatch.setattr("threading.Timer", _NoopTimer)
    manager = JobManager()
    job = _job(manager, submission="s-parks")
    pool = _pool()
    max_parks = pool.MAX_PARKS_BEFORE_SUSPEND
    db.execute("UPDATE correction_jobs SET park_count = ? WHERE id = ?",
               (max_parks, job["id"]))

    pool._park_job(job["id"], 5.0, reason="again", failure_class="HIGH_DEMAND")

    row = db.query_one(
        "SELECT status, error_code, park_count FROM correction_jobs WHERE id = ?",
        (job["id"],))
    assert row["status"] == "FAILED"
    assert row["error_code"] == "retry_suspended"
    assert row["park_count"] == max_parks + 1


def test_the_job_detail_shape_exposes_the_ledger(fresh_state):
    from app.api.routes_all import _job_public
    manager = JobManager()
    job = _job(manager, submission="s-public")
    db.execute(
        "UPDATE correction_jobs SET waited_seconds = 90, park_count = 2, "
        "manual_retries = 1, last_failure_class = 'HIGH_DEMAND', "
        "retry_due_at = ? WHERE id = ?", (time.time() + 60, job["id"]))
    public = _job_public(manager.get(job["id"]))
    assert public["waited_seconds"] == 90
    assert public["park_count"] == 2
    assert public["manual_retries"] == 1
    assert public["max_manual_retries"] == MAX_MANUAL_RETRIES
    assert public["last_failure_class"] == "HIGH_DEMAND"
    assert public["retry_due_at"] is not None


# -- preflight -----------------------------------------------------------------

@pytest.fixture()
def preflight(monkeypatch):
    from app.config import get_secrets
    from app.gemini import preflight as module
    get_secrets().set_gemini_keys(["test-key-for-preflight-000000000000"])
    monkeypatch.setattr(module, "CACHE_SECONDS", 300.0)
    return module


class _Candidate:
    def __init__(self, text="OK"):
        self.text = text


class _Response:
    def __init__(self, candidates=None, text="OK"):
        self.candidates = [_Candidate(text)] if candidates is None else candidates
        self.text = text


def _fake_client(monkeypatch, *, response=None, error=None):
    class FakeModels:
        def generate_content(self, **kwargs):
            if error is not None:
                raise error
            return response if response is not None else _Response()

    class FakeClient:
        def __init__(self, *args, **kwargs):
            self.models = FakeModels()

    import google.genai
    monkeypatch.setattr(google.genai, "Client", FakeClient)


def test_a_working_probe_is_ready_and_cached_without_a_second_request(
        preflight, monkeypatch):
    calls = {"n": 0}
    original = _fake_client

    class CountingModels:
        def generate_content(self, **kwargs):
            calls["n"] += 1
            return _Response()

    class FakeClient:
        def __init__(self, *args, **kwargs):
            self.models = CountingModels()

    import google.genai
    monkeypatch.setattr(google.genai, "Client", FakeClient)

    verdict = preflight.probe()
    assert verdict["state"] == preflight.READY
    assert calls["n"] == 1

    cached = preflight.snapshot()
    assert cached["state"] == preflight.READY
    assert cached["cached"] is True
    assert calls["n"] == 1, "a cached GET must not spend another request"

    forced = preflight.snapshot(recheck=True)
    assert forced["state"] == preflight.READY
    assert calls["n"] == 2


def test_an_invalid_credential_is_not_a_capacity_wait(preflight, monkeypatch):
    _fake_client(monkeypatch, error=RuntimeError(
        "401 Unauthorized: API key not valid. Please pass a valid API key."))
    verdict = preflight.probe()
    assert verdict["state"] == preflight.INVALID_CREDENTIALS
    assert verdict["state"] in preflight.BLOCKING_STATES


def test_a_503_is_a_provider_outage(preflight, monkeypatch):
    _fake_client(monkeypatch, error=RuntimeError(
        "503 UNAVAILABLE: This model is currently experiencing high demand."))
    verdict = preflight.probe()
    assert verdict["state"] == preflight.PROVIDER_UNAVAILABLE
    assert verdict["state"] in preflight.BLOCKING_STATES


def test_quota_scope_is_reported_honestly(preflight, monkeypatch):
    _fake_client(monkeypatch, error=RuntimeError(
        "429 RESOURCE_EXHAUSTED: quota exceeded for metric "
        "generate_content_free_tier_requests, limit: 20, model: gemini-3.6-flash"))
    verdict = preflight.probe()
    assert verdict["state"] in (preflight.RATE_LIMITED,
                                preflight.QUOTA_EXHAUSTED)
    assert verdict["state"] in preflight.BLOCKING_STATES


def test_a_timeout_and_a_network_failure_are_distinct(preflight, monkeypatch):
    _fake_client(monkeypatch, error=RuntimeError("Deadline exceeded: timeout"))
    assert preflight.probe()["state"] == preflight.TIMEOUT
    _fake_client(monkeypatch, error=RuntimeError("Connection reset by peer"))
    assert preflight.probe()["state"] == preflight.NETWORK_ERROR


def test_a_candidate_less_response_is_not_ready(preflight, monkeypatch):
    _fake_client(monkeypatch, response=_Response(candidates=[], text=""))
    verdict = preflight.probe()
    assert verdict["state"] == preflight.MALFORMED_RESPONSE
    assert verdict["state"] not in preflight.READY_STATES


def test_no_configured_key_is_reported_without_a_request(preflight, monkeypatch):
    from app.config import get_secrets
    get_secrets().set_gemini_keys([])
    monkeypatch.setattr(
        "google.genai.Client",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not connect")))
    verdict = preflight.probe()
    assert verdict["state"] == preflight.NOT_CONFIGURED


def test_the_public_shape_never_contains_key_material(preflight, monkeypatch):
    _fake_client(monkeypatch)
    preflight.probe()
    from app.config import get_secrets
    key = get_secrets().gemini_keys()[0]
    payload = json.dumps(preflight.describe(preflight.snapshot()))
    assert key not in payload
    assert "api_key" not in payload.lower()


def test_the_batch_gate_blocks_on_a_fresh_bad_verdict_and_resumes_on_ready(
        fresh_state, monkeypatch):
    from app.gemini import preflight as module
    monkeypatch.delenv("MC5_DISABLE_PREFLIGHT", raising=False)
    manager = JobManager()
    _job(manager, submission="s-gate-1")
    _job(manager, submission="s-gate-2")
    pool = _pool()

    state = {"state": module.PROVIDER_UNAVAILABLE,
             "detail": "Gemini is temporarily overloaded."}
    monkeypatch.setattr(module, "cached_state", lambda **kw: state)
    assert pool._preflight_ready() is False
    rows = db.query("SELECT stage_detail FROM correction_jobs WHERE status = 'QUEUED'")
    assert all("provider unavailable" in (r["stage_detail"] or "").lower()
               for r in rows), rows

    monkeypatch.setattr(module, "cached_state",
                        lambda **kw: {"state": module.READY, "detail": "ok"})
    assert pool._preflight_ready() is True


def test_a_single_paper_does_not_pay_for_a_batch_probe(fresh_state, monkeypatch):
    from app.gemini import preflight as module
    monkeypatch.delenv("MC5_DISABLE_PREFLIGHT", raising=False)
    manager = JobManager()
    _job(manager, submission="s-one")
    pool = _pool()

    monkeypatch.setattr(
        module, "cached_state",
        lambda **kw: (_ for _ in ()).throw(AssertionError("no batch, no probe")))
    assert pool._preflight_ready() is True
