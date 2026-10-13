"""Correction report -> Google Classroom student submission attachment (P0).

Covers the whole chain the feature promises: correct student, Drive upload,
narrow sharing, modifyAttachments on the RIGHT StudentSubmission, API-level
verification, idempotency (no duplicate reports), retry WITHOUT re-marking,
and filename safety.

All Google calls are faked; the engine logic is what is under test.
"""

from __future__ import annotations

import io
import json

import pytest

from app.classroom import client as gc
from app.classroom import report_attachment as ra


def _make_pdf(text: str = "Correction Report for Student A") -> bytes:
    """A genuinely parseable one-page PDF, so the new pre-upload guard sees
    what a real rendered report looks like (a bare ``%PDF-1.4 fake`` is the
    exact thing the guard now rejects)."""
    from reportlab.pdfgen import canvas
    buf = io.BytesIO()
    c = canvas.Canvas(buf)
    c.drawString(72, 720, text)
    c.showPage()
    c.save()
    return buf.getvalue()


PDF_BYTES = _make_pdf()

COURSE = "course-1"
CW = "coursework-1"
#: The Google account the fakes behave as if they are authenticated with.
ACCOUNT = "teacher@school.edu"


# ------------------------------------------------------------------ helpers

def _result(**over):
    row = {
        "id": "result-1",
        "coursework_id": CW,
        "submission_id": "submission-A",   # StudentSubmission id (stable)
        "student_id": "user-A",            # Classroom user id (stable)
        "student_name": "Student A",
        "earned": 67, "available": 103, "percentage": 65.0,
        "provisional_grade": "7",
        "result_json": {"questions": []},
    }
    row.update(over)
    return row


