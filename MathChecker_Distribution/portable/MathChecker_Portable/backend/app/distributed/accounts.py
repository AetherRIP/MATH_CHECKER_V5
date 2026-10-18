"""Accounts, authentication tokens, roles and scoping (§6-§10, §59).

* Passwords are PBKDF2-HMAC-SHA256 (200k iterations, random salt), stored
  base64 — never plaintext, never logged.
* Tokens are random; only the SHA-256 of a token is stored, so a database
  leak cannot be replayed as a session.
* Roles: DEVELOPER > TEACHER > CO_TEACHER / ASSISTANT. Every permission check
  happens SERVER-SIDE (§59): the client never declares its own role.
* Scope: CO_TEACHERs and ASSISTANTs only see schools/classes assigned in
  ``account_scope`` (class_id NULL = the whole school). TEACHER/DEVELOPER see
  everything.
"""
from __future__ import annotations

import base64
import hashlib
import json
import secrets
import time
import uuid

from .. import db

ROLES = ("DEVELOPER", "TEACHER", "CO_TEACHER", "ASSISTANT")
# Highest privilege first. Used to pick the PRIMARY role when an account holds
# several (a Developer+Assistant is primarily a Developer) and to order the
# dashboard switcher.
ROLE_PRECEDENCE = ("DEVELOPER", "TEACHER", "CO_TEACHER", "ASSISTANT")
PBKDF2_ITERATIONS = 200_000
TOKEN_HOURS = 24 * 14  # workers stay logged in across restarts

# Configurable permission defaults (§9: not hard-coded per user).
DEFAULT_PERMISSIONS: dict[str, list[str]] = {
    "DEVELOPER": ["*"],
    "TEACHER": [
        "schools.manage", "accounts.manage_non_teacher", "assignments.manage",
        "automation.manage", "corrections.view_all", "corrections.retry",
        "workers.view", "workers.pause", "reports.view_all", "diagnostics.view",
        "agents.control",
    ],
    "CO_TEACHER": [
        "corrections.view", "corrections.retry", "reports.view",
        "workers.view_own", "diagnostics.limited",
        "agents.control",
    ],
    "ASSISTANT": [
        "corrections.run", "corrections.view", "corrections.retry",
        "workers.manage_own", "reports.view", "diagnostics.limited",
        "agents.control",
    ],
}

_SENSITIVE_LOG_KEYS = ("password", "token", "secret", "key", "authorization",
                       "cookie")


def hash_password(password: str) -> tuple[str, str]:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt,
                                 PBKDF2_ITERATIONS, dklen=32)
    return (base64.b64encode(salt).decode("ascii"),
            base64.b64encode(digest).decode("ascii"))


def verify_password(password: str, salt_b64: str, hash_b64: str) -> bool:
    try:
        salt = base64.b64decode(salt_b64)
        expected = base64.b64decode(hash_b64)
        candidate = hashlib.pbkdf2_hmac("sha256", (password or "").encode("utf-8"),
                                        salt, PBKDF2_ITERATIONS, dklen=32)
        import hmac as _hmac
        return _hmac.compare_digest(candidate, expected)
    except (ValueError, TypeError):
        return False


def _clean(text: str | None, limit: int = 64) -> str:
    return (text or "").strip()[:limit]


def sanitize_for_log(payload: dict) -> dict:
    """Redact sensitive values before anything reaches a log or error report."""
    out: dict = {}
    for k, v in (payload or {}).items():
        if any(s in str(k).lower() for s in _SENSITIVE_LOG_KEYS):
            out[k] = "[redacted]"
        elif isinstance(v, dict):
            out[k] = sanitize_for_log(v)
        else:
            out[k] = v
    return out


# -- accounts -------------------------------------------------------------------

_TABLES_READY = False


def ensure_tables_ready() -> None:
    """Create the distributed tables once per process (idempotent)."""
    global _TABLES_READY
    if _TABLES_READY:
        return
    from .schema import ensure_tables as _ensure
    _ensure()
    _TABLES_READY = True


