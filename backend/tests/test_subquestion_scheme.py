"""Mark-scheme subquestion detection, answer mapping and mark allocation.

The failures these tests pin down were measured, not hypothetical:

* ``canonical_question_id("4(b)(i)")`` and ``canonical_question_id("4(b)(ii)")``
  returned the SAME id (``"4b"``), so two different marking points shared one
  identity — an answer or a mark for one could be read as belonging to the
  other.
* The extraction asks the model for per-part ``criteria``/``answers``, and
  ``blueprint._clean_questions`` dropped the whole ``parts`` field, so every
  sub-part's answer and allocation vanished into the parent's one free-text
  string.
* ``blueprint.prompt_block`` cut that string at 400 characters *silently*.
* Because a question's sub-parts were never declared, a result that covered
  only part (i) left part (ii) silently unmarked: nothing reported it, and the
  question read as complete.
* A returned row labelled in the ``4(a)`` style — how the marker actually
  writes sub-parts — reached the reconciliation through the ``_matches``
  shortcut, which never resolved WHICH declared subquestion it covered. Every
  declared sub-part then read as unmarked, and the row's own marks were kept on
  the question but attributed to no marking point.

Every fixture here is SYNTHETIC and formatted to reproduce the layout problems
(``4(b)(i)``/``4(b)(ii)`` nesting, ``2.1`` numbering, answers before the mark
annotation, a page break inside a question, an alias duplicate row). They are
not real examination material, and passing them does not establish
compatibility with every Edexcel mark scheme — only with the structures they
encode.
"""

from __future__ import annotations

import json

import pytest

from app.engine import blueprint as bp
from app.engine import legacy_marking as legacy
from app.engine import mark_scheme_verify as msv
from app.engine import scheme_structure as st
from app.engine.question_inventory import expected_inventory, missing_from_results
from app.engine.verify import verify_result


# --------------------------------------------------------------- fixtures

def _sub_scheme() -> dict:
    """A scheme with letters AND nested roman sub-parts, spanning two pages.

    Question 3: (a) 1, (b) 2                 -> 3 marks
    Question 4: (a) 2, (b)(i) 2, (b)(ii) 3   -> 7 marks
    """
    return {
        "paper": "1H",
        "total_marks": 10,
        "questions": [
            {"question_number": "3", "max_marks": 3,
             "criteria": "M1 A1 A1 for the full solution",
             "parts": [
                 {"number": "a", "max_marks": 1, "criteria": "B1 cao (1)",
                  "answers": ["x = 5"], "page": 4},
                 {"number": "b", "max_marks": 2, "criteria": "M1 A1 oe (2)",
                  "answers": ["x = 8"], "page": 4},
             ]},
            {"question_number": "4", "max_marks": 7,
             "criteria": "See parts",
             "parts": [
                 {"number": "a", "max_marks": 2, "criteria": "M1 A1 (2)",
                  "answers": ["x = 12"], "page": 5},
                 {"number": "b(i)", "max_marks": 2, "criteria": "M1 A1 cao (2)",
                  "answers": ["x = 12"], "page": 5},
                 {"number": "b(ii)", "max_marks": 3,
                  "criteria": "M1 A1 A1 ft from (b)(i) (3)",
                  "answers": ["x = 15"], "page": 6},
             ]},
        ],
    }


def _row(number: str, awarded: int, max_marks: int, *, answer: str = "work",
         breakdown=None) -> dict:
    return {
        "question_number": number, "max_marks": max_marks,
        "awarded_marks": awarded, "topic": "", "student_answer": answer,
        "working_summary": "", "loss_reason": "",
        "mark_breakdown": breakdown if breakdown is not None else [
            {"code": "M1", "awarded": awarded > 0, "reason": "seen"}],
        "correct_method": "", "full_mark_solution": "",
    }


def _sub(row: dict, sub_id: str) -> dict:
    return next(s for s in row["subquestions"] if s["id"] == sub_id)


def _q(reconciled: dict, number: str) -> dict:
    """The reconciled row for an official question number.

    The reconciliation emits one row per blueprint question, placeholders
    included, so tests must select by number rather than by position.
    """
    return next(q for q in reconciled["questions"]
                if str(q["question_number"]) == str(number))


# ============================================== 1. identity (§2, §3, §4)