def _persist(row: dict) -> dict:
    """Insert a real job + result row.

    report_attachments has a FOREIGN KEY to results(id) on purpose - an
    attachment must never exist without the correction it belongs to - so the
    tests create genuine rows rather than loosening the constraint.
    """
    from app import db
    job_id = f"job-{row['id']}"
    # NOT "INSERT OR REPLACE": REPLACE is a DELETE+INSERT, and results has
    # ON DELETE CASCADE to report_attachments, so re-seeding a result would
    # silently delete its attachment state (production never re-seeds results
    # — corrections are versioned instead). Insert only when absent.
    db.execute(
        """
        INSERT OR IGNORE INTO correction_jobs(
            id, status, created_at, updated_at)
        VALUES(?,?,?,?)
        """,
        (job_id, "COMPLETED", 1.0, 1.0))
    if db.query_one("SELECT id FROM results WHERE id = ?", (row["id"],)):
        return row
    db.execute(
        """
        INSERT INTO results(
            id, job_id, coursework_id, submission_id, student_id,
            student_name, earned, available, percentage, provisional_grade,
            result_json, created_at)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (row["id"], job_id, row.get("coursework_id"), row.get("submission_id"),
         row.get("student_id"), row.get("student_name"), row.get("earned", 0),
         row.get("available", 0), row.get("percentage", 0.0),
         row.get("provisional_grade"),
         json.dumps(row.get("result_json") or {}), 1.0))
    return row


def _submission(attachments=None, *, can_modify=True):
    return {
        "id": "submission-A",
        "userId": "user-A",
        "associatedWithDeveloper": can_modify,
        "assignmentSubmission": {"attachments": attachments or []},
    }


class G:                      # tiny namespace for the fake Google world
    calls: dict = {}
    state: dict = {}


@pytest.fixture
def google(monkeypatch):
    """Fake every Google call and record the exact payloads."""
    calls = {"upload": [], "update": [], "attach": [], "share": [], "read": []}
    state = {"attachments": [], "can_modify": True, "upload_id": "drive-1",
             "upload_fail": False, "attach_fail": None, "ignore": False,
             # Only files WE uploaded carry this app's appProperties, so a file
             # the student or teacher added is genuinely foreign.
             "files": {}}

    def _upload(token, filename, content, **kw):
        calls["upload"].append({"filename": filename, "bytes": len(content)})
        if state["upload_fail"]:
            raise gc.ClassroomApiError(500, "drive exploded")
        state["files"][state["upload_id"]] = dict(kw.get("app_properties") or {})
        return {"id": state["upload_id"], "name": filename,
                "owners": [{"emailAddress": ACCOUNT}]}

    def _update(token, file_id, content):
        calls["update"].append({"file_id": file_id, "bytes": len(content)})

    def _attach(token, course_id, coursework_id, submission_id, **kw):
        calls["attach"].append({
            "course_id": course_id, "coursework_id": coursework_id,
            "submission_id": submission_id,
            "add": list(kw.get("add_drive_file_ids") or []),
            "body_kwargs": sorted(kw)})
        if state["attach_fail"]:
            raise gc.ClassroomApiError(state["attach_fail"], "nope")
        if not state["ignore"]:
            for fid in kw.get("add_drive_file_ids") or []:
                state["attachments"].append({"driveFile": {"id": fid}})
        return _submission(state["attachments"], can_modify=state["can_modify"])

    def _read(token, course_id, coursework_id, submission_id):
        calls["read"].append(submission_id)
        return _submission(state["attachments"], can_modify=state["can_modify"])

    monkeypatch.setattr(gc, "upload_report_to_drive", _upload)
    # A file this app uploaded with its own token is owned by that account, so
    # the pre-attach ownership check passes. The failing-ownership path has its
    # own tests in test_report_drive_lifecycle.py.
    monkeypatch.setattr(
        gc, "verify_drive_ownership",
        lambda token, fid, *, account_email=None: {
            "ok": True, "reason": "owner", "owners": [ACCOUNT],
            "account": (account_email or ACCOUNT).lower(), "exists": True,
            "can_delete": True, "is_our_temp": True, "file_id": fid})
    # Cleanup after a confirmed attachment: the temporary file is deleted.
    monkeypatch.setattr(
        gc, "get_drive_file",
        lambda token, fid: {"id": fid, "trashed": False,
                            "mimeType": "application/pdf",
                            "size": str(len(PDF_BYTES)),
                            "name": "report.pdf",
                            "owners": [{"emailAddress": ACCOUNT}],
                            "appProperties": state["files"].get(fid, {})})
    # The uploaded file can be downloaded back byte-for-byte, which is what the
    # pre-attach Drive verification checks.
    monkeypatch.setattr(
        gc, "download_drive_file",
        lambda token, fid: ("report.pdf", PDF_BYTES, "application/pdf"))
    monkeypatch.setattr(gc, "is_our_temp_report", lambda *a, **k: True)
    monkeypatch.setattr(
        gc, "delete_drive_file",
        lambda token, fid: calls.setdefault("deleted", []).append(fid))
    monkeypatch.setattr(gc, "replace_drive_file_content", _update)
    monkeypatch.setattr(gc, "modify_submission_attachments", _attach)
    monkeypatch.setattr(gc, "get_student_submission", _read)
    monkeypatch.setattr(gc, "share_drive_file",
                        lambda t, f, e, role="reader":
                        calls["share"].append({"file": f, "email": e,
                                              "role": role}))
    monkeypatch.setattr(gc, "get_user_profile",
                        lambda t, uid: {"emailAddress": f"{uid}@school.edu"})
    monkeypatch.setattr(ra, "render_report_pdf",
                        lambda result, detail="standard": PDF_BYTES)
    G.calls, G.state = calls, state
    return G


def attach(row=None, *, course_id=COURSE, title="HW"):
    """Persist the result then attach it (the real call order)."""
    row = _persist(row or _result())
    return ra.attach_report("tok", row, course_id=course_id,
                            assignment_title=title)


# ------------------------------------------------------------- naming/safety

class TestFilenameSafety:
    def test_illegal_characters_are_removed(self):
        out = ra.sanitize_component('A/B\\C:D*E?F"G<H>I|J')
        for ch in '/\\:*?"<>|':
            assert ch not in out
        assert out.strip()

    def test_path_traversal_is_neutralised(self):
        for bad in ("../../etc/passwd", "..", "....//x", "a/../../b"):
            out = ra.sanitize_component(bad)
            assert ".." not in out
            assert "/" not in out and "\\" not in out

    def test_blank_and_dot_only_fall_back(self):
        assert ra.sanitize_component("") == "report"
        assert ra.sanitize_component("   ") == "report"
        assert ra.sanitize_component("...") == "report"

    def test_length_is_capped(self):
        assert len(ra.sanitize_component("x" * 5000)) <= ra._MAX_STEM

    def test_predictable_name_shape(self):
        assert ra.build_report_filename("Student A", "Ch1L7") == \
            "Student A_Ch1L7_Correction_Report.pdf"

    def test_name_is_deterministic_for_duplicate_protection(self):
        # A stable name is what stops Drive making "Report (1).pdf" clones.
        assert (ra.build_report_filename("S", "H")
                == ra.build_report_filename("S", "H"))


# ------------------------------------------------------------- happy path

class TestAttachHappyPath:
    def test_attaches_to_the_right_submission_and_verifies(self, google):
        out = attach()
        assert out.status == ra.STATUS_VERIFIED
        assert google.calls["attach"][0]["submission_id"] == "submission-A"
        assert google.calls["attach"][0]["add"] == ["drive-1"]
        assert ra.get_state("result-1")["status"] == ra.STATUS_VERIFIED

    def test_report_is_shared_with_only_that_student(self, google):
        attach()
        assert len(google.calls["share"]) == 1
        share = google.calls["share"][0]
        assert share["email"] == "user-A@school.edu"
        assert share["role"] == "reader"      # never "anyone"

    def test_uploads_a_pdf_with_the_predictable_name(self, google):
        attach()
        up = google.calls["upload"][0]
        assert up["filename"].endswith("_Correction_Report.pdf")
        assert up["bytes"] > 0


# ---------------------------------------------------------------- mapping

class TestStudentMapping:
    def test_no_submission_id_is_skipped_not_guessed(self, google):
        out = attach(_result(submission_id=None))
        assert out.status == ra.STATUS_SKIPPED
        assert google.calls["attach"] == []
        assert google.calls["upload"] == []

    def test_two_students_never_cross(self, google):
        attach()
        google.state["attachments"] = []
        row_b = _result(id="result-2", submission_id="submission-B",
                        student_id="user-B", student_name="Student B")
        out_b = attach(row_b)
        assert out_b.status == ra.STATUS_VERIFIED
        assert google.calls["attach"][-1]["submission_id"] == "submission-B"
        assert ra.get_state("result-1")["submission_id"] == "submission-A"
        assert ra.get_state("result-2")["submission_id"] == "submission-B"


# ---------------------------------------------------------- google refusals

class TestGoogleRefusals:
    def test_not_modifiable_is_reported_honestly(self, google):
        google.state["can_modify"] = False
        out = attach()
        assert out.status == ra.STATUS_BLOCKED
        assert google.calls["upload"] == [], "must not upload if it cannot attach"
        assert "created" in out.message.lower()

    def test_permission_denied_is_a_failure(self, google):
        google.state["attach_fail"] = 403
        out = attach()
        # A refusal is classified specifically (§20): this is a Classroom
        # attachment failure, NOT a failed correction and NOT a Drive problem.
        assert out.status == ra.STATUS_ATTACH_FAILED
        assert "403" in out.message
        assert ra.get_state("result-1")["status"] == ra.STATUS_ATTACH_FAILED
        # §6: a failed attach must NOT delete the file - it is what "Retry
        # attachment" reuses.
        assert google.calls.get("deleted", []) == []

    def test_drive_upload_failure_is_a_failure(self, google):
        google.state["upload_fail"] = True
        out = attach()
        # Classified separately from every other failure (§20).
        assert out.status == ra.STATUS_UPLOAD_FAILED
        assert "drive" in out.message.lower()
        assert google.calls["attach"] == []


class TestVerificationIsNotAssumed:
    def test_200_without_the_file_is_not_success(self, google):
        google.state["ignore"] = True     # Classroom "succeeds" silently
        out = attach()
        # Its own status: the Drive file exists and Classroom returned 200, but
        # the attachment is not really there (§18).
        assert out.status == ra.STATUS_VERIFY_FAILED
        assert "not present" in out.message
        assert ra.get_state("result-1")["status"] == ra.STATUS_VERIFY_FAILED
        assert ra.get_state("result-1")["attached"] is False

    def test_verify_attached_reads_the_submission_back(self, google):
        google.state["attachments"] = [{"driveFile": {"id": "drive-1"}}]
        check = ra.verify_attached("tok", COURSE, CW, "submission-A", "drive-1")
        assert check["verified"] is True
        assert "drive-1" in check["attachment_ids"]

    def test_verify_reports_missing_file(self, google):
        assert ra.verify_attached("tok", COURSE, CW, "submission-A",
                                  "nope")["verified"] is False


# ----------------------------------------------------------- idempotency

class TestNoDuplicateReports:
    def test_second_attach_does_not_upload_again(self, google):
        row = _result()
        first = attach(row)
        second = attach(row)
        assert first.status == second.status == ra.STATUS_VERIFIED
        assert len(google.calls["upload"]) == 1, "no duplicate report"
        assert len(google.calls["attach"]) == 1, "no re-attach"

    def test_recorrection_updates_the_same_drive_file(self, google):
        """While our file still exists, a re-mark PATCHes it - never re-uploads.

        The first attempt deliberately leaves the file behind (Classroom
        refused), which is exactly the state a retry finds.
        """
        google.state["attach_fail"] = 403
        attach(_result())
        assert ra.get_state("result-1")["drive_file_id"] == "drive-1"
        uploads_after_failure = len(google.calls["upload"])

        google.state["attach_fail"] = None
        out = attach(_result(earned=80))
        assert out.status == ra.STATUS_VERIFIED
        assert google.calls["update"], "same Drive file updated in place"
        assert len(google.calls["upload"]) == uploads_after_failure, \
            "no second Drive file"
        # The attached file is RETAINED (Classroom references it) - never
        # deleted, or the student sees "the document couldn't be opened".
        assert google.calls.get("deleted", []) == []
        assert ra.get_state("result-1")["drive_file_id"] == "drive-1"

    def test_recorrection_reuses_rather_than_accumulates(self, google):
        """A re-mark updates the SAME retained Drive file - never a clone."""
        attach(_result())
        # The attached file is kept, so Drive has exactly one report.
        assert google.calls.get("deleted", []) == []
        uploads_before = len(google.calls["upload"])

        out = attach(_result(earned=80))
        assert out.status == ra.STATUS_VERIFIED
        assert len(google.calls["upload"]) == uploads_before, \
            "the existing file is reused, not duplicated"
        assert google.calls["update"], "the same file was updated in place"
        assert ra.get_state("result-1")["drive_file_id"] == "drive-1"

    def test_repeated_identical_attach_is_a_no_op(self, google):
        """Re-attaching an UNCHANGED correction never re-uploads (§14).

        Drive is transport, so after a confirmed+cleaned attachment there is no
        file left to reuse. Without this, every "Attach" click would create
        another copy of a report the student already has.
        """
        row = _result()
        attach(row)
        second = attach(row)
        assert second.status == ra.STATUS_VERIFIED
        assert len(google.calls["upload"]) == 1, "no duplicate report"
        assert len(google.calls["attach"]) == 1, "no re-attach"

    def test_a_different_detail_is_a_different_report(self, google):
        """The fingerprint covers what the PDF actually depends on."""
        attach(_result())
        uploads_before = len(google.calls["upload"])
        out = ra.attach_report("tok", _result(), course_id=COURSE,
                               assignment_title="HW", detail="detailed")
        assert out.status == ra.STATUS_VERIFIED
        # A different detail updates the retained file rather than uploading a
        # second copy of the report.
        assert len(google.calls["upload"]) == uploads_before
        assert google.calls["update"]

    def test_a_remarked_result_never_reuses_the_old_pdf_bytes(self, google,
                                                               monkeypatch):
        """The retry cache is keyed to the correction, not just to the row."""
        google.state["attach_fail"] = 403
        attach(_result())                      # attempt 1: cached bytes kept
        assert len(google.calls["upload"]) == 1
        google.state["attach_fail"] = None
        out = attach(_result(earned=80))        # attempt 2: different marks
        assert out.status == ra.STATUS_VERIFIED
        assert ra.get_state("result-1")["report_hash"] == \
            ra._report_fingerprint(_result(earned=80), detail="standard")

    def test_never_sends_the_nonexistent_remove_field(self, google):
        """ModifyAttachmentsRequest has NO removeAttachments field.

        A live call proved Google rejects it with 400 "Unknown name
        removeAttachments: Cannot find field", so the request must never
        contain it.
        """
        google.state["attachments"] = [{"driveFile": {"id": "drive-OLD"}}]
        google.state["upload_id"] = "drive-NEW"
        out = attach()
        assert out.status == ra.STATUS_VERIFIED
        assert google.calls["attach"][0]["add"] == ["drive-NEW"]
        # Duplicate suppression is "skip if already there", never "remove".
        assert "remove_drive_file_ids" not in google.calls["attach"][0]["body_kwargs"]

    def test_foreign_attachment_does_not_block_our_report(self, google):
        """The student's own work stays; our report is added alongside it."""
        google.state["attachments"] = [{"driveFile": {"id": "drive-OLD"}}]
        google.state["upload_id"] = "drive-NEW"
        out = attach()
        assert out.status == ra.STATUS_VERIFIED
        ids = {a["driveFile"]["id"] for a in google.state["attachments"]}
        assert ids == {"drive-OLD", "drive-NEW"}


# ---------------------------------------------------------------- retry

class TestRetryWithoutRemarking:
    def test_retry_after_failure_recovers_without_re_marking(self, google,
                                                             monkeypatch):
        renders = []
        _real_render = ra.render_report_pdf
        monkeypatch.setattr(ra, "render_report_pdf",
                            lambda *a, **k: (renders.append(1),
                                             _real_render(*a, **k))[1])
        row = _result()
        google.state["attach_fail"] = 403
        first = attach(row)
        assert first.status == ra.STATUS_ATTACH_FAILED
        # §7: the PDF and the Drive file are both produced once. A retry must
        # re-use them, not regenerate them.
        uploads = len(google.calls["upload"])
        assert len(renders) == 1

        google.state["attach_fail"] = None
        second = attach(row)
        assert second.status == ra.STATUS_VERIFIED
        assert ra.get_state("result-1")["attempts"] == 2
        assert len(google.calls["upload"]) == uploads, "no second report"
        assert len(renders) == 1, "retry must not regenerate the PDF"

    def test_attempt_counter_increments(self, google):
        google.state["upload_fail"] = True
        attach()
        attach()
        assert ra.get_state("result-1")["attempts"] == 2

    def test_failed_state_is_retryable_set(self, google):
        """EVERY failure status must offer the teacher a retry (§7, §20)."""
        google.state["upload_fail"] = True          # Drive upload refused
        attach()
        drive_state = ra.get_state("result-1")
        assert drive_state["status"] == ra.STATUS_UPLOAD_FAILED
        assert drive_state["retryable"] is True

        google.state["upload_fail"] = False
        google.state["attach_fail"] = 403           # Classroom refused
        attach(_result(id="result-2", submission_id="submission-B",
                       student_id="user-B"))
        attach_state = ra.get_state("result-2")
        assert attach_state["status"] == ra.STATUS_ATTACH_FAILED
        assert attach_state["retryable"] is True

        google.state["attach_fail"] = None
        google.state["ignore"] = True               # 200 but not attached
        attach(_result(id="result-3", submission_id="submission-C",
                       student_id="user-C"))
        verify_state = ra.get_state("result-3")
        assert verify_state["status"] == ra.STATUS_VERIFY_FAILED
        assert verify_state["retryable"] is True

    def test_testing_only_refuses_before_any_google_write(self, google,
                                                         monkeypatch):
        """Regression: the local stop-the-line gate was reported as "Google
        Drive upload failed: TESTING ONLY: …", retried three times and parked
        as a Drive problem (production job 35f84682, 2026-10-12).

        Nothing may be uploaded, and the recorded reason must be the gate.
        """
        from app.config import get_config
        monkeypatch.setattr(get_config().testing, "only", True)
        out = attach()
        assert out.status == ra.STATUS_TESTING_BLOCKED
        assert out.status not in ra.FAILED_STATUSES
        assert google.calls["upload"] == []          # nothing left the machine
        assert google.calls["attach"] == []
        assert "Testing only" in out.message
        # NOT the misleading "Google Drive upload failed" wording.
        assert "upload failed" not in out.message.lower()
        assert "nothing was uploaded" in out.message.lower()
        saved = ra.get_state("result-1")
        assert saved["status"] == ra.STATUS_TESTING_BLOCKED
        assert saved["error"].startswith(ra.ERROR_TESTING_ONLY)
        # Not an attached report, so the teacher still sees work to do.
        assert saved["attached"] is False

    def test_a_confirmed_attachment_is_not_retryable(self, google):
        """§12: a success with pending housekeeping must not offer a re-attach
        that would create a second report for the same correction."""
        state = ra.get_state("result-1")
        assert state["status"] == ra.STATUS_PENDING
        attach()
        done = ra.get_state("result-1")
        assert done["attached"] is True
        assert done["retryable"] is False
        # The file is RETAINED for the Classroom attachment, not cleaned.
        assert done["drive_cleanup"] == "retained"


# ------------------------------------------------------------ state query

class TestStateForUi:
    def test_unknown_result_defaults_to_pending(self):
        assert ra.get_state("nope")["status"] == ra.STATUS_PENDING

    def test_list_states_filters_by_coursework(self, google):
        attach()
        # Distinct submission: results has UNIQUE(coursework_id,
        # submission_id) - one Classroom submission yields one correction.
        attach(_result(id="result-2", submission_id="submission-B",
                       student_id="user-B"))
        assert len(ra.list_states(CW)) == 2
        assert len(ra.list_states("other")) == 0

class TestScopeUpgradeMessage:
    def test_invalid_scope_is_not_reported_as_expiry(self, monkeypatch,
                                                     tmp_path):
        """A new scope (drive.file) invalidates the old refresh token.

        Google answers "invalid_scope", which must NOT be shown as "expired or
        revoked" - the teacher has to reconnect to GRANT a permission, which
        is a different instruction.
        """
        import app.classroom.auth as auth
        from app.config import get_secrets

        secrets = get_secrets()
        secrets.set_oauth_refresh_token("refresh-x")
        monkeypatch.setattr(
            secrets, "oauth_client_config",
            lambda: {"installed": {"client_id": "cid",
                                   "client_secret": "secret",
                                   "token_uri": "https://oauth2/x"}})

        class _Creds:
            token = None

            def refresh(self, request):
                raise Exception(
                    "invalid_scope: Bad Request {'error': 'invalid_scope'}")

        # Credentials/Request are imported INSIDE get_access_token(), so the
        # patch has to target the module they are imported from.
        import google.oauth2.credentials as _gocreds
        monkeypatch.setattr(_gocreds, "Credentials", lambda **kw: _Creds())
        with pytest.raises(RuntimeError) as exc:
            auth.get_access_token()
        message = str(exc.value).lower()
        assert "drive.file" in message
        assert "reconnect" in message
        assert "expired" not in message, "must not mislabel a scope change"


# ------------------------------------------------- correct-student mapping

class TestIdentityComesFromTheSubmission:
    """The StudentSubmission's own userId is the only authority on WHO.

    A result row can be sparse (a manual re-run, a job created before roster
    sync) or simply stale. Attaching to the submission is already keyed on
    submission_id, but the Drive *share* and the filename also need an
    identity - and neither may ever drift onto a different student.
    """

    def test_submission_user_id_wins_over_a_stale_local_student_id(self, google):
        out = attach(_result(student_id="user-OLD", student_name="Wrong Person"))
        assert out.status == ra.STATUS_VERIFIED
        # Shared with whoever actually owns the submission, never the stale
        # local id - this is the catastrophic "Student A -> Student B" case.
        assert google.calls["share"][0]["email"] == "user-A@school.edu"
        assert ra.get_state("result-1")["student_id"] == "user-A"
        # The stored name belonged to the OLD id, so it must not survive:
        # the profile is unresolvable here, so a neutral fallback is correct.
        name = out.to_dict()["report_filename"]
        assert "Wrong Person" not in name
        assert name == "Student_HW_Correction_Report.pdf"

    def test_sparse_row_still_resolves_the_real_student(self, google, monkeypatch):
        # No student_id / student_name locally at all: the submission supplies
        # the id, the profile supplies the name.
        monkeypatch.setattr(gc, "get_user_profile", lambda t, uid: {
            "name": {"fullName": "Real Student"},
            "emailAddress": f"{uid}@school.edu"})
        out = attach(_result(student_id=None, student_name=None))
        assert out.status == ra.STATUS_VERIFIED
        outd = out.to_dict()
        assert outd["report_filename"] == "Real Student_HW_Correction_Report.pdf"
        assert ra.get_state("result-1")["student_id"] == "user-A"
        assert google.calls["share"][0]["email"] == "user-A@school.edu"

    def test_a_stored_name_is_not_reused_for_a_different_student(self, google,
                                                                 monkeypatch):
        """A name that belonged to the previous id must not be reused."""
        monkeypatch.setattr(gc, "get_user_profile", lambda t, uid: {
            "name": {"fullName": "True Owner"},
            "emailAddress": f"{uid}@school.edu"})
        out = attach(_result(student_id="user-STALE", student_name="Old Kid"))
        assert out.to_dict()["report_filename"].startswith("True Owner_")

    def test_name_lookup_failure_never_blocks_the_attachment(self, google,
                                                            monkeypatch):
        def _boom(token, uid):
            raise RuntimeError("profile API down")

        monkeypatch.setattr(gc, "get_user_profile", _boom)
        out = attach(_result(student_id="user-STALE", student_name=None))
        assert out.status == ra.STATUS_VERIFIED
        # Falls back to a sanitised placeholder instead of failing the attach.
        assert out.to_dict()["report_filename"].endswith("_HW_Correction_Report.pdf")

    def test_submission_without_user_id_still_attaches(self, google, monkeypatch):
        """No userId anywhere: attachment proceeds, share is simply skipped."""
        def _read(t, c, cw, s):
            return {"id": s, "userId": None, "associatedWithDeveloper": True,
                    "assignmentSubmission": {"attachments": G.state["attachments"]}}

        def _attach(t, c, cw, s, **kw):
            for fid in kw.get("add_drive_file_ids") or []:
                G.state["attachments"].append({"driveFile": {"id": fid}})
            return _read(t, c, cw, s)

        monkeypatch.setattr(gc, "get_student_submission", _read)
        monkeypatch.setattr(gc, "modify_submission_attachments", _attach)
        out = attach(_result(student_id=None, student_name=None))
        assert out.status == ra.STATUS_VERIFIED
        assert google.calls["share"] == []


# --------------------------------------------- upload/attach is PROVEN, not assumed

class TestReportUploadIsProvenNotAssumed:
    """Locks down the bug where the automation said "attached" but no report
    was actually uploaded to Drive or attached to the student's submission."""

    def test_an_invalid_pdf_is_never_uploaded(self, google, monkeypatch):
        monkeypatch.setattr(ra, "render_report_pdf",
                            lambda result, detail="standard": b"not a pdf")
        out = attach()
        assert out.status == ra.STATUS_PDF_INVALID
        assert ra.ERROR_PDF in out.message
        assert google.calls["upload"] == [], "a broken PDF must not reach Drive"
        assert google.calls["attach"] == [], "and must not be attached"
        assert ra.get_state("result-1")["attached"] is False
        assert ra.get_state("result-1")["retryable"] is True

    def test_a_drive_file_that_fails_verification_is_not_attached(
            self, google, monkeypatch):
        # Drive reports a non-PDF mime type, so the upload cannot be trusted.
        monkeypatch.setattr(
            gc, "get_drive_file",
            lambda token, fid: {"id": fid, "trashed": False,
                                "mimeType": "text/plain", "size": "10",
                                "owners": [{"emailAddress": ACCOUNT}],
                                "appProperties": {}})
        out = attach()
        assert out.status == ra.STATUS_DRIVE_VERIFY_FAILED
        assert ra.ERROR_DRIVE_VERIFY in out.message
        assert google.calls["attach"] == [], "unverified upload must not attach"
        assert ra.get_state("result-1")["attached"] is False

    def test_legacy_verified_row_with_no_file_id_is_repaired_not_trusted(
            self, google):
        """The real production rows read VERIFIED/CLEANED with drive_file_id
        NULL - the report was deleted. The old code returned VERIFIED here (a
        false success). It must now actually upload and attach."""
        row = _persist(_result())
        google.state["attachments"] = []
        ra._save({
            "result_id": row["id"], "course_id": COURSE,
            "coursework_id": CW, "submission_id": row["submission_id"],
            "student_id": row["student_id"], "status": ra.STATUS_VERIFIED,
            "attempts": 1, "lifecycle": "CLEANED", "drive_file_id": None,
            "report_filename": "cloned.pdf",
            "report_hash": ra._report_fingerprint(row, detail="standard"),
        })
        out = ra.attach_report("tok", row, course_id=COURSE,
                               assignment_title="HW")
        assert out.status == ra.STATUS_VERIFIED
        assert google.calls["upload"], "the missing report was actually uploaded"
        assert google.calls["attach"], "and attached to the submission"
        assert ra.get_state("result-1")["drive_file_id"]
        assert ra.get_state("result-1")["attached"] is True

    def test_a_verified_row_without_a_file_id_is_not_reported_as_attached(
            self, google):
        """The legacy CLEANED rows are status VERIFIED but have no file id.
        They must NOT read as attached - that is the false tick that hid the
        missing report from the teacher."""
        row = _persist(_result())
        ra._save({
            "result_id": row["id"], "course_id": COURSE, "coursework_id": CW,
            "submission_id": row["submission_id"], "student_id": row["student_id"],
            "status": ra.STATUS_VERIFIED, "attempts": 1, "lifecycle": "CLEANED",
            "drive_file_id": None, "report_filename": "gone.pdf",
            "report_hash": ra._report_fingerprint(row, detail="standard"),
        })
        state = ra.get_state("result-1")
        assert state["status"] == ra.STATUS_VERIFIED
        assert state["attached"] is False, "no file id means NOT attached"

    def test_an_orphaned_report_on_the_submission_is_recovered_not_duplicated(
            self, google, monkeypatch):
        """The row lost its file id, but OUR report is still on the submission:
        adopt it (matched by appProperties) instead of uploading a second copy."""
        row = _persist(_result())
        google.state["attachments"] = [{"driveFile": {"id": "drive-OURS"}}]
        monkeypatch.setattr(
            gc, "get_drive_file",
            lambda token, fid: {"id": fid, "trashed": False,
                                "mimeType": "application/pdf",
                                "size": str(len(PDF_BYTES)), "name": "r.pdf",
                                "owners": [{"emailAddress": ACCOUNT}],
                                "appProperties": {"mathcorrectTemp": "true",
                                                  "mathcorrectResultId": "result-1"}})
        ra._save({
            "result_id": row["id"], "course_id": COURSE, "coursework_id": CW,
            "submission_id": row["submission_id"], "student_id": row["student_id"],
            "status": ra.STATUS_VERIFIED, "attempts": 1, "lifecycle": "CLEANED",
            "drive_file_id": None, "report_filename": "r.pdf",
            "report_hash": ra._report_fingerprint(row, detail="standard"),
        })
        out = ra.attach_report("tok", row, course_id=COURSE,
                               assignment_title="HW")
        assert out.status == ra.STATUS_VERIFIED
        assert google.calls["upload"] == [], "the existing file must be reused"
        assert ra.get_state("result-1")["drive_file_id"] == "drive-OURS"
        assert ra.get_state("result-1")["attached"] is True
        assert int(ra.get_state("result-1")["attempts"]) == 2
