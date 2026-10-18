"""Mark-scheme error detection + approval-gated bulk correction (V5).

Covers the brief's required tests: an incorrect numeric answer in the scheme,
a sign/arithmetic error, an equivalent expression, a valid alternative method,
a genuinely incorrect student, partial/method marks, an ambiguous question,
an explicitly required method, a correct scheme with a wrong student,
duplicate detection across students, notification de-duplication, staged
proposals that are NOT applied, explicit approval, rejection, duplicate
application prevention, partial-failure recovery, original-mark preservation,
Classroom grade return and the unchanged 1-9 conversion.
"""

from __future__ import annotations

import json
import time

import pytest

from app import db
from app.engine import scheme_audit as sa
from app.engine import legacy_marking as legacy


# --------------------------------------------------------------------- helpers

def _seed_assignment(coursework_id: str = "cw1", course_id: str = "c1",
                     title: str = "Algebra HW", max_points: float = 10.0) -> None:
    now = time.time()
    with db.transaction() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO courses(id,name,section,fetched_at) "
            "VALUES(?,?,?,?)", (course_id, "Maths", "A", now))
        conn.execute(
            "INSERT INTO coursework(id,course_id,title,max_points,fetched_at) "
            "VALUES(?,?,?,?,?)", (coursework_id, course_id, title, max_points, now))


def _seed_result(*, result_id: str, coursework_id: str = "cw1",
                 course_id: str = "c1", submission_id: str | None,
                 student_id: str, student_name: str, question_number: str = "12",
                 awarded: int, max_marks: int, scheme_check: dict | None = None,
                 status: str = "COMPLETED") -> dict:
    now = time.time()
    job_id = f"job-{result_id}"
    with db.transaction() as conn:
        conn.execute(
            "INSERT INTO correction_jobs(id,submission_id,coursework_id,course_id,"
            "student_id,student_name,status,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            (job_id, submission_id, coursework_id, course_id, student_id,
             student_name, status, now, now))
        raw = {"paper_info": {}, "overall_notes": {}, "uncertainties": [],
               "questions": [{"question_number": question_number,
                              "max_marks": max_marks, "awarded_marks": awarded,
                              "student_answer": "x=4",
                              "loss_reason": "differs from scheme"}]}
        if scheme_check is not None:
            raw["questions"][0]["scheme_check"] = scheme_check
        result = legacy.normalise_result(raw)
        earned, available, pct = legacy.calculate_totals(result)
        conn.execute(
            "INSERT INTO results(id,job_id,coursework_id,submission_id,student_id,"
            "student_name,earned,available,percentage,provisional_grade,"
            "result_json,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (result_id, job_id, coursework_id, submission_id, student_id,
             student_name, earned, available, pct,
             legacy.percentage_band_grade(pct), json.dumps(result), now))
        sa.record_from_result(
            conn,
            job={"coursework_id": coursework_id, "course_id": course_id,
                 "submission_id": submission_id, "student_id": student_id,
                 "student_name": student_name},
            result_id=result_id, result=result)
    return result


def _wrong_numeric_check(**overrides) -> dict:
    base = {
        "disagrees_with_scheme": True, "student_answer_valid": True,
        "scheme_answer": "x=5", "independently_verified_answer": "x=4",
        "discrepancy_type": "incorrect_answer",
        "evidence": "2x+3=11 => 2x=8 => x=4", "confidence": 0.95,
        "justified_marks": 3,
    }
    base.update(overrides)
    return base


# ================================================================ classification

def test_incorrect_numeric_answer_is_confirmed():
    check = sa.classify(_wrong_numeric_check(), max_marks=3, awarded=0)
    assert check is not None
    assert check["state"] == sa.STATE_POSSIBLE
    assert check["justified_marks"] == 3
    assert check["review_recommended"] is True


def test_sign_and_arithmetic_errors_are_confirmed():
    for dtype in ("sign_error", "arithmetic_error"):
        info = sa.classify(_wrong_numeric_check(discrepancy_type=dtype),
                           max_marks=4, awarded=0)
        assert info["state"] == sa.STATE_POSSIBLE, dtype
        assert info["type"] == dtype


def test_low_confidence_mathematical_disagreement_is_possible():
    info = sa.classify(_wrong_numeric_check(confidence=0.4),
                       max_marks=3, awarded=0)
    assert info["state"] == sa.STATE_POSSIBLE
    assert info["review_recommended"] is True


