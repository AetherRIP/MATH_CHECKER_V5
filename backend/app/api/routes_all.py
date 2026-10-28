"""All REST + SSE routes for Math Checker V5."""

from __future__ import annotations

import asyncio
import json
import logging
import queue
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request, UploadFile, File, Form
from fastapi.responses import StreamingResponse

from .. import db
from ..downloads import attachment_header
from .. import student_identity as _identity
from .. import testing_guard
from ..classroom import sync as classroom_sync
from ..classroom import client as gc
from ..classroom.auth import (
    auth_state, disconnect as classroom_disconnect, get_access_token,
    start_device_or_loopback_flow,
)
from ..config import data_dir, ensure_dirs, get_config, save_config
from ..engine import legacy_marking as legacy
from ..engine import pdf_inspect
from ..errors import AppError, brief_traceback
from ..gemini.tester import KeyManagerHolder as KMHolder, test_key
from ..jobs.manager import ACTIVE_STATUSES, JobManagerHolder
from ..logging_setup import log_event
from ..text import count as _count
from ..version import APP_VERSION, SCHEMA_VERSION
from ..version import build_info as _build
from ..demo import is_demo as _is_demo

router = APIRouter(prefix="/api")

log = logging.getLogger("api")


def register(app) -> None:
    app.include_router(router)


def _km():
    return KMHolder.instance()


def _pool():
    from ..workers.pool import WorkerPoolHolder
    return WorkerPoolHolder.instance()


def _job_manager():
    from ..jobs.manager import JobManagerHolder
    return JobManagerHolder.instance()


# ============================================================
# Workers + queue (§20/§21/§22 of the multi-worker spec)
# ============================================================

@router.get("/workers")
def workers_state():
    return _pool().public_state()


@router.post("/workers/resize")
def workers_resize(payload: dict):
    """Change the configured worker count; the pool caps it by healthy keys."""
    try:
        requested = int(payload.get("workers", 2))
    except (TypeError, ValueError):
        raise AppError("Provide an integer 'workers' value.",
                       status=400, code="bad_request")
    if not 1 <= requested <= 8:
        raise AppError("Worker count must be between 1 and 8.",
                       status=400, code="bad_request")
    cfg = get_config()
    cfg.worker.concurrency = requested
    save_config(cfg)
    actual = _pool().set_size(requested)
    state = _pool().public_state()
    return {"requested": requested, "running": actual,
            "recommended": state["recommended"], **state}


# ============================================================
# Dashboard
# ============================================================

@router.get("/dashboard")
def dashboard(request: Request):
    cfg = get_config()
    from .. import auth_gate as _ag
    identity = _ag.identity_of(request)
    counts = _job_manager().counts()
    today_start = time.time() - 24 * 3600
    corrections_today = db.query_one(
        "SELECT COUNT(*) AS n FROM results WHERE created_at >= ?", (today_start,)
    )["n"]
    pending_subs = db.query_one(
        """
        SELECT COUNT(*) AS n FROM submissions s
        JOIN coursework w ON w.id = s.coursework_id
        WHERE w.allowlisted = 1 AND s.state = 'TURNED_IN'
          AND s.id NOT IN (SELECT submission_id FROM correction_jobs
                           WHERE submission_id IS NOT NULL AND status = 'COMPLETED')
        """
    )["n"]
    keys = _km().status()
    return {
        "corrections_today": corrections_today,
        "pending": counts.get("QUEUED", 0) + pending_subs,
        "processing": sum(counts.get(s, 0) for s in
                          ("PREPARING", "DOWNLOADING", "UPLOADING", "MARKING",
                           "VERIFYING", "SAVING", "WAITING_FOR_API", "RETRYING")),
        "completed": counts.get("COMPLETED", 0),
        "failed": counts.get("FAILED", 0),
        "jobs": counts,
        "api_health": {
            "healthy": keys["healthy"], "cooldown": keys["cooldown"],
            "invalid": keys["invalid"], "total": keys["total"],
            "has_keys": keys["has_keys"], "aggregate": _km().aggregate(),
        },
        "classroom": {
            "configured": auth_state().client_configured,
            "connected": auth_state().connected,
            "email": auth_state().email,
            "last_sync": classroom_sync.last_sync(),
            "courses": db.query_one("SELECT COUNT(*) AS n FROM courses")["n"],
            "allowlisted": db.query_one(
                "SELECT COUNT(*) AS n FROM coursework WHERE allowlisted = 1")["n"],
            "pending_submissions": pending_subs,
        },
        # The role the UI shows is the SIGNED-IN account's role (read live
        # from the backend), never a client-supplied value; with no session it
        # falls back to the local role setting.
        "role": _ag.ui_role(identity["role"]) if identity else cfg.role,
        "account": (identity or {}).get("username"),
        "mock_mode": cfg.mock_mode,
        "version": APP_VERSION,
        "build_number": _build().get("build_number"),
        "build_date": _build().get("build_date"),
        "demo_mode": _is_demo(),
        "first_run": cfg.is_first_run(),
    }


# ============================================================
# Jobs + results
# ============================================================

@router.get("/jobs")
def list_jobs(status: str | None = None, limit: int = 200):
    statuses = status.split(",") if status else None
    jobs = _job_manager().list(statuses, limit)
    return {"jobs": _attach_bandwidth(jobs)}


# NOTE: "/jobs/stream" MUST be declared before "/jobs/{job_id}". FastAPI
# matches routes in declaration order, so a later literal path would be
# captured by the "{job_id}" route ("stream" parsed as a job id) and answer
# 404 — which silently broke the dashboard's live activity stream.
@router.get("/jobs/stream")
async def jobs_stream(request: Request):
    """SSE: live job stage updates (event-driven; no polling)."""
    loop = asyncio.get_running_loop()
    q: asyncio.Queue = asyncio.Queue()

    def listener(job: dict):
        loop.call_soon_threadsafe(q.put_nowait, _job_public(job))

    manager = _job_manager()
    manager.add_stage_listener(listener)

    async def gen():
        try:
            yield f"data: {json.dumps({'type': 'hello'})}\n\n"
            while True:
                if await request.is_disconnected():
                    break
                try:
                    job = await asyncio.wait_for(q.get(), timeout=15)
                    yield f"data: {json.dumps(job)}\n\n"
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
        finally:
            try:
                manager._stage_listeners.remove(listener)
            except ValueError:
                pass

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache"})


