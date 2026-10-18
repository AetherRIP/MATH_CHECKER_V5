"""Automated correction workflow tests (spec §1-§52).

Everything external is faked (Google Classroom, the AI correction engine), so
the workflow LOGIC is what is under test: detection, versioning, duplicate
prevention, the resumable state machine, deterministic grading and PDF
validation.
"""

from __future__ import annotations

import json
import time
from types import SimpleNamespace

import pytest

from app.automation import detection, grading, pdf_validation, runner as runner_mod
from app.automation import states, store


# ------------------------------------------------------------------ helpers

def seed_coursework(coursework_id: str = "w1", course_id: str = "c1",
                    title: str = "Algebra Lesson 1") -> None:
    from app import db
    db.execute("INSERT OR IGNORE INTO courses(id, name, fetched_at) VALUES(?,?,?)",
               (course_id, "Maths", time.time()))
    db.execute(
        "INSERT OR IGNORE INTO coursework(id, course_id, title, fetched_at, allowlisted) "
        "VALUES(?,?,?,?,1)", (coursework_id, course_id, title, time.time()))


def seed_tracking(submission_id: str = "s1", *, user: str = "u1",
                  state: str = "TURNED_IN", update_time: str = "2026-09-01T09:00:00Z",
                  attachment: bool = True, coursework_id: str = "w1",
                  course_id: str = "c1", name: str = "Ada Lovelace") -> None:
    from app import db
    attachments = json.dumps(
        [{"kind": "drive", "id": "f1", "title": "hw.pdf"}] if attachment else [])
    db.execute(
        """
        INSERT INTO classroom_submissions(
            submission_id, course_id, coursework_id, student_id, student_name,
            current_state, update_time, attachments_json, data_quality,
            last_synced_at, first_seen_at, sync_version)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,1)
        ON CONFLICT(submission_id) DO UPDATE SET
            current_state = excluded.current_state,
            update_time = excluded.update_time,
            attachments_json = excluded.attachments_json,
            last_synced_at = excluded.last_synced_at
        """,
        (submission_id, course_id, coursework_id, user, name, state,
         update_time, attachments, "VERIFIED", time.time(), time.time()))


def persist_result(result_id: str = "r1", *, earned: int = 67, available: int = 75,
                   coursework_id: str = "w1", submission_id: str = "s1",
                   student_id: str = "u1", review_required: int = 0) -> str:
    from app import db
    job_id = f"cj-{result_id}"
    db.execute(
        "INSERT OR IGNORE INTO correction_jobs(id, submission_id, coursework_id, "
        "course_id, student_id, status, created_at, updated_at) "
        "VALUES(?,?,?,?,?,?,?,?)",
        (job_id, submission_id, coursework_id, "c1", student_id, "COMPLETED",
         time.time(), time.time()))
    percentage = round(earned / available * 100, 2) if available else 0.0
    db.execute(
        """
        INSERT OR REPLACE INTO results(
            id, job_id, coursework_id, submission_id, student_id, student_name,
            earned, available, percentage, provisional_grade, result_json,
            review_required, manual_override, created_at)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,0,?)
        """,
        (result_id, job_id, coursework_id, submission_id, student_id, "Ada",
         earned, available, percentage, "4",
         json.dumps({"questions": [], "overall_notes": {}}),
         review_required, time.time()))
    return result_id


# --------------------------------------------------------------- detection

def test_eligible_submission_creates_one_job():
    seed_coursework()
    seed_tracking()
    summary = detection.detect_assignment("c1", "w1", assignment_title="Algebra")
    assert summary["queued_count"] == 1
    job = store.get(summary["queued"][0])
    assert job["status"] == states.NEW
    assert job["submission_id"] == "s1"
    assert job["student_id"] == "u1"
    assert job["submission_version"]


def test_duplicate_polling_creates_no_second_job():
    seed_coursework()
    seed_tracking()
    first = detection.detect_assignment("c1", "w1")
    second = detection.detect_assignment("c1", "w1")
    assert first["queued_count"] == 1
    assert second["queued_count"] == 0
    assert len(store.list_jobs(coursework_id="w1")) == 1


def test_not_submitted_and_attachmentless_are_ignored():
    seed_coursework()
    seed_tracking("s1", state="CREATED", attachment=False)
    seed_tracking("s2", state="TURNED_IN", attachment=False, user="u2")
    seed_tracking("s3", state="RECLAIMED_BY_STUDENT", user="u3")
    summary = detection.detect_assignment("c1", "w1")
    assert summary["queued_count"] == 0


def test_resubmission_creates_new_version_job_and_keeps_history():
    seed_coursework()
    seed_tracking(update_time="2026-09-01T09:00:00Z")
    assert detection.detect_assignment("c1", "w1")["queued_count"] == 1
    # Student resubmits: Classroom advances updateTime.
    seed_tracking(update_time="2026-09-02T10:00:00Z")
    assert detection.detect_assignment("c1", "w1")["queued_count"] == 1
    versions = detection.known_versions("w1", "s1")
    assert len(versions) == 2
    assert len(set(versions)) == 2          # v1 history preserved, distinct


def test_returned_submission_is_never_requeued():
    """Returning the work is OUR action — it must not look like a new
    submission and trigger a duplicate correction."""
    seed_coursework()
    seed_tracking("s1", state="RETURNED")
    assert detection.detect_assignment("c1", "w1")["queued_count"] == 0


