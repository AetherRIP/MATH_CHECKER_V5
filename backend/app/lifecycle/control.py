"""How the backend asks its own process to stop — and how it takes that back.

The backend owns the shutdown decision (a browser tab cannot stop a server, and
it must not be able to): the monitor decides, then calls the requester attached
here, which is the uvicorn server's graceful-exit hook installed by
``main.py``. Setting that flag makes uvicorn finish in-flight requests and run
the application's lifespan shutdown — workers drained, tunnels stopped, temp
workspaces cleaned — instead of killing the process.

The request is CANCELLABLE for a short window (``cancel``), because a teacher
reopening the site at the last second must win over the countdown. The monitor
uses that window as its final confirmation step.
"""

from __future__ import annotations

import threading
import time
from typing import Callable

_lock = threading.Lock()
_requester: Callable[[], None] | None = None
_canceller: Callable[[], None] | None = None
_requested_at: float | None = None
_requested_wall: float | None = None
_reason: str = ""
_detail: str = ""


def attach(requester: Callable[[], None] | None,
           canceller: Callable[[], None] | None = None) -> None:
    """Bind the process's graceful-exit (and take-it-back) hooks."""
    global _requester, _canceller
    with _lock:
        _requester = requester
        _canceller = canceller


def attached() -> bool:
    with _lock:
        return _requester is not None


def request(reason: str, detail: str = "") -> bool:
    """Ask the process to exit. True when a requester actually ran."""
    global _requested_at, _requested_wall, _reason, _detail
    with _lock:
        requester = _requester
        _requested_at = time.monotonic()
        _requested_wall = time.time()
        _reason = reason
        _detail = detail
    if requester is None:
        return False
    try:
        requester()
    except Exception:  # noqa: BLE001 — a failed exit request is reported, not raised
        return False
    return True


def cancel() -> bool:
    """Take back a shutdown request made moments ago. True if one was pending."""
    global _requested_at, _requested_wall, _reason, _detail
    with _lock:
        pending = _requested_at is not None
        canceller = _canceller
        _requested_at = None
        _requested_wall = None
        _reason = ""
        _detail = ""
    if not pending:
        return False
    if canceller is not None:
        try:
            canceller()
        except Exception:  # noqa: BLE001 — best effort; the flag is reset anyway
            pass
    return True


def pending() -> bool:
    with _lock:
        return _requested_at is not None


def pending_for() -> float:
    """Seconds since the shutdown was requested (0.0 when none is pending)."""
    with _lock:
        if _requested_at is None:
            return 0.0
        return max(0.0, time.monotonic() - _requested_at)


def state() -> dict:
    """Report the request state without exposing anything else."""
    with _lock:
        return {
            "attached": _requester is not None,
            "pending": _requested_at is not None,
            "pending_seconds": (0.0 if _requested_at is None
                                else max(0.0, time.monotonic() - _requested_at)),
            "reason": _reason,
            "detail": _detail,
            "requested_at": _requested_wall,
        }


def reset() -> None:
    """Forget the requester and any pending request. Tests only."""
    global _requester, _canceller, _requested_at, _requested_wall, _reason, _detail
    with _lock:
        _requester = None
        _canceller = None
        _requested_at = None
        _requested_wall = None
        _reason = ""
        _detail = ""
