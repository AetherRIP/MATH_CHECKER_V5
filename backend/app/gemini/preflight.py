"""Provider readiness check BEFORE a batch spends real work.

A key existing is not evidence that inference works: a key can be present and
still be rejected, out of quota, pointed at a model that does not exist, or
talking to a provider that is refusing every request. Discovering that one
whole correction at a time costs a Drive download, a PDF upload (megabytes of
student work) and a full marking prompt per paper.

This module answers the readiness question with ONE tiny, student-data-free
request:

1. configuration is validated (keys configured, model identifier non-empty);
2. the configured PRIMARY model is the one exercised, so a probe cannot pass
   on a fallback model while the model the batch will actually use is broken;
3. the response must be a real provider response (at least one candidate);
4. the failure is classified into an actionable state — invalid credential,
   quota, rate limit, provider outage, missing model, network, timeout — not
   collapsed into "error";
5. the verdict is cached for :data:`CACHE_SECONDS` so a batch start costs one
   probe, not one probe per paper, with an explicit recheck available;
6. nothing is uploaded and no student PDF is opened.

The probe is deliberately READ-ONLY with respect to key health: it reports
what the provider said but does not bench a key or write cooldowns (the real
request path already does that with per-key evidence). A health check must
never be the reason a working key is removed.

States
------
READY, NOT_CHECKED (no recent verdict), CHECKING (a probe is running),
INVALID_CREDENTIALS, QUOTA_EXHAUSTED, RATE_LIMITED, MODEL_UNAVAILABLE,
PROVIDER_UNAVAILABLE, NETWORK_ERROR, TIMEOUT, MALFORMED_RESPONSE,
NOT_CONFIGURED, ERROR.
"""

from __future__ import annotations

import json
import logging
import os
import time
from typing import Any

from ..config import DEFAULT_PRIMARY_MODELS, get_config, get_secrets
from ..errors import classify_gemini_error
from ..logging_setup import log_event
from .key_manager import fingerprint

log = logging.getLogger("gemini.preflight")

#: Where the last verdict lives. One small JSON row, like the provider gate.
META_KEY = "gemini_preflight"

#: A verdict this fresh is reused instead of spending another request.
CACHE_SECONDS = 300.0

#: Hard ceiling for one probe. It must be short: this runs while a batch waits.
PROBE_TIMEOUT_MS = 15_000

#: The probe asks for the same shape the app's tester uses. The reply text is
#: not required (a thinking model can spend the whole tiny output budget on its
#: reasoning and return empty text); a candidate is required.
PROBE_PROMPT = "Reply with the single word: OK"
PROBE_MAX_OUTPUT_TOKENS = 16

READY = "READY"
NOT_CHECKED = "NOT_CHECKED"
CHECKING = "CHECKING"
INVALID_CREDENTIALS = "INVALID_CREDENTIALS"
QUOTA_EXHAUSTED = "QUOTA_EXHAUSTED"
RATE_LIMITED = "RATE_LIMITED"
MODEL_UNAVAILABLE = "MODEL_UNAVAILABLE"
PROVIDER_UNAVAILABLE = "PROVIDER_UNAVAILABLE"
NETWORK_ERROR = "NETWORK_ERROR"
TIMEOUT = "TIMEOUT"
MALFORMED_RESPONSE = "MALFORMED_RESPONSE"
NOT_CONFIGURED = "NOT_CONFIGURED"
ERROR = "ERROR"

#: States that mean "do not start an expensive batch".
BLOCKING_STATES = frozenset({
    INVALID_CREDENTIALS, QUOTA_EXHAUSTED, RATE_LIMITED, MODEL_UNAVAILABLE,
    PROVIDER_UNAVAILABLE, NETWORK_ERROR, TIMEOUT, NOT_CONFIGURED,
})

#: States that mean "inference answered". MALFORMED_RESPONSE is NOT ready: a
#: provider that returns no candidate cannot be trusted to mark a paper.
READY_STATES = frozenset({READY})

_STATE_DETAIL = {
    READY: "The configured model answered a minimal request.",
    NOT_CHECKED: "No provider health check has run recently.",
    CHECKING: "A provider health check is running.",
    INVALID_CREDENTIALS: "Gemini rejected the credential the check used.",
    QUOTA_EXHAUSTED: "The key's project is out of Gemini quota for now.",
    RATE_LIMITED: "Gemini is refusing the current request rate.",
    MODEL_UNAVAILABLE: "The configured model is not available to this key.",
    PROVIDER_UNAVAILABLE: "Gemini is temporarily overloaded.",
    NETWORK_ERROR: "Gemini could not be reached from this machine.",
    TIMEOUT: "The provider health check timed out.",
    MALFORMED_RESPONSE: "Gemini answered without any usable candidate.",
    NOT_CONFIGURED: "No Gemini API key is configured.",
    ERROR: "The provider health check failed for an unclassified reason.",
}


