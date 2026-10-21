"""Marking-integrity tests for the Large PDF Worker.

These pin the failure the audit started from: a real 50-page, 67-question paper
whose run marked only a handful of questions and was stored as **22/22 = 100%**
because the percentage was computed over the marked subset alone. Every
question that had no outcome was excluded from the denominator instead of
being reported as unresolved, so the worst possible run looked like a perfect
paper.

The rules asserted here are the ones the pipeline must never lose:

* a correction is COMPLETE only when every expected question has a definitive
  outcome (a mark, or a confirmed blank);
* a partial correction never carries a percentage or a provisional grade that
  can be read as the paper's result, and is never stored as publishable;
* every expected question has exactly one explicit state, and the ones with no
  outcome are named;
* a question-level mark is accepted when the marker returned the whole
  question with the scheme's own maximum — but a genuinely partial sub-part
  set is still held for review (the relaxation is narrow, never a licence to
  guess);
* totals are computed in Python from validated rows, never trusted from prose.
"""

from __future__ import annotations

import json

import pytest

import app.config as config
import app.db as db
from app.engine import legacy_marking as legacy
from app.engine.blueprint import apply_blueprint
from app.large_pdf import states, store
from app.large_pdf.runner import BatchOutcome, LargePdfRunner
from app.large_pdf.validate import (
    batch_coverage, coverage_inventory, merge_batches, score_breakdown,
    scheme_total_marks, validate_batch,
)

# Reuse the established fixtures/builders (same fake-PDF + plan shapes the
# pipeline really produces) rather than duplicating them.
from test_large_pdf_incomplete_batch import (  # noqa: E402
    _batch_dicts, _blank_pdf, _blueprint as _scheme_69, _job, _plan, _q,
    _rows, large_env, _ScriptedEngine,
)

#: A 67-question paper with MIXED maxima (2-6 marks), 10 of them multi-part.
#: Sum is the authoritative maximum every assertion below derives from.
MARKS = [(i % 5) + 2 for i in range(67)]
PAPER_TOTAL = sum(MARKS)
PAGES = 50


def _blueprint_67() -> dict:
    questions = []
    for index, max_marks in enumerate(MARKS, start=1):
        row = {"question_number": str(index), "max_marks": max_marks,
               "criteria": ""}
        if index <= 10:
            first = max_marks // 2
            row["parts"] = [
                {"number": "a", "max_marks": first, "criteria": ""},
                {"number": "b", "max_marks": max_marks - first, "criteria": ""},
            ]
        questions.append(row)
    return {"total_marks": PAPER_TOTAL, "questions": questions}


def _full_row(number: int) -> dict:
    return _q(str(number), MARKS[number - 1], MARKS[number - 1])


def _runner(engine, *, recovery=None) -> LargePdfRunner:
    runner = LargePdfRunner(engine=engine, recovery_engine=recovery)
    blueprint = _blueprint_67()
    runner._cached_blueprint = lambda job: blueprint
    runner._get_blueprint = lambda job: (blueprint, {"source": "fixture"})
    return runner


def _no_recovery(*, client, chunk, student_remote, scheme_remote,
                 mark_scheme_path, context_note="", question_remote=None):
    from app.engine.recover_missing import RecoveryOutcome
    return RecoveryOutcome(ok=True, recovered={}, still_missing=list(chunk))


def _stored_result(final: dict) -> tuple[dict, dict, dict]:
    row = db.query_one("SELECT * FROM results WHERE id = ?", (final["result_id"],))
    return (dict(row), json.loads(row["result_json"]),
            json.loads(row["verification_json"]))


# ====================================================== the audit's 22/22 bug

