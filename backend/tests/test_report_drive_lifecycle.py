"""Temporary Drive file lifecycle + the @OwnerAccessRequired 403 (§§3, 5-18, 24).

The live 403 was a permanent ownership mismatch, not a transient error: the
Drive file was owned by the student's account while the app authenticated as
the teacher, and Google requires the caller to OWN a file before
``modifyAttachments`` will attach it. These tests lock down the fix:

* ownership is verified BEFORE attaching, and reported as its own error code;
* a file we do not own is re-created, never blindly retried;
* the temporary file is RETAINED once the attachment is confirmed (Classroom
  references it), so it is never deleted out from under a student;
* a cleanup failure never fails the correction;
* a retry reuses the PDF and the file instead of making "Report (1).pdf";
* only files stamped with this app's own markers are ever deleted.

No network, no tokens, no real Drive: the Drive client is faked.
"""

from __future__ import annotations

import pytest

from app import db
from app.classroom import client as gc
from app.classroom import report_attachment as ra
from app.classroom import report_cleanup as rc

RESULT_ID = "res-lifecycle"
COURSE_ID = "course-1"
COURSEWORK_ID = "cw-1"
SUBMISSION_ID = "sub-1"
TEACHER = "teacher@example.com"
STUDENT = "student@example.com"


def _make_pdf(text: str = "Correction Report for Student One") -> bytes:
    import io
    from reportlab.pdfgen import canvas
    buf = io.BytesIO()
    c = canvas.Canvas(buf)
    c.drawString(72, 720, text)
    c.showPage()
    c.save()
    return buf.getvalue()


#: A real, parseable report, so the pre-upload validation guard sees what a
#: genuine rendered report looks like.
PDF = _make_pdf()


# --------------------------------------------------------------- fake Drive

class FakeDrive:
    """In-memory Drive: ownership, appProperties, and deletion are real."""

    def __init__(self):
        self.files: dict[str, dict] = {}
        self.deleted: list[str] = []
        self._n = 0

    def add(self, *, owner: str, app_properties: dict | None = None,
            name: str = "Report.pdf", trashed: bool = False) -> str:
        self._n += 1
        fid = f"file-{self._n}"
        self.files[fid] = {
            "id": fid, "name": name, "mimeType": "application/pdf",
            "owners": [{"emailAddress": owner}],
            "appProperties": dict(app_properties or {}),
            "trashed": trashed, "createdTime": "2026-01-01T00:00:00Z",
            "capabilities": {"canDelete": True, "canOwnerDrive": True},
        }
        return fid

    def meta(self, _token, file_id):
        f = self.files.get(file_id)
        return dict(f) if f else None

    def delete(self, _token, file_id):
        if file_id not in self.files:
            return
        self.files[file_id]["trashed"] = True
        self.deleted.append(file_id)

    def find_temp(self, _token):
        return [dict(f) for f in self.files.values()
                if str((f.get("appProperties") or {})
                       .get(gc.APP_PROP_TEMP)).lower() == "true"
                and not f.get("trashed")]


@pytest.fixture
def drive(monkeypatch):
    fake = FakeDrive()
    monkeypatch.setattr(gc, "get_drive_file", fake.meta)
    monkeypatch.setattr(gc, "delete_drive_file", fake.delete)
    monkeypatch.setattr(gc, "find_temp_reports", fake.find_temp)
    # The uploaded file downloads back byte-for-byte (pre-attach verification).
    monkeypatch.setattr(gc, "download_drive_file",
                        lambda t, f: ("Report.pdf", PDF, "application/pdf"))
    monkeypatch.setattr(
        gc, "verify_drive_ownership",
        lambda token, fid, *, account_email: _verify(fake, fid, account_email))
    monkeypatch.setattr(
        gc, "is_our_temp_report",
        lambda meta, *, result_id=None, token_value=None:
        gc.__dict__["_orig_is_our"](meta, result_id=result_id,
                                    token_value=token_value))
    return fake


