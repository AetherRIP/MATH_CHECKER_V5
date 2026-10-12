"""Shared-backend connectivity (distribution brief, Stages Two/Three/Five).

One installation is the always-on backend; every other laptop's browser UI
addresses it by URL. That is a CROSS-ORIGIN call, so three things must be true
and are pinned here:

* the allow-list is EMPTY by default — a single-machine install behaves exactly
  as it did before, and nothing is ever allowed implicitly;
* when an operator names origins, only those origins get CORS headers, the
  browser is never permitted to send the app's session cookie, and the preflight
  is answered ahead of the local login gate;
* there is exactly ONE public distributed route — a reachability probe that
  answers with the service name and version and nothing else — so a UI badge can
  follow a real HTTP answer instead of "the page loaded".
"""

from __future__ import annotations

import importlib

import pytest


@pytest.fixture
def client(fresh_state):
    from app.main import app
    from fastapi.testclient import TestClient
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def configured_app(monkeypatch):
    """Rebuild the app with MC5_ALLOWED_ORIGINS set, then restore it."""
    import app.main as main_mod

    def build(origins: str):
        monkeypatch.setenv("MC5_ALLOWED_ORIGINS", origins)
        importlib.reload(main_mod)
        return main_mod.app

    yield build
    # Undo before monkeypatch's own teardown so the process is left as it began.
    monkeypatch.delenv("MC5_ALLOWED_ORIGINS", raising=False)
    importlib.reload(main_mod)


def _lower(headers) -> set[str]:
    return {key.lower() for key in headers}


# ------------------------------------------------------------------ allow-list

def test_allowed_origins_parses_dedupes_and_trims(monkeypatch):
    from app.config import allowed_origins
    monkeypatch.setenv(
        "MC5_ALLOWED_ORIGINS",
        " http://laptop-b:5173/ ,http://teacher-pc:8642,, http://laptop-b:5173 ")
    assert allowed_origins() == ["http://laptop-b:5173", "http://teacher-pc:8642"]


def test_allowed_origins_is_empty_unless_configured(monkeypatch):
    from app.config import allowed_origins
    monkeypatch.setenv("MC5_ALLOWED_ORIGINS", "")
    assert allowed_origins() == []
    monkeypatch.delenv("MC5_ALLOWED_ORIGINS", raising=False)
    assert allowed_origins() == []


# ------------------------------------------------------------------------- CORS

def test_no_cors_headers_when_nothing_is_configured(client):
    """Default install: a foreign Origin gets no permission at all."""
    resp = client.get("/api/distributed/ping",
                      headers={"Origin": "http://laptop-b:5173"})
    assert resp.status_code == 200
    assert "access-control-allow-origin" not in _lower(resp.headers)


def test_only_the_configured_origin_is_allowed_and_never_with_cookies(
        configured_app):
    from fastapi.testclient import TestClient
    with TestClient(configured_app("http://laptop-b:5173/")) as c:
        allowed = c.get("/api/distributed/ping",
                        headers={"Origin": "http://laptop-b:5173"})
        assert allowed.status_code == 200
        assert allowed.headers.get("access-control-allow-origin") \
            == "http://laptop-b:5173"
        # Bearer tokens only: the browser must never be told it may send the
        # app's session cookie to another machine.
        assert "access-control-allow-credentials" not in _lower(allowed.headers)

        stranger = c.get("/api/distributed/ping",
                         headers={"Origin": "http://not-allowed:9999"})
        assert "access-control-allow-origin" not in _lower(stranger.headers)


def test_preflight_is_answered_before_the_local_session_gate(configured_app):
    """A cross-origin POST is preceded by an unauthenticated OPTIONS; if the
    login gate answered it first the browser would never send the real call."""
    from fastapi.testclient import TestClient
    with TestClient(configured_app("http://laptop-b:5173")) as c:
        resp = c.options("/api/distributed/login", headers={
            "Origin": "http://laptop-b:5173",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type,authorization",
        })
        assert resp.status_code in (200, 204)
        assert resp.headers.get("access-control-allow-origin") \
            == "http://laptop-b:5173"
        assert "POST" in (resp.headers.get("access-control-allow-methods") or "")
        assert "authorization" in (
            resp.headers.get("access-control-allow-headers") or "").lower()


# ------------------------------------------------------------- reachability probe

def test_ping_is_public_and_says_only_what_it_must(client):
    resp = client.get("/api/distributed/ping")
    assert resp.status_code == 200
    body = resp.json()
    assert set(body) == {"ok", "service", "version"}
    assert body["ok"] is True
    assert body["service"] == "math-checker-distributed"
    assert body["version"].count(".") == 2          # MAJOR.MINOR.PATCH

    # It must not become a way to enumerate a school, and it must not leak
    # anything usable: no token, no account/device names, no filesystem path.
    text = repr(body).lower()
    for forbidden in ("token", "password", "secret", "api_key", "account",
                      "device", "school", "c:\\\\", "/users/"):
        assert forbidden not in text, forbidden


def test_ping_is_the_only_public_distributed_route(client):
    """Everything else still demands a bearer token."""
    assert client.get("/api/distributed/ping").status_code == 200
    for path in ("/api/distributed/me", "/api/distributed/workers",
                 "/api/distributed/audit", "/api/distributed/teams"):
        resp = client.get(path)
        assert resp.status_code == 401, path
        assert resp.json()["code"] == "auth_required"
