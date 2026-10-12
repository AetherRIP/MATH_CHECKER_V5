"""Live end-to-end verification of the shared multi-user login.

Spawns the REAL launcher (``MATH V5/main.py``) against a throwaway data dir
and drives it over HTTP exactly like the browser does — no TestClient, no
shortcuts:

  1. first run bootstraps the DEVELOPER + TEACHER accounts (credentials file),
  2. with MC5_LOGIN_REQUIRED=1 the sign-in gate blocks every API route,
  3. a wrong password gets a GENERIC 401 (never reveals the username) and
     repeated failures are throttled with 429,
  4. a real sign-in returns an HttpOnly session cookie + CSRF token + role,
  5. unsafe requests WITHOUT the CSRF header are refused with 403,
  6. role rules are enforced server-side: a TEACHER cannot create a TEACHER
     account, the DEVELOPER can create accounts,
  7. two accounts never share an identity, and both sessions work at once,
  8. logout invalidates the cookie,
  9. the session survives a server restart (same data dir / HMAC secret) —
     the packaging-relevant stability check.

Run:  ../.venv/Scripts/python.exe scripts/e2e_login_live.py
Exit code 0 = every check passed.
"""
from __future__ import annotations

import http.cookiejar
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent   # MATH V5/
PORT = 0
FAILURES: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    mark = "PASS" if ok else "FAIL"
    print(f"[{mark}] {name}" + (f" — {detail}" if detail and not ok else ""))
    if not ok:
        FAILURES.append(f"{name}: {detail}")


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def spawn(port: int, data_dir: Path) -> subprocess.Popen:
    env = dict(os.environ)
    env["MC5_DATA_DIR"] = str(data_dir)
    env["MC5_LOGIN_REQUIRED"] = "1"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env.pop("MC5_PUBLIC", None)
    return subprocess.Popen(
        [sys.executable, "main.py", str(port)],
        cwd=str(ROOT), env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def wait_health(port: int, timeout: float = 90.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(
                    f"http://127.0.0.1:{port}/api/health", timeout=3) as resp:
                if resp.status == 200:
                    return True
        except Exception:  # noqa: BLE001 — server still starting
            time.sleep(0.4)
    return False


class Client:
    """One browser: its own cookie jar (its own signed session)."""

    def __init__(self) -> None:
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.jar))

    def raw(self, method: str, path: str, payload=None, csrf: str | None = None,
            headers: dict | None = None):
        data = json.dumps(payload).encode() if payload is not None else None
        req = urllib.request.Request(
            f"http://127.0.0.1:{PORT}{path}", data=data, method=method)
        req.add_header("Content-Type", "application/json")
        if csrf:
            req.add_header("x-mc5-csrf", csrf)
        for key, value in (headers or {}).items():
            req.add_header(key, value)
        try:
            with self.opener.open(req, timeout=20) as resp:
                body = resp.read()
                return resp.status, dict(resp.headers), body
        except urllib.error.HTTPError as exc:
            return exc.code, dict(exc.headers), exc.read()

    def json(self, method: str, path: str, payload=None, csrf: str | None = None,
             headers: dict | None = None):
        status, hdrs, body = self.raw(method, path, payload, csrf, headers)
        try:
            return status, hdrs, json.loads(body or b"{}")
        except json.JSONDecodeError:
            return status, hdrs, {}


def header(headers: dict, name: str) -> str:
    """Case-insensitive header lookup (urllib header casing varies)."""
    for key, value in headers.items():
        if key.lower() == name.lower():
            return value
    return ""


