"""Regression tests for the AUTOMATIC CORRECTION error lifecycle.

Every test here reproduces a real defect (or an explicitly required guarantee)
from the "stale mid-flight error" report:

* a report upload whose RESPONSE was lost was recorded as ``Upload failed`` and
  parked forever, even though Google had accepted the upload and the report was
  already on the student's submission — and the returned work was already done;
* a retry re-ran the same impossible step and left the original error active;
* a stale job kept describing work that a newer attempt had already finished;
* there was no way to clear resolved errors or refresh a job's real state.

The Google Classroom side is fully mocked: no live API call is made, no report
is uploaded, no grade is written and no student work is returned.
"""

from __future__ import annotations

import io
import json
import time
from types import SimpleNamespace

import pytest

from app.automation import operations, runner as runner_mod, states, store

LEGACY_GRADE = "grade:8"


# --------------------------------------------------------------- fixtures

def _fake_pdf() -> bytes:
    from reportlab.pdfgen import canvas
    buf = io.BytesIO()
    c = canvas.Canvas(buf)
    c.drawString(72, 720, "School")
    c.showPage()
    c.save()
    return buf.getvalue()


def seed_work(coursework_id: str = "w1", submission_id: str = "s1",
              student_id: str = "u1", name: str = "Ada",
              title: str = "Algebra Lesson 1") -> None:
    from app import db
    db.execute("INSERT OR IGNORE INTO courses(id, name, fetched_at) VALUES(?,?,?)",
               ("c1", "Maths", time.time()))
    db.execute(
        "INSERT OR IGNORE INTO coursework(id, course_id, title, fetched_at, "
        "allowlisted) VALUES(?,?,?,?,1)",
        (coursework_id, "c1", title, time.time()))
    db.execute(
        """
        INSERT INTO classroom_submissions(
            submission_id, course_id, coursework_id, student_id, student_name,
            current_state, update_time, attachments_json, data_quality,
            last_synced_at, first_seen_at, sync_version)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,1)
        ON CONFLICT(submission_id) DO UPDATE SET current_state = excluded.current_state
        """,
        (submission_id, "c1", coursework_id, student_id, name, "TURNED_IN",
         "2026-09-01T09:00:00Z", json.dumps([]), "VERIFIED",
         time.time(), time.time()))


def persist_result(result_id: str = "r1", *, submission_id: str = "s1",
                   coursework_id: str = "w1", student_id: str = "u1",
                   name: str = "Ada", earned: int = 67,
                   available: int = 75) -> str:
    from app import db
    job_id = f"cj-{result_id}"
    db.execute(
        "INSERT OR IGNORE INTO correction_jobs(id, submission_id, coursework_id, "
        "course_id, student_id, status, created_at, updated_at) VALUES(?,?,?,?,?,?,?,?)",
        (job_id, submission_id, coursework_id, "c1", student_id, "COMPLETED",
         time.time(), time.time()))
    percentage = round(earned / available * 100, 2) if available else 0.0
    db.execute(
        """
        INSERT OR REPLACE INTO results(
            id, job_id, coursework_id, submission_id, student_id, student_name,
            earned, available, percentage, provisional_grade, result_json,
            review_required, manual_override, created_at)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,0,0,?)
        """,
        (result_id, job_id, coursework_id, submission_id, student_id, name,
         earned, available, percentage, "8",
         json.dumps({"questions": [], "overall_notes": {}}), time.time()),
        )
    return result_id


def make_job(*, submission_id: str = "s1", version: str = "v1",
             coursework_id: str = "w1", student_id: str = "u1",
             name: str = "Ada", result_id: str = "r1",
             title: str = "Algebra Lesson 1", grade: str = "8") -> dict:
    seed_work(coursework_id=coursework_id, submission_id=submission_id,
              student_id=student_id, name=name, title=title)
    persist_result(result_id, submission_id=submission_id,
                   coursework_id=coursework_id, student_id=student_id, name=name)
    job = store.create_job(
        course_id="c1", coursework_id=coursework_id, assignment_title=title,
        student_id=student_id, student_name=name, submission_id=submission_id,
        submission_version=version)
    assert job is not None
    return job


def expected_report_name(name: str = "Ada",
                         title: str = "Algebra Lesson 1") -> str:
    from app.classroom.report_attachment import build_report_filename
    return build_report_filename(name, title)


# ------------------------------------------------- mocked Google Classroom

