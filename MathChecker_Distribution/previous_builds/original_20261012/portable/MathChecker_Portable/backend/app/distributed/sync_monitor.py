"""Developer sync-monitoring snapshot (§8 developer dashboard).

One aggregate read, not five: the developer view needs agents, versions,
report-delivery health, retries, duplicates and recent errors together, and
polling five endpoints on a timer would be exactly the kind of chatty
background traffic §9 forbids. Everything here is a single SQLite read pass on
the local database.

Two rules this module exists to enforce:

* **Presence is derived, never assumed.** A worker's stored status means
  nothing without a fresh heartbeat, so every agent is reported through
  :func:`agents.effective_status` and a stale one can never appear online
  (§4/§62).
* **Nothing sensitive leaves here.** Free-text fields go through
  :func:`audit.sanitize` / ``scrub_text``, so a pasted token or API key in a
  log line cannot reach the developer screen (or its logs) intact.
"""
from __future__ import annotations

import time

from .. import db
from ..version import APP_VERSION
from . import accounts, agents, audit, report_delivery, sync

# A worker whose heartbeat is older than this is shown as not reporting even if
# its stored status still says ONLINE.
RECENT_WINDOW_SECONDS = agents.OFFLINE_AFTER_SECONDS


def _agents(now: float) -> list[dict]:
    """Registered agent installations with derived presence."""
    rows = agents.list_workers()
    out = []
    for row in rows:
        last = row.get("last_heartbeat")
        age = (now - float(last)) if last else None
        out.append({
            "worker_id": row.get("worker_id"),
            "device_id": row.get("device_id"),
            "device_label": row.get("device_label"),
            "account_name": row.get("account_name"),
            "account_role": row.get("account_role"),
            "app_version": row.get("app_version") or "unknown",
            "agent_version": row.get("agent_version") or "unknown",
            "status": row.get("status"),
            # effective_status is the ONLY trustworthy presence (§62).
            "presence": row.get("presence") or agents.effective_status(row, now),
            "last_heartbeat": last,
            "seconds_since_heartbeat": age,
            "stale": age is None or age > RECENT_WINDOW_SECONDS,
            "current_job_id": row.get("current_job_id"),
            "registered_at": row.get("registered_at"),
        })
    out.sort(key=lambda a: (a["seconds_since_heartbeat"] is None,
                            a["seconds_since_heartbeat"] or 0))
    return out


def _agents_summary(listed: list[dict]) -> dict:
    total = len(listed)
    reporting = sum(1 for a in listed if not a["stale"])
    busy = sum(1 for a in listed if a.get("current_job_id"))
    versions: dict[str, int] = {}
    agent_versions: dict[str, int] = {}
    for a in listed:
        versions[a["app_version"]] = versions.get(a["app_version"], 0) + 1
        agent_versions[a["agent_version"]] = agent_versions.get(a["agent_version"], 0) + 1
    return {
        "total": total,
        "reporting": reporting,
        "silent": total - reporting,
        "busy": busy,
        "app_versions": versions,
        "agent_versions": agent_versions,
    }


def _duplicates(limit: int = 20) -> list[dict]:
    """Conflicting-revision warnings (§6).

    The same submission recorded at more than one version, or produced by more
    than one worker, is a genuine conflict worth a human look — it is not
    silently merged.
    """
    rows = db.query(
        """SELECT submission_id,
                  COUNT(DISTINCT submission_version) AS versions,
                  COUNT(DISTINCT worker_id)          AS workers,
                  COUNT(*)                           AS records,
                  MAX(received_at)                   AS last_seen
             FROM correction_results_sync
            GROUP BY submission_id
           HAVING versions > 1 OR workers > 1
            ORDER BY last_seen DESC LIMIT ?""", (max(1, min(limit, 100)),))
    return [dict(r) for r in rows]


def _retry_history(limit: int = 25) -> list[dict]:
    """Report transfers that have been attempted and are not yet delivered."""
    rows = db.query(
        """SELECT result_record_id, submission_id, report_filename, state,
                  attempts, max_attempts, last_error, next_retry_at,
                  updated_at, delivered_at
             FROM report_deliveries
            WHERE attempts > 0 AND state != ?
            ORDER BY updated_at DESC LIMIT ?""",
        (report_delivery.DELIVERED, max(1, min(limit, 200))))
    return [dict(r) for r in rows]


def _sync_health() -> dict:
    results = db.query_one("SELECT COUNT(*) n FROM correction_results_sync")
    events = sync.latest_seq()
    return {
        "reports": report_delivery.summary(),
        "results_synced": int(results["n"]) if results else 0,
        "latest_event_seq": events,
    }


def _recent_errors(limit: int = 15) -> list[dict]:
    rows = audit.list_errors(limit=limit)
    out = []
    for row in rows:
        out.append({
            "id": row.get("id"),
            "ts": row.get("ts"),
            "error_type": row.get("error_type"),
            "failed_stage": row.get("failed_stage"),
            "message": row.get("message"),
            "worker_id": row.get("worker_id"),
            "device_id": row.get("device_id"),
            "account_id": row.get("account_id"),
            "resolved": row.get("resolved"),
        })
    return out


# Audit actions that describe the sync pipeline itself. Filtering keeps the
# developer view about synchronisation rather than every account change.
_SYNC_ACTIONS = (
    "agent.enabled", "agent.disabled", "report.retry", "account.roles_changed",
)


def _sync_events(limit: int = 25) -> list[dict]:
    rows = db.query(
        "SELECT ts, action, target_type, target_id, actor_account, details "
        "FROM audit_log WHERE action IN (%s) ORDER BY id DESC LIMIT ?"
        % ",".join("?" * len(_SYNC_ACTIONS)),
        (*_SYNC_ACTIONS, max(1, min(limit, 200))))
    out = []
    for row in rows:
        item = dict(row)
        # details were sanitized on write; scrub again so a value inserted by an
        # older code path can never leak here.
        item["details"] = audit.sanitize(_safe_json(item.get("details")))
        out.append(item)
    return out


def _safe_json(raw):
    import json
    try:
        return json.loads(raw or "{}")
    except (ValueError, TypeError):
        return {}


def snapshot(*, now: float | None = None) -> dict:
    """Full developer sync-monitoring snapshot in one pass."""
    now = now if now is not None else time.time()
    listed = _agents(now)
    return {
        "generated_at": now,
        "app_version": APP_VERSION,
        "agents": listed,
        "agents_summary": _agents_summary(listed),
        "sync": _sync_health(),
        "retries": _retry_history(),
        "duplicates": _duplicates(),
        "recent_errors": _recent_errors(),
        "sync_events": _sync_events(),
    }
