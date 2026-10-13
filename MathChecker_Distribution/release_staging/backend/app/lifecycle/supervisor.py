"""On-demand startup: a tiny always-on relay that starts the backend when used.

The backend can stop itself when nobody is using the site (see
:mod:`app.lifecycle.monitor`), but a stopped backend cannot be started by a
browser: a page can only speak HTTP to something that is already listening.
That something is this module.

``Supervisor`` owns the PUBLIC port with a plain relay socket, keeps ONE
backend child behind it on a private port, and starts that child the moment a
connection needs it. Its own footprint is deliberately tiny — stdlib only, no
FastAPI, no database, no engine, no worker pool — because it is the process
that is still resident while the heavy one is asleep.

How a request is handled
------------------------

* ``GET /api/health`` and ``GET /api/lifecycle/status`` are answered by the
  relay ITSELF while the backend is asleep, so asking "is this app reachable?"
  does not wake a whole correction backend. The answer is honest: it reports
  ``"backend": "stopped"``. Everything else — the UI, every API call, every
  asset — starts the backend first and is then relayed byte for byte, which is
  why streaming reports, uploads and SSE all keep working unchanged.
* A connection that arrives while the backend is starting is simply held until
  it is healthy, so a teacher who opens the site after a shutdown sees a
  slightly slower page load rather than a connection error.

Duplicate prevention
--------------------

The relay binds the public port with a PLAIN bind (no ``SO_REUSEADDR``, which
on Windows would let a second process bind a port that is already in use). A
second supervisor therefore cannot take the port: it reports that the app is
already reachable and exits without spawning anything. Only one supervisor can
own the port, and it holds at most one child, so duplicate backends cannot be
created by repeated launches.

Crash and loop handling
-----------------------

A child that exits 0 went to sleep on purpose (idle shutdown) and is simply
started again on the next request. A child that exits non-zero crashed: it is
restarted on demand too, but a burst of fast failures is rate-limited by a
back-off, so a permanently broken install cannot turn into a start/stop loop
that hammers the machine.
"""

from __future__ import annotations

import errno
import json
import os
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

#: How long to wait for a freshly started backend to answer /api/health.
START_TIMEOUT = 120.0

#: A child that dies this soon after starting is a failure, not a sleep.
FAST_FAILURE_SECONDS = 20.0
#: Consecutive fast failures before the relay slows its retries down.
FAILURE_LIMIT = 3
#: How long to refuse new child starts after a failure burst.
BACKOFF_SECONDS = 30.0

#: Concurrent client connections the relay will hold open (browsers keep a
#: handful each; the cap only stops a scanner from spawning endless threads).
MAX_CLIENTS = 96

#: Requests that must NEVER start the backend: they are pure status probes,
#: used by the launchers, ``--stop`` and the health check.
PASSIVE_PATHS = ("/api/health", "/api/lifecycle/status")

#: How long a client may take to send its first request before it is dropped.
CLIENT_HEAD_TIMEOUT = 20.0

_HEAD_LIMIT = 64 * 1024


def _now() -> str:
    return time.strftime("%H:%M:%S")


def _say(message: str) -> None:
    """One console line. The supervisor's own log is the console window."""
    print(f"[{_now()}] {message}", flush=True)


# --------------------------------------------------------------- port helpers

def port_is_free(port: int, host: str = "127.0.0.1") -> bool:
    """True when a PLAIN bind succeeds (no SO_REUSEADDR, see module docstring)."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind((host, port))
    except OSError:
        return False
    else:
        return True
    finally:
        sock.close()


def health_ok(port: int, host: str = "127.0.0.1", timeout: float = 3.0) -> bool:
    """Does ``port`` answer the app's own health check right now?"""
    import urllib.request

    try:
        with urllib.request.urlopen(f"http://{host}:{port}/api/health",
                                    timeout=timeout) as response:
            if response.status != 200:
                return False
            body = response.read(512)
    except Exception:  # noqa: BLE001 — any failure means "not answering"
        return False
    try:
        return json.loads(body).get("status") == "ok"
    except ValueError:
        return False


