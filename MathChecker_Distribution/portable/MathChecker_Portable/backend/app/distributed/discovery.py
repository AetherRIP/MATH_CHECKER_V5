"""Automatic device registration + account/device discovery (§extension 3-5).

This is the ONE piece a freshly installed machine needs before any human signs
in. The installer contains **no** usernames, passwords or tokens (§1); the new
device instead:

1. generates its own persistent device id (already done by ``tiny_agent``),
2. calls ``register`` with no credentials at all and receives a **short-lived,
   revocable DEVICE credential** — never an account credential,
3. uses that credential to ``discover`` the registered TEACHER/DEVELOPER
   accounts, their device availability, and whether this machine is currently
   authorised to synchronise with them.

Security model (deliberate, each point tested):

* A device credential can **only** register, heartbeat and discover. It can
  never name an account, read a report or run a correction — ``resolve`` returns
  a device, never an account (§8 "a newly installed agent cannot impersonate a
  Teacher or Developer").
* Only the SHA-256 of the credential secret is stored, so a database leak
  cannot be replayed as a session (§8).
* Discovery never returns usernames, scopes or permissions — only the fields the
  spec allows for discovery (role, display name, availability, linkage). It is
  available only to a caller holding a valid device credential, so an anonymous
  network peer learns nothing.
* Accounts created **after** the installer shipped are discovered automatically:
  discovery reads the live accounts table, there is no baked-in list (§3/§4).
* Linking is explicit. Finding an account grants nothing on its own; until an
  administrator (or the account itself) links the device, ``sync_authorized`` is
  False and no student data can flow (§5 "never send reports merely because an
  account is online").
"""
from __future__ import annotations

import hashlib
import secrets
import time

from .. import db
from ..logging_setup import log_event
from . import accounts, agents, audit, teams

#: A device credential is short-lived on purpose and the agent silently renews
#: it before it expires (§3 "short-lived authentication session").
CREDENTIAL_HOURS = 24

#: Roles whose accounts an unauthenticated machine may discover (§4).
DISCOVERABLE_ROLES = ("DEVELOPER", "TEACHER")


def _clean(text: str | None, limit: int = 64) -> str:
    return (text or "").strip()[:limit]


def _hash(credential: str) -> str:
    return hashlib.sha256(credential.encode("utf-8")).hexdigest()


def _ensure() -> None:
    accounts.ensure_tables_ready()


# -- registration ---------------------------------------------------------------

def register(device_id: str, *, label: str | None = None,
             app_version: str | None = None,
             platform: str | None = None, arch: str | None = None,
             ip: str | None = None) -> dict:
    """Register this installation and issue a fresh device credential.

    Idempotent by device id: re-registering (e.g. after the credential expired)
    rotates the secret and invalidates any previous one, so a stolen old
    credential stops working the moment the machine checks in again.
    """
    _ensure()
    device_id = _clean(device_id)
    if not device_id:
        raise ValueError("device_id is required")
    if len(device_id) < 8:
        raise ValueError("device_id is too short to be a real generated id")

    # The device row is created/updated first so the credential FK is valid.
    # Platform is recorded here so platform-specific releases can be filtered
    # (updates.release_compatible) from the first report onwards.
    device = agents.register_device(device_id, label=label, platform=platform,
                                    arch=arch)
    if ip:
        db.execute("UPDATE devices SET last_ip = ? WHERE device_id = ?",
                   (_clean(ip, 64), device_id))

    now = time.time()
    with db.transaction() as conn:
        # Rotate: every previously issued, still-valid credential for THIS
        # device is revoked, so only the newest secret works.
        conn.execute(
            "UPDATE device_credentials SET revoked_at = ? "
            "WHERE device_id = ? AND revoked_at IS NULL", (now, device_id))
        credential = secrets.token_urlsafe(32)
        conn.execute(
            "INSERT INTO device_credentials(credential_hash, device_id, label, "
            "issued_at, expires_at) VALUES(?,?,?,?,?)",
            (_hash(credential), device_id, _clean(label, 120) or None, now,
             now + CREDENTIAL_HOURS * 3600))

    audit.record(action="device.registered", target_type="device",
                 target_id=device_id, details={"label": _clean(label, 120)})
    log_event("distributed", "device registered", device_id=device_id[:12])
    return {
        "device_id": device_id,
        "credential": credential,
        "expires_at": now + CREDENTIAL_HOURS * 3600,
        "credential_hours": CREDENTIAL_HOURS,
        # Registration alone never grants account linkage or sync rights.
        "account_id": None,
        "discovery": discover(device_id),
    }


