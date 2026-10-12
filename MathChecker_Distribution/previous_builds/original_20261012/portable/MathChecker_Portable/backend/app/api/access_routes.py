"""Access controls (the two switches in Settings that change exposure).

Two settings live here rather than in the general ``/api/settings`` payload,
because both change what the installation *is* rather than how it marks:

* :func:`access_state`      — read both switches (never a secret, never a URL
  that is not verifiably online);
* :func:`set_public_address` — switch the app-managed public endpoint on/off
  (the real tunnel is started/stopped by ``app.public_access``);
* :func:`set_multi_account`  — enable/disable the multi-account layer
  (``app.multi_account``; nothing is deleted either way).

Authorization is enforced HERE, server-side: either the request comes straight
from this computer (the designated local owner, no proxy headers — see
``auth_gate.is_genuinely_local``), or it carries a signed session whose account
is a Developer. Everything else is refused, so a LAN peer or a public caller can
never expose this installation by flipping a switch.
"""
from __future__ import annotations

from fastapi import APIRouter, Request

from ..errors import AppError
from ..logging_setup import log_event

router = APIRouter(prefix="/api", tags=["access"])


def _owner_identity(request: Request) -> str:
    """Who is flipping the switch, or raise 403.

    Order matters: a genuinely local request is the installation's own owner
    (this is the single-account case, where there may be no session at all),
    and anything else must prove it is a signed-in Developer.
    """
    from .. import auth_gate as ag
    if ag.is_genuinely_local(request):
        return "local"
    session = ag.read_session(request.cookies.get(ag.SESSION_COOKIE))
    identity = ag.session_identity(session) if session else None
    role = (identity or {}).get("role")
    if identity and ag.ui_role(role) == "developer":
        return str(identity.get("username") or "developer")
    raise AppError(
        "Only the installation's own machine (or a signed-in Developer "
        "account) can change public access and multi-account settings.",
        status=403, code="owner_required")


@router.get("/access/state")
def access_state(request: Request):
    """Both switches, with the requested setting and the measured state."""
    from .. import multi_account
    from ..public_access import status as public_access_status
    owner = None
    try:
        owner = _owner_identity(request)
    except AppError:
        owner = None
    state = public_access_status() if owner else None
    return {
        "owner": owner,
        "public_address": state,
        "multi_account": multi_account.capabilities(),
    }


@router.post("/access/public-address")
def set_public_address(payload: dict, request: Request):
    """Turn the PUBLIC ADDRESS on/off (this is the endpoint's real switch).

    ON  -> a tunnel is created against this server, verified end to end, and
           only then published (any failure returns the setting to OFF with a
           reason).
    OFF -> the tunnel process tree is stopped and the offline status file is
           written, so nothing in the app advertises the address. The address
           itself stays remembered for when it is switched on again.
    """
    from .. import public_access
    actor = _owner_identity(request)
    if "enabled" not in payload:
        raise AppError("Provide 'enabled': true or false.",
                       status=400, code="bad_request")
    wanted = bool(payload.get("enabled"))
    log_event("security", "public address switch", actor=actor,
              enabled=wanted)
    state = public_access.enable() if wanted else public_access.disable()
    from .. import multi_account
    return {"public_address": state,
            "multi_account": multi_account.capabilities()}


@router.post("/access/multi-account")
def set_multi_account(payload: dict, request: Request):
    """Turn MULTI-ACCOUNT MODE on/off (nothing is deleted either way)."""
    from .. import multi_account
    actor = _owner_identity(request)
    if "enabled" not in payload:
        raise AppError("Provide 'enabled': true or false.",
                       status=400, code="bad_request")
    wanted = bool(payload.get("enabled"))
    multi_account.set_enabled(wanted)
    log_event("security", "multi-account mode switch", actor=actor,
              enabled=wanted)
    return {"multi_account": multi_account.capabilities()}


@router.post("/access/public-address/recheck")
def recheck_public_address(request: Request):
    """Re-verify the live address now (the Settings "Re-check" action)."""
    from .. import public_access
    _owner_identity(request)
    return {"public_address": public_access.refresh()}


def register(app) -> None:
    app.include_router(router)