@router.get("/jobs/{job_id}")
def job_detail(job_id: str):
    svc = _correction_service()
    job = svc.get_correction_status(job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    from .. import api_stats
    from ..jobs.manager import stage_history
    job["bandwidth"] = api_stats.job_bandwidth(job_id)
    # What this correction cost in provider requests, by pipeline stage: the
    # detail panel shows it instead of the old "0 AI attempts" that only ever
    # counted successes.
    job["ai_usage"] = api_stats.job_ai_usage(job_id)
    # The stages the backend really recorded, oldest first (the detail panel
    # shows them with their timestamps). A terminal job's last transition is
    # appended from its OWN stored status/finished_at, so the history ends
    # where the job actually ended instead of at the last stage report.
    history = stage_history(job_id)
    if job.get("finished_at") and job.get("status"):
        if not history or history[-1].get("status") != job["status"]:
            history.append({"status": job["status"],
                            "stage": job.get("stage_detail") or job["status"],
                            "progress": job.get("progress"),
                            "at": job["finished_at"]})
    job["stage_history"] = history
    return job


@router.post("/jobs/{job_id}/cancel")
def cancel_job(job_id: str):
    ok = _pool().cancel_job(job_id) or _job_manager().cancel(job_id)
    if not ok:
        raise HTTPException(409, "Job cannot be cancelled (not active or unknown).")
    return {"cancelled": True}


@router.post("/jobs/{job_id}/retry")
def retry_job(job_id: str):
    job = _job_manager().retry(job_id)
    if not job:
        raise HTTPException(409, "Only failed or cancelled jobs can be retried.")
    _pool_wake = _pool()
    _pool_wake._wake.set()
    return job


def _job_public(job: dict) -> dict:
    """Public shape of a correction job, shared by the SSE stream and /jobs.

    Every field the front-end ``Job`` type declares must be present here, or
    streamed rows silently differ from polled ones: without ``coursework_id``
    the live activity panel cannot attribute a running job to its assignment,
    and without ``max_attempts`` the dashboard renders "attempt 1/undefined".
    """
    from .. import api_stats
    return {
        "id": job["id"], "submission_id": job["submission_id"],
        "coursework_id": job["coursework_id"], "course_id": job["course_id"],
        "student_id": job["student_id"],
        "student_name": job["student_name"],
        # Who is doing the work and what it is: the dashboard's worker cards
        # are useless without these, and a streamed row must not differ from
        # the polled one.
        "worker_id": job.get("worker_id"),
        "paper_name": job.get("paper_name"),
        "page_count": job.get("page_count"),
        "is_remark": job.get("is_remark"),
        "updated_at": job.get("updated_at"),
        # Canonical nickname -> Classroom name, so a streamed job shows the
        # same student the results list and PDF do (never a raw id).
        "display_name": _identity.resolve(
            job.get("student_id"),
            submission_id=job.get("submission_id"),
            snapshot_name=job.get("student_name"))["display_name"],
        "status": job["status"],
        "stage_detail": job["stage_detail"], "progress": job["progress"],
        "attempts": job["attempts"], "max_attempts": job["max_attempts"],
        "error": job["error"],
        "created_at": job["created_at"], "started_at": job["started_at"],
        "finished_at": job["finished_at"], "result_id": job["result_id"],
        "bandwidth": api_stats.job_bandwidth(job["id"]),
        "ai_requests": (api_stats.job_bandwidth(job["id"]) or {}).get(
            "ai_attempts") or 0,
    }


def _attach_bandwidth(jobs: list[dict]) -> list[dict]:
    """Add per-job network counters (§17) to a list of job dicts."""
    from .. import api_stats
    mapping = api_stats.job_bandwidth_map([j.get("id") for j in jobs])
    for job in jobs:
        job["bandwidth"] = mapping.get(job.get("id"))
    return jobs


def _correction_service():
    from ..main import app
    return app.state.correction


@router.get("/results")
def list_results(
    q: str | None = None,
    status: str | None = None,
    coursework_id: str | None = None,
    course_id: str | None = None,
    min_percentage: float | None = None,
    max_percentage: float | None = None,
    limit: int = 100,
    offset: int = 0,
):
    """Correction history: searchable, filterable, paginated.

    Every stored correction stays reachable (the old flat list silently
    capped at the first N rows). Rows are enriched with the assignment and
    class titles plus a single derived ``status`` and the recorded Classroom
    publish operation, so the UI can present one consistent table without
    fetching several endpoints. Response stays backward compatible: the
    ``results`` key is unchanged.
    """
    where = ["1=1"]
    params: list = []
    if q:
        like = f"%{q.strip()}%"
        where.append("(r.student_name LIKE ? OR r.student_id LIKE ? OR r.id LIKE ? "
                     "OR r.coursework_id LIKE ? OR r.submission_id LIKE ? "
                     "OR w.title LIKE ?)")
        params.extend([like] * 6)
    if coursework_id:
        where.append("r.coursework_id = ?")
        params.append(coursework_id)
    if course_id:
        where.append("w.course_id = ?")
        params.append(course_id)
    if min_percentage is not None:
        where.append("r.percentage >= ?")
        params.append(float(min_percentage))
    if max_percentage is not None:
        where.append("r.percentage <= ?")
        params.append(float(max_percentage))

    # COALESCE is essential: older rows have a NULL confidence, and a plain
    # "NOT (confidence IN (...))" evaluates to NULL for them, which would hide
    # every legacy correction from the "corrected" filter.
    review_expr = ("(r.review_required = 1 OR COALESCE(r.confidence, '') IN "
                   "('LOW','VALIDATION_FAILED','LARGE_SCORE_CHANGE'))")
    status_sql = {
        "needs_review": review_expr,
        "overridden": "r.manual_override = 1",
        "corrected": (f"(r.manual_override = 0 AND NOT {review_expr})"),
    }
    if status in status_sql:
        where.append(status_sql[status])
    where_sql = " AND ".join(where)

    base = ("FROM results r "
            "LEFT JOIN coursework w ON w.id = r.coursework_id "
            "LEFT JOIN courses c ON c.id = w.course_id")
    total = db.query_one(f"SELECT COUNT(*) AS n {base} WHERE {where_sql}",
                         tuple(params))["n"]

    limit = max(1, min(500, limit))
    offset = max(0, offset)
    rows = db.query(
        "SELECT r.id, r.job_id, r.coursework_id, r.submission_id, r.student_id, "
        "r.student_name, r.earned, r.available, r.percentage, r.provisional_grade, "
        "r.marking_model, r.created_at, r.confidence, r.review_required, "
        "r.manual_override, r.double_check_triggered, r.double_check_status, "
        "r.verification_json, "
        "w.title AS assignment_title, "
        "w.course_id AS course_id, c.name AS course_name, "
        "(SELECT p.status FROM classroom_publish_ops p "
        " WHERE p.coursework_id = r.coursework_id "
        "   AND p.submission_id = r.submission_id "
        " ORDER BY COALESCE(p.finished_at, p.created_at) DESC LIMIT 1) AS publish_status, "
        "(SELECT COUNT(*) FROM result_versions v WHERE v.result_id = r.id) "
        " AS version_count "
        f"{base} WHERE {where_sql} "
        "ORDER BY r.created_at DESC LIMIT ? OFFSET ?",
        tuple(params + [limit, offset]),
    )
    from ..engine.verify import partial_summary
    results = []
    for row in rows:
        item = _identity.decorate(dict(row))
        # A partial correction must never present its marked-subset fraction as
        # a score: the list carries the coverage it is missing instead.
        partial = partial_summary(db.loads(item.pop("verification_json", None), {}))
        if partial:
            item["partial"] = partial
        if review_expr and (item["review_required"]
                            or item["confidence"] in
                            ("LOW", "VALIDATION_FAILED", "LARGE_SCORE_CHANGE")):
            item["status"] = "needs_review"
        elif item["manual_override"]:
            item["status"] = "overridden"
        else:
            item["status"] = "corrected"
        results.append(item)
    return {"results": results, "total": total, "limit": limit, "offset": offset}


@router.get("/results/{result_id}")
def result_detail(result_id: str):
    result = _correction_service().get_result(result_id)
    if not result:
        raise HTTPException(404, "Result not found")
    # Resolve the canonical display name here so every consumer of the result
    # (page, PDF, export) gets the same student (§4/§16). The stored
    # student_name snapshot is kept alongside for history.
    return _identity.decorate(result)


@router.get("/results/{result_id}/export")
def export_result(result_id: str, format: str = "md", detail: str = "standard"):
    result = _correction_service().get_result(result_id)
    if not result:
        raise HTTPException(404, "Result not found")
    # The PDF and the exports use the same canonical name the page shows.
    result = _identity.decorate(result)
    if (format or "").lower() == "pdf":
        from .report_routes import render_result_pdf
        from fastapi.responses import Response
        if detail not in ("summary", "standard", "detailed"):
            raise AppError("detail must be summary, standard or detailed.",
                           status=400, code="bad_detail")
        buf = render_result_pdf(result, detail=detail)
        # Same predictable, sanitised name used for the Classroom attachment,
        # so a teacher who downloads twice does not end up with
        # "...Report (1).pdf" copies.
        from ..classroom.report_attachment import build_report_filename
        filename = build_report_filename(
            result.get("display_name"),
            _assignment_title(result.get("coursework_id")))
        log_event("reports", "correction report exported", result_id=result_id,
                  detail=detail)
        return Response(
            buf.getvalue(), media_type="application/pdf",
            headers={"Content-Disposition": attachment_header(filename)})
    inner = result["result_json"]
    totals = {
        "earned": result["earned"], "available": result["available"],
        "percentage": result["percentage"],
        "provisional_grade": result["provisional_grade"],
    }
    # Already resolved by _identity.decorate; the export shows the same name
    # the teacher sees on screen.
    student = result.get("display_name") or result.get("student_name") or "student"
    fmt = (format or "").lower()
    if fmt == "csv":
        import csv
        import io
        buf = io.StringIO()
        writer = csv.DictWriter(buf, fieldnames=[
            "Student", "Question", "Awarded", "Max", "Percentage",
            "Topic", "Loss Reason", "Correct Method", "Parts"])
        writer.writeheader()
        for row in legacy.build_csv_rows(student, inner):
            writer.writerow(row)
        from fastapi.responses import Response
        return Response(buf.getvalue(), media_type="text/csv",
                        headers={"Content-Disposition":
                                 attachment_header(f"marks_{student}.csv")})
    if fmt not in ("md", "markdown"):
        # Never silently return a different format than the one requested:
        # an "xlsx"/"json" download used to fall through to markdown, so the
        # file the teacher received disagreed with the label on the button.
        raise AppError(
            f"Unsupported export format '{format}'. Use pdf, csv or md.",
            status=400, code="bad_report_format")
    md = legacy.build_markdown_report(student, inner, totals)
    from fastapi.responses import Response
    return Response(md, media_type="text/markdown",
                    headers={"Content-Disposition":
                             attachment_header(f"report_{student}.md")})


# ============================================================
# Correction versioning (§D)
# ============================================================

@router.get("/results/{result_id}/versions")
def result_versions(result_id: str):
    """Append-only correction history for a result (offline, no AI call)."""
    if not db.query_one("SELECT id FROM results WHERE id = ?", (result_id,)):
        raise HTTPException(404, "Result not found")
    from ..engine import versions
    return {"result_id": result_id, "versions": versions.list_versions(result_id)}


@router.post("/results/{result_id}/versions/{version}/restore")
def restore_result_version(result_id: str, version: int):
    """Teacher explicitly chooses a historical version as final.

    Applies the snapshot to the current result row and appends a NEW version
    marked with the teacher's decision (so the choice itself is auditable).
    Never deletes a version."""
    if not db.query_one("SELECT id FROM results WHERE id = ?", (result_id,)):
        raise HTTPException(404, "Result not found")
    from ..engine import versions
    snapshot = versions.get_version(result_id, version)
    if not snapshot:
        raise AppError("That version does not exist for this result.",
                       status=404, code="version_not_found")
    now_ts = time.time()
    with db.transaction() as conn:
        conn.execute(
            "UPDATE results SET earned = ?, available = ?, percentage = ?, "
            "provisional_grade = ?, marking_model = ?, key_fingerprint = ?, "
            "confidence = ?, confidence_reasons = ?, verification_json = ?, "
            "result_json = ?, review_required = 0, manual_override = 1, "
            "created_at = ? WHERE id = ?",
            (snapshot["earned"], snapshot["available"], snapshot["percentage"],
             snapshot["provisional_grade"], snapshot["marking_model"],
             snapshot["key_fingerprint"], snapshot["confidence"],
             snapshot["confidence_reasons"], snapshot["verification_json"],
             snapshot["result_json"], now_ts, result_id),
        )
        versions.record(
            conn, result_id=result_id, earned=snapshot["earned"],
            available=snapshot["available"], percentage=snapshot["percentage"],
            provisional_grade=snapshot["provisional_grade"],
            marking_model=snapshot["marking_model"],
            key_fingerprint=snapshot["key_fingerprint"],
            confidence=snapshot["confidence"],
            confidence_reasons=db.loads(snapshot["confidence_reasons"], []),
            verification=db.loads(snapshot["verification_json"], {}),
            result=db.loads(snapshot["result_json"], {}), created_at=now_ts,
            note=f"Teacher chose version {int(version)} as final",
        )
    log_event("api", "result version restored", result_id=result_id,
              version=int(version))
    return {"restored": True, "result_id": result_id, "from_version": int(version)}


# ============================================================
# Remark / re-mark (§Remark)
# ============================================================

@router.post("/results/{result_id}/remark")
def remark_result(result_id: str):
    """Re-run AI marking for ONE already-corrected submission.

    Deliberately thin: it enqueues an ordinary correction job for the SAME
    (coursework, submission) pair, so the existing worker, verifier and
    result_guard decide what gets stored. Nothing is deleted and no new
    Classroom submission, student or assignment is created.

    A double click is safe — if a job is already running for this submission we
    return that job instead of starting (or billing for) a second one.
    """
    result = _correction_service().get_result(result_id)
    if not result:
        raise HTTPException(404, "Result not found")
    coursework_id = result.get("coursework_id")
    submission_id = result.get("submission_id")
    if not coursework_id or not submission_id:
        raise AppError(
            "This correction has no Google Classroom submission to re-mark.",
            status=400, code="remark_not_possible")
    if _is_demo():
        raise AppError("Remarking is unavailable in demo mode.", status=400,
                       code="demo_mode")

    service = _correction_service()
    from ..jobs.manager import DuplicateJobError
    # Classroom identity only — a remark must never be attributed to a
    # nickname, and never to a submission id posing as a name (§1/§3).
    _identity_now = _identity.resolve(result.get("student_id"),
                                      submission_id=submission_id)
    try:
        job = service.enqueue_classroom_submission(
            submission_id=submission_id, coursework_id=coursework_id,
            course_id=_course_id_for(coursework_id),
            student_id=_identity_now["student_id"],
            student_name=(_identity_now["classroom_name"]
                          or result.get("student_name") or "Student"),
            is_remark=True)
        job_id = job["id"]
        duplicate = False
    except DuplicateJobError as exc:
        # Already re-marking this submission — reuse the running job.
        job_id = exc.existing_id
        duplicate = True

    log_event("api", "remark started", result_id=result_id, job_id=job_id,
              course_id=_course_id_for(coursework_id),
              coursework_id=coursework_id, submission_id=submission_id,
              student_id=result.get("student_id"),
              previous_earned=result.get("earned"),
              previous_available=result.get("available"),
              previous_marking_model=result.get("marking_model"),
              duplicate=duplicate)
    return {"remarked": True, "result_id": result_id, "job_id": job_id,
            "duplicate": duplicate}


@router.get("/results/{result_id}/remark")
def remark_status(result_id: str):
    """Attempts for this correction and the Previous-vs-New comparison.

    Purely a read of the append-only version history — no AI call, no mutation.
    """
    if not db.query_one("SELECT id FROM results WHERE id = ?", (result_id,)):
        raise HTTPException(404, "Result not found")
    from ..engine import remark, versions
    return {"result_id": result_id, "history": versions.list_versions(result_id),
            **remark.summary(result_id)}


@router.post("/results/{result_id}/remark/decision")
def remark_decision(result_id: str, payload: dict):
    """Teacher chooses which attempt is the active correction.

    ``KEEP_PREVIOUS`` leaves the current attempt in place; ``USE_NEW`` promotes
    the chosen attempt. No attempt is ever deleted, and an attempt that failed
    validation is only promoted when the teacher explicitly asks for it (and is
    then flagged for review rather than silently trusted).
    """
    if not db.query_one("SELECT id FROM results WHERE id = ?", (result_id,)):
        raise HTTPException(404, "Result not found")
    from ..engine import remark
    decision = (payload or {}).get("decision")
    version = (payload or {}).get("version")
    try:
        outcome = remark.decide(result_id, decision,
                                version=int(version) if version is not None else None)
    except LookupError as exc:
        raise AppError(str(exc), status=404, code="no_history") from exc
    except ValueError as exc:
        raise AppError(str(exc), status=400, code="bad_decision") from exc

    log_event("api", "remark decision", result_id=result_id,
              decision=outcome["decision"], active_version=outcome["active_version"],
              forced_review=outcome["forced_review"])
    return outcome


@router.post("/results/{result_id}/retry")
def retry_result_operation(result_id: str, payload: dict):
    """Retry ONE downstream step, never the AI marking.

    Actions are deliberately separate so a Classroom/Drive failure can be
    retried without spending another Gemini request:

    ``report``  regenerate the PDF from the existing accepted marking
    ``upload``  upload/PATCH the report to Drive (no Classroom call)
    ``attach``  add the already-uploaded file to the student submission
    ``grade``   re-sync the Classroom draft/returned grade
    """
    action = (payload or {}).get("action")
    detail = (payload or {}).get("detail") or "standard"
    result = _correction_service().get_result(result_id)
    if not result:
        raise HTTPException(404, "Result not found")
    if detail not in ("summary", "standard", "detailed"):
        raise AppError("detail must be summary, standard or detailed.",
                       status=400, code="bad_detail")

    from ..classroom import report_attachment as ra
    course_id = _course_id_for(result.get("coursework_id"))
    title = _assignment_title(result.get("coursework_id"))

    if action == "report":
        try:
            content = ra.render_report_pdf(result, detail=detail)
        except Exception as exc:  # noqa: BLE001
            raise AppError(f"Could not regenerate the report: {exc}",
                           status=502, code="report_failed") from exc
        log_event("api", "report regenerated", result_id=result_id,
                  detail=detail, bytes=len(content))
        return {"action": "report", "ok": True, "bytes": len(content),
                "pdf_url": f"/api/results/{result_id}/export?format=pdf&detail={detail}"}

    if action in ("upload", "attach"):
        _require_classroom()
        if action == "upload":
            token = get_access_token()
            filename = ra.build_report_filename(result.get("student_name"), title)
            try:
                content = ra.render_report_pdf(result, detail=detail)
                state = ra._row_for(result_id) or {}
                drive_file_id = state.get("drive_file_id")
                if drive_file_id:
                    # Same file, newer bytes - never a second copy.
                    gc.replace_drive_file_content(token, drive_file_id, content)
                else:
                    drive_file_id = ra.upload_report_to_drive(
                        token, filename, content)["id"]
            except testing_guard.PublicationBlocked as exc:
                # Refused on purpose by the local testing-only guard: the
                # message says exactly that, so it must not be dressed up as a
                # Google Drive failure.
                raise AppError(str(exc), status=403,
                               code=ra.ERROR_TESTING_ONLY) from exc
            except Exception as exc:  # noqa: BLE001
                raise AppError(f"Google Drive upload failed: {exc}",
                               status=502, code="upload_failed") from exc
            log_event("api", "report uploaded", result_id=result_id,
                      drive_file_id=drive_file_id)
            return {"action": "upload", "ok": True, "drive_file_id": drive_file_id,
                    "filename": filename}
        # action == "attach" -> re-run only the attach + verify half.
        try:
            from ..classroom.auth import account_email as _acct
            outcome = ra.attach_report(
                get_access_token(), result, course_id=course_id,
                assignment_title=title, detail=detail,
                account_email=_acct())
        except Exception as exc:  # noqa: BLE001
            raise _classroom_error(exc)
        log_event("api", "report attach retried", result_id=result_id,
                  status=outcome.status)
        return {"action": "attach", **outcome.to_dict()}

    if action == "grade":
        _require_classroom()
        from ..classroom.auth import get_access_token as _tok
        submission_id = result.get("submission_id")
        if not submission_id:
            raise AppError("This correction has no Classroom submission to sync.",
                           status=400, code="remark_not_possible")
        coursework_id = result.get("coursework_id")
        try:
            grade = gc.patch_draft_grade(_tok(), course_id, coursework_id,
                                         submission_id,
                                         result.get("earned"))
        except Exception as exc:  # noqa: BLE001
            raise _classroom_error(exc)
        log_event("api", "classroom grade re-synced", result_id=result_id,
                  course_id=course_id, coursework_id=coursework_id,
                  submission_id=submission_id)
        return {"action": "grade", "ok": True, "draft_grade": grade}

    raise AppError(
        "action must be one of report, upload, attach or grade.",
        status=400, code="bad_action")


# ============================================================
# Manual corrections (upload flow)
# ============================================================


def _discard_saved_uploads(paths: dict[str, str]) -> None:
    """Delete files staged for an upload that was refused.

    A rejected upload must leave nothing behind: no temp file, and (because the
    caller raises before inserting pending_manual_files) no job row either.
    """
    for path in list(paths.values()):
        try:
            Path(path).unlink(missing_ok=True)
        except OSError:
            pass
    paths.clear()



@router.post("/corrections/manual")
async def manual_correction(
    submission: UploadFile = File(...),
    mark_scheme: UploadFile = File(...),
    question_paper: UploadFile | None = File(None),
    student_name: str = Form("Student"),
):
    _require_usage_budget()
    ensure_dirs()
    job_id = uuid.uuid4().hex
    saved_paths = {}
    uploads = [("submission", submission), ("mark_scheme", mark_scheme)]
    if question_paper is not None and question_paper.filename:
        uploads.append(("question", question_paper))
    for kind, upload in uploads:
        suffix = Path(upload.filename or f"{kind}.pdf").suffix.lower() or ".pdf"
        if suffix not in (".pdf", ".png", ".jpg", ".jpeg", ".webp"):
            raise AppError(f"Unsupported file type for {kind}: {suffix}",
                           status=400, code="bad_file_type")
        out = data_dir() / "tmp" / f"{job_id}_{kind}{suffix}"
        content = await upload.read()
        if len(content) > 50 * 1024 * 1024:
            raise AppError(f"{kind} file is larger than 50 MB.",
                           status=413, code="file_too_large")
        out.write_bytes(content)
        saved_paths[kind] = str(out)
        if suffix == ".pdf":
            # The bytes are the only evidence of what this file IS. A corrupt
            # or empty PDF has nothing to mark, so it is refused HERE, before a
            # job exists, instead of becoming a queued job that fails later.
            problem = pdf_inspect.integrity_problem(out)
            if problem:
                _discard_saved_uploads(saved_paths)
                raise AppError(f"The {kind} PDF cannot be marked: {problem}.",
                               status=400, code="corrupt_pdf")

    with db.transaction() as conn:
        for kind, upload in uploads:
            conn.execute(
                "INSERT INTO pending_manual_files(job_id, kind, path, filename) VALUES(?,?,?,?)",
                (job_id, kind, saved_paths[kind], upload.filename),
            )

    submission_pages = int(pdf_inspect.page_count(saved_paths["submission"]) or 0)
    job = _correction_service().enqueue_manual_pdf(
        student_name=student_name,
        paper_name=submission.filename or "Submission",
        page_count=submission_pages or None)
    # Re-point the pending rows at the real job id.
    db.execute("UPDATE pending_manual_files SET job_id = ? WHERE job_id = ?",
               (job["id"], job_id))
    return job


# ============================================================
# Unified PDF correction (§3)
# ============================================================

@router.post("/corrections/unified")
async def unified_correction(
    submission: UploadFile = File(...),
    mark_scheme: UploadFile = File(...),
    student_name: str = Form("Student"),
):
    """Correct one student PDF through the standard correction pipeline.

    Upload, persistence and queueing are unchanged; the response states the
    document's real page count so the page can report what was accepted.
    """
    _require_usage_budget()
    ensure_dirs()

    token = uuid.uuid4().hex
    staging = data_dir() / "tmp" / "unified_uploads"
    staging.mkdir(parents=True, exist_ok=True)
    saved: dict[str, str] = {}
    sizes: dict[str, int] = {}
    names: dict[str, str] = {}
    for kind, upload in (("submission", submission), ("mark_scheme", mark_scheme)):
        suffix = Path(upload.filename or f"{kind}.pdf").suffix.lower() or ".pdf"
        if suffix not in (".pdf", ".png", ".jpg", ".jpeg", ".webp"):
            raise AppError(f"Unsupported file type for {kind}: {suffix}",
                           status=400, code="bad_file_type")
        content = await upload.read()
        if len(content) > 200 * 1024 * 1024:
            raise AppError(f"{kind} file is larger than 200 MB.",
                           status=413, code="file_too_large")
        out = staging / f"{token}_{kind}{suffix}"
        out.write_bytes(content)
        saved[kind] = str(out)
        sizes[kind] = len(content)
        names[kind] = upload.filename or f"{kind}{suffix}"
        if suffix == ".pdf":
            # Content, not filename or MIME type: an unreadable PDF is refused
            # before any job row exists, so no orphaned job can be left behind.
            problem = pdf_inspect.integrity_problem(out)
            if problem:
                _discard_saved_uploads(saved)
                raise AppError(f"The {kind} PDF cannot be marked: {problem}.",
                               status=400, code="corrupt_pdf")

    page_count = int(pdf_inspect.page_count(saved["submission"]) or 0)

    job_id = uuid.uuid4().hex
    with db.transaction() as conn:
        for kind in ("submission", "mark_scheme"):
            conn.execute(
                "INSERT INTO pending_manual_files(job_id, kind, path, "
                "filename) VALUES(?,?,?,?)",
                (job_id, kind, saved[kind], names[kind]))
    job = _correction_service().enqueue_manual_pdf(
        student_name=student_name or "Student",
        paper_name=names["submission"], page_count=page_count or None)
    db.execute("UPDATE pending_manual_files SET job_id = ? WHERE job_id = ?",
               (job["id"], job_id))
    log_event("corrections", "unified correction queued",
              pipeline="standard", pages=page_count, job_id=job["id"])
    reason = (f"{page_count} pages — the standard single-pass correction "
              "workflow will mark this paper." if page_count else
              "The standard single-pass correction workflow will mark this paper.")
    return {
        "job_id": job["id"],
        "page_count": page_count,
        "reason": reason,
        "files": {"submission": {"name": names["submission"],
                                  "bytes": sizes["submission"]},
                  "mark_scheme": {"name": names["mark_scheme"],
                                  "bytes": sizes["mark_scheme"]}},
        "job": job,
    }


# ============================================================
# Gemini keys
# ============================================================

@router.get("/keys")
def keys_status():
    return _ensure_key_labels(_km().status())


def _ensure_key_labels(status: dict) -> dict:
    """Give every key a stable, safe display label (KEY_1, KEY_2, …) based on
    its position in the configured key list. Manual labels are respected;
    only missing/legacy labels are (re)assigned — never key material."""
    km = _km()
    for index, item in enumerate(status["keys"], start=1):
        label = item.get("label")
        if label is None or str(label).startswith("Gemini Key"):
            auto = f"KEY_{index}"
            if label != auto:
                km.set_label(item["fingerprint"], auto)
                item["label"] = auto
    return status


@router.post("/keys/{fingerprint}/re-enable")
def re_enable_key(fingerprint: str):
    _km().re_enable(fingerprint)
    return keys_status()


@router.post("/keys/{fingerprint}/disable")
def disable_key(fingerprint: str):
    _km().set_disabled(fingerprint, True)
    return keys_status()


@router.post("/keys/{fingerprint}/test")
def test_single_key(fingerprint: str):
    """Tiny API health check for ONE key (§12): models.list + one minimal
    generation. Never touches student data, mark schemes or Classroom."""
    match = [(i, k, fp) for i, (k, fp) in enumerate(get_config_secret_keys(), start=1)
             if fp == fingerprint]
    if not match:
        raise HTTPException(404, "Key not found")
    index, key, fp = match[0]
    state = test_key(index, key, fp)
    _km().register_manual_result(
        fp, ok=state.status == "HEALTHY",
        kind="healthy" if state.status == "HEALTHY" else (state.error_kind or "unknown"),
        message=state.error_message or ("ok" if state.status == "HEALTHY" else "failed"),
        response_time=state.response_time_ms,
    )
    return {
        "fingerprint": fp, "status": state.status,
        "auth_ok": state.auth_ok, "generate_ok": state.generate_ok,
        "response_time_ms": state.response_time_ms,
        "error_kind": state.error_kind,
        # A SUCCESS has no error_message, and a null here left the page with
        # nothing to say about a key that just passed. The add-keys endpoint
        # already words this case; the two ways of testing one key must not
        # disagree about what they report.
        "message": state.error_message or (
            f"Credential accepted — authentication and a minimal generation "
            f"both succeeded ({state.response_time_ms} ms)."
            if state.status == "HEALTHY"
            else "The key did not pass; its status and reason are shown above."),
    }


@router.get("/keys/test/stream")
async def test_all_keys(request: Request):
    """SSE: staged 'Test All API Keys' — live progress, no silent runs."""
    loop = asyncio.get_running_loop()
    q: asyncio.Queue = asyncio.Queue()

    async def gen():
        keys = get_config_secret_keys()
        total = len(keys)
        started = time.time()
        summary = {"total": total, "tested": 0, "passed": 0, "failed": 0,
                   "skipped": 0}
        yield _sse({"type": "start", "total": total})

        for index, (key, fp) in enumerate(keys, start=1):
            if await request.is_disconnected():
                return

            def run_one():
                return test_key(index, key, fp)

            state = await asyncio.to_thread(run_one)

            # forward staged events for live display
            for event in state.events:
                yield _sse({
                    "type": "key_stage", "index": index, "fingerprint": fp,
                    "stage": event["stage"], "message": event["message"],
                })

            # register the outcome in key stats (works in mock mode too)
            _km().register_manual_result(
                fp,
                ok=state.status == "HEALTHY",
                kind="healthy" if state.status == "HEALTHY" else (state.error_kind or "unknown"),
                message=state.error_message or ("ok" if state.status == "HEALTHY" else "failed"),
                response_time=state.response_time_ms,
            )

            summary["tested"] += 1
            if state.status == "HEALTHY":
                summary["passed"] += 1
            elif state.status == "SKIPPED":
                summary["skipped"] += 1
            else:
                summary["failed"] += 1

            yield _sse({
                "type": "key_result", "index": index, "fingerprint": fp,
                "status": state.status,
                "auth_ok": state.auth_ok, "generate_ok": state.generate_ok,
                "response_time_ms": state.response_time_ms,
                "error_kind": state.error_kind,
                "error_message": state.error_message,
                "summary": dict(summary),
            })

        summary["elapsed_s"] = round(time.time() - started, 2)
        yield _sse({"type": "done", "summary": summary})

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache"})


def get_config_secret_keys():
    from ..config import get_secrets
    keys = get_secrets().gemini_keys()
    from ..gemini.key_manager import fingerprint
    return [(k, fingerprint(k)) for k in keys]


def _sse(payload: dict) -> str:
    return f"data: {json.dumps(payload)}\n\n"


# ============================================================
# Classroom
# ============================================================

@router.get("/classroom/status")
def classroom_status():
    state = auth_state()
    return {**state.public(),
            "last_sync": classroom_sync.last_sync(),
            "write_draft_grades": get_config().classroom.write_draft_grades}


@router.post("/classroom/connect")
async def classroom_connect():
    """Runs the OAuth loopback flow. Called from the UI button."""
    from ..demo import guard_classroom
    try:
        guard_classroom()
    except Exception as exc:  # demo mode: refuse, never inherit a real token
        raise AppError(str(exc), status=400, code="demo_mode")
    try:
        result = await asyncio.to_thread(start_device_or_loopback_flow)
        return result
    except Exception as exc:
        raise AppError(
            "Google sign-in failed or was cancelled. No credentials were stored.",
            detail=str(exc), status=400, code="oauth_failed")


@router.post("/classroom/disconnect")
def classroom_do_disconnect():
    classroom_disconnect()
    return {"connected": False}


@router.get("/classroom/courses")
def courses(force: bool = False):
    _require_classroom()
    try:
        return {"courses": classroom_sync.sync_courses(force=force)}
    except Exception as exc:
        raise _classroom_error(exc)


@router.get("/classroom/courses/{course_id}/assignments")
def assignments(course_id: str, force: bool = False):
    _require_classroom()
    try:
        return {"assignments": classroom_sync.sync_coursework(course_id, force=force)}
    except Exception as exc:
        raise _classroom_error(exc)


@router.get("/classroom/courses/{course_id}/assignments/{coursework_id}/submissions")
def submissions(course_id: str, coursework_id: str, force: bool = False):
    _require_classroom()
    try:
        subs = classroom_sync.sync_submissions(course_id, coursework_id, force=force)
        out = []
        for s in subs:
            row = dict(s)
            row["student_name"] = classroom_sync.student_name(s["user_id"])
            # The Classroom grade shown next to a returned paper comes from the
            # tracking layer (the cache above stores no grades), so the page
            # does not list a returned paper with a blank score.
            tracked = db.query_one(
                "SELECT assigned_grade, draft_grade, max_points FROM classroom_submissions "
                "WHERE submission_id = ?", (s["id"],))
            row["assigned_grade"] = tracked["assigned_grade"] if tracked else None
            row["draft_grade"] = tracked["draft_grade"] if tracked else None
            row["max_points"] = tracked["max_points"] if tracked else None
            job = db.query_one(
                "SELECT id, status FROM correction_jobs WHERE submission_id = ? "
                "ORDER BY created_at DESC LIMIT 1", (s["id"],))
            result = db.query_one(
                "SELECT id, earned, available, percentage FROM results "
                "WHERE submission_id = ? ORDER BY created_at DESC LIMIT 1", (s["id"],))
            row["job"] = dict(job) if job else None
            row["result"] = dict(result) if result else None
            out.append(row)
        return {"submissions": out}
    except Exception as exc:
        raise _classroom_error(exc)


@router.post("/classroom/courses/{course_id}/assignments/{coursework_id}/correct")
def correct_selected(course_id: str, coursework_id: str, payload: dict):
    """Enqueue corrections for an EXPLICIT selection of submissions.

    Unlike ``correct-all`` (bulk/automatic, allow-list gated), this endpoint
    encodes deliberate teacher intent: one student, or several ticked rows.
    Every requested id is validated against verified Classroom state first —
    students who have not handed anything in are SKIPPED, never corrected and
    never silently dropped, so no API calls are wasted on empty submissions.

    Body: ``{"submission_ids": ["...", ...]}``
    """
    _require_classroom()
    _require_usage_budget()
    ids = payload.get("submission_ids")
    if not isinstance(ids, list) or not ids:
        raise AppError("Provide at least one submission id to correct.",
                       status=400, code="empty_selection")
    if len(ids) > 200:
        raise AppError("Selection is limited to 200 submissions at once.",
                       status=400, code="selection_too_large")

    scheme_dir = data_dir() / "mark_schemes"
    if not (scheme_dir / f"{coursework_id}.pdf").exists():
        pending = db.query_one(
            "SELECT path FROM pending_mark_schemes WHERE coursework_id = ?",
            (coursework_id,))
        if not (pending and Path(pending["path"]).exists()):
            raise AppError(
                "Upload the mark scheme for this assignment before starting corrections.",
                status=400, code="mark_scheme_missing")

    from ..jobs.manager import DuplicateJobError
    service = _correction_service()
    enqueued: list[str] = []
    duplicates: list[str] = []
    not_submitted: list[str] = []
    no_attachment: list[str] = []
    unknown: list[str] = []

    for submission_id in ids:
        submission_id = str(submission_id)
        state, attachments = _verified_submission_state(submission_id)
        if state is None:
            unknown.append(submission_id)
            continue
        if state not in _HANDED_IN_STATES:
            not_submitted.append(submission_id)
            continue
        if not attachments:
            no_attachment.append(submission_id)
            continue

        row = db.query_one(
            "SELECT user_id FROM submissions WHERE id = ?", (submission_id,))
        tracked = db.query_one(
            "SELECT student_id, student_name FROM classroom_submissions "
            "WHERE submission_id = ?", (submission_id,))
        student_id = (row["user_id"] if row else None) \
            or (tracked["student_id"] if tracked else None)
        student_name = tracked["student_name"] if tracked else None
        if student_id and not student_name:
            # Name lookup uses the local cache first; the profile fetch is a
            # fallback only (and never fails the enqueue).
            student_name = classroom_sync.student_name(student_id)
        if not student_id:
            unknown.append(submission_id)
            continue

        try:
            job = service.enqueue_classroom_submission(
                submission_id=submission_id, coursework_id=coursework_id,
                course_id=course_id, student_id=student_id,
                # Classroom name, never a nickname and never an id posing as
                # one (§1). Identity itself stays the student id (§3).
                student_name=(_identity.resolve(student_id)["classroom_name"]
                              or student_name or "Student"),
            )
            enqueued.append(job["id"])
        except DuplicateJobError:
            duplicates.append(submission_id)

    log_event("api", "selected corrections enqueued",
              coursework_id=coursework_id, enqueued=len(enqueued),
              skipped_not_submitted=len(not_submitted),
              skipped_no_attachment=len(no_attachment))
    return {
        "enqueued": len(enqueued),
        "jobs": enqueued,
        "duplicates": duplicates,
        "skipped_not_submitted": not_submitted,
        "skipped_no_attachment": no_attachment,
        "unknown": unknown,
    }


# Raw Classroom states that mean work has actually been handed in.
_HANDED_IN_STATES = {"TURNED_IN", "STUDENT_EDITED_AFTER_TURN_IN", "RETURNED"}


def _verified_submission_state(submission_id: str) -> tuple[str | None, list[str]]:
    """Resolve (raw state, drive attachment ids) for one submission from
    verified data — tracking layer first (all states), legacy cache second.
    Returns ``(None, [])`` when we simply do not know."""
    tracked = db.query_one(
        "SELECT current_state, attachments_json, data_quality "
        "FROM classroom_submissions WHERE submission_id = ?",
        (submission_id,))
    if tracked and tracked["current_state"]:
        attachments = [
            str(a.get("id")) for a in db.loads(tracked["attachments_json"], [])
            if a.get("kind") == "drive" and a.get("id")
        ]
        return tracked["current_state"], attachments

    legacy_row = db.query_one(
        "SELECT state, attachment_ids FROM submissions WHERE id = ?",
        (submission_id,))
    if legacy_row:
        return (legacy_row["state"] or None), db.loads(legacy_row["attachment_ids"], [])
    return None, []


@router.post("/classroom/courses/{course_id}/assignments/{coursework_id}/allow")
def allow_assignment(course_id: str, coursework_id: str, allowed: bool = True):
    classroom_sync.set_allowlist(coursework_id, allowed)
    return {"coursework_id": coursework_id, "allowlisted": allowed}


#: How many superseded mark schemes to keep per assignment.
PREVIOUS_SCHEMES_KEPT = 3


def _prune_previous_schemes(archive: Path, coursework_id: str) -> None:
    """Keep only the newest few superseded schemes for one assignment."""
    for stale in sorted(archive.glob(f"{coursework_id}-*.pdf"))[:-PREVIOUS_SCHEMES_KEPT]:
        try:
            stale.unlink()
        except OSError:                       # noqa: BLE001 — best effort
            pass


def _preserve_previous_scheme(path: Path, coursework_id: str) -> Path | None:
    """Move an existing mark scheme aside before it is replaced.

    Uploading REPLACES ``mark_schemes/<coursework>.pdf`` in place, so without
    this an accidental upload (the wrong file, or the right file onto the wrong
    assignment) destroyed the teacher's only copy with no way back. Returns the
    archived path, or None when there was nothing to preserve.
    """
    if not path.exists():
        return None
    archive = path.parent / "_previous"
    archive.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    target = archive / f"{coursework_id}-{stamp}.pdf"
    suffix = 1
    while target.exists():                    # same-second re-upload
        target = archive / f"{coursework_id}-{stamp}-{suffix}.pdf"
        suffix += 1
    path.replace(target)
    _prune_previous_schemes(archive, coursework_id)
    return target


@router.post("/classroom/courses/{course_id}/assignments/{coursework_id}/mark-scheme")
async def upload_mark_scheme(course_id: str, coursework_id: str,
                             file: UploadFile = File(...)):
    if not (file.filename or "").lower().endswith(".pdf"):
        raise AppError("The mark scheme must be a PDF.", status=400, code="bad_file_type")
    ensure_dirs()
    scheme_dir = data_dir() / "mark_schemes"
    scheme_dir.mkdir(parents=True, exist_ok=True)
    path = scheme_dir / f"{coursework_id}.pdf"
    content = await file.read()
    if len(content) > 50 * 1024 * 1024:
        raise AppError("Mark scheme is larger than 50 MB.",
                       status=413, code="file_too_large")
    archived = _preserve_previous_scheme(path, coursework_id)
    path.write_bytes(content)
    db.execute(
        "INSERT INTO pending_mark_schemes(coursework_id, path, uploaded_at) VALUES(?,?,?) "
        "ON CONFLICT(coursework_id) DO UPDATE SET path=excluded.path, uploaded_at=excluded.uploaded_at",
        (coursework_id, str(path), time.time()),
    )
    log_event("api", "mark scheme uploaded", coursework_id=coursework_id,
              replaced=bool(archived))
    return {"coursework_id": coursework_id, "stored": True}


# Classroom tracking statuses that mean work was actually handed in.
_SUBMITTED_TRACKING_STATUSES = (
    "SUBMITTED", "SUBMITTED_LATE", "RETURNED", "GRADED", "CORRECTED",
    "PROCESSING", "FAILED", "INVALID", "EDITED_AFTER_SUBMISSION",
)
# correction_jobs statuses that mean work is currently in flight for a student.
_ACTIVE_JOB_STATUSES = (
    "QUEUED", "STARTING", "PREPARING", "DOWNLOADING", "UPLOADING", "MARKING",
    "VERIFYING", "SAVING", "WAITING_FOR_API", "RETRYING",
)


def _active_job_submission_ids(coursework_id: str) -> set[str]:
    """Submissions of this assignment that already have work in flight."""
    marks = ",".join("?" * len(_ACTIVE_JOB_STATUSES))
    rows = db.query(
        f"SELECT submission_id FROM correction_jobs WHERE coursework_id = ? "
        f"AND submission_id IS NOT NULL AND status IN ({marks})",
        tuple([coursework_id] + list(_ACTIVE_JOB_STATUSES)),
    )
    return {r["submission_id"] for r in rows}


def _classify_correction(course_id: str, coursework_id: str, *,
                         refresh: bool) -> dict:
    """Classify every tracked student for whole-assignment correction.

    Reads the tracking layer (ALL states, not just TURNED_IN), the stored
    results and any in-flight jobs. Pure read plus an optional Classroom
    refresh — it NEVER enqueues anything. Shared by the confirmation preview
    and correct-all, so the numbers the teacher confirms are exactly what runs
    (§6/§7): already-corrected submissions are separated out and only
    re-corrected on an explicit recheck.
    """
    sync_warning = None
    if refresh:
        try:
            from ..classroom import submissions as cs
            cs.sync_submission_tracking(course_id, coursework_id)
        except Exception as exc:  # noqa: BLE001 — reported, never fatal
            sync_warning = (f"Classroom refresh failed ({exc}); continuing "
                            f"with the last verified data.")

    from ..classroom import submissions as cs
    tracking = cs.tracking_rows(course_id=course_id, coursework_id=coursework_id)
    active = _active_job_submission_ids(coursework_id)

    eligible: list[dict] = []
    already: list[dict] = []
    needs_review: list[dict] = []
    processing: list[str] = []
    invalid: list[str] = []
    missing: list[str] = []
    draft: list[str] = []
    unknown: list[str] = []
    submitted = 0
    # submission id -> why it is not eligible, so a hand-picked selection can
    # say why one of its papers was refused instead of silently correcting
    # fewer papers than the teacher ticked.
    reasons: dict[str, str] = {}

    for row in tracking["rows"]:
        name = row.get("student_name") or row.get("student_id") or "?"
        sid = row.get("submission_id")
        status = row.get("status")
        if status in _SUBMITTED_TRACKING_STATUSES:
            submitted += 1
        if row.get("synthetic") or status in ("NOT_STARTED", "MISSING", "RECLAIMED"):
            missing.append(name)
            if sid:
                reasons[sid] = "was never handed in"
            continue
        if status == "CREATED":
            draft.append(name)
            if sid:
                reasons[sid] = "was opened but never handed in"
            continue
        state, attachments = _verified_submission_state(sid) if sid else (None, [])
        if state is None:
            unknown.append(name)
            if sid:
                reasons[sid] = ("could not be verified with Google Classroom")
            continue
        if state not in _HANDED_IN_STATES:
            missing.append(name)
            if sid:
                reasons[sid] = "was never handed in"
            continue
        if not attachments:
            invalid.append(name)
            if sid:
                reasons[sid] = "was handed in without a file the app can read"
            continue
        if sid in active:
            processing.append(name)
            reasons[sid] = "is already being corrected"
            continue
        entry = {"submission_id": sid, "student_id": row.get("student_id"),
                 "student_name": name,
                 # Canonical name, so the "correct all" preview shows the same
                 # student the results list and PDF will (§4/§20).
                 "display_name": _identity.resolve(
                     row.get("student_id"),
                     snapshot_name=name)["display_name"]}
        result = db.query_one(
            "SELECT id, earned, available, percentage, review_required, "
            "manual_override FROM results WHERE coursework_id = ? "
            "AND submission_id = ? ORDER BY created_at DESC LIMIT 1",
            (coursework_id, sid))
        if result is not None:
            flagged = bool(int(result["review_required"] or 0)) and not bool(
                int(result["manual_override"] or 0))
            entry = {**entry, "result_id": result["id"],
                     "earned": result["earned"], "available": result["available"],
                     "percentage": result["percentage"], "needs_review": flagged}
            (needs_review if flagged else already).append(entry)
            reasons[sid] = ("was corrected but is waiting for a teacher's decision"
                            if flagged else "was already corrected")
            continue
        eligible.append(entry)

    return {
        "coursework_id": coursework_id, "course_id": course_id,
        "sync_warning": sync_warning,
        "reasons": reasons,
        "total_students": tracking["overview"]["total"],
        "submitted": submitted,
        "eligible": eligible,
        "already_corrected": already,
        "needs_review": needs_review,
        "processing": processing,
        "invalid": invalid,
        "missing": missing,
        "draft": draft,
        "unknown": unknown,
    }


@router.get("/assignments/{coursework_id}/correct-all/preview")
def correct_all_preview(coursework_id: str, refresh: bool = False):
    """Side-effect-free description of what Correct All Submitted would do.

    Backs the confirmation dialog (counts + the students who will actually be
    corrected). Nothing is enqueued and no correction is started. When the
    assignment is not allowlisted or has no mark scheme, the blockers are
    reported rather than failing the request.
    """
    cw = db.query_one("SELECT * FROM coursework WHERE id = ?", (coursework_id,))
    if not cw:
        raise AppError("Assignment not found. Sync Google Classroom first.",
                       status=404, code="not_found")
    course_id = cw["course_id"]
    classified = _classify_correction(course_id, coursework_id, refresh=bool(refresh))

    scheme_dir = data_dir() / "mark_schemes"
    has_scheme = (scheme_dir / f"{coursework_id}.pdf").exists()
    if not has_scheme:
        pending = db.query_one(
            "SELECT path FROM pending_mark_schemes WHERE coursework_id = ?",
            (coursework_id,))
        has_scheme = bool(pending and pending["path"] and Path(pending["path"]).exists())

    blockers: list[str] = []
    if not cw["allowlisted"]:
        blockers.append("This assignment is not allowlisted.")
    if not has_scheme:
        blockers.append("No mark scheme has been uploaded for this assignment.")

    eligible = classified["eligible"]
    return {
        "coursework_id": coursework_id,
        "course_id": course_id,
        "assignment_title": cw["title"],
        "counts": {
            "students": classified["total_students"],
            "submitted": classified["submitted"],
            "already_corrected": len(classified["already_corrected"]),
            "needs_review": len(classified["needs_review"]),
            "not_corrected": len(eligible),
            "processing": len(classified["processing"]),
            "invalid": len(classified["invalid"]),
            "missing": len(classified["missing"]),
            "draft": len(classified["draft"]),
            "unknown": len(classified["unknown"]),
        },
        "eligible": eligible,
        "already_corrected": classified["already_corrected"],
        "needs_review": classified["needs_review"],
        "will_correct": len(eligible),
        "can_start": not blockers,
        "blockers": blockers,
        "has_mark_scheme": has_scheme,
        "allowlisted": bool(cw["allowlisted"]),
        "sync_warning": classified["sync_warning"],
    }


@router.post("/classroom/courses/{course_id}/assignments/{coursework_id}/correct-all")
def correct_all(course_id: str, coursework_id: str, payload: dict | None = None):
    """Correct the ENTIRE assignment (§14): refresh → select → enqueue.

    Steps:
      1. Refresh Classroom tracking data (best-effort: a failed refresh
         falls back to the last verified rows and is reported, never hidden).
      2. Read the CURRENT submission state per student from the tracking
         layer (all states — not just TURNED_IN).
      3. Classify every student: eligible → enqueue; missing/draft → skipped
         safely; handed-in without a usable attachment → marked invalid;
         unknown → skipped and reported.
      4. Each student is enqueued INDEPENDENTLY — one failure never stops
         the rest of the class (§14).

    Requires allow-list + mark scheme (unchanged §9 gate).
    Body (optional): ``{"refresh": false}`` to skip the network refresh,
    ``{"recheck": true}`` to re-correct papers that already have a result, and
    ``{"submission_ids": [...]}`` to correct only the papers the teacher ticked.
    An empty selection is refused: it never means "everything".
    """
    _require_usage_budget()
    refresh = True
    if isinstance(payload, dict) and payload.get("refresh") is False:
        refresh = False
    cw = db.query_one("SELECT * FROM coursework WHERE id = ?", (coursework_id,))
    if not cw or not cw["allowlisted"]:
        raise AppError(
            "This assignment is not allowlisted. Allow it first — nothing is "
            "processed automatically for non-allowlisted assignments.",
            status=403, code="not_allowlisted")
    scheme_dir = data_dir() / "mark_schemes"
    if not (scheme_dir / f"{coursework_id}.pdf").exists():
        pending = db.query_one(
            "SELECT path FROM pending_mark_schemes WHERE coursework_id = ?",
            (coursework_id,))
        if not (pending and Path(pending["path"]).exists()):
            raise AppError(
                "Upload the mark scheme for this assignment before starting corrections.",
                status=400, code="mark_scheme_missing")

    # Classify once from verified state (shared with the preview endpoint).
    classified = _classify_correction(course_id, coursework_id, refresh=refresh)
    sync_warning = classified["sync_warning"]
    recheck = bool(isinstance(payload, dict) and payload.get("recheck"))

    # An explicit selection: only these submissions run. ``None`` keeps the
    # whole-assignment behaviour (every eligible paper); an empty list is a
    # mistake, not "correct everything".
    selection: list[str] | None = None
    requested = payload.get("submission_ids") if isinstance(payload, dict) else None
    if requested is not None:
        if not isinstance(requested, list):
            raise AppError("submission_ids must be a list of submission ids.",
                           status=400, code="bad_selection")
        selection = [str(value).strip() for value in requested
                     if isinstance(value, (str, int)) and str(value).strip()]
        if not selection:
            raise AppError("Select at least one paper to correct. Nothing "
                           "was queued.", status=400, code="empty_selection")
    wanted = set(selection) if selection is not None else None

    # §7: never blindly re-correct a submission that already has a stored
    # result. Already-corrected students are skipped and reported; a recheck
    # (explicit teacher choice) re-corrects them, preserving the old result as
    # a version — the previous correction is never overwritten.
    to_correct = [row for row in classified["eligible"]
                  if wanted is None or row["submission_id"] in wanted]
    if recheck:
        redo = list(classified["already_corrected"]) + list(classified["needs_review"])
        to_correct += [row for row in redo
                       if wanted is None or row["submission_id"] in wanted]

    # Ticked papers that cannot run are named with the classifier's reason, so
    # a partial selection is reported honestly.
    not_eligible: list[dict] = []
    if selection is not None:
        picked = {row["submission_id"] for row in to_correct}
        reasons = classified.get("reasons", {})
        for sid in selection:
            if sid in picked:
                continue
            not_eligible.append({
                "submission_id": sid,
                "reason": reasons.get(
                    sid, "is not a submission of this assignment"),
            })

    # Enqueue independently — a failure for one student never stops the rest.
    from ..jobs.manager import DuplicateJobError
    service = _correction_service()
    enqueued: list[str] = []
    duplicates: list[str] = []
    failed_students: list[dict] = []
    for row in to_correct:
        try:
            job = service.enqueue_classroom_submission(
                submission_id=row["submission_id"],
                coursework_id=coursework_id, course_id=course_id,
                student_id=row.get("student_id"),
                student_name=row.get("student_name") or row["submission_id"],
            )
            enqueued.append(job["id"])
        except DuplicateJobError:
            duplicates.append(row["submission_id"])
        except Exception as exc:  # noqa: BLE001 — isolate per student
            failed_students.append({"student": row.get("student_name"),
                                    "error": str(exc)})

    log_event("api", "whole-assignment correction started",
              course_id=course_id, coursework_id=coursework_id,
              enqueued=len(enqueued), duplicates=len(duplicates),
              requested=len(selection) if selection is not None else None,
              not_eligible=len(not_eligible),
              already_corrected=len(classified["already_corrected"]),
              needs_review=len(classified["needs_review"]),
              skipped_missing=len(classified["missing"]),
              skipped_draft=len(classified["draft"]),
              invalid=len(classified["invalid"]),
              enqueue_failed=len(failed_students), refreshed=refresh,
              recheck=recheck)
    return {
        "enqueued": len(enqueued),
        "jobs": enqueued,
        "requested": len(selection) if selection is not None else None,
        "not_eligible": not_eligible,
        "duplicates": len(duplicates),
        "already_corrected": [e["submission_id"] for e in classified["already_corrected"]],
        "needs_review": [e["submission_id"] for e in classified["needs_review"]],
        "processing": classified["processing"],
        "skipped_missing": classified["missing"],
        "skipped_draft": classified["draft"],
        "invalid": classified["invalid"],
        "unknown": classified["unknown"],
        "enqueue_failed": failed_students,
        "total_students": classified["total_students"],
        "sync_warning": sync_warning,
        "refreshed": refresh and sync_warning is None,
        "recheck": recheck,
    }


# ============================================================
# Correction report -> student's Classroom submission (P0)
# ============================================================

def _assignment_title(coursework_id: str | None) -> str | None:
    if not coursework_id:
        return None
    row = db.query_one("SELECT title FROM coursework WHERE id = ?",
                       (coursework_id,))
    return row["title"] if row else None


def _course_id_for(coursework_id: str | None) -> str:
    """Course that owns an assignment - resolved from OUR database only, so
    the course id always matches the coursework/submission ids we stored."""
    if not coursework_id:
        raise AppError("This correction is not linked to a Classroom assignment.",
                       status=400, code="no_coursework")
    row = db.query_one("SELECT course_id FROM coursework WHERE id = ?",
                       (coursework_id,))
    if not row or not row["course_id"]:
        raise AppError("Could not find the Classroom course for this assignment.",
                       status=400, code="no_course")
    return row["course_id"]


@router.get("/results/{result_id}/classroom-attachment")
def result_attachment_status(result_id: str):
    """What the teacher sees: is the report on this student's submission?"""
    from ..classroom import report_attachment as ra
    if not db.query_one("SELECT id FROM results WHERE id = ?", (result_id,)):
        raise HTTPException(404, "Result not found")
    return ra.get_state(result_id)


@router.post("/results/{result_id}/attach-report")
def attach_report_to_submission(result_id: str, detail: str = "standard"):
    """Upload the report and attach it to THIS student's Classroom submission.

    Safe to call again after a failure: it reuses the existing result and
    report, and never re-runs AI marking.
    """
    _require_classroom()
    if detail not in ("summary", "standard", "detailed"):
        raise AppError("detail must be summary, standard or detailed.",
                       status=400, code="bad_detail")
    result = _correction_service().get_result(result_id)
    if not result:
        raise HTTPException(404, "Result not found")
    course_id = _course_id_for(result.get("coursework_id"))
    try:
        from ..classroom import report_attachment as ra
        from ..classroom.auth import account_email as _acct
        outcome = ra.attach_report(
            get_access_token(), result, course_id=course_id,
            assignment_title=_assignment_title(result.get("coursework_id")),
            detail=detail, account_email=_acct())
    except Exception as exc:  # noqa: BLE001
        raise _classroom_error(exc)
    payload = outcome.to_dict()
    # Non-2xx so the UI can surface a real failure instead of a false tick.
    # An ownership problem gets its OWN code, because it is a permanent
    # configuration fault rather than a transient API error (§3, §20).
    if payload["status"] == ra.STATUS_OWNERSHIP_ERROR:
        raise AppError(payload["message"], status=409,
                       code=ra.ERROR_OWNERSHIP, detail=payload)
    # Testing-only mode refused the write locally: that is a deliberate
    # configuration gate, not a Google/Drive failure, so it must not read as
    # 502 report_attach_failed (the UI would show a broken upload to fix).
    if payload["status"] == ra.STATUS_TESTING_BLOCKED:
        raise AppError(payload["message"], status=403,
                       code=ra.ERROR_TESTING_ONLY, detail=payload)
    if payload["status"] in ra.FAILED_STATUSES:
        raise AppError(payload["message"], status=502,
                       code="report_attach_failed", detail=payload)
    # A confirmed attachment with a pending Drive cleanup is a SUCCESS: the
    # grade was returned and the report is attached; only housekeeping is
    # outstanding (§12).
    return payload


@router.post("/results/{result_id}/report-cleanup")
def retry_report_cleanup(result_id: str):
    """Retry deleting a temporary Drive file left over from a success (§13).

    Never regenerates the report, never re-marks the student and never creates
    another Drive file - it only re-issues the delete for a file we already
    own. Safe to call repeatedly.
    """
    _require_classroom()
    from ..classroom import report_cleanup as rc
    from ..classroom.auth import account_email, get_access_token
    from ..classroom import report_attachment as ra
    try:
        token = get_access_token()
    except Exception as exc:  # noqa: BLE001
        # Housekeeping failing must never look like a crash - and the pending
        # state is already recorded, so the next sweep can finish the job.
        return {"result_id": result_id, "cleanup": {
            "ok": False, "skipped": "unavailable",
            "reason": str(exc)[:200]},
            "state": ra.get_state(result_id)}
    try:
        outcome = rc.cleanup_result(result_id, token,
                                     account_email=account_email())
    except Exception as exc:  # noqa: BLE001
        outcome = {"ok": False, "skipped": "error", "reason": str(exc)[:200]}
    return {"result_id": result_id, "cleanup": outcome,
            "state": ra.get_state(result_id)}


@router.post("/classroom/report-cleanup/sweep")
def sweep_report_cleanup(older_than: int = 0):
    """Retry every pending cleanup, then remove our own orphaned temp files.

    This is the crash-recovery path: a process that died after uploading can
    leave a temporary report behind, and this removes it without touching a
    single teacher, student or mark-scheme file (§16-§17).
    """
    _require_classroom()
    from ..classroom import report_cleanup as rc
    from ..classroom.auth import account_email, get_access_token
    try:
        token = get_access_token()
    except Exception as exc:  # noqa: BLE001
        # A sweep with no usable Google connection reports why and stops. It
        # must never 500, and it must never "succeed" having deleted nothing.
        return {"ok": False, "error": str(exc)[:200], "retried": {},
                "orphans": {"ok": False, "scanned": 0, "deleted": [],
                            "kept": []}}
    acct = account_email()
    retried = rc.retry_pending_cleanup(token, account_email=acct)
    orphans = rc.sweep_orphans(token, account_email=acct,
                               older_than=older_than or
                               rc.DEFAULT_ORPHAN_AGE_SECONDS)
    return {"ok": True, "retried": retried, "orphans": orphans}


@router.post("/classroom/courses/{course_id}/assignments/{coursework_id}/attach-reports")
def attach_reports_batch(course_id: str, coursework_id: str,
                         detail: str = "standard", only: str = "pending"):
    """Attach reports for a whole assignment (and retry the failures).

    ``only``: pending (default) | all | failed
    Reuses each existing result + report - AI marking is NEVER redone, so one
    student's failure cannot stop the batch.
    """
    _require_classroom()
    if detail not in ("summary", "standard", "detailed"):
        raise AppError("detail must be summary, standard or detailed.",
                       status=400, code="bad_detail")
    from ..classroom import report_attachment as ra

    results = db.query(
        "SELECT id FROM results WHERE coursework_id = ? AND submission_id IS NOT NULL",
        (coursework_id,))
    # "all" means NO status filter, which is a legitimate choice - so it needs
    # a sentinel distinct from None (None means "unknown value" below).
    _ALL = object()
    # "failed" must cover EVERY way an attach can fail, not just the legacy
    # status value - otherwise a Drive upload error or an ownership refusal
    # would be invisible to the teacher's "retry the failures" button (§20).
    _FILTERS = {"pending": (ra.STATUS_PENDING, *ra.FAILED_STATUSES),
                "failed": ra.FAILED_STATUSES,
                "all": _ALL}
    key = (only or "pending").lower()
    if key not in _FILTERS:
        raise AppError("only must be pending, failed or all.",
                       status=400, code="bad_only")
    want = None if _FILTERS[key] is _ALL else _FILTERS[key]

    token = get_access_token()
    title = _assignment_title(coursework_id)
    service = _correction_service()
    summary = {"verified": 0, "failed": 0, "blocked": 0, "skipped": 0,
               "total": 0, "failures": []}
    for r in results:
        row = ra.get_state(r["id"])
        if want is not None and row["status"] not in want:
            continue
        # Hydrate through the service so the PDF renderer gets decoded
        # result_json (a raw DB row would carry it as a JSON string).
        result = service.get_result(r["id"])
        if not result:
            summary["failed"] += 1
            summary["failures"].append({"result_id": r["id"],
                                        "message": "result row not found"})
            continue
        summary["total"] += 1
        try:
            from ..classroom.auth import account_email as _acct
            outcome = ra.attach_report(token, result, course_id=course_id,
                                       assignment_title=title, detail=detail,
                                       account_email=_acct())
        except Exception as exc:  # noqa: BLE001
            summary["failed"] += 1
            summary["failures"].append({"result_id": r["id"],
                                        "message": str(exc)[:200]})
            continue
        status = outcome.status
        if status == ra.STATUS_VERIFIED:
            summary["verified"] += 1
        elif status == ra.STATUS_BLOCKED:
            summary["blocked"] += 1
            summary["failures"].append({"result_id": r["id"],
                                        "message": outcome.message})
        elif status == ra.STATUS_SKIPPED:
            summary["skipped"] += 1
        else:
            summary["failed"] += 1
            summary["failures"].append({"result_id": r["id"],
                                        "message": outcome.message})
    log_event("reports", "batch report attachment finished",
              coursework_id=coursework_id, **summary)
    return summary


@router.post("/classroom/grades/push")
def push_grades(course_id: str, coursework_id: str):
    """Attempt draft-grade write-back for completed corrections of one
    assignment; falls back cleanly when Google refuses (403)."""
    cfg = get_config()
    if not cfg.classroom.write_draft_grades:
        raise AppError(
            "Grade write-back is disabled in Settings. Suggested grades are "
            "shown on each result for manual entry.",
            status=400, code="writeback_disabled")
    token = get_access_token()
    cw = db.query_one("SELECT * FROM coursework WHERE id = ?", (coursework_id,))
    if not cw or not cw["max_points"]:
        raise AppError("This assignment has no maximum points in Classroom.",
                       status=400, code="no_max_points")
    results = db.query(
        "SELECT * FROM results WHERE coursework_id = ? AND submission_id IS NOT NULL",
        (coursework_id,))
    pushed, refused, errors = 0, 0, 0
    message = None
    for r in results:
        try:
            grade = round((r["earned"] / r["available"]) * cw["max_points"], 2) if r["available"] else 0
            response = gc.patch_draft_grade(token, course_id, coursework_id,
                                            r["submission_id"], grade)
            if response.get("refused"):
                refused += 1
                message = response.get("reason")
            else:
                pushed += 1
        except Exception:
            errors += 1
    return {"pushed": pushed, "refused": refused, "errors": errors,
            "message": message}


def _require_classroom():
    from ..demo import guard_classroom
    try:
        guard_classroom()
    except Exception as exc:  # demo mode
        raise AppError(str(exc), status=400, code="demo_mode")
    state = auth_state()
    if not state.connected:
        raise AppError(
            state.problem or "Google Classroom is not connected.",
            status=400, code="classroom_not_connected")


def _classroom_error(exc: Exception) -> AppError:
    from ..classroom import report_attachment as ra
    from ..classroom.client import ClassroomApiError
    from ..testing_guard import PublicationBlocked
    if isinstance(exc, PublicationBlocked):
        # Testing-only mode refused the write on purpose. Reported as its own
        # gate (403 + the guard's own wording), never as an unreachable
        # Google service the teacher would try to "fix" by retrying.
        return AppError(str(exc), status=403, code=ra.ERROR_TESTING_ONLY,
                        detail=str(exc))
    if isinstance(exc, ClassroomApiError):
        return AppError(str(exc), status=502, code="classroom_api_error")
    # An authorization problem is NOT "could not reach Classroom". Telling a
    # teacher to retry a network call when their grant has been revoked sends
    # them down the wrong road - and hides the one action that fixes it.
    detail = str(exc)
    if isinstance(exc, RuntimeError) and "reconnect" in detail.lower():
        return AppError(
            detail, status=401, code="classroom_reconnect_required",
            detail=detail)
    return AppError(
        "Could not reach Google Classroom. Check the connection on the "
        "Classroom page.",
        detail=detail, status=502, code="classroom_error")


# ============================================================
# Settings
# ============================================================

@router.get("/search")
def global_search(q: str = "", limit: int = 8):
    """Global search across LOCAL data only — no Google API calls, so it is
    fast enough for per-keystroke use (§15). Groups results by type; every
    item carries a deep link the UI navigates to."""
    query = (q or "").strip()
    if not query:
        return {"query": "", "groups": [], "total": 0}
    limit = max(1, min(25, int(limit or 8)))
    like = f"%{query}%"
    groups: list[dict] = []

    # -- students (stable Google IDs; nickname searchable, never a key) -----
    rows = db.query(
        "SELECT cs.student_id, MIN(cs.student_name) AS name, "
        "       MIN(cs.student_email) AS email, MAX(s.nickname) AS nickname, "
        "       COUNT(DISTINCT cs.coursework_id) AS assignments "
        "FROM classroom_submissions cs "
        "LEFT JOIN students s ON s.id = cs.student_id "
        "WHERE cs.student_id IS NOT NULL AND cs.student_id != '' AND ("
        "  IFNULL(cs.student_name,'') LIKE ? OR IFNULL(cs.student_email,'') LIKE ? "
        "  OR cs.student_id LIKE ? OR IFNULL(s.nickname,'') LIKE ?) "
        "GROUP BY cs.student_id "
        "ORDER BY COALESCE(nickname, name, cs.student_id) LIMIT ?",
        (like, like, like, like, limit),
    )
    if rows:
        from ..student_identity import display_name as _canonical_name
        groups.append({"key": "students", "label": "Students", "items": [
            {"id": r["student_id"],
             # Nickname -> Classroom name. An id is only a last resort.
             "title": _canonical_name(r["nickname"], r["name"],
                                      fallback=r["student_id"]),
             "subtitle": " · ".join([x for x in [
                 (r["name"] if r["nickname"] else None),
                 r["email"],
                 _count(r["assignments"], "assignment")] if x]) or None,
             "route": f"#/students/{r['student_id']}"}
            for r in rows]})

    # -- assignments (incl. course-name matching) ---------------------------
    rows = db.query(
        "SELECT w.id, w.title, w.course_id, w.max_points, c.name AS course_name "
        "FROM coursework w LEFT JOIN courses c ON c.id = w.course_id "
        "WHERE w.title LIKE ? OR IFNULL(w.description,'') LIKE ? OR w.id LIKE ? "
        "  OR IFNULL(c.name,'') LIKE ? "
        "ORDER BY w.update_time DESC LIMIT ?",
        (like, like, like, like, limit),
    )
    if rows:
        groups.append({"key": "assignments", "label": "Assignments", "items": [
            {"id": r["id"], "title": r["title"] or r["id"],
             "subtitle": " · ".join([x for x in [
                 r["course_name"],
                 (f"{r['max_points']:g} pts" if r["max_points"] else None)]
                 if x]) or None,
             "route": f"#/classroom/{r['course_id']}/{r['id']}"}
            for r in rows]})

    # -- courses -------------------------------------------------------------
    rows = db.query(
        "SELECT id, name, section FROM courses "
        "WHERE name LIKE ? OR IFNULL(section,'') LIKE ? OR id LIKE ? "
        "ORDER BY name LIMIT ?",
        (like, like, like, limit),
    )
    if rows:
        groups.append({"key": "courses", "label": "Courses", "items": [
            {"id": r["id"], "title": r["name"] or r["id"],
             "subtitle": r["section"] or f"Course {r['id']}",
             "route": f"#/classroom/{r['id']}"}
            for r in rows]})

    # -- corrections (Math Checker results; manual results included) --------
    rows = db.query(
        "SELECT r.id, r.student_name, r.earned, r.available, r.percentage, "
        "       r.coursework_id, r.created_at, w.title AS assignment_title, "
        "       w.course_id AS course_id "
        "FROM results r LEFT JOIN coursework w ON w.id = r.coursework_id "
        "WHERE IFNULL(r.student_name,'') LIKE ? OR IFNULL(w.title,'') LIKE ? "
        "ORDER BY r.created_at DESC LIMIT ?",
        (like, like, limit),
    )
    if rows:
        groups.append({"key": "corrections", "label": "Corrections", "items": [
            {"id": r["id"],
             "title": _identity.display_name(None, r["student_name"],
                                              fallback="Correction"),
             "subtitle": (f"{r['assignment_title'] or 'Manual correction'} · "
                          f"{r['earned']}/{r['available']} "
                          f"({round(r['percentage'] or 0)}%)"),
             "route": (f"#/classroom/{r['course_id']}/{r['coursework_id']}"
                       if r["coursework_id"] and r["course_id"]
                       else "#/results")}
            for r in rows]})

    total = sum(len(g["items"]) for g in groups)
    return {"query": query, "groups": groups, "total": total}


@router.get("/usage")
def usage_report(hours: int = 24):
    """Internet usage: current hour/day totals, limits and hourly history,
    plus the LOCAL API request/retry/failover statistics (§12) — collected
    from requests already being made, never via an extra API call."""
    from ..net_usage import history, usage_state
    from ..api_stats import snapshot as api_stats_snapshot
    from ..gemini.provider_health import provider_health
    state = usage_state()
    from ..api_stats import ai_usage_summary
    api = {
        **api_stats_snapshot(),
        "provider": provider_health().snapshot(),
        "keys": _km().aggregate(),
        # Measured cost of recent corrections ("how many AI requests does a
        # paper take"), including the requests that failed.
        "ai": ai_usage_summary(),
    }
    return {**state, "api": api,
            "history": history(hours=max(1, min(168, hours)))}


# -- tiny background agent ON/OFF (local device control) ----------------------
#
# Deliberately session-authenticated like the rest of the local Settings page:
# this switch controls the agent on THIS machine only, so it must never be a
# system-wide control (§extension 5 — switching it off here cannot disable the
# teacher's or another assistant's agent).


def _local_agent_seen() -> float | None:
    """Freshness of this installation's own agent heartbeat.

    Presence is read from the recorded heartbeat rather than assumed from the
    backend being up, so the UI never claims the agent is running when it has
    gone quiet (§4/§62).
    """
    try:
        row = db.query_one("SELECT MAX(last_heartbeat) AS seen FROM workers")
        return float(row["seen"]) if row and row["seen"] else None
    except Exception:  # noqa: BLE001 — the switch must render even pre-sync
        return None


@router.get("/agent/state")
def agent_state():
    """Tiny Agent state. ``product_enabled: false`` means this version does not
    run an agent at all, which is the default and is shown as such rather than
    being confused with a user-paused agent."""
    from ..distributed import agent_control
    return agent_control.describe(last_heartbeat=_local_agent_seen())


@router.post("/agent/state")
def set_agent_state(payload: dict):
    """Pause/resume the local agent — refused while the agent is unavailable.

    Hiding the control is not enough: an API caller must not be able to write a
    preference that implies an agent is running in a version that has none.
    Switching OFF stays allowed so a legacy "enabled" record can be cleared.
    """
    from ..distributed import agent_control
    wanted = bool(payload.get("enabled"))
    if wanted:
        agent_control.require_product_enabled()
    agent_control.save_state(wanted, updated_by="settings")
    state = agent_control.describe(last_heartbeat=_local_agent_seen())
    return state


def _local_device_id() -> str | None:
    """This installation's own device id, if the agent has created one."""
    try:
        from ..distributed import agent_control
        path = agent_control.agent_dir() / "device_id"
        value = path.read_text(encoding="utf-8").strip() if path.exists() else ""
        return value or None
    except Exception:  # noqa: BLE001 — discovery is advisory on the settings page
        return None


@router.get("/agent/discovery")
def agent_discovery():
    """Accounts + device availability this machine may synchronise with.

    Read-only and honest: it reports exactly what the shared backend returned
    for THIS device's credential, including that synchronisation is not yet
    established when no account has been linked. With no credential yet it
    returns an empty directory rather than inventing accounts.
    """
    from ..distributed import discovery
    device_id = _local_device_id()
    if not device_id:
        return {"device_id": None, "accounts": [], "linked_accounts": [],
                "registered": False}
    directory = discovery.discover(device_id)
    directory["registered"] = discovery.credential_expiry(device_id) is not None
    return directory


def _testing_settings() -> dict:
    """TESTING ONLY mode in the shape the Settings page needs.

    The value comes from ``app.testing_guard`` — the same source the Classroom
    and Drive gates consult — so what Settings reports can never disagree with
    what actually blocks a write. The reason travels with it so the page can
    explain the block inline instead of only in a failed-approval toast.
    """
    from ..testing_guard import state as testing_state

    current = testing_state()
    return {"only": current.only, "reason": current.reason}


@router.get("/settings")
def get_settings():
    cfg = get_config()
    secrets = _secrets_store()
    return {
        "role": cfg.role,
        "mock_mode": cfg.mock_mode,
        "debug": cfg.debug,
        "worker": {"concurrency": cfg.worker.concurrency,
                   "max_retries": cfg.worker.max_retries},
        "usage": {"hourly_mb": cfg.usage.hourly_mb, "daily_mb": cfg.usage.daily_mb},
        # TESTING ONLY mode (app.testing_guard). A blocked approval tells the
        # teacher to switch this off in Settings, so Settings has to show the
        # real state AND offer the switch; read-only here, changed below.
        "testing": _testing_settings(),
        "gemini": {"primary_models": cfg.gemini.primary_models,
                   "request_timeout_ms": cfg.gemini.request_timeout_ms},
        "classroom": {"enabled": cfg.classroom.enabled,
                      "write_draft_grades": cfg.classroom.write_draft_grades},
        "gemini_key_count": len(secrets.gemini_keys()),
        "has_oauth_client": secrets.oauth_client_config() is not None,
        "teacher": {
            "teacher_name": cfg.teacher.teacher_name,
            "school_name": cfg.teacher.school_name,
            "onboarded": cfg.teacher.onboarded,
        },
        # §42: permanent report branding. This is the ONLY place it changes;
        # Google Classroom sync and the OAuth account can never write here.
        "reports": {
            "teacher_title": cfg.reports.teacher_title,
            "center_name": cfg.reports.center_name,
            "academic_year": cfg.reports.academic_year,
            "quote": cfg.reports.quote,
            "logo_path": cfg.reports.logo_path,
            "monogram": cfg.reports.monogram,
            "footer_note": cfg.reports.footer_note,
            "accent": cfg.reports.accent,
            "document_label": cfg.reports.document_label,
        },
        "grades": {
            "boundaries": [[g, m] for g, m in cfg.grades.boundaries],
            "session_label": cfg.grades.session_label,
        },
        "retention": {
            "keep_submissions": cfg.retention.keep_submissions,
            "keep_days": cfg.retention.keep_days,
            "cleanup_temp_on_start": cfg.retention.cleanup_temp_on_start,
        },
        "network": {
            "access_mode": cfg.network.access_mode,
            "port": cfg.network.port,
        },
        # Server lifecycle (app.lifecycle): start on demand, stay up while a
        # browser is using the app, stop when idle. These are the real numbers
        # the monitor is running with — the page never displays a value it was
        # not told by the backend.
        "lifecycle": {
            "enabled": cfg.lifecycle.enabled,
            "supervisor": cfg.lifecycle.supervisor,
            "heartbeat_seconds": cfg.lifecycle.heartbeat_seconds,
            "session_timeout_seconds": cfg.lifecycle.session_timeout_seconds,
            "idle_grace_seconds": cfg.lifecycle.idle_grace_seconds,
            "max_shutdown_wait_seconds": cfg.lifecycle.max_shutdown_wait_seconds,
            "last_chance_seconds": cfg.lifecycle.last_chance_seconds,
        },
        "version": APP_VERSION,
    }


@router.post("/settings")
def update_settings(payload: dict):
    cfg = get_config()
    # Roles belong to the account / permission system, not to local settings.
    # A request that tries to set one is REJECTED rather than silently ignored,
    # so no caller can believe it just granted itself Diagnostics. The effective
    # role is still reported by GET /settings (read-only) and comes from the
    # signed-in account, falling back to the local config.
    if "role" in payload:
        raise AppError(
            "User roles are managed by the account and permission system and "
            "cannot be changed from Settings.",
            status=400, code="role_managed_elsewhere")
    if isinstance(payload.get("testing"), dict):
        # The stop-the-line publication gate. This screen is the only place it
        # changes, and save_config() at the end refreshes the in-process config
        # the guard reads, so switching it off takes effect on the very next
        # Google call — the teacher can retry the approval without restarting.
        only = bool(payload["testing"].get("only", cfg.testing.only))
        if only != cfg.testing.only:
            cfg.testing.only = only
            log_event("api", "testing-only mode changed", testing_only=only)
    if "mock_mode" in payload:
        cfg.mock_mode = bool(payload["mock_mode"])
    if "debug" in payload:
        cfg.debug = bool(payload["debug"])
    worker_count_changed = None
    if isinstance(payload.get("worker"), dict):
        if "concurrency" in payload["worker"]:
            requested = max(1, min(8, int(payload["worker"]["concurrency"])))
            if requested != cfg.worker.concurrency:
                cfg.worker.concurrency = requested
                worker_count_changed = requested
        if "max_retries" in payload["worker"]:
            cfg.worker.max_retries = max(0, int(payload["worker"]["max_retries"]))
    if worker_count_changed is not None:
        try:
            _pool().set_size(worker_count_changed)
        except Exception:
            log.exception("worker resize failed")
    if isinstance(payload.get("usage"), dict):
        if "hourly_mb" in payload["usage"]:
            try:
                cfg.usage.hourly_mb = max(0, int(payload["usage"]["hourly_mb"]))
            except (TypeError, ValueError):
                raise AppError("hourly_mb must be a whole number of megabytes (0 = unlimited).",
                               status=400, code="bad_usage_limit")
        if "daily_mb" in payload["usage"]:
            try:
                cfg.usage.daily_mb = max(0, int(payload["usage"]["daily_mb"]))
            except (TypeError, ValueError):
                raise AppError("daily_mb must be a whole number of megabytes (0 = unlimited).",
                               status=400, code="bad_usage_limit")
    if isinstance(payload.get("classroom"), dict):
        cfg.classroom.write_draft_grades = bool(payload["classroom"].get("write_draft_grades", cfg.classroom.write_draft_grades))
    if isinstance(payload.get("teacher"), dict):
        cfg.teacher.teacher_name = str(payload["teacher"].get(
            "teacher_name", cfg.teacher.teacher_name))[:120]
        cfg.teacher.school_name = str(payload["teacher"].get(
            "school_name", cfg.teacher.school_name))[:160]
    if isinstance(payload.get("reports"), dict):
        # Dedicated settings surface for the permanent letterhead (§42).
        # Only keys the teacher actually sends are touched, so an unknown
        # client can never blank the branding by accident.
        allowed = ("teacher_title", "center_name", "academic_year", "quote",
                   "logo_path", "signature_path", "monogram", "footer_note",
                   "accent", "document_label")
        for key in allowed:
            if key in payload["reports"]:
                setattr(cfg.reports, key,
                        str(payload["reports"][key] or "")[:400])
    if isinstance(payload.get("grades"), dict):
        from ..config import _parse_boundaries
        parsed = _parse_boundaries(payload["grades"].get("boundaries"))
        if parsed:
            cfg.grades.boundaries = parsed
        if "session_label" in payload["grades"]:
            cfg.grades.session_label = str(payload["grades"].get("session_label") or "")[:120]
    if isinstance(payload.get("retention"), dict):
        mode = str(payload["retention"].get("keep_submissions", cfg.retention.keep_submissions))
        if mode in ("never", "until_complete", "days", "always"):
            cfg.retention.keep_submissions = mode
        if "keep_days" in payload["retention"]:
            try:
                cfg.retention.keep_days = max(0, int(payload["retention"]["keep_days"]))
            except (TypeError, ValueError):
                raise AppError("keep_days must be a whole number of days.",
                               status=400, code="bad_retention")
    if isinstance(payload.get("network"), dict):
        mode = str(payload["network"].get("access_mode", cfg.network.access_mode))
        if mode not in ("local", "lan"):
            raise AppError("Access mode must be 'local' or 'lan'.",
                           status=400, code="bad_access_mode")
        cfg.network.access_mode = mode
    if isinstance(payload.get("lifecycle"), dict):
        # Server lifecycle. ``enabled`` (stop when idle) applies on the next
        # monitor tick — the monitor re-reads the config every few seconds, so
        # the switch takes effect without a restart. ``supervisor`` (the relay
        # that starts the app again) is a PROCESS and therefore needs a
        # restart; the UI says so next to the switch rather than pretending.
        from ..config import apply_lifecycle_settings
        apply_lifecycle_settings(cfg, payload["lifecycle"])
        log_event("api", "lifecycle settings changed",
                  enabled=cfg.lifecycle.enabled,
                  supervisor=cfg.lifecycle.supervisor,
                  heartbeat_seconds=cfg.lifecycle.heartbeat_seconds,
                  session_timeout_seconds=cfg.lifecycle.session_timeout_seconds,
                  idle_grace_seconds=cfg.lifecycle.idle_grace_seconds)
    save_config(cfg)
    log_event("api", "settings updated", role=cfg.role, mock_mode=cfg.mock_mode)
    return get_settings()


@router.post("/settings/gemini-keys")
def set_gemini_keys(payload: dict):
    """Validate, test and store Gemini keys (additive by default).

    The credential is treated as an OPAQUE SECRET STRING: no format or
    prefix rules are applied — a key is rejected only when empty or an
    exact duplicate (compared by fingerprint, never by display). New keys
    are tested against the real API before activation; a credential that
    fails authentication is stored but left INVALID with the reason, so it
    is never silently saved as healthy (spec §5).
    """
    keys = payload.get("keys")
    if not isinstance(keys, list):
        raise AppError("Provide a 'keys' list.", status=400, code="bad_request")
    cleaned: list[str] = []
    skipped_blank = 0
    for entry in keys:
        value = str(entry).strip()
        if value:
            cleaned.append(value)
        else:
            skipped_blank += 1

    store = _secrets_store()
    existing = store.gemini_keys()
    from ..gemini.key_manager import fingerprint as fp_of

    results: list[dict] = []
    accepted: list[str] = []
    seen: dict[str, str] = {fp_of(k): "existing key" for k in existing}

    for key in cleaned:
        fp = fp_of(key)
        if fp in seen:
            # §11 duplicate detection — identify WITHOUT showing the key.
            dup = _km().status()
            dup_info = next((i for i in dup["keys"] if i["fingerprint"] == fp), None)
            results.append({
                "fingerprint": fp, "duplicate": True, "accepted": False,
                "message": (f"This API key is already configured "
                            f"({seen[fp]}{', status ' + dup_info['status'] if dup_info else ''}) — "
                            "not added again."),
            })
            continue
        seen[fp] = "this submission"
        results.append({"fingerprint": fp, "duplicate": False, "accepted": True})
        accepted.append(key)

    if not accepted and payload.get("replace") is not True and not results:
        raise AppError(
            "No usable key material was provided. Paste at least one key.",
            status=400, code="empty_key_list",
        )
    if payload.get("replace") is True and not accepted and not results:
        raise AppError(
            "Refusing to replace the key list with an empty one. Disable "
            "individual keys on the API Keys page instead.",
            status=400, code="empty_key_list",
        )

    # --- Save & Test (§5): validate against the real API BEFORE activating.
    activated = 0
    for idx, key in enumerate(accepted):
        fp = fp_of(key)
        state = test_key(idx + 1, key, fp)
        ok = state.status in ("HEALTHY", "COOLDOWN")  # rate-limited ≠ bad key
        result = next(r for r in results if r["fingerprint"] == fp)
        result["test"] = {
            "status": state.status,
            "auth_ok": state.auth_ok,
            "generate_ok": state.generate_ok,
            "response_time_ms": state.response_time_ms,
            "error_kind": state.error_kind,
            "message": state.error_message
                       or (f"Credential accepted — latency {state.response_time_ms} ms"
                           if state.status == "HEALTHY" else "Credential reached Gemini but is rate limited."),
        }
        result["accepted"] = ok

    # Persist: keep existing keys first (priority order), then newly accepted.
    merged = list(existing)
    for key in accepted:
        if key not in merged:
            merged.append(key)
    if merged:
        store.set_gemini_keys(merged)

    from ..gemini.key_manager import KeyManager
    km = _km()
    for item in results:
        if item.get("duplicate"):
            continue
        fp = item["fingerprint"]
        test = item.get("test")
        if test is None:
            continue
        if item["accepted"]:
            activated += 1
            km.re_enable(fp)  # clears any INVALID state from a failed earlier paste
        else:
            # Store but DO NOT activate (§5): record the failure so the key
            # shows its real status and reason in the list instead of a
            # generic "invalid".
            km.register_manual_result(
                fp, ok=False,
                kind=test.get("error_kind") or "unknown",
                message=test.get("message") or "Key failed validation",
            )
        _km_set_label(fp)

    added = len(merged) - len(existing)
    return {
        "count": len(merged), "added": added, "activated": activated,
        "skipped_blank": skipped_blank, "results": results,
    }


def _km_set_label(fp: str) -> None:
    """Give a newly added key its safe KEY_n label (never the secret)."""
    _ensure_key_labels(_km().status())


@router.post("/keys/{fingerprint}/remove")
def remove_key(fingerprint: str):
    """Remove a key from the secret store (§9/§10); stats row is kept."""
    store = _secrets_store()
    remaining = [k for k in store.gemini_keys()
                 if _fp_of_key(k) != fingerprint]
    if len(remaining) == len(store.gemini_keys()):
        raise HTTPException(404, "Key not found")
    store.set_gemini_keys(remaining)
    _km().mark_removed(fingerprint)
    return keys_status()


def _fp_of_key(key: str) -> str:
    from ..gemini.key_manager import fingerprint
    return fingerprint(key)


def _secrets_store():
    from ..config import get_secrets
    return get_secrets()


def _require_usage_budget() -> None:
    """Refuse NEW corrections when the internet-usage budget is exhausted.
    Running jobs finish; the rolling window rolls over automatically."""
    from ..net_usage import limits, usage_state
    state = usage_state()
    if not (state["hourly_over"] or state["daily_over"]):
        return
    lim = limits()
    which = "hourly" if state["hourly_over"] else "daily"
    cap = lim["hourly_mb"] if state["hourly_over"] else lim["daily_mb"]
    raise AppError(
        f"{which.capitalize()} internet usage limit reached "
        f"({cap} MB) — new corrections are paused until the window resets. "
        "Adjust the limit on the Usage page if this is too strict.",
        status=429, code="usage_limit_reached",
    )


# ============================================================
# Diagnostics (Developer role)
# ============================================================

@router.get("/diagnostics")
def diagnostics():
    cfg = get_config()
    if cfg.role != "developer":
        raise AppError("Diagnostics are available in Developer role only.",
                       status=403, code="developer_only")
    import platform
    import sys
    counts = _job_manager().counts()
    keys = _km().status()
    db_row = db.query_one("SELECT COUNT(*) AS n FROM correction_jobs")
    return {
        "system": {
            "app_version": APP_VERSION,
            "schema_version": SCHEMA_VERSION,
            "python_version": sys.version.split()[0],
            "platform": platform.platform(),
            "database": str(db.DB_PATH),
            "jobs_total": db_row["n"],
            "mock_mode": cfg.mock_mode,
        },
        "gemini": {**keys, "aggregate": _km().aggregate()},
        "classroom": {
            **auth_state().public(),
            "last_sync": classroom_sync.last_sync(),
            "courses": db.query_one("SELECT COUNT(*) AS n FROM courses")["n"],
            "coursework": db.query_one("SELECT COUNT(*) AS n FROM coursework")["n"],
            "submissions": db.query_one("SELECT COUNT(*) AS n FROM submissions")["n"],
        },
        "jobs": counts,
        "storage": _storage_stats(),
    }


def _log_category(entry: dict) -> str:
    """Bucket one structured log line for the UI filters (§17).

    Job/worker lifecycle events are classified by their outcome first (a
    WARNING about a retrying job still belongs in Running), then by level.
    """
    component = str(entry.get("component") or "")
    event = str(entry.get("event") or "").lower()
    level = str(entry.get("level") or "INFO").upper()
    is_job = component in ("jobs", "workers")
    if is_job or "job" in event or "submission" in event:
        if "cancel" in event:
            return "cancelled"
        if "completed" in event or "saved" in event:
            return "completed"
        if "failed" in event or "invalid" in event:
            return "failed"
        if any(word in event for word in
               ("enqueued", "started", "retry", "waiting", "processing",
                "dispatch", "stage", "parked")):
            return "running"
    if level in ("WARNING", "ERROR", "CRITICAL"):
        return "warning"
    return "system"


@router.get("/diagnostics/logs")
def diagnostics_logs(tail: int = 300, category: str | None = None):
    """Structured log entries for the UI log view.

    Returns parsed entries (never raw secret-bearing lines — the logger
    already redacts on write). ``category`` filters to one bucket:
    running | completed | failed | cancelled | warning | system.
    The view is display-only; clearing it never touches the database.
    """
    cfg = get_config()
    if cfg.role != "developer":
        raise AppError("Logs are available in Developer role only.",
                       status=403, code="developer_only")
    log_path = data_dir() / "logs" / "app.log"
    entries: list[dict] = []
    if log_path.exists():
        lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
        for line in lines[-max(1, min(tail, 2000)):]:
            try:
                parsed = json.loads(line)
                if not isinstance(parsed, dict):
                    raise ValueError("not an object")
            except ValueError:
                # Non-JSON line (e.g. a library printed to stdout): keep it
                # as a system entry instead of dropping the information.
                parsed = {"level": "INFO", "component": "app", "event": line}
            entries.append({
                "ts": parsed.get("ts"),
                "level": parsed.get("level", "INFO"),
                "component": parsed.get("component", "app"),
                "event": parsed.get("event", ""),
                "category": _log_category(parsed),
                "context": {k: v for k, v in parsed.items()
                            if k not in ("ts", "level", "component", "event")},
            })
    if category and category.lower() != "all":
        wanted = category.lower()
        entries = [e for e in entries if e["category"] == wanted]
    entries.reverse()  # newest first
    counts: dict[str, int] = {}
    for e in entries:
        counts[e["category"]] = counts.get(e["category"], 0) + 1
    return {"entries": entries, "counts": counts, "total": len(entries)}


@router.post("/diagnostics/logs/clear")
def diagnostics_clear_log(payload: dict | None = None):
    """Clear the stored application log FILE (not the database).

    Body: ``{"confirm": true}`` — required, because this discards stored
    log information. Student data, correction results, Classroom data,
    assignments, job history and audit/state-transition records live in
    SQLite and are NEVER touched here; only ``data/logs/app.log`` is
    truncated. The clear itself is recorded in the log afterwards.
    """
    cfg = get_config()
    if cfg.role != "developer":
        raise AppError("Logs are available in Developer role only.",
                       status=403, code="developer_only")
    if not (payload or {}).get("confirm"):
        raise AppError(
            "Clearing the stored log discards its history. Confirm to "
            "proceed (database records are never affected).",
            status=400, code="confirm_required")
    log_path = data_dir() / "logs" / "app.log"
    size_before = log_path.stat().st_size if log_path.exists() else 0
    # Truncate in place: the FileHandler keeps its handle and mode='a', so
    # logging continues normally afterwards.
    if log_path.exists():
        with open(log_path, "w", encoding="utf-8"):
            pass
    log_event("logs", "log file cleared by user",
              bytes_removed=size_before, role=cfg.role)
    # Guarantee the audit record exists IN the cleared file itself — the
    # configured FileHandler may point elsewhere (tests, custom setups), and
    # a cleared log with no explanation would be useless.
    try:
        if log_path.exists() and log_path.stat().st_size == 0:
            with open(log_path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps({
                    "ts": datetime.now(timezone.utc).isoformat(
                        timespec="milliseconds"),
                    "level": "INFO", "component": "logs",
                    "event": "log file cleared by user",
                    "bytes_removed": size_before, "role": cfg.role,
                }) + "\n")
    except OSError:
        pass
    return {"cleared": True, "bytes_removed": size_before,
            "note": "Only the log file was cleared — database records "
                    "(students, results, Classroom data, jobs) are untouched."}


@router.get("/diagnostics/db")
def diagnostics_db():
    cfg = get_config()
    if cfg.role != "developer":
        raise AppError("Database diagnostics are Developer-role only.",
                       status=403, code="developer_only")
    tables = {}
    for table in ("courses", "coursework", "students", "submissions",
                  "correction_jobs", "results", "gemini_keys", "key_stats"):
        tables[table] = db.query_one(f"SELECT COUNT(*) AS n FROM {table}")["n"]
    return {"tables": tables}


def _storage_stats():
    def dir_size(path: Path) -> int:
        if not path.exists():
            return 0
        return sum(f.stat().st_size for f in path.rglob("*") if f.is_file())

    return {
        "db_bytes": db.DB_PATH.stat().st_size if db.DB_PATH.exists() else 0,
        "tmp_bytes": dir_size(data_dir() / "tmp"),
        "mark_scheme_bytes": dir_size(data_dir() / "mark_schemes"),
        "logs_bytes": dir_size(data_dir() / "logs"),
    }


# ============================================================
# Legacy comparison harness (Regression §25)
# ============================================================

@router.post("/diagnostics/compare-legacy")
async def compare_legacy(submission: UploadFile = File(...),
                         mark_scheme: UploadFile = File(...),
                         request: Request = None):
    """Run V5's pipeline and the legacy engine on the same inputs and diff
    the totals. Requires a real API key; Developer role only."""
    _require_developer(request)
    cfg = get_config()
    import tempfile
    import os
    tmp = Path(tempfile.mkdtemp(prefix="mc5_compare_"))
    sub_path = tmp / (submission.filename or "student.pdf")
    ms_path = tmp / (mark_scheme.filename or "scheme.pdf")
    sub_path.write_bytes(await submission.read())
    ms_path.write_bytes(await mark_scheme.read())
    try:
        from ..gemini.client_proxy import KeyAwareClient
        client = KeyAwareClient(_km())
        remote_sub = client.upload_file(str(sub_path))
        remote_ms = client.upload_file(str(ms_path))
        raw, model = legacy.grade_with_model(
            client=client,
            model_names=cfg.gemini.primary_models,
            student_file=remote_sub,
            mark_scheme_file=remote_ms,
        )
        result = legacy.normalise_result(raw)
        earned, available, percentage = legacy.calculate_totals(result)
        return {
            "engine": "legacy-in-v5",
            "marking_model": model,
            "earned": earned, "available": available,
            "percentage": round(percentage, 2),
            "questions": [
                {"number": q["question_number"], "awarded": q["awarded_marks"],
                 "max": q["max_marks"]} for q in result["questions"]
            ],
        }
    finally:
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)