def choose_backend_port(public_port: int, preferred: int = 0,
                        host: str = "127.0.0.1") -> int:
    """A private port for the backend, never a port a copy could use publicly.

    ``preferred`` (``[lifecycle] backend_port``) wins when it is set, valid and
    free. Otherwise the operating system hands out an unused loopback port.

    Deliberately NOT ``public_port + 1``: that adjacent port is exactly the
    next port a second copy of the app (or the packaged verifier's demo copy)
    uses as ITS public port. The first copy's backend then occupies it, the
    second launch finds a listener that answers the Math Checker health check
    and ATTACHES to it instead of starting - so the second copy silently runs
    against the first copy's data directory. Binding port 0 makes the OS pick a
    free port that is held for the backend's whole life, so no later launch can
    be handed the same number.
    """
    if 1 <= preferred <= 65535 and preferred != public_port:
        if port_is_free(preferred, host):
            return preferred
    for _ in range(32):
        probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            probe.bind((host, 0))
            candidate = probe.getsockname()[1]
        finally:
            probe.close()
        if candidate != public_port and port_is_free(candidate, host):
            return candidate
    raise RuntimeError(
        f"could not obtain a free private port for the backend of "
        f"{public_port}")


# ---------------------------------------------------------------- the relay

class Supervisor:
    """Owns the public port and the ONE backend child behind it."""

    def __init__(self, public_port: int, *, host: str = "127.0.0.1",
                 listen_host: str | None = None,
                 backend_port: int = 0,
                 project_root: Path | None = None,
                 python: str | None = None,
                 startup_timeout: float = START_TIMEOUT,
                 speak=_say) -> None:
        self.public_port = int(public_port)
        self.host = host                       # where the backend listens
        self.listen_host = listen_host or host  # where the relay listens
        self.project_root = Path(project_root or
                                 Path(__file__).resolve().parents[3])
        self.python = python or sys.executable
        self.startup_timeout = float(startup_timeout)
        self._speak = speak
        self.backend_port = choose_backend_port(self.public_port, backend_port,
                                                host)
        self._lock = threading.Lock()
        self._child: subprocess.Popen | None = None
        self._listener: socket.socket | None = None
        self._stop = threading.Event()
        self._clients = 0
        self._failures = 0
        self._backoff_until = 0.0
        self._started_once = False
        self._last_exit: int | None = None
        self._spawned_at = 0.0

    # -- reporting -------------------------------------------------------------

    def backend_running(self) -> bool:
        child = self._child
        return child is not None and child.poll() is None

    def snapshot(self) -> dict:
        return {
            "supervisor": True,
            "backend": "running" if self.backend_running() else "stopped",
            "public_port": self.public_port,
            "backend_port": self.backend_port,
            "clients": self._clients,
            "started_once": self._started_once,
            "failures": self._failures,
        }

    # -- starting / stopping the backend ---------------------------------------

    def ensure_backend(self) -> bool:
        """Make sure a healthy backend is behind the relay. True when ready.

        Safe to call from many threads at once and from many connections: the
        lock plus the health probe mean a burst of fifty requests starts ONE
        backend, and the ones that arrive while it is booting wait for it
        instead of starting rivals.
        """
        if self.backend_running() and health_ok(self.backend_port,
                                                self.host, 2.0):
            return True
        with self._lock:
            # Re-check under the lock: another thread may have just done it.
            if self.backend_running() and health_ok(self.backend_port,
                                                    self.host, 2.0):
                return True
            self._reap_child()
            now = time.monotonic()
            if now < self._backoff_until:
                wait = round(self._backoff_until - now, 1)
                self._speak(f"backend start suppressed for {wait}s after "
                            "repeated failures")
                return False
            if self.backend_running():
                # Alive but not answering. Younger than the start-up allowance
                # means it is still booting — wait for it. Older means it has
                # wedged, and it is REPLACED rather than duplicated (two
                # children on one private port would fight over it).
                if now - self._spawned_at < self.startup_timeout:
                    return self._wait_healthy()
                self._speak("the backend stopped answering — replacing it")
                self._note_failure()
                self.terminate_backend()
            self._spawn_child()
            return self._wait_healthy()

    def _spawn_child(self) -> None:
        env = dict(os.environ)
        # The child serves a PRIVATE port; MC5_PORT stays the PUBLIC one so
        # everything the UI and the tunnel report is the address a user types.
        env["MC5_SUPERVISED"] = "1"
        env["MC5_NO_BROWSER"] = "1"
        env["MC5_PORT"] = str(self.public_port)
        env["MC5_BACKEND_PORT"] = str(self.backend_port)
        command = [self.python, str(self.project_root / "main.py"),
                   "--serve", str(self.backend_port)]
        try:
            self._child = subprocess.Popen(
                command, cwd=str(self.project_root), env=env,
                stdout=None, stderr=None, stdin=subprocess.DEVNULL)
        except OSError as exc:
            self._child = None
            self._speak(f"could not start the backend: {exc}")
            self._note_failure()
            return
        self._started_once = True
        self._spawned_at = time.monotonic()
        self._speak(f"starting the backend on port {self.backend_port} "
                    f"(pid {self._child.pid})")

    def _wait_healthy(self) -> bool:
        deadline = time.monotonic() + self.startup_timeout
        while time.monotonic() < deadline:
            child = self._child
            if child is not None and child.poll() is not None:
                code = child.returncode
                self._speak(f"the backend exited during startup "
                            f"(code {code})")
                self._note_failure()
                return False
            if health_ok(self.backend_port, self.host, 2.0):
                self._failures = 0
                self._speak("backend ready")
                return True
            time.sleep(0.5)
        self._speak(f"the backend did not answer within "
                    f"{int(self.startup_timeout)}s")
        self._note_failure()
        return False

    def _reap_child(self) -> None:
        """Notice a child that exited, and remember how it went."""
        child = self._child
        if child is None:
            return
        code = child.poll()
        if code is None:
            return
        self._child = None
        self._last_exit = code
        if code == 0:
            # The monitor stopped the app because nobody was using it. That is
            # the feature working, not a failure: no back-off, no alarm.
            self._failures = 0
            self._speak("backend went to sleep after being idle "
                        "(it starts again on the next request)")
        else:
            self._speak(f"backend exited unexpectedly (code {code})")
            self._note_failure()

    def _note_failure(self) -> None:
        self._failures += 1
        if self._failures >= FAILURE_LIMIT:
            self._backoff_until = time.monotonic() + BACKOFF_SECONDS
            self._speak(f"{self._failures} startup failures in a row — "
                        f"waiting {int(BACKOFF_SECONDS)}s before trying again")

    def terminate_backend(self, grace: float = 20.0) -> None:
        """Take the child down with the relay (best effort, never raises).

        Deliberately does NOT take ``self._lock``: it is called from inside
        ``ensure_backend`` (which holds it) and from ``shutdown``. Reading the
        child reference is atomic, and the terminate/kill calls below are safe
        to run more than once.
        """
        child = self._child
        if child is None or child.poll() is not None:
            return
        self._speak(f"stopping the backend (pid {child.pid})")
        try:
            child.terminate()
        except Exception:  # noqa: BLE001
            pass
        deadline = time.monotonic() + grace
        while time.monotonic() < deadline and child.poll() is None:
            time.sleep(0.2)
        if child.poll() is None:
            try:
                child.kill()
            except Exception:  # noqa: BLE001
                pass

    # -- serving ---------------------------------------------------------------

    def bind(self) -> None:
        """Take the public port. Raises OSError when it is already owned."""
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.bind((self.listen_host, self.public_port))
        listener.listen(128)
        self._listener = listener

    def serve_forever(self) -> None:
        """Accept connections until ``shutdown()`` is called."""
        listener = self._listener
        if listener is None:  # pragma: no cover - programming error
            raise RuntimeError("bind() must be called before serve_forever()")
        self._speak(f"relay listening on http://{self.listen_host}:"
                    f"{self.public_port} (backend port {self.backend_port})")
        while not self._stop.is_set():
            try:
                client, _addr = listener.accept()
            except OSError as exc:
                if self._stop.is_set() or exc.errno in (errno.EBADF, errno.EINVAL):
                    break
                continue
            if self._clients >= MAX_CLIENTS:
                self._close(client)
                continue
            self._clients += 1
            threading.Thread(target=self._handle, args=(client,), daemon=True,
                             name="mc5-relay").start()

    def shutdown(self) -> None:
        self._stop.set()
        listener, self._listener = self._listener, None
        if listener is not None:
            self._close(listener)
        self.terminate_backend()

    @staticmethod
    def _close(sock: socket.socket) -> None:
        try:
            sock.close()
        except OSError:
            pass

    def _handle(self, client: socket.socket) -> None:
        try:
            head, leftover = self._read_head(client)
            if head is None:
                return
            method, path, _version = _split_request_line(head)
            if self._answer_locally(client, method, path):
                return
            if not self.ensure_backend():
                _send_json(client, 503, {
                    "code": "backend_start_failed",
                    "message": "The Math Checker backend could not be started, "
                               "so this request was not served. Check the "
                               "window running the app for the reason.",
                })
                return
            upstream = socket.create_connection((self.host, self.backend_port),
                                                timeout=15.0)
            try:
                if head:
                    upstream.sendall(head)
                if leftover:
                    upstream.sendall(leftover)
                # Both directions run at once: HTTP is request/response, so a
                # single sequential pump would forward the request and then
                # wait for the client to hang up before the answer could move
                # (a page that simply never loads).
                reply = threading.Thread(target=_pump,
                                         args=(upstream, client), daemon=True,
                                         name="mc5-relay-reply")
                reply.start()
                _pump(client, upstream)
                reply.join(timeout=2.0)
            finally:
                self._close(upstream)
        except Exception as exc:  # noqa: BLE001 — one bad client is not fatal
            self._speak(f"connection dropped: {type(exc).__name__}: {exc}")
        finally:
            self._close(client)
            self._clients -= 1

    def _read_head(self, client: socket.socket) -> tuple[bytes | None, bytes]:
        """Read the first request head (and anything already buffered after it)."""
        client.settimeout(CLIENT_HEAD_TIMEOUT)
        buffer = b""
        while b"\r\n\r\n" not in buffer and b"\n\n" not in buffer:
            try:
                chunk = client.recv(4096)
            except (OSError, socket.timeout):
                return None, b""
            if not chunk:
                return None, b""            # client hung up (a bare port probe)
            buffer += chunk
            if len(buffer) > _HEAD_LIMIT:
                return None, b""
        marker = b"\r\n\r\n" if b"\r\n\r\n" in buffer else b"\n\n"
        head, _, rest = buffer.partition(marker)
        return head + marker, rest

    def _answer_locally(self, client: socket.socket, method: str,
                        path: str) -> bool:
        """Answer status probes ourselves instead of waking the backend.

        Only when the backend is asleep, and only for the two paths the
        launchers use: a status probe must never cost a full backend boot, and
        must never be mistaken for a user. The payload states plainly that this
        is the relay answering and that the backend is stopped — no fake
        "running" state, no invented session counts.
        """
        if method not in ("GET", "HEAD"):
            return False
        if path not in PASSIVE_PATHS:
            return False
        if self.backend_running():
            return False                      # awake: the real answer is better
        self._reap_child()
        if self.backend_running() or health_ok(self.backend_port, self.host,
                                               2.0):
            # A backend that is up is never described as stopped, however it
            # was started.
            return False
        if path == "/api/health":
            payload = {"status": "ok", "backend": "stopped",
                       "supervisor": True,
                       "detail": "Math Checker is installed here and starts on "
                                 "the next request."}
        else:
            payload = _sleeping_status(self.public_port, self.backend_port)
        _send_json(client, 200, payload, head_only=method == "HEAD")
        return True


