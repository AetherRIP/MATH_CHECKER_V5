"""Multi-key Gemini management: fingerprints, health, rotation, persistence.

Rules implemented here (V5 §4):
* keys exist only in the secret store; everywhere else they are a SHA-256
  fingerprint prefix,
* 429/quota → jittered exponential cooldown (5s doubling, 10 min cap),
* 401/leaked → INVALID (out of rotation until re-enabled),
* healthy keys rotate round-robin to balance load,
* all-keys-down → WAITING_FOR_API (caller retries with backoff).
"""

from __future__ import annotations

import hashlib
import logging
import random
import threading
import time

from ..config import get_secrets
from ..db import get_meta, query, query_one, execute, set_meta, now as db_now
from ..errors import classify_gemini_error, http_status_of
from ..logging_setup import log_event, redact

log = logging.getLogger("gemini.keys")

COOLDOWN_BASE_SECONDS = 5.0
COOLDOWN_CAP_SECONDS = 600.0

STATUS_HEALTHY = "HEALTHY"
STATUS_COOLDOWN = "COOLDOWN"
STATUS_INVALID = "INVALID"
STATUS_DISABLED = "DISABLED"
STATUS_NO_KEYS = "NO_KEYS"

STATUSES = (STATUS_HEALTHY, STATUS_COOLDOWN, STATUS_INVALID, STATUS_DISABLED, STATUS_NO_KEYS)


def fingerprint(key: str) -> str:
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:12]


def mask_key(key: str) -> str:
    """Legacy-compatible masked display (first 4 / last 4) — the friend's app
    used this; V5 surfaces only fingerprints in listings instead."""
    if len(key) <= 8:
        return "********"
    return f"{key[:4]}{'*' * max(4, len(key) - 8)}{key[-4:]}"


def _safe_error_text(value: object, limit: int = 500) -> str:
    """Provider error text with key material removed before it is STORED.

    ``gemini_keys.last_error`` is persisted and returned by ``/api/keys``, so
    it gets the same redaction the logs use: a provider error that echoes the
    request URL is the realistic way a key would otherwise land in the
    database and on the screen. Redaction runs BEFORE the length cap, because
    a truncated key is still key material.
    """
    return redact(str(value))[:limit]


