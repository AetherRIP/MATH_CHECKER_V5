"""Product-level routes: assignment overview, creation, attention, publishing.

These sit on top of the preserved correction engine and the Classroom
tracking/submission layers; they add no new marking path. Every Classroom
interaction routes through :mod:`app.classroom.managed`, which records the
real Google response — nothing here reports success without confirmation.
"""

from __future__ import annotations

import logging
import time
import uuid
from pathlib import Path

from fastapi import APIRouter, Request

from .. import db
from .. import student_identity
from .. import testing_guard
from ..api.routes_all import _require_classroom, _classroom_error
from ..config import data_dir
from ..classroom import client as gc
from ..classroom import managed as managed_mod
from ..classroom.auth import auth_state, account_email
from ..errors import AppError
from ..logging_setup import log_event
from ..submission_status import sql_outstanding
from ..text import count as _count, verb as _verb

router = APIRouter(prefix="/api")

LOCAL_COURSE_ID = "local"
LOCAL_COURSE_NAME = "Local assignments"


def register(app) -> None:
    app.include_router(router)


# ================================================================ classes

@router.get("/classes")
def list_classes():
    """Classes with real aggregate counts (unique students, submissions,
    corrections) — derived from stored rows, never estimated."""
    rows = db.query(
        "SELECT id, name, section FROM courses ORDER BY name"
    )
    out: list[dict] = []
    for c in rows:
        cid = c["id"]
        students = db.query_one(
            "SELECT COUNT(DISTINCT student_id) AS n FROM classroom_submissions "
            "WHERE course_id = ?", (cid,))
        cw = db.query_one("SELECT COUNT(*) AS n FROM coursework WHERE course_id = ?", (cid,))
        sub = db.query_one(
            f"""
            SELECT COUNT(*) AS total,
                   SUM(CASE WHEN current_state IN ('TURNED_IN',
                            'STUDENT_EDITED_AFTER_TURN_IN') THEN 1 ELSE 0 END) AS submitted,
                   SUM({sql_outstanding('cs')}) AS missing,
                   MAX(last_synced_at) AS last_sync
            FROM classroom_submissions cs WHERE course_id = ?
            """, (cid,))
        res = db.query_one(
            """
            SELECT COUNT(*) AS checked, AVG(percentage) AS average,
                   SUM(CASE WHEN review_required = 1 AND manual_override = 0
                            THEN 1 ELSE 0 END) AS review
            FROM results WHERE coursework_id IN (
                SELECT id FROM coursework WHERE course_id = ?)
            """, (cid,))
        unmanaged = db.query_one(
            "SELECT COUNT(*) AS n FROM coursework WHERE course_id = ? AND id NOT IN "
            "(SELECT coursework_id FROM classroom_managed_assignments)", (cid,))
        out.append({
            "id": cid,
            "name": c["name"],
            "section": c["section"],
            "local": cid == LOCAL_COURSE_ID,
            "students": (students["n"] if students else 0) or 0,
            "assignments": (cw["n"] if cw else 0) or 0,
            "submissions": (sub["total"] if sub else 0) or 0,
            "submitted": (sub["submitted"] if sub else 0) or 0,
            "missing": (sub["missing"] if sub else 0) or 0,
            "checked": (res["checked"] if res else 0) or 0,
            "review": (res["review"] if res else 0) or 0,
            "average": round(res["average"], 1) if res and res["average"] is not None else None,
            "external_assignments": (unmanaged["n"] if unmanaged else 0) or 0,
            "last_sync": float(sub["last_sync"]) if sub and sub["last_sync"] else None,
        })
    return {"classes": out, "total": len(out)}


# ============================================================ assignments

_SUBMITTED_STATES = ("TURNED_IN", "STUDENT_EDITED_AFTER_TURN_IN")


