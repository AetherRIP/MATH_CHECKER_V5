"""Failure classification + retry/failover policy for correction jobs.

The fundamental rule (API efficiency spec §16):

    FAILED API REQUEST ≠ RESEND EVERYTHING IMMEDIATELY.

Every retry must answer: why did it fail, is it retryable, should we wait,
should we switch keys, can we reuse prepared input, and has a successful
result already been created? This module is the single place where those
answers live, so the worker pool and the job manager behave identically.

Design notes
------------
* Classification is pure (message/kind based) and makes NO API calls.
* Each class gets its own budget: some failures never retry (auth,
  invalid request, context too large), some retry once (timeout — the
  request may have reached the provider, so we must not blindly duplicate
  it), some park the job until capacity returns (rate limit, high demand).
* Backoff is exponential with jitter and honours Retry-After hints.
* The correction engine's *model fallback loop* is preserved legacy code
  and is NOT touched here — this policy governs job-level retries (which
  are the expensive ones: they used to re-download and re-upload whole
  submissions).

Failure classes (spec §8):
    TRANSIENT_PROVIDER_ERROR, RATE_LIMIT, HIGH_DEMAND, TIMEOUT,
    NETWORK_ERROR, AUTHENTICATION_ERROR, INVALID_REQUEST,
    CONTEXT_TOO_LARGE, MODEL_ERROR, PARSING_ERROR, VALIDATION_ERROR,
    UNKNOWN_ERROR

Two internal classes exist for orchestration states that are not request
failures: ``API_UNAVAILABLE`` (no usable/leased key right now) and
``WORKER_CRASH`` (child process died). Both are documented in CLASS_DOC.
"""

from __future__ import annotations

import random
import re
import time
from dataclasses import dataclass

# -- taxonomy (spec §8) --------------------------------------------------------

TRANSIENT_PROVIDER_ERROR = "TRANSIENT_PROVIDER_ERROR"
RATE_LIMIT = "RATE_LIMIT"
HIGH_DEMAND = "HIGH_DEMAND"
TIMEOUT = "TIMEOUT"
NETWORK_ERROR = "NETWORK_ERROR"
AUTHENTICATION_ERROR = "AUTHENTICATION_ERROR"
INVALID_REQUEST = "INVALID_REQUEST"
CONTEXT_TOO_LARGE = "CONTEXT_TOO_LARGE"
MODEL_ERROR = "MODEL_ERROR"
PARSING_ERROR = "PARSING_ERROR"
VALIDATION_ERROR = "VALIDATION_ERROR"
UNKNOWN_ERROR = "UNKNOWN_ERROR"

# Internal orchestration states (not provider request failures).
API_UNAVAILABLE = "API_UNAVAILABLE"   # keys cooling down / all leased / none usable
WORKER_CRASH = "WORKER_CRASH"         # child process died before reporting

FAILURE_CLASSES = (
    TRANSIENT_PROVIDER_ERROR, RATE_LIMIT, HIGH_DEMAND, TIMEOUT,
    NETWORK_ERROR, AUTHENTICATION_ERROR, INVALID_REQUEST, CONTEXT_TOO_LARGE,
    MODEL_ERROR, PARSING_ERROR, VALIDATION_ERROR, UNKNOWN_ERROR,
    API_UNAVAILABLE, WORKER_CRASH,
)

CLASS_DOC = {
    API_UNAVAILABLE: "Infrastructure wait: no API key is usable right now "
                     "(cooldown/lease). Park the job; do not consume attempts.",
    WORKER_CRASH: "The worker process died unexpectedly (not a provider "
                  "error). A small bounded retry is allowed.",
}

# -- backoff tuning ------------------------------------------------------------

BACKOFF_BASE_SECONDS = 2.0      # attempt 1 waits ~2s
BACKOFF_GROWTH = 2.0            # then ~4s, ~8s …
BACKOFF_CAP_SECONDS = 30.0      # never hammer; cap the wait
BACKOFF_JITTER = (0.8, 1.2)
MAX_RETRY_AFTER_SECONDS = 900.0  # an absurd Retry-After must not park forever


# -- failure record + policy ---------------------------------------------------

@dataclass(frozen=True)
class Failure:
    """A classified failure plus its retry policy."""
    cls: str
    retryable: bool
    max_retries: int          # job-level retries allowed for this class
    park: bool = False        # wait for capacity instead of burning attempts
    error_code: str = ""      # tag stored on the FAILED job

    @property
    def summary(self) -> str:
        return self.cls


