"""Per-request provider accounting: what each Gemini call actually cost.

Before this module the application could say *how many* requests a paper cost
(``gemini.call_audit``) but not what they contained: no tokens, no duration, no
cache hits, no per-request outcome. That made every efficiency claim
unverifiable — a smaller number of requests means nothing if each one carries
three times the pages.

Design constraints, in order of importance:

1. **Never break a correction.** Every write is best-effort and swallowed. A
   statistics failure is not a marking failure.
2. **Never store student content.** A row holds identifiers, counts, a model
   name, a duration and an outcome label. No prompt text, no answer, no name.
3. **Never invent a measurement.** Fields the provider did not report stay
   NULL and the row is marked ``estimated = 0``. The dashboard shows
   "unavailable" rather than a confident zero.
4. **Bounded.** Rows are pruned by age and by count, so the ledger cannot grow
   without limit on a teacher's machine.

A correction runs in a worker child process, so rows are written from two
processes (backend and child) into the same SQLite database. WAL makes that
safe; a lost increment under contention is acceptable for statistics and is
documented as such, exactly like ``api_stats``.

Context (prompt/scheme version, purpose, job) travels in context variables, so
the call sites that already exist do not need a new parameter threaded through
every layer.
"""

from __future__ import annotations

import contextvars
import hashlib
import json
import logging
import threading
import time
import uuid

from . import usage as usage_mod

log = logging.getLogger("gemini.telemetry")

#: Provider requests, split so latency/token aggregates can exclude uploads.
KIND_GENERATE = "generate"
KIND_UPLOAD = "upload"

OUTCOME_ACCEPTED = "accepted"
OUTCOME_REPAIRED = "repaired"
OUTCOME_ESCALATED = "escalated"
OUTCOME_REJECTED = "rejected"

DEFAULT_RETENTION_DAYS = 30
DEFAULT_RETENTION_ROWS = 20_000
_PRUNE_EVERY = 50          # inserts between opportunistic prunes
_COLUMNS = (
    "kind", "request_id", "job_id", "model", "endpoint", "stage", "purpose",
    "prompt_version", "scheme_version", "input_tokens", "output_tokens",
    "cached_tokens", "thoughts_tokens", "total_tokens", "duration_ms",
    "attempt", "error_class", "http_status", "upload_count", "reused",
    "outcome", "estimated", "created_at",
)

_context: contextvars.ContextVar[dict] = contextvars.ContextVar(
    "gemini_telemetry_context", default={})
_last_row: contextvars.ContextVar[int | None] = contextvars.ContextVar(
    "gemini_telemetry_last_row", default=None)
_inserts = 0
_lock = threading.Lock()


def _enabled() -> bool:
    try:
        from ..config import get_config
        return bool(getattr(get_config().gemini, "usage_telemetry", True))
    except Exception:  # noqa: BLE001
        return True


def text_version(text: str | None) -> str | None:
    """Stable, short fingerprint of prompt/scheme text (never the text itself).

    Automatic versioning: a prompt edit changes this value without anyone
    remembering to bump a constant, and the benchmark records it next to the
    run so "same prompt, different marks" is detectable.
    """
    if not text:
        return None
    try:
        return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
    except Exception:  # noqa: BLE001
        return None


class call_context:
    """Tag every request made inside the block with pipeline context.

    Usage::

        with telemetry.call_context(purpose="marking", prompt_version=v,
                                    scheme_version=s, job_id=jid):
            client.models.generate_content(...)

    Nested/unknown values fall back to the enclosing context, so a wrapper that
    only knows the purpose does not erase the job ID.
    """

    def __init__(self, **fields):
        self._fields = {k: v for k, v in fields.items() if v is not None}
        self._token = None

    def __enter__(self):
        merged = {**_context.get(), **self._fields}
        self._token = _context.set(merged)
        return self

    def __exit__(self, *exc_info):
        if self._token is not None:
            _context.reset(self._token)
        return False


def current_context() -> dict:
    return dict(_context.get())


def _value(name: str, explicit):
    """Explicit argument wins, then the context, then None."""
    if explicit is not None:
        return explicit
    return _context.get().get(name)


def _stage(explicit: str | None) -> str | None:
    if explicit:
        return explicit
    try:
        from . import call_audit
        return call_audit.current_stage()
    except Exception:  # noqa: BLE001
        return None