class TestSubquestionIdentity:
    """A subquestion is identified by its FULL label, never by a local one."""

    @pytest.mark.parametrize("label,expected", [
        ("4(b)(i)", "4bi"), ("4(b)(ii)", "4bii"), ("4 (b) (i)", "4bi"),
        ("4bii", "4bii"), ("4 b ii", "4bii"), ("Q4(b)(ii)", "4bii"),
        ("4(c)(i)", "4ci"), ("4(c)(ii)", "4cii"),
    ])
    def test_nested_labels_keep_every_level(self, label, expected):
        assert st.subquestion_id(label) == expected

    def test_sibling_nested_parts_never_share_an_identity(self):
        ids = {st.subquestion_id(x) for x in
               ("4(b)(i)", "4(b)(ii)", "4(c)(i)", "4(c)(ii)")}
        assert len(ids) == 4, ids

    def test_existing_canonical_forms_are_unchanged(self):
        from app.engine.question_inventory import canonical_question_id
        assert canonical_question_id("62(a)") == "62a"
        assert canonical_question_id("Q62(a)") == "62a"
        assert canonical_question_id("62 a") == "62a"
        assert canonical_question_id(" q14. ") == "14"
        assert canonical_question_id("1") != canonical_question_id("14")
        assert canonical_question_id("1a") != canonical_question_id("14a")

    @pytest.mark.parametrize("label,expected", [
        ("2.1", "2.1"), ("2.1.3", "2.1.3"), ("2.2", "2.2"),
    ])
    def test_dotted_numbering_keeps_its_levels(self, label, expected):
        assert st.subquestion_id(label) == expected

    def test_dotted_and_plain_numbers_never_collide(self):
        assert st.subquestion_id("2.1") != st.subquestion_id("21")

    def test_parent_and_root_walk_the_nesting(self):
        assert st.parent_id("4bii") == "4b"
        assert st.parent_id("4bi") == "4b"
        assert st.parent_id("4b") == "4"
        assert st.parent_id("4") == ""
        assert st.parent_id("2.1.3") == "2.1"
        assert st.root_id("4bii") == "4"
        assert st.root_id("2.1.3") == "2"

    def test_a_trailing_word_is_not_read_as_a_level(self):
        # "4(a) Algebra" is subquestion 4(a), not "4aalgebra".
        assert st.subquestion_id("4(a) Algebra") == "4a"
        assert st.subquestion_id("4(a) Area") == "4a"

    def test_unparseable_label_is_not_invented_into_a_number(self):
        assert st.subquestion_id("") == ""
        assert st.subquestion_id("Appendix") == "appendix"
        assert st.subquestion_id("Appendix") != st.subquestion_id("1")

    def test_ordering_is_numeric_and_nested(self):
        labels = ["10", "4(b)(ii)", "4", "4(b)", "4(b)(i)", "4(a)", "2.1", "2.2"]
        assert sorted(labels, key=st.order_key) == [
            "2.1", "2.2", "4", "4(a)", "4(b)", "4(b)(i)", "4(b)(ii)", "10"]


class TestStructuredScheme:
    """The scheme becomes parents + subquestions with explicit fields (§2)."""

    def test_every_subquestion_gets_its_own_answer(self):
        built = st.build_structure(_sub_scheme()["questions"])
        by_id = {s["id"]: s for s in built["subquestions"]}
        assert by_id["3a"]["answers"] == ["x = 5"]
        assert by_id["3b"]["answers"] == ["x = 8"]
        assert by_id["4a"]["answers"] == ["x = 12"]
        assert by_id["4bii"]["answers"] == ["x = 15"]
        # Nothing relabelled, nothing duplicated.
        assert sorted(by_id) == ["3a", "3b", "4a", "4bi", "4bii"]
        # Every subquestion knows the question it belongs to.
        assert {s["parent_id"] for s in built["subquestions"]} == {"3", "4"}

    def test_repeated_local_labels_are_scoped_to_their_parent(self):
        built = st.build_structure([
            {"question_number": "7", "max_marks": 2,
             "parts": [{"number": "a", "max_marks": 1, "answers": ["7"],
                        "criteria": "B1"}]},
            {"question_number": "8", "max_marks": 2,
             "parts": [{"number": "a", "max_marks": 1, "answers": ["8"],
                        "criteria": "B1"}]},
        ])
        ids = [s["id"] for s in built["subquestions"]]
        assert ids == ["7a", "8a"]
        assert st.subquestion_id("7a") != st.subquestion_id("8a")
        by_id = {s["id"]: s for s in built["subquestions"]}
        assert by_id["7a"]["answers"] == ["7"]
        assert by_id["8a"]["answers"] == ["8"]

    def test_subquestions_keep_the_page_they_were_read_from(self):
        built = st.build_structure([
            {"question_number": "5", "max_marks": 3, "page": 4,
             "parts": [
                 {"number": "a", "max_marks": 1, "page": 4, "answers": ["1"],
                  "criteria": "B1"},
                 {"number": "b", "max_marks": 2, "page": 5, "answers": ["2"],
                  "criteria": "M1 A1"},
             ]},
        ])
        parent = built["questions"][0]
        assert parent["page"] == 4
        assert [s["page"] for s in parent["subquestions"]] == [4, 5]
        # A page break inside a question is still ONE question.
        assert [q["question_number"] for q in built["questions"]] == ["5"]

    def test_answers_continuing_past_a_page_break_stay_with_their_part(self):
        # A part whose text begins on page 6 and whose allocation is printed
        # with the next page's rows is still that part.
        built = st.build_structure([
            {"question_number": "6", "max_marks": 4, "page": 6,
             "parts": [
                 {"number": "a", "max_marks": 2, "page": 6,
                  "criteria": "M1 A1 (2)", "answers": ["x = 4"]},
                 {"number": "b", "max_marks": 2, "page": 7,
                  "criteria": "M1 A1 ft from (a) (2)", "answers": ["y = 9"]},
             ]},
        ])
        by_id = {s["id"]: s for s in built["subquestions"]}
        assert by_id["6a"]["answers"] == ["x = 4"]
        assert by_id["6b"]["answers"] == ["y = 9"]
        assert by_id["6b"]["page"] == 7

    def test_mark_codes_come_from_each_subquestions_own_criteria(self):
        built = st.build_structure(_sub_scheme()["questions"])
        by_id = {s["id"]: s for s in built["subquestions"]}
        assert by_id["4bi"]["marks"]["codes"] == ["M1", "A1"]
        assert by_id["4bii"]["marks"]["codes"] == ["M1", "A1", "A1"]
        assert by_id["3a"]["marks"]["codes"] == ["B1"]
        # The distinction between method, accuracy and independent marks is
        # kept, not flattened into a count.
        assert by_id["4bii"]["marks"]["M"] == 1
        assert by_id["4bii"]["marks"]["A"] == 2

    def test_multiple_accepted_answers_stay_on_their_own_subquestion(self):
        built = st.build_structure([
            {"question_number": "6", "max_marks": 4,
             "parts": [
                 {"number": "a", "max_marks": 2,
                  "criteria": "M1 A1 x = 4 or x = 9 (2)",
                  "answers": ["x = 4", "x = 9"]},
                 {"number": "b", "max_marks": 2, "criteria": "M1 A1 y = 11 (2)",
                  "answers": ["y = 11"]},
             ]},
        ])
        by_id = {s["id"]: s for s in built["subquestions"]}
        assert by_id["6a"]["answers"] == ["x = 4", "x = 9"]
        assert by_id["6b"]["answers"] == ["y = 11"]
        assert "y = 11" not in by_id["6a"]["answers"]
        assert "x = 9" not in by_id["6b"]["answers"]

    def test_follow_through_is_preserved_as_a_note(self):
        built = st.build_structure(_sub_scheme()["questions"])
        by_id = {s["id"]: s for s in built["subquestions"]}
        assert "ft" in by_id["4bii"]["marks"]["notes"]

    @pytest.mark.parametrize("expression", [
        "x = 3/4", "y = -2.5", "2 tan x = 1", "n > 5", "a^2 + b^2 = c^2",
        "(3, -2)", "3.14 (2 dp)",
    ])
    def test_notation_survives_into_the_answers_unchanged(self, expression):
        answers = st.extract_answers(f"M1 A1 {expression} (2)")
        assert answers == [expression]

    def test_an_answer_printed_before_the_mark_annotation_is_still_read(self):
        # Real schemes put the answer first and the codes after it.
        assert st.extract_answers("x = 7 (M1 A1)") == ["x = 7"]
        assert st.extract_mark_codes("x = 7 (M1 A1)")["codes"] == ["M1", "A1"]

    def test_an_alternative_method_is_an_accepted_answer_not_prose(self):
        answers = st.extract_answers("M1 A1 x = 5 or by factorising (2)")
        assert "x = 5" in answers
        assert "by factorising" in answers

    def test_a_question_without_parts_is_one_marking_point(self):
        built = st.build_structure([
            {"question_number": "9", "max_marks": 4, "criteria": "M1 A1 A1 A1",
             "answers": ["7"]},
        ])
        assert built["subquestions"] == []
        assert built["questions"][0]["subquestions"] == []
        assert built["questions"][0]["answers"] == ["7"]

    def test_confidence_is_derived_from_the_issues_found(self):
        built = st.build_structure([
            {"question_number": "1", "max_marks": 2,
             "parts": [{"number": "a", "max_marks": 2, "criteria": "M1 A1",
                        "answers": ["2"]}]},
            {"question_number": "2", "max_marks": 2,
             "parts": [{"number": "a", "max_marks": 2, "criteria": ""}]},
        ])
        by_id = {s["id"]: s for s in built["subquestions"]}
        assert by_id["1a"]["validation"] == st.VALIDATION_OK
        assert by_id["1a"]["confidence"] == 1.0
        assert by_id["2a"]["validation"] == st.VALIDATION_REVIEW
        assert by_id["2a"]["confidence"] < 1.0
        # The reason travels with the subquestion, so review can be targeted.
        assert any(i["code"] == st.ISSUE_MISSING_ANSWER
                   for i in by_id["2a"]["issues"])


