#!/usr/bin/env python3
"""LIVE verification of the two access switches — REAL tunnel, REAL HTTP.

Nothing in this script is mocked:

* it starts the real application (``main.py``) on an isolated data directory,
* the Public Address switch starts and stops a REAL ``cloudflared`` tunnel,
* every claim is checked with an HTTP request made by a separate client
  process — first from the laptop itself, then THROUGH the public hostname
  (Cloudflare edge -> tunnel -> this machine), then again after the switch is
  turned off.

It answers the questions the change has to be judged on:

1. Is multi-account mode OFF by default, and are its OPERATIONS refused by the
   backend (not merely hidden), even for a signed-in Developer?
2. Is the multi-account data preserved (an account and a team created before
   the switch are still there after it is turned back on)?
3. Does the Public Address switch actually start/stop the endpoint, and does
   the address it displays really serve the app (and refuse unauthenticated
   callers)?
4. Does an AUTHORISED remote client work through that address?
5. Is a failed tunnel reported as failed (never as "active")?
6. Do both settings survive a restart, and does local access plus the backend
   connection keep working while the public address is off?

Usage:  python backend/scripts/live_verify_access_switches.py
Exit code 0 = every check passed. Results are also written to
``docs/results/LIVE_ACCESS_SWITCHES_RESULTS.json``.
"""
from __future__ import annotations

import http.cookiejar
import json
import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]           # .../MATH V5
BACKEND = ROOT / "backend"
WORK = ROOT / ".live_verify"
DATA = WORK / "data"
SERVER_LOG = WORK / "server.log"
RESULTS_PATH = ROOT / "docs" / "results" / "LIVE_ACCESS_SWITCHES_RESULTS.json"

LOGIN = {"username": "owner", "password": "live-verify-passphrase-1"}
RESULTS: list[dict] = []


# --------------------------------------------------------------------- helpers

def free_port() -> int:
    """A port nothing is listening on, so the test never fights a running app."""
    requested = os.environ.get("MC5_LIVE_PORT")
    if requested and requested.isdigit():
        return int(requested)
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


PORT = free_port()


def record(name: str, ok: bool, detail: str = "") -> bool:
    RESULTS.append({"check": name, "status": "PASS" if ok else "FAIL",
                    "detail": detail})
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))
    return ok


def info(name: str, detail: str = "") -> None:
    RESULTS.append({"check": name, "status": "INFO", "detail": detail})
    print(f"  [INFO] {name}" + (f" — {detail}" if detail else ""))


class Client:
    """One HTTP client with its own cookie jar — i.e. its own 'device'."""

    def __init__(self, base: str = f"http://127.0.0.1:{PORT}"):
        self.base = base.rstrip("/")
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}),      # never via a local proxy
            urllib.request.HTTPCookieProcessor(self.jar))
        self.csrf: str | None = None

    def call(self, method: str, path: str, payload=None, timeout: float = 40,
             token: str | None = None) -> tuple[int, dict]:
        url = path if path.startswith("http") else f"{self.base}{path}"
        data = json.dumps(payload).encode() if payload is not None else None
        request = urllib.request.Request(url, data=data, method=method)
        request.add_header("User-Agent", "MathChecker-LiveVerify/1")
        if data is not None:
            request.add_header("Content-Type", "application/json")
        if self.csrf:
            request.add_header("X-MC5-CSRF", self.csrf)
        if token:
            request.add_header("Authorization", f"Bearer {token}")
        try:
            with self.opener.open(request, timeout=timeout) as response:
                body = response.read().decode("utf-8", "replace")
                return response.status, _json(body)
        except urllib.error.HTTPError as exc:
            return exc.code, _json(exc.read().decode("utf-8", "replace"))
        except Exception as exc:                     # unreachable is an answer
            return 0, {"error": str(exc)}

    def get(self, path: str, **kw) -> tuple[int, dict]:
        return self.call("GET", path, **kw)

    def post(self, path: str, payload=None, **kw) -> tuple[int, dict]:
        return self.call("POST", path, payload if payload is not None else {}, **kw)


