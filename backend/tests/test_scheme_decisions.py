"""Per-question handling decisions for an assignment's mark scheme.

A teacher decides, for each question and sub-question, whether it is marked
automatically, checked by hand, or skipped. Three things must hold:

* the decision is stored against the scheme's OWN id, so a re-extraction that
  reorders questions cannot move it onto a different question;
* a skipped question is really out of marking — not silently a zero;
* every change is kept in history, including a change back to a value used
  before, and the uploaded scheme is never edited.
"""

import pytest

from app.engine import scheme_decisions as sd


def _blueprint():
    """A scheme with a bare part label and a nested one — both real shapes."""
    return {
        "questions": [
            {"question_number": "1", "max_marks": 3, "criteria": "", "page": 2,
             "parts": [
                 {"number": "a", "max_marks": 1, "answers": ["24 - 18y"], "page": 2},
                 {"number": "b", "max_marks": 2, "answers": ["y^2 + 8y - 20"], "page": 2},
             ]},
            {"question_number": "2", "max_marks": 1, "answers": ["10p - 15"], "page": 2},
            {"question_number": "3", "max_marks": 4, "criteria": "M1 A1 cao", "page": 3},
        ],
        "total_marks": 8,
    }


CW = "cw-decisions"


# --------------------------------------------------------------- the listing

def test_every_question_and_subquestion_is_listed_in_hierarchy():
    rows, orphans = sd.rows_for(_blueprint(), CW)

    assert [(r["question_label"], r["depth"]) for r in rows] == [
        ("1", 0), ("1(a)", 1), ("1(b)", 1),
        ("2", 0), ("3", 0),
    ]
    assert orphans == []
    labels = {r["question_label"]: r for r in rows}
    assert labels["1(a)"]["max_marks"] == 1
    assert labels["1(a)"]["answers"] == ["24 - 18y"]
    assert labels["1(a)"]["page"] == 2
    # A sub-question knows which question it belongs to.
    assert labels["1(a)"]["parent_key"] == "1"


def test_an_entry_with_no_answer_is_flagged_rather_than_invented():
    rows, _ = sd.rows_for(_blueprint(), CW)
    by_label = {r["question_label"]: r for r in rows}

    assert by_label["1(a)"]["extraction_status"] == sd.RELIABLE
    # Criteria but no answer: cannot be trusted for automatic marking.
    assert by_label["3"]["extraction_status"] == sd.CRITERIA_ONLY
    assert by_label["3"]["reliable"] is False
    assert by_label["3"]["answers"] == []


def test_every_entry_is_proposed_for_automatic_marking():
    """Marking is the default; an independent check is the teacher's exception.

    An unreachable answer must not silently turn a whole scheme into manual
    checks nobody asked for. It stays visible as the row's extraction status
    instead, and the teacher filters it and decides.
    """
    rows, _ = sd.rows_for(_blueprint(), CW)
    by_label = {r["question_label"]: r for r in rows}

    assert by_label["1(a)"]["decision"] == sd.AUTO
    assert by_label["1(a)"]["decided"] is False, "a proposal is not a decision"
    assert by_label["3"]["decision"] == sd.AUTO
    assert by_label["3"]["decided"] is False
    # Nothing is hidden: the row still says the extraction could not support it.
    assert by_label["3"]["reliable"] is False
    assert by_label["3"]["extraction_status"] == sd.CRITERIA_ONLY
    # And the teacher can still choose the other two handlings on that row.
    sd.set_decision(CW, "3", sd.MANUAL, blueprint=_blueprint())
    after, _ = sd.rows_for(_blueprint(), CW)
    assert {r["question_label"]: r for r in after}["3"]["decision"] == sd.MANUAL
    sd.set_decision(CW, "3", sd.SKIP, blueprint=_blueprint())
    skipped, _ = sd.rows_for(_blueprint(), CW)
    assert {r["question_label"]: r for r in skipped}["3"]["skipped"] is True


# -------------------------------------------------------------- the decisions

