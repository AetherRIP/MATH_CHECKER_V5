"""CorrectionService — the V5 facade over the preserved legacy engine.

The rest of the app (API routes, job handlers) calls only this service; it
knows nothing about prompts, schemas, or model fallback chains. Job stages
map directly onto the real pipeline steps, so displayed progress is never
faked:

    PREPARING → DOWNLOADING (classroom only) → UPLOADING → MARKING →
    VERIFYING → SAVING → COMPLETED
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
import uuid
from pathlib import Path

from .. import db
from ..config import data_dir, get_config
from ..gemini.key_manager import KeyManager, WaitForApiError
from ..jobs.manager import WaitForApiRetry
from ..gemini.tester import KeyManagerHolder
from ..logging_setup import log_event
from . import legacy_marking as legacy
from .verify import verify_result

log = logging.getLogger("engine.service")


class CorrectionService:
    """Facade: enqueue + status + cancel + the two correction flows."""

    def __init__(self, key_manager: KeyManager | None = None):
        self.keys = key_manager or KeyManagerHolder.instance()

    # -- public surface (V5 §3) ----------------------------------------------

    def enqueue_classroom_submission(self, *, submission_id: str, coursework_id: str,
                                     course_id: str, student_id: str,
                                     student_name: str,
                                     is_remark: bool = False) -> dict:
        return self._enqueue(
            submission_id=submission_id, coursework_id=coursework_id,
            course_id=course_id, student_id=student_id, student_name=student_name,
            is_remark=is_remark,
        )

    def enqueue_manual_pdf(self, *, student_name: str,
                           paper_name: str | None = None,
                           page_count: int | None = None) -> dict:
        return self._enqueue(
            submission_id=None, coursework_id=None, course_id=None,
            student_id=None, student_name=student_name,
            paper_name=paper_name, page_count=page_count,
        )

    def _enqueue(self, **kwargs) -> dict:
        from ..jobs.manager import JobManagerHolder
        manager = JobManagerHolder.instance()
        return manager.enqueue(
            submission_id=kwargs.get("submission_id"),
            coursework_id=kwargs.get("coursework_id"),
            course_id=kwargs.get("course_id"),
            student_id=kwargs.get("student_id"),
            student_name=kwargs.get("student_name"),
            is_remark=bool(kwargs.get("is_remark")),
            paper_name=kwargs.get("paper_name"),
            page_count=kwargs.get("page_count"),
        )

    def get_correction_status(self, job_id: str) -> dict | None:
        job = db.row_to_dict(db.query_one(
            "SELECT * FROM correction_jobs WHERE id = ?", (job_id,)
        ))
        if job:
            # Same status -> progress normalisation the job list uses: the
            # detail endpoint is what the Corrections page polls, so a job that
            # finished at a stored 0.95 must read as 100% here too.
            from ..jobs.manager import _effective_progress
            job = _effective_progress(job)
        if job and job.get("result_id"):
            job["result"] = self.get_result(job["result_id"])
        return job

    def cancel_correction(self, job_id: str) -> threading.Event:
        from ..jobs.manager import JobManagerHolder
        return JobManagerHolder.instance().cancel(job_id)

    def get_result(self, result_id: str) -> dict | None:
        row = db.query_one("SELECT * FROM results WHERE id = ?", (result_id,))
        if not row:
            return None
        data = dict(row)
        data["result_json"] = db.loads(data.get("result_json"))
        data["verification_json"] = db.loads(data.get("verification_json"))
        return data

    def list_results(self, limit: int = 100) -> list[dict]:
        rows = db.query(
            "SELECT id, job_id, coursework_id, submission_id, student_name, earned, "
            "available, percentage, provisional_grade, marking_model, created_at "
            "FROM results ORDER BY created_at DESC LIMIT ?", (limit,)
        )
        return [dict(r) for r in rows]

    # -- job handler (registered with JobManager) -----------------------------

    def handle_job(self, job: dict, cancel: threading.Event) -> None:
        """Entry point registered with the JobManager."""
        from ..jobs.manager import CancelledError
        # Re-use the manager's exception type so its WAITING_FOR_API branch
        # catches these reliably (a service-local subclass would be silently
        # swallowed by the generic failure path).""

        manager = _job_manager()
        job_id = job["id"]

        def check_cancel():
            if cancel.is_set():
                raise CancelledError()

        def stage(status: str, detail: str):
            manager.set_stage(job_id, status, detail)

        try:
            stage("PREPARING", "Preparing submission…")
            check_cancel()
            # V5 addition (semantics-preserving): drop TEMPORARY model benches
            # from previous submissions so one rate-limited model cannot shadow
            # the rest of the legacy fallback chain for this transaction.
            # Permanent 'unavailable' benches are kept, matching legacy intent.
            legacy.MODEL_HEALTH.clear_temporary()

            # 1. Obtain source files (Classroom download or manual upload).
            if job["submission_id"]:
                stage("DOWNLOADING", "Downloading submission from Google Drive…")
                local_paths, filenames = self._download_classroom_files(job, check_cancel)
            else:
                pending = db.query_one(
                    "SELECT path, filename FROM pending_manual_files WHERE job_id = ? AND kind = 'submission'",
                    (job_id,),
                )
                if not pending:
                    raise RuntimeError("No uploaded submission file found for this job.")
                local_paths = [pending["path"]]
                filenames = [pending["filename"]]

            # Optional question paper (legacy parity: the friend's engine
            # accepts it to improve question identification).
            question_pending = db.query_one(
                "SELECT path FROM pending_manual_files WHERE job_id = ? AND kind = 'question'",
                (job_id,),
            )
            question_local = (
                question_pending["path"]
                if question_pending and Path(question_pending["path"]).exists()
                else None
            )

            check_cancel()

            # 2. Upload to Gemini (Files API). Local inputs are kept until the
            # job reaches a terminal state so retries can re-read them (§11:
            # cleanup happens in cleanup_job_files, called by the manager).
            stage("UPLOADING", "Uploading to Gemini…")
            # Size the request budget to what actually gets sent: every page of
            # the submission, the question paper (when present) and the scheme.
            from .pdf_inspect import page_images
            scheme_local = self._mark_scheme_local_path(job)
            request_pages = (sum(page_images(p) for p in local_paths)
                             + page_images(question_local)
                             + page_images(scheme_local))
            client = _make_client(pages=request_pages)
            remote_students: list = []
            remote_question = None
            for path, name in zip(local_paths, filenames):
                check_cancel()
                remote_students.append(client.upload_file(path))
            if question_local:
                remote_question = client.upload_file(question_local)

            try:
                # 3. Mark scheme: one upload per assignment, reused across
                # that assignment's sibling jobs, deleted after the batch.
                mark_scheme_remote = self._get_mark_scheme(job, client)
                dc_outcome: dict | None = None
                try:
                    stage("MARKING", "Calling Gemini…")
                    raw_result, marking_model = legacy.grade_with_model(
                        client=client,
                        model_names=get_config().gemini.primary_models,
                        student_file=remote_students,
                        mark_scheme_file=mark_scheme_remote,
                        question_file=remote_question,
                        status_callback=lambda msg: stage("MARKING", msg),
                    )
                    result = legacy.normalise_result(raw_result)
                    from .mark_integrity import enforce_evidence
                    result, _integrity = enforce_evidence(result)

                    # High-score double-check (§2/§3): while the uploaded files
                    # are still remote, re-run the SAME trusted engine
                    # INDEPENDENTLY (no first score/reasoning as input) when
                    # the first percentage reached the threshold.
                    from . import double_check as _dc
                    if not job.get("is_remark"):
                        e0, a0, p0 = legacy.calculate_totals(result)
                        if _dc.should_trigger(e0, a0):
                            try:
                                raw2, second_model = legacy.grade_with_model(
                                    client=client,
                                    # Different model first (see double_check
                                    # .second_pass_models): the same model at
                                    # temperature 0 tends to replay pass 1.
                                    model_names=_dc.second_pass_models(
                                        get_config().gemini.primary_models,
                                        marking_model),
                                    student_file=remote_students,
                                    mark_scheme_file=mark_scheme_remote,
                                    question_file=remote_question,
                                    status_callback=lambda msg: stage(
                                        "MARKING", "Double-check: " + msg),
                                    sampling=_dc.SECOND_PASS_SAMPLING,
                                )
                                _second = legacy.normalise_result(raw2)
                                _second, _ = enforce_evidence(_second)
                                dc_outcome = {
                                    "second_result": _second,
                                    "second_marking_model": second_model,
                                    "error": None,
                                }
                            except Exception as exc:  # noqa: BLE001 (§13: keep first)
                                dc_outcome = {"second_result": None,
                                              "second_marking_model": None,
                                              "error": str(exc)[:800]}
                            log_event("engine.service", "high-score double-check",
                                      job_id=job_id,
                                      first=f"{e0}/{a0}",
                                      status=("COMPLETE" if dc_outcome["second_result"]
                                              is not None else "FAILED"))
                finally:
                    for remote in remote_students:
                        client.files_delete(remote)
                    if remote_question is not None:
                        client.files_delete(remote_question)

                # 4. Deterministic verification (no extra API calls).
                stage("VERIFYING", "Verifying marks…")
                verification = verify_result(result)

                # 5. Persist result (legacy totals arithmetic, verbatim).
                stage("SAVING", "Saving result…")
                earned, available, percentage = legacy.calculate_totals(result)
                result_id = self._save_result(
                    job=job, result=result, verification=verification,
                    earned=earned, available=available, percentage=percentage,
                    marking_model=marking_model,
                    double_check_outcome=dc_outcome,
                )
            finally:
                if mark_scheme_remote is not None:
                    client.files_delete(mark_scheme_remote)

            log_event("engine.service", "correction completed", job_id=job_id,
                      student=job["student_name"], earned=earned, available=available)

        except WaitForApiRetry:
            raise
        except WaitForApiError as exc:
            # No key usable right now — park the job; the manager backs off
            # using the real cooldown estimate.
            raise WaitForApiRetry(_wait_estimate()) from exc
        except RuntimeError as exc:
            # The legacy model loop converts 'no key available' into its own
            # 'All configured Gemini models failed' error. Recognise that case
            # WITHOUT touching the preserved code, and park the job instead of
            # burning retry attempts against a cooling-down key.
            message = str(exc)
            if "cooling down" in message or "No Gemini API keys are configured" in message:
                raise WaitForApiRetry(_wait_estimate()) from exc
            raise

    def _record_scheme_audit(self, conn, job: dict, result_id: str,
                             result: dict, now_ts: float) -> None:
        """Record any substantiated mark-scheme disagreement (engine side).

        A side channel by design: a failure to *record* a discrepancy must
        never fail a correction, so it is logged and swallowed here.
        """
        try:
            from . import scheme_audit
            scheme_audit.record_from_result(
                conn, job=job, result_id=result_id, result=result,
                now_ts=now_ts)
        except Exception:  # noqa: BLE001 - never break a save over audit
            log.exception("mark-scheme discrepancy recording failed")

    # -- classroom download -----------------------------------------------------

    def _download_classroom_files(self, job: dict, check_cancel) -> tuple[list[str], list[str]]:
        from ..classroom.auth import get_access_token
        from ..classroom import sync as classroom_sync
        from ..classroom.client import classroom_attachment_ids, download_drive_file

        token = get_access_token()
        sub_row = db.query_one(
            "SELECT attachment_ids FROM submissions WHERE id = ?", (job["submission_id"],)
        )
        attachment_ids = db.loads(sub_row["attachment_ids"], []) if sub_row else []
        if not attachment_ids:
            subs = classroom_sync.sync_submissions(job["course_id"], job["coursework_id"], force=True)
            for s in subs:
                if s["id"] == job["submission_id"]:
                    attachment_ids = db.loads(s["attachment_ids"], [])
                    break
        if not attachment_ids:
            raise RuntimeError("The submission has no Google Drive attachment to mark.")

        local_paths: list[str] = []
        filenames: list[str] = []
        tmp_dir = _tmp_dir()
        for file_id in attachment_ids:
            check_cancel()
            filename, data, mime = download_drive_file(token, file_id)
            supported = (
                mime == "application/pdf"
                or mime.startswith("image/")
                or filename.lower().endswith((".pdf", ".png", ".jpg", ".jpeg", ".webp"))
            )
            if not supported:
                raise RuntimeError(f"Unsupported submitted file type: {mime} ({filename})")
            out_path = Path(tmp_dir) / f"{uuid.uuid4().hex}_{filename}"
            out_path.write_bytes(data)
            local_paths.append(str(out_path))
            filenames.append(filename)
        return local_paths, filenames

    # -- mark scheme ----------------------------------------------------------------

    def _mark_scheme_local_path(self, job: dict) -> str | None:
        """Local file of this job's mark scheme, if one is on disk.

        Shared by the upload (``_get_mark_scheme``) and the request-size
        estimate, so the page budget counts the SAME file that is uploaded.
        """
        coursework_id = job.get("coursework_id")
        if coursework_id:
            cached = _mark_scheme_dir() / f"{coursework_id}.pdf"
            if cached.exists():
                return str(cached)
            pending = db.query_one(
                "SELECT path FROM pending_mark_schemes WHERE coursework_id = ?",
                (coursework_id,),
            )
            if pending and Path(pending["path"]).exists():
                return pending["path"]
            return None

        pending = db.query_one(
            "SELECT path FROM pending_manual_files WHERE job_id = ? AND kind = 'mark_scheme'",
            (job["id"],),
        )
        if pending and Path(pending["path"]).exists():
            return pending["path"]
        return None

    def _get_mark_scheme(self, job: dict, client):
        """Upload the assignment mark scheme (cached per coursework on disk)."""
        _mark_scheme_dir().mkdir(parents=True, exist_ok=True)
        path = self._mark_scheme_local_path(job)
        if path is None:
            if job.get("coursework_id"):
                raise RuntimeError(
                    "No mark scheme has been uploaded for this assignment yet. "
                    "Upload it on the Classroom page before starting corrections."
                )
            raise RuntimeError("No mark scheme uploaded for this correction.")
        # Local copy is kept until terminal cleanup so retries can re-upload.
        return client.upload_file(path)

    def cleanup_job_files(self, job_id: str) -> None:
        cleanup_job_files(job_id)

    # -- persistence -----------------------------------------------------------------

    def _save_result(self, *, job: dict, result: dict, verification: dict,
                     earned: int, available: int, percentage: float,
                     marking_model: str,
                     double_check_outcome: dict | None = None) -> str:
        """Persist a result behind the same guard the worker pool uses (§7).

        This is the legacy single-worker save path. It applies identical
        overwrite protection so a re-correction can never silently replace a
        valid stored result with a suspicious one, whichever path ran it.

        ``double_check_outcome`` carries the independent second correction
        (high-score double-check); when present, ``earned/available/
        percentage`` become the FINAL result and BOTH corrections are
        preserved via the shared ``double_check.record_persistence`` helper.
        """
        from .result_guard import assess_confidence, evaluate_overwrite, ACTION_KEEP_EXISTING
        from .submission_validation import (
            assess_submission_quality, SUBMISSION_REVIEW_REQUIRED)
        from . import double_check, remark, versions

        dc = {"triggered": False, "status": double_check.STATUS_NOT_TRIGGERED,
              "disagreement_status": double_check.DISAGREE_NONE,
              "review_required": False, "reason": None,
              "first": (earned, available, percentage), "second": None,
              "second_result": None, "second_marking_model": None,
              "final_result": result, "final": (earned, available, percentage),
              "error": None, "question_differences": [], "verification": None}
        verification_first = verification
        if double_check_outcome is not None:
            dc = double_check.combine(
                result, double_check_outcome.get("second_result"),
                second_marking_model=double_check_outcome.get(
                    "second_marking_model"),
                error=double_check_outcome.get("error"),
                first_marking_model=marking_model)
            if dc["final_result"] is not result:
                verification = verify_result(dc["final_result"])
            if dc["second_result"] is not None:
                dc["verification"] = verify_result(dc["second_result"])
            earned, available, percentage = dc["final"]

        # Same rule as the worker pool: a paper marked by the fallback model
        # rather than the configured primary is stored but never presented as
        # a routine result — the two models do not award the same partial
        # credit, so its total is not reproducible on a healthy primary.
        _configured = list(get_config().gemini.primary_models or [])
        confidence, confidence_reasons = assess_confidence(
            dc["final_result"], verification, earned, available,
            marking_model=marking_model,
            primary_model=_configured[0] if _configured else None)
        # Submission-quality validation (§9) mirrors the worker-pool path: a
        # blank/unreadable/wrong-assignment submission is never a trusted score.
        submission_quality = assess_submission_quality(result, verification_first)
        if submission_quality["review"]:
            confidence_reasons = list(confidence_reasons) + [
                f"Submission quality ({submission_quality['quality']}): {r}"
                for r in submission_quality["reasons"]]
            if submission_quality["quality"] == SUBMISSION_REVIEW_REQUIRED:
                confidence = SUBMISSION_REVIEW_REQUIRED
        result_id = uuid.uuid4().hex
        now_ts = time.time()
        with db.transaction() as conn:
            existing = None
            if job["coursework_id"] and job["submission_id"]:
                existing = conn.execute(
                    "SELECT * FROM results WHERE coursework_id = ? AND submission_id = ?",
                    (job["coursework_id"], job["submission_id"]),
                ).fetchone()
            decision = evaluate_overwrite(
                existing=dict(existing) if existing else None,
                earned=earned, available=available, verification=verification,
                confidence=confidence, confidence_reasons=confidence_reasons,
                force=False,
            )
            # Review when EITHER the guard OR the double-check asks for it.
            from ..config import is_trusted_marking_model
            review_flag = 1 if (
                decision.review_required
                or dc["review_required"]
                or verification.get("review_required")
                or verification.get("status") not in (None, "VALID")
                or not is_trusted_marking_model(marking_model)
            ) else 0
            # ---- remark: record the attempt, apply NOTHING (§Remark) --------
            # A re-mark must never destroy a valid correction, so it is stored
            # as a PENDING attempt for the teacher to accept or dismiss. The
            # guard still classifies it, so the UI can label a failed/worse
            # attempt as needing review instead of offering it as the default.
            if job.get("is_remark") and existing:
                result_id = existing["id"]
                versions.record_pending(
                    conn, result_id=result_id, earned=earned, available=available,
                    percentage=percentage,
                    provisional_grade=legacy.percentage_band_grade(percentage),
                    marking_model=marking_model, key_fingerprint=None,
                    confidence=decision.confidence,
                    confidence_reasons=decision.confidence_reasons,
                    verification=verification, result=result, created_at=now_ts,
                    note=remark.NOTE_REMARK,
                )
                conn.execute(
                    "UPDATE correction_jobs SET result_id = ?, updated_at = ? WHERE id = ?",
                    (result_id, now_ts, job["id"]),
                )
                self._record_scheme_audit(conn, job, result_id, result, now_ts)
                return result_id
            if existing and decision.action == ACTION_KEEP_EXISTING:
                result_id = existing["id"]
                conn.execute(
                    "UPDATE results SET review_required = 1, confidence = ? WHERE id = ?",
                    (decision.confidence, result_id),
                )
                if dc["triggered"]:
                    # Preserve the double-check evidence WITHOUT applying it.
                    f_e, f_a, f_p = dc["first"]
                    versions.record_pending(
                        conn, result_id=result_id, earned=f_e, available=f_a,
                        percentage=f_p,
                        provisional_grade=legacy.percentage_band_grade(f_p),
                        marking_model=marking_model, key_fingerprint=None,
                        confidence=decision.confidence,
                        confidence_reasons=decision.confidence_reasons,
                        verification=verification_first, result=result,
                        created_at=now_ts,
                        note=("High-score double-check attempt "
                              f"({dc['status']}) — stored result kept: "
                              + (dc["reason"] or "")[:200]),
                    )
                conn.execute(
                    "UPDATE correction_jobs SET result_id = ?, updated_at = ? WHERE id = ?",
                    (result_id, now_ts, job["id"]),
                )
                self._record_scheme_audit(conn, job, result_id, result, now_ts)
                return result_id
            if existing:
                result_id = existing["id"]
                conn.execute(
                    """
                    UPDATE results SET
                        job_id = ?, earned = ?, available = ?, percentage = ?,
                        provisional_grade = ?, marking_model = ?,
                        verification_json = ?, result_json = ?,
                        processing_seconds = ?, confidence = ?,
                        confidence_reasons = ?, review_required = ?,
                        manual_override = 0, created_at = ?
                    WHERE id = ?
                    """,
                    (job["id"], earned, available, percentage,
                     legacy.percentage_band_grade(percentage), marking_model,
                     json.dumps(verification, ensure_ascii=False),
                     json.dumps(dc["final_result"] if dc["triggered"] else result, ensure_ascii=False),
                     round(now_ts - job["created_at"], 2), decision.confidence,
                     json.dumps(decision.confidence_reasons, ensure_ascii=False),
                     review_flag, now_ts, result_id),
                )
            else:
                conn.execute(
                    """
                    INSERT INTO results(
                        id, job_id, coursework_id, submission_id, student_id, student_name,
                        earned, available, percentage, provisional_grade, marking_model,
                        verification_json, result_json, processing_seconds,
                        confidence, confidence_reasons, review_required, created_at)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        result_id, job["id"], job["coursework_id"], job["submission_id"],
                        job["student_id"], job["student_name"], earned, available,
                        percentage, legacy.percentage_band_grade(percentage),
                        marking_model,
                        json.dumps(verification, ensure_ascii=False),
                        json.dumps(dc["final_result"] if dc["triggered"] else result, ensure_ascii=False),
                        round(now_ts - job["created_at"], 2),
                        decision.confidence,
                        json.dumps(decision.confidence_reasons, ensure_ascii=False),
                        review_flag, now_ts,
                    ),
                )
            if dc["triggered"]:
                # Preserve BOTH corrections (§4/§16) via the shared helper.
                double_check.record_persistence(
                    conn, result_id=result_id, first_result=result,
                    first_verification=verification_first, dc=dc,
                    marking_model=marking_model,
                    second_marking_model=dc["second_marking_model"],
                    key_fingerprint=None, confidence=decision.confidence,
                    confidence_reasons=decision.confidence_reasons,
                    now_ts=now_ts, second_verification=dc["verification"])
            # Append-only version history (§D): every accepted state is kept
            # so a recheck can never silently destroy the previous correction.
            versions.record(
                conn, result_id=result_id, earned=earned, available=available,
                percentage=percentage,
                provisional_grade=legacy.percentage_band_grade(percentage),
                marking_model=marking_model, key_fingerprint=None,
                confidence=decision.confidence,
                confidence_reasons=decision.confidence_reasons,
                verification=verification,
                result=dc["final_result"] if dc["triggered"] else result,
                created_at=now_ts,
                note=("Correction saved" if not dc["triggered"]
                      else "Final result after high-score double-check"),
            )
            conn.execute(
                "UPDATE correction_jobs SET result_id = ?, updated_at = ? WHERE id = ?",
                (result_id, now_ts, job["id"]),
            )
            self._record_scheme_audit(conn, job, result_id,
                                      dc["final_result"] if dc["triggered"] else result,
                                      now_ts)
        return result_id


