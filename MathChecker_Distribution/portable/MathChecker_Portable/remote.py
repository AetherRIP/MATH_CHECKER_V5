#!/usr/bin/env python
"""Math Correct — public access launcher. This is THE one mechanism (§7).

Startup sequence (§21), in order, with clear failure at every step:

    1. resolve the port (CLI > config > 8642)
    2. free the port if a previous Math Correct server still owns it (§19/§20)
    3. start Math Correct with MC5_PUBLIC=1 (public mode: auth required)
    4. wait until /api/health answers on the ACTUAL port (readiness, not hope)
    5. start ONE cloudflared quick tunnel against that exact port
    6. detect the real https://....trycloudflare.com address from its output
    7. VERIFY the address end-to-end (health, UI, API) before calling it online
    8. publish data/remote_access.json for the application's Remote Access card
    9. monitor; on any failure write Offline + a reason (§22), never a stale URL

Run:  python remote.py [port]
Stop: Ctrl+C (children are cleaned up, status goes Offline).
"""

from __future__ import annotations

import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
BACKEND = ROOT / "backend"
sys.path.insert(0, str(BACKEND))

# The tunnel primitives themselves live in ONE place:
# ``backend/app/public_access.py``, which the RUNNING application also uses for
# its Public Address switch. The launcher and the app therefore cannot drift
# apart about how a tunnel is created, detected, verified or stopped — and a
# fix in that module fixes both paths. The module-level names below are kept
# (this launcher and its tests use them); only the implementation is shared.
from app import public_access as _pa  # noqa: E402  (after sys.path setup)

URL_RE = _pa.URL_RE
HEALTHY = _pa.HEALTHY
APP_MARKERS = _pa.APP_MARKERS
http = _pa.http


# --------------------------------------------------------------------- utils
# Children we are responsible for, so ANY exit path can stop them (§22).
# The SAME registry the shared module fills, so both sides see every child.
_CHILDREN: list = _pa.CHILDREN


def log(message: str) -> None:
    """Console-safe logging: a Windows code page must never crash the launcher."""
    try:
        print(message, flush=True)
    except UnicodeEncodeError:
        try:
            if hasattr(sys.stdout, "reconfigure"):
                sys.stdout.reconfigure(errors="replace")
            print(message, flush=True)
        except Exception:                          # noqa: BLE001
            pass


def stop_all_children() -> None:
    for child in list(_CHILDREN):
        child.stop()
    _CHILDREN.clear()


def status_write(payload: dict) -> None:
    from app.remote_access import write_status
    write_status(payload)


def http(url: str, timeout: float = 15.0) -> tuple[int, str]:
    """GET a URL, returning (status_code, body_text). Never raises."""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "MathCorrect-RemoteCheck/1"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:          # 4xx/5xx still answered
        try:
            body = exc.read().decode("utf-8", "replace")
        except Exception:                          # noqa: BLE001
            body = ""
        return exc.code, body
    except Exception:                              # noqa: BLE001
        return 0, ""


def local_health(port: int) -> bool:
    code, body = http(f"http://127.0.0.1:{port}/api/health", timeout=5)
    try:
        return code == 200 and json.loads(body).get("status") == "ok"
    except ValueError:
        return False


def port_pids(port: int) -> list[int]:
    """PIDs listening on this exact TCP port (Windows netstat, best effort)."""
    if os.name != "nt":
        return []
    try:
        out = subprocess.run(["netstat", "-ano", "-p", "tcp"],
                             capture_output=True, text=True, timeout=10).stdout
    except Exception:                              # noqa: BLE001
        return []
    pids: list[int] = []
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 5 and parts[3] == "LISTENING":
            local = parts[1]
            if local.endswith(f":{port}"):
                try:
                    pids.append(int(parts[4]))
                except ValueError:
                    pass
    return sorted(set(pids))


def stop_old_server(port: int) -> bool:
    """Stop a previous Math Correct on this port; fail if it is someone else.

    Proves identity with the health endpoint before killing anything — a PID
    that is NOT Math Correct is never touched (§19/§20).
    """
    healthy = http(f"http://127.0.0.1:{port}/api/health", timeout=4)[0] == 200
    pids = port_pids(port)
    if not pids and not healthy:
        return True  # port is free

    if not healthy:
        log(f"  ! Port {port} is held by PID {', '.join(map(str, pids))} but "
            "does not answer the Math Correct health check — refusing to "
            "kill it.")
        return False

    log(f"  Stopping the previous Math Correct server on port {port} "
        f"(PID {', '.join(map(str, pids)) or 'unknown'})...")
    if not pids:
        log("  ! The server answers but no owning PID could be found — "
            "stop it manually.")
        return False
    for pid in pids:
        try:
            if os.name == "nt":
                subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                               capture_output=True, timeout=15)
            else:
                os.kill(pid, signal.SIGTERM)
        except Exception as exc:                   # noqa: BLE001
            log(f"  ! Could not stop PID {pid}: {exc}")
            return False
    deadline = time.time() + 15
    while time.time() < deadline:
        if not port_pids(port) and not local_health(port):
            return True
        time.sleep(0.5)
    return not port_pids(port) and not local_health(port)