class KeyRecord:
    __slots__ = (
        "fingerprint", "requests", "successes", "failures",
        "rate_limit_events", "quota_errors", "auth_errors", "timeout_errors",
        "cooldown_until", "cooldown_kind", "last_used", "last_failure",
        "last_error", "invalid", "disabled", "label",
    )

    def __init__(self, fp: str, label: str | None = None):
        self.fingerprint = fp
        self.label = label
        self.requests = 0
        self.successes = 0
        self.failures = 0
        self.rate_limit_events = 0
        self.quota_errors = 0
        self.auth_errors = 0
        self.timeout_errors = 0
        self.cooldown_until = 0.0
        self.cooldown_kind: str | None = None
        self.last_used: float | None = None
        self.last_failure: float | None = None
        self.last_error: str | None = None
        self.invalid = False
        self.disabled = False

    # -- persistence -----------------------------------------------------

    def save(self) -> None:
        # ``invalid`` is its OWN column. It used to be stored in ``removed``,
        # which the "delete this key from the list" flow also sets — so a key
        # that had ever recorded a failure came back INVALID merely for having
        # been deleted and restored, with no auth failure behind it at all.
        # ``removed`` now means only "no longer in the configured key list" and
        # is owned by :meth:`mark_removed`; bookkeeping must never rewrite it.
        execute(
            """
            INSERT INTO gemini_keys(fingerprint, label, added_at, removed, disabled, invalid)
            VALUES(?, ?, COALESCE((SELECT added_at FROM gemini_keys WHERE fingerprint = ?), ?), 0, ?, ?)
            ON CONFLICT(fingerprint) DO UPDATE SET
                label = COALESCE(excluded.label, gemini_keys.label),
                disabled = excluded.disabled,
                invalid = excluded.invalid
            """,
            (self.fingerprint, self.label, self.fingerprint, db_now(), int(self.disabled), int(self.invalid)),
        )
        execute(
            """
            INSERT INTO key_stats(
                fingerprint, requests, successes, failures, rate_limit_events,
                quota_errors, auth_errors, timeout_errors, cooldown_until,
                cooldown_kind, last_used, last_failure, last_error
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(fingerprint) DO UPDATE SET
                requests = excluded.requests,
                successes = excluded.successes,
                failures = excluded.failures,
                rate_limit_events = excluded.rate_limit_events,
                quota_errors = excluded.quota_errors,
                auth_errors = excluded.auth_errors,
                timeout_errors = excluded.timeout_errors,
                cooldown_until = excluded.cooldown_until,
                cooldown_kind = excluded.cooldown_kind,
                last_used = excluded.last_used,
                last_failure = excluded.last_failure,
                last_error = excluded.last_error
            """,
            (
                self.fingerprint, self.requests, self.successes, self.failures,
                self.rate_limit_events, self.quota_errors, self.auth_errors,
                self.timeout_errors, self.cooldown_until, self.cooldown_kind,
                self.last_used, self.last_failure, self.last_error,
            ),
        )

    def load(self) -> None:
        row = query_one(
            """
            SELECT k.removed, k.disabled, k.invalid, k.label,
                   s.requests, s.successes, s.failures, s.rate_limit_events,
                   s.quota_errors, s.auth_errors, s.timeout_errors,
                   s.cooldown_until, s.cooldown_kind, s.last_used,
                   s.last_failure, s.last_error
            FROM gemini_keys k LEFT JOIN key_stats s USING(fingerprint)
            WHERE k.fingerprint = ?
            """,
            (self.fingerprint,),
        )
        if row is None:
            return
        self.disabled = bool(row["disabled"])
        self.label = row["label"] or self.label
        self.requests = row["requests"] or 0
        self.successes = row["successes"] or 0
        self.failures = row["failures"] or 0
        self.rate_limit_events = row["rate_limit_events"] or 0
        self.quota_errors = row["quota_errors"] or 0
        self.auth_errors = row["auth_errors"] or 0
        self.timeout_errors = row["timeout_errors"] or 0
        self.cooldown_until = row["cooldown_until"] or 0.0
        self.cooldown_kind = row["cooldown_kind"]
        self.last_used = row["last_used"]
        self.last_failure = row["last_failure"]
        self.last_error = row["last_error"]
        # INVALID is read from its OWN flag: it is set only by a recorded
        # credential rejection (locked in :meth:`KeyManager.record_failure`).
        # A key that previously failed auth comes back only as far as INVALID
        # unless the secret-store key itself changed (a new fingerprint would
        # be a new record), and the user can re-enable it from the UI — but a
        # key that was merely removed from the list and later restored, or that
        # only ever hit a transient failure, loads as usable.
        self.invalid = bool(row["invalid"])

    def public(self) -> dict:
        now_ts = time.time()
        if self.disabled:
            status = STATUS_DISABLED
        elif self.invalid:
            status = STATUS_INVALID
        elif self.cooldown_until > now_ts:
            status = STATUS_COOLDOWN
        else:
            status = STATUS_HEALTHY
        return {
            "fingerprint": self.fingerprint,
            "label": self.label,
            "status": status,
            "requests": self.requests,
            "successes": self.successes,
            "failures": self.failures,
            "rate_limit_events": self.rate_limit_events,
            "quota_errors": self.quota_errors,
            "auth_errors": self.auth_errors,
            "timeout_errors": self.timeout_errors,
            "cooldown_remaining": max(0, int(self.cooldown_until - now_ts)),
            "cooldown_kind": self.cooldown_kind,
            "last_used": self.last_used,
            "last_failure": self.last_failure,
            "last_error": self.last_error,
            "disabled": self.disabled,
        }


