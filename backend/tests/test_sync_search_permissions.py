"""Phase-3 integrated-pipeline tests (§3-§4, §15, §22-§23).

Covers the surfaces added to connect Google Classroom → students → submissions
→ reports/search as ONE system:

  * the assignment sync route must answer GET (EventSource cannot POST —
    registering POST-only was the root cause of the dead Sync button);
  * the FULL /classroom/sync route: staged progress, honest fatal errors,
    partial-failure marking, and idempotency (syncing N times never
    duplicates students/assignments/submissions);
  * /classroom/permissions: required vs granted scope reporting;
  * /search: grouped local-only results with correct deep-link routes.

All Google access is monkeypatched — no network, no real quota.
"""

from __future__ import annotations

import json

import pytest

# Reuse the fixtures/helpers from the tracking suite (pytest resolves fixtures
# from module namespace; tests/ is on sys.path via rootdir insertion).
from test_classroom_tracking import _sub, client, seeded_roster  # noqa: F401


def _events(body: str) -> list[dict]:
    """Parse SSE `data:` frames into dicts."""
    out = []
    for line in body.splitlines():
        if line.startswith("data: "):
            try:
                out.append(json.loads(line[6:]))
            except json.JSONDecodeError:
                pass
    return out


def _stream(client, url: str) -> tuple[int, str]:
    with client.stream("GET", url) as resp:
        body = b"".join(resp.iter_bytes()).decode()
    return resp.status_code, body


# =========================================================== GET sync (§3)

def test_assignment_sync_route_accepts_get(client, seeded_roster):
    """EventSource only speaks GET — POST-only registration made the UI's
    Sync button 404 silently (the original 'students never appear' bug)."""
    seeded_roster.set_subs([_sub("s1", "TURNED_IN", "u1")])
    status, body = _stream(client, "/api/classroom/courses/c1/assignments/w1/sync")
    assert status == 200
    assert '"type": "done"' in body
    assert '"type": "error"' not in body
    data = client.get("/api/classroom/tracking?coursework_id=w1").json()
    assert data["overview"]["total"] >= 3          # u1 real + u2/u3 synthetic


# ====================================================== full sync (§3/§22)

@pytest.fixture
def full_sync_mocks(monkeypatch, seeded_roster):
    """Mock the course/coursework fetches + per-assignment submissions.

    The mocks also persist courses/coursework rows exactly like the real
    sync does (FKs are ON), so the 'no duplicates' assertions are honest.
    """
    from app import db
    from app.classroom import client as gc
    from app.classroom import sync as csync

    def fake_courses(force=False):
        rows = [{"id": "c1", "name": "Algebra A"},
                {"id": "c2", "name": "Physics B"}]
        for r in rows:
            db.execute(
                "INSERT INTO courses(id, name, fetched_at) VALUES(?,?,?) "
                "ON CONFLICT(id) DO UPDATE SET name=excluded.name",
                (r["id"], r["name"], 1_700_000_000))
        return rows

    def fake_coursework(course_id, force=False):
        items = ([{"id": "w1", "title": "Algebra Homework 5"}]
                 if course_id == "c1" else
                 [{"id": "w9", "title": "Forces Homework"}])
        for it in items:
            db.execute(
                "INSERT INTO coursework(id, course_id, title, fetched_at) "
                "VALUES(?,?,?,?) ON CONFLICT(id) DO UPDATE SET "
                "title=excluded.title",
                (it["id"], course_id, it["title"], 1_700_000_000))
        return items

    monkeypatch.setattr(csync, "sync_courses", fake_courses)
    monkeypatch.setattr(csync, "sync_coursework", fake_coursework)

    def fake_page(token, course_id, coursework_id, page_token=None):
        return [_sub(f"s-{coursework_id}", "TURNED_IN", "u1",
                     attachments=[{"driveFile": {"id": f"f-{coursework_id}",
                                                 "title": "hw.pdf"}}])], None

    monkeypatch.setattr(gc, "list_student_submissions", fake_page)
    return csync


