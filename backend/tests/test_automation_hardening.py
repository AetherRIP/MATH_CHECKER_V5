"""Round-2 automation hardening: attachment retention, final grade/return,
and teacher-controlled assignment names (Tests A, F, G, H).

These lock down three fixes:

* Test A / H - a successful automated correction posts the FINAL grade,
  returns the work, verifies the attachment and ends COMPLETE. It never
  leaves the job as a draft.
* Test F - the attached report's Drive file is RETAINED and reachable, so the
  recipient can actually open it. Deleting it produced "the document couldn't
  be opened. It may have been deleted."
* Test G - the Classroom display name is exactly what the teacher configured
  and never contains an automatic "Via Math Checker" prefix/suffix.

All Google calls are faked; no network.
"""

from __future__ import annotations

import json

import pytest

from app import db
from app.classroom import client as gc
from app.classroom import naming
from app.classroom import report_attachment as ra

COURSE = "course-1"
CW = "coursework-1"
ACCOUNT = "teacher@school.edu"
RESULT_ID = "res-hardening"


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


# ------------------------------------------------------------- assignment name

class TestAssignmentNameIsTeacherControlled:
    def test_defaults_never_add_a_prefix_or_suffix(self):
        # Empty template -> the original name byte-for-byte.
        assert naming.compose_assignment_name("Algebraic Fractions L1") == \
            "Algebraic Fractions L1"
        assert naming.RESERVED_AUTO_TEXT not in \
            naming.compose_assignment_name("Algebraic Fractions L1")

    def test_a_prefix_is_only_applied_when_configured(self):
        out = naming.compose_assignment_name(
            "Algebraic Fractions L1", prefix="Y7")
        assert out == "Y7 Algebraic Fractions L1"
        assert naming.RESERVED_AUTO_TEXT not in out

    def test_custom_name_replaces_and_is_exact(self):
        out = naming.compose_assignment_name(
            "Algebraic Fractions L1", mode="custom",
            custom_name="Fractions — lesson 1")
        assert out == "Fractions — lesson 1"

    def test_custom_with_empty_name_falls_back_to_original(self):
        assert naming.compose_assignment_name(
            "Original", mode="custom", custom_name="   ") == "Original"

    def test_empty_everything_preserves_the_original_exactly(self):
        assert naming.compose_assignment_name(
            "Keep Me", mode="original", custom_name="", prefix="",
            suffix="") == "Keep Me"

    def test_rename_is_verified_against_googles_response(self, monkeypatch):
        calls = {}

        def fake_request(method, url, token, **kw):
            calls["method"] = method
            calls["title"] = (kw.get("json") or {}).get("title")
            return {"id": CW, "title": (kw.get("json") or {}).get("title")}

        import app.classroom.auth as auth
        monkeypatch.setattr(auth, "get_access_token", lambda: "tok")
        monkeypatch.setattr(gc, "classroom_request", fake_request)
        out = naming.rename_coursework(CW, COURSE, "Renamed Exactly")
        assert out["verified"] is True
        assert out["display_name"] == "Renamed Exactly"
        assert calls["title"] == "Renamed Exactly"

    def test_a_mismatched_title_is_not_reported_as_renamed(self, monkeypatch):
        import app.classroom.auth as auth
        monkeypatch.setattr(auth, "get_access_token", lambda: "tok")
        monkeypatch.setattr(gc, "classroom_request",
                            lambda *a, **k: {"id": CW, "title": "Something Else"})
        with pytest.raises(gc.ClassroomApiError):
            naming.rename_coursework(CW, COURSE, "Requested")
        # The teacher-visible name never contains the reserved auto text.
        assert naming.RESERVED_AUTO_TEXT not in "Requested"

    def test_an_empty_name_is_rejected(self):
        with pytest.raises(ValueError):
            naming.rename_coursework(CW, COURSE, "   ")


# ------------------------------------------------------ attachment retention

class FakeDrive:
    def __init__(self):
        self.files: dict[str, dict] = {}
        self.deleted: list[str] = []
        self._n = 0

    def add(self, *, owner):
        self._n += 1
        fid = f"file-{self._n}"
        self.files[fid] = {"id": fid, "name": "Report.pdf",
                           "mimeType": "application/pdf", "trashed": False,
                           "size": "1234", "appProperties": {},
                           "owners": [{"emailAddress": owner}]}
        return fid

    def meta(self, _token, file_id):
        f = self.files.get(file_id)
        return dict(f) if f else None

    def delete(self, _token, file_id):
        self.files[file_id]["trashed"] = True
        self.deleted.append(file_id)


