"""MULTI-ACCOUNT MODE — one switch that disables (never deletes) the
multi-user layer.

The distributed layer (accounts, roles, permissions, teams, scopes, device↔
account links, audit) stays exactly as it is: every table, module, route and
record is preserved, so flipping the switch back restores the full system
without a migration, a rewrite or data loss.

While the switch is OFF the application runs as a single-account install:

* the multi-account *interface* is hidden (account administration, team
  management, role selection) — see ``capabilities()`` and the dashboard;
* the multi-account *operations* are refused by the backend itself (403
  ``multi_account_disabled``), because hiding a button is not security;
* everything a single-account install actually needs keeps working: sign-in,
  device registration, heartbeats/presence, the sync stream, correction
  results, report delivery, updates, schools/classes/assignments and the
  shared-backend connection.

Design decisions, each deliberate:

* **Default OFF.** ``DEFAULT_ENABLED = False`` — a fresh install is a
  single-account install.
* **One source of truth.** ``[accounts] multi_account`` in ``data/config.toml``
  (written by the same ``save_config`` path as every other setting), with the
  environment variable ``MULTI_ACCOUNT_ENABLED`` (``1``/``true``/``on`` /
  ``0``/``false``/``off``) as an explicit operator/test override that wins.
* **Fail closed.** An unreadable config cannot silently *enable* the layer: the
  flag is read through ``get_config()``, whose failure path keeps the default.
* **Explicit rule table.** :data:`BLOCKED_RULES` names every disabled
  operation; :func:`blocked_operation` is the single implementation both the
  middleware and the tests use, so no route can be "forgotten" in one place.
* **Never a data operation.** Nothing here deletes, migrates, truncates or
  anonymises a row — disabling is availability, not storage.
"""
from __future__ import annotations

import logging
import os
import re

log = logging.getLogger("app.multi_account")

#: The configuration setting (``data/config.toml``) and its environment override.
CONFIG_SECTION = "accounts"
CONFIG_KEY = "multi_account"
ENV_VAR = "MULTI_ACCOUNT_ENABLED"

#: A fresh install (and any install that never touched the setting) is a
#: single-account install.
DEFAULT_ENABLED = False

#: Error code returned by every refused multi-account operation, so a client
#: can distinguish "this install has the feature switched off" from "you are
#: not allowed to do this".
BLOCKED_CODE = "multi_account_disabled"

_TRUTHY = {"1", "true", "yes", "on", "enabled"}
_FALSY = {"0", "false", "no", "off", "disabled"}

#: Operations that only make sense with more than one account. Paths are
#: matched AFTER the ``/api/distributed`` prefix has been stripped, so the
#: table stays readable and cannot drift with the router prefix.
#:
#: Deliberately NOT listed: ``login``/``logout``/``me``/``change-password``
#: (the single account still signs in), ``ping``/``capabilities`` (public
#: status), ``heartbeat``/``register-worker``/``device/register`` (presence and
#: device identity — the connection tests need these), ``agent/state`` (the
#: local Tiny Agent switch), ``schools``/``classes``/``mappings`` (the academic
#: structure the correction workflow uses), ``workers`` (presence),
#: ``events``/``results``/``report-deliveries`` (sync + report delivery),
#: ``errors``/``updates``/``releases``/``audit``/``sync-monitor``
#: (diagnostics and fleet updates).
BLOCKED_RULES: tuple[tuple[tuple[str, ...], str, str], ...] = (
    # -- account administration -------------------------------------------
    (("GET",), r"/accounts$",
     "listing the installation's accounts"),
    (("POST",), r"/accounts$",
     "creating accounts"),
    (("POST",), r"/accounts/[^/]+/scope$",
     "changing an account's school/class scope"),
    (("POST",), r"/accounts/[^/]+/disable$",
     "enabling or disabling another account"),
    (("POST",), r"/accounts/[^/]+/username$",
     "renaming an account"),
    (("POST",), r"/accounts/[^/]+/display-name$",
     "renaming an account"),
    (("POST",), r"/accounts/[^/]+/password-reset$",
     "resetting another account's password"),
    (("POST",), r"/accounts/[^/]+/roles$",
     "assigning roles to an account"),
    # -- teams (team-based account discovery + team sharing) ---------------
    (("GET", "POST", "PUT", "PATCH", "DELETE"), r"/(?:me/)?teams(?:/.*)?$",
     "team management and team-scoped sharing"),
    # -- device ↔ account discovery and linking ---------------------------
    (("GET",), r"/device/discovery$",
     "discovering the accounts of a shared device"),
    (("GET",), r"/devices$",
     "listing linked devices"),
    (("POST",), r"/devices/[^/]+/link$",
     "linking a device to another account"),
    (("POST",), r"/devices/[^/]+/revoke$",
     "revoking another account's device"),
)

_COMPILED = tuple((methods, re.compile(pattern), label)
                  for methods, pattern, label in BLOCKED_RULES)

