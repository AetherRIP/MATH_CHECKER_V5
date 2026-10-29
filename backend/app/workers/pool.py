"""WorkerPool — the V5 orchestrator (manager) around the trusted engine.

Runs N :class:`CorrectionWorker` instances (N configurable, auto-capped by
healthy Gemini keys). The pool:

  claims a QUEUED job  ->  stages inputs into the worker's private temp dir
                       ->  leases one exclusive healthy API key
                       ->  spawns the friend's correction code in the worker's
                           own process
                       ->  collects a structured WorkerResult
                       ->  persists it to the DB (results table unchanged)
                       ->  releases the key + cleans the workspace

Backpressure (§24/§26): the queue accepts everything, but only N jobs run at
once; Classroom-side enqueues are cheap DB rows, never in-flight PDF loads.
"""

from __future__ import annotations

import json
import logging
import shutil
import threading
import time
import uuid
from pathlib import Path

from .. import db
from .. import retry_policy
from ..config import data_dir, get_config
from ..engine import legacy_marking as legacy
from ..engine import remark, result_guard, versions
from ..engine.result_guard import assess_confidence, evaluate_overwrite
from ..engine.verify import verify_result
from ..engine.submission_validation import (
    assess_submission_quality, SUBMISSION_REVIEW_REQUIRED,
)
from ..gemini.key_manager import KeyManager, NoKeysError, WaitForApiError
from ..gemini.tester import KeyManagerHolder
from ..logging_setup import log_event
from .adapter import CorrectionWorker, WorkerJob, WorkerResult

log = logging.getLogger("workers.pool")


def cleanup_job_files(job_id: str) -> None:
    """Shared terminal-state cleanup (re-exported behaviour from the service)."""
    from ..engine.service import cleanup_job_files as _svc_cleanup
    _svc_cleanup(job_id)


def _plain_stage(message: str) -> str:
    """The engine's status text without emoji, for a desktop list column."""
    text = "".join(ch for ch in message if ch.isprintable()).strip()
    while text and not text[0].isalnum():
        text = text[1:].lstrip()
    return " ".join(text.split())[:200]


def _stage_status(message: str) -> str | None:
    """The pipeline status a stage message belongs to (None = keep current)."""
    text = message.lower()
    if "saving the result" in text:
        return "SAVING"
    if "checking questions" in text or "second correction" in text:
        return "VERIFYING"
    if "uploading" in text:
        return "UPLOADING"
    if "marking with" in text or "reading the mark scheme" in text \
            or "mark scheme read" in text:
        return "MARKING"
    return None


def _fmt_pct(pct: float | None) -> str:
    try:
        return f"{float(pct):.2f}%"
    except (TypeError, ValueError):
        return "?"


def _fmt_bytes(size: int | float | None) -> str:
    """A file size a teacher can read (never a raw byte count)."""
    try:
        value = max(0.0, float(size or 0))
    except (TypeError, ValueError):
        return "?"
    if value < 1024:
        return f"{int(value)} B"
    if value < 1024 * 1024:
        return f"{value / 1024:.0f} KB"
    return f"{value / (1024 * 1024):.1f} MB"


def _optimised_stage(reports: list, label: str) -> str:
    """One honest line about what will actually be uploaded.

    Aggregates multiple attachments so the percentage describes the whole
    upload, not one file. A document that kept its original is reported as
    such — the stage never implies a saving that did not happen.
    """
    originals = sum(int(getattr(r, "original_bytes", 0) or 0) for r in reports)
    uploads = sum(
        int(getattr(r, "optimized_bytes", 0) or 0)
        if getattr(r, "used", "original") in ("optimized", "cached")
        else int(getattr(r, "original_bytes", 0) or 0)
        for r in reports
    )
    used = [r for r in reports
            if getattr(r, "used", "original") in ("optimized", "cached")]
    if used and originals > uploads > 0:
        pct = (originals - uploads) / originals * 100
        return (f"{label} compressed — {_fmt_bytes(originals)} → "
                f"{_fmt_bytes(uploads)} ({pct:.0f}% smaller)")
    if originals:
        return f"{label} ready — {_fmt_bytes(uploads)} (already optimised)"
    return f"{label} ready"


def _safe_progress(job_id: str, *, stage: str | None = None,
                   progress: float | None = None,
                   status: str | None = None) -> None:
    """Publish a preparation stage without depending on the live layer.

    Compression must never be skipped because a progress event could not be
    delivered. The job manager is used when it exposes the reporter; a direct
    row update is the fallback, and both failing is still harmless.
    """
    try:
        from ..jobs.manager import JobManagerHolder
        reporter = getattr(JobManagerHolder.instance(), "report_progress", None)
        if reporter is not None:
            reporter(job_id, stage=stage, progress=progress, status=status)
            return
    except Exception:  # noqa: BLE001 — fall through to the row update
        pass
    try:
        now_ts = time.time()
        fields: list[str] = []
        values: list = []
        if stage is not None:
            fields.append("stage_detail = ?")
            values.append(stage[:300])
        if progress is not None:
            fields.append("progress = ?")
            values.append(progress)
        if status is not None:
            fields.append("status = ?")
            values.append(status)
        fields.append("updated_at = ?")
        values.extend((now_ts, job_id))
        db.execute(f"UPDATE correction_jobs SET {', '.join(fields)} WHERE id = ?",
                   tuple(values))
    except Exception:  # noqa: BLE001 — reporting is never load-bearing
        log.debug("preparation progress not recorded", exc_info=True)



# -- prepared-input manifest (§5/§6/§7/§10) ----------------------------------

_PREPARED_MANIFEST = "prepared.json"


def _read_prepared_manifest(cache_dir: Path) -> dict | None:
    try:
        raw = (cache_dir / _PREPARED_MANIFEST).read_text(encoding="utf-8")
        data = json.loads(raw)
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def _prepared_files_exist(prepared: dict, *, require_mark: bool = True) -> bool:
    submission = prepared.get("submission") or []
    if not submission:
        return False
    paths = list(submission)
    if require_mark:
        if not prepared.get("mark_scheme"):
            return False
        paths.append(prepared["mark_scheme"])
    elif prepared.get("mark_scheme"):
        paths.append(prepared["mark_scheme"])
    if prepared.get("question"):
        paths.append(prepared["question"])
    return all(Path(p).is_file() for p in paths)


def _write_prepared_manifest(cache_dir: Path, prepared: dict) -> None:
    """Atomic write — a half-written manifest must never be reused."""
    try:
        target = cache_dir / _PREPARED_MANIFEST
        tmp = cache_dir / (_PREPARED_MANIFEST + ".tmp")
        tmp.write_text(json.dumps(prepared, ensure_ascii=False),
                       encoding="utf-8")
        tmp.replace(target)
    except OSError:
        pass


def _record_scheme_audit(job: dict, result_id: str, result: dict,
                         now_ts: float) -> None:
    """Record substantiated mark-scheme disagreements from a pooled result.

    Mirrors the legacy service path through the SAME engine function, so the
    two save paths can never disagree about detection. A failure to record is
    logged, never allowed to fail the correction.
    """
    try:
        from ..engine import scheme_audit
        with db.transaction() as conn:
            scheme_audit.record_from_result(
                conn, job=job, result_id=result_id, result=result,
                now_ts=now_ts)
    except Exception:  # noqa: BLE001 - audit is a side channel
        logging.getLogger("workers.pool").exception(
            "mark-scheme discrepancy recording failed")


