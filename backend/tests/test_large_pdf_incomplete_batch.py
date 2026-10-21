"""Regression tests for the two Large PDF defects found on the real 49-page
O Level Mathematics submission (Ch1L13 - Sets, 69 questions / 266 marks):

1. An INCOMPLETE batch response was accepted as a COMPLETE batch. Batch 2 of 2
   (pages 25-49, ~136 expected marks) returned 8 sub-rows worth 9 marks and was
   stored with ``problems: []``, so 32 questions the mark scheme requires
   (38-69, 128 marks) were never marked.
2. Those never-returned questions were then divided into the total, so the
   marker's own failure was charged to the student: the stored report read
   124/266 = 46.6% (grade 4) instead of 124/138 = 89.9% on the work that was
   actually marked.

Also pinned here: the large-PDF workflow must use its DEDICATED prompt (not the
ordinary single-file one) and every batch must be marked against the COMPLETE,
unsplit official mark scheme.

The batch engine is injected, so no API quota is consumed.
"""

from __future__ import annotations

import json

import pytest

import app.config as config
import app.db as db
from app.engine import legacy_marking as legacy
from app.large_pdf import prompt as large_prompt
from app.large_pdf import states, store
from app.large_pdf.runner import BatchOutcome, LargePdfRunner
from app.engine import batching
from app.large_pdf.validate import batch_coverage, merge_batches, score_breakdown

# The REAL official mark scheme for Ch1L13 - Sets: 69 questions, 266 marks.
# Question 1-34 = 129 marks, 35-37 = 9, 38-69 = 128. Kept verbatim so the test
# reproduces the live failure's exact arithmetic.
SCHEME_MARKS = [
    5, 2, 4, 4, 4, 4, 4, 3, 6, 5, 3, 5, 4, 4, 3, 4, 4, 4, 3, 3, 2, 3,
    4, 4, 3, 5, 5, 3, 3, 4, 2, 3, 4, 6, 3, 3, 3, 4, 3, 4, 4, 3, 5, 3,
    4, 4, 5, 4, 3, 4, 3, 2, 4, 3, 6, 4, 6, 2, 6, 3, 5, 5, 3, 4, 8, 3,
    4, 3, 4,
]
PAPER_PAGES = 49
PAPER_TOTAL = sum(SCHEME_MARKS)


def _blueprint() -> dict:
    return {"total_marks": PAPER_TOTAL,
            "questions": [{"question_number": str(i + 1), "max_marks": m,
                           "criteria": ""}
                          for i, m in enumerate(SCHEME_MARKS)]}


def _blank_pdf(path, pages: int) -> str:
    from pypdf import PdfWriter
    writer = PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(width=200, height=200)
    with open(path, "wb") as handle:
        writer.write(handle)
    return str(path)


def _plan(*ranges) -> "batching.BatchPlan":
    """A BatchPlan over the given inclusive page ranges (live shapes)."""
    payload = {
        "page_count": max(end for _start, end in ranges),
        "single_pass": len(ranges) == 1, "batch_count": len(ranges),
        "target_min": 24, "target_max": 28, "notes": [],
        "batches": [{"index": i, "start": start, "end": end,
                     "pages": end - start + 1, "context_before": 0,
                     "context_after": 0, "question_ids": [],
                     "adjusted": False, "notes": []}
                    for i, (start, end) in enumerate(ranges)],
        "question_pages": {},
    }
    return batching.BatchPlan.from_dict(payload)


def _batch_dicts(*ranges) -> list[dict]:
    return [{"batch_index": i, "page_start": start, "page_end": end,
             "pages": end - start + 1, "question_ids": []}
            for i, (start, end) in enumerate(ranges)]


def _q(number: str, awarded: int, max_marks: int) -> dict:
    return {
        "question_number": str(number), "max_marks": max_marks,
        "awarded_marks": awarded, "topic": "", "student_answer": "work shown",
        "working_summary": "", "loss_reason": "",
        "mark_breakdown": [{"code": "M1", "awarded": awarded > 0, "reason": "x"}],
        "correct_method": "", "full_mark_solution": "",
    }