class FakeClassroom:
    """A mutable stand-in for the Google Classroom/Drive side.

    ``state``/``assigned_grade``/``attachments`` are what a read returns, and
    ``unreadable`` simulates Classroom being unreachable (the read raises), so
    the difference between "the report is not there" and "we cannot tell" can
    be tested honestly.
    """

    def __init__(self, *, state: str = "TURNED_IN", assigned_grade=None,
                 draft_grade=None, attachments=(), unreadable: bool = False):
        self.submission = {
            "state": state,
            "assignedGrade": assigned_grade,
            "draftGrade": draft_grade,
            "assignmentSubmission": {
                "attachments": [{"driveFile": {"id": fid, "title": title}}
                                for fid, title in attachments]},
        }
        self.unreadable = unreadable
        self.reads = 0
        #: Drive metadata per file id: ``{file_id: appProperties}``. This is
        #: how a report is proven to be OURS for a given result (the same
        #: marker the real uploader stamps on every file it creates).
        self.drive: dict[str, dict] = {}
        self.drive_reads = 0

    def own(self, file_id: str, result_id: str) -> None:
        """Mark a Drive file as this app's report for ``result_id``."""
        from app.classroom import client as gc
        self.drive[file_id] = {gc.APP_PROP_TEMP: "true",
                               gc.APP_PROP_RESULT: str(result_id)}

    def drive_meta(self, token, file_id):
        self.drive_reads += 1
        return {"id": file_id,
                "appProperties": dict(self.drive.get(file_id) or {})}

    # -- reads ---------------------------------------------------------
    def read(self, job):
        self.reads += 1
        if self.unreadable:
            raise RuntimeError("Classroom is unreachable (simulated)")
        return self.submission

    # -- helpers used by the fakes ------------------------------------
    def attach_report(self, title: str, file_id: str = "drive-1",
                      result_id: str | None = None) -> None:
        self.submission["assignmentSubmission"]["attachments"].append(
            {"driveFile": {"id": file_id, "title": title}})
        if result_id:
            self.own(file_id, result_id)

    def titles(self) -> list[str]:
        return [a["driveFile"]["title"]
                for a in self.submission["assignmentSubmission"]["attachments"]]

    def file_ids(self) -> list[str]:
        return [a["driveFile"]["id"]
                for a in self.submission["assignmentSubmission"]["attachments"]]


class FakeAttach:
    """``attach_report`` stand-in driven by a per-call plan.

    Each plan entry is ``(status, mark_attached)``: ``mark_attached`` is how a
    LOST RESPONSE is simulated — Google really did accept the upload (the file
    is on the submission) while the call reports a failure.
    """

    def __init__(self, classroom: FakeClassroom, plan=None, name: str | None = None):
        self.classroom = classroom
        self.plan = list(plan or [])
        self.name = name or expected_report_name()
        self.calls: list[dict] = []
        self._uploads = 0

    def __call__(self, token, result, **kw):
        from app.classroom import report_attachment as ra
        status, mark = (self.plan.pop(0) if self.plan
                        else (ra.STATUS_VERIFIED, True))
        self.calls.append({"status": status, "report_id": result.get("id")})
        file_id = "drive-1"
        if mark:
            self._uploads += 1
            file_id = f"drive-{self._uploads}"
            # The upload stamp: the file Google now holds is OURS for this
            # result, even when the failure is that our response was lost.
            self.classroom.attach_report(self.name, file_id,
                                         result_id=result.get("id"))
        return SimpleNamespace(
            status=status, message=f"{status}: simulated",
            extra={"drive_file_id": file_id if mark else None,
                   "report_filename": self.name},
            to_dict=lambda: {"status": status})


class FakePublish:
    """``managed.publish`` stand-in that records the ops it was asked to run."""

    def __init__(self, classroom: FakeClassroom, plan=None):
        self.classroom = classroom
        self.plan = list(plan or [])
        self.calls: list[str] = []

    def __call__(self, coursework_id, course_id, ops, **kw):
        from app import db
        from app.classroom import managed
        op = ops[0]
        op_type = op["type"]
        self.calls.append(op_type)
        status = self.plan.pop(0) if self.plan else managed.STATUS_PUBLISHED
        grade = op.get("grade")
        if status == managed.STATUS_PUBLISHED:
            if op_type == "return":
                self.classroom.submission["state"] = "RETURNED"
            else:
                self.classroom.submission["assignedGrade"] = grade
                self.classroom.submission["draftGrade"] = grade
            db.execute(
                "INSERT INTO classroom_publish_ops(coursework_id, course_id, "
                "submission_id, op_type, payload_json, status, http_status, "
                "created_at, finished_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (coursework_id, course_id, op.get("submission_id"), op_type,
                 json.dumps({"grade": grade}) if grade is not None else None,
                 "published", 200, time.time(), time.time()))
        return {"results": [{"status": status, "http_status": 200,
                             "message": status,
                             "error_code": None}]}


def make_runner(classroom: FakeClassroom, *, attach=None, publish=None,
                review_mode: bool = False, cleanup_ok: bool = True):
    from app.config import AutomationConfig
    from app import db

    state = {"enqueues": 0, "renders": 0}

    class FakeService:
        def enqueue_classroom_submission(self, **kw):
            state["enqueues"] += 1
            return {"id": "cj1"}

        def get_correction_status(self, job_id):
            return {"id": job_id, "status": "COMPLETED", "result_id": "r1"}

        def get_result(self, result_id):
            row = db.query_one("SELECT * FROM results WHERE id = ?", (result_id,))
            if not row:
                return None
            data = dict(row)
            for key in ("result_json", "verification_json"):
                if isinstance(data.get(key), str):
                    try:
                        data[key] = json.loads(data[key])
                    except ValueError:
                        pass
            return data

    def fake_render(result, **kw):
        state["renders"] += 1
        return _fake_pdf()

    r = runner_mod.WorkflowRunner(
        correction_service=FakeService(),
        attach=attach or FakeAttach(classroom),
        publish=publish or FakePublish(classroom),
        render_pdf=fake_render,
        pdf_validate=lambda content, **kw: {"ok": True, "reason": None,
                                           "size": len(content), "pages": 1},
        drive_verify=lambda *a, **k: {"ok": True},
        drive_read=classroom.drive_meta,
        submission_reader=classroom.read,
        account_email="t@school.org",
        cleanup=lambda *a, **k: ({"ok": True} if cleanup_ok
                                 else {"ok": False, "skipped": "cleanup_failed"}),
        token_fn=lambda: "fake-token",
        config=AutomationConfig(review_mode=review_mode))
    r._counts = state
    return r