def _verify(fake, file_id, account_email):
    meta = fake.meta(None, file_id)
    if meta is None:
        return {"ok": False, "reason": "file_not_found", "owners": [],
                "account": account_email, "exists": False, "can_delete": False,
                "is_our_temp": False, "file_id": file_id}
    owners = gc.drive_file_owners(meta)
    acct = (account_email or "").lower()
    return {"ok": acct in owners, "reason": "owner" if acct in owners else "not_owner",
            "owners": owners, "account": acct, "exists": True,
            "can_delete": True, "is_our_temp": False, "file_id": file_id}


# The real implementation must still be reachable under a private alias.
gc._orig_is_our = gc.is_our_temp_report


@pytest.fixture
def state(fresh_state):
    db.execute("DELETE FROM report_attachments WHERE result_id = ?",
               (RESULT_ID,))
    # report_attachments.result_id is a FK onto results, and results.job_id is
    # a FK onto correction_jobs - both parents must exist.
    db.execute("DELETE FROM results WHERE id = ?", (RESULT_ID,))
    db.execute("DELETE FROM correction_jobs WHERE id = ?", ("job-lifecycle",))
    db.execute(
        "INSERT INTO correction_jobs(id, submission_id, coursework_id, "
        "course_id, student_id, student_name, status, created_at, updated_at) "
        "VALUES(?,?,?,?,?,?,?,?,?)",
        ("job-lifecycle", SUBMISSION_ID, COURSEWORK_ID, COURSE_ID, "stu-1",
         "Student One", "COMPLETED", 0.0, 0.0))
    db.execute(
        "INSERT INTO results(id, job_id, coursework_id, submission_id, "
        "student_id, student_name, earned, available, percentage, result_json, "
        "created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (RESULT_ID, "job-lifecycle", COURSEWORK_ID, SUBMISSION_ID, "stu-1",
         "Student One", 10, 10, 100.0, "{}", 0.0))
    yield RESULT_ID
    db.execute("DELETE FROM report_attachments WHERE result_id = ?",
               (RESULT_ID,))
    db.execute("DELETE FROM results WHERE id = ?", (RESULT_ID,))
    db.execute("DELETE FROM correction_jobs WHERE id = ?", ("job-lifecycle",))


@pytest.fixture
def _fk(state):
    """Ensures the parent result row exists for the attachment state."""
    return state


def _mk_state(lifecycle, *, drive_file_id=None, temp_token=None,
              content=PDF, status=ra.STATUS_UPLOADED):
    from app.classroom.report_attachment import _save
    _save({
        "result_id": RESULT_ID, "course_id": COURSE_ID,
        "coursework_id": COURSEWORK_ID, "submission_id": SUBMISSION_ID,
        "student_id": "stu-1", "report_filename": "R.pdf",
        "drive_file_id": drive_file_id, "status": status, "error": None,
        "attempts": 1, "lifecycle": lifecycle, "owner_email": TEACHER,
        "is_temp": True, "temp_token": temp_token,
        "content_bytes": content, "created_at": 0.0,
    })


# --- §3 ownership is verified BEFORE an attach --------------------------