class WorkerPool:
    """Owns the worker set; round-robins QUEUED jobs onto idle workers."""

    #: How many automatic retries a job may make before it is STOPPED for
    #: diagnosis with its evidence intact. Retrying is not free: every attempt
    #: is a full request, and a loop of them is how one paper burned 60 starts.
    AUTO_RETRY_SUSPEND_AFTER = 2

    #: Capacity parks allowed before a job is stopped for diagnosis. Four waits
    #: for a cooldown that never clears is a provider problem, not patience.
    MAX_PARKS_BEFORE_SUSPEND = 4

    def __init__(self, worker_count: int, key_manager: KeyManager | None = None):
        self._lock = threading.RLock()
        self._wake = threading.Event()
        self._shutdown = False
        self.keys = key_manager or KeyManagerHolder.instance()
        self._workers: list[CorrectionWorker] = []
        self._metrics_lock = threading.Lock()
        self._job_seconds: list[float] = []
        self._queue_wait_seconds: list[float] = []
        self._retries_total = 0
        self._usage_blocked = False
        self._provider_blocked = False
        # Key-availability gate state (see _keys_ready).
        self._keys_blocked = False
        self._keys_next_check = 0.0
        self._keys_last_ready = True
        # Parked-wait accounting per job (spec §2/§3): total seconds a job
        # has spent waiting for API capacity. Bounds the WAITING_FOR_API
        # loop so an outage ends in a clean FAILED, never an infinite park.
        self._api_wait_total: dict[str, float] = {}
        # Capacity parks per job. Parks are cheap now (nothing is sent while
        # keys cool), but a job that has parked this many times is not waiting
        # for a cooldown — it is evidence that the provider cannot serve this
        # paper. Stop and keep the evidence instead of grinding the budget.
        self._park_counts: dict[str, int] = {}
        # Jobs the teacher explicitly asked to reprocess despite a stored
        # result (§23): the guard honours this as a deliberate override.
        self._force_reprocess: set[str] = set()
        # Live stage text and its start time per running job, for the elapsed
        # clock appended while a long AI request is in flight.
        self._active_stage: dict[str, tuple[str, float]] = {}
        self._heartbeat_at: dict[str, float] = {}
        # Terminal/raw-SQL transitions this loop has already pushed to the
        # live listeners, so a state is announced exactly once.
        self._published: dict[str, tuple] = {}
        self._publish_since: float = time.time() - 5.0
        self._thread: threading.Thread | None = None
        self.set_size(max(1, worker_count))

    # -- sizing (§5/§6) -------------------------------------------------------

    def set_size(self, n: int) -> int:
        """Grow or shrink the pool. Never exceeds available API capacity."""
        n = max(1, min(n, self.recommended_workers()))
        with self._lock:
            current = len(self._workers)
            if n > current:
                for i in range(current, n):
                    worker = CorrectionWorker(
                        worker_id=f"W{i + 1}",
                        workspace=data_dir() / "tmp" / f"worker-{i + 1:03d}",
                        key_manager=self.keys,
                    )
                    self._workers.append(worker)
                log_event("workers", "pool grown", workers=n)
            elif n < current:
                # Shrink from the end; stopping idle workers only (busy ones
                # finish their job first — handled by the scheduler loop).
                for worker in self._workers[n:]:
                    if worker.state.status != "PROCESSING":
                        worker.shutdown()
                self._workers = self._workers[:n]
                log_event("workers", "pool shrunk", workers=n)
        return len(self._workers)

    def recommended_workers(self) -> int:
        """Configured workers, capped by healthy API keys and sane bounds (§6)."""
        cfg = get_config()
        configured = max(1, cfg.worker.concurrency)
        healthy = max(0, self.keys.status()["healthy"])
        cap = max(1, min(8, configured))
        if healthy == 0:
            return cap  # let the WAITING_FOR_API path handle no-keys cleanly
        return max(1, min(cap, healthy))

    # -- scheduling loop (§4) ---------------------------------------------------

    def start(self) -> None:
        if self._thread is None or not self._thread.is_alive():
            self._shutdown = False
            # Revive workers parked in STOPPED by a previous shutdown so a
            # restarted pool can dispatch again.
            with self._lock:
                for worker in self._workers:
                    if worker.state.status == "STOPPED":
                        worker.state.status = "IDLE"
            self._thread = threading.Thread(
                target=self._loop, name="worker-pool", daemon=True)
            self._thread.start()
            log_event("workers", "pool started", workers=len(self._workers))

    def shutdown(self, drain: bool = True) -> None:
        """Graceful shutdown (§28): stop accepting, drain if allowed, cleanup."""
        self._shutdown = True
        self._wake.set()
        deadline = time.time() + (45 if drain else 0)
        while drain and time.time() < deadline:
            if all(w.state.status != "PROCESSING" for w in self._workers):
                break
            time.sleep(0.5)
        with self._lock:
            for worker in self._workers:
                worker.shutdown()
                shutil.rmtree(worker.workspace, ignore_errors=True)
        log_event("workers", "pool stopped")

    def _loop(self) -> None:
        while not self._shutdown:
            self._wake.wait(timeout=0.5)
            self._wake.clear()
            # One broken job must never kill the scheduling loop (a dead loop
            # silently stops every future correction).
            try:
                self._collect_finished()
                self._dispatch_queued()
                self._publish_changes()
            except Exception:
                log.exception("pool loop iteration failed")

    # -- dispatch -----------------------------------------------------------------

    def _dispatch_queued(self) -> None:
        with self._lock:
            idle = [w for w in self._workers if w.state.status == "IDLE"]
        if not idle:
            return
        # Network budget gate: when the hourly/daily internet-usage limit is
        # reached, new jobs stay QUEUED (running jobs finish; the budget
        # window rolls automatically).
        from ..net_usage import daily_over, hourly_over
        if hourly_over() or daily_over():
            self._usage_blocked = True
            return
        self._usage_blocked = False

        # Provider high-demand gate (§11): while the provider is cooling
        # down, dispatch NOTHING. Queued jobs (e.g. the remaining 55 of a
        # 75-student batch) wait locally — no Drive downloads, no uploads,
        # no model requests — instead of hammering a provider in outage.
        from ..gemini.provider_health import provider_health
        provider_ok, provider_wait = provider_health().available()
        if not provider_ok:
            if not self._provider_blocked:
                self._provider_blocked = True
                db.execute(
                    "UPDATE correction_jobs SET stage_detail = ?, updated_at = ? "
                    "WHERE status = ?",
                    (f"Provider overloaded — jobs paused for ~{int(provider_wait)}s",
                     time.time(), "QUEUED"),
                )
                log_event("workers", "dispatch paused (provider cooldown)",
                          level=logging.WARNING,
                          cooldown_seconds=round(provider_wait))
            return
        if self._provider_blocked:
            self._provider_blocked = False
            db.execute(
                "UPDATE correction_jobs SET stage_detail = NULL, updated_at = ? "
                "WHERE status = ?", (time.time(), "QUEUED"))
            log_event("workers", "dispatch resumed (provider healthy)")

        # Key-availability gate (§4): when every key is cooling down there is
        # nothing a worker can do — but the old behaviour started one anyway,
        # spawned a child, rebuilt the whole multimodal prompt and took the
        # refusal. One paper was logged starting 60 times that way. The queue
        # now stands still until a key is actually usable.
        if not self._keys_ready():
            return

        jobs = db.query(
            "SELECT * FROM correction_jobs WHERE status = ? ORDER BY created_at ASC",
            ("QUEUED",),
        )
        for job_row in jobs:
            if self._shutdown:
                return
            # Idempotency (§9): a RETRY whose successful result already
            # exists must not re-run the whole correction (e.g. the first
            # attempt actually succeeded but the result was written late).
            if ((job_row["attempts"] or 0) >= 1
                    and job_row["submission_id"] and job_row["coursework_id"]):
                existing = db.query_one(
                    "SELECT id FROM results WHERE coursework_id = ? AND submission_id = ?",
                    (job_row["coursework_id"], job_row["submission_id"]),
                )
                if existing:
                    db.execute(
                        "UPDATE correction_jobs SET status = ?, result_id = ?, "
                        "stage_detail = ?, finished_at = ?, updated_at = ? "
                        "WHERE id = ? AND status = ?",
                        ("COMPLETED", existing["id"],
                         "Result already existed — duplicate correction skipped",
                         time.time(), time.time(), job_row["id"], "QUEUED"),
                    )
                    log_event("workers", "retry skipped: result already exists",
                              job_id=job_row["id"],
                              student=job_row["student_name"],
                              result_id=existing["id"])
                    continue
            with self._lock:
                worker = next((w for w in self._workers if w.state.status == "IDLE"), None)
            if worker is None:
                return  # all busy — backpressure (§26)
            # Idempotency (§13): re-check under a write transaction.
            with db.transaction() as conn:
                row = conn.execute(
                    "SELECT status FROM correction_jobs WHERE id = ?", (job_row["id"],)
                ).fetchone()
                if row is None or row["status"] != "QUEUED":
                    continue  # claimed/cancelled elsewhere
                conn.execute(
                    "UPDATE correction_jobs SET status = ?, worker_id = ?, "
                    "attempts = attempts + 1, started_at = COALESCE(started_at, ?), "
                    "updated_at = ? WHERE id = ?",
                    ("STARTING", worker.worker_id, time.time(), time.time(), job_row["id"]),
                )
            if (job_row["attempts"] or 0) == 0:
                # A fresh (or re-armed) job starts with a clean capacity ledger:
                # the sleep counts from a previous episode must not shorten the
                # patience of a job the teacher deliberately queued again.
                self._park_counts.pop(job_row["id"], None)
            self._report(job_row["id"], status="PREPARING",
                         stage="Preparing the PDFs", progress=0.0)
            try:
                self._launch(worker, dict(job_row))
            except (WaitForApiError, NoKeysError) as exc:
                # No key usable right now. PARK when the problem is
                # capacity (cooldown / all leased — spec §16 question 4);
                # FAIL fast when no usable key is configured at all.
                failure = retry_policy.classify_failure(exc)
                if failure.park:
                    self._park_job(job_row["id"], self._capacity_delay(),
                                   reason="no key available yet")
                else:
                    db.execute(
                        "UPDATE correction_jobs SET status = ?, error = ?, stage_detail = NULL, "
                        "error_code = 'no_keys', finished_at = ?, updated_at = ? WHERE id = ?",
                        ("FAILED",
                         "No usable Gemini API keys. Add keys on the API Keys page "
                         "or re-enable a disabled key.",
                         time.time(), time.time(), job_row["id"]),
                    )
                    self._api_wait_total.pop(job_row["id"], None)
                with self._lock:
                    worker.mark_idle()
            except Exception as exc:
                from ..classroom.submission_validation import InvalidSubmissionError
                if isinstance(exc, InvalidSubmissionError):
                    log_event("workers", "invalid submission (no usable attachment)",
                              job_id=job_row["id"], error=str(exc)[:300])
                    db.execute(
                        "UPDATE correction_jobs SET status = ?, error = ?, "
                        "error_code = 'invalid_submission', finished_at = ?, "
                        "updated_at = ? WHERE id = ?",
                        ("FAILED", str(exc)[:500], time.time(), time.time(), job_row["id"]),
                    )
                    with self._lock:
                        worker.mark_idle()
                    continue
                # Classified dispatch failure (§8): deterministic input
                # problems fail fast; transient download/network errors get
                # ONE bounded retry with the prepared-input cache.
                failure = retry_policy.classify_failure(exc)
                decision = retry_policy.decide(
                    failure,
                    attempts=(job_row["attempts"] or 0) + 1,
                    max_attempts=job_row["max_attempts"] or 3,
                    cfg_max_retries=get_config().worker.max_retries,
                    waited_seconds=self._api_wait_total.get(job_row["id"], 0.0),
                )
                log_event("workers", "dispatch failure classified",
                          level=logging.WARNING, job_id=job_row["id"],
                          student=job_row["student_name"],
                          assignment=job_row["coursework_id"],
                          error_class=failure.cls,
                          action=decision.action,
                          error=str(exc)[:200])
                if decision.action == "park":
                    self._park_job(job_row["id"], decision.delay,
                                   reason=decision.reason)
                elif decision.action == "retry":
                    with self._metrics_lock:
                        self._retries_total += 1
                    try:
                        from ..api_stats import record_retry
                        record_retry(failure.cls)
                    except Exception:
                        pass
                    delay = max(1.0, decision.delay)
                    db.execute(
                        "UPDATE correction_jobs SET status = ?, stage_detail = ?, "
                        "error = ?, updated_at = ? WHERE id = ?",
                        ("RETRYING",
                         f"{failure.cls} — retrying in {int(delay)}s "
                         f"(prepared input reused)",
                         str(exc)[:500], time.time(), job_row["id"]),
                    )
                    threading.Timer(
                        delay,
                        lambda: self._requeue(job_row["id"]),
                    ).start()
                else:
                    db.execute(
                        "UPDATE correction_jobs SET status = ?, error = ?, "
                        "error_code = ?, finished_at = ?, updated_at = ? WHERE id = ?",
                        ("FAILED", str(exc)[:500],
                         decision.error_code or failure.error_code,
                         time.time(), time.time(), job_row["id"]),
                    )
                    cleanup_job_files(job_row["id"])
                    self._api_wait_total.pop(job_row["id"], None)
                with self._lock:
                    worker.mark_idle()

    def _launch(self, worker: CorrectionWorker, job: dict) -> None:
        paths = self._stage_inputs(job)

        cfg = get_config()
        # Offline demo / mock mode builds a MockGeminiClient in the child and
        # never contacts Gemini — so no API key is required. Leasing a key here
        # used to make the documented "demo without a key" impossible: every
        # correction failed with "No usable Gemini API keys".
        if cfg.mock_mode:
            fingerprint: str | None = None
            api_key = ""
            worker.state.fingerprint = None
        else:
            fingerprint = worker.lease_key()
            api_key = self._key_value(fingerprint)
        wjob = WorkerJob(
            job_id=job["id"],
            student_name=job["student_name"] or "Student",
            submission_paths=paths["submission"],
            mark_scheme_path=paths["mark_scheme"],
            question_paper_path=paths["question"],
            api_key=api_key,
            request_timeout_ms=cfg.gemini.request_timeout_ms,
            primary_models=list(cfg.gemini.primary_models),
            audit_enabled=False,
            leased_fingerprint=fingerprint,
            mock=cfg.mock_mode,
            # High-score double-check: never for remark jobs — a remark is
            # already a teacher-initiated re-check recorded as PENDING (§Remark).
            double_check_enabled=(cfg.double_check.enabled
                                  and not job.get("is_remark")),
        )
        worker.start_job(
            wjob,
            on_progress=lambda event: self._on_child_stage(job["id"], event),
        )
        # record queue-wait metric
        if job.get("created_at"):
            with self._metrics_lock:
                self._queue_wait_seconds.append(max(0.0, time.time() - job["created_at"]))
                self._queue_wait_seconds = self._queue_wait_seconds[-500:]

    def _stage_inputs(self, job: dict) -> dict:
        """Prepared-input cache for ONE correction job (spec §5/§6/§7/§10).

        The submission is downloaded from Drive, validated, normalized and
        the mark scheme is resolved exactly ONCE per job; the result is
        recorded in ``data/tmp/job-<id>/prepared.json``. Every retry or
        key-failover attempt reuses those prepared files — a failed API
        request never triggers another Drive download, another PDF
        normalization pass or another mark-scheme fetch. The whole cache is
        deleted at terminal cleanup (cleanup_job_files), so student PDFs are
        not retained permanently.
        """
        import shutil as _shutil
        from ..classroom.auth import get_access_token
        from ..classroom import sync as classroom_sync
        from ..classroom.client import download_drive_file

        cache_dir = data_dir() / "tmp" / f"job-{job['id']}"
        cache_dir.mkdir(parents=True, exist_ok=True)

        # 1. Reuse a previously prepared input set (retry/failover path).
        cached = _read_prepared_manifest(cache_dir)
        if cached is not None and _prepared_files_exist(cached, require_mark=True):
            n = len(cached.get("submission") or [])
            try:
                from ..api_stats import record_avoided, record_job_bandwidth
                if job.get("submission_id"):
                    record_avoided("downloads", n)
                record_avoided("reprepare", 1)
                # Per-job proof that the retry did NOT touch Drive (§17/§18).
                record_job_bandwidth(
                    job.get("id"),
                    avoided_downloads=(n if job.get("submission_id") else 0),
                    avoided_reprepare=1)
            except Exception:
                pass
            log_event("workers", "prepared input reused (no re-download)",
                      job_id=job["id"], student=job["student_name"],
                      files=n)
            summary = cached.get("optimization") or {}
            if summary.get("used"):
                _safe_progress(
                    job["id"],
                    stage=(f"Reusing the compressed PDFs from the last attempt — "
                           f"{_fmt_bytes(summary.get('original_bytes'))} → "
                           f"{_fmt_bytes(summary.get('upload_bytes'))} "
                           f"({summary.get('reduction_percent', 0):.0f}% smaller)"),
                    progress=0.12, status="PREPARING")
            else:
                _safe_progress(job["id"],
                               stage="Reusing the PDFs prepared for the last attempt",
                               progress=0.12, status="PREPARING")
            return {
                "submission": list(cached.get("submission") or []),
                "mark_scheme": cached.get("mark_scheme"),
                "question": cached.get("question"),
            }

        # 2. First attempt: prepare everything once.
        submission_paths: list[str] = []
        mark_scheme_path: str | None = None
        question_path: str | None = None

        if job["submission_id"]:
            token = get_access_token()
            sub_row = db.query_one(
                "SELECT attachment_ids FROM submissions WHERE id = ?",
                (job["submission_id"],))
            attachment_ids = db.loads(sub_row["attachment_ids"], []) if sub_row else []
            if not attachment_ids:
                subs = classroom_sync.sync_submissions(
                    job["course_id"], job["coursework_id"], force=True)
                for s in subs:
                    if s["id"] == job["submission_id"]:
                        attachment_ids = db.loads(s["attachment_ids"], [])
                        break
            if not attachment_ids:
                # Tracking-layer fallback: the legacy cache only holds
                # TURNED_IN rows, while tracking covers every handed-in state
                # (e.g. STUDENT_EDITED_AFTER_TURN_IN) with verified attachments.
                tracked = db.query_one(
                    "SELECT current_state, attachments_json FROM classroom_submissions "
                    "WHERE submission_id = ?", (job["submission_id"],))
                if tracked and tracked["current_state"] in (
                        "TURNED_IN", "STUDENT_EDITED_AFTER_TURN_IN"):
                    attachment_ids = [
                        str(a.get("id"))
                        for a in db.loads(tracked["attachments_json"], [])
                        if a.get("kind") == "drive" and a.get("id")
                    ]
            if not attachment_ids:
                raise RuntimeError("The submission has no Google Drive attachment to mark.")
            from ..classroom.submission_validation import (
                InvalidSubmissionError, validate_and_normalize,
            )
            validation_reasons: list[str] = []
            used_warnings: list[str] = []
            for idx, file_id in enumerate(attachment_ids):
                filename, data, mime = download_drive_file(token, file_id)
                try:
                    from ..api_stats import record_job_bandwidth
                    record_job_bandwidth(job.get("id"), download_count=1,
                                         download_bytes=len(data))
                except Exception:
                    pass
                check = validate_and_normalize(filename, data, mime, cache_dir)
                try:
                    from ..api_stats import record_job_bandwidth
                    record_job_bandwidth(job.get("id"), prepare_count=1)
                except Exception:
                    pass
                if not check["ok"]:
                    # A single bad attachment must never stop the class; collect
                    # the reason and try the student's other attachments.
                    validation_reasons.append(f"{filename}: {check['reason']}")
                    continue
                # validate_and_normalize already wrote normalized copy(ies)
                # into the cache dir; rename each to a per-index, per-submission
                # name so a multi-attachment submission lists files in Classroom
                # order. A ZIP expands to SEVERAL files, all of which belong to
                # this one attachment, so they are grouped under the same index.
                produced = check.get("paths") or [check["path"]]
                for part_no, produced_name in enumerate(produced):
                    out = cache_dir / (
                        f"submission_{idx}_{part_no}_"
                        f"{uuid.uuid4().hex[:8]}_{Path(produced_name).name}")
                    (cache_dir / produced_name).replace(out)
                    submission_paths.append(str(out))
                used_warnings.extend(check.get("warnings") or [])
            if not submission_paths:
                # Nothing usable → a distinct "Invalid submission" failure, NOT
                # a generic correction error, and never a zero mark.
                raise InvalidSubmissionError(
                    validation_reasons or ["no parseable attachment present"])
        else:
            rows = db.query(
                "SELECT kind, path FROM pending_manual_files WHERE job_id = ?", (job["id"],))
            for row in rows:
                src = Path(row["path"])
                if not src.exists():
                    continue
                dst = cache_dir / f"{row['kind']}_{uuid.uuid4().hex[:8]}_{src.name}"
                _shutil.copyfile(src, dst)
                if row["kind"] == "submission":
                    submission_paths.append(str(dst))
                elif row["kind"] == "mark_scheme":
                    mark_scheme_path = str(dst)
                elif row["kind"] == "question":
                    question_path = str(dst)
            if not submission_paths:
                raise RuntimeError("No uploaded submission file found for this job.")
            if mark_scheme_path is None:
                raise RuntimeError("No mark scheme uploaded for this correction.")

        # Mark scheme for classroom jobs: shared per-assignment PDF (read-only).
        if mark_scheme_path is None and job.get("coursework_id"):
            shared = data_dir() / "mark_schemes" / f"{job['coursework_id']}.pdf"
            if shared.exists():
                mark_scheme_path = str(shared)
            else:
                raise RuntimeError(
                    "No mark scheme has been uploaded for this assignment yet. "
                    "Upload it on the Classroom page before starting corrections.")

        # ---- ONE optimization step for BOTH documents (§ compression) ----
        # The staged files stay untouched (retention owns them); the model is
        # given a verified, smaller copy. A cached artifact is reused on every
        # retry and for every student sharing the same mark scheme.
        #
        # Reporting is deliberately OUTSIDE the failure guard: a progress
        # event that cannot be published must never cause the compression to
        # be skipped (that bug would have uploaded the original silently).
        submission_reports: list = []
        scheme_report = None
        question_report = None
        compression_cfg = get_config().pdf_compression
        allow_lossy = bool(compression_cfg.allow_lossy)
        try:
            from ..engine.pdf_optimize import optimize_pdf
        except Exception:  # noqa: BLE001 — dependency trouble: keep originals
            optimize_pdf = None
            log_event("workers", "pdf optimization unavailable",
                      level=logging.WARNING, job_id=job.get("id"))

        if not compression_cfg.enabled:
            _safe_progress(job["id"],
                           stage="PDF compression is off — using the originals",
                           progress=0.02, status="PREPARING")

        if optimize_pdf is not None and compression_cfg.enabled:
            if any(Path(p).suffix.lower() == ".pdf" for p in submission_paths):
                _safe_progress(job["id"], stage="Preparing the student's PDF",
                               progress=0.02, status="PREPARING")
            optimised: list[str] = []
            for p in submission_paths:
                if Path(p).suffix.lower() != ".pdf":
                    optimised.append(p)
                    continue
                try:
                    result = optimize_pdf(p, role="submission",
                                          job_id=job.get("id"),
                                          allow_lossy=allow_lossy)
                except Exception:  # noqa: BLE001 — never block a paper
                    optimised.append(p)
                    continue
                submission_reports.append(result.report)
                optimised.append(result.path)
            submission_paths = optimised
            if submission_reports:
                _safe_progress(job["id"],
                               stage=_optimised_stage(submission_reports,
                                                      "Student PDF"),
                               progress=0.05, status="PREPARING")

            if mark_scheme_path and Path(mark_scheme_path).suffix.lower() == ".pdf":
                _safe_progress(job["id"], stage="Preparing the mark scheme",
                               progress=0.06, status="PREPARING")
                try:
                    scheme_result = optimize_pdf(
                        mark_scheme_path, role="mark_scheme",
                        job_id=job.get("id"), allow_lossy=allow_lossy)
                    mark_scheme_path = scheme_result.path
                    scheme_report = scheme_result.report
                    _safe_progress(job["id"],
                                   stage=_optimised_stage([scheme_report],
                                                          "Mark scheme"),
                                   progress=0.08, status="PREPARING")
                except Exception:  # noqa: BLE001
                    pass
            if question_path and Path(question_path).suffix.lower() == ".pdf":
                try:
                    question_result = optimize_pdf(
                        question_path, role="mark_scheme", job_id=job.get("id"),
                        allow_lossy=allow_lossy)
                    question_path = question_result.path
                    question_report = question_result.report
                except Exception:  # noqa: BLE001
                    pass

        # One summary, stored with the prepared manifest so a retry can show
        # the same sizes without re-measuring anything.
        all_reports = submission_reports + [r for r in (scheme_report,
                                                        question_report) if r]
        original_total = sum(int(r.original_bytes or 0) for r in all_reports)
        upload_total = sum(
            int(r.optimized_bytes or 0)
            if r.used in ("optimized", "cached") else int(r.original_bytes or 0)
            for r in all_reports)
        used = any(r.used in ("optimized", "cached") for r in all_reports)
        reduction = (round((original_total - upload_total) / original_total * 100, 1)
                     if used and original_total > upload_total > 0 else 0.0)

        prepared = {"submission": submission_paths,
                    "mark_scheme": mark_scheme_path,
                    "question": question_path,
                    "optimization": {
                        "original_bytes": original_total,
                        "upload_bytes": upload_total,
                        "used": used,
                        "reduction_percent": reduction,
                    },
                    "created_at": time.time()}
        _write_prepared_manifest(cache_dir, prepared)
        _safe_progress(
            job["id"],
            stage=("Compressed PDFs ready — uploading them to Gemini"
                   if used else "PDFs ready — uploading to Gemini"),
            progress=0.12, status="PREPARING")
        return {"submission": submission_paths, "mark_scheme": mark_scheme_path,
                "question": question_path}

    def _key_value(self, fingerprint: str) -> str:
        from ..config import get_secrets
        from ..gemini.key_manager import fingerprint as fp_of
        for key in get_secrets().gemini_keys():
            if fp_of(key) == fingerprint:
                return key
        raise WaitForApiError("Leased key no longer matches any configured key.")

    # -- collection ------------------------------------------------------------------

    def _collect_finished(self) -> None:
        with self._lock:
            busy = [w for w in self._workers if w.state.status == "PROCESSING"]
        self._heartbeat(busy)
        for worker in busy:
            try:
                result = worker.poll_result(timeout=0.0)
            except Exception:
                log.exception("poll failed for %s", worker.worker_id)
                continue
            if result is None:
                continue
            self._active_stage.pop(worker.state.current_job_id or "", None)
            try:
                self._handle_result(worker, result)
            except Exception:
                # Never lose the key lease or the worker slot because of a
                # persistence error: report, release, and keep the pool alive.
                log.exception("result handling failed for job %s", result.job_id)
                try:
                    worker.release_key(success=False,
                                       error=RuntimeError("result persistence failed"))
                except Exception:
                    pass
                self._cleanup_workspace(worker)
                worker.mark_idle()

    # -- live progress ------------------------------------------------------

    def _report(self, job_id: str, *, stage: str | None = None,
                progress: float | None = None, status: str | None = None,
                heartbeat: bool = False) -> None:
        """Publish one genuine stage transition to the job row and listeners.

        Uses the JobManager so ``/jobs/stream`` hears it: writing the row with
        raw SQL here (as this pool used to) left the desktop showing the state
        it heard at enqueue time for the whole run.
        """
        now_ts = time.time()
        if not hasattr(self, "_active_stage"):
            # A bare instance (unit tests exercise staging without starting
            # the pool) still reports; progress state is optional, not a
            # precondition for preparing a job.
            self._active_stage = {}
        if stage is not None and not heartbeat:
            # Only a genuine stage change restarts the elapsed clock; a
            # heartbeat must append to the same base text, never to its own
            # previous output.
            self._active_stage[job_id] = (stage, now_ts)
        try:
            from ..jobs.manager import JobManagerHolder
            JobManagerHolder.instance().report_progress(
                job_id, stage=stage, progress=progress, status=status,
                # A heartbeat only re-stamps the elapsed clock; recording it
                # would fill the stage history with the same stage 12 times.
                record_event=not heartbeat)
        except Exception:  # noqa: BLE001 — progress must never fail a job
            log.exception("progress report failed for %s", job_id)
            if stage is not None:
                db.execute(
                    "UPDATE correction_jobs SET stage_detail = ?, updated_at = ? "
                    "WHERE id = ?", (stage[:300], now_ts, job_id))

    def _on_child_stage(self, job_id: str, event: dict) -> None:
        """A stage the correction engine reported from inside the child."""
        message = str(event.get("message") or event.get("stage") or "").strip()
        if not message:
            return
        fraction = event.get("progress")
        try:
            progress = float(fraction) if fraction is not None else None
        except (TypeError, ValueError):
            progress = None
        plain = _plain_stage(message)
        self._report(job_id, stage=plain, progress=progress,
                     status=_stage_status(plain))

    def _publish_changes(self) -> None:
        """Announce state written outside :meth:`_report` (terminal states,
        retries, parks and cancellations) to the live listeners.

        The pool writes those rows itself, so without this sweep the desktop
        would hear about them only when it refreshed — the same class of bug
        that left a running job looking frozen.
        """
        now_ts = time.time()
        try:
            rows = db.query(
                "SELECT id, status, stage_detail, progress FROM correction_jobs "
                "WHERE updated_at >= ?", (self._publish_since - 2.0,))
        except Exception:  # noqa: BLE001
            return
        for row in rows:
            key = (row["status"], row["stage_detail"], row["progress"])
            if self._published.get(row["id"]) == key:
                continue
            self._published[row["id"]] = key
            try:
                from ..jobs.manager import JobManagerHolder
                JobManagerHolder.instance().publish(row["id"])
            except Exception:  # noqa: BLE001
                pass
        if len(self._published) > 500:
            self._published = dict(list(self._published.items())[-200:])
        self._publish_since = now_ts

    def _heartbeat(self, busy: list) -> None:
        """Keep an honest 'still working' clock on the current stage.

        While a single AI request runs there is no measurable sub-progress, so
        the stage keeps its real name and only the elapsed time is appended —
        real information, no invented percentage. The stage text is only
        rewritten every few seconds, never on every loop tick.
        """
        now_ts = time.time()
        for worker in busy:
            job_id = worker.state.current_job_id or ""
            entry = self._active_stage.get(job_id)
            if entry is None:
                continue
            base, started = entry
            elapsed = now_ts - started
            if elapsed < 5 or now_ts - self._heartbeat_at.get(job_id, 0.0) < 5:
                continue
            self._heartbeat_at[job_id] = now_ts
            minutes, seconds = divmod(int(elapsed), 60)
            clock = f"{minutes}m {seconds:02d}s" if minutes else f"{seconds}s"
            self._report(job_id, stage=f"{base} · {clock}", heartbeat=True)

    @staticmethod
    def _resolve_student_identity(job: dict) -> tuple[str | None, str | None]:
        """The student id and canonical name to store on the result row.

        A nickname is a local display preference, so the stored
        ``student_name`` stays the Google Classroom name: reports and history
        keep the official identity, and the UI resolves the nickname from the
        student id on read. Identity is resolved from Classroom ids only.
        """
        from .. import student_identity

        job_student_id = (job.get("student_id") or "").strip() or None
        job_name = (job.get("student_name") or "").strip() or None
        identity = student_identity.resolve(
            job_student_id, submission_id=job.get("submission_id"),
            snapshot_name=job_name)
        # Prefer the job's own snapshot when it agrees with the live record;
        # it is what the correction was actually made against.
        classroom_name = identity["classroom_name"] or job_name
        return identity["student_id"], classroom_name

    # -- high-score double-check (§1-§21) -------------------------------------

    @staticmethod
    def _resolve_double_check(result: WorkerResult) -> dict:
        """Combine the child's independent double-check outcome into the state
        to persist. Shared comparison logic lives in
        ``engine.double_check.combine`` so both save paths agree."""
        from ..engine import double_check
        if (not result.double_check_triggered
                and result.second_result is None
                and not result.double_check_error):
            return {
                "triggered": False,
                "status": double_check.STATUS_NOT_TRIGGERED,
                "disagreement_status": double_check.DISAGREE_NONE,
                "review_required": False, "reason": None,
                "first": None, "second": None, "second_result": None,
                "second_marking_model": None, "final_result": None,
                "final": None, "error": None, "question_differences": [],
                "verification": None,
            }
        from ..engine.mark_integrity import enforce_evidence
        result.result, _integrity_first = enforce_evidence(result.result)
        if result.second_result is not None:
            result.second_result, _integrity_second = enforce_evidence(
                result.second_result)
        combined = double_check.combine(
            result.result, result.second_result,
            second_marking_model=result.second_marking_model,
            error=result.double_check_error,
            first_marking_model=result.marking_model)
        combined["verification"] = None
        if result.second_result is not None:
            combined["verification"] = verify_result(result.second_result)
        return combined

    @staticmethod
    def _record_double_check_versions(conn, *, result_id: str, dc: dict,
                                      result: WorkerResult,
                                      verification_first: dict,
                                      verification_final: dict,
                                      confidence: str,
                                      confidence_reasons: list[str],
                                      now_ts: float) -> tuple[str | None, str | None]:
        """Preserve BOTH corrections (§4) via the SHARED persistence helper so
        this path and the legacy service path can never drift apart."""
        from ..engine import double_check
        return double_check.record_persistence(
            conn, result_id=result_id, first_result=result.result,
            first_verification=verification_first, dc=dc,
            marking_model=result.marking_model,
            second_marking_model=dc["second_marking_model"],
            key_fingerprint=result.key_fingerprint, confidence=confidence,
            confidence_reasons=confidence_reasons, now_ts=now_ts,
            second_verification=dc["verification"])

    def _handle_result(self, worker: CorrectionWorker, result: WorkerResult) -> None:
        job_id = result.job_id
        # What this attempt spent, FIRST: a failed attempt is exactly the one
        # whose cost must be visible (3,922 failed attempts against 456
        # successes were previously recorded as "0 AI requests").
        if result.ai_usage:
            try:
                from ..api_stats import record_ai_usage
                record_ai_usage(job_id, result.ai_usage)
            except Exception:
                pass
        job = db.row_to_dict(db.query_one(
            "SELECT * FROM correction_jobs WHERE id = ?", (job_id,)))

        if result.ok and result.result is not None and job is not None:
            # Persist exactly like the single-worker path did (§17/§23):
            # legacy normalised result + deterministic verification + totals,
            # now behind the result guard so a suspicious run can never
            # silently overwrite a valid stored result (§7).
            from ..engine.mark_integrity import enforce_evidence
            result.result, integrity_report = enforce_evidence(result.result)
            if (integrity_report["changed"]
                    or integrity_report["review_questions"]):
                result.blueprint_report = dict(result.blueprint_report or {})
                result.blueprint_report["integrity"] = integrity_report
                result.blueprint_report["review_required"] = True
                result.blueprint_report["review_warnings"] = (
                    list(result.blueprint_report.get("review_warnings") or [])
                    + list(integrity_report["warnings"]))
            verification_first = verify_result(result.result, result.blueprint_report)
            verification = verification_first
            earned, available, percentage = legacy.calculate_totals(result.result)
            # High-score double-check (§4-§6): combine the child's INDEPENDENT
            # second correction BEFORE anything is persisted. ``earned`` and
            # friends below become the FINAL result; the first/second raw
            # results are preserved separately and never overwritten.
            dc = self._resolve_double_check(result)
            if dc["triggered"]:
                if dc["final_result"] is not result.result:
                    verification = dc["verification"]
                earned, available, percentage = dc["final"]
            final_result = dc["final_result"] if dc["triggered"] else result.result
            # Submission-quality validation (§9): blank / unreadable / wrong-
            # assignment submissions must never become a trusted score. The
            # AI cannot reliably self-report these — a near-blank scan marked
            # "honestly" produces a 0/150 that looks like a verified failure.
            submission_quality = assess_submission_quality(
                result.result, verification_first)
            # The model that actually marked this paper must be named: when a
            # 503/timeout pushes the chain onto the fallback model, the marks
            # are not the ones the primary would have given (measured 70 vs 76
            # on the same paper), so the result is stored but flagged instead
            # of passing as routine.
            configured = list(get_config().gemini.primary_models or [])
            confidence, confidence_reasons = assess_confidence(
                final_result, verification, earned, available,
                marking_model=result.marking_model,
                primary_model=configured[0] if configured else None)
            if submission_quality["review"]:
                confidence_reasons = list(confidence_reasons) + [
                    f"Submission quality ({submission_quality['quality']}): {r}"
                    for r in submission_quality["reasons"]]
                if submission_quality["quality"] == SUBMISSION_REVIEW_REQUIRED:
                    confidence = SUBMISSION_REVIEW_REQUIRED
            # Student identity must reach the result row (§16). A job enqueued
            # without it used to produce a result with no student id at all,
            # which left the correction result page with no name to show and
            # no stable key to look one up by. Recover it from the submission
            # (stable Classroom ids only) and store the canonical name.
            student_id, student_name = self._resolve_student_identity(job)
            result_id = uuid.uuid4().hex
            now_ts = time.time()
            keep_existing_id: str | None = None
            with db.transaction() as conn:
                # Re-correcting a Classroom submission must UPDATE the previous
                # result (unique index on coursework+submission), not crash the
                # pool on an IntegrityError. Fetch the stored row (including the
                # guard columns) so the overwrite decision sees it.
                existing = None
                if job["coursework_id"] and job["submission_id"]:
                    existing = conn.execute(
                        "SELECT * FROM results WHERE coursework_id = ? "
                        "AND submission_id = ?",
                        (job["coursework_id"], job["submission_id"]),
                    ).fetchone()
                decision = evaluate_overwrite(
                    existing=dict(existing) if existing else None,
                    earned=earned, available=available, verification=verification,
                    confidence=confidence, confidence_reasons=confidence_reasons,
                    force=job_id in self._force_reprocess,
                )
                # Teacher review when EITHER the result guard OR the
                # double-check comparison asks for it (§6: large disagreement
                # or a failed check is never silently applied).
                from ..config import is_trusted_marking_model
                review_flag = 1 if (
                    decision.review_required
                    or dc["review_required"]
                    or verification.get("review_required")
                    or verification.get("status") not in (None, "VALID")
                    or not is_trusted_marking_model(result.marking_model)
                ) else 0
                if job.get("is_remark") and existing:
                    # ---- remark: record the attempt, apply NOTHING (§Remark)
                    # The re-mark is stored in full as a PENDING attempt so the
                    # teacher can compare it, while the active correction stays
                    # exactly as it was. A remark therefore cannot destroy a
                    # valid result even if this run scored worse or failed
                    # validation - it is simply surfaced with its confidence.
                    result_id = existing["id"]
                    keep_existing_id = result_id
                    versions.record_pending(
                        conn, result_id=result_id, earned=earned,
                        available=available, percentage=percentage,
                        provisional_grade=legacy.percentage_band_grade(percentage),
                        marking_model=result.marking_model,
                        key_fingerprint=result.key_fingerprint,
                        confidence=decision.confidence,
                        confidence_reasons=decision.confidence_reasons,
                        verification=verification, result=result.result,
                        created_at=now_ts, note=remark.NOTE_REMARK,
                    )
                    conn.execute(
                        "UPDATE correction_jobs SET status = ?, result_id = ?, "
                        "stage_detail = ?, finished_at = ?, updated_at = ? WHERE id = ?",
                        ("COMPLETED", result_id,
                         f"Remark recorded — awaiting teacher decision ({decision.confidence})",
                         now_ts, now_ts, job_id),
                    )
                elif existing and decision.action == result_guard.ACTION_KEEP_EXISTING:
                    # Keep the strongest validated result; flag for human review.
                    result_id = existing["id"]
                    keep_existing_id = result_id
                    conn.execute(
                        "UPDATE results SET review_required = 1, confidence = ? WHERE id = ?",
                        (decision.confidence, result_id),
                    )
                    if dc["triggered"]:
                        # Preserve the double-check evidence WITHOUT applying
                        # it — the stored result stays exactly as it was.
                        f_e, f_a, f_p = dc["first"]
                        versions.record_pending(
                            conn, result_id=result_id, earned=f_e,
                            available=f_a, percentage=f_p,
                            provisional_grade=legacy.percentage_band_grade(f_p),
                            marking_model=result.marking_model,
                            key_fingerprint=result.key_fingerprint,
                            confidence=decision.confidence,
                            confidence_reasons=decision.confidence_reasons,
                            verification=verification, result=result.result,
                            created_at=now_ts,
                            note=("High-score double-check attempt "
                                  f"({dc['status']}) — stored result kept: "
                                  + (dc["reason"] or "")[:200]),
                        )
                    conn.execute(
                        "UPDATE correction_jobs SET status = ?, result_id = ?, "
                        "stage_detail = ?, finished_at = ?, updated_at = ? WHERE id = ?",
                        ("COMPLETED", result_id, f"Kept existing result — {decision.reason}",
                         now_ts, now_ts, job_id),
                    )
                elif existing:
                    result_id = existing["id"]
                    conn.execute(
                        """
                        UPDATE results SET
                            job_id = ?, earned = ?, available = ?, percentage = ?,
                            provisional_grade = ?, marking_model = ?,
                            verification_json = ?, result_json = ?,
                            processing_seconds = ?, key_fingerprint = ?,
                            confidence = ?, confidence_reasons = ?,
                            review_required = ?, manual_override = 0,
                            created_at = ?,
                            student_id = COALESCE(NULLIF(?, ''), student_id),
                            student_name = COALESCE(NULLIF(?, ''), student_name)
                        WHERE id = ?
                        """,
                        (job_id, earned, available, percentage,
                         legacy.percentage_band_grade(percentage),
                         result.marking_model,
                         json.dumps(verification, ensure_ascii=False),
                         json.dumps(final_result, ensure_ascii=False),
                         result.processing_seconds, result.key_fingerprint,
                         decision.confidence,
                         json.dumps(decision.confidence_reasons, ensure_ascii=False),
                         review_flag,
                         now_ts,
                         # Fill a lost identity without ever blanking a good one.
                         student_id or "", student_name or "",
                         result_id),
                    )
                else:
                    conn.execute(
                        """
                        INSERT INTO results(
                            id, job_id, coursework_id, submission_id, student_id,
                            student_name, earned, available, percentage,
                            provisional_grade, marking_model, verification_json,
                            result_json, processing_seconds, key_fingerprint,
                            confidence, confidence_reasons, review_required,
                            manual_override, created_at)
                        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,0,?)
                        """,
                        (result_id, job_id, job["coursework_id"], job["submission_id"],
                         student_id, student_name, earned, available,
                         percentage, legacy.percentage_band_grade(percentage),
                         result.marking_model,
                         json.dumps(verification, ensure_ascii=False),
                         json.dumps(final_result, ensure_ascii=False),
                         result.processing_seconds, result.key_fingerprint,
                         decision.confidence,
                         json.dumps(decision.confidence_reasons, ensure_ascii=False),
                         review_flag, now_ts),
                    )
                if not keep_existing_id:
                    # High-score double-check (§4): preserve BOTH corrections
                    # in the append-only history BEFORE the final snapshot, so
                    # neither result is ever lost and the current version is
                    # always the FINAL one.
                    first_vid = second_vid = None
                    if dc["triggered"]:
                        # Records BOTH snapshots + the double-check columns via
                        # the shared helper (§4/§16).
                        first_vid, second_vid = self._record_double_check_versions(
                            conn, result_id=result_id, dc=dc, result=result,
                            verification_first=verification_first,
                            verification_final=verification,
                            confidence=decision.confidence,
                            confidence_reasons=decision.confidence_reasons,
                            now_ts=now_ts)
                    # Append-only version history (§D): keep every accepted
                    # state so a recheck never destroys the previous result.
                    versions.record(
                        conn, result_id=result_id, earned=earned,
                        available=available, percentage=percentage,
                        provisional_grade=legacy.percentage_band_grade(percentage),
                        marking_model=result.marking_model,
                        key_fingerprint=result.key_fingerprint,
                        confidence=decision.confidence,
                        confidence_reasons=decision.confidence_reasons,
                        verification=verification, result=final_result,
                        created_at=now_ts,
                        note=("Correction saved" if not dc["triggered"]
                              else "Final result after high-score double-check"),
                    )
                    detail = "Correction completed"
                    if dc["triggered"]:
                        detail = (f"Correction completed — high-score double-check "
                                  f"{dc['status'].replace('_', ' ').lower()} "
                                  f"(first {_fmt_pct(dc['first'][2])}, "
                                  f"final {_fmt_pct(percentage)})")
                    conn.execute(
                        "UPDATE correction_jobs SET status = ?, result_id = ?, "
                        "stage_detail = ?, finished_at = ?, updated_at = ? WHERE id = ?",
                        ("COMPLETED", result_id, detail, now_ts, now_ts, job_id),
                    )
            _record_scheme_audit(job, result_id, final_result, now_ts)
            worker.state.jobs_done += 1
            cleanup_job_files(job_id)
            self._api_wait_total.pop(job_id, None)
            self._force_reprocess.discard(job_id)
            with self._metrics_lock:
                self._job_seconds.append(result.processing_seconds)
                self._job_seconds = self._job_seconds[-500:]
            log_event("workers", "job completed", worker_id=worker.worker_id,
                      job_id=job_id, student=job["student_name"],
                      earned=earned, available=available,
                      model=result.marking_model,
                      fingerprint=result.key_fingerprint,
                      blueprint=(result.blueprint_report or {}).get("source"),
                      correction_status=verification.get("status"),
                      confidence=decision.confidence,
                      guard_action=decision.action,
                      double_check=(dc["status"] if dc["triggered"] else None),
                      double_check_first=(dc["first"][2] if dc["triggered"] else None),
                      double_check_second=(dc["second"][2] if dc["triggered"] and dc["second"] else None),
                      double_check_final=(percentage if dc["triggered"] else None),
                      disagreement=(dc["disagreement_status"] if dc["triggered"] else None),
                      review_required=bool(review_flag))
            if dc["triggered"]:
                # §18: log the trigger, both scores, the difference, the final
                # score and the status — never keys, tokens or file contents.
                log_event("workers", "high-score double-check",
                          job_id=job_id, student=job["student_name"],
                          first=f"{dc['first'][0]}/{dc['first'][1]}",
                          second=(f"{dc['second'][0]}/{dc['second'][1]}"
                                  if dc["second"] else None),
                          final=f"{earned}/{available}",
                          difference_points=round(dc["first"][2]
                                                  - (dc["second"][2] if dc["second"] else 0), 2),
                          status=dc["status"],
                          disagreement=dc["disagreement_status"])
        else:
            self._handle_failure(worker, job, result)

        worker.release_key(success=result.ok, error=None if result.ok else
                           RuntimeError(result.error or "failed"),
                           child_recorded=result.key_recorded)
        self._cleanup_workspace(worker)
        worker.mark_idle()
        self._wake.set()

    def _handle_failure(self, worker: CorrectionWorker, job: dict | None,
                        result: WorkerResult) -> None:
        if job is None:
            return
        worker.state.jobs_failed += 1
        worker.state.errors += 1
        worker.state.last_error = (result.error or "")[:200]

        # 1. Classify BEFORE deciding anything (spec §8/§16): why did it
        #    fail, is it retryable, should we wait or switch keys?
        failure = retry_policy.failure_for_kind(result.error_kind,
                                                result.error or "")

        # 2. Idempotency (§9): if a successful result already exists for
        #    this submission, reuse it instead of re-running the whole
        #    correction (a duplicate correction must never be created).
        existing_id = self._existing_result_id(job)
        if existing_id is not None:
            self._complete_with_existing(job, existing_id,
                                         via="failure retry guard")
            return

        cfg = get_config()
        attempts = job["attempts"] or 1

        # 3. Park hints follow real capacity signals (never a blind sleep).
        park_hint = None
        if failure.cls == retry_policy.HIGH_DEMAND:
            from ..gemini.provider_health import provider_health
            remaining = provider_health().remaining()
            park_hint = (remaining + 5.0) if remaining > 0 else None
        elif failure.park:
            park_hint = self._capacity_delay()

        decision = retry_policy.decide(
            failure,
            attempts=attempts,
            max_attempts=job["max_attempts"] or 3,
            cfg_max_retries=cfg.worker.max_retries,
            waited_seconds=self._api_wait_total.get(job["id"], 0.0),
            park_hint_delay=park_hint,
        )

        # 4. Structured failure log (§14): student, assignment, provider,
        #    key fingerprint (NEVER the key itself), class, retry N/M, action.
        log_event(
            "workers", "api failure classified", level=logging.WARNING,
            job_id=job["id"], student=job["student_name"],
            assignment=job.get("coursework_id"), provider="gemini",
            key=result.key_fingerprint, error_class=failure.cls,
            error=(result.error or "")[:200],
            retry=f"{decision.retries_used}/{decision.allowed_retries}",
            action=decision.action, next_action=decision.reason,
            delay=round(decision.delay, 1),
        )

        # Stop-on-retry: every retry condition is recorded as a diagnosis BEFORE
        # another attempt may be scheduled, and a job that has already spent its
        # automatic retries is stopped with its evidence intact instead of being
        # sent again. The teacher resumes it deliberately from Corrections.
        from ..jobs.manager import record_stage_event
        record_stage_event(
            job["id"], status=job["status"],
            stage=(f"Diagnosed {failure.cls} (attempt {attempts}/"
                   f"{job['max_attempts']}): {decision.reason[:90]}"),
            progress=None)
        if (decision.action == "retry"
                and decision.retries_used >= self.AUTO_RETRY_SUSPEND_AFTER):
            record_stage_event(
                job["id"], status="FAILED",
                stage=f"Stopped for diagnosis after {decision.retries_used} retries",
                progress=None)
            db.execute(
                "UPDATE correction_jobs SET status = ?, stage_detail = ?, "
                "error_code = 'retry_suspended', finished_at = ?, updated_at = ? "
                "WHERE id = ?",
                ("FAILED",
                 f"Stopped for diagnosis after {decision.retries_used} automatic "
                 f"retries ({failure.cls}) — evidence kept; retry when you have "
                 f"checked why it failed",
                 time.time(), time.time(), job["id"]),
            )
            # The prepared input and stage history are NOT cleaned up: they are
            # the evidence.
            self._api_wait_total.pop(job["id"], None)
            log_event("workers", "job stopped for diagnosis (no further retries)",
                      level=logging.ERROR, worker_id=worker.worker_id,
                      job_id=job["id"], student=job.get("student_name"),
                      error_class=failure.cls, retries=decision.retries_used,
                      error=(result.error or "")[:300])
            return

        if decision.action == "park":
            self._park_job(job["id"], decision.delay, reason=decision.reason)
            return

        if decision.action == "retry":
            # Reuses the prepared-input cache — NO Drive re-download, NO PDF
            # re-normalization, NO mark-scheme refetch on the next attempt.
            with self._metrics_lock:
                self._retries_total += 1
            try:
                from ..api_stats import record_retry
                record_retry(failure.cls)
            except Exception:
                pass
            delay = max(1.0, decision.delay)
            db.execute(
                "UPDATE correction_jobs SET status = ?, stage_detail = ?, error = ?, updated_at = ? WHERE id = ?",
                ("RETRYING",
                 f"{failure.cls} — retry {decision.retries_used + 1}/"
                 f"{decision.allowed_retries} in {int(delay)}s "
                 f"(prepared input reused)",
                 (result.error or "")[:500], time.time(), job["id"]),
            )
            threading.Timer(delay, lambda: self._requeue(job["id"])).start()
            log_event("workers", "job scheduled for retry",
                      worker_id=worker.worker_id, job_id=job["id"],
                      student=job["student_name"], error_class=failure.cls,
                      attempt=attempts, delay=delay)
            return

        # 5. FAIL permanently — never loop (spec §2).
        db.execute(
            "UPDATE correction_jobs SET status = ?, stage_detail = NULL, error = ?, "
            "error_code = ?, finished_at = ?, updated_at = ? WHERE id = ?",
            ("FAILED", (result.error or "Unknown worker failure")[:500],
             decision.error_code or failure.error_code, time.time(), time.time(),
             job["id"]),
        )
        cleanup_job_files(job["id"])
        self._api_wait_total.pop(job["id"], None)
        log_event("workers", "job failed permanently", level=logging.ERROR,
                  worker_id=worker.worker_id, job_id=job["id"],
                  student=job.get("student_name"),
                  assignment=job.get("coursework_id"), provider="gemini",
                  key=result.key_fingerprint, error_class=failure.cls,
                  error_code=decision.error_code or failure.error_code,
                  retries_used=decision.retries_used,
                  error=(result.error or "")[:300])

    def _cleanup_workspace(self, worker: CorrectionWorker) -> None:
        """Per-worker temp isolation (§11): wipe the workspace after each job.
        The job's prepared-input cache lives separately (data/tmp/job-<id>)
        and survives retries — it is removed only at terminal cleanup."""
        shutil.rmtree(worker.workspace, ignore_errors=True)

    # -- retry architecture helpers (§2/§3/§5/§9/§11) -------------------------

    def _keys_ready(self) -> bool:
        """True when at least one key can serve a request right now.

        Deliberately NOT about having no keys at all: that is a configuration
        problem, and the dispatch path already fails it fast with a message the
        teacher can act on. This gate only covers "all of them are cooling".
        """
        now_ts = time.time()
        if now_ts < self._keys_next_check:
            return self._keys_last_ready
        # Re-reading key health touches the secret store; twice a second on a
        # 0.5s wake loop is pointless, so the answer is good for two seconds.
        self._keys_next_check = now_ts + 2.0
        ready = True
        detail = ""
        try:
            state = self.keys.status()
            if state.get("has_keys") and int(state.get("healthy", 0)) == 0:
                ready = False
                remaining = min((int(k.get("cooldown_remaining") or 0)
                                 for k in state.get("keys") or []
                                 if int(k.get("cooldown_remaining") or 0) > 0),
                                default=0)
                detail = (f"Waiting for an API key — every key is cooling down "
                          f"(~{remaining}s)" if remaining > 0
                          else "Waiting for an API key — none is usable right now")
        except Exception:
            ready = True
        self._keys_last_ready = ready

        if ready:
            if self._keys_blocked:
                self._keys_blocked = False
                db.execute(
                    "UPDATE correction_jobs SET stage_detail = NULL, updated_at = ? "
                    "WHERE status = ?", (time.time(), "QUEUED"))
                log_event("workers", "dispatch resumed (API key available)")
            return True

        if not self._keys_blocked:
            self._keys_blocked = True
            db.execute(
                "UPDATE correction_jobs SET stage_detail = ?, updated_at = ? "
                "WHERE status = ?", (detail, time.time(), "QUEUED"))
            log_event("workers", "dispatch paused (no usable API key)",
                      level=logging.WARNING, detail=detail)
        return False

    def _capacity_delay(self) -> float:
        """How long until a key becomes usable again (the FIRST one to free up).

        The earliest cooldown is the right wait: the maximum would sleep through
        the window in which most keys are already back, and a fixed 15s ignored
        a 5s cooldown that had already expired.
        """
        try:
            status = self.keys.status()
            waiting = [int(k["cooldown_remaining"])
                       for k in status["keys"]
                       if int(k.get("cooldown_remaining") or 0) > 0]
            if waiting:
                return float(min(waiting) + 2.0)
        except Exception:
            pass
        return 15.0

    def _requeue(self, job_id: str) -> None:
        """Timer target: put a backoff-waited job back on the queue."""
        try:
            db.execute(
                "UPDATE correction_jobs SET status = ?, stage_detail = NULL, updated_at = ? WHERE id = ?",
                ("QUEUED", time.time(), job_id),
            )
        finally:
            self._wake.set()

    def _park_job(self, job_id: str, delay: float, *, reason: str = "") -> None:
        """Park a job waiting for API capacity (WAITING_FOR_API).

        Parks do NOT consume the job's attempt budget (the claim increment is
        given back at requeue — waiting for capacity is not a failed
        correction). The total parked time is bounded by
        API_WAIT_BUDGET_SECONDS, so an outage ends in a clean FAILED instead
        of an infinite park/requeue loop (spec §2 — no infinite retries).
        """
        from ..jobs.manager import API_WAIT_BUDGET_SECONDS, record_stage_event
        delay = max(1.0, float(delay))

        parks = self._park_counts.get(job_id, 0) + 1
        self._park_counts[job_id] = parks
        if parks > self.MAX_PARKS_BEFORE_SUSPEND:
            record_stage_event(job_id, status="FAILED",
                               stage=f"Stopped for diagnosis after {parks - 1} "
                                     f"capacity waits", progress=None)
            db.execute(
                "UPDATE correction_jobs SET status = ?, stage_detail = ?, "
                "error_code = 'retry_suspended', finished_at = ?, updated_at = ? "
                "WHERE id = ?",
                ("FAILED",
                 f"Stopped for diagnosis: the provider could not serve this paper "
                 f"after {parks - 1} capacity waits ({reason[:80]}). Evidence and "
                 f"prepared files kept — retry once the provider is healthy",
                 time.time(), time.time(), job_id),
            )
            self._park_counts.pop(job_id, None)
            self._api_wait_total.pop(job_id, None)
            log_event("workers", "job stopped for diagnosis (capacity never returned)",
                      level=logging.ERROR, job_id=job_id, parks=parks - 1,
                      reason=reason[:160])
            return
        waited = self._api_wait_total.get(job_id, 0.0)
        if waited + delay > API_WAIT_BUDGET_SECONDS:
            db.execute(
                "UPDATE correction_jobs SET status = ?, stage_detail = NULL, "
                "error = ?, error_code = 'all_keys_unavailable', "
                "finished_at = ?, updated_at = ? WHERE id = ?",
                ("FAILED",
                 "No Gemini API capacity became available within the waiting "
                 "budget (15 min). The job is kept so you can retry it once "
                 "the provider recovers.",
                 time.time(), time.time(), job_id),
            )
            cleanup_job_files(job_id)
            self._api_wait_total.pop(job_id, None)
            log_event("workers", "park budget exhausted — job failed",
                      level=logging.ERROR, job_id=job_id,
                      waited_seconds=round(waited), reason=reason)
            return
        self._api_wait_total[job_id] = waited + delay
        db.execute(
            "UPDATE correction_jobs SET status = ?, stage_detail = ?, updated_at = ? WHERE id = ?",
            ("WAITING_FOR_API",
             f"Waiting for API capacity — retrying in ~{int(delay)}s "
             f"(no request sent meanwhile)",
             time.time(), job_id),
        )
        log_event("workers", "job parked for API capacity",
                  level=logging.WARNING, job_id=job_id,
                  delay=round(delay, 1),
                  waited_total=round(waited + delay), reason=reason)

        def _requeue_after_park():
            try:
                # Give back the claim's attempt increment: waiting for an
                # API key is not a failed correction attempt.
                db.execute(
                    "UPDATE correction_jobs SET status = ?, "
                    "attempts = MAX(0, attempts - 1), stage_detail = ?, "
                    "updated_at = ? WHERE id = ?",
                    ("QUEUED", "Retrying after API cooldown",
                     time.time(), job_id),
                )
            finally:
                self._wake.set()

        threading.Timer(delay, _requeue_after_park).start()

    @staticmethod
    def _existing_result_id(job: dict | None) -> str | None:
        """A successful correction for this submission already exists (§9)."""
        if not job or not job.get("coursework_id") or not job.get("submission_id"):
            return None
        row = db.query_one(
            "SELECT id FROM results WHERE coursework_id = ? AND submission_id = ?",
            (job["coursework_id"], job["submission_id"]),
        )
        return row["id"] if row else None

    def _complete_with_existing(self, job: dict, result_id: str,
                                *, via: str) -> None:
        """Attach an already-existing result instead of re-running (§9)."""
        now_ts = time.time()
        db.execute(
            "UPDATE correction_jobs SET status = ?, result_id = ?, stage_detail = ?, "
            "finished_at = ?, updated_at = ? WHERE id = ?",
            ("COMPLETED", result_id,
             "Reused existing correction result — no duplicate request sent",
             now_ts, now_ts, job["id"]),
        )
        cleanup_job_files(job["id"])
        self._api_wait_total.pop(job["id"], None)
        log_event("workers", "existing result reused (no duplicate correction)",
                  job_id=job["id"], student=job.get("student_name"),
                  result_id=result_id, via=via)

    # -- monitoring (§20/§21/§22) ------------------------------------------------------

    def public_state(self) -> dict:
        with self._lock:
            workers = [w.state.public() for w in self._workers]
        counts = db.query(
            "SELECT status, COUNT(*) AS n FROM correction_jobs GROUP BY status")
        job_counts = {r["status"]: r["n"] for r in counts}
        with self._metrics_lock:
            seconds = list(self._job_seconds)
            waits = list(self._queue_wait_seconds)
            retries = self._retries_total
        import statistics
        metrics = {}
        if seconds:
            metrics = {
                "avg_seconds": round(statistics.mean(seconds), 1),
                "fastest_seconds": round(min(seconds), 1),
                "slowest_seconds": round(max(seconds), 1),
                "completed_measured": len(seconds),
            }
        if waits:
            metrics["avg_queue_wait_seconds"] = round(statistics.mean(waits), 1)
        active = sum(1 for w in workers if w["status"] == "PROCESSING")
        # jobs/hour estimate from recent completions
        recent = db.query_one(
            "SELECT COUNT(*) AS n FROM results WHERE created_at >= ?", (time.time() - 3600,))
        metrics["jobs_per_hour"] = recent["n"] if recent else 0
        metrics["retries_total"] = retries
        # Provider circuit-breaker state (§11): shows why queued jobs pause.
        try:
            from ..gemini.provider_health import provider_health
            provider = provider_health().snapshot()
        except Exception:
            provider = {"available": True, "cooldown_remaining": 0}
        return {
            "workers": workers,
            "active": active,
            "size": len(workers),
            "recommended": self.recommended_workers(),
            "provider": provider,
            "provider_blocked": bool(self._provider_blocked),
            "queue": {
                "queued": job_counts.get("QUEUED", 0),
                "starting": job_counts.get("STARTING", 0),
                "processing": sum(job_counts.get(s, 0) for s in
                                  ("PROCESSING", "MARKING", "UPLOADING", "DOWNLOADING",
                                   "PREPARING", "VERIFYING", "SAVING")),
                "waiting_for_api": job_counts.get("WAITING_FOR_API", 0),
                "retrying": job_counts.get("RETRYING", 0),
                "completed": job_counts.get("COMPLETED", 0),
                "failed": job_counts.get("FAILED", 0),
                "cancelled": job_counts.get("CANCELLED", 0),
            },
            "metrics": metrics,
            "usage_blocked": bool(self._usage_blocked),
        }

    def cancel_job(self, job_id: str) -> bool:
        job = db.row_to_dict(db.query_one(
            "SELECT * FROM correction_jobs WHERE id = ?", (job_id,)))
        if job is None:
            return False
        if job["status"] == "QUEUED":
            db.execute(
                "UPDATE correction_jobs SET status = ?, stage_detail = ?, finished_at = ?, updated_at = ? WHERE id = ?",
                ("CANCELLED", "Cancelled before start", time.time(), time.time(), job_id))
            cleanup_job_files(job_id)
            self._wake.set()
            return True
        with self._lock:
            for worker in self._workers:
                if worker.state.current_job_id == job_id:
                    worker.cancel()
                    db.execute(
                        "UPDATE correction_jobs SET status = ?, stage_detail = ?, finished_at = ?, updated_at = ? WHERE id = ?",
                        ("CANCELLED", "Cancelled by user", time.time(), time.time(), job_id))
                    worker.release_key(success=False, error=RuntimeError("cancelled"))
                    self._cleanup_workspace(worker)
                    worker.mark_idle()
                    cleanup_job_files(job_id)
                    return True
        if job["status"] in ("WAITING_FOR_API", "RETRYING", "STARTING"):
            db.execute(
                "UPDATE correction_jobs SET status = ?, stage_detail = ?, finished_at = ?, updated_at = ? WHERE id = ?",
                ("CANCELLED", "Cancelled while waiting", time.time(), time.time(), job_id))
            cleanup_job_files(job_id)
            self._api_wait_total.pop(job_id, None)
            return True
        return False


# -- singleton -------------------------------------------------------------------

_pool: WorkerPool | None = None
_pool_lock = threading.Lock()


class WorkerPoolHolder:
    @staticmethod
    def instance() -> WorkerPool:
        global _pool
        with _pool_lock:
            if _pool is None:
                from ..config import get_config
                cfg = get_config()
                _pool = WorkerPool(worker_count=cfg.worker.concurrency)
            return _pool

    @staticmethod
    def reset() -> None:
        global _pool
        with _pool_lock:
            if _pool is not None:
                _pool.shutdown(drain=False)
            _pool = None
