"""Distributed REST API routes (§48, §59).

Every permission decision is made SERVER-SIDE from the bearer token (§59):
the client never declares its role.
"""
from __future__ import annotations

import base64
import binascii
import json
import time

from fastapi import APIRouter, Header, Response

from .. import db
from ..errors import AppError
from ..version import APP_VERSION
from . import (accounts, agent_control, agents, audit, discovery, org, sync,
               teams, updates)

router = APIRouter(prefix="/api/distributed")


# -- connectivity probe (public, and deliberately boring) ------------------------

@router.get("/ping")
def ping():
    """Is this really the shared backend, and which build is it?

    The ONE unauthenticated route of the distributed layer, so an installation
    can prove the backend it was pointed at is reachable BEFORE it has a token
    (and so the operator has a one-line check: ``curl <url>/api/distributed/ping``).
    It answers with the service name and version and nothing else — no counts,
    no account or device names, no paths, no errors — so it cannot be used to
    enumerate a school. It is also why the UI can never show "connected" just
    because the page loaded: the badge follows a real HTTP answer from here.
    """
    return {"ok": True, "service": "math-checker-distributed",
            "version": APP_VERSION}


@router.get("/capabilities")
def capabilities():
    """Which optional layers this installation offers (public, no secrets).

    The login screen and the dashboard read this BEFORE a sign-in to decide
    whether to render multi-account surfaces. While multi-account mode is off
    the account-administration and team endpoints are refused by the backend
    as well (403 ``multi_account_disabled``) — hiding a panel is a courtesy,
    never the protection.
    """
    from .. import multi_account
    return multi_account.capabilities()


def _account_from_header(authorization: str | None,
                         x_device_id: str | None = None) -> dict:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise AppError("Authentication required (bearer token).", status=401,
                       code="auth_required")
    token = authorization.split(None, 1)[1].strip()
    account = accounts.resolve_token(token)
    if account is None:
        raise AppError("Invalid or expired token.", status=401,
                       code="auth_invalid")
    if x_device_id:
        db.execute("UPDATE auth_tokens SET device_id = ? WHERE token_hash = ?",
                   (x_device_id, __import__("hashlib").sha256(
                       token.encode()).hexdigest()))
    return account


def _require(account: dict, permission: str) -> None:
    if not accounts.has_permission(account, permission):
        # Report every role the account holds, not just the primary one, so a
        # multi-role user gets an accurate message.
        held = ", ".join(accounts.account_roles(account)) or account["role"]
        raise AppError(
            f"Your roles ({held}) do not allow '{permission}'.",
            status=403, code="forbidden")


# -- auth -----------------------------------------------------------------------

@router.post("/login")
def login(payload: dict):
    account = accounts.authenticate(payload.get("username", ""),
                                    payload.get("password", ""))
    if account is None:
        raise AppError("Invalid username or password.", status=401,
                       code="bad_credentials")
    device_id = payload.get("device_id")
    token = accounts.issue_token(account["id"], device_id)
    db.execute("UPDATE accounts SET last_login = ?, last_device_id = ? WHERE id = ?",
               (db.query_one("SELECT 1 AS one") and __import__("time").time(),
                device_id, account["id"]))
    schools, classes = accounts.scoped_ids(account)
    roles = accounts.account_roles(account)
    return {
        "token": token, "account_id": account["id"],
        "username": account["username"], "display_name": account["display_name"],
        # `role` stays the PRIMARY role (unchanged contract); `roles` carries the
        # full set so a Developer+Assistant can switch dashboards (§6-§10).
        "role": account["role"],
        "roles": roles,
        "permissions": accounts.permissions_for(account),
        "scope": {"school_ids": sorted(schools), "class_ids": sorted(classes)},
        "app_version": APP_VERSION,
    }


@router.post("/change-password")
def change_password(payload: dict, authorization: str | None = Header(None),
                    x_device_id: str | None = Header(None)):
    """Self-service password change for the signed-in Multi-School account.

    The caller must prove they know the CURRENT password. On success every
    other session for the account is revoked (a stolen token stops working)
    and a fresh token is issued so this caller stays signed in. Reserved for
    the account's own credentials — never another user's.
    """
    account = _account_from_header(authorization, x_device_id)
    current = payload.get("current_password", "")
    new_password = payload.get("new_password", "")
    if not accounts.verify_password(current, account["password_salt"],
                                    account["password_hash"]):
        raise AppError("The current password is incorrect.", status=401,
                       code="bad_credentials")
    try:
        accounts.set_password(account["id"], new_password)
    except ValueError as exc:
        raise AppError(str(exc), status=400, code="bad_request")
    accounts.revoke_all_tokens(account["id"])
    token = accounts.issue_token(account["id"], x_device_id)
    return {"ok": True, "token": token}


@router.post("/logout")
def logout(payload: dict, authorization: str | None = Header(None)):
    account = _account_from_header(authorization)
    accounts.revoke_token(authorization.split(None, 1)[1].strip())
    return {"ok": True}


@router.get("/me")
def me(authorization: str | None = Header(None),
       x_device_id: str | None = Header(None)):
    account = _account_from_header(authorization, x_device_id)
    schools, classes = accounts.scoped_ids(account)
    return {
        "account_id": account["id"], "role": account["role"],
        "roles": accounts.account_roles(account),
        "username": account["username"],
        "display_name": account["display_name"],
        "permissions": accounts.permissions_for(account),
        "app_version": APP_VERSION,
        "scope": {"school_ids": sorted(schools), "class_ids": sorted(classes)},
    }


# -- accounts (Developer/Teacher) -----------------------------------------------

@router.get("/accounts")
def list_accounts(authorization: str | None = Header(None),
                  x_device_id: str | None = Header(None)):
    account = _account_from_header(authorization, x_device_id)
    _require(account, "accounts.manage_non_teacher")
    return {"accounts": accounts.list_accounts(),
            "permissions_hint": accounts.DEFAULT_PERMISSIONS}


@router.post("/accounts")
def create_account(payload: dict, authorization: str | None = Header(None),
                   x_device_id: str | None = Header(None)):
    account = _account_from_header(authorization, x_device_id)
    _require(account, "accounts.manage_non_teacher")
    role = (payload.get("role") or "").upper()
    if role == "TEACHER" and account["role"] != "DEVELOPER":
        raise AppError("Only the DEVELOPER can create TEACHER accounts.",
                       status=403, code="forbidden")
    if role == "DEVELOPER" and account["role"] != "DEVELOPER":
        raise AppError("Only a DEVELOPER can create DEVELOPER accounts.",
                       status=403, code="forbidden")
    try:
        created = accounts.create_account(
            username=payload.get("username", ""),
            display_name=payload.get("display_name", ""),
            password=payload.get("password", ""), role=role,
            created_by=account["id"])
    except ValueError as exc:
        raise AppError(str(exc), status=400, code="bad_request")
    return {"account_id": created["id"], "role": created["role"]}