# Per-class policy table. retryable=False means: DO NOT resend the same
# request — the request itself is the problem (auth/config/invalid/context).
# park=True means: capacity problem — wait for cooldown, not attempt budget.
_POLICY: dict[str, tuple[bool, int, bool]] = {
    TRANSIENT_PROVIDER_ERROR: (True, 2, False),
    RATE_LIMIT: (True, 2, True),
    HIGH_DEMAND: (True, 2, True),
    API_UNAVAILABLE: (True, 2, True),
    TIMEOUT: (True, 1, False),          # may have reached provider: 1 only
    NETWORK_ERROR: (True, 1, False),
    AUTHENTICATION_ERROR: (False, 0, False),
    INVALID_REQUEST: (False, 0, False),
    CONTEXT_TOO_LARGE: (False, 0, False),
    MODEL_ERROR: (True, 1, False),
    PARSING_ERROR: (True, 1, False),
    VALIDATION_ERROR: (True, 1, False),  # one controlled retry, NEVER a zero
    UNKNOWN_ERROR: (True, 1, False),
    WORKER_CRASH: (True, 2, False),
}


def failure_for(cls: str) -> Failure:
    """Build a :class:`Failure` for a taxonomy class."""
    if cls not in _POLICY:
        cls = UNKNOWN_ERROR
    retryable, max_retries, park = _POLICY[cls]
    return Failure(cls=cls, retryable=retryable, max_retries=max_retries,
                   park=park, error_code=cls.lower())


# -- message classification ----------------------------------------------------

# Ordered: first match wins. Checked AFTER classify_gemini_error's kind
# mapping (see classify_failure), only when the kind is "unknown".
_NETWORK_PATTERNS = (
    "connection reset", "connection refused", "connection aborted",
    "connection error", "broken pipe", "network error", "name resolution",
    "temporary failure in name resolution", "ssl", "proxy error",
    "unable to connect", "cannot connect", "getaddrinfo",
)
_INVALID_REQUEST_PATTERNS = (
    "invalid argument", "bad request", "malformed request", "malformed",
    "unexpected json", "http 400", " status 400", "400 bad request",
    "invalid input", "invalid value",
)
_CONTEXT_PATTERNS = (
    "context too large", "context window", "context length", "too long",
    "maximum context", "token limit", "token count", "input size exceed",
    "payload too large", "http 413", "413 request",
    "exceeds the maximum number of tokens", "too many tokens",
)
_PARSE_PATTERNS = (
    "expecting value", "unterminated string", "invalid json", "json decode",
    "failed to parse", "parsing error", "not valid json", "unexpected end of json",
)
_VALIDATION_PATTERNS = (
    "schema", "validation failed", "did not match", "response validation",
    "format validation", "missing required",
)
_HIGH_DEMAND_PATTERNS = (
    "503", "high demand", "overloaded", "service unavailable",
    "server is currently overloaded", "temporarily unavailable",
)
_TRANSIENT_PATTERNS = (
    "500 internal", "internal server error", "502 bad gateway",
    "bad gateway", "504", "gateway timeout", "http 500", "http 502",
    "http 504", "backend error",
)
_RATE_LIMIT_PATTERNS = (
    "429", "rate limit", "rate exceeded", "quota exceeded", "resource exhausted",
    "too many requests",
)
_AUTH_PATTERNS = (
    "401", "403", "unauthenticated", "unauthorized", "api key not valid",
    "invalid api key", "permission denied", "permission", "forbidden",
    "api key is invalid", "leaked", "unauthenticated",
)
_TIMEOUT_PATTERNS = (
    "timed out", "timeout", "deadline exceeded", "read timed out",
    "request timeout",
)

# Deterministic staging/config problems: retrying cannot help until the user
# corrects the input (spec: INVALID_REQUEST → no retry until corrected).
_STAGING_PATTERNS = (
    "no uploaded submission file",
    "no mark scheme has been uploaded",
    "no mark scheme uploaded",
    "no google drive attachment",
    "unsupported submitted file type",
)


def _contains(message: str, patterns: tuple[str, ...]) -> bool:
    return any(p in message for p in patterns)


