"""Provider-level health gate — stop hammering an overloaded provider (§11).

A single 503 is handled per-key (cooldown + failover). This module watches
high-demand failures *for the model that actually failed* and opens a
cooldown window during which:

* the worker pool dispatches NOTHING when the provider is genuinely down
  (queued jobs stay queued — no downloads, no uploads, no model requests),
  and
* KeyAwareClient refuses to send generate_content for a cooling model — the
  failed request is NOT immediately resent with the whole submission + mark
  scheme.

This is what stops a 75-student batch from firing 55 huge requests into an
outage: detect → cooldown → pause → resume when healthy again (or use a
different healthy model while this one recovers).

Scope: the outage is PER MODEL, not global
--------------------------------------------
Gemini's ``503 UNAVAILABLE "This model is currently experiencing high
demand"`` is emitted for one specific model. Measured with a 1-token probe
across the configured keys: 11 of 12 keys succeeded on ``gemini-3.8-flash``
in the same minute in which the 12th returned 503, and ``gemini-3.7-flash``
returned 503 while ``gemini-3.6-flash``/``gemini-3.5-flash-lite`` answered
normally. Treating such a 503 as a provider-wide verdict made ONE unlucky
request freeze ALL keys and ALL models — which is what produced the false
"Gemini provider is cooling down after high demand" for every key.

So high-demand state is tracked per model. Only the wildcard bucket (a
failure not attributable to a model) gates the whole provider, and dispatch
pauses only when the wildcard is cooling or EVERY model is cooling.

Half-open recovery (no more lock-out)
-------------------------------------
The previous streak counter was reset *only* by a success. That could never
happen: while the gate was closed no request was sent, so no success could
arrive, so the streak kept growing and the cooldown escalated 30s → 60s →
… → 600s on every probe. The gate could only be left by luck, and while a
batch was running it was effectively stuck (observed: 578s remaining,
69 cooldowns in one log).

Now a cooldown window is HALF-OPEN: when it expires the streak is reset, so
the next failure starts a fresh, bounded backoff instead of continuing to
ratchet. A streak also decays if failures stop arriving. Combined with a
hard cap on the escalation, a single 503 can never lock the app out for
longer than ``COOLDOWN_CAP_SECONDS``.

The gate is SHARED between worker processes
-------------------------------------------
A correction runs in its own child interpreter, so a purely in-memory gate
started every job with a clean slate: six concurrent workers each paid the
full "3 failures before the gate closes" entry cost, and the next paper paid
it again. Measured through this module's own retry path: 4 requests per paper
against an overloaded provider, 24 for six papers, while a gate that
survives between jobs sends 2 for the first paper and 0 for the rest of the
cooldown window.

So the cooldown/streak state is also written to ``app_meta`` (one small JSON
row) and re-read when a gate is consulted, and a fresh child therefore starts
closed instead of learning the outage again. Reads are throttled to once per
``REFRESH_INTERVAL_SECONDS``; a failure to reach the database leaves the
gate exactly as it was (traffic control is never a reason to fail a job).
"""

from __future__ import annotations

import json
import logging
import random
import threading
import time
from dataclasses import dataclass

from ..logging_setup import log_event

log = logging.getLogger("gemini.provider")

# Consecutive high-demand failures on the SAME model before the gate closes.
HIGH_DEMAND_THRESHOLD = 3
COOLDOWN_BASE_SECONDS = 30.0
COOLDOWN_CAP_SECONDS = 240.0
COOLDOWN_JITTER = (0.8, 1.2)

# The streak is reset once a cooldown window expires, so escalation never
# ratchets past a handful of steps (30 → 60 → 120 → 240s), and a failure
# that arrives long after the previous one starts a fresh window.
MAX_ESCALATION_STEPS = 3

# Bucket used when a high-demand failure cannot be attributed to a model.
WILDCARD = "*"

#: Where the shared gate lives (one row, small JSON, rewritten on change).
GATE_META_KEY = "gemini_provider_gate"

#: How often a process re-reads the shared gate (cheap, but not per request).
REFRESH_INTERVAL_SECONDS = 2.0

# Failure class that closes the gate (retry_policy.HIGH_DEMAND).
HIGH_DEMAND = "HIGH_DEMAND"