def op_states(job_id: str) -> dict:
    return operations.summary(job_id)


def op(job_id: str, key: str) -> dict:
    return operations.get(job_id, key)


# =====================================================================
# 1. A report upload that GENUINELY fails stays failed
# =====================================================================

def test_a_genuine_upload_failure_stays_failed():
    from app.classroom import report_attachment as ra
    job = make_job()
    classroom = FakeClassroom()
    attach = FakeAttach(classroom, plan=[(ra.STATUS_UPLOAD_FAILED, False)])
    r = make_runner(classroom, attach=attach, publish=FakePublish(classroom))

    final = r.advance(job["id"])
    assert final["status"] == states.REPORT_UPLOAD_FAILED
    report = op(job["id"], operations.REPORT)
    assert report["state"] == operations.FAILED
    assert report["error"] and not report["resolved"]
    # Nothing was fabricated: the report is NOT on the submission.
    assert expected_report_name() not in classroom.titles()


# =====================================================================
# 2. Upload succeeds but the client sees a TIMEOUT
# =====================================================================

def test_upload_succeeded_but_the_client_timed_out_is_recovered():
    """The upload really reached Classroom; our response was lost.

    Recording that as a permanent ``Upload failed`` is the bug this whole
    change exists to remove: the report IS on the submission, so the operation
    is verified as succeeded (recovered) and the pipeline continues.
    """
    from app.classroom import report_attachment as ra
    job = make_job()
    classroom = FakeClassroom()
    attach = FakeAttach(classroom, plan=[(ra.STATUS_UPLOAD_FAILED, True)])
    r = make_runner(classroom, attach=attach, publish=FakePublish(classroom))

    final = r.advance(job["id"])
    assert final["status"] == states.COMPLETE
    report = op(job["id"], operations.REPORT)
    assert report["state"] == operations.SUCCEEDED
    assert report["recovered"] == 1 and report["verified"] == 1
    assert report["error"] is None            # never recorded as a failure
    assert final["drive_file_id"] == "drive-1"


# =====================================================================
# 3. The application fails while PROCESSING the response
# =====================================================================

def test_crash_while_processing_the_response_is_resolved_on_the_next_attempt():
    from app.classroom import report_attachment as ra
    job = make_job()
    classroom = FakeClassroom()
    attach = FakeAttach(classroom, plan=[(ra.STATUS_DRIVE_VERIFY_FAILED, True)])
    r = make_runner(classroom, attach=attach, publish=FakePublish(classroom))

    first = r.advance(job["id"])
    # The verification of OUR response failed, but the report is on the
    # submission, so it is still not reported as a failure.
    assert first["status"] == states.COMPLETE
    assert op(job["id"], operations.REPORT)["state"] == operations.SUCCEEDED


# =====================================================================
# 4. A retry succeeds and the previous error becomes RESOLVED
# =====================================================================

def test_retry_success_resolves_the_previous_error():
    from app.classroom import report_attachment as ra
    job = make_job()
    classroom = FakeClassroom()
    attach = FakeAttach(classroom, plan=[(ra.STATUS_UPLOAD_FAILED, False),
                                         (ra.STATUS_VERIFIED, True)])
    r = make_runner(classroom, attach=attach, publish=FakePublish(classroom))

    first = r.advance(job["id"])
    assert first["status"] == states.REPORT_UPLOAD_FAILED
    assert op(job["id"], operations.REPORT)["error"]

    r.retry(job["id"])
    final = r.advance(job["id"])
    assert final["status"] == states.COMPLETE
    report = op(job["id"], operations.REPORT)
    assert report["state"] == operations.SUCCEEDED
    assert report["resolved"] == 1 and report["resolved_at"]
    # The history is KEPT (audit) but no longer active.
    assert report["error_code"] == "report_upload_failed"
    assert report["error"] is None or report["resolved"] == 1
    assert final["error"] is None and final["error_code"] is None


# =====================================================================
# 5. The report already exists when a retry begins
# =====================================================================