def find_cloudflared() -> str | None:
    """Locate cloudflared, preferring the copy shipped with this application.

    The bundled copy (``tools/cloudflared(.exe)``, placed there by the release
    build) wins so an installed teacher gets the version we tested even if an
    older one is on their PATH. A system install and PATH remain as fallbacks
    for a development checkout. Implementation shared with the application
    (``app.public_access.find_cloudflared``); ``ROOT`` here is this checkout.
    """
    return _pa.find_cloudflared(ROOT)


def tunnel_argv(cloudflared: str, port: int) -> list[str]:
    """The ONE tunnel command, always aimed at the ACTUAL running port (§19)."""
    return _pa.tunnel_argv(cloudflared, port)


def fail(reason: str, port: int | None = None) -> int:
    status_write({
        "status": "offline",
        "public_url": None,
        "reason": reason,
        "last_verified": None,
        "port": port,
        "checks": {},
    })
    log("")
    log("Remote Access")
    log("  x Failed")
    log(f"  Reason: {reason}")
    return 1


# ------------------------------------------------------------------ children
class Managed(_pa.Managed):
    """A child process started in THIS launcher's folder.

    The implementation (line-reader thread, bounded buffer, hard stop of the
    whole process tree) is shared with the application's Public Address
    manager; only the working directory differs, so the launcher keeps writing
    its logs next to the checkout it was started from.
    """

    def __init__(self, name: str, argv: list[str], *, env: dict | None = None,
                 log_file: Path | None = None):
        super().__init__(name, argv, env=env, log_file=log_file, cwd=ROOT)


def wait_for_health(port: int, proc: Managed, seconds: int = 90) -> bool:
    """Is the local server answering? (used to wait for THIS launcher's app)"""
    deadline = time.time() + seconds
    while time.time() < deadline:
        if proc.dead:
            return False
        if local_health(port):
            return True
        time.sleep(0.5)
    return local_health(port)


def establish(port: int) -> tuple["Managed | None", "str | None"]:
    """Start one tunnel, detect its address, verify it, publish it.

    Returns ``(tunnel, url)`` only after every external check passes;
    otherwise writes Offline + reason and returns ``(None, None)`` — a
    partially working endpoint is never published as online (§25/§26).
    """
    cloudflared = find_cloudflared()
    if not cloudflared:
        status_write({"status": "offline", "public_url": None,
                      "reason": "cloudflared is not installed.",
                      "last_verified": None, "port": port, "checks": {}})
        log("  x cloudflared is not installed - Public access Offline.")
        return None, None

    log(f"  Tunnel: {cloudflared}")
    tunnel = Managed("tunnel", tunnel_argv(cloudflared, port),
                     log_file=ROOT / "data" / "remote_tunnel.log")
    status_write({"status": "starting", "public_url": None, "port": port,
                  "reason": "Creating the public tunnel…", "checks": {}})

    url = wait_for_tunnel_url(tunnel)
    if not url:
        detail = tunnel.tail()
        tunnel.stop()
        status_write({
            "status": "offline", "public_url": None, "port": port,
            "reason": ("The tunnel could not connect to Math Correct on port "
                       f"{port}." + (f" Last output: {detail}" if detail else "")),
            "last_verified": None, "checks": {},
        })
        log("  x The tunnel could not connect - Public access Offline.")
        return None, None
    log(f"  Tunnel up: {url}")

    ok, checks, reason = verify_public(url)
    if not ok:
        tunnel.stop()
        status_write({
            "status": "offline", "public_url": None, "port": port,
            "reason": ("Public endpoint was created but external verification "
                       f"failed. {reason}"),
            "last_verified": None, "checks": checks,
        })
        log(f"  x Public verification failed - {reason}")
        return None, None

    status_write({
        "status": "online", "public_url": url, "reason": None,
        "last_verified": time.strftime("%Y-%m-%d %H:%M:%S"),
        "verified_at": time.time(), "port": port, "checks": checks,
        "tunnel_pid": tunnel.proc.pid, "cloudflared": cloudflared,
    })
    return tunnel, url


def wait_for_tunnel_url(proc: Managed, seconds: int = 60) -> str | None:
    """Read the real address out of the tunnel's own output."""
    return _pa.wait_for_tunnel_url(proc, seconds=seconds)


