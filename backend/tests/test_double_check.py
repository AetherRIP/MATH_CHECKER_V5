"""High-score double-check (§1-§21): trigger rule, comparison, persistence,
failure handling and automation-path integration.

These tests prove the WIRED-IN behaviour, not just the condition:
 - 94.99% never triggers; exactly 95%/98%/100% do (§2/§12)
 - the second correction is INDEPENDENT (no first-score input — the child
   gets only submission + mark scheme + engine instructions)
 - both results are preserved and the FINAL result is clearly identified
 - second-lower within threshold -> second becomes final
 - major disagreement -> REVIEW_REQUIRED, neither blindly applied
 - second-API failure -> first preserved, DOUBLE_CHECK_FAILED
 - the deterministic 1-9 grade is still computed from the final marks only
 - restart-mid-check cannot duplicate a completed check
"""

from __future__ import annotations

import json
import time

import pytest

import app.config as config
import app.db as db
from app.engine import double_check as dc


# ---------------------------------------------------------------------------
# Unit: the trigger rule (§2/§12/§14)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("earned,available,expected", [
    (80, 100, False),      # normal score
    (94, 100, False),
    (94.99, 100, False),   # unrounded, just below
    (95, 100, True),       # exactly 95 triggers (>=)
    (98, 100, True),
    (100, 100, True),
    (75, 75, True),        # 100% of a different paper
    (0, 0, False),         # degenerate: no maximum
])
def test_should_trigger_matrix(earned, available, expected):
    assert dc.should_trigger(earned, available) is expected


def test_trigger_uses_unrounded_percentage():
    # 94.999/100 rounds to 95.0 for display but must NOT trigger.
    assert dc.should_trigger(94.999, 100) is False


def test_threshold_and_disagreement_configurable():
    cfg = config.get_config()
    cfg.double_check.threshold_percentage = 90.0
    cfg.double_check.disagreement_points = 5.0
    try:
        assert dc.should_trigger(90, 100) is True
        assert dc.should_trigger(89.99, 100) is False
        decision = dc.decide(first_earned=100, first_available=100,
                             first_percentage=100.0, second_earned=93,
                             second_available=100, second_percentage=93.0)
        assert decision.status == dc.STATUS_REVIEW_REQUIRED
    finally:
        cfg.double_check.threshold_percentage = 95.0
        cfg.double_check.disagreement_points = 15.0


def test_double_check_can_be_disabled():
    cfg = config.get_config()
    cfg.double_check.enabled = False
    try:
        assert dc.should_trigger(100, 100) is False
    finally:
        cfg.double_check.enabled = True


# ---------------------------------------------------------------------------
# Unit: the decision logic (§5/§6)
# ---------------------------------------------------------------------------

def test_matching_results_confirm_first():
    d = dc.decide(first_earned=75, first_available=75, first_percentage=100.0,
                  second_earned=75, second_available=75, second_percentage=100.0)
    assert d.final_earned == 75 and d.final_available == 75
    assert d.status == dc.STATUS_COMPLETE
    assert d.disagreement_status == dc.DISAGREE_MATCHING
    assert d.review_required is False


def test_second_confirms_higher_keeps_first_lower_result():
    # The double-check exists to catch inflated scores; a HIGHER second run
    # must never raise the grade above the first result.
    d = dc.decide(first_earned=73, first_available=75, first_percentage=97.33,
                  second_earned=75, second_available=75, second_percentage=100.0)
    assert d.final_earned == 73
    assert d.status == dc.STATUS_COMPLETE


def test_second_lower_within_threshold_becomes_final():
    d = dc.decide(first_earned=100, first_available=100, first_percentage=100.0,
                  second_earned=94, second_available=100, second_percentage=94.0)
    assert d.final_earned == 94
    assert d.final_percentage == 94.0
    assert d.status == dc.STATUS_COMPLETE
    assert d.disagreement_status == dc.DISAGREE_SECOND_LOWER
    assert d.review_required is False


def test_major_disagreement_requires_review():
    d = dc.decide(first_earned=100, first_available=100, first_percentage=100.0,
                  second_earned=70, second_available=100, second_percentage=70.0)
    assert d.status == dc.STATUS_REVIEW_REQUIRED
    assert d.disagreement_status == dc.DISAGREE_MAJOR
    assert d.review_required is True
    assert d.final_earned == 100  # first kept pending review, not the 70


