"""Google OAuth for Classroom — reuses the friend's existing OAuth client.

The client secret JSON that sat at the repo root has been moved into
``data/secrets/google_oauth_client.json`` (git-ignored). V5 runs an
InstalledAppFlow against it, persists the refresh token in the secret store,
and refreshes access tokens silently thereafter.

Scopes mirror the legacy app exactly (courses, coursework.students, drive,
rosters) so the preserved REST wrappers keep working.
"""

from __future__ import annotations

import logging
import time

from .. import config as app_config
from ..config import get_config, get_secrets
from ..logging_setup import log_event

log = logging.getLogger("classroom.auth")

SCOPES = [
    "https://www.googleapis.com/auth/classroom.courses.readonly",
    "https://www.googleapis.com/auth/classroom.coursework.students",
    "https://www.googleapis.com/auth/drive.readonly",
    # Uploading the generated correction report back to Drive so it can be
    # attached to the student's Classroom submission. drive.file is the
    # NARROWEST scope that allows this: it grants access ONLY to files this
    # app creates, so we can never read or modify the teacher's other Drive
    # files (drive.readonly above stays for reading student submissions).
    "https://www.googleapis.com/auth/drive.file",
    "https://www.googleapis.com/auth/classroom.rosters.readonly",
    # V5 addition: lets the app show WHICH Google account is connected
    # (email is stored locally; it is never treated as a secret, never sent
    # anywhere except rendered in the Classroom header).
    "openid",
    "https://www.googleapis.com/auth/userinfo.email",
]


class AuthState:
    """Small struct returned by helpers so the API never leaks token values."""

    def __init__(self, connected: bool, email: str | None = None,
                 problem: str | None = None, client_configured: bool = False,
                 connected_at: float | None = None):
        self.connected = connected
        self.email = email
        self.problem = problem
        self.client_configured = client_configured
        self.connected_at = connected_at

    def public(self) -> dict:
        return {
            "connected": self.connected,
            "email": self.email,
            "connected_at": self.connected_at,
            "problem": self.problem,
            "client_configured": self.client_configured,
        }


def client_configured() -> bool:
    # Resolve the client JSON dynamically (single source of truth): the
    # V5 path, or a teacher-placed client_secret_*.json that the teacher may
    # have added to the project root.
    secrets = get_secrets()
    if app_config.resolve_oauth_client_path().exists() and secrets.oauth_client_config():
        return True
    return False


def get_access_token() -> str:
    """Return a valid access token, refreshing silently when needed.

    Raises RuntimeError with a user-appropriate message on failure.
    """
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials

    secrets = get_secrets()
    client_cfg = secrets.oauth_client_config()
    if not client_cfg:
        raise RuntimeError(
            "Google OAuth client is not configured. Place the OAuth client JSON "
            "in data/secrets/google_oauth_client.json."
        )
    installed = client_cfg.get("installed") or client_cfg.get("web") or {}
    refresh_token = secrets.oauth_refresh_token()
    if not refresh_token:
        raise RuntimeError(
            "Google Classroom is not connected yet. Start the connection flow "
            "from the Classroom page."
        )

    creds = Credentials(
        token=None,
        refresh_token=refresh_token,
        token_uri=installed.get("token_uri", "https://oauth2.googleapis.com/token"),
        client_id=installed.get("client_id"),
        client_secret=installed.get("client_secret"),
        scopes=SCOPES,
    )
    try:
        creds.refresh(Request())
    except Exception as exc:
        # A stored refresh token is bound to the scope set it was GRANTED.
        # When the app asks for a new scope (e.g. drive.file for uploading
        # correction reports), Google refuses to extend it silently and answers
        # "invalid_scope" - which is NOT an expiry and must not be reported as
        # one, or the teacher reconnects for the wrong reason.
        detail = str(exc).lower()
        if "invalid_scope" in detail:
            raise RuntimeError(
                "Google Classroom needs to be reconnected to grant the "
                "permission that uploads correction reports to Drive "
                "(drive.file). Click Connect Google Classroom on the "
                "Classroom page and approve the new permission."
            ) from exc
        raise RuntimeError(
            "Google authorization expired or was revoked. Reconnect Google "
            "Classroom from the Classroom page."
        ) from exc

    log_event("classroom.auth", "access token refreshed", fingerprint_user=creds.token[:0])
    return creds.token