# -- helpers (pure functions / per-call state; no hidden globals) --------------

def _tmp_dir() -> str:
    path = data_dir() / "tmp"
    path.mkdir(parents=True, exist_ok=True)
    return str(path)


def _mark_scheme_dir() -> Path:
    return data_dir() / "mark_schemes"


def _make_client(pages: int = 0):
    """A key-rotating client whose request budget matches the paper's size.

    ``pages`` is the number of page images the marking request will carry; the
    proxy turns it into a per-request timeout (a 41-page paper needs minutes,
    a 70-page booklet more). 0 keeps the configured floor.
    """
    from ..gemini.client_proxy import KeyAwareClient
    return KeyAwareClient(KeyManagerHolder.instance(), pages=pages)


def cleanup_job_files(job_id: str) -> None:
    """Terminal-state cleanup (§11): remove the job's local input files.
    Called only when the job reaches COMPLETED/FAILED(final)/CANCELLED —
    never while retries may still need the files. Shared by the legacy
    service surface and the worker pool (which now executes jobs).

    Besides the manual-upload staging rows this removes the per-job
    prepared-input cache (data/tmp/job-<id>, retry architecture §10): the
    temporary download/normalized student PDFs are reused for every retry
    during the job and are NOT retained once the job is terminal.

    When the teacher has asked to keep originals (§19), the SUBMISSION files
    are first moved into a retained store with an expiry, so the retention
    setting corresponds to real behaviour instead of being a label.
    """
    rows = db.query(
        "SELECT path, kind, filename FROM pending_manual_files WHERE job_id = ?",
        (job_id,),
    )
    # Retain EVERY submission the job actually marked, not only manual uploads:
    # a Classroom job stages the downloaded PDF under data/tmp/job-<id>/ and
    # registers no pending row at all, which is why "keep submissions" used to
    # delete a Classroom paper anyway.
    retain_job_submissions(job_id, rows=rows)
    if retention_incomplete(job_id, rows=rows):
        # The teacher asked to keep this paper and the copy FAILED (disk full,
        # permissions, locked file). Deleting the only copy now would silently
        # destroy it, so nothing is removed; the next cleanup or startup sweep
        # retries the retention step.
        log_event("engine.service",
                  "retention failed; inputs preserved instead of deleted",
                  job_id=job_id, level=logging.WARNING)
        return
    # Optimized SUBMISSION copies are processing artifacts, not retained data:
    # drop them now (the original was either retained above or is being deleted
    # per the setting). Mark-scheme artifacts stay in the bounded shared cache.
    try:
        from .pdf_optimize import release_job
        release_job(job_id)
    except Exception:  # noqa: BLE001 — cache hygiene must never fail cleanup
        pass
    for row in rows:
        try:
            Path(row["path"]).unlink(missing_ok=True)
        except OSError:
            pass
    db.execute("DELETE FROM pending_manual_files WHERE job_id = ?", (job_id,))
    try:
        import shutil
        shutil.rmtree(data_dir() / "tmp" / f"job-{job_id}",
                      ignore_errors=True)
    except OSError:
        pass

    # Provider-side files owned by this job (submissions/questions). They were
    # deliberately kept alive across retries; now that the job is terminal we
    # release them (§6). Shared mark schemes are not job-owned, so they stay
    # registered for the assignment's remaining students (§9).
    try:
        from ..config import get_config
        from ..gemini import file_registry
        files = file_registry.job_files(job_id)
        if files and not get_config().mock_mode:
            from ..gemini.client_proxy import KeyAwareClient
            from ..gemini.tester import KeyManagerHolder
            client = KeyAwareClient(KeyManagerHolder.instance())
            for entry in files:
                if file_registry.remote_held_by_other_active_jobs(
                        entry["remote_name"], job_id, _ACTIVE_JOB_STATUSES):
                    # Eight simultaneous corrections of one paper share a single
                    # upload; the first job to finish used to delete it and the
                    # others then died on 403 PERMISSION_DENIED. The last job
                    # holding the file deletes it.
                    log_event("engine.service",
                              "provider file kept for another active job",
                              job_id=job_id,
                              remote=entry["remote_name"],
                              purpose=entry["purpose"])
                    continue
                client.delete_remote_for_key(entry["fingerprint"],
                                            entry["remote_name"])
        file_registry.clear_job_files(job_id)
    except Exception:
        pass


