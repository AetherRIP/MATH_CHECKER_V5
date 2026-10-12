"""Integration: the result guard protects stored results in the real save path.

Unit tests cover the guard's logic; these prove it is actually WIRED IN —
a re-correction that produces a suspicious result must leave the stored
result intact rather than silently replacing it (§7).
"""

import json
import time

import app.config as config
import app.db as db


def _stage_result(coursework_id: str, submission_id: str, earned: int,
                  available: int, **extra) -> str:
    """Insert a prior completed job + result, as a real correction would."""
    job_id = f"job-{submission_id}"
    result_id = f"res-{submission_id}"
    db.execute(
        "INSERT INTO correction_jobs(id, submission_id, coursework_id, course_id, "
        "student_id, student_name, status, created_at, updated_at) "
        "VALUES(?,?,?,?,?,?,'COMPLETED',?,?)",
        (job_id, submission_id, coursework_id, "c1", "u1", "Alice",
         time.time(), time.time()))
    db.execute(
        "INSERT INTO results(id, job_id, coursework_id, submission_id, student_id, "
        "student_name, earned, available, percentage, result_json, created_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (result_id, job_id, coursework_id, submission_id, "u1", "Alice",
         earned, available, earned / available * 100.0, json.dumps(
             {"questions": [{"question_number": 1, "awarded_marks": earned,
                             "max_marks": available}]}), time.time()))
    return result_id


def _fresh_pool():
    cfg = config.get_config()
    cfg.mock_mode = True
    config.save_config(cfg)
    from app.config import get_secrets
    get_secrets().set_gemini_keys(["AIzaGuardTestKey1234567890"])
    from app.workers.pool import WorkerPool
    pool = WorkerPool(worker_count=1)
    pool.start()
    return pool


def _worker_result(job_id: str, awarded: int, maximum: int):
    from app.workers.adapter import WorkerResult
    return WorkerResult(
        job_id=job_id, worker_id="w1", status="COMPLETED", ok=True,
        result={"questions": [{"question_number": 1, "awarded_marks": awarded,
                               "max_marks": maximum}]},
        marking_model="gemini-test", processing_seconds=1.0,
        fingerprint="fp", blueprint_report=None,
    )


def test_suspicious_zero_does_not_overwrite_valid_result(fresh_state):
    """The exact bug from the brief: 67/103 must survive a later 0/103."""
    result_id = _stage_result("cw1", "sub1", 67, 103)
    pool = _fresh_pool()
    try:
        worker = pool._workers[0]
        pool._handle_result(worker, _worker_result("job-sub1", 0, 103))

        row = db.query_one("SELECT * FROM results WHERE id = ?", (result_id,))
        assert row["earned"] == 67, "a valid result must never be silently zeroed"
        assert row["review_required"] == 1, "the conflict must be flagged for review"
        assert row["confidence"] == "LARGE_SCORE_CHANGE"
    finally:
        pool.shutdown(drain=False)


def test_consistent_result_updates_normally(fresh_state):
    result_id = _stage_result("cw2", "sub2", 60, 100)
    pool = _fresh_pool()
    try:
        worker = pool._workers[0]
        pool._handle_result(worker, _worker_result("job-sub2", 62, 100))
        row = db.query_one("SELECT * FROM results WHERE id = ?", (result_id,))
        assert row["earned"] == 62
        assert row["review_required"] == 0
    finally:
        pool.shutdown(drain=False)


def test_force_reprocess_allows_replace(fresh_state):
    result_id = _stage_result("cw3", "sub3", 80, 100)
    pool = _fresh_pool()
    try:
        worker = pool._workers[0]
        pool._force_reprocess.add("job-sub3")
        pool._handle_result(worker, _worker_result("job-sub3", 5, 100))
        row = db.query_one("SELECT * FROM results WHERE id = ?", (result_id,))
        # Explicitly forced: the drop is allowed (the teacher asked for it).
        assert row["earned"] == 5
        assert "job-sub3" not in pool._force_reprocess
    finally:
        pool.shutdown(drain=False)