def test_retry_does_not_upload_again_when_the_report_is_already_there():
    from app.classroom import report_attachment as ra
    job = make_job()
    classroom = FakeClassroom(attachments=[("drive-9", expected_report_name())])
    classroom.own("drive-9", "r1")            # our own report for THIS result
    attach = FakeAttach(classroom, plan=[(ra.STATUS_VERIFIED, True)])
    r = make_runner(classroom, attach=attach, publish=FakePublish(classroom))

    # An interrupted attempt: the ledger says the outcome is unknown.
    operations.begin(job["id"], operations.REPORT)
    operations.abandon_in_flight(job["id"])
    store.set_status(job["id"], states.REPORT_VALIDATED,
                     result_id="r1", grade="8")

    final = r.advance(job["id"])
    assert final["status"] == states.COMPLETE
    assert attach.calls == []                 # nothing uploaded again
    report = op(job["id"], operations.REPORT)
    assert report["state"] == operations.SUCCEEDED and report["recovered"] == 1
    assert classroom.titles().count(expected_report_name()) == 1


# =====================================================================
# 6. Report upload succeeds while the GRADE fails
# =====================================================================

def test_report_succeeds_while_the_grade_fails_is_tracked_separately():
    from app.classroom import managed
    from app.classroom import report_attachment as ra
    job = make_job()
    classroom = FakeClassroom()
    attach = FakeAttach(classroom, plan=[(ra.STATUS_VERIFIED, True)])
    publish = FakePublish(classroom, plan=[managed.STATUS_FAILED])
    r = make_runner(classroom, attach=attach, publish=publish)

    final = r.advance(job["id"])
    assert final["status"] == states.GRADE_FAILED
    states_now = op_states(job["id"])
    assert states_now[operations.REPORT] == operations.SUCCEEDED
    assert states_now[operations.GRADE] == operations.FAILED
    assert states_now[operations.RETURN] == operations.PENDING
    # The report is genuinely on the submission and stays reported as sent.
    assert expected_report_name() in classroom.titles()


# =====================================================================
# 7. Report + grade succeed while RETURNING fails
# =====================================================================

def test_return_failure_is_its_own_operation_state():
    from app.classroom import managed
    from app.classroom import report_attachment as ra
    job = make_job()
    classroom = FakeClassroom()
    attach = FakeAttach(classroom, plan=[(ra.STATUS_VERIFIED, True)])
    publish = FakePublish(classroom, plan=[managed.STATUS_PUBLISHED,
                                           managed.STATUS_FAILED])
    r = make_runner(classroom, attach=attach, publish=publish)

    final = r.advance(job["id"])
    assert final["status"] == states.RETURN_FAILED
    states_now = op_states(job["id"])
    assert states_now[operations.REPORT] == operations.SUCCEEDED
    assert states_now[operations.GRADE] == operations.SUCCEEDED
    assert states_now[operations.RETURN] == operations.FAILED
    # Retrying targets the RETURN only: no second report, no second grade.
    attach_calls = len(attach.calls)
    r.retry(job["id"])
    assert store.get(job["id"])["status"] == states.GRADE_VERIFIED
    assert store.get(job["id"])["status"] != states.REPORT_VALIDATED
    assert len(attach.calls) == attach_calls


# =====================================================================
# 8. Returning succeeds after a retry and the old return error clears
# =====================================================================

def test_return_retry_clears_the_return_error():
    from app.classroom import managed
    from app.classroom import report_attachment as ra
    job = make_job()
    classroom = FakeClassroom()
    attach = FakeAttach(classroom, plan=[(ra.STATUS_VERIFIED, True)])
    publish = FakePublish(classroom, plan=[managed.STATUS_PUBLISHED,
                                           managed.STATUS_FAILED,
                                           managed.STATUS_PUBLISHED])
    r = make_runner(classroom, attach=attach, publish=publish)

    assert r.advance(job["id"])["status"] == states.RETURN_FAILED
    r.retry(job["id"])
    final = r.advance(job["id"])
    assert final["status"] == states.COMPLETE
    returned = op(job["id"], operations.RETURN)
    assert returned["state"] == operations.SUCCEEDED
    assert returned["resolved"] == 1
    assert final["error"] is None


# =====================================================================
# 9. A stale worker response cannot overwrite a newer success
# =====================================================================

def test_a_stale_worker_cannot_overwrite_a_newer_successful_state():
    """The old worker's claim is gone, so its write matches no row."""
    job = make_job()
    classroom = FakeClassroom()
    r = make_runner(classroom)
    store.set_status(job["id"], states.REPORT_VALIDATED, result_id="r1",
                     grade="8")
    claimed = store.claim(job["id"], worker="stale-worker")
    assert claimed is not None

    # A retry (or reconciliation) moves the job on and drops that claim…
    store.release(job["id"])
    store.set_status(job["id"], states.COMPLETE, detail="finished by a newer run")
    # …so the stale worker's guarded write is REFUSED instead of overwriting it.
    with store.claim_guard("stale-worker"):
        result = store.set_status(job["id"], states.REPORT_UPLOAD_FAILED,
                                  error="stale", error_code="report_upload_failed")
    assert result["status"] == states.COMPLETE
    assert store.get(job["id"])["status"] == states.COMPLETE
    assert store.get(job["id"])["error"] is None


# =====================================================================
# 10. Two concurrent retries
# =====================================================================