_ACTIVE_JOB_STATUSES = (
    "QUEUED", "STARTING", "PREPARING", "DOWNLOADING", "UPLOADING", "MARKING",
    "VERIFYING", "SAVING", "WAITING_FOR_API", "RETRYING", "PROCESSING",
)


def retention_incomplete(job_id: str, *, rows=None,
                         directory: Path | None = None) -> bool:
    """Retention is ON, a submission exists, and no copy was stored.

    Used to refuse deletion: a failed copy must never take the only copy of a
    paper the teacher asked to keep with it.
    """
    if not retention_keeps():
        return False
    has_submission = any(row["kind"] == "submission" for row in rows or [])
    if not has_submission:
        job_dir = directory or (data_dir() / "tmp" / f"job-{job_id}")
        has_submission = bool(_staged_submission_files(job_dir))
    if not has_submission:
        return False
    try:
        row = db.query_one(
            "SELECT 1 AS ok FROM retained_files WHERE job_id = ? "
            "AND kind = 'submission' LIMIT 1", (job_id,))
    except Exception:  # noqa: BLE001 — a DB hiccup must not delete anything
        return True
    return row is None


def retention_keeps() -> bool:
    """Does the CURRENT setting ask for submissions to be kept?"""
    try:
        return get_config().retention.keep_submissions in ("days", "always")
    except Exception:  # noqa: BLE001 — never fail a cleanup over config
        return False