def test_full_sync_staged_flow_and_idempotency(client, full_sync_mocks):
    """Staged progress → done summary; running it twice must NOT create two
    copies of the same student/assignment/submission (§23)."""
    from app import db

    status, body = _stream(client, "/api/classroom/sync")
    assert status == 200
    events = _events(body)
    kinds = [e.get("type") for e in events]
    assert kinds[0] == "stage" and kinds[-1] == "done"
    # every stage announces itself for the UI progress line
    messages = [e.get("message", "") for e in events if e.get("type") == "stage"]
    assert any("courses" in m.lower() or "Fetching courses" in m for m in messages)
    assert any("assignments" in m.lower() for m in messages)
    assert any("students" in m.lower() for m in messages)
    assert any("submissions" in m.lower() for m in messages)
    assert any("database" in m.lower() for m in messages)

    done = events[-1]["summary"]
    assert done["courses"] == 2
    assert done["assignments"] == 2
    assert done["students"] == 6          # 3 per course (enrollments)
    assert done["submissions"] == 2
    assert done["partial"] is False
    assert done["errors"] == []
    assert db.get_meta("classroom_last_sync") is not None

    n_rows = db.query_one("SELECT COUNT(*) AS n FROM classroom_submissions")["n"]
    n_students = db.query_one("SELECT COUNT(*) AS n FROM students")["n"]
    assert n_rows == 6                    # (u1 real + u2/u3 synthetic) × 2 works
    assert n_students == 3                # stable Google IDs, upserted

    # ---- second run: identical data → identical counts (idempotent) ----
    status2, body2 = _stream(client, "/api/classroom/sync")
    assert status2 == 200
    assert '"type": "done"' in body2
    n_rows2 = db.query_one("SELECT COUNT(*) AS n FROM classroom_submissions")["n"]
    n_students2 = db.query_one("SELECT COUNT(*) AS n FROM students")["n"]
    assert n_rows2 == n_rows
    assert n_students2 == n_students
    # assignments stay unique too
    n_work = db.query_one("SELECT COUNT(*) AS n FROM coursework")["n"]
    assert n_work == 2


def test_full_sync_fatal_failure_reports_error_not_done(client, monkeypatch,
                                                        seeded_roster):
    """A fatal step must emit `error` with the failing stage — never 'done'."""
    from app.classroom import sync as csync

    def boom(force=False):
        raise RuntimeError("refresh token expired")

    monkeypatch.setattr(csync, "sync_courses", boom)
    status, body = _stream(client, "/api/classroom/sync")
    assert status == 200
    events = _events(body)
    assert events[-1]["type"] == "error"
    assert events[-1]["stage"] == "courses"
    assert "refresh token expired" in events[-1]["message"]
    assert not any(e.get("type") == "done" for e in events)


def test_full_sync_partial_failure_is_marked_partial(client, monkeypatch,
                                                     seeded_roster):
    """One broken assignment must not poison the run, but the done event must
    be flagged partial with the failing part named (§3 honesty)."""
    from app import db
    from app.classroom import client as gc
    from app.classroom import sync as csync

    def fake_courses(force=False):
        rows = [{"id": "c1", "name": "Algebra A"},
                {"id": "c2", "name": "Physics B"}]
        for r in rows:
            db.execute(
                "INSERT INTO courses(id, name, fetched_at) VALUES(?,?,?) "
                "ON CONFLICT(id) DO UPDATE SET name=excluded.name",
                (r["id"], r["name"], 1_700_000_000))
        return rows

    def fake_coursework(course_id, force=False):
        items = ([{"id": "w1", "title": "Algebra Homework 5"}]
                 if course_id == "c1" else
                 [{"id": "w9", "title": "Forces Homework"}])
        for it in items:
            db.execute(
                "INSERT INTO coursework(id, course_id, title, fetched_at) "
                "VALUES(?,?,?,?) ON CONFLICT(id) DO UPDATE SET title=excluded.title",
                (it["id"], course_id, it["title"], 1_700_000_000))
        return items

    monkeypatch.setattr(csync, "sync_courses", fake_courses)
    monkeypatch.setattr(csync, "sync_coursework", fake_coursework)

    def flaky(token, course_id, coursework_id, page_token=None):
        if coursework_id == "w9":
            raise gc.ClassroomApiError(503, "backend quota exceeded")
        return [_sub("s-w1", "TURNED_IN", "u1")], None

    monkeypatch.setattr(gc, "list_student_submissions", flaky)

    status, body = _stream(client, "/api/classroom/sync")
    assert status == 200
    events = _events(body)
    assert not any(e.get("type") == "error" for e in events)   # not fatal
    done = events[-1]
    assert done["type"] == "done"
    assert done["summary"]["partial"] is True
    assert len(done["summary"]["errors"]) == 1
    err = done["summary"]["errors"][0]
    assert err["part"].startswith("submissions")
    assert "Forces Homework" in err["part"]
    # the healthy assignment's data was still stored
    n = db.query_one(
        "SELECT COUNT(*) AS n FROM classroom_submissions WHERE coursework_id='w1'"
    )["n"]
    assert n >= 1