def test_two_retries_do_not_restart_the_step_twice():
    job = make_job()
    classroom = FakeClassroom()
    r = make_runner(classroom)
    store.set_status(job["id"], states.REPORT_UPLOAD_FAILED, result_id="r1",
                     grade="8", error="boom", error_code="report_upload_failed")

    r.retry(job["id"])                       # first request wins
    assert store.get(job["id"])["status"] == states.REPORT_VALIDATED
    with pytest.raises(runner_mod.StepFailure):
        r.retry(job["id"])                   # second is refused, not restarted
    assert store.get(job["id"])["status"] == states.REPORT_VALIDATED

    # A retry that loses the race to ANOTHER retry is reported as the real
    # state instead of a fake failure.
    store.set_status(job["id"], states.REPORT_UPLOAD_FAILED, result_id="r1",
                     grade="8", error="boom", error_code="report_upload_failed")
    assert store.reset_for_retry(job["id"], expected_status="REPORT_UPLOAD_FAILED",
                                 resume_state=states.REPORT_VALIDATED) is True
    assert store.reset_for_retry(job["id"], expected_status="REPORT_UPLOAD_FAILED",
                                 resume_state=states.REPORT_VALIDATED) is False


# =====================================================================
# 11. The application restarts during an upload
# =====================================================================

def test_restart_during_an_upload_marks_the_operation_unverified():
    job = make_job()
    operations.begin(job["id"], operations.REPORT)
    operations.begin(job["id"], operations.GRADE)
    operations.succeed(job["id"], operations.GRADE, recovered=True)

    summary = store.recover_in_flight()
    assert summary["operations_interrupted"] == 1
    assert op(job["id"], operations.REPORT)["state"] == operations.NEEDS_VERIFICATION
    # A finished operation is NOT rewritten by the restart.
    assert op(job["id"], operations.GRADE)["state"] == operations.SUCCEEDED


# =====================================================================
# 12. An ambiguous outcome stays uncertain
# =====================================================================

def test_unverifiable_outcome_is_uncertain_not_failed():
    from app.classroom import report_attachment as ra
    job = make_job()
    classroom = FakeClassroom(unreadable=True)
    attach = FakeAttach(classroom, plan=[(ra.STATUS_UPLOAD_FAILED, False)])
    r = make_runner(classroom, attach=attach, publish=FakePublish(classroom))

    final = r.advance(job["id"])
    assert final["status"] == states.ATTENTION_REQUIRED
    assert final["error_code"] == "needs_verification"
    report = op(job["id"], operations.REPORT)
    assert report["state"] == operations.NEEDS_VERIFICATION
    assert report["error_code"] == "report_outcome_unknown"


# =====================================================================
# 13. Clearing resolved errors leaves unresolved ones visible
# =====================================================================

def test_clearing_resolved_errors_keeps_unresolved_ones():
    resolved_job = make_job(result_id="r1")
    open_job = make_job(submission_id="s2", result_id="r2", version="v1")

    operations.begin(resolved_job["id"], operations.REPORT)
    operations.fail(resolved_job["id"], operations.REPORT, message="timeout",
                    code="report_upload_failed")
    operations.succeed(resolved_job["id"], operations.REPORT, recovered=True)

    operations.begin(open_job["id"], operations.GRADE)
    operations.fail(open_job["id"], operations.GRADE, message="rejected",
                    code="grade_failed")

    cleared = store.clear_resolved_errors()
    assert cleared == 1
    # The resolved MESSAGE is gone, but the operation record (the idempotency
    # guard) is preserved and still reads SUCCEEDED.
    recovered = op(resolved_job["id"], operations.REPORT)
    assert recovered is not None and recovered["state"] == operations.SUCCEEDED
    assert recovered["error"] is None
    # The genuine failure is untouched.
    still_open = op(open_job["id"], operations.GRADE)
    assert still_open["state"] == operations.FAILED
    assert still_open["error"] == "rejected"


# =====================================================================
# 14. Clearing history preserves active jobs and recovery records
# =====================================================================

def test_clearing_history_preserves_active_jobs_and_report_records():
    active = make_job(result_id="r1")
    done = make_job(submission_id="s2", result_id="r2", version="v1")
    store.set_status(active["id"], states.CORRECTING)
    store.set_status(done["id"], states.COMPLETE, result_id="r2")
    operations.succeed(done["id"], operations.REPORT, verified=True,
                       note="attached")

    hidden = store.clear_recent_jobs()
    assert hidden == 1
    assert store.get(active["id"])["hidden_at"] is None
    assert store.get(done["id"])["hidden_at"] is not None
    # The report attachment record (what prevents a duplicate upload) survives.
    assert op(done["id"], operations.REPORT)["state"] == operations.SUCCEEDED
    assert store.get(done["id"])["result_id"] == "r2"


# =====================================================================
# 15. Refreshing shows the correct persisted state
# =====================================================================