@router.post("/accounts/{account_id}/scope")
def set_scope(account_id: str, payload: dict,
              authorization: str | None = Header(None),
              x_device_id: str | None = Header(None)):
    account = _account_from_header(authorization, x_device_id)
    _require(account, "accounts.manage_non_teacher")
    target = accounts.get_account(account_id)
    if not target:
        raise AppError("account not found", status=404, code="not_found")
    accounts.set_scope(account_id, payload.get("scope", []))
    return {"ok": True, "account_id": account_id}


# -- account management (§2/§3) --------------------------------------------------
#
# Every decision is made SERVER-SIDE (§59). A Main Teacher may administer
# CO_TEACHER/ASSISTANT accounts only — never another Teacher or a Developer; a
# Developer may administer any account. A denied attempt is itself audited
# (§4 "operation attempted but denied"), so hiding a button is never the
# control.


def _load_target(account_id: str) -> dict:
    target = accounts.get_account(account_id)
    if not target:
        raise AppError("account not found", status=404, code="not_found")
    return target


def _deny(actor: dict, action: str, target: dict | None, detail: str) -> None:
    audit.record(action=action, target_type="account",
                 target_id=(target or {}).get("id"), actor_account=actor["id"],
                 actor_role=actor["role"],
                 affected_account=(target or {}).get("id"), outcome="denied",
                 details={"reason": detail})
    raise AppError(detail, status=403, code="forbidden")


def _require_manage(actor: dict, target: dict, action: str) -> None:
    if not accounts.can_manage_account(actor, target):
        _deny(actor, action, target,
              "Your role cannot manage this account.")


@router.post("/accounts/{account_id}/disable")
def disable_account(account_id: str, payload: dict,
                    authorization: str | None = Header(None),
                    x_device_id: str | None = Header(None)):
    account = _account_from_header(authorization, x_device_id)
    _require(account, "accounts.manage_non_teacher")
    target = _load_target(account_id)
    if accounts.has_role(target, "TEACHER") or accounts.has_role(target, "DEVELOPER"):
        if not accounts.has_role(account, "DEVELOPER"):
            _deny(account, "account.disable_denied", target,
                  "Only the DEVELOPER can disable TEACHER/DEVELOPER accounts.")
    _require_manage(account, target, "account.disable_denied")
    wanted = bool(payload.get("active", False))
    accounts.set_account_active(account_id, wanted)
    audit.record(action="account.activated" if wanted else "account.deactivated",
                 target_type="account", target_id=account_id,
                 actor_account=account["id"], actor_role=account["role"],
                 affected_account=account_id)
    return {"ok": True, "active": wanted}


@router.post("/accounts/{account_id}/username")
def change_username(account_id: str, payload: dict,
                    authorization: str | None = Header(None),
                    x_device_id: str | None = Header(None)):
    """Change a login username after a uniqueness check (§3)."""
    actor = _account_from_header(authorization, x_device_id)
    _require(actor, "accounts.manage_non_teacher")
    target = _load_target(account_id)
    _require_manage(actor, target, "account.username_change_denied")
    try:
        updated = accounts.set_username(target["id"], payload.get("username", ""),
                                        by=actor["id"], by_role=actor["role"])
    except ValueError as exc:
        raise AppError(str(exc), status=400, code="bad_request")
    return {"account_id": updated["id"], "username": updated["username"]}


@router.post("/accounts/{account_id}/display-name")
def change_display_name(account_id: str, payload: dict,
                        authorization: str | None = Header(None),
                        x_device_id: str | None = Header(None)):
    actor = _account_from_header(authorization, x_device_id)
    _require(actor, "accounts.manage_non_teacher")
    target = _load_target(account_id)
    _require_manage(actor, target, "account.profile_change_denied")
    previous = target["display_name"]
    accounts.set_display_name(target["id"], payload.get("display_name", ""))
    audit.record(action="account.profile_changed", target_type="account",
                 target_id=target["id"], actor_account=actor["id"],
                 actor_role=actor["role"], affected_account=target["id"],
                 details={"field": "display_name", "from": previous,
                          "to": accounts.get_account(target["id"])["display_name"]})
    return {"ok": True}


@router.post("/accounts/{account_id}/password-reset")
def reset_account_password(account_id: str, payload: dict,
                           authorization: str | None = Header(None),
                           x_device_id: str | None = Header(None)):
    """Reset another account's password (§2/§3).

    The actor chooses the NEW password and never sees the old one; every
    existing session for the target is revoked. The event never contains the
    password or its hash.
    """
    actor = _account_from_header(authorization, x_device_id)
    _require(actor, "accounts.manage_non_teacher")
    target = _load_target(account_id)
    _require_manage(actor, target, "account.password_reset_denied")
    try:
        revoked = accounts.admin_reset_password(
            target["id"], payload.get("new_password", ""), by=actor["id"],
            by_role=actor["role"])
    except ValueError as exc:
        raise AppError(str(exc), status=400, code="bad_request")
    # Notify through the existing cross-installation channel: the affected
    # account's own client surfaces this on the sync stream. (There is no
    # email/SMS subsystem in the project, so this is the supported mechanism.)
    sync.append_event(
        type_="account.password_reset",
        payload={"account_id": target["id"], "by": actor["username"]},
        account_id=target["id"],
        client_event_id=f"pwreset-{target['id']}-{int(time.time())}")
    return {"ok": True, "sessions_revoked": revoked, "notified": True}


@router.post("/accounts/{account_id}/roles")
def change_roles(account_id: str, payload: dict,
                 authorization: str | None = Header(None),
                 x_device_id: str | None = Header(None)):
    """Replace an account's role set, bounded by the actor's own privilege."""
    actor = _account_from_header(authorization, x_device_id)
    _require(actor, "accounts.manage_non_teacher")
    target = _load_target(account_id)
    _require_manage(actor, target, "account.roles_change_denied")
    requested = [str(r).strip().upper() for r in (payload.get("roles") or [])]
    allowed = set(accounts.assignable_roles(actor))
    disallowed = set(requested) - allowed
    if disallowed:
        _deny(actor, "account.roles_change_denied", target,
              f"Your role cannot assign {sorted(disallowed)}.")
    try:
        updated = accounts.set_roles(target["id"], requested, by=actor["id"],
                                     by_role=actor["role"])
    except ValueError as exc:
        raise AppError(str(exc), status=400, code="bad_request")
    return {"ok": True, "roles": accounts.account_roles(updated)}


# -- schools / classes ----------------------------------------------------------

