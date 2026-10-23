"""Student profile API — a truthful personal dashboard per student.

Two data sources are kept strictly separated and always labelled:

  SOURCE: Google Classroom  — identity, roster, assignment submission states
  SOURCE: local corrections  — AI marks, totals, correction history

Submission counts are computed ONLY from verified Classroom states
(``classroom_submissions.current_state``). A local correction result can never
make a student look "submitted": when no Classroom tracking data exists the
profile says ``tracking_available: false`` instead of inventing a percentage.

Nicknames are LOCAL application data (``students.nickname``); the official
Classroom name (``students.name``) is never overwritten.
"""

from __future__ import annotations

import io
import time

from fastapi import APIRouter
from fastapi.responses import StreamingResponse

from .. import db
from ..downloads import attachment_header
from ..classroom import submissions as cs
from ..errors import AppError
from ..logging_setup import log_event
from ..submission_status import grade_of as row_grade_of, resolve as resolve_tracking
from ..student_identity import (
    MAX_CLASS_LENGTH,
    MAX_NICKNAME_LENGTH,
    class_label,
    known_classes,
    normalize_class_name,
    normalize_nickname,
)

router = APIRouter(prefix="/api")

SOURCE_CLASSROOM = "Google Classroom"
SOURCE_LOCAL = "Local correction database"
SOURCE_AI = "AI correction result"

# Statuses that mean work was actually handed in (Classroom-verified).
_SUBMITTED_STATUSES = {
    "SUBMITTED", "SUBMITTED_LATE", "RETURNED", "GRADED", "CORRECTED",
    "PROCESSING", "FAILED", "EDITED_AFTER_SUBMISSION",
}
_NOT_SUBMITTED_STATUSES = {"NOT_STARTED", "CREATED", "MISSING", "RECLAIMED"}


def _student_index() -> dict[str, dict]:
    """Known students from every local source (identity is Classroom-first)."""
    students: dict[str, dict] = {}

    for row in db.query("SELECT id, name, email, nickname, class_name "
                        "FROM students WHERE id IS NOT NULL"):
        students[row["id"]] = {
            "student_id": row["id"],
            "official_name": row["name"],
            "email": row["email"],
            "nickname": row["nickname"],
            "class_name": row["class_name"],
        }

    for row in db.query(
            "SELECT DISTINCT student_id, student_name, student_email "
            "FROM classroom_submissions WHERE student_id IS NOT NULL "
            "AND student_id != ''"):
        entry = students.setdefault(row["student_id"], {
            "student_id": row["student_id"],
            "official_name": None, "email": None, "nickname": None,
            "class_name": None,
        })
        entry["official_name"] = entry["official_name"] or row["student_name"]
        entry["email"] = entry["email"] or row["student_email"]

    for row in db.query(
            "SELECT DISTINCT student_id, student_name FROM results "
            "WHERE student_id IS NOT NULL AND student_id != ''"):
        entry = students.setdefault(row["student_id"], {
            "student_id": row["student_id"],
            "official_name": None, "email": None, "nickname": None,
            "class_name": None,
        })
        entry["official_name"] = entry["official_name"] or row["student_name"]

    return students


def _class_label(entry: dict, classroom_rows: list[dict] | None = None) -> str:
    """How a student's class READS, in a report or on screen.

    Priority, exactly as specified:

    1. the class the teacher assigned - it always wins;
    2. the Google Classroom course, but only as a fallback for a student who
       has not been given one (this app already presented the course as the
       student's class before the field existed, so showing it is honest, not
       an invention);
    3. "Not assigned" - never a blank cell.
    """
    from ..student_identity import UNASSIGNED_CLASS

    assigned = normalize_class_name(entry.get("class_name"))
    if assigned:
        return assigned
    course = _course_info(classroom_rows or [])
    if course:
        name = course.get("name")
        section = course.get("section")
        if name and section:
            return f"{name} - {section}"
        if name or section:
            return str(name or section)
    return UNASSIGNED_CLASS


def _display_name(entry: dict) -> str:
    """Nickname -> Classroom name. Delegates to the single canonical rule."""
    from ..student_identity import display_name as canonical
    return canonical(entry.get("nickname"), entry.get("official_name"),
                     fallback=entry.get("student_id"))


