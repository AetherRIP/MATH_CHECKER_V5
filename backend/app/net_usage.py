"""Internet usage accounting + rolling limits (V5 network budget §8).

Tracks bytes up/down in UTC-hour buckets so the app can enforce the
configured network budget (default: 500 MB/hour during active runtime).
Accounting is BEST-EFFORT by design: ``record`` never raises, so usage
tracking can never break a correction. Values are exact where the byte
count is known (file uploads/downloads) and estimated where the SDK hides
the wire format (generation request overhead) — the page labels this.

Categories:
    gemini_upload        file bytes uploaded to the Gemini Files API (exact)
    gemini_generate      one generation round-trip (response exact via
                         response.text; request overhead estimated)
    gemini_health        tiny health probes (Save & Test / Test all keys)
    classroom_api        Classroom REST JSON calls (small)
    classroom_download   Drive attachment / export downloads (exact)
"""

from __future__ import annotations

import threading
import time

from . import db
from .config import get_config

MB = 1024 * 1024

CATEGORIES = (
    "gemini_upload", "gemini_generate", "gemini_health",
    "classroom_api", "classroom_download", "other",
)

# Estimated wire overhead of one generation round-trip besides the response
# text (prompt JSON, file references, TLS/HTTP framing). Documented as an
# estimate on the usage page.
GENERATION_REQUEST_OVERHEAD_BYTES = 20_000
SMALL_REQUEST_BYTES = 1_000

_record_lock = threading.Lock()


def bucket(ts: float | None = None) -> int:
    """UTC hour bucket (epoch seconds at the start of the hour)."""
    ts = time.time() if ts is None else ts
    return int(ts // 3600) * 3600


def day_bucket(ts: float | None = None) -> int:
    """UTC day bucket (epoch seconds at UTC midnight)."""
    ts = time.time() if ts is None else ts
    return int(ts // 86400) * 86400


def record(category: str, bytes_up: int = 0, bytes_down: int = 0,
           ts: float | None = None) -> None:
    """Add bytes to the hour bucket containing ``ts`` (default: now).
    Best-effort: NEVER raises — usage accounting must not be able to break a
    correction (child workers also write here, so any transient SQLite
    contention is ignored)."""
    if bytes_up <= 0 and bytes_down <= 0:
        return
    if category not in CATEGORIES:
        category = "other"
    try:
        with _record_lock:
            db.execute(
                """
                INSERT INTO net_usage(bucket, category, bytes_up, bytes_down)
                VALUES(?, ?, ?, ?)
                ON CONFLICT(bucket, category) DO UPDATE SET
                    bytes_up = bytes_up + excluded.bytes_up,
                    bytes_down = bytes_down + excluded.bytes_down
                """,
                (bucket(ts), category, int(bytes_up), int(bytes_down)),
            )
    except Exception:  # noqa: BLE001 - accounting must never break work
        pass


def _window_totals(start_bucket: int, end_bucket: int) -> dict:
    rows = db.query(
        """
        SELECT category, SUM(bytes_up) AS up, SUM(bytes_down) AS down
        FROM net_usage WHERE bucket >= ? AND bucket < ?
        GROUP BY category
        """,
        (start_bucket, end_bucket),
    )
    by_category = {r["category"]: {"up": r["up"] or 0, "down": r["down"] or 0} for r in rows}
    up = sum(v["up"] for v in by_category.values())
    down = sum(v["down"] for v in by_category.values())
    return {"up": up, "down": down, "total": up + down, "by_category": by_category}


def limits() -> dict:
    """Configured limits in BYTES (0 = unlimited)."""
    cfg = get_config().usage
    return {"hourly_mb": cfg.hourly_mb, "daily_mb": cfg.daily_mb,
            "hourly_bytes": cfg.hourly_mb * MB, "daily_bytes": cfg.daily_mb * MB}


def hourly_over(now_ts: float | None = None) -> bool:
    now_ts = time.time() if now_ts is None else now_ts
    hourly = limits()["hourly_bytes"]
    if hourly <= 0:
        return False
    return _window_totals(bucket(now_ts), bucket(now_ts) + 3600)["total"] >= hourly


def daily_over(now_ts: float | None = None) -> bool:
    now_ts = time.time() if now_ts is None else now_ts
    daily = limits()["daily_bytes"]
    if daily <= 0:
        return False
    return _window_totals(day_bucket(now_ts), day_bucket(now_ts) + 86400)["total"] >= daily


def usage_state(now_ts: float | None = None) -> dict:
    """Everything the usage page / enforcement points need."""
    now_ts = time.time() if now_ts is None else now_ts
    lim = limits()
    hour = _window_totals(bucket(now_ts), bucket(now_ts) + 3600)
    day = _window_totals(day_bucket(now_ts), day_bucket(now_ts) + 86400)
    out = {
        "now": now_ts,
        "hour": hour, "day": day,
        "limits": lim,
        "hourly_over": lim["hourly_bytes"] > 0 and hour["total"] >= lim["hourly_bytes"],
        "daily_over": lim["daily_bytes"] > 0 and day["total"] >= lim["daily_bytes"],
        "hour_remaining_mb": (max(0, lim["hourly_bytes"] - hour["total"]) / MB
                              if lim["hourly_bytes"] > 0 else None),
        "day_remaining_mb": (max(0, lim["daily_bytes"] - day["total"]) / MB
                             if lim["daily_bytes"] > 0 else None),
    }
    return out


def history(hours: int = 24, now_ts: float | None = None) -> list[dict]:
    """Per-hour totals for the last ``hours`` hours (oldest first)."""
    now_ts = time.time() if now_ts is None else now_ts
    start = bucket(now_ts) - (hours - 1) * 3600
    rows = db.query(
        """
        SELECT bucket, category, SUM(bytes_up) AS up, SUM(bytes_down) AS down
        FROM net_usage WHERE bucket >= ? GROUP BY bucket, category
        """,
        (start,),
    )
    agg: dict[int, dict] = {}
    for r in rows:
        slot = agg.setdefault(r["bucket"], {"bucket": r["bucket"], "up": 0, "down": 0})
        slot["up"] += r["up"] or 0
        slot["down"] += r["down"] or 0
    return [agg.get(bucket(start + i * 3600),
                    {"bucket": start + i * 3600, "up": 0, "down": 0})
            for i in range(hours)]