def _rows(numbers: list[int]) -> list[dict]:
    """Top-level rows awarding the full mark scheme mark for each number."""
    return [_q(str(n), SCHEME_MARKS[n - 1], SCHEME_MARKS[n - 1])
            for n in numbers]


#: The live batch-1 (pages 25-49) response: 8 sub-rows, 9 marks, questions
#: 35-37 only. This is what the pipeline accepted as a complete batch.
STUB_ROWS = [
    _q("35a", 1, 1), _q("35b", 1, 1), _q("35c", 1, 1),
    _q("36a", 2, 2), _q("36b", 1, 1),
    _q("37ai", 1, 1), _q("37aii", 1, 1), _q("37b", 1, 1),
]


class _ScriptedEngine:
    """Injectable batch engine with per-(batch, attempt) scripts + a call log."""

    def __init__(self, script=None):
        self.calls: list[dict] = []
        self._script = script or {}

    def __call__(self, *, batch_pdf, mark_scheme_pdf, job, batch):
        index = int(batch["batch_index"])
        attempt = sum(1 for c in self.calls if c["batch_index"] == index) + 1
        self.calls.append({
            "batch_index": index, "attempt": attempt,
            "mark_scheme_pdf": str(mark_scheme_pdf),
            "batch_pdf": str(batch_pdf),
            "paper_pages": batch.get("_paper_pages"),
        })
        outcome = self._script.get(index, "ok")
        if isinstance(outcome, list):          # one entry per attempt
            outcome = outcome[min(attempt, len(outcome)) - 1]
        if callable(outcome):
            return outcome(batch, attempt)
        rows = {"ok": _rows(list(range(1, 35)))}[outcome]
        return BatchOutcome(ok=True, result={"questions": rows}, marking_model="m")

    def calls_for(self, index: int) -> int:
        return sum(1 for c in self.calls if c["batch_index"] == index)


@pytest.fixture
def large_env(fresh_state, monkeypatch):
    cfg = config.get_config()
    cfg.mock_mode = True
    cfg.large_pdf.enabled = True
    cfg.large_pdf.concurrency = 2
    cfg.large_pdf.max_retries = 2
    config.save_config(cfg)
    return fresh_state


def _runner(engine, *, with_blueprint: bool = True) -> LargePdfRunner:
    """A runner whose mark-scheme structure is the real 69-question scheme.

    With ``with_blueprint=False`` the structure is unavailable, which is what
    an extraction failure looks like — the job must STILL be held for review
    when a batch reported an incomplete response.
    """
    runner = LargePdfRunner(engine=engine)
    if with_blueprint:
        runner._cached_blueprint = lambda job: _blueprint()
        runner._get_blueprint = lambda job: (_blueprint(),
                                             {"source": "test-fixture"})
    return runner


def _job(tmp_path, runner, *, pages: int = PAPER_PAGES):
    student = _blank_pdf(tmp_path / "student.pdf", pages)
    scheme = _blank_pdf(tmp_path / "scheme.pdf", 8)
    job = runner.enqueue_manual(student_name="Student A",
                                student_pdf_path=student,
                                mark_scheme_path=scheme)
    return runner.prepare(job["id"])


# ============================================================ coverage check

class TestBatchCoverage:
    def test_live_stub_batch_is_detected(self):
        """The real batch 2 (25 of 49 pages, 9 of ~136 marks) is a stub."""
        coverage = batch_coverage(
            {"questions": STUB_ROWS},
            {"batch_index": 1, "page_start": 25, "page_end": 49, "pages": 25},
            paper_pages=PAPER_PAGES, scheme_total_marks=PAPER_TOTAL)
        assert coverage["stub"] is True
        assert coverage["returned_marks"] == 9
        assert coverage["ratio"] < 0.25

    def test_a_complete_batch_is_not_a_stub(self):
        coverage = batch_coverage(
            {"questions": _rows(list(range(25, 50)))},
            {"batch_index": 1, "page_start": 25, "page_end": 49, "pages": 25},
            paper_pages=PAPER_PAGES, scheme_total_marks=PAPER_TOTAL)
        assert coverage["stub"] is False

    def test_no_structure_means_no_verdict(self):
        """Without a mark scheme structure the check must not invent one."""
        assert batch_coverage({"questions": STUB_ROWS},
                              {"batch_index": 1, "pages": 25},
                              paper_pages=PAPER_PAGES,
                              scheme_total_marks=None) is None