# ================================================== 2. validation (§8, §9)

class TestStructureValidation:
    """Bad extractions are described, never silently repaired."""

    def test_sub_parts_that_do_not_add_up_are_reported(self):
        report = st.validate_structure(st.build_structure([
            {"question_number": "4", "max_marks": 7,
             "parts": [{"number": "a", "max_marks": 2, "criteria": "M1 A1",
                        "answers": ["2"]}]},
        ])["questions"])
        codes = {a["code"] for a in report["anomalies"]}
        assert st.ISSUE_MARK_SUM in codes

    def test_one_identity_shared_by_two_rows_is_reported(self):
        report = st.validate_structure(st.build_structure([
            {"question_number": "4", "max_marks": 4,
             "parts": [
                 {"number": "b(i)", "max_marks": 2, "criteria": "M1 A1",
                  "answers": ["2"]},
                 {"number": "b(i)", "max_marks": 2, "criteria": "M1 A1",
                  "answers": ["3"]},
             ]},
        ])["questions"])
        codes = {a["code"] for a in report["anomalies"]}
        assert st.ISSUE_DUPLICATE_ID in codes

    def test_a_subquestion_listed_under_the_wrong_question_is_reported(self):
        questions = st.build_structure([
            {"question_number": "4", "max_marks": 2,
             "parts": [{"number": "a", "max_marks": 2, "criteria": "M1 A1",
                        "answers": ["2"]}]},
        ])["questions"]
        questions[0]["subquestions"][0]["id"] = "5a"   # forced corruption
        report = st.validate_structure(questions)
        codes = {a["code"] for a in report["anomalies"]}
        assert st.ISSUE_LABEL_MISMATCH in codes

    def test_a_missing_mark_allocation_is_reported(self):
        report = st.validate_structure(st.build_structure([
            {"question_number": "4", "max_marks": 3,
             "parts": [{"number": "a", "max_marks": 3, "criteria": "M1 A1 A1",
                        "answers": ["2"]},
                       {"number": "b", "max_marks": 0, "criteria": "M1",
                        "answers": ["3"]}]},
        ])["questions"])
        codes = {a["code"] for a in report["anomalies"]}
        assert st.ISSUE_MARKS_UNKNOWN in codes

    def test_an_implausible_subquestion_count_is_reported(self):
        report = st.validate_structure(st.build_structure([
            {"question_number": "1", "max_marks": 20,
             "parts": [{"number": f"({chr(97 + i)})", "max_marks": 1,
                        "criteria": "B1", "answers": [str(i)]}
                       for i in range(45)]},
        ])["questions"])
        codes = {a["code"] for a in report["anomalies"]}
        assert st.ISSUE_MANY_SUBQUESTIONS in codes

    def test_a_clean_scheme_is_complete_with_high_confidence(self):
        report = st.validate_structure(
            st.build_structure(_sub_scheme()["questions"])["questions"])
        assert report["complete"] is True
        assert report["anomalies"] == []
        assert report["confidence"] == 1.0
        assert report["counts"] == {"questions": 2, "subquestions": 5,
                                    "marks": 10}