class TestPartialNeverLooksComplete:
    def test_22_of_67_marked_is_not_a_22_out_of_22_result(self, large_env,
                                                         tmp_path):
        """THE regression: 22 marked rows must not become a perfect score."""
        engine = _ScriptedEngine(script={
            0: lambda batch, attempt: BatchOutcome(
                ok=True, marking_model="m",
                result={"questions": [_full_row(n) for n in range(1, 23)]}),
            1: lambda batch, attempt: BatchOutcome(
                ok=True, marking_model="m", result={"questions": []}),
        })
        runner = _runner(engine, recovery=_no_recovery)
        job = _job(tmp_path, runner, pages=PAGES)

        final = runner.process(job["id"])

        assert final["status"] == states.AWAITING_REVIEW
        row, stored, verification = _stored_result(final)
        coverage = verification["coverage"]
        totals = stored["totals"]

        # Nothing may read as a completed result...
        assert row["review_required"] == 1
        assert verification["partial"] is True
        assert totals["partial"] is True
        assert row["provisional_grade"] is None
        assert totals["percentage"] != pytest.approx(100.0)
        assert row["percentage"] != pytest.approx(100.0)

        # ...because 45 of the 67 questions have no outcome at all, and they
        # are NAMED rather than dropped from the denominator.
        assert coverage["expected"] == 67
        assert coverage["marked"] == 22
        assert coverage["requires_review"] == 45
        assert coverage["verified"] is False
        assert coverage["unaccounted"] == []
        assert verification["unresolved_questions"] == [str(n) for n in range(23, 68)]
        assert verification["unresolved_count"] == 45

        # The marked work is still reported honestly, as a share of the PAPER.
        marked_earned = sum(MARKS[:22])
        assert row["earned"] == marked_earned
        assert row["available"] == marked_earned       # the marked subset
        assert totals["paper_available"] == PAPER_TOTAL
        assert row["percentage"] == pytest.approx(marked_earned / PAPER_TOTAL * 100)
        assert verification["marked_percentage"] == pytest.approx(100.0)

    def test_a_genuinely_complete_22_mark_paper_is_still_a_valid_result(
            self, large_env, tmp_path):
        """The guard must not refuse a legitimate full-mark paper."""
        engine = _ScriptedEngine(script={
            0: lambda batch, attempt: BatchOutcome(
                ok=True, marking_model="m",
                result={"questions": [_full_row(n) for n in range(1, 68)]}),
            1: lambda batch, attempt: BatchOutcome(
                ok=True, marking_model="m", result={"questions": []}),
        })
        runner = _runner(engine, recovery=_no_recovery)
        job = _job(tmp_path, runner, pages=PAGES)

        final = runner.process(job["id"])

        assert final["status"] == states.COMPLETED
        row, stored, verification = _stored_result(final)
        assert row["earned"] == PAPER_TOTAL
        assert row["available"] == PAPER_TOTAL
        assert row["percentage"] == pytest.approx(100.0)
        assert row["provisional_grade"] == "9"
        assert verification["partial"] is False
        assert stored["totals"]["partial"] is False
        assert verification["coverage"]["verified"] is True

    def test_a_small_paper_can_still_score_full_marks(self, large_env,
                                                     tmp_path):
        """22/22 is a real result when the paper really holds 22 marks."""
        engine = _ScriptedEngine(script={
            0: lambda batch, attempt: BatchOutcome(
                ok=True, marking_model="m",
                result={"questions": [_q(str(n), marks, marks)
                                      for n, marks in ((1, 10), (2, 12))]}),
            1: lambda batch, attempt: BatchOutcome(
                ok=True, marking_model="m", result={"questions": []}),
        })
        runner = LargePdfRunner(engine=engine, recovery_engine=_no_recovery)
        blueprint = {"total_marks": 22, "questions": [
            {"question_number": "1", "max_marks": 10, "criteria": ""},
            {"question_number": "2", "max_marks": 12, "criteria": ""}]}
        runner._cached_blueprint = lambda job: blueprint
        runner._get_blueprint = lambda job: (blueprint, {"source": "fixture"})
        student = _blank_pdf(tmp_path / "student.pdf", 4)
        scheme = _blank_pdf(tmp_path / "scheme.pdf", 2)
        job = runner.enqueue_manual(student_name="A", student_pdf_path=student,
                                    mark_scheme_path=scheme)
        job = runner.prepare(job["id"])

        final = runner.process(job["id"])

        assert final["status"] == states.COMPLETED
        row, _stored, verification = _stored_result(final)
        assert (row["earned"], row["available"]) == (22, 22)
        assert row["percentage"] == pytest.approx(100.0)
        assert verification["partial"] is False


# ============================================================ mark validation

