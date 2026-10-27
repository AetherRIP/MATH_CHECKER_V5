"""Error envelope and human-friendly error classification.

The user-facing wording follows the legacy app's guidance (401 → re-auth,
429 → key cooled down, etc.) per V5 rule: reuse what works. Traces are only
shown on the Developer diagnostics surface.
"""

from __future__ import annotations

import re
import traceback

from .logging_setup import log_event


class AppError(Exception):
    """An error with a user-facing message plus optional technical detail."""

    def __init__(self, message: str, *, detail: str | None = None,
                 code: str = "internal_error", status: int = 500):
        super().__init__(message)
        self.user_message = message
        self.detail = detail
        self.code = code
        self.status = status

    def to_dict(self, *, include_detail: bool = False) -> dict:
        payload = {"code": self.code, "message": self.user_message}
        if include_detail and self.detail:
            payload["detail"] = self.detail
        return payload


#: Phrases the provider uses ONLY when the CREDENTIAL itself is the problem.
#: A key is struck off the rotation only on evidence like this — never because
#: some other error message happened to contain the digits "401".
_CREDENTIAL_REJECTION_PHRASES = (
    "reported as leaked",
    "api key not valid",
    "api key is invalid",
    "api_key_invalid",
    "invalid api key",
    "api key expired",
    "api key has been",
    "invalid credentials",
    "unauthenticated",
    "authentication failed",
    "requires authentication",
    "invalid authentication",
)

#: "401 Unauthorized" / "401 UNAUTHENTICATED": an auth STATUS WORD next to the
#: 401 status is evidence; a bare "401" inside a payload is not.
_AUTH_STATUS_RE = re.compile(
    r"\b401\b[^\n]{0,24}?\b(?:unauthorized|unauthenticated)\b", re.I)

_FORBIDDEN_RE = re.compile(
    r"\b403\b|\bpermission[ _]?denied\b|\bforbidden\b|\bpermission\b", re.I)

#: "This model does not exist / is not available to this project". Must be
#: checked BEFORE the quota branch: Google reports "no access to this model"
#: with the word *quota* and ``limit: 0`` in the same payload, and the quota
#: branch would otherwise swallow it and park the job as if the KEY were
#: rate-limited.
_MODEL_ABSENT_RE = re.compile(
    r"limit\s*:\s*0|model_no_access|"
    r"\bmodel\b[^\n]{0,80}?\b(?:not found|not_found|does not exist|"
    r"is not supported|does not support|decommissioned|deprecated)\b", re.I)

_QUOTA_RE = re.compile(
    r"\b429\b|\bquota\b|resource exhausted|rate limit|too many requests", re.I)

#: Google's 504 spells the status ``DEADLINE_EXCEEDED`` and its payload says
#: "Deadline expired before operation could complete." Both spellings, and the
#: timeout words, must land in ``timeout``: as ``unknown`` the job policy
#: allowed TWO retries — three full marking passes for a request the provider
#: had already answered with "this will not finish".
_TIMEOUT_RE = re.compile(
    r"timed out|timeout|deadline[ _]?exceeded|deadline expired|took too long",
    re.I)

_TEMPORARY_RE = re.compile(
    r"\b503\b|unavailable|overloaded|high demand|try again later|temporarily",
    re.I)

_NETWORK_RE = re.compile(
    r"connection reset|connection refused|connection aborted|connection error|"
    r"connection closed|broken pipe|network error|name resolution|getaddrinfo|"
    r"unable to connect|cannot connect|ssl|proxy error|server disconnected|"
    r"remote end closed", re.I)

_REQUEST_ERROR_RE = re.compile(
    r"invalid argument|bad request|malformed|invalid request|invalid value|"
    r"unsupported api version|api version not supported|unsupported.{0,20}parameter|"
    r"unknown name|http 400|\bstatus 400\b|\b400 bad request\b", re.I)