# ============================================================ runner behaviour

class TestIncompleteBatchResponse:
    def test_stub_batch_is_remarked_and_the_better_attempt_is_kept(
            self, large_env, tmp_path):
        """Batch 1 stubs once, then answers fully on the re-mark."""
        engine = _ScriptedEngine(script={
            0: "ok",
            1: lambda batch, attempt: BatchOutcome(
                ok=True, marking_model="m",
                result={"questions": STUB_ROWS if attempt == 1
                        else _rows(list(range(35, 70)))}),
        })
        runner = _runner(engine)
        job = _job(tmp_path, runner)

        final = runner.process(job["id"])

        assert engine.calls_for(1) == 2          # re-marked, not accepted
        assert final["status"] == states.COMPLETED, final
        row = db.query_one("SELECT * FROM results WHERE id = ?",
                           (final["result_id"],))
        stored = json.loads(row["result_json"])
        labels = {str(q["question_number"]) for q in stored["questions"]}
        assert {"35", "36", "37", "68", "69"} <= labels
        assert stored["totals"]["unresolved_marks"] == 0
        assert stored["totals"]["score_basis"] == "COMPLETE"

    def test_unmarked_questions_are_never_charged_to_the_student(
            self, large_env, tmp_path):
        """Every attempt stubs: the score covers marked questions only."""
        engine = _ScriptedEngine(script={
            0: "ok",
            1: lambda batch, attempt: BatchOutcome(
                ok=True, marking_model="m", result={"questions": STUB_ROWS}),
        })
        runner = _runner(engine)
        job = _job(tmp_path, runner)

        final = runner.process(job["id"])

        # One re-mark was attempted. The third request was NOT made: both
        # attempts returned an IDENTICAL stub, and a third identical request
        # cannot change the answer (it only cost minutes on live runs). The
        # best (partial) attempt is kept and flagged — never silently completed.
        assert engine.calls_for(1) == 2
        batches = {b["batch_index"]: b for b in store.list_batches(job["id"])}
        validation = json.loads(batches[1]["validation_json"])
        assert validation["stub"] is True
        assert any("INCOMPLETE RESPONSE" in w for w in validation["warnings"])

        assert final["status"] == states.AWAITING_REVIEW, final
        row = db.query_one("SELECT * FROM results WHERE id = ?",
                           (final["result_id"],))
        assert row["review_required"] == 1

        verification = json.loads(row["verification_json"])
        stored = json.loads(row["result_json"])

        # 38-69 (128 marks) were never marked: that is the MARKER's failure.
        assert verification["unresolved_marks"] == 128
        assert verification["unresolved_count"] == 32
        assert verification["paper_total_marks"] == PAPER_TOTAL
        assert verification["score_basis"] == "MARKED_ONLY"

        # The marked work is 138 marks of the 266-mark paper. The score is NOT
        # published as 138/138: a partial correction carries no grade, its
        # stored ``percentage`` is the share of the WHOLE paper the marked work
        # earned, and the marked-only share is reported separately.
        assert row["available"] == 138
        assert row["earned"] == 129 + 9
        assert verification["partial"] is True
        assert verification["marked_percentage"] > 89      # of the marked work
        assert row["percentage"] == pytest.approx(row["earned"] / PAPER_TOTAL * 100)
        assert row["percentage"] < 89
        assert row["provisional_grade"] is None
        assert stored["totals"]["paper_available"] == PAPER_TOTAL
        assert stored["totals"]["partial"] is True
        assert stored["totals"]["coverage"]["requires_review"] == 32
        assert "not a score out of 266" in " ".join(verification["anomalies"])

        # Every unresolved question is explicitly NEEDS_REVIEW, never a 0.
        unresolved = [q for q in stored["questions"]
                      if str(q["question_number"]) in
                      {str(n) for n in range(38, 70)}]
        assert len(unresolved) == 32
        assert all(q["status"] == "NEEDS_REVIEW" for q in unresolved)
        assert all(q["awarded_marks"] == 0 and q["max_marks"] > 0
                   for q in unresolved)

    def test_incomplete_batch_is_held_for_review_even_without_a_blueprint(
            self, large_env, tmp_path):
        """No extracted structure must never turn a partial mark into a total.

        Without the mark scheme's structure the pipeline cannot even name the
        questions that were never returned, so the batch-level INCOMPLETE flag
        is the only evidence left — and it must block finalization.
        """
        engine = _ScriptedEngine(script={
            0: "ok",
            1: lambda batch, attempt: BatchOutcome(
                ok=True, marking_model="m", result={"questions": STUB_ROWS}),
        })
        runner = _runner(engine, with_blueprint=False)
        job = _job(tmp_path, runner)

        final = runner.process(job["id"])

        assert final["status"] == states.AWAITING_REVIEW, final
        row = db.query_one("SELECT * FROM results WHERE id = ?",
                           (final["result_id"],))
        assert row["review_required"] == 1
        verification = json.loads(row["verification_json"])
        assert any("far fewer questions than the other batches" in w
                   for w in verification["warnings"]), verification["warnings"]
        stored = json.loads(row["result_json"])
        assert stored["totals"]["score_basis"] == "COMPLETE"  # nothing vanished
        assert row["percentage"] == pytest.approx(100.0)      # but not published

    def test_thin_batch_detection_needs_siblings_to_compare_with(self):
        """A single batch has nothing to compare against: no false verdict."""
        merged = merge_batches(
            _plan((1, 49)), _batch_dicts((1, 49)),
            {0: {"questions": STUB_ROWS}})
        assert merged["report"]["thin_batches"] == []

    def test_thin_batch_is_flagged_against_its_siblings(self):
        """The live imbalance: 34 questions/24 pages vs 8 rows/25 pages."""
        merged = merge_batches(
            _plan((1, 24), (25, 49)), _batch_dicts((1, 24), (25, 49)),
            {0: {"questions": _rows(list(range(1, 35)))},
             1: {"questions": STUB_ROWS}})
        thin = merged["report"]["thin_batches"]
        assert [entry["batch"] for entry in thin] == [1]
        assert merged["needs_review"] is True

    def test_balanced_batches_are_not_flagged(self):
        merged = merge_batches(
            _plan((1, 24), (25, 49)), _batch_dicts((1, 24), (25, 49)),
            {0: {"questions": _rows(list(range(1, 35)))},
             1: {"questions": [_q(str(n), 1, 1) for n in range(35, 60)]},
        })
        assert merged["report"]["thin_batches"] == []

    def test_score_breakdown_is_pure_and_backwards_compatible(self):
        """A fully marked result is accounted for exactly as before."""
        rows = _rows(list(range(1, 70)))
        breakdown = score_breakdown({"questions": rows})
        assert breakdown["earned"] == PAPER_TOTAL
        assert breakdown["available"] == PAPER_TOTAL
        assert breakdown["unresolved_marks"] == 0
        assert breakdown["score_basis"] == "COMPLETE"
        assert breakdown["percentage"] == pytest.approx(100.0)