@router.get("/schools")
def get_schools(authorization: str | None = Header(None),
                x_device_id: str | None = Header(None)):
    account = _account_from_header(authorization, x_device_id)
    schools = org.list_schools(include_inactive=True)
    schools_ids, class_ids = accounts.scoped_ids(account)
    if schools_ids or class_ids:
        schools = [s for s in schools
                   if s["id"] in schools_ids
                   or any(c for c in org.list_classes(s["id"])
                          if c["id"] in class_ids)]
    visible_classes = []
    for school in schools:
        for c in org.list_classes(school["id"], include_inactive=True):
            if (not class_ids and not schools_ids) or c["id"] in class_ids \
               or school["id"] in schools_ids:
                visible_classes.append(c)
    return {"schools": schools, "classes": visible_classes}


@router.post("/schools")
def create_school(payload: dict, authorization: str | None = Header(None),
                  x_device_id: str | None = Header(None)):
    account = _account_from_header(authorization, x_device_id)
    _require(account, "schools.manage")
    try:
        school = org.create_school(payload.get("name", ""),
                                   code=payload.get("code"),
                                   created_by=account["id"])
    except ValueError as exc:
        raise AppError(str(exc), status=400, code="bad_request")
    return school


@router.post("/schools/{school_id}/rename")
def rename_school(school_id: str, payload: dict,
                  authorization: str | None = Header(None),
                  x_device_id: str | None = Header(None)):
    account = _account_from_header(authorization, x_device_id)
    _require(account, "schools.manage")
    try:
        org.rename_school(school_id, payload.get("name", ""))
    except ValueError as exc:
        raise AppError(str(exc), status=400, code="bad_request")
    return {"ok": True}


@router.post("/schools/{school_id}/active")
def school_active(school_id: str, payload: dict,
                  authorization: str | None = Header(None),
                  x_device_id: str | None = Header(None)):
    account = _account_from_header(authorization, x_device_id)
    _require(account, "schools.manage")
    org.set_school_active(school_id, bool(payload.get("active", True)))
    return {"ok": True}


@router.post("/schools/{school_id}/classes")
def create_class(school_id: str, payload: dict,
                 authorization: str | None = Header(None),
                 x_device_id: str | None = Header(None)):
    account = _account_from_header(authorization, x_device_id)
    _require(account, "schools.manage")
    try:
        cls = org.create_class(school_id, payload.get("name", ""),
                               academic_year=payload.get("academic_year"))
    except ValueError as exc:
        raise AppError(str(exc), status=400, code="bad_request")
    return cls


@router.post("/schools/{school_id}/assign")
def assign(school_id: str, payload: dict,
           authorization: str | None = Header(None),
           x_device_id: str | None = Header(None)):
    account = _account_from_header(authorization, x_device_id)
    _require(account, "accounts.manage_non_teacher")
    try:
        org.assign_account(payload.get("account_id"), school_id,
                           payload.get("class_id"))
    except ValueError as exc:
        raise AppError(str(exc), status=400, code="bad_request")
    return {"ok": True}


# -- Classroom mappings ----------------------------------------------------------

@router.post("/mappings/course")
def map_course(payload: dict, authorization: str | None = Header(None),
               x_device_id: str | None = Header(None)):
    account = _account_from_header(authorization, x_device_id)
    _require(account, "assignments.manage")
    try:
        mapping = org.map_course(payload.get("course_id", ""),
                                 payload.get("school_id", ""),
                                 payload.get("class_id", ""))
    except ValueError as exc:
        raise AppError(str(exc), status=400, code="bad_request")
    return mapping


@router.post("/mappings/assignment")
def map_assignment(payload: dict, authorization: str | None = Header(None),
                   x_device_id: str | None = Header(None)):
    account = _account_from_header(authorization, x_device_id)
    _require(account, "assignments.manage")
    try:
        mapping = org.map_assignment(
            payload.get("coursework_id", ""), payload.get("school_id", ""),
            payload.get("class_id", ""),
            assigned_worker_id=payload.get("assigned_worker_id"),
            correction_config=payload.get("correction_config"))
    except ValueError as exc:
        raise AppError(str(exc), status=400, code="bad_request")
    return mapping


@router.get("/mappings/course/{course_id}")
def course_mapping(course_id: str, authorization: str | None = Header(None),
                   x_device_id: str | None = Header(None)):
    account = _account_from_header(authorization, x_device_id)
    mapping = org.get_course_mapping(course_id)
    if not mapping:
        raise AppError("no mapping", status=404, code="not_found")
    if not accounts.can_see_class(account, mapping["school_id"],
                                  mapping["class_id"]):
        raise AppError("forbidden", status=403, code="forbidden")
    return mapping


# -- workers / devices ------------------------------------------------------------

@router.get("/workers")
def list_workers(authorization: str | None = Header(None),
                 x_device_id: str | None = Header(None)):
    account = _account_from_header(authorization, x_device_id)
    if account["role"] in ("CO_TEACHER", "ASSISTANT"):
        # view_own means view_own: only this account's own workers.
        # workers.manage_own implies seeing what you manage.
        if not (accounts.has_permission(account, "workers.view_own")
                or accounts.has_permission(account, "workers.manage_own")):
            raise AppError(
                f"Your role ({account['role']}) does not allow 'workers.view_own'.",
                status=403, code="forbidden")
        return {"workers": agents.list_workers(account_id=account["id"])}
    _require(account, "workers.view")
    return {"workers": agents.list_workers()}


@router.get("/workers/{worker_id}")
def get_worker(worker_id: str, authorization: str | None = Header(None),
               x_device_id: str | None = Header(None)):
    account = _account_from_header(authorization, x_device_id)
    if account["role"] in ("CO_TEACHER", "ASSISTANT"):
        if not (accounts.has_permission(account, "workers.view_own")
                or accounts.has_permission(account, "workers.manage_own")):
            raise AppError(
                f"Your role ({account['role']}) does not allow 'workers.view_own'.",
                status=403, code="forbidden")
    else:
        _require(account, "workers.view")
    worker = agents.get_worker(worker_id)
    if not worker:
        raise AppError("worker not found", status=404, code="not_found")
    # A limited role may only look up its OWN worker — never another
    # account's (the id is guessable, so scoping happens server-side).
    if account["role"] in ("CO_TEACHER", "ASSISTANT") \
       and worker.get("account_id") != account["id"]:
        raise AppError("worker not found", status=404, code="not_found")
    return worker


@router.post("/register-worker")
def register_worker(payload: dict, authorization: str | None = Header(None),
                    x_device_id: str | None = Header(None)):
    account = _account_from_header(authorization, x_device_id)
    device = agents.register_device(payload.get("device_id", ""),
                                    label=payload.get("label"),
                                    account_id=account["id"],
                                    platform=payload.get("platform"),
                                    arch=payload.get("arch"))
    worker = agents.register_worker(
        payload.get("worker_id", ""), device["device_id"],
        account_id=account["id"], app_version=payload.get("app_version"),
        agent_version=payload.get("agent_version"))
    return {"ok": True, "worker": worker, "device": device}


