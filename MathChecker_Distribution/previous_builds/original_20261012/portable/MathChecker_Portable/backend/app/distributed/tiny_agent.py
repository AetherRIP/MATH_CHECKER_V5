"""TINY BACKGROUND AGENT (§14-§23, §41, §46-§47).

A small independent process that:
  * keeps a persistent device identity (guaranteed by the OS, not the app),
  * sends a lightweight heartbeat every 5 minutes,
  * queues events locally while the backend is unreachable and replays them
    exactly once on reconnect (client_event_id dedupe),
  * reports errors with sanitized contexts,
  * can drive the device updater — so it still works when the main app fails
    to start after an update (§41).

It NEVER does AI correction, Classroom sync, PDF processing or large sync.
"""
from __future__ import annotations

import hashlib
import json
import os
import platform as _platform
import time
import uuid
from pathlib import Path
from urllib import error as urlerror
from urllib import request as urlrequest

HEARTBEAT_INTERVAL = 5 * 60        # §16
HEARTBEAT_MAX_ATTEMPT_SECONDS = 10

try:  # stdlib-only preference; used for persistence helpers
    from .device_updater import DeviceUpdater, Package  # noqa: F401
except ImportError:  # agent runs standalone from its own folder
    DeviceUpdater = Package = None  # type: ignore

try:  # the ON/OFF preference is read from the agent directory (§extension)
    from . import agent_control
    from .agent_control import STATE_FILENAME as _STATE_FILENAME
except ImportError:  # agent runs standalone from its own folder
    agent_control = None  # type: ignore
    _STATE_FILENAME = "agent_state.json"

try:  # secure at-rest storage for the device credential (§8)
    from . import secure_store
except ImportError:  # agent runs standalone from its own folder
    secure_store = None  # type: ignore

#: The device credential lives beside the device id; never in the installer.
CREDENTIAL_FILENAME = "device_credential.json"
#: Renew this long before expiry so the agent never wakes up unable to work.
CREDENTIAL_RENEW_MARGIN_SECONDS = 60 * 60


def host_platform() -> str:
    """Canonical OS name for update compatibility (windows/linux/darwin)."""
    name = sys_platform()
    if name.startswith("win"):
        return "windows"
    if name.startswith("linux"):
        return "linux"
    if name.startswith("darwin"):
        return "darwin"
    return "any"


def sys_platform() -> str:
    import sys
    return (sys.platform or "").lower()


def host_arch() -> str:
    machine = (_platform.machine() or "").lower()
    if machine in ("amd64", "x86_64"):
        return "x64"
    if machine in ("arm64", "aarch64"):
        return "arm64"
    return machine or "unknown"


def device_identity(agent_dir: str | Path) -> str:
    """Persistent device UUID (§21): a file survives restarts and updates."""
    d = Path(agent_dir)
    d.mkdir(parents=True, exist_ok=True)
    path = d / "device_id"
    value = path.read_text(encoding="utf-8").strip() if path.exists() else ""
    if not value:
        value = uuid.uuid4().hex
        path.write_text(value, encoding="utf-8")
    return value


class LocalQueue:
    """Durable offline event queue (§20): survives restarts, replays safely."""

    def __init__(self, agent_dir: str | Path):
        self.path = Path(agent_dir) / "events_queue.jsonl"
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def append(self, event: dict) -> None:
        with open(self.path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(event, ensure_ascii=False) + "\n")

    def pending(self) -> list[str]:
        if not self.path.exists():
            return []
        try:
            lines = self.path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return []
        return [line for line in lines if line.strip()]

    def ack(self, client_event_id: str) -> None:
        """Remove ONE event (by id) from the durable file."""
        pending = self.pending()
        kept = []
        for line in pending:
            try:
                evt = json.loads(line)
            except json.JSONDecodeError:
                kept.append(line.toString() if hasattr(line, "toString") else line)
                continue
            if evt.get("client_event_id") != client_event_id:
                kept.append(line)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text("\n".join(kept) + ("\n" if kept else ""), encoding="utf-8")
        os.replace(tmp, self.path)

    def clear_all(self) -> None:
        if self.path.exists():
            self.path.unlink()


