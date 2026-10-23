"""Teacher report API — Google Classroom + Math Checker, kept strictly apart.

Endpoints (spec §§18-§21):

  GET /api/reports/assignment/{coursework_id}                JSON report data
  GET /api/reports/assignment/{coursework_id}/export?format=pdf
  GET /api/reports/class/{course_id}                         JSON report data
  GET /api/reports/class/{course_id}/export?format=pdf

Every figure is computed from verified local rows (``classroom_submissions``
= Google Classroom truth; ``results``/``correction_jobs`` = Math Checker
truth). Nothing is inferred: a missing grade stays "not graded", a missing
submission stays "not submitted", and a failed correction stays "failed" —
never a silent zero (§26).
"""

from __future__ import annotations

import io
import time
from datetime import datetime, timezone
from typing import Sequence
from xml.sax.saxutils import escape as _esc

from fastapi import APIRouter
from fastapi.responses import StreamingResponse

from .. import db
from ..downloads import attachment_header
from ..classroom import submissions as cs
from ..classroom.auth import account_email, auth_state
from ..errors import AppError
from ..logging_setup import log_event
from ..text import count as _count, verb as _verb
from ..submission_status import (
    GRADE_ONE as GRADE_ONE_STATUS,
    grade_of as row_grade_of,
    resolve as resolve_tracking,
)

router = APIRouter(prefix="/api")

def register(app) -> None:
    app.include_router(router)


def _fmt_ts(ts) -> str:
    if not ts:
        return "—"
    try:
        return datetime.fromtimestamp(float(ts), tz=timezone.utc).strftime("%Y-%m-%d %H:%M")
    except (TypeError, ValueError, OverflowError):
        return str(ts)


def _nickname(student_id: str) -> str | None:
    """Local nickname for a student (display only — never the Classroom name)."""
    from ..student_identity import resolve
    return resolve(student_id)["nickname"]


#: Students with no teacher-assigned class are grouped here, so they are
#: visible in a report instead of silently missing from it.
_UNASSIGNED = "Not assigned"


def _class_of(student_id: str) -> dict:
    """The teacher-assigned class for a student, as ``{name, label}``.

    A report must never show a blank class cell, so the label always reads
    something; grouping uses the raw name so "Not assigned" students stay
    together instead of scattering.
    """
    from ..student_identity import class_label, resolve
    name = resolve(student_id)["class_name"]
    return {"class_name": name, "class_label": class_label(name)}


def _display_name_for(row: dict) -> str:
    """The one canonical name for a student-bearing row.

    Replaces the old ``student_name or student_id`` / ``nickname or "—"``
    fallbacks, which is how an internal id or a dash ended up on a page.
    """
    from ..student_identity import resolve
    return resolve(row.get("student_id"),
                   submission_id=row.get("submission_id"),
                   snapshot_name=row.get("student_name"))["display_name"]


def _course(course_id: str) -> dict | None:
    row = db.query_one("SELECT * FROM courses WHERE id = ?", (course_id,))
    return dict(row) if row else None


def _assignment(coursework_id: str) -> dict | None:
    row = db.query_one("SELECT * FROM coursework WHERE id = ?", (coursework_id,))
    return dict(row) if row else None


# ---------------------------------------------------------------- row build

def _report_rows(course_id: str | None, coursework_id: str | None) -> list[dict]:
    """Student rows combining Classroom status + Math Checker status."""
    data = cs.tracking_rows(course_id=course_id, coursework_id=coursework_id)
    rows: list[dict] = []
    for r in data["rows"]:
        result = r.get("result")
        job = r.get("job")
        assigned = r.get("assigned_grade")
        max_points = r.get("max_points")
        classroom_graded = assigned is not None
        # classroom percentage only when BOTH sides are known (never fake 0)
        classroom_pct = None
        if classroom_graded and max_points:
            try:
                classroom_pct = round(float(assigned) / float(max_points) * 100, 1)
            except (TypeError, ValueError, ZeroDivisionError):
                classroom_pct = None

        # A partial correction is NOT a corrected paper: its stored fraction is
        # a share of the marked subset, so it must never be read as a score or
        # counted in an average.
        partial = (result or {}).get("partial") or None
        if result and partial:
            mc_status = "INCOMPLETE"
        elif result:
            mc_status = "CORRECTED"
        elif job and job.get("status") in (
                "QUEUED", "PREPARING", "DOWNLOADING", "UPLOADING", "MARKING",
                "VERIFYING", "SAVING", "WAITING_FOR_API", "RETRYING"):
            mc_status = "PROCESSING"
        elif job and job.get("status") == "FAILED":
            mc_status = "FAILED"
        elif job and job.get("status") == "CANCELLED":
            mc_status = "CANCELLED"
        else:
            mc_status = "NOT_PROCESSED"

        # One resolver decides what this row MEANS (§31-§35): reports,
        # dashboard and profiles all read the same tracking status.
        track = resolve_tracking(r["status"], row_grade_of({
            "mc_earned": r.get("mc_earned"),
            "result": result,
            "assigned_grade": assigned,
        }))

        rows.append({
            "submission_id": r["submission_id"],
            "student_id": r["student_id"],
            "coursework_id": r.get("coursework_id"),
            "student_name": r.get("student_name"),
            # Canonical: nickname -> Classroom name. Never an internal id.
            "display_name": _display_name_for(r),
            "nickname": _nickname(r["student_id"]),
            **_class_of(r["student_id"]),
            "student_email": r.get("student_email"),
            "status": r["status"],
            "status_label": r["status_label"],
            "tracking": track.status,
            "tracking_label": track.label,
            "tracking_original_status": track.original_status,
            "tracking_original_grade": track.original_grade,
            "outstanding": track.outstanding,
            "raw_state": r.get("raw_state"),
            "late": bool(r.get("late")),
            "creation_time": r.get("creation_time"),
            "update_time": r.get("update_time"),
            "attachments": len(r.get("attachments") or []),
            "attachment_names": [a.get("title") or a.get("id") or ""
                                 for a in (r.get("attachments") or [])],
            "assigned_grade": assigned,
            "max_points": max_points,
            "classroom_graded": classroom_graded,
            "classroom_percentage": classroom_pct,
            "draft_grade": r.get("draft_grade"),
            "mc_status": mc_status,
            "mc_earned": result.get("earned") if result else None,
            "mc_available": result.get("available") if result and not partial else None,
            "mc_percentage": (round(result.get("percentage", 0), 1)
                              if result and not partial else None),
            "mc_partial": partial,
            "job_status": job.get("status") if job else None,
            "job_error": job.get("error") if job else None,
            "data_quality": r.get("data_quality"),
            "stale": bool(r.get("stale")),
            "last_synced_at": r.get("last_synced_at"),
            "synthetic": bool(r.get("synthetic")),
        })
    return rows