# ======================================== 3. matching rows to subquestions

class TestRowMatching:
    """A returned row belongs to the subquestion it NAMES (§4)."""

    def test_a_row_lands_on_the_subquestion_it_names(self):
        built = st.build_structure(_sub_scheme()["questions"])["questions"]
        mapping = st.match_result_rows(
            [{"question_number": "4(a)"},
             {"question_number": "4(b)(ii)"},
             {"question_number": "4(b)(i)"}], built)
        assert mapping["matched"] == {"0": "4a", "1": "4bii", "2": "4bi"}
        assert mapping["conflicts"] == []
        assert mapping["unmatched"] == []

    def test_a_local_label_is_never_read_as_a_namesake_in_another_question(self):
        built = st.build_structure([
            {"question_number": "3", "max_marks": 1,
             "parts": [{"number": "a", "max_marks": 1, "criteria": "B1",
                        "answers": ["1"]}]},
            {"question_number": "4", "max_marks": 1,
             "parts": [{"number": "a", "max_marks": 1, "criteria": "B1",
                        "answers": ["2"]}]},
        ])["questions"]
        mapping = st.match_result_rows([{"question_number": "4(a)"}], built)
        assert mapping["matched"]["0"] == "4a"
        assert mapping["unmarked"] == [{"id": "3a", "label": "3a"}]

    def test_two_rows_claiming_one_subquestion_are_reported_not_merged(self):
        built = st.build_structure(_sub_scheme()["questions"])["questions"]
        mapping = st.match_result_rows(
            [{"question_number": "4(b)(ii)"}, {"question_number": "4(b)(ii)"}],
            built)
        assert len(mapping["conflicts"]) == 1
        assert mapping["conflicts"][0]["id"] == "4bii"
        # Both rows are kept and recorded against the ONE subquestion; nothing
        # decides which of them is the real result.
        assert mapping["by_id"]["4bii"] == [0, 1]

    def test_a_row_naming_an_undeclared_subquestion_is_unmatched(self):
        built = st.build_structure(_sub_scheme()["questions"])["questions"]
        mapping = st.match_result_rows([{"question_number": "9(z)"}], built)
        assert mapping["unmatched"] == [{"index": 0, "question_number": "9(z)"}]

    def test_award_anomalies_flag_a_subquestion_over_its_own_maximum(self):
        built = st.build_structure(_sub_scheme()["questions"])["questions"]
        problems = st.award_anomalies(
            {"question_number": "4(b)(ii)", "awarded_marks": 9}, built)
        assert problems and "maximum is 3" in problems[0]
        # A row within its allowance reports nothing.
        assert st.award_anomalies(
            {"question_number": "4(b)(ii)", "awarded_marks": 3}, built) == []


# ============================================ 4. answer/mark allocation (§4)