def is_connected() -> bool:
    secrets = get_secrets()
    return bool(secrets.oauth_refresh_token())


def start_device_or_loopback_flow() -> dict:
    """Run the console OAuth flow (loopback server when possible).

    Returns a public dict; the refresh token is stored in the secret store
    and never echoed. The account email (if Google returns one) is stored in
    ``app_meta`` so the UI can show which account is connected.
    """
    from google_auth_oauthlib.flow import InstalledAppFlow

    from .. import db

    if not client_configured():
        raise RuntimeError(
            "Google OAuth client is not configured (data/secrets/"
            "google_oauth_client.json missing)."
        )

    flow = InstalledAppFlow.from_client_secrets_file(
        str(app_config.resolve_oauth_client_path()), SCOPES)
    # access_type=offline is what yields a REFRESH token (silent re-auth later);
    # prompt=consent guarantees Google actually returns one — without it a
    # re-consent-skipping account can "successfully" sign in with no refresh
    # token, which looks like a broken connection on the very next request.
    creds = flow.run_local_server(
        port=0, open_browser=True, timeout_seconds=300,
        access_type="offline", prompt="consent",
    )
    if not creds.refresh_token:
        raise RuntimeError(
            "Google did not return a refresh token, so the connection cannot "
            "be kept. Please try again and approve ALL requested permissions."
        )
    get_secrets().set_oauth_refresh_token(creds.refresh_token)

    email = _fetch_account_email(creds)
    db.set_meta("classroom_account_email", email or "")
    db.set_meta("classroom_connected_at", str(time.time()))
    log_event("classroom.auth", "OAuth completed; refresh token stored",
              account=email or "unknown")
    return {"connected": True, "email": email}


def _fetch_account_email(creds) -> str | None:
    """Best-effort: resolve the signed-in account's email address."""
    # 1) id_token from the token response (openid scope)
    try:
        token = getattr(creds, "id_token", None)
        if token:
            import base64
            import json
            payload = token.split(".")[1]
            payload += "=" * (-len(payload) % 4)
            data = json.loads(base64.urlsafe_b64decode(payload))
            email = data.get("email")
            if email:
                return str(email)
    except Exception:  # noqa: BLE001
        pass
    # 2) userinfo endpoint with the fresh access token
    try:
        import urllib.request
        from google.auth.transport.requests import Request
        creds.refresh(Request())
        request = urllib.request.Request(
            "https://www.googleapis.com/oauth2/v3/userinfo",
            headers={"Authorization": f"Bearer {creds.token}"},
        )
        with urllib.request.urlopen(request, timeout=15) as response:
            import json
            data = json.loads(response.read().decode("utf-8"))
        return str(data.get("email")) if data.get("email") else None
    except Exception:  # noqa: BLE001
        return None


def disconnect() -> None:
    from .. import db
    get_secrets().set_oauth_refresh_token(None)
    db.set_meta("classroom_account_email", "")
    db.set_meta("classroom_connected_at", "")
    log_event("classroom.auth", "disconnected")


def account_email() -> str | None:
    from .. import db
    value = db.get_meta("classroom_account_email") or ""
    return value or None


def connected_at() -> float | None:
    from .. import db
    raw = db.get_meta("classroom_connected_at")
    try:
        return float(raw) if raw else None
    except (TypeError, ValueError):
        return None


def auth_state() -> AuthState:
    configured = client_configured()
    connected = is_connected()
    problem = None
    if not configured:
        problem = (
            "OAuth client JSON missing — copy the Google client secret to "
            "data/secrets/google_oauth_client.json."
        )
    elif not connected:
        problem = "Not connected. Click Connect Google Classroom."
    return AuthState(
        connected=connected and configured,
        email=account_email() if connected else None,
        problem=problem,
        client_configured=configured,
        connected_at=connected_at() if connected else None,
    )
