"""API-level tests for the report-attachment endpoints.

Proves the teacher-facing surface behaves: status is readable, a successful
attach returns 200 + VERIFIED, a genuine failure returns a NON-2xx code (no
false success), and the batch endpoint retries without re-marking.
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from app.classroom import client as gc
from app.classroom import report_attachment as ra


#: The Google account the fakes behave as if authenticated with.
ACCOUNT = "teacher@school.edu"


def _make_pdf(text: str = "Correction Report for Student A") -> bytes:
    import io
    from reportlab.pdfgen import canvas
    buf = io.BytesIO()
    c = canvas.Canvas(buf)
    c.drawString(72, 720, text)
    c.showPage()
    c.save()
    return buf.getvalue()


PDF_BYTES = _make_pdf()


@pytest.fixture
def wired(monkeypatch, tmp_path):
    """Connected Classroom + a seeded assignment/result, with Google faked."""
    import app.classroom.auth as auth
    import app.classroom.tracking_routes as tr
    import app.api.routes_all as routes
    from app import db

    from fastapi import FastAPI
    from fastapi.responses import JSONResponse
    from fastapi.testclient import TestClient
    from app.api import routes_all, report_routes
    from app.classroom import tracking_routes
    from app.engine.service import CorrectionService
    from app.errors import AppError

    def _connected():
        return type("S", (), {"connected": True,
                              "email": "teacher@school.edu",
                              "problem": None,
                              "client_configured": True})()

    # routes_all binds these names at import time, so patch them THERE
    # (patching the auth module alone would have no effect).
    monkeypatch.setattr(routes, "auth_state", _connected)
    monkeypatch.setattr(routes, "get_access_token", lambda: "fake-token")
    monkeypatch.setattr(auth, "get_access_token", lambda: "fake-token")
    monkeypatch.setattr(auth, "auth_state", _connected)
    monkeypatch.setattr(tr, "get_access_token", lambda: "fake-token")
    monkeypatch.setattr(tr, "auth_state", _connected)

    db.execute(
        "INSERT OR IGNORE INTO courses(id, name, fetched_at) VALUES(?,?,?)",
        ("course-1", "Test Class", 1.0))
    db.execute(
        """
        INSERT OR IGNORE INTO coursework(
            id, course_id, title, max_points, state, fetched_at)
        VALUES(?,?,?,?,?,?)
        """,
        ("coursework-1", "course-1", "Chapter 1 Lesson 7", 9.0,
         "PUBLISHED", 1.0))
    # Assert the seed landed: courses/coursework both require fetched_at, and
    # a silently-ignored insert would surface much later as a confusing 400.
    assert db.query_one("SELECT id FROM courses WHERE id = ?", ("course-1",))
    assert db.query_one("SELECT id FROM coursework WHERE id = ?",
                        ("coursework-1",))
    db.execute(
        """
        INSERT OR IGNORE INTO correction_jobs(
            id, status, coursework_id, submission_id, created_at, updated_at)
        VALUES(?,?,?,?,?,?)
        """,
        ("job-1", "COMPLETED", "coursework-1", "submission-A", 1.0, 1.0))
    db.execute(
        """
        INSERT OR IGNORE INTO results(
            id, job_id, coursework_id, submission_id, student_id,
            student_name, earned, available, percentage, provisional_grade,
            result_json, created_at)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        ("result-1", "job-1", "coursework-1", "submission-A", "user-A",
         "Student A", 67, 103, 65.0, "7", json.dumps({"questions": []}), 1.0))

    calls = {"upload": [], "attach": [], "read": 0}
    state = {"attachments": [], "can_modify": True, "fail": None}

    monkeypatch.setattr(ra, "render_report_pdf",
                        lambda result, detail="standard": PDF_BYTES)
    monkeypatch.setattr(
        gc, "upload_report_to_drive",
        lambda t, fn, content, **kw: (calls["upload"].append(fn)
                                       or {"id": "drive-1", "name": fn,
                                           "owners": [{"emailAddress": ACCOUNT}]}))
    # The fakes model Drive faithfully: a file this app uploaded with its own
    # token is owned by that account, so the pre-attach ownership check passes
    # and the confirmed attachment is then cleaned up.
    monkeypatch.setattr(
        gc, "verify_drive_ownership",
        lambda token, fid, *, account_email=None: {
            "ok": True, "reason": "owner", "owners": [ACCOUNT],
            "account": (account_email or ACCOUNT).lower(), "exists": True,
            "can_delete": True, "is_our_temp": True, "file_id": fid})
    monkeypatch.setattr(
        gc, "get_drive_file",
        lambda token, fid: {"id": fid, "trashed": False,
                            "mimeType": "application/pdf",
                            "size": str(len(PDF_BYTES)), "name": "report.pdf",
                            "owners": [{"emailAddress": ACCOUNT}],
                            "appProperties": {"mathcorrectTemp": "true",
                                              "mathcorrectResultId": "result-1"}})
    monkeypatch.setattr(
        gc, "download_drive_file",
        lambda token, fid: ("report.pdf", PDF_BYTES, "application/pdf"))
    monkeypatch.setattr(gc, "is_our_temp_report", lambda *a, **k: True)
    monkeypatch.setattr(
        gc, "delete_drive_file",
        lambda token, fid: calls.setdefault("deleted", []).append(fid))
    monkeypatch.setattr(gc, "replace_drive_file_content",
                        lambda t, f, c: None)
    monkeypatch.setattr(
        gc, "get_user_profile", lambda t, u: {"emailAddress": "a@school.edu"})
    monkeypatch.setattr(gc, "share_drive_file",
                        lambda t, f, e, role="reader": {})

    def _read(token, course_id, coursework_id, submission_id):
        calls["read"] += 1
        return {"id": submission_id, "userId": "user-A",
                "associatedWithDeveloper": state["can_modify"],
                "assignmentSubmission": {"attachments": state["attachments"]}}

    def _attach(token, course_id, coursework_id, submission_id, **kw):
        calls["attach"].append(submission_id)
        if state["fail"]:
            raise gc.ClassroomApiError(state["fail"], "denied")
        for fid in kw.get("add_drive_file_ids") or []:
            state["attachments"].append({"driveFile": {"id": fid}})
        return _read(token, course_id, coursework_id, submission_id)

    monkeypatch.setattr(gc, "get_student_submission", _read)
    monkeypatch.setattr(gc, "modify_submission_attachments", _attach)

    # Same minimal-app pattern the other API tests use: the routers plus the
    # AppError handler, with the CorrectionService the endpoints expect.
    app = FastAPI()
    routes_all.register(app)
    tracking_routes.register(app)
    report_routes.register(app)

    @app.exception_handler(AppError)
    async def _app_error_handler(request, exc: AppError):
        return JSONResponse(status_code=exc.status, content=exc.to_dict())

    app.state.correction = CorrectionService()
    # _correction_service() reads the state of the REAL app.main app, so the
    # endpoints under test would otherwise blow up on a missing attribute.
    monkeypatch.setattr(routes, "_correction_service",
                        lambda: CorrectionService())
    return TestClient(app), calls, state