def classify_message(message: str) -> str | None:
    """Map a raw error message onto the taxonomy (None = no signal).

    Only message-based signals live here; this runs after the legacy kind
    classifier, so its established checks (leaked key, 429, timeout, 503…)
    still take precedence via :func:`classify_kind`.
    """
    m = (message or "").lower()
    if not m:
        return None
    # Config/auth problems that parking can never fix — fail fast.
    if ("no gemini api keys are configured" in m
            or "no usable gemini keys" in m):
        return AUTHENTICATION_ERROR
    # Capacity waits (raised BEFORE any request is sent — nothing was sent).
    if "cooling down" in m or "currently leased" in m:
        return API_UNAVAILABLE
    if _contains(m, _STAGING_PATTERNS):
        return INVALID_REQUEST
    if _contains(m, _NETWORK_PATTERNS):
        return NETWORK_ERROR
    if _contains(m, _CONTEXT_PATTERNS):
        return CONTEXT_TOO_LARGE
    if _contains(m, _PARSE_PATTERNS):
        return PARSING_ERROR
    if _contains(m, _VALIDATION_PATTERNS):
        return VALIDATION_ERROR
    if _contains(m, _INVALID_REQUEST_PATTERNS):
        return INVALID_REQUEST
    if _contains(m, _HIGH_DEMAND_PATTERNS):
        return HIGH_DEMAND
    if _contains(m, _TRANSIENT_PATTERNS):
        return TRANSIENT_PROVIDER_ERROR
    if _contains(m, _RATE_LIMIT_PATTERNS):
        return RATE_LIMIT
    if _contains(m, _AUTH_PATTERNS):
        return AUTHENTICATION_ERROR
    if _contains(m, _TIMEOUT_PATTERNS):
        return TIMEOUT
    return None


#: A joined failure across every model and key. It says nothing about the
#: credential this job happened to lease, so it can never be a verdict on it.
_AGGREGATE_MARKER = "all configured gemini models failed"


def classify_kind(kind: str | None, message: str = "") -> str:
    """Map a ``classify_gemini_error`` kind (child worker payload) to a class."""
    kind = kind or "unknown"
    m = (message or "").lower()
    if kind == "invalid_key" or kind == "forbidden":
        # AUTHENTICATION_ERROR is permanent by policy (retryable=False), so it
        # must only ever be won by a verdict on ONE credential. An aggregate
        # "all configured models failed" that carries a 429 from one key was
        # recorded as an auth failure and ended the job on the spot, while
        # every other key was merely exhausted — a transient quota storm
        # turned into "this correction can never run". Capacity dominates.
        if _AGGREGATE_MARKER in m:
            return classify_message(message) or RATE_LIMIT
        return AUTHENTICATION_ERROR
    if kind == "rate_limited":
        return RATE_LIMIT
    if kind == "timeout":
        return TIMEOUT
    if kind == "temporary":
        if _contains(m, _HIGH_DEMAND_PATTERNS) or "unavailable" in m:
            return HIGH_DEMAND
        return TRANSIENT_PROVIDER_ERROR
    if kind == "model_no_access":
        return MODEL_ERROR
    # Kinds whose behaviour is already decided by name, so a short message that
    # does not repeat the provider's wording still lands in the right class.
    if kind == "network":
        return NETWORK_ERROR
    if kind == "request_error":
        return INVALID_REQUEST
    if kind == "unexpected_response":
        # The message may name the failure precisely (an unparseable body is a
        # PARSING_ERROR); only fall back to VALIDATION_ERROR when it does not.
        return classify_message(message) or VALIDATION_ERROR
    # unknown / anything new: full message classification.
    return classify_message(message) or classify_kind_kind_fallback(kind)


def classify_kind_kind_fallback(kind: str) -> str:
    """Last resort for kinds we do not know by name."""
    if kind == "worker_crash":
        return WORKER_CRASH
    if kind in FAILURE_CLASSES:
        return kind
    return UNKNOWN_ERROR


def classify_failure(exc: Exception) -> Failure:
    """Classify a raw exception (job manager / dispatch path)."""
    from .errors import classify_gemini_error
    message = str(exc)
    kind = classify_gemini_error(exc).get("kind", "unknown")
    cls = classify_kind(kind, message)
    return failure_for(cls)


def failure_for_kind(kind: str | None, message: str = "") -> Failure:
    """Classify from a worker result's ``error_kind`` + error message."""
    return failure_for(classify_kind(kind, message))


# -- backoff -------------------------------------------------------------------

_RETRY_AFTER_PATTERNS = (
    re.compile(r"retry[-_ ]after\s*[:=]?\s*(\d+)", re.I),
    re.compile(r"retry in\s+(\d+)\s*s", re.I),
    re.compile(r"retry in (\d+) seconds", re.I),
    re.compile(r"retry_delay\s*\{\s*seconds:\s*(\d+)", re.I),
    re.compile(r"please retry in (\d+)", re.I),
    re.compile(r"available in (\d+)\s*s", re.I),
)


def parse_retry_after(message: str | None) -> float | None:
    """Extract a provider Retry-After hint (seconds) from an error message."""
    if not message:
        return None
    for pattern in _RETRY_AFTER_PATTERNS:
        match = pattern.search(message)
        if match:
            try:
                seconds = float(match.group(1))
            except (TypeError, ValueError):
                continue
            if 0 < seconds <= MAX_RETRY_AFTER_SECONDS:
                return seconds
    return None


