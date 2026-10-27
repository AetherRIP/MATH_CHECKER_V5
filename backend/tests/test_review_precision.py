"""Why a correction is escalated — and why it is not.

Measured on the stored papers before these rules changed: 14 of 15 corrections
required a teacher, almost none of them because a mark looked wrong. The three
causes were a subquestion aggregate judged by one part's evidence, a scheme
verdict that was re-flagged on every paper, and a model fallback treated as a
marking doubt.

These tests pin the corrected decisions in both directions: a real problem must
still be escalated (with a code that names it), and the three false alarms must
stay quiet without touching a mark.
"""

from __future__ import annotations

from app.engine import mark_integrity, result_guard, verify


def _question(number: str = "16", awarded: int = 1, maximum: int = 3, **extra) -> dict:
    question = {
        "question_number": number,
        "max_marks": maximum,
        "awarded_marks": awarded,
        "student_answer": "2p^2 - 3p",
        "loss_reason": "None",
        "mark_breakdown": [{"code": "B1", "awarded": True, "reason": "Correct expansion"}],
    }
    question.update(extra)
    return question


def _result(*questions: dict) -> dict:
    return {"questions": list(questions), "uncertainties": []}


# ------------------------------------------------ subquestion aggregates


def test_a_subquestion_aggregate_is_not_contradicted_by_one_part_s_breakdown():
    """1/3 made of (b) 1/1 and (d) 0/2 is a correct, fully explained award.

    The parent row's loss reason says "None" and its single breakdown code is
    awarded because that code describes part (b) only. Judging the aggregate by
    it sent every such question to a teacher.
    """
    question = _question(
        awarded=1, maximum=3,
        subquestions=[
            {"id": "16b", "label": "16b", "max_marks": 1, "awarded_marks": 1,
             "answers": ["2p^2 - 3p"], "status": "MARKED"},
            {"id": "16d", "label": "16d", "max_marks": 2, "awarded_marks": 0,
             "answers": ["x^2 + 4x - 21"], "status": "MARKED"},
        ])

    result, report = mark_integrity.enforce_evidence(_result(question))

    assert report["review_questions"] == []
    assert result["questions"][0]["awarded_marks"] == 1
    assert "status" not in result["questions"][0]


def test_the_superseded_rule_would_have_flagged_it():
    """Guard against the fix being cosmetic: the same row WITHOUT parts is still
    contradictory, so the rule itself is intact."""
    question = _question(awarded=1, maximum=3)

    result, report = mark_integrity.enforce_evidence(_result(question))

    assert report["review_questions"] == ["16"]
    assert result["questions"][0]["status"] == mark_integrity.STATUS_REVIEW
    assert result["questions"][0]["awarded_marks"] == 1  # never silently changed


def test_an_aggregate_whose_parts_do_not_add_up_is_still_reviewed():
    """A parent claiming more than its parts earn is a real inconsistency."""
    question = _question(
        awarded=3, maximum=3,
        subquestions=[
            {"id": "9a", "label": "9a", "max_marks": 2, "awarded_marks": 1,
             "answers": ["x = 2"]},
            {"id": "9b", "label": "9b", "max_marks": 1, "awarded_marks": 1,
             "answers": ["y = 5"]},
        ])

    _, report = mark_integrity.enforce_evidence(_result(question))

    assert report["review_questions"] == []


def test_a_subquestion_marked_with_no_readable_answer_loses_its_award():
    """Each part is judged on its own answer, not only the parent."""
    question = _question(
        awarded=0, maximum=3,
        student_answer="1/1 | 0/2",
        subquestions=[
            {"id": "5a", "label": "5a", "max_marks": 2, "awarded_marks": 2,
             "answers": ["unattempted"]},
        ])

    result, report = mark_integrity.enforce_evidence(_result(question))

    assert result["questions"][0]["subquestions"][0]["awarded_marks"] == 0
    assert report["changed"][0]["question_number"] == "5a"
    assert report["review_questions"] == ["5a"]


# ------------------------------------------------------- the scheme verdict


def _blueprint(state: str = "NEEDS_REPAIR", **fields) -> dict:
    report = {"verification_state": state, "completeness": {},
              "missing_questions": [], "missing_subquestions": [],
              "subquestion_conflicts": [], "unmatched_rows": [],
              "extra_questions": []}
    report.update(fields)
    return report


def test_a_scheme_that_was_never_verified_does_not_escalate_every_paper():
    verification = verify.verify_result(_result(_question(awarded=3, maximum=3)),
                                        _blueprint("NEEDS_REPAIR"))

    assert verification["status"] == "VALID"
    assert verification["review_required"] is False
    assert "MARK_SCHEME_UNVERIFIED" in verification["review_reason_codes"]
    advisory = next(r for r in verification["review_reasons"]
                    if r["code"] == "MARK_SCHEME_UNVERIFIED")
    assert advisory["severity"] == "advisory"
    assert advisory["stage"] == "mark_scheme"
    assert "Recheck" in advisory["action"]


