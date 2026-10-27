"""Job system + verification tests."""

import threading
import time

import pytest

import app.jobs.manager as jm
from app.jobs.manager import (
    JobManager, DuplicateJobError, CancelledError, WaitForApiRetry,
)
from app.engine.verify import verify_result


class TestEnqueue:
    def test_enqueue_and_claim(self, fresh_state):
        manager = JobManager()
        job = manager.enqueue(submission_id="s1", coursework_id="w1",
                              course_id="c1", student_id="u1", student_name="Alice")
        assert job["status"] == "QUEUED"
        claimed = manager._claim_next()
        assert claimed["id"] == job["id"]
        assert claimed["status"] == "PREPARING"
        assert claimed["attempts"] == 1

    def test_duplicate_active_job_rejected(self, fresh_state):
        manager = JobManager()
        manager.enqueue(submission_id="s1", coursework_id="w1",
                        course_id="c1", student_id="u1", student_name="Alice")
        with pytest.raises(DuplicateJobError):
            manager.enqueue(submission_id="s1", coursework_id="w1",
                            course_id="c1", student_id="u1", student_name="Alice")

    def test_new_job_allowed_after_completion(self, fresh_state):
        manager = JobManager()
        job = manager.enqueue(submission_id="s1", coursework_id="w1",
                              course_id="c1", student_id="u1", student_name="Alice")
        manager._claim_next()
        manager._set_status(job["id"], "COMPLETED", finish=True)
        second = manager.enqueue(submission_id="s1", coursework_id="w1",
                                 course_id="c1", student_id="u1", student_name="Alice")
        assert second["id"] != job["id"]

    def test_manual_jobs_allow_duplicates(self, fresh_state):
        manager = JobManager()
        manager.enqueue(submission_id=None, coursework_id=None, course_id=None,
                        student_id=None, student_name="A")
        manager.enqueue(submission_id=None, coursework_id=None, course_id=None,
                        student_id=None, student_name="B")  # no constraint
        assert len(manager.list()) == 2


class TestExecution:
    def _wait_for(self, cond, timeout=10):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if cond():
                return True
            time.sleep(0.05)
        return False

    def test_success_flow(self, fresh_state):
        manager = JobManager(worker_count=1)
        seen = []

        def handler(job, cancel):
            manager.set_stage(job["id"], "MARKING", "marking now")
            seen.append(job["id"])

        manager.set_handler("classroom_correction", handler)
        manager.start()
        job = manager.enqueue(submission_id="s1", coursework_id="w1",
                              course_id="c1", student_id="u1", student_name="A")
        assert self._wait_for(lambda: manager.get(job["id"])["status"] == "COMPLETED")
        manager.shutdown()
        assert seen == [job["id"]]

    def test_cancellation(self, fresh_state):
        manager = JobManager(worker_count=1)

        def handler(job, cancel):
            for _ in range(100):
                if cancel.is_set():
                    raise CancelledError()
                time.sleep(0.05)

        manager.set_handler("classroom_correction", handler)
        manager.start()
        job = manager.enqueue(submission_id="s1", coursework_id="w1",
                              course_id="c1", student_id="u1", student_name="A")
        assert self._wait_for(lambda: manager.get(job["id"])["attempts"] >= 1)
        assert manager.cancel(job["id"])
        assert self._wait_for(lambda: manager.get(job["id"])["status"] == "CANCELLED")
        manager.shutdown()

    def test_retry_then_success(self, fresh_state):
        manager = JobManager(worker_count=1)
        calls = {"n": 0}

        def handler(job, cancel):
            calls["n"] += 1
            if calls["n"] < 2:
                raise RuntimeError("transient failure")

        manager.set_handler("classroom_correction", handler)
        manager.start()
        job = manager.enqueue(submission_id="s1", coursework_id="w1",
                              course_id="c1", student_id="u1", student_name="A")
        assert self._wait_for(lambda: manager.get(job["id"])["status"] == "COMPLETED", timeout=15)
        manager.shutdown()
        assert calls["n"] == 2

    def test_failure_after_max_attempts(self, fresh_state):
        manager = JobManager(worker_count=1, max_attempts=1)

        def handler(job, cancel):
            raise RuntimeError("permanent problem")

        manager.set_handler("classroom_correction", handler)
        manager.start()
        job = manager.enqueue(submission_id="s1", coursework_id="w1",
                              course_id="c1", student_id="u1", student_name="A")
        assert self._wait_for(
            lambda: manager.get(job["id"])["status"] == "FAILED", timeout=10)
        final = manager.get(job["id"])
        assert final["attempts"] >= final["max_attempts"]
        manager.shutdown()

    def test_wait_for_api_parks_then_recovers(self, fresh_state):
        manager = JobManager(worker_count=1)
        calls = {"n": 0}

        def handler(job, cancel):
            calls["n"] += 1
            if calls["n"] == 1:
                raise WaitForApiRetry(0.2)

        manager.set_handler("classroom_correction", handler)
        manager.start()
        job = manager.enqueue(submission_id="s1", coursework_id="w1",
                              course_id="c1", student_id="u1", student_name="A")
        assert self._wait_for(lambda: manager.get(job["id"])["status"] == "COMPLETED", timeout=15)
        statuses_seen = manager.get(job["id"])
        assert statuses_seen["status"] == "COMPLETED"
        assert calls["n"] == 2
        manager.shutdown()