def _assignment_rows(course_id: str | None = None,
                     coursework_id: str | None = None) -> list[dict]:
    where, params = [], []
    if course_id:
        where.append("w.course_id = ?")
        params.append(course_id)
    if coursework_id:
        where.append("w.id = ?")
        params.append(coursework_id)
    clause = ("WHERE " + " AND ".join(where)) if where else ""
    rows = db.query(
        f"""
        SELECT w.id, w.course_id, w.title, w.max_points, w.state, w.due_date,
               w.allowlisted, w.description, w.alternate_link,
               w.sync_state, w.custom_title, w.classroom_title,
               c.name AS course_name, c.section AS course_section
        FROM coursework w
        LEFT JOIN courses c ON c.id = w.course_id
        {clause}
        ORDER BY COALESCE(w.update_time, '') DESC, w.title
        """,
        params,
    )
    managed = managed_mod.managed_map()
    out: list[dict] = []
    for row in rows:
        r = dict(row)
        wid = r["id"]
        sub = db.query_one(
            f"""
            SELECT COUNT(*) AS total,
                   SUM(CASE WHEN current_state IN ('TURNED_IN','STUDENT_EDITED_AFTER_TURN_IN')
                            THEN 1 ELSE 0 END) AS submitted,
                   SUM(CASE WHEN current_state = 'CREATED' THEN 1 ELSE 0 END) AS draft,
                   SUM(CASE WHEN current_state = 'RECLAIMED_BY_STUDENT' THEN 1 ELSE 0 END) AS reclaimed,
                   SUM({sql_outstanding('cs')}) AS outstanding
            FROM classroom_submissions cs WHERE coursework_id = ?
            """,
            (wid,),
        )
        res = db.query_one(
            """
            SELECT COUNT(*) AS checked,
                   SUM(CASE WHEN review_required = 1 AND manual_override = 0
                            THEN 1 ELSE 0 END) AS review,
                   AVG(percentage) AS average
            FROM results WHERE coursework_id = ? AND submission_id IS NOT NULL
            """,
            (wid,),
        )
        failed = db.query_one(
            "SELECT COUNT(*) AS n FROM correction_jobs WHERE coursework_id = ? "
            "AND status = 'FAILED'", (wid,),
        )
        processing = db.query_one(
            "SELECT COUNT(*) AS n FROM correction_jobs WHERE coursework_id = ? "
            "AND status IN ('QUEUED','STARTING','PREPARING','DOWNLOADING',"
            "'UPLOADING','MARKING','VERIFYING','SAVING','WAITING_FOR_API','RETRYING')",
            (wid,),
        )
        total = (sub["total"] if sub else 0) or 0
        submitted = (sub["submitted"] if sub else 0) or 0
        outstanding = (sub["outstanding"] if sub else 0) or 0
        info = managed_mod.managed_info(wid)
        mark_scheme = _has_mark_scheme(wid)
        synced = db.get_meta(f"classroom_tracking_sync:{wid}")
        out.append({
            **{k: r[k] for k in (
                "id", "course_id", "title", "max_points", "state", "due_date",
                "allowlisted", "description", "alternate_link",
                "course_name", "course_section", "custom_title",
                "classroom_title")},
            "sync_state": r.get("sync_state") or "ACTIVE",
            "deleted_in_classroom": (r.get("sync_state") == "DELETED_IN_CLASSROOM"),
            "local": r["course_id"] == LOCAL_COURSE_ID if r["course_id"] else False,
            "managed": info["managed"],
            "management": info,
            "students_total": total,
            "submitted": submitted,
            "draft": (sub["draft"] if sub else 0) or 0,
            # One source of truth for "still outstanding" (§31): missing,
            # draft and a recorded grade of 0 all count (§48).
            "missing": outstanding,
            "reclaimed": (sub["reclaimed"] if sub else 0) or 0,
            "checked": (res["checked"] if res else 0) or 0,
            "needs_review": (res["review"] if res else 0) or 0,
            "failed": (failed["n"] if failed else 0) or 0,
            "processing": (processing["n"] if processing else 0) or 0,
            "average": round(res["average"], 1) if res and res["average"] is not None else None,
            "has_mark_scheme": mark_scheme,
            "last_synced_at": float(synced) if synced else None,
        })
    return out


def _has_mark_scheme(coursework_id: str) -> bool:
    from pathlib import Path
    path = data_dir() / "mark_schemes" / f"{coursework_id}.pdf"
    if path.exists():
        return True
    row = db.query_one(
        "SELECT path FROM pending_mark_schemes WHERE coursework_id = ?",
        (coursework_id,))
    return bool(row and Path(row["path"]).exists())


@router.get("/assignments")
def list_assignments(course_id: str | None = None, managed: str | None = None,
                     q: str | None = None):
    """Every assignment (Classroom + local) with the counts the UI needs.

    ``managed`` filters by kind: 'managed' | 'external' | 'local'.
    """
    rows = _assignment_rows(course_id=course_id)
    if managed == "managed":
        rows = [r for r in rows if r["managed"]]
    elif managed == "external":
        rows = [r for r in rows if not r["managed"] and not r["local"]]
    elif managed == "local":
        rows = [r for r in rows if r["local"]]
    if q:
        needle = q.strip().lower()
        rows = [r for r in rows if needle in (r["title"] or "").lower()
                or needle in (r["course_name"] or "").lower()]
    return {"assignments": rows, "total": len(rows)}


@router.get("/assignments/{coursework_id}")
def assignment_detail(coursework_id: str):
    rows = _assignment_rows(coursework_id=coursework_id)
    if not rows:
        raise AppError("That assignment is not in the local database. Sync "
                       "Google Classroom first.", status=404, code="not_found")
    item = rows[0]
    tracking = _tracking_overview(coursework_id)
    return {**item, "tracking": tracking,
            "capabilities": _assignment_capabilities(item)}


def _assignment_capabilities(item: dict) -> dict:
    info = item.get("management") or {}
    managed = bool(item.get("managed"))
    return managed_mod.capabilities_for(managed) | {
        "kind": "managed" if managed else ("local" if item.get("local") else "external"),
    }


def _tracking_overview(coursework_id: str) -> dict:
    from ..classroom import submissions as cs
    data = cs.tracking_rows(coursework_id=coursework_id)
    return data["overview"]


# ===================================================== create local + managed

@router.post("/assignments/local")
def create_local_assignment(payload: dict):
    """Create an assignment that lives only in Math Checker (no Classroom).

    Stored under a reserved local course so it appears alongside synced
    assignments; it is explicitly NOT managed and never pretends to be.
    """
    title = str(payload.get("title") or "").strip()
    if not title:
        raise AppError("An assignment title is required.", status=400,
                       code="missing_title")
    now = time.time()
    db.execute(
        "INSERT INTO courses(id, name, section, state, fetched_at) VALUES(?,?,?,?,?) "
        "ON CONFLICT(id) DO UPDATE SET name=excluded.name, fetched_at=excluded.fetched_at",
        (LOCAL_COURSE_ID, LOCAL_COURSE_NAME, "Local", "LOCAL", now),
    )
    coursework_id = f"local-{uuid.uuid4().hex[:12]}"
    max_points = payload.get("max_points")
    try:
        max_points = float(max_points) if max_points not in (None, "") else None
    except (TypeError, ValueError):
        max_points = None
    db.execute(
        """
        INSERT INTO coursework(id, course_id, title, max_points, state, due_date,
                               allowlisted, alternate_link, description, update_time,
                               fetched_at, classroom_title, sync_state)
        VALUES(?,?,?,?, 'LOCAL', ?, 1, NULL, ?, ?, ?, ?, 'ACTIVE')
        """,
        (coursework_id, LOCAL_COURSE_ID, title, max_points,
         payload.get("due_date") or None, payload.get("description") or None,
         time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)), now, title),
    )
    log_event("api", "local assignment created", coursework_id=coursework_id)
    return {"id": coursework_id, "course_id": LOCAL_COURSE_ID, "managed": False,
            "local": True}