def _course_info(classroom_rows: list[dict]) -> dict | None:
    """Class/course for this student's tracked rows (Classroom source)."""
    course_id = next((r.get("course_id") for r in classroom_rows
                      if r.get("course_id")), None)
    if not course_id:
        return None
    row = db.query_one("SELECT name, section FROM courses WHERE id = ?",
                       (course_id,))
    return {
        "course_id": course_id,
        "name": (row["name"] if row else None),
        "section": (row["section"] if row else None),
    }


def _classroom_rows(student_id: str) -> list[dict]:
    """Per-assignment rows for one student, straight from the tracking view."""
    data = cs.tracking_rows()
    return [r for r in data["rows"] if r.get("student_id") == student_id]


def _profile(student_id: str) -> dict:
    students = _student_index()
    entry = students.get(student_id)
    if entry is None:
        raise AppError("Student not found. Run a Classroom sync first.",
                       status=404, code="student_not_found")

    classroom_rows = _classroom_rows(student_id)

    # --- submission states (SOURCE: Google Classroom) -----------------------
    counts: dict[str, int] = {}
    submitted = not_submitted = unknown = 0
    for row in classroom_rows:
        status = row.get("status") or "UNKNOWN"
        counts[status] = counts.get(status, 0) + 1
        if status in _SUBMITTED_STATUSES:
            submitted += 1
        elif status in _NOT_SUBMITTED_STATUSES:
            not_submitted += 1
        else:
            unknown += 1

    # --- corrections (SOURCE: local database) --------------------------------
    results = [dict(r) for r in db.query(
        "SELECT id, coursework_id, submission_id, earned, available, "
        "percentage, provisional_grade, marking_model, verification_json, "
        "review_required, manual_override, "
        "created_at FROM results WHERE student_id = ? ORDER BY created_at DESC",
        (student_id,))]
    for r in results:
        verification = db.loads(r.pop("verification_json"), {}) or {}
        # A teacher decision (approve/override) supersedes the engine's review
        # flag, so the profile reports the same state as the review queue (§S).
        override = bool(r.pop("manual_override", 0))
        flagged = bool(r.pop("review_required", 0))
        r["needs_review"] = flagged and not override
        r["status"] = ("APPROVED" if override
                       else ("REVIEW" if flagged else verification.get("status")))
        r["review_questions"] = verification.get("review_questions") or []

    jobs = [dict(r) for r in db.query(
        "SELECT id, status, coursework_id, created_at, finished_at, error "
        "FROM correction_jobs WHERE student_id = ? ORDER BY created_at DESC",
        (student_id,))]
    active_jobs = [j for j in jobs if j["status"] in (
        "QUEUED", "PREPARING", "DOWNLOADING", "UPLOADING", "MARKING",
        "VERIFYING", "SAVING", "WAITING_FOR_API", "RETRYING", "STARTING",
        "PROCESSING", "RETRYING")]
    failed_jobs = [j for j in jobs if j["status"] == "FAILED"]

    total_earned = sum(r["earned"] or 0 for r in results)
    total_available = sum(r["available"] or 0 for r in results)
    overall_pct = (total_earned / total_available * 100) if total_available else None

    tracking_available = len(classroom_rows) > 0
    submitted_pct = None
    if tracking_available and classroom_rows:
        submitted_pct = round(submitted / len(classroom_rows) * 100, 1)

    # --- performance (§9): every figure source-labelled, never inferred -----
    classroom_scores = []   # percentages from ASSIGNED classroom grades only
    zeros_classroom = 0     # an ACTUAL 0 the teacher/Classroom recorded
    not_graded = 0
    late_count = 0
    for row in classroom_rows:
        if row.get("late"):
            late_count += 1
        grade = row.get("assigned_grade")
        max_points = row.get("max_points")
        if grade is None:
            not_graded += 1
        elif max_points:
            pct = round(float(grade) / float(max_points) * 100, 1)
            classroom_scores.append(pct)
            if float(grade) == 0.0:
                zeros_classroom += 1
        elif float(grade) == 0.0:
            zeros_classroom += 1
            classroom_scores.append(0.0)

    mc_percentages = [r["percentage"] for r in results
                      if r.get("percentage") is not None]
    mc_zeros = sum(1 for r in results if (r.get("earned") or 0) == 0
                   and (r.get("available") or 0) > 0)
    missing_count = sum(
        1 for row in classroom_rows
        # One resolver for every surface (§31, §48): never handed in, a
        # draft, or a recorded grade of 0 all count as outstanding work.
        if resolve_tracking(row.get("status"), row_grade_of(row)).outstanding)
    corrected_count = sum(1 for row in classroom_rows
                          if (row.get("status") or "") == "CORRECTED")

    def _avg(values):
        return round(sum(values) / len(values), 1) if values else None

    performance = {
        "classroom_average": _avg(classroom_scores),
        "classroom_highest": max(classroom_scores) if classroom_scores else None,
        "classroom_lowest": min(classroom_scores) if classroom_scores else None,
        "classroom_graded": len(classroom_scores),
        "classroom_not_graded": not_graded,
        "classroom_zeros": zeros_classroom,
        "mc_average": _avg(mc_percentages),
        "mc_highest": max(mc_percentages) if mc_percentages else None,
        "mc_lowest": min(mc_percentages) if mc_percentages else None,
        "mc_zeros": mc_zeros,
        "total_earned": total_earned,
        "total_available": total_available,
        "overall_percentage": round(total_earned / total_available * 100, 1)
                              if total_available else None,
        "missing": missing_count,
        "late": late_count,
        "corrected": corrected_count,
    }

    behavior = {
        "on_time": sum(1 for row in classroom_rows
                       if (row.get("status") or "") in
                       ("SUBMITTED", "RETURNED", "GRADED", "CORRECTED",
                        "PROCESSING", "FAILED", "INVALID") and not row.get("late")),
        "late": late_count,
        "missing": missing_count,
        "draft": sum(1 for row in classroom_rows
                     if (row.get("status") or "") == "CREATED"),
        "turned_in": sum(1 for row in classroom_rows
                         if (row.get("status") or "") in
                         ("SUBMITTED", "SUBMITTED_LATE", "RETURNED", "GRADED",
                          "CORRECTED", "PROCESSING", "FAILED", "INVALID")),
        "returned": sum(1 for row in classroom_rows
                        if (row.get("status") or "") in ("RETURNED", "GRADED")),
    }

    return {
        **entry,
        "display_name": _display_name(entry),
        "class_label": _class_label(entry, classroom_rows),
        "course": _course_info(classroom_rows),
        "sources": {
            "identity": SOURCE_CLASSROOM if entry.get("official_name") else SOURCE_LOCAL,
            "submission_status": SOURCE_CLASSROOM if tracking_available else "Unavailable — run a Classroom sync",
            "scores": SOURCE_LOCAL if results else "No corrections yet",
            "correction_detail": SOURCE_AI if results else "No corrections yet",
            "nickname": "Local application data",
            "class_name": "Local application data",
        },
        "classroom": {
            "available": tracking_available,
            "assignments_total": len(classroom_rows),
            "submitted": submitted,
            "not_submitted": not_submitted,
            "unknown": unknown,
            "submitted_percentage": submitted_pct,
            "status_counts": counts,
            "assignments": [
                {
                    "submission_id": r["submission_id"],
                    "coursework_id": r["coursework_id"],
                    "title": r.get("coursework_title"),
                    "status": r["status"],
                    "status_label": r["status_label"],
                    # The desktop profile must filter/summarise with the SAME
                    # rules the statistics above use, so the decisions are
                    # computed here once (submission_status.resolve) instead of
                    # being re-guessed in the UI.
                    "submitted": (r.get("status") or "") in _SUBMITTED_STATUSES,
                    "outstanding": resolve_tracking(
                        r.get("status"), row_grade_of(r)).outstanding,
                    "returned": (r.get("status") or "") in ("RETURNED", "GRADED"),
                    "raw_state": r.get("raw_state"),
                    "late": r.get("late"),
                    "attachments": len(r.get("attachments") or []),
                    "assigned_grade": r.get("assigned_grade"),
                    "max_points": r.get("max_points"),
                    "due_date": r.get("due_date"),
                    "creation_time": r.get("creation_time"),
                    "update_time": r.get("update_time"),
                    "stale": r.get("stale"),
                    "data_quality": r.get("data_quality"),
                    "last_synced_at": r.get("last_synced_at"),
                    "result": r.get("result"),
                    "job": r.get("job"),
                }
                for r in classroom_rows
            ],
        },
        "performance": performance,
        "behavior": behavior,
        "corrections": {
            "total": len(results),
            "pending": len(active_jobs),
            "failed": len(failed_jobs),
            "total_earned": total_earned,
            "total_available": total_available,
            "overall_percentage": round(overall_pct, 1) if overall_pct is not None else None,
            "needs_review": sum(1 for r in results if r.get("needs_review")),
            # ``recent`` stays the small list the report and older clients use.
            # ``history`` is the full stored list so a profile can show every
            # correction instead of silently dropping everything past the 10th.
            "recent": results[:10],
            "history": results,
            "jobs": jobs[:10],
            "last_corrected_at": results[0]["created_at"] if results else None,
        },
        "fetched_at": time.time(),
    }