class TestRecovery:
    def test_recover_requeues_inflight(self, fresh_state):
        manager = JobManager()
        job = manager.enqueue(submission_id="s1", coursework_id="w1",
                              course_id="c1", student_id="u1", student_name="A")
        # simulate crash mid-processing
        manager._claim_next()
        result = manager.recover_on_boot()
        assert result["recovered"] == 1
        assert manager.get(job["id"])["status"] == "QUEUED"

    def test_recover_never_touches_completed(self, fresh_state):
        manager = JobManager()
        job = manager.enqueue(submission_id="s1", coursework_id="w1",
                              course_id="c1", student_id="u1", student_name="A")
        manager._claim_next()
        manager._set_status(job["id"], "COMPLETED", finish=True)
        result = manager.recover_on_boot()
        assert result["recovered"] == 0
        assert manager.get(job["id"])["status"] == "COMPLETED"

    def test_recover_fails_exhausted_jobs(self, fresh_state):
        manager = JobManager(max_attempts=1)
        job = manager.enqueue(submission_id="s1", coursework_id="w1",
                              course_id="c1", student_id="u1", student_name="A")
        manager._claim_next()  # attempts = 1 = max
        result = manager.recover_on_boot()
        assert result["failed"] == 1
        assert manager.get(job["id"])["status"] == "FAILED"


class TestVerification:
    def test_verified_clean_result(self):
        result = {
            "questions": [
                {"question_number": "1", "awarded_marks": 3, "max_marks": 4,
                 "mark_breakdown": [{"code": "M1", "awarded": True, "reason": "r"}]},
                {"question_number": "2", "awarded_marks": 5, "max_marks": 6,
                 "mark_breakdown": []},
            ],
            "uncertainties": [],
        }
        verification = verify_result(result)
        assert verification["verified"] is True
        assert verification["earned"] == 8
        assert verification["available"] == 10
        assert verification["percentage"] == 80.0
        assert verification["provisional_grade"] == "8"

    def test_flags_empty_questions(self):
        verification = verify_result({"questions": [], "uncertainties": []})
        assert verification["verified"] is False
        assert any("No questions" in a for a in verification["anomalies"])

    def test_surfaces_uncertainties_as_warnings(self):
        result = {"questions": [{"question_number": "1", "awarded_marks": 1,
                                 "max_marks": 1}],
                  "uncertainties": ["blurry image"]}
        verification = verify_result(result)
        assert any("blurry image" in w for w in verification["warnings"])

    def test_breakdown_overcount_warns(self):
        result = {"questions": [
            {"question_number": "1", "awarded_marks": 2, "max_marks": 1,
             "mark_breakdown": [
                 {"code": "M1", "awarded": True, "reason": "a"},
                 {"code": "A1", "awarded": True, "reason": "b"}]}],
            "uncertainties": []}
        verification = verify_result(result)
        assert any("more awarded" in w for w in verification["warnings"])


