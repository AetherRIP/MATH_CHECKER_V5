"""API tests for remark + the independent retry actions (§Remark).

Focus: the teacher-facing surface. A duplicate remark click must not start a
second Gemini run, a decision must apply exactly one attempt, and each retry
action must touch only its own step - none of them may re-run AI marking.
"""

from __future__ import annotations

import json

import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from app import db
from app.classroom import client as gc
from app.classroom import report_attachment as ra
from app.engine import remark, versions
from app.engine.service import CorrectionService
from app.errors import AppError

COURSE = "course-remark"
CW = "coursework-remark"
#: The Google account the fake Drive behaves as if it is authenticated with.
ACCOUNT = "teacher@school.edu"
SUB = "submission-remark"


def _make_pdf(text: str = "Correction Report for John Smith") -> bytes:
    import io
    from reportlab.pdfgen import canvas
    buf = io.BytesIO()
    c = canvas.Canvas(buf)
    c.drawString(72, 720, text)
    c.showPage()
    c.save()
    return buf.getvalue()


PDF_BYTES = _make_pdf()


def _questions(*rows):
    return {"questions": [{"question_number": n, "awarded_marks": a,
                           "max_marks": m, "status": "VALID"}
                          for n, a, m in rows]}


@pytest.fixture
def api(monkeypatch):
    """A seeded, corrected submission with Classroom connected and Google faked."""
    import app.api.routes_all as routes
    import app.classroom.auth as auth
    from app.api import report_routes, routes_all
    from app.classroom import tracking_routes

    def _connected():
        return type("S", (), {"connected": True, "email": "teacher@school.edu",
                              "problem": None, "client_configured": True})()

    monkeypatch.setattr(routes, "auth_state", _connected)
    monkeypatch.setattr(routes, "get_access_token", lambda: "fake-token")
    monkeypatch.setattr(auth, "get_access_token", lambda: "fake-token")
    monkeypatch.setattr(auth, "auth_state", _connected)

    db.execute("INSERT OR IGNORE INTO courses(id, name, fetched_at) VALUES(?,?,?)",
               (COURSE, "Remark Class", 1.0))
    db.execute(
        """INSERT OR IGNORE INTO coursework(
               id, course_id, title, max_points, state, fetched_at)
           VALUES(?,?,?,?,?,?)""",
        (CW, COURSE, "June 2017 Paper 2 Variant 2", 75.0, "PUBLISHED", 1.0))
    db.execute(
        """INSERT OR IGNORE INTO correction_jobs(
               id, status, coursework_id, submission_id, created_at, updated_at)
           VALUES(?,?,?,?,?,?)""",
        ("job-remark", "COMPLETED", CW, SUB, 1.0, 1.0))
    db.execute(
        """INSERT OR IGNORE INTO results(
               id, job_id, coursework_id, submission_id, student_id,
               student_name, earned, available, percentage, provisional_grade,
               result_json, created_at)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
        ("result-remark", "job-remark", CW, SUB, "user-A", "John Smith",
         68, 75, 90.67, "9", json.dumps(_questions(("4", 5, 7))), 1.0))
    with db.transaction() as conn:
        versions.record(
            conn, result_id="result-remark", earned=68, available=75,
            percentage=90.67, provisional_grade="9", marking_model="gemini-x",
            key_fingerprint=None, confidence="HIGH", confidence_reasons=[],
            verification={"status": "VALID"}, result=_questions(("4", 5, 7)),
            created_at=1.0, note="Correction saved")

    calls = {"enqueue": [], "upload": [], "attach": [], "grade": [],
             "render": 0, "attachments": []}
    monkeypatch.setattr(routes, "_is_demo", lambda: False)

    def _enqueue(**kw):
        calls["enqueue"].append(kw)
        return {"id": f"remark-job-{len(calls['enqueue'])}"}

    monkeypatch.setattr(CorrectionService, "enqueue_classroom_submission",
                        lambda self, **kw: _enqueue(**kw))

    def _render(result, detail="standard"):
        calls["render"] += 1
        return PDF_BYTES

    monkeypatch.setattr(ra, "render_report_pdf", _render)
    monkeypatch.setattr(ra, "build_report_filename",
                        lambda s, t, **k: f"{s or 'Student'}_report.pdf")
    monkeypatch.setattr(gc, "upload_report_to_drive",
                        lambda t, fn, c, **kw: {
                            "id": "drive-remark", "name": fn,
                            "owners": [{"emailAddress": ACCOUNT}]})
    # The fake Drive models ownership faithfully: a file this app uploaded with
    # its own token belongs to that account, so the pre-attach ownership check
    # passes and the confirmed attachment is then cleaned up (§3, §8). The
    # failing-ownership path has its own tests in test_report_drive_lifecycle.
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
                                              "mathcorrectResultId": "r"}})
    monkeypatch.setattr(
        gc, "download_drive_file",
        lambda token, fid: ("report.pdf", PDF_BYTES, "application/pdf"))
    monkeypatch.setattr(gc, "is_our_temp_report", lambda *a, **k: True)
    monkeypatch.setattr(
        gc, "delete_drive_file",
        lambda token, fid: calls.setdefault("deleted", []).append(fid))
    monkeypatch.setattr(gc, "replace_drive_file_content",
                        lambda t, f, c: calls["upload"].append(("patch", f)))
    monkeypatch.setattr(gc, "share_drive_file", lambda t, f, e, role="reader": {})

    def _read(token, course_id, coursework_id, submission_id):
        return {"id": submission_id, "userId": "user-A",
                "associatedWithDeveloper": True,
                "assignmentSubmission": {"attachments": calls["attachments"]}}

    def _attach(token, course_id, coursework_id, submission_id, **kw):
        calls["attach"].append(submission_id)
        for fid in kw.get("add_drive_file_ids") or []:
            calls["attachments"].append({"driveFile": {"id": fid}})
        return _read(token, course_id, coursework_id, submission_id)

    monkeypatch.setattr(gc, "get_student_submission", _read)
    monkeypatch.setattr(gc, "modify_submission_attachments", _attach)
    monkeypatch.setattr(gc, "patch_draft_grade",
                        lambda t, c, cw, s, g: (calls["grade"].append(g)
                                                 or {"draftGrade": g}))

    app = FastAPI()
    routes_all.register(app)
    tracking_routes.register(app)
    report_routes.register(app)

    @app.exception_handler(AppError)
    async def _handler(request, exc: AppError):
        return JSONResponse(status_code=exc.status, content=exc.to_dict())

    monkeypatch.setattr(routes, "_correction_service", lambda: CorrectionService())
    return TestClient(app), calls


def _add_pending(earned, payload, confidence="HIGH"):
    with db.transaction() as conn:
        return versions.record_pending(
            conn, result_id="result-remark", earned=earned, available=75,
            percentage=earned / 75 * 100.0, provisional_grade="9",
            marking_model="gemini-y", key_fingerprint=None,
            confidence=confidence, confidence_reasons=[],
            verification={"status": "VALID"}, result=payload, created_at=2.0,
            note=remark.NOTE_REMARK)


class TestRemarkEndpoint:
    def test_remark_enqueues_one_marking_job(self, api):
        client, calls = api
        r = client.post("/api/results/result-remark/remark")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["remarked"] is True
        assert body["duplicate"] is False
        assert len(calls["enqueue"]) == 1
        # Same student, same submission - never a new Classroom submission.
        assert calls["enqueue"][0]["submission_id"] == SUB
        assert calls["enqueue"][0]["coursework_id"] == CW
        assert calls["enqueue"][0]["student_id"] == "user-A"
        assert calls["enqueue"][0]["is_remark"] is True

    def test_remark_preserves_the_previous_result(self, api):
        client, _ = api
        client.post("/api/results/result-remark/remark")
        assert db.query_one(
            "SELECT earned FROM results WHERE id='result-remark'")["earned"] == 68
        assert len(versions.list_versions("result-remark")) == 1

    def test_missing_result_is_404(self, api):
        client, _ = api
        assert client.post("/api/results/nope/remark").status_code == 404

    def test_result_without_a_submission_cannot_be_re_marked(self, api):
        client, _ = api
        db.execute("UPDATE results SET submission_id = NULL WHERE id='result-remark'")
        r = client.post("/api/results/result-remark/remark")
        assert r.status_code == 400
        assert r.json()["code"] == "remark_not_possible"


class TestDuplicateRemarkClick:
    def test_second_click_reuses_the_running_job(self, api, monkeypatch):
        """A double click must not start (and bill) a second Gemini run."""
        from app.jobs.manager import DuplicateJobError
        client, calls = api

        def _enqueue(self, **kw):
            if calls["enqueue"]:
                raise DuplicateJobError("remark-job-1")
            calls["enqueue"].append(kw)
            return {"id": "remark-job-1"}

        monkeypatch.setattr(CorrectionService, "enqueue_classroom_submission",
                            _enqueue)
        first = client.post("/api/results/result-remark/remark").json()
        second = client.post("/api/results/result-remark/remark").json()
        assert first["duplicate"] is False
        assert second["duplicate"] is True
        assert first["job_id"] == second["job_id"]
        assert len(calls["enqueue"]) == 1


class TestRemarkStatusAndDecision:
    def test_status_reports_no_pending_attempt_initially(self, api):
        client, _ = api
        body = client.get("/api/results/result-remark/remark").json()
        assert body["awaiting_decision"] is False
        assert body["comparison"] is None
        assert body["attempt_count"] == 1

    def test_status_returns_the_comparison_for_a_pending_attempt(self, api):
        client, _ = api
        _add_pending(70, _questions(("4", 6, 7), ("8", 3, 5)))
        body = client.get("/api/results/result-remark/remark").json()
        assert body["awaiting_decision"] is True
        cmp = body["comparison"]
        assert cmp["previous"]["earned"] == 68
        assert cmp["new"]["earned"] == 70
        changed = {c["question_number"] for c in cmp["questions"]}
        assert changed == {"4", "8"}

    def test_use_new_applies_the_remark(self, api):
        client, _ = api
        _add_pending(70, _questions(("4", 6, 7)))
        r = client.post("/api/results/result-remark/remark/decision",
                        json={"decision": "USE_NEW"})
        assert r.status_code == 200, r.text
        assert r.json()["active_version"] == 2
        assert db.query_one(
            "SELECT earned FROM results WHERE id='result-remark'")["earned"] == 70

    def test_keep_previous_leaves_the_correction_untouched(self, api):
        client, _ = api
        _add_pending(12, _questions(("4", 1, 7)), confidence="LOW")
        r = client.post("/api/results/result-remark/remark/decision",
                        json={"decision": "KEEP_PREVIOUS"})
        assert r.status_code == 200, r.text
        assert db.query_one(
            "SELECT earned FROM results WHERE id='result-remark'")["earned"] == 68
        # The attempt survives for the record, just no longer pending.
        assert len(versions.list_versions("result-remark")) == 2

    def test_unknown_decision_is_a_400(self, api):
        client, _ = api
        r = client.post("/api/results/result-remark/remark/decision",
                        json={"decision": "NUKE"})
        assert r.status_code == 400

    def test_failed_attempt_is_flagged_not_silently_promoted(self, api):
        client, _ = api
        _add_pending(0, _questions(("4", 0, 7)), confidence="VALIDATION_FAILED")
        r = client.post("/api/results/result-remark/remark/decision",
                        json={"decision": "USE_NEW"}).json()
        assert r["forced_review"] is True
        assert db.query_one(
            "SELECT review_required FROM results WHERE id='result-remark'"
        )["review_required"] == 1


class TestIndependentRetries:
    """§10: never re-run AI marking for a downstream failure."""

    def test_regenerate_report_never_touches_google(self, api):
        client, calls = api
        r = client.post("/api/results/result-remark/retry",
                        json={"action": "report"})
        assert r.status_code == 200, r.text
        assert r.json()["bytes"] == len(PDF_BYTES)
        assert calls["render"] == 1
        assert calls["attach"] == [] and calls["grade"] == []

    def test_retry_upload_updates_the_same_drive_file(self, api):
        client, calls = api
        db.execute("INSERT OR IGNORE INTO report_attachments("
                   "result_id, coursework_id, submission_id, drive_file_id, "
                   "status, attempts, updated_at) VALUES(?,?,?,?,?,?,?)",
                   ("result-remark", CW, SUB, "drive-existing", "UPLOADED", 1, 1.0))
        r = client.post("/api/results/result-remark/retry", json={"action": "upload"})
        assert r.status_code == 200, r.text
        # A PATCH of the existing file - never a second copy.
        assert calls["upload"] == [("patch", "drive-existing")]

    def test_retry_attach_does_not_re_mark(self, api):
        client, calls = api
        r = client.post("/api/results/result-remark/retry", json={"action": "attach"})
        assert r.status_code == 200, r.text
        assert r.json()["status"] == ra.STATUS_VERIFIED
        assert calls["attach"] == [SUB]
        assert calls["enqueue"] == [], "a retry must never start a marking job"

    def test_retry_grade_only_patches_the_draft_grade(self, api):
        client, calls = api
        r = client.post("/api/results/result-remark/retry", json={"action": "grade"})
        assert r.status_code == 200, r.text
        assert calls["grade"] == [68]
        assert calls["enqueue"] == []
        assert calls["render"] == 0, "a grade retry must not regenerate the PDF"

    def test_unknown_action_is_a_400(self, api):
        client, _ = api
        r = client.post("/api/results/result-remark/retry", json={"action": "magic"})
        assert r.status_code == 400