def test_a_decision_persists_and_reads_back():
    sd.set_decision(CW, "1(a)", sd.MANUAL, blueprint=_blueprint())

    stored = sd.decisions_for(CW)
    assert stored["1a"]["decision"] == sd.MANUAL
    assert stored["1a"]["question_label"] == "1(a)"

    rows, _ = sd.rows_for(_blueprint(), CW)
    row = next(r for r in rows if r["question_label"] == "1(a)")
    assert row["decision"] == sd.MANUAL
    assert row["decided"] is True
    assert row["label"] == sd.LABELS[sd.MANUAL]


def test_a_subquestion_id_is_matched_however_it_is_written():
    for spacing in ("1(a)", "1 a", "Q1(a)", "1a"):
        sd.set_decision(CW, spacing, sd.MANUAL, blueprint=_blueprint())

    assert set(sd.decisions_for(CW)) == {"1a"}


def test_an_unknown_decision_is_refused():
    with pytest.raises(ValueError, match="Unknown decision"):
        sd.set_decision(CW, "1", "PROBABLY", blueprint=_blueprint())


def test_a_question_the_scheme_does_not_contain_is_refused():
    with pytest.raises(ValueError, match="not in this mark scheme"):
        sd.set_decision(CW, "99", sd.SKIP, blueprint=_blueprint())


# ------------------------------------------------------------------- skipping

def test_skipping_a_question_really_takes_it_out_of_marking():
    from app.engine import scheme_exclusions as se

    sd.set_decision(CW, "1(b)", sd.SKIP, blueprint=_blueprint())

    assert "1b" in se.ignored(CW)
    trimmed, removed = se.filter_blueprint(_blueprint(), CW)
    # The part is gone from the question it belonged to...
    kept = next(q for q in trimmed["questions"] if q["question_number"] == "1")
    assert [p["number"] for p in kept["parts"]] == ["a"]
    # ...its marks leave the question's maximum...
    assert kept["max_marks"] == 1
    assert [r["question_number"] for r in removed] == ["1(b)"]
    # ...and they leave the paper total, so skipped marks are not a zero.
    assert trimmed["total_marks"] == 6


def test_skipping_a_whole_question_drops_it_and_its_subquestions():
    from app.engine import scheme_exclusions as se

    sd.set_decision(CW, "1", sd.SKIP, blueprint=_blueprint())

    trimmed, _ = se.filter_blueprint(_blueprint(), CW)
    assert [q["question_number"] for q in trimmed["questions"]] == ["2", "3"]
    assert trimmed["total_marks"] == 5


def test_restoring_a_skipped_question_puts_it_back_exactly():
    from app.engine import scheme_exclusions as se

    sd.set_decision(CW, "1(b)", sd.SKIP, blueprint=_blueprint())
    sd.set_decision(CW, "1(b)", sd.AUTO, blueprint=_blueprint())

    assert se.ignored(CW) == {}
    trimmed, _ = se.filter_blueprint(_blueprint(), CW)
    assert trimmed["total_marks"] == 8


def test_a_skipped_question_is_never_counted_as_earned_zero():
    """The summary reports skipped marks apart from marked marks."""
    sd.set_decision(CW, "1(b)", sd.SKIP, blueprint=_blueprint())
    rows, _ = sd.rows_for(_blueprint(), CW)
    stats = sd.summary(rows, _blueprint())

    assert stats["skipped"] == 1
    assert stats["skipped_marks"] == 2
    assert stats["marked_marks"] == 6
    # Every other row is proposed for automatic marking (question 3 included:
    # its handling is a proposal, and proposals are automatic). The skipped part
    # is NOT folded into that count — it is its own state.
    assert stats["automatic"] == 4
    assert stats["manual"] == 0


# --------------------------------------------------------------- answer edits

def test_editing_an_answer_keeps_the_extraction_intact():
    sd.set_answer(CW, "1(a)", "18y - 24", blueprint=_blueprint())

    rows, _ = sd.rows_for(_blueprint(), CW)
    row = next(r for r in rows if r["question_label"] == "1(a)")
    assert row["answer_text"] == "18y - 24"
    assert row["effective_answer"] == "18y - 24"
    assert row["answers"] == ["24 - 18y"], "the extracted answer must survive"

    # The blueprint object itself is untouched.
    assert _blueprint()["questions"][0]["parts"][0]["answers"] == ["24 - 18y"]