def disabled() -> bool:
    """Is the batch preflight switched off for this process?

    Set ``MC5_DISABLE_PREFLIGHT=1`` in hermetic test runs (and for a teacher who
    wants batches to start without the extra tiny request).
    """
    return os.environ.get("MC5_DISABLE_PREFLIGHT", "").strip().lower() in (
        "1", "true", "yes", "on")


def _cache(max_age: float | None = None) -> dict | None:
    try:
        from .. import db
        raw = db.get_meta(META_KEY)
        if not raw:
            return None
        data = json.loads(raw)
    except Exception:  # noqa: BLE001 - a cache read never fails a batch
        return None
    if not isinstance(data, dict) or data.get("state") not in _STATE_DETAIL:
        return None
    if max_age is not None:
        try:
            age = time.time() - float(data.get("checked_at") or 0.0)
        except (TypeError, ValueError):
            return None
        if age > max_age:
            return None
    return data


def _store(state: str, *, detail: str = "", model: str | None = None,
           fp: str | None = None, duration_ms: int | None = None,
           http_status: int | None = None, simulated: bool = False,
           **extra: Any) -> dict:
    payload = {
        "state": state,
        "detail": detail or _STATE_DETAIL.get(state, ""),
        "model": model,
        "key_fingerprint": fp,
        "duration_ms": duration_ms,
        "http_status": http_status,
        "simulated": bool(simulated),
        "checked_at": time.time(),
        "cache_seconds": CACHE_SECONDS,
        **extra,
    }
    try:
        from .. import db
        db.set_meta(META_KEY, json.dumps(payload, ensure_ascii=False))
    except Exception:  # noqa: BLE001 - the verdict is still returned
        log.debug("provider preflight verdict not cached", exc_info=True)
    return payload


def cached_state(max_age: float | None = CACHE_SECONDS) -> dict | None:
    """The last verdict if it is still fresh, else ``None`` (no network call)."""
    return _cache(max_age=max_age)


def snapshot(*, recheck: bool = False, max_age: float | None = CACHE_SECONDS
             ) -> dict:
    """Return a verdict. Only ``recheck=True`` performs a provider request."""
    if not recheck:
        cached = _cache(max_age=max_age)
        if cached is not None:
            cached["cached"] = True
            return cached
        return {
            "state": NOT_CHECKED, "detail": _STATE_DETAIL[NOT_CHECKED],
            "checked_at": None, "cached": False,
            "model": _configured_model(), "cache_seconds": CACHE_SECONDS,
        }
    return probe()


def _configured_model() -> str | None:
    try:
        chain = list(get_config().gemini.primary_models) or list(
            DEFAULT_PRIMARY_MODELS)
        return chain[0] if chain else None
    except Exception:  # noqa: BLE001
        return None


def _pick_key() -> tuple[str | None, str | None]:
    """(key material, fingerprint) for a key that is not already benched.

    Prefers a key the manager reports HEALTHY; falls back to the first configured
    key when every key is cooling (the probe can still tell a quota problem from
    a dead credential). Returns ``(None, None)`` when nothing is configured.
    """
    try:
        keys = list(get_secrets().gemini_keys())
    except Exception:  # noqa: BLE001
        keys = []
    if not keys:
        return None, None
    try:
        from .tester import KeyManagerHolder
        status = KeyManagerHolder.instance().status()
        healthy_fps = {row["fingerprint"] for row in status.get("keys") or []
                       if row.get("status") == "HEALTHY"}
        for key in keys:
            if fingerprint(key) in healthy_fps:
                return key, fingerprint(key)
    except Exception:  # noqa: BLE001 - selection is an optimisation, not a rule
        pass
    return keys[0], fingerprint(keys[0])


def _classify(exc: Exception) -> tuple[str, str, int | None]:
    """(state, human explanation, http status) for a failed probe."""
    info = classify_gemini_error(exc)
    kind = info.get("kind") or "unknown"
    message = info.get("message") or str(exc)[:300]
    status = info.get("status")
    lowered = str(exc).lower()

    if kind == "invalid_key":
        return INVALID_CREDENTIALS, message, status
    if kind == "billing_required" or kind == "forbidden":
        return QUOTA_EXHAUSTED, message, status
    if kind == "model_no_access":
        return MODEL_UNAVAILABLE, message, status
    if kind == "rate_limited":
        scope = str(info.get("quota_scope") or "")
        if scope == "daily":
            return QUOTA_EXHAUSTED, message, status
        return RATE_LIMITED, message, status
    if kind == "temporary":
        return PROVIDER_UNAVAILABLE, message, status
    if kind == "timeout":
        return TIMEOUT, message, status
    if kind == "network":
        return NETWORK_ERROR, message, status
    if kind == "request_error":
        # A 404/NOT_FOUND on a model identifier is a configuration problem the
        # teacher can fix; any other rejected request is not a capacity wait.
        if "404" in lowered or "not found" in lowered or "model" in lowered:
            return MODEL_UNAVAILABLE, message, status
        return ERROR, message, status
    return ERROR, message, status


