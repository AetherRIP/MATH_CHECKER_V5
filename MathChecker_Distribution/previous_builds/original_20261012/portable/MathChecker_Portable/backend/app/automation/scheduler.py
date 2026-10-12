"""The single authoritative background monitor (§5, §6, §7, §45).

One centralised scheduler checks all ENABLED assignments on the configured
interval — never one polling loop per assignment, and never a request between
checks. It:

    tick -> is automation enabled and is the interval due?
         -> sync tracking for each enabled assignment
         -> detect genuinely new submission versions -> queue persistent jobs
         -> advance queued jobs through the existing correction pipeline

Everything is driven by the persisted configuration, so the interval and the
enabled/disabled state survive a restart and the monitor resumes with them.
"""

from __future__ import annotations

import logging
import threading
import time

from ..logging_setup import log_event
from . import detection, runner as runner_mod, states, store

log = logging.getLogger("automation.scheduler")

#: How often the scheduler wakes to check whether a cycle is due. This is a
#: LOCAL timer, not an API call — no network traffic happens between cycles.
TICK_SECONDS = 5.0

#: How often the runner advances in-flight jobs while waiting for the next
#: detection cycle.
RUNNER_TICK_SECONDS = 3.0

#: Bound on how many assignments are checked in one cycle.
MAX_ASSIGNMENTS_PER_CYCLE = 50