class TestOwnershipVerifiedBeforeAttach:
    def test_a_file_owned_by_someone_else_is_never_attached(
            self, _fk, drive, monkeypatch):
        """The live 403: the app is not the owner, so it must NOT call
        modifyAttachments at all, and must say why."""
        foreign = drive.add(owner=STUDENT)          # student owns it
        _mk_state(rc.LIFECYCLE_UPLOADED, drive_file_id=foreign)
        called = []
        monkeypatch.setattr(gc, "modify_submission_attachments",
                            lambda *a, **k: called.append(1))
        check = gc.verify_drive_ownership("tok", foreign,
                                          account_email=TEACHER)
        assert check["ok"] is False
        assert check["reason"] == "not_owner"
        assert check["owners"] == [STUDENT.lower()]
        # attach_report gates on exactly this check.
        assert not check["ok"]
        assert called == [], "the attach API must not be called when not owner"

    def test_attach_report_stops_at_the_ownership_check(self, _fk, drive,
                                                        monkeypatch):
        """End-to-end: a foreign file makes attach_report return the ownership
        error, keep the file, and never touch the attach API."""
        foreign = drive.add(owner=STUDENT)
        _mk_state(rc.LIFECYCLE_UPLOADED, drive_file_id=foreign)
        monkeypatch.setattr(gc, "get_student_submission", lambda *a, **k: {
            "id": SUBMISSION_ID, "userId": "stu-1",
            "associatedWithDeveloper": True,
            "assignmentSubmission": {"attachments": []}})
        monkeypatch.setattr(ra, "render_report_pdf", lambda *a, **k: PDF)
        monkeypatch.setattr(gc, "get_user_profile",
                            lambda *a, **k: {"emailAddress": STUDENT})
        called = []
        monkeypatch.setattr(gc, "modify_submission_attachments",
                            lambda *a, **k: called.append(1))
        # Even a fresh upload is treated as not-ours, so the guard must hold.
        monkeypatch.setattr(
            gc, "upload_report_to_drive",
            lambda *a, **k: {"id": drive.add(owner=STUDENT)})

        outcome = ra.attach_report(
            "tok", {"id": RESULT_ID, "coursework_id": COURSEWORK_ID,
                    "submission_id": SUBMISSION_ID, "student_id": "stu-1"},
            course_id=COURSE_ID, account_email=TEACHER)

        assert outcome.status == ra.STATUS_OWNERSHIP_ERROR
        assert ra.ERROR_OWNERSHIP in outcome.message
        assert called == [], "no attach attempt may be made without ownership"
        # The file is kept for diagnosis/retry - never deleted on failure (§15).
        assert foreign not in drive.deleted
        state = ra.get_state(RESULT_ID)
        assert state["retryable"] is True
        assert state["attached"] is False

    def test_a_foreign_owned_file_is_re_created_under_the_current_account(
            self, _fk, drive, monkeypatch):
        """The live @OwnerAccessRequired 403 (12 attempts, real DB): the recorded
        Drive file belonged to the student, so the app could never attach it.
        Instead of retrying forever it must create a fresh file IT owns and
        attach that - leaving the foreign file untouched."""
        foreign = drive.add(owner=STUDENT)
        _mk_state(rc.LIFECYCLE_UPLOADED, drive_file_id=foreign)
        monkeypatch.setattr(ra, "render_report_pdf", lambda *a, **k: PDF)
        monkeypatch.setattr(gc, "get_user_profile",
                            lambda *a, **k: {"emailAddress": STUDENT})
        created = {}
        attached = {"ids": []}

        def _read(*a, **k):
            return {"id": SUBMISSION_ID, "userId": "stu-1",
                    "associatedWithDeveloper": True,
                    "assignmentSubmission": {"attachments": [
                        {"driveFile": {"id": f}} for f in attached["ids"]]}}

        monkeypatch.setattr(gc, "get_student_submission", _read)

        def _upload(token, filename, content, **kw):
            fid = drive.add(owner=TEACHER)          # ours this time
            created["new"] = fid
            return {"id": fid, "name": filename,
                    "owners": [{"emailAddress": TEACHER}]}

        monkeypatch.setattr(gc, "upload_report_to_drive", _upload)

        def _attach(token, course_id, coursework_id, submission_id, **kw):
            attached["ids"] = list(kw.get("add_drive_file_ids") or [])
            return _read()

        monkeypatch.setattr(gc, "modify_submission_attachments", _attach)

        outcome = ra.attach_report(
            "tok", {"id": RESULT_ID, "coursework_id": COURSEWORK_ID,
                    "submission_id": SUBMISSION_ID, "student_id": "stu-1"},
            course_id=COURSE_ID, account_email=TEACHER)

        assert outcome.status == ra.STATUS_VERIFIED
        assert created["new"] != foreign, "a fresh, OWNED file was created"
        assert attached["ids"] == [created["new"]]
        assert foreign not in drive.deleted, "the old file is never deleted"

    def test_the_teacher_owning_the_file_passes(self, _fk, drive):
        ours = drive.add(owner=TEACHER)
        check = gc.verify_drive_ownership("tok", ours, account_email=TEACHER)
        assert check["ok"] is True
        assert check["reason"] == "owner"

    def test_a_deleted_file_is_reported_as_missing(self, _fk, drive):
        assert gc.verify_drive_ownership("tok", "nope",
                                         account_email=TEACHER)["reason"] \
            == "file_not_found"

    def test_ownership_never_returns_a_token(self, _fk, drive):
        fid = drive.add(owner=TEACHER)
        check = gc.verify_drive_ownership("super-secret-token", fid,
                                          account_email=TEACHER)
        blob = repr(check)
        assert "super-secret-token" not in blob


