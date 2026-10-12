"""Main login page + backend role enforcement (multi-user integration).

These tests run with ``MC5_LOGIN_REQUIRED=1`` — the production default — and
prove that:

* the API refuses requests without a signed session cookie (fail closed),
* credentials are checked against the Multi-School accounts server-side,
* "Remember me" only changes the cookie lifetime (no password anywhere),
* the session restores account, role and permissions after a page reload,
* logout ends the session,
* a disabled account's session stops working on the very next request,
* role/permission checks are enforced by the BACKEND, not the UI.
"""
import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(fresh_state, monkeypatch):
    monkeypatch.setenv("MC5_LOGIN_REQUIRED", "1")
    from app.config import get_config, save_config
    cfg = get_config()
    cfg.mock_mode = True
    save_config(cfg)

    from app.main import app
    with TestClient(app) as test_client:
        yield test_client


def _account(username, role, password="sup3rsafe2"):
    from app.distributed import accounts
    try:
        return accounts.create_account(
            username=username, display_name=username.title(),
            password=password, role=role, created_by=None)
    except ValueError:
        return accounts.get_account_by_username(username)


def _login(client, username, password="sup3rsafe2", remember=False):
    return client.post("/api/auth/login", json={
        "username": username, "password": password, "remember_me": remember})


def _csrf(client):
    return client.get("/api/auth/status").json().get("csrf")


def _authed(client, username, password="sup3rsafe2", remember=False):
    response = _login(client, username, password, remember)
    assert response.status_code == 204, response.text
    token = response.headers.get("X-MC5-CSRF")
    assert token
    return token


# -- the gate ------------------------------------------------------------------

def test_api_requires_session_when_login_required(client):
    response = client.get("/api/dashboard")
    assert response.status_code == 401
    assert response.json()["code"] == "auth_required"
    # The SPA shell and the sign-in endpoints stay reachable.
    assert client.get("/api/auth/status").status_code == 200
    assert client.get("/api/health").status_code == 200


def test_status_reports_login_requirement_and_identity(client):
    status = client.get("/api/auth/status").json()
    assert status["required"] is True
    assert status["login_required"] is True
    assert status["authenticated"] is False
    assert status["role"] is None


# -- authentication against the shared Multi-School accounts -------------------

def test_wrong_password_is_rejected_without_leaking_existence(client):
    _account("devuser", "DEVELOPER")
    response = _login(client, "devuser", "wrong-password")
    assert response.status_code == 401
    assert response.json()["code"] == "invalid_credentials"
    # No cookie was set.
    assert client.get("/api/dashboard").status_code == 401


def test_login_restores_account_role_and_permissions(client):
    _account("devuser", "DEVELOPER")
    _authed(client, "devuser")

    status = client.get("/api/auth/status").json()
    assert status["authenticated"] is True
    assert status["username"] == "devuser"
    assert status["role"] == "developer"
    assert status["account_id"]

    # The dashboard role comes from the session's account, not the config.
    assert client.get("/api/dashboard").json()["role"] == "developer"


def test_remember_me_sets_a_persistent_cookie_only_when_asked(client):
    _account("teacher1", "TEACHER")

    without = _login(client, "teacher1", remember=False)
    assert without.status_code == 204
    assert "max-age" not in (without.headers.get("set-cookie") or "").lower()
    client.cookies.clear()

    with_remember = _login(client, "teacher1", remember=True)
    assert with_remember.status_code == 204
    assert "max-age" in (with_remember.headers.get("set-cookie") or "").lower()
    # The password itself never appears in the cookie value.
    cookie = with_remember.headers.get("set-cookie") or ""
    assert "sup3rsafe2" not in cookie


def test_logout_ends_the_session(client):
    _account("teacher1", "TEACHER")
    _authed(client, "teacher1")
    assert client.get("/api/dashboard").status_code == 200

    assert client.post("/api/auth/logout").status_code == 204
    # Session cookie removed -> the next API call is refused again.
    assert client.get("/api/dashboard").status_code == 401


def test_disabled_account_session_stops_working_immediately(client):
    from app.distributed import accounts
    account = _account("tempuser", "ASSISTANT")
    _authed(client, "tempuser")
    assert client.get("/api/dashboard").status_code == 200

    accounts.set_account_active(account["id"], False)
    response = client.get("/api/dashboard")
    assert response.status_code == 401
    assert response.json()["code"] == "auth_invalid"


def test_legacy_single_teacher_account_still_signs_in(client):
    from app import auth_gate as ag
    ag.set_credentials("teacher", "legacy-passphrase")

    response = _login(client, "teacher", "legacy-passphrase")
    assert response.status_code == 204, response.text
    status = client.get("/api/auth/status").json()
    assert status["authenticated"] is True
    assert status["username"] == "teacher"


# -- CSRF ----------------------------------------------------------------------

def test_unsafe_requests_require_the_csrf_header(client):
    _account("teacher1", "TEACHER")
    _authed(client, "teacher1")
    # Without the header the mutating request is refused even with a session.
    # (The payload is an ordinary setting: a "role" field is rejected outright
    # now that roles come from the account/permission system.)
    response = client.post("/api/settings", json={"mock_mode": True})
    assert response.status_code == 403
    assert response.json()["code"] == "csrf_failed"

    response = client.post("/api/settings", json={"mock_mode": True},
                           headers={"x-mc5-csrf": _csrf(client)})
    assert response.status_code == 200


# -- backend role / permission enforcement -------------------------------------

def test_role_check_is_enforced_by_the_backend(client):
    from fastapi import Request
    from app import auth_gate as ag

    _account("assistant1", "ASSISTANT")
    _authed(client, "assistant1")

    # Build a real request object carrying the live session cookie.
    scope = {
        "type": "http", "method": "GET", "path": "/api/x", "headers": [],
        "query_string": b"", "client": ("127.0.0.1", 1),
    }
    request = Request(scope)
    request.state.session = ag.read_session(client.cookies.get(ag.SESSION_COOKIE))
    request.state.identity = ag.session_identity(request.state.session)

    assert ag.effective_role(request) == "ASSISTANT"
    with pytest.raises(Exception) as exc:
        ag.require_role(request, "DEVELOPER")
    assert getattr(exc.value, "status", None) == 403

    # The teacher-role account passes the same check.
    client.cookies.clear()
    _account("teacher1", "TEACHER")
    _authed(client, "teacher1")
    request = Request(scope)
    request.state.session = ag.read_session(client.cookies.get(ag.SESSION_COOKIE))
    request.state.identity = ag.session_identity(request.state.session)
    ag.require_role(request, "TEACHER", "DEVELOPER")   # must not raise


def test_permission_check_follows_the_account_permission_list(client):
    from fastapi import Request
    from app import auth_gate as ag

    _account("assistant1", "ASSISTANT")
    _authed(client, "assistant1")
    scope = {
        "type": "http", "method": "GET", "path": "/api/x", "headers": [],
        "query_string": b"", "client": ("127.0.0.1", 1),
    }
    request = Request(scope)
    request.state.session = ag.read_session(client.cookies.get(ag.SESSION_COOKIE))
    request.state.identity = ag.session_identity(request.state.session)

    ag.require_permission(request, "corrections.run")   # assistants have it
    with pytest.raises(Exception) as exc:
        ag.require_permission(request, "automation.manage")  # teachers only
    assert getattr(exc.value, "status", None) == 403