def _stats(rows: list[dict]) -> dict:
    total = len(rows)
    status_counts: dict[str, int] = {}
    for r in rows:
        status_counts[r["status"]] = status_counts.get(r["status"], 0) + 1

    late = sum(1 for r in rows if r["late"])
    attachments = sum(1 for r in rows if r["attachments"] > 0)

    # --- submission statistics (Classroom) ---------------------------------
    submitted_statuses = {"SUBMITTED", "SUBMITTED_LATE", "RETURNED", "GRADED",
                          "CORRECTED", "PROCESSING", "FAILED", "INVALID",
                          "EDITED_AFTER_SUBMISSION"}
    not_submitted_statuses = {"NOT_STARTED", "CREATED", "MISSING", "RECLAIMED"}
    submitted = sum(1 for r in rows if r["status"] in submitted_statuses)
    not_submitted = sum(1 for r in rows if r["status"] in not_submitted_statuses)
    draft = sum(1 for r in rows if r["status"] == "CREATED")
    outstanding = sum(1 for r in rows if r.get("outstanding"))
    grade_one = sum(1 for r in rows if r.get("tracking") == GRADE_ONE_STATUS)
    graded = [r for r in rows if r["classroom_graded"]]
    ungraded = submitted - len(graded)

    classroom_scores = [r["classroom_percentage"] for r in graded
                        if r["classroom_percentage"] is not None]

    # --- Math Checker statistics -------------------------------------------
    corrected = [r for r in rows if r["mc_status"] == "CORRECTED"]
    incomplete = [r for r in rows if r["mc_status"] == "INCOMPLETE"]
    processing = sum(1 for r in rows if r["mc_status"] == "PROCESSING")
    failed = sum(1 for r in rows if r["mc_status"] == "FAILED")
    cancelled = sum(1 for r in rows if r["mc_status"] == "CANCELLED")
    invalid = status_counts.get("INVALID", 0)
    not_processed = sum(1 for r in rows if r["mc_status"] == "NOT_PROCESSED")
    mc_scores = [r["mc_percentage"] for r in corrected
                 if r["mc_percentage"] is not None]
    mc_abs = [(r["mc_earned"], r["mc_available"]) for r in corrected
              if r["mc_available"] is not None]

    def _avg(values: list[float]) -> float | None:
        return round(sum(values) / len(values), 1) if values else None

    mc_total_earned = sum(e for e, _ in mc_abs)
    mc_total_available = sum(a for _, a in mc_abs)

    return {
        "total_students": total,
        "status_counts": status_counts,
        "submission": {
            "submitted": submitted,
            "not_submitted": not_submitted,
            "outstanding": outstanding,
            "grade_one": grade_one,
            "draft": draft,
            "turned_in": status_counts.get("SUBMITTED", 0),
            "turned_in_late": late,
            "returned": status_counts.get("RETURNED", 0),
            "with_attachments": attachments,
        },
        "classroom_grading": {
            "graded": len(graded),
            "ungraded": max(0, ungraded),
            "average": _avg(classroom_scores),
            "highest": max(classroom_scores) if classroom_scores else None,
            "lowest": min(classroom_scores) if classroom_scores else None,
        },
        "math_checker": {
            "corrected": len(corrected),
            "incomplete": len(incomplete),
            "processing": processing,
            "failed": failed,
            "cancelled": cancelled,
            "invalid": invalid,
            "not_processed": not_processed,
            "average": _avg(mc_scores),
            "highest": max(mc_scores) if mc_scores else None,
            "lowest": min(mc_scores) if mc_scores else None,
            "total_earned": mc_total_earned,
            "total_available": mc_total_available,
            "overall_percentage": (round(mc_total_earned / mc_total_available * 100, 1)
                                   if mc_total_available else None),
        },
    }


# ---------------------------------------------------------------- endpoints

def _group_by_class(rows: list[dict]) -> list[dict]:
    """Split report rows into the classes the teacher assigned.

    The teacher-assigned class always wins. Students who have not been given
    one land in a single "Not assigned" group at the end, so they are visible
    rather than silently dropped from a printed report.
    """
    groups: list[dict] = []
    index: dict[str, dict] = {}
    for row in rows:
        key = row.get("class_name") or _UNASSIGNED
        group = index.get(key)
        if group is None:
            group = {"class_name": row.get("class_name"),
                     "class_label": row.get("class_label") or _UNASSIGNED,
                     "students": []}
            index[key] = group
            groups.append(group)
        group["students"].append(row)
    groups.sort(key=lambda g: ((g["class_label"] or _UNASSIGNED).lower()
                               == _UNASSIGNED.lower(),
                               (g["class_label"] or "").lower()))
    for group in groups:
        group["students"].sort(
            key=lambda r: (r.get("display_name") or r.get("student_name")
                           or "").lower())
    return groups


@router.get("/reports/assignment/{coursework_id}")
def assignment_report(coursework_id: str):
    cw = _assignment(coursework_id)
    if not cw:
        raise AppError("Assignment not found. Sync Google Classroom first.",
                       status=404, code="assignment_not_found")
    course = _course(cw["course_id"]) or {}
    rows = _report_rows(cw["course_id"], coursework_id)
    roster = db.query_one(
        "SELECT COUNT(DISTINCT student_id) AS n FROM classroom_submissions "
        "WHERE course_id = ?", (cw["course_id"],))
    groups = _group_by_class(rows)
    return {
        "generated_at": time.time(),
        "is_class_report": False,
        "class": {
            "course_id": cw["course_id"],
            "name": course.get("name") or "Unknown class",
            "section": course.get("section"),
            "teacher": account_email(),
            "connected": auth_state().connected,
            "students_total": roster["n"] if roster else 0,
        },
        "assignment": {
            "id": cw["id"],
            "title": cw["title"],
            "description": cw.get("description"),
            "created": cw.get("update_time"),
            "due_date": cw.get("due_date"),
            "max_points": cw.get("max_points"),
            "state": cw.get("state"),
            "allowlisted": bool(cw.get("allowlisted")),
        },
        "stats": _stats(rows),
        "rows": rows,
        "groups": groups,
    }


@router.get("/reports/class/{course_id}")
def class_report(course_id: str):
    course = _course(course_id)
    if not course:
        raise AppError("Class not found. Sync Google Classroom first.",
                       status=404, code="course_not_found")
    assignments = [dict(a) for a in db.query(
        "SELECT * FROM coursework WHERE course_id = ? ORDER BY update_time DESC",
        (course_id,))]
    rows = _report_rows(course_id, None)

    # Per-student aggregation (nickname + Classroom totals + MC totals).
    by_student: dict[str, dict] = {}
    for r in rows:
        entry = by_student.setdefault(r["student_id"], {
            "student_id": r["student_id"],
            "student_name": r["student_name"],
            "display_name": r["display_name"],
            "nickname": r["nickname"],
            "class_name": r.get("class_name"),
            "class_label": r.get("class_label"),
            "student_email": r["student_email"],
            "assignments": 0, "submitted": 0, "late": 0, "not_submitted": 0,
            "corrected": 0, "failed": 0, "incomplete": 0,
            "classroom_graded": 0, "classroom_scores": [],
            "mc_scores": [], "mc_earned": 0, "mc_available": 0,
        })
        entry["assignments"] += 1
        if r["status"] in {"SUBMITTED", "SUBMITTED_LATE", "RETURNED", "GRADED",
                           "CORRECTED", "PROCESSING", "FAILED", "INVALID",
                           "EDITED_AFTER_SUBMISSION"}:
            entry["submitted"] += 1
        if r["status"] in {"NOT_STARTED", "CREATED", "MISSING", "RECLAIMED"}:
            entry["not_submitted"] += 1
        if r["late"]:
            entry["late"] += 1
        if r["mc_status"] == "CORRECTED":
            entry["corrected"] += 1
        if r["mc_status"] == "INCOMPLETE":
            entry["incomplete"] += 1
        if r["mc_status"] == "FAILED":
            entry["failed"] += 1
        if r["classroom_graded"]:
            entry["classroom_graded"] += 1
            if r["classroom_percentage"] is not None:
                entry["classroom_scores"].append(r["classroom_percentage"])
        if r["mc_percentage"] is not None:
            entry["mc_scores"].append(r["mc_percentage"])
        if r["mc_earned"] is not None and r["mc_status"] == "CORRECTED":
            entry["mc_earned"] += r["mc_earned"] or 0
            entry["mc_available"] += r["mc_available"] or 0

    students = []
    # Sort by the teacher's class first (so the groups read in order), then by
    # display name. Students with no class sort last rather than first.
    for entry in sorted(
            by_student.values(),
            key=lambda e: ((e.get("class_label") or _UNASSIGNED).lower()
                           == _UNASSIGNED.lower(),
                           (e.get("class_label") or "").lower(),
                           (e["display_name"] or "").lower())):
        cs_scores = entry.pop("classroom_scores")
        mc_scores = entry.pop("mc_scores")
        entry["classroom_average"] = (round(sum(cs_scores) / len(cs_scores), 1)
                                      if cs_scores else None)
        entry["mc_average"] = (round(sum(mc_scores) / len(mc_scores), 1)
                               if mc_scores else None)
        entry["mc_total_earned"] = entry.pop("mc_earned")
        entry["mc_total_available"] = entry.pop("mc_available")
        students.append(entry)

    # Grouped by the TEACHER-ASSIGNED class, not by the Classroom course name.
    # This is what a teacher with several classes in one course actually needs:
    # "who in 10A is behind" rather than one undifferentiated list. Groups with
    # no assigned members are omitted, and unassigned students keep their own
    # group at the end instead of being dropped.
    groups: list[dict] = []
    index: dict[str, dict] = {}
    for entry in students:
        key = entry.get("class_name") or _UNASSIGNED
        group = index.get(key)
        if group is None:
            group = {"class_name": entry.get("class_name"),
                     "class_label": entry.get("class_label") or _UNASSIGNED,
                     "students": []}
            index[key] = group
            groups.append(group)
        group["students"].append(entry)

    assignment_summaries = []
    for a in assignments:
        a_rows = _report_rows(course_id, a["id"])
        assignment_summaries.append({
            "id": a["id"],
            "title": a["title"],
            "due_date": a.get("due_date"),
            "max_points": a.get("max_points"),
            "stats": _stats(a_rows),
        })

    return {
        "generated_at": time.time(),
        "is_class_report": True,
        "class": {
            "course_id": course_id,
            "name": course.get("name") or "Unknown class",
            "section": course.get("section"),
            "teacher": account_email(),
            "connected": auth_state().connected,
            "students_total": len(students),
            "assignments_total": len(assignments),
        },
        "stats": _stats(rows),
        "assignments": assignment_summaries,
        "students": students,
        "groups": groups,
    }