def _sleeping_status(public_port: int, backend_port: int) -> dict:
    """The relay's own, real state while the backend is asleep."""
    enabled = True
    try:
        from ..config import get_config
        cfg = get_config().lifecycle
        enabled = bool(cfg.enabled)
        heartbeat = int(cfg.heartbeat_seconds)
        timeout = int(cfg.session_timeout_seconds)
        grace = int(cfg.idle_grace_seconds)
    except Exception:  # noqa: BLE001 — status must never raise
        heartbeat, timeout, grace = 30, 90, 120
    return {
        "source": "supervisor",
        "enabled": enabled,
        "supervisor": True,
        "backend": "stopped",
        "state": "sleeping",
        "active_sessions": 0,
        "session_timeout_seconds": timeout,
        "heartbeat_seconds": heartbeat,
        "idle_grace_seconds": grace,
        "countdown_seconds": None,
        "last_heartbeat_age_seconds": None,
        "blockers": [],
        "public_port": public_port,
        "backend_port": backend_port,
    }


# ------------------------------------------------------------ small helpers

def _split_request_line(head: bytes) -> tuple[str, str, str]:
    """``(method, path, version)`` from a request head (empty when unknown)."""
    try:
        line = head.split(b"\r\n", 1)[0].split(b"\n", 1)[0].decode("latin-1")
    except Exception:  # noqa: BLE001
        return "", "", ""
    parts = line.split()
    if len(parts) < 2:
        return "", "", ""
    method = parts[0].upper()
    target = parts[1]
    path = target.split("?", 1)[0]
    return method, path, parts[2] if len(parts) > 2 else "HTTP/1.0"