# --- §5/§6 the ATTACHED file is retained, never deleted ------------------

class TestAttachedFileIsRetained:
    def test_a_confirmed_attachment_KEEPS_the_temporary_file(
            self, _fk, drive):
        """The bug fix: Classroom holds a reference to this Drive file, so
        deleting it produces "the document couldn't be opened. It may have
        been deleted." It must therefore be RETAINED."""
        token = rc.new_temp_token()
        fid = drive.add(owner=TEACHER,
                        app_properties=rc.stamp_properties(RESULT_ID, token)
                        | {gc.APP_PROP_TEMP: "true"})
        _mk_state(rc.LIFECYCLE_CONFIRMED, drive_file_id=fid, temp_token=token)
        out = rc.cleanup_result(RESULT_ID, "tok", account_email=TEACHER)
        assert out["ok"] is True
        assert out["skipped"] == "retained"
        assert fid not in drive.deleted, (
            "an attached report must never be deleted - that breaks the "
            "Classroom attachment")
        row = db.query_one("SELECT lifecycle, drive_file_id, content_bytes "
                           "FROM report_attachments WHERE result_id = ?",
                           (RESULT_ID,))
        assert row["lifecycle"] == rc.LIFECYCLE_RETAINED
        assert row["drive_file_id"] == fid
        # The retry buffer is dropped, but the file itself stays.
        assert row["content_bytes"] is None

    def test_a_file_still_awaiting_attachment_is_never_deleted(
            self, _fk, drive):
        token = rc.new_temp_token()
        fid = drive.add(owner=TEACHER,
                        app_properties={gc.APP_PROP_TEMP: "true",
                                        gc.APP_PROP_RESULT: RESULT_ID,
                                        gc.APP_PROP_TOKEN: token})
        # Status is still only UPLOADED: the attach has NOT been confirmed.
        _mk_state(rc.LIFECYCLE_UPLOADED, drive_file_id=fid, temp_token=token)
        rc.retry_pending_cleanup("tok", account_email=TEACHER)
        assert fid not in drive.deleted, (
            "a file must survive until the attachment is confirmed (§6/§15)")


# --- §12 cleanup failure is not a correction failure -------------------