# ============================================================ Prompt inspection (Developer only, §prompt page)
# The texts are served from the engine's OWN constants and functions
# (legacy.marking_user_prompt / legacy.effective_system_prompt /
# legacy.audit_prompt), never from a copy, so this page always shows exactly
# what a correction sends to Gemini — manual and automatic alike.

_SAMPLE_BLUEPRINT = {
    "total_marks": 25,
    "questions": [
        {"question_number": "1", "max_marks": 10,
         "criteria": "M1 correct method\nA1 correct answer"},
        {"question_number": "2", "max_marks": 15,
         "criteria": "B1 evidence of chain"},
    ],
}

_SAMPLE_REPORT = {
    "paper_info": {"qualification": "Edexcel IGCSE", "paper": "1P",
                   "session": "Sample", "max_marks": 25},
    "questions": [{
        "question_number": "1", "max_marks": 10, "awarded_marks": 8,
        "topic": "Algebra", "student_answer": "x = 4",
        "working_summary": "Sample working",
        "loss_reason": "M1 earned, A1 lost on arithmetic slip",
        "mark_breakdown": [{"code": "M1", "awarded": True, "reason": "Sample"}],
        "correct_method": "Sample method",
        "full_mark_solution": "Sample full-mark solution",
    }],
    "overall_notes": {"summary": "Sample", "strengths": [],
                      "revision_areas": []},
    "uncertainties": [],
}


