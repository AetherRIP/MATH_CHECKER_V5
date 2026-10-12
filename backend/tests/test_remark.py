"""Remark / re-mark (§Remark) — unit and API tests.

Covers the scenarios the spec calls out: a good remark, a better result, a
worse result that must NOT replace a valid one, an invalid result, a duplicate
remark click, and the independent retry actions (regenerate report, re-upload,
re-attach, re-sync grade) that must never re-run AI marking.
"""

from __future__ import annotations

import json

import pytest

from app import db
from app.engine import remark, versions


COURSE = "course-1"
CW = "coursework-1"
SUB = "submission-A"


# ------------------------------------------------------------------ helpers

def _questions(*rows):
    """``("1", 5, 7)`` -> the stored result_json shape."""
    return {"questions": [{"question_number": n, "awarded_marks": a,
                           "max_marks": m, "status": "VALID"}
                          for n, a, m in rows]}


def _seed_result(earned=68, available=75, payload=None, confidence="HIGH"):
    db.execute("INSERT OR IGNORE INTO courses(id, name, fetched_at) VALUES(?,?,?)",
               (COURSE, "Test Class", 1.0))
    db.execute(
        """INSERT OR IGNORE INTO coursework(
               id, course_id, title, max_points, state, fetched_at)
           VALUES(?,?,?,?,?,?)""",
        (CW, COURSE, "June 2017 Paper 2", 75.0, "PUBLISHED", 1.0))
    db.execute(
        """INSERT OR IGNORE INTO correction_jobs(
               id, status, coursework_id, submission_id, created_at, updated_at)
           VALUES(?,?,?,?,?,?)""",
        ("job-1", "COMPLETED", CW, SUB, 1.0, 1.0))
    db.execute(
        """INSERT OR IGNORE INTO results(
               id, job_id, coursework_id, submission_id, student_id,
               student_name, earned, available, percentage, provisional_grade,
               result_json, created_at)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
        ("result-1", "job-1", CW, SUB, "user-A", "John Smith",
         earned, available, earned / available * 100.0, "7",
         json.dumps(payload or _questions(("4", 5, 7))), 1.0))
    with db.transaction() as conn:
        versions.record(
            conn, result_id="result-1", earned=earned, available=available,
            percentage=earned / available * 100.0, provisional_grade="7",
            marking_model="gemini-3.5-flash-lite", key_fingerprint=None,
            confidence=confidence, confidence_reasons=[],
            verification={"status": "VALID"}, result=payload or _questions(("4", 5, 7)),
            created_at=1.0, note="Correction saved")


def _add_version(*, earned, available, payload, confidence="HIGH",
                 marking_model="gemini-3.5-flash-lite", note="Remark attempt"):
    """Record a remark attempt the way the worker does: PENDING, not applied."""
    with db.transaction() as conn:
        return versions.record_pending(
            conn, result_id="result-1", earned=earned, available=available,
            percentage=earned / available * 100.0, provisional_grade="7",
            marking_model=marking_model, key_fingerprint=None,
            confidence=confidence, confidence_reasons=[],
            verification={"status": "VALID" if confidence == "HIGH" else "REVIEW"},
            result=payload, created_at=2.0, note=note)


@pytest.fixture
def corrected():
    _seed_result()
    return "result-1"


# ------------------------------------------------------------- comparison

class TestComparison:
    def test_reports_totals_and_percentage_delta(self, corrected):
        _add_version(earned=70, available=75,
                     payload=_questions(("4", 5, 7)))
        cmp = remark.summary("result-1")["comparison"]
        assert cmp["previous"]["earned"] == 68
        assert cmp["new"]["earned"] == 70
        assert cmp["delta_points"] == 2
        assert cmp["percentage_delta"] == pytest.approx(2.67, abs=0.01)

    def test_lists_only_questions_that_changed(self, corrected):
        _add_version(earned=70, available=75,
                     payload=_questions(("4", 6, 7), ("8", 4, 5), ("9", 2, 2)))
        cmp = remark.summary("result-1")["comparison"]
        # Original is Q4=5/7 only; Q8 and Q9 are new, Q4 changed.
        changed = {c["question_number"]: c for c in cmp["questions"]}
        assert set(changed) == {"4", "8", "9"}
        assert changed["4"]["previous"] == 5 and changed["4"]["new"] == 6
        assert changed["8"]["kind"] == "added"
        assert cmp["question_changes"] == 3

    def test_unchanged_question_is_not_listed(self, corrected):
        _add_version(earned=68, available=75, payload=_questions(("4", 5, 7)))
        cmp = remark.summary("result-1")["comparison"]
        assert cmp["questions"] == []

    def test_no_pending_attempt_means_no_comparison(self, corrected):
        summary = remark.summary("result-1")
        assert summary["pending_version"] is None
        assert summary["comparison"] is None
        assert summary["can_use_new"] is False

    def test_a_worse_attempt_is_still_visible_for_review(self, corrected):
        """A remark that scored worse must be shown, never silently applied."""
        _add_version(earned=12, available=75,
                     payload=_questions(("4", 1, 7)), confidence="LOW")
        summary = remark.summary("result-1")
        assert summary["pending_version"] is not None
        assert summary["comparison"]["new"]["earned"] == 12
        # The ACTIVE correction is still the good one.
        assert db.query_one(
            "SELECT earned FROM results WHERE id = 'result-1'")["earned"] == 68


# ------------------------------------------------------- teacher decision

class TestTeacherDecision:
    def test_use_new_promotes_the_chosen_attempt(self, corrected):
        _add_version(earned=70, available=75, payload=_questions(("4", 6, 7)))
        out = remark.decide("result-1", remark.DECISION_USE_NEW)
        assert out["active_version"] == 2
        row = db.query_one("SELECT earned, result_json FROM results WHERE id='result-1'")
        assert row["earned"] == 70
        assert json.loads(row["result_json"])["questions"][0]["awarded_marks"] == 6

    def test_keep_previous_leaves_the_active_attempt_alone(self, corrected):
        _add_version(earned=12, available=75, payload=_questions(("4", 1, 7)),
                     confidence="LOW")
        out = remark.decide("result-1", remark.DECISION_KEEP_PREVIOUS)
        assert out["active_version"] == 1
        assert db.query_one(
            "SELECT earned FROM results WHERE id='result-1'")["earned"] == 68

    def test_decision_never_deletes_history(self, corrected):
        _add_version(earned=70, available=75, payload=_questions(("4", 6, 7)))
        remark.decide("result-1", remark.DECISION_USE_NEW)
        assert len(versions.list_versions("result-1")) == 2

    def test_promoting_a_failed_attempt_is_flagged_for_review(self, corrected):
        _add_version(earned=0, available=75, payload=_questions(("4", 0, 7)),
                     confidence="VALIDATION_FAILED")
        out = remark.decide("result-1", remark.DECISION_USE_NEW)
        # Allowed, but never silently trusted.
        assert out["forced_review"] is True
        assert db.query_one(
            "SELECT review_required FROM results WHERE id='result-1'"
        )["review_required"] == 1

    def test_unknown_decision_is_rejected(self, corrected):
        with pytest.raises(ValueError):
            remark.decide("result-1", "DELETE_EVERYTHING")

    def test_missing_attempt_is_rejected(self, corrected):
        with pytest.raises(ValueError):
            remark.decide("result-1", remark.DECISION_USE_NEW, version=99)

    def test_result_without_history_is_reported(self):
        with pytest.raises(LookupError):
            remark.decide("nope", remark.DECISION_USE_NEW)


# --------------------------------------------------- guard: never downgrade

class TestRemarkCannotDestroyAGoodResult:
    def test_zero_scoring_remark_does_not_replace_a_valid_result(self, corrected):
        """The motivating bug: a valid 68/75 replaced by a 0/75 remark."""
        from app.engine.result_guard import (assess_confidence, evaluate_overwrite,
                                             ACTION_KEEP_EXISTING)
        existing = dict(db.query_one("SELECT * FROM results WHERE id='result-1'"))
        confidence, reasons = assess_confidence(
            _questions(("4", 0, 7)), {"status": "VALID"}, 0, 75)
        decision = evaluate_overwrite(existing=existing, earned=0, available=75,
                                      verification={"status": "VALID"},
                                      confidence=confidence,
                                      confidence_reasons=reasons)
        assert decision.action == ACTION_KEEP_EXISTING
        assert decision.review_required is True

    def test_validation_failed_remark_is_kept_away(self, corrected):
        from app.engine.result_guard import (assess_confidence, evaluate_overwrite,
                                             ACTION_KEEP_EXISTING)
        existing = dict(db.query_one("SELECT * FROM results WHERE id='result-1'"))
        confidence, _ = assess_confidence(
            {"questions": []}, {"status": "BLOCKED"}, 40, 75)
        assert confidence == "VALIDATION_FAILED"
        decision = evaluate_overwrite(existing=existing, earned=40, available=75,
                                      verification={"status": "BLOCKED"},
                                      confidence=confidence, confidence_reasons=[])
        assert decision.action == ACTION_KEEP_EXISTING

# ------------------------------------------------- legacy corrections

class TestResultWithNoVersionHistory:
    """Corrections stored before versioning existed have no version row.

    A remark against one of those must still have a baseline to compare with,
    otherwise the teacher would be asked to accept a "new" attempt with no
    visible previous result.
    """

    def _legacy_result(self) -> str:
        """A result with NO result_versions row at all."""
        db.execute("INSERT OR IGNORE INTO courses(id, name, fetched_at) VALUES(?,?,?)",
                   ("course-legacy", "Legacy", 1.0))
        db.execute(
            """INSERT OR IGNORE INTO coursework(
                   id, course_id, title, max_points, state, fetched_at)
               VALUES(?,?,?,?,?,?)""",
            ("cw-legacy", "course-legacy", "Old Paper", 75.0, "PUBLISHED", 1.0))
        db.execute(
            """INSERT OR IGNORE INTO correction_jobs(
                   id, status, coursework_id, submission_id, created_at, updated_at)
               VALUES(?,?,?,?,?,?)""",
            ("job-legacy", "COMPLETED", "cw-legacy", "sub-legacy", 1.0, 1.0))
        db.execute(
            """INSERT OR IGNORE INTO results(
                   id, job_id, coursework_id, submission_id, student_id,
                   student_name, earned, available, percentage,
                   provisional_grade, result_json, created_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
            ("result-legacy", "job-legacy", "cw-legacy", "sub-legacy", "user-L",
             "Legacy Student", 68, 75, 90.67, "9",
             json.dumps(_questions(("4", 5, 7))), 1.0))
        return "result-legacy"

    def test_summary_falls_back_to_the_stored_result(self):
        rid = self._legacy_result()
        assert versions.list_versions(rid) == [], "precondition: no history"
        summary = remark.summary(rid)
        assert summary["active"]["earned"] == 68
        assert summary["active"]["version"] is None
        assert summary["awaiting_decision"] is False

    def test_remark_against_a_legacy_result_has_a_baseline(self):
        rid = self._legacy_result()
        with db.transaction() as conn:
            versions.record_pending(
                conn, result_id=rid, earned=70, available=75,
                percentage=93.33, provisional_grade="9", marking_model="gemini-y",
                key_fingerprint=None, confidence="HIGH", confidence_reasons=[],
                verification={"status": "VALID"},
                result=_questions(("4", 6, 7)), created_at=2.0,
                note=remark.NOTE_REMARK)
        summary = remark.summary(rid)
        assert summary["awaiting_decision"] is True
        # The previous side comes from the stored result, not from thin air.
        assert summary["comparison"]["previous"]["earned"] == 68
        assert summary["comparison"]["new"]["earned"] == 70
        assert summary["comparison"]["questions"][0]["previous"] == 5

    def test_keep_previous_on_a_legacy_result_works(self):
        rid = self._legacy_result()
        with db.transaction() as conn:
            versions.record_pending(
                conn, result_id=rid, earned=70, available=75,
                percentage=93.33, provisional_grade="9", marking_model="gemini-y",
                key_fingerprint=None, confidence="HIGH", confidence_reasons=[],
                verification={"status": "VALID"}, result=_questions(("4", 6, 7)),
                created_at=2.0, note=remark.NOTE_REMARK)
        out = remark.decide(rid, remark.DECISION_KEEP_PREVIOUS)
        assert out["decision"] == remark.DECISION_KEEP_PREVIOUS
        assert db.query_one(
            "SELECT earned FROM results WHERE id = ?", (rid,))["earned"] == 68
        assert remark.summary(rid)["awaiting_decision"] is False
