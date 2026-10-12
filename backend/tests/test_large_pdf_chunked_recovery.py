"""Chunked targeted recovery for a LARGE unmarked gap.

Real case this reproduces (49-page O Level submission, Ch1L13 - Sets): one
batch answered a fraction of its pages, so 32 mark-scheme questions (128 marks)
came back UNMARKED. The single-round recovery gate refused any gap bigger than
12 questions, so the entire gap stayed unresolved purely because of its SIZE.

The recovery path now re-marks the gap in bounded chunks (never a full re-mark):
at most ``large_pdf.recovery_chunk_questions`` questions per request, at most
``large_pdf.recovery_max_chunks`` requests per job, progress persisted after
every chunk, and every question that survives the retries keeps an explicit
reason naming what was tried.

The batch engine AND the recovery engine are injected, so no API quota is used.
"""

from __future__ import annotations

import hashlib
import json

import pytest

import app.config as config
import app.db as db
from app.engine.blueprint import save_cached
from app.engine.recover_missing import RecoveryOutcome
from app.large_pdf import states, store
from app.large_pdf.runner import BatchOutcome, LargePdfRunner

#: 40 questions x 4 marks. Batch 1 answers 1-8; batch 2 answers nothing, which
#: leaves questions 9-40 UNMARKED = 32 questions / 128 marks — the live shape.
SCHEME_QUESTIONS = 40
MARKS_PER_QUESTION = 4
GAP = [str(n) for n in range(9, SCHEME_QUESTIONS + 1)]
GAP_MARKS = len(GAP) * MARKS_PER_QUESTION


def _blueprint(numbers) -> dict:
    return {"questions": [{"question_number": str(n),
                           "max_marks": MARKS_PER_QUESTION,
                           "criteria": ""} for n in numbers]}


def _q(number, awarded, max_marks=MARKS_PER_QUESTION):
    return {
        "question_number": str(number), "max_marks": max_marks,
        "awarded_marks": awarded, "topic": "", "student_answer": "work shown",
        "working_summary": "", "loss_reason": "",
        "mark_breakdown": [{"code": "M1", "awarded": awarded > 0, "reason": "x"}],
        "correct_method": "", "full_mark_solution": "",
    }


def _blank_pdf(path, pages: int = 49) -> str:
    from pypdf import PdfWriter
    writer = PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(width=200, height=200)
    with open(path, "wb") as handle:
        writer.write(handle)
    return str(path)


@pytest.fixture
def large_env(fresh_state):
    cfg = config.get_config()
    cfg.mock_mode = True
    cfg.large_pdf.enabled = True
    cfg.large_pdf.concurrency = 2
    cfg.large_pdf.max_retries = 1
    config.save_config(cfg)
    return fresh_state


class _RecoverySpy:
    """Scripted recovery engine: records each chunk, answers per a rule."""

    def __init__(self, answer=None, error=None):
        self.chunks: list[list[str]] = []
        self._answer = answer or (lambda chunk: list(chunk))
        self._error = error

    def __call__(self, **kwargs):
        chunk = list(kwargs["chunk"])
        self.chunks.append(chunk)
        recovered = {str(n): _q(n, 2) for n in self._answer(chunk)}
        still = [q for q in chunk if str(q) not in recovered]
        return RecoveryOutcome(ok=not still, recovered=recovered,
                               still_missing=still, error=self._error)

    @property
    def questions_asked(self) -> list[str]:
        return [q for chunk in self.chunks for q in chunk]


def _run(tmp_path, recovery, *, engine_script=None, prepare_recovery=None,
         chunk_size=None, max_chunks=None):
    """Mark a 49-page submission whose second batch returns nothing."""
    ms_pdf = tmp_path / "ms.pdf"
    ms_pdf.write_bytes(b"%PDF-1.4 mock mark scheme\n")
    save_cached(hashlib.sha256(ms_pdf.read_bytes()).hexdigest(),
                _blueprint(range(1, SCHEME_QUESTIONS + 1)), model="test")

    student_pdf = _blank_pdf(tmp_path / "student.pdf", 49)

    def engine(*, batch_pdf, mark_scheme_pdf, job, batch):
        rows = ([_q(n, 2) for n in range(1, 9)]
                if batch["batch_index"] == 0 else [])
        return BatchOutcome(ok=True, result={"questions": rows},
                            marking_model="m")

    cfg = config.get_config()
    if chunk_size is not None:
        cfg.large_pdf.recovery_chunk_questions = chunk_size
    if max_chunks is not None:
        cfg.large_pdf.recovery_max_chunks = max_chunks
    config.save_config(cfg)

    runner = LargePdfRunner(engine=engine, recovery_engine=recovery)
    job = runner.enqueue_manual(student_name="Student A",
                                student_pdf_path=str(student_pdf),
                                mark_scheme_path=str(ms_pdf))
    prepared = runner.prepare(job["id"])
    assert prepared["batch_count"] == 2
    if prepare_recovery:
        db.execute("UPDATE large_pdf_jobs SET recovery_json = ? WHERE id = ?",
                   (json.dumps(prepare_recovery), job["id"]))
    final = runner.process(job["id"])
    return runner, job, final


