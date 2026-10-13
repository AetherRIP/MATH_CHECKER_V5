"""PUBLIC ADDRESS — the app-managed public endpoint, with an honest ON/OFF.

The mechanism is the one this project already ships (and the only one it has):
a Cloudflare quick tunnel —

    cloudflared tunnel --no-autoupdate --url http://127.0.0.1:<this port>

pointed at THIS running server. ``remote.py`` / ``start_public.bat`` own the
"start the whole thing from scratch" path; this module lets the *running*
application start and stop that same tunnel, because the switch lives in
Settings and Settings talks to the application.

Rules this module never breaks:

* **The address shown is a MEASURED one.** ``online`` is only reported after
  ``/api/health``, the UI, the auth-gate status and the data-API check have all
  answered THROUGH the public hostname. A running cloudflared process is not
  success.
* **OFF means off.** ``disable()`` stops the tunnel process tree, writes the
  offline status file and makes :func:`app.remote_access.public_url` return
  ``None``, so the address is not advertised anywhere in the app either.
* **The user's switch and the real state are different things.** ``requested``
  is what was saved in ``config.toml``; ``status`` is what is actually
  happening right now (``disabled`` / ``starting`` / ``online`` / ``failed``).
* **No secrets, ever.** A quick tunnel needs no credentials, and the log tail
  kept for diagnostics is truncated and never published: only the address and a
  human reason leave this module.
* **One tunnel per installation.** ``enable()`` is idempotent under a lock, and
  a session owned by the launcher (``MC5_PUBLIC=1`` or a fresh status file with
  a live tunnel pid) is ADOPTED, never duplicated — and the app says so instead
  of pretending it disabled something it does not control.
"""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

log = logging.getLogger("app.public_access")

# ------------------------------------------------------------------ states
STATE_DISABLED = "disabled"
STATE_STARTING = "starting"
STATE_ONLINE = "online"
STATE_FAILED = "failed"

#: Status file vocabulary (shared with the launcher and remote_access).
FILE_ONLINE = "online"
FILE_STARTING = "starting"
FILE_OFFLINE = "offline"

#: Tunables. The heartbeat is one 15-byte request; the full four-check
#: verification is far more chatty, so it runs on the first connection, right
#: after a failure and once every FULL_VERIFY_EVERY heartbeats.
HEARTBEAT_SECONDS = 60
FULL_VERIFY_EVERY = 10
TUNNEL_URL_TIMEOUT = 60
RE_ESTABLISH_PAUSE = 15

URL_RE = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")
HEALTHY = {"status": "ok"}
APP_MARKERS = ('id="root"', "Math Checker", "Math Correct")

#: Every child this module spawns, so any exit path can stop it.
CHILDREN: list = []


# --------------------------------------------------------------------- utils
def http(url: str, timeout: float = 15.0) -> tuple[int, str]:
    """GET a URL, returning ``(status_code, body)``. Never raises."""
    try:
        request = urllib.request.Request(
            url, headers={"User-Agent": "MathChecker-PublicCheck/1"})
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(request, timeout=timeout) as response:
            return response.status, response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        try:
            body = exc.read().decode("utf-8", "replace")
        except Exception:  # noqa: BLE001
            body = ""
        return exc.code, body
    except Exception:  # noqa: BLE001 — an unreachable endpoint is an answer
        return 0, ""


def find_cloudflared(root: Path | None = None) -> str | None:
    """Locate cloudflared, preferring the copy shipped with the application.

    ``root`` is the application folder (the release build puts a tested copy in
    ``tools/``). An explicit ``MC5_CLOUDFLARED`` override wins so an operator
    can point at their own install.
    """
    exe = "cloudflared.exe" if os.name == "nt" else "cloudflared"
    candidates: list[Path] = []
    override = (os.environ.get("MC5_CLOUDFLARED") or "").strip()
    if override:
        candidates.append(Path(override))
    if root is not None:
        candidates += [Path(root) / "tools" / exe, Path(root) / "tools" / "cloudflared.exe"]
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)
    found = shutil.which(exe) or shutil.which("cloudflared")
    if found:
        return found
    for candidate in (
        Path(r"C:\Program Files (x86)\cloudflared\cloudflared.exe"),
        Path(r"C:\Program Files\cloudflared\cloudflared.exe"),
    ):
        if candidate.is_file():
            return str(candidate)
    return None