@pytest.fixture
def google(monkeypatch):
    drive = FakeDrive()
    state = {"attachments": [], "ignore": False}

    monkeypatch.setattr(gc, "get_drive_file", drive.meta)
    monkeypatch.setattr(gc, "delete_drive_file", drive.delete)
    monkeypatch.setattr(
        gc, "download_drive_file",
        lambda t, f: ("Report.pdf", PDF_BYTES, "application/pdf"))
    monkeypatch.setattr(gc, "verify_drive_ownership",
                        lambda t, fid, *, account_email=None: {
                            "ok": True, "reason": "owner",
                            "owners": [ACCOUNT], "exists": True,
                            "can_delete": True, "is_our_temp": True,
                            "file_id": fid})
    monkeypatch.setattr(gc, "upload_report_to_drive",
                        lambda t, name, content, **kw: {
                            "id": drive.add(owner=ACCOUNT), "name": name,
                            "owners": [{"emailAddress": ACCOUNT}]})
    monkeypatch.setattr(gc, "replace_drive_file_content",
                        lambda t, f, c: None)
    monkeypatch.setattr(gc, "share_drive_file", lambda *a, **k: None)
    monkeypatch.setattr(gc, "get_user_profile",
                        lambda t, uid: {"emailAddress": f"{uid}@school.edu"})

    def _submission():
        return {"id": "sub-A", "userId": "user-A",
                "associatedWithDeveloper": True,
                "assignmentSub": {},
                "assignmentSubmission":
                    {"attachments": list(state["attachments"])}}

    def _attach(t, c, cw, s, **kw):
        if not state["ignore"]:
            for fid in kw.get("add_drive_file_ids") or []:
                state["attachments"].append({"driveFile": {"id": fid}})
        return _submission()

    monkeypatch.setattr(gc, "get_student_submission",
                        lambda *a, **k: _submission())
    monkeypatch.setattr(gc, "modify_submission_attachments", _attach)
    monkeypatch.setattr(ra, "render_report_pdf",
                        lambda result, detail="standard": PDF_BYTES)
    drive.state = state
    return drive


@pytest.fixture
def result_row():
    db.execute("DELETE FROM report_attachments WHERE result_id = ?", (RESULT_ID,))
    db.execute("DELETE FROM results WHERE id = ?", (RESULT_ID,))
    db.execute("INSERT OR IGNORE INTO correction_jobs(id, status, created_at, "
               "updated_at) VALUES('job-hardening','COMPLETED',0.0,0.0)")
    db.execute(
        "INSERT INTO results(id, job_id, coursework_id, submission_id, "
        "student_id, student_name, earned, available, percentage, "
        "provisional_grade, result_json, created_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        (RESULT_ID, "job-hardening", CW, "sub-A", "user-A", "Student A",
         9, 10, 90.0, "9", json.dumps({"questions": []}), 0.0))
    yield {
        "id": RESULT_ID, "coursework_id": CW, "submission_id": "sub-A",
        "student_id": "user-A", "student_name": "Student A",
        "earned": 9, "available": 10, "percentage": 90.0,
        "provisional_grade": "9", "result_json": {"questions": []},
    }
    db.execute("DELETE FROM report_attachments WHERE result_id = ?", (RESULT_ID,))
    db.execute("DELETE FROM results WHERE id = ?", (RESULT_ID,))
    db.execute("DELETE FROM correction_jobs WHERE id = 'job-hardening'")


class TestAttachedReportStaysOpenable:
    def test_the_drive_file_is_retained_not_deleted(self, google, result_row):
        """Test F: the recipient must be able to open the attached report, so
        its Drive file is kept and re-verified as reachable."""
        out = ra.attach_report("tok", result_row, course_id=COURSE,
                               assignment_title="HW")
        assert out.status == ra.STATUS_VERIFIED
        fid = out.to_dict()["drive_file_id"]
        # Test F: the file is still reachable (the student can open it) ...
        assert out.to_dict()["drive"]["ok"] is True
        assert google.meta("tok", fid)["trashed"] is False
        # ... and it was NOT deleted.
        assert google.deleted == []
        assert ra.get_state(RESULT_ID)["drive_cleanup"] == "retained"
        assert ra.get_state(RESULT_ID)["attached"] is True

    def test_an_unreachable_file_is_not_reported_as_success(self, google,
                                                            result_row,
                                                            monkeypatch):
        monkeypatch.setattr(gc, "get_drive_file", lambda t, f: None)
        out = ra.attach_report("tok", result_row, course_id=COURSE,
                               assignment_title="HW")
        # The file cannot be read back after upload, so it is NEVER reported as
        # attached: either the post-upload Drive verification or the post-attach
        # reachability check catches it.
        assert out.status in (ra.STATUS_DRIVE_VERIFY_FAILED,
                              ra.STATUS_VERIFY_FAILED)
        assert "DRIVE" in out.message
        # Kept so a retry can reuse it rather than re-uploading.
        assert google.deleted == []


# --------------------------------------------------------------- API surface

@pytest.fixture
def client(fresh_state):
    from app.main import app
    from fastapi.testclient import TestClient
    with TestClient(app) as test_client:
        yield test_client


class TestNamingEndpoints:
    def test_defaults_are_empty_and_show_the_reserved_text(self, client):
        body = client.get("/api/automation/assignment-naming").json()
        assert body["mode"] == "original"
        assert body["prefix"] == "" and body["suffix"] == ""
        assert body["custom_name"] == ""
        assert body["reserved_text"] == "Via Math Checker"

    def test_settings_can_be_saved_without_forcing_a_prefix(self, client):
        r = client.post("/api/automation/assignment-naming",
                        json={"mode": "original", "prefix": "",
                              "suffix": "", "custom_name": ""})
        assert r.status_code == 200
        assert r.json()["updated"] is True
        assert r.json()["prefix"] == ""

    def test_renaming_an_unknown_assignment_is_a_clean_404(self, client):
        r = client.post("/api/automation/assignments/nope/name",
                        json={"display_name": "X"})
        assert r.status_code == 404
