"""Google Classroom error reporting: what counts as a failure (and what must not).

Three real defects, all found from the application's own log and UI:

  1. The Classroom page claimed "Missing OAuth client configuration" on a
     machine whose client WAS configured, because the page read
     ``health.configured`` while ``/api/classroom/status`` sends
     ``client_configured``. A required field that is always ``undefined`` is
     not a cosmetic detail - it hid a working install behind a false error.
  2. Two assignments failed submissions sync with HTTP 400 on every sync. One
     was DRAFT work (Classroom serves no submissions for unpublished work) and
     one had been deleted in Classroom but still existed locally. Both were
     reported as sync errors, so a healthy install carried a permanent red
     "2 sync errors" badge.
  3. An assignment Classroom no longer lists used to be HARD-DELETED, taking
     its correction history with it. It is now ARCHIVED (DELETED_IN_CLASSROOM):
     removed from active selection + automation, never re-requested, but the
     historical corrections, grades and reports are preserved.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import app.classroom.sync as classroom_sync
from app import db

ROOT = Path(__file__).resolve().parent.parent.parent
COURSE = "c-err-1"


@pytest.fixture
def client(fresh_state):
    """A TestClient over the real app (auth mocked per test where needed)."""
    from fastapi.testclient import TestClient

    from app.main import app
    with TestClient(app) as test_client:
        yield test_client
WORK = "cw-err-1"
GHOST = "cw-err-ghost"


def _seed_coursework() -> None:
    db.execute(
        "INSERT OR REPLACE INTO courses(id, name, section, fetched_at) "
        "VALUES(?,?,?,?)", (COURSE, "Mathematics", "O Level", 1.0))
    for cid, title, state in ((WORK, "Chapter 1 Lesson 1", "PUBLISHED"),
                              (GHOST, "ZZ_TEMP_delete_me", "PUBLISHED")):
        db.execute(
            "INSERT OR REPLACE INTO coursework(id, course_id, title, "
            "max_points, state, fetched_at) VALUES(?,?,?,?,?,?)",
            (cid, COURSE, title, 10.0, state, 1.0))


def _ids() -> set[str]:
    return {r["id"] for r in db.query(
        "SELECT id FROM coursework WHERE course_id = ?", (COURSE,))}


# ------------------------------------------------- 3. stale coursework pruning

class TestStaleCourseworkIsArchived:
    def test_work_classroom_no_longer_lists_is_archived(self, fresh_state,
                                                        monkeypatch):
        _seed_coursework()
        monkeypatch.setattr(
            "app.classroom.auth.get_access_token", lambda: "token")
        monkeypatch.setattr(classroom_sync.gc, "list_assignments",
                            lambda token, course_id: [
                                {"id": WORK, "title": "Chapter 1 Lesson 1",
                                 "maxPoints": 10, "state": "PUBLISHED"}])
        classroom_sync.sync_coursework(COURSE, force=True)
        assert WORK in _ids()
        # The deleted assignment is NOT hard-deleted: it is archived so its
        # history survives and it is never polled again.
        assert GHOST in _ids()
        row = db.query_one("SELECT sync_state FROM coursework WHERE id = ?",
                           (GHOST,))
        assert row["sync_state"] == "DELETED_IN_CLASSROOM"

    def test_an_empty_response_never_wipes_a_course(self, fresh_state,
                                                    monkeypatch):
        """A truncated or empty list must not delete a teacher's assignments."""
        _seed_coursework()
        monkeypatch.setattr(
            "app.classroom.auth.get_access_token", lambda: "token")
        monkeypatch.setattr(classroom_sync.gc, "list_assignments",
                            lambda token, course_id: [])
        classroom_sync.sync_coursework(COURSE, force=True)
        assert _ids() == {WORK, GHOST}

    def test_history_of_a_removed_assignment_is_preserved(self, fresh_state,
                                                          monkeypatch):
        """A Classroom deletion must NOT destroy the correction history."""
        _seed_coursework()
        db.execute(
            "INSERT OR REPLACE INTO classroom_submissions(submission_id, "
            "course_id, coursework_id, student_id, student_name, "
            "student_email, current_state, max_points, last_synced_at, "
            "first_seen_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
            ("sub-ghost", COURSE, GHOST, "u1", "Ada", "ada@school.org",
             "TURNED_IN", 10.0, 1.0, 1.0))
        monkeypatch.setattr(
            "app.classroom.auth.get_access_token", lambda: "token")
        monkeypatch.setattr(classroom_sync.gc, "list_assignments",
                            lambda token, course_id: [
                                {"id": WORK, "title": "Chapter 1 Lesson 1",
                                 "maxPoints": 10, "state": "PUBLISHED"}])
        classroom_sync.sync_coursework(COURSE, force=True)
        rows = db.query("SELECT submission_id FROM classroom_submissions "
                        "WHERE coursework_id = ?", (GHOST,))
        assert [r["submission_id"] for r in rows] == ["sub-ghost"], \
            "historical submission rows must survive a Classroom deletion"