def record(*, kind: str = KIND_GENERATE, model: str | None = None,
           endpoint: str = "generateContent", ok: bool = True,
           error_class: str | None = None, http_status: int | None = None,
           duration_ms: int | None = None,
           usage: usage_mod.Usage | dict | None = None,
           outcome: str | None = None, reused: bool = False,
           upload_count: int | None = None, job_id: str | None = None,
           attempt: int | None = None) -> int | None:
    """Append one row. Returns its id, or ``None`` when it could not be written.

    A failed request is a row too: without it, failure rate and retry overhead
    are invisible and a storm looks like an idle system.
    """
    if not _enabled():
        return None
    try:
        parsed = usage if isinstance(usage, usage_mod.Usage) else None
        if isinstance(usage, dict):
            parsed = usage_mod.Usage(
                input_tokens=usage.get("input_tokens"),
                output_tokens=usage.get("output_tokens"),
                cached_tokens=usage.get("cached_tokens"),
                thoughts_tokens=usage.get("thoughts_tokens"),
                total_tokens=usage.get("total_tokens"),
                estimated=bool(usage.get("estimated")),
            )
        values = (
            kind,
            uuid.uuid4().hex,
            _value("job_id", job_id),
            model,
            endpoint,
            _stage(None),
            _value("purpose", None),
            _value("prompt_version", None),
            _value("scheme_version", None),
            parsed.input_tokens if parsed else None,
            parsed.output_tokens if parsed else None,
            parsed.cached_tokens if parsed else None,
            parsed.thoughts_tokens if parsed else None,
            parsed.total_tokens if parsed else None,
            int(duration_ms) if duration_ms is not None else None,
            attempt,
            error_class,
            int(http_status) if http_status is not None else None,
            int(upload_count) if upload_count is not None else None,
            1 if reused else 0,
            outcome or (OUTCOME_ACCEPTED if ok else None),
            1 if (parsed and parsed.estimated) else 0,
            time.time(),
        )
        from .. import db
        cursor = db.execute(
            f"INSERT INTO ai_request_log({', '.join(_COLUMNS)}) "
            f"VALUES({', '.join('?' for _ in _COLUMNS)})", values)
        row_id = getattr(cursor, "lastrowid", None)
        _last_row.set(row_id if isinstance(row_id, int) else None)
        _maybe_prune()
        return _last_row.get()
    except Exception:  # noqa: BLE001 — statistics are never fatal
        return None


def set_outcome(outcome: str) -> None:
    """Correct the outcome of the row just written (accepted → repaired…).

    The proxy records the row when the provider answers; only the engine knows
    whether the body later validated, needed a repair, or was rejected. This
    updates the same row instead of writing a second, contradictory one.
    """
    row_id = _last_row.get()
    if row_id is None or not outcome:
        return
    try:
        from .. import db
        db.execute("UPDATE ai_request_log SET outcome = ? WHERE id = ?",
                   (str(outcome), int(row_id)))
    except Exception:  # noqa: BLE001
        pass


def _retention() -> tuple[int, int]:
    days, rows = DEFAULT_RETENTION_DAYS, DEFAULT_RETENTION_ROWS
    try:
        from ..config import get_config
        cfg = get_config().gemini
        days = max(1, int(getattr(cfg, "usage_retention_days", days)))
        rows = max(100, int(getattr(cfg, "usage_retention_rows", rows)))
    except Exception:  # noqa: BLE001
        pass
    return days, rows


def _maybe_prune() -> None:
    global _inserts
    with _lock:
        _inserts += 1
        due = _inserts % _PRUNE_EVERY == 0
    if due:
        prune()


def prune() -> dict:
    """Drop rows past the retention window, then the oldest over the row cap."""
    days, rows = _retention()
    removed = 0
    try:
        from .. import db
        cutoff = time.time() - days * 86_400
        cursor = db.execute("DELETE FROM ai_request_log WHERE created_at < ?",
                            (cutoff,))
        removed += int(getattr(cursor, "rowcount", 0) or 0)
        excess = db.query_one(
            "SELECT COUNT(*) AS n FROM ai_request_log")
        overflow = int((excess["n"] if excess else 0) or 0) - rows
        if overflow > 0:
            cursor = db.execute(
                "DELETE FROM ai_request_log WHERE id IN ("
                "SELECT id FROM ai_request_log ORDER BY id ASC LIMIT ?)",
                (overflow,))
            removed += int(getattr(cursor, "rowcount", 0) or 0)
    except Exception:  # noqa: BLE001
        return {"removed": removed, "retention_days": days,
                "retention_rows": rows}
    return {"removed": removed, "retention_days": days,
            "retention_rows": rows}