def test_reconcile_marks_a_false_upload_failure_as_complete():
    """The reported scenario: the dashboard said "Upload failed" while the
    report was attached and the work had been graded and returned."""
    job = make_job()
    classroom = FakeClassroom(
        state="RETURNED", assigned_grade=8,
        attachments=[("drive-9", expected_report_name())])
    classroom.own("drive-9", "r1")            # the report really is ours
    r = make_runner(classroom)
    store.set_status(job["id"], states.REPORT_UPLOAD_FAILED, result_id="r1",
                     grade="8", drive_file_id=None,
                     error="REPORT_RECONCILE_FAILED: could not replace",
                     error_code="report_reconcile_failed")
    operations.begin(job["id"], operations.REPORT)
    operations.fail(job["id"], operations.REPORT,
                    message="REPORT_RECONCILE_FAILED: could not replace",
                    code="report_reconcile_failed")

    refreshed = r.reconcile(job["id"])
    assert refreshed["status"] == states.COMPLETE
    assert refreshed["error"] is None and refreshed["error_code"] is None
    assert op_states(job["id"]) == {
        operations.REPORT: operations.SUCCEEDED,
        operations.GRADE: operations.SUCCEEDED,
        operations.RETURN: operations.SUCCEEDED}
    assert op(job["id"], operations.REPORT)["resolved"] == 1


def test_reconcile_keeps_a_genuine_failure_visible():
    job = make_job()
    classroom = FakeClassroom(state="TURNED_IN")
    r = make_runner(classroom)
    store.set_status(job["id"], states.REPORT_UPLOAD_FAILED, result_id="r1",
                     grade="8", error="Drive upload failed",
                     error_code="report_upload_failed")
    operations.begin(job["id"], operations.REPORT)
    operations.fail(job["id"], operations.REPORT, message="Drive upload failed",
                    code="report_upload_failed")

    refreshed = r.reconcile(job["id"])
    assert refreshed["status"] == states.REPORT_UPLOAD_FAILED
    assert op(job["id"], operations.REPORT)["state"] == operations.FAILED
    assert op(job["id"], operations.REPORT)["resolved"] == 0


def test_reconcile_supersedes_a_job_a_newer_attempt_already_finished():
    older = make_job(version="v1")
    newer = make_job(version="v2")
    classroom = FakeClassroom(state="RETURNED", assigned_grade=8)
    r = make_runner(classroom)
    store.set_status(older["id"], states.ATTENTION_REQUIRED, result_id="r1",
                     grade="8", error="no grade was submitted",
                     error_code="requires_review")
    operations.begin(older["id"], operations.GRADE)
    operations.fail(older["id"], operations.GRADE, message="not submitted",
                    code="requires_review")
    store.set_status(newer["id"], states.COMPLETE, result_id="r1", grade="8")

    refreshed = r.reconcile(older["id"])
    assert refreshed["status"] == states.SUPERSEDED
    assert refreshed["error"] is None
    grade_op = op(older["id"], operations.GRADE)
    assert grade_op["state"] == operations.CANCELLED
    assert grade_op["resolved"] == 1
    # Nothing was deleted: the job, its result and its history are all intact.
    assert store.get(older["id"])["result_id"] == "r1"
    assert grade_op["error"] == "not submitted"


def test_completing_a_job_supersedes_older_parked_jobs():
    older = make_job(version="v1")
    newer = make_job(version="v2")
    classroom = FakeClassroom()
    attach = FakeAttach(classroom, plan=[(("VERIFIED"), True)])
    publish = FakePublish(classroom)
    r = make_runner(classroom, attach=attach, publish=publish)
    store.set_status(older["id"], states.RETURN_FAILED, result_id="r1",
                     grade="8", error="return failed", error_code="return_not_verified")

    final = r.advance(newer["id"])
    assert final["status"] == states.COMPLETE
    refreshed_older = store.get(older["id"])
    assert refreshed_older["status"] == states.SUPERSEDED
    assert refreshed_older["error"] is None


def test_reconcile_reports_an_unreadable_classroom_as_uncertain():
    job = make_job()
    classroom = FakeClassroom(unreadable=True)
    r = make_runner(classroom)
    store.set_status(job["id"], states.REPORT_UPLOAD_FAILED, result_id="r1",
                     grade="8", error="boom", error_code="report_upload_failed")

    refreshed = r.reconcile(job["id"])
    assert refreshed["status"] == states.ATTENTION_REQUIRED
    assert refreshed["error_code"] == "needs_verification"
    assert op(job["id"], operations.REPORT)["state"] == operations.NEEDS_VERIFICATION


# =====================================================================
# 16. Retrying a failed upload does not re-run correction or the render
# =====================================================================

def test_retrying_an_upload_never_re_runs_correction_or_the_grade_write():
    from app.classroom import report_attachment as ra
    job = make_job()
    classroom = FakeClassroom()
    attach = FakeAttach(classroom, plan=[(ra.STATUS_UPLOAD_FAILED, False),
                                         (ra.STATUS_VERIFIED, True)])
    publish = FakePublish(classroom)
    r = make_runner(classroom, attach=attach, publish=publish)

    assert r.advance(job["id"])["status"] == states.REPORT_UPLOAD_FAILED
    assert r._counts["enqueues"] == 1
    assert r._counts["renders"] == 1

    r.retry(job["id"])
    assert r.advance(job["id"])["status"] == states.COMPLETE
    assert r._counts["enqueues"] == 1          # the AI was never re-run
    assert r._counts["renders"] == 1           # only the report step re-rendered
    assert publish.calls == ["assign_grade", "return"]