def test_clearing_an_edited_answer_restores_the_extracted_one():
    sd.set_answer(CW, "1(a)", "something else", blueprint=_blueprint())
    sd.set_answer(CW, "1(a)", None, blueprint=_blueprint())

    rows, _ = sd.rows_for(_blueprint(), CW)
    row = next(r for r in rows if r["question_label"] == "1(a)")
    assert row["answer_text"] is None
    assert row["effective_answer"] == "24 - 18y"


def test_an_edited_answer_does_not_silently_approve_the_question():
    """Editing the answer of an undecided question leaves it undecided."""
    sd.set_answer(CW, "3", "2x^2 + 19x + 24", blueprint=_blueprint())

    rows, _ = sd.rows_for(_blueprint(), CW)
    row = next(r for r in rows if r["question_label"] == "3")
    assert row["decided"] is False
    # It keeps the same proposed default every other row has.
    assert row["decision"] == sd.AUTO


# -------------------------------------------------------------- the history

def test_every_change_is_kept_with_both_sides():
    sd.set_decision(CW, "1(a)", sd.AUTO, blueprint=_blueprint())
    sd.set_decision(CW, "1(a)", sd.MANUAL, reason="scheme answer looks wrong",
                    blueprint=_blueprint())
    sd.set_decision(CW, "1(a)", sd.SKIP, blueprint=_blueprint())

    items = sd.history(CW, "1(a)")
    # Newest first, and the FIRST decision has no previous value.
    assert [(i["previous_decision"], i["new_decision"]) for i in items] == [
        (sd.MANUAL, sd.SKIP),
        (sd.AUTO, sd.MANUAL),
        (None, sd.AUTO),
    ]
    reasons = [i["reason"] for i in items]
    assert "scheme answer looks wrong" in reasons
    assert all(i["source"] == "HUMAN" for i in items)


def test_an_answer_edit_records_the_old_and_new_answer():
    sd.set_answer(CW, "1(a)", "first", blueprint=_blueprint())
    sd.set_answer(CW, "1(a)", "second", blueprint=_blueprint())

    items = [i for i in sd.history(CW, "1(a)") if i["action"] == "ANSWER"]
    assert [(i["previous_answer"], i["new_answer"]) for i in items] == [
        ("first", "second"),
        (None, "first"),
    ]


def test_changing_a_decision_back_is_recorded_rather_than_deduplicated():
    sd.set_decision(CW, "1(a)", sd.MANUAL, blueprint=_blueprint())
    sd.set_decision(CW, "1(a)", sd.AUTO, blueprint=_blueprint())
    sd.set_decision(CW, "1(a)", sd.MANUAL, blueprint=_blueprint())

    items = sd.history(CW, "1(a)")
    assert [(i["previous_decision"], i["new_decision"]) for i in items] == [
        (sd.AUTO, sd.MANUAL),
        (sd.MANUAL, sd.AUTO),
        (None, sd.MANUAL),
    ]


def test_a_repeated_identical_decision_does_not_pad_the_history():
    sd.set_decision(CW, "1(a)", sd.MANUAL, blueprint=_blueprint())
    sd.set_decision(CW, "1(a)", sd.MANUAL, blueprint=_blueprint())

    assert len(sd.history(CW, "1(a)")) == 1


def test_history_is_scoped_to_its_assignment():
    sd.set_decision(CW, "1(a)", sd.MANUAL, blueprint=_blueprint())
    sd.set_decision("other-cw", "1(a)", sd.AUTO, blueprint=_blueprint())

    assert len(sd.history(CW)) == 1
    assert sd.decisions_for("other-cw")["1a"]["decision"] == sd.AUTO
    assert sd.decisions_for(CW)["1a"]["decision"] == sd.MANUAL


# -------------------------------------------------------- re-parsing a scheme

