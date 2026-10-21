"""Read-only student-work viewer for the marking workspace (§UI redesign).

The marking workspace shows the ACTUAL submitted work next to the marking
result. These routes never invent content: they serve the original submission
file the job was run from, rendered page by page, or they say plainly that the
original is no longer stored.

Where the original lives (all under the app data dir):

1. ``retained_files``    — the teacher asked to keep submissions (§19) and the
   job already finished; the file sits in ``data/retained/<job_id>/``.
2. ``pending_manual_files`` — the job is still active (or between retry rounds);
   the staged upload has not been cleaned up yet.
3. ``large_pdf_jobs``    — the batched pipeline keeps its own job row with the
   student PDF path until its cleanup runs.

A result whose original is gone (the default retention mode deletes the file
when the job finishes) answers ``available: false`` with a human reason — the
viewer degrades instead of failing. Retention semantics are NOT changed here.

Rendering reuses the verified mark-scheme page pipeline
(``engine.scheme_inventory.render_pages_png``): the same PyMuPDF renderer, the
same per-document/page/dpi disk cache layout, the same ETag behaviour.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import Response

from ..config import data_dir, get_config
from .. import db
from ..errors import AppError

log = logging.getLogger("api.viewer")

router = APIRouter(prefix="/api/results")

#: Same resolution choices as the mark-scheme viewer: readable at 150 dpi,
#: bounded so one page stays a normal HTTP response.
_PAGE_DPI_DEFAULT = 150
_PAGE_DPI_MAX = 300

_IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"}
_IMAGE_MEDIA = {
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".webp": "image/webp", ".gif": "image/gif", ".bmp": "image/bmp",
}


def register(app) -> None:
    app.include_router(router)


# ----------------------------------------------------------------- locating


def _inside_data_dir(path: Path) -> bool:
    """Containment: the viewer only ever reads from the app's own data dir."""
    try:
        path.resolve().relative_to(data_dir().resolve())
    except (OSError, ValueError):
        return False
    return True


def _first_existing(rows, *, job_id: str) -> tuple[Path, str, str] | None:
    """First existing, contained ``(path, filename, source)`` from DB rows."""
    for row in rows:
        raw = row["path"]
        if not raw:
            continue
        path = Path(raw)
        try:
            if not path.is_file():
                continue
        except OSError:
            continue
        if not _inside_data_dir(path):
            log.warning("viewer refused a path outside the data dir")
            continue
        return path, str(row.get("filename") or path.name), str(row["source"])
    return None


def locate_submission(result_row: dict) -> tuple[Path, str, str] | None:
    """``(path, filename, source)`` of the original submission for a result."""
    job_id = result_row.get("job_id") or ""
    if not job_id:
        return None

    rows = [
        dict(r, source="retained")
        for r in db.query(
            "SELECT path, filename FROM retained_files "
            "WHERE job_id = ? AND kind = 'submission'", (job_id,))
    ]
    found = _first_existing(rows, job_id=job_id)
    if found:
        return found

    rows = [
        dict(r, source="staged")
        for r in db.query(
            "SELECT path, filename FROM pending_manual_files "
            "WHERE job_id = ? AND kind = 'submission'", (job_id,))
    ]
    found = _first_existing(rows, job_id=job_id)
    if found:
        return found

    rows = []
    for r in db.query(
            "SELECT student_pdf_path FROM large_pdf_jobs WHERE result_id = ?",
            (result_row.get("id"),)):
        raw = r["student_pdf_path"]
        if raw:
            rows.append({"path": raw, "filename": Path(raw).name,
                         "source": "large_pdf_job"})
    return _first_existing(rows, job_id=job_id)


#: Job states that still have work to do; their files are not deleted yet.
_ACTIVE_JOB_STATES = {
    "QUEUED", "STARTING", "PREPARING", "DOWNLOADING", "UPLOADING", "MARKING",
    "VERIFYING", "SAVING", "WAITING_FOR_API", "RETRYING", "PROCESSING",
    "READY", "PLANNING",
}