# ============================================================ whole scheme

class TestWholeMarkScheme:
    def test_every_batch_is_marked_against_the_identical_full_scheme(
            self, large_env, tmp_path):
        engine = _ScriptedEngine(script={
            0: "ok",
            1: lambda batch, attempt: BatchOutcome(
                ok=True, marking_model="m",
                result={"questions": _rows(list(range(35, 70)))}),
        })
        runner = _runner(engine)
        job = _job(tmp_path, runner)
        runner.process(job["id"])

        schemes = {c["mark_scheme_pdf"] for c in engine.calls}
        assert len(schemes) == 1                       # never split per batch
        # The single scheme handed to every batch may be the SHARED optimized
        # copy (§ compression runs before marking), but it must still be the
        # complete document: same page count, not a per-batch excerpt.
        import pymupdf
        used = schemes.pop()
        with pymupdf.open(used) as chosen, pymupdf.open(
                job["mark_scheme_path"]) as original:
            assert chosen.page_count == original.page_count == 8

    def test_batch_prompt_states_the_complete_scheme_and_page_range(self):
        _, user = large_prompt.prompts_for_batch(
            batch={"batch_index": 1, "page_start": 25, "page_end": 49,
                   "pages": 25, "_batch_total": 2},
            paper_pages=PAPER_PAGES, blueprint=_blueprint())
        assert "batch 2 of 2" in user
        assert "pages 25-49" in user
        assert "COMPLETE official scheme" in user
        assert "69 questions, 266 marks" in user
        assert "do NOT return or score a question that is not visible" in user


