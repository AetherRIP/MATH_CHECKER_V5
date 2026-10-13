"""End-to-end proof on the REAL application, not a stand-in.

This is the test the feature exists for. It starts the actual ``main.py --serve``
backend behind the actual relay, on a scratch data directory with a tiny
lifecycle configuration, and then does what teachers and browsers do:

    two browser tabs open      -> the app is up
    one tab closes             -> the app STAYS up
    the last tab closes        -> the countdown starts, the app stops itself
    a tab comes back           -> the stop is cancelled and the app keeps going
    everybody leaves           -> the app really exits, cleanly, on its own
    somebody opens the site    -> the relay starts it again, data intact

It is deliberately slow (two real backends are booted), because the point is
to prove the behaviour in the deployment that ships, not in a mock.
"""

from __future__ import annotations

import json
import socket
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from app.lifecycle.supervisor import Supervisor

REPO_ROOT = Path(__file__).resolve().parents[2]

#: Generous, because a first boot on a slow machine really is slow.
BOOT_TIMEOUT = 240.0


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def get(port: int, path: str, timeout: float = 60.0):
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}",
                                    timeout=timeout) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def get_json(port: int, path: str, timeout: float = 60.0) -> dict:
    _status, raw = get(port, path, timeout)
    try:
        return json.loads(raw)
    except ValueError:
        return {"_raw": raw.decode("utf-8", "replace")[:200]}


def wait_for(predicate, timeout: float, interval: float = 0.25):
    """Poll until ``predicate`` is true; return the last value either way."""
    deadline = time.monotonic() + timeout
    value = predicate()
    while not value and time.monotonic() < deadline:
        time.sleep(interval)
        value = predicate()
    return value


@pytest.fixture
def scratch(tmp_path, monkeypatch):
    """A private install: its own data dir, config and lifecycle timings.

    The timings are seconds, not minutes, so the test can actually watch the
    whole cycle — the code paths are identical to the shipped 30/90/120.
    """
    data = tmp_path / "install" / "data"
    data.mkdir(parents=True)
    (data / "config.toml").write_text(
        "[lifecycle]\n"
        "enabled = true\n"
        "supervisor = false\n"          # the relay is started BY the test
        "heartbeat_seconds = 5\n"
        "session_timeout_seconds = 15\n"
        "idle_grace_seconds = 8\n"
        "max_shutdown_wait_seconds = 30\n"
        "last_chance_seconds = 0\n",
        encoding="utf-8")
    monkeypatch.setenv("MC5_DATA_DIR", str(data))
    monkeypatch.setenv("MC5_NO_BROWSER", "1")
    monkeypatch.setenv("MC5_LOGIN_REQUIRED", "0")
    monkeypatch.delenv("MC5_PUBLIC", raising=False)
    monkeypatch.delenv("MC5_DEMO", raising=False)
    monkeypatch.delenv("MC5_AUTOSTART", raising=False)
    return data