class TestCleanupFailureIsNotAFailure:
    def test_a_delete_error_is_recorded_and_retryable(self, _fk,
                                                      drive, monkeypatch):
        token = rc.new_temp_token()
        fid = drive.add(owner=TEACHER,
                        app_properties={gc.APP_PROP_TEMP: "true",
                                        gc.APP_PROP_RESULT: RESULT_ID,
                                        gc.APP_PROP_TOKEN: token})
        # A file that is NOT attached (lifecycle UPLOADED) is the only kind
        # eligible for deletion; its delete failing must be recorded.
        _mk_state(rc.LIFECYCLE_UPLOADED, drive_file_id=fid, temp_token=token,
                  status=ra.STATUS_CLEANUP_PENDING)

        def boom(_t, _f):
            raise gc.ClassroomApiError(503, "service unavailable")

        monkeypatch.setattr(gc, "delete_drive_file", boom)
        out = rc.cleanup_result(RESULT_ID, "tok", account_email=TEACHER)
        assert out["ok"] is False
        assert out["skipped"] == "cleanup_failed"
        row = db.query_one("SELECT status, lifecycle, cleanup_attempts, "
                           "cleanup_error FROM report_attachments "
                           "WHERE result_id = ?", (RESULT_ID,))
        # The correction itself stays successful; only housekeeping is pending.
        assert row["status"] == ra.STATUS_CLEANUP_PENDING
        assert row["lifecycle"] == rc.LIFECYCLE_CLEANUP_FAILED
        assert row["cleanup_attempts"] == 1
        assert "drive_delete_failed" in row["cleanup_error"]

    def test_the_retry_then_succeeds(self, _fk, drive, monkeypatch):
        token = rc.new_temp_token()
        fid = drive.add(owner=TEACHER,
                        app_properties={gc.APP_PROP_TEMP: "true",
                                        gc.APP_PROP_RESULT: RESULT_ID,
                                        gc.APP_PROP_TOKEN: token})
        _mk_state(rc.LIFECYCLE_UPLOADED, drive_file_id=fid, temp_token=token)
        monkeypatch.setattr(gc, "delete_drive_file",
                            lambda *a: (_ for _ in ()).throw(
                                gc.ClassroomApiError(503, "busy")))
        rc.cleanup_result(RESULT_ID, "tok", account_email=TEACHER)
        monkeypatch.setattr(gc, "delete_drive_file", drive.delete)
        out = rc.retry_pending_cleanup("tok", account_email=TEACHER)
        assert out["cleaned"] == 1
        assert fid in drive.deleted
        # Only the DELETE was retried - no new file, no re-render (§13).
        assert out["attempted"] == 1

    def test_a_dead_authorization_is_a_pending_cleanup_not_a_crash(
            self, _fk, drive, monkeypatch):
        """A revoked Google grant must never raise out of the cleanup path.

        The live failure mode this guards: the refresh token dies between
        corrections, and "Retry Drive cleanup" turned into a 500 instead of an
        honest "still pending".
        """
        token = rc.new_temp_token()
        fid = drive.add(owner=TEACHER,
                        app_properties={gc.APP_PROP_TEMP: "true",
                                        gc.APP_PROP_RESULT: RESULT_ID,
                                        gc.APP_PROP_TOKEN: token})
        _mk_state(rc.LIFECYCLE_CLEANUP_FAILED, drive_file_id=fid,
                  temp_token=token, status=ra.STATUS_CLEANUP_PENDING)

        def _dead(*_a, **_k):
            raise RuntimeError("Google authorization expired or was revoked. "
                               "Reconnect Google Classroom from the Classroom "
                               "page.")

        monkeypatch.setattr(gc, "get_drive_file", _dead)
        monkeypatch.setattr(gc, "find_temp_reports", _dead)
        out = rc.cleanup_result(RESULT_ID, "tok", account_email=TEACHER)
        assert out["ok"] is False
        assert "drive_lookup_failed" in out["reason"]
        assert drive.deleted == [], "nothing may be deleted blind"

        sweep = rc.retry_pending_cleanup("tok", account_email=TEACHER)
        assert sweep["pending"] == 1
        orphans = rc.sweep_orphans("tok", account_email=TEACHER)
        assert orphans["ok"] is False
        assert "Reconnect" in orphans["error"]

        # And the correction is still a success - only housekeeping is pending.
        assert ra.get_state(RESULT_ID)["attached"] is True
        assert ra.get_state(RESULT_ID)["drive_cleanup"] == "pending"

    def test_state_reports_cleanup_separately_from_the_correction(
            self, _fk, drive):
        token = rc.new_temp_token()
        fid = drive.add(owner=TEACHER,
                        app_properties={gc.APP_PROP_TEMP: "true",
                                        gc.APP_PROP_RESULT: RESULT_ID,
                                        gc.APP_PROP_TOKEN: token})
        _mk_state(rc.LIFECYCLE_CLEANUP_FAILED, drive_file_id=fid,
                  temp_token=token, status=ra.STATUS_CLEANUP_PENDING)
        s = ra.get_state(RESULT_ID)
        assert s["attached"] is True
        assert s["drive_cleanup"] == "pending"
        assert "content_bytes" not in s