@router.post("/classroom/courses/{course_id}/assignments/create")
def create_managed_assignment(course_id: str, payload: dict):
    """Create a CourseWork THROUGH Google Classroom and record it as managed.

    Any Google rejection is surfaced verbatim — this endpoint never reports a
    created assignment that Google did not confirm.
    """
    _require_classroom()
    title = str(payload.get("title") or "").strip()
    if not title:
        raise AppError("A Classroom title is required.", status=400, code="missing_title")
    materials = payload.get("materials") if isinstance(payload.get("materials"), list) else None
    try:
        created = managed_mod.create_coursework(
            course_id,
            title=title,
            description=payload.get("description"),
            max_points=payload.get("max_points"),
            due_date=payload.get("due_date"),
            due_time=payload.get("due_time"),
            state=payload.get("state") or "PUBLISHED",
            materials=materials,
            account_email=account_email(),
        )
    except AppError:
        raise
    except testing_guard.PublicationBlocked:
        # A deliberate LOCAL gate, not a Google failure: it is answered by the
        # app's own 403 handler (main.publication_blocked_handler) with the
        # guard's wording and its own code, so it must not be re-wrapped here.
        raise
    except gc.ClassroomApiError as exc:
        raise _classroom_error(exc)
    except ValueError as exc:
        raise AppError(str(exc), status=400, code="bad_request")
    except Exception as exc:  # noqa: BLE001 - reported, never hidden
        # Assignment creation used to translate ONLY ClassroomApiError and
        # ValueError. Every other failure - Google unreachable (a DNS failure, a
        # timeout, a reset connection), a dead or revoked OAuth grant, an
        # unexpected internal error - escaped to the global handler, whose whole
        # answer is "Something went wrong processing this request. The error has
        # been logged for the developer diagnostics page." That sentence was the
        # ONLY thing a teacher saw when assigning homework failed, and it named
        # neither the cause nor the fix. _classroom_error keeps the gates
        # distinct and actionable (403 testing-only, 401 "Reconnect Google
        # Classroom...", 502 with the real reason); the cause is still logged
        # for the developer diagnostics page.
        log_event("classroom", "create Classroom assignment failed",
                  level=logging.ERROR, error=str(exc)[:500],
                  exc_type=type(exc).__name__, course_id=course_id)
        raise _classroom_error(exc) from exc
    return {"created": True, "coursework": created,
            "management": managed_mod.managed_info(created.get("id"))}


@router.post("/classroom/assignments/{coursework_id}/import")
def import_external_assignment(coursework_id: str, course_id: str | None = None):
    """Import a Classroom assignment that this app did NOT create.

    Records it as EXTERNAL (never managed) and returns the honest list of what
    is and is not permitted for it.
    """
    row = db.query_one("SELECT * FROM coursework WHERE id = ?", (coursework_id,))
    if not row:
        raise AppError("Sync Google Classroom first so this assignment exists "
                       "locally.", status=404, code="not_found")
    cid = course_id or row["course_id"]
    info = managed_mod.managed_info(coursework_id)
    log_event("classroom.managed", "external assignment imported",
              coursework_id=coursework_id, course_id=cid)
    return {
        "coursework_id": coursework_id,
        "course_id": cid,
        "title": row["title"],
        "management": info,
        "capabilities": managed_mod.capabilities_for(info["managed"]),
        "imported": True,
    }


# ==================================================== classroom total points

@router.post("/classroom/assignments/{coursework_id}/classroom-total")
def update_classroom_total(coursework_id: str, payload: dict):
    """Change the assignment's REAL Google Classroom total points.

    Body: ``{"max_points": 20, "course_id": "..."}``. This performs a genuine
    Classroom API update and only reports success once Google confirms it; the
    local assignment is then refreshed. Math Checker's own maximum marks are a
    SEPARATE value and are never altered here.
    """
    _require_classroom()
    row = db.query_one("SELECT course_id, max_points FROM coursework WHERE id = ?",
                       (coursework_id,))
    if not row:
        raise AppError("That assignment is not in the local database. Sync "
                       "Google Classroom first.", status=404, code="not_found")
    course_id = payload.get("course_id") or row["course_id"]
    max_points = payload.get("max_points")
    if max_points in (None, ""):
        raise AppError("Provide the new Classroom total points.", status=400,
                       code="missing_max_points")
    try:
        result = managed_mod.update_coursework_max_points(
            coursework_id, course_id, max_points)
    except AppError:
        raise
    except testing_guard.PublicationBlocked:
        raise  # the testing-only gate's own 403 handler answers this one
    except gc.ClassroomApiError as exc:
        raise _classroom_error(exc)
    except ValueError as exc:
        raise AppError(str(exc), status=400, code="bad_max_points")
    except Exception as exc:  # noqa: BLE001 - reported, never hidden
        # Same hole as assignment creation: a Classroom write that failed for
        # any reason other than a Google error response reached the teacher as
        # the opaque "Something went wrong processing this request" 500.
        log_event("classroom", "update Classroom total failed",
                  level=logging.ERROR, error=str(exc)[:500],
                  exc_type=type(exc).__name__, coursework_id=coursework_id)
        raise _classroom_error(exc) from exc
    # Return the refreshed detail so the UI can render the confirmed value.
    return {**result, "assignment": _assignment_rows(coursework_id=coursework_id)[0]}