_UNEXPECTED_RESPONSE_RE = re.compile(
    r"expecting value|unterminated string|invalid json|json decode|failed to parse|"
    r"parsing error|not valid json|unexpected end of json|empty response|"
    r"returned no json|unexpected response|schema|validation failed|did not match|"
    r"missing required", re.I)

#: The marking engine's multi-cause aggregate. It JOINS one failure per model
#: (and, through key rotation, per key) with " | ", so it is not a verdict
#: about any single credential and must never be read as one: classified as a
#: credential rejection it permanently invalidated whichever key happened to be
#: leased when the aggregate was recorded.
_AGGREGATE_MARKERS = ("all configured gemini models failed",)

#: The provider's own HTTP status, read ONLY from a place a status actually
#: lives: Google's JSON error envelope (``'code': 429``), an ``HTTP 503``
#: prefix, or a status name after the number (``429 RESOURCE_EXHAUSTED``). A
#: bare three-digit number in prose is NOT a status — reporting one would put a
#: confident-looking lie in the diagnostics a person uses to decide whether a
#: credential is dead.
_STATUS_IN_ENVELOPE_RE = re.compile(
    r"(?:code|status)['\"]?\s*[:=]\s*(\d{3})", re.I)
_STATUS_AFTER_HTTP_RE = re.compile(r"\bhttp[/ ]?(\d{3})\b", re.I)
_STATUS_WITH_NAME_RE = re.compile(r"\b(\d{3})\b\s+[A-Z][A-Z_]{2,}\b")


def http_status_of(exc_or_message: object) -> int | None:
    """The provider HTTP status named in an error, or ``None``.

    Diagnostics only: the value is logged next to the error category
    (``401`` vs ``429`` vs ``503`` is the difference between "this credential
    is dead" and "the provider is busy"), and it never carries request
    payloads or credential material.
    """
    text = str(exc_or_message or "")
    for pattern in (_STATUS_IN_ENVELOPE_RE, _STATUS_AFTER_HTTP_RE):
        found = pattern.search(text)
        if found:
            return int(found.group(1))
    found = _STATUS_WITH_NAME_RE.search(text)
    if found:
        return int(found.group(1))
    return None


def _credential_rejection(lowered: str) -> bool:
    """True only for an explicit "your credential is not accepted" verdict."""
    if any(phrase in lowered for phrase in _CREDENTIAL_REJECTION_PHRASES):
        return True
    return bool(_AUTH_STATUS_RE.search(lowered))


def _classify_aggregate(aggregate: str, message: str) -> dict:
    """Classify a joined multi-model/multi-key failure by its causes.

    The credential-rejection verdict is deliberately unavailable here: one
    key's 401 appearing in the joined text says nothing about the key this
    error is recorded against. Quota and overload dominate (they are what
    normally fails every model at once), and anything unrecognised stays
    ``unknown`` so the job policy retries rather than striking off a key.
    """
    if _MODEL_ABSENT_RE.search(aggregate):
        return {"kind": "model_no_access", "message": (
            "No configured Gemini model is available to this key; skipping to "
            "the next model in the fallback chain.")}
    if _QUOTA_RE.search(aggregate):
        return {"kind": "rate_limited", "message": (
            "Gemini rate/quota limit hit across the configured models. The key "
            "was temporarily removed from rotation; work continues on another "
            "available key.")}
    if _TEMPORARY_RE.search(aggregate):
        return {"kind": "temporary", "message": (
            "Gemini is temporarily overloaded. Retrying shortly.")}
    if _TIMEOUT_RE.search(aggregate):
        return {"kind": "timeout", "message": (
            "The Gemini request timed out. It will retry with the next "
            "model/key.")}
    if _NETWORK_RE.search(aggregate):
        return {"kind": "network", "message": (
            "Could not reach Gemini (network error). Retrying shortly.")}
    return {"kind": "unknown", "message": (
        f"Every configured Gemini model failed: {message[:200]}")}