def _staged_submission_files(job_dir: Path) -> list[Path]:
    """The submission file(s) a job was actually marked from.

    A Classroom job downloads and normalizes the attachment into
    ``data/tmp/job-<id>/submission_*``; that file (the copy the marker saw) is
    the only local original such a job has. The mark scheme is never matched.
    """
    try:
        if not job_dir.is_dir():
            return []
        return sorted(p for p in job_dir.glob("submission_*")
                      if p.is_file())
    except OSError:
        return []


def retain_job_submissions(job_id: str, *, rows=None,
                           directory: Path | None = None) -> int:
    """Copy this job's submission file(s) into the retained store (§19).

    THE one place that honours ``retention.keep_submissions``. It is called by
    terminal cleanup AND by the startup temp sweep, so no deletion path can
    discard a paper the teacher asked to keep. Only SUBMISSIONS are retained:
    never a mark scheme (a shared reference document) and never a question
    paper. ``days`` stores an expiry, ``always`` stores none, and
    ``never``/``until_complete`` store nothing and delete as before.

    If a Classroom job had no staged copy left, the file simply cannot be
    retained; nothing is invented and the viewer reports the genuine gap.
    """
    if not retention_keeps():
        return 0
    try:
        cfg = get_config().retention
        import shutil
        retain_dir = data_dir() / "retained" / job_id
        retain_until = (0.0 if cfg.keep_submissions == "always"
                        else time.time() + max(0, cfg.keep_days) * 86400)
        candidates: list[tuple[Path, str]] = []
        for row in rows or []:
            if row["kind"] != "submission":
                continue
            try:
                filename = row["filename"]
            except (IndexError, KeyError):
                filename = None
            candidates.append((Path(row["path"]),
                               filename or Path(row["path"]).name))
        job_dir = directory or (data_dir() / "tmp" / f"job-{job_id}")
        for path in _staged_submission_files(job_dir):
            candidates.append((path, path.name))

        retained = 0
        for src, filename in candidates:
            try:
                if not src.is_file():
                    continue
                dest = retain_dir / src.name
                retain_dir.mkdir(parents=True, exist_ok=True)
                if dest.resolve() == src.resolve():
                    continue          # already the retained copy
                if dest.exists() and dest.stat().st_size != src.stat().st_size:
                    dest = retain_dir / f"{len(list(retain_dir.iterdir()))}-{src.name}"
                shutil.copy2(src, dest)
            except OSError:
                log_event("engine.service", "retention copy failed",
                          job_id=job_id, level=logging.WARNING)
                continue
            db.execute(
                "INSERT OR REPLACE INTO retained_files"
                "(job_id, path, kind, filename, retain_until, created_at)"
                " VALUES(?,?,?,?,?,?)",
                (job_id, str(dest), "submission", filename,
                 retain_until, time.time()),
            )
            retained += 1
        if retained:
            log_event("engine.service", "submission retained", job_id=job_id,
                      files=retained, mode=cfg.keep_submissions,
                      until=retain_until)
        return retained
    except Exception as exc:  # noqa: BLE001 — retention must never break a job
        log_event("engine.service", "retention step skipped", job_id=job_id,
                  error=f"{type(exc).__name__}: {exc}"[:200],
                  level=logging.WARNING)
        return 0