def compute_backoff(retry_index: int, *, retry_after: float | None = None,
                    base: float = BACKOFF_BASE_SECONDS,
                    growth: float = BACKOFF_GROWTH,
                    cap: float = BACKOFF_CAP_SECONDS) -> float:
    """Exponential backoff with jitter (spec §3).

    ``retry_index`` is 1-based: the first retry waits ~2s, the second ~4s,
    the third ~8s — always jittered so a burst of jobs does not resume in
    lockstep, always capped so a wait never becomes a stall. A provider
    ``Retry-After`` hint always wins when it is larger.
    """
    retry_index = max(1, int(retry_index))
    delay = min(cap, base * (growth ** (retry_index - 1)))
    delay *= random.uniform(*BACKOFF_JITTER)
    if retry_after:
        delay = max(delay, min(float(retry_after), MAX_RETRY_AFTER_SECONDS))
    return max(0.5, delay)


# -- the decision --------------------------------------------------------------

ACTION_RETRY = "retry"
ACTION_PARK = "park"
ACTION_FAIL = "fail"

_WAIT_BUDGET_SECONDS = 15 * 60  # total parked time per job (matches JobManager)

#: Minimum wait before re-running a job the provider shed with "high demand".
#:
#: Measured consequence of not having this: the recorded correction jobs show
#: four full attempts, each diagnosed HIGH_DEMAND and re-run about 2 s apart,
#: because the circuit breaker had no cooldown recorded yet when the parent
#: asked. A 503 "high demand" is not resolved in two seconds, so retrying that
#: fast spends the job's attempt budget on a provider that is already shedding
#: work. A minute still fits well inside the 15-minute parking budget, and a
#: longer provider cooldown always wins over it.
HIGH_DEMAND_MIN_PARK_SECONDS = 60.0


@dataclass(frozen=True)
class Decision:
    action: str              # retry | park | fail
    delay: float             # seconds before the next action
    error_code: str | None   # set when action == fail
    reason: str              # human-readable justification (for logs/UI)
    allowed_retries: int = 0
    retries_used: int = 0


def decide(failure: Failure, *, attempts: int, max_attempts: int,
           cfg_max_retries: int, waited_seconds: float = 0.0,
           wait_budget: float = _WAIT_BUDGET_SECONDS,
           park_hint_delay: float | None = None,
           retry_index: int | None = None) -> Decision:
    """The one question every retry must answer (spec §16).

    ``attempts`` = number of runs already started (claims). ``retries_used``
    = attempts - 1. Parks (capacity waits) do NOT consume the attempt budget
    — they are bounded instead by ``wait_budget`` seconds of total parking.
    """
    retries_used = max(0, attempts - 1)
    allowed = max(0, min(int(cfg_max_retries), failure.max_retries))

    if failure.park:
        delay = float(park_hint_delay or 0.0)
        if delay <= 0:
            delay = compute_backoff(max(1, retries_used + 1))
        if failure.cls == HIGH_DEMAND:
            delay = max(delay, HIGH_DEMAND_MIN_PARK_SECONDS)
        if waited_seconds + delay > wait_budget:
            return Decision(
                action=ACTION_FAIL, delay=0.0,
                error_code="all_keys_unavailable",
                reason=(f"{failure.cls}: no capacity within the "
                        f"{int(wait_budget)}s waiting budget"),
                allowed_retries=allowed, retries_used=retries_used)
        return Decision(action=ACTION_PARK, delay=delay, error_code=None,
                        reason=f"{failure.cls}: waiting for API capacity "
                               f"(attempt budget untouched)",
                        allowed_retries=allowed, retries_used=retries_used)

    if not failure.retryable or allowed <= 0:
        return Decision(action=ACTION_FAIL, delay=0.0,
                        error_code=failure.error_code,
                        reason=f"{failure.cls}: not retryable — resending the "
                               f"identical request would waste quota",
                        allowed_retries=allowed, retries_used=retries_used)

    if retries_used >= allowed or attempts >= max_attempts:
        return Decision(action=ACTION_FAIL, delay=0.0,
                        error_code=failure.error_code,
                        reason=(f"{failure.cls}: retry budget exhausted "
                                f"({retries_used}/{allowed} used, "
                                f"attempt {attempts}/{max_attempts})"),
                        allowed_retries=allowed, retries_used=retries_used)

    delay = compute_backoff(retry_index if retry_index else retries_used + 1)
    return Decision(action=ACTION_RETRY, delay=delay, error_code=None,
                    reason=f"{failure.cls}: retryable — reusing prepared "
                           f"input ({retries_used}/{allowed} retries used)",
                    allowed_retries=allowed, retries_used=retries_used)


def now() -> float:
    return time.time()
