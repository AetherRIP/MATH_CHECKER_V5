"""Public-access security tests (section 12-16, 20).

Locks the guarantees that make exposing the laptop through a tunnel safe:

  * local mode is unchanged (no login on localhost),
  * public mode authenticates every API request server-side,
  * public mode with no account configured fails closed,
  * CSRF is required for state-changing requests,
  * secrets are never returned and passwords are hashed,
  * login is rate-limited and first-time setup is local-only.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from app import auth_gate as ag
from app.api import auth_routes
from app.errors import AppError


def _build_app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(ag.PublicAuthMiddleware)
    auth_routes.register(app)

    @app.get("/api/dashboard")
    def _dash():  # stands in for a protected data endpoint
        return {"ok": True}

    @app.post("/api/thing")
    def _thing():
        return {"ok": True}

    @app.get("/")
    def _index():
        return {"shell": True}

    @app.exception_handler(AppError)
    async def _handler(request, exc: AppError):
        return JSONResponse(status_code=exc.status, content=exc.to_dict())

    return app


@pytest.fixture
def local_client(fresh_state):
    with TestClient(_build_app(), base_url="http://testserver") as c:
        yield c


@pytest.fixture
def public_client(fresh_state, monkeypatch):
    monkeypatch.setenv("MC5_PUBLIC", "1")
    # https so the Secure session cookie is actually sent back by httpx
    with TestClient(_build_app(), base_url="https://testserver") as c:
        yield c


# ------------------------------------------------------------ local mode

def test_local_mode_needs_no_login(local_client):
    assert local_client.get("/api/auth/status").json()["required"] is False
    assert local_client.get("/api/dashboard").status_code == 200
    assert local_client.post("/api/thing").status_code == 200


def test_local_status_reports_no_secrets(local_client):
    body = local_client.get("/api/auth/status").json()
    assert body["configured"] is False
    # no secret material is ever echoed
    for key in ("password", "password_hash", "password_salt", "session_secret"):
        assert key not in body


# ------------------------------------------------------- public mode: fail closed

def test_public_mode_without_account_fails_closed(public_client):
    assert public_client.get("/api/dashboard").status_code == 503
    assert public_client.get("/api/thing").status_code == 503
    body = public_client.get("/api/auth/status").json()
    assert body["required"] is True and body["configured"] is False


def test_public_mode_requires_auth_once_configured(public_client):
    ag.set_credentials("teacher", "correct-horse-battery")
    r = public_client.get("/api/dashboard")
    assert r.status_code == 401
    assert r.json()["code"] == "auth_required"


def test_public_shell_loads_but_data_is_blocked(public_client):
    ag.set_credentials("teacher", "correct-horse-battery")
    # the SPA shell may load so the login screen can render …
    assert public_client.get("/").status_code == 200
    # … but no API data is served without a session
    assert public_client.get("/api/dashboard").status_code == 401


# ------------------------------------------------------------- login / sessions

def _login(c: TestClient, username="teacher", password="correct-horse-battery"):
    r = c.post("/api/auth/login", json={"username": username, "password": password})
    return r


def test_login_then_access_with_cookie_and_csrf(public_client):
    ag.set_credentials("teacher", "correct-horse-battery")
    r = _login(public_client)
    assert r.status_code == 204, r.text
    csrf = r.headers["x-mc5-csrf"]
    assert public_client.get("/api/dashboard").status_code == 200

    # unsafe request WITHOUT the CSRF header is refused
    assert public_client.post("/api/thing").status_code == 403
    # with the header it succeeds
    assert public_client.post("/api/thing", headers={"X-MC5-CSRF": csrf}).status_code == 200


def test_wrong_password_is_generic_401(public_client):
    ag.set_credentials("teacher", "correct-horse-battery")
    r = _login(public_client, password="nope")
    assert r.status_code == 401
    assert r.json()["message"] == "Incorrect username or password."


def test_login_is_rate_limited(public_client):
    ag.set_credentials("teacher", "correct-horse-battery")
    for _ in range(ag._MAX_FAILURES):
        _login(public_client, password="nope")
    r = _login(public_client, password="nope")
    assert r.status_code == 429
    assert r.json()["code"] == "too_many_attempts"


def test_logout_clears_session(public_client):
    ag.set_credentials("teacher", "correct-horse-battery")
    _login(public_client)
    assert public_client.get("/api/dashboard").status_code == 200
    assert public_client.post("/api/auth/logout").status_code == 204
    assert public_client.get("/api/dashboard").status_code == 401


def test_tampered_session_cookie_is_rejected(public_client):
    ag.set_credentials("teacher", "correct-horse-battery")
    _login(public_client)
    good = public_client.cookies.get(ag.SESSION_COOKIE)
    assert ag.read_session(good) is not None
    # flip a character in the signature
    bad = good[:-1] + ("A" if good[-1] != "A" else "B")
    assert ag.read_session(bad) is None
    public_client.cookies.set(ag.SESSION_COOKIE, bad)
    assert public_client.get("/api/dashboard").status_code == 401


# ------------------------------------------------------------------- setup rules

def test_setup_reachable_in_public_mode_so_bootstrap_is_possible(public_client, monkeypatch):
    """Public mode must not lock the teacher out of creating the first account.

    The gate covers localhost too (public mode is config-driven), so a local
    bootstrap request has to reach the route — which then enforces local-only.
    """
    monkeypatch.setattr(ag, "is_genuinely_local", lambda request: True)
    r = public_client.post("/api/auth/setup",
                           json={"username": "teacher", "password": "longenoughpw"})
    assert r.status_code == 200, r.text
    assert ag.credentials_configured() is True


def test_setup_requires_local_request(public_client, monkeypatch):
    # a remote (proxied) request must not be able to create the account
    monkeypatch.setattr(ag, "is_genuinely_local", lambda request: False)
    r = public_client.post("/api/auth/setup",
                           json={"username": "teacher", "password": "longenoughpw"})
    assert r.status_code == 403
    assert r.json()["code"] == "local_only"
    assert ag.credentials_configured() is False


def test_setup_is_rate_limited(public_client, monkeypatch):
    """Changing the password without a session must not be a brute-force oracle."""
    monkeypatch.setattr(ag, "is_genuinely_local", lambda request: True)
    ag.set_credentials("teacher", "correct-horse-battery")
    for _ in range(ag._MAX_FAILURES):
        r = public_client.post("/api/auth/setup",
                               json={"username": "teacher", "password": "brand-new-password",
                                     "current_password": "nope"})
        assert r.status_code == 401
    blocked = public_client.post("/api/auth/setup",
                                 json={"username": "teacher", "password": "brand-new-password",
                                       "current_password": "correct-horse-battery"})
    assert blocked.status_code == 429
    assert blocked.json()["code"] == "too_many_attempts"
    # the original password still works: nothing was replaced
    assert ag.verify_credentials("teacher", "correct-horse-battery") is True


def test_changing_the_password_never_renames_the_account(local_client, monkeypatch):
    """A password-only change must not silently rename the host account.

    The Settings card deliberately does not display the stored username to an
    unauthenticated caller, so it sends no username at all. The route used to
    default that to "teacher" and therefore RENAMED an account created under a
    different name as a side effect of changing the password.
    """
    monkeypatch.setattr(ag, "is_genuinely_local", lambda request: True)
    local_client.post("/api/auth/setup",
                      json={"username": "ms.rivers", "password": "first-password"})
    assert ag.configured_username() == "ms.rivers"

    # No username key at all: keep the existing one.
    changed = local_client.post("/api/auth/setup",
                                json={"password": "second-password",
                                      "current_password": "first-password"})
    assert changed.status_code == 200, changed.text
    assert changed.json()["username"] == "ms.rivers"
    assert ag.configured_username() == "ms.rivers"
    assert ag.verify_credentials("ms.rivers", "second-password") is True

    # An empty string behaves the same way.
    blank = local_client.post("/api/auth/setup",
                              json={"username": "   ", "password": "third-password",
                                    "current_password": "second-password"})
    assert blank.json()["username"] == "ms.rivers"

    # An explicit new username still renames it.
    renamed = local_client.post("/api/auth/setup",
                               json={"username": "mr.rivers",
                                     "password": "fourth-password",
                                     "current_password": "third-password"})
    assert renamed.json()["username"] == "mr.rivers"
    assert ag.configured_username() == "mr.rivers"


def test_the_first_account_still_defaults_to_teacher(local_client, monkeypatch):
    """The bootstrap default is only for the FIRST account (no rename risk)."""
    monkeypatch.setattr(ag, "is_genuinely_local", lambda request: True)
    r = local_client.post("/api/auth/setup", json={"password": "longenoughpw"})
    assert r.status_code == 200, r.text
    assert r.json()["username"] == "teacher"
    assert ag.verify_credentials("teacher", "longenoughpw") is True


def test_setup_rejects_weak_password(local_client):
    r = local_client.post("/api/auth/setup",
                          json={"username": "teacher", "password": "short"})
    assert r.status_code == 400
    assert r.json()["code"] == "weak_password"


def test_setup_then_change_requires_current_password(local_client, monkeypatch):
    monkeypatch.setattr(ag, "is_genuinely_local", lambda request: True)
    r = local_client.post("/api/auth/setup",
                          json={"username": "teacher", "password": "first-password"})
    assert r.status_code == 200 and r.json()["configured"] is True
    assert ag.credentials_configured() is True
    # wrong current password is refused
    bad = local_client.post("/api/auth/setup",
                            json={"username": "teacher", "password": "second-password",
                                  "current_password": "wrong"})
    assert bad.status_code == 401
    # correct current password succeeds
    ok = local_client.post("/api/auth/setup",
                           json={"username": "teacher", "password": "second-password",
                                 "current_password": "first-password"})
    assert ok.status_code == 200


# ---------------------------------------------------------------- crypto basics

def test_password_is_hashed_not_stored_plaintext(fresh_state):
    ag.set_credentials("teacher", "correct-horse-battery")
    from app.config import get_secrets
    creds = get_secrets().security_credentials()
    assert "correct-horse-battery" not in json_dumps(creds)
    assert creds["password_hash"] and creds["password_salt"]
    assert ag.verify_credentials("teacher", "correct-horse-battery") is True
    assert ag.verify_credentials("teacher", "wrong") is False
    assert ag.verify_credentials("someone-else", "correct-horse-battery") is False


def json_dumps(obj) -> str:
    import json
    return json.dumps(obj)


def test_proxy_headers_mark_request_remote():
    """A tunnel adds forwarding headers; a direct local hit does not."""
    from starlette.requests import Request

    def make(headers):
        scope = {"type": "http", "headers": headers, "method": "GET", "path": "/",
                 "client": ("127.0.0.1", 5555), "query_string": b"", "scheme": "http"}
        return Request(scope)

    direct = make([])
    proxied = make([(b"cf-connecting-ip", b"203.0.113.7")])
    assert ag.is_genuinely_local(direct) is True
    assert ag.is_genuinely_local(proxied) is False