class KeyManager:
    """Owns the key registry; provides rotation and failure bookkeeping.

    Keys are opaque secret strings — this class never inspects, transforms
    or validates their format; it only fingerprints them for safe display
    and bookkeeping.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._records: dict[str, KeyRecord] = {}
        self._rr = 0
        self._leased: set[str] = set()   # fingerprints exclusively leased (§18)

    # -- registry --------------------------------------------------------

    def sync_with_secret_store(self) -> list[str]:
        """Load fingerprints for configured keys; returns active fingerprints."""
        keys = get_secrets().gemini_keys()
        with self._lock:
            active: list[str] = []
            for key in keys:
                fp = fingerprint(key)
                rec = self._records.get(fp)
                if rec is None:
                    rec = KeyRecord(fp)
                    rec.load()
                    self._records[fp] = rec
                active.append(fp)
            # Mark registry entries no longer present in the secret store.
            for fp, rec in self._records.items():
                if fp not in active and not rec.invalid:
                    # keep stats; mark removed so history stays visible
                    pass
            return active

    def _records_for_active_keys(self) -> list[KeyRecord]:
        fps = self.sync_with_secret_store()
        return [self._records[fp] for fp in fps]

    def available(self) -> list[KeyRecord]:
        """Healthy-or-cooling keys eligible for rotation (cooldown checked lazily)."""
        now_ts = time.time()
        recs = [r for r in self._records_for_active_keys() if not r.invalid and not r.disabled]
        return recs

    def status(self) -> dict:
        recs = self._records_for_active_keys()
        now_ts = time.time()
        items = [r.public() for r in recs]
        healthy = [i for i in items if i["status"] == STATUS_HEALTHY]
        cooling = [i for i in items if i["status"] == STATUS_COOLDOWN]
        invalid = [i for i in items if i["status"] == STATUS_INVALID]
        disabled = [i for i in items if i["status"] == STATUS_DISABLED]
        return {
            "total": len(items),
            "healthy": len(healthy),
            "cooldown": len(cooling),
            "invalid": len(invalid),
            "disabled": len(disabled),
            "has_keys": len(items) > 0,
            "all_down": len(items) > 0 and len(healthy) == 0,
            "keys": items,
        }

    def register_manual_result(self, fp: str, *, ok: bool, kind: str, message: str, response_time: float | None = None) -> None:
        """Record a staged-test result (Test All Keys) into the stats."""
        with self._lock:
            rec = self._records.get(fp) or self._get_or_create(fp)
        rec.requests += 1
        rec.last_used = time.time()
        if ok:
            rec.successes += 1
            rec.cooldown_until = 0
            rec.cooldown_kind = None
        else:
            rec.failures += 1
            rec.last_failure = time.time()
            rec.last_error = _safe_error_text(message)
            if kind == "invalid_key":
                rec.invalid = True
                rec.auth_errors += 1
            elif kind == "rate_limited":
                rec.rate_limit_events += 1
                rec.quota_errors += 1
                self._apply_cooldown(rec, kind)
        rec.save()

    # -- selection ---------------------------------------------------------

    def acquire(self) -> tuple[str, KeyRecord]:
        """Pick the next usable key (round-robin over healthy-first order).

        Raises KeyError-equivalent (WaitForApiError) when nothing is usable.
        Note: this does NOT reserve the key — concurrent callers may share
        it. The worker pool uses :meth:`acquire_exclusive` instead.
        """
        with self._lock:
            configured = self._records_for_active_keys()
            if not configured:
                raise NoKeysError("No Gemini API keys are configured.")
            candidates = [r for r in configured
                          if not r.invalid and not r.disabled]
            if not candidates:
                # Prefer the recovery-oriented signal: when every key is
                # INVALID the user can re-enable; when none are configured we
                # raise NoKeysError above. Either way the caller parks instead
                # of hammering.
                raise WaitForApiError(
                    "No usable Gemini keys remain (all invalid or disabled)."
                )
            healthy = [r for r in candidates if r.cooldown_until <= time.time()]
            if not healthy:
                raise WaitForApiError(
                    "All Gemini keys are cooling down after failures."
                )
            # round-robin across healthy keys to balance usage
            self._rr = (self._rr + 1) % len(healthy)
            rec = healthy[self._rr]
            rec.requests += 1
            rec.last_used = time.time()
            return rec.fingerprint, rec

    def acquire_exclusive(self) -> tuple[str, KeyRecord]:
        """Lease one healthy key EXCLUSIVELY to the caller (§18).

        Two callers never receive the same key while it is leased. The
        caller must return it with :meth:`release_exclusive` (usually via
        ``CorrectionWorker.release_key``). Raises WaitForApiError when every
        usable key is already leased — the caller should park and retry.
        """
        with self._lock:
            configured = self._records_for_active_keys()
            if not configured:
                raise NoKeysError("No Gemini API keys are configured.")
            if all(r.invalid or r.disabled for r in configured):
                raise WaitForApiError(
                    "No usable Gemini keys remain (all invalid or disabled)."
                )
            candidates = [r for r in configured
                          if (not r.invalid and not r.disabled
                              and r.cooldown_until <= time.time()
                              and r.fingerprint not in self._leased)]
            if not candidates:
                raise WaitForApiError(
                    "All healthy Gemini keys are currently leased to running jobs."
                )
            self._rr = (self._rr + 1) % len(candidates)
            rec = candidates[self._rr]
            rec.requests += 1
            rec.last_used = time.time()
            self._leased.add(rec.fingerprint)
            return rec.fingerprint, rec

    def release_exclusive(self, fp: str) -> None:
        """Return an exclusively-leased key to the rotation (§18)."""
        with self._lock:
            self._leased.discard(fp)

    def _get_or_create(self, fp: str) -> KeyRecord:
        rec = self._records.get(fp)
        if rec is None:
            rec = KeyRecord(fp)
            rec.load()
            self._records[fp] = rec
        return rec

    # -- outcome bookkeeping -------------------------------------------------

    def record_success(self, fp: str) -> None:
        with self._lock:
            rec = self._get_or_create(fp)
        rec.successes += 1
        rec.cooldown_until = 0.0
        rec.cooldown_kind = None
        rec.save()

    def record_failure(self, fp: str, exc: Exception) -> None:
        kind = classify_gemini_error(exc)["kind"]
        with self._lock:
            rec = self._get_or_create(fp)
        rec.failures += 1
        rec.last_failure = time.time()
        rec.last_error = _safe_error_text(exc)

        if kind == "invalid_key":
            rec.auth_errors += 1
            rec.invalid = True
            log_event("gemini.keys", "key marked INVALID (auth failure)",
                      level=logging.WARNING, fingerprint=fp,
                      status=http_status_of(exc))
        elif kind == "rate_limited":
            rec.rate_limit_events += 1
            rec.quota_errors += 1
            self._apply_cooldown(rec, kind, message=str(exc))
        elif kind == "temporary":
            # 503 / high demand (spec §4): cool THIS key down and let a
            # healthy alternative serve the next request instead of
            # immediately resending the same giant request through it.
            self._apply_cooldown(rec, kind, message=str(exc))
        elif kind == "timeout":
            rec.timeout_errors += 1
        # forbidden/unknown: failure recorded; retry next request

        rec.save()

    def _apply_cooldown(self, rec: KeyRecord, kind: str,
                        message: str | None = None) -> None:
        # exponential backoff with jitter, hard-capped — and the provider's
        # Retry-After hint always wins when it asks for longer.
        from ..retry_policy import parse_retry_after
        previous = max(0.0, rec.cooldown_until - time.time())
        growth = max(COOLDOWN_BASE_SECONDS, previous * 2.0)
        delay = min(COOLDOWN_CAP_SECONDS, growth * random.uniform(0.8, 1.2))
        retry_after = parse_retry_after(message)
        if retry_after is not None:
            delay = max(delay, min(retry_after, COOLDOWN_CAP_SECONDS))
        rec.cooldown_until = time.time() + delay
        rec.cooldown_kind = kind
        status = http_status_of(message)
        try:
            from ..api_stats import record_cooldown
            record_cooldown()
        except Exception:
            pass
        log_event("gemini.keys", "key entered cooldown",
                  level=logging.WARNING, fingerprint=rec.fingerprint,
                  cooldown_seconds=round(delay), kind=kind,
                  retry_after=retry_after, status=status)

    def re_enable(self, fp: str) -> None:
        with self._lock:
            rec = self._get_or_create(fp)
        rec.invalid = False
        rec.disabled = False
        rec.cooldown_until = 0.0
        rec.cooldown_kind = None
        rec.save()

    def set_disabled(self, fp: str, disabled: bool) -> None:
        with self._lock:
            rec = self._get_or_create(fp)
        rec.disabled = disabled
        rec.save()

    def set_label(self, fp: str, label: str) -> None:
        """Attach a stable display name to a key (never contains the secret)."""
        with self._lock:
            rec = self._get_or_create(fp)
        rec.label = label
        rec.save()

    def mark_removed(self, fp: str) -> None:
        """A key was deleted from the secret store: hide it from listings and
        drop any exclusive lease, keeping its stats row for history."""
        with self._lock:
            self._leased.discard(fp)
            rec = self._records.get(fp)
        if rec is not None:
            execute(
                "UPDATE gemini_keys SET removed = 1 WHERE fingerprint = ?", (fp,))
            with self._lock:
                self._records.pop(fp, None)

    # -- diagnostics -------------------------------------------------------

    def aggregate(self) -> dict:
        items = self.status()["keys"]
        return {
            "requests": sum(i["requests"] for i in items),
            "successes": sum(i["successes"] for i in items),
            "failures": sum(i["failures"] for i in items),
            "rate_limit_events": sum(i["rate_limit_events"] for i in items),
            "timeout_errors": sum(i["timeout_errors"] for i in items),
        }


class NoKeysError(RuntimeError):
    pass


class WaitForApiError(RuntimeError):
    """No Gemini key is currently usable; caller should back off and retry."""