# ---------------------------------------------------------------- PDF render

def _table_style():
    from reportlab.lib import colors
    from reportlab.platypus import TableStyle
    return TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1F3864")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, -1), 7.5),
        ("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#B0B7C3")),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1),
         [colors.white, colors.HexColor("#EEF1F7")]),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 4),
        ("RIGHTPADDING", (0, 0), (-1, -1), 4),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
    ])


def _kv_table(items: list[tuple], styles):
    from reportlab.lib.units import mm
    from reportlab.platypus import Paragraph, Table
    rows = [[Paragraph(f"<b>{k}</b>", styles["BodyText"]),
             Paragraph(str(v), styles["BodyText"])] for k, v in items]
    table = Table(rows, colWidths=[52 * mm, 110 * mm])
    table.setStyle(_table_style())
    return table


def _status_icon(status: str) -> str:
    """Status as PDF-safe text.

    reportlab's built-in Helvetica only covers WinAnsi, so Unicode glyphs
    (✓ ⚠ ○ ✕ …) would print as black squares. Plain ASCII markers keep the
    PDF readable and still communicate status without relying on colour.
    """
    return {
        "SUBMITTED": "[ok] Turned in",
        "SUBMITTED_LATE": "[LATE] Turned in late",
        "RETURNED": "[ok] Returned",
        "GRADED": "[ok] Graded",
        "CORRECTED": "[ok] Corrected",
        "CREATED": "[draft] Opened, not submitted",
        "NOT_STARTED": "[open] Not submitted",
        "MISSING": "[MISSING] No submission",
        "RECLAIMED": "[reclaimed] Unsubmitted",
        "PROCESSING": "[...] Being corrected",
        "FAILED": "[FAILED] Correction failed",
        "INVALID": "[INVALID] Invalid submission",
        "EDITED_AFTER_SUBMISSION": "[edited] Edited after turn-in",
        "UNKNOWN": "[?] Unknown",
    }.get(status, status)


def _fmt_num(value) -> str:
    """17.0 -> '17', 17.5 -> '17.5' (grades must read cleanly)."""
    try:
        f = float(value)
    except (TypeError, ValueError):
        return str(value)
    return str(int(f)) if f == int(f) else str(f)


def _grade_text(earned, available) -> str:
    """Never render a missing value as 0 (§26)."""
    if earned is None or available is None:
        return "Not available"
    return f"{_fmt_num(earned)}/{_fmt_num(available)}"


def _assignment_pdf(data: dict) -> io.BytesIO:
    """Assignment report: identity, headline numbers, then the student table.

    The four stacked "key/value" tables this used to emit are now one scannable
    block plus a real headline strip, so the page answers "how did this
    assignment go?" before it answers "what happened to every student?".
    """
    from ..reporting import design as D

    cw = data["assignment"]
    cls = data["class"]
    st = data["stats"]
    brand = D.load_branding()
    theme = D.make_theme(accent=brand.accent or None, label="assignment")
    styles = D.build_styles(theme)
    story: list = []

    mc = st["math_checker"]
    cg = st["classroom_grading"]
    D.add(story, D.stat_strip(theme, styles, [
        ("Students", str(st["total_students"])),
        ("Submitted", str(st["submission"]["submitted"])),
        ("Corrected", str(mc["corrected"])),
        ("Math Correct average", D.fmt_pct(mc["average"])),
        ("Classroom average", D.fmt_pct(cg["average"])),
    ], tone="accent"))

    if mc["failed"] or mc.get("incomplete") or st["submission"]["not_submitted"]:
        lines = []
        if st["submission"]["not_submitted"]:
            n = st["submission"]["not_submitted"]
            lines.append(f"{_count(n, 'student')} {_verb(n, 'has', 'have')} "
                         "not submitted this assignment.")
        if mc["failed"]:
            n = mc["failed"]
            lines.append(f"{_count(n, 'correction')} {_verb(n, 'failed', 'failed')}"
                         f" and {_verb(n, 'needs', 'need')} a retry.")
        if mc.get("incomplete"):
            n = mc["incomplete"]
            lines.append(f"{_count(n, 'correction')} {_verb(n, 'is', 'are')} "
                         "incomplete: questions are still unmarked, so "
                         f"{_verb(n, 'its', 'their')} mark is excluded from "
                         "every average on this page.")
        D.add(story, D.Spacer(1, 4))
        D.add(story, D.callout(theme, styles, "Needs attention", lines,
                               tone="warn"))

    D.add(story, D.Spacer(1, 5))
    D.add(story, D.section(theme, styles, "Assignment"))
    D.add(story, D.key_values(theme, styles, [
        ("Assignment", cw["title"]),
        ("Class", cls["name"] + (f" - {cls['section']}"
                                 if cls.get("section") else "")),
        ("Description", (cw.get("description") or "Not available")[:400]),
        ("Due date", cw.get("due_date") or "Not available"),
        ("Maximum points", D.fmt_num(cw.get("max_points"))
         if cw.get("max_points") is not None else "Not available"),
        ("State", cw.get("state") or "Not available"),
        ("Created / updated", _fmt_ts(cw.get("created"))),
        ("Course ID", cls["course_id"]),
    ]))

    D.add(story, D.Spacer(1, 5))
    D.add(story, D.section(theme, styles, "Statistics"))
    D.add(story, D.key_values(theme, styles, [
        ("Submitted", str(st["submission"]["submitted"])),
        ("Not submitted", str(st["submission"]["not_submitted"])),
        ("Draft (opened, not submitted)", str(st["submission"]["draft"])),
        ("Turned in late", str(st["submission"]["turned_in_late"])),
        ("Returned", str(st["submission"]["returned"])),
        ("Corrected", str(mc["corrected"])),
        ("Processing", str(mc["processing"])),
        ("Failed", str(mc["failed"])),
        ("Invalid submissions", str(mc["invalid"])),
        ("Not processed", str(mc["not_processed"])),
        ("Highest / lowest", (f"{mc['highest']}% / {mc['lowest']}%"
                               if mc["highest"] is not None
                               else "Not available")),
        ("Graded in Classroom", str(cg["graded"])),
        ("Not graded", str(cg["ungraded"])),
        ("Classroom highest / lowest", (f"{cg['highest']}% / {cg['lowest']}%"
                                         if cg["highest"] is not None
                                         else "Not available")),
    ]))

    D.add(story, D.Spacer(1, 5))
    D.add(story, D.section(theme, styles, "Student results",
                           subtitle="Grouped by the class the teacher assigned."))
    D.add(story, _grouped_student_table(theme, styles, data,
                                       show_class=False,
                                       headers=["Student", "Status", "Late",
                                                "Classroom grade",
                                                "Math Correct", "%", "Correction"],
                                       weights=[24, 18, 7, 16, 17, 7, 11]))

    return _build_report(theme, brand, "Assignment Correction Report",
                         f"{cw['title']}  -  {cls['name']}", story,
                         data.get("generated_at"), label="TEACHER COPY",
                         doc_id=cw.get("id") or "")


