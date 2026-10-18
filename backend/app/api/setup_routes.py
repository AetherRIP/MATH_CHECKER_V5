"""First-run setup, teacher diagnostics, backup/restore and the review queue.

These are the "product" surfaces (§3/§23/§31/§35): they let a teacher who has
never seen the code configure the app, check that it is healthy, back up and
restore their setup, and review uncertain corrections — without editing a
single file.
"""

from __future__ import annotations

import json
import shutil
import time
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import Response

from .. import db
from ..config import data_dir, get_config, get_secrets, save_config
from ..errors import AppError
from ..logging_setup import log_event
from ..version import APP_NAME, APP_VERSION, SCHEMA_VERSION

router = APIRouter(prefix="/api")


def register(app) -> None:
    app.include_router(router)


# ============================================================
# First-run setup (§3/§41)
# ============================================================

@router.get("/setup/status")
def setup_status():
    """Everything the first-run wizard needs to know what is still missing."""
    cfg = get_config()
    secrets = get_secrets()
    key_count = len(secrets.gemini_keys())
    oauth = secrets.oauth_client_config() is not None
    return {
        "app_name": APP_NAME,
        "version": APP_VERSION,
        "first_run": cfg.is_first_run(),
        "teacher": {
            "teacher_name": cfg.teacher.teacher_name,
            "school_name": cfg.teacher.school_name,
            "onboarded": cfg.teacher.onboarded,
            "onboarded_at": cfg.teacher.onboarded_at,
        },
        "checks": {
            "teacher_profile": bool(cfg.teacher.teacher_name.strip()),
            "ai_keys": key_count > 0,
            "ai_key_count": key_count,
            "classroom_oauth_client": oauth,
            "classroom_connected": bool(secrets.oauth_refresh_token()),
            "storage_writable": _storage_writable(),
        },
    }


@router.post("/setup/complete")
def setup_complete(payload: dict):
    """Persist the wizard's answers. Never requires editing source (§3)."""
    cfg = get_config()
    if "teacher_name" in payload:
        cfg.teacher.teacher_name = str(payload.get("teacher_name") or "").strip()[:120]
    if "school_name" in payload:
        cfg.teacher.school_name = str(payload.get("school_name") or "").strip()[:160]
    if isinstance(payload.get("network"), dict):
        mode = str(payload["network"].get("access_mode", cfg.network.access_mode))
        if mode in ("local", "lan"):
            cfg.network.access_mode = mode
    if isinstance(payload.get("retention"), dict):
        mode = str(payload["retention"].get("keep_submissions",
                                           cfg.retention.keep_submissions))
        if mode in ("never", "until_complete", "days", "always"):
            cfg.retention.keep_submissions = mode
    if isinstance(payload.get("grades"), dict):
        from ..config import _parse_boundaries
        parsed = _parse_boundaries(payload["grades"].get("boundaries"))
        if parsed:
            cfg.grades.boundaries = parsed
        if "session_label" in payload["grades"]:
            cfg.grades.session_label = str(payload["grades"].get("session_label") or "")[:120]

    cfg.teacher.onboarded = True
    cfg.teacher.onboarded_at = time.time()
    save_config(cfg)
    log_event("setup", "first-run setup completed",
              teacher=cfg.teacher.teacher_name or "(unnamed)",
              access_mode=cfg.network.access_mode)
    return setup_status()


def _storage_writable() -> bool:
    try:
        probe = data_dir() / ".write_probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        return True
    except OSError:
        return False


# ============================================================
# Teacher-facing self-check (§35)
# ============================================================

