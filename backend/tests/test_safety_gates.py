"""TEST/REVIEW MODE + submission-quality gate (live-accuracy safety fixes).

Locks the two safety changes made while the system is being tested on real
student submissions:

1. ``review_mode`` — the automation NEVER publishes a final grade or returns
   work while review mode is on: the proposed grade is saved as a verified
   draft and the job stops awaiting teacher approval. ``review_mode``
   defaults to True so a fresh install is safe.
2. ``engine.submission_validation`` — a blank / unreadable / wrong-assignment
   submission is deterministically flagged SUBMISSION_REVIEW_REQUIRED instead
   of being trusted as a score.
"""

from __future__ import annotations

import app.config as config
import app.db as db
from app.automation import states
from app.engine.submission_validation import (
    QUALITY_REVIEW, QUALITY_VALID, assess_submission_quality,
)
from app.engine.result_guard import SUBMISSION_REVIEW_REQUIRED


# ---------------------------------------------------------------------------
# Test/review mode
# ---------------------------------------------------------------------------

def test_review_mode_defaults_on():
    cfg = config.AppConfig()
    assert cfg.automation.review_mode is True


def test_review_mode_round_trips_through_config_toml(tmp_path):
    import tomllib
    cfg = config.AppConfig()
    cfg.teacher.onboarded = True
    cfg.automation.review_mode = True
    config.save_config(cfg)
    data = tomllib.loads((config.CONFIG_PATH).read_text(encoding="utf-8"))
    assert data["automation"]["review_mode"] is True
    # And a fresh load honours it (explicit False, then explicit True).
    cfg2 = config.AppConfig()
    cfg2.teacher.onboarded = True
    cfg2.automation.review_mode = False
    config.save_config(cfg2)
    cfg3 = config.get_config(reload=True)
    assert cfg3.automation.review_mode is False
    cfg3.automation.review_mode = True
    config.save_config(cfg3)
    assert config.get_config(reload=True).automation.review_mode is True


def _runner_with_publish(capture, *, cfg=None):
    from app.automation.runner import WorkflowRunner

    def fake_publish(coursework_id, course_id, ops):
        capture.extend(ops)
        return {"results": [{"status": "PUBLISHED"}]}

    return WorkflowRunner(config=cfg, publish=fake_publish)


import pytest


@pytest.fixture()
def auto_job_store(fresh_state):
    """Provide store access against the test DB (conftest gives a fresh db)."""
    from app.automation import store
    job = store.create_job(
        course_id="c-rm", coursework_id="cw-rm",
        assignment_title="Review-mode assignment",
        student_id="s-rm", student_name="Review Mode Student",
        submission_id="sub-rm", submission_version="v1")
    assert job is not None
    return job


def test_review_mode_grade_step_saves_draft_and_stops(monkeypatch, auto_job_store):
    captured_ops = []
    runner = _runner_with_publish(captured_ops)

    calls = {"verify": 0}

    def fake_verify(job, grade):
        calls["verify"] += 1

    monkeypatch.setattr(runner, "_verify_grade", fake_verify)
    monkeypatch.setattr(
        runner, "_grade_op",
        lambda job, op, grade: captured_ops.append((op, grade)))

    job = auto_job_store
    from app.automation import store as _store
    _store.set_status(job["id"], states.ATTACHMENT_VERIFIED, grade="8")
    job = _store.get(job["id"])
    runner._step_attachment_verified(job)
    refreshed = _store.get(job["id"])
    assert refreshed["status"] == states.ATTENTION_REQUIRED
    assert captured_ops == [("draft_grade", "8")]
    assert calls["verify"] == 1
    assert "NOT returned" in (refreshed.get("step_detail") or "")


def test_review_mode_blocks_publish_and_return_at_grade_verified(auto_job_store):
    from app.automation import store as _store
    runner = _runner_with_publish([])
    job = auto_job_store
    _store.set_status(job["id"], states.GRADE_VERIFIED, grade="7")
    refreshed = _store.get(job["id"])
    progressed = runner._step_grade_verified(refreshed)
    assert progressed is False
    final = _store.get(job["id"])
    assert final["status"] == states.ATTENTION_REQUIRED
    assert final.get("error_code") == "review_mode_await_teacher"


# ---------------------------------------------------------------------------
# Submission-quality validation (§9)
# ---------------------------------------------------------------------------

def _result(questions, earned=None, available=None):
    return {"questions": questions,
            "totals": {"earned": earned or 0, "available": available or 0}}


def test_valid_submission_passes():
    questions = [
        {"question_number": str(n), "max_marks": 5, "awarded_marks": 3,
         "student_answer": "something", "status": "VALID"}
        for n in range(1, 10)
    ]
    verdict = assess_submission_quality(_result(questions), {})
    assert verdict["quality"] == QUALITY_VALID
    assert verdict["review"] is False