def _class_pdf(data: dict) -> io.BytesIO:
    """Class performance report: an overview, then students grouped by class."""
    from ..reporting import design as D

    cls = data["class"]
    st = data["stats"]
    mc = st["math_checker"]
    students = data.get("students") or []
    groups = data.get("groups") or []

    scored = [s for s in students if s.get("mc_average") is not None]
    highest = max((s["mc_average"] for s in scored), default=None)
    lowest = min((s["mc_average"] for s in scored), default=None)

    brand = D.load_branding()
    theme = D.make_theme(accent=brand.accent or None, label="class")
    styles = D.build_styles(theme)
    story: list = []

    D.add(story, D.stat_strip(theme, styles, [
        ("Class average", D.fmt_pct(mc["average"])),
        ("Highest", D.fmt_pct(highest)),
        ("Lowest", D.fmt_pct(lowest)),
        ("Students assessed", str(len(students))),
        ("Assignments", str(cls.get("assignments_total", 0))),
    ], tone="accent", bar_for=0))

    attention = []
    if mc["failed"]:
        attention.append(f"{_count(mc['failed'], 'correction')} failed.")
    if mc.get("incomplete"):
        attention.append(
            f"{_count(mc['incomplete'], 'correction')} "
            f"{_verb(mc['incomplete'], 'is', 'are')} incomplete - questions are "
            "still unmarked, so those marks are excluded from every average.")
    if st["submission"]["not_submitted"]:
        n = st["submission"]["not_submitted"]
        attention.append(f"{_count(n, 'submission')} {_verb(n, 'was', 'were')} "
                         "never handed in.")
    ungraded = st["classroom_grading"]["ungraded"]
    if ungraded:
        attention.append(f"{_count(ungraded, 'student')} "
                         f"{_verb(ungraded, 'is', 'are')} still ungraded in "
                         "Classroom.")
    if attention:
        D.add(story, D.Spacer(1, 4))
        D.add(story, D.callout(theme, styles, "Needs attention", attention,
                               tone="warn"))

    D.add(story, D.Spacer(1, 5))
    D.add(story, D.section(theme, styles, "Class",
                           subtitle=(f"{cls.get('teacher') or 'Teacher not recorded'}"
                                     f"  ·  Course {cls['course_id']}")))
    D.add(story, D.key_values(theme, styles, [
        ("Students", str(cls.get("students_total", 0))),
        ("Assignments", str(cls.get("assignments_total", 0))),
        ("Submitted", str(st["submission"]["submitted"])),
        ("Not submitted", str(st["submission"]["not_submitted"])),
        ("Corrected", str(mc["corrected"])),
        ("Math Correct average", D.fmt_pct(mc["average"])),
    ]))

    if data.get("assignments"):
        D.add(story, D.Spacer(1, 5))
        D.add(story, D.section(theme, styles, "Assignments"))
        rows = []
        for a in data["assignments"]:
            s = a["stats"]
            rows.append([
                a["title"],
                str(a.get("due_date") or "—"),
                str(s["total_students"]),
                str(s["submission"]["submitted"]),
                str(s["math_checker"]["corrected"]),
                str(s["math_checker"]["failed"]),
                D.fmt_pct(s["classroom_grading"]["average"]),
                D.fmt_pct(s["math_checker"]["average"]),
            ])
        if not rows:
            rows = [["No assignments synced yet", "—", "0", "0", "0", "0",
                     "Not available", "Not available"]]
        D.add(story, D.data_table(
            theme, styles,
            ["Assignment", "Due", "Students", "Submitted", "Corrected",
             "Failed", "Classroom avg", "Math Correct avg"], rows,
            widths=D.fit_widths(theme, [30, 13, 8, 10, 9, 7, 11, 12])))

    D.add(story, D.Spacer(1, 5))
    D.add(story, D.section(theme, styles, "Student performance",
                           subtitle="Grouped by the class each student is assigned to."))
    D.add(story, _grouped_student_table(
        theme, styles, data, show_class=False,
        headers=["Student", "Assignments", "Submitted", "Missing", "Late",
                 "Corrected", "Classroom avg", "Math Checker avg", "MC score"],
        weights=[23, 10, 10, 9, 6, 9, 11, 12, 10]))

    return _build_report(theme, brand, "Class Performance Report",
                         cls["name"], story, data.get("generated_at"),
                         label="TEACHER COPY", doc_id=cls.get("course_id") or "")


def _grouped_student_table(theme, styles, data, *, show_class: bool = True,
                           headers, weights):
    """One table per teacher-assigned class, each with its own heading.

    Grouping by the class the teacher assigned is what makes a multi-class
    course readable; when nobody has been given a class this collapses to the
    single "Not assigned" group and behaves like the old flat table.

    The group heading already names the class, so ``show_class=False`` keeps
    a redundant per-row column out of every table here. It must then match the
    header list exactly — the check below turns a silent column shift into a
    loud failure.
    """
    from ..reporting import design as D

    groups = data.get("groups") or []
    if not groups:
        groups = [{"class_label": "All students", "students": data["students"]}]

    out: list = []
    for index, group in enumerate(groups):
        rows, tones = [], []
        for s in group["students"]:
            base = [s.get("display_name") or s.get("student_name") or "—"]
            if show_class:
                base.append(s.get("class_label") or "Not assigned")
            if data.get("is_class_report"):
                base += [str(s["assignments"]), str(s["submitted"]),
                         str(s["not_submitted"]), str(s["late"]),
                         str(s["corrected"]),
                         (f"{s['classroom_average']}%"
                          if s["classroom_average"] is not None
                          else "Not graded"),
                         (f"{s['mc_average']}%" if s["mc_average"] is not None
                          else ("Incomplete" if s.get("incomplete")
                                else "Not processed")),
                         (f"{s['mc_total_earned']}/{s['mc_total_available']}"
                          if s["mc_total_available"]
                          else ("Incomplete" if s.get("incomplete")
                                else "Not available"))]
            else:
                partial = s.get("mc_partial") or {}
                if partial:
                    # The mark exists but is not a score: name the coverage
                    # instead of printing a fraction that reads as one.
                    mc_text = (f"Incomplete - {partial.get('marked_questions', 0)}"
                               f" of {partial.get('expected_questions', 0)}"
                               " questions marked")
                else:
                    mc_text = D.fmt_score(s.get("mc_earned"), s.get("mc_available"))
                    if s.get("mc_percentage") is not None:
                        mc_text += f"  ({s['mc_percentage']}%)"
                base += [D.fmt_num(s.get("status_label") or "—"),
                         "yes" if s.get("late") else "no",
                         (f"{D.fmt_num(s['assigned_grade'])}/"
                          f"{D.fmt_num(s['max_points'])}"
                          if s.get("classroom_graded") else "Not graded"),
                         mc_text,
                         D.fmt_pct(s.get("mc_percentage")),
                         str(s.get("mc_status") or "—").replace("_", " ").title()]
            if len(base) != len(headers):
                raise AssertionError(
                    f"{len(base)} cells for {len(headers)} headers "
                    f"({headers}) — every label would be shifted.")
            rows.append(base)
            tones.append(D.status_tone(s.get("mc_status")))
        if not rows:
            rows = [["No students in this group"] + [""] * (len(headers) - 1)]
            tones = [None]
        if len(groups) > 1:
            if index:
                D.add(out, D.Spacer(1, 3))
            D.add(out, D.group_divider(
                theme, styles, group["class_label"],
                meta=_count(len(group["students"]), "student")))
        D.add(out, D.data_table(
            theme, styles, headers, rows,
            widths=D.fit_widths(theme, weights), row_tones=tones,
            aligns=["left"] + ["right"] * (len(headers) - 1)))
    return out


def _build_report(theme, brand, title, context, story, generated_at, *,
                  label, doc_id):
    """Hand a finished story to the design system and return the buffer."""
    from ..reporting import design as D
    report = D.Report(theme=theme, branding=brand, title=title, story=story,
                      context=context, label=label, eyebrow="Math Correct",
                      doc_id=(f"MC-{str(doc_id)[:10]}" if doc_id else ""))
    return report.build()


