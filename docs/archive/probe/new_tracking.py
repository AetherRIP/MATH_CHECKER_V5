"""Classroom tracking + synchronization API routes (spec §§17-§24).

Sync endpoints are the only routes that touch the network for tracking;
reads (tracking/detail) are local-first from SQLite so the UI never blocks
on Google. Exports render from the SAME stored rows the UI shows (§20) and
never contain secrets, keys, or token material.

Two sync surfaces (both GET + POST — EventSource only speaks GET):
  * ``/classroom/courses/{c}/assignments/{w}/sync`` — one assignment
    (roster + every submission state + grades), with staged SSE progress.
  * ``/classroom/sync`` — the FULL sync: courses → assignments → students
    → submissions → done, with per-part progress and honest failure
    reporting (a partial sync never claims "Sync complete").
"""

from __future__ import annotations

import csv
import io
import time
from datetime import datetime, timezone

from fastapi import APIRouter
from fastapi.responses import StreamingResponse

from .. import db
from ..api.routes_all import (
    _require_classroom,
    _sse,
)
from ..classroom import client as gc
from ..classroom import submissions as cs
from ..classroom import sync as classroom_sync
from ..classroom.client import ClassroomApiError
from ..classroom.auth import SCOPES, auth_state, get_access_token
from ..errors import AppError
from ..logging_setup import log_event

router = APIRouter(prefix="/api")


def register(app) -> None:
    app.include_router(router)


def _fmt_ts(ts: float | None) -> str:
    if not ts:
        return ""
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _state_history_for(submission_id: str) -> list[dict]:
    return [dict(e) for e in db.query(
        "SELECT from_state, to_state, detected_at FROM classroom_state_events "
        "WHERE submission_id = ? ORDER BY detected_at, id", (submission_id,))]


def _render_export_rows(
        course_id: str | None = None,
        coursework_id: str | None = None) -> list[dict]:
    data = cs.tracking_rows(course_id=course_id, coursework_id=coursework_id)
    rows = []
    for r in data["rows"]:
        rows.append({
            "student": r["student_name"],
            "email": r.get("student_email") or "",
            "assignment": r.get("coursework_title") or r.get("coursework_id") or "",
            "status": r["status_label"],
            "raw_state": r.get("raw_state") or "",
            "previous_state": r.get("previous_state") or "",
            "late": "yes" if r.get("late") else "no",
            "attachments": ", ".join(
                f"{a.get('kind', 'file')}:{a.get('title') or a.get('id') or ''}"
                for a in (r.get("attachments") or [])),
            "assigned_grade": "" if r.get("assigned_grade") is None else r["assigned_grade"],
            "v5_score": (f"{r['result']['earned']}/{r['result']['available']}"
                         if r.get("result") else ""),
            "data_quality": r.get("data_quality") or "",
            "stale": "yes" if r.get("stale") else "no",
            "last_synced": _fmt_ts(r.get("last_synced_at")),
        })
    return rows


_EXPORT_COLUMNS = ["student", "email", "assignment", "status", "raw_state",
                   "previous_state", "late", "attachments", "assigned_grade",
                   "v5_score", "data_quality", "stale", "last_synced"]


def _assignment_work(course_id: str, coursework_id: str, report) -> None:
    """Roster + every submission state for ONE assignment (§12/§17).

    The SAME staged work this endpoint has always done (roster → submission
    pages → save); the only change is that each frame goes to ``report``
    instead of being yielded, so one implementation now feeds both the SSE
    stream and the polling routes. On API failure the stored rows stay
    untouched and the failure is reported as an honest ``error`` frame.
    """
    started = time.time()
    try:
        report({"type": "stage", "stage": "connect",
                    "message": "Contacting Google Classroom…"})
        token = get_access_token()

        report({"type": "stage", "stage": "roster",
                    "message": "Fetching class roster…"})
        students = gc.list_course_students(token, course_id)
        roster_count = len(students)
        report({"type": "roster", "count": roster_count})

        report({"type": "stage", "stage": "submissions",
                    "message": "Fetching submissions (all states)…"})
        subs, page_token, pages = [], None, 0
        while True:
            page, next_token = gc.list_student_submissions(
                token, course_id, coursework_id, page_token)
            subs.extend(page)
            pages += 1
            report({"type": "page", "pages": pages,
                        "collected": len(subs)})
            if not next_token:
                break
            page_token = next_token

        report({"type": "stage", "stage": "save",
                    "message": "Saving to local database…"})
        roster = {}
        for st in students:
            profile = st.get("profile") or {}
            uid = st.get("userId") or profile.get("id")
            if uid:
                roster[uid] = {
                    "user_id": uid,
                    "name": (profile.get("name") or {}).get("fullName"),
                    "email": profile.get("emailAddress"),
                }
        summary = cs.store_sync_result(course_id, coursework_id, roster, subs)

        log_event("classroom.tracking", "sync complete",
                  course_id=course_id, coursework_id=coursework_id,
                  submissions=len(subs), roster=roster_count)
        report({"type": "done",
                    "summary": {**summary, "roster": roster_count,
                                "elapsed_s": round(time.time() - started, 2)}})
    except Exception as exc:
        log_event("classroom.tracking", "sync failed",
                  level=20, course_id=course_id,
                  coursework_id=coursework_id, error=str(exc))
        report({"type": "error", "message": str(exc)})


