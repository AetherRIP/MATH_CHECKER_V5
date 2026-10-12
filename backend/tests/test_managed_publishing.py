"""Managed vs External assignment behaviour + honest publishing.

These lock the single most important product guarantee: a Classroom operation
is reported as successful ONLY when Google confirmed it. All Classroom network
calls are faked; the logic under test is the classification and result mapping.
"""

from __future__ import annotations

import time

import pytest

from app import db
from app.classroom import client as gc
from app.classroom import managed as mg


def _seed_coursework(coursework_id: str = "w1", course_id: str = "c1",
                     max_points: float | None = 20) -> None:
    db.execute(
        "INSERT INTO courses(id, name, section, state, fetched_at) "
        "VALUES(?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET name=excluded.name",
        (course_id, "Maths 10A", "A", "ACTIVE", time.time()),
    )
    db.execute(
        "INSERT INTO coursework(id, course_id, title, max_points, state, "
        "allowlisted, fetched_at) VALUES(?,?,?,?,?,?,?) "
        "ON CONFLICT(id) DO UPDATE SET title=excluded.title",
        (coursework_id, course_id, "Algebra HW", max_points, "PUBLISHED", 1, time.time()),
    )


def _seed_result(submission_id: str, earned: int, available: int,
                 coursework_id: str = "w1", review: int = 0,
                 override: int = 0) -> None:
    job_id = f"job-{submission_id}"
    db.execute(
        "INSERT OR REPLACE INTO correction_jobs(id, submission_id, coursework_id, "
        "status, progress, attempts, max_attempts, created_at, updated_at) "
        "VALUES(?,?,?, 'COMPLETED', 1, 1, 3, ?, ?)",
        (job_id, submission_id, coursework_id, time.time(), time.time()),
    )
    db.execute(
        "INSERT INTO results(id, job_id, coursework_id, submission_id, student_id, "
        "student_name, earned, available, percentage, review_required, "
        "manual_override, result_json, created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (f"res-{submission_id}", job_id, coursework_id, submission_id, "u1",
         "Ada Lovelace", earned, available, 100 * earned / available,
         review, override, "{}", time.time()),
    )


# --------------------------------------------------------------- management

def test_management_defaults_to_external():
    assert mg.is_managed("nope") is False
    info = mg.managed_info("nope")
    assert info["managed"] is False
    assert info["kind"] == mg.EXTERNAL


def test_record_then_managed():
    mg.record_managed("w1", "c1", title="Algebra HW", google_account="t@school.org",
                      max_points=20)
    assert mg.is_managed("w1") is True
    info = mg.managed_info("w1")
    assert info["kind"] == mg.MANAGED
    assert info["google_account"] == "t@school.org"


def test_capabilities_differ_by_kind():
    managed = mg.capabilities_for(True)
    external = mg.capabilities_for(False)
    assert managed["final_grade"] is True
    assert external["final_grade"] is False
    assert external["reason"]
    # attachment is never offered by the StudentSubmission API
    assert managed["attach_report"] is False


# ------------------------------------------------------------- publish queue

def test_publish_queue_summarises_real_state():
    _seed_coursework()
    _seed_result("s1", 15, 20)
    _seed_result("s2", 8, 20, review=1)
    queue = mg.build_publish_queue("w1", "c1")
    assert queue["summary"]["total"] == 2
    assert queue["summary"]["needs_review"] == 1
    assert queue["summary"]["grades_ready"] == 1
    # suggested grade maps to Classroom points (max 20)
    by_id = {i["submission_id"]: i for i in queue["items"]}
    assert by_id["s1"]["suggested_grade"] == 15.0


def test_publish_queue_marks_external_capabilities():
    _seed_coursework()
    queue = mg.build_publish_queue("w1", "c1")
    assert queue["management"]["managed"] is False
    assert queue["capabilities"]["final_grade"] is False


# ------------------------------------------------------------------ publish

def test_publish_external_without_attempt_is_not_supported(monkeypatch):
    _seed_coursework()
    called = {"n": 0}

    def boom(*a, **k):
        called["n"] += 1
        raise AssertionError("must not call Classroom for a disallowed external op")

    monkeypatch.setattr(gc, "classroom_request", boom)
    result = mg.publish("w1", "c1", [
        {"type": "final_grade", "submission_id": "s1", "grade": 15},
    ])
    assert result["summary"]["not_supported"] == 1
    assert called["n"] == 0
    assert "not created by Math Checker" in result["results"][0]["message"]