class AutomationScheduler:
    def __init__(self, *, sync_fn=None, detect_fn=None, cfg_fn=None,
                 runner=None):
        self._sync_fn = sync_fn
        self._detect_fn = detect_fn
        self._cfg_fn = cfg_fn
        self._runner = runner
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._last_tick = 0.0
        self._running = False

    # -- configuration -------------------------------------------------------

    def _cfg(self):
        if self._cfg_fn is not None:
            return self._cfg_fn()
        from ..config import get_config
        return get_config().automation

    def _runner_obj(self):
        if self._runner is not None:
            return self._runner
        return runner_mod.runner()

    def _sync(self, course_id: str, coursework_id: str) -> dict:
        if self._sync_fn is not None:
            return self._sync_fn(course_id, coursework_id)
        from ..classroom import submissions as cs
        return cs.sync_submission_tracking(course_id, coursework_id)

    def _detect(self, course_id: str, coursework_id: str,
                title: str | None) -> dict:
        if self._detect_fn is not None:
            return self._detect_fn(course_id, coursework_id, title)
        return detection.detect_assignment(course_id, coursework_id,
                                           assignment_title=title)

    # -- lifecycle -----------------------------------------------------------

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="automation-scheduler",
                                        daemon=True)
        self._thread.start()
        log_event("automation", "scheduler started")

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        thread = self._thread
        if thread and thread.is_alive():
            thread.join(timeout=3.0)
        self._thread = None
        log_event("automation", "scheduler stopped")

    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def wake(self) -> None:
        self._wake.set()

    # -- loop ----------------------------------------------------------------

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self._tick()
            except Exception:  # noqa: BLE001 — the monitor must never die
                log.exception("automation tick failed")
            self._wake.wait(timeout=TICK_SECONDS)
            self._wake.clear()

    def _tick(self) -> None:
        cfg = self._cfg()
        now_ts = time.time()
        # In-flight jobs advance regardless of the detection interval, so a
        # correction that finished while we waited is picked up promptly.
        self._advance_jobs(cfg, now_ts)
        if not cfg.enabled:
            self._running = False
            return
        next_check = store.get_meta("next_check")
        if next_check is None or now_ts >= next_check:
            self.run_cycle(now_ts=now_ts)

    def _advance_jobs(self, cfg, now_ts: float) -> None:
        if now_ts - self._last_tick < RUNNER_TICK_SECONDS:
            return
        self._last_tick = now_ts
        try:
            # Worker OFF: jobs already in flight still advance to a safe
            # stopping point, but jobs that have not started (NEW) stay
            # queued until the worker is turned back on.
            self._runner_obj().run_pending(max_jobs=10,
                                           include_new=bool(cfg.enabled))
        except Exception:  # noqa: BLE001
            log.exception("advancing automation jobs failed")

    # -- one detection cycle -------------------------------------------------

    def run_cycle(self, *, now_ts: float | None = None) -> dict:
        """Run one detection + queueing cycle across every enabled assignment."""
        now_ts = now_ts if now_ts is not None else time.time()
        cfg = self._cfg()
        interval = cfg.effective_interval()
        assignments = store.enabled_assignments()[:MAX_ASSIGNMENTS_PER_CYCLE]
        found = 0
        checked = 0
        errors: list[dict] = []

        from .. import db
        for assignment in assignments:
            coursework_id = assignment["coursework_id"]
            course_id = assignment["course_id"]
            if not course_id:
                continue
            # An assignment deleted in Classroom is archived locally; never
            # poll or correct it (Google answers 400 forever). Self-heal the
            # automation flag too in case it was enabled before the deletion.
            cw = db.query_one(
                "SELECT title, sync_state FROM coursework WHERE id = ?",
                (coursework_id,))
            if cw is not None and (cw["sync_state"] or "ACTIVE") \
                    == "DELETED_IN_CLASSROOM":
                store.set_assignment_enabled(coursework_id, course_id, False)
                log_event("automation", "skipped archived assignment",
                          coursework_id=coursework_id)
                continue
            checked += 1
            sync_error = None
            try:
                self._sync(course_id, coursework_id)
            except Exception as exc:  # noqa: BLE001 — a failed refresh must not
                # stop detection: we queue from the last VERIFIED data, exactly
                # like the rest of the app, and report the refresh failure.
                sync_error = str(exc)[:200]
            try:
                row = cw
                summary = self._detect(course_id, coursework_id,
                                       row["title"] if row else None)
                found += summary.get("queued_count", 0)
                store.touch_assignment_check(coursework_id, error=sync_error)
                if sync_error:
                    errors.append({"coursework_id": coursework_id,
                                   "error": sync_error})
            except Exception as exc:  # noqa: BLE001 — one assignment cannot stop the rest
                errors.append({"coursework_id": coursework_id,
                               "error": str(exc)[:200]})
                store.touch_assignment_check(coursework_id, error=str(exc)[:200])
                log_event("automation", "assignment check failed",
                          level=logging.WARNING, coursework_id=coursework_id,
                          error=str(exc)[:200])

        store.set_meta("last_check", now_ts)
        next_check = now_ts + interval * 60
        store.set_meta("next_check", next_check)
        self._running = True
        if found:
            store.log_event_entry("success", "cycle",
                                  f"Found {found} new submission(s)")
        log_event("automation", "detection cycle complete", checked=checked,
                  found=found, errors=len(errors))
        return {"checked": checked, "found": found, "errors": errors,
                "next_check": next_check, "interval_minutes": interval}

    # -- status for the UI ---------------------------------------------------

    def status(self) -> dict:
        cfg = self._cfg()
        counts = store.counts()
        assignments = store.enabled_assignments()
        total = sum(counts.values())
        processing_states = (
            states.NEW, states.CORRECTING, states.RESULT_VALIDATED,
            states.REPORT_GENERATED, states.REPORT_VALIDATED,
            states.REPORT_UPLOADED, states.REPORT_ATTACHED,
            states.ATTACHMENT_VERIFIED, states.GRADE_DRAFTED,
            states.GRADE_VERIFIED, states.RETURNING,
        )
        waiting = sum(counts.get(s, 0) for s in states.RETRYABLE_FAILURES) \
            + counts.get(states.NEW, 0)
        thread_alive = self.running()
        last_activity = store.last_activity()
        candidate_activity = store.get_meta("last_check")
        if candidate_activity and (last_activity is None
                                   or candidate_activity > last_activity):
            last_activity = float(candidate_activity)
        if not cfg.enabled:
            worker_state = "stopped"
        elif thread_alive:
            worker_state = "running"
        else:
            worker_state = "stalled"   # enabled but no thread — needs attention
        return {
            "enabled": bool(cfg.enabled),
            "interval_minutes": cfg.effective_interval(),
            "last_check": store.get_meta("last_check"),
            "next_check": store.get_meta("next_check"),
            "running": thread_alive and bool(cfg.enabled),
            # Background-worker control (§3): the persisted preference, the
            # REAL thread state and the queue depth, reported separately so
            # the UI can never show a locally-toggled button as "running".
            "worker_enabled": bool(cfg.enabled),
            "worker_state": worker_state,
            "thread_running": thread_alive,
            "last_activity": last_activity,
            "pending_jobs": waiting,
            "active_jobs": sum(counts.get(s, 0) for s in processing_states),
            "assignments_enabled": len(assignments),
            # Post-correction behaviour, exposed so the UI can show/toggle it.
            # ``review_mode`` holds every grade as a teacher-approved draft and
            # never returns work until the teacher approves it.
            "review_mode": bool(getattr(cfg, "review_mode", False)),
            "draft_only": bool(getattr(cfg, "draft_only", False)),
            "attach_report": bool(getattr(cfg, "attach_report", True)),
            "submit_grade": bool(getattr(cfg, "submit_grade", True)),
            "return_work": bool(getattr(cfg, "return_work", True)),
            # Profile of the PDF attached to the student's submission.
            # "detailed" attaches the WHOLE report: after the score and the
            # question table, every question gets its own block with the
            # student's answer, what the mark scheme required, the awarded
            # mark points and the marking comment.
            "report_detail": str(getattr(cfg, "report_detail", "detailed")),
            "counts": {
                "total": total,
                "completed": counts.get(states.COMPLETE, 0),
                "processing": sum(counts.get(s, 0) for s in processing_states),
                "waiting": waiting,
                "failed": sum(counts.get(s, 0)
                              for s in states.RETRYABLE_FAILURES) +
                          sum(counts.get(s, 0) for s in states.NEEDS_ATTENTION),
                "needs_attention": sum(counts.get(s, 0)
                                       for s in states.NEEDS_ATTENTION),
            },
            "by_state": counts,
        }


# -- process-wide singleton ---------------------------------------------------

_scheduler: AutomationScheduler | None = None
_lock = threading.Lock()


class AutomationSchedulerHolder:
    @staticmethod
    def instance() -> AutomationScheduler:
        global _scheduler
        with _lock:
            if _scheduler is None:
                _scheduler = AutomationScheduler()
            return _scheduler

    @staticmethod
    def reset() -> None:
        global _scheduler
        with _lock:
            if _scheduler is not None:
                _scheduler.stop()
            _scheduler = None
