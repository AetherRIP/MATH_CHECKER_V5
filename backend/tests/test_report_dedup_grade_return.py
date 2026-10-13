"""Regression tests for three defects fixed together:

1. a student submission ended up with MULTIPLE Math Correct report attachments,
2. the automatic workflow was not reliably returning the grade to Classroom,
3. the student-facing report revealed which AI model marked the work.

All Google calls are faked; the app's own decision logic is what is under test.
"""

from __future__ import annotations

import io
import json
import threading

import pytest

from app import db
from app.classroom import client as gc
from app.classroom import managed as mg
from app.classroom import report_attachment as ra

COURSE = "course-1"
CW = "coursework-1"
ACCOUNT = "teacher@school.edu"
SUBMISSION = "submission-A"
STUDENT = "user-A"


def _make_pdf(lines: int = 40) -> bytes:
    """A real, parseable PDF big enough to pass the >=800-byte report guard."""
    from reportlab.pdfgen import canvas
    buf = io.BytesIO()
    c = canvas.Canvas(buf)
    for i in range(lines):
        c.drawString(72, 760 - i * 16, f"Correction Report line {i} " + "x" * 20)
    c.showPage()
    c.save()
    return buf.getvalue()


PDF_BYTES = _make_pdf()


# --------------------------------------------------------------- Google fake

class FakeGoogle:
    """A tiny Classroom/Drive world that remembers each file and its props."""

    def __init__(self, monkeypatch):
        self.files: dict[str, dict] = {}
        self.attachments: dict[str, list[str]] = {SUBMISSION: []}
        self.extra: list[str] = []            # unrelated student/teacher files
        self.calls = {"upload": [], "update": [], "attach": [], "share": []}
        self._n = 1
        self.upload_fail = False
        self.attach_fail: int | None = None
        self.ownership_ok = True
        self._patch(monkeypatch)

    # ---- Drive -----------------------------------------------------------
    def _upload(self, token, filename, content, **kw):
        self.calls["upload"].append({"filename": filename, "size": len(content)})
        if self.upload_fail:
            raise gc.ClassroomApiError(500, "drive exploded")
        fid = f"drive-{self._n}"
        self._n += 1
        self.files[fid] = {"content": content,
                           "props": dict(kw.get("app_properties") or {}),
                           "owners": [ACCOUNT], "name": filename}
        return {"id": fid, "name": filename,
                "owners": [{"emailAddress": ACCOUNT}]}

    def _update(self, token, file_id, content):
        self.calls["update"].append(file_id)
        self.files[file_id]["content"] = content

    def _get_file(self, token, fid):
        f = self.files.get(fid)
        if f is None:
            return None
        return {"id": fid, "name": f.get("name"), "trashed": False,
                "mimeType": "application/pdf", "size": str(len(f["content"])),
                "owners": [{"emailAddress": e} for e in f["owners"]],
                "appProperties": f["props"]}

    def _download(self, token, fid):
        f = self.files[fid]
        return f.get("name"), f["content"], "application/pdf"

    # ---- Classroom -------------------------------------------------------
    def _attach(self, token, course_id, coursework_id, submission_id, **kw):
        add = list(kw.get("add_drive_file_ids") or [])
        self.calls["attach"].append({"submission_id": submission_id, "add": add})
        if self.attach_fail:
            raise gc.ClassroomApiError(self.attach_fail, "nope")
        self.attachments.setdefault(submission_id, []).extend(add)
        return self._submission(submission_id)

    def _submission(self, submission_id=SUBMISSION):
        ids = list(self.extra) + list(self.attachments.get(submission_id, []))
        return {"id": submission_id, "userId": STUDENT,
                "associatedWithDeveloper": True,
                "assignmentSubmission": {
                    "attachments": [{"driveFile": {"id": i}} for i in ids]}}

    def _read(self, token, course_id, coursework_id, submission_id):
        return self._submission(submission_id)

    def _patch(self, monkeypatch):
        monkeypatch.setattr(gc, "upload_report_to_drive", self._upload)
        monkeypatch.setattr(gc, "replace_drive_file_content", self._update)
        monkeypatch.setattr(gc, "get_drive_file", self._get_file)
        monkeypatch.setattr(gc, "download_drive_file", self._download)
        monkeypatch.setattr(gc, "modify_submission_attachments", self._attach)
        monkeypatch.setattr(gc, "get_student_submission", self._read)
        monkeypatch.setattr(gc, "share_drive_file",
                            lambda t, f, e, role="reader":
                            self.calls["share"].append((f, e)))
        monkeypatch.setattr(gc, "get_user_profile",
                            lambda t, uid: {"emailAddress": f"{uid}@school.edu"})
        monkeypatch.setattr(
            gc, "drive_file_owners",
            lambda meta: [o["emailAddress"] for o in (meta or {}).get("owners", [])])
        monkeypatch.setattr(
            gc, "verify_drive_ownership",
            lambda token, fid, *, account_email=None: {
                "ok": self.ownership_ok, "owners": [ACCOUNT],
                "reason": "owner" if self.ownership_ok else "not_owner",
                "file_id": fid})
        monkeypatch.setattr(gc, "is_our_temp_report", lambda *a, **k: True)
        monkeypatch.setattr(gc, "delete_drive_file", lambda *a, **k: None)
        monkeypatch.setattr(ra, "render_report_pdf",
                            lambda result, detail="standard": PDF_BYTES)

    # ---- assertions ------------------------------------------------------
    def report_files(self, submission_id=SUBMISSION) -> list[str]:
        """The app's OWN Math Correct files currently attached."""
        out = []
        for fid in self.attachments.get(submission_id, []):
            props = (self.files.get(fid) or {}).get("props") or {}
            if str(props.get(gc.APP_PROP_TEMP)).lower() == "true":
                out.append(fid)
        return out