def account_count() -> int:
    ensure_tables_ready()
    return int(db.query_one("SELECT COUNT(*) n FROM accounts")["n"])


def developer_count() -> int:
    # Counts accounts that hold DEVELOPER as ANY of their roles, so a
    # Developer+Assistant account still satisfies a developer check.
    rows = db.query("SELECT role, roles FROM accounts WHERE active = 1")
    return sum(1 for r in rows if "DEVELOPER" in account_roles(dict(r)))


def create_account(*, username: str, display_name: str, password: str,
                   role: str | None = None, created_by: str | None = None,
                   permissions: list[str] | None = None,
                   active: bool = True,
                   roles: list[str] | None = None) -> dict:
    """Create an account.

    Pass ``role`` for the single-role case (unchanged for existing callers) or
    ``roles`` for a multi-role account such as ["ASSISTANT","DEVELOPER"]. The
    stored ``role`` column is the primary (most privileged) role; the full set
    lives in ``roles`` so one person can assist AND develop (§6-§10).
    """
    ensure_tables_ready()
    username = _clean(username).lower()
    display_name = _clean(display_name, 120) or username

    if roles is not None:
        requested = [str(r).strip().upper() for r in roles if str(r).strip()]
    elif role:
        requested = [str(role).strip().upper()]
    else:
        requested = []
    unknown = [r for r in requested if r not in ROLES]
    if unknown:
        raise ValueError(f"unknown role(s) {unknown}; role must be one of {ROLES}")
    if not requested:
        raise ValueError(f"at least one role is required (one of {ROLES})")
    resolved = [r for r in ROLE_PRECEDENCE if r in set(requested)]
    primary = primary_role(resolved)

    if not username or not password or len(password) < 8:
        raise ValueError("username and a password of at least 8 characters are required")
    if db.query_one("SELECT 1 FROM accounts WHERE username = ?", (username,)):
        raise ValueError("username already exists")
    salt, digest = hash_password(password)
    account_id = uuid.uuid4().hex
    # Explicit permissions win when supplied; otherwise the union of defaults
    # for every held role, so a multi-role account is never under-privileged.
    if permissions is not None:
        perms = json.dumps(permissions)
    else:
        perms = json.dumps(_default_union(resolved))
    with db.transaction() as conn:
        conn.execute(
            """INSERT INTO accounts(id, username, display_name, password_salt,
               password_hash, role, roles, active, permissions, created_by,
               created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            (account_id, username, display_name, salt, digest, primary,
             json.dumps(resolved), 1 if active else 0, perms, created_by,
             time.time()))
    from . import audit
    audit.record(action="account.created", target_type="account",
                 target_id=account_id, actor_account=created_by,
                 actor_role=_actor_role(created_by), affected_account=account_id,
                 outcome="success" if active else "failure",
                 details={"roles": resolved, "username": username,
                          "active": bool(active)})
    return get_account(account_id)


def _actor_role(account_id: str | None) -> str | None:
    """Primary role of an actor id, for the audit trail (never a secret)."""
    if not account_id:
        return None
    actor = get_account(account_id) if account_id else None
    return actor["role"] if actor else None


def set_roles(account_id: str, roles: list[str], *, by: str | None = None,
              by_role: str | None = None) -> dict | None:
    """Replace an account's role set (multi-role support, §6-§10)."""
    requested = [str(r).strip().upper() for r in roles if str(r).strip()]
    unknown = [r for r in requested if r not in ROLES]
    if unknown:
        raise ValueError(f"unknown role(s) {unknown}; role must be one of {ROLES}")
    if not requested:
        raise ValueError("an account must keep at least one role")
    resolved = [r for r in ROLE_PRECEDENCE if r in set(requested)]
    current = get_account(account_id)
    if current is None:
        raise ValueError("account not found")
    stored = json.loads(current.get("permissions") or "[]")
    # Follow the new roles ONLY when the permissions were simply the old role
    # defaults — a hand-granted or hand-revoked list is never silently
    # rewritten by a role change.
    if not stored or stored == _default_union(account_roles(current)):
        db.execute(
            "UPDATE accounts SET role = ?, roles = ?, permissions = ? WHERE id = ?",
            (primary_role(resolved), json.dumps(resolved),
             json.dumps(_default_union(resolved)), account_id))
    else:
        db.execute("UPDATE accounts SET role = ?, roles = ? WHERE id = ?",
                   (primary_role(resolved), json.dumps(resolved), account_id))
    from . import audit
    audit.record(action="account.roles_changed", target_type="account",
                 target_id=account_id, actor_account=by, actor_role=by_role,
                 affected_account=account_id,
                 details={"roles": resolved, "previous": account_roles(current)})
    return get_account(account_id)


def get_account(account_id: str) -> dict | None:
    ensure_tables_ready()
    row = db.query_one("SELECT * FROM accounts WHERE id = ?", (account_id,))
    return dict(row) if row else None


def get_account_by_username(username: str) -> dict | None:
    ensure_tables_ready()
    row = db.query_one("SELECT * FROM accounts WHERE username = ?",
                       (_clean(username).lower(),))
    return dict(row) if row else None


def list_accounts() -> list[dict]:
    rows = db.query(
        "SELECT id, username, display_name, role, roles, active, created_at, "
        "last_login, last_device_id, permissions FROM accounts ORDER BY role, username")
    out = []
    for row in rows:
        item = dict(row)
        # `roles` is the authoritative set; `role` is kept for ordering/legacy.
        item["roles"] = account_roles(item)
        item["permissions"] = permissions_for(item)
        out.append(item)
    return out


def set_account_active(account_id: str, active: bool) -> None:
    db.execute("UPDATE accounts SET active = ? WHERE id = ?",
               (1 if active else 0, account_id))


#: Roles a TEACHER may administer. A Teacher can never touch a TEACHER or
#: DEVELOPER account (including another Main Teacher's) — only the DEVELOPER
#: can (§2).
MANAGEABLE_BY_TEACHER = ("CO_TEACHER", "ASSISTANT")


def can_manage_account(actor: dict | None, target: dict | None) -> bool:
    """Whether ``actor`` may edit/administrate ``target`` (§2).

    Server-side truth, never trusted from the client:

    * a DEVELOPER may manage any account;
    * a TEACHER may manage CO_TEACHER/ASSISTANT accounts only — never another
      TEACHER or a DEVELOPER;
    * everybody may manage their own account (self-service endpoints).
    """
    if actor is None or target is None:
        return False
    if actor["id"] == target["id"]:
        return True
    actor_roles = set(account_roles(actor))
    if "DEVELOPER" in actor_roles:
        return True
    if "TEACHER" in actor_roles:
        return bool(set(account_roles(target)) & set(MANAGEABLE_BY_TEACHER))
    return False


def assignable_roles(actor: dict | None) -> tuple[str, ...]:
    """The role set ``actor`` may assign, so a Teacher cannot mint a peer."""
    if actor is None:
        return ()
    if "DEVELOPER" in set(account_roles(actor)):
        return ROLES
    if "TEACHER" in set(account_roles(actor)):
        return MANAGEABLE_BY_TEACHER
    return ()


def _write_password(account_id: str, password: str) -> None:
    if not password or len(password) < 8:
        raise ValueError("password of at least 8 characters required")
    salt, digest = hash_password(password)
    db.execute("UPDATE accounts SET password_salt = ?, password_hash = ? WHERE id = ?",
               (salt, digest, account_id))


def set_password(account_id: str, password: str) -> None:
    _write_password(account_id, password)
    from . import audit
    audit.record(action="account.password_changed", target_type="account",
                 target_id=account_id, affected_account=account_id)


def admin_reset_password(account_id: str, password: str, *, by: str | None = None,
                         by_role: str | None = None) -> int:
    """Reset ANOTHER account's password (§2/§3).

    The actor chooses the new password and never sees the old one; every
    existing session for the target is revoked so a reset actually takes
    effect. The event is audited WITHOUT the password or its hash.
    """
    current = get_account(account_id)
    if current is None:
        raise ValueError("account not found")
    _write_password(account_id, password)
    revoked = revoke_all_tokens(account_id)
    from . import audit
    audit.record(action="account.password_reset", target_type="account",
                 target_id=account_id, actor_account=by, actor_role=by_role,
                 affected_account=account_id, details={"sessions_revoked": revoked})
    return revoked


def set_username(account_id: str, username: str, *, by: str | None = None,
                 by_role: str | None = None) -> dict:
    """Change a login username after a uniqueness check (§3).

    Lowercased for consistency with sign-in; the old value is recorded in the
    audit event so the change is traceable.
    """
    ensure_tables_ready()
    username = _clean(username).lower()
    if len(username) < 3:
        raise ValueError("username must be at least 3 characters")
    if not username.replace("_", "").replace(".", "").replace("-", "").isalnum():
        raise ValueError("username may contain only letters, digits, dot, dash and underscore")
    current = get_account(account_id)
    if current is None:
        raise ValueError("account not found")
    clash = db.query_one("SELECT id FROM accounts WHERE username = ?", (username,))
    if clash and clash["id"] != account_id:
        raise ValueError("username already exists")
    old = current["username"]
    if old == username:
        return current
    db.execute("UPDATE accounts SET username = ? WHERE id = ?", (username, account_id))
    from . import audit
    audit.record(action="account.username_changed", target_type="account",
                 target_id=account_id, actor_account=by, actor_role=by_role,
                 affected_account=account_id, details={"from": old, "to": username})
    return get_account(account_id)


def set_display_name(account_id: str, display_name: str) -> None:
    db.execute("UPDATE accounts SET display_name = ? WHERE id = ?",
               (_clean(display_name, 120), account_id))


def account_roles(account: dict | None) -> list[str]:
    """Every role the account holds, most privileged first (§6-§10).

    Read from the ``roles`` JSON array when present. Accounts created before
    multi-role support have ``roles = NULL`` and are read as their single
    legacy ``role`` value, so no existing account changes behaviour.
    """
    if not account:
        return []
    raw = account.get("roles")
    values: list[str] = []
    if raw:
        try:
            parsed = json.loads(raw) if isinstance(raw, str) else raw
            if isinstance(parsed, list):
                values = [str(v).strip().upper() for v in parsed]
        except (ValueError, TypeError):
            values = []
    if not values:
        legacy = (account.get("role") or "").strip().upper()
        values = [legacy] if legacy else []
    # De-duplicate, drop anything unknown, then order by privilege.
    seen = {r for r in values if r in ROLES}
    return [r for r in ROLE_PRECEDENCE if r in seen]


def has_role(account: dict | None, role: str) -> bool:
    return (role or "").strip().upper() in account_roles(account)


def primary_role(roles: list[str]) -> str:
    """The most privileged role, used as the ``role`` column value."""
    for role in ROLE_PRECEDENCE:
        if role in roles:
            return role
    return "ASSISTANT"


def _default_union(roles: list[str]) -> list[str]:
    """The permission list implied by a role set (deduplicated, ordered)."""
    merged: list[str] = []
    for role in roles:
        for perm in DEFAULT_PERMISSIONS.get(role, []):
            if perm not in merged:
                merged.append(perm)
    return merged


def permissions_for(account: dict) -> list[str]:
    """Effective permissions — exactly the stored ``permissions`` column.

    Deliberately NOT merged with role defaults at read time. The stored list
    is authoritative so an administrator can still REVOKE rights by setting
    ``[]``; a read-time union would silently undo that. Multi-role needs no
    fallback here because the union is computed and WRITTEN when the account
    is created or its roles change (see ``create_account``/``set_roles``).
    """
    return json.loads(account.get("permissions") or "[]")


def has_permission(account: dict, permission: str) -> bool:
    perms = permissions_for(account)
    if "*" in perms:
        return True
    return permission in perms


def set_scope(account_id: str, scope: list[dict]) -> None:
    """Replace the account's school/class scope.

    ``scope`` items: {"school_id": ..., "class_id": ... | None}.
    Only meaningful for CO_TEACHER/ASSISTANT; TEACHER/DEVELOPER keep global.
    """
    with db.transaction() as conn:
        conn.execute("DELETE FROM account_scope WHERE account_id = ?",
                     (account_id,))
        for item in scope or []:
            school_id = _clean(item.get("school_id"), 64)
            if not school_id:
                continue
            class_id = _clean(item.get("class_id"), 64) or None
            conn.execute(
                "INSERT OR IGNORE INTO account_scope(account_id, school_id, class_id)"
                " VALUES(?,?,?)", (account_id, school_id, class_id))
    from . import audit
    audit.record(action="account.scope_changed", target_type="account",
                 target_id=account_id, details={"entries": len(scope or [])})


def scoped_ids(account: dict) -> tuple[set[str], set[str]]:
    """(school_ids, class_ids) the account may see; empty set = unrestricted."""
    # Unrestricted if ANY held role grants global visibility — an account that
    # is both an Assistant and a Developer must not be narrowed to the
    # assistant's scope (§7: switching dashboards changes nothing).
    if set(account_roles(account)) & {"DEVELOPER", "TEACHER"}:
        return set(), set()
    rows = db.query("SELECT school_id, class_id FROM account_scope "
                    "WHERE account_id = ?", (account["id"],))
    schools: set[str] = set()
    classes: set[str] = set()
    for row in rows:
        if row["class_id"]:
            # A CLASS-scoped entry grants that one class — it must NOT also
            # imply the whole school, or an assistant assigned to 9B could
            # read every other class in the school (§7 "an assistant must not
            # access an unauthorised class"). Only a NULL class_id grants the
            # whole school, as the scope model documents.
            classes.add(row["class_id"])
        else:
            schools.add(row["school_id"])
    return schools, classes


def can_see_class(account: dict, school_id: str, class_id: str | None) -> bool:
    schools, classes = scoped_ids(account)
    if not schools and not classes:
        # Empty scope is unrestricted ONLY for roles that grant global access.
        # A newly created assistant with no assignments must see no classes.
        return bool(set(account_roles(account)) & {"DEVELOPER", "TEACHER"})
    if class_id and class_id in classes:
        return True
    return bool(school_id and school_id in schools)


def authenticate(username: str, password: str) -> dict | None:
    """Return the account row on success; None on any failure (§59)."""
    ensure_tables_ready()
    account = get_account_by_username(username)
    if not account or not account.get("active"):
        return None
    if not verify_password(password, account["password_salt"],
                           account["password_hash"]):
        return None
    return account


# -- tokens ---------------------------------------------------------------------

def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def issue_token(account_id: str, device_id: str | None = None) -> str:
    ensure_tables_ready()
    token = secrets.token_urlsafe(32)
    now = time.time()
    db.execute(
        "INSERT INTO auth_tokens(token_hash, account_id, device_id, created_at, "
        "expires_at) VALUES(?,?,?,?,?)",
        (_token_hash(token), account_id, device_id, now, now + TOKEN_HOURS * 3600))
    return token


def resolve_token(token: str) -> dict | None:
    ensure_tables_ready()
    if not token:
        return None
    row = db.query_one(
        """SELECT a.* FROM auth_tokens t JOIN accounts a ON a.id = t.account_id
           WHERE t.token_hash = ? AND t.expires_at > ? AND a.active = 1""",
        (_token_hash(token), time.time()))
    return dict(row) if row else None


def revoke_token(token: str) -> None:
    db.execute("DELETE FROM auth_tokens WHERE token_hash = ?",
               (_token_hash(token),))


def revoke_all_tokens(account_id: str) -> int:
    """Invalidate every active session for an account (§password change).

    Used after a password change so a stolen or old token cannot keep working.
    Returns the number of sessions revoked.
    """
    row = db.query_one("SELECT COUNT(*) n FROM auth_tokens WHERE account_id = ?",
                       (account_id,))
    db.execute("DELETE FROM auth_tokens WHERE account_id = ?", (account_id,))
    return int(row["n"]) if row else 0