class TestGradeStateIsItsOwnState:
    """§19/§20: the three states are reported separately, never inferred.

    "Grade returned", "Report attached" and "Drive cleanup" are three
    different facts. A confirmed attachment must never paint a green tick
    over a grade we have no evidence for.
    """

    def test_attaching_the_report_does_not_imply_a_grade(self, _fk):
        # A genuinely attached report keeps its Drive file id (retained).
        _mk_state(rc.LIFECYCLE_ATTACHED, drive_file_id="file-keep",
                  status=ra.STATUS_VERIFIED)
        s = ra.get_state(RESULT_ID)
        assert s["attached"] is True
        assert s["grade_returned"] is False, \
            "an attachment is not proof of a returned grade"
        assert s["drive_cleanup"] == "retained"

    def test_a_synced_draft_grade_is_read_back_as_returned(self, _fk):
        from app.classroom import managed
        _mk_state(rc.LIFECYCLE_CLEANED, status=ra.STATUS_VERIFIED)
        db.execute(
            "INSERT OR REPLACE INTO classroom_submissions("
            "submission_id, coursework_id, course_id, student_id, "
            "current_state, draft_grade, max_points, last_synced_at, "
            "first_seen_at) VALUES(?,?,?,?,?,?,?,?,?)",
            (SUBMISSION_ID, COURSEWORK_ID, COURSE_ID, "stu-1", "TURNED_IN",
             9.0, 10.0, 0.0, 0.0))
        try:
            s = ra.get_state(RESULT_ID)
            assert s["grade_returned"] is True
        finally:
            db.execute("DELETE FROM classroom_submissions WHERE submission_id = ?",
                       (SUBMISSION_ID,))

    def test_a_published_grade_op_counts_as_returned(self, _fk):
        from app.classroom import managed
        _mk_state(rc.LIFECYCLE_CLEANED, status=ra.STATUS_VERIFIED)
        db.execute(
            "INSERT INTO classroom_publish_ops("
            "coursework_id, course_id, submission_id, op_type, status, "
            "created_at, attempted_at) VALUES(?,?,?,?,?,?,?)",
            (COURSEWORK_ID, COURSE_ID, SUBMISSION_ID, "draft_grade",
             managed.STATUS_PUBLISHED, 0.0, 0.0))
        try:
            assert ra.get_state(RESULT_ID)["grade_returned"] is True
        finally:
            db.execute("DELETE FROM classroom_publish_ops WHERE submission_id = ?",
                       (SUBMISSION_ID,))

    def test_a_refused_grade_write_is_never_reported_as_returned(self, _fk):
        from app.classroom import managed
        _mk_state(rc.LIFECYCLE_CLEANED, status=ra.STATUS_VERIFIED)
        db.execute(
            "INSERT INTO classroom_publish_ops("
            "coursework_id, course_id, submission_id, op_type, status, "
            "created_at, attempted_at) VALUES(?,?,?,?,?,?,?)",
            (COURSEWORK_ID, COURSE_ID, SUBMISSION_ID, "draft_grade",
             managed.STATUS_FAILED, 0.0, 0.0))
        try:
            s = ra.get_state(RESULT_ID)
            assert s["grade_returned"] is False
            assert "failed" in (s["grade_return_note"] or "")
        finally:
            db.execute("DELETE FROM classroom_publish_ops WHERE submission_id = ?",
                       (SUBMISSION_ID,))


# --- §17 we only ever delete our own temporary files -------------------

class TestNeverDeleteUserFiles:
    def test_a_file_without_our_marker_is_refused(self, _fk, drive):
        teacher_doc = drive.add(owner=TEACHER, name="Report.pdf",
                                app_properties=None)   # a teacher's own file
        _mk_state(rc.LIFECYCLE_UPLOADED, drive_file_id=teacher_doc)
        out = rc.cleanup_result(RESULT_ID, "tok", account_email=TEACHER)
        assert out["ok"] is False
        assert "not provably" in out["reason"]
        assert teacher_doc not in drive.deleted

    def test_a_marker_for_a_different_correction_is_refused(self, _fk,
                                                             drive):
        token = rc.new_temp_token()
        other = drive.add(owner=TEACHER,
                          app_properties={gc.APP_PROP_TEMP: "true",
                                          gc.APP_PROP_RESULT: "some-other-result",
                                          gc.APP_PROP_TOKEN: token})
        _mk_state(rc.LIFECYCLE_UPLOADED, drive_file_id=other,
                  temp_token=token)
        out = rc.cleanup_result(RESULT_ID, "tok", account_email=TEACHER)
        assert out["ok"] is False
        assert other not in drive.deleted

    def test_a_matching_token_is_required(self, _fk, drive):
        fid = drive.add(owner=TEACHER,
                        app_properties={gc.APP_PROP_TEMP: "true",
                                        gc.APP_PROP_RESULT: RESULT_ID,
                                        gc.APP_PROP_TOKEN: "guessed"})
        _mk_state(rc.LIFECYCLE_UPLOADED, drive_file_id=fid,
                  temp_token="the-real-token")
        out = rc.cleanup_result(RESULT_ID, "tok", account_email=TEACHER)
        assert out["ok"] is False
        assert fid not in drive.deleted

    def test_a_file_we_no_longer_own_is_refused(self, _fk, drive):
        token = rc.new_temp_token()
        fid = drive.add(owner=STUDENT,          # ownership moved away
                        app_properties={gc.APP_PROP_TEMP: "true",
                                        gc.APP_PROP_RESULT: RESULT_ID,
                                        gc.APP_PROP_TOKEN: token})
        _mk_state(rc.LIFECYCLE_UPLOADED, drive_file_id=fid, temp_token=token)
        out = rc.cleanup_result(RESULT_ID, "tok", account_email=TEACHER)
        assert out["ok"] is False
        assert "no longer owns" in out["reason"]
        assert fid not in drive.deleted

    def test_sharing_with_a_student_can_never_transfer_ownership(self):
        with pytest.raises(gc.ClassroomApiError):
            gc.share_drive_file("tok", "fid", STUDENT, role="owner")