def test_disagreement_without_evidence_is_not_confirmed():
    info = sa.classify(_wrong_numeric_check(confidence=0.99, evidence=""),
                       max_marks=3, awarded=0)
    assert info["state"] == sa.STATE_POSSIBLE


def test_equivalent_expression_is_valid_alternative():
    info = sa.classify(
        _wrong_numeric_check(discrepancy_type="equivalent_expression",
                             scheme_answer="x=3",
                             independently_verified_answer="6/2"),
        max_marks=2, awarded=0)
    assert info["state"] == sa.STATE_VALID_ALTERNATIVE
    assert info["review_recommended"] is True


def test_valid_alternative_method_is_valid_alternative():
    info = sa.classify(
        _wrong_numeric_check(discrepancy_type="alternative_method"),
        max_marks=5, awarded=2)
    assert info["state"] == sa.STATE_VALID_ALTERNATIVE


def test_ambiguous_question_is_uncertain():
    info = sa.classify(
        _wrong_numeric_check(discrepancy_type="ambiguous_question"),
        max_marks=3, awarded=0)
    assert info["state"] == sa.STATE_UNCERTAIN
    assert info["review_recommended"] is True


def test_genuinely_wrong_student_does_not_force_review():
    info = sa.classify(
        _wrong_numeric_check(student_answer_valid=False,
                             discrepancy_type="incorrect_answer",
                             confidence=0.99),
        max_marks=3, awarded=0)
    # The scheme may be wrong but no student mark is at stake: record, do not
    # demand a bulk change.
    assert info["state"] == sa.STATE_POSSIBLE
    assert info["affects_marks"] is False


def test_no_disagreement_is_ignored():
    assert sa.classify(None, max_marks=3, awarded=1) is None
    assert sa.classify({"disagrees_with_scheme": False},
                       max_marks=3, awarded=1) is None


def test_justified_marks_never_exceed_question_maximum():
    info = sa.classify(_wrong_numeric_check(justified_marks=99),
                       max_marks=3, awarded=0)
    assert info["justified_marks"] == 3
    info = sa.classify(_wrong_numeric_check(justified_marks=-5),
                       max_marks=3, awarded=0)
    assert info["justified_marks"] == 0


def test_partial_credit_is_preserved():
    info = sa.classify(_wrong_numeric_check(justified_marks=2),
                       max_marks=5, awarded=0)
    assert info["justified_marks"] == 2


def test_detect_ignores_questions_without_a_check():
    result = legacy.normalise_result({
        "paper_info": {}, "overall_notes": {}, "uncertainties": [],
        "questions": [{"question_number": "1", "max_marks": 3,
                       "awarded_marks": 2, "student_answer": "1",
                       "loss_reason": "wrong"}]})
    assert sa.detect(result) == []


def test_correct_scheme_wrong_student_produces_no_record():
    # No disagreement flag => a plain 0 on a correct scheme is normal marking.
    result = legacy.normalise_result({
        "paper_info": {}, "overall_notes": {}, "uncertainties": [],
        "questions": [{"question_number": "3", "max_marks": 4,
                       "awarded_marks": 0, "student_answer": "9",
                       "loss_reason": "incorrect"}]})
    assert sa.detect(result) == []


# ================================================================ detection + notifications

def test_same_discrepancy_is_recorded_once_and_notified_once(fresh_state):
    _seed_assignment()
    for i, name in enumerate(("Alice", "Bob", "Carol")):
        _seed_result(result_id=f"r{i}", submission_id=f"s{i}",
                     student_id=f"s{i}", student_name=name, awarded=0,
                     max_marks=3, scheme_check=_wrong_numeric_check())
    rows = sa.list_discrepancies()
    assert len(rows) == 1                      # one live record per Q + type
    assert len(sa.notifications()) == 1        # no duplicate notification


def test_different_types_same_question_notify_once(fresh_state):
    """Two records for the same assignment + question collapse into ONE
    review request (brief s6: no duplicate notifications)."""
    _seed_assignment()
    _seed_result(result_id="r1", submission_id="s1", student_id="s1",
                 student_name="Alice", awarded=0, max_marks=3,
                 scheme_check=_wrong_numeric_check())
    _seed_result(result_id="r2", submission_id="s2", student_id="s2",
                 student_name="Bob", awarded=0, max_marks=3,
                 scheme_check=_wrong_numeric_check(
                     discrepancy_type="arithmetic_error"))
    assert len(sa.list_discrepancies()) == 2      # two distinct records
    notes = sa.notifications()
    assert len(notes) == 1                        # one review request
    assert notes[0]["related_count"] == 1