def _stored_rows(result_id):
    row = db.query_one("SELECT * FROM results WHERE id = ?", (result_id,))
    return json.loads(row["result_json"]), row


def _recovery_state(job_id):
    record = db.query_one("SELECT recovery_json FROM large_pdf_jobs "
                          "WHERE id = ?", (job_id,))
    return json.loads(record["recovery_json"] or "{}")


# ============================================================ chunked recovery

class TestChunkedRecovery:
    def test_a_32_question_gap_is_re_marked_in_bounded_chunks(
            self, large_env, tmp_path):
        recovery = _RecoverySpy()
        _runner, job, final = _run(tmp_path, recovery)

        # Bounded requests: six chunks of at most six questions cover all 32.
        assert [len(c) for c in recovery.chunks] == [6, 6, 6, 6, 6, 2]
        assert sorted(recovery.questions_asked, key=int) == sorted(GAP, key=int)
        assert len(recovery.chunks) <= \
            config.get_config().large_pdf.recovery_max_chunks

        state = _recovery_state(job["id"])
        assert state["rounds"] == 6
        assert len(state["recovered"]) == len(GAP)
        assert state["blocked_reason"] is None
        assert [entry["chunk"] for entry in state["chunks"]] == list(range(6))

        # Every question now has a real mark: nothing is left unresolved, so
        # the correction completes instead of parking for review.
        stored, row = _stored_rows(final["result_id"])
        assert final["status"] == states.COMPLETED, final
        rows = {str(q["question_number"]): q for q in stored["questions"]}
        for number in GAP:
            assert rows[number]["awarded_marks"] == 2
            assert rows[number]["max_marks"] == MARKS_PER_QUESTION
            assert str(rows[number].get("status") or "").upper() not in (
                "REVIEW", "NEEDS_REVIEW", "UNMARKED")
        assert row["review_required"] == 0
        assert stored["totals"]["unresolved_marks"] == 0
        assert stored["totals"]["score_basis"] == "COMPLETE"
        # 8 fully-marked + 32 half-marked questions of 4 marks each.
        assert stored["totals"]["earned"] == 8 * 2 + 32 * 2
        assert stored["totals"]["available"] == SCHEME_QUESTIONS * MARKS_PER_QUESTION

    def test_chunk_size_and_budget_are_configuration_driven(
            self, large_env, tmp_path):
        recovery = _RecoverySpy()
        _runner, job, final = _run(tmp_path, recovery, chunk_size=4,
                                   max_chunks=2)

        assert [len(c) for c in recovery.chunks] == [4, 4]
        state = _recovery_state(job["id"])
        assert state["rounds"] == 2
        # The rest is explicitly DEFERRED, not silently dropped.
        assert len(state["deferred"]) == len(GAP) - 8

        stored, row = _stored_rows(final["result_id"])
        assert final["status"] == states.AWAITING_REVIEW
        rows = {str(q["question_number"]): q for q in stored["questions"]}
        unresolved = [rows[n] for n in state["deferred"]]
        assert unresolved
        for q in unresolved:
            assert q["status"] == "NEEDS_REVIEW"
            assert q["awarded_marks"] == 0        # unmarked, never a fake 0
            assert "targeted retry re-marked" in q["review_reason"]
            assert q["retry_outcome"]["chunks_run"] == 2
        assert stored["totals"]["unresolved_marks"] == \
            len(state["deferred"]) * MARKS_PER_QUESTION

    def test_partial_recovery_leaves_an_explicit_reason_per_question(
            self, large_env, tmp_path):
        # Each chunk answers only its first question, so most of the gap stays.
        recovery = _RecoverySpy(answer=lambda chunk: chunk[:1])
        _runner, job, final = _run(tmp_path, recovery)

        state = _recovery_state(job["id"])
        assert state["rounds"] == 6
        stored, row = _stored_rows(final["result_id"])
        assert final["status"] == states.AWAITING_REVIEW
        rows = {str(q["question_number"]): q for q in stored["questions"]}
        still_missing = [q for q in state["missing_reported"]
                         if q not in state["recovered"]]
        assert still_missing
        for number in still_missing:
            q = rows[number]
            assert q["status"] == "NEEDS_REVIEW"
            assert "UNMARKED (not a confirmed 0)" in q["review_reason"]
            assert "6 bounded chunk(s)" in q["review_reason"]
            assert q["retry_outcome"]["still_unmarked"] == still_missing
        # A recovered question really did get its mark.
        recovered = state["recovered"]
        assert recovered
        assert rows[recovered[0]]["awarded_marks"] == 2

    def test_retry_errors_are_kept_on_the_unresolved_questions(
            self, large_env, tmp_path):
        recovery = _RecoverySpy(answer=lambda chunk: [], error="503 unavailable")
        _runner, job, final = _run(tmp_path, recovery)

        state = _recovery_state(job["id"])
        assert state["error"] == "503 unavailable"
        assert all(entry["error"] == "503 unavailable"
                   for entry in state["chunks"])
        stored, _row = _stored_rows(final["result_id"])
        rows = {str(q["question_number"]): q for q in stored["questions"]}
        assert "last retry error: 503 unavailable" in \
            rows[GAP[0]]["review_reason"]