# -- heartbeat (the tiny agent's only regular traffic, §16) ------------------------

@router.post("/heartbeat")
def heartbeat(payload: dict):
    """Lightweight presence (§16).

    Authenticates with the account's token when present; otherwise it accepts a
    device credential so a freshly installed, not-yet-linked machine can still
    be seen. Neither path can claim an account: an unlinked device heartbeats
    with account_id = None (§59).
    """
    token = payload.get("token")
    account = accounts.resolve_token(token) if token else None
    device_id = payload.get("device_id", "unknown")
    linked_account_id = None
    device_credential = payload.get("device_credential")
    if not account and device_credential:
        device = discovery.resolve(device_id, device_credential)
        if device:
            # The credential proves the device id; linkage (if any) is
            # server-side truth, never client-supplied.
            linked = discovery.linked_account_ids(device["device_id"])
            linked_account_id = next(iter(linked)) if len(linked) == 1 else None
            owner = db.query_one("SELECT account_id FROM devices WHERE device_id = ?",
                                 (device["device_id"],))
            if linked_account_id is None and owner and owner["account_id"]:
                linked_account_id = owner["account_id"]
            device_id = device["device_id"]
    result = agents.heartbeat(
        device_id=device_id,
        worker_id=payload.get("worker_id"),
        account_id=account["id"] if account else linked_account_id,
        app_version=payload.get("app_version"),
        agent_version=payload.get("agent_version"),
        status=payload.get("status") or agents.PRESENCE_ONLINE,
        current_job_id=payload.get("current_job_id"),
        label=payload.get("label"),
        platform=payload.get("platform"), arch=payload.get("arch"))
    return result


# -- device registration + discovery (§extension 3-5) ------------------------------
#
# These are the only endpoints a machine uses BEFORE a human signs in. The
# installer ships no credentials; the device generates its own id and exchanges
# it for a short-lived, revocable DEVICE credential. A device credential can
# never name an account, read reports or run corrections.

def _device_from_credential(x_device_id: str | None,
                            x_device_credential: str | None) -> dict:
    if not x_device_id or not x_device_credential:
        raise AppError("Device credential required.", status=401,
                       code="device_auth_required")
    device = discovery.resolve(x_device_id, x_device_credential)
    if device is None:
        raise AppError("Invalid, expired or revoked device credential.",
                       status=401, code="device_auth_invalid")
    return device


@router.post("/device/register")
def register_device_and_credential(payload: dict):
    """Register a newly installed machine and issue a device credential.

    Requires NO user credentials (§1): the installer stores none. The response
    carries the discovery directory so the app can show what is reachable the
    moment it starts, and it can never carry account rights.
    """
    try:
        return discovery.register(payload.get("device_id", ""),
                                  label=payload.get("label"),
                                  app_version=payload.get("app_version"),
                                  platform=payload.get("platform"),
                                  arch=payload.get("arch"),
                                  ip=payload.get("ip"))
    except ValueError as exc:
        raise AppError(str(exc), status=400, code="bad_device")


@router.get("/device/discovery")
def device_discovery(x_device_id: str | None = Header(None),
                     x_device_credential: str | None = Header(None)):
    """Accounts + device availability visible to THIS registered device."""
    device = _device_from_credential(x_device_id, x_device_credential)
    return discovery.discover(device["device_id"])


@router.get("/devices")
def list_devices(authorization: str | None = Header(None),
                 x_device_id: str | None = Header(None),
                 scope: str = "all"):
    """Registered devices (Developer/Teacher). 'own' narrows to yours."""
    account = _account_from_header(authorization, x_device_id)
    _require(account, "workers.view")
    if scope == "own":
        return {"devices": discovery.devices_for_account(account["id"])}
    return {"devices": discovery.list_devices()}


@router.post("/devices/{device_id}/revoke")
def revoke_device(device_id: str, authorization: str | None = Header(None),
                  x_device_id: str | None = Header(None)):
    """Revoke a device's credential and links (§8 device revocation)."""
    account = _account_from_header(authorization, x_device_id)
    _require(account, "accounts.manage_non_teacher")
    revoked = discovery.revoke_device(device_id, by=account["username"])
    return {"ok": True, "device_id": device_id, "credentials_revoked": revoked}


@router.post("/devices/{device_id}/link")
def link_device(device_id: str, payload: dict,
                authorization: str | None = Header(None),
                x_device_id: str | None = Header(None)):
    """Explicitly authorise a device to synchronise with an account (§5)."""
    account = _account_from_header(authorization, x_device_id)
    _require(account, "accounts.manage_non_teacher")
    target = payload.get("account_id") or account["id"]
    # A limited role may only link to itself, never hand another account a
    # device it does not own.
    if target != account["id"] and account["role"] != "DEVELOPER":
        raise AppError("Only the DEVELOPER can link a device to another account.",
                       status=403, code="forbidden")
    try:
        return discovery.link_device(device_id, target, by=account["username"])
    except ValueError as exc:
        raise AppError(str(exc), status=404, code="not_found")


# -- assistant teams (§1) ---------------------------------------------------------
#
# Team membership is the ONLY thing that shares correction records between
# assistants. It is checked server-side on every read, so two assistants in
# different schools never see each other's work (§1), and the original
# correcting
# account always stays attached to each record.


def _require_team_manager(actor: dict, school_id: str | None,
                          action: str = "team.manage_denied") -> None:
    if accounts.has_role(actor, "DEVELOPER") or accounts.has_role(actor, "TEACHER"):
        return
    audit.record(action=action, target_type="team", actor_account=actor["id"],
                 actor_role=actor["role"], school_id=school_id, outcome="denied",
                 details={"reason": "role cannot manage teams"})
    raise AppError("Your role cannot manage assistant teams.", status=403,
                   code="forbidden")


@router.get("/teams")
def list_teams(authorization: str | None = Header(None),
               x_device_id: str | None = Header(None)):
    """Teams the caller manages (Teacher/Developer) or belongs to (limited)."""
    account = _account_from_header(authorization, x_device_id)
    mine = teams.team_ids_for_account(account["id"])
    schools, classes = accounts.scoped_ids(account)
    if schools or classes:
        return {"teams": teams.teams_for_account(account["id"]), "mine": mine}
    _require(account, "accounts.manage_non_teacher")
    return {"teams": teams.list_teams(include_inactive=True), "mine": mine}


@router.get("/me/teams")
def my_teams(authorization: str | None = Header(None),
             x_device_id: str | None = Header(None)):
    account = _account_from_header(authorization, x_device_id)
    return {"teams": teams.teams_for_account(account["id"])}