# ---- remark (§Remark): the production worker path, not just the API ----

def _remark_job(coursework_id: str, submission_id: str) -> str:
    """A remark job exactly as the Remark endpoint creates it."""
    job_id = f"remark-job-{submission_id}"
    db.execute(
        "INSERT INTO correction_jobs(id, submission_id, coursework_id, course_id, "
        "student_id, student_name, status, created_at, updated_at, is_remark) "
        "VALUES(?,?,?,?,?,?,'COMPLETED',?,?,1)",
        (job_id, submission_id, coursework_id, "c1", "u1", "Alice",
         time.time(), time.time()))
    return job_id


def test_remark_attempt_is_recorded_but_never_applied(fresh_state):
    """The real worker path: a re-mark must not touch the active correction."""
    from app.engine import remark, versions

    result_id = _stage_result("cw-remark", "sub-remark", 68, 75)
    job_id = _remark_job("cw-remark", "sub-remark")
    pool = _fresh_pool()
    try:
        worker = pool._workers[0]
        # A BETTER result than the stored one - it still must not auto-apply.
        pool._handle_result(worker, _worker_result(job_id, 70, 75))
    finally:
        pool.shutdown(drain=False)

    row = db.query_one("SELECT * FROM results WHERE id = ?", (result_id,))
    assert row["earned"] == 68, "a remark must never auto-apply"

    summary = remark.summary(result_id)
    assert summary["awaiting_decision"] is True
    assert summary["active"]["earned"] == 68
    assert summary["pending"]["earned"] == 70
    assert summary["comparison"]["delta_points"] == 2

    # The previous attempt is still there, and the new one is recorded.
    assert len(versions.list_versions(result_id)) >= 1
    job = db.query_one("SELECT * FROM correction_jobs WHERE id = ?", (job_id,))
    assert job["result_id"] == result_id


def test_remark_that_scores_zero_still_cannot_destroy_the_result(fresh_state):
    """The motivating bug, reached through remark: 68/75 must survive a 0/75."""
    from app.engine import remark

    result_id = _stage_result("cw-remark2", "sub-remark2", 68, 75)
    job_id = _remark_job("cw-remark2", "sub-remark2")
    pool = _fresh_pool()
    try:
        worker = pool._workers[0]
        pool._handle_result(worker, _worker_result(job_id, 0, 75))
    finally:
        pool.shutdown(drain=False)

    row = db.query_one("SELECT earned FROM results WHERE id = ?", (result_id,))
    assert row["earned"] == 68, "a zero-scoring remark must not destroy a valid result"

    summary = remark.summary(result_id)
    assert summary["awaiting_decision"] is True
    assert summary["pending"]["earned"] == 0
    # An unreliable attempt is never presented as the sensible default.
    assert summary["pending"]["reliable"] is False


def test_teacher_can_then_promote_a_remark_through_the_real_path(fresh_state):
    from app.engine import remark

    result_id = _stage_result("cw-remark3", "sub-remark3", 68, 75)
    job_id = _remark_job("cw-remark3", "sub-remark3")
    pool = _fresh_pool()
    try:
        worker = pool._workers[0]
        pool._handle_result(worker, _worker_result(job_id, 70, 75))
    finally:
        pool.shutdown(drain=False)

    out = remark.decide(result_id, remark.DECISION_USE_NEW)
    # _stage_result writes no baseline version row, so this pending attempt is
    # version 1 - what matters is that the teacher's choice is applied.
    assert out["active_version"] == 1
    assert out["decision"] == remark.DECISION_USE_NEW
    assert db.query_one(
        "SELECT earned FROM results WHERE id = ?", (result_id,))["earned"] == 70
    assert remark.summary(result_id)["awaiting_decision"] is False