def _retain_originals(job_id: str, rows) -> None:
    """Backwards-compatible wrapper around :func:`retain_job_submissions`."""
    retain_job_submissions(job_id, rows=rows)


def sweep_expired_retained() -> int:
    """Delete retained originals whose expiry has passed (§19).

    Called at startup so a 'keep for N days' setting actually reclaims disk
    over time rather than accumulating forever.
    """
    now = time.time()
    rows = db.query(
        "SELECT job_id, path FROM retained_files "
        "WHERE retain_until > 0 AND retain_until <= ?", (now,))
    removed = 0
    for row in rows:
        try:
            Path(row["path"]).unlink(missing_ok=True)
            removed += 1
        except OSError:
            pass
        db.execute("DELETE FROM retained_files WHERE job_id = ? AND path = ?",
                   (row["job_id"], row["path"]))
    # Tidy empty per-job directories left behind.
    retain_root = data_dir() / "retained"
    if retain_root.exists():
        import shutil
        for child in retain_root.iterdir():
            if child.is_dir() and not any(child.iterdir()):
                shutil.rmtree(child, ignore_errors=True)
    if removed:
        log_event("engine.service", "expired retained files removed", removed=removed)
    return removed


def sweep_stale_temp(max_age_seconds: float = 24 * 3600) -> int:
    """Remove ORPHANED per-job staging dirs at startup (§20/§25 hygiene).

    A job that reaches a terminal state deletes its own cache; a killed or
    crashed process leaves ``data/tmp/job-<id>`` behind, and those old student
    downloads then accumulate on disk forever.

    A dir is removed when either:
      * its job is not in the DB or has reached a terminal state (precise), or
      * it has no matching job row and is older than ``max_age_seconds``.

    A dir belonging to a still-ACTIVE job is never touched.
    """
    import shutil
    tmp = data_dir() / "tmp"
    if not tmp.exists():
        return 0
    active: set[str] = set()
    try:
        marks = ",".join("?" * len(_ACTIVE_JOB_STATUSES))
        active = {r["id"] for r in db.query(
            f"SELECT id FROM correction_jobs WHERE status IN ({marks})",
            tuple(_ACTIVE_JOB_STATUSES))}
    except Exception:
        active = set()
    now_ts = time.time()
    removed = 0
    try:
        for entry in tmp.iterdir():
            if not entry.is_dir() or not entry.name.startswith("job-"):
                continue
            job_id = entry.name[4:]
            try:
                if job_id in active:
                    continue
                age = now_ts - entry.stat().st_mtime
                # Terminal/missing jobs go immediately; unknown dirs age out.
                if job_id not in _known_job_ids(job_id) \
                        and age < max_age_seconds:
                    continue
                # A sweep must not be a second, retention-blind deletion path:
                # a job that never reached terminal cleanup (awaiting review,
                # cancelled, crashed) would otherwise lose the only copy of a
                # paper the teacher asked to keep.
                try:
                    retain_job_submissions(job_id, directory=entry)
                    if retention_incomplete(job_id, directory=entry):
                        log_event("engine.service",
                                  "sweep kept a job dir: retention failed",
                                  job_id=job_id, level=logging.WARNING)
                        continue
                except Exception:  # noqa: BLE001 — never block the sweep
                    continue
                shutil.rmtree(entry, ignore_errors=True)
                removed += 1
            except OSError:
                continue
    except OSError:
        return removed
    return removed


def _known_job_ids(job_id: str) -> set[str]:
    try:
        return {r["id"] for r in db.query(
            "SELECT id FROM correction_jobs WHERE id = ?", (job_id,))}
    except Exception:
        return set()


def _wait_estimate() -> float:
    """How long until some key likely becomes usable (cooldown + buffer)."""
    from ..gemini.tester import KeyManagerHolder
    status = KeyManagerHolder.instance().status()
    remaining = [k["cooldown_remaining"] for k in status["keys"]]
    return float(max(remaining or [0]) + 5.0)


def _job_manager():
    from ..jobs.manager import JobManagerHolder
    return JobManagerHolder.instance()
