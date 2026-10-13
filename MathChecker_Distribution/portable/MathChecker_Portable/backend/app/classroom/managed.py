"""Managed vs External Google Classroom assignments + a honest publish queue.

The single correctness rule this module exists to uphold:

    Never report a Classroom operation as successful unless Google actually
    confirmed it.

Two assignment kinds:

    MANAGED   — created by THIS integration through the Classroom API. The
                project that created a CourseWork is the only project Google
                permits to modify its student submissions (grades, returns,
                attachments). Membership is therefore stored when we create
                the coursework, and is never inferred from a title.

    EXTERNAL  — created anywhere else (directly in Classroom, by another
                tool). Read/sync/correct is allowed; write-back is attempted
                and its REAL result recorded — typically a 403 that we surface
                as a plain-language limitation, never as success.

Everything here writes the true outcome of each Google call into
``classroom_publish_ops`` so the UI can show per-operation results, including
partial failures and unsupported operations.
"""

from __future__ import annotations

import json
import logging
import time

from .. import db
from .. import student_identity
from ..logging_setup import log_event
from . import client as gc

log = logging.getLogger("classroom.managed")

# ------------------------------------------------------------------- kinds

MANAGED = "managed"
EXTERNAL = "external"

# Operation result vocabulary shared with the frontend.
STATUS_PENDING = "pending"
STATUS_PUBLISHED = "published"
STATUS_FAILED = "failed"
STATUS_NOT_SUPPORTED = "not_supported"
STATUS_REQUIRES_REVIEW = "requires_review"
STATUS_SKIPPED = "skipped"
STATUS_REQUIRES_AUTH = "requires_auth"


# ------------------------------------------------------------------ records

def is_managed(coursework_id: str | None) -> bool:
    """True only when THIS integration created the CourseWork (persisted fact)."""
    if not coursework_id:
        return False
    row = db.query_one(
        "SELECT 1 FROM classroom_managed_assignments WHERE coursework_id = ?",
        (coursework_id,),
    )
    return row is not None


def managed_map() -> dict[str, dict]:
    """Every managed assignment keyed by coursework id (for list views)."""
    out: dict[str, dict] = {}
    for row in db.query("SELECT * FROM classroom_managed_assignments"):
        out[row["coursework_id"]] = dict(row)
    return out


def record_managed(coursework_id: str, course_id: str, *, title: str | None = None,
                   google_account: str | None = None, project_id: str | None = None,
                   classroom_state: str | None = None, alternate_link: str | None = None,
                   max_points: float | None = None, due_date: str | None = None) -> None:
    """Persist that this integration created ``coursework_id``."""
    db.execute(
        """
        INSERT INTO classroom_managed_assignments(
            coursework_id, course_id, title, google_account, integration_source,
            project_id, classroom_state, alternate_link, max_points, due_date,
            created_at, last_verified_at)
        VALUES(?,?,?,?, 'math_checker', ?,?,?,?,?,?,?)
        ON CONFLICT(coursework_id) DO UPDATE SET
            course_id=excluded.course_id,
            title=COALESCE(excluded.title, classroom_managed_assignments.title),
            google_account=COALESCE(excluded.google_account,
                                    classroom_managed_assignments.google_account),
            classroom_state=COALESCE(excluded.classroom_state,
                                     classroom_managed_assignments.classroom_state),
            alternate_link=COALESCE(excluded.alternate_link,
                                    classroom_managed_assignments.alternate_link),
            max_points=COALESCE(excluded.max_points,
                                classroom_managed_assignments.max_points),
            due_date=COALESCE(excluded.due_date,
                              classroom_managed_assignments.due_date),
            last_verified_at=excluded.last_verified_at
        """,
        (coursework_id, course_id, title, google_account, project_id,
         classroom_state, alternate_link, max_points, due_date,
         time.time(), time.time()),
    )
    log_event("classroom.managed", "assignment recorded as managed",
              course_id=course_id, coursework_id=coursework_id)


