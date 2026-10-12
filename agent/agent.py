"""Math Correct TINY AGENT — standalone entry point.

Usage:  python agent.py [--backend URL] [--interval SECONDS]

Loads device identity + worker id from ``agent.json`` next to this file,
heartbeats the shared backend every 5 minutes, queues events while offline
and replays them on reconnect. Does NOT run AI correction, Classroom sync,
PDF processing, or large transfers.

IMPORTANT — this version does not use the Tiny Agent by default. The switch is
``MC_AGENT_ENABLED`` (environment), ``tiny_agent_enabled`` in ``agent.json``,
or ``[agent] tiny_agent`` in the application's ``config.toml``; with none of
them set the agent does NOTHING: it prints why and exits before any network
call. That is deliberate, so an auto-start entry left behind by an older
install can never bring background traffic back after a restart or an update.
Nothing was deleted — see DISTRIBUTED_DEPLOYMENT.md for the re-enable steps.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit

AGENT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(AGENT_DIR))


def _load_backend_and_app():
    """Import the TinyAgent; works both inside the app tree and standalone."""
    try:
        # Preferred: reuse the app's implementation (one copy of the truth).
        sys.path.insert(0, str(AGENT_DIR.parent / "backend"))
        from app.distributed.tiny_agent import TinyAgent
        return TinyAgent
    except ImportError:
        from tiny_agent import TinyAgent  # agent ships with a copy
        return TinyAgent


def tiny_agent_enabled() -> tuple[bool, str]:
    """Is the Tiny Agent used here? Returns (enabled, where the answer came from).

    Order, most explicit wins: environment → ``agent.json`` → the application's
    ``config.toml`` (when the app is importable) → **disabled by default**.
    """
    raw = (os.environ.get("MC_AGENT_ENABLED") or "").strip().lower()
    if raw in {"1", "true", "yes", "on", "enabled"}:
        return True, "environment"
    if raw in {"0", "false", "no", "off", "disabled"}:
        return False, "environment"

    cfg_path = AGENT_DIR / "agent.json"
    if cfg_path.exists():
        try:
            data = json.loads(cfg_path.read_text(encoding="utf-8"))
            value = (data or {}).get("tiny_agent_enabled")
            if isinstance(value, bool):
                return value, "agent.json"
        except (OSError, ValueError):
            pass

    try:
        sys.path.insert(0, str(AGENT_DIR.parent / "backend"))
        from app.distributed import agent_control
        return bool(agent_control.product_enabled()), "application config"
    except Exception:  # noqa: BLE001 — the standalone copy may have no app
        pass
    return False, "default"


def default_config() -> dict:
    app_version = "unknown"
    try:
        import app.version as _v  # the installed Math Correct, if present
        app_version = _v.APP_VERSION
    except ImportError:
        pass
    return {
        "backend_url": os.environ.get("MC_BACKEND_URL", "http://127.0.0.1:8642"),
        "worker_id": os.environ.get("MC_WORKER_ID") or os.path.basename(str(os.getcwd())),
        "agent_version": "1.0.0",
        "interval_seconds": 300,
        # §10: an issued session TOKEN (never the password). Populated when the
        # Assistant signs in once on the Multi-School page; required to receive
        # updates. Empty means "heartbeat only".
        "auth_token": os.environ.get("MC_AUTH_TOKEN", ""),
        # §19/§25: key_id -> Ed25519 public key PEM, pinned by the Developer.
        # A package is installed only if signed by one of these keys.
        "trusted_update_keys": {},
        "app_dir": str(AGENT_DIR.parent),
        "data_dir": str(AGENT_DIR.parent / "data"),
        "backup_root": str(AGENT_DIR.parent / "update_backups"),
        "current_version": app_version,
    }


def _build_updater(cfg: dict, request: dict):
    """Construct a DeviceUpdater for one update request.

    Uses the pinned keys from agent.json and the app's own liveness as the
    busy/health signals, so an update never interrupts live correction work.
    """
    try:
        from app.distributed.device_updater import DeviceUpdater
    except ImportError:
        from tiny_agent import DeviceUpdater  # shipped copy

    def busy_check():
        # While a correction is running the app records the current job; the
        # updater then parks the update in WAITING_FOR_IDLE.
        job = None
        try:
            import app.db as db
            row = db.query_one(
                "SELECT id FROM correction_jobs WHERE status IN "
                "('QUEUED','DOWNLOADING','CORRECTING','RECHECKING','VERIFYING')"
                " LIMIT 1")
            job = row["id"] if row else None
        except Exception:
            job = None
        return (job is not None, "CORRECTING" if job else "") if job else (False, "")

    def health_check():
        # The app is healthy if its own module imports and its DB opens.
        try:
            import app.db as db
            db.init_db()
            return True, "app database opens"
        except Exception as exc:  # noqa: BLE001
            return False, f"app unavailable: {type(exc).__name__}"

    return DeviceUpdater(
        app_dir=cfg.get("app_dir") or str(AGENT_DIR.parent),
        data_dir=cfg.get("data_dir") or str(AGENT_DIR.parent / "data"),
        backup_root=cfg.get("backup_root") or str(AGENT_DIR.parent / "update_backups"),
        current_version=str(cfg.get("current_version") or "0.0.0"),
        trusted_public_keys=cfg.get("trusted_update_keys") or {},
        busy_check=busy_check,
        health_check=health_check)


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(0x1000, False, pid)  # QUERY_LIMITED
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


def acquire_lock() -> bool:
    """Single-instance guard: only one tiny agent per installation (§1).

    A lock file with the owner PID is taken before anything else. A live owner
    means another agent is already running (the autostart launcher then exits
    cleanly instead of stacking a duplicate heartbeat source); a dead owner is
    stale and is taken over.
    """
    lock = AGENT_DIR / "agent.lock"
    if lock.exists():
        try:
            owner = int(lock.read_text(encoding="utf-8").strip() or 0)
        except (OSError, ValueError):
            owner = 0
        if owner and owner != os.getpid() and _pid_alive(owner):
            return False
    try:
        lock.write_text(str(os.getpid()), encoding="utf-8")
    except OSError:
        # A read-only install location: continue without a lock rather than
        # refusing to run — duplicate agents are a nuisance, not data loss.
        return True
    return True


def release_lock() -> None:
    lock = AGENT_DIR / "agent.lock"
    try:
        owner = int(lock.read_text(encoding="utf-8").strip() or 0)
        if owner == os.getpid():
            lock.unlink()
    except (OSError, ValueError):
        pass


def validate_backend(raw: str) -> str:
    """Normalise + sanity-check the shared backend address.

    A wrong address must fail LOUDLY here. The symptom otherwise is a silent
    "cannot reach the backend" heartbeat failure that looks like a network
    fault; the most common mistake is pasting a UI/web address, so a path is
    rejected rather than quietly ignored.
    """
    url = (raw or "").strip().rstrip("/")
    if not url:
        raise SystemExit(
            "No shared backend configured. Set \"backend_url\" in agent.json "
            "(next to this file), or pass --backend http://<host>:<port>.")
    parsed = urlsplit(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise SystemExit(
            f"backend_url {raw!r} is not a valid http(s) address — expected "
            "something like http://teacher-pc:8642")
    if parsed.path or parsed.query or parsed.fragment:
        raise SystemExit(
            f"backend_url {raw!r} must be just scheme://host:port — remove "
            f"'{parsed.path or parsed.query}'. The agent appends the API path "
            "itself (e.g. http://teacher-pc:8642).")
    return url


def backend_warnings(url: str) -> list[str]:
    """Operational/security notes to print before the agent goes resident."""
    parsed = urlsplit(url)
    host = (parsed.hostname or "").lower()
    notes: list[str] = []
    if host in ("127.0.0.1", "localhost", "::1"):
        notes.append(
            "backend_url points at THIS computer (loopback). That is correct "
            "only while this machine also runs the backend; a second laptop "
            "must use the backend computer's address, e.g. "
            "http://teacher-pc:8642")
    elif parsed.scheme != "https":
        notes.append(
            "backend_url uses plain http over the network, so the agent's "
            "token and heartbeats travel unencrypted. Prefer https (a reverse "
            "proxy in front of the backend) beyond a trusted LAN test")
    return notes


def _banner(enabled: bool, device_id: str, backend: str, interval: int) -> None:
    state = "ENABLED" if enabled else "PAUSED (no network activity)"
    print(f"Math Correct tiny agent starting — device {device_id}, "
          f"backend {backend}, interval {interval}s, state {state}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Math Correct tiny agent")
    parser.add_argument("--backend", default=None)
    parser.add_argument("--interval", type=int, default=None)
    parser.add_argument("--once", action="store_true", help="one tick then exit")
    parser.add_argument("--force", action="store_true",
                        help="run even though this version disables the Tiny "
                             "Agent (only for the documented re-enable path)")
    args = parser.parse_args()

    # THE SWITCH, checked before the lock, before the config and before any
    # network call. A disabled agent must be inert even when Windows starts it
    # again from an old auto-start entry.
    available, source = tiny_agent_enabled()
    if not available and not args.force:
        detail = {
            "agent": "disabled_in_this_version",
            "source": source,
            "network_calls": 0,
            "reenable": "Set MC_AGENT_ENABLED=1 (or [agent] tiny_agent = true "
                        "in config.toml), or run with --force.",
        }
        if args.once:
            print(json.dumps(detail))
        else:
            print("Math Correct tiny agent is disabled in this version "
                  f"({source}) — nothing was sent and no queue was touched.")
            print("  " + detail["reenable"])
        return 0

    # --once is a read-only diagnostic tick: it must work even while the
    # resident agent holds the lock (tests/heartbeat probes rely on it).
    if not args.once and not acquire_lock():
        print("Math Correct tiny agent is already running (agent.lock held) "
              "— not starting a second copy.")
        return 0

    cfg_path = AGENT_DIR / "agent.json"
    cfg = default_config()
    if cfg_path.exists():
        try:
            cfg.update(json.loads(cfg_path.read_text(encoding="utf-8")))
        except json.JSONDecodeError:
            pass
    if args.backend:
        cfg["backend_url"] = args.backend
    if args.interval:
        cfg["interval_seconds"] = args.interval
    cfg["backend_url"] = validate_backend(cfg["backend_url"])
    for note in backend_warnings(cfg["backend_url"]):
        print(f"WARNING: {note}")

    TinyAgent = _load_backend_and_app()
    agent = TinyAgent(
        backend_url=cfg["backend_url"],
        agent_dir=AGENT_DIR,
        app_version=cfg.get("current_version") or cfg.get("app_version", "unknown"),
        agent_version=cfg.get("agent_version", "1.0.0"),
        worker_id=cfg.get("worker_id"),
        auth_token=cfg.get("auth_token") or None,
        updater_factory=lambda request: _build_updater(cfg, request),
    )

    if args.once:
        # A diagnostic tick reports the real switch state instead of silently
        # looking healthy while the agent is paused (§extension 6).
        result = agent.tick()
        print(json.dumps({"device_id": agent.device_id, **result}))
        return 0

    interval = max(30, int(cfg["interval_seconds"]))
    # While paused the agent parks on a short LOCAL file read: no heartbeat, no
    # availability check, no queued transfers. 30s keeps "turn it back on"
    # responsive without any network traffic (§extension 2/3, §9).
    pause_poll = max(5, int(cfg.get("pause_poll_seconds") or 30))

    _banner(not agent.is_paused(), agent.device_id, cfg["backend_url"], interval)
    announced_paused: bool | None = None
    try:
        while True:
            paused = agent.is_paused()
            if paused != announced_paused:
                # Log only on a real transition — no logging loop (§9).
                print("tiny agent paused by user — no background network activity"
                      if paused else "tiny agent resumed — synchronisation active")
                announced_paused = paused
            agent.tick()
            time.sleep(pause_poll if paused else interval)
    except KeyboardInterrupt:
        print("agent stopped")
        return 0
    finally:
        release_lock()


if __name__ == "__main__":
    raise SystemExit(main())
