"""Portable-launcher support: ``main.py --stop`` and the UI auto-open.

The packaging work added a real way to stop a running copy (``Stop.bat`` ->
``main.py --stop``) and made a double-clicked launcher open the user interface
once the server actually answers. These tests cover the parts a teacher depends
on, without starting the whole application:

* identity is proven through the health endpoint BEFORE anything is killed,
* a program that merely holds the port is never touched,
* the PID really is discovered from the listening socket,
* the browser is only opened after the health check succeeds, and never for a
  boot/autostart launch or when MC5_NO_BROWSER is set.
"""
from __future__ import annotations

import importlib.util
import json
import os
import shutil
import signal
import socket
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

MAIN_PY = Path(__file__).resolve().parents[2] / "main.py"


@pytest.fixture(scope="module")
def main_entry():
    """Load the project's ``main.py`` entry point as a module."""
    spec = importlib.util.spec_from_file_location("mc5_main_entry", MAIN_PY)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class _Handler(BaseHTTPRequestHandler):
    payload: dict = {"status": "ok"}

    def do_GET(self):  # noqa: N802 — http.server's API
        body = json.dumps(self.payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):  # noqa: D102 — keep the test output clean
        pass


class _Stub:
    """A tiny HTTP server used as the thing sitting on a port."""

    def __init__(self, payload: dict):
        handler = type("Stub", (_Handler,), {"payload": payload})
        self.server = HTTPServer(("127.0.0.1", 0), handler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       daemon=True)
        self.thread.start()

    def close(self) -> None:
        # Idempotent: a test may emulate the kill (which closes the instance)
        # and the fixture teardown closes it again.
        if getattr(self, "_closed", False):
            return
        self._closed = True
        self.server.shutdown()
        self.server.server_close()


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture
def idle_instance():
    """A server that answers the Math Checker health check."""
    stub = _Stub({"status": "ok"})
    yield stub
    stub.close()


@pytest.fixture
def foreign_instance():
    """A server that is NOT Math Checker (it answers, but with something else)."""
    stub = _Stub({"hello": "world"})
    yield stub
    stub.close()


# --------------------------------------------------------------- stop command

def test_stop_reports_nothing_to_stop_when_the_port_is_free(main_entry, capsys):
    port = _free_port()
    assert main_entry.stop_running_instance(port) == 0
    assert "Nothing to stop" in capsys.readouterr().out


def test_stop_reports_nothing_to_stop_for_a_port_left_in_time_wait(main_entry,
                                                                   idle_instance,
                                                                   capsys):
    """Regression: a just-closed listener must not look like a running app.

    Closing a listener leaves the connections that were made to it in
    TIME_WAIT, and a plain ``bind`` fails while one lingers. The old probe read
    that failure as "something holds the port but will not answer", so Stop.bat
    reported a FAILED stop after a successful one, and the next launch refused
    to start with a bogus PORT CONFLICT -- even though uvicorn binds with
    SO_REUSEADDR and would have started normally.

    Nothing is listening here, so the only correct answer is "free".
    """
    port = idle_instance.port
    # A real probe first: it both proves identity and leaves the connection that
    # puts the port in the state this regression is about.
    assert main_entry.probe_port(port) == "ours"

    idle_instance.close()          # the app is gone; nothing is listening
    assert main_entry.probe_port(port) == "free"
    assert main_entry.stop_running_instance(port) == 0
    assert "Nothing to stop" in capsys.readouterr().out


def test_stop_refuses_a_program_that_is_not_math_checker(main_entry, foreign_instance,
                                                        capsys):
    """The port is held, but not by us: refusing is the whole safety property."""
    assert main_entry.stop_running_instance(foreign_instance.port) == 5
    out = capsys.readouterr().out
    assert "REFUSING TO STOP" in out
    # and it is still running: nothing was killed
    assert main_entry.probe_port(foreign_instance.port) == "foreign"


def test_stop_names_the_instance_before_taking_it_down(main_entry, idle_instance,
                                                       capsys, monkeypatch):
    """A healthy instance is stopped by PID, and the app is told who it was.

    The kill itself is captured instead of performed, so the test proves the
    decision (which PID, only after the identity check) rather than the OS call.
    """
    calls: list[list[str]] = []
    real_run = subprocess.run

    class _Completed:
        returncode = 0

    def fake_run(argv, **kwargs):
        # Only the stop call is captured: port discovery uses subprocess too
        # (netstat), and it must still see the real output.
        if not (isinstance(argv, (list, tuple)) and argv
                and str(argv[0]).lower().endswith("taskkill")):
            return real_run(argv, **kwargs)
        calls.append(list(argv))
        # The real taskkill removes the instance; emulate that so the wait loop
        # finishes instead of sitting out its 20 second deadline.
        idle_instance.close()
        return _Completed()

    # ``main.py`` imports subprocess/os inside the function, so patch the real
    # modules it will find rather than an attribute on the entry module.
    if os.name != "nt" and shutil.which("lsof") is None:
        # On POSIX the listening socket is mapped to a PID with ``lsof``; with
        # no ``lsof`` the stop command correctly reports that it could not find
        # the process ID, which is a different property (see
        # test_port_pids_finds_the_process_listening_on_a_port).
        pytest.skip("lsof is required to map a port to a PID on this platform")

    monkeypatch.setattr(subprocess, "run", fake_run)
    if os.name != "nt":  # stop_running_instance picks a signal on POSIX
        def fake_kill(pid, sig):
            calls.append(["kill", pid, sig])
            # The real SIGTERM brings the instance down; emulate that, otherwise
            # the wait loop can never observe the port becoming free and would
            # sit out its whole 20 second deadline on a healthy instance.
            idle_instance.close()

        monkeypatch.setattr(os, "kill", fake_kill)

    assert main_entry.stop_running_instance(idle_instance.port) == 0
    out = capsys.readouterr().out
    assert f"port {idle_instance.port}" in out
    assert "stopped" in out
    if os.name == "nt":
        assert calls and calls[0][:2] == ["taskkill", "/PID"]
        assert calls[0][-2:] == ["/T", "/F"]     # the workers go with it
    else:
        # The PID really is the process holding the port, and the signal is a
        # graceful SIGTERM (not SIGKILL): the app's own shutdown must run.
        assert calls and calls[0][0] == "kill"
        assert calls[0][1] == os.getpid()
        assert calls[0][2] == signal.SIGTERM


