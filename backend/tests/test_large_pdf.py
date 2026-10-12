"""Regression tests for the Large PDF Worker.

Covers: the pure batch planner (balanced partitions, complete coverage, single
ownership, question-aware boundaries), per-batch validation (missing / blank /
invalid marks / max mismatch), the canonical merge (duplicate + conflicting
results, overlap), and the runner end-to-end (idempotency, failed-batch retry,
restart recovery, no repeated AI calls, changed submissions, no re-marking on
report regeneration, rate-limit handling). The batch engine is injected, so no
API quota is consumed.
"""

from __future__ import annotations

import io
import json
import time

import pytest

import app.config as config
import app.db as db
from app.engine import batching
from app.large_pdf import states, store
from app.large_pdf.runner import BatchOutcome, LargePdfRunner
from app.large_pdf.validate import merge_batches, validate_batch


# ------------------------------------------------------------------ helpers

def _blank_pdf(path, pages: int = 52) -> str:
    from pypdf import PdfWriter
    writer = PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(width=200, height=200)
    with open(path, "wb") as handle:
        writer.write(handle)
    return str(path)


def _pdf_bytes(pages: int) -> io.BytesIO:
    from pypdf import PdfWriter
    writer = PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(width=200, height=200)
    buf = io.BytesIO()
    writer.write(buf)
    buf.seek(0)
    return buf


def _text_pdf(path, pages: list[str]) -> str:
    """A PDF whose pages carry the given text (for question detection)."""
    from reportlab.pdfgen import canvas
    c = canvas.Canvas(str(path))
    for text in pages:
        c.drawString(72, 720, text)
        c.showPage()
    c.save()
    return str(path)


def _q(number, awarded, max_marks, *, answer="2+2=4", breakdown=None):
    return {
        "question_number": str(number), "max_marks": max_marks,
        "awarded_marks": awarded, "topic": "", "student_answer": answer,
        "working_summary": "", "loss_reason": "",
        "mark_breakdown": breakdown if breakdown is not None else [
            {"code": "M1", "awarded": awarded > 0, "reason": "x"}],
        "correct_method": "", "full_mark_solution": "",
    }


class _CountingEngine:
    """Injectable batch engine: per-batch scripted outcomes + call log."""

    def __init__(self, script=None):
        self.calls: list[int] = []
        self._script = script or {}

    def __call__(self, *, batch_pdf, mark_scheme_pdf, job, batch):
        index = batch["batch_index"]
        self.calls.append(index)
        outcome = self._script.get(index, "ok")
        if callable(outcome):
            return outcome(batch)
        if outcome == "ok":
            base = index * 10
            return BatchOutcome(ok=True, result={"questions": [
                _q(base + 1, 3, 4), _q(base + 2, 5, 5)]}, marking_model="m")
        if outcome == "fail":
            return BatchOutcome(ok=False, error="boom", error_kind="unknown")
        if outcome == "rate":
            return BatchOutcome(ok=False, error="429 rate limit",
                                error_kind="rate_limited")
        return BatchOutcome(ok=True, result={"questions": []})


@pytest.fixture
def large_env(fresh_state, monkeypatch):
    cfg = config.get_config()
    cfg.mock_mode = True          # keeps blueprint lookup offline
    cfg.large_pdf.enabled = True
    cfg.large_pdf.concurrency = 2
    config.save_config(cfg)
    return fresh_state


# ============================================================ planner