def test_version_is_stable_when_nothing_changes():
    row = {"update_time": "2026-09-01T09:00:00Z", "raw_state": "TURNED_IN",
           "attachments": [{"kind": "drive", "id": "f1"}]}
    assert detection.submission_version(row) == detection.submission_version(dict(row))
    changed = {**row, "attachments": [{"kind": "drive", "id": "f2"}]}
    assert detection.submission_version(row) != detection.submission_version(changed)


# ------------------------------------------------------------- grading (§18-§21)

def test_grade_is_deterministic_one_to_nine():
    gr = grading.compute_grade(67, 75)
    assert gr.percentage == pytest.approx(89.33, abs=0.01)
    assert gr.grade == "8"           # 89.33% falls in the 80-90 band
    assert gr.marks_obtained == 67 and gr.maximum_marks == 75
    # Pure lookup: same inputs, same answer, every time.
    assert grading.compute_grade(67, 75).grade == gr.grade


def test_grade_uses_assignment_boundaries_when_present():
    store.set_grade_boundaries("w1", [("1", 0.0), ("9", 50.0)])
    gr = grading.compute_grade(60, 100, coursework_id="w1")
    assert gr.boundary_source == "assignment"
    assert gr.grade == "9"
    assert grading.compute_grade(10, 100, coursework_id="w1").grade == "1"


def test_invalid_marks_never_produce_a_grade():
    with pytest.raises(grading.GradeError) as exc:
        grading.compute_grade(80, 75)
    assert exc.value.code == "marks_exceed_maximum"
    with pytest.raises(grading.GradeError):
        grading.compute_grade(None, 75)
    with pytest.raises(grading.GradeError):
        grading.compute_grade(10, 0)


def test_invalid_boundaries_never_produce_a_grade():
    with pytest.raises(grading.GradeError) as exc:
        grading.compute_grade(50, 100, boundaries=[("A", 0.0), ("B", 50.0)])
    assert exc.value.code == "boundaries_invalid"
    with pytest.raises(grading.GradeError):
        grading.compute_grade(50, 100, boundaries=[])


# --------------------------------------------------------- PDF validation (§25-§27)

def _fake_pdf(text: str = "Math Correct Grade 8") -> bytes:
    """A minimal, genuinely parseable PDF (one page, correct header/trailer)."""
    from reportlab.pdfgen import canvas
    import io
    buf = io.BytesIO()
    c = canvas.Canvas(buf)
    c.drawString(72, 720, text)
    c.showPage()
    c.save()
    return buf.getvalue()


def test_valid_pdf_passes_validation():
    out = pdf_validation.validate_report_pdf(_fake_pdf(), expected_text=["Math Correct"])
    assert out["ok"] is True
    assert out["pages"] >= 1
    assert out["size"] > 0


