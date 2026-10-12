"""Error envelope and human-friendly error classification.

The user-facing wording follows the legacy app's guidance (401 → re-auth,
429 → key cooled down, etc.) per V5 rule: reuse what works. Traces are only
shown on the Developer diagnostics surface.
"""

from __future__ import annotations

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


def classify_gemini_error(exc: Exception) -> dict:
    """Map a Gemini exception to (kind, user message) — preserves the friend's
    diagnostic categories (leaked key / invalid / forbidden / quota)."""
    message = str(exc)
    lowered = message.lower()

    if "reported as leaked" in lowered:
        return {"kind": "invalid_key", "message": (
            "This API key has been blocked because Google reports it as leaked. "
            "Create a new API key in Google AI Studio.")}
    if ("401" in message or "unauthenticated" in lowered
            or "invalid api key" in lowered or "api key not valid" in lowered
            or "api_key_invalid" in lowered):
        return {"kind": "invalid_key", "message": (
            "Gemini rejected this API key. It has been removed from rotation — "
            "add a replacement key in Settings.")}
    if "403" in message or "permission" in lowered or "forbidden" in lowered:
        return {"kind": "forbidden", "message": (
            "Gemini received the key but refused access. The key's project may "
            "have a permission or API-enablement problem. It has been cooled down.")}
    if "429" in message or "quota" in lowered or "resource exhausted" in lowered:
        return {"kind": "rate_limited", "message": (
            "Gemini rate/quota limit hit. The key was temporarily removed from "
            "rotation; work continues on another available key.")}
    if "limit: 0" in lowered or "limit:0" in lowered:
        return {"kind": "model_no_access", "message": (
            "The key has no access to this model (quota limit 0); skipping to the "
            "next model in the fallback chain.")}
    if "timed out" in lowered or "timeout" in lowered or "deadline exceeded" in lowered:
        return {"kind": "timeout", "message": (
            "The Gemini request timed out. It will retry with the next model/key.")}
    if "503" in message or "unavailable" in lowered or "overloaded" in lowered or "high demand" in lowered:
        return {"kind": "temporary", "message": (
            "Gemini is temporarily overloaded. Retrying shortly.")}

    return {"kind": "unknown", "message": f"Unexpected Gemini error: {message[:200]}"}


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
    """Developer-page-only traceback rendering."""
    return traceback.format_exc()[-limit:]