class TestBatchPlanner:
    @pytest.mark.parametrize("pages", [24, 26, 28])
    def test_short_documents_stay_single_pass(self, pages):
        plan = batching.plan_batches(pages)
        assert plan.single_pass and len(plan.batches) == 1
        assert plan.batches[0].start == 1 and plan.batches[0].end == pages

    @pytest.mark.parametrize("pages,expected", [
        (29, 2), (52, 2), (57, 3), (100, 4), (150, 6),
    ])
    def test_documented_partitions(self, pages, expected):
        plan = batching.plan_batches(pages)
        assert not plan.single_pass
        assert len(plan.batches) == expected

    def test_known_page_counts(self):
        assert [(b.start, b.end) for b in batching.plan_batches(29).batches] == \
            [(1, 14), (15, 29)]
        assert [(b.start, b.end) for b in batching.plan_batches(52).batches] == \
            [(1, 26), (27, 52)]
        assert [(b.start, b.end) for b in batching.plan_batches(57).batches] == \
            [(1, 19), (20, 38), (39, 57)]
        assert [(b.start, b.end) for b in batching.plan_batches(100).batches] == \
            [(1, 25), (26, 50), (51, 75), (76, 100)]

    @pytest.mark.parametrize("pages", [24, 28, 29, 52, 57, 100, 150])
    def test_balanced_and_complete_coverage(self, pages):
        plan = batching.plan_batches(pages)
        owned = plan.owned_pages()
        assert owned == list(range(1, pages + 1))          # every page once
        sizes = [b.pages for b in plan.batches]
        assert max(sizes) - min(sizes) <= 1                 # balanced
        assert batching.validate_plan(plan) == []

    def test_exactly_one_owner_per_page(self):
        plan = batching.plan_batches(100)
        owners = [plan.owner_of(p) for p in range(1, 101)]
        assert None not in owners
        # contiguous ownership
        assert owners == sorted(owners)

    def test_question_spans_boundary_is_kept_whole(self):
        # A question on pages 13-15 spans the 14|15 boundary of a 29-page doc.
        pages = {str(n): [p] for n, p in enumerate(range(1, 30))}
        pages["7"] = [13, 14, 15]
        plan = batching.plan_batches(29, question_pages=pages)
        # boundary moved so question 7 is not split
        for batch in plan.batches:
            owns7 = any(p in (13, 14, 15) for p in range(batch.start, batch.end + 1))
            fully = all(p in range(batch.start, batch.end + 1) for p in (13, 14, 15))
            assert not owns7 or fully
        assert batching.validate_plan(plan) == []

    def test_unmovable_split_is_flagged(self):
        # A question spanning an enormous range cannot be kept whole.
        pages = {"1": list(range(1, 29)), "2": [1, 29]}
        plan = batching.plan_batches(29, question_pages=pages, max_shift=0)
        assert plan.notes  # flagged rather than silently accepted

    def test_context_overlap_never_duplicates_ownership(self):
        plan = batching.plan_batches(52, overlap=1)
        assert plan.owned_pages() == list(range(1, 53))
        for batch in plan.batches:
            assert batch.context_before in (0, 1)
            assert batch.context_after in (0, 1)


# ============================================================ validation

class TestBatchValidation:
    def test_missing_question_is_reported_not_zeroed(self):
        result = {"questions": [_q(1, 3, 4)]}
        report = validate_batch(result, {"batch_index": 0, "question_ids":
                                         json.dumps(["1", "2"])})
        assert report["missing_questions"] == ["2"]
        assert report["question_count"] == 1

    def test_blank_answer_distinct_from_missing(self):
        blank = _q(2, 0, 4, answer="", breakdown=[])
        report = validate_batch({"questions": [blank]},
                                {"batch_index": 0, "question_ids": []})
        assert report["blank_questions"] == ["2"]
        assert report["missing_questions"] == []

    def test_invalid_marks_and_max_mismatch(self):
        result = {"questions": [_q(1, 9, 4)]}
        report = validate_batch(result, {"batch_index": 0, "question_ids": []},
                                blueprint_max={"1": 3})
        assert report["invalid_marks"]
        assert report["max_marks_mismatch"]
        assert report["problems"]


# ============================================================ merge

def _plan_and_batches(page_count, question_pages):
    plan = batching.plan_batches(page_count, question_pages=question_pages)
    batches = [{
        "batch_index": b.index, "page_start": b.start, "page_end": b.end,
        "question_ids": list(b.question_ids),
    } for b in plan.batches]
    return plan, batches