@router.get("/diagnostics/selfcheck")
def selfcheck(test_ai: bool = False):
    """A non-technical health summary. Every check has a status and a plain
    sentence explaining what it means and what to do (§24: no silent gaps)."""
    cfg = get_config()
    secrets = get_secrets()
    checks: list[dict] = []

    def add(name: str, status: str, detail: str, fix: str | None = None):
        checks.append({"name": name, "status": status, "detail": detail, "fix": fix})

    # Application + schema
    from ..version import SCHEMA_VERSION as _SCHEMA
    stored_schema = db.get_meta("schema_version")
    add("Application", "pass", f"{APP_NAME} v{APP_VERSION} is running.")
    if stored_schema and int(stored_schema) > _SCHEMA:
        add("Database version", "warn",
            f"The database was created by a newer version (schema {stored_schema}). "
            "This build will not modify it.",
            "Update Math Checker to the latest version.")
    else:
        add("Database version", "pass", f"Data schema {_SCHEMA} matches this build.")

    # Storage
    try:
        usage = shutil.disk_usage(str(data_dir()))
        free_gb = usage.free / (1024 ** 3)
        data_size = _dir_size(data_dir())
        status = "warn" if free_gb < 2 else "pass"
        add("Storage", status,
            f"{free_gb:.1f} GB free; Math Checker data uses {_human(data_size)}.",
            "Free up disk space." if status == "warn" else None)
    except OSError as exc:
        add("Storage", "fail", f"Could not read the data directory ({exc}).",
            "Check the storage location and permissions.")
    if _storage_writable():
        add("Storage writable", "pass", "The data folder can be written to.")
    else:
        add("Storage writable", "fail", "The data folder is not writable.",
            "Check folder permissions or choose another storage location.")

    # AI keys
    key_count = len(secrets.gemini_keys())
    if key_count == 0:
        add("AI keys", "fail", "No Gemini API keys are configured.",
            "Add at least one key on the API Keys page.")
    else:
        add("AI keys", "pass", f"{key_count} key(s) configured.")

    # Optional live AI probe (tiny, student-data-free)
    if test_ai and key_count:
        from ..gemini.tester import test_key
        from ..gemini.key_manager import fingerprint as fp_of
        keys = secrets.gemini_keys()
        state = test_key(1, keys[0], fp_of(keys[0]))
        if state.status == "HEALTHY":
            add("AI model connection", "pass",
                f"Reached Gemini in {state.response_time_ms} ms.")
        elif state.status == "COOLDOWN":
            add("AI model connection", "warn",
                "Gemini is rate-limited right now; corrections will wait and retry.",
                "Try again shortly.")
        else:
            add("AI model connection", "fail",
                state.error_message or "The model could not be reached.",
                "Check the API key on the API Keys page.")

    # Classroom
    from ..classroom.auth import auth_state
    state = auth_state()
    if not state.client_configured:
        add("Google Classroom", "warn", "Google sign-in is not set up yet.",
            "Add the Google OAuth client file, then connect from the Classroom page.")
    elif state.connected:
        add("Google Classroom", "pass",
            f"Connected as {state.email or 'your Google account'}.")
    else:
        add("Google Classroom", "warn", "Google Classroom is not connected.",
            "Click Connect on the Classroom page.")

    # Network access mode
    mode = cfg.network.access_mode
    add("Network access", "pass" if mode == "local" else "warn",
        ("Local only (127.0.0.1) — the safest setting." if mode == "local"
         else "LAN mode — other devices on your network can reach the app."),
        None if mode == "local" else "Only enable this on a trusted network.")

    overall = "fail" if any(c["status"] == "fail" for c in checks) else (
        "warn" if any(c["status"] == "warn" for c in checks) else "pass")
    return {"overall": overall, "checks": checks,
            "version": APP_VERSION, "schema_version": SCHEMA_VERSION}


def _dir_size(path: Path) -> int:
    total = 0
    try:
        for item in path.rglob("*"):
            if item.is_file():
                try:
                    total += item.stat().st_size
                except OSError:
                    continue
    except OSError:
        return total
    return total


def _human(num: int) -> str:
    value = float(num)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024:
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} TB"


# ============================================================
# Backup / restore / config import-export (§31/§32)
# ============================================================