# ------------------------------------- 2. unpublished / unavailable coursework

class TestUnavailableWorkIsNotAFailure:
    """The sync summary must separate "broken" from "not applicable"."""

    @pytest.fixture
    def sync_client(self, fresh_state, monkeypatch):
        from app.config import get_config, save_config, get_secrets
        cfg = get_config()
        cfg.mock_mode = True
        save_config(cfg)
        get_secrets().set_gemini_keys(["AIzaMockKey1234567890abcdef"])

        import app.classroom.auth as auth
        import app.api.routes_all as routes_mod

        class FakeState:
            connected = True
            email = "t@school.org"
            problem = None
            client_configured = True

        monkeypatch.setattr(auth, "auth_state", lambda: FakeState())
        monkeypatch.setattr(routes_mod, "auth_state", lambda: FakeState())

        import app.classroom.tracking_routes as tr
        # Both binding sites: the sync engine resolves the token through
        # app.classroom.auth, the route module holds its own reference.
        monkeypatch.setattr(auth, "get_access_token", lambda: "token")
        monkeypatch.setattr(tr, "get_access_token", lambda: "token")

        from app.main import app
        from fastapi.testclient import TestClient
        with TestClient(app) as test_client:
            yield test_client

    def test_a_draft_assignment_is_skipped_not_failed(self, sync_client,
                                                     monkeypatch):
        import app.classroom.tracking_routes as tr
        _seed_coursework()
        calls: list[str] = []

        def coursework_list(token, course_id):
            return [
                {"id": WORK, "title": "Published work", "maxPoints": 10,
                 "state": "PUBLISHED"},
                {"id": "cw-draft", "title": "Half-finished draft",
                 "maxPoints": 10, "state": "DRAFT"},
            ]

        def submissions(token, course_id, coursework_id, page_token=None):
            calls.append(coursework_id)
            return [], None

        monkeypatch.setattr(tr.classroom_sync.gc, "list_assignments",
                            coursework_list)
        monkeypatch.setattr(tr.classroom_sync.gc, "list_teacher_courses",
                            lambda token: [{"id": COURSE, "name": "Class",
                                            "courseState": "ACTIVE"}])
        monkeypatch.setattr(tr.gc, "list_course_students",
                            lambda token, course_id: [])
        monkeypatch.setattr(tr.gc, "list_student_submissions", submissions)

        with sync_client.stream("POST", "/api/classroom/sync") as resp:
            body = b"".join(resp.iter_bytes()).decode()
        assert '"type": "done"' in body, body[-500:]
        # Classroom is never even asked about the draft.
        assert "cw-draft" not in calls
        summary = body.split('"type": "done"')[1]
        assert '"errors": []' in summary
        assert '"partial": false' in summary
        assert "Draft" in summary  # named as unavailable, with the reason

    def test_a_deleted_assignment_is_unavailable_not_an_error(
            self, sync_client, monkeypatch):
        """HTTP 400/404 from Classroom is \"gone\", not \"sync broken\"."""
        import app.classroom.tracking_routes as tr
        from app.classroom.client import ClassroomApiError
        _seed_coursework()

        def boom(token, course_id, coursework_id, page_token=None):
            raise ClassroomApiError(400, '{"error": {"code": 400}}')

        monkeypatch.setattr(tr.classroom_sync.gc, "list_assignments",
                            lambda token, course_id: [
                                {"id": WORK, "title": "Gone work",
                                 "maxPoints": 10, "state": "PUBLISHED"}])
        monkeypatch.setattr(tr.classroom_sync.gc, "list_teacher_courses",
                            lambda token: [{"id": COURSE, "name": "Class",
                                            "courseState": "ACTIVE"}])
        monkeypatch.setattr(tr.gc, "list_course_students",
                            lambda token, course_id: [])
        monkeypatch.setattr(tr.gc, "list_student_submissions", boom)

        with sync_client.stream("POST", "/api/classroom/sync") as resp:
            body = b"".join(resp.iter_bytes()).decode()
        summary = body.split('"type": "done"')[1]
        assert '"errors": []' in summary
        assert '"unavailable"' in summary
        assert '"partial": false' in summary


