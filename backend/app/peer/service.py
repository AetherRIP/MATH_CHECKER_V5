"""Phase 2 — the supervised peer service: lifecycle, secure startup, local IPC.

What this is
------------
A supervisor for exactly **one** peer process per installation, plus every trust
decision that has to succeed before that process may report ``RUNNING``. It opens
no socket: the child speaks a JSON-lines protocol over its own stdin/stdout
pipes, which are reachable only by a process that can already run code as this
Windows user. Network transports start in Phase 3 (plan §13); nothing here
addresses, discovers or connects to another machine.

Why a child process at all
--------------------------
The peer layer must be provably inert with respect to marking (plan §10.4, R-10).
A separate interpreter means a peer crash — an import-time failure, a runaway
loop, a leaked handle — cannot reach the marking engine's memory, and ``stop()``
is a real process boundary rather than a flag in a shared process.

Trust boundary
--------------
* The supervisor (this module) runs inside the backend, in the signed-in user's
  session. It owns the DPAPI scope, so it is the only party that can read the
  private key.
* The child receives a credentials **path**, not the key. It re-verifies the
  chain itself, so a supervisor bug cannot make it serve with material it was
  never allowed to have.
* Neither side ever logs, sends or returns private key material: ``status()`` is
  built from public fields only, and a test asserts it.
"""
from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from ..distributed import secure_store
from . import (CERT_EXPIRED, MALFORMED, ROSTER_INCOMPLETE, WRONG_DEPARTMENT,
               PeerError, PeerVerificationError)
from .ca import verify_certificate
from .identity import peer_dir, raw_public_bytes
from .invite import PeerCredentials

# -- states -----------------------------------------------------------------
# The vocabulary is the plan's (§10.2) plus the two conditions a supervisor can
# report and a certificate cannot: the switch is off, or the process died.
DISABLED = "DISABLED"
STARTING = "STARTING"
RUNNING = "RUNNING"
STOPPING = "STOPPING"
STOPPED = "STOPPED"
UNHEALTHY = "UNHEALTHY"
AUTH_FAILED = "AUTH_FAILED"
CREDENTIALS_UNAVAILABLE = "CREDENTIALS_UNAVAILABLE"

#: Wire version of the local protocol. A request announcing anything else is
#: refused rather than half-understood, exactly like the signed documents.
IPC_PROTOCOL = "mc5-peer-ipc-v1"

#: The only operations the child accepts. There is deliberately no "run this",
#: no file path, no shell: the IPC surface is not a command interface.
REQUEST_OPS = frozenset({"ping", "status", "shutdown"})

#: A request larger than this is not one of ours.
MAX_REQUEST_CHARS = 64 * 1024
MAX_ID_CHARS = 64

LOCK_FILENAME = "service.lock"


class IpcError(Exception):
    """A request the child refused. ``code`` is stable; ``detail`` is a sentence."""

    def __init__(self, code: str, detail: str = "") -> None:
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}" if detail else code)


def validate_request(raw: str) -> dict:
    """Parse and validate one IPC request, or raise :class:`IpcError`.

    Kept as a pure function so the validation rules can be tested exhaustively
    without starting a process — and so both ends can use it.
    """
    if not isinstance(raw, str):
        raise IpcError("empty_request", "a request is required")
    # Size is checked before content: an oversized line must be refused without
    # being parsed, and "    ...    " is not "empty" after a 64 KB upload.
    if len(raw) > MAX_REQUEST_CHARS:
        raise IpcError("request_too_large",
                       f"a request must be at most {MAX_REQUEST_CHARS} characters")
    if not raw.strip():
        raise IpcError("empty_request", "a request is required")
    try:
        payload = json.loads(raw)
    except ValueError as exc:
        raise IpcError("malformed_request", "the request is not JSON") from exc
    if not isinstance(payload, dict):
        raise IpcError("malformed_request", "the request must be an object")
    if payload.get("protocol") != IPC_PROTOCOL:
        raise IpcError("unsupported_protocol",
                       f"expected protocol {IPC_PROTOCOL!r}")
    request_id = payload.get("request_id")
    if (not isinstance(request_id, str) or not request_id.strip()
            or len(request_id) > MAX_ID_CHARS):
        raise IpcError("malformed_request", "request_id must be a short string")
    op = payload.get("operation")
    if op not in REQUEST_OPS:
        raise IpcError("unsupported_operation",
                       f"operation {op!r} is not one of {sorted(REQUEST_OPS)}")
    return payload