def render_result_pdf(result: dict, *, detail: str = "standard",
                      generated_at: float | None = None) -> io.BytesIO:
    """Single-correction PDF: "how did THIS student do on THIS assignment?".

    Built entirely from the shared design system, so it shares one identity
    with every other Math Correct export. Information is reorganised into a
    hierarchy, not removed:

    page 1  who + how they did (score, percentage, performance verdict)
            -> performance summary -> question breakdown table
    later   one expandable-style block per question, with the student's work,
            the mark scheme, the awarded mark points and the correction

    The canonical display name and the teacher's class come from the caller
    (``student_identity.decorate``); this function never looks a name up
    itself, so the PDF can never disagree with the page the teacher was
    looking at.
    """
    from ..reporting import design as D

    inner = result.get("result_json") or {}
    questions = inner.get("questions") or []
    verification = result.get("verification_json") or {}
    notes = inner.get("overall_notes") or {}
    detail = (detail or "standard").lower()

    assignment = None
    if result.get("coursework_id"):
        row = db.query_one("SELECT title FROM coursework WHERE id = ?",
                           (result["coursework_id"],))
        assignment = row["title"] if row else None

    def _mark(q: dict) -> tuple:
        maximum = q.get("max_marks") or 0
        awarded = q.get("awarded_marks") or 0
        return awarded, maximum

    full = sum(1 for q in questions
               if _mark(q)[0] == _mark(q)[1] and _mark(q)[1])
    zero = sum(1 for q in questions if _mark(q)[0] == 0)
    partial = len(questions) - full - zero
    review = sum(1 for q in questions if q.get("status") == "REVIEW")
    lost = sum(max(0, (q.get("max_marks") or 0) - (q.get("awarded_marks") or 0))
               for q in questions)

    from ..engine.verify import partial_summary
    partial_info = partial_summary(verification)

    student = str(result.get("display_name")
                  or result.get("student_name") or D.esc("Student"))
    class_label = str(result.get("class_label") or "Not assigned")

    brand = D.load_branding()
    theme = D.make_theme(accent=brand.accent or None, label="correction")
    styles = D.build_styles(theme)
    story: list = []

    # ---- headline: the number a teacher looks for first --------------------
    earned = result.get("earned")
    available = result.get("available")
    percentage = result.get("percentage")
    # A partial correction must never print a percentage that reads as a score
    # for the paper: the marked subset's fraction is not the student's result.
    D.add(story, D.stat_strip(theme, styles, [
        ("Marked only" if partial_info else "Score",
         D.fmt_score(earned, available)),
        ("Percentage", "—" if partial_info else D.fmt_pct(percentage)),
        ("Questions", str(len(questions)) if questions else "0"),
        ("Marks lost", D.fmt_num(lost)),
    ], tone="accent", bar_for=1))

    if partial_info:
        D.add(story, D.Spacer(1, 4))
        D.add(story, D.callout(theme, styles, "Incomplete correction", [
            f"Only {partial_info['marked_questions']} of "
            f"{partial_info['expected_questions']} questions have a mark "
            f"({partial_info['unresolved_questions']} are unmarked).",
            f"{partial_info['unresolved_marks']} of "
            f"{partial_info['paper_marks'] or '?'} marks were never assessed, "
            "so this is a DRAFT for review — not a result and not a grade.",
        ], tone="warn"))

    verdict = ("Incomplete" if partial_info
               else D.performance_word(percentage))
    verdict_tone = ("warn" if partial_info else
                    "ok" if percentage is not None and percentage >= 80
                    else "warn" if percentage is not None and percentage >= 50
                    else "mute")
    status = "Approved by teacher" if result.get("manual_override") else (
        "Needs review" if verification.get("status") == "REVIEW" else "Verified")
    status_tone = ("ok" if status == "Verified"
                   else "warn" if status == "Needs review" else "info")

    D.add(story, D.Spacer(1, 5))
    D.add(story, D.badges([(verdict, verdict_tone), (status, status_tone)],
                          theme))

    # ---- who and what -----------------------------------------------------
    D.add(story, D.Spacer(1, 5))
    D.add(story, D.section(theme, styles, "Student",
                           subtitle="Who this report is about"))
    meta = [
        ("Student", student),
        ("Class", class_label),
        ("Assignment", assignment or "Not recorded"),
        ("Date", D.esc(_fmt_ts(result.get("created_at")))),
        # NOTE: the AI model/provider that marked this work is deliberately NOT
        # printed. This PDF is student-facing, so it must never reveal which
        # model corrected the homework (§ report privacy). ``marking_model``
        # stays available in the database and the internal diagnostics/logs for
        # debugging and provider management - it is simply kept out of the
        # report the student receives.
    ]
    if result.get("classroom_name") and result.get("nickname"):
        # The teacher asked for the nickname; the official name stays visible
        # so the report can always be reconciled with Classroom.
        meta.append(("Classroom name", str(result["classroom_name"])))
    D.add(story, D.key_values(theme, styles, meta))

    if verification.get("anomalies"):
        D.add(story, D.Spacer(1, 4))
        D.add(story, D.callout(
            theme, styles, "This correction needs a second look",
            [str(a) for a in verification["anomalies"]], tone="warn",
            note="Verification flagged this result. Check it before the grade "
                 "is returned."))

    # ---- high-score double-check (§8/§19): never present the first result
    # as the final one. The row's earned/available/percentage ARE the final
    # result; the first/second raw corrections are reported separately.
    if result.get("double_check_triggered"):
        first = result.get("first_earned")
        first_max = result.get("first_available")
        second = result.get("second_earned")
        second_max = result.get("second_available")
        status = str(result.get("double_check_status") or "")
        review = bool(result.get("review_required"))
        tone = "warn" if (review or status == "REVIEW_REQUIRED") else (
            "ok" if status == "COMPLETE" else "warn")
        headline = {
            "COMPLETE": "Independently double-checked",
            "REVIEW_REQUIRED": "Double-check: REVIEW REQUIRED",
            "DOUBLE_CHECK_FAILED": "Double-check failed — first result kept",
        }.get(status, "Independently double-checked")
        rows = [("Status", headline)]
        if first is not None:
            rows.append(("First correction",
                         f"{D.fmt_score(first, first_max)} "
                         f"({D.fmt_pct(result.get('first_percentage'))})"))
        if second is not None:
            rows.append(("Second correction (independent)",
                         f"{D.fmt_score(second, second_max)} "
                         f"({D.fmt_pct(result.get('second_percentage'))})"))
        rows.append(("Final result",
                     f"{D.fmt_score(earned, available)} "
                     f"({D.fmt_pct(percentage)})"))
        if first is not None and second is not None:
            try:
                diff = int(first) - int(second)
                rows.append(("Difference", _count(diff, "mark")))
            except (TypeError, ValueError):
                pass
        if result.get("disagreement_status"):
            rows.append(("Agreement", str(result["disagreement_status"]).replace("_", " ").title()))
        reason = str(result.get("double_check_reason") or "")
        D.add(story, D.Spacer(1, 5))
        D.add(story, D.section(theme, styles, "Independent double-check",
                               subtitle="A second independent correction verified this score"))
        D.add(story, D.key_values(theme, styles, rows))
        diffs = result.get("double_check_question_differences") or []
        if diffs:
            bullets = [
                f"Question {d.get('question_number')}: first "
                f"{d.get('first_awarded')}/{d.get('max_marks')}, second "
                f"{d.get('second_awarded')}/{d.get('max_marks')}"
                + (f" — {d.get('second_loss_reason')}" if d.get("second_loss_reason") else "")
                for d in diffs[:12]
            ]
            D.add(story, D.Spacer(1, 3))
            D.add(story, D.callout(
                theme, styles, "Where the two corrections differed", bullets,
                tone="info",
                note="Only differences recorded by the corrections themselves "
                     "are listed."))
        if reason:
            D.add(story, D.Spacer(1, 3))
            D.add(story, D.callout(theme, styles, "Why", [reason], tone=tone))

    if notes.get("summary") or notes.get("strengths") or notes.get("revision_areas"):
        D.add(story, D.Spacer(1, 5))
        D.add(story, D.section(theme, styles, "Overview"))
        if notes.get("summary"):
            D.add(story, D.Paragraph(D.esc(notes["summary"]), styles["body"]))
            D.add(story, D.Spacer(1, 2))
        for label, key, tone in (("Strengths", "strengths", "ok"),
                                 ("What to revise", "revision_areas", "warn")):
            values = notes.get(key) or []
            if not values:
                continue
            D.add(story, D.subsection(theme, styles, label))
            D.add(story, D.bullets(theme, styles, [str(v) for v in values]))

    if detail == "summary":
        return _finish_pdf(theme, brand, student, assignment, story,
                           generated_at, result)

    # ---- question breakdown ----------------------------------------------
    D.add(story, D.Spacer(1, 5))
    D.add(story, D.section(
        theme, styles, "Question breakdown",
        subtitle=("Score and outcome for every question. Detailed marking "
                  "follows.")))
    if not questions:
        D.add(story, D.note(theme, styles,
                            "No question-level results were stored for this "
                            "correction."))
    else:
        rows, tones = [], []
        for q in questions:
            awarded, maximum = _mark(q)
            if q.get("status") == "REVIEW":
                state = "Unmarked - review"
            elif maximum and awarded == maximum:
                state = "Correct"
            elif awarded == 0:
                state = "No marks"
            else:
                state = "Partial"
            rows.append([f"Q{D.esc(q.get('question_number', '?'))}",
                         f"{D.fmt_num(awarded)} / {D.fmt_num(maximum)}",
                         state,
                         D.esc(q.get("loss_reason") or
                               (q.get("correct_method") or
                                "No comment recorded"))])
            tones.append(D.status_tone(state))
        D.add(story, D.data_table(
            theme, styles,
            ["Question", "Score", "Status", "Comment"], rows,
            widths=D.fit_widths(theme, [16, 14, 18, 52]),
            aligns=["left", "right", "left", "left"], row_tones=tones,
            emphasis=[1]))
        # The four counts are computed from the same rows the table prints,
        # so the line can never disagree with the table above it.
        tally = (f"{full} fully correct, {partial} with partial credit, "
                 f"{zero} with no marks")
        if review:
            tally += f" ({review} held for review)"
        D.add(story, D.Spacer(1, 1.6))
        D.add(story, D.note(theme, styles,
                            f"{tally}. {D.fmt_num(lost)} "
                            f"{'mark' if lost == 1 else 'marks'} lost in total "
                            "across "
                            f"{_count(len(questions), 'question')}."))

    # ---- one block per question, with the full marking detail --------------
    if detail == "detailed" and questions:
        D.add(story, D.Spacer(1, 5))
        D.add(story, D.section(
            theme, styles, "Marking detail",
            subtitle="Student work, mark scheme, awarded marks and correction."))
        for q in questions:
            D.add(story, _question_block(theme, styles, q))

    return _finish_pdf(theme, brand, student, assignment, story,
                       generated_at, result)


