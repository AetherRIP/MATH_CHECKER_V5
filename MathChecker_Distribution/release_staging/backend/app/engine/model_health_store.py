"""Cross-process model health + model pinning.

The legacy engine keeps its model bench (``MODEL_HEALTH``) in process memory.
Worker children are short-lived processes, so before this module every job
re-discovered which models were failing — and the model that actually marked a
submission depended on transient 429/503s at that exact moment. Two runs of the
same PDF could therefore land on ``gemini-3.7-flash`` and ``gemini-3.5-flash``
and disagree on marks.

This module persists that state (bench entries + a pinned preferred model) in
``app_meta`` so that:

* a model benched by one job is skipped by the next job inside the window,
* the model chain order is stable across runs: the last model that worked for
  marking keeps the first slot until it actually fails,
* model changes become rare, deliberate events instead of per-job coin flips.

All of it is best-effort: any failure to read/write the state simply means the
legacy behaviour (fresh in-process health) is used.
"""

from __future__ import annotations

import json
import logging

from .. import db
from . import legacy_marking as legacy

log = logging.getLogger("engine.model_health")

_META_KEY = "model_health_v1"


def load() -> None:
    """Restore bench state + preferred model (call at job start)."""
    try:
        raw = db.get_meta(_META_KEY)
        if not raw:
            return
        data = json.loads(raw)
    except Exception:  # noqa: BLE001 - corrupt state must never break a job
        return
    legacy.MODEL_HEALTH.restore(data.get("bad") or {})


def save() -> None:
    """Persist current bench state (call after marking)."""
    try:
        data = {"bad": legacy.MODEL_HEALTH.snapshot(),
                "preferred": preferred_model()}
        db.set_meta(_META_KEY, json.dumps(data))
    except Exception:  # noqa: BLE001
        log.debug("model health persistence skipped", exc_info=True)


def _read() -> dict:
    try:
        raw = db.get_meta(_META_KEY)
        return json.loads(raw) if raw else {}
    except Exception:  # noqa: BLE001
        return {}


def preferred_model() -> str | None:
    return _read().get("preferred")


def ordered_models(chain: list[str]) -> list[str]:
    """The configured chain with the pinned model moved to the front."""
    preferred = preferred_model()
    if preferred and preferred in chain:
        return [preferred] + [m for m in chain if m != preferred]
    return list(chain)


def record_success(model: str, chain: list[str]) -> None:
    """Re-pin only for a REAL reason: no pin yet, the pinned model left the
    configured chain, or it is permanently unavailable (404 / limit: 0).

    A transient 429/503 must NOT move the pin — that is exactly what made the
    marking model a per-job lottery before. ``usable_models`` still skips a
    temporarily benched model within a run, and the pin brings the chain back
    to the same first model as soon as the bench expires.
    """
    if model not in chain:
        return
    preferred = preferred_model()
    if preferred == model:
        return
    benched = legacy.MODEL_HEALTH.benched()
    preferred_dead = (
        preferred is None
        or preferred not in chain
        or (preferred in benched and benched[preferred][0] == "unavailable")
    )
    if preferred_dead:
        try:
            data = _read()
            data["preferred"] = model
            db.set_meta(_META_KEY, json.dumps(data))
        except Exception:  # noqa: BLE001
            log.debug("model pin skipped", exc_info=True)