class TestAttachmentEndpoints:
    def test_status_starts_pending(self, wired):
        api, _, _ = wired
        r = api.get("/api/results/result-1/classroom-attachment")
        assert r.status_code == 200
        assert r.json()["status"] in (ra.STATUS_PENDING, ra.STATUS_VERIFIED)

    def test_attach_returns_verified(self, wired):
        api, calls, _ = wired
        r = api.post("/api/results/result-1/attach-report")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["status"] == ra.STATUS_VERIFIED
        assert calls["attach"] == ["submission-A"]

    def test_attach_is_idempotent_over_http(self, wired):
        api, calls, _ = wired
        api.post("/api/results/result-1/attach-report")
        r = api.post("/api/results/result-1/attach-report")
        assert r.status_code == 200
        assert len(calls["upload"]) == 1, "no duplicate report upload"
        assert len(calls["attach"]) == 1, "no duplicate attachment"

    def test_failure_returns_non_2xx_not_false_success(self, wired):
        api, _, state = wired
        state["fail"] = 403
        r = api.post("/api/results/result-1/attach-report")
        assert r.status_code >= 400, "a failure must not look like success"
        assert r.json()["code"] == "report_attach_failed"
        # ...and the stored state records WHICH failure it was (§20), so the
        # UI can offer a retry and never claim the correction failed.
        st = api.get("/api/results/result-1/classroom-attachment").json()
        assert st["status"] == ra.STATUS_ATTACH_FAILED
        assert st["error"]
        assert st["attached"] is False
        assert st["retryable"] is True

    def test_ownership_failure_is_a_409_with_its_own_code(self, wired,
                                                         monkeypatch):
        """§3: a non-owner file is a permanent configuration fault, so it gets
        its own HTTP status and error code instead of a generic 502."""
        api, calls, _ = wired
        monkeypatch.setattr(
            gc, "verify_drive_ownership",
            lambda token, fid, *, account_email=None: {
                "ok": False, "reason": "not_owner",
                "owners": ["someone-else@school.edu"],
                "account": (account_email or ACCOUNT).lower(), "exists": True,
                "can_delete": False, "is_our_temp": True, "file_id": fid})
        r = api.post("/api/results/result-1/attach-report")
        assert r.status_code == 409, r.text
        body = r.json()
        assert body["code"] == ra.ERROR_OWNERSHIP
        assert "someone-else@school.edu" in body["message"]
        # Never blindly retry the same attach, and never delete the file.
        assert calls["attach"] == []
        st = api.get("/api/results/result-1/classroom-attachment").json()
        assert st["status"] == ra.STATUS_OWNERSHIP_ERROR
        assert calls.get("deleted", []) == []

    def test_blocked_when_coursework_not_app_created(self, wired):
        api, calls, state = wired
        state["can_modify"] = False
        r = api.post("/api/results/result-1/attach-report")
        body = r.json()
        assert body["status"] == ra.STATUS_BLOCKED
        assert calls["upload"] == [], "must not upload when it cannot attach"

    def test_bad_detail_is_rejected(self, wired):
        api, _, _ = wired
        assert api.post(
            "/api/results/result-1/attach-report?detail=nope").status_code == 400

    def test_a_dead_grant_says_reconnect_not_try_again(self, wired, monkeypatch):
        """A revoked refresh token is an AUTHORIZATION problem.

        "Could not reach Classroom, try again" sends the teacher to debug the
        wrong thing; the only action that helps is reconnecting (§7, §23).
        """
        import app.api.routes_all as routes
        api, _, _ = wired

        def _dead():
            raise RuntimeError("Google authorization expired or was revoked. "
                               "Reconnect Google Classroom from the Classroom "
                               "page.")

        monkeypatch.setattr(routes, "get_access_token", _dead)
        r = api.post("/api/results/result-1/attach-report")
        assert r.status_code == 401, r.text
        body = r.json()
        assert body["code"] == "classroom_reconnect_required"
        assert "Reconnect" in body["message"]

    def test_cleanup_endpoints_degrade_instead_of_crashing(self, wired,
                                                          monkeypatch):
        """§12/§13: housekeeping that cannot run reports WHY; it never 500s and
        never claims to have deleted anything."""
        import app.api.routes_all as routes
        import app.classroom.auth as auth
        api, _, _ = wired
        api.post("/api/results/result-1/attach-report")

        def _dead():
            raise RuntimeError("Google authorization expired or was revoked. "
                               "Reconnect Google Classroom from the Classroom "
                               "page.")

        # These two routes import get_access_token from the auth module inside
        # the function body, so the patch has to land there AND on the router
        # module for the other endpoints.
        monkeypatch.setattr(auth, "get_access_token", _dead)
        monkeypatch.setattr(routes, "get_access_token", _dead)

        one = api.post("/api/results/result-1/report-cleanup")
        assert one.status_code == 200, one.text
        assert one.json()["cleanup"]["ok"] is False
        assert "Reconnect" in one.json()["cleanup"]["reason"]
        assert one.json()["state"]["status"] in ra.ATTACHED_STATUSES

        sweep = api.post("/api/classroom/report-cleanup/sweep")
        assert sweep.status_code == 200, sweep.text
        assert sweep.json()["ok"] is False
        assert "Reconnect" in sweep.json()["error"]
        assert sweep.json()["orphans"]["deleted"] == []