class ProviderHealth:
    """Circuit breaker for model/provider overload (thread-safe)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        # model -> streak state (see _Streak)
        self._buckets: dict[str, _Streak] = {}
        self._cooldowns = 0
        self._last_failure_at: float | None = None
        self._last_failure_kind: str | None = None
        self._last_failure_model: str | None = None
        self._last_success_at: float | None = None
        self._last_refresh = 0.0

    # -- outcomes --------------------------------------------------------

    def record_success(self, model: str | None = None) -> None:
        """A request succeeded. Clears that model's streak (and the global
        streak — proof the provider is reachable again)."""
        with self._lock:
            self._last_success_at = time.time()
            if model:
                self._buckets.pop(model, None)
            else:
                self._buckets.clear()
        self._persist()

    def record_failure(self, cls: str, message: str = "",
                       model: str | None = None) -> None:
        """Feed a classified failure. Only high-demand overload is tracked."""
        if cls != HIGH_DEMAND:
            return
        now_ts = time.time()
        bucket = model or WILDCARD
        delay: float | None = None
        with self._lock:
            self._last_failure_at = now_ts
            self._last_failure_kind = cls
            self._last_failure_model = model
            streak = self._buckets.get(bucket)
            if streak is None:
                streak = _Streak()
                self._buckets[bucket] = streak
            # Decay: a long gap between failures starts a fresh window, so
            # an isolated 503 hours apart cannot accumulate into a lock-out.
            if streak.last_failure_at is not None \
                    and now_ts - streak.last_failure_at > COOLDOWN_CAP_SECONDS:
                streak.consecutive = 0
            # Half-open: the previous cooldown already elapsed without a
            # success (none could arrive while the gate was closed), so the
            # streak restarts instead of escalating without bound.
            if (streak.consecutive >= HIGH_DEMAND_THRESHOLD
                    and streak.cooldown_until
                    and now_ts >= streak.cooldown_until):
                streak.consecutive = 0
            streak.last_failure_at = now_ts
            streak.consecutive += 1
            if streak.consecutive < HIGH_DEMAND_THRESHOLD:
                return
            steps = min(streak.consecutive - HIGH_DEMAND_THRESHOLD,
                        MAX_ESCALATION_STEPS)
            delay = min(COOLDOWN_CAP_SECONDS,
                        COOLDOWN_BASE_SECONDS * (2 ** steps))
            delay *= random.uniform(*COOLDOWN_JITTER)
            streak.cooldown_until = max(streak.cooldown_until, now_ts + delay)
            self._cooldowns += 1
        self._persist()
        log_event("gemini.provider",
                  "provider cooldown engaged after consecutive high-demand "
                  "failures", level=logging.WARNING,
                  model=bucket, consecutive=streak.consecutive,
                  cooldown_seconds=round(delay),
                  last_error=(message or "")[:200])

    # -- gate -------------------------------------------------------------

    def available(self, model: str | None = None) -> tuple[bool, float]:
        """(allowed_to_send, seconds_remaining_of_cooldown).

        With ``model``: is THIS model able to serve a request?
        Without: is the provider as a whole able to serve *anything*?
        Dispatch uses the no-argument form and therefore pauses only when
        the provider itself is down or every model is cooling.
        """
        self._refresh()
        now_ts = time.time()
        with self._lock:
            if model is not None:
                # A wildcard cooldown is a provider-wide verdict: it blocks
                # every model, because nothing is attributable to a specific
                # one.
                wildcard = self._buckets.get(WILDCARD)
                global_remaining = max(0.0, (wildcard.cooldown_until if wildcard
                                              else 0.0) - now_ts)
                bucket = self._buckets.get(model)
                remaining = max(global_remaining,
                                max(0.0, (bucket.cooldown_until if bucket else 0.0)
                                    - now_ts))
                return (remaining <= 0.0, remaining)
            wildcard = self._buckets.get(WILDCARD)
            remaining = max(0.0, (wildcard.cooldown_until if wildcard else 0.0)
                            - now_ts)
            if remaining > 0:
                return False, remaining
            cooling = [b.cooldown_until - now_ts
                       for name, b in self._buckets.items()
                       if name != WILDCARD and b.cooldown_until > now_ts]
            if cooling:
                chain = _configured_models()
                # Models that have never failed are healthy by definition,
                # so dispatch is only paused when the WHOLE configured
                # fallback chain is cooling — a single model being busy
                # still leaves the others to carry the batch.
                if chain:
                    healthy = [m for m in chain
                               if not self._cooling_now(m, now_ts)]
                    if not healthy:
                        return False, max(cooling)
                elif len(cooling) == len([n for n in self._buckets
                                          if n != WILDCARD]):
                    # No chain known (tests/diagnostics): every model we have
                    # ever seen is cooling.
                    return False, max(cooling)
            return True, 0.0

    def _cooling_now(self, model: str, now_ts: float) -> bool:
        bucket = self._buckets.get(model)
        return bool(bucket and bucket.cooldown_until > now_ts)

    def remaining(self, model: str | None = None) -> float:
        ok, wait = self.available(model)
        return 0.0 if ok else wait

    # -- diagnostics --------------------------------------------------------

    def snapshot(self, model: str | None = None) -> dict:
        ok, remaining = self.available(model)
        now_ts = time.time()
        with self._lock:
            models = {name: {
                "consecutive": b.consecutive,
                "cooldown_remaining": round(max(0.0, b.cooldown_until - now_ts), 1),
                "last_failure_at": b.last_failure_at,
            } for name, b in self._buckets.items()}
            totals = [b for n, b in self._buckets.items() if n != WILDCARD]
            consecutive = max([b.consecutive for b in totals], default=0)
            return {
                "available": ok,
                "cooldown_remaining": round(max(0.0, remaining), 1),
                "consecutive_high_demand": consecutive,
                "threshold": HIGH_DEMAND_THRESHOLD,
                "cooldowns": self._cooldowns,
                "cooldowns_cap_seconds": COOLDOWN_CAP_SECONDS,
                "last_failure_at": self._last_failure_at,
                "last_failure_kind": self._last_failure_kind,
                "last_failure_model": self._last_failure_model,
                "last_success_at": self._last_success_at,
                "models": models,
            }

    def reset(self) -> None:
        """Testing/diagnostics: clear the gate (shared state included)."""
        with self._lock:
            self._buckets.clear()
            self._cooldowns = 0
            self._last_failure_at = None
            self._last_failure_kind = None
            self._last_failure_model = None
            self._last_success_at = None
            self._last_refresh = 0.0
        self._persist()

    # -- sharing between worker processes --------------------------------

    def _persist(self) -> None:
        """Publish the cooldown state so the NEXT job's process starts warm."""
        try:
            from .. import db
            with self._lock:
                payload = {
                    "version": 1,
                    "models": {
                        name: {"consecutive": bucket.consecutive,
                               "cooldown_until": bucket.cooldown_until}
                        for name, bucket in self._buckets.items()
                        if bucket.cooldown_until > 0
                    },
                    "updated_at": time.time(),
                }
            db.set_meta(GATE_META_KEY, json.dumps(payload))
        except Exception:  # noqa: BLE001 — the gate must never break a job
            pass

    def _refresh(self) -> None:
        """Adopt a cooldown another worker process already established.

        Only the process-wide singleton reads shared state: a ProviderHealth
        created by a unit test stays hermetic.
        """
        if self is not _provider:
            return
        now_ts = time.time()
        if now_ts - self._last_refresh < REFRESH_INTERVAL_SECONDS:
            return
        self._last_refresh = now_ts
        try:
            from .. import db
            raw = db.get_meta(GATE_META_KEY)
            if not raw:
                return
            data = json.loads(raw)
            models = data.get("models") or {}
        except Exception:  # noqa: BLE001
            return
        if not isinstance(models, dict):
            return
        with self._lock:
            for name, entry in models.items():
                if not isinstance(entry, dict):
                    continue
                try:
                    cooldown = float(entry.get("cooldown_until") or 0.0)
                except (TypeError, ValueError):
                    continue
                if cooldown <= now_ts:
                    continue
                bucket = self._buckets.get(name)
                if bucket is None:
                    bucket = _Streak()
                    self._buckets[name] = bucket
                if cooldown > bucket.cooldown_until:
                    bucket.cooldown_until = cooldown
                    bucket.consecutive = max(
                        bucket.consecutive,
                        int(entry.get("consecutive") or 0))


@dataclass
class _Streak:
    consecutive: int = 0
    cooldown_until: float = 0.0
    last_failure_at: float | None = None


def _configured_models() -> list[str]:
    """The production fallback chain (primary + audit models), best effort."""
    try:
        from ..config import get_config
        cfg = get_config().gemini
        return list(cfg.primary_models) + list(cfg.audit_models)
    except Exception:
        return []


# process-wide singleton (child processes get their own — the pool gate is
# the authoritative dispatcher; the child gate is a second line of defence)
_provider = ProviderHealth()


def provider_health() -> ProviderHealth:
    return _provider