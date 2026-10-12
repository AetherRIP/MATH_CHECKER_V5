"""A small background scheduler so long-document jobs run independently of the
browser.

It is intentionally minimal (it does NOT duplicate the worker pool): it simply
picks up jobs in QUEUED/READY and drives them through the existing runner.
Processing therefore continues when the user navigates away or refreshes, and a
restart resumes unfinished jobs (boot recovery requeues them). A short
per-job cooldown prevents a parked job from being retried in a tight loop.
"""

from __future__ import annotations

import logging
import threading
import time

from ..config import get_config
from ..logging_setup import log_event
from . import states, store
from .runner import LargePdfRunnerHolder

log = logging.getLogger("large_pdf.scheduler")

_INTERVAL_SECONDS = 4.0
_PER_JOB_COOLDOWN = 4.0
_MAX_PER_TICK = 5


class LargePdfScheduler:
    def __init__(self):
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._last: dict[str, float] = {}

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="large-pdf-scheduler",
                                        daemon=True)
        self._thread.start()
        log_event("large_pdf", "scheduler started")

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=3.0)
            self._thread = None

    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception:
                log.exception("large PDF scheduler tick failed")
            self._stop.wait(_INTERVAL_SECONDS)

    def tick(self) -> int:
        if not get_config().large_pdf.enabled:
            return 0
        runner = LargePdfRunnerHolder.instance()
        now = time.time()
        processed = 0
        jobs = store.list_jobs(statuses=[states.QUEUED, states.READY],
                               limit=50)
        for job in jobs:
            if processed >= _MAX_PER_TICK:
                break
            if now - self._last.get(job["id"], 0.0) < _PER_JOB_COOLDOWN:
                continue
            self._last[job["id"]] = now
            try:
                runner.process(job["id"])
                processed += 1
            except Exception:
                log.exception("large PDF job failed to process")
        return processed


class LargePdfSchedulerHolder:
    _instance: LargePdfScheduler | None = None
    _lock = threading.Lock()

    @classmethod
    def instance(cls) -> LargePdfScheduler:
        with cls._lock:
            if cls._instance is None:
                cls._instance = LargePdfScheduler()
            return cls._instance

    @classmethod
    def reset(cls) -> None:
        with cls._lock:
            if cls._instance is not None:
                cls._instance.stop()
            cls._instance = None
