"""Local API usage accounting for the retry/failover architecture (§12).

Every counter here is derived from requests the app is ALREADY making —
collecting these statistics never performs an additional API call. Values
are persisted in ``app_meta`` (not in the log file), so the Clear Log
button can never destroy them (§15).

Counters:
    requests / successes / failures   actual generate_content round-trips
    retries                           job-level retries scheduled
    failovers                         mid-request switches to another key
    cooldowns                         key + provider cooldowns entered
    avoided                           requests/bytes NOT spent thanks to
                                      reuse (downloads, uploads, reprepare)

Accounting is BEST-EFFORT by design (same contract as net_usage): any
failure here is swallowed so stats can never break a correction. Child
worker processes write through the same SQLite (WAL), so their requests
are counted too — occasional lost increments under contention are
acceptable for statistics.
"""

from __future__ import annotations

import json
import threading
import time

from . import db

_META_KEY = "api_stats"
_lock = threading.Lock()

_DEFAULT = {
    "requests": 0,
    "successes": 0,
    "failures": 0,
    "retries": 0,
    "failovers": 0,
    "cooldowns": 0,
    "avoided_downloads": 0,
    "avoided_uploads": 0,
    "avoided_reprepare": 0,
    "by_class": {},
}


def _load() -> dict:
    try:
        raw = db.get_meta(_META_KEY)
        if not raw:
            return dict(_DEFAULT)
        data = json.loads(raw)
        merged = dict(_DEFAULT)
        merged.update({k: v for k, v in data.items() if k in _DEFAULT})
        if isinstance(data.get("updated_at"), (int, float)):
            merged["updated_at"] = data["updated_at"]
        if isinstance(merged.get("by_class"), dict):
            return merged
        merged["by_class"] = {}
        return merged
    except Exception:
        return dict(_DEFAULT)


def _persist(data: dict) -> None:
    try:
        data["updated_at"] = time.time()
        db.set_meta(_META_KEY, json.dumps(data, ensure_ascii=False))
    except Exception:
        pass


def _bump(field: str, n: int = 1) -> None:
    if n <= 0:
        return
    with _lock:
        try:
            data = _load()
            data[field] = int(data.get(field, 0)) + int(n)
            _persist(data)
        except Exception:
            pass


def record_request(*, ok: bool, error_class: str | None = None) -> None:
    """One actual generate_content round-trip finished (locally observed)."""
    with _lock:
        try:
            data = _load()
            data["requests"] = int(data.get("requests", 0)) + 1
            if ok:
                data["successes"] = int(data.get("successes", 0)) + 1
            else:
                data["failures"] = int(data.get("failures", 0)) + 1
                cls = (error_class or "UNKNOWN_ERROR").upper()
                by_class = data.setdefault("by_class", {})
                by_class[cls] = int(by_class.get(cls, 0)) + 1
            _persist(data)
        except Exception:
            pass


def record_retry(error_class: str | None = None, n: int = 1) -> None:
    """A job-level retry was scheduled (a whole correction re-run)."""
    _bump("retries", n)


def record_failover(n: int = 1) -> None:
    """A failed request was re-sent through a DIFFERENT healthy key."""
    _bump("failovers", n)


def record_cooldown(n: int = 1) -> None:
    _bump("cooldowns", n)


def record_avoided(kind: str, n: int = 1) -> None:
    """Work NOT redone because prepared input was reused.

    kind: ``downloads`` (Drive re-downloads skipped), ``uploads`` (Gemini
    file re-uploads skipped), ``reprepare`` (PDF normalize/validate passes
    skipped).
    """
    field = {"downloads": "avoided_downloads",
             "uploads": "avoided_uploads",
             "reprepare": "avoided_reprepare"}.get(kind)
    if field:
        _bump(field, n)


def snapshot() -> dict:
    try:
        data = _load()
    except Exception:
        data = dict(_DEFAULT)
    return {
        "requests": int(data.get("requests", 0)),
        "successes": int(data.get("successes", 0)),
        "failures": int(data.get("failures", 0)),
        "retries": int(data.get("retries", 0)),
        "failovers": int(data.get("failovers", 0)),
        "cooldowns": int(data.get("cooldowns", 0)),
        "avoided": {
            "downloads": int(data.get("avoided_downloads", 0)),
            "uploads": int(data.get("avoided_uploads", 0)),
            "reprepare": int(data.get("avoided_reprepare", 0)),
        },
        "by_class": dict(data.get("by_class") or {}),
        "updated_at": data.get("updated_at"),
    }


_BANDWIDTH_FIELDS = (
    "download_count", "download_bytes", "prepare_count", "upload_count",
    "upload_bytes", "reused_uploads", "ai_attempts",
    "avoided_downloads", "avoided_reprepare",
)


def record_job_bandwidth(job_id: str | None, **deltas: int) -> None:
    """Accumulate per-job bandwidth counters (§17/§18).

    Called from the API process (download/prepare) AND worker children
    (uploads/generation attempts) — SQLite WAL makes both writers safe.
    Best-effort: any failure is swallowed.
    """
    if not job_id:
        return
    clean = {k: int(v) for k, v in deltas.items()
             if k in _BANDWIDTH_FIELDS and int(v)}
    if not clean:
        return
    try:
        columns = ", ".join(clean)
        updates = ", ".join(f"{k} = {k} + excluded.{k}" for k in clean)
        values = list(clean.values())
        db.execute(
            f"INSERT INTO job_bandwidth(job_id, updated_at, {columns}) "
            f"VALUES(?, ?, {', '.join('?' for _ in clean)}) "
            f"ON CONFLICT(job_id) DO UPDATE SET {updates}, "
            f"updated_at = excluded.updated_at",
            [job_id, time.time(), *values],
        )
    except Exception:
        pass


def job_bandwidth(job_id: str) -> dict | None:
    try:
        row = db.query_one("SELECT * FROM job_bandwidth WHERE job_id = ?",
                           (job_id,))
        return dict(row) if row else None
    except Exception:
        return None


def job_bandwidth_map(job_ids: list[str]) -> dict[str, dict]:
    """Batch lookup so a job list never needs one query per row."""
    ids = [j for j in job_ids if j]
    if not ids:
        return {}
    try:
        placeholders = ",".join("?" for _ in ids)
        rows = db.query(
            f"SELECT * FROM job_bandwidth WHERE job_id IN ({placeholders})", ids)
        return {r["job_id"]: dict(r) for r in rows}
    except Exception:
        return {}


def recent_job_bandwidth(limit: int = 50) -> list[dict]:
    try:
        rows = db.query(
            "SELECT * FROM job_bandwidth ORDER BY updated_at DESC LIMIT ?",
            (max(1, int(limit)),))
        return [dict(r) for r in rows]
    except Exception:
        return []


def reset() -> None:
    """Testing helper."""
    with _lock:
        try:
            _persist(dict(_DEFAULT))
        except Exception:
            pass
