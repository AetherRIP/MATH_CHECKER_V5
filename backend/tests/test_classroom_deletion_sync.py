"""Classroom assignment DELETION / change synchronization (§1-§2).

When an assignment is deleted in Google Classroom it must disappear from
active selection and automation, while its historical corrections, grades and
reports are PRESERVED. A resync must never destroy the teacher's Math Correct
configuration (custom display name, automation flag, grade boundaries).
"""

from __future__ import annotations

import json
import time

import pytest

from app import db
from app.classroom import client as gc
from app.classroom import sync as csync

COURSE = "course-del"


def _seed_course():
    db.execute(
        "INSERT INTO courses(id, name, state, fetched_at) VALUES(?,?,?,?) "
        "ON CONFLICT(id) DO UPDATE SET state=excluded.state, fetched_at=excluded.fetched_at",
        (COURSE, "Maths 10A", "ACTIVE", time.time()))


def _assignment(cid, title, *, max_points=20, description="d", due_year=2026):
    return {
        "id": cid, "title": title, "maxPoints": max_points,
        "state": "PUBLISHED", "description": description,
        "alternateLink": f"https://classroom.google.com/{cid}",
        "updateTime": "2026-01-01T00:00:00Z",
        "dueDate": {"year": due_year, "month": 5, "day": 1},
        "dueTime": {"hours": 9, "minutes": 0},
    }


@pytest.fixture
def google(monkeypatch):
    """Fake the Classroom API so we control exactly what 'exists'."""
    state = {"items": []}

    import app.classroom.auth as auth
    monkeypatch.setattr(auth, "get_access_token", lambda: "tok")
    monkeypatch.setattr(gc, "list_assignments",
                        lambda token, course_id: [dict(a) for a in state["items"]])
    return state


def _seed_history(coursework_id):
    """A prior correction: job + result + tracking row for the assignment."""
    db.execute(
        "INSERT OR REPLACE INTO correction_jobs(id, submission_id, coursework_id, "
        "course_id, status, created_at, updated_at) VALUES(?,?,?,?,?,?,?)",
        ("job-del", "sub-del", coursework_id, COURSE, "COMPLETED", 0.0, 0.0))
    db.execute(
        "INSERT OR REPLACE INTO results(id, job_id, coursework_id, submission_id, "
        "student_id, student_name, earned, available, percentage, result_json, created_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        ("res-del", "job-del", coursework_id, "sub-del", "u1", "Ada",
         9, 10, 90.0, json.dumps({"questions": []}), 0.0))
    db.execute(
        "INSERT OR REPLACE INTO classroom_submissions(submission_id, course_id, "
        "coursework_id, student_id, student_name, current_state, data_quality, "
        "last_synced_at, first_seen_at, sync_version) VALUES(?,?,?,?,?,?,?,?,?,1)",
        ("sub-del", COURSE, coursework_id, "u1", "Ada", "TURNED_IN",
         "VERIFIED", time.time(), time.time()))


def _cleanup(coursework_ids):
    for cid in coursework_ids:
        db.execute("DELETE FROM classroom_submissions WHERE coursework_id = ?", (cid,))
        db.execute("DELETE FROM results WHERE coursework_id = ?", (cid,))
        db.execute("DELETE FROM correction_jobs WHERE coursework_id = ?", (cid,))
        db.execute("DELETE FROM automation_assignments WHERE coursework_id = ?", (cid,))
        db.execute("DELETE FROM coursework WHERE id = ?", (cid,))


class TestDeletedAssignmentIsArchivedNotLost:
    def test_a_deleted_assignment_is_archived_and_history_preserved(
            self, google, fresh_state):
        _seed_course()
        cid = "cw-algebra"
        try:
            google["items"] = [_assignment(cid, "Algebraic Fractions L1")]
            csync.sync_coursework(COURSE, force=True)
            row = db.query_one("SELECT * FROM coursework WHERE id = ?", (cid,))
            assert row["sync_state"] == "ACTIVE"
            assert row["classroom_title"] == "Algebraic Fractions L1"

            _seed_history(cid)
            db.execute(
                "INSERT INTO automation_assignments(coursework_id, course_id, "
                "enabled, created_at, updated_at) VALUES(?,?,1,?,?)",
                (cid, COURSE, time.time(), time.time()))

            # The teacher deletes it in Classroom; the API no longer lists it.
            google["items"] = []
            # Seed a sibling so the list is non-empty (a real, non-truncated
            # response still lists the remaining assignment).
            google["items"] = [_assignment("cw-other", "Geometry L2")]
            csync.sync_coursework(COURSE, force=True)

            row = dict(db.query_one("SELECT * FROM coursework WHERE id = ?", (cid,)))
            assert row is not None, "the local record must NOT be deleted"
            assert row["sync_state"] == "DELETED_IN_CLASSROOM"
            assert row["deleted_detected_at"] is not None
            # Removed from active selection / allowlist ...
            assert row["allowlisted"] == 0
            # ... and from automation monitoring.
            setting = db.query_one(
                "SELECT enabled FROM automation_assignments WHERE coursework_id = ?",
                (cid,))
            assert setting["enabled"] == 0
            # ... but the history survives.
            assert db.query_one("SELECT id FROM results WHERE id = 'res-del'")
            assert db.query_one(
                "SELECT 1 FROM classroom_submissions WHERE submission_id = 'sub-del'")
        finally:
            _cleanup([cid, "cw-other"])

    def test_an_archived_assignment_is_not_refetched(self, google, fresh_state):
        _seed_course()
        cid = "cw-arch"
        try:
            google["items"] = [_assignment(cid, "X")]
            csync.sync_coursework(COURSE, force=True)
            google["items"] = [_assignment("cw-keep", "Y")]
            csync.sync_coursework(COURSE, force=True)
            from app.classroom import submissions as csub
            calls = []
            import app.classroom.auth as auth
            summary = csub.sync_submission_tracking(COURSE, cid)
            assert summary.get("skipped") == "deleted_in_classroom"
        finally:
            _cleanup([cid, "cw-keep"])