# ============================================================ budget / refusals

class TestRecoveryBudgetAndRefusals:
    def test_spent_budget_is_never_re_spent(self, large_env, tmp_path):
        recovery = _RecoverySpy()
        _runner, job, final = _run(
            tmp_path, recovery,
            prepare_recovery={"rounds": 8, "recovered": []})

        assert recovery.chunks == []          # no further AI request
        state = _recovery_state(job["id"])
        assert state["rounds"] == 8
        assert "budget of 8 request(s)" in (state["blocked_reason"] or "")

        stored, _row = _stored_rows(final["result_id"])
        assert final["status"] == states.AWAITING_REVIEW
        rows = {str(q["question_number"]): q for q in stored["questions"]}
        assert "Why no retry ran" in rows[GAP[0]]["review_reason"]
        assert stored["totals"]["score_basis"] == "MARKED_ONLY"

    def test_progress_is_durable_across_a_repeated_finalize(
            self, large_env, tmp_path):
        recovery = _RecoverySpy()
        runner, job, final = _run(tmp_path, recovery)
        assert len(recovery.chunks) == 6

        # A second process() call must not re-run (or re-bill) any chunk: the
        # job is terminal and the recorded rounds are authoritative.
        runner.process(job["id"])
        assert len(recovery.chunks) == 6
        assert _recovery_state(job["id"])["rounds"] == 6
        assert len(db.query("SELECT id FROM results")) == 1

    def test_an_almost_whole_paper_gap_is_refused_with_a_reason(
            self, large_env, tmp_path):
        # Only question 1 is marked: 39 of 40 questions unmarked is a broken
        # run / broken scheme extraction, not a marker omission.
        ms_pdf = tmp_path / "ms.pdf"
        ms_pdf.write_bytes(b"%PDF-1.4 mock mark scheme\n")
        save_cached(hashlib.sha256(ms_pdf.read_bytes()).hexdigest(),
                    _blueprint(range(1, SCHEME_QUESTIONS + 1)), model="test")
        student_pdf = _blank_pdf(tmp_path / "student.pdf", 49)

        def engine(*, batch_pdf, mark_scheme_pdf, job, batch):
            rows = [_q(1, 4)] if batch["batch_index"] == 0 else []
            return BatchOutcome(ok=True, result={"questions": rows},
                                marking_model="m")

        recovery = _RecoverySpy()
        runner = LargePdfRunner(engine=engine, recovery_engine=recovery)
        job = runner.enqueue_manual(student_name="A",
                                    student_pdf_path=str(student_pdf),
                                    mark_scheme_path=str(ms_pdf))
        runner.prepare(job["id"])
        final = runner.process(job["id"])

        assert recovery.chunks == []          # refused before any AI call
        state = _recovery_state(job["id"])
        assert state["rounds"] == 0
        assert "essentially the whole paper" in state["blocked_reason"]

        stored, _row = _stored_rows(final["result_id"])
        assert final["status"] == states.AWAITING_REVIEW
        rows = {str(q["question_number"]): q for q in stored["questions"]}
        assert "Why no retry ran" in rows["2"]["review_reason"]

    def test_no_gap_means_no_recovery_at_all(self, large_env, tmp_path):
        ms_pdf = tmp_path / "ms.pdf"
        ms_pdf.write_bytes(b"%PDF-1.4 mock mark scheme\n")
        save_cached(hashlib.sha256(ms_pdf.read_bytes()).hexdigest(),
                    _blueprint([1, 2]), model="test")
        student_pdf = _blank_pdf(tmp_path / "student.pdf", 10)

        def engine(*, batch_pdf, mark_scheme_pdf, job, batch):
            return BatchOutcome(ok=True,
                                result={"questions": [_q(1, 4), _q(2, 4)]},
                                marking_model="m")

        recovery = _RecoverySpy()
        runner = LargePdfRunner(engine=engine, recovery_engine=recovery)
        job = runner.enqueue_manual(student_name="A",
                                    student_pdf_path=str(student_pdf),
                                    mark_scheme_path=str(ms_pdf))
        final = runner.process(job["id"])

        assert recovery.chunks == []
        assert final["status"] == states.COMPLETED
        stored, _row = _stored_rows(final["result_id"])
        assert stored["totals"]["score_basis"] == "COMPLETE"