def _json(text: str) -> dict:
    try:
        body = json.loads(text or "{}")
    except ValueError:
        return {"raw": text[:400]}
    return body if isinstance(body, dict) else {"raw": body}


def start_server() -> subprocess.Popen:
    env = dict(os.environ)
    env["MC5_DATA_DIR"] = str(DATA)
    env["MC5_NO_BROWSER"] = "1"
    env["MC5_LOGIN_REQUIRED"] = "1"          # the shipped default, made explicit
    env.pop("MC5_PUBLIC", None)              # the app owns the tunnel, not a launcher
    env.pop("MULTI_ACCOUNT_ENABLED", None)   # the value under test is the default
    env.pop("MC5_PUBLIC_URL", None)
    log = open(SERVER_LOG, "a", encoding="utf-8")
    creation = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    proc = subprocess.Popen([sys.executable, "main.py", str(PORT)], cwd=str(ROOT),
                            env=env, stdout=log, stderr=subprocess.STDOUT,
                            creationflags=creation)
    return proc


def wait_for_health(client: Client, seconds: float = 90) -> bool:
    deadline = time.time() + seconds
    while time.time() < deadline:
        code, body = client.get("/api/health", timeout=5)
        if code == 200 and body.get("status") == "ok":
            return True
        time.sleep(0.5)
    return False


def stop_server(proc: subprocess.Popen | None) -> None:
    if proc is None or proc.poll() is not None:
        return
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(proc.pid), "/F"],
                           capture_output=True, timeout=20)
        else:
            proc.terminate()
            proc.wait(timeout=10)
    except Exception:  # noqa: BLE001
        pass


def tunnel_pid_for_port(port: int) -> int | None:
    """The cloudflared pid pointing at OUR port (never somebody else's)."""
    if os.name != "nt":
        return None
    command = (
        "Get-CimInstance Win32_Process -Filter \"Name='cloudflared.exe'\" | "
        "Where-Object { $_.CommandLine -like '*%d*' } | "
        "Select-Object -First 1 -ExpandProperty ProcessId" % port)
    try:
        out = subprocess.run(["powershell", "-NoProfile", "-Command", command],
                             capture_output=True, text=True, timeout=40)
        text = (out.stdout or "").strip().splitlines()
        return int(text[0]) if text and text[0].isdigit() else None
    except Exception:  # noqa: BLE001
        return None


def kill_tunnel_for_port(port: int) -> bool:
    pid = tunnel_pid_for_port(port)
    if pid is None:
        return False
    try:
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                       capture_output=True, timeout=20)
        return True
    except Exception:  # noqa: BLE001
        return False


def read_status_file() -> dict:
    path = DATA / "remote_access.json"
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}


# ------------------------------------------------------------------ the checks

