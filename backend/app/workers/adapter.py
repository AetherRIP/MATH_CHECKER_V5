"""Worker adapter around the FRIEND'S trusted correction code.

The correction algorithm, prompts, schema and marking methodology live in
``app.engine.legacy_marking`` — a verbatim copy of the friend's code with a
documented, semantics-preserving adaptation list. THIS module adds none of
that: it is pure orchestration. Each :class:`CorrectionWorker` executes the
trusted engine inside its OWN OS PROCESS (the ``app/workers/child.py`` script,
run by path — not with ``-m``, which a bundled embeddable Python cannot
resolve), with:

* an isolated per-worker temporary workspace (no shared temp filenames),
* an exclusive lease on one healthy Gemini key for the whole job
  (acquired from the central KeyManager, released afterwards),
* structured result reporting back to the orchestrator over stdin/stdout JSON.

Why processes (§9/§10): the friend's code was written as a standalone
Streamlit script with module-level globals (MODEL_HEALTH store, prompt
constants). Those are safe to share read-only, but process isolation makes
every worker independent of crashes, memory growth and global mutation —
exactly the safety property §10 asks for. Workers never touch Classroom,
the UI or app state; they receive job input and return job results. The
subprocess+stdin/stdout design (instead of multiprocessing.spawn) keeps the
``__main__`` re-import out of the picture entirely, which is what makes the
child launchable under pytest and frozen binaries alike.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from ..gemini.key_manager import KeyManager
from ..logging_setup import log_event

#: How long a child may be COMPLETELY silent before it is treated as wedged.
#:
#: The child emits a heartbeat every few seconds (``child.HEARTBEAT_SECONDS``)
#: precisely so that this number can be small enough to matter: a legitimate
#: provider call keeps signalling while it thinks, and is bounded separately by
#: the SDK request timeout. Five minutes of total silence therefore means the
#: process is not running our code any more — deadlocked, blocked in a native
#: call, or stopped by the OS — and it is holding a worker slot, a leased key
#: and a job row in PROCESSING the whole time.
WORKER_STALL_SECONDS = 300.0


# ---------------------------------------------------------------------------
# Message contracts (the ONLY things crossing the process boundary)
# ---------------------------------------------------------------------------

@dataclass
class WorkerJob:
    """Input for one correction, fully self-contained (no shared state)."""
    job_id: str
    student_name: str
    # Local file paths readable by the worker process (staged by the
    # orchestrator inside the worker's own temp workspace).
    submission_paths: list[str]
    mark_scheme_path: str | None
    question_paper_path: str | None
    api_key: str                  # leased exclusively to this worker/job
    request_timeout_ms: int
    primary_models: list[str]
    audit_enabled: bool = False
    # Safe fingerprint of the leased key: the child starts sticky on it and
    # only rotates away when that key actually fails mid-job.
    leased_fingerprint: str | None = None
    # Offline demo / mock mode. The worker child is a separate interpreter
    # that reads config.toml, which does NOT record the in-memory demo flag,
    # so the mode must travel WITH the job — otherwise a demo correction
    # tries to reach Gemini and dies on "no API key".
    mock: bool = False
    # High-score double-check: when True the child runs an independent second
    # correction after a first correction at/above the configured threshold.
    double_check_enabled: bool = True


@dataclass
class WorkerResult:
    """Structured result (§17) — workers never touch the DB or UI."""
    job_id: str
    worker_id: str
    status: str                    # COMPLETED | FAILED | CANCELLED
    ok: bool
    error: str | None = None
    error_kind: str | None = None  # rate_limited | timeout | invalid_key | ...
    result: dict | None = None     # normalised legacy result dict
    totals: dict | None = None
    marking_model: str | None = None
    processing_seconds: float = 0.0
    fingerprint: str | None = None
    groups: list | None = None
    audit_model: str | None = None
    audit_warning: str | None = None
    # Diagnostics from the child: mark-scheme blueprint reconciliation report
    # and the key that actually served the request (safe identifier only).
    blueprint_report: dict | None = None
    key_fingerprint: str | None = None
    key_recorded: bool = False
    # High-score double-check payload (independent second correction).
    double_check_triggered: bool = False
    second_result: dict | None = None
    second_marking_model: str | None = None
    double_check_error: str | None = None
    # What the correction actually cost: provider requests by stage/kind,
    # counted where the requests are sent (gemini.call_audit).
    ai_usage: dict | None = None


# ---------------------------------------------------------------------------
# Orchestrator-side worker handle
# ---------------------------------------------------------------------------

@dataclass
class WorkerState:
    worker_id: str
    status: str = "IDLE"           # IDLE | STARTING | PROCESSING | RETRYING | STOPPED
    current_job_id: str | None = None
    student_name: str | None = None
    fingerprint: str | None = None  # leased key (safe identifier only)
    jobs_done: int = 0
    jobs_failed: int = 0
    errors: int = 0
    last_error: str | None = None
    started_at: float | None = None

    def public(self) -> dict:
        return {
            "worker_id": self.worker_id, "status": self.status,
            "current_job_id": self.current_job_id,
            "student_name": self.student_name,
            "fingerprint": self.fingerprint,
            "jobs_done": self.jobs_done, "jobs_failed": self.jobs_failed,
            "errors": self.errors, "last_error": self.last_error,
            "elapsed": (round(time.time() - self.started_at, 1)
                        if self.started_at and self.status == "PROCESSING" else None),
        }


class CorrectionWorker:
    """One isolated execution context for the friend's correction engine.

    Isolation is a real OS process (a subprocess running the
    ``app/workers/child.py`` script, see :meth:`start_job`), giving the child
    its own memory and interpreter state with stdin/stdout JSON as the only
    IPC — no shared globals, no shared temp files, and no multiprocessing
    ``__main__`` re-import issues under pytest or frozen binaries."""

    def __init__(self, worker_id: str, workspace: Path, key_manager: KeyManager):
        self.worker_id = worker_id
        self.workspace = workspace          # temp/worker-<id>/ — private
        self.keys = key_manager
        self.state = WorkerState(worker_id=worker_id)
        self._process: subprocess.Popen | None = None
        self._reader: threading.Thread | None = None
        self._stdout_data = ""
        self._leased_fp: str | None = None
        self._last_error: Exception | None = None
        self._on_progress = None
        #: Wall-clock of the last line the child wrote (stage, heartbeat or
        #: result). Silence is the only evidence available to the parent that a
        #: child has stopped working without exiting.
        self._last_signal = 0.0

    def workspace_dir(self) -> Path:
        self.workspace.mkdir(parents=True, exist_ok=True)
        return self.workspace

    def lease_key(self) -> str:
        """Exclusive key lease for the duration of one job (§7/§8/§18):
        two running jobs never share a key while free keys exist."""
        fp, rec = self.keys.acquire_exclusive()
        self._leased_fp = fp
        self.state.fingerprint = fp
        return fp

    def release_key(self, *, success: bool, error: Exception | None = None,
                    child_recorded: bool = False) -> None:
        if self._leased_fp is None:
            self._last_error = None
            return
        if success:
            # The child's KeyAwareClient already recorded the per-request
            # success for whichever key served it — avoid double counting.
            if not child_recorded:
                self.keys.record_success(self._leased_fp)
        elif error is not None:
            self._last_error = error
            # ``child_recorded`` means the child ran a KeyAwareClient, which
            # recorded every real per-key outcome as it happened. This
            # ``error`` is the JOB's terminal error — for a model-loop failure
            # it is the engine's aggregate of every model and key
            # ("All configured Gemini models failed. … | gemini-3.6-flash: 401
            # …"). Recording it again here attributed ANOTHER key's failure to
            # whichever key happened to be leased, which is how a healthy key
            # was permanently marked INVALID by somebody else's 401. Successes
            # were already deduplicated this way; failures now are too.
            if not child_recorded:
                self.keys.record_failure(self._leased_fp, error)
        self.keys.release_exclusive(self._leased_fp)
        self._leased_fp = None
        self.state.fingerprint = None
        self._last_error = None

    def start_job(self, job: WorkerJob, on_progress=None) -> None:
        """Launch the child process running the trusted engine (§10).

        ``on_progress`` receives each stage event the child writes to stdout
        (``{"type": "stage", ...}``) as it happens, so the pool can record and
        publish real progress instead of waiting for the final result.
        """
        self.workspace_dir()
        payload = job.__dict__.copy()
        payload["worker_id"] = self.worker_id
        stdin_data = json.dumps(payload, ensure_ascii=False)
        backend_root = str(Path(__file__).resolve().parent.parent.parent)
        # The child resolves its data dir from MC5_DATA_DIR at import time.
        # Pass the LIVE config.DATA_DIR, never a copy of os.environ: tests
        # rebind config.DATA_DIR per test (so the child must see the test's
        # data dir to honour mock mode), and in production DATA_DIR is derived
        # from that same env var anyway.
        from .. import config as _config
        # The child is launched as a SCRIPT, not with ``-m app.workers.child``.
        # The packaged application runs on CPython's *embeddable* distribution,
        # which ships a ``python313._pth`` file: with that file present CPython
        # ignores PYTHONPATH entirely, so a ``-m`` child could not find the
        # ``app`` package at all and died with exit code 1 on every job —
        # invisible in a dev checkout (system Python has no ._pth) and fatal in
        # the release. Running the file by path works in both: ``sys.path[0]``
        # is the script's own directory, and child.py adds the backend root.
        child_script = Path(__file__).resolve().parent / "child.py"
        # The child's stderr is captured (not discarded): a crash must explain
        # itself in the job's error instead of only "died unexpectedly".
        self._stderr_tmp = tempfile.TemporaryFile(mode="w+", encoding="utf-8",
                                                  errors="replace")
        self._process = subprocess.Popen(
            [sys.executable, str(child_script)],
            cwd=backend_root,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=self._stderr_tmp,
            text=True, encoding="utf-8", errors="replace",
            env={**os.environ, "PYTHONIOENCODING": "utf-8",
                 "PYTHONPATH": backend_root + os.pathsep + os.environ.get("PYTHONPATH", ""),
                 "MC5_DATA_DIR": str(_config.DATA_DIR)},
        )
        assert self._process.stdin is not None
        self._process.stdin.write(stdin_data)
        self._process.stdin.close()
        # Drain stdout from a helper thread: the result payload can exceed
        # the OS pipe buffer, and a child blocked writing to a full pipe
        # would never exit if stdout were only read after it dies.
        self._stdout_data = ""
        self._last_signal = time.time()
        self._on_progress = on_progress
        self._reader = threading.Thread(
            target=self._drain_stdout, name=f"{self.worker_id}-stdout", daemon=True)
        self._reader.start()
        self.state.status = "PROCESSING"
        self.state.current_job_id = job.job_id
        self.state.student_name = job.student_name
        self.state.started_at = time.time()
        log_event("workers", "worker started job", worker_id=self.worker_id,
                  job_id=job.job_id, student=job.student_name)

    def _child_stderr(self) -> str:
        """The child's stderr so far (empty when it said nothing)."""
        handle = getattr(self, "_stderr_tmp", None)
        if handle is None:
            return ""
        try:
            handle.seek(0)
            return (handle.read() or "").strip()[-1500:]
        except (OSError, ValueError):
            return ""

    def _close_child_stderr(self) -> None:
        handle = getattr(self, "_stderr_tmp", None)
        self._stderr_tmp = None
        if handle is not None:
            try:
                handle.close()
            except OSError:
                pass

    def _drain_stdout(self) -> None:
        """Read the child's stdout line by line until EOF.

        Stage events are handed to the pool the moment they arrive; the last
        non-event line is the job result the pool polls for. Reading line by
        line (instead of one blocking ``read()``) is what turns the child's
        progress into something the UI can show while the AI call runs.
        """
        proc = self._process
        if proc is None or proc.stdout is None:
            return
        try:
            for raw in proc.stdout:
                line = raw.strip()
                if not line:
                    continue
                # ANY line is proof the child is alive; a heartbeat exists so
                # that a long provider call (which prints no stage event) is
                # not mistaken for a hung process.
                self._last_signal = time.time()
                event = None
                if line.startswith("{"):
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError:
                        event = None
                if isinstance(event, dict) and event.get("type") == "heartbeat":
                    continue
                if isinstance(event, dict) and event.get("type") == "stage":
                    callback = getattr(self, "_on_progress", None)
                    if callback is not None:
                        try:
                            callback(event)
                        except Exception:  # noqa: BLE001 — never break the drain
                            pass
                    continue
                self._stdout_data = line
        except (OSError, ValueError):
            self._stdout_data = ""

    def poll_result(self, timeout: float = 0.0) -> WorkerResult | None:
        """Non-blocking poll; returns the WorkerResult when the child finishes."""
        if self._process is None:
            return None
        if timeout:
            time.sleep(timeout)
        if self._process.poll() is None:
            return None  # still running
        exitcode = self._process.wait()
        if self._reader is not None:
            self._reader.join(timeout=2.0)  # drain flushes once the child exits
        stdout = self._stdout_data
        self._process = None
        self._reader = None
        if exitcode != 0 or not stdout.strip():
            detail = self._child_stderr()
            self._close_child_stderr()
            error = f"Worker process died unexpectedly (exit code {exitcode})"
            if detail:
                error = f"{error}: {detail}"
            log_event("workers", "worker child exited without a result",
                      level=logging.ERROR, worker_id=self.worker_id,
                      job_id=self.state.current_job_id, exit_code=exitcode,
                      stderr=detail or "(no output)")
            return WorkerResult(
                job_id=self.state.current_job_id or "?",
                worker_id=self.worker_id, status="FAILED", ok=False,
                error=error,
                error_kind="worker_crash",
                processing_seconds=round(
                    (time.time() - (self.state.started_at or time.time())), 2),
            )
        self._close_child_stderr()
        try:
            payload = json.loads(stdout.strip().splitlines()[-1])
        except json.JSONDecodeError:
            return WorkerResult(
                job_id=self.state.current_job_id or "?",
                worker_id=self.worker_id, status="FAILED", ok=False,
                error="Worker returned an unreadable result",
                error_kind="worker_crash",
                processing_seconds=round(
                    (time.time() - (self.state.started_at or time.time())), 2),
            )
        return WorkerResult(
            job_id=payload.get("job_id", self.state.current_job_id or "?"),
            worker_id=payload.get("worker_id", self.worker_id),
            status=payload.get("status", "FAILED"),
            ok=bool(payload.get("ok")), error=payload.get("error"),
            error_kind=payload.get("error_kind"), result=payload.get("result"),
            totals=payload.get("totals"), marking_model=payload.get("marking_model"),
            processing_seconds=payload.get("processing_seconds", 0.0),
            groups=payload.get("groups"), audit_model=payload.get("audit_model"),
            audit_warning=payload.get("audit_warning"),
            blueprint_report=payload.get("blueprint_report"),
            key_fingerprint=payload.get("key_fingerprint"),
            key_recorded=bool(payload.get("key_recorded")),
            double_check_triggered=bool(payload.get("double_check_triggered")),
            second_result=payload.get("second_result"),
            second_marking_model=payload.get("second_marking_model"),
            double_check_error=payload.get("double_check_error"),
            ai_usage=payload.get("ai_usage"),
        )

    def cancel(self) -> None:
        if self._process is not None and self._process.poll() is None:
            self._process.terminate()
        self._close_child_stderr()
        if self.state.status == "PROCESSING":
            self.state.errors += 1

    # -- liveness ----------------------------------------------------------

    def seconds_since_signal(self) -> float:
        """Seconds since the child last wrote anything (0 when not running)."""
        if self._process is None or self.state.status != "PROCESSING":
            return 0.0
        return max(0.0, time.time() - (self._last_signal or 0.0))

    def force_stop(self, *, reason: str) -> WorkerResult:
        """Kill a wedged child and return the failure as a normal result.

        A child that has stopped signalling still holds its worker slot, its
        leased key and its memory; only the SDK's own timeout bounds an
        in-flight request, and nothing at all bounds a deadlock or a hung
        native call. This terminates the process, waits for it, escalates to a
        kill if it will not go, and reaps it so no zombie is left behind.
        """
        proc = self._process
        job_id = self.state.current_job_id or "?"
        started = self.state.started_at or time.time()
        silence = self.seconds_since_signal()
        if proc is not None:
            try:
                proc.terminate()
                proc.wait(timeout=10)
            except Exception:  # noqa: BLE001 — escalate, never hang here
                try:
                    proc.kill()
                    proc.wait(timeout=10)
                except Exception:  # noqa: BLE001
                    pass
        self._process = None
        self._reader = None
        detail = self._child_stderr()
        self._close_child_stderr()
        log_event("workers", "worker stalled — child terminated",
                  level=logging.ERROR, worker_id=self.worker_id,
                  job_id=job_id, reason=reason,
                  silence_seconds=round(silence, 1),
                  stderr=detail[-400:] if detail else None)
        return WorkerResult(
            job_id=job_id, worker_id=self.worker_id, status="FAILED", ok=False,
            error=(f"Worker stopped responding ({reason}) — the process was "
                   f"terminated. No result was produced by this attempt."),
            error_kind="worker_stalled",
            processing_seconds=round(time.time() - started, 2),
        )

    def mark_idle(self) -> None:
        self.state.status = "IDLE"
        self.state.current_job_id = None
        self.state.student_name = None
        self.state.started_at = None

    def shutdown(self) -> None:
        self.cancel()
        self.state.status = "STOPPED"