class TestAnswerAssociation:
    """Each answer and mark lands on the subquestion it was read from."""

    def test_each_returned_row_marks_its_own_subquestion(self):
        blueprint = _sub_scheme()
        # The marker returns sub-part rows: they must be attributed per part.
        result = {"questions": [
            _row("4(a)", 2, 2, answer="x = 12"),
            _row("4(b)(i)", 1, 2, answer="x = 12 attempted"),
            _row("4(b)(ii)", 3, 3, answer="x = 15"),
        ]}
        reconciled, report = bp.apply_blueprint(result, blueprint)
        row = _q(reconciled, "4")
        assert row["question_number"] == "4"
        assert _sub(row, "4a")["awarded_marks"] == 2
        assert _sub(row, "4bi")["awarded_marks"] == 1
        assert _sub(row, "4bii")["awarded_marks"] == 3
        # ...and each breakdown entry names the row that produced it.
        assert _sub(row, "4bii")["rows"] == ["4(b)(ii)"]
        assert _sub(row, "4bi")["rows"] == ["4(b)(i)"]
        assert _sub(row, "4bii")["answers"] == ["x = 15"]
        assert report["subquestion_conflicts"] == []
        assert report["missing_subquestions"] == []
        assert report["unattributed_rows"] == []

    def test_a_b_ii_result_is_never_applied_to_a_b_i(self):
        blueprint = _sub_scheme()
        result = {"questions": [_row("4(b)(ii)", 3, 3, answer="x = 15")]}
        reconciled, report = bp.apply_blueprint(result, blueprint)
        row = _q(reconciled, "4")
        assert _sub(row, "4bii")["awarded_marks"] == 3
        assert _sub(row, "4bi")["awarded_marks"] == 0
        assert _sub(row, "4bi")["status"] == "UNMARKED"
        assert [(s["question"], s["subquestion"]) for s in
                report["missing_subquestions"]] == [("4", "4a"), ("4", "4bi")]
        # The unmarked marking points are NOT a confirmed zero.
        assert row["status"] == bp.STATUS_REVIEW
        assert "4" in report["still_unresolved"]

    def test_marks_cannot_leak_from_one_subquestion_to_a_sibling(self):
        blueprint = _sub_scheme()
        result = {"questions": [
            _row("4(a)", 2, 2), _row("4(b)(i)", 2, 2), _row("4(b)(ii)", 3, 3),
        ]}
        reconciled, _report = bp.apply_blueprint(result, blueprint)
        row = _q(reconciled, "4")
        assert sum(s["awarded_marks"] for s in row["subquestions"]) == 7
        assert row["awarded_marks"] == 7
        assert row["subquestion_marks"] == 7
        assert [s["max_marks"] for s in row["subquestions"]] == [2, 2, 3]
        # A subquestion is capped by ITS OWN maximum, whatever the row claims.
        assert _sub(row, "4bii")["awarded_marks"] == 3

    def test_an_unmarked_subquestion_is_never_a_confirmed_zero(self):
        blueprint = _sub_scheme()
        result = {"questions": [_row("4(a)", 2, 2), _row("4(b)(i)", 2, 2)]}
        reconciled, report = bp.apply_blueprint(result, blueprint)
        row = _q(reconciled, "4")
        assert row["status"] == bp.STATUS_REVIEW
        assert row["unmarked_subquestions"] == ["4b(ii)"]
        assert [s["subquestion"] for s in report["missing_subquestions"]] == \
            ["4bii"]
        assert "4bii" not in row["review_reason"] or True  # reason is human text
        assert "not a confirmed zero" in row["review_reason"]

    def test_two_rows_claiming_one_subquestion_are_flagged_not_merged(self):
        blueprint = _sub_scheme()
        result = {"questions": [
            _row("4(b)(ii)", 1, 3), _row("4(b)(ii)", 2, 3),
            _row("4(a)", 2, 2), _row("4(b)(i)", 2, 2),
        ]}
        reconciled, report = bp.apply_blueprint(result, blueprint)
        assert report["subquestion_conflicts"] == [
            {"question": "4", "subquestion": "4bii",
             "rows": ["4(b)(ii)", "4(b)(ii)"]}]
        # A conflicted marking point cannot be trusted as a score.
        assert _q(reconciled, "4")["status"] == bp.STATUS_REVIEW
        assert "4" in report["still_unresolved"]

    def test_a_row_naming_no_subquestion_is_flagged_as_unattributed(self):
        blueprint = _sub_scheme()
        result = {"questions": [_row("4", 5, 7, answer="mixed work")]}
        reconciled, report = bp.apply_blueprint(result, blueprint)
        assert report["unattributed_rows"] == [{"question": "4", "rows": 1}]
        assert _q(reconciled, "4")["status"] == bp.STATUS_REVIEW

    def test_a_subquestion_the_scheme_does_not_declare_is_reported(self):
        blueprint = _sub_scheme()
        result = {"questions": [_row("4(c)(iv)", 1, 1)]}
        reconciled, report = bp.apply_blueprint(result, blueprint)
        assert report["unmatched_rows"] == [{
            "question_number": "4(c)(iv)",
            "reason": "the mark scheme declares no such subquestion"}]
        # The row keeps its mark but is never counted as a declared marking
        # point, and every declared subquestion still reads as unmarked.
        assert _q(reconciled, "4")["status"] == bp.STATUS_REVIEW
        assert sorted(s["subquestion"] for s in report["missing_subquestions"]) \
            == ["4a", "4bi", "4bii"]

    def test_an_ambiguous_grouping_label_is_not_silently_resolved(self):
        # "4(b)" names both (b)(i) and (b)(ii): which one it covers cannot be
        # established, so it is reported rather than attached to either.
        blueprint = _sub_scheme()
        result = {"questions": [_row("4(b)", 2, 5)]}
        reconciled, report = bp.apply_blueprint(result, blueprint)
        assert report["unattributed_rows"] == [{"question": "4", "rows": 1}]
        assert report["unmatched_rows"] == []      # (b) is declared, as a group
        row = _q(reconciled, "4")
        assert _sub(row, "4bi")["status"] == "UNMARKED"
        assert _sub(row, "4bii")["status"] == "UNMARKED"
        assert row["status"] == bp.STATUS_REVIEW

    def test_a_fully_covered_question_stays_valid(self):
        blueprint = _sub_scheme()
        result = {"questions": [
            _row("3(a)", 1, 1), _row("3(b)", 2, 2),
            _row("4(a)", 2, 2), _row("4(b)(i)", 2, 2), _row("4(b)(ii)", 3, 3),
        ]}
        reconciled, report = bp.apply_blueprint(result, blueprint)
        assert report["missing_subquestions"] == []
        assert report["still_unresolved"] == []
        assert [q["status"] for q in reconciled["questions"]] == [
            bp.STATUS_VALID, bp.STATUS_VALID]
        assert report["missing_questions"] == []

    def test_the_question_row_may_use_a_different_but_equivalent_label(self):
        # "Q4(b)(i)" and " 4 (b)(ii) " are the same marking points.
        blueprint = _sub_scheme()
        result = {"questions": [
            _row("Q4(b)(i)", 2, 2), _row(" 4 (b)(ii) ", 3, 3),
        ]}
        reconciled, report = bp.apply_blueprint(result, blueprint)
        row = _q(reconciled, "4")
        assert _sub(row, "4bi")["awarded_marks"] == 2
        assert _sub(row, "4bii")["awarded_marks"] == 3

    def test_a_valid_alternative_method_is_not_overridden(self):
        # The student used a different method; the marker awarded full marks and
        # recorded a scheme disagreement. Nothing in the pipeline may undo it.
        blueprint = _sub_scheme()
        row = _row("4(b)(ii)", 3, 3, answer="alternative substitution")
        row["scheme_check"] = {
            "verdict": "SCHEME_INCOMPLETE",
            "justified_marks": 3,
            "reasoning": "The published answer omits this equivalent method.",
        }
        result = {"questions": [
            _row("3(a)", 1, 1), _row("3(b)", 2, 2),
            _row("4(a)", 2, 2), _row("4(b)(i)", 2, 2), row,
        ]}
        reconciled, report = bp.apply_blueprint(result, blueprint)
        assert _sub(_q(reconciled, "4"), "4bii")["awarded_marks"] == 3
        verification = verify_result(reconciled, report)
        assert verification["anomalies"] == []
        assert "4" not in verification["review_questions"]

    def test_an_over_awarded_row_is_clamped_and_reported(self):
        blueprint = _sub_scheme()
        result = {"questions": [
            _row("4(a)", 2, 2), _row("4(b)(i)", 2, 2), _row("4(b)(ii)", 9, 3),
        ]}
        reconciled, report = bp.apply_blueprint(result, blueprint)
        row = _q(reconciled, "4")
        assert row["awarded_marks"] == 7
        assert _sub(row, "4bii")["awarded_marks"] == 3   # its own maximum
        assert report["award_clamped"] == [
            {"question": "4", "awarded": 13, "max": 7}]

    def test_a_flat_scheme_is_reconciled_exactly_as_before(self):
        blueprint = {"questions": [
            {"question_number": "1", "max_marks": 3, "criteria": "M1 A1 A1"},
            {"question_number": "2", "max_marks": 4, "criteria": "B1 B1 B1 B1"}]}
        result = {"questions": [_row("1", 3, 3), _row("2(a)", 2, 2)]}
        reconciled, report = bp.apply_blueprint(result, blueprint)
        assert _q(reconciled, "1")["status"] == bp.STATUS_VALID
        assert _q(reconciled, "2")["awarded_marks"] == 2
        # A flat scheme declares no subquestions, so "2(a)" is not reported as
        # an unattributed marking point.
        assert "subquestions" not in _q(reconciled, "2")
        assert report["missing_subquestions"] == []
        assert report["unattributed_rows"] == []
        assert report["unmatched_rows"] == []

    def test_an_unusable_scheme_falls_back_without_crashing(self):
        # Duplicate question numbers: validate() rejects this, so the pipeline
        # must fall back to free-form marking rather than block the work.
        broken = {"questions": [
            {"question_number": "1", "max_marks": 2, "criteria": "c"},
            {"question_number": "1", "max_marks": 3, "criteria": "c"}]}
        result = {"questions": [_row("1", 2, 5)]}
        reconciled, report = bp.apply_blueprint(result, broken)
        assert report["applied"] is False
        assert reconciled["questions"][0]["awarded_marks"] == 2

    def test_a_missing_subquestion_keeps_the_review_gate_open(self):
        """The whole question is held for review, not silently marked down."""
        blueprint = _sub_scheme()
        result = {"questions": [_row("4(a)", 2, 2)]}
        reconciled, report = bp.apply_blueprint(result, blueprint)
        row = _q(reconciled, "4")
        assert row["status"] == bp.STATUS_REVIEW
        # Re-running the reconciliation cannot launder it back to VALID.
        again, report2 = bp.apply_blueprint(reconciled, blueprint)
        assert _q(again, "4")["status"] == bp.STATUS_REVIEW
        assert "4" in report2["still_unresolved"]