def resolve(device_id: str, credential: str) -> dict | None:
    """Validate a device credential; return the DEVICE row, never an account."""
    _ensure()
    device_id = _clean(device_id)
    if not device_id or not credential:
        return None
    row = db.query_one(
        """SELECT * FROM device_credentials
           WHERE credential_hash = ? AND device_id = ?
             AND revoked_at IS NULL AND expires_at > ?""",
        (_hash(credential), device_id, time.time()))
    if not row:
        return None
    db.execute(
        "UPDATE device_credentials SET last_used_at = ?, use_count = use_count + 1 "
        "WHERE credential_hash = ?", (time.time(), row["credential_hash"]))
    device = db.query_one("SELECT * FROM devices WHERE device_id = ?", (device_id,))
    return dict(device) if device else None


def credential_expiry(device_id: str) -> float | None:
    row = db.query_one(
        "SELECT MAX(expires_at) e FROM device_credentials "
        "WHERE device_id = ? AND revoked_at IS NULL", (_clean(device_id),))
    return float(row["e"]) if row and row["e"] else None


def revoke_device(device_id: str, *, by: str | None = None) -> int:
    """Revoke every credential for a device and drop its links (§8).

    The device stays in the registry (its history and audit trail are kept) but
    can no longer authenticate, discover or heartbeat as a known device.
    """
    _ensure()
    device_id = _clean(device_id)
    now = time.time()
    with db.transaction() as conn:
        cur = conn.execute(
            "UPDATE device_credentials SET revoked_at = ? "
            "WHERE device_id = ? AND revoked_at IS NULL", (now, device_id))
        revoked = cur.rowcount if cur.rowcount is not None else 0
        conn.execute("UPDATE device_links SET revoked_at = ? "
                     "WHERE device_id = ? AND revoked_at IS NULL", (now, device_id))
        conn.execute("UPDATE devices SET account_id = NULL WHERE device_id = ?",
                     (device_id,))
    audit.record(action="device.revoked", target_type="device", target_id=device_id,
                 details={"by": by, "credentials_revoked": revoked})
    return revoked


# -- explicit linking (§5) ------------------------------------------------------

def link_device(device_id: str, account_id: str, *, by: str | None = None) -> dict:
    """Approve a device↔account relationship (administrator or self-service).

    Linking is what turns "this account is discoverable" into "this account may
    receive this machine's data". It never grants the device account rights.
    """
    _ensure()
    device_id = _clean(device_id)
    account = accounts.get_account(account_id)
    if not account:
        raise ValueError("account not found")
    device = db.query_one("SELECT device_id FROM devices WHERE device_id = ?",
                          (device_id,))
    if not device:
        raise ValueError("device not found")
    now = time.time()
    db.execute(
        """INSERT INTO device_links(device_id, account_id, linked_at, linked_by)
           VALUES(?,?,?,?)
           ON CONFLICT(device_id, account_id) DO UPDATE SET
             linked_at = excluded.linked_at, linked_by = excluded.linked_by,
             revoked_at = NULL""", (device_id, account_id, now, by))
    db.execute("UPDATE devices SET account_id = ? WHERE device_id = ?",
               (account_id, device_id))
    audit.record(action="device.linked", target_type="device", target_id=device_id,
                 details={"account_id": account_id, "by": by})
    return {"device_id": device_id, "account_id": account_id,
            "linked_at": now, "sync_authorized": True}


def unlink_device(device_id: str, account_id: str, *, by: str | None = None) -> None:
    _ensure()
    db.execute("UPDATE device_links SET revoked_at = ? "
               "WHERE device_id = ? AND account_id = ? AND revoked_at IS NULL",
               (time.time(), _clean(device_id), _clean(account_id)))
    db.execute("UPDATE devices SET account_id = NULL "
               "WHERE device_id = ? AND account_id = ?",
               (_clean(device_id), _clean(account_id)))
    audit.record(action="device.unlinked", target_type="device",
                 target_id=_clean(device_id),
                 details={"account_id": account_id, "by": by})


def linked_account_ids(device_id: str) -> set[str]:
    rows = db.query(
        "SELECT account_id FROM device_links WHERE device_id = ? AND revoked_at IS NULL",
        (_clean(device_id),))
    return {r["account_id"] for r in rows}


# -- discovery (§4) -------------------------------------------------------------