@router.post("/teams")
def create_team(payload: dict, authorization: str | None = Header(None),
                x_device_id: str | None = Header(None)):
    account = _account_from_header(authorization, x_device_id)
    _require(account, "accounts.manage_non_teacher")
    school_id = payload.get("school_id")
    _require_team_manager(account, school_id, "team.create_denied")
    try:
        return teams.create_team(payload.get("name", ""), school_id,
                                 created_by=account["id"],
                                 by_role=account["role"])
    except ValueError as exc:
        raise AppError(str(exc), status=400, code="bad_request")


@router.get("/teams/{team_id}")
def get_team(team_id: str, authorization: str | None = Header(None),
             x_device_id: str | None = Header(None)):
    account = _account_from_header(authorization, x_device_id)
    team = teams.get_team(team_id)
    if not team:
        raise AppError("team not found", status=404, code="not_found")
    allowed = (accounts.has_role(account, "DEVELOPER")
               or accounts.has_role(account, "TEACHER")
               or account["id"] in teams.member_account_ids(team_id))
    if not allowed:
        _deny(account, "team.view_denied", None, "You are not a member of this team.")
    return {**team, "members": teams.members(team_id)}


@router.post("/teams/{team_id}/members")
def add_team_member(team_id: str, payload: dict,
                    authorization: str | None = Header(None),
                    x_device_id: str | None = Header(None)):
    account = _account_from_header(authorization, x_device_id)
    _require(account, "accounts.manage_non_teacher")
    team = teams.get_team(team_id)
    if not team:
        raise AppError("team not found", status=404, code="not_found")
    _require_team_manager(account, team["school_id"], "team.member_add_denied")
    member_id = payload.get("account_id") or ""
    member = accounts.get_account(member_id)
    if not member:
        raise AppError("account not found", status=404, code="not_found")
    # A Main Teacher can only add accounts it may administer; a Developer may
    # add any account.
    if not accounts.can_manage_account(account, member):
        _deny(account, "team.member_add_denied", member,
              "Your role cannot add this account to a team.")
    try:
        updated = teams.add_member(team_id, member_id, by=account["id"],
                                   by_role=account["role"],
                                   role_in_team=payload.get("role_in_team"))
    except ValueError as exc:
        raise AppError(str(exc), status=400, code="bad_request")
    return {"ok": True, "team": updated}


@router.post("/teams/{team_id}/members/remove")
def remove_team_member(team_id: str, payload: dict,
                       authorization: str | None = Header(None),
                       x_device_id: str | None = Header(None)):
    account = _account_from_header(authorization, x_device_id)
    _require(account, "accounts.manage_non_teacher")
    team = teams.get_team(team_id)
    if not team:
        raise AppError("team not found", status=404, code="not_found")
    _require_team_manager(account, team["school_id"], "team.member_remove_denied")
    member_id = payload.get("account_id") or ""
    member = accounts.get_account(member_id)
    if member and not accounts.can_manage_account(account, member):
        _deny(account, "team.member_remove_denied", member,
              "Your role cannot remove this account from a team.")
    try:
        updated = teams.remove_member(team_id, member_id, by=account["id"],
                                      by_role=account["role"])
    except ValueError as exc:
        raise AppError(str(exc), status=400, code="bad_request")
    return {"ok": True, "team": updated}


@router.post("/teams/{team_id}/active")
def set_team_active(team_id: str, payload: dict,
                    authorization: str | None = Header(None),
                    x_device_id: str | None = Header(None)):
    account = _account_from_header(authorization, x_device_id)
    _require(account, "accounts.manage_non_teacher")
    team = teams.get_team(team_id)
    if not team:
        raise AppError("team not found", status=404, code="not_found")
    _require_team_manager(account, team["school_id"], "team.active_denied")
    teams.set_team_active(team_id, bool(payload.get("active", True)),
                          by=account["id"], by_role=account["role"])
    return {"ok": True}


@router.get("/teams/{team_id}/results")
def team_results(team_id: str, authorization: str | None = Header(None),
                 x_device_id: str | None = Header(None), limit: int = 200):
    """Correction records shared by the team's members (§1).

    Only a member (or a Teacher/Developer) may read them, and the results are
    the UNION of the members' records — every record still carries the
    ``account_id`` of the assistant who actually corrected it.
    """
    account = _account_from_header(authorization, x_device_id)
    team = teams.get_team(team_id)
    if not team:
        raise AppError("team not found", status=404, code="not_found")
    member_ids = teams.member_account_ids(team_id)
    if not (accounts.has_role(account, "DEVELOPER")
            or accounts.has_role(account, "TEACHER")
            or account["id"] in member_ids):
        _deny(account, "team.results_denied", None,
              "You are not a member of this team.")
    rows = sync.list_correction_results(limit=max(1, min(limit, 1000)))
    shared = [r for r in rows if r.get("account_id") in member_ids]
    return {"results": shared, "member_count": len(member_ids)}


# -- tiny agent ON/OFF (§extension) ------------------------------------------------


def _own_last_heartbeat(account: dict) -> float | None:
    """Freshest heartbeat among THIS account's own workers.

    Only the caller's own devices are inspected, so the switch reports the
    reality of the local machine rather than someone else's.
    """
    try:
        rows = agents.list_workers(account_id=account["id"])
    except Exception:  # noqa: BLE001 — status is advisory, never fatal
        return None
    stamps = [float(r["last_heartbeat"]) for r in rows if r.get("last_heartbeat")]
    return max(stamps) if stamps else None


@router.get("/agent/state")
def get_agent_state(authorization: str | None = Header(None),
                    x_device_id: str | None = Header(None)):
    """Current ON/OFF preference plus the observed agent reality."""
    account = _account_from_header(authorization, x_device_id)
    _require(account, "agents.control")
    return agent_control.describe(last_heartbeat=_own_last_heartbeat(account))


@router.post("/agent/state")
def set_agent_state(payload: dict, authorization: str | None = Header(None),
                    x_device_id: str | None = Header(None)):
    """Switch the LOCAL tiny agent on or off.

    This is a per-installation control: it writes this machine's preference
    file only, so one laptop switching the agent off can never disable the
    teacher's or another assistant's agent (§extension 5). The stored markdown
    is durable, so OFF stays OFF across restarts (§extension 4).
    """
    account = _account_from_header(authorization, x_device_id)
    _require(account, "agents.control")
    # This version does not run a Tiny Agent at all, so switching one ON is
    # refused rather than silently stored: a preference that cannot take effect
    # would be a control that lies (§1). Switching it OFF is still accepted, so
    # an installation that still has an old "enabled" record can clear it.
    if bool(payload.get("enabled")):
        agent_control.require_product_enabled()
    wanted = bool(payload.get("enabled"))
    state = agent_control.save_state(wanted, updated_by=account["id"])
    audit.record(action="agent.enabled" if wanted else "agent.disabled",
                 target_type="agent", target_id=state_path_label(),
                 details={"by": account["username"]})
    from .sync import append_event
    # The switch is a signed local decision worth publishing on the sync
    # stream so the developer view can see changes without polling this host.
    append_event(type_="agent.state",
                 payload={"enabled": wanted, "account_id": account["id"]},
                 account_id=account["id"], device_id=x_device_id,
                 client_event_id=f"agent-state-{account['id']}-{int(state['updated_at'])}")
    return agent_control.describe(last_heartbeat=_own_last_heartbeat(account))