class TestQuestionLevelValidation:
    """§5: 0 <= awarded <= max. A violation is a REVIEW signal, never a clamp."""

    def test_awarded_above_max_is_review(self):
        result = {"questions": [
            {"question_number": "1", "awarded_marks": 6, "max_marks": 4},
        ]}
        verification = verify_result(result)
        assert verification["status"] == "REVIEW"
        assert any("awarded 6" in a and "maximum is 4" in a
                   for a in verification["anomalies"])
        # The reported mark is never silently rewritten.
        assert result["questions"][0]["awarded_marks"] == 6

    def test_negative_awarded_is_review(self):
        result = {"questions": [
            {"question_number": "2", "awarded_marks": -1, "max_marks": 4},
        ]}
        verification = verify_result(result)
        assert verification["status"] == "REVIEW"
        assert any("negative awarded" in a for a in verification["anomalies"])

    def test_in_range_marks_stay_valid(self):
        result = {"questions": [
            {"question_number": "1", "awarded_marks": 4, "max_marks": 4},
            {"question_number": "2", "awarded_marks": 0, "max_marks": 3},
        ]}
        assert verify_result(result)["status"] == "VALID"


class TestScoreAboveAvailable:
    """A score above the paper maximum must be visible AND explained.

    The verifier may never silently lower a mark, so ``earned`` keeps the sum
    the questions actually claim. What it must not do is print ``8 / 7`` as if
    it were an ordinary score: the difference is named, the offending question
    is named, and the REVIEW state stays.
    """

    def _over_awarded(self):
        return {"questions": [
            {"question_number": "1", "awarded_marks": 5, "max_marks": 4},
            {"question_number": "2", "awarded_marks": 3, "max_marks": 3},
        ]}

    def test_eight_of_seven_is_reported_and_not_clamped(self):
        result = self._over_awarded()
        verification = verify_result(result)
        assert verification["earned"] == 8
        assert verification["available"] == 7
        assert verification["over_available"] == 1
        assert verification["status"] == "REVIEW"
        assert any("exceeds the 7 marks available by 1" in w
                   for w in verification["warnings"])
        assert any("question 1 (5 of 4)" in w for w in verification["warnings"])
        assert any("No mark has been changed" in w
                   for w in verification["warnings"])
        # The awarded marks themselves are untouched.
        assert [q["awarded_marks"] for q in result["questions"]] == [5, 3]

    def test_a_normal_score_reports_no_excess(self):
        result = {"questions": [
            {"question_number": "1", "awarded_marks": 3, "max_marks": 4},
            {"question_number": "2", "awarded_marks": 3, "max_marks": 3},
        ]}
        verification = verify_result(result)
        assert verification["over_available"] == 0
        assert verification["earned"] == 6 and verification["available"] == 7
        assert verification["status"] == "VALID"
        assert not any("exceeds" in w for w in verification["warnings"])

    def test_the_excess_warning_is_never_duplicated(self):
        verification = verify_result(self._over_awarded())
        excess = [w for w in verification["warnings"] if "exceeds" in w]
        assert len(excess) == 1