def _percentile(values: list[int], pct: float) -> int | None:
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1,
                       int(round((pct / 100.0) * (len(ordered) - 1)))))
    return int(ordered[index])


def _empty_bucket() -> dict:
    return {"requests": 0, "ok": 0, "failed": 0, "input_tokens": 0,
            "output_tokens": 0, "cached_tokens": 0, "estimated_rows": 0,
            "durations": []}


def _finish(bucket: dict) -> dict:
    durations = bucket.pop("durations", [])
    return {**bucket,
            "p50_ms": _percentile(durations, 50),
            "p95_ms": _percentile(durations, 95),
            "max_ms": max(durations) if durations else None}


def summary(hours: int = 168, limit: int = 50_000) -> dict:
    """Aggregate the ledger. Generative requests and uploads stay separate.

    ``unavailable`` names the measurements this window could not produce, so a
    consumer never has to guess whether a zero means "none" or "not measured".
    """
    window = max(1, int(hours))
    cutoff = time.time() - window * 3600
    result: dict = {
        "window_hours": window,
        "available": False,
        "unavailable": [],
        "requests": 0, "ok": 0, "failed": 0,
        "input_tokens": 0, "output_tokens": 0, "cached_tokens": 0,
        "thoughts_tokens": 0, "total_tokens": 0, "estimated_rows": 0,
        "cache_hit_rate": None,
        "latency": {"p50_ms": None, "p95_ms": None, "max_ms": None},
        "by_model": {}, "by_stage": {}, "by_outcome": {},
        "per_correction": {"papers": 0, "requests": None,
                           "input_tokens": None, "output_tokens": None},
        "uploads": {"requests": 0, "reused": 0},
        "retention_days": None, "oldest_row": None,
    }
    try:
        from .. import db
        rows = db.query(
            "SELECT * FROM ai_request_log WHERE created_at >= ? "
            "ORDER BY id DESC LIMIT ?", (cutoff, max(1, limit)))
    except Exception:  # noqa: BLE001
        result["unavailable"].append("ledger unreadable")
        return result
    if not rows:
        result["unavailable"].append(
            "no requests recorded in this window")
        return result

    durations: list[int] = []
    by_job: dict[str, dict] = {}
    for row in rows:
        kind = row["kind"] or KIND_GENERATE
        bucket_key = "uploads" if kind == KIND_UPLOAD else "generation"
        if bucket_key == "uploads":
            result["uploads"]["requests"] += 1
            if int(row["reused"] or 0):
                result["uploads"]["reused"] += 1
            continue
        result["requests"] += 1
        failed = bool(row["error_class"])
        if failed:
            result["failed"] += 1
        else:
            result["ok"] += 1
        for field in ("input_tokens", "output_tokens", "cached_tokens",
                      "total_tokens", "thoughts_tokens"):
            result[field] += int(row[field] or 0)
        if int(row["estimated"] or 0):
            result["estimated_rows"] += 1
        if row["duration_ms"] is not None:
            durations.append(int(row["duration_ms"]))
        model = row["model"] or "unknown"
        stage = row["stage"] or "unattributed"
        for target, key in ((result["by_model"], model),
                            (result["by_stage"], stage)):
            entry = target.setdefault(key, _empty_bucket())
            entry["requests"] += 1
            entry["ok" if not failed else "failed"] += 1
            entry["input_tokens"] += int(row["input_tokens"] or 0)
            entry["output_tokens"] += int(row["output_tokens"] or 0)
            entry["cached_tokens"] += int(row["cached_tokens"] or 0)
            if int(row["estimated"] or 0):
                entry["estimated_rows"] += 1
            if row["duration_ms"] is not None:
                entry["durations"].append(int(row["duration_ms"]))
        outcome = row["outcome"] or ("failed" if failed else "unknown")
        result["by_outcome"][outcome] = result["by_outcome"].get(outcome, 0) + 1
        if row["job_id"] and not failed:
            job = by_job.setdefault(row["job_id"],
                                    {"requests": 0, "input": 0, "output": 0})
            job["requests"] += 1
            job["input"] += int(row["input_tokens"] or 0)
            job["output"] += int(row["output_tokens"] or 0)

    result["by_model"] = {k: _finish(v) for k, v in result["by_model"].items()}
    result["by_stage"] = {k: _finish(v) for k, v in result["by_stage"].items()}
    result["latency"] = {
        "p50_ms": _percentile(durations, 50),
        "p95_ms": _percentile(durations, 95),
        "max_ms": max(durations) if durations else None,
    }
    billed_input = result["input_tokens"] + result["cached_tokens"]
    if billed_input > 0:
        result["cache_hit_rate"] = round(
            result["cached_tokens"] / billed_input, 4)
    else:
        result["unavailable"].append("cache hit rate (no input tokens reported)")
    if result["input_tokens"] == 0:
        result["unavailable"].append(
            "token counts (provider reported none in this window)")
    if not durations:
        result["unavailable"].extend([
            "latency (no durations recorded)",
        ])
    if by_job:
        papers = len(by_job)
        result["per_correction"] = {
            "papers": papers,
            "requests": round(sum(j["requests"] for j in by_job.values())
                              / papers, 2),
            "input_tokens": round(sum(j["input"] for j in by_job.values())
                                  / papers, 1),
            "output_tokens": round(sum(j["output"] for j in by_job.values())
                                   / papers, 1),
        }
    else:
        result["unavailable"].append(
            "per-correction averages (no successful request carried a job id)")
    try:
        oldest = db.query_one(
            "SELECT MIN(created_at) AS first FROM ai_request_log")
        result["oldest_row"] = oldest["first"] if oldest else None
    except Exception:  # noqa: BLE001
        pass
    result["retention_days"] = _retention()[0]
    result["available"] = result["requests"] > 0
    return result