class TestMarkValidation:
    def test_award_above_the_maximum_is_clamped_and_flagged(self):
        blueprint = {"total_marks": 4, "questions": [
            {"question_number": "1", "max_marks": 4, "criteria": ""}]}
        result = {"questions": [_q("1", 9, 4)]}
        reconciled, report = apply_blueprint(result, blueprint)
        question = reconciled["questions"][0]
        assert question["awarded_marks"] == 4
        assert question["max_marks"] == 4
        assert report["award_clamped"] == [{"question": "1", "awarded": 9,
                                            "max": 4}]
        assert question["status"] == "VALID"

    def test_negative_and_non_numeric_marks_never_reach_the_total(self):
        breakdown = score_breakdown({"questions": [
            {"question_number": "1", "max_marks": 4, "awarded_marks": -3},
            {"question_number": "2", "max_marks": 4, "awarded_marks": "n/a"},
            {"question_number": "3", "max_marks": 4, "awarded_marks": 2},
        ]})
        assert breakdown["earned"] == 2
        assert breakdown["available"] == 12
        assert breakdown["partial"] is False

    def test_maximum_comes_from_the_scheme_not_the_model(self):
        blueprint = {"total_marks": 15, "questions": [
            {"question_number": "1", "max_marks": 5, "criteria": ""},
            {"question_number": "2", "max_marks": 10, "criteria": ""}]}
        result = {"questions": [_q("1", 5, 5), _q("2", 10, 10)]}
        reconciled, _report = apply_blueprint(result, blueprint)
        assert sum(q["max_marks"] for q in reconciled["questions"]) == 15
        assert scheme_total_marks(blueprint) == 15
        # The model claiming a different maximum is corrected to the scheme's.
        result = {"questions": [_q("1", 5, 7), _q("2", 10, 10)]}
        reconciled, report = apply_blueprint(result, blueprint)
        assert reconciled["questions"][0]["max_marks"] == 5
        assert report["max_marks_corrected"]


# ==================================================== malformed model output

class TestMalformedModelOutput:
    """A provider can return junk. It must be REPORTED, never raise or guess."""

    def test_validation_never_raises_on_malformed_rows(self):
        batch = {"question_ids": json.dumps(["1", "2", "3"]),
                 "pages": 10, "page_start": 1, "page_end": 10}
        result = {"questions": [
            "not a row",
            {"awarded_marks": 2, "max_marks": 2},             # no number
            {"question_number": "2", "awarded_marks": "n/a",
             "max_marks": 3},                                    # unreadable
            {"question_number": "3", "awarded_marks": 2.5,
             "max_marks": 3},                                    # not whole
        ]}

        validation = validate_batch(result, batch)

        assert validation["status"] == states.BATCH_REVIEW_REQUIRED
        assert any("not an object" in p.lower() or "no question number" in p.lower()
                   for p in validation["problems"])
        assert "2:unreadable" in validation["invalid_marks"]
        assert "3:unreadable" in validation["invalid_marks"]
        # Unreadable is never a confirmed blank (that would be a free 0).
        assert validation["blank_questions"] == []

    def test_a_non_object_response_is_a_problem_not_a_crash(self):
        validation = validate_batch(["junk"], {"question_ids": "[]"})
        assert validation["status"] == states.BATCH_REVIEW_REQUIRED
        assert any("not a JSON object" in p for p in validation["problems"])
        # Coverage still runs: an unreadable response returns 0 marks, which is
        # a stub (re-marked), never "nothing to check".
        coverage = batch_coverage(["junk"], {"pages": 10},
                                  paper_pages=10, scheme_total_marks=50)
        assert coverage["returned_marks"] == 0 and coverage["stub"] is True

    def test_merge_holds_an_unreadable_mark_for_review_not_zero(self):
        plan = _plan((1, 10))
        batches = [{"batch_index": 0, "page_start": 1, "page_end": 10,
                    "pages": 10, "question_ids": json.dumps(["1"])}]
        merged = merge_batches(
            plan, batches,
            {0: {"questions": [
                {"question_number": "1", "awarded_marks": "n/a",
                 "max_marks": 3, "student_answer": "x = 4",
                 "mark_breakdown": ["M1"]}]}},
            blueprint_questions=[{"question_number": "1", "max_marks": 3}])
        row = merged["result"]["questions"][0]
        assert row["status"] == "NEEDS_REVIEW"
        assert "unreadable" in row["review_reason"].lower()
        assert merged["report"]["blank_questions"] == []

    def test_an_unreadable_mark_cannot_finalize_a_correction(self, large_env,
                                                            tmp_path):
        engine = _ScriptedEngine(script={
            0: lambda batch, attempt: BatchOutcome(
                ok=True, marking_model="m",
                result={"questions": [
                    {"question_number": str(n), "awarded_marks": "n/a",
                     "max_marks": MARKS[n - 1], "student_answer": "work"}
                    for n in range(1, 68)]}),
            1: lambda batch, attempt: BatchOutcome(
                ok=True, marking_model="m", result={"questions": []}),
        })
        runner = _runner(engine, recovery=_no_recovery)
        job = _job(tmp_path, runner, pages=PAGES)

        final = runner.process(job["id"])

        assert final["status"] == states.AWAITING_REVIEW
        row, stored, verification = _stored_result(final)
        assert row["review_required"] == 1
        assert verification["partial"] is True
        # 67 rows but none of them a mark: nothing may be credited.
        assert row["earned"] == 0
        assert verification["coverage"]["verified"] is False