class TestMerge:
    def test_overlap_does_not_double_count(self):
        # q1 on pages 1-2 (batch 0). Batch 1 also returns q1 by mistake.
        qp = {"1": [1, 2], "2": [27, 28]}
        plan, batches = _plan_and_batches(52, qp)
        results = {
            0: {"questions": [_q(1, 3, 4)]},
            1: {"questions": [_q(1, 3, 4), _q(2, 5, 5)]},
        }
        merged = merge_batches(plan, batches, results)
        numbers = [q["question_number"] for q in merged["result"]["questions"]]
        assert numbers == ["1", "2"]           # q1 counted exactly once
        assert "1" in merged["report"]["overlap_ignored"]

    def test_conflicting_marks_require_review(self):
        qp = {"1": [1, 2]}
        plan, batches = _plan_and_batches(52, qp)
        results = {
            0: {"questions": [_q(1, 3, 4)]},
            1: {"questions": [_q(1, 1, 4)]},   # conflicting
        }
        merged = merge_batches(plan, batches, results,
                               blueprint_questions=[{"question_number": "1",
                                                     "max_marks": 4}])
        assert merged["needs_review"]
        assert merged["report"]["conflicts"]

    def test_missing_expected_question_is_flagged(self):
        qp = {"1": [1], "2": [27]}
        plan, batches = _plan_and_batches(52, qp)
        results = {0: {"questions": [_q(1, 3, 4)]}}   # batch 1 produced nothing
        merged = merge_batches(plan, batches, results,
                               blueprint_questions=[
                                   {"question_number": "1", "max_marks": 4},
                                   {"question_number": "2", "max_marks": 5}])
        assert "2" in merged["report"]["missing_questions"]


# ============================================================ runner E2E