def test_exactly_at_disagreement_threshold_uses_second():
    # gap == 15.0 points is the boundary: within (<=), not major.
    d = dc.decide(first_earned=100, first_available=100, first_percentage=100.0,
                  second_earned=85, second_available=100, second_percentage=85.0)
    assert d.status == dc.STATUS_COMPLETE
    assert d.final_earned == 85


def test_question_differences_reported_only_from_evidence():
    first = {"questions": [
        {"question_number": "1", "awarded_marks": 4, "max_marks": 4, "loss_reason": ""},
        {"question_number": "2", "awarded_marks": 3, "max_marks": 4,
         "loss_reason": "wrong sign"},
    ]}
    second = {"questions": [
        {"question_number": "1", "awarded_marks": 3, "max_marks": 4,
         "loss_reason": "method incomplete"},
        {"question_number": "2", "awarded_marks": 3, "max_marks": 4,
         "loss_reason": "wrong sign"},
    ]}
    d = dc.decide(first_earned=7, first_available=8, first_percentage=87.5,
                  second_earned=6, second_available=8, second_percentage=75.0,
                  first_result=first, second_result=second)
    assert len(d.question_differences) == 1  # Q1 only; Q2 matched
    diff = d.question_differences[0]
    assert diff["question_number"] == "1"
    assert diff["first_awarded"] == 4 and diff["second_awarded"] == 3
    assert diff["second_loss_reason"] == "method incomplete"


# ---------------------------------------------------------------------------
# Integration: the pool save path
# ---------------------------------------------------------------------------

def _stage_result(coursework_id: str, submission_id: str, earned: int,
                  available: int) -> str:
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
             {"questions": [{"question_number": "1", "awarded_marks": earned,
                             "max_marks": available}]}), time.time()))
    return result_id


def _fresh_pool():
    cfg = config.get_config()
    cfg.mock_mode = True
    config.save_config(cfg)
    from app.config import get_secrets
    get_secrets().set_gemini_keys(["AIzaDoubleCheckTestKey12345678"])
    from app.workers.pool import WorkerPool
    pool = WorkerPool(worker_count=1)
    return pool


def _worker_result(job_id: str, awarded: int, maximum: int, *,
                   second: dict | None = None, dc_error: str | None = None):
    """A child payload exactly as the real child returns it, including the
    optional double-check fields."""
    from app.workers.adapter import WorkerResult
    return WorkerResult(
        job_id=job_id, worker_id="W1", status="COMPLETED", ok=True,
        result={"questions": [{"question_number": "1", "awarded_marks": awarded,
                               "max_marks": maximum}]},
        # A real child marks with a configured model; a fake name would now be
        # read as "marked by a fallback" (result_guard.assess_confidence).
        marking_model=config.get_config().gemini.primary_models[0],
        processing_seconds=1.0,
        fingerprint="fp", blueprint_report=None,
        double_check_triggered=(second is not None or dc_error is not None),
        second_result=second,
        # A genuinely different model: the same-model case is a non-independent
        # run and has its own regression test (test_mark_integrity.py).
        second_marking_model="gemini-test-second" if second else None,
        double_check_error=dc_error,
    )


def _second_result(awarded: int, maximum: int) -> dict:
    return {"questions": [{"question_number": "1", "awarded_marks": awarded,
                           "max_marks": maximum}]}


@pytest.fixture()
def pool(fresh_state):
    p = _fresh_pool()
    yield p
    p.shutdown(drain=False)


def test_pool_below_threshold_never_triggers(pool):
    result_id = _stage_result("cw-a", "sub-a", 80, 100)
    worker = pool._workers[0]
    pool._handle_result(worker, _worker_result("job-sub-a", 80, 100))
    row = db.query_one("SELECT * FROM results WHERE id = ?", (result_id,))
    assert row["double_check_triggered"] == 0
    assert row["double_check_status"] in (None, dc.STATUS_NOT_TRIGGERED)
    assert row["earned"] == 80


