"""Which browser sessions are using the website right now.

Deliberately IN MEMORY ONLY. A heartbeat every 30 seconds per open tab is the
single most frequent thing the frontend does, and writing each one to SQLite
would (a) grow the database for no reason, (b) take the write lock constantly
and (c) make "is a user here?" depend on disk health instead of the network.
Nothing here needs to survive a restart either: if the process died, every
session is gone by definition, and the supervisor starts a fresh backend whose
registry starts empty.

Identity is a per-TAB id the frontend generates (sessionStorage), so two tabs
count as two sessions — which is what "the site stays up while at least one
browser session exists" means. A session is inactive once no heartbeat has been
seen for ``timeout_seconds``; the frontend also says goodbye explicitly on
``pagehide`` so a closed tab is noticed immediately rather than a timeout later.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass

#: Longest session id accepted. The id comes from the browser, so an absurd one
#: must be rejected rather than kept in memory (a cheap guard against a hostile
#: or buggy caller filling the dict with megabytes of keys).
MAX_SESSION_ID = 200


@dataclass
class Session:
    """One browser tab using the site."""

    id: str
    opened_at: float
    last_seen: float
    beats: int = 1
    hidden: bool = False

    def age(self, now: float) -> float:
        """Seconds since this session's last heartbeat (never negative)."""
        return max(0.0, now - self.last_seen)


class SessionRegistry:
    """Thread-safe, dependency-free set of live browser sessions.

    FastAPI runs sync route handlers in a thread pool, so heartbeats can arrive
    on several threads at once: every mutation is under one lock and no method
    ever calls out to the database, the clock or the log.
    """

    def __init__(self, timeout_seconds: float = 90.0) -> None:
        self._lock = threading.Lock()
        self._sessions: dict[str, Session] = {}
        self._timeout = max(1.0, float(timeout_seconds))
        #: How many sessions have ever been registered (diagnostics only).
        self.opened_total = 0

    # -- configuration ---------------------------------------------------------

    @property
    def timeout_seconds(self) -> float:
        return self._timeout

    def set_timeout(self, seconds: float) -> None:
        """Follow a live config change (the Settings page can edit this)."""
        self._timeout = max(1.0, float(seconds))

    # -- session lifecycle -----------------------------------------------------

    def beat(self, session_id: str, *, hidden: bool = False,
             now: float | None = None) -> Session | None:
        """Record a heartbeat, registering the session if it is new.

        Returns the session, or ``None`` for an id that is empty, unprintable
        or absurdly long — a rejection the caller reports rather than hiding.
        """
        sid = (session_id or "").strip()
        if not sid or len(sid) > MAX_SESSION_ID or not sid.isprintable():
            return None
        ts = time.monotonic() if now is None else now
        with self._lock:
            session = self._sessions.get(sid)
            if session is None:
                session = Session(id=sid, opened_at=ts, last_seen=ts,
                                  hidden=hidden)
                self._sessions[sid] = session
                self.opened_total += 1
            else:
                session.last_seen = ts
                session.beats += 1
                session.hidden = hidden
            return session

    def leave(self, session_id: str) -> bool:
        """Drop a session the browser says it is leaving. True if it existed."""
        sid = (session_id or "").strip()
        with self._lock:
            return self._sessions.pop(sid, None) is not None

    def prune(self, now: float | None = None) -> list[str]:
        """Remove sessions whose heartbeat stopped, oldest first.

        This is what turns "the browser closed without saying goodbye" (a
        crashed browser, a pulled cable, a slept laptop) into an inactive
        session after the timeout, with no reliance on the unload events.
        """
        ts = time.monotonic() if now is None else now
        with self._lock:
            expired = [s for s in self._sessions.values()
                       if s.age(ts) > self._timeout]
            expired.sort(key=lambda s: s.last_seen)
            for session in expired:
                del self._sessions[session.id]
            return [s.id for s in expired]

    def active(self, now: float | None = None) -> list[Session]:
        """Live sessions, most recently seen first."""
        ts = time.monotonic() if now is None else now
        with self._lock:
            live = [s for s in self._sessions.values() if s.age(ts) <= self._timeout]
        live.sort(key=lambda s: s.last_seen, reverse=True)
        return live

    def active_count(self, now: float | None = None) -> int:
        return len(self.active(now))

    def last_seen_age(self, now: float | None = None) -> float | None:
        """Seconds since the most recent heartbeat, or None if none is live."""
        live = self.active(now)
        if not live:
            return None
        return live[0].age(time.monotonic() if now is None else now)

    def next_expiry_in(self, now: float | None = None) -> float | None:
        """Seconds until the longest-lived session stops counting as active.

        Used only to report real state (a countdown the teacher can read), not
        to decide anything: the decision is always a fresh ``active_count()``.
        """
        live = self.active(now)
        if not live:
            return None
        ts = time.monotonic() if now is None else now
        return max(0.0, self._timeout - max(s.age(ts) for s in live))

    def clear(self) -> None:
        with self._lock:
            self._sessions.clear()
