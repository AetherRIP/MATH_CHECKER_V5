"""Authentication endpoints for public (tunnel) access.

Local mode needs no account. Public mode requires one: the teacher sets it up
once from the laptop, then every remote request must present a valid session
cookie (enforced by :class:`app.auth_gate.PublicAuthMiddleware`, not by the UI).

Secrets never leave the backend: responses carry only booleans, the username and
a CSRF token — never the password, its hash, or the signing secret.
"""

from __future__ import annotations

from fastapi import APIRouter, Request, Response

from .. import auth_gate as ag
from ..errors import AppError
from ..logging_setup import log_event

router = APIRouter(prefix="/api")


def register(app) -> None:
    app.include_router(router)


def _cookie_options() -> dict:
    return {
        "key": ag.SESSION_COOKIE,
        "httponly": True,
        "samesite": "lax",
        "secure": ag.cookie_secure(),
        "path": "/",
    }


def _current_session(request: Request) -> dict | None:
    session = getattr(request.state, "session", None)
    if session is not None:
        return session
    return ag.read_session(request.cookies.get(ag.SESSION_COOKIE))


def _public_url_hint() -> str | None:
    """The *currently verified* public address (never a stale or internal one)."""
    from ..remote_access import public_url
    return public_url()


@router.get("/health")
def health():
    """Minimal liveness probe — no internal details, no student data."""
    return {"status": "ok"}


@router.get("/remote/status")
def remote_status(request: Request):
    """Remote Access panel data (spec §23): status, addresses, verification.

    Carries no secrets and no student data, so the sign-in screen can show
    the public address too. The address comes from the launcher's verified
    status file — an offline tunnel is reported as offline, never as a
    stale working URL (spec §10, §22).

    The local/LAN rows are proven with a real ``/api/health`` request rather
    than a process check, and the internal LAN address is only disclosed to
    callers already on this machine or its network.
    """
    from ..remote_access import summary
    host = (request.client.host if request.client else "") or ""
    return summary(expose_lan=_is_local_client(host))


def _is_local_client(host: str) -> bool:
    """True for loopback and private-LAN callers (no public disclosure)."""
    import ipaddress

    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        return False
    return bool(addr.is_loopback or addr.is_private or addr.is_link_local)


@router.get("/auth/status")
def status(request: Request):
    session = _current_session(request)
    public = ag.public_mode_enabled()
    configured = ag.credentials_configured()
    identity = ag.session_identity(session) if session else None
    # The main login page shows whenever either mode needs a session AND there
    # is something to sign in to (first-run setup stays reachable).
    required = bool(public or ag.login_required_enabled())
    if not public and required and not ag._has_any_account():
        required = False
    return {
        "public_mode": public,
        "required": required,
        "login_required": ag.login_required_enabled(),
        "configured": bool(configured or ag._has_any_account()),
        "authenticated": identity is not None,
        "username": (identity or {}).get("username") if identity else None,
        "display_name": (identity or {}).get("display_name") if identity else None,
        "role": ag.ui_role((identity or {}).get("role")) if identity else None,
        "account_id": (identity or {}).get("account_id") if identity else None,
        "csrf": (session or {}).get("csrf") if session else None,
        "local_request": ag.is_genuinely_local(request),
        "public_url": _public_url_hint(),
        "local_url": _local_url(),
    }


def _local_url() -> str:
    """Where this server answers on the laptop itself (never the tunnel)."""
    import os
    try:
        port = int(os.environ.get("MC5_PORT") or get_config_port())
    except Exception:  # noqa: BLE001
        port = 8642
    return f"http://127.0.0.1:{port}"


def get_config_port() -> int:
    from ..config import get_config
    return int(get_config().network.port)