# ======================================================= question inventories

class TestQuestionLevelMarks:
    def test_a_whole_question_row_is_accepted_when_it_claims_the_question_max(self):
        blueprint = {"total_marks": 4, "questions": [
            {"question_number": "1", "max_marks": 4, "criteria": "",
             "parts": [{"number": "a", "max_marks": 2, "criteria": ""},
                       {"number": "b", "max_marks": 2, "criteria": ""}]}]}
        result = {"questions": [_q("1", 3, 4)]}
        reconciled, report = apply_blueprint(result, blueprint)
        question = reconciled["questions"][0]
        assert question["status"] == "VALID"
        assert question["awarded_marks"] == 3
        assert question["subquestion_attribution"] == "question_level"
        assert report["question_level_marks"] == [
            {"question": "1", "max_marks": 4, "awarded": 3}]
        # The attribution gap is reported, never invented as sub-part marks.
        assert "subquestions" not in question

    def test_a_partial_sub_part_set_is_still_held_for_review(self):
        blueprint = {"total_marks": 4, "questions": [
            {"question_number": "1", "max_marks": 4, "criteria": "",
             "parts": [{"number": "a", "max_marks": 2, "criteria": ""},
                       {"number": "b", "max_marks": 2, "criteria": ""}]}]}
        result = {"questions": [_q("1a", 2, 2)]}
        reconciled, report = apply_blueprint(result, blueprint)
        question = reconciled["questions"][0]
        assert question["status"] == "REVIEW"
        assert "1b" in question["unmarked_subquestions"]
        assert report["still_unresolved"] == ["1"]
        assert question["awarded_marks"] == 2      # never inflated to 4

    def test_a_fragment_claiming_a_smaller_max_stays_ambiguous(self):
        blueprint = {"total_marks": 6, "questions": [
            {"question_number": "1", "max_marks": 6, "criteria": "",
             "parts": [{"number": "a", "max_marks": 2, "criteria": ""},
                       {"number": "b", "max_marks": 4, "criteria": ""}]}]}
        result = {"questions": [_q("1", 2, 2)]}
        reconciled, _report = apply_blueprint(result, blueprint)
        assert reconciled["questions"][0]["status"] == "REVIEW"