class TestSchemeAuditPropagation:
    """A scheme the audit refused to vouch for must reach the result.

    ``NEEDS_REPAIR`` was stored inside ``verification.blueprint`` and read by
    nobody: a paper marked against a scheme whose own total disagreed with its
    parts still looked like a routine, valid correction.
    """

    def _result(self):
        return {"questions": [
            {"question_number": "1", "awarded_marks": 3, "max_marks": 4},
            {"question_number": "2", "awarded_marks": 3, "max_marks": 3},
        ]}

    def test_an_unverified_scheme_is_a_note_not_a_review(self):
        """A scheme read without the independent pass is UNVERIFIED, not wrong.

        It is reported on every paper as an advisory (so the teacher can recheck
        it once), but it cannot make each paper's marks look doubtful: no
        structural fault was found in the marking. Re-flagging every paper was
        the single largest source of reviews (14 of 15 stored corrections).
        """
        verification = verify_result(self._result(), {
            "source": "built", "verification_state": "NEEDS_REPAIR",
            "completeness": {"verdict": "COMPLETE", "unresolved": False},
            "missing_questions": [], "extra_questions": [],
        })
        assert any("NEEDS_REPAIR" in w and "never" in w
                   for w in verification["warnings"])
        assert verification["earned"] == 6
        assert verification["status"] == "VALID"
        assert verification["review_required"] is False
        assert "MARK_SCHEME_UNVERIFIED" in verification["review_reason_codes"]
        assert (verification["blueprint"]["verification_state"]
                == "NEEDS_REPAIR")

    def test_a_scheme_the_marking_contradicts_is_a_review(self):
        """A declared question nobody marked is a fault, not a note."""
        verification = verify_result(self._result(), {
            "source": "built", "verification_state": "NEEDS_REPAIR",
            "completeness": {"verdict": "INCOMPLETE", "unresolved": True},
            "missing_questions": [], "extra_questions": [],
        })
        assert verification["status"] == "REVIEW"
        assert "MARK_SCHEME_CONTRADICTED" in verification["review_reason_codes"]
        assert verification["earned"] == 6

    def test_a_verified_scheme_adds_no_warning(self):
        verification = verify_result(self._result(), {
            "source": "cache", "verification_state": "VERIFIED",
            "completeness": {"verdict": "COMPLETE", "unresolved": False},
        })
        assert not any("NEEDS_REPAIR" in w for w in verification["warnings"])
        assert verification["status"] == "VALID"

    def test_the_state_warning_is_emitted_once(self):
        verification = verify_result(self._result(), {
            "verification_state": "NEEDS_REPAIR", "completeness": {}})
        assert len([w for w in verification["warnings"]
                    if "NEEDS_REPAIR" in w]) == 1

    def test_existing_warnings_keep_their_place(self):
        result = self._result()
        result["uncertainties"] = ["handwriting unclear"]
        verification = verify_result(result, {
            "verification_state": "NEEDS_MANUAL_REVIEW", "completeness": {}})
        joined = verification["warnings"]
        assert any("handwriting unclear" in w for w in joined)
        assert any("NEEDS_MANUAL_REVIEW" in w for w in joined)
        # The marker's own note is still reported before the scheme warning.
        assert (next(i for i, w in enumerate(joined) if "handwriting" in w)
                < next(i for i, w in enumerate(joined)
                       if "NEEDS_MANUAL_REVIEW" in w))

class TestProgressReporting:
    """The ``progress`` column must never contradict the stored status."""

    def test_terminal_status_reports_a_real_fraction(self, fresh_state):
        """Regression: a COMPLETED job must not report 0% progress.

        A job run by the WorkerPool is moved to COMPLETED with raw SQL, which
        never touches ``progress``, so the API reported 0 for a finished job
        while three UI pages rendered that value as a percentage bar. The read
        path derives it from this module's own ``STAGE_PROGRESS`` map instead.
        """
        assert jm._effective_progress(
            {"status": "COMPLETED", "progress": 0.0})["progress"] == 1.0
        # A failure is not a finished correction: it keeps the work it really
        # did instead of presenting a full bar.
        assert jm._effective_progress(
            {"status": "FAILED", "progress": 0.35})["progress"] == 0.35
        assert jm._effective_progress(
            {"status": "CANCELLED", "progress": 0})["progress"] == 0.0

    def test_real_progress_is_never_overwritten(self, fresh_state):
        row = {"status": "MARKING", "progress": 0.5}
        assert jm._effective_progress(row)["progress"] == 0.5

    def test_a_completed_job_finishes_at_one_hundred_percent(self, fresh_state):
        # Real worker stages leave 0.95 behind ("Saving the result"); the save
        # it describes has finished by the time the job is COMPLETED.
        assert jm._effective_progress(
            {"status": "COMPLETED", "progress": 0.95})["progress"] == 1.0

    def test_status_without_a_mapping_keeps_its_stored_value(self, fresh_state):
        # STARTING/PROCESSING are the pool's in-flight states: no fraction is
        # claimed for them rather than inventing one.
        row = {"status": "STARTING", "progress": 0.0}
        assert jm._effective_progress(row)["progress"] == 0.0

    def test_manager_get_and_list_report_terminal_progress(self, fresh_state):
        from app import db

        manager = JobManager()
        job = manager.enqueue(submission_id="s9", coursework_id="w9",
                              course_id="c9", student_id="u9",
                              student_name="Zoe")
        # Exactly what the worker pool does when it finishes a job.
        db.execute("UPDATE correction_jobs SET status = 'COMPLETED', "
                   "progress = 0 WHERE id = ?", (job["id"],))

        got = manager.get(job["id"])
        assert got["status"] == "COMPLETED"
        assert got["progress"] == 1.0

        listed = manager.list(statuses=["COMPLETED"])
        assert any(r["id"] == job["id"] and r["progress"] == 1.0
                   for r in listed)


