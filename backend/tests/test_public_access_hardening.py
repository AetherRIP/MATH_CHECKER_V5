"""Public-access hardening tests (§14 file security, §21 no internal details).

These lock real defects found while auditing the app for internet exposure:

  * the SPA fallback used to join the raw request path onto ``frontend/dist``
    and serve whatever existed, so ``/%2e%2e/%2e%2e/backend/app/config.py``
    (percent-encoded, therefore un-normalised by the client) returned the
    file's real contents to an *unauthenticated* caller — including the
    teacher's ``secrets.toml`` and the SQLite database;
  * ``/docs`` / ``/openapi.json`` stayed reachable in public mode and
    described every internal endpoint and model.

The fix is containment (resolve, then require the file to live inside the build
directory) plus hiding the developer surfaces when public mode is on.
"""

from __future__ import annotations

from urllib.parse import quote

import pytest
from fastapi.testclient import TestClient

from app.main import FRONTEND_DIST, app, safe_static_path

# Files that exist on disk and must never be reachable over HTTP.
_SECRET_BEARING = [
    "../../backend/app/config.py",
    "../../backend/app/auth_gate.py",
    "../../backend/tests/conftest.py",
    "../../frontend/src/App.tsx",
    "../../README.md",
]


# ------------------------------------------------------------------ unit level

@pytest.mark.parametrize("bad", _SECRET_BEARING + [
    "",
    "/etc/passwd",
    "~/.ssh/id_rsa",
    "..\\..\\backend\\app\\config.py",
    "assets/../../../data/secrets.toml",
    "nul\x00.html",
])
def test_safe_static_path_rejects_escapes(bad):
    assert safe_static_path(bad) is None


def test_safe_static_path_allows_real_build_files():
    index = safe_static_path("index.html")
    assert index is not None and index.is_file()
    assert index.parent == FRONTEND_DIST
    if (FRONTEND_DIST / "assets").is_dir():
        asset = next(p for p in (FRONTEND_DIST / "assets").iterdir() if p.is_file())
        assert safe_static_path(f"assets/{asset.name}") == asset


# ------------------------------------------------------------- over real HTTP

def _get_raw(client: TestClient, encoded_path: str):
    """Issue a request whose path is *not* normalised by the client library.

    ``TestClient`` (httpx) collapses ``..`` segments, so the attack has to be
    percent-encoded the way a curl/attacker request would be.
    """
    return client.get(encoded_path, follow_redirects=False)


def _encode_traversal(target: str) -> str:
    """Percent-encode every character so no client can normalise the path away."""
    return "/" + quote(target, safe="")


@pytest.mark.parametrize("target", _SECRET_BEARING)
def test_traversal_cannot_read_project_files(fresh_state, target):
    # sanity: the payload really does point outside the build directory
    from pathlib import Path
    assert (FRONTEND_DIST / target).resolve().is_file(), target

    with TestClient(app) as client:
        r = _get_raw(client, _encode_traversal(target))
    assert r.status_code == 200
    # it falls through to the SPA shell instead of the requested file
    assert r.headers["content-type"].startswith("text/html")
    assert "<!doctype html>" in r.text.lower()
    assert "Central configuration" not in r.text
    assert "session_secret" not in r.text
    assert "pytest" not in r.text


def test_traversal_cannot_read_data_directory(fresh_state):
    (fresh_state / "secrets.toml").write_text(
        '[security]\nsession_secret = "LEAKCANARY"\n', encoding="utf-8")
    with TestClient(app) as client:
        for encoded in ("/%2e%2e%2f%2e%2e%2fdata%2fsecrets.toml",
                        "/..%2f..%2fdata%2fsecrets.toml",
                        "/%2e%2e/%2e%2e/data/secrets.toml"):
            r = _get_raw(client, encoded)
            assert "LEAKCANARY" not in r.text, encoded


def test_spa_fallback_still_serves_the_real_shell(fresh_state):
    with TestClient(app) as client:
        assert client.get("/").status_code == 200
        assert client.get("/results").status_code == 200       # client-side route
        assert client.get("/unknown/deep/route").status_code == 200
        missing = client.get("/api/definitely-not-a-route")
        assert missing.status_code == 404
        assert missing.json()["code"] == "not_found"


def test_the_shell_is_never_cached(fresh_state):
    """index.html names the hashed bundle, so a cached copy is a stale UI.

    Found while verifying this feature in a real browser: after an in-place
    rebuild the page kept running the OLD bundle, because index.html was
    revalidated as unchanged (same bytes, different JS). The shell is now
    served no-store; the hashed assets stay cacheable.
    """
    with TestClient(app) as client:
        for path in ("/", "/results", "/settings"):
            response = client.get(path)
            assert response.headers.get("cache-control") == "no-store", path


# ------------------------------------------------------------- debug surfaces

def test_debug_surfaces_are_hidden_in_public_mode(fresh_state, monkeypatch):
    monkeypatch.setenv("MC5_PUBLIC", "1")
    with TestClient(app) as client:
        for path in ("/docs", "/redoc", "/openapi.json", "/docs/oauth2-redirect"):
            r = client.get(path)
            assert r.status_code == 404, path
            assert "openapi" not in r.text.lower()


def test_debug_surfaces_remain_available_locally(fresh_state):
    with TestClient(app) as client:
        assert client.get("/openapi.json").status_code == 200
        assert client.get("/docs").status_code == 200


# ------------------------------------------------------------ no internal leaks

def test_error_bodies_expose_no_internals(fresh_state, monkeypatch):
    """No Windows paths, tracebacks or stack frames in any user-facing error."""
    monkeypatch.setenv("MC5_PUBLIC", "1")
    with TestClient(app) as client:
        for path in ("/api/dashboard", "/api/results", "/api/students",
                     "/api/definitely-not-a-route", "/api/auth/me"):
            body = client.get(path).text
            for needle in ("C:\\", "E:/", "Traceback", 'File "', "app.db",
                           "secrets.toml", "site-packages"):
                assert needle not in body, (path, needle)


def test_health_endpoint_is_minimal(fresh_state):
    with TestClient(app) as client:
        assert client.get("/api/health").json() == {"status": "ok"}