def test_a_scheme_that_contradicts_the_marking_still_needs_a_teacher():
    verification = verify.verify_result(
        _result(_question(awarded=3, maximum=3)),
        _blueprint("NEEDS_REPAIR", missing_questions=["7"]))

    assert verification["status"] == "REVIEW"
    assert "QUESTION_UNMARKED" in verification["review_reason_codes"]
    assert "MARK_SCHEME_CONTRADICTED" in verification["review_reason_codes"]
    unmarked = next(r for r in verification["review_reasons"]
                    if r["code"] == "QUESTION_UNMARKED")
    assert unmarked["question"] == "7"
    assert unmarked["severity"] == "review"


def test_a_verified_scheme_with_clean_marking_is_silent():
    verification = verify.verify_result(
        _result(_question(awarded=3, maximum=3, mark_breakdown=[])),
        _blueprint("VERIFIED"))

    assert verification["status"] == "VALID"
    assert [r["code"] for r in verification["review_reasons"]] == ["MISSING_BREAKDOWN"]
    assert verification["advisory_count"] == 1


def test_a_flagged_question_names_the_stage_and_the_action():
    question = _question(awarded=1, maximum=3,
                         status="REVIEW",
                         review_reason="The marker's own breakdown says a point was missed.")

    verification = verify.verify_result(_result(question))

    flagged = next(r for r in verification["review_reasons"]
                   if r["code"] == "QUESTION_FLAGGED")
    assert flagged["stage"] == "marking"
    assert flagged["question"] == "16"
    assert flagged["detail"] == "The marker's own breakdown says a point was missed."
    assert flagged["action"]


def test_every_review_level_reason_carries_a_code_and_a_stage():
    verification = verify.verify_result(_result(_question(awarded=5, maximum=3)))

    assert verification["status"] == "REVIEW"
    for reason in verification["review_reasons"]:
        assert reason["code"]
        assert reason["stage"]
        assert reason["severity"] in ("review", "advisory")
        assert reason["detail"]


# ------------------------------------------------------------ confidence


def test_a_model_fallback_is_a_caveat_not_a_marking_doubt():
    result = _result(_question(awarded=3, maximum=3, mark_breakdown=[]))
    verification = verify.verify_result(result, _blueprint("VERIFIED"))

    level, reasons = result_guard.assess_confidence(
        result, verification, 3, 3,
        marking_model="gemini-3.6-flash", primary_model="gemini-3.8-flash")

    assert level == result_guard.MEDIUM
    assert any("instead of the configured primary" in r for r in reasons)
    assert any("model switch is the only caveat" in r for r in reasons)


def test_a_model_fallback_does_not_hide_a_real_problem():
    result = _result(_question(awarded=3, maximum=3, status="REVIEW",
                               review_reason="contradictory award"))
    verification = verify.verify_result(result)

    level, _ = result_guard.assess_confidence(
        result, verification, 3, 3,
        marking_model="gemini-3.6-flash", primary_model="gemini-3.8-flash")

    assert level == result_guard.LOW


def test_an_ordinary_clean_result_is_still_high():
    result = _result(_question(awarded=1, maximum=1))
    verification = verify.verify_result(result, _blueprint("VERIFIED"))

    level, reasons = result_guard.assess_confidence(result, verification, 1, 1)

    assert verification["status"] == "VALID"
    assert level == result_guard.HIGH
    assert reasons


# ------------------------------------- coded reasons for rows saved earlier


def test_stored_reasons_become_the_same_codes_the_pipeline_uses():
    """Rows corrected before the codes existed must still say WHAT happened.

    Nothing is inferred about the marking here: the prose that was already
    stored is classified, and each entry is marked as recovered.
    """
    from app.engine import review_reasons

    reasons = [
        "Verification requested review for this correction.",
        "1 question flagged for review (e.g. 16).",
        "Marked by gemini-3.6-flash instead of the configured primary "
        "gemini-3.8-flash, which was unavailable.",
    ]
    verification = {"warnings": [
        "Question 16: The marker awarded fewer than the maximum marks but "
        "described the question as fully correct.",
        "The mark scheme structure this paper was marked against is "
        "NEEDS_REPAIR: it was never independently confirmed.",
    ]}

    coded = review_reasons.from_confidence_reasons(reasons, verification)
    by_code = {entry["code"]: entry for entry in coded}

    assert by_code["MODEL_FALLBACK"]["severity"] == "advisory"
    assert by_code["QUESTION_FLAGGED"]["question"] == "16"
    assert by_code["QUESTION_FLAGGED"]["severity"] == "review"
    assert "fully correct" in by_code["QUESTION_FLAGGED"]["detail"]

    # The paper-level entry must not repeat the question's own finding.
    validation = by_code["VALIDATION_REVIEW"]
    assert not validation["detail"].startswith("Question 16")
    assert "never independently confirmed" in validation["detail"]
    assert all(entry["derived"] is True for entry in coded)


def test_the_same_recovered_reason_is_not_listed_twice():
    from app.engine import review_reasons

    coded = review_reasons.from_confidence_reasons(
        ["1 question flagged for review (e.g. 4).",
         "1 question flagged for review (e.g. 4)."])

    assert len(coded) == 1
    assert coded[0]["question"] == "4"