def managed_info(coursework_id: str | None) -> dict:
    """Public shape describing an assignment's management status."""
    if not coursework_id:
        return {"kind": EXTERNAL, "managed": False}
    row = db.query_one(
        "SELECT * FROM classroom_managed_assignments WHERE coursework_id = ?",
        (coursework_id,),
    )
    if not row:
        return {"kind": EXTERNAL, "managed": False}
    return {
        "kind": MANAGED,
        "managed": True,
        "google_account": row["google_account"],
        "integration_source": row["integration_source"],
        "created_at": row["created_at"],
        "last_verified_at": row["last_verified_at"],
        "classroom_state": row["classroom_state"],
    }


# ------------------------------------------------------------------ creating

def create_coursework(course_id: str, *, title: str, description: str | None = None,
                      max_points: float | None = None, due_date: str | None = None,
                      due_time: str | None = None, state: str = "PUBLISHED",
                      materials: list[dict] | None = None,
                      account_email: str | None = None) -> dict:
    """Create a CourseWork through the Classroom API and record it as managed.

    Raises ``ClassroomApiError`` on any Google rejection (the caller surfaces
    the real error). Returns the created resource plus our management metadata.
    """
    from .auth import get_access_token
    from .naming import default_name_for

    # Apply the teacher's OPTIONAL naming template. With the default (empty)
    # template this is a no-op, so the title is preserved exactly and no
    # "Via Math Checker" prefix/suffix is ever forced on.
    if title:
        title = default_name_for(title)

    token = get_access_token()
    body: dict = {
        "title": title,
        "workType": "ASSIGNMENT",
        "state": state if state in ("PUBLISHED", "DRAFT") else "PUBLISHED",
    }
    if description:
        body["description"] = description
    if max_points is not None:
        body["maxPoints"] = float(max_points)
    if due_date:
        body["dueDate"] = _parse_due_date(due_date)
    if due_time:
        body["dueTime"] = _parse_due_time(due_time)
    if materials:
        body["materials"] = materials

    created = gc.classroom_request(
        "POST", f"{gc.CLASSROOM_BASE}/courses/{course_id}/courseWork",
        token, json=body,
    )

    # Best-effort: keep the local coursework cache in step so the new managed
    # assignment appears immediately without a full resync.
    try:
        _cache_coursework(course_id, created)
    except Exception:  # noqa: BLE001 — caching is not critical to correctness
        log.exception("could not cache newly created coursework")

    project_id = None
    try:
        from ..config import get_secrets
        project_id = get_secrets().oauth_client_id()
    except Exception:  # noqa: BLE001
        project_id = None

    record_managed(
        created.get("id"), course_id,
        title=created.get("title") or title,
        google_account=account_email,
        project_id=project_id,
        classroom_state=created.get("state"),
        alternate_link=created.get("alternateLink"),
        max_points=created.get("maxPoints"),
        due_date=created.get("dueDate") and _due_str(created.get("dueDate")),
    )
    return {**created, "management": managed_info(created.get("id"))}


