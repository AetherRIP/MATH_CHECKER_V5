"""Assistant teams (§1): shared correction records within one school team.

A team is the unit of *sharing*. It belongs to exactly one school, so two
assistants in different schools can never see each other's records even if they
install the same application — sharing is decided server-side by active team
membership, never by "the other person also has the app".

* Membership is never destroyed: removing a member sets ``revoked_at`` so the
  historical record of who shared with whom survives (§1 "without accidentally
  deleting historical records").
* The original correcting account stays attached to every correction record
  (``correction_results_sync.account_id`` / ``correction_ownership``), so a
  shared record always names who produced it.
* A newly installed assistant discovers its team through the device credential
  (see :mod:`discovery`), so no installer ever contains a membership list.
"""
from __future__ import annotations

import time
import uuid

from .. import db
from . import accounts


def _clean(text: str | None, limit: int = 120) -> str:
    return (text or "").strip()[:limit]


def _ensure() -> None:
    accounts.ensure_tables_ready()


# -- teams ----------------------------------------------------------------------

def create_team(name: str, school_id: str, *, created_by: str | None = None,
                by_role: str | None = None) -> dict:
    _ensure()
    name = _clean(name)
    if not name:
        raise ValueError("team name is required")
    if not db.query_one("SELECT id FROM schools WHERE id = ?", (school_id,)):
        raise ValueError("school not found")
    if db.query_one("SELECT id FROM teams WHERE school_id = ? AND name = ?",
                    (school_id, name)):
        raise ValueError("a team with this name already exists in this school")
    team_id = uuid.uuid4().hex
    now = time.time()
    db.execute(
        """INSERT INTO teams(id, name, school_id, active, created_by, created_at,
           updated_at) VALUES(?,?,?,?,?,?,?)""",
        (team_id, name, school_id, 1, created_by, now, now))
    from . import audit
    audit.record(action="team.created", target_type="team", target_id=team_id,
                 actor_account=created_by, actor_role=by_role, school_id=school_id,
                 team_id=team_id, details={"name": name})
    return get_team(team_id)


def get_team(team_id: str) -> dict | None:
    row = db.query_one("SELECT * FROM teams WHERE id = ?", (team_id,))
    if not row:
        return None
    team = dict(row)
    team["member_count"] = len(members(team_id))
    return team


def list_teams(*, school_id: str | None = None,
               include_inactive: bool = False) -> list[dict]:
    sql = "SELECT * FROM teams"
    clauses, params = [], []
    if school_id:
        clauses.append("school_id = ?")
        params.append(school_id)
    if not include_inactive:
        clauses.append("active = 1")
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    rows = db.query(sql + " ORDER BY name", tuple(params))
    return [get_team(r["id"]) or dict(r) for r in rows]


def set_team_active(team_id: str, active: bool, *, by: str | None = None,
                    by_role: str | None = None) -> None:
    db.execute("UPDATE teams SET active = ?, updated_at = ? WHERE id = ?",
               (1 if active else 0, time.time(), team_id))
    from . import audit
    audit.record(action="team.activated" if active else "team.deactivated",
                 target_type="team", target_id=team_id, actor_account=by,
                 actor_role=by_role, team_id=team_id)


def delete_team(team_id: str, *, by: str | None = None) -> None:
    """Deactivate rather than delete, so historical sharing stays auditable."""
    set_team_active(team_id, False, by=by)


# -- membership ------------------------------------------------------------------

def members(team_id: str) -> list[dict]:
    _ensure()
    rows = db.query(
        """SELECT m.account_id, m.role_in_team, m.added_at, m.added_by,
                  a.username, a.display_name, a.role, a.roles, a.active
           FROM team_members m JOIN accounts a ON a.id = m.account_id
           WHERE m.team_id = ? AND m.revoked_at IS NULL
           ORDER BY a.display_name""", (team_id,))
    out = []
    for row in rows:
        item = dict(row)
        item["roles"] = accounts.account_roles(item)
        out.append(item)
    return out


def member_account_ids(team_id: str) -> set[str]:
    rows = db.query(
        "SELECT account_id FROM team_members WHERE team_id = ? AND revoked_at IS NULL",
        (team_id,))
    return {r["account_id"] for r in rows}


def add_member(team_id: str, account_id: str, *, by: str | None = None,
               by_role: str | None = None,
               role_in_team: str | None = None) -> dict:
    _ensure()
    team = get_team(team_id)
    if not team:
        raise ValueError("team not found")
    target = accounts.get_account(account_id)
    if not target:
        raise ValueError("account not found")
    if not target.get("active"):
        raise ValueError("cannot add a disabled account to a team")
    now = time.time()
    db.execute(
        """INSERT INTO team_members(team_id, account_id, role_in_team, added_at,
           added_by) VALUES(?,?,?,?,?)
           ON CONFLICT(team_id, account_id) DO UPDATE SET
             role_in_team = excluded.role_in_team, added_at = excluded.added_at,
             added_by = excluded.added_by, revoked_at = NULL""",
        (team_id, account_id, _clean(role_in_team, 40) or None, now, by))
    from . import audit
    audit.record(action="team.member_added", target_type="team", target_id=team_id,
                 actor_account=by, actor_role=by_role, affected_account=account_id,
                 school_id=team["school_id"], team_id=team_id,
                 details={"member": target["username"]})
    return get_team(team_id)


def remove_member(team_id: str, account_id: str, *, by: str | None = None,
                  by_role: str | None = None) -> dict:
    """Revoke a membership without deleting any historical record (§1)."""
    _ensure()
    team = get_team(team_id)
    if not team:
        raise ValueError("team not found")
    db.execute(
        "UPDATE team_members SET revoked_at = ? "
        "WHERE team_id = ? AND account_id = ? AND revoked_at IS NULL",
        (time.time(), team_id, account_id))
    from . import audit
    audit.record(action="team.member_removed", target_type="team", target_id=team_id,
                 actor_account=by, actor_role=by_role, affected_account=account_id,
                 school_id=team["school_id"], team_id=team_id)
    return get_team(team_id)


# -- visibility ------------------------------------------------------------------

def team_ids_for_account(account_id: str) -> list[str]:
    rows = db.query(
        """SELECT t.id FROM team_members m JOIN teams t ON t.id = m.team_id
           WHERE m.account_id = ? AND m.revoked_at IS NULL AND t.active = 1""",
        (account_id,))
    return [r["id"] for r in rows]


def teams_for_account(account_id: str) -> list[dict]:
    return [get_team(tid) or {"id": tid} for tid in team_ids_for_account(account_id)]


def teammate_account_ids(account_id: str) -> set[str]:
    """Accounts whose shared records this account may read (§1).

    The union of the members of every active team the account belongs to,
    minus the account itself. Recomputed on every read, so a newly added
    teammate becomes visible immediately and a removed one loses access at
    once — no cached membership anywhere.
    """
    team_ids = team_ids_for_account(account_id)
    if not team_ids:
        return set()
    placeholders = ",".join("?" * len(team_ids))
    rows = db.query(
        f"""SELECT DISTINCT m.account_id FROM team_members m
            JOIN accounts a ON a.id = m.account_id
            WHERE m.team_id IN ({placeholders}) AND m.revoked_at IS NULL
              AND a.active = 1""", tuple(team_ids))
    return {r["account_id"] for r in rows if r["account_id"] != account_id}


def shareable_account_ids(account: dict | None) -> set[str]:
    """Accounts whose records ``account`` may read: itself + its teammates."""
    if not account:
        return set()
    return {account["id"]} | teammate_account_ids(account["id"])