def _availability_for_account(account_id: str, now: float) -> tuple[str, float | None, int, int]:
    """(availability, last_heartbeat, online_devices, device_count).

    Availability comes ONLY from real heartbeats (§4: never assume a device is
    reachable because it shares a network). An account with no registered device
    is 'unknown', not 'online'.
    """
    rows = db.query("SELECT worker_id, status, last_heartbeat FROM workers "
                    "WHERE account_id = ?", (account_id,))
    if not rows:
        return "unknown", None, 0, 0
    last = None
    online = 0
    for row in rows:
        item = dict(row)
        if item.get("last_heartbeat"):
            last = max(last or 0.0, float(item["last_heartbeat"]))
        if agents.effective_status(item, now) != agents.PRESENCE_OFFLINE:
            online += 1
    return ("online" if online else "offline"), last, online, len(rows)


def discover(device_id: str) -> dict:
    """The discovery directory a registered device may read.

    Returns every active DISCOVERABLE_ROLES account with only the fields the
    spec permits, plus this device's own linkage so the UI can say honestly
    whether synchronisation is established yet.
    """
    _ensure()
    device_id = _clean(device_id)
    now = time.time()
    linked = linked_account_ids(device_id)
    # A device may also already be owned by an account (legacy register-worker).
    owner = db.query_one("SELECT account_id FROM devices WHERE device_id = ?",
                        (device_id,))
    owner_id = owner["account_id"] if owner else None

    out = []
    for account in accounts.list_accounts():
        if not account.get("active"):
            continue
        roles = accounts.account_roles(account)
        if not (set(roles) & set(DISCOVERABLE_ROLES)):
            continue
        availability, last, online, total = _availability_for_account(
            account["id"], now)
        is_linked = account["id"] in linked or account["id"] == owner_id
        out.append({
            "account_id": account["id"],
            # Display name only — never the username, scope or permissions (§8).
            "display_name": account["display_name"],
            "role": account["role"],
            "roles": roles,
            "availability": availability,
            "last_heartbeat": last,
            "online_devices": online,
            "registered_devices": total,
            "linked": is_linked,
            "sync_authorized": bool(is_linked and account.get("active")),
            "linking_required": not is_linked,
        })
    out.sort(key=lambda a: (a["role"] != "DEVELOPER", a["display_name"]))

    return {
        "device_id": device_id,
        "credential_expires_at": credential_expiry(device_id),
        "server_time": now,
        "accounts": out,
        "linked_accounts": sorted(linked),
        # Assistant teams (§1) are only revealed once this device is linked to
        # an account: an unlinked machine learns no team names. A linked device
        # sees the teams in its account's schools and whether it is already a
        # member, so an invited assistant discovers its team automatically.
        "teams": _teams_for(linked),
    }


def _teams_for(linked_account_ids: set[str]) -> list[dict]:
    if not linked_account_ids:
        return []
    owner = accounts.get_account(sorted(linked_account_ids)[0])
    if not owner:
        return []
    schools, _classes = accounts.scoped_ids(owner)
    out = []
    for team in teams.list_teams():
        if schools and team["school_id"] not in schools:
            continue
        members = teams.member_account_ids(team["id"])
        out.append({
            "team_id": team["id"], "name": team["name"],
            "school_id": team["school_id"],
            "member": owner["id"] in members,
            "member_count": len(members),
        })
    out.sort(key=lambda t: t["name"])
    return out


# -- admin views (§8: revoke + inspect) ----------------------------------------

def list_devices(*, limit: int = 500) -> list[dict]:
    """Registered devices with credential + linkage status (Teacher/Developer)."""
    _ensure()
    rows = db.query(
        """SELECT d.device_id, d.label, d.account_id, d.first_seen,
                  d.last_heartbeat, a.display_name AS account_name,
                  (SELECT MAX(c.expires_at) FROM device_credentials c
                     WHERE c.device_id = d.device_id AND c.revoked_at IS NULL) AS expires_at,
                  (SELECT COUNT(*) FROM device_credentials c
                     WHERE c.device_id = d.device_id AND c.revoked_at IS NULL) AS active_credentials,
                  (SELECT COUNT(*) FROM workers w
                     WHERE w.device_id = d.device_id) AS workers
           FROM devices d
           LEFT JOIN accounts a ON a.id = d.account_id
           ORDER BY d.last_heartbeat DESC NULLS LAST
           LIMIT ?""", (max(1, min(limit, 2000)),))
    now = time.time()
    out = []
    for row in rows:
        item = dict(row)
        item["credential_active"] = bool(item.pop("active_credentials", 0))
        if not item.get("last_heartbeat") or \
           now - float(item["last_heartbeat"]) > agents.OFFLINE_AFTER_SECONDS:
            item["presence"] = agents.PRESENCE_OFFLINE
        else:
            item["presence"] = agents.PRESENCE_ONLINE
        out.append(item)
    return out


def devices_for_account(account_id: str) -> list[dict]:
    return [d for d in list_devices() if d.get("account_id") == account_id]
