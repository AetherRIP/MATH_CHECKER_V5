"""Google Classroom grading extension: assignment total + Correct All workflow.

Covers the two behaviours added on top of the existing Classroom machinery:

  * Section 1 — editing the assignment's REAL Google Classroom total points
    through the API, confirmed from Google's response, with local refresh and
    a hard failure when Google does not confirm the change.
  * Sections 6/7 — the Correct All Submitted preview (confirmation counts) and
    the rule that already-corrected submissions are NOT re-corrected unless the
    teacher explicitly asks for a recheck.

No live Google calls: ``classroom_request`` is monkeypatched and its recorded
arguments are asserted, so we prove the request we send is the real one.
"""

from __future__ import annotations

import time

import pytest

from app import db
from app.classroom import submissions as cs

ROSTER = {
    "u1": {"user_id": "u1", "name": "Ada Lovelace", "email": "ada@school.org"},
    "u2": {"user_id": "u2", "name": "Ben Bray", "email": "ben@school.org"},
}


def _sub(sid, user, state, attachments=None):
    return {
        "id": sid, "userId": user, "state": state, "late": False,
        "creationTime": "2026-09-01T08:00:00Z",
        "updateTime": "2026-09-01T09:00:00Z",
        "assignmentSubmission": {"attachments": attachments or []},
    }


def seed_assignment(max_points: float = 20.0) -> None:
    subs = [
        _sub("s1", "u1", "TURNED_IN", [{"driveFile": {"id": "f1", "title": "hw1.pdf"}}]),
        _sub("s2", "u2", "TURNED_IN", [{"driveFile": {"id": "f2", "title": "hw2.pdf"}}]),
    ]
    cs.store_sync_result("c1", "w1", ROSTER, subs)
    now = time.time()
    db.execute("INSERT INTO courses(id, name, section, fetched_at) "
               "VALUES('c1','Maths 10A','A',?) ON CONFLICT(id) DO NOTHING", (now,))
    db.execute(
        "INSERT INTO coursework(id, course_id, title, max_points, due_date, "
        "allowlisted, fetched_at) VALUES('w1','c1','Algebra HW',?,"
        "'2026-09-10 18:00',1,?) ON CONFLICT(id) DO NOTHING", (max_points, now))


def add_result(student_id: str, submission_id: str, earned: int = 16,
               available: int = 20) -> None:
    now = time.time()
    with db.transaction() as conn:
        job_id = f"job-{student_id}"
        conn.execute(
            "INSERT INTO correction_jobs(id, submission_id, coursework_id, "
            "course_id, student_id, student_name, status, created_at, updated_at) "
            "VALUES(?,?,?,?,?,?, 'COMPLETED', ?, ?)",
            (job_id, submission_id, "w1", "c1", student_id, "Student", now, now))
        conn.execute(
            "INSERT INTO results(id, job_id, coursework_id, submission_id, "
            "student_id, student_name, earned, available, percentage, "
            "provisional_grade, result_json, created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (f"res-{student_id}", job_id, "w1", submission_id, student_id, "Student",
             earned, available, earned / available * 100, "16",
             '{"questions": []}', now))


class _AuthState:
    connected = True
    client_configured = True
    problem = None
    email = "teacher@school.org"


@pytest.fixture
def client(fresh_state, monkeypatch):
    """App with the product routes registered and Classroom auth faked."""
    import app.api.routes_all as ra
    from app.classroom import auth as auth_mod
    from app.config import get_config, save_config

    cfg = get_config()
    cfg.mock_mode = False
    cfg.role = "developer"
    save_config(cfg)

    monkeypatch.setattr(ra, "auth_state", lambda: _AuthState())
    monkeypatch.setattr(auth_mod, "get_access_token", lambda: "fake-token")

    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from fastapi.responses import JSONResponse
    from app.api import routes_all, product_routes
    from app.errors import AppError

    app = FastAPI()
    routes_all.register(app)
    product_routes.register(app)

    @app.exception_handler(AppError)
    async def _handler(request, exc: AppError):
        return JSONResponse(status_code=exc.status, content=exc.to_dict())

    with TestClient(app) as test_client:
        yield test_client


def _mark_scheme(coursework_id: str = "w1") -> None:
    from app import config as app_config
    scheme_dir = app_config.DATA_DIR / "mark_schemes"
    scheme_dir.mkdir(parents=True, exist_ok=True)
    (scheme_dir / f"{coursework_id}.pdf").write_bytes(b"%PDF-1.4 scheme")


# ------------------------------------------------------- Section 1: total points