def _question_block(theme, styles, q: dict):
    """One question as a self-contained block, never split across pages."""
    from reportlab.platypus import KeepTogether, Paragraph, Spacer, Table, TableStyle

    from ..reporting import design as D

    awarded = q.get("awarded_marks") or 0
    maximum = q.get("max_marks") or 0
    if q.get("status") == "REVIEW":
        state, tone = "Unmarked - review", "warn"
    elif maximum and awarded == maximum:
        state, tone = "Correct", "ok"
    elif awarded == 0:
        state, tone = "No marks", "err"
    else:
        state, tone = "Partial", "warn"

    head = D._left(Table(
        [[Paragraph(f"Q{D.esc(q.get('question_number', '?'))}", styles["h3"]),
          Paragraph(f"<b>{D.fmt_num(awarded)} / {D.fmt_num(maximum)}</b> "
                    f"marks", styles["cell"])]],
        colWidths=[theme.width * 0.5, theme.width * 0.5]))
    head.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "BOTTOM"),
        ("ALIGN", (1, 0), (1, 0), "RIGHT"),
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 0),
        ("TOPPADDING", (0, 0), (-1, -1), 0),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
    ]))

    items = []
    if q.get("student_answer") or q.get("working_summary"):
        items.append(("Student work",
                      str(q.get("student_answer") or q.get("working_summary"))))
    if q.get("correct_method"):
        items.append(("Mark scheme", str(q["correct_method"])))
    for item in q.get("mark_breakdown") or []:
        mark = "awarded" if item.get("awarded") else "not awarded"
        items.append((f"Mark point {D.esc(item.get('code', ''))}",
                      f"{mark} - {item.get('reason', '')}".strip(" -")))
    if q.get("loss_reason"):
        items.append(("Where marks were lost", str(q["loss_reason"])))
    if q.get("full_mark_solution"):
        items.append(("Full-mark solution", str(q["full_mark_solution"])))

    block = [head, D.badges([(state, tone)], theme), Spacer(1, 2.2 * D.mm)]
    if items:
        block.append(D.key_values(theme, styles, items, columns=1))
    else:
        block.append(D.note(theme, styles,
                            "No mark-scheme detail was stored for this "
                            "question."))
    block.append(Spacer(1, 3.4 * D.mm))
    # KeepTogether so a question's heading is never stranded at a page foot.
    return KeepTogether(block)


def _finish_pdf(theme, brand, student, assignment, story, generated_at, result):
    """Hand the story to the design system and return the finished buffer."""
    from datetime import datetime

    from ..reporting import design as D

    context_bits = [b for b in (student, assignment) if b]
    report = D.Report(
        theme=theme, branding=brand,
        title="Student Correction Report",
        story=story,
        context="  -  ".join(context_bits),
        label="CORRECTION REPORT",
        eyebrow="Math Correct",
        doc_id=(f"MC-{result['id'][:8]}" if result.get("id") else ""),
        generated_at=(datetime.fromtimestamp(generated_at) if generated_at
                      else None),
    )
    return report.build()


@router.get("/reports/assignment/{coursework_id}/export")
def export_assignment_report(coursework_id: str, format: str = "pdf"):
    if (format or "pdf").lower() != "pdf":
        raise AppError("Only pdf export is available for reports. "
                       "Use the tracking export for CSV/XLSX/JSON.",
                       status=400, code="bad_report_format")
    data = assignment_report(coursework_id)
    buf = _assignment_pdf(data)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    title = "".join(c for c in data["assignment"]["title"]
                    if c.isalnum() or c in (" ", "-", "_")).strip().replace(" ", "_")
    log_event("reports", "assignment report exported",
              coursework_id=coursework_id)
    return StreamingResponse(
        buf, media_type="application/pdf",
        headers={"Content-Disposition":
                 attachment_header(f"assignment_{title or coursework_id}_{stamp}.pdf")})


@router.get("/reports/class/{course_id}/export")
def export_class_report(course_id: str, format: str = "pdf"):
    if (format or "pdf").lower() != "pdf":
        raise AppError("Only pdf export is available for reports. "
                       "Use the tracking export for CSV/XLSX/JSON.",
                       status=400, code="bad_report_format")
    data = class_report(course_id)
    buf = _class_pdf(data)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    title = "".join(c for c in data["class"]["name"]
                    if c.isalnum() or c in (" ", "-", "_")).strip().replace(" ", "_")
    log_event("reports", "class report exported", course_id=course_id)
    return StreamingResponse(
        buf, media_type="application/pdf",
        headers={"Content-Disposition":
                 attachment_header(f"class_{title or course_id}_{stamp}.pdf")})


# ---------------------------------------------------------------- class-report
# (§6-§12): a teacher-chosen subset of assignments, grouped by the
# teacher-assigned class, with separate missing-homework and grade-1 sections.

def _class_report_data(course_id: str, coursework_ids: Sequence[str]) -> dict:
    """Assemble the ClassReportData dict for the selected assignments.

    ``coursework_ids`` is already resolved (comma-separated or "all").
    """
    course_row = _course(course_id)
    if not course_row:
        raise AppError("Class not found. Sync Google Classroom first.",
                       status=404, code="course_not_found")
    rows = _report_rows(course_id, None)

    # Build the coursework list assemble() expects (§22).
    coursework_list = []
    for cid in coursework_ids:
        cw = _assignment(cid)
        if cw:
            coursework_list.append(dict(cw))
    course = {"id": course_row["id"], "name": course_row["name"],
              "courses": coursework_list}

    # Limit rows to the selected assignments so assemble() never sees stray
    # coursework_ids that were not requested (§22).
    wanted = [str(c) for c in coursework_ids if str(c).strip()]
    rows = [r for r in rows if str(r.get("coursework_id") or "") in wanted]

    from ..reporting.class_report import assemble
    data = assemble(course, rows, coursework_ids,
                    generated_at=time.time())
    return data.as_dict()