# ---------------------------------------------------------------- routes

def register(app) -> None:
    app.include_router(router)


@router.get("/students")
def list_students():
    """All known students with headline numbers from their real sources.

    Performance: ONE tracking pass + two aggregated queries for the whole
    list — the previous per-student rebuild of the full tracking view made
    this O(students × rows) (~1.7s with a real 77-student class).
    """
    by_student: dict[str, list[dict]] = {}
    for r in cs.tracking_rows()["rows"]:
        sid = r.get("student_id")
        if sid:
            by_student.setdefault(sid, []).append(r)

    result_stats = {r["student_id"]: r for r in db.query(
        "SELECT student_id, COUNT(*) AS n, SUM(earned) AS e, "
        "       SUM(available) AS a "
        "FROM results WHERE student_id IS NOT NULL AND student_id != '' "
        "GROUP BY student_id")}
    pending_counts = {r["student_id"]: r["n"] for r in db.query(
        "SELECT student_id, COUNT(*) AS n FROM correction_jobs "
        "WHERE student_id IS NOT NULL AND student_id != '' "
        "AND status IN ('QUEUED','PREPARING','DOWNLOADING','UPLOADING',"
        "'MARKING','VERIFYING','SAVING','WAITING_FOR_API','RETRYING','STARTING')"
        "GROUP BY student_id")}

    out = []
    for student_id, entry in sorted(
            _student_index().items(),
            key=lambda item: (_display_name(item[1]) or "").lower()):
        classroom_rows = by_student.get(student_id, [])
        submitted = sum(1 for r in classroom_rows
                        if r.get("status") in _SUBMITTED_STATUSES)
        stats = result_stats.get(student_id)
        out.append({
            **entry,
            "display_name": _display_name(entry),
            "class_label": _class_label(entry, classroom_rows),
            "assignments": len(classroom_rows),
            "submitted": submitted,
            "tracking_available": bool(classroom_rows),
            "corrections": (stats["n"] or 0) if stats else 0,
            "total_earned": (stats["e"] or 0) if stats else 0,
            "total_available": (stats["a"] or 0) if stats else 0,
            "pending": pending_counts.get(student_id, 0),
        })
    return {"students": out, "source": {
        "identity": SOURCE_CLASSROOM, "submission_status": SOURCE_CLASSROOM,
        "scores": SOURCE_LOCAL}}