def state_path_label() -> str:
    try:
        return str(agent_control.state_path())
    except Exception:  # noqa: BLE001
        return "agent_state.json"


# -- events / sync ------------------------------------------------------------------

@router.post("/events")
def post_event(payload: dict, authorization: str | None = Header(None),
               x_device_id: str | None = Header(None)):
    account = _account_from_header(authorization, x_device_id)
    type_ = payload.get("type") or "client.event"
    payload_body = payload.get("payload") or {}
    seq = sync.append_event(
        type_=type_, payload=accounts.sanitize_for_log(payload_body),
        school_id=payload_body.get("school_id"),
        class_id=payload_body.get("class_id"), account_id=account["id"],
        device_id=payload.get("device_id"), worker_id=payload.get("worker_id"),
        client_event_id=payload.get("client_event_id"))
    return {"ok": True, "seq": seq, "duplicate": seq is None}


@router.post("/results")
def post_result(payload: dict, authorization: str | None = Header(None),
                x_device_id: str | None = Header(None)):
    account = _account_from_header(authorization, x_device_id)
    _require(account, "corrections.run")
    try:
        outcome = sync.record_correction_result(
            payload, account_id=account["id"],
            device_id=payload.get("device_id"), worker_id=payload.get("worker_id"))
    except ValueError as exc:
        raise AppError(str(exc), status=400, code="bad_result")
    return outcome


@router.get("/results")
def list_results(authorization: str | None = Header(None),
                 x_device_id: str | None = Header(None),
                 school_id: str | None = None, class_id: str | None = None,
                 limit: int = 200):
    account = _account_from_header(authorization, x_device_id)
    return {"results": sync.list_correction_results(
        account=account, limit=limit, school_id=school_id, class_id=class_id)}


# -- report delivery (§5/§6) -------------------------------------------------------

@router.get("/report-deliveries")
def list_report_deliveries(authorization: str | None = Header(None),
                           x_device_id: str | None = Header(None),
                           limit: int = 200):
    """Report states, separate from the grade records.

    Limited roles need 'reports.view'; Teacher/Developer hold
    'reports.view_all'. Either way the decision is made server-side (§59).
    """
    account = _account_from_header(authorization, x_device_id)
    if not (accounts.has_permission(account, "reports.view")
            or accounts.has_permission(account, "reports.view_all")):
        raise AppError("Your roles do not allow viewing reports.", status=403,
                       code="forbidden")
    from . import report_delivery
    # A limited role only sees deliveries for the correction records it may
    # read — its own plus its teammates' (§1/§48). Teacher/Developer are
    # unrestricted, so they pass None.
    schools, classes = accounts.scoped_ids(account)
    result_ids = None
    if schools or classes:
        visible = sync.list_correction_results(account=account, limit=1000)
        result_ids = {r["id"] for r in visible}
    return {"deliveries": report_delivery.list_deliveries(limit=limit,
                                                          result_ids=result_ids),
            "summary": report_delivery.summary()}


@router.post("/report-deliveries/{result_record_id}/retry")
def retry_report_delivery(result_record_id: str,
                          authorization: str | None = Header(None),
                          x_device_id: str | None = Header(None)):
    """Manual retry for a failed/stuck report transfer (§6).

    An already DELIVERED report is returned unchanged: an acknowledged upload
    must not be re-sent for no reason.
    """
    account = _account_from_header(authorization, x_device_id)
    _require(account, "corrections.retry")
    from . import report_delivery
    item = report_delivery.retry_now(result_record_id)
    if item is None:
        raise AppError("report delivery not found", status=404, code="not_found")
    audit.record(action="report.retry", target_type="report_delivery",
                 target_id=result_record_id, details={"by": account["username"]})
    return item


@router.post("/report-deliveries/{result_record_id}/delivered")
def confirm_report_delivery(result_record_id: str,
                            payload: dict | None = None,
                            authorization: str | None = Header(None),
                            x_device_id: str | None = Header(None)):
    """Record a VERIFIED report transfer.

    The caller must assert the receiving service confirmed storage; this is
    never called speculatively from a successful grade sync (§5).
    """
    account = _account_from_header(authorization, x_device_id)
    _require(account, "corrections.run")
    from . import report_delivery
    body = payload or {}
    if not body.get("confirmed"):
        raise AppError("An upload may only be marked delivered once the "
                       "receiving service confirms storage (confirmed=true).",
                       status=400, code="confirmation_required")
    item = report_delivery.mark_delivered(
        result_record_id, report_sha256=body.get("report_sha256"))
    if item is None:
        raise AppError("report delivery not found", status=404, code="not_found")
    return item


@router.get("/events")
def get_events(authorization: str | None = Header(None),
               x_device_id: str | None = Header(None), since: int = 0,
               limit: int = 500):
    account = _account_from_header(authorization, x_device_id)
    events = sync.events_since(since, limit=limit, account=account)
    return {"events": events, "latest_seq": sync.latest_seq()}


# -- developer sync monitoring (§8) ------------------------------------------------

@router.get("/sync-monitor")
def sync_monitor(authorization: str | None = Header(None),
                 x_device_id: str | None = Header(None)):
    """Developer/Teacher sync-monitoring snapshot.

    One aggregate read: agents with derived presence and versions, report
    delivery health, retry history, conflicting-revision warnings and recent
    sanitized errors. Restricted to 'diagnostics.view' (Developer/Teacher) —
    an Assistant's 'diagnostics.limited' deliberately does NOT open the whole
    fleet's operational picture (§7/§10).
    """
    account = _account_from_header(authorization, x_device_id)
    _require(account, "diagnostics.view")
    from . import sync_monitor as monitor
    return monitor.snapshot()


# -- errors / diagnostics ------------------------------------------------------------

@router.post("/errors")
def post_error(payload: dict, authorization: str | None = Header(None),
               x_device_id: str | None = Header(None)):
    account = _account_from_header(authorization, x_device_id)
    outcome = audit.report_error(
        error_type=payload.get("error_type", "unknown"),
        message=payload.get("message", ""),
        failed_stage=payload.get("failed_stage"), account=account,
        device_id=payload.get("device_id"), worker_id=payload.get("worker_id"),
        context=payload.get("context"),
        session_events=payload.get("session_events"),
        stack_trace=payload.get("stack_trace"))
    return outcome