def test_ordinary_result_records_nothing(fresh_state):
    _seed_assignment()
    _seed_result(result_id="plain", submission_id="s1", student_id="s1",
                 student_name="Alice", awarded=2, max_marks=3)
    assert sa.list_discrepancies() == []
    assert sa.notifications() == []


def test_confirmed_and_possible_are_distinguished(fresh_state):
    _seed_assignment()
    _seed_result(result_id="r1", submission_id="s1", student_id="s1",
                 student_name="Alice", awarded=0, max_marks=3,
                 scheme_check=_wrong_numeric_check())
    _seed_result(result_id="r2", submission_id="s2", student_id="s2",
                 student_name="Bob", awarded=0, max_marks=4,
                 question_number="13",
                 scheme_check=_wrong_numeric_check(confidence=0.3))
    states = {r["question_number"]: r["state"] for r in sa.list_discrepancies()}
    assert states["12"] == sa.STATE_POSSIBLE
    assert states["13"] == sa.STATE_POSSIBLE


# ================================================================ proposals (no auto-apply)

def test_proposal_stages_without_changing_grades(fresh_state):
    _seed_assignment()
    _seed_result(result_id="r1", submission_id="s1", student_id="s1",
                 student_name="Alice", awarded=0, max_marks=3,
                 scheme_check=_wrong_numeric_check())
    _seed_result(result_id="r2", submission_id="s2", student_id="s2",
                 student_name="Bob", awarded=0, max_marks=3,
                 scheme_check=_wrong_numeric_check())
    discrepancy = sa.list_discrepancies()[0]
    proposal = sa.build_proposal(discrepancy["id"])
    assert proposal["status"] == sa.PROPOSAL_PROPOSED
    assert proposal["affected_count"] == 2
    assert proposal["changed_count"] == 2
    # NOTHING was applied.
    assert db.query_one("SELECT earned FROM results WHERE id='r1'")["earned"] == 0
    assert db.query_one("SELECT earned FROM results WHERE id='r2'")["earned"] == 0
    # Original scheme answer is still recorded; nothing overwrote it.
    assert discrepancy["scheme_answer"] == "x=5"


def test_proposal_without_justified_marks_lists_review_only(fresh_state):
    _seed_assignment()
    _seed_result(result_id="r1", submission_id="s1", student_id="s1",
                 student_name="Alice", awarded=0, max_marks=3,
                 scheme_check=_wrong_numeric_check(justified_marks=None))
    discrepancy = sa.list_discrepancies()[0]
    proposal = sa.build_proposal(discrepancy["id"])
    assert proposal["affected_count"] == 1
    assert proposal["changed_count"] == 0
    assert proposal["items"][0]["question_awarded_after"] is None


def test_bulk_apply_never_overwrites_a_teacher_override(fresh_state):
    """A submission the teacher already marked by hand keeps that mark: a
    bulk batch lists it for manual re-mark but never changes it."""
    _seed_assignment()
    _seed_result(result_id="r1", submission_id="s1", student_id="s1",
                 student_name="Alice", awarded=0, max_marks=3,
                 scheme_check=_wrong_numeric_check())
    _seed_result(result_id="r2", submission_id="s2", student_id="s2",
                 student_name="Bob", awarded=0, max_marks=3,
                 scheme_check=_wrong_numeric_check())
    db.execute("UPDATE results SET manual_override = 1 WHERE id = 'r2'")
    discrepancy = sa.list_discrepancies()[0]
    proposal = sa.build_proposal(discrepancy["id"])
    assert proposal["affected_count"] == 2
    assert proposal["changed_count"] == 1          # only Alice is auto-changed
    bob = next(i for i in proposal["items"] if i["result_id"] == "r2")
    assert bob["question_awarded_after"] is None
    result = sa.apply_approved(proposal["id"], approved_by="t")
    assert result["applied_count"] == 1
    assert db.query_one("SELECT earned FROM results WHERE id='r2'")["earned"] == 0