# ==================================================== permissions (§4)

def test_scopes_are_exactly_what_features_need():
    """No unrelated permissions, nothing missing for the implemented
    features (courses, coursework+submissions, rosters, Drive attachments).

    ``drive.file`` was added for the correction-report attachment: the app
    must UPLOAD the generated report to Drive. It is the narrowest scope
    that allows this - it grants access only to files this app creates, so
    it cannot reach any of the teacher's other Drive files (which stay behind
    ``drive.readonly``). ``drive`` (full) is deliberately NOT requested.
    """
    from app.classroom.auth import SCOPES
    assert set(SCOPES) == {
        "https://www.googleapis.com/auth/classroom.courses.readonly",
        "https://www.googleapis.com/auth/classroom.coursework.students",
        "https://www.googleapis.com/auth/drive.readonly",
        "https://www.googleapis.com/auth/drive.file",
        "https://www.googleapis.com/auth/classroom.rosters.readonly",
        "openid",
        "https://www.googleapis.com/auth/userinfo.email",
    }
    # Guard against a future "just use full drive access" shortcut.
    assert "https://www.googleapis.com/auth/drive" not in SCOPES


def test_permissions_not_connected_says_so(client, monkeypatch):
    """Not connected → checked=False with a clear detail, not a fake pass."""
    import app.classroom.tracking_routes as tr

    class St:          # binding-order independent: patch where it's used
        connected = False

    monkeypatch.setattr(tr, "auth_state", lambda: St())
    data = client.get("/api/classroom/permissions").json()
    assert data["checked"] is False
    assert data["connected"] is False
    assert data["required"]
    assert "connect" in data["detail"].lower()


def test_permissions_detects_missing_scopes(client, monkeypatch):
    import app.classroom.tracking_routes as tr

    class St:
        connected = True

    monkeypatch.setattr(tr, "auth_state", lambda: St())
    monkeypatch.setattr(tr, "get_access_token", lambda: "tok")
    monkeypatch.setattr(
        tr, "_granted_scopes",
        lambda token: ({"https://www.googleapis.com/auth/classroom.courses.readonly"},
                       None))
    data = client.get("/api/classroom/permissions").json()
    assert data["checked"] is True
    assert data["ok"] is False
    missing = " ".join(data["missing"])
    assert "classroom.coursework.students" in missing
    assert "drive.readonly" in missing
    assert "classroom.rosters.readonly" in missing
    assert "classroom.courses.readonly" not in missing


def test_permissions_all_granted_ok(client, monkeypatch):
    import app.classroom.tracking_routes as tr

    class St:
        connected = True

    monkeypatch.setattr(tr, "auth_state", lambda: St())
    monkeypatch.setattr(tr, "get_access_token", lambda: "tok")
    monkeypatch.setattr(tr, "_granted_scopes", lambda token: (set(tr.SCOPES), None))
    data = client.get("/api/classroom/permissions").json()
    assert data["checked"] is True
    assert data["ok"] is True
    assert data["missing"] == []


