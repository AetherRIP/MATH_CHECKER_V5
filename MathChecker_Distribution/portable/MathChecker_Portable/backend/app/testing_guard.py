"""STOP-THE-LINE guard for testing-only runs.

While the marker is being validated against real students' work, NOTHING may
leave the machine: no grade published, no report uploaded or attached, no work
returned, no submission attachment modified. The UI can hide buttons, but a
retry, a scheduled job, a background worker or a direct API call must not be
able to publish either — so the check lives HERE, on the HTTP chokepoints every
Classroom/Drive write already goes through.

Enabled by ``[testing] only = true`` in ``config.toml`` (off by default so no
existing behaviour changes). Read-only access — listing courses, downloading
student work, reading the mark scheme — is deliberately NOT affected: testing
must still be able to see the real submissions.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

log = logging.getLogger("testing_guard")

#: Methods that cannot change anything on Google's side.
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


class PublicationBlocked(RuntimeError):
    """Raised when a write is attempted while testing-only mode is on."""

    def __init__(self, action: str, reason: str):
        self.action = action
        self.reason = reason
        super().__init__(
            f"TESTING ONLY: '{action}' was blocked and nothing was sent to "
            f"Google Classroom or Drive. {reason} Turn off Testing only mode "
            f"in Settings to allow publishing again."
        )


@dataclass(frozen=True)
class TestingState:
    only: bool
    reason: str


def state() -> TestingState:
    """The current testing-only state (never raises)."""
    try:
        from .config import get_config

        cfg = get_config()
        flag = bool(getattr(cfg, "testing", None) and cfg.testing.only)
    except Exception:  # noqa: BLE001 — a broken config must not crash a route
        return TestingState(False, "")
    if not flag:
        return TestingState(False, "")
    return TestingState(
        True,
        "Testing only mode is ON: results from this run are private to this "
        "machine and must not be released.")


def blocked_reason() -> str | None:
    """A reason string when writes are blocked, else None."""
    current = state()
    return current.reason if current.only else None


def is_mutating(method: str) -> bool:
    return str(method or "GET").upper() not in SAFE_METHODS


def assert_publication_allowed(action: str, *, method: str = "POST") -> None:
    """Raise :class:`PublicationBlocked` when this write must not happen."""
    if not is_mutating(method):
        return
    reason = blocked_reason()
    if reason:
        log.warning("testing-only: blocked %s", action)
        raise PublicationBlocked(action, reason)


__all__ = [
    "PublicationBlocked", "TestingState", "assert_publication_allowed",
    "blocked_reason", "is_mutating", "state",
]
