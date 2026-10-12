"""DB-cached Classroom synchronization (local-first, V5 §10).

Courses / coursework / submissions are cached in SQLite with fetched_at
timestamps. Nothing is auto-processed: coursework carries an allowlist flag
that only the teacher can set.
"""

from __future__ import annotations

import json
import logging
import time

from .. import db
from ..logging_setup import log_event
from . import client as gc

log = logging.getLogger("classroom.sync")

DEFAULT_TTL_SECONDS = 15 * 60


class SyncError(RuntimeError):
    pass


def _token() -> str:
    from .auth import get_access_token
    return get_access_token()


def sync_courses(force: bool = False) -> list[dict]:
    ttl = DEFAULT_TTL_SECONDS if not force else 0
    rows = db.query("SELECT * FROM courses ORDER BY name")
    now_ts = time.time()
    if rows and (now_ts - rows[0]["fetched_at"]) < ttl:
        return [dict(r) for r in rows]

    token = _token()
    courses = gc.list_teacher_courses(token)
    with db.transaction() as conn:
        for c in courses:
            conn.execute(
                """
                INSERT INTO courses(id, name, section, enrollment_code, state, update_time, fetched_at)
                VALUES(?,?,?,?,?,?,?)
                ON CONFLICT(id) DO UPDATE SET
                    name=excluded.name, section=excluded.section,
                    enrollment_code=excluded.enrollment_code,
                    state=excluded.state, update_time=excluded.update_time,
                    fetched_at=excluded.fetched_at
                """,
                (c["id"], c.get("name", "Unnamed course"), c.get("section"),
                 c.get("enrollmentCode"), c.get("courseState"),
                 c.get("updateTime"), now_ts),
            )
    db.set_meta("classroom_last_sync", str(now_ts))
    log_event("classroom.sync", "courses synced", count=len(courses), forced=force)
    return [dict(r) for r in db.query("SELECT * FROM courses ORDER BY name")]


def sync_coursework(course_id: str, force: bool = False) -> list[dict]:
    row = db.query_one(
        "SELECT MAX(fetched_at) AS latest FROM coursework WHERE course_id = ?",
        (course_id,),
    )
    now_ts = time.time()
    if row and row["latest"] and (now_ts - row["latest"]) < DEFAULT_TTL_SECONDS and not force:
        return _active_coursework(course_id)

    token = _token()
    items = gc.list_assignments(token, course_id)
    with db.transaction() as conn:
        for a in items:
            due = a.get("dueDate")
            due_str = None
            if due:
                due_time = a.get("dueTime") or {}
                due_str = f"{due.get('year')}-{due.get('month', 0):02d}-{due.get('day', 0):02d}" \
                          f" {due_time.get('hours', 0):02d}:{due_time.get('minutes', 0):02d}"
            conn.execute(
                """
                INSERT INTO coursework(id, course_id, title, max_points, state,
                    due_date, alternate_link, description, update_time, fetched_at,
                    classroom_title, sync_state, allowlisted)
                VALUES(?,?,?,?,?,?,?,?,?,?,?, 'ACTIVE',
                    COALESCE((SELECT allowlisted FROM coursework WHERE id = ?), 0))
                ON CONFLICT(id) DO UPDATE SET
                    -- Google's data is refreshed every sync, but the teacher's
                    -- custom display name ALWAYS wins (COALESCE order), so a
                    -- resync can never wipe Math Correct configuration.
                    classroom_title=excluded.classroom_title,
                    title=COALESCE(coursework.custom_title,
                                   excluded.classroom_title, coursework.title),
                    max_points=excluded.max_points,
                    state=excluded.state, due_date=excluded.due_date,
                    alternate_link=excluded.alternate_link,
                    description=excluded.description,
                    update_time=excluded.update_time,
                    fetched_at=excluded.fetched_at,
                    -- It is back in Classroom: revive it (history intact).
                    sync_state='ACTIVE',
                    deleted_detected_at=NULL, archived_at=NULL,
                    allowlisted=COALESCE(
                        (SELECT allowlisted FROM coursework WHERE id = excluded.id),
                        excluded.allowlisted)
                """,
                (a["id"], course_id, a.get("title", "Untitled assignment"),
                 a.get("maxPoints"), a.get("state"), due_str,
                 a.get("alternateLink"), a.get("description"),
                 a.get("updateTime"), now_ts,
                 a.get("title", "Untitled assignment"), a["id"]),
            )
        # An assignment Classroom no longer lists is DELETED_IN_CLASSROOM, NOT
        # deleted. We archive it so its historical corrections, grades and
        # reports survive, remove it from active selection + automation, and
        # stop polling it. Guarded by a non-empty list so a truncated/failed
        # response can never archive a whole course's assignments.
        if items:
            live = {str(a["id"]) for a in items}
            existing = [r["id"] for r in conn.execute(
                "SELECT id FROM coursework WHERE course_id = ? "
                "AND sync_state != 'DELETED_IN_CLASSROOM'",
                (course_id,)).fetchall()]
            gone = [cid for cid in existing if cid not in live]
            for cid in gone:
                conn.execute(
                    "UPDATE coursework SET sync_state = 'DELETED_IN_CLASSROOM', "
                    "deleted_detected_at = ?, archived_at = ?, allowlisted = 0 "
                    "WHERE id = ?", (now_ts, now_ts, cid))
                # Remove it from active automation monitoring so the scheduler
                # never polls/corrects a submission that cannot be written to.
                conn.execute(
                    "UPDATE automation_assignments SET enabled = 0, "
                    "updated_at = ? WHERE coursework_id = ?", (now_ts, cid))
            if gone:
                log_event("classroom.sync", "coursework archived (deleted in "
                          "classroom)", course_id=course_id, archived=len(gone))
    log_event("classroom.sync", "coursework synced", course_id=course_id, count=len(items))
    return _active_coursework(course_id)


