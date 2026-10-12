"""Student profile API tests: the profile must never lie about submissions.

Core invariants:
  * submission counts come ONLY from verified Classroom states,
  * a local correction result can never make a student look "submitted",
  * missing Classroom data is reported as unavailable (never as 0% or 100%),
  * nicknames are local and never overwrite the official Classroom name.
"""

from __future__ import annotations

import time

import pytest

from app import db
from app.classroom import submissions as cs


ROSTER = {
    "u1": {"user_id": "u1", "name": "Ada Lovelace", "email": "ada@school.org"},
    "u2": {"user_id": "u2", "name": "Ben Bray", "email": "ben@school.org"},
    "u3": {"user_id": "u3", "name": "Cleo Chan", "email": "cleo@school.org"},
}


def _sub(sid: str, user: str, state: str, attachments: list[dict] | None = None) -> dict:
    return {
        "id": sid,
        "userId": user,
        "state": state,
        "late": False,
        "creationTime": "2026-09-01T08:00:00Z",
        "updateTime": "2026-09-01T09:00:00Z",
        "assignmentSubmission": {"attachments": attachments or []},
    }


def seed_classroom() -> None:
    """u1 turned in, u2 opened only (CREATED), u3 roster-only (NOT_STARTED)."""
    subs = [
        _sub("s1", "u1", "TURNED_IN",
             [{"driveFile": {"id": "f1", "title": "hw.pdf"}}]),
        _sub("s2", "u2", "CREATED"),
    ]
    cs.store_sync_result("c1", "w1", ROSTER, subs)


def add_result(student_id: str, submission_id: str | None = None,
               earned: int = 8, available: int = 10) -> None:
    now = time.time()
    with db.transaction() as conn:
        job_id = f"job-{student_id}-{now}"
        conn.execute(
            "INSERT INTO correction_jobs(id, submission_id, coursework_id, "
            "course_id, student_id, student_name, status, created_at, "
            "updated_at) VALUES(?,?,?,?,?,?, 'COMPLETED', ?, ?)",
            (job_id, submission_id, "w1", "c1", student_id, "Student",
             now, now))
        conn.execute(
            "INSERT INTO results(id, job_id, coursework_id, submission_id, "
            "student_id, student_name, earned, available, percentage, "
            "provisional_grade, result_json, verification_json, created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (f"res-{student_id}-{now}", job_id, "w1", submission_id,
             student_id, "Student", earned, available,
             earned / available * 100, "8", '{"questions": []}',
             '{"status": "VALID"}', now))


@pytest.fixture
def client(fresh_state, monkeypatch):
    from app.config import get_config, save_config
    cfg = get_config()
    cfg.mock_mode = True
    save_config(cfg)
    from app.config import get_secrets
    get_secrets().set_gemini_keys(["AIzaMockKey1234567890abcdef"])

    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app.api import routes_all, student_routes
    from app.classroom import tracking_routes

    app = FastAPI()
    routes_all.register(app)
    tracking_routes.register(app)
    student_routes.register(app)

    # same error envelope as the real app (app.main registers this)
    from fastapi.responses import JSONResponse
    from app.errors import AppError

    @app.exception_handler(AppError)
    async def _app_error_handler(request, exc: AppError):
        return JSONResponse(status_code=exc.status, content=exc.to_dict())

    with TestClient(app) as test_client:
        yield test_client


# ---------------------------------------------------------------- tests

def test_list_uses_verified_classroom_states(client):
    seed_classroom()
    data = client.get("/api/students").json()
    by_id = {s["student_id"]: s for s in data["students"]}

    assert by_id["u1"]["submitted"] == 1 and by_id["u1"]["assignments"] == 1
    assert by_id["u2"]["submitted"] == 0 and by_id["u2"]["assignments"] == 1
    assert by_id["u3"]["submitted"] == 0 and by_id["u3"]["assignments"] == 1
    assert by_id["u1"]["display_name"] == "Ada Lovelace"