def test_empty_and_truncated_and_non_pdf_are_rejected():
    assert pdf_validation.validate_report_pdf(b"")["ok"] is False
    assert pdf_validation.validate_report_pdf(b"not a pdf at all")["ok"] is False
    good = _fake_pdf()
    truncated = good[:len(good) // 2]     # no trailer
    assert pdf_validation.validate_report_pdf(truncated)["ok"] is False


def test_missing_expected_content_is_rejected():
    out = pdf_validation.validate_report_pdf(
        _fake_pdf("Some other report"), expected_text=["Grade 8"])
    assert out["ok"] is False
    assert "expected content" in (out["reason"] or "")


def test_drive_report_verification(monkeypatch):
    from app.classroom import client as gc

    def fake_get(token, file_id):
        return {"id": file_id, "mimeType": "application/pdf", "size": "1234",
                "name": "R.pdf", "trashed": False}

    monkeypatch.setattr(gc, "get_drive_file", fake_get)
    assert pdf_validation.verify_drive_report("t", "d1")["ok"] is True

    def wrong_mime(token, file_id):
        return {"id": file_id, "mimeType": "text/plain", "size": "10"}

    monkeypatch.setattr(gc, "get_drive_file", wrong_mime)
    assert pdf_validation.verify_drive_report("t", "d1")["ok"] is False

    def missing(token, file_id):
        return None

    monkeypatch.setattr(gc, "get_drive_file", missing)
    assert pdf_validation.verify_drive_report("t", "d1")["ok"] is False


# -------------------------------------------------- workflow runner (§1, §38)

class FakeAttach:
    """Stands in for classroom.report_attachment.attach_report."""

    def __init__(self, statuses=None):
        self.calls = 0
        # Every call's keyword arguments, so a test can prove WHICH report
        # profile the automation actually attached.
        self.kwargs: list[dict] = []
        self._statuses = list(statuses or [])

    def __call__(self, token, result, **kw):
        self.calls += 1
        self.kwargs.append(dict(kw))
        from app.classroom import report_attachment as ra
        status = self._statuses.pop(0) if self._statuses else ra.STATUS_VERIFIED
        return SimpleNamespace(
            status=status,
            message={
                ra.STATUS_VERIFIED: "attached",
                ra.STATUS_UPLOAD_FAILED: "upload failed",
                ra.STATUS_BLOCKED: "blocked by google",
            }.get(status, "failed"),
            extra={"drive_file_id": "drive-1",
                   "report_filename": "Ada_Algebra_Correction_Report.pdf"},
            to_dict=lambda: {"status": status})


class FakePublish:
    """Stands in for classroom.managed.publish."""

    def __init__(self, outcomes=None):
        self.calls = []
        self._outcomes = list(outcomes or [])

    def __call__(self, coursework_id, course_id, operations, **kw):
        self.calls.append(operations[0]["type"])
        status = self._outcomes.pop(0) if self._outcomes else "published"
        return {"results": [{"status": status, "http_status": 200,
                              "message": status}]}


def make_runner(*, attach=None, publish=None, grade=8, submission_state="RETURNED",
                draft_grade=None, config=None):
    """A WorkflowRunner whose external systems are all faked."""
    from app.config import AutomationConfig as _AC
    # The historical suite locks the FULLY AUTOMATIC workflow; review mode
    # (default-on safety gate) is covered separately in test_safety_gates.py.
    if config is None and not isinstance(config, _AC):
        config = None
    if config is None:
        config = _AC(review_mode=False)
    from app.engine import service as service_mod

    state = {"enqueues": 0, "renders": 0, "reads": 0}

    class FakeService:
        def enqueue_classroom_submission(self, **kw):
            state["enqueues"] += 1
            return {"id": "cj1"}

        def get_correction_status(self, job_id):
            return {"id": job_id, "status": "COMPLETED", "result_id": "r1"}

        def get_result(self, result_id):
            from app import db
            row = db.query_one("SELECT * FROM results WHERE id = ?", (result_id,))
            return dict(row) if row else None

    def fake_render(result, **kw):
        state["renders"] += 1
        return _fake_pdf()

    def fake_validator(content, **kw):
        return {"ok": True, "reason": None, "size": len(content), "pages": 1}

    def fake_reader(job):
        state["reads"] += 1
        return {"draftGrade": grade if draft_grade is None else draft_grade,
                "assignedGrade": None, "state": submission_state,
                "updateTime": "2026-09-03T00:00:00Z"}

    r = runner_mod.WorkflowRunner(
        correction_service=FakeService(), attach=attach or FakeAttach(),
        publish=publish or FakePublish(), render_pdf=fake_render,
        pdf_validate=fake_validator, drive_verify=lambda *a, **k: {"ok": True},
        submission_reader=fake_reader, account_email="t@school.org",
        cleanup=lambda *a, **k: {"ok": True}, token_fn=lambda: "fake-token",
        config=config)
    r._state = state
    return r


def _make_job(**over) -> dict:
    """One workflow job. A second call must name another submission (or
    version): a job already exists for s1/v1, and create_job returns None
    rather than a duplicate - which is exactly the §11 guarantee."""
    submission_id = over.pop("submission_id", "s1")
    student_id = over.pop("student_id", "u1")
    seed_coursework()
    seed_tracking(submission_id, user=student_id, name=over.pop("name", "Ada"))
    persist_result(over.pop("result_id", "r1"), submission_id=submission_id,
                   student_id=student_id)
    job = store.create_job(
        course_id="c1", coursework_id="w1", assignment_title="Algebra Lesson 1",
        student_id=student_id, student_name=over.pop("name", "Ada"),
        submission_id=submission_id,
        submission_version=over.pop("version", "v1"))
    return job


def test_happy_path_reaches_complete_with_one_of_each_step():
    job = _make_job()
    r = make_runner()
    final = r.advance(job["id"])
    assert final["status"] == states.COMPLETE
    assert final["grade"] == "8"
    assert final["marks_obtained"] == 67 and final["maximum_marks"] == 75
    assert r._state["enqueues"] == 1          # one correction
    assert r._state["renders"] == 1           # one report render
    # Default automation posts the FINAL grade (assignedGrade) then returns,
    # never leaving a successful job as a draft.
    assert r._publish.calls == ["assign_grade", "return"]
    assert final["completed_at"] is not None


def test_successful_job_is_final_returned_and_complete():
    """Test H: a successful automated correction ends with GRADE=FINAL,
    RETURNED=YES, ATTACHMENT=VERIFIED and STATUS=COMPLETE - never a draft."""
    job = _make_job()
    r = make_runner()
    final = r.advance(job["id"])
    assert final["status"] == states.COMPLETE
    # Final grade (assignedGrade), not a draft; then returned and verified.
    assert r._publish.calls == ["assign_grade", "return"]
    assert "draft_grade" not in r._publish.calls


def test_draft_only_mode_never_posts_a_final_grade_or_returns():
    """Draft-only is the ONLY path that legitimately leaves a successful
    correction unreturned; it must never be marked COMPLETE."""
    from app.config import AutomationConfig
    job = _make_job()
    # review_mode OFF: this locks the DRAFT-ONLY contract specifically —
    # review mode holds the job even earlier (before any Classroom write).
    r = make_runner(config=AutomationConfig(draft_only=True, review_mode=False))
    final = r.advance(job["id"])
    assert final["status"] != states.COMPLETE
    assert r._publish.calls == ["draft_grade"]
    assert "return" not in r._publish.calls


def test_result_flagged_for_review_never_pushes_a_grade():
    job = _make_job()
    persist_result("r1", review_required=1)
    publish = FakePublish()
    r = make_runner(publish=publish)
    final = r.advance(job["id"])
    assert final["status"] == states.ATTENTION_REQUIRED
    assert publish.calls == []                # no grade, no return


def test_upload_failure_resumes_without_rerunning_correction_or_report():
    job = _make_job()
    from app.classroom import report_attachment as ra
    attach = FakeAttach(statuses=[ra.STATUS_UPLOAD_FAILED, ra.STATUS_VERIFIED])
    r = make_runner(attach=attach)
    first = r.advance(job["id"])
    assert first["status"] == states.REPORT_UPLOAD_FAILED
    assert r._state["enqueues"] == 1
    # Retry resumes at the upload step; the AI correction is never redone.
    store.set_status(job["id"], states.REPORT_VALIDATED, next_attempt_at=0)
    final = r.advance(job["id"])
    assert final["status"] == states.COMPLETE
    assert r._state["enqueues"] == 1
    assert attach.calls == 2


def test_grade_failure_does_not_reattach_the_report():
    job = _make_job()
    attach = FakeAttach()
    publish = FakePublish(outcomes=["failed", "published", "published"])
    r = make_runner(attach=attach, publish=publish)
    first = r.advance(job["id"])
    assert first["status"] == states.GRADE_FAILED
    assert attach.calls == 1
    r.retry(job["id"])
    assert store.get(job["id"])["status"] == states.ATTACHMENT_VERIFIED
    final = r.advance(job["id"])
    assert final["status"] == states.COMPLETE
    assert attach.calls == 1                  # report NOT regenerated/re-attached


def test_grade_verification_failure_is_its_own_state():
    job = _make_job()
    r = make_runner(draft_grade=3)            # Classroom stored the wrong grade
    final = r.advance(job["id"])
    assert final["status"] == states.GRADE_VERIFICATION_FAILED


def test_return_verification_failure_is_retryable():
    job = _make_job()
    r = make_runner(submission_state="TURNED_IN")
    final = r.advance(job["id"])
    assert final["status"] == states.RETURN_FAILED


def test_blocked_assignment_is_reported_honestly():
    job = _make_job()
    from app.classroom import report_attachment as ra
    r = make_runner(attach=FakeAttach(statuses=[ra.STATUS_BLOCKED]))
    final = r.advance(job["id"])
    assert final["status"] == states.BLOCKED_NOT_MODIFIABLE
    assert final["status"] != states.COMPLETE


def test_an_invalid_report_pdf_never_uploads_or_attaches():
    job = _make_job()
    from app.classroom import report_attachment as ra
    attach = FakeAttach(statuses=[ra.STATUS_PDF_INVALID])
    r = make_runner(attach=attach)
    final = r.advance(job["id"])
    assert final["status"] == states.REPORT_FAILED
    assert attach.calls == 1


def test_an_unverified_drive_upload_is_not_attached():
    job = _make_job()
    from app.classroom import report_attachment as ra
    attach = FakeAttach(statuses=[ra.STATUS_DRIVE_VERIFY_FAILED])
    r = make_runner(attach=attach)
    final = r.advance(job["id"])
    assert final["status"] == states.REPORT_UPLOAD_FAILED
    assert final["status"] != states.COMPLETE


def test_retry_report_repairs_a_complete_job_without_re_running_anything():
    """The core fix: a job that already finished but whose report was never
    uploaded can be repaired with ONLY the report stage - no AI, no re-render,
    no second grade."""
    from app import db
    job = _make_job()
    r = make_runner()
    assert r.advance(job["id"])["status"] == states.COMPLETE
    enqueues, renders = r._state["enqueues"], r._state["renders"]
    publishes = list(r._publish.calls)
    # Model the real bug: COMPLETE, but no verified Drive file id.
    db.execute("UPDATE automation_jobs SET drive_file_id = NULL WHERE id = ?",
               (job["id"],))
    repair = FakeAttach()
    r._attach = repair
    updated = r.retry_report(job["id"])
    assert updated["status"] == states.COMPLETE
    assert repair.calls == 1
    assert updated["drive_file_id"] == "drive-1"
    assert r._state["enqueues"] == enqueues, "the AI correction must not re-run"
    assert r._state["renders"] == renders, "the report must not be regenerated"
    assert r._publish.calls == publishes, "no second grade may be posted"


def test_retry_report_records_a_failure_instead_of_raising():
    job = _make_job()
    r = make_runner()
    r.advance(job["id"])
    from app.classroom import report_attachment as ra
    r._attach = FakeAttach(statuses=[ra.STATUS_UPLOAD_FAILED])
    updated = r.retry_report(job["id"])
    assert updated["status"] == states.REPORT_UPLOAD_FAILED
    assert updated["status"] != states.COMPLETE


# ------------------------------------------------ concurrency + crash recovery

def test_claim_is_exclusive():
    job = _make_job()
    first = store.claim(job["id"], worker="a")
    assert first is not None
    # A second worker cannot claim the same job while the first holds it.
    from app import db
    db.execute("UPDATE automation_jobs SET claimed_at = ? WHERE id = ?",
               (time.time(), job["id"]))
    assert store.claim(job["id"], worker="b") is None


def test_boot_recovery_releases_claims_without_restarting_from_new():
    job = _make_job()
    assert store.claim(job["id"], worker="dead-worker") is not None
    store.set_status(job["id"], states.REPORT_UPLOAD_FAILED,
                     next_attempt_at=time.time() + 9999)
    from app import db
    db.execute("UPDATE automation_jobs SET claimed_at = ? WHERE id = ?",
               (time.time() - 1000, job["id"]))
    summary = store.recover_in_flight()
    assert summary["claims_released"] >= 1
    recovered = store.get(job["id"])
    # It keeps its exact state (resumes at the failed step), never NEW.
    assert recovered["status"] == states.REPORT_UPLOAD_FAILED
    assert recovered["claimed_by"] is None
    assert recovered["next_attempt_at"] <= time.time()


def test_manual_retry_resumes_from_the_failed_step():
    job = _make_job()
    store.set_status(job["id"], states.ATTACHMENT_FAILED)
    r = make_runner()
    retried = r.retry(job["id"])
    assert retried["status"] == states.REPORT_VALIDATED
    assert states.RESUME_FROM[states.GRADE_FAILED] == states.ATTACHMENT_VERIFIED
    assert states.RESUME_FROM[states.RETURN_FAILED] == states.GRADE_VERIFIED


# ------------------------------------------- permanent failures must PARK
# Regression: these failure states are RUNNABLE, so leaving them due meant the
# scheduler re-ran the same failing step on every tick forever (production log
# recorded 209 "workflow step failed permanently" attempts on ONE job, each a
# real Drive/Classroom round-trip).

def test_permanent_failure_parks_the_job_instead_of_retrying_forever():
    job = _make_job()
    r = make_runner()
    r._record_failure(store.get(job["id"]),
                      RuntimeError("upload endpoint is gone"))
    failed = store.get(job["id"])
    # The honest failure state for the UI (and its Retry button) is kept…
    assert failed["status"] == states.CORRECTION_FAILED
    assert failed["error_code"] == "step_error"
    assert failed["attempts"] == 1
    # …but the job is PARKED: neither the poller nor a claim can take it.
    assert failed["next_attempt_at"] > time.time() + 86400
    assert store.next_runnable() is None
    assert store.claim(job["id"]) is None
    assert r.run_pending() == 0, "a permanent failure must not be re-run"
    # A teacher's retry re-arms it: same state machine, resumed at the step.
    r.retry(job["id"])
    retried = store.get(job["id"])
    assert retried["status"] == states.RESUME_FROM[states.CORRECTION_FAILED]
    assert store.next_runnable()["id"] == job["id"]


def test_parked_job_stays_parked_across_a_restart():
    job = _make_job()
    r = make_runner()
    r._record_failure(store.get(job["id"]), RuntimeError("gone for good"))
    assert store.get(job["id"])["status"] == states.CORRECTION_FAILED
    # Boot recovery must not re-due a job that already gave up.
    store.recover_in_flight()
    assert store.next_runnable() is None


def test_transient_failure_still_schedules_an_automatic_retry():
    job = _make_job()
    r = make_runner()
    r._record_failure(store.get(job["id"]),
                      RuntimeError("connection reset by peer"))
    failed = store.get(job["id"])
    assert failed["status"] == states.CORRECTION_FAILED
    assert failed["error_code"] == "transient_error"
    assert failed["attempts"] == 1
    now_ts = time.time()
    assert now_ts < failed["next_attempt_at"] <= now_ts + 2 * runner_mod.BACKOFF_CAP
    # Once the backoff elapses the scheduler picks it up AGAIN automatically.
    from app import db
    db.execute("UPDATE automation_jobs SET next_attempt_at = ? WHERE id = ?",
               (now_ts, job["id"]))
    assert store.next_runnable()["id"] == job["id"]


# ------------------------------------------------------ scheduler (§5, §6, §7)

def test_run_cycle_checks_enabled_assignments_and_records_timestamps():
    from app.automation.scheduler import AutomationScheduler
    seed_coursework()
    store.set_assignment_enabled("w1", "c1", True)
    synced, detected = [], []

    cfg = SimpleNamespace(enabled=True, effective_interval=lambda: 10)
    sched = AutomationScheduler(
        cfg_fn=lambda: cfg,
        sync_fn=lambda c, w: synced.append((c, w)),
        detect_fn=lambda c, w, t: (detected.append((c, w)),
                                   {"queued_count": 2})[1],
        runner=SimpleNamespace(run_pending=lambda **k: 0))
    summary = sched.run_cycle(now_ts=1000.0)
    assert synced == [("c1", "w1")]
    assert detected == [("c1", "w1")]
    assert summary["found"] == 2
    assert store.get_meta("last_check") == 1000.0
    assert store.get_meta("next_check") == 1000.0 + 10 * 60


def test_disabled_assignment_is_never_monitored():
    from app.automation.scheduler import AutomationScheduler
    seed_coursework()
    # w1 enabled, w2 not present in automation_assignments at all.
    store.set_assignment_enabled("w1", "c1", True)
    seen = []
    cfg = SimpleNamespace(enabled=True, effective_interval=lambda: 5)
    sched = AutomationScheduler(
        cfg_fn=lambda: cfg, sync_fn=lambda c, w: seen.append(w),
        detect_fn=lambda c, w, t: {"queued_count": 0},
        runner=SimpleNamespace(run_pending=lambda **k: 0))
    sched.run_cycle(now_ts=10.0)
    assert seen == ["w1"]


def test_global_disable_stops_polling():
    from app.automation.scheduler import AutomationScheduler
    seed_coursework()
    store.set_assignment_enabled("w1", "c1", True)
    cfg = SimpleNamespace(enabled=False, effective_interval=lambda: 10)
    called = []
    sched = AutomationScheduler(
        cfg_fn=lambda: cfg, runner=SimpleNamespace(run_pending=lambda **k: 0))
    sched.run_cycle = lambda **k: called.append(1)
    sched._tick()
    assert called == []                    # nothing polls while disabled


def test_interval_is_restricted_to_supported_values():
    from app.config import AutomationConfig
    c = AutomationConfig(interval_minutes=7)
    assert c.effective_interval() == 10
    assert AutomationConfig(interval_minutes=30).effective_interval() == 30


# --------------------------------------------------------------- API layer

@pytest.fixture
def client(fresh_state):
    from app.main import app
    from fastapi.testclient import TestClient
    with TestClient(app) as test_client:
        yield test_client


def test_status_endpoint_defaults_off(client):
    data = client.get("/api/automation/status").json()
    assert data["enabled"] is False
    assert data["interval_minutes"] in data["intervals"]
    assert "pipeline" in data


def test_settings_persist_and_survive_reload(client):
    resp = client.post("/api/automation/settings",
                       json={"enabled": True, "interval_minutes": 5}).json()
    assert resp["interval_minutes"] == 5 and resp["enabled"] is True
    # Persisted to config.toml, so a restart keeps it (§4/§45).
    from app.config import get_config
    get_config(reload=True)
    assert get_config().automation.enabled is True
    assert get_config().automation.interval_minutes == 5


def test_default_attached_report_is_the_full_per_question_report():
    """The attached report must explain EVERY question by default."""
    from app.config import AutomationConfig
    assert AutomationConfig().report_detail == "detailed"


def test_status_reports_the_report_detail(client):
    assert client.get("/api/automation/status").json()["report_detail"] == "detailed"


def test_report_detail_setting_roundtrips_and_persists(client):
    resp = client.post("/api/automation/settings",
                       json={"report_detail": "standard"}).json()
    assert resp["report_detail"] == "standard"
    from app.config import get_config
    get_config(reload=True)
    assert get_config().automation.report_detail == "standard"
    # And back to the full report.
    again = client.post("/api/automation/settings",
                        json={"report_detail": "detailed"}).json()
    assert again["report_detail"] == "detailed"
    assert "report_detail" in again["updated"]


def test_report_detail_setting_rejects_unknown_value(client):
    resp = client.post("/api/automation/settings",
                       json={"report_detail": "everything"}).json()
    assert resp["report_detail"] == "detailed"      # unchanged, still the default
    assert "report_detail" not in resp["updated"]


def test_automation_attaches_the_configured_report_detail():
    """The configured profile is what the attach step actually receives."""
    from app.config import AutomationConfig
    job = _make_job()
    attach = FakeAttach()
    r = make_runner(attach=attach,
                    config=AutomationConfig(review_mode=False,
                                            report_detail="detailed"))
    r.advance(job["id"])
    assert attach.calls == 1
    assert attach.kwargs[0]["detail"] == "detailed"


def test_settings_reject_unsupported_interval(client):
    resp = client.post("/api/automation/settings", json={"interval_minutes": 7})
    assert resp.status_code == 400
    assert resp.json()["code"] == "bad_interval"


def test_assignment_selection_roundtrip(client):
    seed_coursework("w1")
    seed_coursework("w2", title="Geometry Lesson 1")
    client.post("/api/automation/assignments/w1", json={"enabled": True})
    data = client.get("/api/automation/assignments").json()
    by_id = {a["coursework_id"]: a for a in data["assignments"]}
    assert by_id["w1"]["enabled"] is True
    assert by_id["w2"]["enabled"] is False      # not auto-enabled
    assert data["enabled_count"] == 1


def test_jobs_and_log_endpoints(client):
    job = _make_job()
    store.log_job("success", "detected", "New submission detected", job_id=job["id"])
    jobs = client.get("/api/automation/jobs").json()["jobs"]
    assert any(j["id"] == job["id"] for j in jobs)
    detail = client.get(f"/api/automation/jobs/{job['id']}").json()
    assert detail["status_label"]
    log = client.get("/api/automation/log").json()["entries"]
    assert any(e["event"] == "detected" for e in log)
    # no credentials ever appear in the activity feed
    assert "AIza" not in json.dumps(log)


def test_job_detail_endpoint_feeds_the_visualizer(client):
    """The click-through drawer + node graph read these exact fields."""
    job = _make_job()
    resp = client.get(f"/api/automation/jobs/{job['id']}")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    # Real stage mapping the graph renders.
    assert body["status"] == states.NEW
    assert "stage_index" in body and "status_label" in body
    assert "result" in body
    # The list the graph is built from carries the same stage fields.
    listing = client.get("/api/automation/jobs?limit=5").json()
    assert all("stage_index" in j and "status_label" in j for j in listing["jobs"])
    # The pipeline the graph places nodes on.
    pipe = client.get("/api/automation/status").json()["pipeline"]
    assert any(p["state"] == states.ATTACHMENT_VERIFIED for p in pipe)


def test_job_detail_exposes_the_real_report_state(client):
    """The UI must read the ACTUAL attachment state, never infer it."""
    job = _make_job()
    body = client.get(f"/api/automation/jobs/{job['id']}").json()
    assert "attachment" in body
    # A brand-new job has no correction and nothing uploaded yet.
    assert body["report_uploaded"] is False
    assert body["has_result"] is False

    # Once the workflow actually attaches a verified report, the SAME endpoint
    # must reflect it - this is what stops the UI showing COMPLETE on no report.
    r = make_runner()
    assert r.advance(job["id"])["status"] == states.COMPLETE
    body = client.get(f"/api/automation/jobs/{job['id']}").json()
    assert body["has_result"] is True
    assert body["report_uploaded"] is True
    assert body["attachment"] is not None


def test_retry_report_endpoint_repairs_without_re_running_the_ai(
        client, monkeypatch):
    """The manual "Retry Report Upload" action, end to end over HTTP, on a job
    already marked COMPLETE whose report was never uploaded."""
    from app import db
    import app.automation.runner as runner_mod
    job = _make_job()
    r = make_runner()
    assert r.advance(job["id"])["status"] == states.COMPLETE
    db.execute("UPDATE automation_jobs SET drive_file_id = NULL WHERE id = ?",
               (job["id"],))
    repair = FakeAttach()
    r._attach = repair
    monkeypatch.setattr(runner_mod, "runner", lambda: r)
    resp = client.post(f"/api/automation/jobs/{job['id']}/retry-report")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert repair.calls == 1
    assert body["report_uploaded"] is True
    assert body["status"] == states.COMPLETE
    assert r._state["enqueues"] == 1, "the AI correction must not re-run"


def test_grade_boundaries_endpoint_validates(client):
    seed_coursework("w1")
    bad = client.post("/api/automation/grade-boundaries/w1",
                      json={"boundaries": [["A", 0], ["B", 50]]})
    assert bad.status_code == 400
    ok = client.post("/api/automation/grade-boundaries/w1",
                     json={"boundaries": [["1", 0], ["5", 50], ["9", 90]]})
    assert ok.status_code == 200
    got = client.get("/api/automation/grade-boundaries/w1").json()
    assert got["source"] == "assignment"
    assert got["boundaries"] == [["1", 0.0], ["5", 50.0], ["9", 90.0]]


# --------------------------------------------- end-to-end orchestration (§1, §50)

def test_scheduler_detects_then_runner_completes_the_workflow():
    """The whole chain, with only the AI engine and Google faked.

    scheduler cycle -> detection -> persistent job -> existing correction
    (faked completion) -> deterministic grade -> verified report attach ->
    grade -> return -> COMPLETE.
    """
    from app.automation.scheduler import AutomationScheduler
    seed_coursework("w1")
    seed_tracking("s1")
    persist_result("r1")
    store.set_assignment_enabled("w1", "c1", True)

    r = make_runner()
    sched = AutomationScheduler(
        sync_fn=lambda c, w: None,
        detect_fn=lambda c, w, t: detection.detect_assignment(c, w,
                                                              assignment_title=t),
        cfg_fn=lambda: SimpleNamespace(enabled=True, effective_interval=lambda: 10),
        runner=r)

    summary = sched.run_cycle(now_ts=1000.0)
    assert summary["found"] == 1
    sched._advance_jobs(SimpleNamespace(), now_ts=10_000.0)
    r.run_pending()

    jobs = store.list_jobs(coursework_id="w1")
    assert len(jobs) == 1
    assert jobs[0]["status"] == states.COMPLETE
    assert jobs[0]["grade"] == "8"
    assert jobs[0]["marks_obtained"] == 67
    assert jobs[0]["result_id"] == "r1"

    # Another cycle (e.g. after a restart) must NOT create a second job.
    assert sched.run_cycle(now_ts=2000.0)["found"] == 0
    assert len(store.list_jobs(coursework_id="w1")) == 1


def test_resubmission_after_completion_queues_a_new_version():
    from app.automation.scheduler import AutomationScheduler
    seed_coursework("w1")
    seed_tracking("s1")
    persist_result("r1")
    store.set_assignment_enabled("w1", "c1", True)
    r = make_runner()
    sched = AutomationScheduler(
        sync_fn=lambda c, w: None,
        detect_fn=lambda c, w, t: detection.detect_assignment(c, w, assignment_title=t),
        cfg_fn=lambda: SimpleNamespace(enabled=True, effective_interval=lambda: 10),
        runner=r)
    sched.run_cycle(now_ts=1000.0)
    sched._advance_jobs(SimpleNamespace(), now_ts=10_000.0)
    r.run_pending()
    assert store.list_jobs(coursework_id="w1")[0]["status"] == states.COMPLETE

    # The student resubmits: a NEW version must queue a new correction, while
    # the completed version-1 job stays in the history untouched (§48).
    seed_tracking("s1", update_time="2026-09-05T11:00:00Z")
    assert sched.run_cycle(now_ts=3000.0)["found"] == 1
    jobs = store.list_jobs(coursework_id="w1")
    assert len(jobs) == 2
    assert any(j["status"] == states.COMPLETE for j in jobs)
    assert len({j["submission_version"] for j in jobs}) == 2


# ---------------------------------------------- teacher approval (review mode)

def test_review_mode_holds_then_teacher_approve_publishes_and_returns():
    """Review mode ON: the correction completes, the report is generated and
    validated, but NOTHING is sent to Classroom until the teacher approves.
    The approval then sends the report, the FINAL grade and the return as one
    coordinated workflow."""
    from app.config import AutomationConfig
    job = _make_job()
    publish = FakePublish()
    attach = FakeAttach()
    r = make_runner(config=AutomationConfig(review_mode=True), publish=publish,
                    attach=attach)
    held = r.advance(job["id"])
    assert held["status"] == states.ATTENTION_REQUIRED
    assert held["error_code"] == "review_mode_await_teacher"
    assert held["grade"] == "8"
    # NOTHING has reached Google Classroom yet: no grade write, no report.
    assert publish.calls == []          # not even a draft
    assert attach.calls == 0            # report delivery waits for approval

    final = r.approve(job["id"])
    assert final["status"] == states.COMPLETE
    assert attach.calls == 1            # report sent exactly once, on approval
    assert publish.calls == ["assign_grade", "return"]
    assert final["completed_at"] is not None

    # Approving again must never duplicate anything.
    import pytest
    from app.automation.runner import StepFailure
    with pytest.raises(StepFailure):
        r.approve(job["id"])
    assert attach.calls == 1
    assert publish.calls == ["assign_grade", "return"]


def test_approve_resumes_at_the_failed_stage_without_redoing_earlier_ones():
    """A half-finished approval (report sent, grade published, return failed)
    resumes at the FAILED stage: no second report, no second grade write."""
    job = _make_job()
    persist_result("r1")
    publish = FakePublish()
    attach = FakeAttach()
    r = make_runner(publish=publish, attach=attach,
                    config=__import__("app.config", fromlist=["AutomationConfig"])
                    .AutomationConfig(review_mode=True))
    from app.automation import store as _store
    # Simulate: approval ran, report + grade succeeded, then the process died
    # before the return (the job row records exactly that stage).
    _store.set_status(job["id"], states.GRADE_VERIFIED, grade="8",
                      result_id="r1")

    final = r.approve(job["id"])
    assert final["status"] == states.COMPLETE
    assert attach.calls == 0            # report stage was already done
    assert publish.calls == ["return"]  # only the failed stage ran


def test_approve_on_review_flagged_result_accepts_then_publishes():
    """A result flagged for review blocks the grade until approved; approving
    accepts it and publishes — without re-running the correction."""
    job = _make_job()
    persist_result("r1", review_required=1)
    publish = FakePublish()
    r = make_runner(publish=publish)               # review mode OFF
    blocked = r.advance(job["id"])
    assert blocked["status"] == states.ATTENTION_REQUIRED
    assert publish.calls == []                     # refused before any grade

    final = r.approve(job["id"])
    assert final["status"] == states.COMPLETE
    assert publish.calls == ["assign_grade", "return"]
    assert r._state["enqueues"] == 1               # AI correction never re-run

    from app import db
    row = db.query_one("SELECT manual_override, review_required FROM results "
                       "WHERE id = 'r1'")
    assert row["manual_override"] == 1 and row["review_required"] == 0


def test_approve_rejects_a_job_that_is_not_waiting_for_approval():
    from app.automation.runner import StepFailure
    job = _make_job()
    r = make_runner()
    final = r.advance(job["id"])
    assert final["status"] == states.COMPLETE      # already finished
    with pytest.raises(StepFailure):
        r.approve(job["id"])


def test_active_job_count_counts_live_workflow_lease_only_once():
    """Three moves through one job's stages are still ONE job in flight."""
    from app import db
    job = _make_job()
    now_ts = time.time()
    for _ in range(3):
        db.execute("UPDATE automation_jobs SET status = ?, claimed_by = ?, claimed_at = ? WHERE id = ?",
                   (states.CORRECTING, "worker", now_ts, job["id"]))
        assert store.execution_counts(now_ts)["active"] == 1
    store.set_status(job["id"], states.COMPLETE)
    assert store.execution_counts(now_ts)["active"] == 0
    # A finished job is not waiting either; only the retryable one is.
    assert store.execution_counts(now_ts)["waiting"] == 0
    failed = _make_job(submission_id="s2", student_id="u2", result_id="r2")
    store.set_status(failed["id"], states.REPORT_UPLOAD_FAILED,
                     next_attempt_at=now_ts + 30)
    assert store.execution_counts(now_ts)["active"] == 0
    assert store.execution_counts(now_ts)["waiting"] == 1


def test_active_job_count_ignores_stale_claims_and_deduplicates_child_stages():
    from app import db
    now_ts = time.time()
    first = _make_job()
    second = _make_job(submission_id="s2", student_id="u2", result_id="r2")
    db.execute("UPDATE automation_jobs SET status=?, claimed_by=?, claimed_at=? WHERE id=?",
               (states.CORRECTING, "stale", now_ts - store.CLAIM_LEASE_SECONDS - 1, first["id"]))
    db.execute("UPDATE automation_jobs SET status=?, correction_job_id=? WHERE id=?",
               (states.CORRECTING, "child-active", second["id"]))
    db.execute("INSERT INTO correction_jobs(id,status,created_at,updated_at) VALUES(?,?,?,?)",
               ("child-active", "MARKING", now_ts, now_ts))
    assert store.execution_counts(now_ts)["active"] == 1


def test_clear_recent_hides_terminal_jobs_but_preserves_active_and_results():
    from app import db
    active = _make_job()
    terminal = _make_job(submission_id="s2", student_id="u2", result_id="r2")
    result_id = persist_result("archive-result", submission_id="s2",
                               student_id="u2")
    store.set_status(terminal["id"], states.COMPLETE, result_id=result_id)
    before = store.clear_recent_count()
    assert before >= 1
    assert store.clear_recent_jobs() >= 1
    assert store.get(terminal["id"]) is not None
    assert db.query_one("SELECT id FROM results WHERE id=?", (result_id,)) is not None
    assert terminal["id"] not in {j["id"] for j in store.list_jobs()}
    assert active["id"] in {j["id"] for j in store.list_jobs()}
