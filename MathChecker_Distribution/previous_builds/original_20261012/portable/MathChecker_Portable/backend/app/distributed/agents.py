"""Device + worker identity, heartbeat and presence (§14-§23).

* A device is identified by a generated persistent UUID created on the machine
  itself — never by IP/hostname/username (§21).
* A worker is one correction installation on a device; it registers once and
  then heartbeats lightweight metadata (~a few hundred bytes, §16).
* Presence is derived from the LAST heartbeat, not from live polling (§17/§47):
  the backend never pushes; the Teacher page reads the stored state.
"""
from __future__ import annotations

import time

from .. import db
from . import accounts, sync

PRESENCE_ONLINE = "ONLINE"
PRESENCE_CHECKING = "ONLINE_CHECKING"
PRESENCE_AVAILABLE = "AVAILABLE"
PRESENCE_BUSY = "BUSY"
PRESENCE_PAUSED = "PAUSED"
PRESENCE_OFFLINE = "OFFLINE"
PRESENCE_ERROR = "ERROR"
PRESENCE_UPDATING = "UPDATING"
PRESENCE_RECOVERY = "RECOVERY"

# After this long without a heartbeat a worker is OFFLINE (§23).
OFFLINE_AFTER_SECONDS = 11 * 60   # ~2 missed 5-minute heartbeats

HEARTBEAT_INTERVAL_SECONDS = 5 * 60


#: Canonical platform names a release may target (updates.PLATFORMS).
_PLATFORMS = ("windows", "linux", "darwin")


def normalize_platform(value: str | None) -> str | None:
    """Map a reported OS string to a canonical name, or None if unknown.

    Accepts both ``sys.platform`` values (win32/linux/darwin) and friendly
    names (Windows/Linux/macOS), so the agent and a human client both work.
    """
    text = (value or "").strip().lower()
    if not text:
        return None
    if text.startswith("win"):
        return "windows"
    if text.startswith("linux"):
        return "linux"
    if text.startswith("darwin") or text.startswith("mac") or text == "osx":
        return "darwin"
    return text if text in _PLATFORMS else None


def register_device(device_id: str, *, label: str | None = None,
                    account_id: str | None = None,
                    platform: str | None = None,
                    arch: str | None = None) -> dict:
    accounts.ensure_tables_ready()
    device_id = (device_id or "").strip()[:64]
    if not device_id:
        raise ValueError("device_id is required")
    # COALESCE keeps a previously recorded platform when a heartbeat omits it,
    # so one partial report can never erase a known-good value.
    db.execute(
        """INSERT INTO devices(device_id, label, account_id, platform, arch,
           first_seen) VALUES(?,?,?,?,?,?)
           ON CONFLICT(device_id) DO UPDATE SET label = COALESCE(excluded.label,
             label), account_id = COALESCE(excluded.account_id, account_id),
             platform = COALESCE(excluded.platform, platform),
             arch = COALESCE(excluded.arch, arch)""",
        (device_id, (label or "").strip()[:120] or None, account_id,
         normalize_platform(platform), (arch or "").strip()[:32] or None,
         time.time()))
    row = db.query_one("SELECT * FROM devices WHERE device_id = ?", (device_id,))
    return dict(row) if row else {}


def register_worker(worker_id: str, device_id: str, *,
                    account_id: str | None = None, app_version: str | None = None,
                    agent_version: str | None = None) -> dict:
    now = time.time()
    db.execute(
        """INSERT INTO workers(worker_id, device_id, account_id, app_version,
           agent_version, status, registered_at, last_heartbeat)
           VALUES(?,?,?,?,?,?,?,?)
           ON CONFLICT(worker_id) DO UPDATE SET device_id = excluded.device_id,
             account_id = COALESCE(excluded.account_id, account_id),
             app_version = excluded.app_version,
             agent_version = excluded.agent_version""",
        (worker_id, device_id, account_id, app_version, agent_version,
         PRESENCE_ONLINE, now, now))
    row = db.query_one("SELECT * FROM workers WHERE worker_id = ?", (worker_id,))
    return dict(row) if row else {}


