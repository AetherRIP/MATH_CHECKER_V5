"""The real signals that decide whether an idle shutdown may proceed.

Nothing here is a guess or a UI flag: every source reads the same state the
engine itself acts on — the job tables, the long-document worker's own
in-flight set, the automation runner's live leases and the in-process counter
that every database write transaction marks.

A source that cannot be read is treated as BUSY, not as clear. "We could not
tell whether a correction is running" must never end in a process exiting
underneath it; the monitor's maximum wait is what stops that being an
indefinite state.
"""

from __future__ import annotations

import time
from typing import Callable

from . import activity

#: A source returns ``None`` when nothing is in progress, or a short
#: description of what is still running.
Source = Callable[[], str | None]


def _correction_jobs() -> str | None:
    """Corrections in the queue or being marked (they resume, but let them run)."""
    from ..jobs import manager as jobs_manager

    counts = jobs_manager.JobManagerHolder.instance().counts()
    busy = {status: counts.get(status, 0)
            for status in jobs_manager.ACTIVE_STATUSES if counts.get(status)}
    if not busy:
        return None
    total = sum(busy.values())
    detail = ", ".join(f"{status} {n}" for status, n in sorted(busy.items()))
    return f"{total} correction job(s) in progress ({detail})"


def _automation_jobs() -> str | None:
    """Automation steps the runner is holding a live lease on right now.

    A job waiting for a teacher's approval, or parked for its next retry, is
    NOT work in progress: it is a row that survives the shutdown and is picked
    up exactly where it stopped when the app comes back. Only a live lease
    means something is running at this moment.
    """
    from .. import db
    from ..automation import store as automation_store

    horizon = time.time() - getattr(automation_store, "CLAIM_LEASE_SECONDS", 900)
    row = db.query_one(
        "SELECT COUNT(*) AS n FROM automation_jobs "
        "WHERE claimed_by IS NOT NULL AND claimed_at >= ?",
        (horizon,))
    count = int(row["n"]) if row else 0
    if not count:
        return None
    return f"{count} Classroom automation job(s) being processed"


def _critical_operations() -> str | None:
    """Database write transactions and file operations happening right now."""
    labels = activity.busy()
    if not labels:
        return None
    return "operation(s) in flight: " + ", ".join(labels)


#: How long a hold answer is reused. The hold only has to be right on the
#: scale of the idle grace period, and answering it means reading a status file
#: and checking a live PID — once every 20s is plenty, every 5s is a poll.
_HOLD_TTL_SECONDS = 20.0
_hold_cache: tuple[float, str | None] | None = None


def reset_hold_cache() -> None:
    """Forget the cached hold answer (tests, and after a settings change)."""
    global _hold_cache
    _hold_cache = None


def _public_endpoint() -> str | None:
    """A published public address that is currently reachable from the internet.

    A remote visitor cannot start a process on this machine. If the app shut
    itself down while its public address was live, the link would simply stop
    working until somebody local started it again, and the next user could not
    do anything about it. So a live public endpoint is a HOLD: the app stays
    up for as long as it is published, however empty the room is.

    A local-only install (the normal case) is unaffected: nothing is published,
    nothing is held, and the idle stop works exactly as configured.
    """
    global _hold_cache
    now = time.monotonic()
    if _hold_cache is not None and now - _hold_cache[0] < _HOLD_TTL_SECONDS:
        return _hold_cache[1]

    from .. import public_access

    reason: str | None = None
    if public_access.status().get("status") == "online":
        reason = ("the public address is live — someone arriving from the "
                  "internet cannot start this app again, so it stays up "
                  "until Public Address is switched off")
    _hold_cache = (now, reason)
    return reason


#: Sources that PREVENT the idle stop entirely rather than defer it until a
#: job finishes. Nothing here is transient work; each entry is a reason this
#: process must keep running until the reason goes away.
HOLD_SOURCES: tuple[tuple[str, Source], ...] = (
    ("public_endpoint", _public_endpoint),
)


SOURCES: tuple[tuple[str, Source], ...] = (
    ("correction_jobs", _correction_jobs),
    ("automation_jobs", _automation_jobs),
    ("critical_operations", _critical_operations),
)


def _read(sources: tuple[tuple[str, Source], ...]) -> list[dict]:
    """Run sources, treating one that cannot be read as NOT clear."""
    blockers: list[dict] = []
    for name, source in sources:
        try:
            detail = source()
        except Exception as exc:  # noqa: BLE001 — an unreadable source is not "clear"
            blockers.append({"source": name,
                             "detail": f"could not be checked ({exc})"})
            continue
        if detail:
            blockers.append({"source": name, "detail": detail})
    return blockers


def collect() -> list[dict]:
    """Everything that is still running, as ``{"source", "detail"}`` entries."""
    return _read(SOURCES)


def holds() -> list[dict]:
    """Reasons the app must stay up regardless of the idle countdown."""
    return _read(HOLD_SOURCES)