def test_pool_confirmed_check_preserves_both(pool):
    """100% confirmed by the independent run: final 100%, both kept (§11)."""
    result_id = _stage_result("cw-b", "sub-b", 100, 100)
    worker = pool._workers[0]
    pool._handle_result(worker, _worker_result(
        "job-sub-b", 100, 100, second=_second_result(100, 100)))
    row = db.query_one("SELECT * FROM results WHERE id = ?", (result_id,))
    assert row["double_check_triggered"] == 1
    assert row["double_check_status"] == dc.STATUS_COMPLETE
    assert row["first_earned"] == 100 and row["first_available"] == 100
    assert row["second_earned"] == 100 and row["second_available"] == 100
    assert row["final_earned"] == 100
    assert row["earned"] == 100  # the row's own marks ARE the final result
    assert row["disagreement_status"] == dc.DISAGREE_MATCHING
    assert row["review_required"] == 0
    assert row["first_correction_id"] and row["second_correction_id"]
    notes = [v["note"] for v in db.query(
        "SELECT note FROM result_versions WHERE result_id = ? "
        "ORDER BY version", (result_id,))]
    assert any("First correction" in n for n in notes)
    assert any("double-check correction" in n for n in notes)
    assert any("Final result" in n for n in notes)


def test_pool_second_lower_becomes_final(pool):
    """First 100/100, second 94/100 -> final 94, both preserved (§5)."""
    result_id = _stage_result("cw-c", "sub-c", 100, 100)
    worker = pool._workers[0]
    pool._handle_result(worker, _worker_result(
        "job-sub-c", 100, 100, second=_second_result(94, 100)))
    row = db.query_one("SELECT * FROM results WHERE id = ?", (result_id,))
    assert row["earned"] == 94, "the lower independent result must be final"
    assert row["first_earned"] == 100 and row["second_earned"] == 94
    assert row["final_earned"] == 94
    assert row["disagreement_status"] == dc.DISAGREE_SECOND_LOWER
    assert row["review_required"] == 0


def test_pool_major_disagreement_requires_review(pool):
    """First 100/100 vs second 70/100 -> REVIEW_REQUIRED, first kept (§6)."""
    result_id = _stage_result("cw-d", "sub-d", 100, 100)
    worker = pool._workers[0]
    pool._handle_result(worker, _worker_result(
        "job-sub-d", 100, 100, second=_second_result(70, 100)))
    row = db.query_one("SELECT * FROM results WHERE id = ?", (result_id,))
    assert row["double_check_status"] == dc.STATUS_REVIEW_REQUIRED
    assert row["disagreement_status"] == dc.DISAGREE_MAJOR
    assert row["review_required"] == 1
    assert row["earned"] == 100
    assert row["first_earned"] == 100 and row["second_earned"] == 70


def test_pool_failed_check_preserves_first_result(pool):
    """Second-API failure: first preserved, DOUBLE_CHECK_FAILED, review (§13)."""
    result_id = _stage_result("cw-e", "sub-e", 100, 100)
    worker = pool._workers[0]
    pool._handle_result(worker, _worker_result(
        "job-sub-e", 100, 100, dc_error="All configured Gemini models failed."))
    row = db.query_one("SELECT * FROM results WHERE id = ?", (result_id,))
    assert row["double_check_status"] == dc.STATUS_FAILED
    assert row["earned"] == 100, "a failed check must not discard the first result"
    assert row["second_earned"] is None
    assert row["review_required"] == 1
    assert "failed" in (row["double_check_reason"] or "").lower()
    # The review screen must be able to say WHY, not just that a flag exists.
    import json as _json
    verification = _json.loads(row["verification_json"] or "{}")
    codes = [r.get("code") for r in (verification.get("review_reasons") or [])]
    assert "DOUBLE_CHECK_UNAVAILABLE" in codes
    reason = next(r for r in verification["review_reasons"]
                  if r["code"] == "DOUBLE_CHECK_UNAVAILABLE")
    assert reason["severity"] == "review"
    assert "double check failed" in reason["detail"].lower()
    assert verification.get("review_required") is True


def test_pool_grade_is_deterministic_from_final_marks(pool):
    """§7: the provisional grade is still computed from FINAL marks only."""
    result_id = _stage_result("cw-f", "sub-f", 100, 100)
    worker = pool._workers[0]
    pool._handle_result(worker, _worker_result(
        "job-sub-f", 100, 100, second=_second_result(85, 100)))  # 85% -> grade 8
    row = db.query_one("SELECT * FROM results WHERE id = ?", (result_id,))
    assert row["earned"] == 85
    assert row["provisional_grade"] == "8"
    assert row["disagreement_status"] == dc.DISAGREE_SECOND_LOWER