class TestLiveProgress:
    """The pool must publish real stages, not only a final status.

    Regression: a job run by the WorkerPool stayed at ``STARTING`` with
    ``progress = 0`` and no stage text for the whole correction, and
    ``/jobs/stream`` heard nothing after the enqueue event — the desktop showed
    "Starting… 0%" for minutes while the backend was working.
    """

    def test_report_progress_publishes_and_never_goes_backwards(self, fresh_state):
        manager = JobManager()
        job = manager.enqueue(submission_id="s1", coursework_id="w1",
                              course_id="c1", student_id="u1",
                              student_name="Alice")
        manager._claim_next()
        seen = []
        manager.add_stage_listener(seen.append)

        manager.report_progress(job["id"], stage="Marking with gemini-3.8-flash",
                                progress=0.35, status="MARKING")
        row = manager.get(job["id"])
        assert row["status"] == "MARKING"
        assert row["stage_detail"] == "Marking with gemini-3.8-flash"
        assert row["progress"] == pytest.approx(0.35)
        # The listener is how the desktop hears the change without a refresh.
        assert seen and seen[-1]["stage_detail"] == "Marking with gemini-3.8-flash"
        assert seen[-1]["status"] == "MARKING"

        # A lower value (a stale event from an earlier stage) cannot rewind it.
        manager.report_progress(job["id"], progress=0.2)
        assert manager.get(job["id"])["progress"] == pytest.approx(0.35)

        manager.report_progress(job["id"], stage="Saving the result",
                                progress=0.95, status="SAVING")
        assert manager.get(job["id"])["progress"] == pytest.approx(0.95)

    def test_progress_never_reopens_a_terminal_job(self, fresh_state):
        manager = JobManager()
        job = manager.enqueue(submission_id="s1", coursework_id="w1",
                              course_id="c1", student_id="u1",
                              student_name="Alice")
        manager._claim_next()
        manager._set_status(job["id"], "COMPLETED",
                            stage_detail="Correction completed", finish=True)

        manager.report_progress(job["id"], stage="late event", progress=0.5)
        row = manager.get(job["id"])
        assert row["status"] == "COMPLETED"
        assert row["stage_detail"] == "Correction completed"

    def test_a_finished_job_reports_complete_progress(self, fresh_state):
        manager = JobManager()
        job = manager.enqueue(submission_id="s1", coursework_id="w1",
                              course_id="c1", student_id="u1",
                              student_name="Alice")
        manager._claim_next()
        manager.report_progress(job["id"], stage="Saving the result",
                                progress=0.95, status="SAVING")
        manager._set_status(job["id"], "COMPLETED", stage_detail="Correction completed",
                            finish=True)
        assert manager.get(job["id"])["progress"] == 1.0


class TestStageMessages:
    """Engine stage text is mapped to a status and shown without emoji."""

    def test_plain_stage_strips_emoji_and_keeps_the_sentence(self):
        from app.workers.pool import _plain_stage
        assert _plain_stage("🧠 Marking with gemini-3.8-flash") == \
            "Marking with gemini-3.8-flash"
        assert _plain_stage("📘 Reading the mark scheme structure…") == \
            "Reading the mark scheme structure…"

    def test_stage_status_follows_the_pipeline(self):
        from app.workers.pool import _stage_status
        assert _stage_status("Marking with gemini-3.8-flash") == "MARKING"
        assert _stage_status("Uploading the paper to the AI service") == "UPLOADING"
        assert _stage_status("Saving the result") == "SAVING"
        assert _stage_status("Checking questions the first pass missed") == "VERIFYING"
        assert _stage_status("some unrelated note") is None

    def test_child_stage_event_is_one_json_line(self, monkeypatch):
        import io
        import json as _json
        from app.workers import child

        buffer = io.StringIO()
        monkeypatch.setattr(child.sys, "stdout", buffer)
        child._emit("Marking with gemini-3.8-flash", progress=0.35)
        lines = [ln for ln in buffer.getvalue().splitlines() if ln.strip()]
        assert len(lines) == 1
        event = _json.loads(lines[0])
        assert event == {"type": "stage",
                         "message": "Marking with gemini-3.8-flash",
                         "progress": 0.35}
