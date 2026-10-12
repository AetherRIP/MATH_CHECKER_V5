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