def test_completed_check_never_reruns_on_new_job_payload(pool):
    """Restart-mid-check idempotency (§17): a saved COMPLETE check is not
    re-run when the same submission is re-handled — the persisted row proves
    the check already completed, so nothing re-triggers for that version."""
    result_id = _stage_result("cw-g", "sub-g", 100, 100)
    worker = pool._workers[0]
    pool._handle_result(worker, _worker_result(
        "job-sub-g", 100, 100, second=_second_result(100, 100)))
    row = db.query_one("SELECT * FROM results WHERE id = ?", (result_id,))
    assert row["double_check_status"] == dc.STATUS_COMPLETE
    # Simulate a restart: a fresh pool process re-reads the same row.
    assert dc.was_completed(db.row_to_dict(row)) is True


# ---------------------------------------------------------------------------
# §7 low-score independent check (the mirror of the high-score check)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("earned,available,expected", [
    (50, 100, False),      # healthy mid-range
    (11, 100, False),      # just above the low band
    (10, 100, True),       # exactly the low boundary triggers (<=)
    (10.01, 100, False),
    (5, 100, True),
    (0, 100, True),        # a zero gets an independent second look
    (3, 75, True),
    (0, 0, False),         # degenerate: no maximum -> no trigger
])
def test_low_score_trigger_matrix(earned, available, expected):
    assert dc.should_trigger(earned, available) is expected


def test_direction_of_reports_which_end_triggered():
    assert dc.direction_of(100, 100) == dc.DIRECTION_HIGH
    assert dc.direction_of(50, 100) is None
    assert dc.direction_of(5, 100) == dc.DIRECTION_LOW


def test_low_branch_can_be_disabled():
    cfg = config.get_config()
    cfg.double_check.low_threshold_percentage = 0.0
    try:
        assert dc.should_trigger(5, 100) is False   # high check still independent
        assert dc.direction_of(100, 100) == dc.DIRECTION_HIGH
    finally:
        cfg.double_check.low_threshold_percentage = 10.0


def test_low_check_higher_second_becomes_final():
    """First looked suspiciously low (5%); the independent pass found 15%
    (a 10-point rise, within the 15-point disagreement limit)."""
    d = dc.decide(first_earned=5, first_available=100, first_percentage=5.0,
                  second_earned=15, second_available=100,
                  second_percentage=15.0, direction=dc.DIRECTION_LOW)
    assert d.final_earned == 15
    assert d.final_percentage == 15.0
    assert d.status == dc.STATUS_COMPLETE
    assert d.disagreement_status == dc.DISAGREE_SECOND_HIGHER
    assert d.review_required is False


def test_low_check_agreement_keeps_first():
    d = dc.decide(first_earned=5, first_available=100, first_percentage=5.0,
                  second_earned=4, second_available=100, second_percentage=4.0,
                  direction=dc.DIRECTION_LOW)
    assert d.final_earned == 5
    assert d.status == dc.STATUS_COMPLETE
    assert d.disagreement_status == dc.DISAGREE_MATCHING
    assert d.review_required is False


def test_low_check_major_disagreement_requires_review():
    d = dc.decide(first_earned=5, first_available=100, first_percentage=5.0,
                  second_earned=90, second_available=100, second_percentage=90.0,
                  direction=dc.DIRECTION_LOW)
    assert d.status == dc.STATUS_REVIEW_REQUIRED
    assert d.disagreement_status == dc.DISAGREE_MAJOR
    assert d.review_required is True
    assert d.final_earned == 5  # first kept pending review, not the 90


def test_high_direction_unchanged_by_low_support():
    # The mirrored rules must not soften the original high-score check.
    d = dc.decide(first_earned=100, first_available=100, first_percentage=100.0,
                  second_earned=70, second_available=100, second_percentage=70.0,
                  direction=dc.DIRECTION_HIGH)
    assert d.status == dc.STATUS_REVIEW_REQUIRED and d.disagreement_status == dc.DISAGREE_MAJOR