def job_totals(job_id: str | None) -> dict:
    """Summed token counts for one job, taken from the ledger.

    The ledger is the authority: these are ADDED from the rows the provider
    actually reported, so a retried job cannot double-count and a job whose
    provider reported nothing stays 0 ("not recorded") rather than being
    filled with an estimate.
    """
    empty = {"input_tokens": 0, "output_tokens": 0, "cached_tokens": 0,
             "total_tokens": 0}
    if not job_id:
        return empty
    try:
        from .. import db
        row = db.query_one(
            "SELECT COALESCE(SUM(input_tokens), 0) AS input_tokens, "
            "COALESCE(SUM(output_tokens), 0) AS output_tokens, "
            "COALESCE(SUM(cached_tokens), 0) AS cached_tokens, "
            "COALESCE(SUM(total_tokens), 0) AS total_tokens "
            "FROM ai_request_log WHERE job_id = ?", (job_id,))
    except Exception:  # noqa: BLE001 — statistics are never fatal
        return empty
    if not row:
        return empty
    values = {key: int(row[key] or 0) for key in empty}
    return values


def for_job(job_id: str) -> list[dict]:
    """Rows for one correction, newest last (diagnostics, never bulk lists)."""
    if not job_id:
        return []
    try:
        from .. import db
        rows = db.query(
            "SELECT kind, request_id, job_id, model, endpoint, stage, purpose, "
            "input_tokens, output_tokens, cached_tokens, total_tokens, "
            "duration_ms, attempt, error_class, http_status, upload_count, "
            "reused, outcome, estimated, created_at "
            "FROM ai_request_log WHERE job_id = ? ORDER BY id ASC", (job_id,))
        return [dict(row) for row in rows]
    except Exception:  # noqa: BLE001
        return []


def reset() -> None:
    """Testing helper: empty the ledger."""
    try:
        from .. import db
        db.execute("DELETE FROM ai_request_log")
    except Exception:  # noqa: BLE001
        pass
    _last_row.set(None)


def export_json(hours: int = 168) -> str:
    """The summary as JSON (used by the diagnostics page)."""
    return json.dumps(summary(hours=hours), indent=2, ensure_ascii=True)