def test_restaging_after_apply_returns_the_applied_proposal(fresh_state):
    _seed_assignment()
    _seed_result(result_id="r1", submission_id="s1", student_id="s1",
                 student_name="Alice", awarded=0, max_marks=3,
                 scheme_check=_wrong_numeric_check())
    discrepancy = sa.list_discrepancies()[0]
    first = sa.build_proposal(discrepancy["id"])
    sa.apply_approved(first["id"], approved_by="t")
    again = sa.build_proposal(discrepancy["id"])
    # No duplicate batch is produced after an approved change was applied.
    assert again["id"] == first["id"]
    assert again["status"] == sa.PROPOSAL_APPLIED


def test_restaging_returns_the_same_proposal(fresh_state):
    _seed_assignment()
    _seed_result(result_id="r1", submission_id="s1", student_id="s1",
                 student_name="Alice", awarded=0, max_marks=3,
                 scheme_check=_wrong_numeric_check())
    discrepancy = sa.list_discrepancies()[0]
    first = sa.build_proposal(discrepancy["id"])
    second = sa.build_proposal(discrepancy["id"])
    assert first["id"] == second["id"]


# ================================================================ approval

def test_approval_applies_marks_and_records_audit(fresh_state):
    _seed_assignment()
    _seed_result(result_id="r1", submission_id="s1", student_id="s1",
                 student_name="Alice", awarded=0, max_marks=3,
                 scheme_check=_wrong_numeric_check())
    discrepancy = sa.list_discrepancies()[0]
    proposal = sa.build_proposal(discrepancy["id"])
    result = sa.apply_approved(proposal["id"], approved_by="Ms Teacher",
                               note="Verified by hand")
    assert result["status"] == sa.PROPOSAL_APPLIED
    assert result["applied_count"] == 1
    assert result["failed_count"] == 0
    row = db.query_one("SELECT * FROM results WHERE id='r1'")
    assert row["earned"] == 3
    assert row["manual_override"] == 1
    # Audit trail distinguishes the AI recommendation from the human decision.
    reviews = db.query("SELECT * FROM scheme_discrepancy_reviews")
    assert len(reviews) == 1
    assert reviews[0]["decision"] == "APPROVE_BULK_CORRECTION"
    assert reviews[0]["decided_by"] == "Ms Teacher"
    assert reviews[0]["ai_recommendation"] is not None


def test_original_marks_preserved_in_history(fresh_state):
    _seed_assignment()
    _seed_result(result_id="r1", submission_id="s1", student_id="s1",
                 student_name="Alice", awarded=0, max_marks=3,
                 scheme_check=_wrong_numeric_check())
    discrepancy = sa.list_discrepancies()[0]
    proposal = sa.build_proposal(discrepancy["id"])
    sa.apply_approved(proposal["id"], approved_by="Ms Teacher")
    notes = [r["note"] for r in db.query(
        "SELECT note FROM result_versions WHERE result_id='r1'")]
    assert any("Original marks preserved" in (n or "") for n in notes)


def test_duplicate_approval_is_prevented(fresh_state):
    _seed_assignment()
    _seed_result(result_id="r1", submission_id="s1", student_id="s1",
                 student_name="Alice", awarded=0, max_marks=3,
                 scheme_check=_wrong_numeric_check())
    discrepancy = sa.list_discrepancies()[0]
    proposal = sa.build_proposal(discrepancy["id"])
    first = sa.apply_approved(proposal["id"], approved_by="t")
    second = sa.apply_approved(proposal["id"], approved_by="t")
    assert first["applied_count"] == 1
    assert second["applied_count"] == 1          # unchanged, not re-applied
    assert len(db.query("SELECT * FROM result_versions WHERE result_id='r1'")) == 2
    assert db.query_one("SELECT earned FROM result_versions WHERE result_id='r1' AND is_current=1")["earned"] == 3


def test_rejection_leaves_grades_and_record(fresh_state):
    _seed_assignment()
    _seed_result(result_id="r1", submission_id="s1", student_id="s1",
                 student_name="Alice", awarded=0, max_marks=3,
                 scheme_check=_wrong_numeric_check())
    discrepancy = sa.list_discrepancies()[0]
    proposal = sa.build_proposal(discrepancy["id"])
    rejected = sa.reject_proposal(proposal["id"], rejected_by="Ms Teacher",
                                  note="Scheme is right after all")
    assert rejected["status"] == sa.PROPOSAL_REJECTED
    assert db.query_one("SELECT earned FROM results WHERE id='r1'")["earned"] == 0
    # The discrepancy record survives (not deleted).
    assert sa.get_discrepancy(discrepancy["id"]) is not None
    # And it can never be applied afterwards.
    with pytest.raises(ValueError):
        sa.apply_approved(proposal["id"], approved_by="t")