@router.post("/auth/login")
def login(request: Request, payload: dict):
    """Sign in to Math Correct (the single main login for the whole app).

    Credentials are checked against, in order:
      1. the Multi-School accounts (the shared multi-user system — the role
         and permissions come from there), then
      2. the legacy single-teacher local account (backwards compatible).

    ``remember_me`` extends the signed cookie's lifetime; without it the
    cookie is a browser-session cookie. The password is never stored by the
    client and the session only carries an identity pointer — role and
    permissions are re-read from the database on every request.
    """
    username = str(payload.get("username") or "")
    password = str(payload.get("password") or "")
    remember = bool(payload.get("remember_me"))

    if not ag._has_any_account():
        raise AppError("No account exists yet. Create one from the laptop "
                       "(first-run setup) and sign in again.",
                       status=409, code="auth_not_configured")
    if ag.login_blocked(request):
        raise AppError("Too many failed attempts. Try again later.",
                       status=429, code="too_many_attempts")

    account = None
    try:
        from ..distributed import accounts as dist_accounts
        if dist_accounts.account_count() > 0:
            account = dist_accounts.authenticate(username, password)
    except Exception:  # noqa: BLE001 — tables may not exist yet
        account = None

    if account is not None:
        ag.clear_login_failures(request)
        hours = 24 * 30 if remember else _session_hours()
        token, csrf = ag.issue_session(account["username"], hours=hours,
                                       account_id=account["id"])
        log_event("security", "signed in", actor=account["username"],
                  role=account["role"], remember=remember)
        return _session_response(token, csrf, remember, hours,
                                 role=ag.ui_role(account["role"]))

    # Legacy single-teacher account (public-mode setups from earlier versions).
    if ag.credentials_configured() and ag.verify_credentials(username, password):
        ag.clear_login_failures(request)
        hours = 24 * 30 if remember else _session_hours()
        token, csrf = ag.issue_session(ag.configured_username() or username,
                                       hours=hours)
        log_event("security", "signed in", actor=ag.configured_username())
        return _session_response(token, csrf, remember, hours)

    ag.record_login_failure(request)
    log_event("security", "failed sign-in attempt", level=40)
    # Generic message: never reveal whether the username exists.
    raise AppError("Incorrect username or password.", status=401,
                   code="invalid_credentials")


def _session_response(token: str, csrf: str, remember: bool, hours: int,
                      role: str | None = None) -> Response:
    """Set the signed session cookie.

    ``remember_me`` -> persistent cookie (max-age = hours); otherwise a
    browser-session cookie that disappears when the browser closes. The value
    is the same HMAC-signed token either way — no password, no token store.
    """
    response = Response(content="", status_code=204)
    options = {k: v for k, v in _cookie_options().items() if k != "max_age"}
    response.set_cookie(value=token,
                        max_age=(hours * 3600 if remember else None), **options)
    response.headers["X-MC5-CSRF"] = csrf
    if role:
        response.headers["X-MC5-ROLE"] = role
    return response


def _session_hours() -> int:
    try:
        from ..config import get_config
        return int(get_config().security.session_hours)
    except Exception:  # noqa: BLE001
        return ag.SESSION_MAX_HOURS


@router.post("/auth/logout")
def logout():
    response = Response(content="", status_code=204)
    response.delete_cookie(ag.SESSION_COOKIE, path="/")
    return response


def _local_only(request: Request, action: str) -> None:
    """First-time security changes must originate on the laptop itself."""
    if ag.is_genuinely_local(request):
        return
    raise AppError(
        f"{action} must be done on the laptop running Math Checker (open "
        f"http://127.0.0.1 locally), not through the public address.",
        status=403, code="local_only")


@router.post("/auth/setup")
def setup(request: Request, payload: dict):
    """Create (or replace) the teacher account for public access.

    A genuinely local, unproxied request may (re)set the password. This is the
    deliberate bootstrap path so a public URL can never be used to take over the
    account. When public mode is on and an account already exists, the current
    password is required.
    """
    # An ABSENT/blank username keeps the existing one (and only the very first
    # account falls back to "teacher"). Otherwise a client that only meant to
    # change the password — for example the Settings card, which deliberately
    # never displays the stored username to an unauthenticated caller — would
    # silently RENAME the host account as a side effect.
    provided = str(payload.get("username") or "").strip()
    username = provided or (ag.configured_username()
                            if ag.credentials_configured() else None) or "teacher"
    password = str(payload.get("password") or "")
    if len(password) < 10:
        raise AppError("Choose a password of at least 10 characters.",
                       status=400, code="weak_password")

    # Same throttle as sign-in: this route can be reached without a session, so
    # the "current password" check below must not become a brute-force oracle.
    if ag.login_blocked(request):
        raise AppError("Too many failed attempts. Try again later.",
                       status=429, code="too_many_attempts")

    configured = ag.credentials_configured()
    if configured:
        # Changing an existing account requires the current password, even
        # locally, so a stray local process cannot silently replace it.
        current = str(payload.get("current_password") or "")
        if not ag.verify_credentials(ag.configured_username() or username, current):
            ag.record_login_failure(request)
            log_event("security", "failed account change attempt", level=40)
            raise AppError("Current password is incorrect.", status=401,
                           code="invalid_credentials")
    else:
        _local_only(request, "Setting the public-access password")

    ag.set_credentials(username, password)
    ag.reset_throttle()
    return {"configured": True, "username": ag.configured_username()}


@router.get("/auth/me")
def me(request: Request):
    session = _current_session(request)
    if session is None:
        raise AppError("Not signed in.", status=401, code="auth_required")
    return {"username": session.get("u"), "csrf": session.get("csrf")}