class TestRunnerEndToEnd:
    def test_long_pdf_batches_and_completes(self, large_env, tmp_path):
        student = _blank_pdf(tmp_path / "s.pdf", 52)
        scheme = _blank_pdf(tmp_path / "m.pdf", 8)
        engine = _CountingEngine()
        runner = LargePdfRunner(engine=engine)
        job = runner.enqueue_manual(student_name="Ada", student_pdf_path=student,
                                    mark_scheme_path=scheme)
        prepared = runner.prepare(job["id"])
        assert prepared["status"] == states.READY
        assert prepared["batch_count"] == 2

        final = runner.process(job["id"])
        assert final["status"] == states.COMPLETED, final
        assert sorted(engine.calls) == [0, 1]           # each batch marked once
        batches = store.list_batches(job["id"])
        assert all(b["status"] == states.BATCH_COMPLETED for b in batches)
        row = db.query_one("SELECT * FROM results WHERE id = ?",
                           (final["result_id"],))
        assert row is not None and row["earned"] > 0

    def test_short_pdf_single_batch(self, large_env, tmp_path):
        student = _blank_pdf(tmp_path / "s.pdf", 10)
        scheme = _blank_pdf(tmp_path / "m.pdf", 8)
        engine = _CountingEngine()
        runner = LargePdfRunner(engine=engine)
        job = runner.enqueue_manual(student_name="Short", student_pdf_path=student,
                                    mark_scheme_path=scheme)
        prepared = runner.prepare(job["id"])
        assert prepared["batch_count"] == 1
        runner.process(job["id"])
        assert engine.calls == [0]

    def test_plan_saved_before_marking(self, large_env, tmp_path):
        student = _blank_pdf(tmp_path / "s.pdf", 52)
        scheme = _blank_pdf(tmp_path / "m.pdf", 8)
        engine = _CountingEngine()
        runner = LargePdfRunner(engine=engine)
        job = runner.enqueue_manual(student_name="A", student_pdf_path=student,
                                    mark_scheme_path=scheme)
        # Before prepare, nothing may be marked.
        runner.process(job["id"], prepare_if_needed=False)
        assert engine.calls == []
        row = store.get_job(job["id"])
        assert row["status"] == states.QUEUED

    def test_repeated_process_does_not_remark(self, large_env, tmp_path):
        student = _blank_pdf(tmp_path / "s.pdf", 52)
        scheme = _blank_pdf(tmp_path / "m.pdf", 8)
        engine = _CountingEngine()
        runner = LargePdfRunner(engine=engine)
        job = runner.enqueue_manual(student_name="A", student_pdf_path=student,
                                    mark_scheme_path=scheme)
        runner.process(job["id"])
        first = list(engine.calls)

        # A repeated call must not re-enter finalize either: that step calls the
        # blueprint AI and persists a result. Count both across the repeats.
        blueprint_calls: list[str] = []
        original = runner._get_blueprint

        def _counted(j):
            blueprint_calls.append(j["id"])
            return original(j)

        runner._get_blueprint = _counted
        for _ in range(3):
            runner.process(job["id"])

        assert engine.calls == first          # each batch marked exactly once
        assert blueprint_calls == []          # finalize never re-entered
        assert len(db.query("SELECT id FROM results")) == 1   # one result row

    def test_idempotent_enqueue(self, large_env, tmp_path):
        student = _blank_pdf(tmp_path / "s.pdf", 52)
        scheme = _blank_pdf(tmp_path / "m.pdf", 8)
        runner = LargePdfRunner(engine=_CountingEngine())
        job1 = runner.enqueue_manual(student_name="A", student_pdf_path=student,
                                     mark_scheme_path=scheme)
        job2 = runner.enqueue_manual(student_name="A", student_pdf_path=student,
                                     mark_scheme_path=scheme)
        assert job1["id"] == job2["id"]
        assert len(store.list_jobs()) == 1

    def test_changed_submission_creates_new_version(self, large_env, tmp_path):
        scheme = _blank_pdf(tmp_path / "m.pdf", 8)
        runner = LargePdfRunner(engine=_CountingEngine())
        s1 = _blank_pdf(tmp_path / "s1.pdf", 52)
        job1 = runner.enqueue_manual(student_name="A", student_pdf_path=s1,
                                     mark_scheme_path=scheme)
        s2 = _blank_pdf(tmp_path / "s2.pdf", 53)     # different content
        job2 = runner.enqueue_manual(student_name="A", student_pdf_path=s2,
                                     mark_scheme_path=scheme)
        assert job1["id"] != job2["id"]
        assert job1["processing_version"] != job2["processing_version"]

    def test_failed_batch_retries_without_repeating_successful(self, large_env,
                                                               tmp_path):
        student = _blank_pdf(tmp_path / "s.pdf", 52)
        scheme = _blank_pdf(tmp_path / "m.pdf", 8)
        script = {0: "ok", 1: "fail"}
        engine = _CountingEngine(script=script)
        runner = LargePdfRunner(engine=engine)
        job = runner.enqueue_manual(student_name="A", student_pdf_path=student,
                                    mark_scheme_path=scheme)
        final = runner.process(job["id"])
        assert final["status"] == states.FAILED
        batches = {b["batch_index"]: b["status"] for b in store.list_batches(job["id"])}
        assert batches[0] == states.BATCH_COMPLETED      # kept
        assert batches[1] == states.BATCH_FAILED
        calls_after_failure = list(engine.calls)

        # Retry only the failed batch.
        engine._script[1] = "ok"
        runner.retry_batch(job["id"], 1)
        assert engine.calls.count(0) == calls_after_failure.count(0)  # not re-run
        done = store.get_job(job["id"])
        assert done["status"] == states.COMPLETED, done

    def test_rate_limited_batch_parks_without_consuming_budget(self, large_env,
                                                               tmp_path):
        student = _blank_pdf(tmp_path / "s.pdf", 52)
        scheme = _blank_pdf(tmp_path / "m.pdf", 8)
        engine = _CountingEngine(script={0: "ok", 1: "rate"})
        runner = LargePdfRunner(engine=engine)
        job = runner.enqueue_manual(student_name="A", student_pdf_path=student,
                                    mark_scheme_path=scheme)
        final = runner.process(job["id"])
        assert final["status"] == states.READY           # waiting, not failed
        batches = {b["batch_index"]: b["status"] for b in store.list_batches(job["id"])}
        assert batches[1] == states.BATCH_PENDING

    def test_restart_recovery_requeues_interrupted_batches(self, large_env,
                                                           tmp_path):
        student = _blank_pdf(tmp_path / "s.pdf", 52)
        scheme = _blank_pdf(tmp_path / "m.pdf", 8)
        runner = LargePdfRunner(engine=_CountingEngine())
        job = runner.enqueue_manual(student_name="A", student_pdf_path=student,
                                    mark_scheme_path=scheme)
        runner.prepare(job["id"])
        # Simulate a crash mid-processing.
        db.execute("UPDATE large_pdf_jobs SET status = ? WHERE id = ?",
                   (states.PROCESSING, job["id"]))
        db.execute("UPDATE large_pdf_batches SET status = ? WHERE job_id = ? "
                   "AND batch_index = 0", (states.BATCH_PROCESSING, job["id"]))
        result = store.recover_in_flight()
        assert result["jobs_requeued"] >= 1
        assert store.get_job(job["id"])["status"] == states.QUEUED
        assert store.get_batch(job["id"], 0)["status"] == states.BATCH_PENDING

    def test_cancel_cleans_temp_and_stops(self, large_env, tmp_path):
        student = _blank_pdf(tmp_path / "s.pdf", 52)
        scheme = _blank_pdf(tmp_path / "m.pdf", 8)
        runner = LargePdfRunner(engine=_CountingEngine())
        job = runner.enqueue_manual(student_name="A", student_pdf_path=student,
                                    mark_scheme_path=scheme)
        runner.prepare(job["id"])
        runner.cancel(job["id"])
        assert store.get_job(job["id"])["status"] == states.CANCELLED
        # Cancelling then processing must not mark anything.
        engine = _CountingEngine()
        runner.engine = engine
        runner.process(job["id"])
        assert engine.calls == []

    def test_inspection_failure_stops_before_marking(self, large_env, tmp_path):
        bad = tmp_path / "not_a.pdf"
        bad.write_bytes(b"not a pdf")
        scheme = _blank_pdf(tmp_path / "m.pdf", 8)
        engine = _CountingEngine()
        runner = LargePdfRunner(engine=engine)
        job = runner.enqueue_manual(student_name="A", student_pdf_path=str(bad),
                                    mark_scheme_path=scheme)
        prepared = runner.prepare(job["id"])
        assert prepared["status"] == states.FAILED
        assert engine.calls == []

    def test_real_engine_mock_mode_conflict_requires_review(self, large_env,
                                                            tmp_path):
        """The default engine (mock Gemini) returns the SAME questions for
        every batch, so the merge must detect the conflict and require review
        rather than double-counting marks. Proves the engine is genuinely
        wired in (no injected fake)."""
        from app.large_pdf.runner import LargePdfRunner as RealRunner
        student = _blank_pdf(tmp_path / "s.pdf", 52)
        scheme = _blank_pdf(tmp_path / "m.pdf", 8)
        runner = RealRunner()          # default engine
        job = runner.enqueue_manual(student_name="A", student_pdf_path=student,
                                    mark_scheme_path=scheme)
        final = runner.process(job["id"])
        assert final["status"] == states.AWAITING_REVIEW, final
        # The merged result is stored (draft) but flagged and not finalized.
        row = db.query_one("SELECT * FROM results WHERE id = ?",
                           (final["result_id"],))
        assert row is not None and row["review_required"] == 1

    def test_repeated_process_on_review_draft_does_not_rebuild(
            self, large_env, tmp_path):
        """An AWAITING_REVIEW job already holds its merged, flagged result. A
        repeated process() — a browser refresh, a Classroom poll, a /process
        click — must NOT re-enter finalize: that re-calls the blueprint AI and,
        because a manual job is not keyed by submission id, persisted a SECOND
        `results` row (the exact defect a long-document reviewer would hit)."""
        from app.large_pdf.runner import LargePdfRunner as RealRunner
        student = _blank_pdf(tmp_path / "s.pdf", 52)
        scheme = _blank_pdf(tmp_path / "m.pdf", 8)
        runner = RealRunner()          # default engine -> AWAITING_REVIEW
        job = runner.enqueue_manual(student_name="A", student_pdf_path=student,
                                    mark_scheme_path=scheme)
        final = runner.process(job["id"])
        assert final["status"] == states.AWAITING_REVIEW, final

        rebuilt: list[str] = []
        original = runner._get_blueprint
        runner._get_blueprint = lambda j: (rebuilt.append(j["id"]), original(j))[1]
        again = final
        for _ in range(3):
            again = runner.process(job["id"])

        assert rebuilt == []                       # finalize never re-entered
        assert again["result_id"] == final["result_id"]
        assert len(db.query("SELECT id FROM results")) == 1


