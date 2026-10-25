"""Assignment Details: the expected roster, classified from real records.

The page exists to answer three questions without guessing: who handed the
work in, who did not, and whose record cannot be trusted. These tests pin the
classification (and the fact that the summary is computed from the SAME rows
the table shows), because a summary that disagrees with the table is worse than
no summary at all.
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(fresh_state, monkeypatch):
    from app.config import get_config, save_config
    cfg = get_config()
    cfg.mock_mode = True
    save_config(cfg)
    from app.main import app
    with TestClient(app) as test_client:
        yield test_client


def _sub(sid: str, state: str, user: str, *, late: bool = False,
         attachments: list[dict] | None = None, update: str = "2026-09-01T09:00:00Z",
         **extra) -> dict:
    sub = {
        "id": sid,
        "userId": user,
        "state": state,
        "late": late,
        "creationTime": "2026-09-01T08:00:00Z",
        "updateTime": update,
        "assignmentSubmission": {"attachments": attachments or []},
        "submissionHistory": [],
    }
    sub.update(extra)
    return sub


ROSTER = {
    "u1": {"user_id": "u1", "name": "Ada Lovelace", "email": "ada@test.invalid"},
    "u2": {"user_id": "u2", "name": "Ben Bray", "email": "ben@test.invalid"},
    "u3": {"user_id": "u3", "name": "Cleo Chan", "email": "cleo@test.invalid"},
    "u4": {"user_id": "u4", "name": "Dev Doe", "email": "dev@test.invalid"},
}


def _seed_assignment(coursework_id: str = "cw-roster", course_id: str = "c1",
                     title: str = "Algebra Homework 5") -> None:
    from app import db
    db.execute("INSERT OR IGNORE INTO courses(id, name, section, fetched_at) "
               "VALUES(?,?,?,?)", (course_id, "10A Maths", "Set 1", 1.0))
    db.execute(
        "INSERT OR IGNORE INTO coursework(id, course_id, title, max_points, "
        "state, due_date, description, fetched_at) VALUES(?,?,?,?,?,?,?,?)",
        (coursework_id, course_id, title, 75.0, "PUBLISHED",
         "2026-10-30 20:59", "Factorising practice", 1.0))


def _store(roster: dict, subs: list[dict], coursework_id: str = "cw-roster",
           course_id: str = "c1") -> None:
    from app.classroom import submissions as cs
    cs.store_sync_result(course_id, coursework_id, roster, subs)


def _result(submission_id: str, coursework_id: str = "cw-roster",
            earned: float = 60, available: float = 75) -> None:
    from app import db
    db.execute(
        "INSERT OR IGNORE INTO correction_jobs(id, status, coursework_id, "
        "submission_id, created_at, updated_at) VALUES(?,?,?,?,?,?)",
        ("job-" + submission_id, "COMPLETED", coursework_id, submission_id, 1.0, 1.0))
    db.execute(
        "INSERT OR IGNORE INTO results(id, job_id, coursework_id, submission_id, "
        "student_name, earned, available, percentage, result_json, created_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?)",
        ("res-" + submission_id, "job-" + submission_id, coursework_id,
         submission_id, "Student", earned, available,
         round(100.0 * earned / available, 2), json.dumps({"questions": []}), 1.0))


def _job(submission_id: str, status: str, error: str | None = None) -> None:
    from app import db
    db.execute(
        "INSERT OR IGNORE INTO correction_jobs(id, status, coursework_id, "
        "submission_id, created_at, updated_at, error) VALUES(?,?,?,?,?,?,?)",
        ("job-" + submission_id, status, "cw-roster", submission_id, 1.0, 1.0, error))


def _payload(client, coursework_id: str = "cw-roster") -> dict:
    response = client.get(f"/api/assignments/{coursework_id}/students")
    assert response.status_code == 200, response.text
    return response.json()


class TestClassification:
    def test_submitted_draft_and_roster_only_are_three_different_answers(self, client):
        _seed_assignment()
        _store(ROSTER, [
            _sub("s1", "TURNED_IN", "u1"),
            _sub("s2", "CREATED", "u2"),
        ])
        data = _payload(client)
        by_id = {s["student_id"]: s for s in data["students"]}

        assert by_id["u1"]["completion"] == "COMPLETED"
        assert by_id["u1"]["submitted"] is True

        # A draft is opened-but-not-handed-in: not completed, and the reason
        # says so instead of leaving the row unexplained.
        assert by_id["u2"]["completion"] == "NOT_COMPLETED"
        assert by_id["u2"]["submitted"] is False
        assert "nothing was handed in" in by_id["u2"]["completion_reason"]

        # Enrolled with no submission resource: NOT missing.
        assert by_id["u3"]["completion"] == "NOT_COMPLETED"
        assert by_id["u3"]["synthetic"] is True
        assert by_id["u3"]["completion_reason"] is None

    def test_summary_is_computed_from_the_table_rows(self, client):
        _seed_assignment()
        _store(ROSTER, [
            _sub("s1", "TURNED_IN", "u1"),
            _sub("s2", "CREATED", "u2"),
        ])
        data = _payload(client)
        summary = data["summary"]
        students = data["students"]

        assert summary["expected"] == len(students) == 4
        assert summary["completed"] == sum(
            1 for s in students if s["completion"] == "COMPLETED")
        assert summary["not_completed"] == sum(
            1 for s in students if s["completion"] == "NOT_COMPLETED")
        assert summary["missing"] == sum(
            1 for s in students if s["completion"] == "MISSING")
        assert summary["unknown"] == sum(
            1 for s in students if s["completion"] == "UNKNOWN")
        assert summary["completion_percentage"] == pytest.approx(
            100.0 * summary["completed"] / summary["expected"], abs=0.1)

    def test_a_failed_correction_does_not_erase_the_submission(self, client):
        _seed_assignment()
        _store(ROSTER, [_sub("s1", "TURNED_IN", "u1")])
        _job("s1", "FAILED", "The Gemini request timed out.")

        data = _payload(client)
        row = next(s for s in data["students"] if s["student_id"] == "u1")
        assert row["completion"] == "COMPLETED"
        assert row["correction"]["state"] == "FAILED"
        assert "timed out" in row["correction"]["error"]

    def test_a_running_correction_is_not_reported_as_corrected(self, client):
        _seed_assignment()
        _store(ROSTER, [_sub("s1", "TURNED_IN", "u1")])
        _job("s1", "MARKING")

        row = next(s for s in _payload(client)["students"]
                   if s["student_id"] == "u1")
        assert row["completion"] == "COMPLETED"
        assert row["correction"]["state"] == "PROCESSING"
        assert row["correction"]["earned"] is None

    def test_marks_come_from_the_stored_result(self, client):
        _seed_assignment()
        _store(ROSTER, [_sub("s1", "TURNED_IN", "u1")])
        _result("s1", earned=60, available=75)

        row = next(s for s in _payload(client)["students"]
                   if s["student_id"] == "u1")
        assert row["correction"]["state"] == "CORRECTED"
        assert row["correction"]["earned"] == 60
        assert row["correction"]["available"] == 75

    def test_an_unreadable_hand_in_is_not_completed(self):
        from app.classroom import submissions as cs
        bucket, reason = cs.completion_bucket(
            {"status": "INVALID", "student_id": "u1", "data_quality": "VERIFIED"})
        assert bucket == cs.NOT_COMPLETED
        assert "cannot be read" in reason

    def test_a_sync_error_is_missing_rather_than_guessed(self):
        from app.classroom import submissions as cs
        bucket, reason = cs.completion_bucket(
            {"status": "SUBMITTED", "student_id": "u1",
             "sync_error": "403 from Google"})
        assert bucket == cs.MISSING
        assert "could not be synchronized" in reason

    def test_an_uninterpretable_state_is_unknown(self):
        from app.classroom import submissions as cs
        bucket, _ = cs.completion_bucket(
            {"status": "SOMETHING_NEW", "student_id": "u1",
             "data_quality": "VERIFIED"})
        assert bucket == cs.UNKNOWN

    def test_a_reclaimed_submission_is_not_completed(self):
        from app.classroom import submissions as cs
        bucket, reason = cs.completion_bucket(
            {"status": "RECLAIMED", "student_id": "u1",
             "data_quality": "VERIFIED"})
        assert bucket == cs.NOT_COMPLETED
        assert "took the work back" in reason


class TestDeduplication:
    def test_two_submission_records_for_one_student_do_not_inflate_the_roster(self, client):
        _seed_assignment()
        _store(ROSTER, [
            _sub("s-old", "CREATED", "u1", update="2026-09-01T09:00:00Z"),
            _sub("s-new", "TURNED_IN", "u1", update="2026-09-02T09:00:00Z"),
        ])
        data = _payload(client)

        assert data["summary"]["expected"] == 4
        assert len([s for s in data["students"] if s["student_id"] == "u1"]) == 1
        row = next(s for s in data["students"] if s["student_id"] == "u1")
        # The newest record wins, so the late hand-in is what the teacher sees.
        assert row["completion"] == "COMPLETED"
        assert data["summary"]["completed"] == 1
        assert data["summary"]["duplicate_records"] == 1
        assert any("merged" in w for w in data["warnings"])

    def test_a_synthetic_row_loses_to_the_real_submission_of_the_same_student(self, client):
        """The roster row created before the submission arrived must not keep
        the student listed as \"not started\" beside their own hand-in."""
        from app import db

        _seed_assignment()
        _store(ROSTER, [])  # everyone gets a synthetic roster row
        _store(ROSTER, [_sub("s1", "TURNED_IN", "u1")])

        rows = db.query("SELECT submission_id FROM classroom_submissions "
                        "WHERE coursework_id = 'cw-roster' AND student_id = 'u1'")
        assert len(rows) >= 2  # the synthetic row is still stored

        data = _payload(client)
        assert data["summary"]["expected"] == 4
        row = next(s for s in data["students"] if s["student_id"] == "u1")
        assert row["completion"] == "COMPLETED"
        assert row["synthetic"] is False