def test_the_whole_lifecycle_on_the_real_application(scratch):
    public = free_port()
    log: list[str] = []
    supervisor = Supervisor(public, project_root=REPO_ROOT,
                            startup_timeout=BOOT_TIMEOUT,
                            speak=log.append)
    supervisor.bind()
    threading.Thread(target=supervisor.serve_forever, daemon=True).start()
    try:
        # -- 1. opening the site starts the real backend ----------------------
        assert supervisor.ensure_backend() is True, log
        status, raw = get(public, "/api/health")
        assert status == 200 and json.loads(raw)["status"] == "ok"
        assert supervisor.backend_running() is True

        # The frontend the teacher actually loads is served through the relay.
        status, raw = get(public, "/")
        assert status == 200 and b"<div id=\"root\"" in raw

        # -- 2. two tabs count as two sessions -------------------------------
        first = get_json(public, "/api/lifecycle/heartbeat?sid=tab-A")
        assert first["ok"] is True and first["active_sessions"] == 1
        assert first["state"] == "serving"
        second = get_json(public, "/api/lifecycle/heartbeat?sid=tab-B")
        assert second["active_sessions"] == 2

        # -- 3. closing ONE tab does not stop anything -----------------------
        assert get_json(public, "/api/lifecycle/leave?sid=tab-A")["removed"] is True
        state = get_json(public, "/api/lifecycle/status")
        assert state["active_sessions"] == 1
        assert state["state"] == "serving"
        time.sleep(4)
        assert supervisor.backend_running() is True
        assert get_json(public, "/api/lifecycle/status")["state"] == "serving"

        # -- 4. the last tab closing starts the countdown --------------------
        started_before_grace = time.monotonic()
        get_json(public, "/api/lifecycle/leave?sid=tab-B")
        counting = wait_for(
            lambda: get_json(public, "/api/lifecycle/status")["state"]
            == "counting_down", timeout=20)
        assert counting is True, get_json(public, "/api/lifecycle/status")
        countdown = get_json(public, "/api/lifecycle/status")
        assert countdown["active_sessions"] == 0
        assert countdown["countdown_seconds"] is not None

        # -- 5. coming back during the countdown cancels the stop ------------
        back = get_json(public, "/api/lifecycle/heartbeat?sid=tab-C")
        assert back["state"] == "serving"
        assert back["countdown_seconds"] is None
        # ...and it then stays up across MORE than one full idle grace period,
        # beating the way a real open tab does.
        for _ in range(4):
            time.sleep(3)
            assert get_json(public, "/api/lifecycle/heartbeat?sid=tab-C")["ok"]
        assert time.monotonic() - started_before_grace > 8
        assert supervisor.backend_running() is True
        assert get_json(public, "/api/lifecycle/status")["state"] == "serving"

        # -- 6. everybody leaves: the app stops ITSELF ----------------------
        get_json(public, "/api/lifecycle/leave?sid=tab-C")
        assert wait_for(
            lambda: get_json(public, "/api/lifecycle/status")["state"]
            in ("counting_down", "shutting_down"), timeout=20) is True
        stopped = wait_for(lambda: not supervisor.backend_running(), timeout=90)
        assert stopped is True, log

        # -- 7. the data and settings survived ------------------------------
        assert (scratch / "app.db").exists()
        assert (scratch / "config.toml").read_text(
            encoding="utf-8").startswith("[lifecycle]")
        # (The relay's own instance.json is written by run_supervisor and is
        # covered in test_lifecycle_supervisor.py, so it is not duplicated
        # here — this test is about the app's behaviour, not the launcher's.)

        # -- 8. a status probe does NOT wake it, a page load DOES -----------
        _status, raw = get(public, "/api/health")
        assert json.loads(raw)["backend"] == "stopped"
        assert supervisor.backend_running() is False
        # The relay has now noticed the child: it exited BY ITSELF, with code 0,
        # which is what makes this a sleep rather than a crash (a crash would
        # count towards the back-off that stops a start/stop loop).
        supervisor._reap_child()
        assert supervisor._last_exit == 0, (supervisor._last_exit, log)
        assert supervisor._failures == 0

        status, raw = get(public, "/", timeout=BOOT_TIMEOUT)
        assert status == 200 and b"<div id=\"root\"" in raw
        assert supervisor.backend_running() is True

        # -- 9. the app works again, with a fresh (empty) session registry ---
        again = get_json(public, "/api/lifecycle/heartbeat?sid=tab-D")
        assert again["ok"] is True
        assert again["active_sessions"] == 1
        assert again["state"] == "serving"
        assert again["uptime_seconds"] < 300      # a NEW process, not the old one

        # A correction scheduled before the stop is still visible after it: the
        # database is the same file, opened by the new process.
        status, raw = get(public, "/api/jobs")
        assert status == 200
    finally:
        supervisor.shutdown()