def update_coursework_max_points(coursework_id: str, course_id: str,
                                 max_points: float, *, verify: bool = True) -> dict:
    """Change the ASSIGNMENT's real Google Classroom total points.

    This is a genuine Classroom write: ``PATCH /courses/{c}/courseWork/{w}
    ?updateMask=maxPoints``. Google's returned resource is the authority — we
    only report success once the returned ``maxPoints`` equals the requested
    value (optionally re-reading it to confirm). The local ``coursework`` cache
    and the managed record are refreshed so every surface shows the new total.

    Raises ``ClassroomApiError`` when Google rejects the change (e.g. 403 for
    an assignment this project did not create) and ``ValueError`` for an
    invalid number. Never reports success that Google did not confirm.
    """
    from .auth import get_access_token

    try:
        value = float(max_points)
    except (TypeError, ValueError):
        raise ValueError("Classroom total points must be a number.")
    if value <= 0 or value != value or value in (float("inf"), float("-inf")):
        raise ValueError("Classroom total points must be greater than 0.")
    value = round(value, 2)

    token = get_access_token()
    url = f"{gc.CLASSROOM_BASE}/courses/{course_id}/courseWork/{coursework_id}"
    updated = gc.classroom_request(
        "PATCH", url, token,
        params={"updateMask": "maxPoints"}, json={"maxPoints": value},
    )
    returned = updated.get("maxPoints")

    def _matches(raw) -> bool:
        try:
            return abs(float(raw) - value) < 1e-6
        except (TypeError, ValueError):
            return False

    verified = _matches(returned)
    if verify and not verified:
        # The PATCH may return a sparse/unchanged resource; re-read the
        # coursework to confirm before we claim the change landed.
        current = gc.classroom_request("GET", url, token)
        returned = current.get("maxPoints")
        verified = _matches(returned)

    if not verified:
        raise gc.ClassroomApiError(
            502,
            "Google did not confirm the new total points (returned "
            f"{returned!r}). The Classroom assignment was not updated.",
        )

    # Refresh local state so the new total appears everywhere immediately.
    final = float(returned)
    db.execute("UPDATE coursework SET max_points = ?, fetched_at = ? WHERE id = ?",
               (final, time.time(), coursework_id))
    db.execute(
        "UPDATE classroom_managed_assignments SET max_points = ?, last_verified_at = ? "
        "WHERE coursework_id = ?", (final, time.time(), coursework_id))
    log_event("classroom.managed", "classroom total updated",
              coursework_id=coursework_id, max_points=final, verified=True)
    return {
        "coursework_id": coursework_id,
        "course_id": course_id,
        "max_points": final,
        "verified": True,
        "classroom": updated,
        "management": managed_info(coursework_id),
    }


def _cache_coursework(course_id: str, cw: dict) -> None:
    due = cw.get("dueDate") or {}
    due_date = _due_str(due) if due else None
    db.execute(
        """
        INSERT INTO coursework(id, course_id, title, max_points, state, due_date,
                               allowlisted, alternate_link, description, update_time,
                               fetched_at, classroom_title, sync_state)
        VALUES(?,?,?,?,?,?, 0,?,?,?,?,?, 'ACTIVE')
        ON CONFLICT(id) DO UPDATE SET
            classroom_title=excluded.classroom_title,
            -- The teacher's custom display name always wins over Google's.
            title=COALESCE(coursework.custom_title, excluded.title),
            max_points=COALESCE(excluded.max_points, coursework.max_points),
            state=excluded.state,
            due_date=COALESCE(excluded.due_date, coursework.due_date),
            alternate_link=COALESCE(excluded.alternate_link, coursework.alternate_link),
            description=COALESCE(excluded.description, coursework.description),
            update_time=excluded.update_time,
            fetched_at=excluded.fetched_at
        """,
        (cw.get("id"), course_id, cw.get("title") or "Untitled",
         cw.get("maxPoints"), cw.get("state"), due_date,
         cw.get("alternateLink"), cw.get("description"),
         cw.get("updateTime"), time.time(), cw.get("title") or "Untitled"),
    )


def _due_str(due: dict) -> str | None:
    try:
        y, m, d = due.get("year"), due.get("month"), due.get("day")
        if y and m and d:
            return f"{y:04d}-{m:02d}-{d:02d}"
    except (TypeError, ValueError):
        pass
    return None


def _parse_due_date(value: str) -> dict:
    """Accept 'YYYY-MM-DD' (the <input type=date> format)."""
    parts = str(value).split("-")
    if len(parts) != 3:
        raise ValueError("due_date must be YYYY-MM-DD")
    return {"year": int(parts[0]), "month": int(parts[1]), "day": int(parts[2])}


def _parse_due_time(value: str) -> dict:
    """Accept 'HH:MM' (the <input type=time> format) in UTC."""
    parts = str(value).split(":")
    if len(parts) < 2:
        raise ValueError("due_time must be HH:MM")
    return {"hours": int(parts[0]), "minutes": int(parts[1]), "seconds": 0}


# --------------------------------------------------------------- publish queue