def test_update_classroom_total_sends_real_patch_and_refreshes(client, monkeypatch):
    seed_assignment(max_points=20)
    from app.classroom import client as gc

    calls = []

    def fake_request(method, url, token, **kwargs):
        calls.append((method, url, kwargs))
        if method == "PATCH":
            return {"id": "w1", "maxPoints": 10}
        return {"id": "w1", "maxPoints": 10}

    monkeypatch.setattr(gc, "classroom_request", fake_request)

    r = client.post("/api/classroom/assignments/w1/classroom-total",
                    json={"max_points": 10})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["max_points"] == 10
    assert body["verified"] is True
    assert body["assignment"]["max_points"] == 10

    patched = [c for c in calls if c[0] == "PATCH"]
    assert patched, "no PATCH was sent to Google"
    method, url, kwargs = patched[0]
    assert url.endswith("/courses/c1/courseWork/w1")
    assert kwargs["params"] == {"updateMask": "maxPoints"}
    assert kwargs["json"] == {"maxPoints": 10.0}

    # local cache refreshed so every surface shows the confirmed value
    row = db.query_one("SELECT max_points FROM coursework WHERE id = 'w1'")
    assert row["max_points"] == 10


def test_update_classroom_total_fails_when_google_does_not_confirm(client, monkeypatch):
    seed_assignment(max_points=20)
    from app.classroom import client as gc

    # Google keeps returning the OLD value — we must not claim success.
    monkeypatch.setattr(gc, "classroom_request",
                        lambda *a, **k: {"id": "w1", "maxPoints": 20})
    r = client.post("/api/classroom/assignments/w1/classroom-total",
                    json={"max_points": 10})
    assert r.status_code == 502, r.text
    assert r.json()["code"] == "classroom_api_error"
    # local value unchanged (still whatever Google actually has)
    row = db.query_one("SELECT max_points FROM coursework WHERE id = 'w1'")
    assert row["max_points"] == 20


def test_update_classroom_total_rejects_bad_number(client):
    seed_assignment(max_points=20)
    r = client.post("/api/classroom/assignments/w1/classroom-total",
                    json={"max_points": 0})
    assert r.status_code == 400
    assert r.json()["code"] == "bad_max_points"


def test_update_classroom_total_unknown_assignment(client):
    r = client.post("/api/classroom/assignments/nope/classroom-total",
                    json={"max_points": 10})
    assert r.status_code == 404


# ------------------------------------------- Sections 6/7: preview + skip rule

def test_correct_all_preview_separates_already_corrected(client):
    seed_assignment()
    _mark_scheme()
    add_result("u1", "s1", earned=16, available=20)  # s1 already corrected

    r = client.get("/api/assignments/w1/correct-all/preview")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["counts"]["not_corrected"] == 1     # only s2 remains
    assert body["counts"]["already_corrected"] == 1
    assert body["will_correct"] == 1
    assert body["can_start"] is True and body["blockers"] == []
    assert [e["submission_id"] for e in body["eligible"]] == ["s2"]


def test_correct_all_preview_reports_blockers(client):
    seed_assignment()
    db.execute("UPDATE coursework SET allowlisted = 0 WHERE id = 'w1'")
    r = client.get("/api/assignments/w1/correct-all/preview")
    assert r.status_code == 200
    body = r.json()
    assert body["can_start"] is False
    assert any("allowlist" in b.lower() for b in body["blockers"])
    assert any("mark scheme" in b.lower() for b in body["blockers"])


def test_correct_all_skips_already_corrected_by_default(client, monkeypatch):
    seed_assignment()
    _mark_scheme()
    add_result("u1", "s1")

    enqueued: list[str] = []

    class FakeService:
        def enqueue_classroom_submission(self, **kwargs):
            enqueued.append(kwargs["submission_id"])
            return {"id": f"job-{kwargs['submission_id']}"}

    import app.api.routes_all as ra
    monkeypatch.setattr(ra, "_correction_service", lambda: FakeService())

    r = client.post("/api/classroom/courses/c1/assignments/w1/correct-all",
                    json={"refresh": False})
    assert r.status_code == 200, r.text
    body = r.json()
    # only the NOT-yet-corrected submission is corrected
    assert enqueued == ["s2"]
    assert body["enqueued"] == 1
    assert body["already_corrected"] == ["s1"]
    assert body["recheck"] is False


def test_correct_all_recheck_corrects_everything(client, monkeypatch):
    seed_assignment()
    _mark_scheme()
    add_result("u1", "s1")

    enqueued: list[str] = []

    class FakeService:
        def enqueue_classroom_submission(self, **kwargs):
            enqueued.append(kwargs["submission_id"])
            return {"id": f"job-{kwargs['submission_id']}"}

    import app.api.routes_all as ra
    monkeypatch.setattr(ra, "_correction_service", lambda: FakeService())

    r = client.post("/api/classroom/courses/c1/assignments/w1/correct-all",
                    json={"refresh": False, "recheck": True})
    assert r.status_code == 200, r.text
    body = r.json()
    assert sorted(enqueued) == ["s1", "s2"]
    assert body["recheck"] is True
    assert body["enqueued"] == 2