# ------------------------------------------------------------------ helpers

@pytest.fixture
def google(monkeypatch):
    return FakeGoogle(monkeypatch)


def _result(result_id: str, **over) -> dict:
    row = {"id": result_id, "coursework_id": CW, "submission_id": SUBMISSION,
           "student_id": STUDENT, "student_name": "Student A",
           "earned": 5, "available": 10, "percentage": 50.0,
           "provisional_grade": "5", "result_json": {"questions": []}}
    row.update(over)
    return row


def _persist(row: dict) -> dict:
    """Create (or re-mark) the genuine correction_jobs + results rows.

    ``results`` is unique per (coursework_id, submission_id) - there is exactly
    ONE result per submission, and a re-correction UPDATES it - so this is an
    upsert rather than an insert.
    """
    db.execute(
        "INSERT OR IGNORE INTO correction_jobs(id, status, created_at, updated_at)"
        " VALUES(?,?,?,?)", (f"job-{row['id']}", "COMPLETED", 1.0, 1.0))
    blob = json.dumps(row.get("result_json") or {})
    if db.query_one("SELECT id FROM results WHERE id = ?", (row["id"],)):
        db.execute(
            "UPDATE results SET earned = ?, available = ?, percentage = ?, "
            "provisional_grade = ?, result_json = ?, marking_model = ? "
            "WHERE id = ?",
            (row.get("earned", 0), row.get("available", 0),
             row.get("percentage", 0.0), row.get("provisional_grade"), blob,
             row.get("marking_model"), row["id"]))
        return row
    db.execute(
        """
        INSERT INTO results(id, job_id, coursework_id, submission_id, student_id,
            student_name, earned, available, percentage, provisional_grade,
            result_json, created_at)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (row["id"], f"job-{row['id']}", row.get("coursework_id"),
         row.get("submission_id"), row.get("student_id"), row.get("student_name"),
         row.get("earned", 0), row.get("available", 0), row.get("percentage", 0.0),
         row.get("provisional_grade"), blob, 1.0))
    return row


def _attach(result_id: str, **over) -> ra.AttachmentOutcome:
    row = _persist(_result(result_id, **over))
    return ra.attach_report("tok", row, course_id=COURSE,
                            assignment_title="HW")


# =========================================================================
# 1. ONE Math Correct report per submission
# =========================================================================

def _lose_attachment_id(result_id: str) -> None:
    """Simulate the legacy/repair case: our row no longer knows the file id,
    while the report file itself is still attached to the submission."""
    db.execute("UPDATE report_attachments SET drive_file_id = NULL "
               "WHERE result_id = ?", (result_id,))


class TestNoDuplicateReports:

    def test_first_correction_attaches_exactly_one_report(self, google):
        out = _attach("r1")
        assert out.status == ra.STATUS_VERIFIED
        assert google.report_files() == ["drive-1"]
        assert len(google.calls["attach"]) == 1

    def test_reattaching_the_same_correction_is_idempotent(self, google):
        _attach("r1")
        out = _attach("r1")
        assert out.status == ra.STATUS_VERIFIED
        assert google.report_files() == ["drive-1"]
        assert len(google.calls["attach"]) == 1
        assert len(google.calls["upload"]) == 1

    def test_a_remark_updates_the_same_file_in_place(self, google):
        _attach("r1")
        # A remark changes the marks -> the fingerprint changes.
        out = _attach("r1", earned=9, percentage=90.0, provisional_grade="9",
                      result_json={"questions": [{"question_number": "1",
                                                  "max_marks": 10,
                                                  "awarded_marks": 9}]})
        assert out.status == ra.STATUS_VERIFIED
        assert google.report_files() == ["drive-1"]
        assert len(google.calls["upload"]) == 1
        assert "drive-1" in google.calls["update"]

    def test_third_correction_leaves_only_the_newest(self, google):
        for marks in (3, 6, 9):
            _attach("r1", earned=marks, percentage=float(marks * 10),
                    result_json={"questions": [{"question_number": "1",
                                                "max_marks": 10,
                                                "awarded_marks": marks}]})
        assert google.report_files() == ["drive-1"]
        assert len(google.calls["upload"]) == 1
        canonical = db.query_one(
            "SELECT canonical_result_id, drive_file_id FROM canonical_reports "
            "WHERE submission_id = ?", (SUBMISSION,))
        assert canonical["canonical_result_id"] == "r1"
        assert canonical["drive_file_id"] == "drive-1"

    def test_a_lost_drive_file_id_adopts_instead_of_duplicating(self, google):
        """THE regression this fixes: our row forgot the file id while the
        report was still attached. The old code uploaded a SECOND file."""
        _attach("r1")
        _lose_attachment_id("r1")
        out = _attach("r1", earned=9, percentage=90.0,
                      result_json={"questions": [{"question_number": "1",
                                                  "max_marks": 10,
                                                  "awarded_marks": 9}]})
        assert out.status == ra.STATUS_VERIFIED
        # The existing Math Correct file was reused/updated, never duplicated.
        assert google.report_files() == ["drive-1"]
        assert len(google.calls["upload"]) == 1
        assert len(google.calls["attach"]) == 1

    def test_a_deleted_attachment_row_adopts_instead_of_duplicating(self,
                                                                   google):
        _attach("r1")
        db.execute("DELETE FROM report_attachments WHERE result_id = ?", ("r1",))
        out = _attach("r1", earned=8, percentage=80.0)
        assert out.status == ra.STATUS_VERIFIED
        assert google.report_files() == ["drive-1"]
        assert len(google.calls["upload"]) == 1

    def test_a_report_from_a_replaced_result_is_reused_not_duplicated(self,
                                                                     google):
        """The regression this fixes end to end.

        When the result row for a submission is recreated (a re-correction from
        scratch / a repair), its canonical + report_attachment rows go with it
        while the Math Correct file is STILL attached. The old code could not
        find that file (it searched by the NEW result id only), so it uploaded a
        fresh report and ADDed it - leaving the submission with two Math
        Checker reports.
        """
        _attach("r1")
        assert google.report_files() == ["drive-1"]
        # The old result goes away (its downstream rows cascade with it) and a
        # new result id takes over the SAME submission. The Drive file keeps the
        # appProperties it was created with.
        db.execute("DELETE FROM results WHERE id = ?", ("r1",))
        out = _attach("r2")
        assert out.status == ra.STATUS_VERIFIED
        # ONE Math Checker report: the attached file was updated in place.
        assert google.report_files() == ["drive-1"]
        assert len(google.calls["upload"]) == 1
        assert len(google.calls["attach"]) == 1

    def test_reprocessing_after_restart_does_not_duplicate(self, google):
        _attach("r1")
        _lose_attachment_id("r1")
        # A fresh process has no in-memory state: everything is re-read from the
        # database, which is exactly what a restart looks like.
        ra._submission_locks.clear()
        _attach("r1")
        assert google.report_files() == ["drive-1"]
        assert len(google.calls["attach"]) == 1
        assert len(google.calls["upload"]) == 1

    def test_upload_failure_is_safe_and_never_adds_a_duplicate(self, google):
        # The report was removed from the submission, so a fresh upload is
        # required - and that upload fails.
        google.upload_fail = True
        out = _attach("r1")
        assert out.status in ra.FAILED_STATUSES
        assert google.report_files() == []
        assert google.calls["attach"] == []

    def test_reconciliation_failure_adds_no_second_report(self, google):
        _attach("r1")
        _lose_attachment_id("r1")
        # Ownership changed, so the attached file cannot be replaced - and
        # Google offers no API to remove it.
        google.ownership_ok = False
        out = _attach("r1", earned=9, percentage=90.0)
        assert out.status == ra.STATUS_RECONCILE_FAILED
        assert "no API to remove" in out.message
        # The ONE existing report is left intact; no duplicate is added.
        assert google.report_files() == ["drive-1"]
        assert len(google.calls["attach"]) == 1

    def test_unrelated_attachments_are_never_touched(self, google):
        google.extra = ["student-own-work", "teacher-mark-scheme"]
        _attach("r1")
        _lose_attachment_id("r1")
        _attach("r1", earned=9)
        assert google.extra == ["student-own-work", "teacher-mark-scheme"]
        assert set(google.extra).issubset(
            set(ra._attachment_ids(google._submission())))
        assert google.report_files() == ["drive-1"]

    def test_two_simultaneous_attachments_cannot_duplicate(self, google):
        _attach("r1")
        _lose_attachment_id("r1")
        errors: list = []

        def run():
            try:
                ra.attach_report("tok", _result("r1", earned=9),
                                 course_id=COURSE, assignment_title="HW")
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=run) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert not errors
        assert google.report_files() == ["drive-1"]
        assert len(google.calls["upload"]) == 1


# =========================================================================
# 2. Automatic grade return
# =========================================================================

class TestGradeWrite:

    def test_assign_grade_sets_both_draft_and_assigned(self, monkeypatch):
        """Google's guide: never set assignedGrade without draftGrade, and
        ``return`` does not copy the draft into the assigned grade."""
        _persist(_result("r1"))
        mg.record_managed(CW, COURSE, title="HW")
        seen: list[tuple] = []

        def fake_request(method, url, token, **kw):
            seen.append((method, kw.get("params"), kw.get("json")))
            return {}

        monkeypatch.setattr(gc, "classroom_request", fake_request)
        monkeypatch.setattr("app.classroom.auth.get_access_token", lambda: "t")
        out = mg.publish(CW, COURSE, [{"type": "assign_grade",
                                       "submission_id": SUBMISSION, "grade": 8}])
        assert out["summary"]["published"] == 1
        method, params, body = seen[0]
        assert method == "PATCH"
        assert set(params["updateMask"].split(",")) == {"draftGrade", "assignedGrade"}
        assert body["draftGrade"] == 8
        assert body["assignedGrade"] == 8


class TestGradeVerification:

    def _runner(self, submission: dict):
        from app.automation.runner import WorkflowRunner
        return WorkflowRunner(submission_reader=lambda job: dict(submission),
                              config=None, token_fn=lambda: "t")

    def test_a_stale_draft_does_not_mask_a_correct_assigned_grade(self):
        r = self._runner({"draftGrade": 3, "assignedGrade": 8,
                          "state": "RETURNED"})
        # Must not raise: the assigned grade IS what was submitted.
        r._verify_grade({"submission_id": SUBMISSION, "grade": 8}, 8)

    def test_verification_fails_when_neither_field_holds_the_grade(self):
        from app.automation import runner as runner_mod
        r = self._runner({"draftGrade": 3, "assignedGrade": None,
                          "state": "RETURNED"})
        with pytest.raises(runner_mod.StepFailure) as err:
            r._verify_grade({"submission_id": SUBMISSION, "grade": 8}, 8)
        assert err.value.state == "GRADE_VERIFICATION_FAILED"


class TestGradeWriteIsRetrySafe:

    def _job(self) -> dict:
        return {"coursework_id": CW, "submission_id": SUBMISSION, "grade": 8}

    def test_no_prior_attempt_always_writes(self):
        from app.automation.runner import WorkflowRunner
        called = {"n": 0}

        def reader(job):
            called["n"] += 1
            return {"draftGrade": 8, "assignedGrade": 8}

        r = WorkflowRunner(submission_reader=reader)
        # Our audit log shows no grade write ever attempted -> evidence "none".
        assert mg.grade_write_evidence(CW, SUBMISSION) == "none"
        assert r._grade_already_present(self._job(), 8) is False

    def test_an_unconfirmed_write_triggers_a_remote_check(self):
        from app.automation.runner import WorkflowRunner
        # Simulate a grade write that was attempted but never confirmed.
        op_id = mg._start_op(CW, COURSE, SUBMISSION,
                             {"type": "assign_grade", "grade": 8})
        mg._finish_op(op_id, mg.STATUS_FAILED, http=504, code="timeout")
        assert mg.grade_write_evidence(CW, SUBMISSION) == "unknown"

        r = WorkflowRunner(submission_reader=lambda job: {
            "draftGrade": 8, "assignedGrade": 8})
        # Google really did apply it -> do NOT submit again.
        assert r._grade_already_present(self._job(), 8) is True
        # But a DIFFERENT grade is not present, so the write proceeds.
        assert r._grade_already_present({**self._job(), "grade": 5}, 5) is False

    def test_a_published_write_is_recognised_as_evidence(self):
        op_id = mg._start_op(CW, COURSE, SUBMISSION,
                             {"type": "assign_grade", "grade": 8})
        mg._finish_op(op_id, mg.STATUS_PUBLISHED, http=200)
        assert mg.grade_write_evidence(CW, SUBMISSION) == "published"


# =========================================================================
# 3. No AI model identity in student-facing reports
# =========================================================================

MODEL = "gemini-3.0-flash-preview-09-2025"


def _report_text(pdf_bytes) -> str:
    """Extract the visible text of a rendered PDF (bytes or BytesIO)."""
    from pypdf import PdfReader
    if hasattr(pdf_bytes, "getvalue"):
        pdf_bytes = pdf_bytes.getvalue()
    reader = PdfReader(io.BytesIO(pdf_bytes))
    return "\n".join((p.extract_text() or "") for p in reader.pages)


def _correction_result(**over) -> dict:
    row = {
        "id": "r1", "coursework_id": CW, "submission_id": SUBMISSION,
        "student_id": STUDENT, "display_name": "Student A",
        "earned": 7, "available": 10, "percentage": 70.0,
        "provisional_grade": "7",
        "marking_model": MODEL,
        "result_json": {"questions": [
            {"question_number": "1", "max_marks": 5, "awarded_marks": 4,
             "correct_method": "expand and simplify",
             "loss_reason": "sign slip"},
            {"question_number": "2", "max_marks": 5, "awarded_marks": 3,
             "correct_method": "factorise", "loss_reason": "incomplete"},
        ], "overall_notes": {"summary": "Steady work.",
                             "strengths": ["clear method"],
                             "revision_areas": ["signs"]}},
    }
    row.update(over)
    return row


class TestReportPrivacy:

    @pytest.mark.parametrize("detail", ["summary", "standard", "detailed"])
    def test_correction_report_never_names_the_model(self, detail):
        from app.api.report_routes import render_result_pdf
        text = _report_text(render_result_pdf(_correction_result(),
                                             detail=detail)).lower()
        assert "gemini" not in text
        assert "model" not in text
        assert MODEL.lower() not in text

    def test_correction_report_keeps_marks_and_feedback(self):
        from app.api.report_routes import render_result_pdf
        text = _report_text(render_result_pdf(_correction_result(),
                                             detail="detailed"))
        # Removing the model line must not disturb the student's own result.
        assert "Student A" in text
        assert "sign slip" in text
        assert "Steady work." in text
        assert "70" in text
        assert "Q1" in text and "Q2" in text

    def test_student_tracking_report_has_no_model_column(self):
        import app.api.student_routes as sr
        # A student exists once a result refers to them.
        _persist(_result("r1", marking_model=MODEL))
        data = sr._student_report_data(STUDENT)
        pdf = sr._render_student_report_pdf(data, "stamp")
        text = _report_text(pdf)
        assert "gemini" not in text.lower()
        assert MODEL.lower() not in text.lower()
        assert "Model" not in text