# ================================================================ publishing

@router.get("/classroom/assignments/{coursework_id}/publish/queue")
def publish_queue(coursework_id: str, course_id: str | None = None):
    """Real, side-effect-free description of what could be published."""
    row = db.query_one("SELECT course_id FROM coursework WHERE id = ?", (coursework_id,))
    cid = course_id or (row["course_id"] if row else None)
    if not cid:
        raise AppError("Unknown assignment — sync Classroom first.",
                       status=404, code="not_found")
    queue = managed_mod.build_publish_queue(coursework_id, cid)
    queue["recent"] = managed_mod.recent_publish_ops(coursework_id, limit=50)
    return queue


@router.post("/classroom/assignments/{coursework_id}/publish")
def publish_operations(coursework_id: str, payload: dict):
    """Publish a set of operations, recording Google's REAL response for each.

    Body: ``{"course_id": "...", "operations": [{"type": "draft_grade",
    "submission_id": "...", "grade": 12.5}, ...], "attempt_unmanaged": false}``
    """
    _require_classroom()
    row = db.query_one("SELECT course_id FROM coursework WHERE id = ?", (coursework_id,))
    course_id = payload.get("course_id") or (row["course_id"] if row else None)
    if not course_id:
        raise AppError("Unknown assignment — sync Classroom first.",
                       status=404, code="not_found")
    operations = payload.get("operations")
    if not isinstance(operations, list) or not operations:
        raise AppError("Provide at least one operation to publish.",
                       status=400, code="empty_operations")
    if len(operations) > 500:
        raise AppError("Too many operations in one request (max 500).",
                       status=400, code="too_many_operations")

    # Safety gate: never publish a grade for a result the teacher has not
    # accepted. Required post-confirmation is enforced here, not in the UI.
    blocked = _review_blocked(coursework_id, operations)
    allowed = [op for op in operations if not _is_blocked(op, blocked)]
    result = managed_mod.publish(
        coursework_id, course_id, allowed,
        attempt_unmanaged=bool(payload.get("attempt_unmanaged")))
    if blocked:
        # Surface blocked operations as explicit requires_review results so a
        # partial publish is never mistaken for a complete one.
        for b in blocked:
            result["results"].append({
                "id": None, "status": managed_mod.STATUS_REQUIRES_REVIEW,
                "http_status": None, "error_code": "review_required",
                "message": ("Skipped: this result still requires teacher review. "
                            "Open the Review queue and accept or override it first."),
                "submission_id": b["submission_id"],
            })
        result["blocked"] = blocked
        result["summary"]["requires_review"] = len(blocked)
        result["summary"]["total"] += len(blocked)
    return result


def _review_blocked(coursework_id: str, operations: list[dict]) -> list[dict]:
    """Submission ids whose latest result still requires review (and has no
    teacher override). These must not be published as-is."""
    blocked: list[dict] = []
    for op in operations:
        if op.get("type") not in ("draft_grade", "final_grade", "assign_grade"):
            continue
        sid = op.get("submission_id")
        if not sid:
            continue
        r = db.query_one(
            "SELECT review_required, manual_override FROM results "
            "WHERE coursework_id = ? AND submission_id = ? "
            "ORDER BY created_at DESC LIMIT 1", (coursework_id, sid))
        if r and int(r["review_required"] or 0) and not int(r["manual_override"] or 0):
            blocked.append({"submission_id": sid,
                            "reason": "result requires teacher review"})
    return blocked


def _is_blocked(op: dict, blocked: list[dict]) -> bool:
    return any(b["submission_id"] == op.get("submission_id") for b in blocked)


# ================================================================ attention