def _retention_state() -> dict:
    """The authoritative retention setting, safe to expose to the teacher."""
    try:
        cfg = get_config().retention
        return {"keep_submissions": cfg.keep_submissions,
                "keep_days": int(cfg.keep_days or 0),
                "keeps": cfg.keep_submissions in ("days", "always")}
    except Exception:  # noqa: BLE001 — a broken config must not break the viewer
        return {"keep_submissions": "unknown", "keep_days": 0, "keeps": False}


def _job_state(result_row: dict) -> str:
    """The job's status, from whichever pipeline owns it."""
    job_id = result_row.get("job_id") or ""
    if not job_id:
        return ""
    row = db.query_one("SELECT status FROM correction_jobs WHERE id = ?",
                       (job_id,))
    if row and row["status"]:
        return str(row["status"])
    row = db.query_one("SELECT status FROM large_pdf_jobs WHERE id = ?",
                       (job_id,))
    return str(row["status"] or "") if row else ""


def _missing_payload(result_id: str, result_row: dict) -> dict:
    """Why the original is not here — the honest, specific reason.

    The four cases must never be conflated (§ retention): retention switched
    off, retention on but this result has no stored copy (legacy or lost),
    the job is still running, and a result with no job link at all. Only the
    first is "the setting deleted it".
    """
    retention = _retention_state()
    state = _job_state(result_row)
    if state in _ACTIVE_JOB_STATES:
        reason = "processing"
        message = ("This paper is still being processed. The original file is "
                   "readable here once the correction finishes.")
        retryable = True
    elif not result_row.get("job_id"):
        reason = "no_link"
        message = ("This result is not linked to a stored correction job, so "
                   "there is no file this device could have kept.")
        retryable = False
    elif retention["keeps"]:
        reason = "file_missing"
        message = ("Submission retention is ON ("
                   + ("keep for %d days" % retention["keep_days"]
                      if retention["keep_submissions"] == "days"
                      else "always keep")
                   + "), but no stored copy exists for this result. It was "
                   "probably marked before retention was enabled, or the "
                   "stored copy was removed outside the app.")
        retryable = True
    else:
        reason = "retention_disabled"
        message = ("Submission retention is OFF, so the original is deleted "
                   "when a correction finishes. Set Settings → Privacy & Data "
                   "to keep submissions if you want papers kept.")
        retryable = False
    return {
        "result_id": result_id,
        "available": False,
        "reason": reason,
        "message": message,
        "retryable": retryable,
        "job_status": state or None,
        "retention": retention,
        "checked_at": time.time(),
        "page_count": 0,
        "file_kind": None,
        "file_name": None,
        "source": None,
        "dpi": _PAGE_DPI_DEFAULT,
        "page_url_template": f"/api/results/{result_id}/pages/{{page}}.png",
    }


def _rendered_page_path(sha: str, page: int, dpi: int) -> Path:
    """Disk cache for rendered submission pages (same layout as scheme pages)."""
    folder = data_dir() / "tmp" / "result_pages" / sha[:16]
    folder.mkdir(parents=True, exist_ok=True)
    return folder / f"p{page:04d}@{dpi}.png"


# -------------------------------------------------------------------- routes


@router.get("/{result_id}/pages")
def result_pages(result_id: str):
    """Which pages of the ORIGINAL submission exist for this result.

    Deterministic and offline: reads the stored file's page count only. When
    the original is not stored (the default retention mode deletes it when the
    job finishes) the answer is ``available: false`` with the reason — the
    workspace shows that instead of a broken viewer.
    """
    row = db.query_one("SELECT * FROM results WHERE id = ?", (result_id,))
    if not row:
        raise AppError("That correction result does not exist.",
                       status=404, code="result_not_found")

    located = locate_submission(dict(row))
    if not located:
        return _missing_payload(result_id, dict(row))

    path, filename, source = located
    suffix = path.suffix.lower()
    if suffix in _IMAGE_SUFFIXES:
        file_kind, page_count = "image", 1
    else:
        from ..engine import pdf_inspect
        page_count = int(pdf_inspect.page_count(path) or 0)
        file_kind = "pdf"

    if page_count <= 0:
        # A path that exists but yields no readable page is NOT an available
        # submission: say the stored copy is damaged instead of showing a
        # broken viewer.
        payload = _missing_payload(result_id, dict(row))
        payload.update({"reason": "file_unreadable", "retryable": False,
                        "message": ("The stored copy of this submission cannot "
                                    "be read (no page could be opened). The "
                                    "marks are unaffected; re-correcting the "
                                    "paper is the only way to restore it.")})
        return payload

    return {
        "result_id": result_id,
        "available": True,
        "reason": None,
        "message": None,
        "retryable": False,
        "job_status": _job_state(dict(row)) or None,
        "retention": _retention_state(),
        "checked_at": time.time(),
        "page_count": page_count,
        "file_kind": file_kind,
        "file_name": filename,
        "source": source,
        "dpi": _PAGE_DPI_DEFAULT,
        "page_url_template": f"/api/results/{result_id}/pages/{{page}}.png",
    }