#: Router prefix every rule above is relative to.
ROUTE_PREFIX = "/api/distributed"


def _env_value() -> bool | None:
    """The environment override, or None when it is not set/invalid."""
    raw = (os.environ.get(ENV_VAR) or "").strip().lower()
    if raw in _TRUTHY:
        return True
    if raw in _FALSY:
        return False
    return None


def _config_value() -> bool | None:
    """The persisted preference, or None when it cannot be read."""
    try:
        from .config import get_config
        return bool(get_config().accounts.multi_account)
    except Exception:  # noqa: BLE001 — a broken config keeps the default
        return None


def enabled() -> bool:
    """Is multi-account mode on? (env override > config > default OFF)."""
    env = _env_value()
    if env is not None:
        return env
    config = _config_value()
    return DEFAULT_ENABLED if config is None else config


def source() -> str:
    """Where the effective value came from: ``env`` / ``config`` / ``default``.

    ``default`` means "this installation never changed the setting", which is
    why a value equal to :data:`DEFAULT_ENABLED` is reported as the default
    rather than as a configured one: the two are indistinguishable in effect
    and the distinction is not worth a fake precision.
    """
    if _env_value() is not None:
        return "env"
    config = _config_value()
    if config is None or config == DEFAULT_ENABLED:
        return "default"
    return "config"


def set_enabled(value: bool) -> bool:
    """Persist the switch and return the (re-read) effective value.

    Kept deliberately small: one dataclass field, written through the normal
    ``save_config`` path so the setting survives restarts and upgrades and
    stays human-editable in ``config.toml``.
    """
    from .config import get_config, save_config
    cfg = get_config()
    cfg.accounts.multi_account = bool(value)
    save_config(cfg)
    log.info("multi-account mode %s (persisted to [%s] %s)",
             "ENABLED" if value else "DISABLED", CONFIG_SECTION, CONFIG_KEY)
    return enabled()


def blocked_operation(method: str, path: str) -> str | None:
    """The label of the disabled operation this request performs, else None.

    ``path`` is the full request path; anything outside the distributed router
    is never a multi-account operation.
    """
    if not path.startswith(ROUTE_PREFIX):
        return None
    remainder = path[len(ROUTE_PREFIX):] or "/"
    verb = (method or "GET").upper()
    for methods, pattern, label in _COMPILED:
        if verb in methods and pattern.match(remainder):
            return label
    return None


def refusal_message(operation: str) -> str:
    """Operator-facing reason, including how to turn the feature back on."""
    return (f"Multi-account mode is off on this installation, so "
            f"{operation} is not available. Existing accounts, teams and "
            f"audit records are untouched; an owner can switch multi-account "
            f"mode back on in Settings, or set [{CONFIG_SECTION}] "
            f"{CONFIG_KEY} = true (environment override: {ENV_VAR}=1).")


def capabilities() -> dict:
    """What the client needs to adapt its interface (no secrets, no data).

    Public on the distributed router: the login screen and the dashboard read
    it before a sign-in to decide whether to show multi-account surfaces.
    """
    return {
        "multi_account": enabled(),
        "default": DEFAULT_ENABLED,
        "source": source(),
        "setting": f"[{CONFIG_SECTION}] {CONFIG_KEY}",
        "env_var": ENV_VAR,
        # Disabling is immediately effective for every request (no restart),
        # because both the middleware and the UI read this module's answer.
        "restart_required": False,
    }


class MultiAccountGuard:
    """Refuse disabled multi-account operations at the edge (ASGI middleware).

    A middleware (rather than a per-route dependency) is used on purpose: it
    cannot be bypassed by a route added later, and the decision itself lives in
    :func:`blocked_operation` where it can be unit-tested exhaustively.

    Ordering, stated precisely (measured, not assumed): this runs INSIDE
    ``PublicAuthMiddleware`` for everything the session gate guards, so those
    requests are authenticated first. ``/api/distributed/*`` is deliberately
    passed through by the session gate (bearer tokens, see auth_gate) and
    authenticates per route, so a request there is answered by this guard
    BEFORE that per-route authentication: with the layer off it gets 403
    ``multi_account_disabled``, and with the layer on the unchanged 401. Both
    are refusals that serve no data; a 403 here never authenticates anybody.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http" or enabled():
            await self.app(scope, receive, send)
            return
        operation = blocked_operation(scope.get("method", "GET"),
                                      scope.get("path", ""))
        if operation is None:
            await self.app(scope, receive, send)
            return
        from starlette.responses import JSONResponse

        from .errors import AppError
        error = AppError(refusal_message(operation), status=403,
                         code=BLOCKED_CODE)
        log.info("multi-account operation refused: %s %s (%s)",
                 scope.get("method"), scope.get("path"), operation)
        # Returned directly (not raised): this middleware sits outside the
        # application's exception handlers, so the envelope is built here in
        # exactly the same shape an AppError would produce.
        response = JSONResponse(status_code=error.status,
                                content=error.to_dict(include_detail=False))
        await response(scope, receive, send)