@router.get("/errors")
def list_errors(authorization: str | None = Header(None),
                x_device_id: str | None = Header(None), limit: int = 100,
                worker_id: str | None = None):
    account = _account_from_header(authorization, x_device_id)
    if account["role"] in ("CO_TEACHER", "ASSISTANT"):
        _require(account, "diagnostics.limited")
        # Limited roles see ONLY their own account's reports. A caller-supplied
        # worker_id narrows that further but can never widen it (§26: a
        # Co-Teacher must not read another school's failure details).
        return {"errors": audit.list_errors(limit=limit, worker_id=worker_id,
                                             account_id=account["id"])}
    _require(account, "diagnostics.view")
    return {"errors": audit.list_errors(limit=limit, worker_id=worker_id)}


# -- updates ---------------------------------------------------------------------

@router.post("/updates")
def create_update(payload: dict, authorization: str | None = Header(None),
                  x_device_id: str | None = Header(None)):
    """Create a release DRAFT (developer-only).

    Deliberately a draft: this endpoint can never make a release available to
    clients on its own, so an unvalidated or unsigned package cannot be pushed
    out by calling it directly. Promotion happens only through
    ``POST /releases/{id}/publish``, which validates first and requires an
    explicit confirmation.
    """
    account = _account_from_header(authorization, x_device_id)
    _require(account, "*")     # developer-only request creation
    try:
        request = updates.create_request(
            status=updates.STATUS_DRAFT,
            target_version=payload.get("target_version", ""),
            created_by=account["id"],
            target_component=payload.get("target_component", "app"),
            minimum_supported_version=payload.get("minimum_supported_version"),
            release_notes=payload.get("release_notes"),
            priority=payload.get("priority", "normal"),
            required=bool(payload.get("required", False)),
            auto_install=bool(payload.get("auto_install", False)),
            scope=payload.get("scope"),
            package_sha256=payload.get("package_sha256"),
            package_url=payload.get("package_url"),
            package_signature=payload.get("package_signature"),
            signing_key_id=payload.get("signing_key_id"),
            signing_public_key=payload.get("signing_public_key"))
    except ValueError as exc:
        raise AppError(str(exc), status=400, code="bad_request")
    return request


@router.get("/updates")
def list_updates(authorization: str | None = Header(None),
                 x_device_id: str | None = Header(None)):
    account = _account_from_header(authorization, x_device_id)
    _require(account, "diagnostics.view")
    return {"updates": updates.list_requests()}


@router.get("/updates/for-device")
def updates_for_device(authorization: str | None = Header(None),
                       x_device_id: str | None = Header(None)):
    """Applicable releases for the calling device.

    Only PUBLISHED, compatible, non-downgrade releases are returned, and the
    current version is derived server-side from what this device has actually
    reported — never from a value the client supplies (§4/§7).
    """
    account = _account_from_header(authorization, x_device_id)
    device_id = x_device_id or account["last_device_id"]
    return {"updates": updates.requests_for_device(device_id or "")}


@router.post("/updates/{update_id}/status")
def update_status(update_id: str, payload: dict,
                  authorization: str | None = Header(None),
                  x_device_id: str | None = Header(None)):
    account = _account_from_header(authorization, x_device_id)
    device_id = x_device_id or account["last_device_id"]
    try:
        updates.set_device_status(update_id, device_id,
                                 payload.get("state", ""),
                                 payload.get("detail"))
    except ValueError as exc:
        raise AppError(str(exc), status=400, code="bad_request")
    return {"ok": True}


@router.post("/updates/history")
def update_history_post(payload: dict,
                        authorization: str | None = Header(None),
                        x_device_id: str | None = Header(None)):
    account = _account_from_header(authorization, x_device_id)
    device_id = x_device_id or account["last_device_id"]
    history_id = updates.record_history(
        device_id=device_id, update_id=payload.get("update_id"),
        old_version=payload.get("old_version", ""),
        new_version=payload.get("new_version", ""),
        result=payload.get("result", "unknown"),
        started_at=payload.get("started_at"),
        completed_at=payload.get("completed_at"),
        failure_reason=payload.get("failure_reason"),
        rollback_result=payload.get("rollback_result"))
    return {"ok": True, "id": history_id}


@router.get("/updates/history")
def update_history(authorization: str | None = Header(None),
                   x_device_id: str | None = Header(None), device_id: str | None = None,
                   limit: int = 100):
    account = _account_from_header(authorization, x_device_id)
    if not accounts.has_permission(account, "diagnostics.view") and \
       account["role"] != "DEVELOPER":
        # Limited roles get THEIR OWN device's update history (the Updates
        # panel shows everyone where their device stands) — never the whole
        # fleet's rollout log, and never another device picked by id.
        own = x_device_id or account.get("last_device_id")
        if not own:
            return {"history": []}
        return {"history": updates.list_history(device_id=own,
                                                limit=min(int(limit), 50))}
    return {"history": updates.list_history(device_id=device_id, limit=limit)}


# -- update delivery (§19/§25): the signed package body -------------------------

@router.post("/updates/{update_id}/package")
def upload_package(update_id: str, payload: dict,
                   authorization: str | None = Header(None),
                   x_device_id: str | None = Header(None)):
    """Publisher side: attach the signed package bytes to an update request.

    The body is base64 of ``Package.to_bytes()`` — the exact canonical bytes
    the Developer signed. Developer-only.
    """
    account = _account_from_header(authorization, x_device_id)
    _require(account, "*")
    data_b64 = payload.get("data")
    if not isinstance(data_b64, str) or not data_b64:
        raise AppError("data (base64 package body) is required", status=400,
                       code="bad_request")
    try:
        data = base64.b64decode(data_b64, validate=True)
    except (binascii.Error, ValueError):
        raise AppError("data must be valid base64", status=400,
                       code="bad_request")
    try:
        return updates.store_package(update_id, data)
    except ValueError as exc:
        raise AppError(str(exc), status=400, code="bad_request")