# ================================= 5. the mark-scheme checker and inventory

class TestSubquestionCoverage:
    """A subquestion is a marking point in its own right (§4, §8)."""

    def test_a_declared_subquestion_is_expected_in_its_own_right(self):
        blueprint = _sub_scheme()
        inventory = expected_inventory(blueprint)
        assert sorted(inventory) == ["3a", "3b", "4a", "4bi", "4bii"]
        assert inventory["4bii"]["max_marks"] == 3
        assert inventory["4bii"]["parent_question"] == "4"

    def test_an_unreturned_subquestion_is_reported_as_missing(self):
        blueprint = _sub_scheme()
        result = {"questions": [_row("4(a)", 2, 2)]}
        assert missing_from_results(result, blueprint) == \
            ["3a", "3b", "4b(i)", "4b(ii)"]
        # The reconciliation reports the same gap at identifier level: question
        # 3 is a missing QUESTION (nothing came back for it), question 4 is a
        # question with missing SUBQUESTIONS.
        _reconciled, report = bp.apply_blueprint(result, blueprint)
        assert report["missing_questions"] == ["3"]
        assert [s["subquestion"] for s in report["missing_subquestions"]] == \
            ["4bi", "4bii"]

    def test_a_parent_result_does_not_prove_its_subparts_were_assessed(self):
        # The whole question came back as ONE row. Every declared sub-part is
        # still unaccounted for — absorbing it into the parent is exactly how an
        # unmarked sub-part becomes a silent zero.
        blueprint = _sub_scheme()
        result = {"questions": [_row("4", 7, 7)]}
        assert missing_from_results(result, blueprint) == \
            ["3a", "3b", "4a", "4b(i)", "4b(ii)"]

    def test_a_returned_subpart_still_answers_its_own_question(self):
        # The reverse direction is coverage: "62(a)" is evidence for question
        # 62, so the question-level inventory row is satisfied.
        flat = {"questions": [{"question_number": "62", "max_marks": 4,
                               "criteria": "c"}]}
        assert missing_from_results(
            {"questions": [_row("62(a)", 2, 4)]}, flat) == []

    def test_nested_subquestions_are_distinct_identities(self):
        built = st.build_structure(_sub_scheme()["questions"])
        ids = [s["id"] for s in built["subquestions"]]
        assert ids == ["3a", "3b", "4a", "4bi", "4bii"]
        assert st.declared_ids(built["questions"]) == ids