def _send_json(client: socket.socket, status: int, payload: dict,
               head_only: bool = False) -> None:
    body = json.dumps(payload).encode("utf-8")
    head = (
        f"HTTP/1.1 {status} {'OK' if status == 200 else 'Service Unavailable'}\r\n"
        "Content-Type: application/json; charset=utf-8\r\n"
        f"Content-Length: {len(body)}\r\n"
        "Cache-Control: no-store\r\n"
        "Connection: close\r\n\r\n"
    ).encode("latin-1")
    try:
        client.sendall(head)
        if not head_only:
            client.sendall(body)
    except OSError:
        pass


def _pump(source: socket.socket, target: socket.socket) -> None:
    """Copy bytes one way until the source is done, then half-close the target."""
    try:
        while True:
            data = source.recv(65536)
            if not data:
                break
            target.sendall(data)
    except OSError:
        pass
    finally:
        try:
            target.shutdown(socket.SHUT_WR)
        except OSError:
            pass


# ----------------------------------------------------------------- entry point

def run_supervisor(public_port: int, *, listen_host: str = "127.0.0.1",
                   backend_port: int = 0, project_root: Path | None = None,
                   open_browser: bool = False) -> int:
    """Foreground supervisor for ``main.py``. Returns a process exit code.

    Exit codes match ``main.py``'s existing contract: 0 when the app is (or was
    already) reachable, 4 when another program owns the port.
    """
    supervisor = Supervisor(public_port, listen_host=listen_host,
                            backend_port=backend_port,
                            project_root=project_root)
    try:
        supervisor.bind()
    except OSError as exc:
        if health_ok(public_port, listen_host):
            _say(f"Math Checker is already running at "
                 f"http://{listen_host}:{public_port}")
            _say("Attaching to the running application — not starting a "
                 "second copy.")
            return 0
        print("=" * 56, flush=True)
        print(f"  PORT CONFLICT: port {public_port} is already in use and does "
              "not answer as Math Checker.", flush=True)
        print(f"  Nothing was started. ({exc})", flush=True)
        print("=" * 56, flush=True)
        return 4

    record = _write_record(public_port, supervisor)
    if open_browser:
        threading.Thread(target=_open_ui_when_ready,
                         args=(public_port, supervisor), daemon=True,
                         name="mc5-open-ui").start()
    # A normal launch starts the backend straight away: the teacher who just
    # double-clicked the launcher should get a warm app, and only the LATER
    # wakes (after an idle shutdown) pay the start-up cost.
    supervisor.ensure_backend()
    try:
        supervisor.serve_forever()
    except KeyboardInterrupt:
        _say("interrupted — stopping")
    finally:
        supervisor.shutdown()
        if record is not None:
            try:
                record.unlink()
            except OSError:
                pass
    return 0


