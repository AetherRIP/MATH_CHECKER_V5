"""REST surface for the dedicated Large PDF Worker page.

Additive only: the existing correction routes are untouched. Every endpoint
returns teacher-safe data — page ranges, batch states, question counts — and
never exposes file paths, API keys or full answer text. Technical diagnostics
stay on the developer diagnostics page.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path

from fastapi import APIRouter, File, Form, UploadFile

from .. import db
from ..config import ensure_dirs, get_config
from ..errors import AppError
from . import runner as runner_mod
from . import states, store

router = APIRouter(prefix="/api")

_ALLOWED_SUFFIXES = (".pdf",)
_MAX_UPLOAD = 200 * 1024 * 1024  # long answer booklets can be large


def register(app) -> None:
    app.include_router(router)


def _runner():
    return runner_mod.LargePdfRunnerHolder.instance()


def _data_dir() -> Path:
    from .. import config
    return config.DATA_DIR


# ------------------------------------------------------------------ reads

def _job_progress(job: dict) -> dict:
    """REAL per-job progress for the Workers page (no simulated percentages).

    Every number is read from the persisted job + batch rows the scheduler
    and runner maintain, so the display is exactly what the backend knows:

    * ``stage`` — the current pipeline stage (label + code);
    * ``current_batch`` / ``batch_total`` — e.g. 3 of 5;
    * ``pages_done`` / ``page_count`` — owned pages already completed;
    * ``current_batch_pages`` — the page range being processed right now;
    * ``questions_total`` / ``questions_marked`` — detected vs marked questions
      (from the plan evidence and completed batches' validations).
    """
    job_id = job["id"]
    batches = store.list_batches(job_id)
    total_batches = job.get("batch_count") or len(batches)
    page_count = job.get("page_count") or 0
    status = job.get("status") or ""

    completed_batches = [b for b in batches if b["status"] in (
        states.BATCH_COMPLETED, states.BATCH_REVIEW_REQUIRED)]
    processing = next((b for b in batches
                       if b["status"] == states.BATCH_PROCESSING), None)

    # Current batch: what is running right now, else the first unfinished one.
    current_batch = processing or next(
        (b for b in batches if b["status"] == states.BATCH_PENDING), None)
    if current_batch is None:
        current_batch = batches[0] if batches else None

    questions_total = 0
    questions_marked = 0
    for b in batches:
        ids = db.loads(b.get("question_ids"), []) or []
        questions_total += len(ids)
        validation = db.loads(b.get("validation_json"), {}) or {}
        if b["status"] in (states.BATCH_COMPLETED, states.BATCH_REVIEW_REQUIRED):
            questions_marked += (validation.get("question_count") or 0)

    # Fallback when the planner could not detect question numbers: count the
    # questions the plan's question-page evidence recorded instead.
    if questions_total == 0 and not batches:
        plan = db.loads(job.get("plan_json"), {}) or {}
        for pages in (plan.get("plan") or {}).get("question_pages", {}).values():
            questions_total += 1

    # Stage label mirrors the persisted state machine — it IS the stage.
    stage = {
        "state": status or "QUEUED",
        "label": states.progress_message(status, pages=page_count,
                                         batch_total=total_batches),
    }

    pages_done = sum(b["page_end"] - b["page_start"] + 1
                     for b in completed_batches)
    return {
        "stage": stage,
        "current_batch": (current_batch["batch_index"] + 1) if current_batch else None,
        "batch_total": total_batches or None,
        "page_count": page_count,
        "pages_done": pages_done,
        "current_batch_pages": ({"start": current_batch["page_start"],
                                 "end": current_batch["page_end"]}
                                if current_batch else None),
        "questions_total": questions_total or None,
        "questions_marked": questions_marked,
        "batch_summary": store.summary(job_id),
    }


@router.get("/large-pdf/jobs")
def list_large_pdf_jobs():
    jobs = store.list_jobs(limit=200)
    counts = store.counts()
    return {
        "config": {
            "enabled": get_config().large_pdf.enabled,
            "threshold_pages": get_config().large_pdf.threshold_pages,
            "target_min": get_config().large_pdf.target_min,
            "target_max": get_config().large_pdf.target_max,
            "concurrency": get_config().large_pdf.concurrency,
        },
        "counts": {
            "queued": counts.get(states.QUEUED, 0),
            "inspecting": counts.get(states.INSPECTING, 0),
            "planning": counts.get(states.PLANNING, 0),
            "ready": counts.get(states.READY, 0),
            "processing": counts.get(states.PROCESSING, 0),
            "awaiting_review": counts.get(states.AWAITING_REVIEW, 0),
            "completed": counts.get(states.COMPLETED, 0),
            "failed": counts.get(states.FAILED, 0),
        },
        "jobs": [_job_public(j) for j in jobs],
        "progress": {j["id"]: _job_progress(j) for j in jobs
                     if j.get("status") not in (states.COMPLETED,
                                                states.CANCELLED)},
    }


@router.get("/large-pdf/jobs/{job_id}")
def get_large_pdf_job(job_id: str):
    job = store.get_job(job_id)
    if job is None:
        raise AppError("That large-PDF job does not exist.", status=404,
                       code="not_found")
    payload = _job_public(job, detailed=True)
    payload["batches"] = [_batch_public(b) for b in store.list_batches(job_id)]
    payload["summary"] = store.summary(job_id)
    plan = db.loads(job.get("plan_json"), {}) or {}
    payload["plan"] = plan.get("plan")
    payload["plan_notes"] = plan.get("notes") or []
    return payload


@router.get("/large-pdf/config")
def get_large_pdf_config():
    cfg = get_config().large_pdf
    return {
        "enabled": cfg.enabled,
        "threshold_pages": cfg.threshold_pages,
        "target_min": cfg.target_min,
        "target_max": cfg.target_max,
        "concurrency": cfg.concurrency,
        "max_retries": cfg.max_retries,
        "context_overlap": cfg.context_overlap,
    }


# ------------------------------------------------------------------ writes

@router.post("/large-pdf/jobs")
async def create_large_pdf_job(
    submission: UploadFile = File(...),
    mark_scheme: UploadFile = File(...),
    student_name: str = Form("Student"),
):
    """Upload a student answer PDF + mark scheme, inspect, and plan batches.

    Inspection + planning happen synchronously (fast, deterministic, no AI);
    marking is left to the scheduler so the request returns immediately and a
    browser refresh cannot interrupt it.
    """
    if not get_config().large_pdf.enabled:
        raise AppError("The Large PDF Worker is disabled in settings.",
                       status=400, code="disabled")
    ensure_dirs()
    staging = _data_dir() / "tmp" / "large_pdf_uploads"
    staging.mkdir(parents=True, exist_ok=True)
    token = uuid.uuid4().hex
    saved: dict[str, str] = {}
    for kind, upload in (("submission", submission), ("mark_scheme", mark_scheme)):
        suffix = Path(upload.filename or f"{kind}.pdf").suffix.lower() or ".pdf"
        if suffix not in _ALLOWED_SUFFIXES:
            raise AppError(
                f"The Large PDF Worker accepts PDF files only (got {suffix}).",
                status=400, code="bad_file_type")
        content = await upload.read()
        if len(content) > _MAX_UPLOAD:
            raise AppError(f"{kind} file is larger than 200 MB.", status=413,
                           code="file_too_large")
        out = staging / f"{token}_{kind}{suffix}"
        out.write_bytes(content)
        saved[kind] = str(out)

    job = _runner().enqueue_manual(
        student_name=student_name, student_pdf_path=saved["submission"],
        mark_scheme_path=saved["mark_scheme"])
    job = _runner().prepare(job["id"])
    return _job_public(job, detailed=True)


@router.post("/large-pdf/jobs/{job_id}/process")
def process_large_pdf_job(job_id: str):
    job = store.get_job(job_id)
    if job is None:
        raise AppError("That large-PDF job does not exist.", status=404,
                       code="not_found")
    return _job_public(_runner().process(job_id), detailed=True)


@router.post("/large-pdf/jobs/{job_id}/batches/{batch_index}/retry")
def retry_large_pdf_batch(job_id: str, batch_index: int):
    job = store.get_job(job_id)
    if job is None:
        raise AppError("That large-PDF job does not exist.", status=404,
                       code="not_found")
    if store.get_batch(job_id, batch_index) is None:
        raise AppError("That batch does not exist.", status=404,
                       code="not_found")
    return _job_public(_runner().retry_batch(job_id, batch_index), detailed=True)


@router.post("/large-pdf/jobs/{job_id}/retry")
def retry_large_pdf_job(job_id: str):
    job = store.get_job(job_id)
    if job is None:
        raise AppError("That large-PDF job does not exist.", status=404,
                       code="not_found")
    return _job_public(_runner().retry_job(job_id), detailed=True)


@router.post("/large-pdf/jobs/{job_id}/cancel")
def cancel_large_pdf_job(job_id: str):
    job = store.get_job(job_id)
    if job is None:
        raise AppError("That large-PDF job does not exist.", status=404,
                       code="not_found")
    return _job_public(_runner().cancel(job_id), detailed=True)


# ------------------------------------------------------------------ shaping

def _job_public(job: dict, *, detailed: bool = False) -> dict:
    summary = store.summary(job["id"]) if detailed else None
    payload = {
        "id": job["id"],
        "student_name": job.get("student_name"),
        "student_id": job.get("student_id"),
        "submission_id": job.get("submission_id"),
        "coursework_id": job.get("coursework_id"),
        "status": job.get("status"),
        "status_label": states.label(job.get("status") or ""),
        "step_detail": job.get("step_detail"),
        "page_count": job.get("page_count") or 0,
        "batch_count": job.get("batch_count") or 0,
        "result_id": job.get("result_id"),
        "attempts": job.get("attempts") or 0,
        "error": job.get("error"),
        "error_code": job.get("error_code"),
        "retryable": job.get("status") in (states.FAILED, states.AWAITING_REVIEW),
        "awaiting_review": job.get("status") == states.AWAITING_REVIEW,
        "created_at": job.get("created_at"),
        "updated_at": job.get("updated_at"),
        "finished_at": job.get("finished_at"),
    }
    if detailed and summary is not None:
        payload["summary"] = summary
    return payload


def _batch_public(batch: dict) -> dict:
    validation = db.loads(batch.get("validation_json"), {}) or {}
    return {
        "batch_index": batch["batch_index"],
        "batch_number": batch["batch_index"] + 1,
        "page_start": batch["page_start"],
        "page_end": batch["page_end"],
        "pages": batch["page_end"] - batch["page_start"] + 1,
        "status": batch["status"],
        "status_label": batch["status"].replace("_", " ").title(),
        "question_ids": db.loads(batch.get("question_ids"), []) or [],
        "validated_questions": validation.get("question_count"),
        "missing_questions": validation.get("missing_questions") or [],
        "ambiguous": bool(validation.get("problems")),
        "attempts": batch.get("attempts") or 0,
        "error": batch.get("error"),
        "error_code": batch.get("error_code"),
        "finished_at": batch.get("finished_at"),
    }