# =====================================================================
# 17. Repeated polling/reconciliation is idempotent
# =====================================================================

def test_repeated_reconcile_does_not_duplicate_or_revert_anything():
    job = make_job()
    classroom = FakeClassroom(
        state="RETURNED", assigned_grade=8,
        attachments=[("drive-9", expected_report_name())])
    classroom.own("drive-9", "r1")
    r = make_runner(classroom)
    store.set_status(job["id"], states.REPORT_UPLOAD_FAILED, result_id="r1",
                     grade="8", error="boom", error_code="report_upload_failed")

    first = r.reconcile(job["id"])
    assert first["status"] == states.COMPLETE
    snapshot = (first["status"], first["drive_file_id"], first["grade"],
                sorted(classroom.titles()), len(classroom.titles()))
    for _ in range(3):
        again = r.reconcile(job["id"])
        assert (again["status"], again["drive_file_id"], again["grade"],
                sorted(classroom.titles()), len(classroom.titles())) == snapshot
    assert classroom.reads > 0
    assert op(job["id"], operations.REPORT)["recovered"] == 1


def test_operation_payload_reports_active_and_resolved_errors_separately():
    from app.automation import routes
    job = make_job()
    store.set_status(job["id"], states.GRADE_FAILED, result_id="r1", grade="8",
                     error="Classroom rejected the grade",
                     error_code="grade_failed")
    operations.begin(job["id"], operations.REPORT)
    operations.fail(job["id"], operations.REPORT, message="upload timed out",
                    code="report_upload_failed")
    operations.succeed(job["id"], operations.REPORT, recovered=True)
    operations.begin(job["id"], operations.GRADE)
    operations.fail(job["id"], operations.GRADE,
                    message="Classroom rejected the grade", code="grade_failed")

    public = routes._job_public(store.get(job["id"]))
    assert [e["op_key"] for e in public["active_errors"]] == ["grade"]
    assert [e["op_key"] for e in public["resolved_errors"]] == ["report"]
    assert public["has_active_error"] is True
    assert public["operation_states"] == {
        operations.REPORT: operations.SUCCEEDED,
        operations.GRADE: operations.FAILED,
        operations.RETURN: operations.PENDING}
    report = next(o for o in public["operations"]
                  if o["op_key"] == operations.REPORT)
    assert report["error_resolved"] is True
    assert report["recovered"] is True


# =====================================================================
# teacher controls
# =====================================================================

def test_dismiss_hides_a_resolved_error_but_never_a_genuine_failure():
    job = make_job()
    operations.begin(job["id"], operations.REPORT)
    operations.fail(job["id"], operations.REPORT, message="timeout",
                    code="report_upload_failed")
    operations.succeed(job["id"], operations.REPORT, recovered=True)

    updated, refusal = operations.dismiss(job["id"], operations.REPORT)
    assert refusal is None
    assert updated["dismissed_at"]
    assert updated["state"] == operations.SUCCEEDED      # state untouched

    operations.begin(job["id"], operations.GRADE)
    operations.fail(job["id"], operations.GRADE, message="rejected",
                    code="grade_failed")
    unchanged, refusal = operations.dismiss(job["id"], operations.GRADE)
    assert refusal is not None
    assert unchanged["state"] == operations.FAILED       # still a failure
    assert unchanged["dismissed_at"] is None


def test_reconciling_a_superseded_or_cancelled_job_changes_nothing():
    job = make_job()
    store.set_status(job["id"], states.CANCELLED)
    classroom = FakeClassroom(unreadable=True)
    r = make_runner(classroom)
    assert r.reconcile(job["id"])["status"] == states.CANCELLED
    assert classroom.reads == 0


def test_a_superseded_job_is_never_retried():
    job = make_job()
    classroom = FakeClassroom()
    r = make_runner(classroom)
    store.set_status(job["id"], states.SUPERSEDED)
    with pytest.raises(runner_mod.StepFailure) as exc:
        r.retry(job["id"])
    assert "newer attempt" in str(exc.value)


def test_reconcile_stale_is_bounded_and_reports_its_outcome():
    jobs = [make_job(submission_id=f"s{i}", result_id=f"r{i}", version="v1")
            for i in range(1, 4)]
    classroom = FakeClassroom(state="RETURNED", assigned_grade=8,
                              attachments=[("drive-9", expected_report_name())])
    r = make_runner(classroom)
    for job in jobs:
        store.set_status(job["id"], states.REPORT_UPLOAD_FAILED,
                         result_id=job["id"].replace("job", "r") or None,
                         grade="8", error="boom",
                         error_code="report_upload_failed")
    summary = r.reconcile_stale(limit=2)
    assert summary["checked"] <= 2
    assert summary["failed"] == 0


# =====================================================================
# The HTTP surface the Automatic Correction page actually calls
# =====================================================================

@pytest.fixture
def client(fresh_state):
    from fastapi.testclient import TestClient
    from app.main import app
    with TestClient(app) as test_client:
        yield test_client