def heartbeat(*, device_id: str, worker_id: str | None = None,
              account_id: str | None = None, app_version: str | None = None,
              agent_version: str | None = None, status: str = PRESENCE_ONLINE,
              current_job_id: str | None = None,
              label: str | None = None, platform: str | None = None,
              arch: str | None = None) -> dict:
    """One lightweight heartbeat (§16): a few hundred bytes of metadata."""
    device = register_device(device_id, label=label, account_id=account_id,
                             platform=platform, arch=arch)
    now = time.time()
    if worker_id:
        register_worker(worker_id, device_id, account_id=account_id,
                        app_version=app_version, agent_version=agent_version)
        previous = db.query_one("SELECT status, current_job_id FROM workers "
                                "WHERE worker_id = ?", (worker_id,))
        db.execute(
            """UPDATE workers SET status = ?, current_job_id = ?,
               last_heartbeat = ?, app_version = COALESCE(?, app_version),
               agent_version = COALESCE(?, agent_version) WHERE worker_id = ?""",
            (status, current_job_id, now, app_version, agent_version, worker_id))
        # Presence changes become events (§17/§52); identical heartbeats do NOT
        # flood the stream — audit stays small (§52).
        if previous and previous["status"] != status:
            sync.append_event(
                type_=sync.EVENT_WORKER_STATUS,
                payload={"worker_id": worker_id, "from": previous["status"],
                         "to": status, "job_id": current_job_id},
                account_id=account_id, device_id=device_id, worker_id=worker_id,
                client_event_id=f"presence-{worker_id}-{status}-{int(now)}")
    db.execute("UPDATE devices SET last_heartbeat = ? WHERE device_id = ?",
               (now, device_id))
    return {"ok": True, "server_time": now, "device": device["device_id"]}


def effective_status(row: dict, now: float | None = None) -> str:
    """The REAL presence: a stored status means nothing without a fresh
    heartbeat (§62 — never display ONLINE unless verified)."""
    now = now if now is not None else time.time()
    last = row.get("last_heartbeat")
    if not last or (now - float(last)) > OFFLINE_AFTER_SECONDS:
        return PRESENCE_OFFLINE
    return row.get("status") or PRESENCE_OFFLINE


def list_workers(account_id: str | None = None) -> list[dict]:
    """Registered workers ordered by id.

    ``account_id`` narrows the list to ONE account's own workers — used by the
    ``workers.view_own`` path so a Co-Teacher/Assistant can never enumerate the
    whole fleet (§48: view_own means view_own).
    """
    accounts.ensure_tables_ready()
    sql = """SELECT w.*, d.label AS device_label, a.display_name AS account_name,
                  a.role AS account_role
           FROM workers w
           LEFT JOIN devices d ON d.device_id = w.device_id
           LEFT JOIN accounts a ON a.id = w.account_id"""
    params: tuple = ()
    if account_id:
        sql += " WHERE w.account_id = ?"
        params = (account_id,)
    sql += " ORDER BY w.worker_id"
    rows = db.query(sql, params)
    now = time.time()
    out = []
    for row in rows:
        item = dict(row)
        item["presence"] = effective_status(item, now)
        out.append(item)
    return out


def get_worker(worker_id: str) -> dict | None:
    row = db.query_one(
        """SELECT w.*, d.label AS device_label FROM workers w
           LEFT JOIN devices d ON d.device_id = w.device_id
           WHERE w.worker_id = ?""", (worker_id,))
    if not row:
        return None
    item = dict(row)
    item["presence"] = effective_status(item)
    return item


def set_worker_status(worker_id: str, status: str) -> None:
    db.execute("UPDATE workers SET status = ? WHERE worker_id = ?",
               (status, worker_id))


def recent_failures(worker_id: str, limit: int = 10) -> list[dict]:
    rows = db.query(
        "SELECT * FROM error_reports WHERE worker_id = ? ORDER BY ts DESC LIMIT ?",
        (worker_id, max(1, min(limit, 50))))
    return [dict(r) for r in rows]