@router.get("/attention")
def attention():
    """Aggregate what genuinely needs the teacher's attention, from real state.

    Nothing is inferred: every item is derived from stored Classroom/job/result
    rows. Empty categories are simply absent.
    """
    items: list[dict] = []
    cfg_keys = _key_health()

    # -- failed corrections ------------------------------------------------
    failed = db.query(
        "SELECT id, student_id, student_name, coursework_id, error FROM correction_jobs "
        "WHERE status = 'FAILED' ORDER BY updated_at DESC LIMIT 20")
    for j in failed:
        items.append({
            "type": "correction_failed", "tone": "err", "icon": "alert",
            "title": f"Correction failed — {student_identity.display_name(None, j['student_name'], fallback='a student')}",
            "detail": (j["error"] or "The AI correction did not complete.")[:160],
            "route": "#/corrections", "count": 1,
        })

    # -- results needing review -------------------------------------------
    review = db.query(
        "SELECT r.id, r.student_id, r.student_name, r.confidence, r.confidence_reasons, "
        "       r.coursework_id, r.submission_id FROM results r "
        "WHERE r.review_required = 1 AND r.manual_override = 0 "
        "ORDER BY r.created_at DESC LIMIT 25")
    for r in review:
        reasons = db.loads(r["confidence_reasons"], []) or []
        detail = reasons[0] if reasons else f"Confidence {r['confidence'] or 'low'}"
        # A large-PDF run can carry only a Classroom student id, and a 21-digit
        # number is not something a teacher can act on. Say "a submission"
        # instead, and keep the id out of the visible title.
        who = student_identity.display_name(r["student_id"], r["student_name"])
        if who.isdigit() or who == student_identity.FALLBACK_DISPLAY_NAME:
            who = "A submission"
        items.append({
            "type": "needs_review", "tone": "warn", "icon": "review",
            "title": f"{who} needs review",
            "detail": str(detail)[:160], "route": "#/review", "count": 1,
        })

    # -- unreadable / invalid submissions ---------------------------------
    invalid = db.query(
        "SELECT cs.student_name, cs.coursework_id FROM classroom_submissions cs "
        "JOIN correction_jobs j ON j.submission_id = cs.submission_id "
        "WHERE j.error_code = 'invalid_submission' GROUP BY cs.submission_id "
        "ORDER BY j.updated_at DESC LIMIT 20")
    for inv in invalid:
        items.append({
            "type": "unreadable", "tone": "err", "icon": "file",
            "title": f"{inv['student_name'] or 'A submission'} could not be read",
            "detail": "The submitted file was empty or unreadable — no mark was awarded.",
            "route": "#/classroom", "count": 1,
        })

    # -- missing submissions per assignment -------------------------------
    missing = db.query(
        """
        SELECT w.id AS coursework_id, w.title, w.course_id,
               SUM(CASE WHEN cs.current_state IN ('NOT_STARTED','RECLAIMED_BY_STUDENT')
                        THEN 1 ELSE 0 END) AS missing,
               COUNT(cs.submission_id) AS total
        FROM coursework w
        JOIN classroom_submissions cs ON cs.coursework_id = w.id
        GROUP BY w.id
        HAVING missing > 0
        ORDER BY missing DESC LIMIT 10
        """)
    for m in missing:
        items.append({
            "type": "missing_submissions", "tone": "info", "icon": "clock",
            "title": (f"{_count(m['missing'], 'student')} "
                      f"{_verb(m['missing'], 'has', 'have')} not submitted — {m['title']}"),
            "detail": f"Out of {m['total']} in the class.",
            "route": f"#/assignments/{m['coursework_id']}",
            "count": m["missing"],
        })

    # -- unchecked (submitted but not corrected) --------------------------
    unchecked = db.query_one(
        """
        SELECT COUNT(*) AS n FROM classroom_submissions cs
        WHERE cs.current_state IN ('TURNED_IN','STUDENT_EDITED_AFTER_TURN_IN')
          AND cs.submission_id NOT IN (
              SELECT submission_id FROM results WHERE submission_id IS NOT NULL)
        """)
    if unchecked and unchecked["n"]:
        items.append({
            "type": "unchecked", "tone": "info", "icon": "wand",
            "title": f"{_count(unchecked['n'], 'submitted piece')} not yet corrected",
            "detail": "Run correction to mark them.",
            "route": "#/assignments", "count": unchecked["n"],
        })

    # -- Classroom sync problems ------------------------------------------
    sync_errors = db.query_one(
        "SELECT COUNT(*) AS n FROM classroom_submissions WHERE sync_error IS NOT NULL")
    if sync_errors and sync_errors["n"]:
        items.append({
            "type": "sync_error", "tone": "warn", "icon": "sync",
            "title": f"{_count(sync_errors['n'], 'submission')} had sync problems",
            "detail": "Last sync could not verify these rows; stored data kept.",
            "route": "#/classroom", "count": sync_errors["n"],
        })

    # -- Classroom connection ---------------------------------------------
    state = auth_state()
    if state.client_configured and not state.connected:
        items.append({
            "type": "classroom_disconnected", "tone": "warn", "icon": "classroom",
            "title": "Google Classroom is not connected",
            "detail": "Connect to sync courses and publish grades.",
            "route": "#/classroom", "count": 1,
        })

    # -- AI / model problems ----------------------------------------------
    if not cfg_keys["has_keys"]:
        items.append({
            "type": "no_keys", "tone": "err", "icon": "key",
            "title": "No Gemini API keys configured",
            "detail": "Corrections cannot run until a key is added.",
            "route": "#/ai", "count": 1,
        })
    elif cfg_keys["healthy"] == 0 and cfg_keys["total"] > 0:
        items.append({
            "type": "keys_down", "tone": "err", "icon": "key",
            "title": "All Gemini API keys are unavailable",
            "detail": f"{_count(cfg_keys['total'], 'key')} cooling down or invalid.",
            "route": "#/ai", "count": cfg_keys["total"],
        })

    # -- suspected mark-scheme errors --------------------------------------
    # Grouped and de-duplicated by the engine (one live record per assignment
    # + question + type), so the same error can never notify twice.
    from ..engine import scheme_audit as _scheme_audit
    items.extend(_scheme_audit.notifications(limit=25))

    counts: dict[str, int] = {}
    for it in items:
        counts[it["type"]] = counts.get(it["type"], 0) + 1
    return {
        "items": items,
        "total": len(items),
        "counts": counts,
        "generated_at": time.time(),
    }


def _key_health() -> dict:
    from ..gemini.tester import KeyManagerHolder
    try:
        status = KeyManagerHolder.instance().status()
        return {"has_keys": status["has_keys"], "healthy": status["healthy"],
                "total": status["total"]}
    except Exception:  # noqa: BLE001
        return {"has_keys": False, "healthy": 0, "total": 0}


# ================================================== mark-scheme validation (Part F)

def _mark_scheme_path(coursework_id: str) -> Path | None:
    """Locate the uploaded mark scheme for an assignment (no network)."""
    direct = data_dir() / "mark_schemes" / f"{coursework_id}.pdf"
    if direct.exists():
        return direct
    pending = db.query_one(
        "SELECT path FROM pending_mark_schemes WHERE coursework_id = ?",
        (coursework_id,))
    if pending and pending["path"] and Path(pending["path"]).exists():
        return Path(pending["path"])
    return None


