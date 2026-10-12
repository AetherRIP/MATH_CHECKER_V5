"""Staged per-key testing for the 'Test All API Keys' feature.

API HEALTH TEST ONLY — never the correction pipeline (spec: keep health
checks cheap, deterministic and student-data-free):

* no PDF uploads, no student submissions, no mark schemes,
* no Google Classroom calls, no correction engine,
* one tiny deterministic prompt per key whose response is VERIFIED
  ("Return exactly: API_TEST_OK"), so the test checks that the key can
  actually reach the model, not merely that some HTTP call returned.

A passing test means exactly: "this credential can communicate with the
configured Gemini model" — nothing about marking accuracy. Correction
benchmarks are a separate, explicitly developer-triggered flow.

Yields live events so the UI can show a progress wizard, never exposing key
material — every event refers to keys by index + fingerprint only.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

from ..config import get_config
from ..errors import classify_gemini_error
from ..logging_setup import log_event
from ..net_usage import GENERATION_REQUEST_OVERHEAD_BYTES, record
from .key_manager import fingerprint

log = logging.getLogger("gemini.tester")

# Tiny deterministic health-check prompt + expected response (kept well
# under any sensible network budget: a few dozen bytes each way).
HEALTH_PROBE_PROMPT = "Return exactly: API_TEST_OK"
HEALTH_PROBE_EXPECTED = "API_TEST_OK"


@dataclass
class KeyTestState:
    index: int
    fingerprint: str
    stage: str = "queued"          # queued connecting auth generate done skipped
    auth_ok: bool | None = None
    generate_ok: bool | None = None
    response_time_ms: int | None = None
    error_kind: str | None = None
    error_message: str | None = None
    status: str = "PENDING"        # PENDING TESTING HEALTHY COOLDOWN INVALID SKIPPED
    events: list[dict] = field(default_factory=list)


def _real_client(key: str):
    from google.genai import types
    import google.genai as genai
    cfg = get_config().gemini.request_timeout_ms
    return genai.Client(api_key=key, http_options=types.HttpOptions(timeout=min(cfg, 30_000)))


def _stage_event(state: KeyTestState, stage: str, message: str) -> dict:
    state.stage = stage
    event = {"stage": stage, "message": message}
    state.events.append(event)
    return event


def test_key(index: int, key: str, fp: str) -> KeyTestState:
    """Run the staged test for one key. Network-light: models.list + one tiny
    generation on a cheap model."""
    state = KeyTestState(index=index, fingerprint=fp)
    if not key:
        state.status = "SKIPPED"
        _stage_event(state, "done", "No key material configured — skipped.")
        return state

    if get_config().mock_mode:
        state.auth_ok = True
        state.generate_ok = True
        state.response_time_ms = 42
        state.status = "HEALTHY"
        _stage_event(state, "auth", "Authentication: PASS (mock mode)")
        _stage_event(state, "generate", "Gemini request: PASS (mock mode, 42 ms)")
        _stage_event(state, "done", "Status: HEALTHY (simulated — mock mode)")
        return state

    _stage_event(state, "connecting", "Connecting…")
    try:
        client = _real_client(key)
    except Exception as exc:
        state.auth_ok = False
        state.error_kind = "invalid_key"
        state.status = "INVALID"
        _stage_event(state, "done", f"Could not create client: {exc}")
        return state

    # Stage 1: authentication via the lightweight models list (legacy parity:
    # the friend's app used the same call to validate keys without spending
    # generation tokens).
    _stage_event(state, "auth", "Checking authentication (models.list)…")
    try:
        models = list(client.models.list())
        state.auth_ok = True
        record("gemini_health", 1_000, 4_000)  # tiny fixed estimate for models.list
        _stage_event(state, "auth", f"Authentication: PASS ({len(models)} models visible)")
    except Exception as exc:
        kind = classify_gemini_error(exc)
        state.auth_ok = False
        state.error_kind = kind["kind"]
        state.error_message = kind["message"]
        state.status = "INVALID" if kind["kind"] == "invalid_key" else "COOLDOWN"
        _stage_event(state, "done", f"Authentication: FAIL — {kind['message']}")
        _record(fp, False, kind["kind"], kind["message"])
        return state

    # Stage 2: one minimal, deterministic generation on the last model of the
    # configured fallback chain (cheapest; the legacy chain is the authority
    # on which models are live). The response content is verified.
    probe_model = (get_config().gemini.primary_models[-1] or "gemini-3.5-flash-lite")
    _stage_event(state, "generate", f"Running minimal health probe ({probe_model})…")
    started = time.time()
    try:
        response = client.models.generate_content(
            model=probe_model,
            contents=HEALTH_PROBE_PROMPT,
        )
        elapsed_ms = int((time.time() - started) * 1000)
        text = (getattr(response, "text", "") or "").strip()
        record("gemini_health", GENERATION_REQUEST_OVERHEAD_BYTES, len(text))
        if HEALTH_PROBE_EXPECTED not in text:
            state.generate_ok = False
            state.error_kind = "unexpected_response"
            state.error_message = (
                "Model responded but did not return the expected probe reply "
                "— the credential reaches Gemini, but the model output was "
                f'unexpected (len {len(text)} chars).')
            state.status = "COOLDOWN"  # not a bad key; retry later
            _stage_event(state, "done", f"Gemini request: UNEXPECTED RESPONSE ({elapsed_ms} ms)")
            _record(fp, False, "unexpected_response", state.error_message, elapsed_ms)
        else:
            state.generate_ok = True
            state.response_time_ms = elapsed_ms
            state.status = "HEALTHY"
            _stage_event(state, "done", f"Gemini request: PASS ({elapsed_ms} ms)")
            _record(fp, True, "healthy", "ok", elapsed_ms)
    except Exception as exc:
        kind = classify_gemini_error(exc)
        state.generate_ok = False
        state.error_kind = kind["kind"]
        state.error_message = kind["message"]
        state.status = "INVALID" if kind["kind"] == "invalid_key" else "COOLDOWN"
        record("gemini_health", GENERATION_REQUEST_OVERHEAD_BYTES, 512)
        _stage_event(state, "done", f"Gemini request: FAIL — {kind['message']}")
        _record(fp, False, kind["kind"], kind["message"])

    _stage_event(state, "done", f"Status: {state.status}")
    return state


def _record(fp: str, ok: bool, kind: str, message: str, response_time: float | None = None) -> None:
    from .key_manager import KeyManager
    KeyManagerHolder.instance().register_manual_result(
        fp, ok=ok, kind=kind, message=message, response_time=response_time
    )


class KeyManagerHolder:
    """Single shared KeyManager instance (the friend's app used
    st.cache_resource for the same purpose; V5 uses one module singleton)."""

    import threading as _threading
    _instance: "KeyManager | None" = None
    _lock = _threading.Lock()

    @classmethod
    def instance(cls) -> "KeyManager":
        with cls._lock:
            if cls._instance is None:
                from .key_manager import KeyManager as _KM
                cls._instance = _KM()
            return cls._instance
