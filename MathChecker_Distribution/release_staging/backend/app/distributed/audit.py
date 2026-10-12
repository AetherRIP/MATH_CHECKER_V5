"""Audit trail (§52) + error reporting (§24-§26).

Audit records important actions only — never heartbeat traffic. Error reports
are SANITIZED before storage (§25): secrets are redacted recursively, stack
traces are truncated, no binary/homework payloads are accepted.
"""
from __future__ import annotations

import json
import time
import traceback
import uuid

from .. import db
from . import accounts

MAX_STACK_CHARS = 6000
MAX_CONTEXT_CHARS = 8000
MAX_SESSION_EVENTS = 50

_REDACTED = "[redacted]"
_SENSITIVE = ("password", "passwd", "secret", "token", "api_key", "apikey",
              "authorization", "cookie", "credential", "private_key")

# Free-text scrubber (§25): error MESSAGES written by humans/AI can leak
# secrets, so key=value patterns are redacted there too.
import re as _re

_TEXT_SECRET_RE = _re.compile(
    r"(?i)\b(password|passwd|api[_-]?key|apikey|token|secret|authorization|"
    r"cookie|credential)\s*[=:]\s*\S+")


def scrub_text(text: str) -> str:
    if not text:
        return text or ""
    return _TEXT_SECRET_RE.sub(lambda m: m.group(1) + "=[redacted]", text)


def sanitize(value, _depth: int = 0):
    """Recursively redact sensitive keys / huge values (§25)."""
    if _depth > 6:
        return "[truncated]"
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            if any(s in str(k).lower() for s in _SENSITIVE):
                out[k] = _REDACTED
            else:
                out[k] = sanitize(v, _depth + 1)
        return out
    if isinstance(value, (list, tuple)):
        return [sanitize(v, _depth + 1) for v in value[:MAX_SESSION_EVENTS]]
    if isinstance(value, str) and len(value) > 2000:
        return value[:2000] + "…[truncated]"
    if isinstance(value, (bytes, bytearray)):
        return f"[{len(value)} bytes omitted]"
    if isinstance(value, str):
        # Free text inside structured context can still contain key=value
        # secret strings (e.g. a pasted log line): scrub those too (§25).
        return scrub_text(value)
    return value


def record(*, action: str, target_type: str | None = None,
           target_id: str | None = None, actor_account: str | None = None,
           actor_role: str | None = None, device_id: str | None = None,
           affected_account: str | None = None, school_id: str | None = None,
           team_id: str | None = None, outcome: str = "success",
           details: dict | None = None) -> str:
    """Append one structured audit event and return its stable event id.

    Written SERVER-SIDE only: a client can never write, edit or erase its own
    audit history (§4). ``details`` is sanitized, so passwords, hashes and
    tokens can never reach the trail even if a caller passes them by mistake.
    """
    accounts.ensure_tables_ready()
    event_id = uuid.uuid4().hex
    db.execute(
        """INSERT INTO audit_log(event_id, ts, actor_account, actor_role,
           device_id, action, target_type, target_id, affected_account,
           school_id, team_id, outcome, details)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (event_id, time.time(), actor_account, actor_role, device_id, action,
         target_type, target_id, affected_account, school_id, team_id,
         outcome if outcome in ("success", "failure", "denied") else "success",
         json.dumps(sanitize(details or {}), ensure_ascii=False)))
    return event_id


def list_audit(limit: int = 200, action: str | None = None,
               actor_account: str | None = None,
               affected_account: str | None = None,
               school_id: str | None = None, team_id: str | None = None,
               outcome: str | None = None, since: float | None = None,
               q: str | None = None) -> list[dict]:
    """Audit events, newest first, with the filters the Developer log needs.

    ``q`` is a case-insensitive substring search over the action and the
    (already sanitized) details, so no secret can be searched out of the trail.
    """
    accounts.ensure_tables_ready()
    limit = max(1, min(int(limit), 1000))
    clauses, params = [], []
    if action:
        clauses.append("action = ?")
        params.append(action)
    if actor_account:
        clauses.append("actor_account = ?")
        params.append(actor_account)
    if affected_account:
        clauses.append("affected_account = ?")
        params.append(affected_account)
    if school_id:
        clauses.append("school_id = ?")
        params.append(school_id)
    if team_id:
        clauses.append("team_id = ?")
        params.append(team_id)
    if outcome:
        clauses.append("outcome = ?")
        params.append(outcome)
    if since is not None:
        clauses.append("ts >= ?")
        params.append(float(since))
    if q:
        clauses.append("(action LIKE ? OR details LIKE ? OR target_id LIKE ?)")
        like = f"%{q}%"
        params.extend([like, like, like])
    sql = "SELECT * FROM audit_log"
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    rows = db.query(sql + " ORDER BY id DESC LIMIT ?", (*params, limit))
    out = []
    for row in rows:
        item = dict(row)
        item["details"] = json.loads(item.get("details") or "{}")
        # Pre-column rows have no event_id; fall back to the row id so every
        # event still has a stable identifier.
        item["event_id"] = item.get("event_id") or f"legacy-{item['id']}"
        item["outcome"] = item.get("outcome") or "success"
        out.append(item)
    return out


# -- error reporting (§24) --------------------------------------------------------

def report_error(*, error_type: str, message: str, failed_stage: str | None,
                 account: dict | None = None, device_id: str | None = None,
                 worker_id: str | None = None, context: dict | None = None,
                 session_events: list | None = None,
                 stack_trace: str | None = None) -> dict:
    accounts.ensure_tables_ready()
    clean_context = sanitize(context or {})
    clean_session = sanitize(session_events or [])
    clean_stack = (stack_trace or "")[-MAX_STACK_CHARS:]
    if not clean_stack and stack_trace is None:
        clean_stack = traceback.format_exc()[-MAX_STACK_CHARS:]
    error_id = uuid.uuid4().hex
    db.execute(
        """INSERT INTO error_reports(id, ts, account_id, device_id, worker_id,
           error_type, failed_stage, message, stack_trace, context,
           session_events) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
        (error_id, time.time(),
         account.get("id") if account else None, device_id, worker_id,
         (error_type or "unknown")[:120], (failed_stage or "")[:120],
         scrub_text(message)[:2000], scrub_text(clean_stack),
         json.dumps(clean_context, ensure_ascii=False)[:MAX_CONTEXT_CHARS],
         json.dumps(clean_session, ensure_ascii=False)[:MAX_CONTEXT_CHARS]))
    return {"id": error_id}


def list_errors(*, limit: int = 100, worker_id: str | None = None,
                account_id: str | None = None,
                unresolved_only: bool = False) -> list[dict]:
    accounts.ensure_tables_ready()
    limit = max(1, min(int(limit), 500))
    sql = "SELECT * FROM error_reports"
    clauses, params = [], []
    if worker_id:
        clauses.append("worker_id = ?")
        params.append(worker_id)
    if account_id:
        # Scope for limited roles (CO_TEACHER/ASSISTANT): their own reports
        # only. Combined with worker_id it narrows further, never widens.
        clauses.append("account_id = ?")
        params.append(account_id)
    if unresolved_only:
        clauses.append("resolved = 0")
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    rows = db.query(sql + " ORDER BY ts DESC LIMIT ?", (*params, limit))
    out = []
    for row in rows:
        item = dict(row)
        item["context"] = json.loads(item.get("context") or "{}")
        item["session_events"] = json.loads(item.get("session_events") or "[]")
        out.append(item)
    return out


def mark_resolved(error_id: str) -> None:
    db.execute("UPDATE error_reports SET resolved = 1 WHERE id = ?", (error_id,))