def _grade_for(result_row: dict, max_points: float | None) -> float | None:
    if not result_row:
        return None
    earned, available = result_row.get("earned"), result_row.get("available")
    if not available:
        return None
    if max_points:
        return round((earned / available) * max_points, 2)
    return round(float(earned), 2)


def build_publish_queue(coursework_id: str, course_id: str) -> dict:
    """Describe what COULD be published for an assignment — no side effects.

    Every entry is derived from real stored state (a completed, verified
    result) so the teacher sees the true queue before anything is sent.
    """
    info = managed_info(coursework_id)
    managed = info["managed"]
    cw = db.query_one("SELECT * FROM coursework WHERE id = ?", (coursework_id,))
    max_points = cw["max_points"] if cw else None

    results = db.query(
        """
        SELECT r.*, cs.student_name AS tracked_name, cs.assigned_grade,
               cs.draft_grade, cs.current_state
        FROM results r
        LEFT JOIN classroom_submissions cs
          ON cs.submission_id = r.submission_id
        WHERE r.coursework_id = ? AND r.submission_id IS NOT NULL
        ORDER BY r.created_at DESC
        """,
        (coursework_id,),
    )
    seen: set[str] = set()
    items: list[dict] = []
    already_graded = 0
    needs_review = 0
    for r in results:
        sid = r["submission_id"]
        if sid in seen:
            continue
        seen.add(sid)
        if int(r["review_required"] or 0) and not int(r["manual_override"] or 0):
            needs_review += 1
            review = True
        else:
            review = False
        if r["assigned_grade"] is not None:
            already_graded += 1
        _who = student_identity.resolve(r["student_id"],
                                        snapshot_name=r["tracked_name"]
                                        or r["student_name"])
        items.append({
            "submission_id": sid,
            "student_id": r["student_id"],
            "student_name": r["tracked_name"] or r["student_name"],
            # Canonical: nickname -> Classroom name (§4).
            "display_name": _who["display_name"],
            "classroom_name": _who["classroom_name"],
            "nickname": _who["nickname"],
            "earned": r["earned"],
            "available": r["available"],
            "percentage": r["percentage"],
            "provisional_grade": r["provisional_grade"],
            "confidence": r["confidence"],
            "review_required": review,
            "manual_override": bool(r["manual_override"]),
            "suggested_grade": _grade_for(dict(r), max_points),
            "max_points": max_points,
            "assigned_grade": r["assigned_grade"],
            "draft_grade": r["draft_grade"],
            "current_state": r["current_state"],
        })

    return {
        "coursework_id": coursework_id,
        "course_id": course_id,
        "management": info,
        "max_points": max_points,
        "items": items,
        "summary": {
            "grades_ready": len(items) - needs_review,
            "needs_review": needs_review,
            "already_graded": already_graded,
            "total": len(items),
        },
        # Capability hints the UI uses to disable controls and explain why.
        "capabilities": _capabilities(managed),
    }


def _capabilities(managed: bool) -> dict:
    """What Classroom will actually let us do for this assignment kind.

    External assignments: read/sync/correct/report locally, but write-back is
    not permitted by Google for a CourseWork another project created. We still
    ATTEMPT it (Google is the authority) and report the real response, but the
    UI must warn first.
    """
    if managed:
        return {
            "draft_grade": True, "final_grade": True, "return": True,
            "attach_report": False,       # not offered by the Classroom API
            "reason": None,
        }
    return {
        "draft_grade": False, "final_grade": False, "return": False,
        "attach_report": False,
        "reason": ("Google Classroom only permits the integration that created "
                   "a CourseWork to change its grades or return it. This "
                   "assignment was not created by Math Checker, so publishing "
                   "is not available. You can still correct it and download "
                   "reports locally."),
    }


