"""The Large PDF Worker orchestrator.

It reuses the existing correction engine (``legacy_marking.grade_with_model``
plus the same normalisation / blueprint / verification / totals pipeline). It
adds NO new marking logic — only the long-document workflow around it:

    inspect -> plan -> (mark batches, bounded concurrency) -> validate -> merge

Guarantees:

* marking never starts before a validated plan exists (``process`` refuses);
* every page has exactly one owner, so no question is marked twice;
* a missing batch result never becomes a silent 0 (it stays unmarked and
  requires review);
* successful batches are kept and a failed batch is retried on its own;
* all writes are idempotent, so polling / refreshes / restarts create no
  duplicate AI calls.

The batch engine is injectable so tests can drive the full plan -> validate ->
merge pipeline with controlled fixtures and no API quota.
"""

from __future__ import annotations

import hashlib
import json
import logging
import shutil
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

from .. import db
from .. import retry_policy
from ..config import DATA_DIR, get_config
from ..engine import batching
from ..engine import legacy_marking as legacy
from ..engine.blueprint import apply_blueprint, get_blueprint
from ..engine.recover_missing import (
    RecoveryOutcome, as_json, plan_chunked_recovery, run_recovery_round,
    unresolved_reason,
)
from ..engine.result_guard import assess_confidence, evaluate_overwrite
from ..engine.verify import verify_result
from ..logging_setup import log_event
from . import planner, prompt as large_prompt, states, store
from .validate import (
    batch_coverage, merge_batches, score_breakdown, scheme_total_marks,
    totals_for, validate_batch,
)

log = logging.getLogger("large_pdf.runner")


# ------------------------------------------------------------- engine bridge

@dataclass
class BatchOutcome:
    ok: bool
    result: dict | None = None
    marking_model: str | None = None
    error: str | None = None
    error_kind: str | None = None


def default_batch_engine(*, batch_pdf: str, mark_scheme_pdf: str,
                         job: dict, batch: dict) -> BatchOutcome:
    """Run ONE batch through the existing correction engine.

    The mark-scheme blueprint is intentionally NOT applied per batch:
    reconciling a batch against the full mark scheme would mark every question
    belonging to other batches as missing. Reconciliation happens ONCE on the
    merged result, which is the only place that sees every batch.

    The DEDICATED large-PDF prompt (``app/large_pdf/prompt.py``) is used here,
    never the ordinary single-file prompt: only it tells the model that these
    pages are one batch of a longer script, that the attached scheme is the
    COMPLETE scheme, and that the response must not stop early. The attached
    mark scheme is always the whole, unsplit official scheme — the student
    pages are what gets batched, never the marking reference.

    The runner passes the batch's paper-wide context (page count, batch number,
    the mark-scheme blueprint) inside the ``batch`` dict as ``_``-prefixed keys,
    so the engine contract stays ``(batch_pdf, mark_scheme_pdf, job, batch)``
    for every injected engine.
    """
    cfg = get_config()
    models = list(cfg.gemini.primary_models)
    mock = cfg.mock_mode
    if mock:
        from ..gemini.mock import MockGeminiClient
        client = MockGeminiClient()
        remote_student = client.upload(file=batch_pdf)
        remote_scheme = client.upload(file=mark_scheme_pdf)
        inner = None
    else:
        from ..gemini.client_proxy import KeyAwareClient
        from ..gemini.tester import KeyManagerHolder
        inner = KeyAwareClient(KeyManagerHolder.instance(),
                               job_id=f"lp-{job['id']}-{batch['batch_index']}")
        client = inner
        remote_student = inner.upload_file(batch_pdf, purpose="submission")
        remote_scheme = inner.upload_file(mark_scheme_pdf, purpose="mark_scheme",
                                          shared=True)
    system_prompt, user_prompt = large_prompt.prompts_for_batch(
        batch=batch,
        paper_pages=int(batch.get("_paper_pages") or job.get("page_count") or 0),
        blueprint=batch.get("_blueprint"))
    try:
        raw, model = legacy.grade_with_model(
            client=client, model_names=models,
            student_file=[remote_student], mark_scheme_file=remote_scheme,
            question_file=None, blueprint=None,
            system_prompt=system_prompt, user_prompt=user_prompt)
        result = legacy.normalise_result(raw)
        return BatchOutcome(ok=True, result=result, marking_model=model)
    except Exception as exc:  # noqa: BLE001
        from ..errors import classify_gemini_error
        kind = classify_gemini_error(exc).get("kind", "unknown")
        return BatchOutcome(ok=False, error=str(exc)[:800], error_kind=kind)
    finally:
        if inner is not None:
            try:
                inner.files_delete(remote_student)
            except Exception:
                pass
            try:
                inner.release_job_files()
            except Exception:
                pass
        else:
            for remote in (remote_student, remote_scheme):
                try:
                    client.files.delete(name=remote.name)
                except Exception:
                    pass


# ------------------------------------------------------------------- runner

def _sha256(path: str | Path) -> str | None:
    try:
        digest = hashlib.sha256()
        with open(path, "rb") as handle:
            for chunk in iter(lambda: handle.read(65536), b""):
                digest.update(chunk)
        return digest.hexdigest()
    except OSError:
        return None


def processing_version(student_pdf_sha: str | None, mark_scheme_sha: str | None) -> str:
    """Stable idempotency key: source content + marking configuration.

    The LARGE PDF prompt is the hashed prompt (not the ordinary one): a change
    to the long-document prompt must invalidate a cached job exactly like a
    changed PDF does.
    """
    cfg = get_config()
    sys_prompt = large_prompt.effective_large_pdf_system_prompt()
    payload = "|".join([
        student_pdf_sha or "unknown-student",
        mark_scheme_sha or "unknown-scheme",
        ",".join(cfg.gemini.primary_models),
        hashlib.sha256(sys_prompt.encode("utf-8")).hexdigest()[:16],
        f"t{cfg.large_pdf.target_min}-{cfg.large_pdf.target_max}",
    ])
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]


