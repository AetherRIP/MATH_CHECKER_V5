"""Pollable progress for Google Classroom syncs (transport fallback, §14).

Why this exists
---------------
A Cloudflare Quick Tunnel — and many school/corporate HTTP proxies — *buffer*
a streaming response. The browser's ``EventSource`` connects, receives
``200`` with the right ``text/event-stream`` headers, and then **nothing**
arrives until the entire response is finished.

Measured on the real public address (2026-10-06):

  * loopback, identical request .......... 1388 bytes in 12 s (first frame 3 ms)
  * through the tunnel ................... **0 bytes in 12 s**
  * short 2.5 s assignment sync ........... all frames arrived at once at 2.78 s

So a 40 s full-class sync left the teacher staring at a frozen
"Connecting…" screen with no way to tell a working sync from a dead one —
and the single-assignment sync appeared to do nothing at all until it was
already over.

What this module does
---------------------
Every sync runs on a background thread and publishes its progress into a tiny
in-process registry:

  * the SSE routes keep their EXACT frame contract — they now stream whatever
    the registry reports, so no UI or test sees a different event;
  * ``/api/classroom/sync/start`` + ``/api/classroom/sync/progress`` expose the
    same progress over plain request/response, which survives any proxy.

One run at a time is deliberate: a full sync is the heaviest thing this app
does, a second copy would duplicate Google quota use and SQLite writes, and
the UI has always presented syncing as a single serialized action.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from queue import Queue
from typing import Callable

#: Coarse progress per announced stage. The per-assignment events below refine
#: the "submissions" band, which is where a full sync spends almost all of its
#: time (18 assignments x ~2 s each).
STAGE_BASE: dict[str, float] = {
    "connect": 0.05,
    "courses": 0.10,
    "assignments": 0.20,
    "students": 0.45,
    "submissions": 0.60,
    "save": 0.95,
}

#: A full sync reports roughly two events per assignment; 400 frames is far
#: more than any single run produces and keeps the replay buffer bounded.
MAX_EVENTS = 400

#: How long a streaming client waits for the next frame before re-checking
#: whether the run finished. Small enough to end promptly, large enough not to
#: spin.
POLL_TICK_SECONDS = 0.25


class SyncRun:
    """One sync: its progress, its frames, and its stream subscribers."""

    def __init__(self, kind: str, target: dict) -> None:
        self.kind = kind                 # "full" | "assignment"
        self.target = dict(target)
        self.id = f"{kind}-{int(time.time() * 1000)}"
        self.started_at = time.time()
        self.finished_at: float | None = None
        self.running = True
        self.stage: str | None = None
        self.message = "Connecting…"
        self.progress = 0.0
        self.summary: dict | None = None
        self.error: str | None = None
        self.events: deque = deque(maxlen=MAX_EVENTS)
        self.queue: Queue = Queue()
        self._lock = threading.Lock()

    # ------------------------------------------------------------- reporting

    def report(self, event: dict) -> None:
        """Record one frame and hand it to every streaming subscriber."""
        with self._lock:
            self._apply(event)
            self.events.append(event)
        self.queue.put(event)

    def close(self) -> None:
        """Mark the run finished and release streaming subscribers."""
        self.running = False
        self.finished_at = time.time()
        self.queue.put(None)

    def _apply(self, event: dict) -> None:
        """Fold a frame into the aggregate progress a poller reads."""
        kind = event.get("type")
        if kind == "stage":
            stage = event.get("stage") or self.stage
            self.stage = stage
            if event.get("message"):
                self.message = event["message"]
            if stage in STAGE_BASE:
                self.progress = max(self.progress, STAGE_BASE[stage])
        elif kind == "assignment":
            total = int(event.get("total") or 0)
            index = int(event.get("index") or 0)
            title = event.get("title") or "assignment"
            self.message = f"Fetching submissions — {title} ({index}/{total})…"
            if total:
                self.progress = max(self.progress, 0.60 + 0.35 * (index / total))
        elif kind == "students":
            course = event.get("course") or "class"
            self.message = f"Fetching students — {event.get('count')} in {course}"
        elif kind == "roster":
            self.message = f"Roster: {event.get('count')} students"
        elif kind == "page":
            pages = event.get("pages") or 1
            self.message = (f"Fetched {event.get('collected')} submissions "
                            f"({pages} page{'' if pages == 1 else 's'})")
        elif kind == "done":
            self.summary = event.get("summary")
            self.progress = 1.0
            self.message = "Sync complete"
        elif kind == "error":
            self.error = event.get("message") or "Sync failed"
            self.message = "Sync failed"

    # -------------------------------------------------------------- reading

    def snapshot(self) -> dict:
        """JSON-serializable state: what a polling client needs to render the
        same progress line, progress bar, and final outcome as SSE gives."""
        with self._lock:
            return {
                "id": self.id,
                "kind": self.kind,
                "target": dict(self.target),
                "running": self.running,
                "stage": self.stage,
                "message": self.message,
                "progress": round(self.progress, 4),
                "started_at": self.started_at,
                "finished_at": self.finished_at,
                "elapsed_s": round((self.finished_at or time.time()) - self.started_at, 2),
                "summary": self.summary,
                "error": self.error,
                "event_count": len(self.events),
                "events": list(self.events),
            }

    def buffered_events(self) -> list[dict]:
        with self._lock:
            return list(self.events)


def empty_snapshot() -> dict:
    """Stable shape so a client can poll before anything has ever run."""
    return {
        "id": None, "kind": None, "target": {}, "running": False, "stage": None,
        "message": None, "progress": 0.0, "started_at": None, "finished_at": None,
        "elapsed_s": None, "summary": None, "error": None,
        "event_count": 0, "events": [],
    }


# ------------------------------------------------------------------ registry

_lock = threading.RLock()
_active: SyncRun | None = None
_last: SyncRun | None = None


def _run_worker(run: SyncRun, worker: Callable[[Callable[[dict], None]], None]) -> None:
    """Run the sync, guaranteeing the registry always reaches a final state.

    The worker reports its own honest ``error`` frame (it knows which stage
    failed); this only adds a frame for a crash that escaped it, and always
    closes the run so no client can wait forever on a dead sync.
    """
    try:
        worker(run.report)
    except Exception as exc:                      # noqa: BLE001 — never orphan a run
        run.report({"type": "error",
                    "message": f"Sync stopped unexpectedly: {exc}"})
    finally:
        run.close()


def start(kind: str, target: dict,
          worker: Callable[[Callable[[dict], None]], None]) -> tuple[SyncRun, bool]:
    """Begin a sync (or join the one already running).

    Returns ``(run, started)``. ``started`` is False when a sync was already in
    progress — the caller then watches THAT run, which is the honest answer:
    the app can only sync once at a time.
    """
    global _active, _last
    with _lock:
        if _active is not None and _active.running:
            return _active, False
        run = SyncRun(kind, target)
        _active = run
        _last = run
    threading.Thread(target=_run_worker, args=(run, worker),
                     name=f"mc5-sync-{kind}", daemon=True).start()
    return run, True


def current() -> SyncRun | None:
    """The running sync, else the most recent one (so a late poller still sees
    the result it triggered), else None."""
    with _lock:
        if _active is not None and _active.running:
            return _active
        return _last


def snapshot() -> dict:
    run = current()
    return run.snapshot() if run is not None else empty_snapshot()


def reset_for_tests() -> None:
    global _active, _last
    with _lock:
        _active = None
        _last = None