def publish(coursework_id: str, course_id: str, operations: list[dict],
            *, attempt_unmanaged: bool = False) -> dict:
    """Execute publish operations, recording the TRUE result of each one.

    ``operations`` is a list of ``{type, submission_id, grade?}`` where type is
    one of draft_grade | final_grade | return | attach_report. Nothing here
    ever invents success: the outcome is whatever Google returned (or a
    pre-cleared ``not_supported`` for a capability Google does not offer).
    """
    from .auth import get_access_token

    info = managed_info(coursework_id)
    managed = info["managed"]
    caps = _capabilities(managed)
    results: list[dict] = []

    token = None
    for op in operations:
        op_type = str(op.get("type") or "").strip()
        submission_id = op.get("submission_id")
        row = _start_op(coursework_id, course_id, submission_id, op)

        # Attachment is never supported by the StudentSubmission API.
        if op_type == "attach_report":
            results.append(_finish_op(row, STATUS_NOT_SUPPORTED,
                message="Google Classroom does not allow attaching files to a "
                        "student submission. Download the report and share it "
                        "yourself, or attach it to the assignment in Classroom."))
            continue

        if op_type not in ("draft_grade", "final_grade", "assign_grade",
                           "return"):
            results.append(_finish_op(row, STATUS_FAILED,
                message=f"Unknown operation '{op_type}'."))
            continue

        if not managed and not attempt_unmanaged:
            results.append(_finish_op(row, STATUS_NOT_SUPPORTED,
                message=caps["reason"] or "Not permitted for this assignment."))
            continue

        if not submission_id:
            results.append(_finish_op(row, STATUS_FAILED,
                message="No submission id supplied."))
            continue

        try:
            if token is None:
                token = get_access_token()
            outcome = _execute(token, course_id, coursework_id, submission_id,
                               op_type, op)
            results.append(_finish_op(row, outcome["status"], http=outcome.get("http"),
                                      code=outcome.get("code"), message=outcome.get("message"),
                                      payload=outcome.get("payload")))
        except Exception as exc:  # noqa: BLE001 — must be reported, never hidden
            results.append(_finish_op(row, STATUS_FAILED,
                message=_clean_error(exc)))

    published = sum(1 for r in results if r["status"] == STATUS_PUBLISHED)
    failed = sum(1 for r in results if r["status"] == STATUS_FAILED)
    unsupported = sum(1 for r in results if r["status"] == STATUS_NOT_SUPPORTED)
    auth = sum(1 for r in results if r["status"] == STATUS_REQUIRES_AUTH)
    skipped = sum(1 for r in results if r["status"] == STATUS_SKIPPED)
    log_event("classroom.managed", "publish run finished",
              coursework_id=coursework_id, published=published, failed=failed,
              not_supported=unsupported, requires_auth=auth)
    return {
        "coursework_id": coursework_id,
        "management": info,
        "results": results,
        "summary": {
            "published": published, "failed": failed,
            "not_supported": unsupported, "requires_auth": auth,
            "skipped": skipped, "total": len(results),
        },
    }