class TestAssignmentMetadata:
    def test_metadata_and_roster_are_returned_together(self, client):
        _seed_assignment()
        _store(ROSTER, [_sub("s1", "TURNED_IN", "u1")])

        data = _payload(client)
        assignment = data["assignment"]
        assert assignment["id"] == "cw-roster"
        assert assignment["title"] == "Algebra Homework 5"
        assert assignment["course_name"] == "10A Maths"
        assert assignment["description"] == "Factorising practice"
        assert assignment["due_date"] == "2026-10-30 20:59"
        assert assignment["max_points"] == 75.0
        assert data["sync"]["last_sync"] is not None
        assert data["sync"]["roster_known"] is True

    def test_a_never_synced_assignment_says_so(self, client):
        _seed_assignment(coursework_id="cw-empty")
        data = _payload(client, "cw-empty")
        assert data["summary"]["expected"] == 0
        assert any("never been synchronized" in w for w in data["warnings"])
        assert any("No students are recorded" in w for w in data["warnings"])

    def test_an_unknown_assignment_is_a_404(self, client):
        response = client.get("/api/assignments/does-not-exist/students")
        assert response.status_code == 404
        assert response.json()["code"] == "not_found"

    def test_student_rows_are_sorted_by_name(self, client):
        _seed_assignment()
        _store(ROSTER, [])
        names = [s["name"] for s in _payload(client)["students"]]
        assert names == sorted(names, key=str.casefold)