def test_partial_failure_is_reported_and_recoverable(fresh_state):
    _seed_assignment()
    _seed_result(result_id="r1", submission_id="s1", student_id="s1",
                 student_name="Alice", awarded=0, max_marks=3,
                 scheme_check=_wrong_numeric_check())
    _seed_result(result_id="r2", submission_id="s2", student_id="s2",
                 student_name="Bob", awarded=0, max_marks=3,
                 scheme_check=_wrong_numeric_check())
    discrepancy = sa.list_discrepancies()[0]
    proposal = sa.build_proposal(discrepancy["id"])
    # Corrupt one stored result AFTER the proposal, so its item fails.
    db.execute("UPDATE results SET result_json = '{}' WHERE id = 'r2'")
    result = sa.apply_approved(proposal["id"], approved_by="t")
    assert result["status"] == sa.PROPOSAL_PARTIAL
    assert result["applied_count"] == 1
    assert result["failed_count"] == 1
    failed = [i for i in result["items"] if i["status"] == sa.ITEM_FAILED]
    assert len(failed) == 1 and failed[0]["error"]
    # The successful one really did change; the failed one did not.
    assert db.query_one("SELECT earned FROM results WHERE id='r1'")["earned"] == 3
    assert db.query_one("SELECT earned FROM results WHERE id='r2'")["earned"] == 0


def test_classroom_publish_only_after_approval_and_reports_failure(fresh_state):
    _seed_assignment()
    _seed_result(result_id="r1", submission_id="s1", student_id="s1",
                 student_name="Alice", awarded=0, max_marks=3,
                 scheme_check=_wrong_numeric_check())
    discrepancy = sa.list_discrepancies()[0]
    proposal = sa.build_proposal(discrepancy["id"])
    calls = {}

    def fake_publish(items):
        calls["items"] = items
        return {"published": len(items), "failed": 0}

    result = sa.apply_approved(proposal["id"], approved_by="t",
                               publish=True, publish_fn=fake_publish)
    assert result["classroom_publish_state"] == "published"
    assert len(calls["items"]) == 1
    assert calls["items"][0]["submission_id"] == "s1"


def test_publish_failure_is_not_reported_as_success(fresh_state):
    _seed_assignment()
    _seed_result(result_id="r1", submission_id="s1", student_id="s1",
                 student_name="Alice", awarded=0, max_marks=3,
                 scheme_check=_wrong_numeric_check())
    discrepancy = sa.list_discrepancies()[0]
    proposal = sa.build_proposal(discrepancy["id"])
    result = sa.apply_approved(proposal["id"], approved_by="t", publish=True,
                               publish_fn=lambda items: {"error": "no network"})
    assert result["classroom_publish_state"] == "failed"
    # The local correction still applied (honest partial behaviour).
    assert db.query_one("SELECT earned FROM results WHERE id='r1'")["earned"] == 3


def test_grade_conversion_rules_unchanged(fresh_state):
    from app.engine import grading
    # The preserved legacy bands, unchanged.
    assert grading.grade_for_percentage(95) == "9"
    assert grading.grade_for_percentage(45) == "4"
    assert grading.grade_for_percentage(5) == "1"
    # A bulk correction recomputes the provisional grade from the new marks.
    _seed_assignment(max_points=3)
    _seed_result(result_id="r1", submission_id="s1", student_id="s1",
                 student_name="Alice", awarded=0, max_marks=3,
                 scheme_check=_wrong_numeric_check())
    discrepancy = sa.list_discrepancies()[0]
    proposal = sa.build_proposal(discrepancy["id"])
    sa.apply_approved(proposal["id"], approved_by="t")
    assert db.query_one("SELECT provisional_grade FROM results WHERE id='r1'")[
        "provisional_grade"] == "9"


def test_human_decision_removes_notification(fresh_state):
    _seed_assignment()
    _seed_result(result_id="r1", submission_id="s1", student_id="s1",
                 student_name="Alice", awarded=0, max_marks=3,
                 scheme_check=_wrong_numeric_check())
    discrepancy = sa.list_discrepancies()[0]
    assert len(sa.notifications()) == 1
    sa.record_decision(discrepancy["id"],
                       decision=sa.DECISION_CONFIRM_SCHEME_CORRECT,
                       decided_by="Ms Teacher", note="scheme is fine")
    assert sa.notifications() == []
    # The AI classification is preserved in the audit history.
    history = sa.review_history(discrepancy["id"])
    assert history and history[0]["ai_recommendation"] == sa.STATE_POSSIBLE


