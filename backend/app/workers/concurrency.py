"""Adaptive worker concurrency: additive increase, multiplicative decrease.

The configured ``worker.concurrency`` is a CEILING, not a target. Before this
controller the pool read it once and never moved, so every worker discovered the
same provider throttling independently and the batch kept pressing: one 429
storm was amplified by exactly the number of workers running.

The controller only ever REDUCES parallelism while the provider is throttling,
and recovers one slot at a time after a sustained run of successes. Two
properties matter more than the arithmetic:

* **It is shared.** A correction runs in a worker child process, so the child is
  the process that sees the 429. The decision is written to ``app_meta`` and the
  pool reads it, so throttling inside one child slows the parent's dispatching
  instead of being invisible to it.
* **It survives a restart.** The limit is persisted, so restarting the app in
  the middle of an outage does not reset every worker to maximum and re-create
  the storm.
"""

from __future__ import annotations

import json
import logging
import threading
import time

log = logging.getLogger("worker.concurrency")

META_KEY = "worker.concurrency_control"

#: A negative verdict is acted on immediately; a positive one is earned.
SUCCESSES_TO_GROW = 5
#: After a reduction, hold before growing again so recovery is not a sawtooth.
RECOVERY_HOLD_SECONDS = 120.0
#: Another process's decision is adopted within this window.
REFRESH_SECONDS = 5.0
FLOOR = 1

#: Failure classes that mean "the provider is pushing back", not "this request
#: was wrong". A 400 or 403 says nothing about capacity and must not shrink the
#: pool — that would throttle healthy work over one malformed paper.
THROTTLE_CLASSES = frozenset({
    "RATE_LIMIT",
    "HIGH_DEMAND",
    "TRANSIENT_PROVIDER_ERROR",
})


def _as_count(value) -> int | None:
    """A positive whole number, or None. Shared state is never trusted."""
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number >= FLOOR else None


def _enabled() -> bool:
    try:
        from ..config import get_config
        return bool(getattr(get_config().gemini, "adaptive_concurrency", True))
    except Exception:  # noqa: BLE001
        return True


class ConcurrencyControl:
    """AIMD limit shared by every process through ``app_meta``."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._limit: int | None = None
        self._ceiling: int = 1
        self._successes: int = 0
        self._last_decrease_at: float | None = None
        self._last_reason: str = ""
        self._decreases: int = 0
        self._last_refresh = 0.0

    # -- state -----------------------------------------------------------

    def _persist(self) -> None:
        try:
            from .. import db
            with self._lock:
                payload = {
                    "version": 1,
                    "limit": self._limit,
                    "ceiling": self._ceiling,
                    "successes": self._successes,
                    "last_decrease_at": self._last_decrease_at,
                    "last_reason": self._last_reason,
                    "decreases": self._decreases,
                    "updated_at": time.time(),
                }
            db.set_meta(META_KEY, json.dumps(payload))
        except Exception:  # noqa: BLE001 — never break a job over a statistic
            pass

    def _refresh(self) -> None:
        """Adopt state another process wrote (a child sees the throttle first)."""
        now = time.time()
        if now - self._last_refresh < REFRESH_SECONDS:
            return
        self._last_refresh = now
        try:
            from .. import db
            raw = db.get_meta(META_KEY)
        except Exception:  # noqa: BLE001
            return
        if not raw:
            return
        try:
            data = json.loads(raw)
        except (TypeError, ValueError):
            return
        if not isinstance(data, dict):
            return
        ceiling = _as_count(data.get("ceiling"))
        limit = _as_count(data.get("limit"))
        try:
            decrease = float(data.get("last_decrease_at") or 0) or None
        except (TypeError, ValueError):
            decrease = None
        with self._lock:
            # A worker child has no pool, so the ceiling it should reason about
            # can only come from here. Never let an adopted ceiling shrink the
            # local one: the pool recomputes it on every dispatch anyway.
            if ceiling and self._last_decrease_at is None:
                self._ceiling = max(self._ceiling, ceiling)
            if limit is not None and (self._limit is None or limit < self._limit):
                self._limit = limit
            if decrease and (self._last_decrease_at is None
                             or decrease > self._last_decrease_at):
                self._last_decrease_at = decrease
                self._last_reason = str(data.get("last_reason") or "")

    # -- the API the pool uses --------------------------------------------

    def limit(self, ceiling: int) -> int:
        """How many corrections may run at once, never above ``ceiling``.

        Without a recorded throttle this simply IS the ceiling — a ceiling that
        happens to be low (one healthy key, a small pool) must never be mistaken
        for a provider problem and then be remembered as one.
        """
        ceiling = max(FLOOR, int(ceiling or FLOOR))
        if not _enabled():
            return ceiling
        self._refresh()
        with self._lock:
            self._ceiling = max(self._ceiling, ceiling)
            if self._last_decrease_at is None:
                self._limit = ceiling
                return ceiling
            self._limit = max(FLOOR, min(self._limit or ceiling, ceiling))
            return self._limit

    def note_outcome(self, *, ok: bool, error_class: str | None = None) -> None:
        """Feed one provider outcome. Only throttling shrinks the pool."""
        if not _enabled():
            return
        now = time.time()
        if not ok and (error_class in THROTTLE_CLASSES):
            with self._lock:
                current = max(FLOOR, self._limit or self._ceiling or FLOOR)
                reduced = max(FLOOR, current // 2)
                if reduced >= current:
                    # Already at the floor: record it once, do not churn.
                    self._last_reason = str(error_class)
                    return
                self._limit = reduced
                self._successes = 0
                self._last_decrease_at = now
                self._last_reason = str(error_class)
                self._decreases += 1
            self._persist()
            log.warning("worker concurrency reduced to %s after %s",
                        reduced, error_class)
            return
        if not ok:
            return
        with self._lock:
            self._successes += 1
            at_limit = self._limit is None or self._limit >= self._ceiling
            held = (self._last_decrease_at is not None
                    and now - self._last_decrease_at < RECOVERY_HOLD_SECONDS)
            if at_limit or held or self._successes < SUCCESSES_TO_GROW:
                return
            self._limit = min(self._ceiling, (self._limit or FLOOR) + 1)
            self._successes = 0
            grown = self._limit
        self._persist()
        log.info("worker concurrency recovered to %s", grown)

    def snapshot(self) -> dict:
        self._refresh()
        with self._lock:
            return {
                "enabled": _enabled(),
                "limit": self._limit,
                "ceiling": self._ceiling,
                "successes_since_change": self._successes,
                "last_decrease_at": self._last_decrease_at,
                "last_reason": self._last_reason,
                "decreases": self._decreases,
                "successes_to_grow": SUCCESSES_TO_GROW,
                "recovery_hold_seconds": RECOVERY_HOLD_SECONDS,
                "floor": FLOOR,
            }

    def reset(self) -> None:
        """Testing/diagnostics: forget the reduction (back to the ceiling)."""
        with self._lock:
            self._limit = None
            self._successes = 0
            self._last_decrease_at = None
            self._last_reason = ""
            self._decreases = 0
            self._last_refresh = 0.0
        self._persist()


_control = ConcurrencyControl()


def concurrency_control() -> ConcurrencyControl:
    return _control
