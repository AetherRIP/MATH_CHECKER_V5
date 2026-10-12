"""Remote/public access state — one authoritative place (spec §9-§10, §23).

The launcher (``remote.py``) owns the tunnel lifecycle; the application only
needs to answer three questions:

  1. What host should the server bind?        :func:`resolve_host`
  2. What is the current public address?      :func:`public_url`
  3. Is remote access online right now?       :func:`read_status`

The launcher writes a small status file (``data/remote_access.json``) after it
has *verified* the public endpoint. The app never invents a URL: if the file
says nothing (or says offline), the app reports offline — a stale address is
never presented as active (spec §10, §22).

A verification is only trusted while it is RECENT. The launcher re-verifies
every 60 seconds and re-stamps the file each round, so if the launcher itself
is killed - leaving nobody to write "offline" - the recorded address expires
within a few minutes instead of being served as if the tunnel still worked.

Precedence for the public URL: the ``MC5_PUBLIC_URL`` environment variable
(an explicit operator override for a tunnel managed outside this app) > the
recently verified status file > nothing.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

#: Status file written by remote.py after external verification.
STATUS_FILENAME = "remote_access.json"

VALID_STATUSES = ("online", "starting", "offline")

#: How long a recorded verification stays trustworthy. The launcher re-verifies
#: every 60s, so three missed rounds mean the session is gone (spec §10, §22).
STATUS_TTL_SECONDS = 180


def status_path() -> Path:
    """Resolved on every call so MC5_DATA_DIR overrides stay correct."""
    from .config import DATA_DIR
    return Path(DATA_DIR) / STATUS_FILENAME


def write_status(payload: dict) -> Path:
    """Atomically persist the remote-access status (never raises into callers)."""
    path = status_path()
    tmp = path.with_suffix(".json.tmp")
    data = dict(payload)
    data.setdefault("updated_at", time.time())
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        tmp.replace(path)
    except OSError:
        # A status file problem must never take the app down; the launcher
        # still prints the truth on the console.
        pass
    return path


def read_status() -> dict:
    """The persisted status, or an explicit offline record when absent."""
    path = status_path()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            return raw
    except (OSError, ValueError):
        pass
    return {
        "status": "offline",
        "public_url": None,
        "reason": "No public-access session has been started.",
        "last_verified": None,
        "port": None,
    }


def status_is_fresh(state: dict | None = None,
                   ttl: int = STATUS_TTL_SECONDS) -> bool:
    """True when the recorded "online" verification is recent enough.

    This is what stops a killed launcher from leaving an address on screen
    that no longer resolves to anything (spec §10, §22).
    """
    state = read_status() if state is None else state
    if state.get("status") != "online":
        return False
    try:
        stamp = float(state.get("verified_at") or state.get("updated_at") or 0)
    except (TypeError, ValueError):
        return False
    return bool(stamp) and (time.time() - stamp) <= ttl


def _address_is_requested() -> bool:
    """Is publishing a public address switched ON for this installation?

    ``MC5_PUBLIC=1`` means the launcher started this process to be public, so
    it wins. Otherwise the Public Address switch (Settings) decides. This is
    what makes switching public access OFF stop the address from being
    advertised anywhere in the app — discovery, the sign-in screen and the
    Remote Access card all read :func:`public_url`.
    """
    if os.environ.get("MC5_PUBLIC") == "1":
        return True
    try:
        from .auth_gate import public_mode_enabled
        return bool(public_mode_enabled())
    except Exception:  # noqa: BLE001 — unreadable config means "do not publish"
        return False


def public_url() -> str | None:
    """The CURRENT public address, or None when not verifiably online.

    Never returns a remembered URL from a dead tunnel: an explicit operator
    override wins, the switch must be ON, and the status file only counts while
    it says online AND the verification behind it is fresh.
    """
    env = (os.environ.get("MC5_PUBLIC_URL") or "").strip()
    if env:
        return env
    if not _address_is_requested():
        return None
    state = read_status()
    if status_is_fresh(state) and state.get("public_url"):
        return str(state["public_url"])
    return None


def resolve_host(access_mode: str | None = None,
                 env: dict | None = None) -> str:
    """Bind address for this process (spec §8).

    * public mode (``MC5_PUBLIC=1``): 0.0.0.0 — the tunnel and LAN tests must
      both reach the server;
    * ``access_mode == "lan"``: 0.0.0.0 — the teacher opted in via Settings;
    * otherwise: 127.0.0.1 — local only, the safe default.
    """
    environ = os.environ if env is None else env
    if environ.get("MC5_PUBLIC") == "1":
        return "0.0.0.0"
    if access_mode is None:
        from .config import get_config
        try:
            access_mode = get_config().network.access_mode
        except Exception:  # noqa: BLE001 — a config failure stays local-only
            access_mode = "local"
    return "0.0.0.0" if access_mode == "lan" else "127.0.0.1"


def probe_health(url: str, timeout: float = 1.2) -> bool:
    """GET ``<url>/api/health`` once and report whether Math Correct answers.

    This is the smallest endpoint the app has: no student data, no Classroom
    sync, no Drive, no AI, no report work. It is used both to prove an address
    really serves the app and to keep the Remote Access panel honest - a row
    only reads "Reachable" when this returns True.

    The URLs passed here are loopback/LAN, so the probe never leaves the
    machine and costs no internet bandwidth.
    """
    import json
    import urllib.request

    # A loopback/LAN probe must NEVER be sent to a system proxy: on Windows a
    # corporate proxy would answer for 127.0.0.1 and make a dead server look
    # alive (or a live one look dead). Bypass proxies explicitly.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(f"{url}/api/health",  # noqa: S310 - fixed http
                         timeout=timeout) as response:
            if int(getattr(response, "status", 200)) != 200:
                return False
            body = json.loads(response.read(200).decode("utf-8", "replace"))
            return isinstance(body, dict) and body.get("status") == "ok"
    except Exception:  # noqa: BLE001 - "unreachable" is a valid answer
        return False


def summary(*, expose_lan: bool = True) -> dict:
    """What the UI needs to render the Remote Access panel (spec §23).

    Reachability is MEASURED, never assumed: the local and LAN rows come from
    an actual ``/api/health`` request, and the public row only reads online
    when the launcher recorded a successful end-to-end verification. A tunnel
    process that is merely running never counts as success (spec §10, §22).

    ``expose_lan=False`` withholds the internal LAN address from callers that
    are not on this machine, so the public address never advertises the
    private network.
    """
    from .auth_gate import public_mode_enabled
    state = read_status()
    url = public_url()
    # The LOCAL address must describe THIS running server, so the port this
    # process was actually started with wins over anything recorded earlier
    # (a second copy of the app, or a launcher session on another port, would
    # otherwise make the panel advertise an address that is not serving).
    port = 0
    env_port = (os.environ.get("MC5_PORT") or "").strip()
    if env_port.isdigit():
        port = int(env_port)
    if not port:
        try:
            port = int(state.get("port") or 0)
        except (TypeError, ValueError):
            port = 0
    if not port:
        try:
            from .config import get_config
            port = int(get_config().network.port)
        except Exception:  # noqa: BLE001
            port = 8642

    online = bool(url and status_is_fresh(state))
    checks = state.get("checks") or {}
    # The Public Address switch (Settings) is reported separately from the
    # state it produced, so the UI can show OFF / STARTING / ONLINE / FAILED
    # without ever inventing an address (see app.public_access).
    switch = {}
    try:
        from .public_access import status as public_access_status
        switch = public_access_status()
    except Exception:  # noqa: BLE001 — the panel still renders without it
        switch = {}
    local_url = f"http://127.0.0.1:{port}"
    lan_ip = _lan_ip()
    lan_enabled = resolve_host() == "0.0.0.0"
    lan_url = f"http://{lan_ip}:{port}" if (lan_ip and lan_enabled) else None

    local_up = probe_health(local_url)
    lan_up = bool(lan_url) and local_up and probe_health(lan_url)

    # A file that says "online" with a stale verification is a dead session:
    # report it as offline and say why, rather than showing a dead address.
    stale = state.get("status") == "online" and not online
    reason = str(state.get("reason") or "Public tunnel is not running.")
    if stale:
        reason = ("The last public verification is stale, so remote access "
                  "is not running. Switch the Public Address on in Settings "
                  "(or start the app with start_public.bat).")

    return {
        "public_mode": bool(public_mode_enabled()),
        "public_access": switch,
        "status": ("online" if online
                   else ("starting" if state.get("status") == "starting"
                         and not stale else "offline")),
        "public_url": url,
        "reason": None if online else reason,
        "last_verified": state.get("last_verified"),
        "verified_checks": checks,
        "port": port,
        "local_url": local_url,
        "lan_url": lan_url if expose_lan else None,
        "lan_enabled": lan_enabled,
        "updated_at": state.get("updated_at"),
        # ---- measured reachability (spec §25: no fake success) ----------
        "reachable": {
            "local": bool(local_up),
            "lan": bool(lan_up),
            "public": online,
            "api": bool(local_up and (checks.get("api_gated") is not False)),
            "websocket": "not_required",
            "events": "polling" if online else "sse",
        },
    }


def _lan_ip() -> str | None:
    """Best-effort LAN address of this machine (no external traffic)."""
    import socket
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.connect(("10.255.255.255", 1))
            return sock.getsockname()[0]
        finally:
            sock.close()
    except OSError:
        return None