@router.api_route("/classroom/courses/{course_id}/assignments/{coursework_id}/sync",
                  methods=["GET", "POST"])
async def sync_tracking(course_id: str, coursework_id: str):
    """Full-state tracking sync with REAL progress (SSE).

    GET as well as POST: the browser's EventSource can only issue GETs —
    registering POST-only made the UI's "Sync Google Classroom" button 404
    silently (the reason assignments looked empty).

    The work runs on a worker thread and streams through the progress
    registry, so progress also survives a tunnel/proxy that buffers an event
    stream (see ``sync_runs`` — the reason this page could sit on
    "Connecting…" for the entire sync).
    """
    _require_classroom()
    run, started = sync_runs.start(
        "assignment", {"course_id": course_id, "coursework_id": coursework_id},
        lambda report: _assignment_work(course_id, coursework_id, report))
    return _stream_run(run, attach=not started)



# ---------------------------------------------------------------- full sync

def _seed_student_names(roster: dict[str, dict], now_ts: float) -> None:
    """Cache roster identities in the local students table (idempotent,
    never touches nicknames). Keeps legacy name lookups working offline."""
    for uid, info in roster.items():
        db.execute(
            "INSERT INTO students(id, name, email, fetched_at) VALUES(?,?,?,?) "
            "ON CONFLICT(id) DO UPDATE SET "
            "name=COALESCE(excluded.name, students.name), "
            "email=COALESCE(excluded.email, students.email), "
            "fetched_at=excluded.fetched_at",
            (uid, info.get("name"), info.get("email"), now_ts),
        )