def probe() -> dict:
    """Perform ONE minimal inference request and cache the verdict."""
    cfg = get_config()
    model = _configured_model()
    if cfg.mock_mode:
        # Demo mode never contacts Gemini; saying READY here is honest because
        # the correction path it will use genuinely does not need the provider.
        return _store(READY, detail="Demo mode — no provider request is used.",
                      model=model, simulated=True, duration_ms=0)

    key, fp = _pick_key()
    if not key:
        return _store(NOT_CONFIGURED, model=model, fp=None, duration_ms=0)

    started = time.time()
    try:
        from google.genai import types
        import google.genai as genai
        timeout = min(int(cfg.gemini.request_timeout_ms or PROBE_TIMEOUT_MS),
                      PROBE_TIMEOUT_MS)
        client = genai.Client(
            api_key=key,
            http_options=types.HttpOptions(timeout=timeout))
    except Exception as exc:  # noqa: BLE001 - local SDK/config failure
        duration = int((time.time() - started) * 1000)
        log_event("gemini.preflight", "could not build the probe client",
                  level=logging.WARNING, error=type(exc).__name__,
                  duration_ms=duration)
        return _store(ERROR,
                      detail=("Could not prepare the Gemini client on this "
                              f"machine ({type(exc).__name__}). The provider "
                              "was not tested."),
                      model=model, fp=fp, duration_ms=duration)

    try:
        kwargs: dict[str, Any] = {}
        try:
            kwargs["config"] = types.GenerateContentConfig(
                max_output_tokens=PROBE_MAX_OUTPUT_TOKENS, temperature=0)
        except Exception:  # noqa: BLE001 - older SDK: the minimal call is enough
            kwargs = {}
        response = client.models.generate_content(
            model=model, contents=PROBE_PROMPT, **kwargs)
        duration = int((time.time() - started) * 1000)
        candidates = list(getattr(response, "candidates", None) or [])
        text = (getattr(response, "text", "") or "").strip()
        try:
            from ..net_usage import record
            record("gemini_health", 1_000, len(text))
        except Exception:  # noqa: BLE001
            pass
        if not candidates:
            return _store(
                MALFORMED_RESPONSE,
                detail=("Gemini answered HTTP 200 but returned no candidate. "
                        "The provider is reachable; the response is not usable "
                        "for marking."),
                model=model, fp=fp, duration_ms=duration)
        log_event("gemini.preflight", "provider ready", model=model,
                  fingerprint=fp, duration_ms=duration,
                  response_chars=len(text))
        return _store(READY, model=model, fp=fp, duration_ms=duration,
                      response_chars=len(text))
    except Exception as exc:  # noqa: BLE001 - classified below
        duration = int((time.time() - started) * 1000)
        state, detail, status = _classify(exc)
        try:
            from ..net_usage import record
            record("gemini_health", 1_000, 512)
        except Exception:  # noqa: BLE001
            pass
        log_event("gemini.preflight", "provider not ready",
                  level=logging.WARNING, state=state, model=model,
                  fingerprint=fp, http_status=status, duration_ms=duration,
                  error=str(exc)[:200])
        return _store(state, detail=detail, model=model, fp=fp,
                      duration_ms=duration, http_status=status)


def describe(state: dict | None) -> dict:
    """Public, secret-free shape for the UI."""
    state = state or {}
    code = str(state.get("state") or NOT_CHECKED)
    return {
        "state": code,
        "ready": code in READY_STATES,
        "blocking": code in BLOCKING_STATES,
        "detail": state.get("detail") or _STATE_DETAIL.get(code, ""),
        "model": state.get("model"),
        "checked_at": state.get("checked_at"),
        "age_seconds": (round(time.time() - float(state["checked_at"]), 1)
                        if state.get("checked_at") else None),
        "duration_ms": state.get("duration_ms"),
        "http_status": state.get("http_status"),
        "cache_seconds": CACHE_SECONDS,
        "simulated": bool(state.get("simulated")),
        "cached": bool(state.get("cached")),
    }