def test_stop_port_uses_the_recorded_instance_port(main_entry, idle_instance,
                                                   tmp_path, monkeypatch):
    """``--stop`` must find an instance launched on a non-default port.

    A copy is often started on another port (the default was taken, or the
    teacher passed one). ``instance.json`` records the port the launch really
    used, so Stop.bat has to consult it - otherwise it reports "Nothing to
    stop" while the app keeps running.
    """
    import app.config as config

    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    # Hermetic: never read the DEVELOPER's config.toml, whose port may well be
    # the one their own running copy is serving right now — the answer here
    # must depend on this install's record, not on the machine's state.
    monkeypatch.setattr(config, "CONFIG_PATH", tmp_path / "config.toml")
    monkeypatch.setattr(config, "_config", None)
    (tmp_path / "instance.json").write_text(json.dumps({
        "pid": os.getpid(), "port": idle_instance.port,
        "started_at": 0, "argv": ["main.py", str(idle_instance.port)],
    }), encoding="utf-8")

    # The record is consulted first, so the live instance is found even when
    # something else (another copy, a developer's own app) holds the default
    # port at the same time.
    assert main_entry.recorded_instance_port() == idle_instance.port
    assert main_entry.resolve_stop_port() == idle_instance.port


def test_stop_port_falls_back_when_no_instance_is_running(main_entry, tmp_path,
                                                          monkeypatch):
    import app.config as config

    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    # Hermetic: never read the DEVELOPER's config.toml, whose port may well be
    # the one their own running copy is serving right now — the answer here
    # must depend on this install's record, not on the machine's state.
    monkeypatch.setattr(config, "CONFIG_PATH", tmp_path / "config.toml")
    monkeypatch.setattr(config, "_config", None)
    # No record at all: the documented default is the candidate.
    assert main_entry.recorded_instance_port() == 0
    assert main_entry.resolve_stop_port() == 8642


def test_stop_port_ignores_a_corrupt_record(main_entry, tmp_path, monkeypatch):
    import app.config as config

    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    # Hermetic: never read the DEVELOPER's config.toml, whose port may well be
    # the one their own running copy is serving right now — the answer here
    # must depend on this install's record, not on the machine's state.
    monkeypatch.setattr(config, "CONFIG_PATH", tmp_path / "config.toml")
    monkeypatch.setattr(config, "_config", None)
    (tmp_path / "instance.json").write_text("not json", encoding="utf-8")
    assert main_entry.recorded_instance_port() == 0
    assert main_entry.resolve_stop_port() == 8642


def test_port_pids_finds_the_process_listening_on_a_port(main_entry):
    if os.name != "nt" and shutil.which("lsof") is None:
        pytest.skip("lsof is required to map a port to a PID on this platform")
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        sock.listen(1)
        port = sock.getsockname()[1]
        assert os.getpid() in main_entry.port_pids(port)


# ------------------------------------------------------------ opening the UI

def test_open_ui_waits_for_health_then_opens_the_browser(main_entry, idle_instance,
                                                         monkeypatch):
    opened: list[str] = []
    monkeypatch.delenv("MC5_NO_BROWSER", raising=False)
    monkeypatch.delenv("MC5_AUTOSTART", raising=False)
    monkeypatch.setattr("webbrowser.open", lambda url: opened.append(url) or True)

    main_entry.open_ui_when_ready(idle_instance.port, timeout=10)

    assert opened == [f"http://127.0.0.1:{idle_instance.port}"]


def test_open_ui_stays_hidden_for_autostart_and_no_browser(main_entry, idle_instance,
                                                           monkeypatch):
    opened: list[str] = []
    monkeypatch.setattr("webbrowser.open", lambda url: opened.append(url) or True)

    monkeypatch.setenv("MC5_AUTOSTART", "1")
    monkeypatch.delenv("MC5_NO_BROWSER", raising=False)
    main_entry.open_ui_when_ready(idle_instance.port, timeout=1)

    monkeypatch.delenv("MC5_AUTOSTART")
    monkeypatch.setenv("MC5_NO_BROWSER", "1")
    main_entry.open_ui_when_ready(idle_instance.port, timeout=1)

    assert opened == []


def test_open_ui_gives_up_quietly_when_nothing_answers(main_entry, monkeypatch):
    opened: list[str] = []
    monkeypatch.delenv("MC5_NO_BROWSER", raising=False)
    monkeypatch.delenv("MC5_AUTOSTART", raising=False)
    monkeypatch.setattr("webbrowser.open", lambda url: opened.append(url) or True)

    # Nothing is listening: it must return (not raise, not open a dead page).
    main_entry.open_ui_when_ready(_free_port(), timeout=1.5)
    assert opened == []