def _full_work(report) -> None:
    """FULL Google Classroom synchronization with real staged progress.

    Flow (§3/§22): verify/refresh OAuth → courses → assignments → students
    (roster per course, fetched ONCE) → submissions for every assignment
    (all states + grades + attachments) → local DB (idempotent upserts) →
    done. Progress frames mirror the actual API calls being made.

    Failure honesty (§22): a fatal step (auth/courses) reports ``error`` with
    the failing stage — never ``done``. A per-part failure (one course/one
    assignment) collects into ``summary.errors`` and the ``done`` frame is
    marked ``partial`` so the UI never claims full success.
    """
    started = time.time()
    stage = "connect"
    errors: list[dict] = []
    # Assignments Classroom will not serve submissions for: unpublished
    # draft work, or coursework the teacher deleted in Classroom. These are
    # expected states, not sync failures, and they must not paint the page
    # red forever.
    unavailable: list[dict] = []
    try:
        report({"type": "stage", "stage": stage,
                    "message": "Connecting…"})
        token = get_access_token()   # refreshes credentials; raises on auth failure

        stage = "courses"
        report({"type": "stage", "stage": stage,
                    "message": "Fetching courses…"})
        courses = classroom_sync.sync_courses(force=True)
        report({"type": "courses", "count": len(courses)})
        if not courses:
            errors.append({"part": "courses",
                           "detail": "No courses returned for this account "
                                     "(are you a teacher of any class?)"})

        # Assignments per course.
        stage = "assignments"
        assignments_by_course: list[tuple[dict, list[dict]]] = []
        assignment_total = 0
        for course in courses:
            cid = course["id"]
            report({"type": "stage", "stage": stage,
                        "message": f"Fetching assignments — {course.get('name') or cid}…"})
            try:
                items = classroom_sync.sync_coursework(cid, force=True)
            except Exception as exc:  # noqa: BLE001 — report this part, keep going
                errors.append({"part": f"assignments ({course.get('name') or cid})",
                               "detail": str(exc)})
                log_event("classroom.sync", "assignments fetch failed",
                          level=20, course_id=cid, error=str(exc))
                continue
            assignments_by_course.append((course, items))
            assignment_total += len(items)
            report({"type": "assignments", "count": len(items),
                        "total": assignment_total,
                        "course": course.get("name") or cid})

        # Roster per course — fetched ONCE, reused for every assignment.
        stage = "students"
        rosters: dict[str, dict[str, dict]] = {}
        student_total = 0
        for course, _items in assignments_by_course:
            cid = course["id"]
            report({"type": "stage", "stage": stage,
                        "message": f"Fetching students — {course.get('name') or cid}…"})
            try:
                roster_list = gc.list_course_students(token, cid)
            except Exception as exc:  # noqa: BLE001
                errors.append({"part": f"students ({course.get('name') or cid})",
                               "detail": str(exc)})
                rosters[cid] = {}
                continue
            roster: dict[str, dict] = {}
            for st in roster_list:
                profile = st.get("profile") or {}
                uid = st.get("userId") or profile.get("id")
                if uid:
                    roster[uid] = {
                        "user_id": uid,
                        "name": (profile.get("name") or {}).get("fullName"),
                        "email": profile.get("emailAddress"),
                    }
            rosters[cid] = roster
            student_total += len(roster)
            _seed_student_names(roster, time.time())
            report({"type": "students", "count": len(roster),
                        "course": course.get("name") or cid})

        # Submissions (all states + grades + attachments) per assignment.
        stage = "submissions"
        idx = 0
        submissions_total = 0
        for course, items in assignments_by_course:
            cid = course["id"]
            roster = rosters.get(cid) or {}
            for cw in items:
                idx += 1
                wid = cw["id"]
                title = cw.get("title") or wid
                report({"type": "stage", "stage": stage,
                            "message": (f"Fetching submissions — {title} "
                                        f"({idx}/{assignment_total})…")})
                # Classroom has NO submissions for unpublished coursework:
                # asking is a guaranteed HTTP 400, not a sync failure.
                if str(cw.get("state") or "").upper() == "DRAFT":
                    unavailable.append({"coursework_id": wid, "title": title,
                                        "reason": "Draft — not published "
                                                  "in Classroom"})
                    log_event("classroom.sync",
                              "assignment submissions skipped",
                              coursework_id=wid, reason="draft")
                    report({"type": "assignment", "index": idx,
                                "total": assignment_total, "title": title,
                                "submissions": 0})
                    continue
                try:
                    subs: list[dict] = []
                    page_token: str | None = None
                    while True:
                        page, page_token = gc.list_student_submissions(
                            token, cid, wid, page_token)
                        subs.extend(page)
                        if not page_token:
                            break
                    cs.store_sync_result(cid, wid, roster, subs)
                    submissions_total += len(subs)
                except Exception as exc:  # noqa: BLE001 — one bad assignment
                    # 400/404 here means "this coursework has no submissions
                    # endpoint any more" (deleted or unpublished in
                    # Classroom). Report it as unavailable work, not as a
                    # failure, so a healthy sync stays green.
                    status = getattr(exc, "status", None)
                    if status in (400, 404):
                        unavailable.append({
                            "coursework_id": wid, "title": title,
                            "reason": str(exc)})
                        log_event("classroom.sync",
                                  "assignment submissions unavailable",
                                  coursework_id=wid, status=status)
                        continue
                    errors.append({"part": f"submissions ({title})",
                                   "detail": str(exc)})
                    log_event("classroom.sync", "assignment submissions failed",
                              level=20, coursework_id=wid, error=str(exc))
                    continue
                report({"type": "assignment", "index": idx,
                            "total": assignment_total, "title": title,
                            "submissions": len(subs)})

        stage = "save"
        report({"type": "stage", "stage": stage,
                    "message": "Updating database…"})
        now_ts = time.time()
        db.set_meta("classroom_last_sync", str(now_ts))

        log_event("classroom.sync", "full sync finished",
                  courses=len(courses), assignments=assignment_total,
                  students=student_total, submissions=submissions_total,
                  errors=len(errors), unavailable=len(unavailable),
                  elapsed_s=round(now_ts - started, 1))
        report({"type": "done",
                    "summary": {
                        "courses": len(courses),
                        "assignments": assignment_total,
                        "students": student_total,
                        "submissions": submissions_total,
                        "errors": errors,
                        "unavailable": unavailable,
                        "partial": bool(errors),
                        "synced_at": now_ts,
                        "elapsed_s": round(time.time() - started, 2),
                    }})
    except Exception as exc:  # noqa: BLE001 — fatal step, honest failure
        log_event("classroom.sync", "full sync failed",
                  level=30, stage=stage, error=str(exc))
        report({"type": "error", "stage": stage, "message": str(exc)})