def _mark_scheme_validation(coursework_id: str) -> dict:
    from ..engine import blueprint as bp_mod
    cw = db.query_one("SELECT id, title FROM coursework WHERE id = ?", (coursework_id,))
    path = _mark_scheme_path(coursework_id)
    if not path:
        return {
            "coursework_id": coursework_id,
            "assignment_title": cw["title"] if cw else None,
            "has_mark_scheme": False,
            "processed": False,
            "validation": bp_mod.structure_report(None),
            "warnings": ["No mark scheme has been uploaded for this assignment."],
            "manual_override": False,
        }
    blueprint, meta = bp_mod.blueprint_for_mark_scheme(path)
    if blueprint is None:
        return {
            "coursework_id": coursework_id,
            "assignment_title": cw["title"] if cw else None,
            "has_mark_scheme": True,
            "processed": False,
            "validation": bp_mod.structure_report(None),
            "warnings": ["This mark scheme has not been processed yet. It is "
                         "extracted automatically the first time work is corrected, "
                         "or you can recheck it now."],
            "manual_override": False,
        }
    report = bp_mod.structure_report(blueprint)
    declared = blueprint.get("total_marks")

    # Independent local reconstruction of the PDF (§§3-§9). Free, deterministic,
    # and it is what lets us say WHY a structure is doubtful instead of just
    # that the AI was unhappy.
    from ..engine import mark_scheme as ms_mod
    from ..engine import mark_scheme_verify as msv
    try:
        evidence = ms_mod.analyse_document(str(path))
    except Exception as exc:  # noqa: BLE001 - diagnostics must never 500
        evidence = {"mode": ms_mod.MODE_NONE, "error": str(exc)[:200]}
    evidence_info = msv.evidence_summary(evidence)
    local_numbers = ms_mod.candidate_question_numbers(evidence)
    structure = msv.canonicalize(
        {"questions": blueprint.get("questions", []), "printed_total": declared})
    # The page-scoped completeness verdict, when one exists, is an input here:
    # a MISSING question the document's own numbering proves cannot be
    # forgotten just because this diagnostics call does not re-read the PDF.
    record = bp_mod.load_record(bp_mod.file_hash(path)) or {}
    inventory = record.get("inventory") or None
    verification = msv.cross_validate(structure, evidence, inventory)
    # The structure the questions add up to, vs the total the mark scheme itself
    # states. Disagreement is the §20 cross-check: never silently continue.
    confidence = bp_mod.mark_scheme_confidence(report, declared_total=declared)
    # The headline badge must never disagree with the cross-validation. A
    # structure we cannot cross-check is not "verified" just because the
    # numbering looks tidy - uncertainty must not become false certainty.
    if verification["discrepancies"]:
        confidence = {
            "confidence": bp_mod.CONFIDENCE_NEEDS_REVIEW,
            "reasons": confidence["reasons"] + verification["anomalies"],
            "requires_verification": True,
        }
    elif verification["evidence_notes"]:
        # How the pages had to be read is not a finding about the mark scheme.
        confidence = {
            **confidence,
            "reasons": confidence["reasons"] + verification["evidence_notes"],
        }
    completeness = record.get("completeness") or {}
    verification_state = bp_mod.verification_status(record)
    # "Verified" needs BOTH: nothing contradicting the structure, and a
    # page-scoped read that established how many questions the document has and
    # accounted for all of them. The second half is what stops a visually-read
    # scheme from looking verified merely because no text layer means no text
    # evidence to disagree with.
    complete = (completeness.get("expected_count") is not None
                and not completeness.get("unresolved", False))
    verified = bool(verification["verified"]) and complete
    if completeness.get("unresolved") and verification_state == bp_mod.STATUS_VERIFIED:
        # Never show a badge that contradicts the stored verdict.
        verification_state = bp_mod.STATUS_NEEDS_MANUAL_REVIEW
    return {
        "coursework_id": coursework_id,
        "assignment_title": cw["title"] if cw else None,
        "has_mark_scheme": True,
        "processed": True,
        "validation": report,
        "questions": blueprint.get("questions", []),
        # Subquestion structure (§2/§8/§9): every declared marking point with
        # its own id, answers, M/A/B codes, page and validation status, plus the
        # anomalies the checker found at that level. The review surface can
        # therefore point a teacher at ONE subquestion instead of invalidating
        # an otherwise-correct scheme.
        "subquestions": report.get("subquestions") or [],
        "subquestion_count": report.get("subquestion_count") or 0,
        "subquestion_anomalies": report.get("subquestion_anomalies") or [],
        "total_marks": declared if declared is not None else report["total_marks"],
        "detected_total": report["total_marks"],
        "declared_total": declared,
        "totals_agree": (declared is None or int(declared or 0)
                         == report["total_marks"]),
        "paper": blueprint.get("paper", ""),
        "manual_override": bool(blueprint.get("manual_override")),
        "warnings": bp_mod.validation_warnings(report),
        "confidence": confidence["confidence"],
        "requires_verification": confidence["requires_verification"],
        "confidence_reasons": confidence["reasons"],
        # ---- how far this mark scheme has actually been verified ----------
        "verification_state": verification_state,
        "completeness": completeness,
        "inventory": inventory or {},
        # A result marked against an incomplete structure must say so.
        "marking_warning": (bp_mod.review_requirement(path)["warnings"]
                            if completeness.get("unresolved") else []),
        # Question/document-level marks reconciliation (§7): every index once,
        # a question with parts summed from its parts, unread marks reported.
        "reconciliation": (record.get("reconciliation")
                           or bp_mod.reconcile_marks(blueprint, inventory)),
        # Independent verification evidence (§§17/§20/§21).
        "verification": {
            "extraction_mode": evidence_info.get("extraction_mode"),
            "pdf_pages": evidence_info.get("pages"),
            "extracted_blocks": evidence_info.get("blocks"),
            "two_column_pages": evidence_info.get("two_column_pages"),
            "image_count": evidence_info.get("image_count"),
            "local_question_candidates": len(local_numbers),
            "local_candidates": local_numbers,
            "page_number_candidates": evidence_info.get("page_number_candidates"),
            "parser_total": report["total_marks"],
            "calculated_total": (structure or {}).get("calculated_total"),
            "printed_total": declared,
            "totals_agree": (structure is None
                             or declared is None
                             or int(declared) == report["total_marks"]),
            "structural_confidence": verification["structural_confidence"],
            "confidence_basis": verification["confidence_basis"],
            "verified": verified,
            # What contradicts the structure, vs how the document had to be
            # read. The review surface must not present the second as the first.
            "discrepancies": verification["discrepancies"],
            "evidence_notes": verification["evidence_notes"],
            "evidence_kind": verification["evidence_kind"],
            "anomaly_records": verification["anomaly_records"],
            "evidence_agrees": verification["evidence_agrees"],
            "missing_questions": verification["checks"].get("missing_questions"),
            "subquestion_counts": verification["checks"].get(
                "subquestion_counts"),
            "anomalies": verification["anomalies"],
            # One actionable sentence instead of a bare "AI verification failed".
            "likely_cause": msv.likely_cause(verification),
        },
    }