@router.get("/updates/{update_id}/package")
def download_package(update_id: str,
                     authorization: str | None = Header(None),
                     x_device_id: str | None = Header(None)):
    """Device side: download the signed package body.

    Authorization is enforced server-side: the request must actually TARGET
    this device (§3/§34). The bytes are returned verbatim with the checksum and
    signature in headers so the agent verifies the same content it received.
    """
    account = _account_from_header(authorization, x_device_id)
    device_id = x_device_id or account["last_device_id"] or ""
    request = updates.get_request(update_id)
    if not request:
        raise AppError("update not found", status=404, code="not_found")
    if not updates.request_applies(request, device_id):
        raise AppError("this update does not target this device", status=403,
                       code="forbidden")
    package = updates.get_package(update_id)
    if not package:
        raise AppError("package is not available yet", status=404,
                       code="not_found")
    return Response(
        content=package["content"], media_type="application/octet-stream",
        headers={
            "X-Package-Sha256": package["sha256"],
            "X-Package-Signature": request.get("package_signature") or "",
            "X-Package-Key-Id": request.get("signing_key_id") or "",
            "X-Target-Version": request.get("target_version") or "",
        })


# -- release management (§release management, Developer only) ---------------------

@router.get("/releases")
def list_releases(authorization: str | None = Header(None),
                  x_device_id: str | None = Header(None),
                  status: str | None = None):
    """Full release history. DEVELOPER only — this is the control plane."""
    account = _account_from_header(authorization, x_device_id)
    _require(account, "*")
    releases = updates.list_requests()
    if status:
        releases = [r for r in releases if r["status"] == status.lower()]
    for release in releases:
        if release["has_package"]:
            package = updates.get_package(release["update_id"]) or {}
            release["package_size"] = package.get("size")
            release["package_sha256"] = package.get("sha256")
    return {"releases": releases}


@router.get("/releases/overview")
def releases_overview(authorization: str | None = Header(None),
                      x_device_id: str | None = Header(None)):
    """Current-release summary + fleet installation status (Developer only)."""
    account = _account_from_header(authorization, x_device_id)
    _require(account, "*")
    return updates.overview()


@router.get("/releases/installations")
def release_installations(authorization: str | None = Header(None),
                          x_device_id: str | None = Header(None)):
    """Per-installation status breakdown (Developer only)."""
    account = _account_from_header(authorization, x_device_id)
    _require(account, "*")
    return updates.install_status()


@router.post("/releases")
def create_release(payload: dict, authorization: str | None = Header(None),
                   x_device_id: str | None = Header(None)):
    """Create a DRAFT release. A draft is never offered to a client."""
    account = _account_from_header(authorization, x_device_id)
    _require(account, "*")
    try:
        return updates.create_request(
            target_version=payload.get("target_version", ""),
            created_by=account["id"],
            target_component=payload.get("target_component", "app"),
            minimum_supported_version=payload.get("minimum_supported_version"),
            release_notes=payload.get("release_notes"),
            priority=payload.get("priority", "normal"),
            required=bool(payload.get("required", False)),
            auto_install=bool(payload.get("auto_install", False)),
            scope=payload.get("scope"),
            package_sha256=payload.get("package_sha256"),
            package_url=payload.get("package_url"),
            package_signature=payload.get("package_signature"),
            signing_key_id=payload.get("signing_key_id"),
            signing_public_key=payload.get("signing_public_key"),
            channel=(payload.get("channel") or "stable").lower(),
            status=updates.STATUS_DRAFT,
            platform=payload.get("platform"),
            arch=payload.get("arch"))
    except ValueError as exc:
        raise AppError(str(exc), status=400, code="bad_request")


@router.post("/releases/{update_id}/validate")
def validate_release(update_id: str, authorization: str | None = Header(None),
                     x_device_id: str | None = Header(None)):
    account = _account_from_header(authorization, x_device_id)
    _require(account, "*")
    try:
        return updates.validate_release(update_id)
    except ValueError as exc:
        raise AppError(str(exc), status=404, code="not_found")


@router.post("/releases/{update_id}/publish")
def publish_release(update_id: str, payload: dict | None = None,
                    authorization: str | None = Header(None),
                    x_device_id: str | None = Header(None)):
    """Publish a validated release. Requires an explicit ``confirm=true``."""
    account = _account_from_header(authorization, x_device_id)
    _require(account, "*")
    if not (payload or {}).get("confirm"):
        raise AppError("Publishing requires explicit confirmation (confirm=true).",
                       status=400, code="confirmation_required")
    try:
        return updates.publish_release(update_id, actor_account=account["id"])
    except ValueError as exc:
        raise AppError(str(exc), status=400, code="bad_request")


@router.post("/releases/{update_id}/withdraw")
def withdraw_release(update_id: str, payload: dict | None = None,
                     authorization: str | None = Header(None),
                     x_device_id: str | None = Header(None)):
    """Withdraw from FUTURE distribution. Nothing is uninstalled (§6)."""
    account = _account_from_header(authorization, x_device_id)
    _require(account, "*")
    if not (payload or {}).get("confirm"):
        raise AppError("Withdrawing requires explicit confirmation (confirm=true).",
                       status=400, code="confirmation_required")
    try:
        return updates.withdraw_release(update_id, actor_account=account["id"],
                                        reason=(payload or {}).get("reason"))
    except ValueError as exc:
        raise AppError(str(exc), status=400, code="bad_request")


@router.patch("/releases/{update_id}")
def edit_release(update_id: str, payload: dict,
                 authorization: str | None = Header(None),
                 x_device_id: str | None = Header(None)):
    """Edit release notes / minimum supported version. Never the version."""
    account = _account_from_header(authorization, x_device_id)
    _require(account, "*")
    try:
        return updates.edit_release(
            update_id, release_notes=payload.get("release_notes"),
            minimum_supported_version=payload.get("minimum_supported_version"),
            actor_account=account["id"])
    except ValueError as exc:
        raise AppError(str(exc), status=400, code="bad_request")


@router.post("/releases/{update_id}/rollback")
def rollback_release(update_id: str, authorization: str | None = Header(None),
                     x_device_id: str | None = Header(None)):
    """Make an earlier published release the recommended one again."""
    account = _account_from_header(authorization, x_device_id)
    _require(account, "*")
    try:
        return updates.rollback_recommended(update_id,
                                            actor_account=account["id"])
    except ValueError as exc:
        raise AppError(str(exc), status=400, code="bad_request")


@router.get("/audit")
def get_audit(authorization: str | None = Header(None),
              x_device_id: str | None = Header(None), limit: int = 200,
              action: str | None = None, actor_account: str | None = None,
              affected_account: str | None = None, school_id: str | None = None,
              team_id: str | None = None, outcome: str | None = None,
              since: float | None = None, q: str | None = None):
    """Account activity / audit trail (§4) with search + filtering.

    Developer-facing: shared/private changes are visible to authorized
    Developers only. Every event is written server-side, so a client can never
    edit or erase its own history.
    """
    account = _account_from_header(authorization, x_device_id)
    _require(account, "diagnostics.view")
    return {"audit": audit.list_audit(
        limit=limit, action=action, actor_account=actor_account,
        affected_account=affected_account, school_id=school_id, team_id=team_id,
        outcome=outcome, since=since, q=q)}