def test_blank_submission_is_flagged():
    questions = [{"question_number": str(n), "max_marks": 2,
                  "awarded_marks": 0, "student_answer": "", "status": "VALID"}
                 for n in range(1, 20)]
    questions[0]["student_answer"] = " "     # whitespace only
    verdict = assess_submission_quality(_result(questions), {})
    assert verdict["quality"] == QUALITY_REVIEW
    assert any("attempted" in r or "blank" in r for r in verdict["reasons"])


def test_unreadable_scan_is_flagged():
    questions = [{"question_number": "1", "max_marks": 3, "awarded_marks": 0,
                  "student_answer": "unreadable handwriting",
                  "status": "VALID"}
                 for _ in range(6)]
    verdict = assess_submission_quality(_result(questions), {})
    assert verdict["quality"] == QUALITY_REVIEW
    assert any("read" in r for r in verdict["reasons"])


def test_wrong_assignment_via_blueprint_extras_is_flagged():
    questions = [{"question_number": str(n), "max_marks": 5,
                  "awarded_marks": 2, "student_answer": "ans",
                  "status": "REVIEW"} for n in range(1, 7)]
    verification = {"blueprint": {"extra_questions": ["1", "2", "3", "4"]},
                    "review_questions": []}
    verdict = assess_submission_quality(_result(questions), verification)
    assert verdict["quality"] == QUALITY_REVIEW
    assert any("wrong assignment" in r for r in verdict["reasons"])


def test_mass_unmarked_tech_gaps_are_flagged():
    questions = [{"question_number": str(n), "max_marks": 3,
                  "awarded_marks": 1, "student_answer": "a", "status": "REVIEW"}
                 for n in range(1, 5)]
    verification = {"review_questions": ["1", "2", "3", "4"]}
    verdict = assess_submission_quality(_result(questions), verification)
    assert verdict["quality"] == QUALITY_REVIEW
    assert any("UNMARKED" in r or "technical gap" in r
               for r in verdict["reasons"])


def test_submission_review_confidence_is_untrusted():
    # The guard's review rule must treat SUBMISSION_REVIEW_REQUIRED as needing
    # a teacher — otherwise the flag would not block publish.
    from app.engine.result_guard import OverwriteDecision
    decision = OverwriteDecision(
        action="insert", reason="x",
        confidence=SUBMISSION_REVIEW_REQUIRED)
    assert decision.review_required is True


# ---------------------------------------------------------------------------
# Independent second correction: a DIFFERENT model leads the second pass
# ---------------------------------------------------------------------------

def test_second_pass_rotates_to_a_different_model():
    from app.engine.double_check import second_pass_models
    chain = ["m1", "m2", "m3"]
    assert second_pass_models(chain, "m1") == ["m2", "m3", "m1"]
    assert second_pass_models(chain, "m2") == ["m3", "m1", "m2"]


def test_second_pass_keeps_fallback_when_only_one_model():
    from app.engine.double_check import second_pass_models
    assert second_pass_models(["m1"], "m1") == ["m1"]
    assert second_pass_models([], "m1") == []
    # Unknown first model: chain unchanged.
    assert second_pass_models(["m1", "m2"], "zzz") == ["m1", "m2"]


def test_review_gate_runs_before_review_mode_draft(auto_job_store, monkeypatch):
    """An engine-flagged (review_required) result must never receive even a
    draft grade in review mode — it lands in requires_review instead."""
    import time as _time
    from app.automation import store as _store
    from app import db as _db
    job = auto_job_store
    _store.set_status(job["id"], states.ATTACHMENT_VERIFIED, grade="9",
                      result_id="res-review-gate")
    _db.execute(
        "INSERT INTO correction_jobs(id, submission_id, coursework_id, "
        "course_id, student_id, student_name, status, created_at, updated_at) "
        "VALUES('job-review-gate','sub-gate','cw-rm','c-rm','s-gate',"
        "'Gate Student','COMPLETED',?,?)",
        (_time.time(), _time.time()))
    _db.execute(
        "INSERT OR REPLACE INTO results(id, job_id, coursework_id, "
        "submission_id, student_id, student_name, earned, available, "
        "percentage, review_required, result_json, created_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,1,?,?)",
        ("res-review-gate", "job-review-gate", "cw-rm", "sub-gate",
         "s-gate", "Gate Student", 90, 100, 90.0,
         '{"questions": []}', _time.time()))
    captured_ops = []
    runner = _runner_with_publish(captured_ops)
    job = _store.get(job["id"])
    from app.automation.runner import StepFailure
    with pytest.raises(StepFailure) as excinfo:
        runner._step_attachment_verified(job)
    assert excinfo.value.code == "requires_review"
    assert captured_ops == []          # no grade operation at all — not even a draft