def test_page_payload_shows_operation_states_not_a_stale_error(client):
    """What the page renders after a refresh: the report's error is history,
    the grade is the only ACTIVE error, and each stage is separately stated."""
    job = make_job()
    store.set_status(job["id"], states.GRADE_FAILED, result_id="r1", grade="8",
                     error="Classroom rejected the grade",
                     error_code="grade_failed")
    operations.begin(job["id"], operations.REPORT)
    operations.fail(job["id"], operations.REPORT, message="upload timed out",
                    code="report_upload_failed")
    operations.succeed(job["id"], operations.REPORT, recovered=True)
    operations.begin(job["id"], operations.GRADE)
    operations.fail(job["id"], operations.GRADE,
                    message="Classroom rejected the grade", code="grade_failed")

    body = client.get(f"/api/automation/jobs/{job['id']}").json()
    assert body["operation_states"] == {
        operations.REPORT: operations.SUCCEEDED,
        operations.GRADE: operations.FAILED,
        operations.RETURN: operations.PENDING}
    assert [e["op_key"] for e in body["active_errors"]] == [operations.GRADE]
    assert [e["op_key"] for e in body["resolved_errors"]] == [operations.REPORT]
    # The stage cards carry the per-operation state, so "report" cannot read
    # as failed just because the job is parked on a grade failure.
    publication = body["publication"]
    assert publication["report"] == "sent"
    assert publication["report_sent"] is True
    assert publication["report_state"] == operations.SUCCEEDED
    # The list endpoint agrees with the detail endpoint (no drift between them).
    listed = next(j for j in client.get("/api/automation/jobs").json()["jobs"]
                  if j["id"] == job["id"])
    assert listed["operation_states"] == body["operation_states"]


def test_clear_resolved_errors_endpoint_needs_confirmation(client):
    job = make_job()
    operations.begin(job["id"], operations.REPORT)
    operations.fail(job["id"], operations.REPORT, message="timeout",
                    code="report_upload_failed")
    operations.succeed(job["id"], operations.REPORT, recovered=True)

    assert client.get("/api/automation/errors/clearable-count").json()["count"] == 1
    refused = client.post("/api/automation/errors/clear", json={})
    assert refused.status_code == 400
    assert refused.json()["code"] == "confirmation_required"
    assert operations.get(job["id"], operations.REPORT)["error"]

    ok = client.post("/api/automation/errors/clear", json={"confirm": True})
    assert ok.status_code == 200
    body = ok.json()
    assert body["cleared"] == 1 and body["operations_preserved"] is True
    assert body["remaining"] == 0
    # The message is gone; the RECORD (the duplicate-upload guard) is not.
    report = operations.get(job["id"], operations.REPORT)
    assert report["error"] is None
    assert report["state"] == operations.SUCCEEDED


def test_dismiss_endpoint_refuses_to_hide_a_genuine_failure(client):
    job = make_job()
    operations.begin(job["id"], operations.GRADE)
    operations.fail(job["id"], operations.GRADE, message="rejected",
                    code="grade_failed")

    resp = client.post(
        f"/api/automation/jobs/{job['id']}/errors/{operations.GRADE}/dismiss")
    assert resp.status_code == 409
    assert resp.json()["code"] == "not_dismissible"
    assert operations.get(job["id"], operations.GRADE)["state"] == operations.FAILED


def test_dismiss_endpoint_hides_a_resolved_error(client):
    job = make_job()
    operations.begin(job["id"], operations.REPORT)
    operations.fail(job["id"], operations.REPORT, message="timeout",
                    code="report_upload_failed")
    operations.succeed(job["id"], operations.REPORT, recovered=True)

    resp = client.post(
        f"/api/automation/jobs/{job['id']}/errors/{operations.REPORT}/dismiss")
    assert resp.status_code == 200
    body = resp.json()
    assert body["resolved_errors"] == []
    assert body["operation_states"][operations.REPORT] == operations.SUCCEEDED


def test_clear_history_endpoint_keeps_active_jobs_and_records(client):
    active = make_job(result_id="r1")
    done = make_job(submission_id="s2", result_id="r2", version="v1")
    store.set_status(active["id"], states.CORRECTING)
    store.set_status(done["id"], states.COMPLETE, result_id="r2")
    operations.succeed(done["id"], operations.REPORT, verified=True)

    assert client.get("/api/automation/jobs/clearable-count").json()["count"] == 1
    assert client.post("/api/automation/jobs/clear", json={}).status_code == 400
    body = client.post("/api/automation/jobs/clear", json={"confirm": True}).json()
    assert body["hidden"] == 1 and body["active_jobs"] >= 1

    visible = {j["id"] for j in client.get("/api/automation/jobs").json()["jobs"]}
    assert active["id"] in visible
    assert done["id"] not in visible
    # Archiving is not deleting: the record that prevents a duplicate upload of
    # the same report is still there.
    assert operations.get(done["id"], operations.REPORT)["state"] == \
        operations.SUCCEEDED
    assert client.get(f"/api/automation/jobs/{done['id']}").status_code == 200
