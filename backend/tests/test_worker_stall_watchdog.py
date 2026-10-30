"""Worker liveness: a wedged child must not hold a worker forever.

Why this file exists
--------------------
Every provider request in a correction is bounded by the SDK's request timeout.
Nothing at all bounded a child that stops running our code: a deadlock, a hung
native call, a process the OS suspended. Such a child keeps its worker slot, its
exclusively leased API key and its PROCESSING job row indefinitely, and the only
way out was for a teacher to press Cancel.

The child now writes a heartbeat line every few seconds, and the pool terminates
a child that has written nothing at all for ``WORKER_STALL_SECONDS``. These
tests cover the three properties that make that safe:

* a heartbeat never becomes (or corrupts) the job result;
* a *live* child is never killed, however long its provider call runs;
* a wedged child is really terminated — the OS process is gone — and reports a
  normal, classified failure so the existing retry policy handles it.
"""

from __future__ import annotations

import io
import json
import subprocess
import sys
import time

import pytest

from app.gemini.key_manager import KeyManager
from app.workers import adapter
from app.workers import child as child_mod
from app.workers.adapter import CorrectionWorker


class _FakeProcess:
    """Minimal stand-in for ``subprocess.Popen`` inside the drain thread."""

    def __init__(self, lines: list[str]):
        self.stdout = iter(lines)

    def poll(self):
        return None


def _worker(tmp_path) -> CorrectionWorker:
    return CorrectionWorker("W-test", tmp_path / "ws", KeyManager())


# -- the heartbeat itself -------------------------------------------------------

def test_heartbeat_writes_only_a_timestamp(monkeypatch):
    """A heartbeat carries liveness, never data (no key, text or mark)."""
    monkeypatch.setattr(child_mod, "HEARTBEAT_SECONDS", 0.02)
    buffer = io.StringIO()
    monkeypatch.setattr(sys, "stdout", buffer)

    stop = child_mod._start_heartbeat()
    time.sleep(0.15)
    stop.set()

    lines = [line for line in buffer.getvalue().splitlines() if line.strip()]
    assert lines, "the heartbeat never fired"
    for line in lines:
        event = json.loads(line)
        assert event["type"] == "heartbeat"
        assert set(event) == {"type", "ts"}


def test_heartbeat_cannot_be_mistaken_for_the_result(tmp_path):
    """The drain thread records only the last NON-event line as the result."""
    worker = _worker(tmp_path)
    worker._process = _FakeProcess([
        json.dumps({"type": "heartbeat", "ts": 1.0}) + "\n",
        json.dumps({"type": "stage", "message": "Marking"}) + "\n",
        json.dumps({"type": "heartbeat", "ts": 2.0}) + "\n",
        json.dumps({"ok": True, "status": "COMPLETED"}) + "\n",
        json.dumps({"type": "heartbeat", "ts": 3.0}) + "\n",
    ])
    worker.state.status = "PROCESSING"

    worker._drain_stdout()

    assert json.loads(worker._stdout_data) == {"ok": True, "status": "COMPLETED"}


def test_every_line_counts_as_a_sign_of_life(tmp_path):
    """Silence is what the watchdog measures, so a heartbeat resets the clock."""
    worker = _worker(tmp_path)
    worker._process = _FakeProcess([
        json.dumps({"type": "heartbeat", "ts": 1.0}) + "\n",
    ])
    worker.state.status = "PROCESSING"
    worker._last_signal = time.time() - 10_000

    worker._drain_stdout()

    assert worker.seconds_since_signal() < 5


def test_an_idle_worker_is_never_reported_as_stalled(tmp_path):
    """No process means no silence to measure (0.0), so nothing is killed."""
    worker = _worker(tmp_path)
    assert worker.seconds_since_signal() == 0.0


# -- the watchdog ---------------------------------------------------------------

@pytest.fixture
def pool(fresh_state, monkeypatch):
    import app.config as config

    cfg = config.get_config()
    cfg.mock_mode = True
    cfg.worker.concurrency = 1
    from app.workers.pool import WorkerPool

    created = WorkerPool(worker_count=1)   # not started: no scheduling loop
    yield created
    created.shutdown(drain=False)


def test_a_live_child_is_never_killed(pool, tmp_path):
    """A long provider call is silence-free; the watchdog must do nothing."""
    worker = _worker(tmp_path)
    worker.state.status = "PROCESSING"
    worker._last_signal = time.time()

    assert pool._stall_result(worker) is None


def test_a_wedged_child_is_terminated_and_reported(pool, tmp_path):
    """The OS process is really gone — not merely considered timed out."""
    proc = subprocess.Popen([sys.executable, "-c",
                             "import time; time.sleep(120)"])
    try:
        worker = _worker(tmp_path)
        worker._process = proc
        worker.state.status = "PROCESSING"
        worker.state.current_job_id = "job-wedged"
        worker.state.started_at = time.time()
        # Well past the threshold: no line for far longer than allowed.
        worker._last_signal = time.time() - (adapter.WORKER_STALL_SECONDS + 60)

        result = pool._stall_result(worker)

        assert result is not None
        assert result.error_kind == "worker_stalled"
        assert "terminated" in result.error
        assert proc.poll() is not None, "the child process was not actually stopped"
    finally:
        if proc.poll() is None:      # pragma: no cover - only on failure
            proc.kill()
            proc.wait(timeout=10)


def test_a_stall_is_an_infrastructure_failure_not_a_provider_retry():
    """A killed child earns a bounded infra retry — not a re-sent API request."""
    from app import retry_policy

    failure = retry_policy.failure_for_kind("worker_stalled",
                                            "Worker stopped responding")
    assert failure.cls == retry_policy.WORKER_CRASH
    assert failure.retryable
    decision = retry_policy.decide(failure, attempts=2, max_attempts=3,
                                   cfg_max_retries=3)
    assert decision.action == retry_policy.ACTION_RETRY
    exhausted = retry_policy.decide(failure, attempts=3, max_attempts=3,
                                    cfg_max_retries=3)
    assert exhausted.action == retry_policy.ACTION_FAIL