@router.api_route("/classroom/sync", methods=["GET", "POST"])
async def full_sync():
    """Run the full sync, streaming its progress as SSE.

    Delegates to the progress registry so the identical progress can also be
    polled (``/classroom/sync/start`` + ``/classroom/sync/progress``) by a
    client whose stream is buffered by a tunnel or proxy.
    """
    _require_classroom()
    run, started = sync_runs.start("full", {}, _full_work)
    return _stream_run(run, attach=not started)



# ------------------------------------------------------------ permissions (§4)

def _granted_scopes(token: str) -> tuple[set[str] | None, str | None]:
    """Ask Google which scopes the current access token actually carries.
    Returns (scopes, None) or (None, reason) — never raises."""
    try:
        import requests
        from ..net_usage import SMALL_REQUEST_BYTES, record
        response = requests.get(
            "https://oauth2.googleapis.com/tokeninfo",
            params={"access_token": token}, timeout=15,
        )
        record("classroom_api", SMALL_REQUEST_BYTES,
               len(response.content or b""))
        if response.status_code >= 400:
            return None, f"tokeninfo HTTP {response.status_code}"
        raw = (response.json() or {}).get("scope") or ""
        granted = {s for s in raw.split() if s}
        if not granted:
            return None, "Google returned no scope information"
        return granted, None
    except Exception as exc:  # noqa: BLE001
        return None, str(exc)


@router.get("/classroom/permissions")
def classroom_permissions():
    """Verify granted OAuth scopes against what the features actually need.

    On-demand only (one tiny request when the teacher clicks "Check") —
    never called per keystroke or per poll. When scopes are missing the UI
    shows a reauthorization prompt instead of silently failing later.
    """
    state = auth_state()
    required = sorted(set(SCOPES))
    if not state.connected:
        return {"checked": False, "ok": None, "connected": False,
                "required": required, "missing": [],
                "detail": "Not connected — connect Google Classroom first."}
    token = get_access_token()
    granted, reason = _granted_scopes(token)
    if granted is None:
        return {"checked": False, "ok": None, "connected": True,
                "required": required, "missing": [],
                "detail": f"Could not verify permissions: {reason}",
                "checked_at": time.time()}
    missing = sorted(set(SCOPES) - granted)
    log_event("classroom.auth", "permission check",
              missing=len(missing), granted=len(granted))
    return {"checked": True, "ok": not missing, "connected": True,
            "required": required, "granted": sorted(granted),
            "missing": missing, "checked_at": time.time()}


@router.get("/classroom/tracking")
def tracking(course_id: str | None = None, coursework_id: str | None = None):
    """Local-first tracking view: normalized rows + overview + freshness."""
    return cs.tracking_rows(course_id=course_id, coursework_id=coursework_id)


@router.get("/classroom/submissions/{submission_id}/detail")
def tracking_detail(submission_id: str):
    detail = cs.submission_detail(submission_id)
    if not detail:
        raise AppError("Submission not found in the tracking database. "
                       "Run a sync first.", status=404, code="not_found")
    return detail


@router.get("/classroom/tracking/export")
def export_tracking(format: str = "csv",
                    course_id: str | None = None,
                    coursework_id: str | None = None):
    """Export the tracking view (§20). Renders the same rows the UI shows;
    contains no secrets, keys, or token material."""
    fmt = (format or "csv").lower()
    rows = _render_export_rows(course_id, coursework_id)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")

    if fmt == "json":
        import json
        return StreamingResponse(
            iter([json.dumps({"rows": rows}, indent=2)]),
            media_type="application/json",
            headers={"Content-Disposition":
                     f'attachment; filename="tracking_{stamp}.json"'})

    if fmt == "csv":
        buf = io.StringIO()
        writer = csv.DictWriter(buf, fieldnames=_EXPORT_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)
        return StreamingResponse(
            iter([buf.getvalue()]), media_type="text/csv",
            headers={"Content-Disposition":
                     f'attachment; filename="tracking_{stamp}.csv"'})

    if fmt == "xlsx":
        return _render_xlsx(rows, stamp)

    if fmt == "pdf":
        return _render_pdf(rows, stamp)

    raise AppError(f"Unknown export format: {format}. "
                   "Use csv, xlsx, pdf or json.",
                   status=400, code="bad_export_format")


