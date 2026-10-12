"""Classroom OAuth connection tests (no real browser, no network).

Covers the failure modes that made the old connect button decorative:
  * the flow must request offline+consent so a REFRESH token is actually
    returned (otherwise "connected" dies on the next request),
  * a sign-in without a refresh token must fail loudly, not half-succeed,
  * the connected account email is captured and cleared on disconnect.
"""

from __future__ import annotations

import base64
import json

import pytest

import app.classroom.auth as auth


class FakeCreds:
    def __init__(self, refresh_token="rt-secret-token", id_token=None):
        self.refresh_token = refresh_token
        self.id_token = id_token
        self.token = "ya29.fake-access-token"

    def refresh(self, request):  # noqa: ARG002 - never used in these tests
        raise AssertionError("no network in unit tests")


def _email_id_token(email: str) -> str:
    payload = base64.urlsafe_b64encode(
        json.dumps({"email": email}).encode()).decode().rstrip("=")
    return f"hdr.{payload}.sig"


@pytest.fixture
def oauth_configured(fresh_state, monkeypatch):
    """Point the auth module at a throwaway OAuth client JSON (the test data
    dir starts empty; the real client file lives outside it)."""
    import app.config as config
    client_dir = fresh_state / "secrets"
    client_dir.mkdir(parents=True, exist_ok=True)
    client_json = client_dir / "google_oauth_client.json"
    client_json.write_text(json.dumps({
        "installed": {
            "client_id": "fake.apps.googleusercontent.com",
            "client_secret": "GOCSPX-fake-test-secret",
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": ["http://localhost"],
        }
    }), encoding="utf-8")
    # Both auth.client_configured() and SecretStore.oauth_client_config()
    # resolve the path through app.config at call time — repoint it there.
    monkeypatch.setattr(config, "OAUTH_CLIENT_PATH", client_json)
    assert config.OAUTH_CLIENT_PATH.name == client_json.name  # same shape
    return client_json


@pytest.fixture
def fake_flow(oauth_configured, monkeypatch):
    """Replace InstalledAppFlow with a recorder; returns captured kwargs."""
    captured: dict = {}

    class FakeFlow:
        @classmethod
        def from_client_secrets_file(cls, path, scopes):
            captured["path"] = str(path)
            captured["scopes"] = list(scopes)
            return cls()

        def run_local_server(self, **kwargs):
            captured["run_kwargs"] = kwargs
            return FakeCreds(id_token=_email_id_token("teacher@school.org"))

    from google_auth_oauthlib import flow as flow_module
    monkeypatch.setattr(flow_module.InstalledAppFlow,
                        "from_client_secrets_file",
                        FakeFlow.from_client_secrets_file)
    monkeypatch.setattr(FakeFlow, "run_local_server", FakeFlow.run_local_server)
    return captured


def test_connect_requests_offline_and_consent(oauth_configured, fresh_state, fake_flow):
    assert auth.client_configured()          # the real OAuth client JSON exists
    result = auth.start_device_or_loopback_flow()

    # offline+consent is what guarantees Google returns a refresh token
    assert fake_flow["run_kwargs"]["access_type"] == "offline"
    assert fake_flow["run_kwargs"]["prompt"] == "consent"
    assert fake_flow["run_kwargs"].get("port") == 0

    # scopes include classroom + identity (for the connected-account display)
    scopes = fake_flow["scopes"]
    assert any("classroom.courses.readonly" in s for s in scopes)
    assert "openid" in scopes
    assert any("userinfo.email" in s for s in scopes)

    assert result == {"connected": True, "email": "teacher@school.org"}

    # stored, surfaced, and never echoed as a token
    assert auth.is_connected()
    state = auth.auth_state()
    assert state.connected and state.email == "teacher@school.org"
    assert state.connected_at is not None
    public = json.dumps(state.public())
    assert "rt-secret-token" not in public
    assert "ya29" not in public


def test_connect_without_refresh_token_fails_loudly(oauth_configured, fresh_state, monkeypatch):
    class NoRefreshFlow:
        @classmethod
        def from_client_secrets_file(cls, path, scopes):
            return cls()

        def run_local_server(self, **kwargs):
            return FakeCreds(refresh_token=None)

    from google_auth_oauthlib import flow as flow_module
    monkeypatch.setattr(flow_module.InstalledAppFlow,
                        "from_client_secrets_file", NoRefreshFlow.from_client_secrets_file)
    monkeypatch.setattr(NoRefreshFlow, "run_local_server",
                        NoRefreshFlow.run_local_server)

    with pytest.raises(RuntimeError, match="refresh token"):
        auth.start_device_or_loopback_flow()
    assert not auth.is_connected()           # nothing half-stored


def test_disconnect_clears_credentials_and_identity(oauth_configured, fresh_state, fake_flow):
    auth.start_device_or_loopback_flow()
    assert auth.is_connected() and auth.account_email()

    auth.disconnect()
    assert not auth.is_connected()
    assert auth.account_email() is None
    assert auth.connected_at() is None
    assert auth.auth_state().connected is False


def test_unconnected_state_explains_next_step(oauth_configured, fresh_state):
    state = auth.auth_state()
    assert state.connected is False
    assert "Connect" in (state.problem or "")