class TestCoverageInventory:
    def test_every_state_is_counted_once(self):
        coverage = coverage_inventory({"questions": [
            _q("1", 4, 4),
            {"question_number": "2", "max_marks": 3, "awarded_marks": 0,
             "student_answer": "", "mark_breakdown": [], "status": "CONFIRMED_ZERO"},
            {"question_number": "3", "max_marks": 5, "awarded_marks": 0,
             "status": "REVIEW", "review_reason": "handwriting is illegible"},
            {"question_number": "4", "max_marks": 6, "awarded_marks": 0,
             "status": "NEEDS_REVIEW"},
        ]}, {"applied": True, "missing_questions": ["5"]})
        assert coverage["expected"] == 5
        assert coverage["marked"] == 1
        assert coverage["unanswered"] == 1
        assert coverage["illegible"] == 1
        assert coverage["requires_review"] == 2
        assert coverage["unaccounted"] == ["5"]
        assert coverage["verified"] is False

    def test_blank_answers_are_not_marker_failures(self, large_env, tmp_path):
        """A returned blank is a confirmed 0, never an unresolved question."""
        engine = _ScriptedEngine(script={
            0: lambda batch, attempt: BatchOutcome(
                ok=True, marking_model="m", result={"questions": [
                    _q("1", 3, 3),
                    {"question_number": "2", "max_marks": 4,
                     "awarded_marks": 0, "student_answer": "",
                     "mark_breakdown": []},
                ]}),
            1: lambda batch, attempt: BatchOutcome(
                ok=True, marking_model="m", result={"questions": []}),
        })
        runner = LargePdfRunner(engine=engine, recovery_engine=_no_recovery)
        blueprint = {"total_marks": 7, "questions": [
            {"question_number": "1", "max_marks": 3},
            {"question_number": "2", "max_marks": 4}]}
        runner._cached_blueprint = lambda job: blueprint
        runner._get_blueprint = lambda job: (blueprint, {"source": "fixture"})
        student = _blank_pdf(tmp_path / "s.pdf", 4)
        scheme = _blank_pdf(tmp_path / "m.pdf", 2)
        job = runner.prepare(runner.enqueue_manual(
            student_name="A", student_pdf_path=student,
            mark_scheme_path=scheme)["id"])

        final = runner.process(job["id"])

        _row, stored, verification = _stored_result(final)
        assert final["status"] == states.COMPLETED, verification
        assert verification["partial"] is False
        assert verification["coverage"]["unanswered"] == 1
        blanks = [q for q in stored["questions"]
                  if str(q["question_number"]) == "2"]
        assert blanks and blanks[0]["status"] == "CONFIRMED_ZERO"


# ========================================================== failure handling

class TestFailuresAreNeverHidden:
    def test_a_permanent_batch_failure_cannot_complete(self, large_env,
                                                       tmp_path):
        engine = _ScriptedEngine(script={
            0: "ok",
            1: lambda batch, attempt: BatchOutcome(
                ok=False, error="deadline exceeded", error_kind="timeout"),
        })
        runner = _runner(engine, recovery=_no_recovery)
        job = _job(tmp_path, runner, pages=PAGES)

        final = runner.process(job["id"])

        assert final["status"] == states.FAILED
        assert final["result_id"] in (None, "")
        assert db.query_one("SELECT * FROM results WHERE job_id = ?",
                            (job["id"],)) is None

    def test_a_timeout_on_one_batch_is_retried_without_re_marking_the_other(
            self, large_env, tmp_path):
        calls = {"n": 0}

        def once_timeout(batch, attempt):
            calls["n"] += 1
            if calls["n"] == 1:
                return BatchOutcome(ok=False, error="deadline exceeded",
                                    error_kind="timeout")
            return BatchOutcome(ok=True, marking_model="m",
                                result={"questions": [_full_row(n)
                                                      for n in range(23, 68)]})

        engine = _ScriptedEngine(script={
            0: lambda batch, attempt: BatchOutcome(
                ok=True, marking_model="m",
                result={"questions": [_full_row(n) for n in range(1, 23)]}),
            1: once_timeout,
        })
        runner = _runner(engine, recovery=_no_recovery)
        job = _job(tmp_path, runner, pages=PAGES)

        final = runner.process(job["id"])

        assert final["status"] == states.COMPLETED
        assert engine.calls_for(0) == 1        # successful batch never re-marked
        assert engine.calls_for(1) == 2        # the timed-out one was retried

    def test_duplicate_question_ids_are_counted_once(self):
        rows = [_q("5", 3, 3), _q("5", 1, 3)]
        validation = validate_batch({"questions": rows},
                                    {"batch_index": 0, "question_ids": []})
        assert validation["duplicate_questions"] == ["5"]
        merged = merge_batches(_plan((1, 10)), _batch_dicts((1, 10)),
                               {0: {"questions": rows}})
        assert merged["report"]["duplicate_questions"] == ["5"]
        numbers = [q["question_number"] for q in merged["result"]["questions"]]
        assert numbers.count("5") == 1
        assert merged["needs_review"] is True

    def test_questions_the_scheme_does_not_declare_are_flagged_not_counted(self):
        blueprint = {"total_marks": 4, "questions": [
            {"question_number": "1", "max_marks": 4}]}
        result = {"questions": [_q("1", 4, 4), _q("99", 5, 5)]}
        reconciled, report = apply_blueprint(result, blueprint)
        extras = [q for q in reconciled["questions"]
                  if str(q["question_number"]) == "99"]
        assert extras and extras[0]["status"] == "REVIEW"
        assert "99" in report["extra_questions"]
        assert scheme_total_marks(blueprint) == 4

    def test_a_reshaped_stub_does_not_cause_an_endless_retry(self, large_env,
                                                            tmp_path):
        """Repeated identical stubs buy nothing: two requests, then stop."""
        engine = _ScriptedEngine(script={
            0: "ok",
            1: lambda batch, attempt: BatchOutcome(
                ok=True, marking_model="m",
                result={"questions": [_q("35a", 1, 1)]}),
        })
        runner = _runner(engine, recovery=_no_recovery)
        job = _job(tmp_path, runner, pages=PAGES)
        runner.process(job["id"])
        assert engine.calls_for(1) == 2