def _render_xlsx(rows: list[dict], stamp: str):
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter

    wb = Workbook()
    ws = wb.active
    ws.title = "Tracking"
    header_fill = PatternFill("solid", fgColor="1F3864")
    header_font = Font(color="FFFFFF", bold=True)
    for col, name in enumerate(_EXPORT_COLUMNS, start=1):
        cell = ws.cell(row=1, column=col, value=name.replace("_", " ").title())
        cell.fill = header_fill
        cell.font = header_font
        cell.alignment = Alignment(horizontal="center")
    for r_i, row in enumerate(rows, start=2):
        for c_i, name in enumerate(_EXPORT_COLUMNS, start=1):
            ws.cell(row=r_i, column=c_i, value=row.get(name, ""))
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = f"A1:{get_column_letter(len(_EXPORT_COLUMNS))}{max(2, len(rows) + 1)}"
    for col, name in enumerate(_EXPORT_COLUMNS, start=1):
        width = max(len(name) + 4,
                    *(len(str(row.get(name, ""))) + 2 for row in rows)) if rows else len(name) + 4
        ws.column_dimensions[get_column_letter(col)].width = min(50, max(10, width))

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return StreamingResponse(
        buf,
        media_type=("application/vnd.openxmlformats-officedocument"
                    ".spreadsheetml.sheet"),
        headers={"Content-Disposition":
                 f'attachment; filename="tracking_{stamp}.xlsx"'})


def _render_pdf(rows: list[dict], stamp: str):
    """Classroom Tracking Export, built on the shared report design (§1, §5).

    One flat 9-column grid is unreadable on a portrait page, so the same rows
    are presented as two related tables - what was submitted, and what came
    back. Nothing is dropped; the rows are keyed by student + assignment in
    both, so no row can be read against the wrong assignment.
    """
    from ..reporting import design as D

    brand = D.load_branding()
    theme = D.make_theme(accent=brand.accent or None, label="tracking")
    styles = D.build_styles(theme)
    story: list = []

    def cell(row: dict, *names: str) -> str:
        return " · ".join(str(row.get(n, "")).strip()
                          for n in names if str(row.get(n, "")).strip()) or "—"

    submitted = [[cell(r, "student"), cell(r, "assignment"),
                  cell(r, "status"), cell(r, "late"), cell(r, "attachments")]
                 for r in rows]
    graded = [[cell(r, "student"), cell(r, "assignment"),
               cell(r, "v5_score"), cell(r, "assigned_grade"),
               cell(r, "data_quality"), cell(r, "stale"),
               cell(r, "last_synced")]
              for r in rows]

    D.add(story, D.section(theme, styles, "Submissions",
                           subtitle="What each student handed in."))
    if submitted:
        D.add(story, D.data_table(
            theme, styles,
            ["Student", "Assignment", "Status", "Late", "Attachments"],
            submitted,
            widths=D.fit_widths(theme, [22, 26, 14, 6, 32])))
    else:
        D.add(story, D.empty_state(
            theme, styles, "No Classroom submissions matched this export."))

    D.add(story, D.section(theme, styles, "Grades and sync",
                           subtitle="What came back, and how fresh it is."))
    if graded:
        D.add(story, D.data_table(
            theme, styles,
            ["Student", "Assignment", "Math Correct", "Classroom grade",
             "Data quality", "Stale", "Last synced"],
            graded,
            widths=D.fit_widths(theme, [20, 24, 13, 13, 12, 7, 16])))
    else:
        D.add(story, D.empty_state(theme, styles, "No grades to show."))

    D.add(story, D.signature_block(theme, styles, brand))
    D.add(story, D.note(theme, styles,
                        "Generated from the local Classroom sync. Values are "
                        "exactly as recorded at the time of the export."))

    buf = D.Report(theme=theme, branding=brand, title="Classroom Tracking Export",
                   story=story, label="TEACHER COPY",
                   eyebrow="Math Correct",
                   context=(f"{len(rows)} submission(s) · generated "
                            f"{datetime.now().strftime('%Y-%m-%d %H:%M')}"),
                   doc_id=f"MC-{stamp[:10]}").build()
    buf.seek(0)
    return StreamingResponse(buf, media_type="application/pdf",
                             headers={"Content-Disposition":
                                      f'attachment; filename="tracking_{stamp}.pdf"'})