def test_permissions_unverifiable_is_not_a_fake_pass(client, monkeypatch):
    """tokeninfo failure → checked=False, ok=None (unknown ≠ granted)."""
    import app.classroom.tracking_routes as tr

    class St:
        connected = True

    monkeypatch.setattr(tr, "auth_state", lambda: St())
    monkeypatch.setattr(tr, "get_access_token", lambda: "tok")
    monkeypatch.setattr(tr, "_granted_scopes", lambda token: (None, "tokeninfo HTTP 403"))
    data = client.get("/api/classroom/permissions").json()
    assert data["checked"] is False
    assert data["ok"] is None
    assert "tokeninfo" in data["detail"]


# ======================================================= global search (§15)

@pytest.fixture
def seeded_search_data():
    """Local-only data: course, assignment, student (+nickname), submission,
    correction result — the exact sources /search reads."""
    from app import db

    db.execute(
        "INSERT INTO courses(id, name, section, fetched_at) VALUES(?,?,?,?)",
        ("c1", "Algebra A", "Section 2", 1_700_000_000))
    db.execute(
        "INSERT INTO coursework(id, course_id, title, max_points, description, "
        "update_time, fetched_at) VALUES(?,?,?,?,?,?,?)",
        ("w1", "c1", "Algebra Homework 5", 20, "Simultaneous equations",
         "2026-09-01T00:00:00Z", 1_700_000_000))
    db.execute(
        "INSERT INTO students(id, name, email, nickname, fetched_at) "
        "VALUES(?,?,?,?,?)",
        ("u1", "Rudransha Pratihar", "rud@school.org", "Rud", 1_700_000_000))
    db.execute(
        "INSERT INTO classroom_submissions(submission_id, course_id, "
        "coursework_id, student_id, student_name, student_email, current_state, "
        "late, assigned_grade, max_points, first_seen_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        ("s1", "c1", "w1", "u1", "Rudransha Pratihar", "rud@school.org",
         "TURNED_IN", 0, 18, 20, 1_700_000_000))
    db.execute(
        "INSERT INTO correction_jobs(id, status, created_at, updated_at) "
        "VALUES(?,?,?,?)",
        ("j1", "COMPLETED", 1_700_000_100, 1_700_000_200))
    db.execute(
        "INSERT INTO results(id, job_id, coursework_id, submission_id, "
        "student_id, student_name, earned, available, percentage, result_json, "
        "created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        ("r1", "j1", "w1", "s1", "u1", "Rudransha Pratihar", 17, 20, 85.0,
         "{}", 1_700_000_200))
    return True


def test_search_groups_students_assignments_courses(client,
                                                    seeded_search_data):
    # -- students: nickname is searchable, links to the profile route
    data = client.get("/api/search?q=Rud").json()
    students = next(g for g in data["groups"] if g["key"] == "students")
    item = students["items"][0]
    assert item["title"] == "Rud"                    # nickname wins for display
    assert "Rudransha Pratihar" in (item["subtitle"] or "")
    assert item["route"] == "#/students/u1"

    # -- assignments: matches title/description/course name → classroom deep link
    data = client.get("/api/search?q=Algebra").json()
    keys = [g["key"] for g in data["groups"]]
    assert "assignments" in keys and "courses" in keys
    assignment = next(g for g in data["groups"] if g["key"] == "assignments")["items"][0]
    assert assignment["title"] == "Algebra Homework 5"
    assert assignment["route"] == "#/classroom/c1/w1"
    course = next(g for g in data["groups"] if g["key"] == "courses")["items"][0]
    assert course["route"] == "#/classroom/c1"

    # -- corrections: student + assignment + score → the assignment page
    data = client.get("/api/search?q=Rudransha").json()
    corrections = next((g for g in data["groups"] if g["key"] == "corrections"), None)
    assert corrections is not None
    assert corrections["items"][0]["route"] == "#/classroom/c1/w1"
    assert "17/20" in corrections["items"][0]["subtitle"]


def test_search_empty_and_no_match(client, seeded_search_data):
    data = client.get("/api/search?q=").json()
    assert data == {"query": "", "groups": [], "total": 0}
    data = client.get("/api/search?q=zzz-no-such-thing").json()
    assert data["total"] == 0
    assert data["groups"] == []
