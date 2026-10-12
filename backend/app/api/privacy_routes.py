"""Privacy endpoints: data locations, factory reset, sanitized diagnostics.

Two teacher-facing guarantees live here:

  * FACTORY RESET removes every piece of LOCAL user data (keys, OAuth token,
    configuration, database, cache, logs, reports) while leaving the installed
    application untouched and fully reinstallable.

  * EXPORT DIAGNOSTICS produces a shareable support file that is deliberately
    sanitized: no API keys, no OAuth material, no student names, submissions or
    emails, no reports. Only build/environment facts, integration booleans,
    aggregate counts and recent redacted log lines.
"""

from __future__ import annotations

import json
import os
import platform
import re
import shutil
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import Response

from .. import config as app_config
from .. import db
from ..errors import AppError
from ..logging_setup import log_event, reset_logging, setup_logging
from ..version import build_info

router = APIRouter(prefix="/api")


def register(app) -> None:
    app.include_router(router)


# --------------------------------------------------------------- locations

def _data_dir() -> Path:
    return Path(app_config.DATA_DIR)


def _remove(target: Path, attempts: int = 6) -> None:
    """Delete a file or tree, retrying brief Windows lock contention.

    Antivirus, the search indexer and a just-closed handle can hold a file for
    a fraction of a second; a naive unlink leaves data behind. We retry on
    Windows' "file in use" error and re-raise anything else immediately.
    """
    for attempt in range(attempts):
        try:
            if target.is_dir():
                shutil.rmtree(target, ignore_errors=False)
            else:
                target.unlink()
            return
        except OSError as exc:
            winerror = getattr(exc, "winerror", None)
            locked = winerror in (5, 32) or isinstance(exc, PermissionError)
            if not locked or attempt == attempts - 1:
                raise
            time.sleep(0.25 * (attempt + 1))


def _redact_path(path: Path) -> str:
    """Never leak a full user path (it contains the account name)."""
    text = str(path)
    home = str(Path.home())
    if home and home in text:
        text = "%USERPROFILE%" + text[len(home):]
    return text


@router.get("/privacy/info")
def privacy_info():
    data = _data_dir()
    def exists(rel: str) -> bool:
        return (data / rel).exists()
    return {
        "data_directory": _redact_path(data),
        "application_files_separate": True,
        "items": [
            {"key": "config", "label": "Configuration", "present": exists("config.toml")},
            {"key": "credentials", "label": "API keys & Google credentials",
             "present": exists("secrets.toml") or exists("secrets")},
            {"key": "database", "label": "Local database", "present": exists("app.db")},
            {"key": "cache", "label": "Temporary/cache files", "present": exists("tmp")},
            {"key": "logs", "label": "Logs", "present": exists("logs")},
            {"key": "reports", "label": "Generated reports",
             "present": exists("reports") or exists("mark_schemes")},
        ],
        "note": ("Your data is kept separate from the application files, so "
                 "updating or reinstalling Math Checker never removes it."),
    }


# ------------------------------------------------------------ factory reset

_DELETE_ON_RESET = (
    "config.toml", "secrets.toml", "secrets", "app.db", "app.db-wal",
    "app.db-shm", "app.db-journal", "logs", "tmp", "reports", "mark_schemes",
    "blueprints", "retained", "demo",
)


@router.post("/privacy/factory-reset")
def factory_reset(payload: dict):
    """Remove all local user data. The application itself is left installed.

    Requires ``{"confirm": "RESET"}`` so a stray click can never wipe a teacher's
    work. Nothing outside the data directory is ever touched.
    """
    if str(payload.get("confirm", "")) != "RESET":
        raise AppError("Factory reset requires an explicit confirmation.",
                       status=400, code="confirmation_required")

    data = _data_dir()
    removed: list[str] = []
    failures: list[str] = []

    # Everything below runs with EXCLUSIVE database access: no other thread may
    # query or re-open app.db while it is being deleted and rebuilt. Before
    # this, a concurrent request re-opened the file mid-delete, so Windows
    # refused the delete and the old data silently survived the "reset".
    with db.reset_exclusive():
        # Release the open app.log handle too: Windows will not delete a file a
        # stream still holds (WinError 32), which used to leave `logs` behind.
        reset_logging()

        for name in _DELETE_ON_RESET:
            target = data / name
            if not target.exists():
                continue
            try:
                _remove(target)
                removed.append(name)
            except OSError as exc:  # noqa: BLE001 — one failure must not abort the rest
                failures.append(f"{name}: {exc}")

        # Rebuild a clean, empty data layout, logging and a fresh database so
        # the running process is immediately usable again (no restart needed).
        app_config.ensure_dirs()
        app_config._config = None  # force configuration reload (wizard will show)
        app_config._secrets = None
        setup_logging()
        db.init_db()

    log_event("privacy", "factory reset performed",
              removed=len(removed), failures=len(failures))
    return {
        "reset": True,
        "removed": removed,
        "failures": failures,
        "reinstall_state": "first_run",
        "message": ("All local data and credentials were removed. Math Checker "
                    "remains installed and will start fresh."),
    }


# ------------------------------------------------------- diagnostic export