def test_a_decision_whose_question_disappeared_is_reported_not_reattached():
    """A re-parse that renames a question must not hand its decision to another."""
    sd.set_decision(CW, "1(b)", sd.SKIP, blueprint=_blueprint())

    reparsed = {
        "questions": [
            {"question_number": "1", "max_marks": 2, "page": 2,
             "parts": [{"number": "a", "max_marks": 2, "answers": ["x"]}]},
            {"question_number": "2", "max_marks": 1, "answers": ["10p - 15"]},
        ],
        "total_marks": 3,
    }
    rows, orphans = sd.rows_for(reparsed, CW)

    assert [o["question_key"] for o in orphans] == ["1b"]
    # No row picked the decision up by position.
    assert all(r["question_key"] != "1b" for r in rows)
    assert not any(r["skipped"] for r in rows if r["question_label"] == "1(a)")


def test_reparsing_with_the_same_ids_keeps_every_decision():
    sd.set_decision(CW, "1(a)", sd.MANUAL, blueprint=_blueprint())
    sd.set_decision(CW, "2", sd.SKIP, blueprint=_blueprint())

    rows, orphans = sd.rows_for(_blueprint(), CW)
    by_label = {r["question_label"]: r for r in rows}

    assert orphans == []
    assert by_label["1(a)"]["decision"] == sd.MANUAL
    assert by_label["2"]["skipped"] is True


# ----------------------------------------------------------------- finalising

def test_finalising_refuses_to_approve_undecided_questions_by_silence():
    with pytest.raises(ValueError, match="no decision"):
        sd.finalize(CW, blueprint=_blueprint())


def test_finalising_with_acknowledgement_records_it():
    result = sd.finalize(CW, acknowledge_unresolved=True, note="checked by hand",
                         blueprint=_blueprint())

    assert result["unresolved"] == 5
    assert result["acknowledged_unresolved"] == 5
    items = [i for i in sd.history(CW) if i["action"] == "FINALIZED"]
    assert len(items) == 1
    assert items[0]["reason"] == "checked by hand"


def test_a_manual_question_is_flagged_for_review_without_losing_its_marks():
    """Manual means "a person checks this", not "award nothing"."""
    result = {
        "questions": [
            {"question_number": "1", "awarded_marks": 3, "max_marks": 3},
            {"question_number": "1(a)", "awarded_marks": 1, "max_marks": 1},
            {"question_number": "2", "awarded_marks": 1, "max_marks": 1},
        ],
    }
    flagged_result, count = sd.mark_manual(result, {"1"})

    assert count == 2, "a manual parent flags its own row and its sub-question"
    by_id = {q["question_number"]: q for q in flagged_result["questions"]}
    assert by_id["1"]["manual_check"] is True
    assert by_id["1(a)"]["review_status"] == sd.MANUAL
    assert by_id["2"].get("manual_check") is None
    # The marks are untouched: a manual check does not invent a zero.
    assert sum(q["awarded_marks"] for q in flagged_result["questions"]) == 5


def test_only_real_decisions_are_treated_as_manual():
    """A proposed handling is not an instruction from the teacher."""
    rows, _ = sd.rows_for(_blueprint(), CW)
    proposed = next(r for r in rows if r["question_label"] == "3")
    assert proposed["decision"] == sd.AUTO and proposed["decided"] is False

    # Nothing was decided, so correction is given no manual keys.
    assert sd.manual_keys(CW) == set()
    assert sd.manual_keys(CW, _blueprint()) == set()

    # Once the teacher asks for an independent check, correction is told.
    sd.set_decision(CW, "3", sd.MANUAL, blueprint=_blueprint())
    assert sd.manual_keys(CW) == {"3"}
    assert sd.manual_keys(CW, _blueprint()) == {"3"}

    sd.set_decision(CW, "3", sd.MANUAL, blueprint=_blueprint())
    assert sd.manual_keys(CW) == {"3"}


def test_finalising_after_deciding_everything_needs_no_acknowledgement():
    for label in ("1", "1(a)", "1(b)", "2", "3"):
        sd.set_decision(CW, label, sd.AUTO, blueprint=_blueprint())

    result = sd.finalize(CW, blueprint=_blueprint())
    assert result["unresolved"] == 0
    assert result["automatic"] == 5
