"""Data portability endpoints (§2): student/result export, preview and import.

Four calls, all under ``/api`` so the existing auth gate (session + CSRF when
public access is on) covers them:

* ``GET  /api/data/inventory``      — what this installation holds, plus the
  export format's identity so the UI can describe it truthfully;
* ``GET  /api/data/export``         — download the versioned JSON package;
* ``POST /api/data/import/preview`` — analyse an uploaded package and report
  new / matching / invalid records WITHOUT touching the database;
* ``POST /api/data/import``         — apply it, transactionally, under an
  explicit policy (``add_only`` or ``update_existing``).

Nothing here is a mock: the preview counts come from the same tables the import
writes to, and the import runs inside a single SQLite transaction.
"""

from __future__ import annotations

from fastapi import APIRouter, File, Form, Request, UploadFile
from fastapi.responses import Response

from .. import portability
from ..errors import AppError
from ..logging_setup import log_event

router = APIRouter(prefix="/api", tags=["data"])

#: Refuse absurd uploads before reading them into memory.
MAX_UPLOAD_BYTES = 64 * 1024 * 1024


def register(app) -> None:
    app.include_router(router)


def _actor(request: Request) -> str:
    """A log-friendly identity that is never a credential."""
    try:
        from .. import auth_gate as ag
        if ag.is_genuinely_local(request):
            return "local"
        session = ag.read_session(request.cookies.get(ag.SESSION_COOKIE))
        identity = ag.session_identity(session) if session else None
        if identity:
            return str(identity.get("username") or "signed-in")
    except Exception:  # noqa: BLE001 — identity is only for the audit log
        pass
    return "unknown"


async def _read_upload(file: UploadFile) -> bytes:
    if file is None or not getattr(file, "filename", None):
        raise AppError("Choose an exported .json file to import.",
                       status=400, code="no_file")
    raw = await file.read()
    if not raw:
        raise AppError("That file is empty.", status=400, code="empty_file")
    if len(raw) > MAX_UPLOAD_BYTES:
        raise AppError("That file is larger than 64 MB, which no student/"
                       "result export reaches.", status=400,
                       code="export_too_large")
    return raw


@router.get("/data/inventory")
def data_inventory():
    """Storage facts for the Storage page: real counts from the real tables."""
    return portability.storage_inventory()


@router.get("/data/export")
def data_export(request: Request):
    """Download the versioned, secrets-free student/result package."""
    payload = portability.export_json()
    filename = portability.export_filename()
    log_event("portability", "student/result export downloaded",
              bytes=len(payload), actor=_actor(request))
    return Response(
        payload,
        media_type="application/json",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Cache-Control": "no-store",
        },
    )


@router.post("/data/import/preview")
async def data_import_preview(request: Request,
                              file: UploadFile = File(...)):
    """Analyse an uploaded package. Read-only: nothing is written."""
    raw = await _read_upload(file)
    package = portability.parse(raw)
    result = portability.analyse(package)
    result["filename"] = file.filename
    result["bytes"] = len(raw)
    log_event("portability", "student/result import previewed",
              new=result["totals"]["new"],
              matching=result["totals"]["matching"],
              invalid=result["totals"]["invalid"], actor=_actor(request))
    return result


@router.post("/data/import")
async def data_import(request: Request,
                      file: UploadFile = File(...),
                      mode: str = Form("add_only"),
                      confirm: str = Form("")):
    """Apply a package. Transactional, with an explicit overwrite policy.

    ``update_existing`` additionally requires ``confirm=replace`` so a client
    cannot start an overwriting import by accident, and the database is backed
    up (SQLite's backup API) before the transaction opens.
    """
    raw = await _read_upload(file)
    package = portability.parse(raw)
    if mode == "update_existing" and confirm != "replace":
        raise AppError(
            "Updating existing records needs explicit confirmation.",
            status=400, code="confirm_required")
    outcome = portability.apply_import(package, mode=mode,
                                       actor=_actor(request))
    outcome["filename"] = file.filename
    return outcome
