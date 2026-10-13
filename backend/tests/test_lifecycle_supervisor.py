"""The on-demand relay: it owns the app's address and starts the backend.

These tests run a REAL relay against a REAL (tiny, fake) backend process, so
what is proven is the actual mechanism a teacher depends on: a stopped app is
one page load away, a status probe does not wake it, two launchers cannot
create two servers, and a broken install backs off instead of looping.

The fake backend is a few lines of stdlib HTTP server written into a temporary
"project" as ``main.py``; the relay's contract with the real one is only
``python main.py --serve <port>`` plus ``/api/health`` answering
``{"status": "ok"}``, which is exactly what the real ``main.py --serve`` does.
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

from app.lifecycle.supervisor import (
    BACKOFF_SECONDS, FAILURE_LIMIT, Supervisor, choose_backend_port,
)

FAKE_BACKEND = '''
import json
import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = int(sys.argv[2])
ROOT = os.path.dirname(os.path.abspath(__file__))
with open(os.path.join(ROOT, "starts.log"), "a", encoding="utf-8") as fh:
    fh.write(f"{os.getpid()}\\n")


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802 — http.server's spelling
        if self.path.startswith("/api/health"):
            payload = {"status": "ok", "from": "fake-backend"}
        else:
            payload = {"path": self.path, "from": "fake-backend",
                       "argv": sys.argv[1:]}
        body = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
'''

#: A "project" whose main.py dies immediately — the failure-loop case.
BROKEN_BACKEND = "import sys\nsys.exit(3)\n"


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def project(tmp_path: Path, source: str = FAKE_BACKEND) -> Path:
    root = tmp_path / "project"
    root.mkdir(exist_ok=True)
    (root / "main.py").write_text(source, encoding="utf-8")
    return root


def http_get(port: int, path: str, timeout: float = 30.0):
    """GET through the relay. Returns (status, parsed body or raw text)."""
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}",
                                    timeout=timeout) as response:
            raw = response.read()
            status = response.status
    except urllib.error.HTTPError as exc:  # a real error status still answers
        raw = exc.read()
        status = exc.code
    try:
        return status, json.loads(raw)
    except ValueError:
        return status, raw.decode("utf-8", "replace")


@pytest.fixture
def relay(tmp_path):
    """A running relay with the public port it owns."""
    made: list[Supervisor] = []

    def build(root: Path, **kwargs) -> tuple[Supervisor, int]:
        public = kwargs.pop("public_port", None) or free_port()
        supervisor = Supervisor(public, project_root=root,
                                startup_timeout=kwargs.pop("startup_timeout", 60),
                                speak=kwargs.pop("speak", lambda _m: None),
                                **kwargs)
        made.append(supervisor)
        return supervisor, public

    yield build

    for supervisor in made:
        supervisor.shutdown()


def start(supervisor: Supervisor) -> threading.Thread:
    """Bind and serve in the background (no eager start: tests choose)."""
    supervisor.bind()
    thread = threading.Thread(target=supervisor.serve_forever, daemon=True)
    thread.start()
    # The socket is listening the moment bind() returned; give accept() a tick.
    for _ in range(100):
        if supervisor._listener is not None:
            break
        time.sleep(0.01)
    return thread


# ------------------------------------------------------------------- mechanics

def test_a_private_port_never_steals_another_copy_s_public_port():
    """The backend port must not be the NEXT port after the public one.

    ``public + 1`` is exactly where a second copy of the app is normally
    started (8642, then 8643). When the first copy's supervised backend took
    that port, the second copy's launcher probed it, found a listener that
    answers the Math Checker health check and ATTACHED to it - so the second
    copy silently ran against the first copy's data directory (and, for
    ``--demo``, without demo data or its mock engine). A documented public
    port must never be taken by a backend.
    """
    for public in (8642, 8643):
        chosen = choose_backend_port(public)
        assert chosen not in (public, public + 1)
        # It really is usable: a plain bind succeeds on the chosen port.
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.bind(("127.0.0.1", chosen))
    # An explicit preference wins when it is free and not the public port.
    public = free_port()
    assert choose_backend_port(public, public + 5) == public + 5
    assert choose_backend_port(public, public) != public


def test_two_relays_cannot_own_the_same_port(relay, tmp_path):
    """A second launch must never create a second server tree."""
    root = project(tmp_path)
    first, public = relay(root)
    start(first)
    second, _ = relay(root, public_port=public)
    with pytest.raises(OSError):
        second.bind()
    first.shutdown()


def test_a_status_probe_does_not_wake_the_backend(relay, tmp_path):
    """The launchers ask constantly: that must never cost a full boot."""
    root = project(tmp_path)
    supervisor, public = relay(root)
    start(supervisor)

    status, body = http_get(public, "/api/lifecycle/status")
    assert status == 200
    assert body["source"] == "supervisor"
    assert body["backend"] == "stopped"
    assert body["state"] == "sleeping"

    status, body = http_get(public, "/api/health")
    assert status == 200 and body["status"] == "ok"
    assert body["backend"] == "stopped"

    time.sleep(0.3)
    assert supervisor.backend_running() is False
    assert not (root / "starts.log").exists(), "the backend was started by a probe"


def test_the_first_real_request_starts_the_backend(relay, tmp_path):
    root = project(tmp_path)
    supervisor, public = relay(root)
    start(supervisor)
    assert not supervisor.backend_running()

    status, body = http_get(public, "/")
    assert status == 200
    assert body["from"] == "fake-backend"          # served BY the backend
    assert body["path"] == "/"
    assert supervisor.backend_running() is True
    # And the passive path now answers from the backend, not the relay.
    _status, health = http_get(public, "/api/health")
    assert health["from"] == "fake-backend"
    assert health.get("backend") != "stopped"


def test_a_request_arriving_during_startup_is_held_not_refused(relay, tmp_path):
    """The teacher reopening the site waits a moment; it does not fail."""
    root = project(tmp_path)
    supervisor, public = relay(root)
    start(supervisor)
    results: list[tuple[int, dict]] = []

    def client():
        results.append(http_get(public, "/api/dashboard", timeout=60))

    workers = [threading.Thread(target=client) for _ in range(4)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(timeout=60)
        assert not worker.is_alive()

    assert len(results) == 4
    for status, body in results:
        assert status == 200 and body["from"] == "fake-backend"


def test_many_requests_start_exactly_one_backend(relay, tmp_path):
    """No duplicate servers: a burst of connections is still one child."""
    root = project(tmp_path)
    supervisor, public = relay(root)
    start(supervisor)

    workers = [threading.Thread(target=lambda: http_get(public, "/api/x",
                                                        timeout=60))
               for _ in range(8)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(timeout=60)

    starts = (root / "starts.log").read_text(encoding="utf-8").split()
    assert len(starts) == 1, f"expected one backend, got {starts}"


def test_a_sleeping_backend_is_started_again_for_the_next_visitor(relay, tmp_path):
    """The whole point: the app can come back after it stopped itself."""
    root = project(tmp_path)
    supervisor, public = relay(root)
    start(supervisor)
    assert supervisor.ensure_backend() is True

    supervisor.terminate_backend()
    deadline = time.time() + 20
    while supervisor.backend_running() and time.time() < deadline:
        time.sleep(0.2)
    assert supervisor.backend_running() is False

    status, body = http_get(public, "/api/health")
    assert body["backend"] == "stopped"           # honest while it is down

    _status, body = http_get(public, "/api/heartbeat-check")
    assert body["from"] == "fake-backend"         # ...and started again
    starts = (root / "starts.log").read_text(encoding="utf-8").split()
    assert len(starts) == 2


def test_a_clean_exit_is_a_sleep_and_not_a_failure(relay, tmp_path):
    """Exit code 0 is the idle stop working, so it must not trigger backoff."""
    root = project(tmp_path)
    supervisor, _public = relay(root)

    class Exited:
        returncode = 0

        @staticmethod
        def poll():
            return 0

    supervisor._child = Exited()
    supervisor._reap_child()
    assert supervisor._failures == 0
    assert supervisor._child is None
    assert supervisor._last_exit == 0
    assert supervisor._backoff_until == 0.0


def test_a_crash_is_remembered(relay, tmp_path):
    root = project(tmp_path)
    supervisor, _public = relay(root)

    class Crashed:
        returncode = 1

        @staticmethod
        def poll():
            return 1

    supervisor._child = Crashed()
    supervisor._reap_child()
    assert supervisor._failures == 1
    assert supervisor._last_exit == 1


def test_a_broken_install_backs_off_instead_of_looping(relay, tmp_path):
    """Three fast failures in a row stop the relay hammering the machine."""
    root = project(tmp_path, BROKEN_BACKEND)
    supervisor, _public = relay(root, startup_timeout=20)

    for _ in range(FAILURE_LIMIT):
        assert supervisor.ensure_backend() is False

    assert supervisor._failures >= FAILURE_LIMIT
    assert supervisor._backoff_until > time.monotonic()
    # Inside the back-off window nothing new is started.
    before = supervisor._started_once
    assert supervisor.ensure_backend() is False
    assert supervisor._started_once == before
    assert BACKOFF_SECONDS >= 5


def test_a_backend_that_never_answers_does_not_hang_the_request(relay, tmp_path):
    """A boot that never completes answers 503 with a sentence, not a hang."""
    root = project(tmp_path, "import time\ntime.sleep(600)\n")
    supervisor, public = relay(root, startup_timeout=3)
    start(supervisor)

    started = time.monotonic()
    status, body = http_get(public, "/api/dashboard", timeout=40)
    assert status == 503
    assert body["code"] == "backend_start_failed"
    assert time.monotonic() - started < 30


def test_the_relay_records_itself_as_this_install_s_instance(relay, tmp_path):
    """instance.json must name the RELAY and the PUBLIC port.

    It is what Stop.bat and start.bat read: pointing it at the backend child
    would mean stopping half the app and leaving the other half listening.
    """
    import os

    from app.lifecycle import supervisor as module

    root = project(tmp_path)
    supervisor, public = relay(root)
    path = module._write_record(public, supervisor)
    assert path is not None and path.exists()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        assert data["port"] == public
        assert data["backend_port"] == supervisor.backend_port
        assert data["supervised"] is True
        assert data["pid"] == os.getpid()
    finally:
        path.unlink()


def test_the_relay_stays_stdlib_only():
    """The always-on process must not drag in the app's heavy layer.

    That is the whole reason it can sit resident all day: no FastAPI, no
    worker pool, no database, no engine. Importing any of them here would make
    "always listening" mean "always heavy".
    """
    import app.lifecycle.supervisor as module

    source = Path(module.__file__).read_text(encoding="utf-8")
    for forbidden in ("import fastapi", "from fastapi", "import uvicorn",
                      "from ..db", "from ..engine"):
        assert forbidden not in source
