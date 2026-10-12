"""Public-access security gate: authentication, sessions, CSRF, rate limiting.

Why this exists
---------------
Math Checker runs on the teacher's laptop and is reachable publicly through an
outbound tunnel (Cloudflare Tunnel). A tunnel removes the need for router port
forwarding, but it does NOT authenticate anyone — so the application itself must
enforce authentication and authorization server-side.

Design rules
------------
* **Fail closed.** In public mode with no credentials configured, every API
  request is refused instead of being served.
* **Local use is unchanged.** When public mode is off the gate is a no-op, so
  `localhost` keeps working exactly as before (one codebase, two modes).
* **Stdlib only.** PBKDF2 password hashing + HMAC-signed session cookies, so the
  installed runtime needs no new dependencies.
* **Secrets never leave the backend.** The password hash/salt and the session
  signing secret live only in ``data/secrets.toml``; no endpoint ever returns
  them, and they are never written to logs, URLs or the frontend.

Local vs remote
---------------
A genuinely local request arrives at 127.0.0.1 with no proxy headers. A tunnel
always adds ``CF-Connecting-IP`` / ``X-Forwarded-For``. That distinction is the
only thing used to decide whether first-time local setup is allowed; it is a
convenience signal, never an authorization decision (authorization is always the
session cookie).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import secrets as _secrets
import threading
import time

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

from .logging_setup import log_event

log = logging.getLogger("security.auth")

SESSION_COOKIE = "mc5_session"
CSRF_HEADER = "x-mc5-csrf"
PBKDF2_ITERATIONS = 200_000
SESSION_MAX_HOURS = 12

# Login throttling (in-memory, per client address). Deliberately small: this is
# a single-teacher private server, not a public multi-tenant service.
_MAX_FAILURES = 8
_FAILURE_WINDOW_SECONDS = 15 * 60
_failures: dict[str, list[float]] = {}
_fail_lock = threading.Lock()

# Paths that must stay reachable without a session while public mode is on.
#
# ``/api/auth/setup`` is here out of necessity: public mode is turned on by
# configuration, so the gate also applies to the laptop's own localhost traffic.
# If setup were gated, the teacher could never create the first account and the
# application would be permanently locked out. The route itself is the guard —
# it is local-only while no account exists and demands the current password
# afterwards (both rate-limited).
_PUBLIC_PATHS = {
    "/api/auth/status",
    "/api/auth/login",
    "/api/auth/logout",
    "/api/auth/setup",
    "/api/health",
    # Remote-access status: addresses and timestamps only — no student data,
    # no secrets. The sign-in screen needs it to show the public address.
    "/api/remote/status",
}
_SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}

# Interactive API documentation and the OpenAPI schema describe every endpoint,
# parameter and model. That is a developer convenience, not public information,
# so it is hidden the moment the app is reachable from the internet (§20/§21).
_DEBUG_PATHS = {"/docs", "/docs/", "/redoc", "/redoc/", "/openapi.json",
                "/docs/oauth2-redirect"}


# ------------------------------------------------------------------ config

def public_mode_enabled() -> bool:
    """True when the app is exposed publicly and must authenticate requests."""
    import os
    if os.environ.get("MC5_PUBLIC") == "1":
        return True
    from .config import get_config
    try:
        return bool(get_config().security.enabled)
    except Exception:  # noqa: BLE001 — a config failure must not open the gate
        return True


def login_required_enabled() -> bool:
    """True when LOCAL use also demands a sign-in session (main login page).

    ``MC5_LOGIN_REQUIRED`` is an explicit environment override so tests,
    packaging smoke runs and emergency local access never depend on editing a
    config file.
    """
    import os
    env = os.environ.get("MC5_LOGIN_REQUIRED")
    if env in ("0", "1"):
        return env == "1"
    from .config import get_config
    try:
        return bool(get_config().security.login_required)
    except Exception:  # noqa: BLE001 — a config failure must not open the gate
        return False


def gate_enabled() -> bool:
    """The session gate runs when either mode needs it."""
    return public_mode_enabled() or login_required_enabled()


def _has_any_account() -> bool:
    """Is there anything to authenticate against? (first-run detection)"""
    try:
        from .distributed import accounts
        if accounts.account_count() > 0:
            return True
    except Exception:  # noqa: BLE001 — missing tables == no accounts yet
        pass
    return credentials_configured()


def _secrets_store():
    from .config import get_secrets
    return get_secrets()


# ------------------------------------------------------------------ credentials

def _hash_password(password: str, salt: bytes) -> bytes:
    return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt,
                               PBKDF2_ITERATIONS, dklen=32)


def credentials_configured() -> bool:
    creds = _secrets_store().security_credentials()
    return bool(creds and creds.get("password_hash") and creds.get("session_secret"))


def set_credentials(username: str, password: str) -> None:
    """Create/replace the single teacher account (writes secrets.toml only)."""
    username = (username or "teacher").strip()[:64] or "teacher"
    salt = _secrets.token_bytes(16)
    digest = _hash_password(password, salt)
    store = _secrets_store()
    existing = store.security_credentials() or {}
    store.set_security_credentials({
        "username": username,
        "password_salt": base64.b64encode(salt).decode("ascii"),
        "password_hash": base64.b64encode(digest).decode("ascii"),
        # Keep an existing signing secret so enabling this does not log the
        # teacher out unnecessarily; generate one when absent.
        "session_secret": existing.get("session_secret")
        or base64.b64encode(_secrets.token_bytes(32)).decode("ascii"),
    })
    log_event("security", "public access credentials configured", username=username)


def verify_credentials(username: str, password: str) -> bool:
    creds = _secrets_store().security_credentials()
    if not creds or not creds.get("password_hash"):
        return False
    try:
        salt = base64.b64decode(creds.get("password_salt", ""))
        expected = base64.b64decode(creds["password_hash"])
    except (ValueError, TypeError):
        return False
    stored_user = str(creds.get("username", "teacher"))
    candidate = _hash_password(password or "", salt)
    # Constant-time on BOTH the digest and the username.
    ok = hmac.compare_digest(candidate, expected)
    same_user = hmac.compare_digest(
        stored_user.encode("utf-8"), (username or "").encode("utf-8"))
    return ok and same_user


def configured_username() -> str | None:
    creds = _secrets_store().security_credentials()
    return str(creds["username"]) if creds and creds.get("username") else None


# ------------------------------------------------------------------ sessions

def _session_secret() -> bytes | None:
    creds = _secrets_store().security_credentials() or {}
    raw = creds.get("session_secret")
    if raw:
        try:
            return base64.b64decode(raw)
        except (ValueError, TypeError):
            pass
    # No signing secret yet: the main login page needs one even when the
    # legacy single-teacher password was never configured (Multi-School
    # accounts authenticate separately). Generate and PERSIST it so sessions
    # survive a restart — the secret never leaves the secret store.
    import os
    secret = _secrets.token_bytes(32)
    payload = dict(creds)
    payload["session_secret"] = base64.b64encode(secret).decode("ascii")
    try:
        _secrets_store().set_security_credentials(payload)
        log_event("security", "session signing secret created")
    except OSError:
        # Read-only location: keep working for this process rather than
        # refusing every sign-in (the next restart will try again).
        log_event("security", "session signing secret not persisted",
                  level=logging.WARNING)
    return secret


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _unb64(text: str) -> bytes:
    pad = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + pad)


def issue_session(username: str, *, hours: int = SESSION_MAX_HOURS,
                  account_id: str | None = None) -> tuple[str, str]:
    """Return ``(cookie_value, csrf_token)`` for a freshly authenticated user."""
    secret = _session_secret()
    if not secret:
        raise RuntimeError("No session secret is configured.")
    now = int(time.time())
    payload = {"u": username, "iat": now, "exp": now + hours * 3600,
               "csrf": _secrets.token_urlsafe(24)}
    if account_id:
        payload["aid"] = account_id
    body = _b64(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    sig = _b64(hmac.new(secret, body.encode("ascii"), hashlib.sha256).digest())
    return f"{body}.{sig}", payload["csrf"]


def read_session(token: str | None) -> dict | None:
    """Validate a cookie value and return its payload, or ``None`` if invalid."""
    if not token or "." not in token:
        return None
    secret = _session_secret()
    if not secret:
        return None
    body, _, sig = token.partition(".")
    expected = _b64(hmac.new(secret, body.encode("ascii"), hashlib.sha256).digest())
    if not hmac.compare_digest(expected, sig):
        return None
    try:
        payload = json.loads(_unb64(body).decode("utf-8"))
    except Exception:  # noqa: BLE001 — malformed token is simply unauthenticated
        return None
    if not isinstance(payload, dict) or int(payload.get("exp", 0)) < int(time.time()):
        return None
    return payload


# ------------------------------------------------------------------ identity
#: Distributed role  ->  the lowercase role string the app UI has always used.
ROLE_UI = {"DEVELOPER": "developer", "TEACHER": "teacher",
           "CO_TEACHER": "coteacher", "ASSISTANT": "assistant"}
#: The legacy local config role -> the normalised role used for checks.
ROLE_FROM_CONFIG = {"developer": "DEVELOPER", "coteacher": "CO_TEACHER",
                    "assistant": "ASSISTANT", "teacher": "TEACHER"}


def ui_role(role: str | None) -> str:
    """Normalised lowercase role for the UI (never trusts the client)."""
    if not role:
        return "teacher"
    role = str(role)
    if role.upper() in ROLE_UI:
        return ROLE_UI[role.upper()]
    return ROLE_FROM_CONFIG.get(role.lower(), "teacher")


def session_identity(session: dict | None) -> dict | None:
    """Resolve a signed session into a LIVE identity, or ``None``.

    Roles and permissions are read from the database on every request, so a
    disabled account or a changed permission list takes effect immediately —
    the cookie only proves *who* signed in, never *what* they may do.

    * distributed account session (``aid``) -> account row (must be active)
    * legacy single-teacher session         -> configured local credentials
    """
    if not session:
        return None
    account_id = session.get("aid")
    if account_id:
        try:
            from .distributed import accounts
            row = accounts.get_account(str(account_id))
        except Exception:  # noqa: BLE001 — missing tables = no valid identity
            return None
        if not row or not row.get("active"):
            return None
        import json as _json
        try:
            permissions = _json.loads(row.get("permissions") or "[]")
        except ValueError:
            permissions = []
        return {
            "username": row.get("username"),
            "display_name": row.get("display_name") or row.get("username"),
            "account_id": row.get("id"),
            "role": str(row.get("role") or "ASSISTANT").upper(),
            "permissions": permissions,
            "source": "account",
        }
    # Legacy local single-teacher session.
    if credentials_configured():
        username = str(session.get("u") or "")
        if not username or username != configured_username():
            return None
        from .config import get_config
        role = ROLE_FROM_CONFIG.get(
            str(get_config().role).lower(), "TEACHER")
        return {"username": username, "display_name": username,
                "account_id": None, "role": role, "permissions": ["*"],
                "source": "local"}
    return None


def identity_of(request) -> dict | None:
    """The live identity of this request (set by the gate, else from cookie)."""
    identity = getattr(request.state, "identity", None)
    if identity is not None:
        return identity
    session = getattr(request.state, "session", None)
    if session is None:
        session = read_session(request.cookies.get(SESSION_COOKIE))
    return session_identity(session)


def effective_role(request) -> str:
    """Role for authorization: the signed-in account, else the local config.

    With the gate off (shared local machine) the configured local role keeps
    enforcing the same hierarchy the UI shows — a client never declares its
    own role.
    """
    identity = identity_of(request)
    if identity:
        return identity["role"]
    from .config import get_config
    return ROLE_FROM_CONFIG.get(str(get_config().role).lower(), "TEACHER")


def require_role(request, *roles: str) -> None:
    """Raise 403 unless the caller's live role is one of ``roles`` (upper)."""
    wanted = {r.upper() for r in roles}
    actual = effective_role(request)
    if actual not in wanted:
        from .errors import AppError
        raise AppError(
            f"This action requires the {'/'.join(sorted(wanted))} role "
            f"(you are {actual}).", status=403, code="forbidden")