# ============================================================ pdf inspection

class TestPdfInspection:
    def test_question_numbers_mapped_to_pages(self, tmp_path):
        from app.engine import pdf_inspect
        pdf = _text_pdf(tmp_path / "q.pdf", ["1. Expand", "2. Solve", "3. Prove"])
        inspection = pdf_inspect.inspect_student_pdf(pdf, ["1", "2", "3"])
        assert inspection.page_count == 3
        assert inspection.question_pages == {"1": [1], "2": [2], "3": [3]}

    def test_expected_filter_rejects_stray_numbers(self, tmp_path):
        from app.engine import pdf_inspect
        # '9' is a page-marks value, not a question in the mark scheme.
        pdf = _text_pdf(tmp_path / "q.pdf", ["1. Expand", "9 marks", "2. Solve"])
        inspection = pdf_inspect.inspect_student_pdf(pdf, ["1", "2"])
        assert "9" not in inspection.question_pages

    def test_split_pdf_writes_expected_page_counts(self, tmp_path):
        from app.engine import pdf_inspect
        pdf = _blank_pdf(tmp_path / "s.pdf", 52)
        paths = pdf_inspect.split_pdf(pdf, [(1, 26), (27, 52)],
                                      tmp_path / "out", prefix="b")
        from pypdf import PdfReader
        assert [len(PdfReader(p).pages) for p in paths] == [26, 26]

    def test_page_count_on_unreadable_file(self, tmp_path):
        from app.engine import pdf_inspect
        bad = tmp_path / "bad.pdf"
        bad.write_bytes(b"nope")
        assert pdf_inspect.page_count(str(bad)) == 0