@router.get("/students/classes")
def list_classes():
    """The class vocabulary for the UI: the teacher's own, then the usual set.

    The UI treats this as SUGGESTIONS - a teacher whose school says "11
    Computer Science" must be able to type it, so the input is never a locked
    dropdown (§4).
    """
    return {"classes": known_classes()}


@router.get("/students/{student_id}")
def student_profile(student_id: str):
    return _profile(student_id)


@router.put("/students/{student_id}/nickname")
def set_nickname(student_id: str, payload: dict):
    """Set (or clear) the LOCAL nickname. Never touches the Classroom name."""
    _save_details(student_id,
                  {"nickname": payload.get("nickname")},
                  event="nickname updated")
    return _profile(student_id)


@router.put("/students/{student_id}/details")
def set_student_details(student_id: str, payload: dict):
    """Save the teacher's Student Information: nickname AND class/grade.

    One endpoint and one Save button, because the two fields are the same kind
    of thing: local, teacher-defined display metadata that Google never sees
    and that no Classroom request depends on (§9).
    """
    updates: dict = {}
    for field in ("nickname", "class_name"):
        if field in payload:
            updates[field] = payload[field]
    if not updates:
        raise AppError("Send a nickname and/or a class_name.",
                       status=400, code="nothing_to_save")
    _save_details(student_id, updates, event="student details updated")
    return _profile(student_id)