def main() -> int:  # noqa: C901 — a linear checklist reads better here
    print("LIVE verification: multi-account mode + Public Address switch")
    print(f"  port {PORT} · data dir {DATA}\n")

    if DATA.exists():
        shutil.rmtree(DATA, ignore_errors=True)
    WORK.mkdir(parents=True, exist_ok=True)
    server = None
    try:
        # ---------------------------------------------------------------- boot
        print("Starting the real application…")
        server = start_server()
        local = Client()
        if not record("application starts on an isolated data directory",
                      wait_for_health(local),
                      f"http://127.0.0.1:{PORT}"):
            print(Path(SERVER_LOG).read_text(encoding="utf-8", errors="replace")[-2500:])
            return 1

        code, body = local.post("/api/auth/setup", LOGIN)
        if not record("a local account can be created (first-run setup)", code == 200,
                      f"HTTP {code}"):
            return 1
        code, _ = local.post("/api/auth/login", LOGIN)
        code, status = local.get("/api/auth/status")
        local.csrf = status.get("csrf")
        record("the local owner signs in (session + CSRF)", code == 200
               and status.get("authenticated") is True, f"HTTP {code}")

        # ------------------------------------------- 1. multi-account default
        print("\n1. Multi-account mode — default state and enforcement")
        code, state = local.get("/api/access/state")
        caps = state.get("multi_account") or {}
        record("default is OFF", caps.get("multi_account") is False,
               f"multi_account={caps.get('multi_account')} source={caps.get('source')}")
        record("no restart is required to change it",
               caps.get("restart_required") is False)
        record("the setting is a documented config key",
               caps.get("setting") == "[accounts] multi_account"
               and caps.get("env_var") == "MULTI_ACCOUNT_ENABLED",
               f"{caps.get('setting')} / {caps.get('env_var')}")

        # The distributed layer's own account: created here so the preservation
        # check below has real rows to preserve.
        bootstrap = DATA / "distributed_bootstrap.txt"
        dev = json.loads(bootstrap.read_text(encoding="utf-8"))["developer"] \
            if bootstrap.exists() else None
        if dev is None:
            info("bootstrap credentials not found — skipping the token-based checks")
        token = None
        if dev is not None:
            code, login = local.post("/api/distributed/login", {
                "username": dev["username"], "password": dev["password"],
                "device_id": "live-verify-device"})
            token = login.get("token")
            record("a Developer of the distributed layer can sign in", code == 200
                   and bool(token), f"HTTP {code} role={login.get('role')}")

        # Create one account + one team WITH multi-account still off? No: create
        # them now through the backend itself, but only after switching the
        # layer ON, so the OFF state is proven to refuse them below.
        code, body = local.post("/api/access/multi-account", {"enabled": True})
        record("multi-account mode can be switched ON", code == 200
               and body.get("multi_account", {}).get("multi_account") is True,
               f"HTTP {code}")
        created_account = created_team = None
        if token:
            code, acct = local.post("/api/distributed/accounts", {
                "username": "assistant-one", "display_name": "Assistant One",
                "password": "assistant-pass-1", "role": "ASSISTANT"}, token=token)
            created_account = acct.get("account_id")
            record("an account can be created while the layer is ON",
                   code == 200 and bool(created_account), f"HTTP {code}")
            # Teams belong to a school, so one is created first (schools are
            # part of the correction workflow and stay available either way).
            code, school = local.post("/api/distributed/schools",
                                      {"name": "Live Verify School"}, token=token)
            school_id = school.get("id")
            record("a school can be created", code == 200 and bool(school_id),
                   f"HTTP {code}")
            code, team = local.post("/api/distributed/teams",
                                    {"name": "Live Verify Team",
                                     "school_id": school_id}, token=token)
            created_team = team.get("id")
            record("a team can be created while the layer is ON",
                   code == 200 and bool(created_team), f"HTTP {code} {team}")

        # ------------------------------------------------- 2. switch it OFF
        print("\n2. Multi-account mode OFF — refused by the backend, data kept")
        code, body = local.post("/api/access/multi-account", {"enabled": False})
        record("the switch can be turned OFF", code == 200
               and body.get("multi_account", {}).get("multi_account") is False,
               f"HTTP {code}")

        refusals = []
        if token:
            for method, path, payload in (
                ("GET", "/api/distributed/accounts", None),
                ("POST", "/api/distributed/accounts", {"username": "nope"}),
                ("POST", "/api/distributed/accounts/x/roles", {"roles": ["DEVELOPER"]}),
                ("GET", "/api/distributed/teams", None),
                ("POST", "/api/distributed/teams", {"name": "should not exist"}),
                ("GET", "/api/distributed/devices", None),
                ("GET", "/api/distributed/device/discovery", None),
            ):
                code, body = local.call(method, path, payload, token=token)
                ok = code == 403 and body.get("code") == "multi_account_disabled"
                refusals.append((method, path, code, body.get("code")))
                record(f"{method} {path} is refused (403 multi_account_disabled)",
                       ok, f"HTTP {code} {body.get('code')}")

        # Preserved single-account functionality (must NOT be switched off).
        code, ping = local.get("/api/distributed/ping")
        record("the backend connection still answers (/api/distributed/ping)",
               code == 200 and ping.get("ok") is True, f"HTTP {code}")
        code, hb = local.post("/api/distributed/heartbeat", {
            "device_id": "live-verify-device", "worker_id": "live-verify-worker",
            "status": "ONLINE", "app_version": "5.0.0"})
        record("device registration/heartbeats still work",
               code == 200 and hb.get("code") != "multi_account_disabled",
               f"HTTP {code}")
        code, settings = local.get("/api/settings")
        boundaries = [g for g, _ in (settings.get("grades", {}).get("boundaries") or [])]
        record("settings still load and the 1–9 grade scale is intact",
               code == 200 and boundaries[:9] == ["1", "2", "3", "4", "5", "6", "7", "8", "9"],
               f"HTTP {code} grades={boundaries[:9]}")
        code, dash = local.get("/api/dashboard")
        record("core data still serves (dashboard)", code == 200, f"HTTP {code}")

        # ------------------------------------- 3. turn multi-account back ON
        print("\n3. Multi-account mode ON again — the data is still there")
        code, body = local.post("/api/access/multi-account", {"enabled": True})
        record("the switch can be turned ON again", code == 200)
        if token:
            code, listing = local.get("/api/distributed/accounts", token=token)
            usernames = [a.get("username") for a in (listing.get("accounts") or [])]
            record("the account created before the switch is still there",
                   code == 200 and "assistant-one" in usernames,
                   f"HTTP {code} accounts={usernames}")
            code, teams = local.get("/api/distributed/teams", token=token)
            names = [t.get("name") for t in (teams.get("teams") or teams.get("items") or [])] \
                if isinstance(teams, dict) else []
            record("the team created before the switch is still there",
                   code == 200 and "Live Verify Team" in names, f"HTTP {code} teams={names}")
        # With the layer ON, an unauthenticated caller gets the ordinary 401 —
        # i.e. the 403 seen earlier is the refusal replacing it, never a bypass.
        if token:
            stranger = Client()
            code, _ = stranger.get("/api/distributed/accounts", timeout=20)
            record("with the layer ON an unauthenticated caller gets 401",
                   code == 401, f"HTTP {code}")

        # Return to the shipped single-account default for the rest of the run.
        local.post("/api/access/multi-account", {"enabled": False})

        # ------------------------------------------------- 4. public address
        print("\n4. Public Address — OFF means off, ON means a real, verified endpoint")
        code, state = local.get("/api/access/state")
        pa = state.get("public_address") or {}
        record("the switch starts OFF and nothing is exposed",
               pa.get("requested") is False and pa.get("status") == "disabled"
               and pa.get("public_url") is None, f"status={pa.get('status')}")
        record("cloudflared is available and the switch can be used",
               pa.get("can_enable") is True, str(pa.get("blocked_reason") or ""))
        code, _ = local.get("/api/dashboard")
        record("local access works while it is OFF", code == 200, f"HTTP {code}")
        code, _ = local.get("/api/distributed/ping")
        record("the separately configured backend connection works while it is OFF",
               code == 200, f"HTTP {code}")

        print("  … turning the Public Address ON (this creates a real tunnel)")
        # This call waits for the tunnel to come up AND for the end-to-end
        # verification, so it needs a longer client timeout than a read.
        code, body = local.post("/api/access/public-address", {"enabled": True},
                                timeout=300)
        pa = body.get("public_address") or {}
        url = pa.get("public_url")
        if not record("the endpoint is created and VERIFIED (not just started)",
                      code == 200 and pa.get("status") == "online" and bool(url),
                      f"HTTP {code} status={pa.get('status')} reason={pa.get('reason')}"):
            print(f"  tunnel pid: {read_status_file().get('tunnel_pid')}")
            print(Path(SERVER_LOG).read_text(encoding="utf-8", errors="replace")[-1500:])
            return 1
        info("public address", str(url))
        record("every check of the verification passed",
               all((pa.get("checks") or {}).values())
               and {"health", "ui", "auth_status", "api_gated"} <= set(pa.get("checks") or {}),
               json.dumps(pa.get("checks")))

        # ------------------------------------- 5. unauthenticated remote client
        print("\n5. Through the PUBLIC address (a separate client, no session)")
        remote = Client(str(url))
        code, health = remote.get("/api/health", timeout=45)
        record("the address really serves this app (/api/health)", code == 200
               and health.get("status") == "ok", f"HTTP {code}")
        code, page = remote.get("/", timeout=45)
        record("the app UI loads through the address", code == 200,
               f"HTTP {code} bytes={len(str(page.get('raw') or ''))}")
        code, auth = remote.get("/api/auth/status", timeout=45)
        record("public mode is reported by the auth status", code == 200
               and auth.get("public_mode") is True, f"HTTP {code}")
        code, body = remote.get("/api/dashboard", timeout=45)
        record("an unauthenticated caller is REFUSED protected data",
               code == 401 and body.get("code") == "auth_required",
               f"HTTP {code} {body.get('code')}")
        # The distributed layer authenticates per route (bearer tokens), and the
        # multi-account guard sits in front of it: with the layer off this is the
        # refusal (403) rather than the 401 a working route would answer. Either
        # way it is a refusal — and the layer-ON case is checked below to prove
        # the 401 is what an unauthenticated caller normally gets.
        code, body = remote.get("/api/distributed/accounts", timeout=45)
        record("an unauthenticated caller cannot reach account administration",
               code in (401, 403) and "accounts" not in body,
               f"HTTP {code} {body.get('code')}")
        code, _ = remote.get("/docs", timeout=45)
        record("the interactive API docs stay hidden while public",
               code in (404, 403, 401), f"HTTP {code}")

        # ----------------------------------------- 6. authorised remote client
        print("\n6. An AUTHORISED remote client through the same address")
        code, _ = remote.post("/api/auth/login", LOGIN, timeout=45)
        # Sign-in answers 204 with the session cookie (no body by design), then
        # the CSRF token for state-changing calls comes from the auth status.
        _, signed_in = remote.get("/api/auth/status", timeout=45)
        remote.csrf = signed_in.get("csrf")
        record("sign-in works through the address",
               code in (200, 204) and signed_in.get("authenticated") is True,
               f"HTTP {code}")
        code, _ = remote.get("/api/dashboard", timeout=45)
        record("the signed-in remote client can read data", code == 200, f"HTTP {code}")
        code, ping = remote.get("/api/distributed/ping", timeout=45)
        record("the remote client's backend connection works", code == 200
               and ping.get("ok") is True, f"HTTP {code}")
        code, hb = remote.post("/api/distributed/heartbeat", {
            "device_id": "live-verify-remote", "worker_id": "live-verify-remote",
            "status": "ONLINE", "app_version": "5.0.0"}, timeout=45)
        record("a remote device can still heartbeat", code == 200, f"HTTP {code}")

        # ------------------------------------------------ 7. failure honesty
        print("\n7. A dead tunnel is reported as FAILED, never as active")
        pid = int(read_status_file().get("tunnel_pid") or 0)
        if pid:
            if os.name == "nt":
                subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                               capture_output=True, timeout=20)
            else:
                os.kill(pid, 9)
            info("killed the tunnel process on purpose", f"pid={pid}")
        code, body = local.post("/api/access/public-address/recheck", timeout=120)
        pa = body.get("public_address") or {}
        record("the app reports FAILED after the tunnel died",
               pa.get("status") == "failed", f"status={pa.get('status')}")
        record("no address is displayed as active while failed",
               pa.get("public_url") is None, str(pa.get("public_url")))
        record("an actionable reason is given", bool(pa.get("reason")),
               str(pa.get("reason"))[:160])
        record("the address is still REMEMBERED for the next time",
               bool(pa.get("last_address")), str(pa.get("last_address")))

        # ------------------------------------------------------ 8. switch OFF
        print("\n8. Public Address OFF — the endpoint stops answering")
        code, body = local.post("/api/access/public-address", {"enabled": False},
                                timeout=90)
        pa = body.get("public_address") or {}
        record("the switch reports OFF", pa.get("requested") is False
               and pa.get("status") == "disabled" and pa.get("public_url") is None,
               f"status={pa.get('status')}")
        record("the saved address survives the switch being turned off",
               bool(pa.get("last_address")), str(pa.get("last_address")))
        record("no tunnel process is left behind by the app",
               tunnel_pid_for_port(PORT) is None)
        stopped, waited = False, 0.0
        while waited < 60 and not stopped:
            code, _ = Client(str(url)).get("/api/health", timeout=20)
            stopped = code != 200
            if not stopped:
                time.sleep(3)
                waited += 3
        record("the old address no longer serves the app", stopped,
               f"last HTTP {code} after {int(waited)}s")
        code, _ = local.get("/api/dashboard")
        record("local access still works after switching it off", code == 200,
               f"HTTP {code}")
        code, _ = local.get("/api/distributed/ping")
        record("the backend connection still works after switching it off",
               code == 200, f"HTTP {code}")

        # ---------------------------------------------------- 9. persistence
        print("\n9. Both settings survive a restart")
        local.post("/api/access/public-address", {"enabled": False}, timeout=90)
        local.post("/api/access/multi-account", {"enabled": False})
        stop_server(server)
        server = start_server()
        if not record("the application restarts", wait_for_health(Client()), 90):
            return 1
        fresh = Client()
        fresh.post("/api/auth/login", LOGIN)
        code, status = fresh.get("/api/auth/status")
        fresh.csrf = status.get("csrf")
        code, state = fresh.get("/api/access/state")
        pa = state.get("public_address") or {}
        caps = state.get("multi_account") or {}
        record("the Public Address preference is still OFF after the restart",
               pa.get("requested") is False and pa.get("status") == "disabled",
               f"status={pa.get('status')}")
        record("multi-account mode is still OFF after the restart",
               caps.get("multi_account") is False, f"source={caps.get('source')}")
        code, _ = fresh.get("/api/dashboard")
        record("local access works after the restart", code == 200, f"HTTP {code}")
        return 0 if all(r["status"] != "FAIL" for r in RESULTS) else 1
    finally:
        # Never leave a public tunnel running: the app stops its own on the way
        # out, and this is the belt-and-braces sweep for the port WE chose (a
        # tunnel started by the user for another port is never touched).
        stop_server(server)
        if kill_tunnel_for_port(PORT):
            info("removed a leftover tunnel for this test port", str(PORT))
        RESULTS_PATH.write_text(json.dumps({
            "port": PORT, "data_dir": str(DATA),
            "mocked": False,
            "note": ("Real application, real cloudflared tunnel, real HTTP. The "
                     "remote client is a separate process on the same machine "
                     "reaching the app through the Cloudflare edge, not a second "
                     "physical machine."),
            "results": RESULTS,
            "summary": {
                "pass": sum(1 for r in RESULTS if r["status"] == "PASS"),
                "fail": sum(1 for r in RESULTS if r["status"] == "FAIL"),
                "info": sum(1 for r in RESULTS if r["status"] == "INFO"),
            },
        }, indent=2), encoding="utf-8")
        print(f"\nResults written to {RESULTS_PATH}")


if __name__ == "__main__":
    code = main()
    summary = {r["status"] for r in RESULTS}
    print("\n" + ("ALL CHECKS PASSED" if code == 0 and "FAIL" not in summary
                  else "SOME CHECKS FAILED — see the log above"))
    sys.exit(code)