# ---------------------------------------------- 1. the false OAuth error (UI)

class TestClassroomStatusFieldNames:
    """The page and the endpoint must agree on the field name.

    A mismatch here is invisible to the backend tests and shows the teacher a
    false, alarming error, so it is checked where it can actually regress: at
    the source of the page that renders it.
    """

    def test_the_status_endpoint_reports_client_configured(self, client):
        payload = client.get("/api/classroom/status").json()
        assert "client_configured" in payload
        assert isinstance(payload["client_configured"], bool)

    def test_the_classroom_page_reads_client_configured(self):
        page = (ROOT / "frontend" / "src" / "pages" / "ClassroomPage.tsx"
                ).read_text(encoding="utf-8")
        assert "client_configured" in page
        # The page once read `health.` + the wrong field name, which is always
        # undefined - so a configured install was told its client was missing.
        assert "health.configured" not in page
        assert "health.client_configured ?" in page

    def test_the_misleading_hardcoded_message_is_gone(self):
        page = (ROOT / "frontend" / "src" / "pages" / "ClassroomPage.tsx"
                ).read_text(encoding="utf-8")
        assert "Missing OAuth client configuration" not in page


# ------------------------------------- 4. a dead grant must not become a 500

class TestDeadGrantReachesTheTeacher:
    """Found in the production log: six "unhandled error" 500s whose message
    was the actionable "Reconnect Google Classroom…" instruction. Only routes
    that call routes_all._classroom_error translate it; every other endpoint
    that touches Classroom hid the fix behind a generic 500. The global
    handler is the safety net, with the SAME status + code.
    """

    def test_dead_grant_is_answered_as_reconnect_not_generic_500(self):
        import asyncio
        import json

        from starlette.requests import Request

        from app.main import unhandled_error_handler

        request = Request({"type": "http", "method": "GET", "path": "/",
                           "headers": [], "query_string": b"",
                           "server": ("test", 80), "client": ("test", 1),
                           "scheme": "http"})
        resp = asyncio.run(unhandled_error_handler(
            request,
            RuntimeError("Google authorization expired or was revoked. "
                         "Reconnect Google Classroom from the Classroom page.")))
        assert resp.status_code == 401
        body = json.loads(resp.body)
        assert body["code"] == "classroom_reconnect_required"
        assert "Reconnect Google Classroom" in body["message"]

        # The invalid_scope variant (new drive.file permission) too.
        resp2 = asyncio.run(unhandled_error_handler(
            request,
            RuntimeError("Google Classroom needs to be reconnected to grant "
                         "the permission that uploads correction reports to "
                         "Drive (drive.file). Click Connect Google Classroom "
                         "on the Classroom page and approve the new "
                         "permission.")))
        assert resp2.status_code == 401
        assert json.loads(resp2.body)["code"] == "classroom_reconnect_required"

    def test_a_real_internal_error_stays_a_500(self):
        import asyncio
        import json

        from starlette.requests import Request

        from app.main import unhandled_error_handler

        request = Request({"type": "http", "method": "GET", "path": "/",
                           "headers": [], "query_string": b"",
                           "server": ("test", 80), "client": ("test", 1),
                           "scheme": "http"})
        resp = asyncio.run(unhandled_error_handler(
            request, ZeroDivisionError("division by zero")))
        assert resp.status_code == 500
        assert json.loads(resp.body)["code"] == "internal_error"

    def test_the_guard_is_registered_as_its_own_403_gate(self):
        """Production 2026-10-12 09:12:50 logged an "unhandled error" 500 whose
        message was the testing-only refusal, so the UI reported a crash for
        the one condition whose fix it printed. The guard must have its own
        handler, answered as 403.
        """
        from app.main import app
        from app.testing_guard import PublicationBlocked

        assert PublicationBlocked in app.exception_handlers
        assert app.exception_handlers[PublicationBlocked] is not None

    def test_publication_blocked_is_answered_as_403_not_500(self):
        import asyncio
        import json

        from starlette.requests import Request

        from app.main import publication_blocked_handler
        from app.testing_guard import PublicationBlocked

        request = Request({"type": "http", "method": "POST",
                           "path": "/api/classroom/work", "headers": [],
                           "query_string": b"", "server": ("test", 80),
                           "client": ("test", 1), "scheme": "http"})
        exc = PublicationBlocked("POST https://classroom.googleapis.com/...",
                                 "Testing only mode is ON.")
        resp = asyncio.run(publication_blocked_handler(request, exc))
        assert resp.status_code == 403
        body = json.loads(resp.body)
        assert body["code"] == "testing_only_blocked"
        assert "Testing only mode is ON" in body["message"]

    def test_an_unhandled_error_records_the_route_and_the_traceback(self,
                                                                   caplog):
        """A one-line "unhandled error" cannot be reproduced: the request and
        the stack must be recorded (production logged exactly one such line,
        KeyError('status'), which no amount of log reading could locate)."""
        import asyncio
        import logging

        from starlette.requests import Request

        from app.main import unhandled_error_handler

        request = Request({"type": "http", "method": "POST",
                           "path": "/api/automation/jobs/abc/retry",
                           "headers": [], "query_string": b"",
                           "server": ("test", 80), "client": ("test", 1),
                           "scheme": "http"})
        try:
            {}["status"]              # the production KeyError('status')
        except KeyError as exc:
            caught = exc
        with caplog.at_level(logging.ERROR):
            resp = asyncio.run(unhandled_error_handler(request, caught))
        assert resp.status_code == 500
        record = next(r for r in caplog.records if r.message == "unhandled error")
        context = getattr(record, "context", {})
        assert context.get("path") == "/api/automation/jobs/abc/retry"
        assert context.get("method") == "POST"
        assert context.get("exc_type") == "KeyError"
        assert "KeyError" in context.get("traceback", "")

    def test_the_sign_in_screen_is_not_popped_by_a_dead_grant(self):
        """The frontend fires mc5:auth-required (sign-in screen) on any 401 —
        but a dead Google grant is not a dead local session, and swapping to
        sign-in hides the one instruction that fixes it."""
        api_ts = (ROOT / "frontend" / "src" / "api.ts").read_text(encoding="utf-8")
        assert 'parsed.code !== "classroom_reconnect_required"' in api_ts