def require_permission(request, permission: str) -> None:
    """Raise 403 unless the caller's live permissions allow ``permission``.

    With no session (gate off) the local config role decides, using the same
    default permission map as the distributed accounts.
    """
    identity = identity_of(request)
    if identity is None:
        from .config import get_config
        from .distributed.accounts import DEFAULT_PERMISSIONS
        role = ROLE_FROM_CONFIG.get(str(get_config().role).lower(), "TEACHER")
        perms = DEFAULT_PERMISSIONS.get(role, [])
    else:
        perms = list(identity.get("permissions") or [])
    if "*" in perms or permission in perms:
        return
    from .errors import AppError
    raise AppError(
        f"Your role does not allow '{permission}'.", status=403,
        code="forbidden")


# ------------------------------------------------------------------ throttling

def _client_key(request) -> str:
    client = request.client
    return client.host if client and client.host else "unknown"


def login_blocked(request) -> bool:
    key = _client_key(request)
    now = time.time()
    with _fail_lock:
        attempts = [t for t in _failures.get(key, []) if now - t < _FAILURE_WINDOW_SECONDS]
        _failures[key] = attempts
        return len(attempts) >= _MAX_FAILURES


def record_login_failure(request) -> None:
    key = _client_key(request)
    with _fail_lock:
        _failures.setdefault(key, []).append(time.time())