#: Heartbeat cadence for the public endpoint, and how many heartbeats pass
#: between full four-check verifications (PHASE 1A: stay internet-cheap).
HEARTBEAT_SECONDS = 60
FULL_VERIFY_EVERY = 10


def verify_alive(base: str, timeout: int = 20) -> tuple[bool, str]:
    """Cheap liveness beat: ONE request for the smallest endpoint we have.

    `/api/health` answers 15 bytes and touches no student data, no Classroom
    sync, no Drive, no AI and no report work. That is the whole idle cost of
    knowing an idle tunnel is still there. ``http`` is passed explicitly so
    this launcher's own reference stays patchable/testable.
    """
    return _pa.verify_alive(base, timeout=timeout, http_fn=http)


def verify_public(base: str, timeout: int = 90) -> tuple[bool, dict, str]:
    """Prove the public endpoint actually serves Math Correct (§25).

    Every check travels through the public hostname -> Cloudflare -> tunnel ->
    this machine. The API check also proves the auth gate is answering.
    """
    return _pa.verify_public(base, timeout=timeout, http_fn=http)


# --------------------------------------------------------------------- main
def main() -> int:
    """Any failure — including an unexpected crash — must stop our children
    and leave a truthful Offline status, never orphans or a stale URL (§22)."""
    try:
        return _run()
    except KeyboardInterrupt:
        log("")
        log("Stopping remote access…")
        stop_all_children()
        status_write({"status": "offline", "public_url": None,
                      "reason": "Remote access stopped.",
                      "last_verified": None, "checks": {}})
        log("Remote Access: [FAIL] Offline (stopped).")
        return 0
    except Exception as exc:                      # noqa: BLE001
        stop_all_children()
        status_write({"status": "offline", "public_url": None,
                      "reason": f"Launcher error: {exc}",
                      "last_verified": None, "checks": {}})
        log(f"[FAIL] Launcher error: {exc}")
        return 1


