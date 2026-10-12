"""The worker child must start on the EMBEDDED runtime the release ships.

The packaged application runs CPython's *embeddable* distribution. Its
``python313._pth`` file makes CPython ignore ``PYTHONPATH``, so the old launch
(``python -m app.workers.child``) could not find the ``app`` package there: the
child exited 1 on every job and — because its stderr went to ``DEVNULL`` — the
job only ever reported "Worker process died unexpectedly". A development
checkout cannot see this, because system Python has no ``._pth``.

These tests reproduce the packaged condition in the development environment:

* launching child.py with PYTHONPATH removed, from an unrelated directory, must
  still start and answer with a JSON result (what ``-m`` cannot do), and
* the adapter must launch it by PATH, hand it the live data directory and keep
  its stderr instead of discarding it.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[1]
CHILD = BACKEND / "app" / "workers" / "child.py"


def test_child_runs_as_a_script_without_pythonpath():
    """The ._pth condition: no PYTHONPATH, an unrelated working directory."""
    env = dict(os.environ)
    env.pop("PYTHONPATH", None)
    env["MC5_DATA_DIR"] = tempfile.mkdtemp(prefix="mc5_child_")
    env["PYTHONIOENCODING"] = "utf-8"
    result = subprocess.run(
        [sys.executable, str(CHILD)], input=json.dumps({"job_id": "launch-probe"}),
        capture_output=True, text=True, cwd=tempfile.gettempdir(), env=env,
        timeout=180)
    assert result.returncode == 0, f"child exited {result.returncode}: {result.stderr[-800:]}"
    payload = json.loads(result.stdout.strip().splitlines()[-1])
    # It started far enough to READ the job and answer about it. A missing
    # module (the embeddable-runtime failure) produced no JSON at all.
    assert payload["job_id"] == "launch-probe"
    assert payload["status"] == "FAILED"
    assert "No module named" not in (payload.get("error") or "")


def test_child_reports_the_missing_input_not_an_import_error():
    """An empty job must fail on its own terms, proving the imports worked."""
    env = dict(os.environ)
    env.pop("PYTHONPATH", None)
    env["MC5_DATA_DIR"] = tempfile.mkdtemp(prefix="mc5_child_")
    result = subprocess.run([sys.executable, str(CHILD)], input="{}",
                            capture_output=True, text=True,
                            cwd=tempfile.gettempdir(), env=env, timeout=180)
    payload = json.loads(result.stdout.strip().splitlines()[-1])
    assert "submission_paths" in (payload.get("error") or "")


def test_adapter_launches_the_child_by_path_and_keeps_its_stderr(monkeypatch):
    """The launch contract itself, so the fix cannot be undone by accident."""
    from app.gemini.key_manager import KeyManager
    from app.workers.adapter import CorrectionWorker, WorkerJob

    captured: dict = {}

    class _FakeStdin:
        def __init__(self):
            self.written: list[str] = []

        def write(self, data: str) -> None:
            self.written.append(data)

        def close(self) -> None:
            pass

    class _FakeProcess:
        def __init__(self):
            self.stdin = _FakeStdin()
            self.stdout = None

        def poll(self):
            return None

    def fake_popen(argv, **kwargs):
        captured["argv"] = argv
        captured["kwargs"] = kwargs
        process = _FakeProcess()
        captured["process"] = process
        return process

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    monkeypatch.setattr(CorrectionWorker, "workspace_dir",
                        lambda self: self.workspace)

    worker = CorrectionWorker("W1", Path(tempfile.mkdtemp(prefix="mc5_ws_")),
                              KeyManager())
    job = WorkerJob(job_id="contract-1", student_name="Student",
                    submission_paths=[] , mark_scheme_path=None,
                    question_paper_path=None, api_key="k",
                    request_timeout_ms=1000, primary_models=["m"])
    worker.start_job(job)
    # The job travelled to the child as JSON on stdin.
    assert json.loads(captured["process"].stdin.written[0])["job_id"] == "contract-1"

    argv = captured["argv"]
    assert argv[0] == sys.executable
    assert Path(argv[1]).name == "child.py", (
        "the child must be launched BY PATH: 'python -m app.workers.child' "
        "cannot import the app on the embeddable runtime the release ships")
    assert argv[1] != "app.workers.child"
    assert captured["kwargs"]["cwd"] == str(BACKEND)
    assert captured["kwargs"]["stderr"] is not subprocess.DEVNULL, (
        "a crashing child must be able to say why")
    env = captured["kwargs"]["env"]
    assert env["MC5_DATA_DIR"]
    assert env["PYTHONIOENCODING"] == "utf-8"