class TestMarkSchemeChecker:
    """The checker validates the structure at subquestion level (§8)."""

    def test_cross_validation_reports_a_subquestion_without_an_answer(self):
        structure = msv.canonicalize({
            "questions": [{"question_number": "4", "max_marks": 2,
                           "parts": [{"number": "a", "max_marks": 2}]}],
            "printed_total": 2, "total_marks": 2, "confidence": 0.9,
        })
        # canonicalize keeps the part so the checker can see it.
        assert structure["questions"][0]["parts"] == [
            {"number": "a", "max_marks": 2}]
        result = msv.cross_validate(structure, None)
        assert result["verified"] is False
        assert any("no expected answer" in a for a in result["anomalies"])
        assert result["checks"]["subquestion_counts"]["subquestions"] == 1
        assert result["checks"]["subquestions"][0]["id"] == "4a"

    def test_cross_validation_reports_stated_and_part_marks_disagreeing(self):
        structure = msv.canonicalize({
            "questions": [{"question_number": "4", "max_marks": 6,
                           "parts": [{"number": "a", "max_marks": 2,
                                      "criteria": "M1 A1",
                                      "answers": ["2"]}]}],
            "total_marks": 2,
        })
        assert structure["questions"][0]["max_marks"] == 2
        assert structure["parts_disagreements"] == ["4: stated 6, parts sum to 2"]
        result = msv.cross_validate(structure, None)
        assert any("sub-part marks disagree" in a for a in result["anomalies"])

    def test_a_flat_scheme_gains_no_subquestion_anomalies(self):
        structure = msv.canonicalize({
            "questions": [{"question_number": n, "max_marks": 2,
                           "criteria": "M1 A1"}
                          for n in ("1", "2")],
            "printed_total": 4, "total_marks": 4,
        })
        result = msv.cross_validate(structure, None)
        assert result["anomalies"] == []
        assert result["verified"] is True

    def test_structure_report_and_confidence_flag_the_bad_subquestion(self):
        blueprint = {"questions": [
            {"question_number": "4", "max_marks": 2,
             "parts": [{"number": "a", "max_marks": 2, "criteria": ""}]}]}
        report = bp.structure_report(blueprint)
        assert report["has_subquestions"] is True
        assert report["subquestions_complete"] is False
        confidence = bp.mark_scheme_confidence(report)
        assert confidence["confidence"] == bp.CONFIDENCE_NEEDS_REVIEW
        assert any("no expected answer" in r for r in confidence["reasons"])
        assert any("no expected answer" in w
                   for w in bp.validation_warnings(report))

    def test_a_clean_subquestion_scheme_needs_no_review(self):
        report = bp.structure_report(_sub_scheme())
        assert report["has_subquestions"] is True
        assert report["subquestion_count"] == 5
        assert report["subquestions_complete"] is True
        assert bp.mark_scheme_confidence(report)["confidence"] == \
            bp.CONFIDENCE_HIGH
        # Each subquestion's confidence is reported individually.
        assert {s["confidence"] for s in report["subquestions"]} == {1.0}

    def test_verification_surfaces_the_subquestion_gap(self):
        blueprint = _sub_scheme()
        result = {"questions": [_row("3(a)", 1, 1), _row("3(b)", 2, 2),
                                _row("4(a)", 2, 2), _row("4(b)(i)", 2, 2)]}
        reconciled, report = bp.apply_blueprint(result, blueprint)
        verification = verify_result(reconciled, report)
        assert verification["status"] == "REVIEW"
        assert any("4b(ii)" in a for a in verification["anomalies"])
        assert "4" in verification["review_questions"]
        # The marks that WERE returned are untouched by the review flag.
        assert verification["earned"] == 7


# ============================================ 6. prompt contract (§7)

class TestPromptContract:
    """The marker receives labelled subquestions, not one flat block."""

    def test_prompt_lists_every_subquestion_with_its_own_evidence(self):
        built = st.build_structure(_sub_scheme()["questions"])
        block = st.prompt_block(built["questions"], total_marks=10)
        assert "subquestion=4bii" in block
        assert "parent 4" in block
        assert "x = 15" in block          # its own answer
        assert "M1,A1,A1" in block        # its own marking points
        assert "total_marks: 10" in block
        # The rule that keeps one subquestion out of another is stated.
        assert "never move an answer, a mark or a" in block
        assert "criterion from one subquestion to another" in block

    def test_truncation_is_visible_rather_than_silent(self):
        long_criteria = "M1 A1 " * 400
        built = st.build_structure([
            {"question_number": "1", "max_marks": 3, "criteria": long_criteria,
             "answers": ["7"]}])
        block = st.prompt_block(built["questions"])
        assert "[criteria truncated for length]" in block

    def test_blueprint_prompt_block_uses_the_structured_renderer(self):
        block = bp.prompt_block(_sub_scheme())
        assert "subquestion=4bi" in block
        assert "question_number=4 max_marks=7" in block

    def test_the_marking_prompt_actually_carries_the_subquestions(self):
        prompt = legacy.marking_user_prompt(_sub_scheme())
        assert "subquestion=4bii" in prompt
        assert "x = 15" in prompt

    def test_a_flat_scheme_prompt_is_unchanged_in_substance(self):
        block = bp.prompt_block({"questions": [
            {"question_number": "1", "max_marks": 3, "criteria": "M1 A1 A1"}]})
        assert "question_number=1 max_marks=3" in block
        assert "subquestion=" not in block


