"""What each Gemini model can do, and whether THIS project can actually use it.

Two facts are deliberately never merged:

* **discovered** — Google's model listing returned the model. That proves the
  name exists somewhere, not that this account, project, region or free tier
  may call it.
* **verified** — a real request to this model succeeded from this installation.
  Only that proves operational availability.

A model is therefore never "available" because it appears in a list. The
registry keeps both flags, the evidence that produced them (timestamps, request
and failure counts), and whether a human approved the model for marking.

Approval is explicit, versioned and reversible: :func:`promote` refuses without
teacher-certified evidence, records the decision with the previous
configuration, and never runs automatically. Discovery may RECOMMEND a
candidate; it can never switch production over.

State lives in ``app_meta`` (one JSON document), so it survives a restart and is
shared by the backend and the worker child processes without a second store.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import asdict, dataclass, field

log = logging.getLogger("gemini.models")

META_KEY = "gemini.model_registry"
SCHEMA_VERSION = 1

ROLE_PRODUCTION = "production"
ROLE_LOW_RISK = "low_risk"
ROLES = (ROLE_PRODUCTION, ROLE_LOW_RISK)

_LOCK = threading.RLock()
_loaded_at = 0.0


class PromotionRefused(ValueError):
    """The evidence does not justify promoting this model."""


@dataclass
class ModelRecord:
    """One model, with discovery and verification kept apart."""

    model_id: str
    display_name: str = ""
    #: The listing API returned it (proves the name exists, nothing more).
    discovered: bool = False
    #: A real request from this installation succeeded.
    verified: bool = False
    endpoint: str = "generateContent"
    #: Best-known input modalities, from the listing or from documentation.
    inputs: list[str] = field(default_factory=lambda: ["text"])
    #: True when a structured response schema is known to be accepted.
    structured_output: bool | None = None
    input_token_limit: int | None = None
    output_token_limit: int | None = None
    #: Media-resolution settings this model accepts ("" = provider default).
    media_resolution: list[str] = field(default_factory=list)
    last_checked_at: float | None = None
    last_success_at: float | None = None
    last_failure_at: float | None = None
    last_error: str = ""
    requests: int = 0
    failures: int = 0
    approved_for_production: bool = False
    approved_for_low_risk: bool = False
    notes: list[str] = field(default_factory=list)

    @property
    def error_rate(self) -> float | None:
        if self.requests <= 0:
            return None
        return round(self.failures / self.requests, 4)

    def as_dict(self) -> dict:
        data = asdict(self)
        data["error_rate"] = self.error_rate
        # What a consumer may claim, stated once here instead of in each view.
        data["availability"] = _availability(self)
        return data


def _availability(record: ModelRecord) -> str:
    if record.verified:
        return "verified"
    if record.discovered:
        return "listed_not_verified"
    return "unknown"


def normalise_id(raw: object) -> str:
    """``models/gemini-x`` and ``gemini-x`` are the same model."""
    text = str(raw or "").strip()
    if text.startswith("models/"):
        text = text[len("models/"):]
    return text.strip().lower()


# -- state --------------------------------------------------------------------

def _empty_state() -> dict:
    return {
        "schema": SCHEMA_VERSION,
        "models": {},
        "promotion": None,
        "history": [],
        "updated_at": None,
    }


_STATE: dict = _empty_state()


def _load() -> dict:
    """Read the shared document once per process (and after any write)."""
    global _STATE, _loaded_at
    with _LOCK:
        try:
            from .. import db
            raw = db.get_meta(META_KEY)
        except Exception:  # noqa: BLE001 — the registry never breaks a job
            return _STATE
        if not raw:
            return _STATE
        try:
            data = json.loads(raw)
        except (TypeError, ValueError):
            log.warning("model registry document is unreadable; ignoring it")
            return _STATE
        if not isinstance(data, dict) or int(data.get("schema") or 0) != SCHEMA_VERSION:
            # A future/foreign schema is ignored rather than half-adopted.
            return _STATE
        _STATE = data
        _loaded_at = time.time()
        return _STATE


def _save() -> None:
    global _STATE
    with _LOCK:
        _STATE["updated_at"] = time.time()
        try:
            from .. import db
            db.set_meta(META_KEY, json.dumps(_STATE, ensure_ascii=False))
        except Exception:  # noqa: BLE001
            pass


def reset() -> None:
    """Testing/diagnostics: forget every record and promotion."""
    global _STATE
    with _LOCK:
        _STATE = _empty_state()
        _save()


def records() -> dict[str, ModelRecord]:
    """Every known model, as records (defensive copy of the stored state)."""
    state = _load()
    out: dict[str, ModelRecord] = {}
    for model_id, entry in (state.get("models") or {}).items():
        if not isinstance(entry, dict):
            continue
        known = {f for f in ModelRecord.__dataclass_fields__}  # type: ignore[attr-defined]
        clean = {k: v for k, v in entry.items() if k in known}
        clean.setdefault("model_id", model_id)
        try:
            out[model_id] = ModelRecord(**clean)
        except TypeError:
            continue
    return out


def _record_for(model: str) -> ModelRecord:
    model_id = normalise_id(model)
    state = _load()
    entry = (state.get("models") or {}).get(model_id)
    if isinstance(entry, dict):
        known = {f for f in ModelRecord.__dataclass_fields__}  # type: ignore[attr-defined]
        clean = {k: v for k, v in entry.items() if k in known}
        clean.setdefault("model_id", model_id)
        try:
            return ModelRecord(**clean)
        except TypeError:
            pass
    return ModelRecord(model_id=model_id)


def _store_record(record: ModelRecord) -> None:
    state = _load()
    models = state.setdefault("models", {})
    models[record.model_id] = asdict(record)
    _save()


# -- discovery ----------------------------------------------------------------

def discover(entries, *, endpoint: str = "generateContent") -> dict:
    """Mark models the provider's listing returned. Never marks them verified.

    ``entries`` items may be SDK objects or dicts; unknown shapes are skipped
    rather than guessed at.
    """
    discovered: list[str] = []
    for entry in entries or ():
        model_id = normalise_id(_field(entry, "name", "model", "model_id"))
        if not model_id:
            continue
        record = _record_for(model_id)
        record.discovered = True
        record.endpoint = endpoint
        record.last_checked_at = time.time()
        display = _field(entry, "display_name", "displayName")
        if display:
            record.display_name = str(display)[:120]
        limit = _field(entry, "input_token_limit", "inputTokenLimit")
        if isinstance(limit, int) and limit > 0:
            record.input_token_limit = limit
        out_limit = _field(entry, "output_token_limit", "outputTokenLimit")
        if isinstance(out_limit, int) and out_limit > 0:
            record.output_token_limit = out_limit
        actions = _field(entry, "supported_actions", "supportedActions",
                         "supported_generation_methods") or ()
        if any("generate" in str(a).lower() for a in actions):
            record.notes = _merge_note(
                record.notes, "listing advertises generateContent")
        _store_record(record)
        discovered.append(model_id)
    return {"ok": True, "discovered": discovered, "count": len(discovered)}


def _field(entry, *names):
    for name in names:
        if isinstance(entry, dict):
            if name in entry:
                return entry[name]
        else:
            value = getattr(entry, name, None)
            if value is not None:
                return value
    return None


def _merge_note(notes: list[str], note: str) -> list[str]:
    notes = list(notes or [])
    if note not in notes:
        notes.append(note)
    return notes[-8:]


def discover_live(client=None) -> dict:
    """Ask the provider for its model list. One cheap call, no inference.

    On failure the previously verified state is kept untouched, so a listing
    outage can never erase what this installation knows it can use.
    """
    try:
        if client is None:
            from .client_proxy import KeyAwareClient
            from .key_manager import KeyManager
            client = KeyAwareClient(KeyManager())
        entries = list(client.models.list())
    except Exception as exc:  # noqa: BLE001 — discovery is optional
        log.warning("model discovery failed: %s", type(exc).__name__)
        return {"ok": False, "reason": type(exc).__name__,
                "discovered": [], "count": 0,
                "detail": "previous verified state kept"}
    return discover(entries)


# -- verification -------------------------------------------------------------

def note_request(model: str | None, *, ok: bool, error: str = "") -> None:
    """Feed one real request outcome. Success is what makes a model verified."""
    if not model:
        return
    try:
        record = _record_for(model)
        record.requests += 1
        now = time.time()
        if ok:
            record.verified = True
            record.last_success_at = now
            if not record.discovered:
                # Operationally usable even though the listing never showed it.
                record.notes = _merge_note(
                    record.notes, "usable without appearing in the listing")
        else:
            record.failures += 1
            record.last_failure_at = now
            record.last_error = (error or "")[:200]
        _store_record(record)
    except Exception:  # noqa: BLE001 — statistics are never fatal
        pass


# -- approval -----------------------------------------------------------------

def promote(model: str, *, role: str = ROLE_PRODUCTION, actor: str,
            evidence: dict | None = None, apply: bool = False) -> dict:
    """Record an explicit, reversible approval of one model.

    Refusals are the point of this function, not an afterthought:

    * a model this installation has never successfully called cannot be
      approved, because approval would be a guess;
    * production role requires teacher-certified benchmark evidence, per the
      acceptance criteria — a faster or cheaper model is not a reason;
    * ``apply`` is required to touch the live configuration, so recording a
      decision and changing what marks student work are separate acts.
    """
    model_id = normalise_id(model)
    if role not in ROLES:
        raise PromotionRefused(f"unknown role {role!r}")
    if not model_id:
        raise PromotionRefused("a model id is required")
    if not str(actor or "").strip():
        raise PromotionRefused("an approver is required for an auditable decision")

    record = _record_for(model_id)
    if not record.verified:
        raise PromotionRefused(
            f"{model_id} has never served a successful request from this "
            "installation; discover it and verify access before approving it")

    evidence = dict(evidence or {})
    if role == ROLE_PRODUCTION:
        if not evidence.get("certified"):
            raise PromotionRefused(
                "production approval requires teacher-certified benchmark "
                "evidence; scheme-derived or synthetic results are not enough")
        if not evidence.get("run_ids"):
            raise PromotionRefused(
                "production approval requires the benchmark run ids it relies on")

    from ..config import get_config

    cfg = get_config()
    previous_models = list(cfg.gemini.primary_models or [])
    state = _load()
    previous_promotion = state.get("promotion")

    state["promotion"] = {
        "model": model_id,
        "role": role,
        "actor": str(actor).strip()[:120],
        "at": time.time(),
        "evidence": evidence,
        "previous_models": previous_models,
        "previous_promotion": previous_promotion,
        "applied": bool(apply),
    }
    history = state.setdefault("history", [])
    history.append(dict(state["promotion"]))
    del history[:-20]          # bounded: the last 20 decisions are enough
    _save()

    record.approved_for_production = role == ROLE_PRODUCTION
    record.approved_for_low_risk = role in ROLES
    record.notes = _merge_note(
        record.notes, f"approved for {role} by {str(actor).strip()[:60]}")
    _store_record(record)

    if apply and role == ROLE_PRODUCTION:
        if previous_models != [model_id]:
            cfg.gemini.primary_models = [model_id]
            from ..config import save_config
            save_config(cfg)
        log.warning("production marking model changed to %s by %s",
                    model_id, str(actor).strip()[:60])
    return dict(state["promotion"])


def rollback(*, actor: str) -> dict:
    """Undo the latest promotion, restoring the previous model chain."""
    if not str(actor or "").strip():
        raise PromotionRefused("an approver is required for a rollback")
    state = _load()
    promotion = state.get("promotion")
    if not isinstance(promotion, dict):
        raise PromotionRefused("there is no promotion to roll back")

    previous_models = list(promotion.get("previous_models") or [])
    promoted = promotion.get("model")

    from ..config import get_config, save_config

    cfg = get_config()
    if promotion.get("applied") and previous_models:
        cfg.gemini.primary_models = previous_models
        save_config(cfg)

    if promoted:
        record = _record_for(promoted)
        if previous_models and normalise_id(previous_models[0]) == record.model_id:
            record.notes = _merge_note(record.notes, "promotion rolled back")
        else:
            record.approved_for_production = False
            record.approved_for_low_risk = False
            record.notes = _merge_note(record.notes, f"approval rolled back by {str(actor).strip()[:60]}")
        _store_record(record)

    state["promotion"] = promotion.get("previous_promotion")
    history = state.setdefault("history", [])
    history.append({"action": "rollback", "model": promoted,
                    "actor": str(actor).strip()[:120], "at": time.time(),
                    "restored_models": previous_models})
    del history[:-20]
    _save()
    return {"rolled_back": promoted, "restored_models": previous_models}


# -- reporting ----------------------------------------------------------------

def snapshot() -> dict:
    """Read-only view for the dashboard. No secret, no prompt, no student data."""
    state = _load()
    models = [record.as_dict() for record in records().values()]
    models.sort(key=lambda m: (m["availability"] != "verified",
                               not m["approved_for_production"],
                               m["model_id"]))
    from ..config import get_config

    try:
        configured = list(get_config().gemini.primary_models or [])
    except Exception:  # noqa: BLE001
        configured = []

    return {
        "configured_models": configured,
        "production_model": configured[0] if configured else None,
        "promotion": state.get("promotion"),
        "history": list(state.get("history") or [])[-8:],
        "models": models,
        "verified_count": sum(1 for m in models if m["availability"] == "verified"),
        "discovered_count": sum(1 for m in models if m["discovered"]),
        "updated_at": state.get("updated_at"),
        "notes": [
            "A model appearing in the provider listing does not prove this "
            "project can call it; only a successful request does.",
            "Approval requires teacher-certified evidence and an explicit, "
            "auditable decision. Nothing is promoted automatically.",
        ],
    }