# -- secure startup ---------------------------------------------------------

@dataclass(frozen=True)
class StoreProbe:
    """What a write/read round trip through the secure store actually proved."""

    writable: bool
    encrypted: bool
    detail: str = ""


def probe_secure_store(target: str | Path | None = None) -> StoreProbe:
    """Write a probe blob, read it back, and report whether it was encrypted.

    The store falls back to a plaintext marker when DPAPI is unavailable
    (``secure_store``'s deliberate behaviour). Phase 2 must *know* which of the
    two happened rather than assume encryption, because D1 puts the root key in
    this store: a degraded store is reported, not hidden.
    """
    path = Path(target) if target is not None else peer_dir() / "store_probe.json"
    token = uuid.uuid4().hex
    try:
        secure_store.save(path, {"probe": token})
        marker = path.read_bytes()[:4]
        data = secure_store.load(path)
    except OSError as exc:
        return StoreProbe(False, False, f"the secure store is not writable: {exc}")
    finally:
        try:
            path.unlink()
        except OSError:
            pass
    if not isinstance(data, dict) or data.get("probe") != token:
        return StoreProbe(False, False,
                          "the secure store returned a different value than it stored")
    encrypted = marker == b"MC1D"
    return StoreProbe(
        True, encrypted,
        "DPAPI-protected for the current user" if encrypted
        else "stored WITHOUT encryption (plaintext marker); DPAPI is unavailable "
             "for this account or process")


def verify_credentials(credentials: PeerCredentials, *,
                       pinned_department_key: str | None = None,
                       now: float | None = None) -> dict:
    """The gate between "a credential file exists" and "the service may run".

    Checks, in this order: pinned department key (if one is supplied) ->
    certificate chain against that key -> identifier/key binding -> validity
    window -> revocation -> roster membership -> identity/credentials agreement.
    Every failure is a :class:`PeerVerificationError` with the Phase 1 reason, so
    the status surface can explain it without inventing a second vocabulary.

    Returns the public snapshot used by ``status()``. Never includes a key.
    """
    moment = float(now if now is not None else time.time())
    if pinned_department_key is not None:
        if (raw_public_bytes(credentials.department_public_key)
                != raw_public_bytes(pinned_department_key)):
            raise PeerVerificationError(
                WRONG_DEPARTMENT,
                "the stored credentials were issued by a different department key")

    verify_certificate(credentials.certificate, credentials.department_public_key,
                       expected_dept_id=credentials.dept_id, now=moment,
                       revoked=credentials.revocations)

    if credentials.roster.find(credentials.identity.device_id) is None:
        raise PeerVerificationError(
            ROSTER_INCOMPLETE, "the stored roster does not list this device")

    return {
        "device_id": credentials.identity.device_id,
        "dept_id": credentials.dept_id,
        "department_name": credentials.department_name,
        "certificate_expires_at": credentials.certificate.not_after,
        "certificate_days_remaining": round(
            (credentials.certificate.not_after - moment) / 86400.0, 1),
        "roster_epoch": credentials.roster.epoch,
        "revocation_epoch": credentials.revocations.epoch,
        "roles": list(credentials.certificate.roles),
        "warn_renewal": (credentials.certificate.not_after - moment) <= 15 * 86400.0,
    }


# -- status -----------------------------------------------------------------