# ================================================================ API surface

@pytest.fixture
def api_client(large_env):
    from fastapi.testclient import TestClient
    from app.main import app
    with TestClient(app) as client:
        yield client


class TestApiSurface:
    def test_an_incomplete_result_is_exposed_as_incomplete(self, api_client,
                                                           tmp_path):
        """The review queue and results list must carry the gap, not a score."""
        engine = _ScriptedEngine(script={
            0: lambda batch, attempt: BatchOutcome(
                ok=True, marking_model="m",
                result={"questions": [_full_row(n) for n in range(1, 23)]}),
            1: lambda batch, attempt: BatchOutcome(
                ok=True, marking_model="m", result={"questions": []}),
        })
        runner = _runner(engine, recovery=_no_recovery)
        job = _job(tmp_path, runner, pages=PAGES)
        final = runner.process(job["id"])

        review = api_client.get("/api/review?limit=50").json()
        item = next(i for i in review["items"] if i["id"] == final["result_id"])
        assert item["partial"]["marked_questions"] == 22
        assert item["partial"]["expected_questions"] == 67
        assert item["partial"]["unresolved_questions"] == 45
        assert "verification_json" not in item

        listing = api_client.get("/api/results?limit=50").json()
        row = next(r for r in listing["results"]
                   if r["id"] == final["result_id"])
        assert row["partial"]["unresolved_questions"] == 45
        assert row["partial"]["paper_marks"] == PAPER_TOTAL
        assert "verification_json" not in row

    def test_a_partial_cannot_be_approved_without_a_whole_paper_mark(
            self, api_client, tmp_path):
        engine = _ScriptedEngine(script={
            0: lambda batch, attempt: BatchOutcome(
                ok=True, marking_model="m",
                result={"questions": [_full_row(n) for n in range(1, 23)]}),
            1: lambda batch, attempt: BatchOutcome(
                ok=True, marking_model="m", result={"questions": []}),
        })
        runner = _runner(engine, recovery=_no_recovery)
        job = _job(tmp_path, runner, pages=PAGES)
        result_id = runner.process(job["id"])["result_id"]

        refused = api_client.post(f"/api/results/{result_id}/override", json={})
        assert refused.status_code == 409
        body = refused.json()
        message = str(body.get("message") or body.get("error", {}).get("message", ""))
        assert "incomplete" in message.lower()
        stored = db.query_one("SELECT * FROM results WHERE id = ?", (result_id,))
        assert stored["earned"] == stored["available"]   # untouched
        assert json.loads(stored["verification_json"])["partial"] is True

        settled = api_client.post(f"/api/results/{result_id}/override",
                                  json={"earned": 200, "note": "Marked by hand"})
        assert settled.status_code == 200
        stored = db.query_one("SELECT * FROM results WHERE id = ?", (result_id,))
        verification = json.loads(stored["verification_json"])
        assert stored["earned"] == 200
        assert stored["available"] == PAPER_TOTAL
        assert stored["percentage"] == pytest.approx(200 / PAPER_TOTAL * 100)
        assert verification["partial"] is False
        assert stored["review_required"] == 0