class TestBatchAttach:
    def test_batch_verifies_and_reports(self, wired):
        api, calls, _ = wired
        r = api.post(
            "/api/classroom/courses/course-1/assignments/coursework-1"
            "/attach-reports?only=all")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["verified"] == 1
        assert body["failed"] == 0
        assert calls["attach"] == ["submission-A"]

    def test_batch_failure_is_reported_not_hidden(self, wired):
        api, _, state = wired
        state["fail"] = 403
        r = api.post(
            "/api/classroom/courses/course-1/assignments/coursework-1"
            "/attach-reports?only=all")
        body = r.json()
        assert body["verified"] == 0
        assert body["failed"] == 1
        assert body["failures"], "teacher must be able to see which failed"

    def test_batch_retry_failed_does_not_remark(self, wired):
        api, calls, state = wired
        state["fail"] = 403
        api.post("/api/classroom/courses/course-1/assignments/"
                 "coursework-1/attach-reports?only=all")
        uploads_after_fail = len(calls["upload"])
        state["fail"] = None
        r = api.post("/api/classroom/courses/course-1/assignments/"
                     "coursework-1/attach-reports?only=failed")
        assert r.json()["verified"] == 1
        # The Drive file created during the failed attempt is REUSED, so the
        # retry must not create a second one - that is what keeps Drive from
        # spawning "Report (1).pdf" clones.
        assert len(calls["upload"]) == uploads_after_fail, \
            "retry must not upload a second report"
        # ...and once confirmed, that one file is RETAINED because Classroom
        # references it - deleting it broke the student's attachment.
        assert calls.get("deleted", []) == []

    def test_batch_retry_covers_every_failure_status(self, wired, monkeypatch):
        """"Retry the failures" must not be blind to the newer statuses (§20)."""
        api, _, state = wired
        state["fail"] = 403
        api.post("/api/classroom/courses/course-1/assignments/"
                 "coursework-1/attach-reports?only=all")
        assert api.post("/api/classroom/courses/course-1/assignments/"
                        "coursework-1/attach-reports?only=failed").json()[
                            "total"] == 1
        monkeypatch.setattr(
            gc, "upload_report_to_drive",
            lambda *a, **k: (_ for _ in ()).throw(gc.ClassroomApiError(500, "x")))
        # A Drive upload failure is a different status, and must still be
        # selected by ?only=failed.
        api.post("/api/classroom/courses/course-1/assignments/"
                 "coursework-1/attach-reports?only=all")
        st = api.get("/api/results/result-1/classroom-attachment").json()
        assert st["status"] in ra.FAILED_STATUSES
        assert api.post("/api/classroom/courses/course-1/assignments/"
                        "coursework-1/attach-reports?only=failed").json()[
                            "total"] == 1

    def test_bad_only_is_rejected(self, wired):
        api, _, _ = wired
        r = api.post("/api/classroom/courses/course-1/assignments/"
                     "coursework-1/attach-reports?only=bogus")
        assert r.status_code == 400