@router.get("/backup/config")
def export_config(include_secrets: bool = False):
    """Export the non-secret configuration as JSON.

    Secrets (API keys, OAuth refresh token) are EXCLUDED by default. A
    teacher may request them explicitly, and the response always warns that
    the file then contains credentials (§31).
    """
    cfg = get_config()
    payload = {
        "exported_at": time.time(),
        "version": APP_VERSION,
        "schema_version": SCHEMA_VERSION,
        "config": json.loads(json.dumps(cfg, default=lambda o: o.__dict__)),
        "contains_secrets": bool(include_secrets),
    }
    if include_secrets:
        payload["secrets_warning"] = (
            "This file contains API keys and/or a Google refresh token. "
            "Store it securely and delete it when no longer needed.")
        payload["secrets"] = {
            "gemini_api_keys": get_secrets().gemini_keys(),
            "classroom_refresh_token": get_secrets().oauth_refresh_token(),
        }
    filename = "math_checker_config.json"
    return Response(
        json.dumps(payload, indent=2, default=str),
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.post("/backup/config")
def import_config(payload: dict):
    """Restore configuration from an exported file.

    Deliberately conservative: only known sections are applied, secrets are
    imported ONLY when present in the file, and the teacher is told exactly
    what was restored. A malformed file is rejected rather than half-applied.
    """
    if not isinstance(payload, dict) or "config" not in payload:
        raise AppError("This does not look like a Math Checker backup file.",
                       status=400, code="bad_backup")
    incoming = payload.get("config")
    if not isinstance(incoming, dict):
        raise AppError("The backup file's configuration section is invalid.",
                       status=400, code="bad_backup")
    cfg = get_config()

    teacher = incoming.get("teacher") or {}
    if isinstance(teacher, dict):
        cfg.teacher.teacher_name = str(teacher.get("teacher_name", cfg.teacher.teacher_name))[:120]
        cfg.teacher.school_name = str(teacher.get("school_name", cfg.teacher.school_name))[:160]
        cfg.teacher.onboarded = bool(teacher.get("onboarded", cfg.teacher.onboarded))

    grades = incoming.get("grades") or {}
    if isinstance(grades, dict):
        from ..config import _parse_boundaries
        parsed = _parse_boundaries(grades.get("boundaries"))
        if parsed:
            cfg.grades.boundaries = parsed
        cfg.grades.session_label = str(grades.get("session_label", cfg.grades.session_label))[:120]

    retention = incoming.get("retention") or {}
    if isinstance(retention, dict):
        mode = str(retention.get("keep_submissions", cfg.retention.keep_submissions))
        if mode in ("never", "until_complete", "days", "always"):
            cfg.retention.keep_submissions = mode

    usage = incoming.get("usage") or {}
    if isinstance(usage, dict):
        try:
            cfg.usage.hourly_mb = max(0, int(usage.get("hourly_mb", cfg.usage.hourly_mb)))
            cfg.usage.daily_mb = max(0, int(usage.get("daily_mb", cfg.usage.daily_mb)))
        except (TypeError, ValueError):
            pass

    save_config(cfg)

    secrets_restored = 0
    secrets_block = payload.get("secrets")
    if isinstance(secrets_block, dict):
        keys = secrets_block.get("gemini_api_keys")
        if isinstance(keys, list) and keys:
            get_secrets().set_gemini_keys([str(k).strip() for k in keys if str(k).strip()])
            secrets_restored += len(keys)
        token = secrets_block.get("classroom_refresh_token")
        if token:
            get_secrets().set_oauth_refresh_token(str(token))
            secrets_restored += 1

    log_event("setup", "configuration imported",
              secrets_restored=secrets_restored)
    return {"restored": True, "secrets_restored": secrets_restored,
            "note": ("Configuration restored. Restart the application for "
                     "network/worker changes to take full effect.")}


@router.get("/backup/database")
def backup_database():
    """Download a consistent copy of the SQLite database.

    Uses SQLite's own backup API so the copy is safe even while the app is
    running (no half-written file, no WAL race)."""
    import sqlite3
    import tempfile

    src = db.get_connection()
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp:
        tmp_path = tmp.name
    try:
        dest = sqlite3.connect(tmp_path)
        with dest:
            src.backup(dest)
        dest.close()
        data = Path(tmp_path).read_bytes()
    finally:
        try:
            Path(tmp_path).unlink()
        except OSError:
            pass
    stamp = time.strftime("%Y%m%d-%H%M%S")
    return Response(
        data,
        media_type="application/octet-stream",
        headers={"Content-Disposition":
                 f'attachment; filename="math_checker_backup_{stamp}.db"'},
    )


# ============================================================
# Human review queue (§23)
# ============================================================

@router.get("/review")
def review_queue(limit: int = 100):
    """Results a human should look at: low confidence, validation failures,
    large score changes, or anything the guard kept for review."""
    rows = db.query(
        """
        SELECT r.id, r.student_name, r.student_id, r.coursework_id,
               r.submission_id, r.earned, r.available, r.percentage,
               r.confidence, r.confidence_reasons, r.review_required,
               r.manual_override, r.created_at, w.title AS assignment_title,
               EXISTS(SELECT 1 FROM automation_jobs a WHERE a.result_id = r.id
                      AND a.status = 'ATTENTION_REQUIRED'
                      AND a.error_code = 'review_mode_await_teacher') AS automation_hold
        FROM results r
        LEFT JOIN coursework w ON w.id = r.coursework_id
        WHERE ((r.review_required = 1
               OR r.confidence IN ('LOW', 'VALIDATION_FAILED', 'LARGE_SCORE_CHANGE'))
               AND r.manual_override = 0)
           OR EXISTS(SELECT 1 FROM automation_jobs a WHERE a.result_id = r.id
                     AND a.status = 'ATTENTION_REQUIRED'
                     AND a.error_code = 'review_mode_await_teacher')
        ORDER BY r.created_at DESC
        LIMIT ?
        """,
        (max(1, min(500, limit)),),
    )
    items = []
    for row in rows:
        item = dict(row)
        item["confidence_reasons"] = db.loads(row["confidence_reasons"], [])
        items.append(item)
    return {"items": items, "total": len(items)}


@router.post("/results/{result_id}/override")
def manual_override(result_id: str, payload: dict):
    """Teacher override of a result (§23).

    Marks the result as manually overridden so no later automated run can
    silently replace it, and (optionally) records the teacher's own mark.
    The correction engine is NOT re-run — this is a human decision layered on
    top of the validated data.
    """
    row = db.query_one("SELECT * FROM results WHERE id = ?", (result_id,))
    if not row:
        raise HTTPException(404, "Result not found")
    earned = payload.get("earned")
    note = str(payload.get("note") or "")[:500]
    previous_earned = row["earned"]
    previous_confidence = row["confidence"]
    # An approval (no new mark supplied) is a decision too: it must clear the
    # confidence flags that put the result in the queue, otherwise the review
    # queue and every confidence-based view keep showing it as unreviewed.
    approved_note = note or "Accepted by teacher"
    with db.transaction() as conn:
        if earned is not None:
            try:
                earned_int = int(earned)
            except (TypeError, ValueError):
                raise AppError("The override mark must be a whole number.",
                               status=400, code="bad_override")
            if earned_int < 0 or earned_int > (row["available"] or 0):
                raise AppError(
                    f"The override mark must be between 0 and {row['available']}.",
                    status=400, code="bad_override")
            pct = (earned_int / row["available"] * 100.0) if row["available"] else 0.0
            conn.execute(
                "UPDATE results SET earned = ?, percentage = ?, manual_override = 1, "
                "review_required = 0, confidence = 'HIGH', confidence_reasons = ? "
                "WHERE id = ?",
                (earned_int, pct, json.dumps(
                    [f"Manually overridden by the teacher. {note}".strip()]), result_id),
            )
            action = "overridden"
            new_earned = earned_int
        else:
            conn.execute(
                "UPDATE results SET manual_override = 1, review_required = 0, "
                "confidence = 'HIGH', confidence_reasons = ? WHERE id = ?",
                (json.dumps([f"Approved by the teacher. {approved_note}".strip()]),
                 result_id),
            )
            action = "approved"
            new_earned = previous_earned
        conn.execute(
            "INSERT INTO result_reviews(result_id, action, previous_earned, "
            "new_earned, previous_confidence, note, created_at) "
            "VALUES(?,?,?,?,?,?,?)",
            (result_id, action, previous_earned, new_earned,
             previous_confidence, note or approved_note, time.time()),
        )
    log_event("api", "result reviewed by teacher", result_id=result_id,
              action=action, earned=earned)
    return {"overridden": True, "result_id": result_id, "action": action}


@router.post("/results/{result_id}/questions/{question_number}/mark")
def override_question_mark(result_id: str, question_number: str, payload: dict):
    """Teacher override of a SINGLE question mark (Part N/O).

    The engine's original AI mark is preserved in the version history; the
    current result now reflects the teacher's decision, totals are recomputed
    from the stored per-question marks, and a new version is appended.
    """
    from ..engine import legacy_marking as legacy
    from ..engine import versions
    row = db.query_one("SELECT * FROM results WHERE id = ?", (result_id,))
    if not row:
        raise HTTPException(404, "Result not found")
    try:
        awarded = int(payload.get("awarded"))
    except (TypeError, ValueError):
        raise AppError("Provide a whole-number mark for this question.",
                       status=400, code="bad_question_mark")
    result = db.loads(row["result_json"], {}) or {}
    questions = result.get("questions") or []
    target = next((q for q in questions
                   if str(q.get("question_number")) == str(question_number)), None)
    if target is None:
        raise AppError("That question is not part of this result.",
                       status=404, code="question_not_found")
    max_marks = int(target.get("max_marks") or 0)
    if awarded < 0 or awarded > max_marks:
        raise AppError(f"The mark must be between 0 and {max_marks}.",
                       status=400, code="bad_question_mark")
    note = str(payload.get("note") or "")[:300]
    previous = target.get("awarded_marks")

    target["awarded_marks"] = awarded
    target["teacher_mark"] = awarded
    target["status"] = "VALID"
    target.pop("review_reason", None)

    earned, available, percentage = legacy.calculate_totals(result)
    provisional = legacy.percentage_band_grade(percentage)
    result["totals"] = {
        "earned": earned, "available": available,
        "percentage": percentage, "provisional_grade": provisional,
    }
    verification = db.loads(row["verification_json"], {}) or {}
    reviewed = [q for q in (verification.get("review_questions") or [])
                if str(q) != str(question_number)]
    verification["review_questions"] = reviewed
    if not verification.get("anomalies") and not reviewed:
        verification["status"] = "VALID"

    reason = (f"Question {question_number} marked {awarded}/{max_marks} by the "
              f"teacher (AI mark {previous}). {note}").strip()
    now_ts = time.time()
    with db.transaction() as conn:
        conn.execute(
            "UPDATE results SET earned = ?, available = ?, percentage = ?, "
            "provisional_grade = ?, result_json = ?, verification_json = ?, "
            "review_required = 0, manual_override = 1, confidence = 'HIGH', "
            "confidence_reasons = ? WHERE id = ?",
            (earned, available, percentage, provisional,
             json.dumps(result, ensure_ascii=False),
             json.dumps(verification, ensure_ascii=False),
             json.dumps([reason]), result_id),
        )
        versions.record(
            conn, result_id=result_id, earned=earned, available=available,
            percentage=percentage, provisional_grade=provisional,
            marking_model=row["marking_model"], key_fingerprint=row["key_fingerprint"],
            confidence="HIGH", confidence_reasons=[reason],
            verification=verification, result=result, created_at=now_ts,
            note=f"Question {question_number} re-marked by teacher",
        )
    log_event("api", "question mark overridden", result_id=result_id,
              question=str(question_number), awarded=awarded, previous=previous)
    return {"updated": True, "result_id": result_id,
            "question_number": str(question_number), "awarded": awarded,
            "previous": previous, "earned": earned, "available": available,
            "percentage": round(percentage, 2)}


@router.get("/results/{result_id}/reviews")
def result_review_history(result_id: str):
    """Append-only teacher review history for one result (§23/§D)."""
    rows = db.query(
        "SELECT id, action, previous_earned, new_earned, previous_confidence, "
        "note, created_at FROM result_reviews WHERE result_id = ? "
        "ORDER BY created_at DESC", (result_id,))
    return {"result_id": result_id, "history": [dict(r) for r in rows]}


@router.post("/jobs/{job_id}/reprocess")
def force_reprocess(job_id: str):
    """Force a re-correction even if a valid result already exists (§22).

    This is the ONLY path that lets a suspicious run replace a valid stored
    result — the teacher asked for it explicitly."""
    job = db.query_one("SELECT * FROM correction_jobs WHERE id = ?", (job_id,))
    if not job:
        raise HTTPException(404, "Job not found")
    from ..jobs.manager import JobManagerHolder
    from ..workers.pool import WorkerPoolHolder
    pool = WorkerPoolHolder.instance()
    pool._force_reprocess.add(job_id)
    retried = JobManagerHolder.instance().retry(job_id)
    if not retried:
        pool._force_reprocess.discard(job_id)
        raise AppError("This job cannot be reprocessed (it is still active).",
                       status=409, code="job_active")
    pool._wake.set()
    log_event("api", "forced reprocess requested", job_id=job_id)
    return {"reprocessing": True, "job_id": job_id}