class TestReportIsHonestAboutPartials:
    def test_partial_report_pdf_prints_no_percentage_and_says_draft(self):
        import io as _io

        from pypdf import PdfReader
        from app.api.report_routes import render_result_pdf

        row = {
            "id": "r1", "student_name": "Ali", "display_name": "Ali",
            "earned": 22, "available": 22, "percentage": 100.0,
            "provisional_grade": None, "manual_override": 0,
            "created_at": 1792000000.0, "coursework_id": None,
            "verification_json": {
                "status": "REVIEW", "partial": True,
                "coverage": {"expected": 67, "marked": 7,
                             "requires_review": 60, "unanswered": 0,
                             "verified": False},
                "unresolved_marks": 233, "paper_total_marks": 255,
                "earned": 22, "available": 22,
                "anomalies": [], "warnings": []},
            "result_json": {
                "questions": [{"question_number": str(i), "max_marks": 4,
                               "awarded_marks": 4, "status": "VALID"}
                              for i in range(1, 8)],
                "overall_notes": {"summary": "", "strengths": [],
                                  "revision_areas": []},
                "uncertainties": [], "paper_info": {}},
        }
        data = render_result_pdf(row).getvalue()
        text = "\n".join(page.extract_text() or ""
                         for page in PdfReader(_io.BytesIO(data)).pages)
        assert "Incomplete correction" in text
        assert "DRAFT" in text
        assert "MARKED ONLY" in text.upper()
        assert "100" not in text          # the marked-only fraction is not a %


class TestRecoveryKeepsWorking:
    def test_an_empty_chunk_does_not_stop_later_recovery(self, large_env,
                                                         tmp_path):
        """Live run: chunks 1-2 recovered nothing, chunks 3/6 recovered four
        questions. An empty chunk must never end the retry."""
        from app.engine.recover_missing import RecoveryOutcome

        blueprint = {"total_marks": 80, "questions": [
            {"question_number": str(n), "max_marks": 4}
            for n in range(1, 21)]}
        engine = _ScriptedEngine(script={
            0: lambda batch, attempt: BatchOutcome(
                ok=True, marking_model="m",
                result={"questions": [_q(str(n), 4, 4)
                                      for n in range(1, 13)]}),
            1: lambda batch, attempt: BatchOutcome(
                ok=True, marking_model="m", result={"questions": []}),
        })
        calls = {"n": 0}

        def recovery(*, client, chunk, student_remote, scheme_remote,
                     mark_scheme_path, context_note="", question_remote=None):
            calls["n"] += 1
            if calls["n"] == 1:
                return RecoveryOutcome(ok=True, recovered={},
                                       still_missing=list(chunk))
            later = [str(n) for n in range(13, 21)]
            return RecoveryOutcome(
                ok=True,
                recovered={q: _q(q, 4, 4) for q in later},
                still_missing=[q for q in chunk if q not in later])

        runner = LargePdfRunner(engine=engine, recovery_engine=recovery)
        runner._cached_blueprint = lambda job: blueprint
        runner._get_blueprint = lambda job: (blueprint, {"source": "fixture"})
        student = _blank_pdf(tmp_path / "s.pdf", 3)
        scheme = _blank_pdf(tmp_path / "m.pdf", 2)
        job = runner.prepare(runner.enqueue_manual(
            student_name="A", student_pdf_path=student,
            mark_scheme_path=scheme)["id"])

        final = runner.process(job["id"])

        assert calls["n"] >= 2                     # later chunks still ran
        assert final["status"] == states.COMPLETED
        _row, stored, verification = _stored_result(final)
        assert verification["partial"] is False
        assert len(stored["questions"]) == 20


# ================================================= unchanged normal behaviour

class TestNormalCorrectorUnaffected:
    def test_normal_prompt_is_byte_identical(self):
        """The relaxation lives in reconciliation, not in the standard prompt."""
        from app.large_pdf import prompt as large_prompt
        normal = legacy.effective_system_prompt()
        assert normal != large_prompt.effective_large_pdf_system_prompt()
        assert "ONE BATCH of pages" not in normal
        # The ordinary prompt still asks for per-question marking of one file.
        assert "student" in normal.lower()

    def test_standard_totals_arithmetic_is_unchanged(self):
        rows = [_q("1", 2, 5), _q("2", 5, 5)]
        earned, available, percentage = legacy.calculate_totals(
            {"questions": rows})
        assert (earned, available) == (7, 10)
        assert percentage == pytest.approx(70.0)

    def test_a_normal_correction_with_full_coverage_has_no_partial_flag(self):
        breakdown = score_breakdown({"questions": [_q("1", 5, 5)]})
        assert breakdown["partial"] is False
        assert breakdown["score_basis"] == "COMPLETE"