@dataclass(frozen=True)
class ServiceStatus:
    """A truthful snapshot. Public fields only — no key material, ever."""

    state: str
    detail: str = ""
    reason: str | None = None
    pid: int | None = None
    restarts: int = 0
    started_at: float | None = None
    last_health_at: float | None = None
    encrypted_store: bool | None = None
    store_detail: str = ""
    peer: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "state": self.state,
            "detail": self.detail,
            "reason": self.reason,
            "pid": self.pid,
            "restarts": self.restarts,
            "started_at": self.started_at,
            "last_health_at": self.last_health_at,
            "encrypted_store": self.encrypted_store,
            "store_detail": self.store_detail,
            "peer": dict(self.peer),
        }


# -- duplicate prevention ---------------------------------------------------

def _pid_alive(pid: int) -> bool:
    """Is ``pid`` a live process this user can see? (No dependencies.)"""
    if pid <= 0:
        return False
    if os.name == "nt":                      # pragma: no cover - Windows only
        import ctypes

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        handle = ctypes.windll.kernel32.OpenProcess(
            PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
        if not handle:
            return False
        try:
            return ctypes.windll.kernel32.WaitForSingleObject(handle, 0) != 0
        finally:
            ctypes.windll.kernel32.CloseHandle(handle)
    try:                                     # pragma: no cover - POSIX only
        os.kill(int(pid), 0)
    except OSError:
        return False
    return True


def process_usage(pid: int) -> dict:
    """Working set (bytes) and total CPU seconds for a live process.

    Used by the resource measurement so the Phase 2 numbers come from the process
    that is actually running rather than from an estimate.
    """
    if os.name == "nt":                      # pragma: no cover - Windows only
        import ctypes
        from ctypes import wintypes

        class PMC(ctypes.Structure):
            _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD),
                        ("PeakWorkingSetSize", ctypes.c_size_t),
                        ("WorkingSetSize", ctypes.c_size_t),
                        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                        ("QuotaPagedPoolUsage", ctypes.c_size_t),
                        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                        ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                        ("PagefileUsage", ctypes.c_size_t),
                        ("PeakPagefileUsage", ctypes.c_size_t)]

        class FT(ctypes.Structure):
            _fields_ = [("low", wintypes.DWORD), ("high", wintypes.DWORD)]

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        handle = ctypes.windll.kernel32.OpenProcess(
            PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
        if not handle:
            return {"available": False, "detail": "process not openable"}
        try:
            counters = PMC()
            counters.cb = ctypes.sizeof(PMC)
            ok = ctypes.windll.psapi.GetProcessMemoryInfo(
                handle, ctypes.byref(counters), counters.cb)
            created, exited, kernel, user = FT(), FT(), FT(), FT()
            ctypes.windll.kernel32.GetProcessTimes(
                handle, ctypes.byref(created), ctypes.byref(exited),
                ctypes.byref(kernel), ctypes.byref(user))
            cpu = ((kernel.high << 32 | kernel.low) + (user.high << 32 | user.low)) / 1e7
            return {"available": bool(ok), "working_set_bytes": int(counters.WorkingSetSize),
                    "cpu_seconds": round(cpu, 3)}
        finally:
            ctypes.windll.kernel32.CloseHandle(handle)
    return {"available": False, "detail": "measurement implemented for Windows"}


# -- the supervisor ---------------------------------------------------------

class PeerService:
    """Owns at most one peer child process, and the state that describes it."""

    def __init__(self, *, directory: str | Path | None = None,
                 credentials_path: str | Path | None = None,
                 python: str | None = None,
                 pinned_department_key: str | None = None,
                 start_timeout: float = 25.0,
                 request_timeout: float = 5.0,
                 stop_timeout: float = 6.0,
                 max_restarts: int = 3,
                 restart_delay: float = 1.0,
                 popen=None, clock=None) -> None:
        self._dir = Path(directory) if directory is not None else peer_dir()
        self._credentials_path = (Path(credentials_path) if credentials_path is not None
                                  else self._dir / "credentials.json")
        self._python = python or sys.executable
        self._pinned_key = pinned_department_key
        self._start_timeout = float(start_timeout)
        self._request_timeout = float(request_timeout)
        self._stop_timeout = float(stop_timeout)
        self._max_restarts = int(max_restarts)
        self._restart_delay = float(restart_delay)
        self._popen = popen or subprocess.Popen
        self._clock = clock or time.time
        self._lock = threading.RLock()
        self._proc = None
        self._lines: queue.Queue = queue.Queue()
        self._reader: threading.Thread | None = None
        self._stop_requested = False
        self._restarts = 0
        self._status = ServiceStatus(state=STOPPED, detail="not started")

    # -- observation --------------------------------------------------------
    @property
    def status(self) -> ServiceStatus:
        with self._lock:
            return self._status

    def status_dict(self) -> dict:
        return self.status.as_dict()

    def _set(self, state: str, detail: str = "", *, reason: str | None = None,
             **fields) -> ServiceStatus:
        with self._lock:
            current = self._status
            self._status = ServiceStatus(
                state=state, detail=detail, reason=reason,
                pid=fields.get("pid", current.pid),
                restarts=self._restarts,
                started_at=fields.get("started_at", current.started_at),
                last_health_at=fields.get("last_health_at", current.last_health_at),
                encrypted_store=fields.get("encrypted_store", current.encrypted_store),
                store_detail=fields.get("store_detail", current.store_detail),
                peer=fields.get("peer", current.peer),
            )
            return self._status

    # -- lock file (duplicate prevention) -----------------------------------
    @property
    def _lock_path(self) -> Path:
        return self._dir / LOCK_FILENAME

    def _acquire_lock(self) -> tuple[bool, int | None]:
        self._dir.mkdir(parents=True, exist_ok=True)
        for _ in range(2):
            try:
                fd = os.open(self._lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError:
                other = 0
                try:
                    other = int(self._lock_path.read_text(encoding="utf-8").strip() or 0)
                except (OSError, ValueError):
                    other = 0
                if other and _pid_alive(other):
                    return False, other
                try:                          # stale: the owner is gone
                    self._lock_path.unlink()
                except OSError:
                    return False, other or None
                continue
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(str(os.getpid()))
            return True, None
        return False, None

    def _release_lock(self) -> None:
        try:
            if int(self._lock_path.read_text(encoding="utf-8").strip() or 0) == os.getpid():
                self._lock_path.unlink()
        except (OSError, ValueError):
            pass

    # -- lifecycle ----------------------------------------------------------
    def start(self) -> ServiceStatus:
        """Start the child if the switch is on and startup checks pass."""
        with self._lock:
            if self._status.state in (RUNNING, STARTING):
                return self._status
            from . import control

            if not control.is_enabled(self._dir):
                return self._set(DISABLED, "peer networking is switched off")

            self._set(STARTING, "checking the secure store")
            probe = probe_secure_store(self._dir / "store_probe.json")
            if not probe.writable:
                return self._set(UNHEALTHY, probe.detail,
                                 encrypted_store=False, store_detail=probe.detail)

            self._set(STARTING, "loading credentials",
                      encrypted_store=probe.encrypted, store_detail=probe.detail)
            try:
                credentials = PeerCredentials.load(self._credentials_path)
            except PeerError as exc:
                return self._set(CREDENTIALS_UNAVAILABLE, str(exc),
                                 encrypted_store=probe.encrypted,
                                 store_detail=probe.detail)
            if credentials is None:
                return self._set(
                    CREDENTIALS_UNAVAILABLE,
                    "this device has not been invited to a department yet",
                    encrypted_store=probe.encrypted, store_detail=probe.detail)

            try:
                snapshot = verify_credentials(credentials,
                                              pinned_department_key=self._pinned_key,
                                              now=self._clock())
            except PeerVerificationError as exc:
                return self._set(AUTH_FAILED, exc.detail or exc.reason,
                                 reason=exc.reason, encrypted_store=probe.encrypted,
                                 store_detail=probe.detail)
            except (PeerError, OSError, ValueError) as exc:
                return self._set(AUTH_FAILED, f"credentials could not be verified: {exc}",
                                 reason=MALFORMED, encrypted_store=probe.encrypted,
                                 store_detail=probe.detail)

            acquired, other = self._acquire_lock()
            if not acquired:
                return self._set(
                    UNHEALTHY,
                    (f"another peer service is already running (pid {other})"
                     if other else "another peer service holds the lock file"),
                    encrypted_store=probe.encrypted, store_detail=probe.detail,
                    peer=snapshot)

            self._set(STARTING, "starting the peer process",
                      encrypted_store=probe.encrypted, store_detail=probe.detail,
                      peer=snapshot)
            self._stop_requested = False
            self._lines = queue.Queue()
            try:
                process = self._popen(self._command(), stdin=subprocess.PIPE,
                                      stdout=subprocess.PIPE,
                                      stderr=subprocess.DEVNULL, text=True,
                                      encoding="utf-8", bufsize=1,
                                      cwd=str(_backend_root()))
            except Exception as exc:              # noqa: BLE001 - reported, not raised
                self._release_lock()
                return self._set(UNHEALTHY, f"the peer process could not be started: {exc}",
                                 encrypted_store=probe.encrypted,
                                 store_detail=probe.detail, peer=snapshot)

            self._proc = process
            self._reader = threading.Thread(target=self._read_lines,
                                            name="peer-service-reader", daemon=True)
            self._reader.start()

            ready = self._await_ready(process)
            if ready is not None:
                self._terminate()
                self._release_lock()
                return self._set(UNHEALTHY, ready,
                                 encrypted_store=probe.encrypted,
                                 store_detail=probe.detail, peer=snapshot)

            return self._set(RUNNING, "peer service is running", pid=process.pid,
                             started_at=self._clock(), last_health_at=self._clock(),
                             encrypted_store=probe.encrypted,
                             store_detail=probe.detail, peer=snapshot)

    def _command(self) -> list[str]:
        return [self._python, "-m", "app.peer.service_main",
                "--credentials", str(self._credentials_path)]

    def _await_ready(self, process) -> str | None:
        """Wait for the child's own verification. Returns a failure sentence."""
        deadline = time.monotonic() + self._start_timeout
        while time.monotonic() < deadline:
            try:
                message = self._lines.get(timeout=0.1)
            except queue.Empty:
                if process.poll() is not None:
                    return (f"the peer process exited during startup "
                            f"(code {process.returncode})")
                continue
            if message.get("event") == "ready":
                return None
            if message.get("event") == "failed":
                return (f"{message.get('reason') or 'startup failed'}: "
                        f"{message.get('detail') or 'no detail'}")
        return "the peer process did not report ready in time"

    def _read_lines(self) -> None:
        process = self._proc
        if process is None or process.stdout is None:
            return
        try:
            for line in process.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    self._lines.put(json.loads(line))
                except ValueError:
                    self._lines.put({"event": "malformed_output", "raw": line[:200]})
        except (OSError, ValueError):
            pass
        finally:
            code = process.poll()
            if code is None:
                try:
                    code = process.wait(timeout=5)
                except Exception:                 # noqa: BLE001
                    code = None
            self._handle_exit(code)

    def _handle_exit(self, code: int | None) -> None:
        with self._lock:
            if self._stop_requested:
                if self._status.state != STOPPED:
                    self._set(STOPPED, "peer service stopped")
                self._release_lock()
                return
            self._proc = None
        detail = f"the peer process exited unexpectedly (code {code})"
        if self._restarts < self._max_restarts and not self._stop_requested:
            self._restarts += 1
            threading.Thread(target=self._recover, args=(detail,),
                             name="peer-service-recovery", daemon=True).start()
        else:
            with self._lock:
                self._set(UNHEALTHY, detail + "; not restarting again")
                self._release_lock()

    def _recover(self, detail: str) -> None:
        with self._lock:
            self._set(UNHEALTHY, detail + f"; restarting ({self._restarts}/"
                                          f"{self._max_restarts})")
        time.sleep(self._restart_delay)
        if self._stop_requested:
            return
        with self._lock:
            self._status = ServiceStatus(
                state=STOPPED, detail="restarting", restarts=self._restarts,
                encrypted_store=self._status.encrypted_store,
                store_detail=self._status.store_detail, peer=self._status.peer)
        self.start()

    def stop(self) -> ServiceStatus:
        """Ask the child to exit, then make sure it did."""
        with self._lock:
            if self._status.state in (STOPPED, DISABLED) and self._proc is None:
                self._release_lock()
                return self._status
            self._stop_requested = True
            process = self._proc
            self._set(STOPPING, "stopping the peer service")
        if process is not None:
            try:
                self._request({"operation": "shutdown"}, timeout=self._request_timeout)
            except (IpcError, TimeoutError, OSError):
                pass                       # the timeout below is the real contract
            try:
                process.wait(timeout=self._stop_timeout)
            except subprocess.TimeoutExpired:
                self._terminate()
        with self._lock:
            self._proc = None
            self._release_lock()
            return self._set(STOPPED, "peer service stopped")

    def restart(self) -> ServiceStatus:
        self.stop()
        with self._lock:
            self._restarts = 0
        return self.start()

    def _terminate(self) -> None:
        process, self._proc = self._proc, None
        if process is None:
            return
        try:
            process.terminate()
            process.wait(timeout=5)
        except Exception:                     # noqa: BLE001 - best effort
            try:
                process.kill()
            except Exception:                 # noqa: BLE001
                pass

    # -- IPC ----------------------------------------------------------------
    def _request(self, payload: dict, *, timeout: float | None = None) -> dict:
        """Send one validated request and wait for its matching reply."""
        process = self._proc
        if process is None or process.stdin is None:
            raise IpcError("not_running", "the peer service is not running")
        request_id = uuid.uuid4().hex
        message = {"protocol": IPC_PROTOCOL, "request_id": request_id, **payload}
        validate_request(json.dumps(message))     # never send what we would refuse
        process.stdin.write(json.dumps(message) + "\n")
        process.stdin.flush()
        deadline = time.monotonic() + float(timeout if timeout is not None
                                            else self._request_timeout)
        while time.monotonic() < deadline:
            try:
                reply = self._lines.get(timeout=0.05)
            except queue.Empty:
                if process.poll() is not None:
                    raise IpcError("process_exited",
                                   "the peer process exited before answering")
                continue
            if reply.get("request_id") != request_id:
                continue                          # an event, not our reply
            if not reply.get("ok"):
                error = reply.get("error") or {}
                raise IpcError(str(error.get("code") or "refused"),
                               str(error.get("detail") or ""))
            return reply.get("result") or {}
        raise TimeoutError(f"no answer from the peer service within "
                           f"{timeout or self._request_timeout}s")

    def ping(self, *, timeout: float | None = None) -> dict:
        result = self._request({"operation": "ping"}, timeout=timeout)
        with self._lock:
            self._set(self._status.state, self._status.detail,
                      last_health_at=self._clock())
        return result

    def peer_status(self, *, timeout: float | None = None) -> dict:
        return self._request({"operation": "status"}, timeout=timeout)


def _backend_root() -> Path:
    """The directory that must be on ``sys.path`` for the child to import ``app``."""
    return Path(__file__).resolve().parents[2]