# ---------------------- 5. assigning homework must never answer opaquely

ASSIGN_URL = "/api/classroom/courses/{course}/assignments/create"
TOTAL_URL = "/api/classroom/assignments/{coursework}/classroom-total"
OPAQUE = "Something went wrong processing this request"


class _ConnectedClassroom:
    """Just enough of the auth state for the connection gate to pass."""

    connected = True
    client_configured = True
    problem = None
    email = "teacher@school.org"


@pytest.fixture
def assign_client(fresh_state, monkeypatch):
    """The REAL app, with the Classroom CONNECTION faked and nothing else.

    ``requests.request`` is deliberately left alone: each test stubs the HTTP
    layer itself, so the real ``classroom_request`` guard and the real error
    translation are what run.
    """
    from fastapi.testclient import TestClient

    import app.api.routes_all as routes_mod
    from app.api import product_routes
    from app.classroom import auth as auth_mod
    from app.config import get_config, save_config
    from app.main import app

    cfg = get_config()
    cfg.mock_mode = False
    cfg.classroom.enabled = True
    cfg.teacher.onboarded = True
    save_config(cfg)

    monkeypatch.setattr(routes_mod, "auth_state", lambda: _ConnectedClassroom())
    monkeypatch.setattr(product_routes, "auth_state",
                        lambda: _ConnectedClassroom())
    monkeypatch.setattr(auth_mod, "get_access_token", lambda *a, **k: "fake-token")

    with TestClient(app) as test_client:
        yield test_client