def default_recovery_engine(*, client, chunk: list[str], student_remote,
                           scheme_remote, mark_scheme_path: str,
                           context_note: str = "",
                           question_remote=None) -> RecoveryOutcome:
    """Re-mark ONE bounded chunk of missing questions.

    Same narrow-prompt retry as before (only the listed questions enter the
    blueprint, so the request — and its cost — is proportional to the gap),
    now called once per chunk instead of once per job.
    """
    return run_recovery_round(
        client=client, missing=list(chunk), student_remote=student_remote,
        mark_scheme_remote=scheme_remote, question_remote=question_remote,
        mark_scheme_path=mark_scheme_path, context_note=context_note)


class LargePdfRunner:
    def __init__(self, engine=None, recovery_engine=None):
        self.engine = engine or default_batch_engine
        self._recovery_engine = recovery_engine or default_recovery_engine
        self._lock = threading.RLock()
        self._batch_paths: dict[str, dict[int, str]] = {}

    # -- enqueue (idempotent) ------------------------------------------------

    def enqueue_manual(self, *, student_name: str, student_pdf_path: str,
                       mark_scheme_path: str, job_id: str | None = None) -> dict:
        return self._enqueue(
            submission_id=None, coursework_id=None, course_id=None,
            student_id=None, student_name=student_name,
            student_pdf_path=student_pdf_path, mark_scheme_path=mark_scheme_path,
            job_id=job_id)

    def enqueue_classroom(self, *, submission_id: str, coursework_id: str,
                          course_id: str, student_id: str | None,
                          student_name: str | None, student_pdf_path: str,
                          mark_scheme_path: str, job_id: str | None = None) -> dict:
        return self._enqueue(
            submission_id=submission_id, coursework_id=coursework_id,
            course_id=course_id, student_id=student_id,
            student_name=student_name, student_pdf_path=student_pdf_path,
            mark_scheme_path=mark_scheme_path, job_id=job_id)

    def _enqueue(self, **kw) -> dict:
        student_pdf = kw["student_pdf_path"]
        scheme = kw["mark_scheme_path"]
        if not Path(student_pdf).is_file():
            raise FileNotFoundError("The student answer PDF is missing.")
        if not Path(scheme).is_file():
            raise FileNotFoundError("The mark scheme PDF is missing.")
        student_sha = _sha256(student_pdf)
        scheme_sha = _sha256(scheme)
        version = processing_version(student_sha, scheme_sha)

        # Delegated jobs are pinned to the correction job's id so the whole
        # submission keeps ONE job row and ONE result through approval.
        pinned = kw.get("job_id")
        if pinned:
            existing_pinned = store.get_job(pinned)
            if existing_pinned:
                return existing_pinned
        else:
            # Idempotency: the exact same inputs (and config) reuse the job.
            existing = store.find_job(kw.get("coursework_id"),
                                      kw.get("submission_id"), version)
            if existing:
                return existing
            # Duplicate-launch protection: an in-flight job for the submission.
            active = store.find_active_for_submission(kw.get("coursework_id"),
                                                      kw.get("submission_id"))
            if active:
                return active
        return store.create_job(
            submission_id=kw.get("submission_id"),
            coursework_id=kw.get("coursework_id"),
            course_id=kw.get("course_id"),
            student_id=kw.get("student_id"),
            student_name=kw.get("student_name"),
            student_pdf_path=student_pdf, mark_scheme_path=scheme,
            processing_version=version, student_pdf_sha=student_sha,
            mark_scheme_sha=scheme_sha,
            max_attempts=get_config().large_pdf.max_retries + 1,
            job_id=pinned)

    # -- inspection + planning ----------------------------------------------

    def prepare(self, job_id: str) -> dict:
        """Inspect the document and persist the complete plan (no marking)."""
        job = store.get_job(job_id)
        if job is None:
            raise KeyError(job_id)
        if store.list_batches(job_id):
            # Already inspected and planned — a retry, a re-claim after a
            # restart, or a repeated routed call. Re-planning would duplicate
            # the batches, so the saved plan is authoritative.
            return job
        store.set_status(job_id, states.INSPECTING, detail="Preparing document")
        prepared = planner.prepare(job["student_pdf_path"],
                                   job["mark_scheme_path"])
        if prepared.blocked:
            store.set_status(job_id, states.FAILED, detail="Inspection failed",
                             error=prepared.block_reason,
                             error_code="inspection_failed",
                             page_count=prepared.page_count)
            return store.get_job(job_id) or job

        store.set_status(job_id, states.PLANNING, detail="Preparing batches",
                         page_count=prepared.page_count)
        plan = prepared.plan
        store.create_batches(job_id, [b.to_dict() for b in plan.batches])
        store.set_status(
            job_id, states.READY,
            detail=states.progress_message(states.READY,
                                           batch_total=plan.batch_count),
            page_count=prepared.page_count, batch_count=plan.batch_count,
            plan_json=json.dumps({
                "plan": plan.to_dict(),
                "scheme_questions": prepared.scheme_questions,
                "mark_scheme_pages": prepared.mark_scheme_pages,
                "question_pages": {k: list(v) for k, v in
                                   prepared.question_pages.items()},
                "notes": prepared.notes,
            }, ensure_ascii=False))
        log_event("large_pdf", "plan saved", job_id=job_id,
                  pages=prepared.page_count, batches=plan.batch_count,
                  single_pass=plan.single_pass)
        return store.get_job(job_id) or job

    # -- processing ----------------------------------------------------------

    def process(self, job_id: str, *, prepare_if_needed: bool = True) -> dict:
        """Mark any PENDING batches, then validate + merge when complete."""
        job = store.get_job(job_id)
        if job is None:
            raise KeyError(job_id)
        if job["status"] in (states.COMPLETED, states.CANCELLED):
            return job
        # A job that already holds a persisted, finalized result is NEVER
        # re-finalized. Without this guard a repeated call — Classroom polling,
        # a page refresh, a boot-recovery sweep or a manual /process click —
        # re-ran the whole finalize step, which re-called the blueprint AI and
        # INSERTed a second `results` row for a manual job (its identity is not
        # keyed by submission, so the upsert could not catch it). An explicit
        # retry first moves the job back to READY (retry_job / retry_batch), so
        # genuine retries still finalize normally.
        if job.get("result_id") and job["status"] in (
                states.COMPLETED, states.AWAITING_REVIEW):
            return job
        if prepare_if_needed and job["status"] in (
                states.QUEUED, states.INSPECTING, states.PLANNING):
            job = self.prepare(job_id)
        if job["status"] not in states.PROCESSABLE:
            # Never mark without a validated plan.
            return job

        batches = store.list_batches(job_id)
        pending = [b for b in batches if b["status"] == states.BATCH_PENDING]
        if pending:
            paths = self._ensure_batch_pdfs(job, batches)
            blueprint = self._cached_blueprint(job)
            for b in batches:
                b["_batch_pdf"] = paths.get(b["batch_index"])
                # Paper-wide context the batch prompt AND the coverage check
                # need; carried on the batch so the engine contract is stable.
                b["_paper_pages"] = job.get("page_count")
                b["_batch_total"] = len(batches)
                b["_blueprint"] = blueprint
                b["_scheme_total_marks"] = scheme_total_marks(blueprint)
            store.set_status(
                job_id, states.PROCESSING,
                detail=states.progress_message(
                    states.PROCESSING, batch_index=pending[0]["batch_index"] + 1,
                    batch_total=len(batches)))
            cfg = get_config().large_pdf
            workers = max(1, min(int(cfg.concurrency), len(pending)))
            runnable = [b for b in pending if b.get("_batch_pdf")]
            with ThreadPoolExecutor(max_workers=workers) as pool:
                list(pool.map(lambda b: self._process_batch(job, b), runnable))

        return self._finalize(job_id)

    def _ensure_batch_pdfs(self, job: dict, batches: list[dict]) -> dict[int, str]:
        """Split the student PDF into one file per batch (owned + context)."""
        from ..engine.pdf_inspect import split_pdf
        existing = self._batch_paths.get(job["id"])
        if existing and all(Path(p).is_file() for p in existing.values()):
            return existing
        out_dir = DATA_DIR / "tmp" / "large_pdf" / job["id"]
        read_ranges = [
            (max(1, b["page_start"] - (b["context_before"] or 0)),
             b["page_end"] + (b["context_after"] or 0))
            for b in batches]
        paths = split_pdf(job["student_pdf_path"], read_ranges, out_dir,
                          prefix=f"job{job['id'][:8]}")
        mapping = {b["batch_index"]: path for b, path in zip(batches, paths)}
        self._batch_paths[job["id"]] = mapping
        return mapping

    def _process_batch(self, job: dict, batch: dict) -> None:
        claimed = store.claim_batch(job["id"], batch["batch_index"],
                                    worker="large-pdf")
        if claimed is None:
            return  # another worker owns it — never mark it twice
        for key in ("_batch_pdf", "_paper_pages", "_batch_total", "_blueprint",
                    "_scheme_total_marks"):
            if key in batch:
                claimed[key] = batch[key]
        self._run_engine_with_retries(job, claimed)

    def _run_engine_with_retries(self, job: dict, batch: dict) -> str:
        cfg = get_config().large_pdf
        empty_questions = []
        if batch.get("question_ids"):
            try:
                empty_questions = json.loads(batch["question_ids"])
            except (json.JSONDecodeError, TypeError):
                empty_questions = []
        # Best attempt seen for THIS batch in this process: a STUB response is
        # re-marked, and the attempt that marked more of the batch's pages wins.
        # No mark is ever invented — both attempts are the marker's own work.
        best: tuple[int, dict, dict] | None = None
        attempts_ran = 0
        while True:
            attempts_ran += 1
            try:
                outcome = self.engine(
                    batch_pdf=batch["_batch_pdf"],
                    mark_scheme_pdf=job["mark_scheme_path"],
                    job=job, batch=batch)
            except Exception as exc:  # noqa: BLE001 — never lose the batch
                from ..errors import classify_gemini_error
                outcome = BatchOutcome(
                    ok=False, error=str(exc)[:800],
                    error_kind=classify_gemini_error(exc).get("kind", "unknown"))

            if outcome.ok and outcome.result is not None:
                coverage = batch_coverage(
                    outcome.result, batch,
                    paper_pages=batch.get("_paper_pages"),
                    scheme_total_marks=batch.get("_scheme_total_marks"))
                validation = validate_batch(
                    outcome.result, batch, expected_questions=empty_questions,
                    coverage=coverage)
                returned_marks = int((coverage or {}).get("returned_marks") or 0)
                if best is None or returned_marks > best[0]:
                    best = (returned_marks, outcome.result, validation)
                if validation["stub"]:
                    # The model answered a few questions and stopped (typically
                    # after provider failures). Accepting this as a COMPLETED
                    # batch is what turned a 90% script into 46.6%: the pages it
                    # never answered became UNMARKED questions counted as 0.
                    # Re-mark the SAME batch within the existing bounded retry
                    # budget, then keep whichever attempt covered more.
                    log_event("large_pdf", "incomplete batch response",
                              job_id=job["id"],
                              batch_index=batch["batch_index"],
                              coverage=coverage,
                              attempts=batch["attempts"] or 1)
                    attempts_used = int(batch["attempts"] or 1)
                    if attempts_used <= cfg.max_retries:
                        store.fail_batch(
                            job["id"], batch["batch_index"],
                            error="The marker returned an incomplete result "
                                  "for this batch's pages; it was re-marked.",
                            error_code="incomplete_response")
                        store.reset_batch(job["id"], batch["batch_index"])
                        refreshed = store.claim_batch(job["id"],
                                                      batch["batch_index"])
                        if refreshed is not None:
                            for key in ("_batch_pdf", "_paper_pages",
                                        "_batch_total", "_blueprint",
                                        "_scheme_total_marks"):
                                if key in batch:
                                    refreshed[key] = batch[key]
                            batch = refreshed
                            continue
                kept_marks, result_best, validation_best = best
                if attempts_ran > 1:
                    validation_best = dict(validation_best)
                    validation_best["attempts_ran"] = attempts_ran
                    validation_best["kept"] = (
                        f"Best of {attempts_ran} attempt(s): the kept response "
                        f"returns the most questions for this batch's pages "
                        f"({kept_marks} mark(s) of question content).")
                status = (states.BATCH_COMPLETED
                          if not validation_best["problems"]
                          else states.BATCH_REVIEW_REQUIRED)
                store.complete_batch(job["id"], batch["batch_index"],
                                     result=result_best,
                                     validation=validation_best,
                                     status=status)
                return "completed"

            failure = retry_policy.failure_for_kind(outcome.error_kind,
                                                    outcome.error or "")
            decision = retry_policy.decide(
                failure, attempts=batch["attempts"] or 1,
                max_attempts=cfg.max_retries + 1,
                cfg_max_retries=cfg.max_retries)
            store.fail_batch(job["id"], batch["batch_index"],
                             error=outcome.error or "batch failed",
                             error_code=failure.cls)
            if decision.action == retry_policy.ACTION_RETRY:
                time.sleep(min(decision.delay, 5.0))
                store.reset_batch(job["id"], batch["batch_index"])
                refreshed = store.claim_batch(job["id"], batch["batch_index"])
                if refreshed is None:
                    return "failed"
                refreshed["_batch_pdf"] = batch["_batch_pdf"]
                batch = refreshed
                continue
            if decision.action == retry_policy.ACTION_PARK:
                # Capacity wait: leave the batch PENDING for a later run
                # WITHOUT consuming its retry budget.
                store.reset_batch(job["id"], batch["batch_index"])
                return "parked"
            return "failed"

    def _finalize(self, job_id: str) -> dict:
        job = store.get_job(job_id)
        if job is None:
            raise KeyError(job_id)
        batches = store.list_batches(job_id)

        failed = [b for b in batches if b["status"] == states.BATCH_FAILED]
        unfinished = [b for b in batches
                      if b["status"] in (states.BATCH_PENDING,
                                         states.BATCH_PROCESSING)]
        done = [b for b in batches
                if b["status"] in (states.BATCH_COMPLETED,
                                   states.BATCH_REVIEW_REQUIRED)]

        if failed and not unfinished:
            self._cleanup_delegated(job_id)
            return store.set_status(
                job_id, states.FAILED,
                detail=f"Correction failed — {len(failed)} batch(es) eligible "
                       f"for retry",
                error="; ".join((b["error"] or "batch failed")[:200]
                                for b in failed)[:800],
                error_code="batch_failed")
        if unfinished:
            nxt = unfinished[0]["batch_index"] + 1
            return store.set_status(
                job_id, states.READY,
                detail=states.progress_message(
                    states.PROCESSING, batch_index=nxt,
                    batch_total=len(batches)))

        # All batches finished: validate + merge.
        store.set_status(job_id, states.VALIDATING,
                         detail=states.progress_message(states.VALIDATING))
        plan = self._load_plan(job)
        batch_results: dict[int, dict] = {}
        review_batches: list[int] = []
        incomplete_batches: list[int] = []
        for b in done:
            payload = db.loads(b.get("result_json"))
            if b["status"] == states.BATCH_REVIEW_REQUIRED:
                review_batches.append(b["batch_index"])
            # A batch whose response was flagged INCOMPLETE is a known-partial
            # mark of its pages. Even when nothing else can prove coverage
            # (e.g. no extracted mark-scheme structure), such a job must never
            # finalize as COMPLETED.
            validation = db.loads(b.get("validation_json"), {}) or {}
            if validation.get("stub"):
                incomplete_batches.append(int(b["batch_index"]))
            if payload is not None:
                batch_results[b["batch_index"]] = payload

        blueprint, blueprint_meta = self._get_blueprint(job)
        blueprint_questions = (blueprint or {}).get("questions") if blueprint else None

        store.set_status(job_id, states.MERGING,
                         detail=states.progress_message(states.MERGING))
        merged = merge_batches(plan, batches, batch_results,
                               blueprint_questions=blueprint_questions)
        result = merged["result"]
        result, blueprint_report = apply_blueprint(result, blueprint)
        if blueprint_meta.get("source"):
            blueprint_report["source"] = blueprint_meta.get("source")
        # §9: an incomplete/unreviewed structure must travel with the result.
        # Nothing about the marking changes; the verdict just cannot be lost.
        blueprint_report["completeness"] = blueprint_meta.get("completeness") or {}
        blueprint_report["verification_state"] = blueprint_meta.get(
            "verification_state")
        review = blueprint_meta.get("review") or {}
        if review.get("warnings"):
            blueprint_report["review_required"] = True
            blueprint_report["review_warnings"] = list(review["warnings"])
            result["mark_scheme_review_required"] = True
            result["mark_scheme_warnings"] = list(review["warnings"])

        # ---- QUESTION-INVENTORY GAPS: detect, retry narrowly, never zero ---
        # 6-8 of the "questions 62-69 are UNMARKED" class: the marker omitted
        # questions the mark scheme requires. apply_blueprint inserts
        # status=REVIEW placeholders, which merge_batches reads as plain
        # zeros — the bug. Recovery re-marks ONLY the gap using a prompt
        # narrowed to the missing questions, merges the recovered rows in,
        # re-reconciles and re-validates BEFORE persisting anything.
        # ---- WHICH gap is authoritative -----------------------------------
        # ``merge_batches`` keys its contributions by the marker's RAW ids, so
        # a marker that answers the sub-parts of a question ("1(a)", "1(b)(i)")
        # looks to it like question "1" was never answered. That list is an
        # ADVISORY pre-reconciliation signal (it is kept in the report as
        # ``missing_questions_pre_reconciliation``); the AUTHORITATIVE coverage
        # check is ``apply_blueprint``, which groups those sub-parts onto the
        # question the mark scheme actually declares.
        #
        # Using the advisory list here was a real defect: on a live 49-page
        # submission it reported 31 phantom gaps against a mark scheme that
        # was — per the reconciliation — 69/69 complete. That (a) refused the
        # targeted retry because the phantom count exceeded the safety cap,
        # and (b) parked a fully marked, fully verified correction in the
        # teacher-review queue with review_required=1.
        advisory_missing = list(merged["report"]["missing_questions"])
        if blueprint_report.get("applied"):
            missing_after_merge = list(blueprint_report.get("missing_questions") or [])
        else:
            # No mark-scheme structure was applied, so the raw gap is all we
            # have to work with.
            missing_after_merge = advisory_missing
        attempted_json = job.get("recovery_json")
        previous_state = db.loads(attempted_json, {}) or {}
        attempts_used = int(previous_state.get("rounds", 0) or 0)
        previously_recovered = {str(q) for q in
                                (previous_state.get("recovered") or [])}
        cfg_recovery = get_config().large_pdf

        # ---- BOUNDED, CHUNKED TARGETED RECOVERY -------------------------------
        # A real 49-page submission came back with 32 questions (128 marks)
        # unmarked because one batch answered a fraction of its pages. The
        # earlier single-round gate refused any gap bigger than 12 questions,
        # so the whole gap stayed unresolved purely because of its SIZE. The
        # gap is now re-marked in bounded chunks of ``recovery_chunk_questions``
        # (the same narrow-prompt retry — never a full re-mark), at most
        # ``recovery_max_chunks`` requests per job, and whatever is still
        # unresolved afterwards keeps an explicit reason saying so.
        plan = plan_chunked_recovery(
            missing=missing_after_merge,
            chunk_size=cfg_recovery.recovery_chunk_questions,
            max_chunks=cfg_recovery.recovery_max_chunks,
            already_attempted=attempts_used,
            scheme_questions=self._scheme_question_count(job))
        block_reason = plan.blocked_reason

        recovered_rows: dict[str, dict] = {}
        chunk_log: list[dict] = []
        recovery_error = None
        if plan.chunks:
            try:
                client = self._recovery_client()
                # Upload through the chunk client itself: the file/key
                # ownership it records is what lets its cross-key guard
                # re-upload instead of dying on a 403 mid-retry.
                student_remote = self._upload_for_recovery(job, client=client)
                scheme_remote = self._upload_for_recovery(
                    job, purpose="mark_scheme", client=client)
                try:
                    for chunk in plan.chunks:
                        outcome = self._recovery_engine(
                            client=client, chunk=chunk.questions,
                            student_remote=[student_remote],
                            scheme_remote=scheme_remote,
                            mark_scheme_path=job["mark_scheme_path"],
                            context_note=(f"Chunk {chunk.index + 1} of "
                                          f"{len(plan.chunks)}"))
                        attempts_used += 1
                        if outcome.recovered:
                            recovered_rows.update(outcome.recovered)
                        if outcome.error:
                            recovery_error = outcome.error
                        chunk_log.append({
                            "chunk": chunk.index,
                            "questions": list(chunk.questions),
                            "recovered": sorted(outcome.recovered),
                            "still_missing": list(outcome.still_missing),
                            "error": outcome.error,
                        })
                        log_event("large_pdf", "recovery chunk applied",
                                  job_id=job_id, chunk=chunk.index,
                                  questions=chunk.questions,
                                  recovered=sorted(outcome.recovered),
                                  error=outcome.error)
                        # Persist AFTER EACH chunk: a crash or a repeated call
                        # can never re-spend a request that already ran.
                        self._persist_recovery(job_id, {
                            "rounds": attempts_used,
                            "missing_reported": list(missing_after_merge),
                            "advisory_missing": advisory_missing,
                            "recovered": sorted(
                                previously_recovered
                                | set(recovered_rows)),
                            "chunks": chunk_log,
                            "deferred": list(plan.deferred),
                            "error": recovery_error,
                            "blocked_reason": block_reason,
                        })
                        still = [q for q in missing_after_merge
                                 if q not in recovered_rows]
                        if not still:
                            break            # nothing left to recover
                finally:
                    for remote in (student_remote, scheme_remote):
                        try:
                            client.files_delete(remote)
                        except Exception:
                            pass
            except Exception as exc:  # noqa: BLE001 — recovery must never fail the job
                recovery_error = str(exc)[:500]
                log.exception("large-pdf recovery failed for %s", job_id)

        if recovered_rows:
            merged_q = {str(q.get("question_number")): q
                        for q in result.get("questions", [])}
            for orig, row in recovered_rows.items():
                if row:  # never overwrite a VALID row with a recovery row
                    merged_q[orig] = row
            result["questions"] = list(merged_q.values())
            result, blueprint_report = apply_blueprint(result, blueprint)
            verification = verify_result(result, blueprint_report)
            earned, available, percentage = totals_for(result)
            recovered_qids = [str(q.get("question_number"))
                              for q in recovered_rows.values() if q]
            log_event("large_pdf", "recovery chunks applied", job_id=job_id,
                      recovered=recovered_qids,
                      still_missing=[q for q in missing_after_merge
                                     if q not in recovered_rows],
                      chunks=len(chunk_log), error=recovery_error)

        # Any question that survived every retry keeps an EXPLICIT unresolved
        # reason naming what was tried — never a bare 0 and never a silence.
        unresolved_now = [q for q in
                          (blueprint_report.get("missing_questions") or [])
                          if q not in recovered_rows] \
            if blueprint_report.get("applied") else \
            [q for q in missing_after_merge if q not in recovered_rows]
        unresolved_note = None
        if unresolved_now:
            unresolved_note = unresolved_reason(
                chunks_run=len(chunk_log),
                recovered=len([r for r in recovered_rows.values() if r]),
                total=len(missing_after_merge),
                error=recovery_error, blocked_reason=block_reason)

        # The budget is durable: a restart / repeated finalize never re-runs a
        # chunk that already ran, and a refused retry always says WHY.
        recovery_state = {
            "rounds": attempts_used,
            "missing_reported": list(missing_after_merge),
            "advisory_missing": advisory_missing,
            "recovered": sorted(set(recovered_rows) | previously_recovered),
            "chunks": chunk_log or list(previous_state.get("chunks") or []),
            "deferred": list(plan.deferred),
            "error": recovery_error,
            "blocked_reason": block_reason,
        }
        if plan.chunks or plan.deferred or missing_after_merge:
            self._persist_recovery(job_id, recovery_state)

        verification = verify_result(result, blueprint_report)
        earned, available, percentage = totals_for(result)

        # ---- explicit per-question states -----------------------------------
        # Upgrade the missing-result placeholders to an explicit NEEDS_REVIEW
        # state (never a confirmed 0), and label evidence-backed zeros as
        # CONFIRMED_ZERO so the UI/report can distinguish "the student wrote
        # nothing and earned nothing" from "the marker never answered".
        from ..engine.question_inventory import (
            confirmed_zero_status, mark_missing_results,
        )
        upgraded = mark_missing_results(result)
        if upgraded:
            log_event("large_pdf", "missing questions marked NEEDS_REVIEW",
                      job_id=job_id, count=upgraded)
        if unresolved_note:
            # Replace the generic placeholder reason with the OUTCOME of the
            # retries — which chunks ran, what they recovered, why this
            # question is still unresolved — for exactly the questions that
            # are still missing. A teacher must never see a bare 0, and a row
            # flagged for an unrelated reason keeps ITS reason.
            still_unmarked = {str(q) for q in unresolved_now}
            retry_outcome = {
                "chunks_run": len(chunk_log),
                "recovered": sorted(str(r.get("question_number"))
                                    for r in recovered_rows.values() if r),
                "still_unmarked": list(unresolved_now),
                "blocked_reason": block_reason,
                "last_error": recovery_error,
            }
            for q in result.get("questions") or []:
                if str(q.get("question_number")) not in still_unmarked:
                    continue
                if str(q.get("status") or "").upper() not in (
                        "REVIEW", "NEEDS_REVIEW", "UNMARKED"):
                    continue
                q["review_reason"] = unresolved_note
                q["retry_outcome"] = dict(retry_outcome)
        zero_rows = 0
        for q in result.get("questions") or []:
            if confirmed_zero_status(q) == "CONFIRMED_ZERO" and \
                    not q.get("status"):
                q["status"] = "CONFIRMED_ZERO"
                zero_rows += 1
        if zero_rows:
            log_event("large_pdf", "confirmed zeros labelled",
                      job_id=job_id, count=zero_rows)

        # ---- HONEST SCORE BASIS ---------------------------------------------
        # An UNMARKED question is the marker's failure, not the student's. The
        # old finalize divided by the FULL paper, so 32 questions the marker
        # never returned were charged to the student and a 124/138 = 89.9%
        # script was reported and stored as 124/266 = 46.6% (grade 4). The
        # score is now computed over the questions that WERE marked, with the
        # unresolved remainder named explicitly (and the job still held in
        # review, so nothing is published on a partial paper).
        breakdown = score_breakdown(result)
        if breakdown["unresolved_marks"]:
            log_event("large_pdf", "score computed on marked questions only",
                      job_id=job_id,
                      marked_earned=breakdown["earned"],
                      marked_available=breakdown["available"],
                      paper_available=breakdown["paper_available"],
                      unresolved=breakdown["unresolved_count"],
                      unresolved_marks=breakdown["unresolved_marks"])
        earned = breakdown["earned"]
        available = breakdown["available"]
        percentage = breakdown["percentage"]
        verification = dict(verification)
        verification.update({
            "earned": earned,
            "available": available,
            "percentage": round(percentage, 2),
            "provisional_grade": legacy.percentage_band_grade(percentage),
            "score_basis": breakdown["score_basis"],
            "paper_total_marks": breakdown["paper_available"],
            "unresolved_questions": breakdown["unresolved_questions"],
            "unresolved_count": breakdown["unresolved_count"],
            "unresolved_marks": breakdown["unresolved_marks"],
        })
        if breakdown["unresolved_marks"]:
            verification.setdefault("anomalies", []).append(
                f"{breakdown['unresolved_count']} question(s) worth "
                f"{breakdown['unresolved_marks']} marks are UNMARKED (the "
                f"marker returned no result). The score shown is over the "
                f"{available} marks that WERE marked; it is not a score out "
                f"of {breakdown['paper_available']} and must not be published "
                f"until the unmarked questions are resolved.")
        result["totals"] = {
            "earned": earned, "available": available,
            "paper_available": breakdown["paper_available"],
            "percentage": round(percentage, 2),
            "score_basis": breakdown["score_basis"],
            "unresolved_questions": breakdown["unresolved_questions"],
            "unresolved_marks": breakdown["unresolved_marks"],
        }

        report = merged["report"]
        # The remaining gap is read from the LATEST reconciliation (post
        # recovery when the chunks ran), never from the advisory raw-id list.
        remaining_missing = list(blueprint_report.get("missing_questions") or []) \
            if blueprint_report.get("applied") else advisory_missing
        report["incomplete_batches"] = incomplete_batches
        thin_batches = list(report.get("thin_batches") or [])

        # ---- is the paper's coverage PROVEN? ---------------------------------
        # The batch-level incompleteness signals (an INCOMPLETE response, a
        # batch far thinner than its siblings) exist to catch unmarked pages.
        # Once the targeted retry has accounted for every question the mark
        # scheme declares — nothing missing and nothing unresolved — the
        # coverage IS complete, so those signals are recorded as warnings
        # instead of parking a complete correction in the review queue.
        coverage_verified = (
            bool(blueprint_report.get("applied"))
            and not remaining_missing
            and not breakdown["unresolved_marks"])

        needs_review = (
            bool(review_batches)
            or (bool(incomplete_batches or thin_batches) and not coverage_verified)
            or bool(remaining_missing)
            or bool(report["cross_owned"])
            or bool(breakdown["unresolved_marks"])
            or verification.get("status") == "REVIEW")
        recovered_by_retry = (coverage_verified
                              and bool(incomplete_batches or thin_batches
                                       or chunk_log))
        report["coverage_verified"] = coverage_verified
        report["recovered_by_retry"] = (
            sorted(str(q.get("question_number"))
                   for q in recovered_rows.values() if q)
            if recovered_by_retry else [])
        if recovered_by_retry:
            verification.setdefault("warnings", []).append(
                "The marker first returned an incomplete result for "
                f"{len({str(q.get('question_number')) for q in recovered_rows.values() if q})} "
                "question(s); the targeted retry re-marked the gap in "
                f"{len(chunk_log)} bounded chunk(s) and every mark-scheme "
                "question now has a mark.")
        if thin_batches and not coverage_verified:
            verification.setdefault("warnings", []).append(
                "Batch(es) "
                + ", ".join(str(entry["batch"] + 1) for entry in thin_batches)
                + " returned far fewer questions than the other batches for "
                  "the same number of pages — the mark of those pages may be "
                  "incomplete, so this correction is held for teacher review.")
        if incomplete_batches and not coverage_verified:
            verification.setdefault("warnings", []).append(
                "Batch(es) "
                + ", ".join(str(i + 1) for i in incomplete_batches)
                + " returned an INCOMPLETE response even after re-marking. "
                  "The pages they own may hold unmarked questions, so this "
                  "correction is held for teacher review.")
        log_event("large_pdf", "merge complete", job_id=job_id,
                  questions=report["merged_questions"],
                  missing=len(remaining_missing),
                  advisory_missing=len(advisory_missing),
                  conflicts=len(report["conflicts"]),
                  review=needs_review)

        if needs_review:
            # Do NOT finalize an incomplete/ambiguous correction. Persist the
            # merged draft (flagged) so the teacher can inspect and review it.
            result_id = self._persist_result(
                job, result, verification, earned, available, percentage,
                review=1)
            return store.set_status(
                job_id, states.AWAITING_REVIEW,
                detail="Awaiting teacher review", result_id=result_id,
                batch_count=len(batches), page_count=job["page_count"])

        result_id = self._persist_result(
            job, result, verification, earned, available, percentage, review=0)
        self._cleanup_delegated(job_id)
        return store.set_status(
            job_id, states.COMPLETED, detail="Correction completed",
            result_id=result_id, batch_count=len(batches),
            page_count=job["page_count"])

    # -- retry / cancel / recovery ------------------------------------------

    def retry_batch(self, job_id: str, batch_index: int) -> dict:
        """Retry ONE failed batch without touching the successful ones."""
        job = store.get_job(job_id)
        if job is None:
            raise KeyError(job_id)
        if job["status"] == states.CANCELLED:
            return job
        store.reset_batch(job_id, batch_index)
        with db.transaction() as conn:
            conn.execute(
                "UPDATE large_pdf_jobs SET status = ?, updated_at = ? "
                "WHERE id = ? AND status = ?",
                (states.READY, time.time(), job_id, states.FAILED))
        return self.process(job_id)

    def retry_job(self, job_id: str) -> dict:
        job = store.get_job(job_id)
        if job is None:
            raise KeyError(job_id)
        for b in store.list_batches(job_id):
            if b["status"] == states.BATCH_FAILED:
                store.reset_batch(job_id, b["batch_index"])
        with db.transaction() as conn:
            conn.execute(
                "UPDATE large_pdf_jobs SET status = ?, error = NULL, "
                "error_code = NULL, updated_at = ? WHERE id = ?",
                (states.READY, time.time(), job_id))
        return self.process(job_id)

    def cancel(self, job_id: str) -> dict:
        store.set_status(job_id, states.CANCELLED, detail="Cancelled")
        self._cleanup_job_files(job_id)
        self._cleanup_delegated(job_id)
        return store.get_job(job_id) or {}

    def _cleanup_job_files(self, job_id: str) -> None:
        shutil.rmtree(DATA_DIR / "tmp" / "large_pdf" / job_id,
                      ignore_errors=True)
        self._batch_paths.pop(job_id, None)

    def _cleanup_delegated(self, job_id: str) -> None:
        """Remove the source files a ROUTED long document no longer needs.

        A document delegated from the normal pipeline shares its id with a
        ``correction_jobs`` row and owns the uploaded/downloaded student PDF and
        mark scheme. Once the large job is truly terminal those inputs are
        removed by the SAME cleanup the normal pipeline uses, so privacy
        retention is identical whichever pipeline corrected the document. A
        job merely AWAITING_REVIEW keeps its inputs, because a teacher may
        still re-mark it.
        """
        try:
            # Only a DELEGATED job (one sharing an id with the normal pipeline)
            # owns source files this cleanup understands.
            if db.query_one("SELECT id FROM correction_jobs WHERE id = ?",
                            (job_id,)) is None:
                return
            from ..engine.service import cleanup_job_files
            cleanup_job_files(job_id)
        except Exception:  # noqa: BLE001 — cleanup must never fail the job
            log.exception("delegated source cleanup failed for %s", job_id)

    # -- helpers -------------------------------------------------------------

    def _upload_for_recovery(self, job: dict, purpose: str = "submission",
                             client=None):
        """Upload (or reuse) the source file the retry round operates on.

        The upload MUST go through the SAME client instance that will run the
        chunk requests. Gemini Files are scoped to the API key that uploaded
        them, and the client's cross-key guard can only re-upload a file when
        it knows which key owns it (``_file_owner``). A separate upload client
        loses that tracking, so a chunk routed to another key died with
        ``403 PERMISSION_DENIED`` on this very file (observed on real runs).
        """
        path = (job["student_pdf_path"] if purpose == "submission"
                else job["mark_scheme_path"])
        if client is None:
            if get_config().mock_mode:
                from ..gemini.mock import MockGeminiClient
                client = MockGeminiClient()
            else:
                from ..gemini.client_proxy import KeyAwareClient
                from ..gemini.tester import KeyManagerHolder
                client = KeyAwareClient(KeyManagerHolder.instance())
        if hasattr(client, "upload_file"):
            return client.upload_file(
                path, purpose=purpose,
                shared=(purpose == "mark_scheme"))
        # Mock parity: the offline client exposes ``upload(file=...)``.
        return client.upload(file=path)

    def _load_plan(self, job: dict):
        raw = db.loads(job.get("plan_json"), {}) or {}
        plan_dict = raw.get("plan")
        if not plan_dict:
            raise RuntimeError("This job has no saved batch plan.")
        return batching.BatchPlan.from_dict(plan_dict)

    def _get_blueprint(self, job: dict):
        try:
            if get_config().mock_mode:
                from ..gemini.mock import MockGeminiClient
                client = MockGeminiClient()
            else:
                from ..gemini.client_proxy import KeyAwareClient
                from ..gemini.tester import KeyManagerHolder
                client = KeyAwareClient(KeyManagerHolder.instance())
            return get_blueprint(client, job["mark_scheme_path"])
        except Exception as exc:  # noqa: BLE001
            return None, {"source": "unavailable", "error": str(exc)[:200]}

    def _recovery_client(self):
        """The client a recovery chunk marks with (mock mode stays offline)."""
        if get_config().mock_mode:
            from ..gemini.mock import MockGeminiClient
            return MockGeminiClient()
        from ..gemini.client_proxy import KeyAwareClient
        from ..gemini.tester import KeyManagerHolder
        return KeyAwareClient(KeyManagerHolder.instance())

    def _scheme_question_count(self, job: dict) -> int | None:
        """How many questions the official scheme declares (None if unknown)."""
        blueprint = self._cached_blueprint(job)
        rows = (blueprint or {}).get("questions") if blueprint else None
        return len(rows) if isinstance(rows, list) and rows else None

    def _persist_recovery(self, job_id: str, state: dict) -> None:
        """Record retry progress durably (after every chunk)."""
        db.execute(
            "UPDATE large_pdf_jobs SET recovery_json = ? WHERE id = ?",
            (json.dumps(state, ensure_ascii=False), job_id))

    def _cached_blueprint(self, job: dict) -> dict | None:
        """The mark scheme's question structure for batch context (cached).

        Used for the batch prompt and the coverage check. Best-effort: a
        missing structure must never block marking — the coverage check simply
        does not run, and the merge-level reconciliation still reports every
        question the marker did not return.
        """
        try:
            blueprint, _meta = self._get_blueprint(job)
        except Exception:  # noqa: BLE001
            return None
        return blueprint if isinstance(blueprint, dict) else None

    def _ensure_correction_job(self, conn, job: dict) -> None:
        """Mirror the large job into ``correction_jobs``.

        ``results`` references ``correction_jobs(id)``, and every existing
        surface (Results, Review Queue, Reports, publishing) reads that table.
        Writing one linked row here is what lets the merged long-document
        result flow through the unchanged reporting/approval pipeline instead
        of creating a parallel grade store.
        """
        now_ts = time.time()
        conn.execute(
            """
            INSERT INTO correction_jobs(
                id, submission_id, coursework_id, course_id, student_id,
                student_name, status, stage_detail, attempts, max_attempts,
                created_at, updated_at, finished_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(id) DO UPDATE SET
                status = excluded.status,
                stage_detail = excluded.stage_detail,
                updated_at = excluded.updated_at,
                finished_at = excluded.finished_at
            """,
            (job["id"], job.get("submission_id"), job.get("coursework_id"),
             job.get("course_id"), job.get("student_id"),
             job.get("student_name"), "COMPLETED",
             "Large PDF Worker — merged long-document correction",
             0, job.get("max_attempts") or 3, job.get("created_at") or now_ts,
             now_ts, now_ts),
        )

    def _persist_result(self, job: dict, result: dict, verification: dict,
                        earned: int, available: int, percentage: float,
                        *, review: int) -> str:
        """Persist the merged result with the same columns the pool uses."""
        provisional = legacy.percentage_band_grade(percentage)
        review_flag = int(review)
        now_ts = time.time()
        with db.transaction() as conn:
            self._ensure_correction_job(conn, job)
            existing = None
            if job["coursework_id"] and job["submission_id"]:
                existing = conn.execute(
                    "SELECT * FROM results WHERE coursework_id = ? "
                    "AND submission_id = ?",
                    (job["coursework_id"], job["submission_id"])).fetchone()
            if existing:
                result_id = existing["id"]
                from ..engine import versions
                # The same teacher-override invariant as standard correction:
                # preserve the candidate for review, never replace a human mark.
                if existing["manual_override"]:
                    versions.record_pending(
                        conn, result_id=result_id, earned=earned, available=available,
                        percentage=percentage, provisional_grade=provisional,
                        marking_model=None, key_fingerprint=None, confidence="REVIEW",
                        confidence_reasons=["Teacher override preserved"], verification=verification,
                        result=result, created_at=now_ts, note="Large-PDF candidate; teacher override preserved")
                    return result_id
                versions.record(
                    conn, result_id=result_id, earned=existing["earned"], available=existing["available"],
                    percentage=existing["percentage"], provisional_grade=existing["provisional_grade"],
                    marking_model=existing["marking_model"], key_fingerprint=existing["key_fingerprint"],
                    confidence=existing["confidence"], confidence_reasons=existing["confidence_reasons"],
                    verification=existing["verification_json"], result=existing["result_json"],
                    created_at=now_ts, note="Original before large-PDF correction")
                conn.execute(
                    """
                    UPDATE results SET
                        job_id = ?, earned = ?, available = ?, percentage = ?,
                        provisional_grade = ?,
                        student_id = COALESCE(?, student_id),
                        student_name = COALESCE(?, student_name),
                        verification_json = ?, result_json = ?,
                        confidence_reasons = ?, review_required = ?,
                        manual_override = 0, created_at = ?
                    WHERE id = ?
                    """,
                    (job["id"], earned, available, percentage, provisional,
                     job.get("student_id"), job.get("student_name"),
                     json.dumps(verification, ensure_ascii=False),
                     json.dumps(result, ensure_ascii=False),
                     json.dumps([f"Large PDF Worker "
                                 f"({job['batch_count'] or 0} batches)"],
                                ensure_ascii=False),
                     review_flag, now_ts, result_id),
                )
            else:
                result_id = uuid.uuid4().hex
                conn.execute(
                    """
                    INSERT INTO results(
                        id, job_id, coursework_id, submission_id, student_id,
                        student_name, earned, available, percentage,
                        provisional_grade, verification_json, result_json,
                        confidence_reasons, review_required, manual_override,
                        created_at)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,0,?)
                    """,
                    (result_id, job["id"], job["coursework_id"],
                     job["submission_id"], job.get("student_id"),
                     job.get("student_name"), earned, available, percentage,
                     provisional, json.dumps(verification, ensure_ascii=False),
                     json.dumps(result, ensure_ascii=False),
                     json.dumps([f"Large PDF Worker "
                                 f"({job['batch_count'] or 0} batches)"],
                                ensure_ascii=False),
                     review_flag, now_ts),
                )
            from ..engine import scheme_audit, versions
            scheme_audit.record_from_result(conn, job=job, result_id=result_id,
                                            result=result, now_ts=now_ts)
            versions.record(
                conn, result_id=result_id, earned=earned, available=available,
                percentage=percentage, provisional_grade=provisional,
                marking_model=None, key_fingerprint=None, confidence=None,
                confidence_reasons=["Large PDF Worker"], verification=verification,
                result=result, created_at=now_ts, note="Large-PDF merged correction")
        return result_id


# ------------------------------------------------------------------- holder

class LargePdfRunnerHolder:
    _instance: LargePdfRunner | None = None
    _lock = threading.Lock()

    @classmethod
    def instance(cls) -> LargePdfRunner:
        with cls._lock:
            if cls._instance is None:
                cls._instance = LargePdfRunner()
            return cls._instance

    @classmethod
    def reset(cls) -> None:
        with cls._lock:
            cls._instance = None