def _write_record(public_port: int, supervisor: Supervisor) -> Path | None:
    """Record the SUPERVISOR as this install's instance.

    ``--stop`` and the launchers identify the running app through this record
    and the port it names, so it must point at the public port and at the
    process that owns the port tree. ``main.py --serve`` deliberately does not
    overwrite it, which is what makes ``Stop.bat`` stop the supervisor and its
    backend child together instead of leaving a half-alive app behind.
    """
    try:
        from ..config import DATA_DIR
        base = Path(DATA_DIR)
    except Exception:  # noqa: BLE001 — diagnostics must never block startup
        base = Path(__file__).resolve().parents[3] / "data"
    path = base / "instance.json"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({
            "pid": os.getpid(),
            "port": public_port,
            "backend_port": supervisor.backend_port,
            "supervised": True,
            "started_at": int(time.time()),
            "argv": sys.argv,
        }, indent=2), encoding="utf-8")
    except OSError:
        return None
    return path


def _open_ui_when_ready(port: int, supervisor: Supervisor) -> None:
    """Open the browser once the BACKEND answers, not merely the relay."""
    import urllib.request
    import webbrowser

    deadline = time.monotonic() + supervisor.startup_timeout
    while time.monotonic() < deadline:
        # The BACKEND's own health, not the relay's local answer: opening the
        # browser the instant the relay is up would show the teacher a page
        # that is still waiting for the app to boot.
        if health_ok(supervisor.backend_port, supervisor.host):
            try:
                webbrowser.open(f"http://{supervisor.listen_host}:{port}")
            except Exception:  # noqa: BLE001 — a browser is a convenience
                pass
            return
        time.sleep(0.5)