def classify_gemini_error(exc: Exception) -> dict:
    """Map a Gemini exception to (kind, user message, status) — preserves the
    friend's diagnostic categories (leaked key / invalid / forbidden / quota).

    The rule this function exists to enforce is that **a failed API request is
    not proof that a key is invalid**: only an explicit credential rejection
    yields ``invalid_key``. Model availability, quota, rate limits, provider
    faults, timeouts, network faults, malformed requests and unparseable
    responses each get their own kind, so transient trouble can never be
    persisted as "this credential is dead".
    """
    message = str(exc)
    lowered = message.lower()
    status = http_status_of(message)

    def _result(kind: str, text: str) -> dict:
        # ``status`` is additive and safe: it is the provider's own number when
        # the message names one, and None otherwise. No payload, no credential.
        return {"kind": kind, "message": text, "status": status}

    if any(marker in lowered for marker in _AGGREGATE_MARKERS):
        return {**_classify_aggregate(message, message), "status": status}

    if _credential_rejection(lowered):
        return _result("invalid_key", (
            "Gemini rejected this API key. It has been removed from rotation — "
            "add a replacement key in Settings."))
    if _FORBIDDEN_RE.search(lowered):
        return _result("forbidden", (
            "Gemini received the key but refused access. The key's project may "
            "have a permission or API-enablement problem. It has been cooled down."))
    if _MODEL_ABSENT_RE.search(lowered):
        return _result("model_no_access", (
            "The key has no access to this model (quota limit 0); skipping to the "
            "next model in the fallback chain."))
    if _QUOTA_RE.search(lowered):
        return _result("rate_limited", (
            "Gemini rate/quota limit hit. The key was temporarily removed from "
            "rotation; work continues on another available key."))
    if _TIMEOUT_RE.search(lowered):
        return _result("timeout", (
            "The Gemini request timed out. It will retry with the next "
            "model/key."))
    if _TEMPORARY_RE.search(lowered):
        return _result("temporary", (
            "Gemini is temporarily overloaded. Retrying shortly."))
    if _NETWORK_RE.search(lowered):
        return _result("network", (
            "Could not reach Gemini (network error). Retrying shortly."))
    if _REQUEST_ERROR_RE.search(lowered):
        return _result("request_error", (
            "Gemini rejected the request itself (a configuration or request "
            "problem), not the key."))
    if _UNEXPECTED_RESPONSE_RE.search(lowered):
        return _result("unexpected_response", (
            "Gemini answered, but the response could not be read — a validation "
            "problem, not a bad key."))

    return _result("unknown", f"Unexpected Gemini error: {message[:200]}")


def classroom_error_message(status_code: int, detail: str) -> str:
    if status_code == 401:
        return ("Google authorization expired or was revoked. Reconnect Google "
                "Classroom from the Classroom page.")
    if status_code == 403:
        return ("Google denied Classroom/Drive access. Check the OAuth scopes, "
                "the enabled APIs in Google Cloud, and that this account is a "
                "teacher in the selected class.")
    if status_code == 404:
        return ("The requested Classroom resource was not found. Refresh the "
                "sync and try again.")
    return f"Google Classroom request failed (HTTP {status_code})."


def log_exception(component: str, event: str, exc: Exception, **context) -> None:
    log_event(component, event, level=logging.ERROR, error=str(exc), **context)


def brief_traceback(exc: Exception, limit: int = 4000) -> str:
    """Developer-page-only traceback rendering.

    Built from the EXCEPTION's own traceback rather than the interpreter's
    "current" one, so it is still correct when rendered outside an active
    ``except`` block (a global handler, a background thread's error report).
    """
    if exc is not None and getattr(exc, "__traceback__", None) is not None:
        text = "".join(traceback.format_exception(
            type(exc), exc, exc.__traceback__))
    else:
        text = traceback.format_exc()
    return text[-limit:]