def _execute(token: str, course_id: str, coursework_id: str, submission_id: str,
             op_type: str, op: dict) -> dict:
    """Perform one Classroom write and translate Google's response honestly."""
    base = (f"{gc.CLASSROOM_BASE}/courses/{course_id}/courseWork/"
            f"{coursework_id}/studentSubmissions/{submission_id}")
    try:
        if op_type == "return":
            gc.classroom_request("POST", f"{base}:return", token, json={})
            return {"status": STATUS_PUBLISHED, "http": 200,
                    "message": "Returned to student."}

        grade = op.get("grade")
        if grade is None:
            return {"status": STATUS_FAILED, "code": "no_grade",
                    "message": "No grade value to publish."}
        grade = float(grade)
        if op_type == "draft_grade":
            gc.classroom_request("PATCH", base, token,
                                 params={"updateMask": "draftGrade"},
                                 json={"draftGrade": grade})
            return {"status": STATUS_PUBLISHED, "http": 200,
                    "message": f"Draft grade {grade:g} saved (not yet visible to the student).",
                    "payload": {"grade": grade}}
        if op_type == "assign_grade":
            # Write the FINAL (assigned) grade WITHOUT returning, so the return
            # is a separate, independently verifiable step. Google's grade
            # guide is explicit that ``assignedGrade`` must NOT be set without
            # ``draftGrade`` ("Don't set the assignedGrade without setting the
            # draftGrade"), and the ``return`` method does NOT copy the draft
            # into the assigned grade - so BOTH fields are written here. Writing
            # only ``assignedGrade`` is the unreliable write that could leave a
            # "successful" correction with no grade actually in Classroom.
            gc.classroom_request("PATCH", base, token,
                                 params={"updateMask": "draftGrade,assignedGrade"},
                                 json={"draftGrade": grade,
                                       "assignedGrade": grade})
            return {"status": STATUS_PUBLISHED, "http": 200,
                    "message": f"Final grade {grade:g} assigned to the submission.",
                    "payload": {"grade": grade}}
        # final grade: set both grade fields, then return so the student sees it.
        gc.classroom_request("PATCH", base, token,
                             params={"updateMask": "draftGrade,assignedGrade"},
                             json={"draftGrade": grade, "assignedGrade": grade})
        gc.classroom_request("POST", f"{base}:return", token, json={})
        return {"status": STATUS_PUBLISHED, "http": 200,
                "message": f"Final grade {grade:g} published and returned.",
                "payload": {"grade": grade}}
    except gc.ClassroomApiError as exc:
        return _translate_api_error(exc)
    except Exception as exc:  # noqa: BLE001
        return {"status": STATUS_FAILED, "message": _clean_error(exc)}


def _translate_api_error(exc: "gc.ClassroomApiError") -> dict:
    status = exc.status
    detail = (exc.detail or "")
    if status == 401:
        return {"status": STATUS_REQUIRES_AUTH, "http": status,
                "message": "Google authorization expired. Reconnect Google Classroom."}
    if status == 403:
        # The overwhelmingly common case: coursework created by another app.
        if "PERMISSION_DENIED" in detail or "ProjectPermissionDenied" in detail \
                or "forbidden" in detail.lower():
            return {"status": STATUS_NOT_SUPPORTED, "http": status,
                    "code": "permission_denied",
                    "message": ("Google Classroom refused the operation: only the "
                                "integration that created this assignment may change "
                                "its grades. This assignment is external to Math Checker.")}
        return {"status": STATUS_FAILED, "http": status, "code": "forbidden",
                "message": f"Google refused the operation (403). {_clean_error(exc)}"}
    if status == 404:
        return {"status": STATUS_FAILED, "http": status, "code": "not_found",
                "message": "Google could not find that submission — run a sync and retry."}
    if status == 429:
        return {"status": STATUS_FAILED, "http": status, "code": "rate_limited",
                "message": "Google rate-limited the request. Try again shortly."}
    if status >= 500:
        return {"status": STATUS_FAILED, "http": status, "code": "google_error",
                "message": f"Google reported a server error ({status}). Try again shortly."}
    return {"status": STATUS_FAILED, "http": status,
            "message": _clean_error(exc)}


def _clean_error(exc: Exception) -> str:
    text = str(exc).strip()
    return text[:500] if text else "The operation failed."


# --------------------------------------------------- publish-ops persistence

def _start_op(coursework_id: str, course_id: str, submission_id: str | None,
              op: dict) -> int:
    student_name = None
    student_id = None
    if submission_id:
        row = db.query_one(
            "SELECT student_id, student_name FROM classroom_submissions "
            "WHERE submission_id = ?", (submission_id,))
        if row:
            student_id, student_name = row["student_id"], row["student_name"]
    cur = db.execute(
        """
        INSERT INTO classroom_publish_ops(
            coursework_id, course_id, submission_id, student_id, student_name,
            op_type, payload_json, status, created_at, attempted_at)
        VALUES(?,?,?,?,?,?,?, 'pending', ?, ?)
        """,
        (coursework_id, course_id, submission_id, student_id, student_name,
         str(op.get("type")), json.dumps(op), time.time(), time.time()),
    )
    return int(cur.lastrowid)


