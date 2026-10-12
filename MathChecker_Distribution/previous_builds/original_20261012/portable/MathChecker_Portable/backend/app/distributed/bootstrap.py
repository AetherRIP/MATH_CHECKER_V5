"""Distributed bootstrap: creates the additive tables and, on a fresh
database, the initial DEVELOPER account with a one-time setup code.

Security: the one-time code is written to ``data/distributed_bootstrap.txt``
(chmod 600) and shown once in the server log — never over the API, never in
the database, never in a success response afterwards (§59).
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


def bootstrap() -> dict:
    ensure_tables()
    if accounts.developer_count() > 0:
        return {"bootstrap_needed": False}

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
    return {"bootstrap_needed": True, "developer_id": dev["id"],
            "teacher_id": teacher["id"], "credentials_path": str(path)}