class TinyAgent:
    """Loop driver; HTTP calls go through ``post`` (injectable for tests)."""

    def __init__(self, *, backend_url: str, agent_dir: str | Path,
                 app_version: str = "0.0.0", agent_version: str = "0.0.0",
                 worker_id: str | None = None, account_id: str | None = None,
                 status: str = "ONLINE", post=None, clock=time.time,
                 auth_token: str | None = None,
                 updater_factory=None, get=None, get_bytes=None,
                 state_loader=None):
        self.backend_url = backend_url.rstrip("/")
        self.agent_dir = Path(agent_dir)
        self.device_id = device_identity(self.agent_dir)
        self.app_version = app_version
        self.agent_version = agent_version
        self.worker_id = worker_id
        self.account_id = account_id
        self.status = status
        self.current_job_id: str | None = None
        # §10: the agent stores an issued SESSION TOKEN, never the user's
        # password. Without one it still heartbeats, but cannot pull updates.
        self.auth_token = auth_token
        # A callable(request) -> DeviceUpdater, built by the installer/entry
        # point with the pinned signed-update key(s). None disables updates.
        self._updater_factory = updater_factory
        self.queue = LocalQueue(self.agent_dir)
        self._post = post or self._http_post
        self._get = get or self._http_get
        self._get_bytes = get_bytes or self._http_get_bytes
        self._clock = clock
        self.last_success: float | None = None
        self.running = False
        # ON/OFF (§extension): the switch lives in a file in the agent dir so
        # the preference survives restarts and is readable without the backend.
        self._state_loader = state_loader or self._default_state_loader
        # Device credential (§extension 3): a short-lived, revocable secret the
        # agent obtains WITHOUT user credentials and stores encrypted at rest.
        self._credential: str | None = None
        self._credential_expires_at: float | None = None
        self.discovery: dict | None = None
        self._load_credential()

    # -- on/off switch ---------------------------------------------------------

    def _default_state_loader(self) -> bool:
        """True when the agent is allowed to do network work."""
        if agent_control is not None:
            return agent_control.is_enabled(self.agent_dir)
        # Standalone copy without the module: read the same file directly.
        path = self.agent_dir / _STATE_FILENAME
        try:
            return bool(json.loads(path.read_text(encoding="utf-8"))["enabled"])
        except (OSError, ValueError, KeyError, json.JSONDecodeError):
            return True  # no/corrupt preference keeps the previous behaviour

    def is_paused(self) -> bool:
        """Re-read the switch on every call: flipping it takes effect at once.

        Paused also covers "this version does not use the Tiny Agent": while the
        product switch is off the agent parks without any network call, so a
        leftover auto-start entry from an older install can never heartbeat,
        poll or flush again (§1).
        """
        if not self.product_available():
            return True
        try:
            return not bool(self._state_loader())
        except Exception:  # noqa: BLE001 — a broken switch must not kill the agent
            return False

    def product_available(self) -> bool:
        """Is the Tiny Agent used by this version? (module absent → assume yes)

        The standalone copy has no access to the application config, so an
        explicitly present copy keeps behaving as before; the shipped entry
        point (``agent/agent.py``) checks the same switch before it starts.
        """
        if agent_control is None:
            return True
        try:
            return bool(agent_control.product_enabled())
        except Exception:  # noqa: BLE001 — an unreadable switch keeps the default
            return False

    # -- device credential (§extension 3/8) ------------------------------------

    def _credential_path(self) -> Path:
        return self.agent_dir / CREDENTIAL_FILENAME

    def _load_credential(self) -> None:
        obj = None
        path = self._credential_path()
        if secure_store is not None:
            obj = secure_store.load(path)
        elif path.exists():          # standalone copy: plain JSON
            try:
                obj = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError, json.JSONDecodeError):
                obj = None
        if isinstance(obj, dict) and obj.get("credential"):
            self._credential = str(obj["credential"])
            self._credential_expires_at = obj.get("expires_at")

    def _store_credential(self, credential: str, expires_at: float | None) -> None:
        self._credential = credential
        self._credential_expires_at = expires_at
        payload = {"credential": credential, "expires_at": expires_at,
                   "device_id": self.device_id, "obtained_at": self._clock()}
        path = self._credential_path()
        if secure_store is not None:
            secure_store.save(path, payload)
        else:
            path.write_text(json.dumps(payload), encoding="utf-8")

    def _credential_is_fresh(self) -> bool:
        if not self._credential:
            return False
        exp = self._credential_expires_at
        if exp is None:
            return True
        return self._clock() < float(exp) - CREDENTIAL_RENEW_MARGIN_SECONDS

    def register_device(self) -> bool:
        """Register this machine and store the returned device credential.

        Carries NO account credentials — the installer ships none (§1).
        Returns True when a usable credential is in hand.
        """
        try:
            data = self._post("/api/distributed/device/register", {
                "device_id": self.device_id,
                "label": self.worker_id,
                "app_version": self.app_version,
                "platform": host_platform(),
                "arch": host_arch(),
            })
        except (urlerror.URLError, OSError, ValueError):
            return False
        credential = (data or {}).get("credential")
        if not credential:
            return False
        self._store_credential(credential, (data or {}).get("expires_at"))
        self.discovery = (data or {}).get("discovery")
        self.last_success = self._clock()
        return True

    def ensure_credential(self) -> bool:
        """Renew the credential when missing or near expiry; else reuse it."""
        if self._credential_is_fresh():
            return True
        return self.register_device()

    def refresh_discovery(self) -> dict | None:
        """Fetch the accounts + device availability this device may see."""
        if not self._credential:
            return None
        try:
            data = self._get("/api/distributed/device/discovery", headers={
                "X-Device-Id": self.device_id,
                "X-Device-Credential": self._credential,
            })
        except (urlerror.URLError, OSError, ValueError):
            return self.discovery     # keep the last known directory
        if isinstance(data, dict):
            self.discovery = data
        return self.discovery

    # -- transport -------------------------------------------------------------

    def _http_post(self, path: str, payload: dict, timeout: int = HEARTBEAT_MAX_ATTEMPT_SECONDS):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urlrequest.Request(
            self.backend_url + path, data=body,
            headers={"Content-Type": "application/json"}, method="POST")
        with urlrequest.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
        return json.loads(raw.decode("utf-8")) if raw else {}

    def _auth_headers(self) -> dict:
        return {"Authorization": f"Bearer {self.auth_token}"} if self.auth_token else {}

    def _http_get(self, path: str, timeout: int = HEARTBEAT_MAX_ATTEMPT_SECONDS,
                  headers: dict | None = None):
        hdrs = self._auth_headers()
        if headers:
            hdrs.update(headers)
        req = urlrequest.Request(self.backend_url + path, headers=hdrs, method="GET")
        with urlrequest.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
        return json.loads(raw.decode("utf-8")) if raw else {}

    def _http_get_bytes(self, path: str, timeout: int = 120):
        req = urlrequest.Request(self.backend_url + path,
                                 headers=self._auth_headers(), method="GET")
        with urlrequest.urlopen(req, timeout=timeout) as resp:
            return resp.read()

    # -- core operations ---------------------------------------------------------

    def heartbeat_payload(self) -> dict:
        # §16: small metadata only — a few hundred bytes.
        payload = {
            "device_id": self.device_id,
            "account_id": self.account_id,
            "worker_id": self.worker_id,
            "app_version": self.app_version,
            "agent_version": self.agent_version,
            "status": self.status,
            "timestamp": self._clock(),
            "current_job_id": self.current_job_id,
            # Platform/arch are recorded so a platform-specific release is only
            # offered to a device that can run it (updates.release_compatible).
            "platform": host_platform(),
            "arch": host_arch(),
        }
        if self.auth_token:
            payload["token"] = self.auth_token
        elif self._credential:
            # No signed-in account: present the device credential so the backend
            # can verify the device identity (still cannot claim an account).
            payload["device_credential"] = self._credential
        return payload

    def send_heartbeat(self) -> bool:
        try:
            self._post("/api/distributed/heartbeat", self.heartbeat_payload())
            self.last_success = self._clock()
            return True
        except (urlerror.URLError, OSError, ValueError):
            return False

    def queue_event(self, type_: str, payload: dict,
                    client_event_id: str | None = None) -> str:
        event_id = client_event_id or f"{type_}-{uuid.uuid4().hex}"
        self.queue.append({
            "client_event_id": event_id, "type": type_, "ts": self._clock(),
            "device_id": self.device_id, "worker_id": self.worker_id,
            "account_id": self.account_id, "payload": payload,
        })
        return event_id

    def flush_queue(self) -> int:
        """Replay pending events; only remove ACKED ones (exactly-once §20)."""
        sent = 0
        for line in self.queue.pending():
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            try:
                self._post("/api/distributed/events", event)
                self.queue.ack(event.get("client_event_id"))
                sent += 1
            except (urlerror.URLError, OSError, ValueError):
                break     # still offline; leave everything queued
        return sent

    def report_error(self, *, error_type: str, message: str,
                     failed_stage: str | None = None, context=None,
                     session_events=None, stack_trace: str | None = None) -> None:
        payload = {
            "device_id": self.device_id, "worker_id": self.worker_id,
            "account_id": self.account_id, "error_type": error_type,
            "message": message, "failed_stage": failed_stage,
            "context": context, "session_events": session_events,
            "stack_trace": stack_trace,
        }
        try:
            self._post("/api/distributed/errors", payload)
        except (urlerror.URLError, OSError, ValueError):
            self.queue_event("error.report", payload,
                             client_event_id=f"err-{uuid.uuid4().hex[:10]}")

    # -- remote updates (§19/§30/§35/§36) ---------------------------------------

    def fetch_updates(self) -> list[dict]:
        """Lightweight update check over the normal authenticated channel.

        No separate polling loop (§17): this rides the same cadence as the
        heartbeat and returns an empty list when offline or unauthenticated.
        """
        if not self.auth_token:
            return []
        try:
            data = self._get("/api/distributed/updates/for-device")
        except (urlerror.URLError, OSError, ValueError):
            return []
        return data.get("updates", []) if isinstance(data, dict) else []

    def download_package(self, update_id: str) -> bytes | None:
        try:
            return self._get_bytes(
                f"/api/distributed/updates/{update_id}/package")
        except (urlerror.URLError, OSError, ValueError):
            return None

    def report_update(self, update_id: str, state: str,
                      detail: str | None = None) -> None:
        try:
            self._post(f"/api/distributed/updates/{update_id}/status",
                       {"state": state, "detail": detail})
        except (urlerror.URLError, OSError, ValueError):
            pass  # the next heartbeat retries the whole check

    def report_update_history(self, *, update_id: str, old_version: str,
                              new_version: str, result: str,
                              failure_reason: str | None = None,
                              rollback_result: str | None = None) -> None:
        try:
            self._post("/api/distributed/updates/history", {
                "update_id": update_id, "old_version": old_version,
                "new_version": new_version, "result": result,
                "failure_reason": failure_reason,
                "rollback_result": rollback_result,
                "completed_at": self._clock()})
        except (urlerror.URLError, OSError, ValueError):
            pass

    def apply_pending_updates(self) -> dict:
        """Download, verify (checksum + Ed25519), apply, and report.

        The heavy machinery lives in :class:`DeviceUpdater`, which enforces
        backup → verify → install → health-check → rollback and the
        never-interrupt-a-correction guard. This method only orchestrates it
        and reports the resulting state. It never raises: an update failure
        must not take the agent down with it.
        """
        out = {"checked": 0, "applied": 0, "results": []}
        if self._updater_factory is None or not self.auth_token:
            return out
        from .device_updater import package_from_bytes
        for request in self.fetch_updates():
            out["checked"] += 1
            update_id = request.get("update_id")
            target = request.get("target_version")
            if not update_id or target == self.app_version:
                continue   # already on this version

            updater = self._updater_factory(request)
            out["applied"] += 1
            data = self.download_package(update_id)
            if data is None:
                self.report_update(update_id, "FAILED", "download failed")
                out["results"].append({"update_id": update_id,
                                       "state": "FAILED"})
                continue

            self.report_update(update_id, "DOWNLOADING")
            ok, why = updater.verify_download(data, request)
            if not ok:
                self.report_update(update_id, "FAILED", why)
                self.report_update_history(
                    update_id=update_id, old_version=self.app_version,
                    new_version=target or "", result="FAILED",
                    failure_reason=why)
                out["results"].append({"update_id": update_id,
                                       "state": "FAILED", "reason": why})
                continue

            self.report_update(update_id, "VERIFYING")
            package = package_from_bytes(request, data)
            result = updater.apply_update(package, update_id=update_id,
                                          device_id=self.device_id)
            state = result.final_state.value
            self.report_update(update_id, state, result.failure_reason)
            self.report_update_history(
                update_id=update_id, old_version=self.app_version,
                new_version=target or "", result=state,
                failure_reason=result.failure_reason,
                rollback_result=result.rollback_result)
            out["results"].append({"update_id": update_id, "state": state})
        return out

    def tick(self) -> dict:
        """One agent cycle: heartbeat, flush queue, then pull any updates.

        While the agent is switched OFF this performs **no network call at all**
        — no heartbeat, no availability check, no event flush, no update poll —
        and leaves every queued record exactly where it was. Pending work is
        therefore preserved for a later switch-on (§extension 2).
        """
        if self.is_paused():
            return {"paused": True, "heartbeat": False, "flushed": 0,
                    "updates": {}, "registered": False, "accounts": None,
                    "pending": len(self.queue.pending()),
                    # Distinguishes "switched off by the user" from "this version
                    # does not run an agent at all", so a diagnostic never
                    # reports a healthy agent that is deliberately unused.
                    "disabled_in_this_version": not self.product_available()}
        out: dict = {}
        # Registration/discovery is routine background work: like the heartbeat
        # it happens only while the agent is ON (§7).
        out["registered"] = self.ensure_credential()
        out["heartbeat"] = self.send_heartbeat()
        if out["heartbeat"]:
            out["flushed"] = self.flush_queue()
            directory = self.refresh_discovery()
            out["accounts"] = len(directory.get("accounts", [])) if directory else None
            out["updates"] = self.apply_pending_updates()
        else:
            out["flushed"] = 0
            out["accounts"] = None
            out["updates"] = {}
        out["paused"] = False
        return out

    def run_forever(self, stop_check=None, sleep=None) -> None:
        """Blocking main loop (the Windows service entry point).

        A paused agent parks here without traffic; it does not exit, so the
        single-instance lock is held and switching back on resumes instantly
        rather than racing to start a second process.
        """
        sleep = sleep or time.sleep
        self.running = True
        while self.running and (stop_check is None or not stop_check()):
            self.tick()
            sleep(HEARTBEAT_INTERVAL)