# ============================================================ dedicated prompt

class TestDedicatedLargePdfPrompt:
    def test_dedicated_prompt_differs_from_the_normal_one(self):
        normal = legacy.effective_system_prompt()
        large = large_prompt.effective_large_pdf_system_prompt()
        assert large != normal
        for anchor in ("ONE BATCH of pages", "COMPLETE, UNSHORTENED scheme",
                       "DO NOT STOP EARLY", "continues onto the next page"):
            assert anchor in large

    def test_developer_override_replaces_only_the_large_prompt(
            self, large_env, monkeypatch):
        cfg = config.get_config()
        cfg.large_pdf.system_prompt_override = "CUSTOM LARGE PROMPT"
        config.save_config(cfg)
        try:
            assert large_prompt.effective_large_pdf_system_prompt() == \
                "CUSTOM LARGE PROMPT"
            assert legacy.effective_system_prompt() != "CUSTOM LARGE PROMPT"
        finally:
            cfg.large_pdf.system_prompt_override = ""
            config.save_config(cfg)

    def test_prompt_change_invalidates_the_processing_version(
            self, large_env):
        from app.large_pdf.runner import processing_version
        before = processing_version("student-sha", "scheme-sha")
        cfg = config.get_config()
        cfg.large_pdf.system_prompt_override = "CUSTOM LARGE PROMPT"
        config.save_config(cfg)
        try:
            assert processing_version("student-sha", "scheme-sha") != before
        finally:
            cfg.large_pdf.system_prompt_override = ""
            config.save_config(cfg)
        assert processing_version("student-sha", "scheme-sha") == before

    def test_grade_with_model_sends_the_dedicated_prompt(self, large_env):
        """The prompt must actually reach the model request."""
        captured: dict = {}

        class _Response:
            text = json.dumps({"paper_info": {
                "qualification": "q", "paper": "p", "session": "s"},
                "questions": [], "overall_notes": {
                    "summary": "", "strengths": [], "revision_areas": []},
                "uncertainties": []})

        class _Models:
            def generate_content(self, *, model, contents, config=None):
                captured["system_instruction"] = config["system_instruction"]
                captured["prompt"] = contents[-1]
                return _Response()

        class _Client:
            models = _Models()

            def upload_file(self, *a, **k):        # pragma: no cover
                raise AssertionError("not used")

        system, user = large_prompt.prompts_for_batch(
            batch={"batch_index": 0, "page_start": 1, "page_end": 24,
                   "pages": 24, "_batch_total": 2},
            paper_pages=PAPER_PAGES, blueprint=_blueprint())
        legacy.grade_with_model(
            client=_Client(), model_names=["test-model"],
            student_file=object(), mark_scheme_file=object(),
            system_prompt=system, user_prompt=user)

        assert captured["system_instruction"] == system
        assert "ONE BATCH of pages" in captured["system_instruction"]
        assert captured["prompt"] == user