def _fail_requests_with(exc: Exception):
    def boom(*args, **kwargs):
        raise exc
    return boom


def _seed_assignment(coursework_id: str = "cw-assign", course_id: str = "c1"):
    db.execute(
        "INSERT OR REPLACE INTO courses(id, name, section, fetched_at) "
        "VALUES(?,?,?,?)", (course_id, "Mathematics", "O Level", 1.0))
    db.execute(
        "INSERT OR REPLACE INTO coursework(id, course_id, title, max_points, "
        "state, allowlisted, fetched_at) VALUES(?,?,?,?,?,?,?)",
        (coursework_id, course_id, "Algebra HW", 20.0, "PUBLISHED", 1, 1.0))


class TestAssigningHomeworkIsNeverAnOpaque500:
    """Production 2026-10-12 09:12:50: a teacher assigning homework got

        "Something went wrong processing this request. The error has been
         logged for the developer diagnostics page."

    TWO different events produced that one sentence, and neither named a fix.
    The testing-only guard's deliberate refusal is one (it has its own 403
    gate), but the assign endpoint translated only ``ClassroomApiError`` and
    ``ValueError`` — so a Google that could not be reached at all (a DNS
    failure, a timeout, a reset connection, a revoked grant) fell straight
    through to the global 500. These drive the REAL route and pin what the
    teacher is told.
    """

    def test_a_google_that_cannot_be_reached_names_the_connection(
            self, assign_client, monkeypatch):
        import requests

        from app.classroom import client as gc

        monkeypatch.setattr(gc.requests, "request", _fail_requests_with(
            requests.exceptions.ConnectionError(
                "Failed to resolve classroom.googleapis.com")))
        response = assign_client.post(ASSIGN_URL.format(course="c1"),
                                      json={"title": "Algebra HW"})
        assert response.status_code == 502, response.text
        body = response.json()
        assert body["code"] == "classroom_error"
        assert "Could not reach Google Classroom" in body["message"]
        assert OPAQUE not in body["message"]

    def test_a_timeout_does_not_become_the_opaque_500(self, assign_client,
                                                      monkeypatch):
        import requests

        from app.classroom import client as gc

        monkeypatch.setattr(gc.requests, "request", _fail_requests_with(
            requests.exceptions.Timeout("read timed out")))
        response = assign_client.post(ASSIGN_URL.format(course="c1"),
                                      json={"title": "Algebra HW"})
        assert response.status_code == 502, response.text
        body = response.json()
        assert body["code"] == "classroom_error"
        assert "Could not reach Google Classroom" in body["message"]

    def test_a_revoked_grant_says_reconnect(self, assign_client, monkeypatch):
        """The one instruction that actually fixes it must reach the teacher."""
        from app.classroom import auth as auth_mod

        monkeypatch.setattr(auth_mod, "get_access_token", _fail_requests_with(
            RuntimeError("Google authorization expired or was revoked. "
                         "Reconnect Google Classroom from the Classroom page.")))
        response = assign_client.post(ASSIGN_URL.format(course="c1"),
                                      json={"title": "Algebra HW"})
        assert response.status_code == 401, response.text
        body = response.json()
        assert body["code"] == "classroom_reconnect_required"
        assert "Reconnect Google Classroom" in body["message"]
        assert OPAQUE not in body["message"]

    def test_the_testing_only_gate_keeps_its_own_403(
            self, assign_client, monkeypatch):
        """The guard is a deliberate LOCAL gate, not a Google failure, and it
        answers with the wording that names the fix (turn it off in Settings)."""
        from app.config import get_config

        get_config().testing.only = True
        response = assign_client.post(ASSIGN_URL.format(course="c1"),
                                      json={"title": "Algebra HW"})
        assert response.status_code == 403, response.text
        body = response.json()
        assert body["code"] == "testing_only_blocked"
        assert "TESTING ONLY" in body["message"]
        assert "Settings" in body["message"]
        assert OPAQUE not in body["message"]

    def test_a_google_refusal_keeps_its_own_explanation(
            self, assign_client, monkeypatch):
        from app.classroom import client as gc

        class _Denied:
            status_code = 403
            content = b'{"error":{"code":403,"message":"insufficient permission"}}'
            text = content.decode()

            @staticmethod
            def json():
                return {"error": {"code": 403}}

        monkeypatch.setattr(gc.requests, "request", lambda *a, **k: _Denied())
        response = assign_client.post(ASSIGN_URL.format(course="c1"),
                                      json={"title": "Algebra HW"})
        assert response.status_code == 502, response.text
        body = response.json()
        assert body["code"] == "classroom_api_error"
        assert "Google denied" in body["message"]

    def test_a_bad_title_is_still_a_400_not_a_google_call(self, assign_client,
                                                          monkeypatch):
        """Validation must win: no request may leave the machine for it."""
        from app.classroom import client as gc

        sent = []
        monkeypatch.setattr(gc.requests, "request",
                            lambda *a, **k: sent.append(a))
        response = assign_client.post(ASSIGN_URL.format(course="c1"),
                                      json={"title": "   "})
        assert response.status_code == 400, response.text
        assert response.json()["code"] == "missing_title"
        assert sent == []

    def test_the_real_cause_is_still_logged_for_the_diagnostics_page(
            self, assign_client, monkeypatch, caplog):
        """Translating the error must not destroy the evidence: the diagnostics
        page still needs the exception type and message."""
        import logging

        import requests

        from app.classroom import client as gc

        monkeypatch.setattr(gc.requests, "request", _fail_requests_with(
            requests.exceptions.ConnectionError("no route to host")))
        with caplog.at_level(logging.ERROR):
            assign_client.post(ASSIGN_URL.format(course="c1"),
                               json={"title": "Algebra HW"})
        record = next(r for r in caplog.records
                      if r.message == "create Classroom assignment failed")
        context = getattr(record, "context", {})
        assert context.get("exc_type") == "ConnectionError"
        assert "no route to host" in context.get("error", "")
        assert context.get("course_id") == "c1"

    def test_changing_the_classroom_total_has_the_same_guarantee(
            self, assign_client, monkeypatch):
        """The same hole lived on the total-points write, which shares the
        assignment screen: it must not answer with the opaque 500 either."""
        import requests

        from app.classroom import client as gc

        _seed_assignment()
        monkeypatch.setattr(gc.requests, "request", _fail_requests_with(
            requests.exceptions.ConnectionError("connection refused")))
        response = assign_client.post(TOTAL_URL.format(coursework="cw-assign"),
                                      json={"max_points": 10})
        assert response.status_code == 502, response.text
        body = response.json()
        assert body["code"] == "classroom_error"
        assert OPAQUE not in body["message"]

    def test_renaming_an_assignment_has_the_same_guarantee(
            self, assign_client, monkeypatch):
        """And on the rename write, which the same testing-only guard also
        blocks (a PATCH to Classroom)."""
        import requests

        from app.classroom import client as gc

        _seed_assignment()
        monkeypatch.setattr(gc.requests, "request", _fail_requests_with(
            requests.exceptions.ConnectionError("connection refused")))
        response = assign_client.post(
            "/api/automation/assignments/cw-assign/name",
            json={"display_name": "Algebra HW (renamed)"})
        assert response.status_code == 502, response.text
        body = response.json()
        assert body["code"] == "classroom_error"
        assert OPAQUE not in body["message"]