def _active_coursework(course_id: str) -> list[dict]:
    """Coursework Classroom still lists (archived ones are excluded).

    A DELETED_IN_CLASSROOM assignment is kept in the database for its history,
    but it is never returned as active work: the sync must not re-request an
    assignment Google has deleted (it answers HTTP 400 forever).
    """
    return [dict(r) for r in db.query(
        "SELECT * FROM coursework WHERE course_id = ? "
        "AND (sync_state IS NULL OR sync_state != 'DELETED_IN_CLASSROOM') "
        "ORDER BY update_time DESC", (course_id,))]


def sync_submissions(course_id: str, coursework_id: str, force: bool = False) -> list[dict]:
    row = db.query_one(
        "SELECT MAX(fetched_at) AS latest FROM submissions WHERE coursework_id = ?",
        (coursework_id,),
    )
    now_ts = time.time()
    if row and row["latest"] and (now_ts - row["latest"]) < DEFAULT_TTL_SECONDS and not force:
        return [dict(r) for r in db.query(
            "SELECT * FROM submissions WHERE coursework_id = ? ORDER BY id",
            (coursework_id,),
        )]

    token = _token()
    subs = gc.list_turned_in_submissions(token, course_id, coursework_id)
    with db.transaction() as conn:
        for s in subs:
            attachment_ids = gc.classroom_attachment_ids(s)
            conn.execute(
                """
                INSERT INTO submissions(id, coursework_id, course_id, user_id,
                    state, late, update_time, attachment_ids, fetched_at)
                VALUES(?,?,?,?,?,?,?,?,?)
                ON CONFLICT(id) DO UPDATE SET
                    state=excluded.state, late=excluded.late,
                    update_time=excluded.update_time,
                    attachment_ids=excluded.attachment_ids,
                    fetched_at=excluded.fetched_at
                """,
                (s["id"], coursework_id, course_id, s.get("userId"),
                 s.get("state"), int(bool(s.get("late"))),
                 s.get("updateTime"), json.dumps(attachment_ids), now_ts),
            )
    log_event("classroom.sync", "submissions synced",
              coursework_id=coursework_id, count=len(subs))
    return [dict(r) for r in db.query(
        "SELECT * FROM submissions WHERE coursework_id = ? ORDER BY id",
        (coursework_id,),
    )]


def student_name(user_id: str, token: str | None = None) -> str:
    """Resolve a student name, cached in the students table (legacy parity)."""
    row = db.query_one("SELECT name FROM students WHERE id = ?", (user_id,))
    if row and row["name"]:
        return row["name"]
    try:
        token = token or _token()
        profile = gc.get_user_profile(token, user_id)
        name = profile.get("name", {}).get("fullName", user_id)
    except Exception:
        name = user_id
    db.execute(
        "INSERT INTO students(id, name, fetched_at) VALUES(?,?,?) "
        "ON CONFLICT(id) DO UPDATE SET name=excluded.name, fetched_at=excluded.fetched_at",
        (user_id, name, time.time()),
    )
    return name


def set_allowlist(coursework_id: str, allowed: bool) -> None:
    db.execute("UPDATE coursework SET allowlisted = ? WHERE id = ?",
               (int(allowed), coursework_id))
    log_event("classroom.sync", "allowlist updated",
              coursework_id=coursework_id, allowed=allowed)


def last_sync() -> float | None:
    raw = db.get_meta("classroom_last_sync")
    return float(raw) if raw else None