@router.get("/reports/class-report")
def class_report_json(class_id: str, assignments: str = "all"):
    """Class Report: teacher-selected assignments, grouped by class.

    ``assignments`` is a comma-separated list of coursework ids, or "all"
    to include every assignment in the class. The teacher explicitly controls
    what is included (§7); "all" is one option among several, never forced.
    """
    course = _course(class_id)
    if not course:
        raise AppError("Class not found. Sync Google Classroom first.",
                       status=404, code="course_not_found")

    coursework_ids: list[str]
    if (assignments or "").strip().lower() == "all":
        coursework_ids = [str(a["id"]) for a in db.query(
            "SELECT id FROM coursework WHERE course_id = ? ORDER BY update_time DESC",
            (class_id,))]
    else:
        coursework_ids = [s.strip() for s in assignments.split(",")
                          if s.strip()]
        if not coursework_ids:
            raise AppError("No assignments selected. Supply one or more "
                           "coursework ids, or 'all'.",
                           status=400, code="no_assignments_selected")
        # Reject ids that do not belong to this class (§22).
        owned = {str(a["id"]) for a in db.query(
            "SELECT id FROM coursework WHERE course_id = ?", (class_id,))}
        unknown = [cid for cid in coursework_ids if cid not in owned]
        if unknown:
            raise AppError(
                f"{_count(len(unknown), 'Assignment', 'Assignments')} not found "
                f"in this class: {', '.join(unknown)}",
                status=404, code="assignment_not_found")

    return _class_report_data(class_id, coursework_ids)


@router.get("/reports/class-report/export")
def class_report_export(class_id: str, assignments: str = "all",
                        mode: str = "summary", format: str = "pdf"):
    """Export the Class Report as an A4 portrait PDF (§4, §26).

    The PDF uses the shared design system: certificate frame, teacher
    letterhead, structured tables and signature block - the same visual
    language as every other Math Correct export (§27).

    ``mode=summary`` (the default) prints the consolidated missing-work list:
    each student named once beside their outstanding assignment codes.
    ``mode=detailed`` adds the per-assignment breakdown. No code path prints
    both at once, so a teacher never sees the same student twice.
    """
    if (format or "pdf").lower() != "pdf":
        raise AppError("Only pdf export is available for reports. "
                       "Use the tracking export for CSV/XLSX/JSON.",
                       status=400, code="bad_report_format")

    course = _course(class_id)
    if not course:
        raise AppError("Class not found. Sync Google Classroom first.",
                       status=404, code="course_not_found")

    coursework_ids: list[str]
    if (assignments or "").strip().lower() == "all":
        coursework_ids = [str(a["id"]) for a in db.query(
            "SELECT id FROM coursework WHERE course_id = ? ORDER BY update_time DESC",
            (class_id,))]
    else:
        coursework_ids = [s.strip() for s in assignments.split(",")
                          if s.strip()]
        if not coursework_ids:
            raise AppError("No assignments selected. Supply one or more "
                           "coursework ids, or 'all'.",
                           status=400, code="no_assignments_selected")
        owned = {str(a["id"]) for a in db.query(
            "SELECT id FROM coursework WHERE course_id = ?", (class_id,))}
        unknown = [cid for cid in coursework_ids if cid not in owned]
        if unknown:
            raise AppError(
                f"{_count(len(unknown), 'Assignment', 'Assignments')} not found "
                f"in this class: {', '.join(unknown)}",
                status=404, code="assignment_not_found")

    from ..reporting import design as D
    from ..reporting.class_report import assemble, render_pdf
    rows = _report_rows(class_id, None)

    # Build the coursework list assemble() expects (§22).
    coursework_list = []
    for cid in coursework_ids:
        cw = _assignment(cid)
        if cw:
            coursework_list.append(dict(cw))
    course = {"id": course["id"], "name": course["name"],
              "courses": coursework_list}

    wanted = [str(c) for c in coursework_ids if str(c).strip()]
    rows = [r for r in rows if str(r.get("coursework_id") or "") in wanted]

    data = assemble(course, rows, coursework_ids,
                    generated_at=time.time())
    brand = D.load_branding()
    buf = render_pdf(data, brand,
                     title="CLASS REPORT",
                     label="FOR SIGNATURE",
                     detailed=_parse_mode(mode),
                     focus_missing=False)

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    cname = "".join(c for c in (course.get("name") or "")
                     if c.isalnum() or c in (" ", "-", "_")).strip().replace(" ", "_")
    log_event("reports", "class report exported", class_id=class_id,
               assignment_count=len(coursework_ids))
    return StreamingResponse(
        buf, media_type="application/pdf",
        headers={"Content-Disposition":
                 attachment_header(f"class_report_{cname or class_id}_{stamp}.pdf")})


# ------------------------------------------------------- unified workspace
# (§17-§20, §47-§49): ONE report pipeline. The JSON preview and the PDF
# export are built from the SAME filters -> SAME model -> SAME renderer, so
# a teacher can never see one report on screen and a different one on paper.

REPORT_TYPES = ("class", "missing", "assignment", "student", "performance")

#: The teacher's low-grade threshold options (§24). The default lives in one
#: place only - ``reporting.class_report.DEFAULT_LOW_THRESHOLD``.
THRESHOLD_OPTIONS = (0, 1, 2, 3, 4, 5, 6, 7, 8, 9)


def _parse_threshold(raw: str | None) -> float | None:
    """``None`` disables the low-grade section; anything else is a number."""
    from ..reporting.class_report import DEFAULT_LOW_THRESHOLD
    text = str(raw if raw is not None else "").strip()
    if not text:
        return DEFAULT_LOW_THRESHOLD
    if text.lower() in {"off", "none", "disabled", "no"}:
        return None
    try:
        value = float(text)
    except ValueError:
        raise AppError("The low-grade threshold must be a number (or 'off').",
                       status=400, code="bad_threshold")
    if value < 0 or value > 100:
        raise AppError("The low-grade threshold must be between 0 and 100.",
                       status=400, code="bad_threshold")
    return value


def _all_coursework_ids(course_id: str | None) -> list[str]:
    if course_id:
        rows = db.query("SELECT id FROM coursework WHERE course_id = ? "
                        "ORDER BY update_time DESC", (course_id,))
    else:
        rows = db.query("SELECT id FROM coursework ORDER BY update_time DESC")
    return [str(r["id"]) for r in rows]


def _selected_ids(assignments: str | None, course_id: str | None) -> list[str]:
    """Resolve the teacher's assignment selection to real coursework ids.

    ``all`` includes every assignment in scope; anything else is a
    comma-separated list, and an id outside the scope is rejected loudly
    rather than silently dropped (§22, §46).
    """
    text = str(assignments or "all").strip()
    if text.lower() in {"all", "*"}:
        return _all_coursework_ids(course_id)
    wanted = [s.strip() for s in text.split(",") if s.strip()]
    if not wanted:
        raise AppError("No assignments selected. Choose one or more "
                       "assignments, or 'all'.",
                       status=400, code="no_assignments_selected")
    owned = set(_all_coursework_ids(course_id))
    unknown = [cid for cid in wanted if cid not in owned]
    if unknown:
        raise AppError(
            f"{_count(len(unknown), 'Assignment', 'Assignments')} not found "
            f"in this class: {', '.join(unknown)}",
            status=404, code="assignment_not_found")
    return wanted


def _parse_classes(classes: str | None) -> list[str] | None:
    """``None`` means every teacher-assigned class (§49)."""
    text = str(classes or "all").strip()
    if text.lower() in {"all", "*"}:
        return None
    labels = [s.strip() for s in text.split(",") if s.strip()]
    if not labels:
        return None
    return labels


def _classroom_note(labels: list[str] | None) -> list[dict]:
    """Students in a selected class with NO Google Classroom record (§19/§31).

    A teacher-assigned class whose student has never appeared in any
    Classroom sync is an administrative fact the report should state above
    the tables - never a row in the missing-homework table (the app has no
    evidence about their homework either way).  When there is no such
    student the note is empty and nothing is printed.
    """
    from ..student_identity import class_label, resolve

    rows = db.query(
        "SELECT id, name, nickname, class_name FROM students "
        "WHERE class_name IS NOT NULL AND TRIM(class_name) != '' "
        "AND id NOT IN (SELECT student_id FROM classroom_submissions "
        " WHERE student_id IS NOT NULL AND student_id != '')")
    wanted = {str(x) for x in labels} if labels else None
    note: list[dict] = []
    for row in rows:
        label = class_label(row["class_name"])
        if wanted is not None and label not in wanted:
            continue
        identity = resolve(row["id"], snapshot_name=row["name"])
        note.append({"student_id": row["id"],
                     "display_name": identity["display_name"],
                     "class_label": label})
    note.sort(key=lambda n: (n["class_label"], n["display_name"]))
    return note