def _save_details(student_id: str, updates: dict, *, event: str) -> None:
    """Persist nickname/class metadata, creating the profile row if needed.

    The Google Classroom name is only ever COPIED here, never overwritten: a
    student with no local row yet is seeded from the synced Classroom data,
    and an existing row keeps its own ``name``/``email`` untouched.
    """
    nickname = class_name = None
    if "nickname" in updates:
        raw = updates["nickname"]
        if isinstance(raw, (int, float)):
            raw = str(raw)
        if raw is not None and not isinstance(raw, str):
            raise AppError("Nickname must be text.", status=400,
                           code="nickname_invalid")
        if raw is not None and len(str(raw)) > MAX_NICKNAME_LENGTH + 20:
            raise AppError(f"Nicknames are limited to {MAX_NICKNAME_LENGTH} "
                           "characters.", status=400,
                           code="nickname_too_long")
        nickname = normalize_nickname(raw)
    if "class_name" in updates:
        raw = updates["class_name"]
        if isinstance(raw, (int, float)):
            raw = str(raw)
        if raw is not None and not isinstance(raw, str):
            raise AppError("Class must be text.", status=400,
                           code="class_invalid")
        if raw is not None and len(str(raw)) > MAX_CLASS_LENGTH + 20:
            raise AppError(f"Class names are limited to {MAX_CLASS_LENGTH} "
                           "characters.", status=400, code="class_too_long")
        class_name = normalize_class_name(raw)

    existing = db.query_one(
        "SELECT name, email FROM students WHERE id = ?", (student_id,))
    if existing is None:
        row = db.query_one(
            "SELECT student_name, student_email FROM classroom_submissions "
            "WHERE student_id = ? LIMIT 1", (student_id,))
        if row is None:
            # Never fabricate a student. The Classroom id is the authoritative
            # identity, so an id the app has never seen from Classroom has no
            # official name to attach metadata to — creating a nameless row
            # here would put a phantom student in every list and report.
            raise AppError("Student not found.", status=404,
                           code="student_not_found")
        db.execute(
            "INSERT INTO students(id, name, email, nickname, class_name, "
            "fetched_at) VALUES(?,?,?,?,?,?) "
            "ON CONFLICT(id) DO UPDATE SET "
            "  nickname=COALESCE(excluded.nickname, students.nickname), "
            "  class_name=COALESCE(excluded.class_name, students.class_name)",
            (student_id, row["student_name"] if row else None,
             row["student_email"] if row else None,
             nickname if "nickname" in updates else None,
             class_name if "class_name" in updates else None,
             time.time()),
        )
        if row is not None:
            # Seed the official name too, but never let this overwrite one.
            db.execute(
                "UPDATE students SET name = COALESCE(name, ?), "
                "email = COALESCE(email, ?) WHERE id = ?",
                (row["student_name"], row["student_email"], student_id))
    else:
        if "nickname" in updates:
            db.execute("UPDATE students SET nickname = ? WHERE id = ?",
                       (nickname, student_id))
        if "class_name" in updates:
            db.execute("UPDATE students SET class_name = ? WHERE id = ?",
                       (class_name, student_id))

    log_event("students", event, student_id=student_id)