@router.get("/assignments/{coursework_id}/mark-scheme")
def assignment_mark_scheme(coursework_id: str):
    """The extracted mark-scheme structure + deterministic validation (Part F).

    Offline: reads the cached structure only — no AI call, so historical
    inspection keeps working with no keys or network (Part M)."""
    return _mark_scheme_validation(coursework_id)


@router.post("/assignments/{coursework_id}/mark-scheme/recheck")
def recheck_assignment_mark_scheme(coursework_id: str):
    """REPROCESS the mark scheme (re-extract, not re-count) and compare passes."""
    from ..config import get_config
    from ..engine import blueprint as bp_mod
    path = _mark_scheme_path(coursework_id)
    if not path:
        raise AppError("Upload a mark scheme for this assignment first.",
                       status=400, code="mark_scheme_missing")

    if get_config().mock_mode:
        raise AppError(
            "Mark-scheme recheck needs a real AI model; the app is in offline "
            "demo mode.", status=409, code="mock_mode")

    # Is a re-verification justified at all? An unchanged document whose
    # verification already concluded is returned as-is — the identical request
    # cannot change its own answer, and repeating it is what filled the log
    # with two identical "67 questions / 255 marks" runs per click.
    run, reason, state = bp_mod.recheck_decision(path)
    if not run:
        log_event("api", "mark-scheme recheck skipped",
                  coursework_id=coursework_id, state=state, reason=reason)
        return _mark_scheme_validation(coursework_id) | {
            "recheck": {
                "skipped": True,
                "state": state,
                "reason": reason,
                "previous": bp_mod.structure_report(
                    bp_mod.blueprint_for_mark_scheme(path)[0]),
                "passes": [],
                "disagreement": False,
                "warnings": [],
            },
        }

    from ..gemini.client_proxy import KeyAwareClient
    from ..gemini.key_manager import KeyManager
    client = KeyAwareClient(KeyManager())
    try:
        outcome = bp_mod.recheck_blueprint(client, path)
    except Exception as exc:  # noqa: BLE001 — report the real reason
        log_event("api", "mark-scheme recheck failed",
                  coursework_id=coursework_id, error=str(exc))
        raise AppError(f"The mark-scheme recheck failed: {exc}",
                       status=502, code="recheck_failed")
    if outcome.get("busy"):
        log_event("api", "mark-scheme recheck already running",
                  coursework_id=coursework_id)
        raise AppError(
            "This mark scheme is already being verified. The running check "
            "will finish and its result will be shown — no second request was "
            "sent.", status=409, code="recheck_in_progress")
    log_event("api", "mark-scheme rechecked", coursework_id=coursework_id,
              ok=outcome.get("ok"),
              count=outcome.get("validation", {}).get("question_count"),
              disagreement=outcome.get("disagreement"),
              state=outcome.get("state"),
              missing=(outcome.get("completeness") or {}).get("missing_indices"))
    if not outcome.get("ok"):
        raise AppError(
            outcome.get("error") or "The mark scheme could not be reprocessed.",
            status=502, code="recheck_failed")
    return _mark_scheme_validation(coursework_id) | {
        "recheck": {
            "skipped": False,
            "state": outcome.get("state"),
            "previous": outcome.get("previous"),
            "passes": outcome.get("passes"),
            "disagreement": outcome.get("disagreement", False),
            "warnings": outcome.get("warnings", []),
            "completeness": outcome.get("completeness") or {},
        },
    }


#: Rendered page resolution for the review viewer. High enough to read a
#: compiled mark scheme's small print, low enough that one page is a normal
#: HTTP response. Clamped per request.
_PAGE_DPI_DEFAULT = 150
_PAGE_DPI_MAX = 300


def _rendered_page_path(sha: str, page: int, dpi: int) -> Path:
    """Where a rendered page is cached on disk (per document, per page, per dpi)."""
    folder = data_dir() / "tmp" / "scheme_pages" / sha[:16]
    folder.mkdir(parents=True, exist_ok=True)
    return folder / f"p{page:04d}@{dpi}.png"