def tunnel_argv(cloudflared: str, port: int) -> list[str]:
    """The ONE tunnel command, always aimed at the ACTUAL running port."""
    return [cloudflared, "tunnel", "--no-autoupdate",
            "--url", f"http://127.0.0.1:{port}"]


class Managed:
    """A child process with a bounded line buffer and a hard stop."""

    def __init__(self, name: str, argv: list[str], *, env: dict | None = None,
                 log_file: Path | None = None, cwd: Path | None = None):
        self.name = name
        self.lines: list[str] = []
        self.dead = False
        self._lock = threading.Lock()
        kwargs: dict = {}
        if cwd is not None:
            kwargs["cwd"] = str(cwd)
        if os.name == "nt":
            kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
        self.proc = subprocess.Popen(
            argv, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace", **kwargs)
        CHILDREN.append(self)
        self._sink = None
        if log_file:
            try:
                log_file.parent.mkdir(parents=True, exist_ok=True)
                self._sink = open(log_file, "a", encoding="utf-8")
            except OSError:
                self._sink = None
        self._thread = threading.Thread(target=self._pump, daemon=True)
        self._thread.start()

    def _pump(self) -> None:
        try:
            for line in self.proc.stdout:
                line = line.rstrip()
                with self._lock:
                    self.lines.append(line)
                    if len(self.lines) > 200:
                        del self.lines[:100]
                if self._sink:
                    try:
                        self._sink.write(line + "\n")
                        self._sink.flush()
                    except OSError:
                        pass
        finally:
            self.dead = True

    def tail(self, n: int = 6) -> str:
        with self._lock:
            return " | ".join(self.lines[-n:])

    @property
    def pid(self) -> int:
        """The child's process id (the only detail callers need)."""
        return int(self.proc.pid)

    def alive(self) -> bool:
        return self.proc.poll() is None

    def stop(self) -> None:
        """Stop the process tree (the tunnel's own supported mechanism)."""
        try:
            if os.name == "nt":
                subprocess.run(["taskkill", "/PID", str(self.proc.pid), "/T", "/F"],
                               capture_output=True, timeout=15)
            else:
                self.proc.terminate()
                try:
                    self.proc.wait(timeout=8)
                except subprocess.TimeoutExpired:
                    self.proc.kill()
        except Exception:  # noqa: BLE001
            pass
        if self._sink:
            try:
                self._sink.close()
            except Exception:  # noqa: BLE001
                pass
        if self in CHILDREN:
            CHILDREN.remove(self)


def wait_for_tunnel_url(proc: "Managed", seconds: int = TUNNEL_URL_TIMEOUT) -> str | None:
    deadline = time.time() + seconds
    while time.time() < deadline:
        if proc.dead:
            return None
        for line in list(proc.lines):
            match = URL_RE.search(line)
            if match:
                return match.group(0)
        time.sleep(0.5)
    return None


def wait_for_health(port: int, proc: "Managed", seconds: int = 90) -> bool:
    deadline = time.time() + seconds
    while time.time() < deadline:
        if proc.dead:
            return False
        code, body = http(f"http://127.0.0.1:{port}/api/health", timeout=5)
        if code == 200:
            try:
                if json.loads(body) == HEALTHY:
                    return True
            except ValueError:
                pass
        time.sleep(0.5)
    return False


def verify_alive(base: str, timeout: int = 20,
                 http_fn=None) -> tuple[bool, str]:
    """Cheap liveness beat: ONE request for the smallest endpoint we have."""
    call = http_fn or http
    code, body = call(f"{base}/api/health", timeout=timeout)
    try:
        if code == 200 and json.loads(body) == HEALTHY:
            return True, ""
    except ValueError:
        pass
    return False, "The public endpoint did not answer /api/health."


def verify_public(base: str, timeout: int = 90,
                  http_fn=None) -> tuple[bool, dict, str]:
    """Prove the public endpoint actually serves Math Correct.

    Every check travels through the public hostname -> Cloudflare -> tunnel ->
    this machine. The last one also proves the auth gate is answering, so an
    OPEN data API is never published as a working address.
    """
    call = http_fn or http
    checks: dict[str, object] = {}
    deadline = time.time() + timeout
    health_ok = False
    while time.time() < deadline and not health_ok:
        code, body = call(f"{base}/api/health", timeout=15)
        try:
            health_ok = code == 200 and json.loads(body) == HEALTHY
        except ValueError:
            health_ok = False
        if not health_ok:
            time.sleep(1)
    checks["health"] = health_ok
    if not health_ok:
        return False, checks, "Public endpoint did not answer /api/health."

    code, body = call(f"{base}/", timeout=15)
    checks["ui"] = code == 200 and any(m in body for m in APP_MARKERS)
    if not checks["ui"]:
        return False, checks, ("Public endpoint answered but did not serve the "
                               "Math Checker UI.")

    code, body = call(f"{base}/api/auth/status", timeout=15)
    try:
        payload = json.loads(body)
        checks["auth_status"] = code == 200 and payload.get("public_mode") is True
    except ValueError:
        checks["auth_status"] = False
    if not checks["auth_status"]:
        return False, checks, ("Public API status endpoint failed (the "
                               "authentication layer is not in public mode).")

    code, _ = call(f"{base}/api/dashboard", timeout=15)
    checks["api_gated"] = code in (401, 403, 503)
    if not checks["api_gated"]:
        return False, checks, ("The data API answered an unauthenticated "
                               "request — refusing to publish this address.")

    return True, checks, ""


# --------------------------------------------------------------- the switch
def requested() -> bool:
    """The saved preference: is the public address switch ON?

    Read through ``auth_gate.public_mode_enabled`` so the switch, the auth gate
    and the launcher's ``MC5_PUBLIC=1`` can never disagree about what is on.
    """
    from .auth_gate import public_mode_enabled
    try:
        return bool(public_mode_enabled())
    except Exception:  # noqa: BLE001 — a broken config stays OFF
        return False


def _launcher_owns_this_process() -> bool:
    """True when ``remote.py`` started this server (public mode is its job)."""
    return os.environ.get("MC5_PUBLIC") == "1"


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(0x1000, False, pid)
        if not handle:
            return False
        code = ctypes.c_ulong(0)
        kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
        kernel32.CloseHandle(handle)
        return code.value == 259
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def server_port() -> int:
    """The port THIS process is serving on (env first, then config)."""
    raw = (os.environ.get("MC5_PORT") or "").strip()
    if raw.isdigit():
        return int(raw)
    try:
        from .config import get_config
        return int(get_config().network.port)
    except Exception:  # noqa: BLE001
        return 8642


def app_root() -> Path:
    """The application folder (where ``tools/cloudflared`` ships)."""
    return Path(__file__).resolve().parents[2]


# ------------------------------------------------------------------- manager
class PublicAccess:
    """Start/stop/monitor the ONE app-managed tunnel (single instance)."""

    def __init__(self, *, port: int | None = None):
        self._lock = threading.RLock()
        self._tunnel: Managed | None = None
        self._url: str | None = None
        self._status = STATE_DISABLED
        self._reason: str | None = None
        self._checks: dict = {}
        self._verified_at: float | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._port = port

    # -- helpers ------------------------------------------------------------
    def port(self) -> int:
        return self._port or server_port()

    def _write_status(self, status: str, *, url: str | None,
                      reason: str | None, checks: dict | None = None,
                      tunnel_pid: int | None = None) -> None:
        from .remote_access import write_status
        write_status({
            "status": status,
            "public_url": url,
            "reason": reason,
            "last_verified": (time.strftime("%Y-%m-%d %H:%M:%S")
                              if status == FILE_ONLINE else None),
            "verified_at": time.time() if status == FILE_ONLINE else None,
            "port": self.port(),
            "checks": checks or {},
            "managed_by": "application",
            "tunnel_pid": tunnel_pid,
        })

    def _remember_address(self, url: str | None) -> None:
        """Remember the last verified address (never shown as active)."""
        if not url:
            return
        try:
            from .config import get_config, save_config
            cfg = get_config()
            if cfg.security.last_public_url != url:
                cfg.security.last_public_url = url
                save_config(cfg)
        except Exception:  # noqa: BLE001 — remembering is best-effort
            pass

    def external_session(self) -> dict | None:
        """A public session this application did NOT start, if one is live.

        Two cases: this server was started by ``remote.py`` (``MC5_PUBLIC=1``),
        or another process left a fresh, verified status file with a live
        tunnel pid. Either way the app must not start a second tunnel and must
        not claim it switched public access off.
        """
        if _launcher_owns_this_process():
            from .remote_access import read_status
            state = read_status()
            return {"owner": "launcher",
                    "public_url": state.get("public_url"),
                    "status": state.get("status"),
                    "reason": state.get("reason")}
        from .remote_access import read_status, status_is_fresh
        state = read_status()
        if not status_is_fresh(state):
            return None
        pid = int(state.get("tunnel_pid") or 0)
        if not pid or not _pid_alive(pid):
            return None
        if self._tunnel is not None and self._tunnel.pid == pid:
            return None  # that is OUR own session
        return {"owner": "external", "public_url": state.get("public_url"),
                "status": state.get("status"), "reason": state.get("reason"),
                "tunnel_pid": pid}

    # -- state --------------------------------------------------------------
    def state(self) -> dict:
        with self._lock:
            external = self.external_session()
            requested_flag = requested()
            status = self._status
            url = self._url
            reason = self._reason
            checks = dict(self._checks)
            verified_at = self._verified_at
            managed_by = "application" if self._tunnel is not None else None
            if external is None and requested_flag and status == STATE_DISABLED:
                # Switched ON (e.g. restored after a restart) but this process
                # has not established its tunnel yet: report STARTING, never a
                # running state and never an address.
                status = STATE_STARTING
                reason = reason or "Public access is switched on — starting the tunnel…"
            if external is not None:
                managed_by = "launcher" if external["owner"] == "launcher" else "external"
                online = bool(external.get("public_url"))
                status = STATE_ONLINE if online else (
                    STATE_STARTING if external.get("status") == FILE_STARTING
                    else STATE_FAILED)
                url = external.get("public_url") if online else None
                reason = None if online else external.get("reason")
                verified_at = None
            elif not requested_flag and status != STATE_FAILED:
                status = STATE_DISABLED
            # The address is published ONLY while a verification stands: a
            # failed/starting/disabled session must never advertise one, or the
            # panel would offer an address that no longer answers.
            if status != STATE_ONLINE:
                url = None
            cloudflared = find_cloudflared(app_root())
            can_enable, blocked = self._enable_preconditions(cloudflared)
            from .config import get_config
            try:
                last_address = get_config().security.last_public_url or None
            except Exception:  # noqa: BLE001
                last_address = None
            return {
                "requested": requested_flag,
                "status": status,
                "public_url": url,
                "reason": reason,
                "last_address": last_address,
                "managed_by": managed_by,
                "checks": checks,
                "last_verified": verified_at,
                "port": self.port(),
                "tunnel_running": bool(self._tunnel is not None and self._tunnel.alive()),
                "cloudflared_available": cloudflared is not None,
                "can_enable": can_enable,
                "blocked_reason": blocked,
                "mode": "public" if requested_flag else "local",
            }

    def _enable_preconditions(self, cloudflared: str | None) -> tuple[bool, str | None]:
        """Can public access be switched on right now? (reason when not)"""
        external = self.external_session()
        if external is not None:
            if external["owner"] == "launcher":
                return True, ("Public access is provided by start_public.bat / "
                              "remote.py in this session; stop it there (Ctrl+C).")
            return True, ("Another public-access session is already running on "
                          "this computer; stop it first.")
        if cloudflared is None:
            return False, ("cloudflared was not found, so no public tunnel can "
                           "be created on this computer. Install Cloudflare "
                           "Tunnel (or place cloudflared.exe in tools\\), then "
                           "try again.")
        if not self._has_sign_in_account():
            return False, ("No sign-in account exists yet, so nobody could get "
                           "in through the public address. Create one on the "
                           "Host account card on this page (Access & accounts), "
                           "then switch public access on.")
        return True, None

    def _has_sign_in_account(self) -> bool:
        """Would an authorised user still be able to sign in? (fail closed)"""
        try:
            from . import auth_gate as ag
            if ag.credentials_configured():
                return True
            return bool(ag._has_any_account())
        except Exception:  # noqa: BLE001 — unknown means "do not expose"
            return False

    # -- start / stop -------------------------------------------------------
    def enable(self) -> dict:
        """Switch public access ON: persist, start, VERIFY, then publish."""
        with self._lock:
            if self._tunnel is not None and self._tunnel.alive():
                return self.state()  # idempotent: one tunnel per installation
            cloudflared = find_cloudflared(app_root())
            can_enable, blocked = self._enable_preconditions(cloudflared)
            if not can_enable:
                self._status = STATE_FAILED
                self._reason = blocked
                self._write_status(FILE_OFFLINE, url=None, reason=blocked)
                return self.state()

            # Public mode must be on BEFORE the verification runs: the checks
            # prove the auth gate is answering, which is only true once this
            # preference is persisted.
            from .config import get_config, save_config
            cfg = get_config()
            if not cfg.security.enabled:
                cfg.security.enabled = True
                save_config(cfg)

            self._status = STATE_STARTING
            self._reason = "Creating the public tunnel…"
            self._checks = {}
            self._write_status(FILE_STARTING, url=None, reason=self._reason)

            from .logging_setup import log_event
            log_event("security", "public address starting",
                      port=self.port())
            tunnel = Managed("tunnel", tunnel_argv(cloudflared, self.port()),
                             log_file=self._tunnel_log())
            url = wait_for_tunnel_url(tunnel)
            if not url:
                detail = tunnel.tail(4)
                tunnel.stop()
                self._fail_and_rollback(
                    "The tunnel could not connect to Math Checker on port "
                    f"{self.port()}." + (f" Last output: {detail}" if detail else ""))
                return self.state()

            ok, checks, reason = verify_public(url)
            if not ok:
                tunnel.stop()
                self._fail_and_rollback(
                    f"The public endpoint was created but verification failed. {reason}")
                return self.state()

            self._tunnel = tunnel
            self._url = url
            self._checks = checks
            self._verified_at = time.time()
            self._status = STATE_ONLINE
            self._reason = None
            self._write_status(FILE_ONLINE, url=url, reason=None, checks=checks,
                               tunnel_pid=tunnel.pid)
            self._remember_address(url)
            log_event("security", "public address online", port=self.port())
            self._start_monitor()
            return self.state()

    def _fail_and_rollback(self, reason: str) -> None:
        """A switch that could not start is switched back off (honestly).

        Leaving "public mode" enabled with no working endpoint would force
        sign-in for no benefit, so the request returns to OFF and the reason is
        reported as-is.
        """
        try:
            from .config import get_config, save_config
            cfg = get_config()
            if cfg.security.enabled:
                cfg.security.enabled = False
                save_config(cfg)
        except Exception:  # noqa: BLE001
            pass
        self._status = STATE_FAILED
        self._url = None
        self._checks = {}
        self._reason = (f"{reason} The Public Address setting was returned to "
                        "OFF; nothing is exposed.")
        self._write_status(FILE_OFFLINE, url=None, reason=self._reason)
        from .logging_setup import log_event
        log_event("security", "public address failed", level=logging.WARNING,
                  reason=reason)

    def disable(self) -> dict:
        """Switch public access OFF: stop the tunnel, stop advertising it."""
        with self._lock:
            external = self.external_session()
            if external is not None:
                from .errors import AppError
                if external["owner"] == "launcher":
                    raise AppError(
                        "Public access in this session is run by "
                        "start_public.bat / remote.py, which the application "
                        "does not control. Stop that window (Ctrl+C) to end "
                        "public access.", status=409,
                        code="public_access_external_session")
                raise AppError(
                    "A public-access session started outside this application "
                    "is still running, so public access cannot be switched "
                    "off from here. Stop it, then try again.", status=409,
                    code="public_access_external_session")

            self._stop.set()
            thread, self._thread = self._thread, None
            if thread is not None and thread.is_alive():
                thread.join(timeout=5)

            tunnel, self._tunnel = self._tunnel, None
            if tunnel is not None:
                tunnel.stop()
            self._url = None
            self._status = STATE_DISABLED
            self._reason = None
            self._checks = {}
            self._verified_at = None

            from .config import get_config, save_config
            cfg = get_config()
            if cfg.security.enabled:
                cfg.security.enabled = False
                save_config(cfg)
            # The status file goes offline so nothing in the app can advertise
            # the address; the address itself stays remembered for the switch.
            self._write_status(FILE_OFFLINE, url=None,
                               reason="Public address is switched off in Settings.")
            from .logging_setup import log_event
            log_event("security", "public address switched off")
            return self.state()

    def _tunnel_log(self) -> Path:
        try:
            from .config import DATA_DIR
            return Path(DATA_DIR) / "logs" / "public_tunnel.log"
        except Exception:  # noqa: BLE001
            return Path("public_tunnel.log")

    # -- monitor ------------------------------------------------------------
    def _start_monitor(self) -> None:
        self._stop.clear()
        if self._thread is not None and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._monitor, daemon=True,
                                        name="public-access-monitor")
        self._thread.start()

    def _monitor(self) -> None:
        """Keep the truth in the status file while the switch is on."""
        beats = 0
        failures = 0
        while not self._stop.wait(HEARTBEAT_SECONDS):
            with self._lock:
                tunnel, url = self._tunnel, self._url
            if tunnel is None or not tunnel.alive() or not url:
                # The address stops being advertised the moment the tunnel is
                # gone: offline NOW, then try again (never a dead URL kept on
                # screen while a reconnect is attempted).
                self._write_status(
                    FILE_OFFLINE, url=None,
                    reason="The tunnel stopped. Reconnecting…")
                self._reestablish()
                beats = 0
                failures = 0
                continue
            beats += 1
            if failures or beats >= FULL_VERIFY_EVERY:
                ok, checks, reason = verify_public(url, timeout=30)
                beats = 0
            else:
                ok, reason = verify_alive(url, timeout=20)
                checks = dict(self._checks)
            with self._lock:
                if ok:
                    failures = 0
                    self._checks = checks
                    self._status = STATE_ONLINE
                    self._reason = None
                    self._verified_at = time.time()
                    self._write_status(FILE_ONLINE, url=url, reason=None,
                                       checks=checks, tunnel_pid=tunnel.pid)
                    self._remember_address(url)
                else:
                    failures += 1
                    self._status = STATE_FAILED
                    self._reason = f"External health check failed. {reason}"
                    # Offline immediately: a failing tunnel must never keep
                    # advertising an address that no longer answers.
                    self._write_status(FILE_OFFLINE, url=None,
                                       reason=self._reason)
                    if failures >= 3:
                        self._stop_tunnel_locked()

    def _stop_tunnel_locked(self) -> None:
        with self._lock:
            tunnel, self._tunnel = self._tunnel, None
            self._url = None
        if tunnel is not None:
            tunnel.stop()

    def shutdown(self) -> None:
        """Stop the app-managed tunnel WITHOUT changing the saved preference.

        Called when the application is stopping: no orphan cloudflared process
        and no advertised address are left behind, while the switch keeps its
        value so the next launch brings the endpoint back
        (:meth:`enable` runs again from the app's startup).
        """
        with self._lock:
            self._stop.set()
            thread, self._thread = self._thread, None
        if thread is not None and thread.is_alive():
            thread.join(timeout=5)
        self._stop_tunnel_locked()
        with self._lock:
            self._checks = {}
            self._verified_at = None
            if requested():
                self._status = STATE_STARTING
                self._reason = ("Public access is switched on and will return "
                                "at the next launch.")
                self._write_status(FILE_OFFLINE, url=None, reason=self._reason)
            else:
                self._status = STATE_DISABLED
                self._reason = None

    def reverify(self) -> dict:
        """Re-run the full verification now (an explicit Re-check in the UI)."""
        with self._lock:
            tunnel, url = self._tunnel, self._url
        if tunnel is not None and url:
            ok, checks, reason = verify_public(url, timeout=30)
            with self._lock:
                if ok:
                    self._checks = checks
                    self._status = STATE_ONLINE
                    self._reason = None
                    self._verified_at = time.time()
                    self._write_status(FILE_ONLINE, url=url, reason=None,
                                       checks=checks, tunnel_pid=tunnel.pid)
                else:
                    self._status = STATE_FAILED
                    self._reason = f"External health check failed. {reason}"
                    # Stop advertising immediately: the panel falls back to the
                    # saved (inactive) address and the reason.
                    self._url = None
                    self._write_status(FILE_OFFLINE, url=None,
                                       reason=self._reason)
        return self.state()

    def _reestablish(self) -> None:
        """The tunnel died while the switch is ON: report, then try again."""
        with self._lock:
            if self._status != STATE_STARTING:
                self._status = STATE_STARTING
                self._reason = "The tunnel stopped. Reconnecting…"
                self._write_status(FILE_STARTING, url=None, reason=self._reason)
            self._stop_tunnel_locked()
        self._stop.wait(RE_ESTABLISH_PAUSE)
        if self._stop.is_set():
            return
        try:
            self.enable()
        except Exception as exc:  # noqa: BLE001 — the monitor must survive
            log.warning("public access re-establish failed: %s", exc)


_manager: PublicAccess | None = None
_manager_lock = threading.Lock()


def manager() -> PublicAccess:
    """The process-wide manager (one tunnel per installation)."""
    global _manager
    with _manager_lock:
        if _manager is None:
            _manager = PublicAccess()
        return _manager


def status() -> dict:
    return manager().state()


def enable() -> dict:
    return manager().enable()


def disable() -> dict:
    return manager().disable()


def refresh() -> dict:
    """Re-verify right now (used by an explicit "Re-check" in the UI)."""
    return manager().reverify()


def shutdown() -> None:
    """Stop the app-managed tunnel as the application exits."""
    manager().shutdown()


def autostart_if_requested() -> bool:
    """Bring the public endpoint back after a restart (the switch is ON).

    Returns True when this call started (or found) a session. A launcher-owned
    session is never duplicated: ``remote.py`` is already handling it.
    """
    mgr = manager()
    if mgr.external_session() is not None:
        return False
    if not requested():
        return False
    mgr.enable()
    return True
