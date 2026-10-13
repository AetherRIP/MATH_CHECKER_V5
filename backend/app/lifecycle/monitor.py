"""The backend's own decision to stop, and the state it reports while deciding.

The lifecycle is a small state machine, driven by one background task:

    serving            at least one browser session is beating
    counting_down      nobody is active; the grace period is running
    waiting_for_work   the grace period expired but work is still in flight
    held               a reason that never expires forbids stopping (e.g. the
                       app is published on a public address)
    shutting_down      a graceful exit has been requested (cancellable briefly)
    always_on          the feature is switched off in Settings

Only ``shutting_down`` ends the process, and it is reached only after the
built-in checks in :mod:`app.lifecycle.busy` report nothing running. Every
transition is logged once (never per heartbeat) so the log shows the story of a
day — started, idle, stopped, restarted — without growing per beat.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from typing import Callable

from ..logging_setup import log_event
from . import busy, control
from .sessions import SessionRegistry

log = logging.getLogger("lifecycle.monitor")

#: How often the state machine looks at the world.
TICK_SECONDS = 5.0

SERVING = "serving"
COUNTING_DOWN = "counting_down"
WAITING_FOR_WORK = "waiting_for_work"
HELD = "held"
SHUTTING_DOWN = "shutting_down"
ALWAYS_ON = "always_on"
STOPPED = "stopped"


class LifecycleMonitor:
    """Owns sessions, the countdown and the shutdown request."""

    def __init__(self, *, registry: SessionRegistry | None = None,
                 clock: Callable[[], float] = time.monotonic,
                 tick_seconds: float = TICK_SECONDS,
                 collect: Callable[[], list[dict]] = busy.collect,
                 holds: Callable[[], list[dict]] = busy.holds,
                 request: Callable[..., bool] = control.request,
                 cancel: Callable[[], bool] = control.cancel) -> None:
        self.registry = registry or SessionRegistry()
        self._clock = clock
        self._tick_seconds = max(0.05, float(tick_seconds))
        self._collect = collect
        self._holds = holds
        self._request = request
        self._cancel = cancel
        self.state = ALWAYS_ON
        self.deadline: float | None = None
        self.blocked_since: float | None = None
        self.blockers: list[dict] = []
        self.started_at = time.time()
        self.stops = 0
        self.cancels = 0
        self.deferrals = 0
        self._requested = False
        self._task: asyncio.Task | None = None
        self._stopped = threading.Event()
        self._last_blockers: tuple[str, ...] = ()

    # -- lifecycle of the monitor itself ---------------------------------------

    def start(self, loop: asyncio.AbstractEventLoop | None = None) -> None:
        """Start the tick loop (no-op if it is already running)."""
        if self._task is not None and not self._task.done():
            return
        self._stopped.clear()
        self.state = SERVING
        # Take the configured session timeout BEFORE the first tick: the
        # registry would otherwise decide "is this session still here?" with its
        # built-in default for up to one tick, and the log line below would
        # report a number the configuration does not agree with.
        cfg = _cfg()
        if cfg is not None and getattr(cfg, "enabled", False):
            self.registry.set_timeout(cfg.session_timeout_seconds)
        self._task = asyncio.ensure_future(self.run())
        log_event("lifecycle", "idle monitor started",
                  heartbeat_seconds=(cfg.heartbeat_seconds if cfg else 30),
                  session_timeout_seconds=self.registry.timeout_seconds,
                  idle_grace_seconds=(cfg.idle_grace_seconds if cfg else 120))

    async def stop(self) -> None:
        self._stopped.set()
        task = self._task
        self._task = None
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        self.state = STOPPED

    async def run(self) -> None:
        while not self._stopped.is_set():
            try:
                self.tick()
            except Exception as exc:  # noqa: BLE001 — never kill the loop
                log_event("lifecycle", "idle check failed",
                          level=logging.WARNING, error=str(exc))
            await asyncio.sleep(self._tick_seconds)

    # -- heartbeats -------------------------------------------------------------

    def beat(self, session_id: str, *, hidden: bool = False) -> dict:
        """Register a heartbeat and report the state the caller should follow."""
        # The monitor's clock is passed everywhere so that ONE time source
        # decides ages and deadlines; letting the registry pick its own would
        # make "last seen" and "countdown" incomparable.
        session = self.registry.beat(session_id, hidden=hidden,
                                     now=self._clock())
        if session is None:
            return {"accepted": False, **self.snapshot()}
        self._settle_state()
        # A heartbeat is the ONLY thing that cancels a shutdown, and it cancels
        # it even when the countdown had already expired: a tab that comes back
        # before the process has gone must keep the app alive.
        if self.state in (COUNTING_DOWN, WAITING_FOR_WORK, SHUTTING_DOWN):
            self._cancel_pending(reason="a browser session came back")
        return {"accepted": True, **self.snapshot()}

    def leave(self, session_id: str) -> dict:
        """Record a deliberate goodbye (tab closed / navigated away)."""
        removed = self.registry.leave(session_id)
        return {"removed": removed, **self.snapshot()}

    def _settle_state(self) -> None:
        """Report the truthful state before the first tick has run.

        The very first heartbeat (and the very first status request) can arrive
        before the monitor's own first tick — it ticks every few seconds and a
        browser is faster. Saying "always on" then would claim the feature is
        switched off, which is false, so an enabled lifecycle that has not
        ticked yet reads as ``serving``: the app is up and serving. The next
        tick takes it from there.
        """
        cfg = _cfg()
        if cfg is None or not cfg.enabled:
            return
        if self.state in (ALWAYS_ON, STOPPED):
            self.state = SERVING

    def _cancel_pending(self, reason: str) -> None:
        if control.pending() or self.state == SHUTTING_DOWN:
            if self._cancel():
                self.cancels += 1
                log_event("lifecycle", "shutdown cancelled", reason=reason)
        if self.state in (COUNTING_DOWN, WAITING_FOR_WORK, SHUTTING_DOWN):
            self.state = SERVING
            self.deadline = None
            self.blocked_since = None
            self.blockers = []
            # The next idle period is a NEW decision: it may request again.
            self._requested = False
            log_event("lifecycle", "serving again",
                      active_sessions=self.registry.active_count(self._clock()))

    # -- the state machine ------------------------------------------------------

    def tick(self, now: float | None = None) -> str:
        """One pass. Returns the resulting state (also visible in snapshot())."""
        cfg = _cfg()
        if cfg is None:
            return self.state
        self.registry.set_timeout(cfg.session_timeout_seconds)
        ts = self._clock() if now is None else now

        expired = self.registry.prune(ts)
        if expired:
            log_event("lifecycle", "sessions timed out",
                      expired=len(expired),
                      still_active=self.registry.active_count(ts))

        if not cfg.enabled:
            if self.state != ALWAYS_ON:
                self.state = ALWAYS_ON
                log_event("lifecycle", "lifecycle management disabled — staying up")
            self.deadline = None
            self.blockers = []
            return self.state

        if self.registry.active_count(ts):
            if self.state != SERVING:
                self.state = SERVING
                self.deadline = None
                self.blocked_since = None
                self.blockers = []
                log_event("lifecycle", "serving", active_sessions=self.registry.active_count(ts))
            return self.state

        # A source that forbids stopping (a live public address) is checked
        # BEFORE any countdown: there is no point starting a clock that must
        # not be allowed to reach zero.
        held = self._holds()
        if held:
            if self.state != HELD:
                self.state = HELD
                self.deadline = None
                self.blocked_since = None
                self.blockers = held
                log_event("lifecycle", "idle stop held",
                          reasons=[h.get("source") for h in held])
            return self.state

        if self.state == SHUTTING_DOWN:
            # The decision is made. Only a heartbeat (handled in ``beat``) can
            # take it back; otherwise wait out the last-chance window and stop.
            if self.deadline is not None and ts < self.deadline:
                return self.state
            self._request_shutdown()
            return self.state

        if self.deadline is None:
            self.deadline = ts + cfg.idle_grace_seconds
            self.blocked_since = None
            self.state = COUNTING_DOWN
            log_event("lifecycle", "idle countdown started",
                      seconds=cfg.idle_grace_seconds)
            return self.state

        if ts < self.deadline:
            self.state = COUNTING_DOWN
            return self.state

        # Countdown expired: only now is it worth asking what is still running.
        self.blockers = self._collect()
        if self.blockers:
            if self.blocked_since is None:
                self.blocked_since = ts
                self.deferrals += 1
                log_event("lifecycle", "idle shutdown deferred",
                          blockers=[b.get("source") for b in self.blockers],
                          max_wait_seconds=cfg.max_shutdown_wait_seconds)
            waited = ts - self.blocked_since
            self.state = WAITING_FOR_WORK
            if waited < cfg.max_shutdown_wait_seconds:
                return self.state
            # The cap exists so a wedged operation cannot pin the process open
            # forever. Jobs interrupted here are NOT lost: boot recovery
            # requeues in-flight corrections and long documents on the next
            # start, which is the documented safe checkpoint.
            log_event("lifecycle", "maximum wait reached — shutting down",
                      waited_seconds=round(waited, 1),
                      blockers=[b.get("source") for b in self.blockers])

        # The decision: nobody is here and nothing is running. Hold the request
        # for a short last-chance window so a teacher arriving right now still
        # cancels it (that is the difference between "idle" and "gone").
        self.state = SHUTTING_DOWN
        self.stops += 1
        log_event("lifecycle", "idle shutdown — no active sessions and no work",
                  grace_seconds=cfg.idle_grace_seconds,
                  last_chance_seconds=cfg.last_chance_seconds)
        self.deadline = (ts + cfg.last_chance_seconds
                         if cfg.last_chance_seconds else None)
        if self.deadline is not None and ts < self.deadline:
            return self.state
        self._request_shutdown()
        return self.state

    def _request_shutdown(self) -> None:
        """Ask the process to exit once; repeated ticks never re-ask."""
        if self._requested:
            return
        requested = self._request("idle", "no active browser sessions")
        self._requested = True
        log_event("lifecycle", "shutdown requested" if requested
                  else "shutdown requested (no process hook attached)",
                  hook=requested)

    # -- reporting --------------------------------------------------------------

    def snapshot(self) -> dict:
        """The real, unembellished state — what the UI and the supervisor show."""
        self._settle_state()
        cfg = _cfg()
        now = self._clock()
        active = self.registry.active_count(now)
        countdown = None
        if self.state == COUNTING_DOWN and self.deadline is not None:
            countdown = max(0.0, round(self.deadline - now, 1))
        elif self.state == SHUTTING_DOWN and self.deadline is not None:
            countdown = max(0.0, round(self.deadline - now, 1))
        return {
            "enabled": bool(cfg.enabled) if cfg else False,
            "supervisor": _supervised(),
            "backend": "running",
            "state": self.state,
            "active_sessions": active,
            "session_timeout_seconds": self.registry.timeout_seconds,
            "heartbeat_seconds": cfg.heartbeat_seconds if cfg else 30,
            "idle_grace_seconds": cfg.idle_grace_seconds if cfg else 0,
            "max_shutdown_wait_seconds": cfg.max_shutdown_wait_seconds if cfg else 0,
            "last_chance_seconds": cfg.last_chance_seconds if cfg else 0,
            "countdown_seconds": countdown,
            "last_heartbeat_age_seconds": (None if self.registry.last_seen_age(now) is None
                                           else round(self.registry.last_seen_age(now) or 0.0, 1)),
            "session_expires_in_seconds": (None if self.registry.next_expiry_in(now) is None
                                           else round(self.registry.next_expiry_in(now) or 0.0, 1)),
            "blockers": list(self.blockers),
            "deferrals": self.deferrals,
            "stops": self.stops,
            "cancels": self.cancels,
            "uptime_seconds": round(time.time() - self.started_at, 1),
            "shutdown": control.state(),
        }


class LifecycleMonitorHolder:
    """One monitor per process, shared by the routes and the lifespan."""

    _instance: LifecycleMonitor | None = None
    _lock = threading.Lock()

    @classmethod
    def instance(cls) -> LifecycleMonitor:
        with cls._lock:
            if cls._instance is None:
                cls._instance = LifecycleMonitor()
            return cls._instance

    @classmethod
    def reset(cls) -> None:
        with cls._lock:
            cls._instance = None


def get_monitor() -> LifecycleMonitor:
    return LifecycleMonitorHolder.instance()


# -- config helpers ------------------------------------------------------------

def _cfg():
    """Live lifecycle config, or None when configuration cannot be read."""
    try:
        from ..config import get_config
        return get_config().lifecycle
    except Exception:  # noqa: BLE001 — reporting must never raise
        return None


def _supervised() -> bool:
    """Is this backend running under the on-demand supervisor?"""
    import os
    return os.environ.get("MC5_SUPERVISED") == "1"