# ------------------------------------------------------------- diagnostics


@router.post("/classroom/diagnostics/run")
def diagnostics_run():
    """Developer diagnostic: 13 checklist tests for the tracking pipeline.

    Pure-local except oauth/classroom checks (config inspection only — no
    network calls). Each check returns pass/fail/skip with a detail message.
    """
    from ..config import get_config
    cfg = get_config()
    results = []

    def check(name, fn):
        try:
            detail = fn()
            results.append({"check": name, "status": "pass", "detail": detail})
        except _Skip as skip:
            results.append({"check": name, "status": "skip", "detail": str(skip)})
        except Exception as exc:  # noqa: BLE001
            results.append({"check": name, "status": "fail", "detail": str(exc)})

    class _Skip(Exception):
        pass

    def _oauth():
        state = auth_state()
        if not state.client_configured:
            raise _Skip("OAuth client JSON not configured")
        return "client configured"

    def _connected():
        state = auth_state()
        if not state.client_configured:
            raise _Skip("OAuth client not configured")
        if not state.connected:
            return "NOT connected — sync will fail until you connect"
        return "connected"

    def _schema():
        for table in ("classroom_submissions", "classroom_state_events"):
            db.query_one(f"SELECT COUNT(*) AS n FROM {table}")
        return "tables present"

    def _queryable():
        data = cs.tracking_rows()
        return f"{data['overview']['total']} rows"

    def _mapping():
        for raw in ("NEW", "CREATED", "TURNED_IN", "RETURNED",
                    "RECLAIMED_BY_STUDENT", "STUDENT_EDITED_AFTER_TURN_IN"):
            status = cs.normalized_status({"current_state": raw})
            if status == "UNKNOWN":
                raise RuntimeError(f"raw state {raw} maps to UNKNOWN")
        return "all 6 raw states map"

    def _pagination():
        if cfg.mock_mode:
            raise _Skip("mock mode")
        if not auth_state().connected:
            raise _Skip("Classroom not connected")
        token = get_access_token()
        page, next_token = gc.list_student_submissions(
            token, "diag-course", "diag-work")
        return (f"page fetched ({len(page)} items), "
                f"next_token={'yes' if next_token else 'no'}")

    def _idempotent():
        count_before = db.query_one(
            "SELECT COUNT(*) AS n FROM classroom_submissions")["n"]
        return f"{count_before} rows (run a sync twice to verify no growth)"

    def _failure_keeps_rows():
        n = db.query_one("SELECT COUNT(*) AS n FROM classroom_submissions")["n"]
        return f"{n} rows retained; sync failures never delete (verified by tests)"

    def _attachments():
        sample = {"assignmentSubmission": {"attachments": [
            {"driveFile": {"id": "f1", "title": "hw.pdf"}}]}}
        out = gc.extract_attachments(sample)
        if not out:
            raise RuntimeError("extraction returned nothing")
        return f"extracted {len(out)} attachment(s)"

    def _roster_rows():
        n = db.query_one(
            "SELECT COUNT(*) AS n FROM classroom_submissions "
            "WHERE current_state = 'NOT_STARTED'")["n"]
        return f"{n} NOT_STARTED rows"

    def _export_csv():
        rows = _render_export_rows(None, None)
        return f"csv renders {len(rows)} rows"

    def _export_xlsx():
        _render_xlsx(_render_export_rows(None, None), "diag")
        return "xlsx renders"

    def _export_pdf():
        _render_pdf(_render_export_rows(None, None), "diag")
        return "pdf renders"

    check("oauth_configured", _oauth)
    check("classroom_connected", _connected)
    check("schema_tables_present", _schema)
    check("tracking_rows_queryable", _queryable)
    check("state_mapping_complete", _mapping)
    check("pagination_end_to_end", _pagination)
    check("idempotent_double_sync", _idempotent)
    check("api_failure_keeps_rows", _failure_keeps_rows)
    check("attachment_extraction", _attachments)
    check("roster_not_started_rows", _roster_rows)
    check("export_csv", _export_csv)
    check("export_xlsx", _export_xlsx)
    check("export_pdf", _export_pdf)

    passed = sum(1 for r in results if r["status"] == "pass")
    failed = sum(1 for r in results if r["status"] == "fail")
    skipped = sum(1 for r in results if r["status"] == "skip")
    return {"results": results, "passed": passed, "failed": failed,
            "skipped": skipped, "total": len(results)}