def _finish_op(op_id: int, status: str, *, http: int | None = None,
               code: str | None = None, message: str | None = None,
               payload: dict | None = None) -> dict:
    db.execute(
        "UPDATE classroom_publish_ops SET status=?, http_status=?, error_code=?, "
        "message=?, payload_json=?, finished_at=? WHERE id=?",
        (status, http, code, message,
         json.dumps(payload) if payload is not None else None, time.time(), op_id),
    )
    return {"id": op_id, "status": status, "http_status": http,
            "error_code": code, "message": message}


# Public alias: the UI reads capabilities to disable unsupported controls
# and explain why, without reaching into a private name.
capabilities_for = _capabilities


def recent_publish_ops(coursework_id: str, limit: int = 200) -> list[dict]:
    rows = db.query(
        "SELECT * FROM classroom_publish_ops WHERE coursework_id = ? "
        "ORDER BY created_at DESC LIMIT ?", (coursework_id, limit))
    return [dict(r) for r in rows]


#: The grade write-backs that actually put a mark in Classroom.
GRADE_OPS = ("draft_grade", "final_grade", "assign_grade", "return")


def grade_write_evidence(coursework_id: str | None,
                         submission_id: str | None) -> str:
    """What our own audit log says about the LAST grade write for a submission.

    * ``"published"`` - a grade write was accepted by Google, so repeating it
      is a duplicate request.
    * ``"unknown"``   - a grade write was attempted but never confirmed (a
      crash mid-request or a timeout): Google MAY still have applied it, so the
      remote state must be read before anything is submitted again.
    * ``"none"``      - no grade write of ours has been attempted yet, so a
      write always proceeds (and a teacher's own grade can never be mistaken
      for ours).
    """
    if not coursework_id or not submission_id:
        return "none"
    row = db.query_one(
        f"""
        SELECT status FROM classroom_publish_ops
         WHERE coursework_id = ? AND submission_id = ?
           AND op_type IN ({','.join('?' * len(GRADE_OPS))})
         ORDER BY created_at DESC LIMIT 1
        """,
        (coursework_id, submission_id, *GRADE_OPS))
    if row is None:
        return "none"
    return "published" if row["status"] == STATUS_PUBLISHED else "unknown"


def grade_return_state(coursework_id: str | None,
                       submission_id: str | None) -> dict:
    """Did we actually return this student's grade to Classroom?

    The result page has to show "Grade returned to Classroom" as its own
    state (§19), so it needs EVIDENCE rather than an assumption that the
    attachment implies a grade. Two independent kinds are accepted:

    1. our own publish-op log, which is only written ``published`` after
       Google accepted the grade write;
    2. a grade read BACK off the synced submission row - proof the mark is
       really in Classroom, however it got there (a publish, a bulk push, or
       the teacher entering it by hand).

    With neither, the answer is honestly "no evidence yet" rather than a
    green tick.
    """
    if not coursework_id or not submission_id:
        return {"returned": False, "reason": "no_classroom_submission"}
    row = db.query_one(
        f"""
        SELECT status, op_type, finished_at
          FROM classroom_publish_ops
         WHERE coursework_id = ? AND submission_id = ?
           AND op_type IN ({','.join('?' * len(GRADE_OPS))})
         ORDER BY created_at DESC LIMIT 1
        """,
        (coursework_id, submission_id, *GRADE_OPS))
    if row and row["status"] == STATUS_PUBLISHED:
        return {"returned": True, "source": "publish_op",
                "reason": None, "op_type": row["op_type"],
                "at": row["finished_at"]}

    synced = db.query_one(
        "SELECT assigned_grade, draft_grade, current_state "
        "FROM classroom_submissions WHERE submission_id = ?",
        (submission_id,))
    if synced:
        for field in ("assigned_grade", "draft_grade"):
            if synced[field] is not None:
                return {"returned": True, "source": "submission_sync",
                        "reason": None, "value": synced[field], "field": field}
    return {"returned": False,
            "reason": (f"grade write {row['status']}" if row
                       else "no grade recorded in Classroom yet"),
            "op_type": row["op_type"] if row else None}