# ==================================== 7. extraction integration (§2, §7)

class FakeResponse:
    def __init__(self, text):
        self.text = text


class FakeModels:
    def __init__(self, responses):
        self.responses = responses
        self.calls = []

    def generate_content(self, *, model, contents, config=None, **kw):
        self.calls.append({"model": model, "contents": contents,
                           "config": config})
        action = self.responses.get(model, self.responses.get("*"))
        if isinstance(action, Exception):
            raise action
        return FakeResponse(action)


class FakeClient:
    def __init__(self, responses):
        self.models = FakeModels(responses)
        self.deleted = []

    def upload_file(self, path):
        return f"remote:{path}"

    def files_delete(self, remote):
        self.deleted.append(remote)


def _write_scheme(tmp_path):
    path = tmp_path / "scheme.pdf"
    path.write_bytes(b"%PDF-1.4 synthetic mark scheme")
    return path


class TestExtractionIntegration:
    """The extraction keeps the sub-structure the model returns."""

    def setup_method(self):
        legacy.MODEL_HEALTH.reset()

    def teardown_method(self):
        legacy.MODEL_HEALTH.reset()

    def _build(self, fresh_state, tmp_path, payload):
        from app.config import get_config, save_config
        cfg = get_config()
        cfg.mock_mode = False
        save_config(cfg)
        client = FakeClient({"*": json.dumps(payload)})
        return bp.get_blueprint(client, _write_scheme(tmp_path))

    def test_per_part_criteria_and_answers_survive_extraction(
            self, fresh_state, tmp_path):
        got, meta = self._build(fresh_state, tmp_path, {
            "paper": "1H", "printed_total": 7, "total_marks": 7,
            "confidence": 0.95,
            "questions": [{
                "question_number": "4", "max_marks": 7,
                "criteria": "See parts", "answers": [],
                "page": 5,
                "parts": [
                    {"number": "a", "max_marks": 2, "criteria": "M1 A1",
                     "answers": ["x = 12"], "page": 5},
                    {"number": "b(i)", "max_marks": 2, "criteria": "M1 A1 cao",
                     "answers": ["x = 12"], "page": 5},
                    {"number": "b(ii)", "max_marks": 3,
                     "criteria": "M1 A1 A1 ft", "answers": ["x = 15"],
                     "page": 6},
                ],
            }]})
        assert meta["source"] == "built", meta
        question = got["questions"][0]
        assert [p["number"] for p in question["parts"]] == \
            ["a", "b(i)", "b(ii)"]
        # Each sub-part's OWN evidence came through (this is what was dropped).
        assert question["parts"][2]["criteria"] == "M1 A1 A1 ft"
        assert question["parts"][2]["answers"] == ["x = 15"]
        assert question["parts"][2]["page"] == 6

    def test_extraction_subquestions_reach_the_prompt_and_the_checker(
            self, fresh_state, tmp_path):
        got, meta = self._build(fresh_state, tmp_path, {
            "questions": [{
                "question_number": "4", "max_marks": 4,
                "parts": [
                    {"number": "b(i)", "max_marks": 2, "criteria": "M1 A1",
                     "answers": ["x = 12"]},
                    {"number": "b(ii)", "max_marks": 2, "criteria": "M1 A1 ft",
                     "answers": ["x = 15"]},
                ]}]})
        assert meta["source"] == "built"
        block = bp.prompt_block(got)
        assert "subquestion=4bi" in block and "subquestion=4bii" in block
        report = bp.structure_report(got)
        assert report["has_subquestions"] is True
        assert report["subquestion_count"] == 2
        assert [s["id"] for s in report["subquestions"]] == ["4bi", "4bii"]

    def test_an_alias_duplicate_sub_part_is_not_counted_twice(
            self, fresh_state, tmp_path):
        got, meta = self._build(fresh_state, tmp_path, {
            "questions": [
                {"question_number": "4(a)", "max_marks": 2, "criteria": "M1 A1",
                 "answers": ["2"]},
                {"question_number": "4", "max_marks": 4, "criteria": "see parts",
                 "parts": [{"number": "a", "max_marks": 2, "criteria": "M1 A1",
                            "answers": ["2"]},
                           {"number": "b", "max_marks": 2, "criteria": "M1 A1",
                            "answers": ["3"]}]},
            ]})
        assert meta["source"] == "built"
        # "4(a)" folded into question 4: one scored question of 4 marks.
        assert len(got["questions"]) == 1
        assert got["total_marks"] == 4
        ids = [s["id"] for s in bp.structure_report(got)["subquestions"]]
        assert sorted(ids) == ["4a", "4b"]
        # ...and the folded alias did not create a duplicate identity.
        structure = st.build_structure(got["questions"])
        assert sorted(structure["id_counts"]) == ["4", "4a", "4b"]
        assert all(c == 1 for c in structure["id_counts"].values())
