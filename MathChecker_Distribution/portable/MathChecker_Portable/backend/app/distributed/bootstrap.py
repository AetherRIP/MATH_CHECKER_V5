"""Distributed bootstrap: the additive tables, and — ONLY with Multi-Account
Mode on — the initial DEVELOPER account with a one-time setup code.

Security: the one-time code is written to ``data/distributed_bootstrap.txt``
(chmod 600) and shown once in the server log — never over the API, never in
the database, never in a success response afterwards (§59).

Why the accounts are conditional
--------------------------------
These accounts exist to administer the *multi-account* layer: the distributed
API authenticates accounts with its own bearer tokens, so with none present the
feature could be switched on and be unreachable (``POST /accounts`` needs an
existing developer). A single-account installation — the shipped default, see
``app.multi_account`` — must NOT have them. ``auth_gate._has_any_account()``
counts any account, so creating them on a fresh install made the app report
itself as already configured: a brand-new copy opened on a sign-in form
demanding a password that existed only in a file nobody was told about, and the
"Create the first account" path was unreachable. The tables, by contrast, are
ensured on every start, because the single-account essentials (device
registration, presence, sync, report delivery, updates) use them.
"""
from __future__ import annotations

import json
import os
import secrets
import time

from .. import db
from ..logging_setup import log_event
from . import accounts
from .schema import ensure_tables

BOOTSTRAP_FILE = "distributed_bootstrap.txt"


def accounts_wanted() -> bool:
    """Should the DEVELOPER/TEACHER accounts exist on this installation?

    Only in Multi-Account Mode: ``[accounts] multi_account`` in
    ``data/config.toml``, or the ``MULTI_ACCOUNT_ENABLED`` override, default
    OFF (``app.multi_account`` owns that decision and is the single source of
    truth — this is deliberately not a second copy of the rule).
    """
    from .. import multi_account
    return multi_account.enabled()


def bootstrap(*, create_accounts: bool | None = None) -> dict:
    """Ensure the tables; create the admin accounts when they are wanted.

    ``create_accounts`` overrides the Multi-Account Mode decision — the switch
    itself passes ``True`` when it turns the layer on, and tests use it to
    exercise both branches without editing configuration. Returns the original
    ``bootstrap_needed`` key plus ``accounts``: ``created`` / ``present`` /
    ``skipped``.
    """
    ensure_tables()
    if create_accounts is None:
        create_accounts = accounts_wanted()
    if not create_accounts:
        return {"bootstrap_needed": False, "accounts": "skipped",
                "reason": "multi-account mode is off"}
    if accounts.developer_count() > 0:
        return {"bootstrap_needed": False, "accounts": "present"}

    password = secrets.token_urlsafe(16)
    dev = accounts.create_account(
        username="developer",
        display_name="Developer",
        password=password,
        role="DEVELOPER",
        created_by=None,
        permissions=["*"])
    # Also create the initial TEACHER account (§8): only Developer exists at
    # first login; the Developer sets the real teacher credentials there.
    teacher_password = secrets.token_urlsafe(16)
    teacher = accounts.create_account(
        username="teacher",
        display_name="Main Teacher",
        password=teacher_password,
        role="TEACHER",
        created_by=dev["id"])

    from ..config import DATA_DIR
    path = DATA_DIR / BOOTSTRAP_FILE
    payload = {
        "developer": {"username": "developer", "password": password},
        "teacher": {"username": "teacher", "password": teacher_password},
        "created_at": time.time(),
    }
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        existing = json.loads(path.read_text(encoding="utf-8")) \
            if path.exists() else {}
        existing.update(payload)
        path.write_text(json.dumps(existing, indent=2), encoding="utf-8")
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    except OSError:
        log_event("distributed", "bootstrap credentials could not be written",
                  level=os.environ.get("MC5_LOG_LEVEL", "WARNING"))

    log_event("distributed", "bootstrap accounts created",
              developer="developer", teacher="teacher")
    return {"bootstrap_needed": True, "accounts": "created",
            "developer_id": dev["id"], "teacher_id": teacher["id"],
            "credentials_path": str(path)}