@router.post("/students/bulk-details")
def bulk_set_details(payload: dict):
    """Assign a class (and optionally nicknames) to many students at once.

    Editing a hundred profiles one at a time is a spectacularly inefficient
    use of human life, so the Students page can select a cohort and send it
    here. Every id is a real Google Classroom student id; nothing is matched by
    name, so two students who share a nickname are still two separate rows.
    """
    ids = payload.get("student_ids")
    if not isinstance(ids, list) or not ids:
        raise AppError("Select at least one student.", status=400,
                       code="no_students_selected")
    ids = [str(i).strip() for i in ids if str(i or "").strip()]
    if len(ids) > 2000:
        raise AppError("Too many students selected at once (max 2000).",
                       status=400, code="too_many_students")

    class_name = None
    if "class_name" in payload:
        class_name = normalize_class_name(payload.get("class_name"))

    # Optional per-student nicknames: {student_id: nickname}.
    nicknames = payload.get("nicknames")
    nickname_map: dict[str, str | None] = {}
    if isinstance(nicknames, dict):
        nickname_map = {str(k): v for k, v in nicknames.items()}

    known = _student_index()
    updated, skipped = [], []
    for student_id in ids:
        entry = known.get(student_id)
        has_nickname = student_id in nickname_map
        if entry is None and not has_nickname and class_name is None:
            skipped.append(student_id)
            continue
        try:
            updates: dict = {}
            if class_name is not None or "class_name" in payload:
                updates["class_name"] = class_name
            if has_nickname:
                updates["nickname"] = nickname_map[student_id]
            _save_details(student_id, updates, event="bulk student update")
            updated.append(student_id)
        except AppError as exc:
            skipped.append(student_id)
            log_event("students", "bulk update skipped", student_id=student_id,
                      reason=exc.code)

    log_event("students", "bulk student update finished",
              requested=len(ids), updated=len(updated), skipped=len(skipped))
    return {"updated": updated, "skipped": skipped,
            "class_name": class_name,
            "profiles": [_profile(s) for s in updated]}


# ------------------------------------------------------- per-student report

def _student_report_data(student_id: str) -> dict:
    """Combine the profile into a JSON-safe shape used by PDF/CSV rendering."""
    profile = _profile(student_id)
    identity = {
        "student_id": profile.get("student_id"),
        "official_name": profile.get("official_name") or profile.get("display_name"),
        "nickname": profile.get("nickname"),
        "email": profile.get("email"),
        "display_name": profile.get("display_name"),
    }
    classroom = profile.get("classroom") or {}
    corrections = profile.get("corrections") or {}
    return {
        "identity": identity,
        "classroom": {
            "available": classroom.get("available", False),
            "assignments_total": classroom.get("assignments_total", 0),
            "submitted": classroom.get("submitted", 0),
            "not_submitted": classroom.get("not_submitted", 0),
            "unknown": classroom.get("unknown", 0),
            "submitted_percentage": classroom.get("submitted_percentage"),
            "assignments": classroom.get("assignments", []),
        },
        "corrections": {
            "total": corrections.get("total", 0),
            "pending": corrections.get("pending", 0),
            "failed": corrections.get("failed", 0),
            "needs_review": corrections.get("needs_review", 0),
            "total_earned": corrections.get("total_earned", 0),
            "total_available": corrections.get("total_available", 0),
            "overall_percentage": corrections.get("overall_percentage"),
            "last_corrected_at": corrections.get("last_corrected_at"),
            "recent": corrections.get("recent", []),
        },
    }


def _fmt_report_ts(ts) -> str:
    if not ts:
        return "—"
    try:
        from datetime import datetime, timezone
        return datetime.fromtimestamp(float(ts), tz=timezone.utc).strftime("%Y-%m-%d %H:%M")
    except (TypeError, ValueError, OverflowError):
        return str(ts)