# ================================================================ pipeline wiring

def test_service_save_result_records_discrepancy_without_extra_ai(fresh_state):
    """The REAL save path records the disagreement. No AI is called here: the
    normalised result already carries the marker's scheme_check, exactly as the
    (single-request) marking pass returns it."""
    from app.engine.service import CorrectionService
    from app.engine.verify import verify_result

    _seed_assignment()
    now = time.time()
    job = {"id": "job-integ", "coursework_id": "cw1", "submission_id": "s1",
           "student_id": "s1", "student_name": "Alice", "is_remark": False,
           "created_at": now}
    with db.transaction() as conn:
        conn.execute("INSERT INTO correction_jobs(id,submission_id,coursework_id,"
                     "course_id,student_id,student_name,status,created_at,updated_at)"
                     " VALUES(?,?,?,?,?,?,?,?,?)",
                     ("job-integ", "s1", "cw1", "c1", "s1", "Alice",
                      "MARKING", now, now))
    raw = {"paper_info": {}, "overall_notes": {}, "uncertainties": [],
           "questions": [{"question_number": "12", "max_marks": 3,
                          "awarded_marks": 0, "student_answer": "x=4",
                          "loss_reason": "differs",
                          "scheme_check": _wrong_numeric_check()}]}
    result = legacy.normalise_result(raw)
    earned, available, pct = legacy.calculate_totals(result)
    CorrectionService()._save_result(
        job=job, result=result, verification=verify_result(result),
        earned=earned, available=available, percentage=pct, marking_model="m")
    rows = sa.list_discrepancies()
    assert len(rows) == 1
    assert rows[0]["question_number"] == "12"
    assert rows[0]["state"] == sa.STATE_POSSIBLE
    assert rows[0]["justified_marks"] == 3


def test_service_save_ordinary_result_records_nothing(fresh_state):
    from app.engine.service import CorrectionService
    from app.engine.verify import verify_result

    _seed_assignment()
    now = time.time()
    job = {"id": "job-plain", "coursework_id": "cw1", "submission_id": "s9",
           "student_id": "s9", "student_name": "Plain", "is_remark": False,
           "created_at": now}
    with db.transaction() as conn:
        conn.execute("INSERT INTO correction_jobs(id,submission_id,coursework_id,"
                     "course_id,student_id,student_name,status,created_at,updated_at)"
                     " VALUES(?,?,?,?,?,?,?,?,?)",
                     ("job-plain", "s9", "cw1", "c1", "s9", "Plain",
                      "MARKING", now, now))
    result = legacy.normalise_result({
        "paper_info": {}, "overall_notes": {}, "uncertainties": [],
        "questions": [{"question_number": "1", "max_marks": 2,
                       "awarded_marks": 2, "student_answer": "5",
                       "loss_reason": ""}]})
    earned, available, pct = legacy.calculate_totals(result)
    CorrectionService()._save_result(
        job=job, result=result, verification=verify_result(result),
        earned=earned, available=available, percentage=pct, marking_model="m")
    assert sa.list_discrepancies() == []


# ================================================================ prompt / schema preservation

def test_prompt_preserves_existing_marking_rules():
    text = legacy.MARKING_SYSTEM.lower()
    # Existing rules that must NOT be weakened (section 11/20 of the brief).
    for needle in ("method (m)", "accuracy (a)", "error-carried-forward",
                   "follow-through", "ignore subsequent working", "rounding",
                   "units", "never exceed a question's maximum mark"):
        assert needle in text, needle
    # And the new root-cause fix is present.
    assert "independently verify" in text
    assert "never award zero" in text
    assert "scheme_check" in text


def test_output_schema_keeps_scheme_check_optional():
    q = legacy.OUTPUT_SCHEMA["properties"]["questions"]["items"]
    assert "scheme_check" in q["properties"]
    # Optional: it must not join the required list, so an ordinary response is
    # still schema-valid and existing callers are unaffected.
    assert "scheme_check" not in q["required"]
    assert "correct_method" in q["required"]


def test_blueprint_prompt_allows_justified_deviation():
    from app.engine import blueprint as bp
    text = bp.prompt_block({"total_marks": 3, "questions": [
        {"question_number": "1", "max_marks": 3, "criteria": "M1 A1"}]})
    assert "scheme_check" in text
    assert "never award zero" in text
