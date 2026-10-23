"""Background job system (V5 §7, §13).

Jobs live in SQLite, so a restart never loses them: QUEUED jobs are picked
up on boot, in-flight jobs are recovered, and completed work is never
repeated. Duplicate prevention comes from a partial UNIQUE index (one active
job per submission). Stage transitions are published to listeners (SSE) the
moment they happen — progress is real, never simulated.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
import time
import uuid

from .. import db
from .. import retry_policy
from ..logging_setup import log_event

log = logging.getLogger("jobs.manager")

QUEUED = "QUEUED"
PREPARING = "PREPARING"
DOWNLOADING = "DOWNLOADING"
UPLOADING = "UPLOADING"
MARKING = "MARKING"
VERIFYING = "VERIFYING"
SAVING = "SAVING"
WAITING_FOR_API = "WAITING_FOR_API"
RETRYING = "RETRYING"
COMPLETED = "COMPLETED"
FAILED = "FAILED"
CANCELLED = "CANCELLED"

# Statuses a job can hold while something is actually working on it.
# The WorkerPool uses 'STARTING' (it only writes QUEUED->STARTING on claim and
# COMPLETED/FAILED at the end), so recovery must include it or a restarted
# app never wakes a mid-flight job (§12).
STARTING = "STARTING"
PROCESSING = "PROCESSING"

ACTIVE_STATUSES = (QUEUED, STARTING, PREPARING, DOWNLOADING, UPLOADING, MARKING,
                   VERIFYING, SAVING, WAITING_FOR_API, RETRYING)

STAGE_PROGRESS = {
    QUEUED: 0.0,
    PREPARING: 0.05,
    DOWNLOADING: 0.15,
    UPLOADING: 0.3,
    MARKING: 0.5,
    VERIFYING: 0.85,
    SAVING: 0.95,
    COMPLETED: 1.0,
    FAILED: 1.0,
    CANCELLED: 1.0,
}


def _effective_progress(row: dict | None) -> dict | None:
    """Return ``row`` with ``progress`` filled in from its real status.

    Only :meth:`JobManager._set_status` maintains the ``progress`` column. A job
    run by the WorkerPool is moved through its states with raw SQL, so it would
    otherwise report ``0`` for its entire life - including once it is COMPLETED,
    which three UI pages render as a percentage bar and one prints as "0%".

    ``STAGE_PROGRESS`` is this module's own documented status -> fraction map, so
    a terminal status reports the fraction it genuinely reached. Nothing is
    invented: a status with no entry in the map (STARTING/PROCESSING, the pool's
    in-flight states) keeps whatever was stored, and a non-zero stored value is
    never overwritten.
    """
    if not row:
        return row
    try:
        stored = float(row.get("progress") or 0.0)
    except (TypeError, ValueError):
        stored = 0.0
    if stored <= 0.0:
        mapped = STAGE_PROGRESS.get(str(row.get("status") or "").strip().upper())
        if mapped is not None:
            row["progress"] = mapped
    return row


class DuplicateJobError(RuntimeError):
    def __init__(self, existing_id: str | None):
        super().__init__("A correction job for this submission is already active.")
        self.existing_id = existing_id


class WaitForApiRetry(RuntimeError):
    def __init__(self, delay: float):
        super().__init__("All Gemini keys are cooling down.")
        self.delay = delay


# Total time a job may spend parked waiting for an API key before it is
# failed cleanly (cooldowns are capped at 10 min by the key manager, so two
# full waits always fit inside this budget).
API_WAIT_BUDGET_SECONDS = 15 * 60


class CancelledError(RuntimeError):
    pass


class JobManager:
    """Queue + worker pool + persistence + recovery."""

    def __init__(self, worker_count: int = 2, max_attempts: int = 3):
        self._wake = threading.Event()
        self._cancel_flags: dict[str, threading.Event] = {}
        self._stage_listeners: list = []
        self._worker_count = max(1, worker_count)
        self._max_attempts = max(1, max_attempts)
        self._handlers: dict[str, object] = {}
        self._cleanup_handler = None
        self._api_wait_total: dict[str, float] = {}
        self._shutdown = False

    # -- lifecycle -----------------------------------------------------------

    def start(self) -> None:
        for i in range(self._worker_count):
            threading.Thread(
                target=self._worker_loop, name=f"job-worker-{i}", daemon=True
            ).start()
        log_event("jobs", "worker pool started", workers=self._worker_count)

    def shutdown(self) -> None:
        self._shutdown = True
        self._wake.set()

    def set_handler(self, kind: str, handler) -> None:
        self._handlers[kind] = handler

    def set_cleanup_handler(self, handler) -> None:
        """Called with the job id when a job reaches a terminal state
        (COMPLETED / final FAILED / CANCELLED) so its local inputs can be
        removed. Never called while retries may still need the files."""
        self._cleanup_handler = handler

    def add_stage_listener(self, listener) -> None:
        self._stage_listeners.append(listener)

    # -- enqueue / query ------------------------------------------------------

    def enqueue(self, *, submission_id: str | None, coursework_id: str | None,
                course_id: str | None, student_id: str | None,
                student_name: str | None, is_remark: bool = False) -> dict:
        now_ts = time.time()
        job_id = uuid.uuid4().hex
        try:
            with db.transaction() as conn:
                conn.execute(
                    """
                    INSERT INTO correction_jobs(
                        id, submission_id, coursework_id, course_id, student_id,
                        student_name, status, created_at, updated_at, max_attempts,
                        is_remark)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (job_id, submission_id, coursework_id, course_id,
                     student_id, student_name, QUEUED, now_ts, now_ts,
                     self._max_attempts, 1 if is_remark else 0),
                )
        except sqlite3.IntegrityError as exc:
            existing = db.query_one(
                "SELECT id FROM correction_jobs WHERE submission_id = ? AND status IN "
                + "(%s)" % ",".join("?" * len(ACTIVE_STATUSES)),
                tuple([submission_id] + list(ACTIVE_STATUSES)),
            )
            raise DuplicateJobError(existing["id"] if existing else None) from exc
        job = self.get(job_id)
        log_event("jobs", "job enqueued", job_id=job_id,
                  submission_id=submission_id, student=student_name)
        self._publish_stage(job)
        self._wake.set()
        return job

    def get(self, job_id: str) -> dict | None:
        return _effective_progress(db.row_to_dict(db.query_one(
            "SELECT * FROM correction_jobs WHERE id = ?", (job_id,)
        )))

    def list(self, statuses: list[str] | None = None, limit: int = 200) -> list[dict]:
        if statuses:
            marks = ",".join("?" * len(statuses))
            rows = db.query(
                f"SELECT * FROM correction_jobs WHERE status IN ({marks}) "
                f"ORDER BY created_at DESC LIMIT ?",
                tuple(statuses) + (limit,),
            )
        else:
            rows = db.query(
                "SELECT * FROM correction_jobs ORDER BY created_at DESC LIMIT ?",
                (limit,),
            )
        return [_effective_progress(dict(r)) for r in rows]

    def counts(self) -> dict:
        rows = db.query(
            "SELECT status, COUNT(*) AS n FROM correction_jobs GROUP BY status"
        )
        return {r["status"]: r["n"] for r in rows}

    # -- worker -----------------------------------------------------------------

    def _worker_loop(self) -> None:
        while not self._shutdown:
            self._wake.wait(timeout=1.0)
            self._wake.clear()
            while True:
                job = self._claim_next()
                if job is None:
                    break
                self._run_job(job)

    def _claim_next(self) -> dict | None:
        with db.transaction() as conn:
            row = conn.execute(
                "SELECT * FROM correction_jobs WHERE status = ? ORDER BY created_at ASC LIMIT 1",
                (QUEUED,),
            ).fetchone()
            if row is None:
                return None
            conn.execute(
                "UPDATE correction_jobs SET status = ?, attempts = attempts + 1, "
                "started_at = COALESCE(started_at, ?), updated_at = ? WHERE id = ?",
                (PREPARING, time.time(), time.time(), row["id"]),
            )
        return self.get(row["id"])

    def _set_status(self, job_id: str, status: str, *,
                    stage_detail: str | None = None, error: str | None = None,
                    error_code: str | None = None,
                    finish: bool = False, result_id: str | None = None) -> None:
        sets = ["status = ?", "updated_at = ?"]
        params: list = [status, time.time()]
        if stage_detail is not None:
            sets.append("stage_detail = ?")
            params.append(stage_detail)
        if error is not None:
            sets.append("error = ?")
            params.append(error)
        if error_code is not None:
            sets.append("error_code = ?")
            params.append(error_code)
        if result_id is not None:
            sets.append("result_id = ?")
            params.append(result_id)
        if finish:
            sets.append("finished_at = ?")
            params.append(time.time())
        params.append(job_id)
        db.execute(f"UPDATE correction_jobs SET {', '.join(sets)} WHERE id = ?", params)
        if status in STAGE_PROGRESS:
            db.execute("UPDATE correction_jobs SET progress = ? WHERE id = ?",
                       (STAGE_PROGRESS[status], job_id))
        job = self.get(job_id)
        if job:
            self._publish_stage(job)
        if finish and self._cleanup_handler is not None:
            try:
                self._cleanup_handler(job_id)
            except Exception:
                log.exception("terminal cleanup failed for job %s", job_id)

    def set_stage(self, job_id: str, status: str, detail: str) -> None:
        self._set_status(job_id, status, stage_detail=detail)

    def _publish_stage(self, job: dict) -> None:
        for listener in list(self._stage_listeners):
            try:
                listener(job)
            except Exception:
                log.exception("stage listener failed")

    # -- execution -----------------------------------------------------------

    def _run_job(self, job: dict) -> None:
        job_id = job["id"]
        cancel = threading.Event()
        self._cancel_flags[job_id] = cancel

        handler_kind = "classroom_correction" if job["submission_id"] else "manual_correction"
        handler = self._handlers.get(handler_kind)

        try:
            if handler is None:
                raise RuntimeError(f"No handler registered for '{handler_kind}'")

            while True:
                try:
                    handler(job, cancel)
                    self._set_status(job_id, COMPLETED,
                                     stage_detail="Correction completed", finish=True)
                    return

                except WaitForApiRetry as exc:
                    # Infrastructure wait (not a correction failure): park the
                    # job until the key cooldown should have expired. Waiting
                    # does NOT consume the job's real retry attempts; the total
                    # wait is bounded by API_WAIT_BUDGET_SECONDS so a day-long
                    # quota exhaustion ends in a clean FAILED, never a loop.
                    delay = max(1.0, exc.delay)
                    waited_total = self._api_wait_total.get(job_id, 0.0)
                    if waited_total + delay > API_WAIT_BUDGET_SECONDS:
                        self._set_status(
                            job_id, FAILED, finish=True,
                            error="No Gemini API key became available within the "
                                  "waiting budget (15 min). The job is kept so you "
                                  "can retry it from Corrections once quota resets.",
                            error_code="all_keys_unavailable",
                        )
                        return
                    self._set_status(
                        job_id, WAITING_FOR_API,
                        stage_detail=f"All API keys are cooling down — retrying in ~{int(delay)}s",
                    )
                    remaining = delay
                    while remaining > 0:
                        if cancel.wait(timeout=1.0):
                            self._set_status(job_id, CANCELLED,
                                             stage_detail="Cancelled while waiting for an API key",
                                             finish=True)
                            return
                        remaining -= 1.0
                    self._api_wait_total[job_id] = waited_total + delay
                    # Give back the claim's attempt increment: waiting for an
                    # API key is not a failed correction attempt.
                    db.execute(
                        "UPDATE correction_jobs SET status = ?, "
                        "attempts = MAX(0, attempts - 1), stage_detail = ?, updated_at = ? WHERE id = ?",
                        (QUEUED, "Retrying after API cooldown", time.time(), job_id),
                    )
                    self._wake.set()
                    return

                except CancelledError:
                    self._set_status(job_id, CANCELLED,
                                     stage_detail="Cancelled by user", finish=True)
                    return

        except CancelledError:
            self._set_status(job_id, CANCELLED, stage_detail="Cancelled by user", finish=True)

        except Exception as exc:
            current = self.get(job_id)
            attempts = current["attempts"] if current else 1

            # Idempotency (§9): before any retry, check whether a successful
            # result already exists — never create a duplicate correction.
            existing_id = None
            if current and current.get("coursework_id") and current.get("submission_id"):
                row = db.query_one(
                    "SELECT id FROM results WHERE coursework_id = ? AND submission_id = ?",
                    (current["coursework_id"], current["submission_id"]),
                )
                existing_id = row["id"] if row else None
            if existing_id is not None and not cancel.is_set():
                log_event("jobs", "existing result reused — no re-run",
                          job_id=job_id, result_id=existing_id)
                self._set_status(
                    job_id, COMPLETED,
                    stage_detail="Reused existing correction result — no "
                                 "duplicate request sent",
                    result_id=existing_id, finish=True,
                )
                return

            # Classified retry (§8/§16): a temporary failure may retry with
            # prepared input reused; auth/invalid/context failures never do.
            from ..config import get_config
            failure = retry_policy.classify_failure(exc)
            decision = retry_policy.decide(
                failure,
                attempts=attempts,
                max_attempts=(current["max_attempts"] if current else 3) or 3,
                cfg_max_retries=get_config().worker.max_retries,
                waited_seconds=self._api_wait_total.get(job_id, 0.0),
            )

            if decision.action == "retry" and not cancel.is_set():
                delay = max(1.0, decision.delay)
                log_event("jobs", "job failed; will retry",
                          level=logging.WARNING,
                          job_id=job_id, error=str(exc), attempt=attempts,
                          error_class=failure.cls,
                          retry=f"{decision.retries_used}/{decision.allowed_retries}",
                          delay=round(delay, 1))
                self._set_status(
                    job_id, RETRYING,
                    stage_detail=(f"{failure.cls} — retry "
                                  f"{decision.retries_used + 1}/"
                                  f"{decision.allowed_retries} in "
                                  f"{int(delay)}s (prepared input reused)"),
                    error=str(exc)[:500],
                )
                if cancel.wait(timeout=delay):
                    self._set_status(job_id, CANCELLED,
                                     stage_detail="Cancelled during retry wait",
                                     finish=True)
                    return
                db.execute(
                    "UPDATE correction_jobs SET status = ?, updated_at = ? WHERE id = ?",
                    (QUEUED, time.time(), job_id),
                )
                self._wake.set()
            elif decision.action == "park" and not cancel.is_set():
                # Capacity wait: does NOT consume the attempt budget; total
                # parked time is bounded (decide() failed it otherwise).
                delay = max(1.0, decision.delay)
                log_event("jobs", "job parked for API capacity",
                          level=logging.WARNING, job_id=job_id,
                          error_class=failure.cls, delay=round(delay, 1))
                self._set_status(
                    job_id, WAITING_FOR_API,
                    stage_detail=(f"Waiting for API capacity — retrying in "
                                  f"~{int(delay)}s (no request sent meanwhile)"),
                )
                if cancel.wait(timeout=delay):
                    self._set_status(job_id, CANCELLED,
                                     stage_detail="Cancelled while waiting for an API key",
                                     finish=True)
                    return
                self._api_wait_total[job_id] = (
                    self._api_wait_total.get(job_id, 0.0) + delay)
                db.execute(
                    "UPDATE correction_jobs SET status = ?, "
                    "attempts = MAX(0, attempts - 1), stage_detail = ?, "
                    "updated_at = ? WHERE id = ?",
                    (QUEUED, "Retrying after API cooldown", time.time(), job_id),
                )
                self._wake.set()
            else:
                log_event("jobs", "job failed permanently", level=logging.ERROR,
                          job_id=job_id, error=str(exc),
                          error_class=failure.cls,
                          error_code=decision.error_code)
                self._set_status(job_id, FAILED, error=str(exc)[:500],
                                 error_code=decision.error_code or "job_failed",
                                 finish=True)
        finally:
            self._cancel_flags.pop(job_id, None)

    # -- control ---------------------------------------------------------------

    def cancel(self, job_id: str) -> bool:
        job = self.get(job_id)
        if job is None:
            return False
        if job["status"] in (COMPLETED, FAILED, CANCELLED):
            return False
        if job["status"] == QUEUED:
            self._set_status(job_id, CANCELLED, stage_detail="Cancelled before start", finish=True)
            return True
        flag = self._cancel_flags.get(job_id)
        if flag:
            flag.set()
        self._set_status(job_id, CANCELLED, stage_detail="Cancellation requested", finish=True)
        return True

    def retry(self, job_id: str) -> dict | None:
        job = self.get(job_id)
        if job is None or job["status"] not in (FAILED, CANCELLED):
            return None
        db.execute(
            "UPDATE correction_jobs SET status = ?, attempts = 0, error = NULL, "
            "error_code = NULL, finished_at = NULL, progress = 0, updated_at = ? WHERE id = ?",
            (QUEUED, time.time(), job_id),
        )
        self._wake.set()
        return self.get(job_id)

    # -- recovery ----------------------------------------------------------------

    def recover_on_boot(self) -> dict:
        """Restart-safety: requeue orphaned in-flight jobs; QUEUED jobs stay
        queued; terminal jobs are never blindly re-run (no duplicate marking).

        Covers every in-flight state the pool can leave in the DB (STARTING,
        PREPARING, DOWNLOADING, UPLOADING, MARKING, VERIFYING, SAVING,
        WAITING_FOR_API, RETRYING) plus anything the WorkerPool leaves while
        draining at shutdown. Duplicate prevention: the one-active-per-job
        index covers these statuses, and recover_on_boot is invoked on every
        startup, so a restarted app never silently re-marks a job.
        """
        recovered = 0
        failed = 0
        reused = 0
        rows = db.query(
            "SELECT id, status, attempts, max_attempts, submission_id, coursework_id "
            "FROM correction_jobs"
        )
        inflight = [s for s in ACTIVE_STATUSES if s not in (QUEUED,)]  # terminal set only
        for row in rows:
            if row["status"] in inflight:
                # Idempotency (§9): if a successful result already exists,
                # finish the job with it instead of re-marking after restart.
                if row["submission_id"] and row["coursework_id"]:
                    existing = db.query_one(
                        "SELECT id FROM results WHERE coursework_id = ? "
                        "AND submission_id = ?",
                        (row["coursework_id"], row["submission_id"]),
                    )
                    if existing:
                        db.execute(
                            "UPDATE correction_jobs SET status = ?, result_id = ?, "
                            "stage_detail = ?, updated_at = ? WHERE id = ?",
                            (COMPLETED, existing["id"],
                             "Recovered after restart — result already existed "
                             "(duplicate correction skipped)",
                             time.time(), row["id"]),
                        )
                        reused += 1
                        continue
                if row["attempts"] < row["max_attempts"]:
                    db.execute(
                        "UPDATE correction_jobs SET status = ?, stage_detail = ?, updated_at = ? WHERE id = ?",
                        (QUEUED, "Recovered after restart; requeued", time.time(), row["id"]),
                    )
                    recovered += 1
                else:
                    db.execute(
                        "UPDATE correction_jobs SET status = ?, error = ?, "
                        "stage_detail = NULL, updated_at = ? WHERE id = ?",
                        (FAILED, "Application restarted while processing", time.time(), row["id"]),
                    )
                    failed += 1
        if recovered:
            self._wake.set()
        log_event("jobs", "boot recovery complete", recovered=recovered,
                  failed=failed, reused_existing_result=reused)
        return {"recovered": recovered, "failed": failed,
                "reused_existing_result": reused}


# -- process-wide singleton ------------------------------------------------------

_manager: JobManager | None = None
_manager_lock = threading.Lock()


class JobManagerHolder:
    @staticmethod
    def instance() -> JobManager:
        global _manager
        with _manager_lock:
            if _manager is None:
                from ..config import get_config
                cfg = get_config()
                _manager = JobManager(
                    worker_count=cfg.worker.concurrency,
                    max_attempts=max(3, cfg.worker.max_retries + 1),
                )
            return _manager