# --- §14 no duplicate files -------------------------------------------

class TestNoDuplicateFiles:
    def test_a_retry_reuses_the_existing_owned_file(self, _fk, drive,
                                                    monkeypatch):
        token = rc.new_temp_token()
        fid = drive.add(owner=TEACHER,
                        app_properties={gc.APP_PROP_TEMP: "true",
                                        gc.APP_PROP_RESULT: RESULT_ID,
                                        gc.APP_PROP_TOKEN: token})
        _mk_state(rc.LIFECYCLE_UPLOADED, drive_file_id=fid, temp_token=token)
        uploads = []
        monkeypatch.setattr(gc, "replace_drive_file_content",
                            lambda t, f, c: uploads.append(("update", f)))
        def _no_upload(*a, **k):
            raise AssertionError("a retry must not upload a second file")
        monkeypatch.setattr(gc, "upload_report_to_drive", _no_upload)
        # A file we own is reused in place: no "Report (1).pdf".
        assert gc.verify_drive_ownership("t", fid, account_email=TEACHER)["ok"]
        assert len(drive.files) == 1


# --- §16 orphan sweeping ----------------------------------------------

class TestOrphanSweep:
    def test_orphans_are_removed_and_user_files_are_kept(self, _fk,
                                                        drive):
        ours = rc.new_temp_token()
        orphan = drive.add(owner=TEACHER, name="Report.pdf",
                           app_properties={gc.APP_PROP_TEMP: "true",
                                           gc.APP_PROP_RESULT: "gone-result",
                                           gc.APP_PROP_TOKEN: ours,
                                           gc.APP_PROP_CREATED: "1"})
        teacher_file = drive.add(owner=TEACHER, name="Lesson plan.pdf")
        out = rc.sweep_orphans("tok", account_email=TEACHER, older_than=0)
        assert orphan in [d["file_id"] for d in out["deleted"]]
        assert teacher_file not in drive.deleted
        assert teacher_file not in [d["file_id"] for d in out["deleted"]]

    def test_a_recent_file_is_not_swept(self, _fk, drive):
        import time as _t
        recent = drive.add(owner=TEACHER,
                           app_properties={gc.APP_PROP_TEMP: "true",
                                           gc.APP_PROP_RESULT: "gone",
                                           gc.APP_PROP_TOKEN: "t",
                                           gc.APP_PROP_CREATED:
                                               str(int(_t.time()))})
        rc.sweep_orphans("tok", account_email=TEACHER, older_than=86400)
        assert recent not in drive.deleted

    def test_a_file_still_referenced_is_never_swept(self, _fk, drive):
        token = rc.new_temp_token()
        fid = drive.add(owner=TEACHER,
                        app_properties={gc.APP_PROP_TEMP: "true",
                                        gc.APP_PROP_RESULT: RESULT_ID,
                                        gc.APP_PROP_TOKEN: token,
                                        gc.APP_PROP_CREATED: "1"})
        _mk_state(rc.LIFECYCLE_UPLOADED, drive_file_id=fid, temp_token=token)
        rc.sweep_orphans("tok", account_email=TEACHER, older_than=0)
        assert fid not in drive.deleted