class TestOtherClassroomChanges:
    def test_a_rename_updates_google_title_but_keeps_the_teacher_name(
            self, google, fresh_state):
        _seed_course()
        cid = "cw-rename"
        try:
            google["items"] = [_assignment(cid, "Original Name")]
            csync.sync_coursework(COURSE, force=True)
            # The teacher set a Math Correct display name.
            db.execute("UPDATE coursework SET custom_title = ?, title = ? WHERE id = ?",
                       ("Fractions (Y7)", "Fractions (Y7)", cid))
            # Teacher renames it in Classroom.
            google["items"] = [_assignment(cid, "Renamed In Classroom")]
            csync.sync_coursework(COURSE, force=True)
            row = db.query_one("SELECT title, classroom_title, custom_title "
                               "FROM coursework WHERE id = ?", (cid,))
            assert row["classroom_title"] == "Renamed In Classroom"
            assert row["title"] == "Fractions (Y7)", \
                "a sync must never wipe the teacher's display name"
        finally:
            _cleanup([cid])

    def test_max_points_and_due_date_changes_are_reflected(
            self, google, fresh_state):
        _seed_course()
        cid = "cw-points"
        try:
            google["items"] = [_assignment(cid, "Points", max_points=20, due_year=2026)]
            csync.sync_coursework(COURSE, force=True)
            before = db.query_one("SELECT max_points, due_date FROM coursework WHERE id = ?",
                                  (cid,))
            google["items"] = [_assignment(cid, "Points", max_points=50, due_year=2027)]
            csync.sync_coursework(COURSE, force=True)
            after = db.query_one("SELECT max_points, due_date FROM coursework WHERE id = ?",
                                 (cid,))
            assert after["max_points"] == 50
            assert after["due_date"] != before["due_date"]
        finally:
            _cleanup([cid])

    def test_an_empty_api_response_never_archives(self, google, fresh_state):
        """A truncated/failed response (empty list) must not wipe a course."""
        _seed_course()
        cid = "cw-empty"
        try:
            google["items"] = [_assignment(cid, "Keep Me")]
            csync.sync_coursework(COURSE, force=True)
            google["items"] = []              # suspicious empty response
            csync.sync_coursework(COURSE, force=True)
            row = db.query_one("SELECT sync_state FROM coursework WHERE id = ?", (cid,))
            assert row["sync_state"] == "ACTIVE", \
                "an empty list must not be treated as 'everything deleted'"
        finally:
            _cleanup([cid])


class TestSchedulerSkipsDeletedAssignments:
    def test_deleted_assignment_is_not_polled(self, google, fresh_state, monkeypatch):
        _seed_course()
        cid = "cw-skip"
        try:
            google["items"] = [_assignment(cid, "Gone")]
            csync.sync_coursework(COURSE, force=True)
            db.execute(
                "INSERT OR REPLACE INTO automation_assignments(coursework_id, "
                "course_id, enabled, created_at, updated_at) VALUES(?,?,1,?,?)",
                (cid, COURSE, time.time(), time.time()))
            google["items"] = [_assignment("cw-other2", "Present")]
            csync.sync_coursework(COURSE, force=True)

            from app.automation.scheduler import AutomationScheduler
            from types import SimpleNamespace
            synced = []
            cfg = SimpleNamespace(enabled=True, effective_interval=lambda: 10)
            sched = AutomationScheduler(
                sync_fn=lambda c, w: synced.append(w) or {},
                detect_fn=lambda c, w, t: {"queued_count": 0},
                cfg_fn=lambda: cfg,
                runner=SimpleNamespace(run_pending=lambda **k: 0))
            sched.run_cycle()
            assert cid not in synced, "a deleted assignment must never be polled"
        finally:
            _cleanup([cid, "cw-other2"])