def test_local_correction_never_marks_student_submitted(client):
    """The headline failure mode: a local result must not fake a submission."""
    seed_classroom()
    add_result("u2", submission_id="s2")   # corrected locally, NEVER submitted

    data = client.get("/api/students").json()
    u2 = next(s for s in data["students"] if s["student_id"] == "u2")
    assert u2["submitted"] == 0            # Classroom says not submitted
    assert u2["corrections"] == 1          # local truth still shown separately

    profile = client.get("/api/students/u2").json()
    assert profile["classroom"]["submitted"] == 0
    assert profile["classroom"]["not_submitted"] == 1
    assert profile["classroom"]["submitted_percentage"] == 0.0
    assert profile["corrections"]["total"] == 1
    assert profile["classroom"]["assignments"][0]["status"] == "CREATED"


def test_profile_without_classroom_data_says_unavailable(client):
    """A student known only from local corrections must not get a fake 0%."""
    add_result("u9")
    profile = client.get("/api/students/u9").json()
    assert profile["classroom"]["available"] is False
    assert profile["classroom"]["submitted_percentage"] is None
    assert profile["classroom"]["assignments_total"] == 0
    assert "unavailable" in profile["sources"]["submission_status"].lower() \
        or "sync" in profile["sources"]["submission_status"].lower()
    assert profile["corrections"]["total"] == 1


def test_reclaimed_work_is_not_counted_as_submitted(client):
    subs = [_sub("s5", "u1", "RECLAIMED_BY_STUDENT")]
    cs.store_sync_result("c1", "w1", {"u1": ROSTER["u1"]}, subs)
    profile = client.get("/api/students/u1").json()
    assert profile["classroom"]["submitted"] == 0
    assert profile["classroom"]["assignments"][0]["status"] == "RECLAIMED"


def test_nickname_is_local_and_never_overwrites_official_name(client):
    seed_classroom()
    updated = client.put("/api/students/u1/nickname",
                         json={"nickname": "Ada"}).json()
    assert updated["nickname"] == "Ada"
    assert updated["display_name"] == "Ada"
    assert updated["official_name"] == "Ada Lovelace"   # untouched

    row = db.query_one("SELECT name, nickname FROM students WHERE id = 'u1'")
    assert row["name"] == "Ada Lovelace"
    assert row["nickname"] == "Ada"

    # clearing works and keeps the official name
    cleared = client.put("/api/students/u1/nickname", json={"nickname": ""})
    assert cleared.json()["nickname"] is None
    assert cleared.json()["official_name"] == "Ada Lovelace"


def test_profile_labels_every_source(client):
    seed_classroom()
    add_result("u1", submission_id="s1")
    profile = client.get("/api/students/u1").json()
    sources = profile["sources"]
    assert "Classroom" in sources["submission_status"]
    assert "correction" in sources["scores"].lower()
    assert profile["corrections"]["overall_percentage"] is not None


def test_unknown_student_404(client):
    assert client.get("/api/students/nope").status_code == 404


def test_profile_contains_no_key_material(client):
    seed_classroom()
    body = client.get("/api/students").text + client.get("/api/students/u1").text
    assert "AIzaMockKey" not in body


# ------------------------------------------------------- per-student report

def test_report_endpoint_returns_pdf(client):
    """The per-student tracking report exports a real PDF (§13/§14)."""
    seed_classroom()
    add_result("u1", "s1")
    resp = client.get("/api/students/u1/report?format=pdf")
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/pdf"
    body = resp.content
    assert body.startswith(b"%PDF-") and b"endobj" in body


def test_report_endpoint_returns_csv(client):
    seed_classroom()
    resp = client.get("/api/students/u1/report?format=csv")
    assert resp.status_code == 200
    assert "assignment" in resp.text


def test_report_unknown_student_404(client, fresh_state, monkeypatch):
    resp = client.get("/api/students/nope/report?format=pdf")
    assert resp.status_code == 404