def _run() -> int:
    port = 8642
    args = [a for a in sys.argv[1:] if a != "--"]
    if args and args[0].isdigit():
        port = int(args[0])
    else:
        try:
            from app.config import get_config
            port = int(get_config().network.port)
        except Exception:                          # noqa: BLE001
            pass

    log("=" * 56)
    log("Math Correct — Remote Access")
    log("=" * 56)

    # 1) the port must be ours (or free)
    if not stop_old_server(port):
        return fail(f"Port {port} is used by another program. Stop it, then "
                    "run remote.py again.", port)
    if port_pids(port):
        return fail(f"Port {port} is still occupied after cleanup.", port)

    # 2) the server, in public mode
    env = dict(os.environ)
    env["MC5_PUBLIC"] = "1"
    server = Managed("server",
                     [sys.executable, str(ROOT / "main.py"), str(port)],
                     env=env, log_file=ROOT / "data" / "remote_server.log")
    status_write({"status": "starting", "public_url": None, "port": port,
                  "reason": "Starting the Math Correct server...", "checks": {}})
    log(f"  Server starting on 0.0.0.0:{port} (public mode)...")
    if not wait_for_health(port, server):
        detail = server.tail()
        server.stop()
        return fail(f"The application server never became ready on port {port}"
                    + (f" — last output: {detail}" if detail else "."), port)
    log("  Server ready (/api/health OK).")

    # account check (fail closed means 503 until an account exists locally)
    _, body = http(f"http://127.0.0.1:{port}/api/auth/status", timeout=5)
    try:
        auth = json.loads(body)
        if auth.get("public_mode") and not auth.get("configured"):
            log("  ! No sign-in account yet: open http://127.0.0.1:%d and "
                "create one. Remote users cannot pass the API until then." % port)
    except ValueError:
        pass

    # 3) the tunnel, against the ACTUAL port (start happens in establish())
    cloudflared = find_cloudflared()
    if not cloudflared:
        server.stop()
        return fail("cloudflared is not installed (expected in Program Files "
                    "or on PATH). Install Cloudflare Tunnel, then retry.", port)

    # 4) VERIFY before publishing (§25, §26)
    tunnel, url = establish(port)
    if tunnel is None:
        server.stop()
        return 1  # establish() already wrote Offline + reason

    lan_ip = None
    try:
        from app.remote_access import _lan_ip
        lan_ip = _lan_ip()
    except Exception:                              # noqa: BLE001
        pass
    log("")
    log("Remote Access")
    log("  Local  [ok] Online   http://127.0.0.1:%d" % port)
    if lan_ip:
        log("  LAN    [ok] Reachable  http://%s:%d" % (lan_ip, port))
    log("  Public [ok] Online   %s" % url)
    log("  Last verified %s" % time.strftime("%Y-%m-%d %H:%M:%S"))
    log("=" * 56)
    log("Press Ctrl+C to stop remote access.")

    # 5) monitor: keep the truth in the status file (§22, §23).
    #    A dead tunnel does NOT take the local app down (§30): the status
    #    goes Offline with a reason, and the tunnel is re-established
    #    (a new quick tunnel means a new address — the file always has the
    #    current one, never a stale URL).
    verify_failures = 0
    server_restarts = 0
    beats_since_full = 0
    full_due = False
    checks: dict = {}
    next_verify = time.time() + HEARTBEAT_SECONDS
    last_reason = "Tunnel is not running."
    try:
        while True:
            time.sleep(5)

            # --- server health (restart at most twice, then fail clearly)
            if server.dead or not local_health(port):
                server_restarts += 1
                if server_restarts > 2:
                    status_write({"status": "offline", "public_url": None,
                                  "reason": "The application server stopped and "
                                            "could not be restarted.",
                                  "last_verified": None, "port": port,
                                  "checks": {}})
                    log("  x The application server stopped and could not be "
                        "restarted. Remote access is Offline.")
                    break
                log("  ! Application server stopped — restarting…")
                server = Managed("server",
                                 [sys.executable, str(ROOT / "main.py"), str(port)],
                                 env=env,
                                 log_file=ROOT / "data" / "remote_server.log")
                if not wait_for_health(port, server):
                    log("  ! Restart failed; will retry.")
                    continue
                server_restarts = 0
                log("  Server restarted.")

            # --- tunnel dead -> Offline NOW (no stale URL), then re-establish
            if tunnel is None or tunnel.dead:
                if tunnel is not None and url:
                    log("  ! Tunnel process exited — Public access Offline.")
                    last_reason = "The tunnel process exited. Reconnecting…"
                if tunnel is not None:
                    tunnel.stop()
                    tunnel = None
                status_write({"status": "offline", "public_url": None,
                              "reason": last_reason, "last_verified": None,
                              "port": port, "checks": {}})
                time.sleep(15)  # brief pause before grabbing a new address
                tunnel, url = establish(port)
                verify_failures = 0
                beats_since_full = 0
                full_due = False
                next_verify = time.time() + HEARTBEAT_SECONDS
                if tunnel is None:
                    time.sleep(45)
                continue

            # --- periodic external re-verification (§25, PHASE 1A) -------
            # The heartbeat is ONE tiny request. The four-check `verify_public`
            # (which also pulls the UI page through the tunnel) runs only when
            # the address is first established, every FULL_VERIFY_EVERY
            # heartbeats, or right after a failure - so an idle server costs a
            # few hundred bytes a minute instead of a page fetch.
            if time.time() >= next_verify:
                next_verify = time.time() + HEARTBEAT_SECONDS
                beats_since_full += 1
                if full_due or beats_since_full >= FULL_VERIFY_EVERY:
                    fresh_ok, fresh_checks, fresh_reason = verify_public(url,
                                                                        timeout=30)
                    beats_since_full = 0
                    full_due = False
                else:
                    alive, fresh_reason = verify_alive(url, timeout=20)
                    fresh_ok = alive
                    fresh_checks = dict(checks)  # keep the last full picture
                if fresh_ok:
                    verify_failures = 0
                    checks = fresh_checks
                    status_write({
                        "status": "online", "public_url": url, "reason": None,
                        "last_verified": time.strftime("%Y-%m-%d %H:%M:%S"),
                        "verified_at": time.time(), "port": port,
                        "checks": fresh_checks,
                        "tunnel_pid": tunnel.proc.pid,
                        "server_pid": server.proc.pid,
                        "cloudflared": cloudflared,
                    })
                else:
                    verify_failures += 1
                    last_reason = (f"External health check failed. {fresh_reason}")
                    log(f"  ! Public verification failed "
                        f"({verify_failures}/3): {fresh_reason}")
                    # A failed heartbeat gets the full treatment next round, so
                    # a half-broken tunnel is described precisely, not vaguely.
                    full_due = True
                    if verify_failures >= 3:
                        status_write({"status": "offline", "public_url": None,
                                      "reason": last_reason,
                                      "last_verified": None, "port": port,
                                      "checks": fresh_checks})
                        tunnel.stop()
                        tunnel = None
                        url = None
    except KeyboardInterrupt:
        log("")
        log("Stopping remote access…")
    finally:
        if tunnel is not None:
            tunnel.stop()
        server.stop()
        status_write({"status": "offline", "public_url": None, "port": port,
                      "reason": "Remote access stopped.", "last_verified": None,
                      "checks": {}})
        log("Remote Access: x Offline (stopped).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