_SECRET_PATTERNS = re.compile(
    r"(AIza[0-9A-Za-z_\-]{10,}|sk-[A-Za-z0-9]{10,}|GOCSPX-[A-Za-z0-9_\-]{10,}|"
    r"refresh_token|Bearer\s+[A-Za-z0-9_\-\.]{10,}|-----BEGIN)", re.IGNORECASE)


def _memory_gb() -> float | None:
    try:
        if sys.platform.startswith("win"):
            import ctypes

            class MEMORYSTATUSEX(ctypes.Structure):
                _fields_ = [("dwLength", ctypes.c_ulong),
                            ("dwMemoryLoad", ctypes.c_ulong),
                            ("ullTotalPhys", ctypes.c_ulonglong),
                            ("ullAvailPhys", ctypes.c_ulonglong),
                            ("ullTotalPageFile", ctypes.c_ulonglong),
                            ("ullAvailPageFile", ctypes.c_ulonglong),
                            ("ullTotalVirtual", ctypes.c_ulonglong),
                            ("ullAvailVirtual", ctypes.c_ulonglong),
                            ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]

            stat = MEMORYSTATUSEX()
            stat.dwLength = ctypes.sizeof(stat)
            ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat))
            return round(stat.ullTotalPhys / (1024 ** 3), 1)
        pages = os.sysconf("SC_PHYS_PAGES")
        page_size = os.sysconf("SC_PAGE_SIZE")
        return round(pages * page_size / (1024 ** 3), 1)
    except Exception:  # noqa: BLE001
        return None


def _recent_log_lines(limit: int = 80) -> list[str]:
    log_dir = _data_dir() / "logs"
    if not log_dir.is_dir():
        return []
    files = sorted(log_dir.glob("*.log"), key=lambda p: p.stat().st_mtime)
    if not files:
        return []
    lines: list[str] = []
    try:
        text = files[-1].read_text(encoding="utf-8", errors="ignore")
        for raw in text.splitlines()[-400:]:
            if _SECRET_PATTERNS.search(raw):
                lines.append("[redacted line]")
            else:
                lines.append(raw[:500])
    except OSError:
        return []
    return lines[-limit:]


def _ai_status() -> dict:
    try:
        from ..gemini.tester import KeyManagerHolder
        status = KeyManagerHolder.instance().status()
        # counts only — never fingerprints or key material
        return {
            "configured_keys": status.get("total", 0),
            "healthy": status.get("healthy", 0),
            "cooldown": status.get("cooldown", 0),
            "invalid": status.get("invalid", 0),
            "disabled": status.get("disabled", 0),
        }
    except Exception:  # noqa: BLE001
        return {"configured_keys": 0, "healthy": 0, "cooldown": 0,
                "invalid": 0, "disabled": 0}


def _error_code_counts() -> dict:
    rows = db.query(
        "SELECT COALESCE(error_code, 'unknown') AS code, COUNT(*) AS n "
        "FROM correction_jobs WHERE status IN ('FAILED','CANCELLED') "
        "GROUP BY code ORDER BY n DESC LIMIT 20")
    return {r["code"]: r["n"] for r in rows}


def _attention_counts() -> dict:
    """Category counts only — never the student names those items carry."""
    counts = {
        "failed_corrections": db.query_one(
            "SELECT COUNT(*) AS n FROM correction_jobs WHERE status='FAILED'")["n"],
        "needs_review": db.query_one(
            "SELECT COUNT(*) AS n FROM results WHERE review_required=1 "
            "AND manual_override=0")["n"],
        "sync_errors": db.query_one(
            "SELECT COUNT(*) AS n FROM classroom_submissions "
            "WHERE sync_error IS NOT NULL")["n"],
    }
    return counts


@router.get("/privacy/diagnostic-export")
def diagnostic_export():
    """Sanitized support file. Contains NO keys, tokens, names or submissions."""
    from ..classroom.auth import auth_state

    state = auth_state()
    info = build_info()
    payload = {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "application": info["app_name"],
        "version": info["version"],
        "build_number": info["build_number"],
        "build_date": info["build_date"],
        "schema_version": info["schema_version"],
        "environment": {
            "os": f"{platform.system()} {platform.release()}",
            "os_version": platform.version(),
            "architecture": platform.machine(),
            "python": platform.python_version(),
            "cpu_count": os.cpu_count(),
            "memory_gb": _memory_gb(),
        },
        "integrations": {
            "ai_keys_configured": _ai_status()["configured_keys"] > 0,
            "google_classroom_oauth_configured": bool(state.client_configured),
            "google_classroom_connected": bool(state.connected),
            "demo_mode": os.environ.get("MC5_DEMO") == "1",
        },
        "ai_provider": _ai_status(),
        "error_codes": _error_code_counts(),
        "attention_counts": _attention_counts(),
        "recent_logs": _recent_log_lines(),
        "redaction_note": ("API keys, OAuth tokens, student names, submissions, "
                           "emails and generated reports are NOT included in "
                           "this file."),
    }
    body = json.dumps(payload, indent=2)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_event("privacy", "diagnostic export generated")
    return Response(
        body, media_type="application/json",
        headers={"Content-Disposition":
                 f'attachment; filename="math_checker_diagnostics_{stamp}.json"'})