def test_publish_managed_success(monkeypatch):
    _seed_coursework()
    mg.record_managed("w1", "c1", title="Algebra HW")
    monkeypatch.setattr("app.classroom.auth.get_access_token", lambda: "t")
    calls: list[tuple] = []

    def fake_request(method, url, token, **kwargs):
        calls.append((method, url, kwargs))
        return {"assignedGrade": kwargs.get("json", {}).get("assignedGrade")}

    monkeypatch.setattr(gc, "classroom_request", fake_request)
    result = mg.publish("w1", "c1", [
        {"type": "draft_grade", "submission_id": "s1", "grade": 12.5},
    ])
    assert result["summary"]["published"] == 1
    assert result["results"][0]["status"] == "published"
    # exactly one PATCH with updateMask=draftGrade
    assert len(calls) == 1
    assert calls[0][2]["params"]["updateMask"] == "draftGrade"


def test_publish_managed_final_sets_grade_and_returns(monkeypatch):
    _seed_coursework()
    mg.record_managed("w1", "c1")
    monkeypatch.setattr("app.classroom.auth.get_access_token", lambda: "t")
    methods: list[str] = []

    def fake_request(method, url, token, **kwargs):
        methods.append(method + ":" + url.rsplit("/", 1)[-1])
        return {}

    monkeypatch.setattr(gc, "classroom_request", fake_request)
    result = mg.publish("w1", "c1", [
        {"type": "final_grade", "submission_id": "s1", "grade": 18},
    ])
    assert result["summary"]["published"] == 1
    assert any(m.startswith("PATCH") for m in methods)
    assert "POST:s1:return" in methods


def test_publish_403_is_not_supported_not_success(monkeypatch):
    _seed_coursework()
    mg.record_managed("w1", "c1")
    monkeypatch.setattr("app.classroom.auth.get_access_token", lambda: "t")

    def forbidden(method, url, token, **kwargs):
        raise gc.ClassroomApiError(403, "ProjectPermissionDenied")

    monkeypatch.setattr(gc, "classroom_request", forbidden)
    result = mg.publish("w1", "c1", [
        {"type": "draft_grade", "submission_id": "s1", "grade": 10},
    ])
    assert result["summary"]["published"] == 0
    assert result["summary"]["not_supported"] == 1
    row = result["results"][0]
    assert row["http_status"] == 403
    # The true result is persisted for the audit trail.
    stored = db.query_one(
        "SELECT status FROM classroom_publish_ops WHERE coursework_id='w1' "
        "ORDER BY id DESC LIMIT 1")
    assert stored["status"] == "not_supported"


def test_publish_401_requires_auth(monkeypatch):
    _seed_coursework()
    mg.record_managed("w1", "c1")
    monkeypatch.setattr("app.classroom.auth.get_access_token", lambda: "t")

    def expired(method, url, token, **kwargs):
        raise gc.ClassroomApiError(401, "Invalid Credentials")

    monkeypatch.setattr(gc, "classroom_request", expired)
    result = mg.publish("w1", "c1", [
        {"type": "draft_grade", "submission_id": "s1", "grade": 10},
    ])
    assert result["summary"]["requires_auth"] == 1


def test_attach_report_is_explicitly_unsupported():
    _seed_coursework()
    mg.record_managed("w1", "c1")
    result = mg.publish("w1", "c1", [
        {"type": "attach_report", "submission_id": "s1"},
    ])
    assert result["summary"]["not_supported"] == 1
    assert "attach" in result["results"][0]["message"].lower()


# ------------------------------------------------------------ attention feed

def test_attention_reports_real_problems():
    from fastapi.testclient import TestClient
    from app.main import app

    _seed_coursework()
    _seed_result("s1", 5, 20, review=1)
    db.execute(
        "INSERT INTO correction_jobs(id, coursework_id, student_name, status, "
        "progress, attempts, max_attempts, error, created_at, updated_at) "
        "VALUES('jfail','w1','Ben Bray','FAILED',0,3,3,'model unavailable',?,?)",
        (time.time(), time.time()),
    )
    with TestClient(app) as client:
        data = client.get("/api/attention").json()
    types = {i["type"] for i in data["items"]}
    assert "correction_failed" in types
    assert "needs_review" in types
    assert data["total"] >= 2


def test_attention_endpoint_never_fabricates():
    from fastapi.testclient import TestClient
    from app.main import app

    _seed_coursework()  # assignment exists but nothing failed
    with TestClient(app) as client:
        data = client.get("/api/attention").json()
    # With no keys configured the only honest item is the missing-keys warning.
    types = {i["type"] for i in data["items"]}
    assert "correction_failed" not in types
    assert "needs_review" not in types