def _require_developer(request: Request | None = None) -> None:
    """Developer-only gate for the prompt page and developer diagnostics.

    Two conditions, because the app now has BOTH kinds of role:
      1. the app-wide role (Settings → developer mode) — the historical gate,
      2. with the multi-user system on, the SIGNED-IN account must itself be a
         DEVELOPER. A teacher's session must never reach these endpoints just
         because the machine is in developer mode.
    A local run without any session (login not required) keeps the historical
    behaviour: the app role alone decides.
    """
    if get_config().role != "developer":
        raise AppError("This page is Developer-role only.", status=403,
                       code="developer_only")
    if request is None:
        return
    identity = getattr(request.state, "identity", None)
    if identity is not None and identity.get("role") != "DEVELOPER":
        raise AppError("This page is Developer-role only.", status=403,
                       code="developer_only")


@router.get("/dev/prompts")
def dev_prompts(request: Request):
    """Every prompt the marking engine sends, served from the engine itself.

    Includes the live override state, the model chains, the sampling config
    and the JSON response schema — enough to see exactly what Gemini receives
    for a correction before deciding to edit anything.
    """
    _require_developer(request)
    cfg = get_config()
    from ..engine import blueprint as bp_mod
    from ..engine import double_check as dc_mod

    system_effective = legacy.effective_system_prompt()
    user_effective = legacy.marking_user_prompt()
    return {
        "role": "developer",
        "engine_source": (
            "app/engine/legacy_marking.py — these are the engine's own "
            "constants and functions, not a copy"),
        "overrides": {
            "system_prompt_override": cfg.gemini.system_prompt_override,
            "user_prompt_override": cfg.gemini.user_prompt_override,
            "active": bool(cfg.gemini.system_prompt_override
                           or cfg.gemini.user_prompt_override),
        },
        "models": {
            "chain": list(cfg.gemini.primary_models),
            "double_check_second_pass": dc_mod.second_pass_models(
                list(cfg.gemini.primary_models), None),
        },
        "sampling": {
            "marking": getattr(legacy, "DETERMINISTIC_SAMPLING", {}),
            "double_check": dc_mod.SECOND_PASS_SAMPLING,
        },
        "prompts": [
            {"id": "marking_system",
             "when": "every marking call — manual AND automatic correction",
             "transport": "config.system_instruction",
             "edited": bool(cfg.gemini.system_prompt_override),
             "text": system_effective,
             "builtin": legacy.MARKING_SYSTEM},
            {"id": "marking_user",
             "when": "every marking call — sent as the last part, after the "
                     "uploaded files",
             "transport": "last item of contents",
             "edited": bool(cfg.gemini.user_prompt_override),
             "text": user_effective,
             "text_with_blueprint": legacy.marking_user_prompt(
                 _SAMPLE_BLUEPRINT),
             "builtin": legacy._MARKING_USER_BASE},
            {"id": "blueprint_block",
             "when": "appended to marking_user once the mark-scheme blueprint "
                     "was extracted (keeps numbering + max marks stable)",
             "transport": "same content part",
             "edited": False,
             "text": bp_mod.prompt_block(_SAMPLE_BLUEPRINT)},
            {"id": "audit_user",
             "when": "legacy audit second pass (not run by default in V5)",
             "transport": "last item of contents",
             "edited": False,
             "text": legacy.audit_prompt(_SAMPLE_REPORT)},
        ],
        "response_schema": legacy.OUTPUT_SCHEMA,
    }


@router.post("/dev/prompts")
def set_dev_prompts(payload: dict, request: Request):
    """Save (or clear) the Developer prompt overrides.

    An empty string clears the override, restoring the built-in legacy prompt.
    The override lives in config.toml, so the worker child and the automation
    scheduler read the same text — every correction, manual or automatic, uses
    one prompt.
    """
    _require_developer(request)
    changed = []
    if "system_prompt_override" in payload:
        changed.append("system_prompt_override")
    if "user_prompt_override" in payload:
        changed.append("user_prompt_override")
    cfg = get_config()
    if "system_prompt_override" in payload:
        cfg.gemini.system_prompt_override = str(
            payload.get("system_prompt_override") or "").strip() or None
    if "user_prompt_override" in payload:
        cfg.gemini.user_prompt_override = str(
            payload.get("user_prompt_override") or "").strip() or None
    save_config(cfg)
    from ..logging_setup import log_event
    log_event("engine", "prompt override updated",
              system_override=bool(cfg.gemini.system_prompt_override),
              user_override=bool(cfg.gemini.user_prompt_override))
    return {**dev_prompts(request), "updated": changed}