class TestManualFallback:
    """Google refuses API attachments for teacher-created coursework, so the
    app must offer the teacher a working manual route instead of dead-ending.
    """

    def test_fallback_offers_pdf_and_classroom_link(self, wired):
        from app import db
        db.execute("UPDATE coursework SET alternate_link = ? WHERE id = ?",
                   ("https://classroom.google.com/c/ODg/a/ODg/details",
                    "coursework-1"))
        api, _, state = wired
        state["can_modify"] = False
        api.post("/api/results/result-1/attach-report")
        body = api.get("/api/results/result-1/classroom-attachment").json()
        fb = body["fallback"]
        assert fb is not None, "a blocked attach must offer a manual route"
        assert fb["pdf_url"].startswith("/api/results/result-1/export")
        assert fb["pdf_url"].endswith("format=pdf&detail=standard")
        assert fb["filename"].endswith("_Correction_Report.pdf")
        assert fb["classroom_url"].endswith("/details")
        assert fb["student_name"] == "Student A"
        assert fb["submission_id"] == "submission-A"

    def test_fallback_instructions_never_leak_a_missing_name(self, wired):
        """A sparse result row must not print "None's submission" at the teacher."""
        from app import db
        db.execute("UPDATE coursework SET alternate_link = ? WHERE id = ?",
                   ("https://classroom.google.com/c/ODg/a/ODg/details",
                    "coursework-1"))
        db.execute("UPDATE results SET student_name = NULL WHERE id = ?",
                   ("result-1",))
        api, _, state = wired
        state["can_modify"] = False
        api.post("/api/results/result-1/attach-report")
        body = api.get("/api/results/result-1/classroom-attachment").json()
        fb = body["fallback"]
        assert fb is not None
        assert "None" not in fb["instructions"]
        assert "the student's submission" in fb["instructions"]

    def test_fallback_absent_once_verified(self, wired):
        api, _, _ = wired
        api.post("/api/results/result-1/attach-report")
        body = api.get("/api/results/result-1/classroom-attachment").json()
        assert body["status"] == ra.STATUS_VERIFIED
        assert body["fallback"] is None, "no manual route needed when attached"

    def test_export_uses_the_same_predictable_filename(self, wired):
        api, _, _ = wired
        r = api.get("/api/results/result-1/export?format=pdf")
        assert r.status_code == 200
        cd = r.headers.get("content-disposition", "")
        assert "Student A_Chapter 1 Lesson 7_Correction_Report.pdf" in cd

    def test_export_filename_is_sanitised(self, wired):
        from app import db
        db.execute("UPDATE results SET student_name = ? WHERE id = ?",
                   ("A/B:C*D?", "result-1"))
        api, _, _ = wired
        r = api.get("/api/results/result-1/export?format=pdf")
        cd = r.headers.get("content-disposition", "")
        # The header itself is quoted, so assert on the NAME between the
        # quotes - illegal characters must not survive inside it.
        name = cd.split('filename="', 1)[1].rsplit('"', 1)[0]
        for ch in r'/\:*?"<>|':
            assert ch not in name, f"{ch!r} survived in {name!r}"
        assert ".." not in name
        assert name.endswith("_Correction_Report.pdf")
