"""Classroom submission TRACKING engine (spec §§3-§16).

Two layers, kept strictly apart:

RAW LAYER  — current_state holds the verbatim Classroom state string
             (NEW / CREATED / TURNED_IN / RETURNED / RECLAIMED_BY_STUDENT /
             STUDENT_EDITED_AFTER_TURN_IN). This is the source of truth and
             is never rewritten. Roster students with no submission resource
             get the marker state 'NOT_STARTED' (there is no Classroom truth
             to store for them).

VIEW LAYER — normalized_status() maps a raw state + flags onto the 13
             user-facing statuses. It is a pure function of its inputs, so
             it can evolve without touching stored data.

Sync is IDEMPOTENT (keyed by the stable Classroom submission id) and
FAILURE-SAFE: on any API failure the previously synced rows are KEPT (never
deleted) and simply age into 'stale' via last_synced_at — the UI shows the
last known truth plus a freshness indicator, which is exactly what a teacher
needs at 11pm before grades are due.

A submission resource existing says nothing about whether work was turned
in — the state field is the only source of truth, and CREATED must never be
shown as "submitted" (spec §3.1).
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

from .. import db
from .. import student_identity
from ..logging_setup import log_event
from . import client as gc

log = logging.getLogger("classroom.submissions")

# ---------------------------------------------------------------- view layer

# All user-facing statuses (§3.2). NOT_STARTED covers roster students with
# no submission resource; PROCESSING/CORRECTED/FAILED are overlaid from V5's
# own job state; GRADED from Classroom's assigned grade.
STATUSES = (
    "NOT_STARTED", "CREATED", "SUBMITTED", "SUBMITTED_LATE", "RETURNED",
    "RECLAIMED", "EDITED_AFTER_SUBMISSION", "MISSING", "PROCESSING",
    "CORRECTED", "GRADED", "FAILED", "INVALID", "UNKNOWN",
)

STATUS_LABELS = {
    "NOT_STARTED": "Not started",
    "CREATED": "Opened, not submitted",
    "SUBMITTED": "Submitted",
    "SUBMITTED_LATE": "Submitted late",
    "RETURNED": "Returned",
    "RECLAIMED": "Reclaimed (unsubmitted)",
    "EDITED_AFTER_SUBMISSION": "Edited after submission",
    "MISSING": "No submission",
    "PROCESSING": "Being corrected",
    "CORRECTED": "Corrected",
    "GRADED": "Graded",
    "FAILED": "Correction failed",
    "INVALID": "Invalid submission",
    "UNKNOWN": "Unknown",
}

# Raw Classroom states that mean "the student has handed something in".
# STUDENT_EDITED_AFTER_TURN_IN is deliberately NOT here: it gets the more
# specific EDITED_AFTER_SUBMISSION status, and it also means any earlier
# correction result may be stale.
_TURNED_IN_STATES = {"TURNED_IN", "STUDENT_EDITED_AFTER_TURN_IN"}

_STALE_AFTER_SECONDS = 30 * 60  # rows older than this display as stale


def stale_after_seconds() -> int:
    return _STALE_AFTER_SECONDS


def normalized_status(row: dict[str, Any], job: dict | None = None,
                      result: dict | None = None) -> str:
    """Map a classroom_submissions row (+ V5's latest job/result) onto a status.

    Pure function of the inputs — no DB access, so tests can call it with
    synthetic dicts.

    Overlay rules:
      * PROCESSING / CORRECTED only apply while the work is still handed in
        (raw TURNED_IN family). If a student reclaimed or re-edited, the
        Classroom state wins — "Reclaimed" must never display as "Corrected".
      * FAILED applies while work is still handed in: a failed correction on
        a submitted piece of work is what the teacher must act on.
      * GRADED wins whenever Classroom itself holds an assigned grade.
      * data_quality ERROR (sync could not verify) degrades to UNKNOWN only
        when nothing at all is known.
    """
    job = job or {}
    result = result or {}
    raw = row.get("current_state") or ""
    late = bool(row.get("late"))
    still_handed_in = raw in _TURNED_IN_STATES

    if row.get("data_quality") == "ERROR" and not raw:
        return "UNKNOWN"

    if still_handed_in:
        status = (job.get("status") or "").upper()
        if status == "FAILED":
            # A submission with no usable attachment is "Invalid submission",
            # not merely a correction failure (spec §8 / §10).
            if (job.get("error_code") or "").lower() == "invalid_submission":
                return "INVALID"
            return "FAILED"
        if status in ("", "COMPLETED") or result:
            # No in-flight job: a finished correction or an existing result
            # means V5 has marked it. Classroom's own grade outranks that.
            if row.get("assigned_grade") is not None:
                return "GRADED"
            if result or status == "COMPLETED":
                return "CORRECTED"
        if status:
            return "PROCESSING"

    if row.get("assigned_grade") is not None:
        return "GRADED"

    return _base_status(raw, late)


def _base_status(raw: str, late: bool) -> str:
    if raw == "TURNED_IN":
        return "SUBMITTED_LATE" if late else "SUBMITTED"
    if raw == "STUDENT_EDITED_AFTER_TURN_IN":
        return "EDITED_AFTER_SUBMISSION"
    if raw == "RECLAIMED_BY_STUDENT":
        return "RECLAIMED"
    if raw == "RETURNED":
        return "RETURNED"
    if raw in ("CREATED", "NEW"):
        return "CREATED"  # assignment opened, nothing handed in yet
    if raw == "NOT_STARTED":
        return "NOT_STARTED"
    if raw == "MISSING":
        return "MISSING"
    if not raw:
        return "MISSING"
    return "UNKNOWN"


# ---------------------------------------------------------------- sync engine

def fetch_tracking_inputs(course_id: str, coursework_id: str) -> dict:
    """Fetch the raw inputs for a tracking sync: full roster + ALL submission
    pages. Pure fetch, no DB writes — the SSE route streams progress between
    these steps and then hands both to store_sync_result()."""
    from .auth import get_access_token

    token = get_access_token()

    # 1) Roster first (§5): students without a submission resource must
    #    still appear as NOT_STARTED, not silently vanish.
    students = gc.list_course_students(token, course_id)
    roster: dict[str, dict] = {}
    for st in students:
        profile = st.get("profile") or {}
        uid = st.get("userId") or profile.get("id")
        if uid:
            roster[uid] = {
                "user_id": uid,
                "name": (profile.get("name") or {}).get("fullName"),
                "email": profile.get("emailAddress"),
            }

    # 2) ALL submissions, following pagination to the end (§4).
    subs: list[dict] = []
    page_token: str | None = None
    pages = 0
    while True:
        page, next_token = gc.list_student_submissions(
            token, course_id, coursework_id, page_token)
        subs.extend(page)
        pages += 1
        if not next_token:
            break
        page_token = next_token

    return {"roster": roster, "submissions": subs, "pages": pages}


def sync_submission_tracking(course_id: str, coursework_id: str) -> dict:
    """Sync one assignment's submissions with full-state fidelity.

    Idempotent: re-running with unchanged data updates only last_synced_at
    and sync_version. Never deletes: submissions that disappear from the API
    response keep their last known row and age into stale. Returns a summary
    dict for the API progress stream.

    An assignment DELETED in Classroom is never re-fetched: Google would
    answer HTTP 400 forever. Its stored history is left untouched.
    """
    cw = db.query_one("SELECT sync_state FROM coursework WHERE id = ?",
                      (coursework_id,))
    if cw is not None and (cw["sync_state"] or "ACTIVE") == \
            "DELETED_IN_CLASSROOM":
        return {"course_id": course_id, "coursework_id": coursework_id,
                "skipped": "deleted_in_classroom", "submissions": 0,
                "roster": 0, "transitions": 0, "pages": 0,
                "synced_at": time.time()}
    fetched = fetch_tracking_inputs(course_id, coursework_id)
    summary = store_sync_result(course_id, coursework_id,
                                fetched["roster"], fetched["submissions"])
    summary["pages"] = fetched["pages"]
    return summary


def store_sync_result(course_id: str, coursework_id: str,
                      roster: dict[str, dict], subs: list[dict]) -> dict:
    """Persist a fetched roster+submissions (idempotent, failure-safe).

    Exposed separately so the SSE sync route can stream page progress and
    still store through the exact same code path as plain sync_submission_tracking().
    """
    now_ts = time.time()
    transitions = 0
    with db.transaction() as conn:
        for s in subs:
            if not s.get("id"):
                continue
            transitions += _upsert_submission(conn, s, course_id, coursework_id,
                                              roster, now_ts)

        submission_user_ids = {s.get("userId") for s in subs if s.get("userId")}
        for uid, info in roster.items():
            if uid in submission_user_ids:
                continue
            # If a real row already exists for this student (their submission
            # resource vanished from the API), keep it — never duplicate.
            existing_real = conn.execute(
                "SELECT 1 FROM classroom_submissions WHERE coursework_id = ? "
                "AND student_id = ? AND submission_id NOT LIKE '%:missing'",
                (coursework_id, uid),
            ).fetchone()
            if existing_real:
                continue
            conn.execute(
                """
                INSERT INTO classroom_submissions(
                    submission_id, course_id, coursework_id, student_id,
                    student_name, student_email, current_state, late,
                    data_quality, last_synced_at, first_seen_at, sync_version)
                VALUES(?,?,?,?,?,?, 'NOT_STARTED', 0, 'VERIFIED', ?, ?, 1)
                ON CONFLICT(submission_id) DO UPDATE SET
                    student_name=excluded.student_name,
                    student_email=excluded.student_email,
                    last_synced_at=excluded.last_synced_at,
                    sync_version=classroom_submissions.sync_version + 1
                """,
                (f"{coursework_id}:{uid}:missing", course_id, coursework_id,
                 uid, info.get("name"), info.get("email"), now_ts, now_ts),
            )

    _touch_coursework_sync(coursework_id, now_ts)

    log_event("classroom.submissions", "tracking synced",
              course_id=course_id, coursework_id=coursework_id,
              submissions=len(subs), roster=len(roster), transitions=transitions)

    return {
        "course_id": course_id,
        "coursework_id": coursework_id,
        "submissions": len(subs),
        "roster": len(roster),
        "transitions": transitions,
        "synced_at": now_ts,
    }


def _upsert_submission(conn, s: dict, course_id: str, coursework_id: str,
                       roster: dict[str, dict], now_ts: float) -> int:
    """Upsert ONE StudentSubmission; returns 1 if a state transition fired."""
    sid = s["id"]
    uid = s.get("userId")
    info = roster.get(uid) or {}
    raw_state = s.get("state") or ""
    attachments = gc.extract_attachments(s)
    history = gc.extract_state_history(s)

    existing = conn.execute(
        "SELECT current_state FROM classroom_submissions WHERE submission_id = ?",
        (sid,),
    ).fetchone()
    prev_state = existing["current_state"] if existing else None
    changed = prev_state is not None and prev_state != raw_state

    conn.execute(
        """
        INSERT INTO classroom_submissions(
            submission_id, course_id, coursework_id, student_id, student_name,
            student_email, current_state, previous_state, state_changed_at,
            late, creation_time, update_time, submission_history,
            attachments_json, assigned_grade, draft_grade, max_points,
            data_quality, sync_error, last_synced_at, first_seen_at, sync_version)
        VALUES(?,?,?,?,?,?,?,?,?,
               ?,?,?,?,?,?,?,?,?,
               NULL, ?, ?, COALESCE(
                   (SELECT sync_version + 1 FROM classroom_submissions
                     WHERE submission_id = ?), 1))
        ON CONFLICT(submission_id) DO UPDATE SET
            student_name=excluded.student_name,
            student_email=excluded.student_email,
            current_state=excluded.current_state,
            previous_state=COALESCE(excluded.previous_state,
                                    classroom_submissions.previous_state),
            state_changed_at=COALESCE(
                CASE WHEN classroom_submissions.current_state != excluded.current_state
                     THEN excluded.state_changed_at END,
                classroom_submissions.state_changed_at),
            late=excluded.late,
            creation_time=excluded.creation_time,
            update_time=excluded.update_time,
            submission_history=excluded.submission_history,
            attachments_json=excluded.attachments_json,
            assigned_grade=excluded.assigned_grade,
            draft_grade=excluded.draft_grade,
            data_quality=excluded.data_quality,
            sync_error=NULL,
            last_synced_at=excluded.last_synced_at,
            sync_version=classroom_submissions.sync_version + 1
        """,
        (
            sid, course_id, coursework_id, uid, info.get("name"),
            info.get("email"), raw_state,
            prev_state if changed else None, now_ts if changed else None,
            int(bool(s.get("late"))), s.get("creationTime"), s.get("updateTime"),
            json.dumps(history), json.dumps(attachments),
            s.get("assignedGrade"), s.get("draftGrade"), None,
            _quality_for(raw_state, attachments), now_ts, now_ts, sid,
        ),
    )

    if changed:
        conn.execute(
            "INSERT INTO classroom_state_events(submission_id, from_state,"
            " to_state, detected_at) VALUES(?,?,?,?)",
            (sid, prev_state, raw_state, now_ts),
        )
    return 1 if changed else 0


def _quality_for(raw_state: str, attachments: list[dict]) -> str:
    """Data-quality model (§14): PARTIAL = handed in but no attachment
    metadata came back (we cannot yet prove WHAT was submitted); VERIFIED =
    everything we expect was seen first-hand this sync."""
    if raw_state in _TURNED_IN_STATES:
        return "VERIFIED" if attachments else "PARTIAL"
    return "VERIFIED"


def _touch_coursework_sync(coursework_id: str, now_ts: float) -> None:
    db.set_meta(f"classroom_tracking_sync:{coursework_id}", str(now_ts))


def last_tracking_sync(coursework_id: str) -> float | None:
    raw = db.get_meta(f"classroom_tracking_sync:{coursework_id}")
    return float(raw) if raw else None


# ---------------------------------------------------------------- read model

def tracking_rows(course_id: str | None = None,
                  coursework_id: str | None = None,
                  now_ts: float | None = None) -> dict:
    """Everything the tracking UI needs: normalized rows + overview + freshness."""
    now_ts = time.time() if now_ts is None else now_ts
    where, params = [], []
    if course_id:
        where.append("cs.course_id = ?")
        params.append(course_id)
    if coursework_id:
        where.append("cs.coursework_id = ?")
        params.append(coursework_id)
    clause = ("WHERE " + " AND ".join(where)) if where else ""

    rows = db.query(
        f"""
        SELECT cs.*, w.title AS coursework_title,
               w.max_points AS coursework_max_points,
               w.due_date AS coursework_due_date
        FROM classroom_submissions cs
        LEFT JOIN coursework w ON w.id = cs.coursework_id
        {clause}
        ORDER BY COALESCE(cs.student_name, cs.student_id), cs.coursework_id
        """,
        params,
    )

    jobs_by_sub: dict[str, dict] = {}
    results_by_sub: dict[str, dict] = {}
    sub_ids = [r["submission_id"] for r in rows
               if not r["submission_id"].endswith(":missing")]
    if sub_ids:
        marks = ",".join("?" * len(sub_ids))
        for j in db.query(
            f"SELECT id, submission_id, status, error, error_code, updated_at, result_id "
            f"FROM correction_jobs WHERE submission_id IN ({marks})",
            sub_ids,
        ):
            cur = jobs_by_sub.get(j["submission_id"])
            if cur is None or (j["updated_at"] or 0) >= (cur["updated_at"] or 0):
                jobs_by_sub[j["submission_id"]] = dict(j)
        for res in db.query(
            f"SELECT id, submission_id, earned, available, percentage, created_at, "
            f"verification_json FROM results WHERE submission_id IN ({marks}) "
            f"ORDER BY created_at DESC",
            sub_ids,
        ):
            row_result = dict(res)
            # A partial correction has no publishable score. Every consumer of
            # this projection (tracking table, reports, class report) gets the
            # coverage instead of a fraction that reads as a mark.
            from ..engine.verify import partial_summary
            row_result["partial"] = partial_summary(
                db.loads(row_result.pop("verification_json", None), {}))
            results_by_sub.setdefault(res["submission_id"], row_result)

    out_rows: list[dict] = []
    counts: dict[str, int] = {s: 0 for s in STATUSES}
    sync_errors = 0
    stale_rows = 0
    last_sync: float | None = None

    for r in rows:
        row = dict(r)
        sid = row["submission_id"]
        job = jobs_by_sub.get(sid)
        result = results_by_sub.get(sid)
        quality = row.get("data_quality") or "UNKNOWN"
        last_synced = row.get("last_synced_at")
        is_stale = bool(last_synced and (now_ts - last_synced) > _STALE_AFTER_SECONDS)
        status = normalized_status(row, job=job, result=result)
        if row.get("sync_error"):
            sync_errors += 1
        if is_stale:
            stale_rows += 1
        if last_synced:
            last_sync = max(last_sync or 0, last_synced)
        counts[status] = counts.get(status, 0) + 1

        # Canonical name: nickname, then Classroom name. The old code fell back
        # to the internal student id, which is how a raw id reached a page.
        _identity = student_identity.resolve(row.get("student_id"))
        out_rows.append({
            "submission_id": sid,
            "course_id": row.get("course_id"),
            "coursework_id": row.get("coursework_id"),
            "coursework_title": row.get("coursework_title"),
            "student_id": row.get("student_id"),
            "student_name": row.get("student_name") or _identity["classroom_name"],
            "display_name": _identity["display_name"],
            "classroom_name": _identity["classroom_name"],
            "nickname": _identity["nickname"],
            "student_email": row.get("student_email"),
            "raw_state": row.get("current_state"),
            "previous_state": row.get("previous_state"),
            "state_changed_at": row.get("state_changed_at"),
            "status": status,
            "status_label": STATUS_LABELS.get(status, status),
            "late": bool(row.get("late")),
            "creation_time": row.get("creation_time"),
            "update_time": row.get("update_time"),
            "attachments": db.loads(row.get("attachments_json"), []),
            "assigned_grade": row.get("assigned_grade"),
            "draft_grade": row.get("draft_grade"),
            "max_points": row.get("max_points") or row.get("coursework_max_points"),
            "due_date": row.get("coursework_due_date"),
            "data_quality": quality,
            "stale": is_stale,
            "sync_error": row.get("sync_error"),
            "last_synced_at": last_synced,
            "first_seen_at": row.get("first_seen_at"),
            "synthetic": sid.endswith(":missing"),
            "job": {"id": job["id"], "status": job["status"],
                    "error": job.get("error"),
                    "error_code": job.get("error_code")} if job else None,
            "result": {"id": result["id"], "earned": result["earned"],
                       "available": result["available"],
                       "percentage": result["percentage"],
                       "partial": result.get("partial")} if result else None,
        })

    return {
        "rows": out_rows,
        "overview": {
            "counts": counts,
            "total": len(out_rows),
            "sync_errors": sync_errors,
            "stale": stale_rows,
            "last_sync": last_sync,
        },
    }


# ------------------------------------------------ assignment roster (detail page)

#: What the Assignment Details page reports for one student. These are NOT the
#: 14 raw submission states: they answer the question a teacher asks about the
#: assignment (did they hand it in?), and they are computed from the same rows
#: the status table shows, so the cards can never disagree with the table.
COMPLETED = "COMPLETED"
NOT_COMPLETED = "NOT_COMPLETED"
MISSING = "MISSING"
UNKNOWN = "UNKNOWN"

COMPLETION_LABELS = {
    COMPLETED: "Completed",
    NOT_COMPLETED: "Not completed",
    MISSING: "Missing",
    UNKNOWN: "Unknown",
}

#: Statuses that only exist BECAUSE the student handed work in. A correction
#: failure is one of them: a failed marking run must never erase the fact that
#: the paper was submitted.
_SUBMITTED_STATUSES = frozenset({
    "SUBMITTED", "SUBMITTED_LATE", "EDITED_AFTER_SUBMISSION", "RETURNED",
    "PROCESSING", "CORRECTED", "GRADED", "FAILED",
})


_EDIT_AFTER_SUBMISSION = (
    "The student changed the work after handing it in; the latest version is "
    "what Classroom holds.")


def completion_bucket(row: dict) -> tuple[str, str | None]:
    """Classify one tracking row as Completed / Not completed / Missing / Unknown.

    Pure function of the row, so the reason string can be tested without a
    database. A draft (assignment opened, nothing handed in) is NOT completed,
    and a student with no submission is NOT "missing" when the roster confirms
    they are enrolled — they are simply not completed.
    """
    status = (row.get("status") or "").strip().upper()
    quality = (row.get("data_quality") or "").strip().upper()

    if row.get("sync_error"):
        return MISSING, ("This record could not be synchronized, so its "
                         "submission state is unverified.")
    if quality == "ERROR":
        return MISSING, ("The backend recorded an error for this student, so "
                         "the submission cannot be reconciled.")
    if not row.get("student_id"):
        return MISSING, "The submission carries no student identifier."
    if not status:
        return UNKNOWN, "No submission state has been stored for this student."
    if status in _SUBMITTED_STATUSES:
        return COMPLETED, (_EDIT_AFTER_SUBMISSION
                           if status == "EDITED_AFTER_SUBMISSION" else None)
    if status == "INVALID":
        return NOT_COMPLETED, ("Work was handed in but it cannot be read, so it "
                               "does not count as a completed submission.")
    if status == "CREATED":
        return NOT_COMPLETED, ("The assignment was opened but nothing was "
                               "handed in.")
    if status == "RECLAIMED":
        return NOT_COMPLETED, ("The student took the work back, so there is no "
                               "submission to mark.")
    if status == "NOT_STARTED":
        return NOT_COMPLETED, None
    if status == "MISSING":
        return MISSING, ("No usable submission record was stored for this "
                         "student.")
    return UNKNOWN, (f"The stored submission state “{status}” is not one this "
                     "application can interpret.")


def correction_view(row: dict) -> dict:
    """Where V5's own correction of this student's paper stands.

    Kept separate from completion: a paper can be completed (handed in) and
    still be uncorrected, and a failed correction never un-submits it.
    """
    job = row.get("job") or {}
    result = row.get("result") or {}
    status = (job.get("status") or "").strip().upper()

    if result:
        partial = result.get("partial") or {}
        return {
            "state": "CORRECTED",
            "label": "Corrected",
            "job_status": status or None,
            "earned": result.get("earned"),
            "available": result.get("available"),
            "percentage": result.get("percentage"),
            # A partial result has no publishable score; say so instead of
            # presenting the coverage as a mark. ``partial_summary`` returns
            # None for a complete correction, a block for an incomplete one.
            "partial": bool(partial),
            "error": None,
        }
    if status == "FAILED":
        return {"state": "FAILED", "label": "Correction failed",
                "job_status": status, "earned": None, "available": None,
                "percentage": None, "partial": False,
                "error": job.get("error") or "The correction did not finish."}
    if status:
        return {"state": "PROCESSING", "label": "Being corrected",
                "job_status": status, "earned": None, "available": None,
                "percentage": None, "partial": False, "error": None}
    return {"state": "NOT_CORRECTED", "label": "Not corrected",
            "job_status": None, "earned": None, "available": None,
            "percentage": None, "partial": False, "error": None}


def _attachment_summary(raw: Any) -> list[dict]:
    """Only the parts of an attachment a teacher needs, never provider ids."""
    items = raw if isinstance(raw, list) else []
    out = []
    for item in items:
        if not isinstance(item, dict):
            continue
        out.append({"title": item.get("title") or item.get("name"),
                    "link": item.get("alternateLink") or item.get("link"),
                    "mime_type": item.get("mimeType") or item.get("mime_type")})
    return out


def assignment_roster(coursework_id: str,
                      now_ts: float | None = None) -> dict:
    """The Assignment Details view: every expected student, classified.

    One query path (`tracking_rows`) feeds both the per-student rows and the
    summary counts, so the cards and the table cannot drift apart. Duplicate
    submission resources are collapsed by the read model already: the table is
    keyed by submission id and roster students without one get a single
    synthetic row.
    """
    now_ts = time.time() if now_ts is None else now_ts
    data = tracking_rows(coursework_id=coursework_id, now_ts=now_ts)
    overview = data["overview"]

    # One student is one row. Classroom normally keeps one submission per
    # student, but a re-submission resource (or a stale duplicate row) must not
    # count twice: the newest REAL row wins, and a synthetic roster row is only
    # used when no submission row exists at all.
    def rank(row: dict) -> tuple:
        return (0 if row.get("synthetic") else 1,
                str(row.get("update_time") or ""),
                str(row.get("creation_time") or ""),
                str(row.get("submission_id") or ""))

    unique: dict[str, dict] = {}
    duplicates = 0
    for row in data["rows"]:
        key = row.get("student_id") or row.get("submission_id") or ""
        current = unique.get(key)
        if current is None:
            unique[key] = row
            continue
        duplicates += 1
        if rank(row) > rank(current):
            unique[key] = row
    rows = list(unique.values())

    counts = {COMPLETED: 0, NOT_COMPLETED: 0, MISSING: 0, UNKNOWN: 0}
    students: list[dict] = []
    submitted = corrected = failed = invalid = awaiting = 0
    stale = sync_errors = 0

    for row in rows:
        bucket, reason = completion_bucket(row)
        counts[bucket] += 1
        correction = correction_view(row)
        is_submitted = (row.get("status") or "").upper() in _SUBMITTED_STATUSES
        submitted += 1 if is_submitted else 0
        if correction["state"] == "CORRECTED":
            corrected += 1
        elif correction["state"] == "FAILED":
            failed += 1
        elif is_submitted:
            awaiting += 1
        invalid += 1 if (row.get("status") or "").upper() == "INVALID" else 0
        stale += 1 if row.get("stale") else 0
        sync_errors += 1 if row.get("sync_error") else 0

        students.append({
            "student_id": row.get("student_id"),
            "name": row.get("display_name") or row.get("student_name"),
            "classroom_name": row.get("classroom_name"),
            "email": row.get("student_email"),
            "status": row.get("status"),
            "status_label": row.get("status_label"),
            "raw_state": row.get("raw_state"),
            "completion": bucket,
            "completion_label": COMPLETION_LABELS[bucket],
            "completion_reason": reason,
            "submitted": is_submitted,
            "submitted_at": row.get("creation_time") or row.get("state_changed_at"),
            "late": bool(row.get("late")),
            "attachments": _attachment_summary(row.get("attachments")),
            "correction": correction,
            "max_points": row.get("max_points"),
            "assigned_grade": row.get("assigned_grade"),
            "draft_grade": row.get("draft_grade"),
            "quality": row.get("data_quality"),
            "stale": bool(row.get("stale")),
            "sync_error": row.get("sync_error"),
            "last_synced_at": row.get("last_synced_at"),
            "synthetic": bool(row.get("synthetic")),
            "submission_id": None if row.get("synthetic") else row.get("submission_id"),
        })

    students.sort(key=lambda s: ((s["name"] or "").casefold(), s["student_id"] or ""))

    expected = len(students)
    last_sync = overview.get("last_sync") or last_tracking_sync(coursework_id)
    cw = db.query_one("SELECT sync_state FROM coursework WHERE id = ?",
                      (coursework_id,))
    assignment_state = (cw["sync_state"] if cw else None) or "ACTIVE"

    warnings: list[str] = []
    if duplicates:
        warnings.append(f"{duplicates} extra submission record"
                        f"{'s' if duplicates != 1 else ''} were merged"
                        " into the newest one for the same student.")
    if expected == 0:
        warnings.append("No students are recorded for this assignment yet. "
                        "Sync Google Classroom to read its roster.")
    if last_sync is None:
        warnings.append("This assignment has never been synchronized, so its "
                        "student records are not verifiable.")
    if stale:
        warnings.append(f"{stale} student record{'s' if stale != 1 else ''} "
                        "were last synchronized more than "
                        f"{_STALE_AFTER_SECONDS // 60} minutes ago.")
    if sync_errors:
        warnings.append(f"{sync_errors} student record{'s' if sync_errors != 1 else ''} "
                        "could not be synchronized and are classified as missing.")
    if assignment_state == "DELETED_IN_CLASSROOM":
        warnings.append("This assignment no longer exists in Google Classroom. "
                        "The rows below are the last records kept locally.")

    return {
        "coursework_id": coursework_id,
        "students": students,
        "summary": {
            "expected": expected,
            "completed": counts[COMPLETED],
            "not_completed": counts[NOT_COMPLETED],
            "missing": counts[MISSING],
            "unknown": counts[UNKNOWN],
            "completion_percentage": (round(100.0 * counts[COMPLETED] / expected, 1)
                                      if expected else None),
            "submitted": submitted,
            "corrected": corrected,
            "failed": failed,
            "invalid": invalid,
            "awaiting_correction": max(0, awaiting),
            "stale": stale,
            "sync_errors": sync_errors,
            "duplicate_records": duplicates,
        },
        "sync": {
            "last_sync": last_sync,
            "stale_after_seconds": _STALE_AFTER_SECONDS,
            "assignment_state": assignment_state,
            "roster_known": expected > 0,
        },
        "warnings": warnings,
    }


def submission_detail(submission_id: str) -> dict | None:
    """Full detail: row + state-event timeline + attachments + V5 history."""
    row = db.query_one(
        "SELECT cs.*, w.title AS coursework_title FROM classroom_submissions cs "
        "LEFT JOIN coursework w ON w.id = cs.coursework_id "
        "WHERE cs.submission_id = ?",
        (submission_id,),
    )
    if not row:
        return None
    row = dict(row)
    events = [dict(e) for e in db.query(
        "SELECT from_state, to_state, detected_at FROM classroom_state_events "
        "WHERE submission_id = ? ORDER BY detected_at, id", (submission_id,))]
    return {
        "submission": {**tracking_row_public(row),
                       "submission_history": db.loads(row.get("submission_history"), [])},
        "state_events": events,
        "attachments": db.loads(row.get("attachments_json"), []),
        "jobs": [dict(j) for j in db.query(
            "SELECT id, status, created_at, finished_at, error, error_code "
            "FROM correction_jobs "
            "WHERE submission_id = ? ORDER BY created_at", (submission_id,))],
        "results": [dict(res) for res in db.query(
            "SELECT id, earned, available, percentage, created_at FROM results "
            "WHERE submission_id = ? ORDER BY created_at", (submission_id,))],
    }


def tracking_row_public(row: dict) -> dict:
    """Public projection of a classroom_submissions row (no internal noise)."""
    return {
        "submission_id": row.get("submission_id"),
        "course_id": row.get("course_id"),
        "coursework_id": row.get("coursework_id"),
        "coursework_title": row.get("coursework_title"),
        "student_id": row.get("student_id"),
        "student_name": row.get("student_name"),
        "current_state": row.get("current_state"),
        "previous_state": row.get("previous_state"),
        "state_changed_at": row.get("state_changed_at"),
        "late": bool(row.get("late")),
        "creation_time": row.get("creation_time"),
        "update_time": row.get("update_time"),
        "assigned_grade": row.get("assigned_grade"),
        "draft_grade": row.get("draft_grade"),
        "max_points": row.get("max_points"),
        "data_quality": row.get("data_quality"),
        "sync_error": row.get("sync_error"),
        "last_synced_at": row.get("last_synced_at"),
        "first_seen_at": row.get("first_seen_at"),
        "sync_version": row.get("sync_version"),
    }