def clear_login_failures(request) -> None:
    with _fail_lock:
        _failures.pop(_client_key(request), None)


def reset_throttle() -> None:
    """Forget all recorded failures (test isolation, and after a password change)."""
    with _fail_lock:
        _failures.clear()


# ------------------------------------------------------------------ local check

def is_genuinely_local(request) -> bool:
    """A direct loopback request with no tunnel proxy headers.

    Used ONLY to allow first-time credential setup and never for authorization.
    """
    client = request.client
    host = (client.host if client else "") or ""
    loopback = host in ("127.0.0.1", "::1", "localhost")
    proxied = any(h in request.headers for h in
                  ("cf-connecting-ip", "x-forwarded-for", "x-real-ip"))
    return loopback and not proxied


# ------------------------------------------------------------------ middleware

class PublicAuthMiddleware(BaseHTTPMiddleware):
    """Enforce authentication + CSRF on every API request when the gate is on."""

    async def dispatch(self, request, call_next):
        if not gate_enabled():
            return await call_next(request)  # gate off: unchanged behaviour

        public = public_mode_enabled()
        path = request.url.path
        if path in _PUBLIC_PATHS:
            return await call_next(request)

        # Distributed layer (§59): a self-authenticating API. Tiny agents and
        # remote dashboards authenticate with per-account bearer tokens — not
        # the app session cookie — so the session gate must not stand in front
        # of it, or Assistants could never reach the backend from their own
        # machines. Every /api/distributed route enforces its own auth:
        # _account_from_header() rejects invalid/missing tokens (401), /login
        # verifies credentials server-side, and /heartbeat is presence-only
        # metadata that can never claim an account. Bearer auth cannot be
        # CSRF'd (no ambient cookie), so the CSRF check does not apply here.
        if path == "/api/distributed" or path.startswith("/api/distributed/"):
            return await call_next(request)

        # Non-API requests (the SPA shell and its assets) are allowed to load so
        # the browser can show the sign-in screen; they carry no student data.
        if not path.startswith("/api"):
            if path.rstrip("/") in _DEBUG_PATHS or path in _DEBUG_PATHS:
                return JSONResponse(
                    status_code=404,
                    content={"code": "not_found", "message": "Not found."},
                )
            return await call_next(request)

        if public and not credentials_configured() and not _has_any_account():
            # Fail closed: never serve data when public mode has NOTHING to
            # authenticate against. Multi-School accounts count here — the
            # sign-in route accepts them — so an installation that never set a
            # legacy local password can still authenticate, and switching
            # public access on cannot lock the owner out of their own app with
            # a 503 while there are accounts to sign in with.
            return JSONResponse(
                status_code=503,
                content={"code": "auth_not_configured",
                         "message": ("Public access is enabled but no account is "
                                     "configured. Set a password locally, then "
                                     "reconnect the tunnel.")},
            )

        if not public and not _has_any_account():
            # First run with nothing to authenticate against: the setup flow
            # must stay reachable or the app could never be initialised.
            return await call_next(request)

        session = read_session(request.cookies.get(SESSION_COOKIE))
        if session is None:
            return JSONResponse(
                status_code=401,
                content={"code": "auth_required",
                         "message": "Sign in to Math Correct to continue."},
            )

        # A session whose account was disabled or deleted stops working on the
        # very next request — role and permissions are always read live.
        identity = session_identity(session)
        if identity is None:
            response = JSONResponse(
                status_code=401,
                content={"code": "auth_invalid",
                         "message": "This account is no longer active. "
                                    "Sign in again."},
            )
            response.delete_cookie(SESSION_COOKIE, path="/")
            return response

        if request.method.upper() not in _SAFE_METHODS:
            supplied = request.headers.get(CSRF_HEADER, "")
            if not supplied or not hmac.compare_digest(str(session.get("csrf", "")),
                                                       supplied):
                return JSONResponse(
                    status_code=403,
                    content={"code": "csrf_failed",
                             "message": "This request could not be verified. "
                                        "Reload the page and try again."},
                )

        request.state.session = session
        request.state.identity = identity
        return await call_next(request)


def cookie_secure() -> bool:
    """Send the session cookie only over HTTPS when publicly exposed."""
    return public_mode_enabled()