def assemble_report(*, report_type: str = "class",
                    assignments: str = "all",
                    classes: str = "all",
                    course_id: str | None = None,
                    threshold: str | None = None,
                    statuses: str | None = None,
                    student_id: str | None = None):
    """Build the ONE report model every preview and export is derived from.

    Filters are applied here and nowhere else: the class filter, the
    assignment selection, the student scope and the low-grade threshold all
    resolve before a single row reaches the renderer.
    """
    from ..reporting.class_report import assemble

    rtype = (report_type or "class").lower()
    if rtype not in REPORT_TYPES:
        raise AppError(f"Unknown report type: {report_type}",
                       status=400, code="bad_report_type")
    coursework_ids = _selected_ids(assignments, course_id)
    wanted = set(coursework_ids)
    limit = _parse_threshold(threshold)
    labels = _parse_classes(classes)

    rows = [r for r in _report_rows(course_id, None)
            if str(r.get("coursework_id") or "") in wanted]
    if rtype == "student" or student_id:
        if not student_id:
            raise AppError("A student report needs a student.",
                           status=400, code="student_required")
        rows = [r for r in rows if r.get("student_id") == student_id]
        if not rows:
            raise AppError("That student has no tracked submissions for the "
                           "selected assignments.",
                           status=404, code="student_not_found")

    coursework_list = []
    for cid in coursework_ids:
        cw = _assignment(cid)
        if cw:
            coursework_list.append(dict(cw))
    course_row = _course(course_id) if course_id else None
    course = {
        "id": (course_row["id"] if course_row
               else (coursework_list[0]["course_id"]
                     if coursework_list else "all")),
        "name": (course_row["name"] if course_row else "All classes"),
        "courses": coursework_list,
    }
    return assemble(course, rows, coursework_ids, class_labels=labels,
                    generated_at=time.time(), threshold=limit,
                    classroom_note=_classroom_note(labels))


def _parse_statuses(raw: str | None) -> list[str]:
    """Comma-separated status filters -> the sections they select (§17)."""
    if not str(raw or "").strip():
        return []
    if str(raw).strip().lower() in {"all", "*"}:
        return ["missing", "draft", "low_grade", "submitted"]
    return [s.strip().lower() for s in str(raw).split(",") if s.strip()]


def _parse_mode(raw: str | None) -> bool:
    """Report layout -> is it the detailed one?

    ``summary`` is the default: "Missing only" really is only the missing
    information, one consolidated row per student. ``detailed`` keeps the
    older per-assignment breakdown for teachers who want the full audit.
    """
    return str(raw or "").strip().lower() in {"detailed", "full", "all"}


def _branding_dict() -> dict:
    from ..reporting import design as D
    brand = D.load_branding()
    return {
        "teacher_name": brand.teacher_name,
        "teacher_title": brand.teacher_title,
        "center": brand.center,
        "year": brand.year,
        "quote": brand.quote,
        "monogram": brand.monogram,
        "logo": bool(brand.logo_path),
    }


@router.get("/reports/options")
def reports_options():
    """Everything the Reports workspace needs to render its controls (§17)."""
    from ..reporting.class_report import DEFAULT_LOW_THRESHOLD, parse_assignment
    from ..student_identity import known_classes
    from ..classroom.auth import auth_state

    # Teacher-assigned classes actually in use, with their real head count.
    classes = []
    for label in known_classes():
        count = db.query_one(
            "SELECT COUNT(*) AS n FROM students WHERE class_name = ?",
            (label,))
        students = count["n"] if count else 0
        if students or label in {"10A", "10B", "10C"}:
            classes.append({"label": label, "students": students or 0})

    assignments = []
    for row in db.query(
            "SELECT w.id, w.course_id, w.title, w.max_points, w.due_date, "
            "c.name AS course_name FROM coursework w "
            "LEFT JOIN courses c ON c.id = w.course_id "
            "ORDER BY w.update_time DESC"):
        item = dict(row)
        code = parse_assignment(item.get("title"),
                                coursework_id=item.get("id"),
                                max_points=item.get("max_points"))
        assignments.append({
            "id": item["id"], "course_id": item["course_id"],
            "course_name": item.get("course_name"),
            "title": item["title"], "max_points": item.get("max_points"),
            "due_date": item.get("due_date"),
            "code": code.code, "chapter": code.chapter,
            "lesson": code.lesson, "topic": code.topic,
            "detail": code.detail, "label": code.label,
        })

    try:
        from ..engine.grading import active_boundaries, describe_boundaries
        boundaries = [[g, m] for g, m in active_boundaries()]
        scale = describe_boundaries(boundaries)
    except Exception:  # noqa: BLE001 - options must never fail
        boundaries, scale = [], ""

    return {
        "report_types": [
            {"value": "class", "label": "Class Report"},
            {"value": "missing", "label": "Missing Homework Report"},
            {"value": "assignment", "label": "Assignment Report"},
            {"value": "student", "label": "Student Report"},
            {"value": "performance", "label": "Performance / Grade Report"},
        ],
        "classes": classes,
        "assignments": assignments,
        "threshold_default": DEFAULT_LOW_THRESHOLD,
        "threshold_options": list(THRESHOLD_OPTIONS),
        "grade_scale": scale,
        "boundaries": boundaries,
        "branding": _branding_dict(),
        "connected": auth_state().connected,
        "statuses": [
            {"value": "missing", "label": "Missing / not turned in"},
            {"value": "draft", "label": "Draft / incomplete"},
            {"value": "low_grade", "label": "Below threshold"},
            {"value": "submitted", "label": "Handed in"},
        ],
        "modes": [
            {"value": "summary",
             "label": "Missing only - one line per student",
             "hint": "Student names grouped by class, with the missing "
                     "assignment codes beside them and nothing repeated."},
            {"value": "detailed",
             "label": "Detailed - per-assignment breakdown",
             "hint": "Adds a row per student per assignment, with chapter, "
                     "lesson and the recorded status."},
        ],
        "mode_default": "summary",
    }


@router.get("/reports/build")
def reports_build(report_type: str = "class", assignments: str = "all",
                  classes: str = "all", course_id: str | None = None,
                  threshold: str | None = None, statuses: str | None = None,
                  mode: str = "summary",
                  student_id: str | None = None):
    """The preview model: the same data the PDF is generated from (§45)."""
    from ..reporting.class_report import preview_model
    data = assemble_report(report_type=report_type, assignments=assignments,
                           classes=classes, course_id=course_id,
                           threshold=threshold, student_id=student_id)
    detailed = _parse_mode(mode)
    model = preview_model(data, report_type=report_type,
                          statuses=_parse_statuses(statuses),
                          detailed=detailed)
    model["branding"] = _branding_dict()
    model["generated_at"] = data.generated_at
    model["statuses"] = _parse_statuses(statuses)
    model["mode"] = "detailed" if detailed else "summary"
    return model


@router.get("/reports/build/export")
def reports_build_export(report_type: str = "class", assignments: str = "all",
                         classes: str = "all", course_id: str | None = None,
                         threshold: str | None = None,
                         statuses: str | None = None,
                         mode: str = "summary",
                         student_id: str | None = None,
                         format: str = "pdf"):
    """Export the selected report as an A4 portrait PDF (§4, §43)."""
    if (format or "pdf").lower() != "pdf":
        raise AppError("Only pdf export is available for reports. "
                       "Use the tracking export for CSV/XLSX/JSON.",
                       status=400, code="bad_report_format")
    from ..reporting import design as D
    from ..reporting.class_report import REPORT_TITLES, render_pdf

    rtype = (report_type or "class").lower()
    data = assemble_report(report_type=rtype, assignments=assignments,
                           classes=classes, course_id=course_id,
                           threshold=threshold, student_id=student_id)
    brand = D.load_branding()
    buf = render_pdf(data, brand,
                     title=REPORT_TITLES.get(rtype, "CLASS REPORT"),
                     label="FOR SIGNATURE", report_type=rtype,
                     statuses=_parse_statuses(statuses),
                     detailed=_parse_mode(mode))

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    slug = "".join(c for c in (data.course.get("name") or rtype)
                   if c.isalnum() or c in (" ", "-", "_")).strip()
    slug = slug.replace(" ", "_") or rtype
    log_event("reports", "report exported", report_type=rtype,
              assignment_count=len(data.codes),
              students=data.total_students)
    return StreamingResponse(
        buf, media_type="application/pdf",
        headers={"Content-Disposition":
                 attachment_header(f"{rtype}_{slug}_{stamp}.pdf")})