def _render_student_report_pdf(data: dict, stamp: str):
    """Professional, single-student tracking PDF (reportlab)."""
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.lib.units import mm
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table

    ident = data["identity"]
    classroom = data["classroom"]
    corr = data["corrections"]

    import io
    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=A4,
        leftMargin=14 * mm, rightMargin=14 * mm,
        topMargin=14 * mm, bottomMargin=14 * mm,
        title=f"{ident.get('display_name') or 'Student'} — Tracking Report")

    styles = getSampleStyleSheet()
    identity_lines = [
        ("Student", ident.get("display_name") or "—"),
        ("Classroom name", ident.get("official_name") or "—"),
        ("Nickname", ident.get("nickname") or "—"),
        ("Email", ident.get("email") or "—"),
    ]
    score = f"{corr.get('total_earned', 0)}/{corr.get('total_available', 0)}"
    if corr.get("overall_percentage") is not None:
        score += f" ({corr['overall_percentage']}%)"
    overview_lines = [
        ("Assignments tracked", classroom.get("assignments_total", 0)),
        ("Submitted", classroom.get("submitted", 0)),
        ("Not submitted", classroom.get("not_submitted", 0)),
        ("Corrections (Math Checker)", corr.get("total", 0)),
        ("Overall score", score),
        ("Pending / Failed / Review",
         f"{corr.get('pending', 0)} / {corr.get('failed', 0)} / {corr.get('needs_review', 0)}"),
    ]

    def info_table(items):
        t = Table(
            [[Paragraph(f"<b>{k}</b>", styles["BodyText"]),
              Paragraph(str(v), styles["BodyText"])] for k, v in items],
            colWidths=[42 * mm, 120 * mm])
        t.setStyle(_report_table_style())
        return t

    a_head = ["Assignment", "Status", "Late", "Classroom grade",
              "Math Checker", "Quality", "Synced"]
    a_rows = [a_head]
    for a in classroom.get("assignments", []):
        result = a.get("result") or {}
        mc = ""
        if result:
            mc = f"{result.get('earned')}/{result.get('available')} ({result.get('percentage', 0):g}%)"
        a_rows.append([
            str(a.get("title") or a.get("coursework_id") or "—"),
            str(a.get("status_label") or a.get("status") or "—"),
            "yes" if a.get("late") else "no",
            (f"{a.get('assigned_grade')}/{a.get('max_points')}" if a.get("assigned_grade") is not None else "—"),
            mc or "—",
            str(a.get("data_quality") or "—"),
            _fmt_report_ts(a.get("last_synced_at")),
        ])
    a_table = Table(a_rows, repeatRows=1, colWidths=[62 * mm, 34 * mm, 14 * mm, 28 * mm, 28 * mm, 20 * mm, 26 * mm])
    a_table.setStyle(_report_table_style())

    # NOTE: the AI model that marked each correction is intentionally NOT
    # listed here. This report may be handed to a student, so it must not
    # reveal which model corrected the work; ``marking_model`` remains in the
    # database and internal diagnostics for debugging and provider management.
    r_head = ["Date", "Score", "Status"]
    r_rows = [r_head]
    for res in corr.get("recent", []):
        r_rows.append([
            _fmt_report_ts(res.get("created_at")),
            f"{res.get('earned')}/{res.get('available')} ({res.get('percentage', 0):g}%)"
            if res.get('available') else "—",
            str(res.get("status") or "done"),
        ])
    r_table = Table(r_rows, repeatRows=1)
    r_table.setStyle(_report_table_style())

    story = [
        Paragraph("Student Tracking Report", styles["Title"]), Spacer(1, 6),
        info_table(identity_lines), Spacer(1, 8),
        info_table(overview_lines), Spacer(1, 12),
        Paragraph("Assignment history", styles["Heading2"]), Spacer(1, 4),
        a_table,
    ]
    if len(r_rows) > 1:
        story += [Spacer(1, 12), Paragraph("Recent corrections", styles["Heading2"]),
                  Spacer(1, 4), r_table]
    doc.build(story)
    buf.seek(0)
    return buf


def _report_table_style():
    from reportlab.lib import colors
    from reportlab.platypus import TableStyle
    return TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1F3864")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, -1), 8),
        ("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#B0B7C3")),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1),
         [colors.white, colors.HexColor("#EEF1F7")]),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
    ])