@router.get("/{result_id}/pages/{page}.png")
def result_page_image(result_id: str, page: int, request: Request,
                      dpi: int = _PAGE_DPI_DEFAULT):
    """One ACTUAL page of the student's work, rendered as PNG.

    Cached per document/page/dpi with an ETag — flipping through a paper does
    not re-render it, and a conditional request gets an empty 304.
    """
    from ..engine import blueprint as bp_mod
    from ..engine import scheme_inventory as si

    row = db.query_one("SELECT * FROM results WHERE id = ?", (result_id,))
    if not row:
        raise AppError("That correction result does not exist.",
                       status=404, code="result_not_found")
    located = locate_submission(dict(row))
    if not located:
        raise AppError(
            "The original submission file is not stored on this device, so its "
            "pages cannot be shown.",
            status=404, code="original_not_stored")
    path, filename, source = located
    suffix = path.suffix.lower()

    if suffix in _IMAGE_SUFFIXES:
        if page != 1:
            raise AppError(f"This submission is one image; page {page} does "
                           "not exist.", status=404, code="page_out_of_range")
        try:
            content = path.read_bytes()
        except OSError as exc:
            raise AppError(f"The submission image could not be read: {exc}",
                           status=500, code="page_render_failed")
        return Response(
            content=content, media_type=_IMAGE_MEDIA.get(suffix, "image/png"),
            headers={"Cache-Control": "private, max-age=86400",
                     "Content-Disposition": f'inline; filename="{filename}"'})

    dpi = max(72, min(int(dpi or _PAGE_DPI_DEFAULT), _PAGE_DPI_MAX))
    try:
        sha = bp_mod.file_hash(path)
        total = int(si.page_count(path) or 0)
    except OSError as exc:
        raise AppError(f"The submission file could not be read: {exc}",
                       status=500, code="submission_unreadable")
    if page < 1 or page > total:
        raise AppError(f"This submission has {total} page(s); page {page} does "
                       "not exist.", status=404, code="page_out_of_range")

    target = _rendered_page_path(sha, page, dpi)
    etag = f'"{sha[:16]}-p{page}-{dpi}"'
    if etag in (request.headers.get("if-none-match") or ""):
        return Response(status_code=304,
                        headers={"ETag": etag,
                                 "Cache-Control": "private, max-age=86400"})
    if not target.exists():
        try:
            images = si.render_pages_png(path, [page], dpi=dpi)
        except si.InventoryError as exc:
            raise AppError(f"The page could not be rendered: {exc}",
                           status=500, code="page_render_failed")
        if not images:
            raise AppError(f"Page {page} could not be rendered.",
                           status=500, code="page_render_failed")
        try:
            target.write_bytes(images[0]["png"])
        except OSError:  # caching is best-effort; the response still goes out
            pass
        content = images[0]["png"]
    else:
        try:
            content = target.read_bytes()
        except OSError as exc:
            raise AppError(f"The cached page could not be read: {exc}",
                           status=500, code="page_render_failed")
    return Response(
        content=content, media_type="image/png",
        headers={"ETag": etag,
                 "Cache-Control": "private, max-age=86400",
                 "Content-Disposition": f'inline; filename="page-{page}.png"'})