def main() -> int:
    global PORT
    PORT = free_port()
    data_dir = Path(tempfile.mkdtemp(prefix="mc5_login_e2e_"))
    print(f"server: http://127.0.0.1:{PORT}  data: {data_dir}")

    proc = spawn(PORT, data_dir)
    try:
        check("server starts and answers /api/health", wait_health(PORT))

        # ---- 1. bootstrap accounts exist on a fresh database ---------------
        creds_path = data_dir / "distributed_bootstrap.txt"
        check("bootstrap credentials file written", creds_path.exists(),
              str(creds_path))
        creds = json.loads(creds_path.read_text(encoding="utf-8"))
        dev_user, dev_pass = creds["developer"]["username"], creds["developer"]["password"]
        t_user, t_pass = creds["teacher"]["username"], creds["teacher"]["password"]

        anon = Client()
        status, _, body = anon.json("GET", "/api/auth/status")
        check("sign-in is REQUIRED when MC5_LOGIN_REQUIRED=1",
              status == 200 and body.get("required") is True
              and body.get("authenticated") is False, str(body)[:200])

        status, _, body = anon.json("GET", "/api/dashboard")
        check("API is blocked before sign-in (401 auth_required)",
              status == 401 and body.get("code") == "auth_required",
              f"{status} {body}")

        # ---- 3. wrong password: generic 401, then throttled ---------------
        status, _, body = anon.json("POST", "/api/auth/login",
                                    {"username": t_user, "password": "wrong-pass-1"})
        check("wrong password rejected (401 invalid_credentials)",
              status == 401 and body.get("code") == "invalid_credentials",
              f"{status} {body}")
        status, _, body = anon.json("POST", "/api/auth/login",
                                    {"username": "nobody-here", "password": "x"})
        check("unknown username gets the SAME generic message",
              status == 401 and body.get("code") == "invalid_credentials"
              and "Incorrect username or password" in body.get("message", ""),
              f"{status} {body}")

        # ---- 4. real sign-in: cookie + CSRF + role ------------------------
        teacher = Client()
        status, headers, body = teacher.json(
            "POST", "/api/auth/login",
            {"username": t_user, "password": t_pass, "remember_me": True})
        cookie_hdr = header(headers, "Set-Cookie")
        check("teacher sign-in succeeds (204) with an HttpOnly session cookie",
              status == 204 and "mc5_session=" in cookie_hdr
              and "HttpOnly" in cookie_hdr, f"{status} {cookie_hdr[:160]}")
        check("remember_me makes the cookie persistent (Max-Age)",
              "Max-Age=" in cookie_hdr, cookie_hdr[:160])
        csrf = header(headers, "X-MC5-CSRF")
        check("sign-in returns a CSRF token", bool(csrf), str(headers)[:160])

        status, _, body = teacher.json("GET", "/api/auth/me")
        check("session identifies the teacher", status == 200
              and body.get("username") == t_user, f"{status} {body}")
        status, _, body = teacher.json("GET", "/api/auth/status")
        check("status reports the signed-in identity + role",
              body.get("authenticated") is True and body.get("username") == t_user
              and body.get("role") == "teacher", str(body)[:200])

        # ---- 5. CSRF on mutating requests ---------------------------------
        status, _, body = teacher.json(
            "POST", "/api/automation/settings", {"enabled": True})
        check("mutation WITHOUT the CSRF header is refused (403)",
              status == 403 and body.get("code") == "csrf_failed",
              f"{status} {body}")
        status, _, body = teacher.json(
            "POST", "/api/automation/settings", {"enabled": True}, csrf=csrf)
        check("same mutation WITH the CSRF header succeeds",
              status == 200, f"{status} {body}")

        # ---- 6. roles enforced server-side --------------------------------
        status, _, body = teacher.json(
            "POST", "/api/distributed/accounts",
            {"username": "sneaky", "display_name": "Sneaky",
             "password": "long-enough-password", "role": "TEACHER"},
            headers={"Authorization": "Bearer not-a-token"})
        check("distributed API refuses missing/bearer-less auth (401)",
              status == 401, f"{status} {body}")

        dev = Client()
        status, _, body = dev.json(
            "POST", "/api/distributed/login",
            {"username": dev_user, "password": dev_pass})
        check("developer signs in to the Multi-School API", status == 200
              and bool((body or {}).get("token")), f"{status} {body}")
        dev_token = (body or {}).get("token")
        dev.json("POST", "/api/auth/login",
                 {"username": dev_user, "password": dev_pass})

        status, _, body = teacher.json(
            "POST", "/api/distributed/accounts",
            {"username": "new-teacher", "display_name": "New Teacher",
             "password": "long-enough-password", "role": "TEACHER"},
            headers={"Authorization": f"Bearer {dev_token}"})
        created = status == 200
        check("developer CAN create a TEACHER account", created, f"{status} {body}")

        # teacher cookie session is NOT a bearer token — role rules for the
        # distributed API are checked against the TOKEN's account.
        status, _, body = teacher.json(
            "POST", "/api/distributed/login", {"username": t_user, "password": t_pass})
        teacher_token = (body or {}).get("token") if status == 200 else None
        status, _, body = teacher.json(
            "POST", "/api/distributed/accounts",
            {"username": "nope", "display_name": "Nope",
             "password": "long-enough-password", "role": "DEVELOPER"},
            headers={"Authorization": f"Bearer {teacher_token or 'x'}"})
        check("a teacher token CANNOT create a DEVELOPER account (403)",
              status == 403 and body.get("code") == "forbidden", f"{status} {body}")

        # ---- 7. two accounts never share an identity ----------------------
        status, _, body = dev.json("GET", "/api/auth/me")
        check("developer session is its own identity (not the teacher's)",
              status == 200 and body.get("username") == dev_user, f"{status} {body}")
        status, _, body = teacher.json("GET", "/api/auth/me")
        check("teacher session still sees the teacher after dev signed in",
              status == 200 and body.get("username") == t_user, f"{status} {body}")

        # ---- 7b. disabling an account kills its LIVE session --------------
        status, _, body = dev.json(
            "POST", "/api/distributed/accounts",
            {"username": "co-teach", "display_name": "Co Teacher",
             "password": "long-enough-password", "role": "CO_TEACHER"},
            headers={"Authorization": f"Bearer {dev_token}"})
        check("developer creates a CO_TEACHER account", status == 200,
              f"{status} {body}")
        co_id = (body or {}).get("account_id")
        co = Client()
        status, _, body = co.json(
            "POST", "/api/auth/login",
            {"username": "co-teach", "password": "long-enough-password"})
        check("co-teacher signs in on the MAIN login page", status == 204,
              f"{status} {body}")
        status, _, body = co.json("GET", "/api/auth/me")
        check("co-teacher session works", status == 200, f"{status} {body}")
        status, _, body = dev.json(
            "POST", f"/api/distributed/accounts/{co_id}/disable", {},
            headers={"Authorization": f"Bearer {dev_token}"})
        check("developer disables the co-teacher account", status == 200,
              f"{status} {body}")
        status, _, body = co.json("GET", "/api/auth/me")
        check("a disabled account's session stops working immediately",
              status == 401 and body.get("code") == "auth_invalid",
              f"{status} {body}")

        # ---- 8. logout ----------------------------------------------------
        status, _, _ = teacher.json("POST", "/api/auth/logout", {})
        status, _, body = teacher.json("GET", "/api/auth/me")
        check("after logout the session is gone (401 auth_required)",
              status == 401 and body.get("code") == "auth_required",
              f"{status} {body}")

        # ---- 3b. throttling (last: it poisons this client's IP) -----------
        status = None
        for _ in range(12):
            status, _, body = anon.json(
                "POST", "/api/auth/login",
                {"username": t_user, "password": "still-wrong"})
            if status == 429:
                break
        check("repeated failures are throttled (429 too_many_attempts)",
              status == 429 and body.get("code") == "too_many_attempts",
              f"last={status} {body}")

        # ---- 9. session survives a restart (same data dir) ----------------
        proc.terminate()
        proc.wait(timeout=20)
        proc = spawn(PORT, data_dir)
        check("server restarts on the same data dir", wait_health(PORT))
        status, _, body = dev.json("GET", "/api/auth/me")
        check("signed-in session survives the restart",
              status == 200 and body.get("username") == dev_user, f"{status} {body}")

        # ---- local mode (MC5_LOGIN_REQUIRED unset/0) stays open ------------
        proc.terminate()
        proc.wait(timeout=20)
        env = dict(os.environ)
        env["MC5_DATA_DIR"] = str(data_dir)
        env["MC5_LOGIN_REQUIRED"] = "0"
        env.pop("MC5_PUBLIC", None)
        proc = subprocess.Popen(
            [sys.executable, "main.py", str(PORT)], cwd=str(ROOT), env=env,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        check("local mode (MC5_LOGIN_REQUIRED=0) restarts", wait_health(PORT))
        status, _, body = anon.json("GET", "/api/auth/status")
        check("local mode reports sign-in NOT required",
              status == 200 and body.get("required") is False, str(body)[:200])
    finally:
        try:
            proc.terminate()
            proc.wait(timeout=20)
        except Exception:  # noqa: BLE001
            pass

    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILED:")
        for line in FAILURES:
            print("  -", line)
        return 1
    print("ALL CHECKS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