@router.get("/assignments/{coursework_id}/mark-scheme/pages")
def assignment_mark_scheme_pages(coursework_id: str):
    """Which pages of the mark scheme exist, and what each one contributed.

    Feeds the review viewer: the teacher sees the SOURCE document page next to
    the questions, marks and anomalies read from it. Deterministic and offline —
    it reads the PDF's page count and the persisted inventory, never a model.
    """
    from ..engine import scheme_inventory as si
    from ..engine import blueprint as bp_mod

    path = _mark_scheme_path(coursework_id)
    if not path:
        raise AppError("Upload a mark scheme for this assignment first.",
                       status=400, code="mark_scheme_missing")
    try:
        total = si.page_count(path)
        sha = bp_mod.file_hash(path)
    except OSError as exc:
        raise AppError(f"The mark scheme file could not be read: {exc}",
                       status=500, code="mark_scheme_unreadable")
    record = bp_mod.load_record(sha) or {}
    inventory = record.get("inventory") or {}
    completeness = record.get("completeness") or {}

    # Per-page evidence: which questions were read from each page, so selecting
    # a question can point at its source page and vice versa.
    by_page: dict[int, list[dict]] = {}
    for item in (inventory.get("items") or []):
        page = item.get("page")
        if not page:
            continue
        by_page.setdefault(int(page), []).append({
            "source_index": item.get("source_index"),
            "label": item.get("label"),
            "max_marks": item.get("max_marks"),
        })

    pages = []
    for number in range(1, total + 1):
        items = by_page.get(number, [])
        pages.append({
            "page": number,
            "has_items": bool(items),
            "item_count": len(items),
            "indices": [i["source_index"] for i in items],
            # A page with nothing on it is the one a reviewer should look at:
            # either it is a cover, or a page whose read failed.
            "needs_review": (not items)
                            and number in (completeness.get("pages_without_items") or []),
        })
    return {
        "coursework_id": coursework_id,
        "sha256": sha,
        "page_count": total,
        "pages": pages,
        "items": inventory.get("items") or [],
        "completeness": completeness,
        # Both readings of a disputed index, so the reviewer can see the
        # disagreement instead of being told that one exists.
        "conflicts": inventory.get("conflicts") or [],
        "label_variants": inventory.get("label_variants") or [],
        "verification_state": bp_mod.verification_status(record),
        "anomalies": record.get("anomalies") or [],
        "dpi": _PAGE_DPI_DEFAULT,
        "page_url_template": (
            f"/api/assignments/{coursework_id}/mark-scheme/page/{{page}}.png"),
    }


@router.get("/assignments/{coursework_id}/mark-scheme/page/{page}.png")
def assignment_mark_scheme_page_image(coursework_id: str, page: int,
                                      request: Request,
                                      dpi: int = _PAGE_DPI_DEFAULT):
    """The ACTUAL uploaded page, rendered as PNG — never a reconstruction.

    Cached on disk per document/page/dpi so flipping through pages in review
    does not re-render, and served with an ETag so the browser can keep showing
    the page while verification runs.
    """
    from fastapi.responses import Response
    from ..engine import scheme_inventory as si
    from ..engine import blueprint as bp_mod

    path = _mark_scheme_path(coursework_id)
    if not path:
        raise AppError("Upload a mark scheme for this assignment first.",
                       status=400, code="mark_scheme_missing")
    dpi = max(72, min(int(dpi or _PAGE_DPI_DEFAULT), _PAGE_DPI_MAX))
    try:
        sha = bp_mod.file_hash(path)
        total = si.page_count(path)
    except OSError as exc:
        raise AppError(f"The mark scheme file could not be read: {exc}",
                       status=500, code="mark_scheme_unreadable")
    if page < 1 or page > total:
        raise AppError(f"This mark scheme has {_count(total, 'page')}; "
                       f"page {page} does not exist.",
                       status=404, code="page_out_of_range")

    target = _rendered_page_path(sha, page, dpi)
    etag = f'"{sha[:16]}-p{page}-{dpi}"'
    # A conditional request gets an empty 304: the page for this document hash
    # can never change, so there is nothing to send and nothing to re-render.
    if etag in (request.headers.get("if-none-match") or ""):
        return Response(status_code=304, headers={"ETag": etag,
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
                 # The page for a given document hash never changes, so the
                 # browser may reuse it: a reload must not blank the viewer.
                 "Cache-Control": "private, max-age=86400",
                 "Content-Disposition": f'inline; filename="page-{page}.png"'})


@router.post("/assignments/{coursework_id}/mark-scheme/override")
def override_assignment_mark_scheme(coursework_id: str, payload: dict):
    """Teacher edits the extracted structure (question count/numbering/marks).

    Validated before saving: an unusable structure is rejected. Applies to
    FUTURE corrections of this mark scheme; no stored result is altered."""
    from ..engine import blueprint as bp_mod
    path = _mark_scheme_path(coursework_id)
    if not path:
        raise AppError("Upload a mark scheme for this assignment first.",
                       status=400, code="mark_scheme_missing")
    questions = payload.get("questions")
    if not isinstance(questions, list):
        raise AppError("Provide a 'questions' list to save a manual structure.",
                       status=400, code="bad_override")
    try:
        sha = bp_mod.file_hash(path)
        bp_mod.save_manual_blueprint(
            sha, questions, total_marks=payload.get("total_marks"),
            note=str(payload.get("note") or ""))
    except ValueError as exc:
        raise AppError(str(exc), status=400, code="bad_override")
    except OSError as exc:
        raise AppError(f"The mark scheme file could not be read: {exc}",
                       status=500, code="mark_scheme_unreadable")
    log_event("api", "mark-scheme manually overridden",
              coursework_id=coursework_id, questions=len(questions))
    return _mark_scheme_validation(coursework_id)