# ============================================ file/key ownership (real 403s)

class TestRecoveryUploadsUseTheMarkingClient:
    """Gemini Files are scoped to the key that uploaded them.

    On real repair runs every recovery chunk of an affected submission died
    with ``403 PERMISSION_DENIED — You do not have permission to access the
    File <id>``: the upload helper built its OWN client, so the client that
    ran the requests never learned which key owned the file and its cross-key
    re-upload guard could not fire. The upload must go through the same
    client instance that marks the chunks.
    """

    def test_recovery_upload_binds_to_the_chunk_client(self, large_env, tmp_path,
                                                       monkeypatch):
        import app.large_pdf.runner as runner_mod

        ms_pdf = tmp_path / "ms.pdf"
        ms_pdf.write_bytes(b"%PDF-1.4 mock mark scheme\n")
        save_cached(hashlib.sha256(ms_pdf.read_bytes()).hexdigest(),
                    _blueprint(range(1, SCHEME_QUESTIONS + 1)), model="test")
        student_pdf = _blank_pdf(tmp_path / "student.pdf", 49)

        class FakeUploadClient:
            def __init__(self):
                self.uploaded: list[tuple[str, bool]] = []

            def upload_file(self, path, purpose="submission", shared=False):
                self.uploaded.append((purpose, shared))
                return f"{purpose}:remote"

            def release_job_files(self):
                pass

        fake = FakeUploadClient()
        monkeypatch.setattr(runner_mod.LargePdfRunner, "_recovery_client",
                            lambda self: fake)

        captured: dict = {}

        def recovery_engine(**kwargs):
            captured["client"] = kwargs["client"]
            return RecoveryOutcome(ok=False, recovered={},
                                   still_missing=list(kwargs["chunk"]))

        def engine(*, batch_pdf, mark_scheme_pdf, job, batch):
            rows = ([_q(n, 2) for n in range(1, 9)]
                    if batch["batch_index"] == 0 else [])
            return BatchOutcome(ok=True, result={"questions": rows},
                                marking_model="m")

        runner = LargePdfRunner(engine=engine, recovery_engine=recovery_engine)
        job = runner.enqueue_manual(student_name="Student A",
                                    student_pdf_path=str(student_pdf),
                                    mark_scheme_path=str(ms_pdf))
        runner.prepare(job["id"])
        runner.process(job["id"])

        # The chunks were marked by the very client that uploaded the files…
        assert captured["client"] is fake
        # …and that client really did own the uploads (so a rotation can
        # re-upload them instead of 403ing on the file).
        assert ("submission", False) in fake.uploaded
        assert ("mark_scheme", True) in fake.uploaded


# ============================================================ planner unit

class TestRecoveryPlanner:
    def test_planner_chunks_any_size_gap_within_budget(self):
        from app.engine.recover_missing import plan_chunked_recovery
        plan = plan_chunked_recovery(missing=[str(n) for n in range(38, 70)],
                                     scheme_questions=69)
        assert [len(c.questions) for c in plan.chunks] == [6, 6, 6, 6, 6, 2]
        assert plan.deferred == []
        assert plan.blocked_reason is None

    def test_planner_defers_what_the_budget_cannot_cover(self):
        from app.engine.recover_missing import plan_chunked_recovery
        plan = plan_chunked_recovery(missing=[str(n) for n in range(1, 41)],
                                     max_chunks=3, scheme_questions=100)
        assert len(plan.chunks) == 3
        assert len(plan.deferred) == 22

    def test_planner_refuses_without_a_structureless_scheme_basis(self):
        from app.engine.recover_missing import plan_chunked_recovery
        plan = plan_chunked_recovery(missing=["1", "2"])
        assert len(plan.chunks) == 1        # small gap: nothing to refuse
        plan = plan_chunked_recovery(missing=[str(n) for n in range(1, 40)],
                                     scheme_questions=40)
        assert plan.chunks == []
        assert "essentially the whole paper" in plan.blocked_reason

    def test_planner_reports_a_spent_budget(self):
        from app.engine.recover_missing import plan_chunked_recovery
        plan = plan_chunked_recovery(missing=["5"], already_attempted=8)
        assert plan.chunks == []
        assert "budget" in plan.blocked_reason