# -- Windows auto-start helper (§15) ---------------------------------------------

def autostart_file(agent_path: str | Path, target: str | Path,
                   *, allow_disabled: bool = False) -> Path:
    """Generate a silent-look VBS launcher + explain where to register it.

    We deliberately do NOT touch the registry ourselves: the teacher/assistant
    installs the .vbs shortcut into the Startup folder (or a scheduled task)
    via the generated document, so the agent never silently registers itself.

    While the Tiny Agent is unavailable in this version this REFUSES (§1):
    generating an auto-start entry is how the agent would come back after a
    restart, which is exactly what must not happen. ``allow_disabled=True``
    exists for the documented re-enable path.
    """
    if agent_control is not None and not allow_disabled:
        # Raises the standard "not used in this version" refusal.
        agent_control.require_product_enabled()
    agent_path = Path(agent_path)
    target = Path(target)
    vbs = target / "mathcorrect_agent_startup.vbs"
    vbs.write_text(
        'CreateObject("Wscript.Shell").Run """" & WScript.Arguments(0) & """", 0, False\n',
        encoding="utf-8")
    readme = target / "AUTOSTART_INSTRUCTIONS.txt"
    readme.write_text(
        "Math Correct tiny agent auto-start (Windows)\n\n"
        "1. Copy mathcorrect_agent_startup.vbs into:\n"
        "   shell:startup   (press Win+R, type shell:startup, Enter)\n"
        f"2. Create a shortcut in that folder with target:\n"
        f'   wscript.exe "{vbs}" "{agent_path}"\n'
        "3. The agent then starts silently with Windows (no console window).\n"
        "Alternative: create a Scheduled Task with 'At log on' trigger and\n"
        "the same command for a more robust auto-start.\n",
        encoding="utf-8")
    return vbs