@router.get("/students/{student_id}/report")
def student_report(student_id: str, format: str = "pdf"):
    """Export a single student's tracking report (PDF or CSV) (§13/§14)."""
    from datetime import datetime, timezone

    data = _student_report_data(student_id)
    ident = data["identity"]
    stamp = datetime.now(tz=timezone.utc).strftime("%Y%m%d_%H%M%S")
    safe_name = "".join(c for c in str(ident.get("display_name") or "student")
                        if c.isalnum() or c in (" ", "-", "_")).strip() or "student"
    safe_name = safe_name.replace(" ", "_")

    fmt = (format or "pdf").lower()
    if fmt == "pdf":
        buf = _render_student_report_pdf(data, stamp)
        return StreamingResponse(
            buf, media_type="application/pdf",
            headers={"Content-Disposition":
                     attachment_header(f"student_{safe_name}_{stamp}.pdf")})

    if fmt == "csv":
        import csv as _csv
        buf = io.StringIO()
        lines = [["student", "official_name", "nickname", "email",
                  "assignment", "status", "late", "assigned_grade",
                  "max_points", "v5_earned", "v5_available", "quality",
                  "last_synced"]]
        for a in data["classroom"].get("assignments", []):
            result = a.get("result") or {}
            lines.append([
                str(ident.get("display_name") or ""),
                str(ident.get("official_name") or ""),
                str(ident.get("nickname") or ""),
                str(ident.get("email") or ""),
                str(a.get("title") or a.get("coursework_id") or ""),
                str(a.get("status_label") or a.get("status") or ""),
                "yes" if a.get("late") else "no",
                "" if a.get("assigned_grade") is None else a["assigned_grade"],
                "" if a.get("max_points") is None else a["max_points"],
                "" if not result else result.get("earned"),
                "" if not result else result.get("available"),
                str(a.get("data_quality") or ""),
                _fmt_report_ts(a.get("last_synced_at")),
            ])
        writer = _csv.writer(buf)
        writer.writerows(lines)
        return StreamingResponse(
            iter([buf.getvalue()]), media_type="text/csv",
            headers={"Content-Disposition":
                     attachment_header(f"student_{safe_name}_{stamp}.csv")})

    raise AppError(f"Unknown report format: {format}. Use pdf or csv.",
                   status=400, code="bad_report_format")


@router.get("/students/{student_id}/assignments/{coursework_id}/report")
def student_assignment_report(student_id: str, coursework_id: str,
                              format: str = "pdf", detail: str = "detailed"):
    """ONE student + ONE assignment detailed correction report (Part I).

    Reads the stored correction from the database (offline — never re-calls
    the AI) and renders it. Returns 404 with a plain message when no stored
    correction exists yet, rather than producing an empty report."""
    from datetime import datetime, timezone

    fmt = (format or "pdf").lower()
    if fmt != "pdf":
        raise AppError("Only PDF is available for a student assignment report.",
                       status=400, code="bad_report_format")
    if detail not in ("summary", "standard", "detailed"):
        raise AppError("detail must be summary, standard or detailed.",
                       status=400, code="bad_detail")

    # Locate the stored correction: current result for this student + assignment
    # via the verified submission id, falling back to the student_id column.
    row = db.query_one(
        "SELECT r.* FROM results r "
        "JOIN classroom_submissions cs ON cs.submission_id = r.submission_id "
        "WHERE r.coursework_id = ? AND cs.student_id = ? "
        "ORDER BY r.created_at DESC LIMIT 1", (coursework_id, student_id))
    if not row:
        row = db.query_one(
            "SELECT * FROM results WHERE coursework_id = ? AND student_id = ? "
            "ORDER BY created_at DESC LIMIT 1", (coursework_id, student_id))
    if not row:
        raise AppError(
            "No stored correction was found for this student and assignment yet.",
            status=404, code="no_correction")

    result = dict(row)
    result["result_json"] = db.loads(result.get("result_json"), {})
    result["verification_json"] = db.loads(result.get("verification_json"), {})

    from .report_routes import render_result_pdf
    buf = render_result_pdf(result, detail=detail)
    stamp = datetime.now(tz=timezone.utc).strftime("%Y%m%d_%H%M%S")
    safe_base = "".join(
        c for c in f"{(result.get('student_name') or 'student')}_{coursework_id}"
        if c.isalnum() or c in (" ", "-", "_")).strip().replace(" ", "_")
    log_event("reports", "student assignment report exported",
              student_id=student_id, coursework_id=coursework_id, detail=detail)
    return StreamingResponse(
        buf, media_type="application/pdf",
        headers={"Content-Disposition":
                 attachment_header(f"{safe_base or 'report'}_{stamp}.pdf")})