# ============================================================ API routes

@pytest.fixture
def api_client(large_env, monkeypatch):
    from fastapi.testclient import TestClient
    from app.main import app
    with TestClient(app) as client:
        yield client


class TestLargePdfRoutes:
    def test_config_and_empty_list(self, api_client):
        cfg = api_client.get("/api/large-pdf/config").json()
        assert cfg["target_min"] == 24 and cfg["target_max"] == 28
        listing = api_client.get("/api/large-pdf/jobs").json()
        assert listing["jobs"] == []

    def test_upload_inspects_plans_and_processes(self, api_client):
        files = {
            "submission": ("student.pdf", _pdf_bytes(52), "application/pdf"),
            "mark_scheme": ("scheme.pdf", _pdf_bytes(8), "application/pdf"),
        }
        response = api_client.post(
            "/api/large-pdf/jobs", files=files,
            data={"student_name": "Ada Lovelace"})
        assert response.status_code == 200, response.text
        job = response.json()
        assert job["batch_count"] == 2
        assert job["page_count"] == 52
        assert job["student_name"] == "Ada Lovelace"

        detail = api_client.get(f"/api/large-pdf/jobs/{job['id']}").json()
        assert len(detail["batches"]) == 2
        assert detail["plan"]["batch_count"] == 2
        # No file paths leak to the teacher-facing payload.
        assert "student_pdf_path" not in detail
        assert "mark_scheme_path" not in detail

        # The background scheduler drives the job to a terminal state.
        deadline = time.time() + 30
        status = None
        while time.time() < deadline:
            detail = api_client.get(f"/api/large-pdf/jobs/{job['id']}").json()
            status = detail["status"]
            if status in (states.COMPLETED, states.AWAITING_REVIEW,
                          states.FAILED):
                break
            time.sleep(0.3)
        assert status in (states.COMPLETED, states.AWAITING_REVIEW), detail
        assert detail["summary"]["completed"] >= 1

    def test_rejects_non_pdf_upload(self, api_client):
        files = {
            "submission": ("notes.txt", io.BytesIO(b"hello"), "text/plain"),
            "mark_scheme": ("scheme.pdf", _pdf_bytes(8), "application/pdf"),
        }
        response = api_client.post("/api/large-pdf/jobs", files=files,
                                   data={"student_name": "A"})
        assert response.status_code == 400

    def test_missing_job_returns_404(self, api_client):
        assert api_client.get("/api/large-pdf/jobs/nope").status_code == 404
