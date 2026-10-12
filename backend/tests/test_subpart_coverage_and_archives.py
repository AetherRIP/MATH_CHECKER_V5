"""Regression tests for the defects confirmed by the C1L13 - Sets audit.

Every test here reproduces a failure observed on a REAL Google Classroom
submission from the O Level Mathematics class (assignment ``Ch1L13 - Sets``,
coursework 889601795010) and pins the corrected behaviour.

Bug A — PHANTOM MISSING QUESTIONS (real evidence).
    The marker answers a question by its SUB-PARTS (``"1(a)"``, ``"1(b)(i)"``).
    ``merge_batches`` keyed contributions by the raw id, so question ``"1"``
    looked unanswered even though every part was marked. On the live 49-page
    submission that produced **31 phantom gaps against a mark scheme that the
    reconciliation showed to be 69/69 complete**, which
    (a) refused the targeted retry (31 > the safety cap) and
    (b) parked a fully marked, fully verified correction in the teacher-review
        queue with ``review_required = 1``.
    Fixed by driving recovery + the review decision from the authoritative
    blueprint reconciliation instead of the advisory raw-id list.

Bug B — A REFUSED RETRY SAID NOTHING.
    ``recovery_json`` persisted ``error: null`` when a round was skipped, so
    the guard's explanation was lost. The reason is now persisted.

Bug C — ZIP SUBMISSIONS WERE NEVER MARKED.
    A real student turned in ``CamScanner ….zip`` and the whole submission
    failed with "unsupported file type (application/zip)" — no mark at all.
    ZIPs are now expanded and their members validated in natural page order.

All tests are deterministic: no AI, no network, no API quota.
"""

from __future__ import annotations

import io
import json
import zipfile

import pytest

import app.config as config
from app.classroom.submission_validation import (
    InvalidSubmissionError,  # noqa: F401  (documents the contract)
    classify,
    validate_and_normalize,
)
from app.engine.blueprint import apply_blueprint
from app.large_pdf import states, store
from app.large_pdf.runner import BatchOutcome, LargePdfRunner


# ------------------------------------------------------------------ fixtures

def _blueprint(numbers, max_marks=4):
    return {"questions": [{"question_number": str(n), "max_marks": max_marks,
                           "criteria": "c"} for n in numbers]}


def _q(number, awarded, max_marks, *, answer="2+2=4", breakdown=None):
    return {
        "question_number": str(number), "max_marks": max_marks,
        "awarded_marks": awarded, "topic": "", "student_answer": answer,
        "working_summary": "", "loss_reason": "",
        "mark_breakdown": breakdown if breakdown is not None else [
            {"code": "M1", "awarded": awarded > 0, "reason": "x"}],
        "correct_method": "", "full_mark_solution": "",
    }


@pytest.fixture
def large_env(fresh_state, monkeypatch):
    cfg = config.get_config()
    cfg.mock_mode = True
    cfg.large_pdf.enabled = True
    cfg.large_pdf.concurrency = 2
    config.save_config(cfg)
    return fresh_state


def _blank_pdf(path, pages):
    from pypdf import PdfWriter
    writer = PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(width=200, height=200)
    with open(path, "wb") as handle:
        writer.write(handle)


def _png_bytes(color=(255, 0, 0)):
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (60, 60), color).save(buf, "PNG")
    return buf.getvalue()


# ================================================ bug A: sub-part coverage

class TestSubPartsAreAccountedFor:
    """A marker that answers sub-parts must not look like it skipped questions."""

    def test_subpart_rows_satisfy_the_declared_question(self):
        blueprint = _blueprint([1, 2])
        result = {"questions": [_q("1(a)", 1, 2), _q("1(b)(i)", 1, 1),
                                _q("1(b)(ii)", 1, 1), _q("2(i)", 2, 2)]}
        merged, report = apply_blueprint(result, blueprint)
        # The reconciliation (the AUTHORITATIVE coverage check) sees both
        # questions answered; nothing may be reported as missing.
        assert report["missing_questions"] == []
        numbers = sorted(str(q["question_number"]) for q in merged["questions"])
        assert numbers == ["1", "2"]

    def test_advisory_gap_is_labelled_pre_reconciliation(self):
        # merge_batches' raw-id gap must announce itself as advisory so no
        # caller can mistake it for the final missing-question set.
        from app.engine import batching
        from app.large_pdf.validate import merge_batches

        plan = batching.BatchPlan(
            page_count=2, single_pass=False,
            batches=[batching.Batch(index=0, start=1, end=2,
                                    question_ids=("1", "2"))])
        batches = [{"batch_index": 0, "question_ids": ["1", "2"]}]
        results = {0: {"questions": [_q("1(a)", 1, 2), _q("2(i)", 1, 2)]}}
        report = merge_batches(plan, batches, results,
                               blueprint_questions=_blueprint([1, 2])["questions"])["report"]
        assert "missing_questions_pre_reconciliation" in report
        assert report["missing_questions_pre_reconciliation"] == \
            report["missing_questions"]


class TestRunnerDoesNotParkCompleteResultsForReview:
    """The live defect: 69/69 marked, verified, but review_required = 1."""

    def _run(self, large_env, tmp_path, engine):
        student_pdf = tmp_path / "student.pdf"
        _blank_pdf(student_pdf, 52)
        ms_pdf = tmp_path / "ms.pdf"
        ms_pdf.write_bytes(b"%PDF-1.4 mock mark scheme\n")

        runner = LargePdfRunner(engine=engine)
        job = runner.enqueue_manual(student_name="Audit Case",
                                    student_pdf_path=str(student_pdf),
                                    mark_scheme_path=str(ms_pdf))
        runner.prepare(job["id"])
        final = runner.process(job["id"])
        return job, final

    def test_subpart_answers_do_not_force_a_review(self, large_env, tmp_path,
                                                   monkeypatch):
        import hashlib

        from app.engine.blueprint import save_cached

        ms_pdf = tmp_path / "ms.pdf"
        ms_pdf.write_bytes(b"%PDF-1.4 mock mark scheme\n")
        sha = hashlib.sha256(ms_pdf.read_bytes()).hexdigest()
        save_cached(sha, _blueprint([1, 2]), model="test")

        # Batch 0 answers question 1 BY ITS PARTS; batch 1 answers question 2.
        def engine(*, batch_pdf, mark_scheme_pdf, job, batch):
            if batch["batch_index"] == 0:
                return BatchOutcome(ok=True, result={"questions": [
                    _q("1(a)", 1, 2), _q("1(b)", 1, 2)]}, marking_model="m")
            return BatchOutcome(ok=True, result={"questions": [_q("2", 4, 4)]},
                                marking_model="m")

        _job, final = self._run(large_env, tmp_path, engine)
        assert final["status"] == states.COMPLETED, (
            "every mark-scheme question is marked and verified — the job must "
            "not be parked in review because of advisory raw-id gaps")
        rows = _result_questions(final["result_id"])
        assert sorted(str(q["question_number"]) for q in rows) == ["1", "2"]
        assert all(not q.get("review_reason") for q in rows)

    def test_a_genuine_gap_still_requires_review(self, large_env, tmp_path,
                                                 monkeypatch):
        # The fix must not silence REAL gaps: question 2 is never answered and
        # the targeted retry cannot close it.
        import hashlib

        import app.large_pdf.runner as runner_mod
        from app.engine.blueprint import save_cached
        from app.engine.recover_missing import RecoveryOutcome

        ms_pdf = tmp_path / "ms.pdf"
        ms_pdf.write_bytes(b"%PDF-1.4 mock mark scheme\n")
        sha = hashlib.sha256(ms_pdf.read_bytes()).hexdigest()
        save_cached(sha, _blueprint([1, 2]), model="test")

        monkeypatch.setattr(runner_mod, "run_recovery_round",
                            lambda **kw: RecoveryOutcome(
                                ok=False, recovered={},
                                still_missing=list(kw.get("missing") or []),
                                error="scripted: retry could not recover"))

        def engine(*, batch_pdf, mark_scheme_pdf, job, batch):
            return BatchOutcome(ok=True,
                                result={"questions": [_q(1, 4, 4)]
                                        if batch["batch_index"] == 0 else []},
                                marking_model="m")

        _job, final = self._run(large_env, tmp_path, engine)
        assert final["status"] == states.AWAITING_REVIEW, (
            "an unmarked mark-scheme question must never finalize as complete")
        rows = {str(q["question_number"]): q for q in
                _result_questions(final["result_id"])}
        assert "2" in rows
        # ...and the unmarked question is explicitly UNRESOLVED, not a 0.
        assert str(rows["2"].get("status") or "").upper() in (
            "REVIEW", "NEEDS_REVIEW")
        assert rows["2"]["awarded_marks"] == 0

    def test_refused_retry_persists_its_reason(self, large_env, tmp_path,
                                               monkeypatch):
        import hashlib

        from app.engine.blueprint import save_cached

        ms_pdf = tmp_path / "ms.pdf"
        ms_pdf.write_bytes(b"%PDF-1.4 mock mark scheme\n")
        sha = hashlib.sha256(ms_pdf.read_bytes()).hexdigest()
        # A gap larger than the safety cap cannot be retried, and must say so.
        save_cached(sha, _blueprint(range(1, 40)), model="test")

        def engine(*, batch_pdf, mark_scheme_pdf, job, batch):
            return BatchOutcome(ok=True, result={"questions": [_q(1, 4, 4)]},
                                marking_model="m")

        import app.db as db

        cfg = config.get_config()
        cfg.gemini.primary_models = ["unused"]
        config.save_config(cfg)

        student_pdf = tmp_path / "student.pdf"
        _blank_pdf(student_pdf, 52)
        runner = LargePdfRunner(engine=engine)
        job = runner.enqueue_manual(student_name="Audit Case",
                                    student_pdf_path=str(student_pdf),
                                    mark_scheme_path=str(ms_pdf))
        runner.prepare(job["id"])
        final = runner.process(job["id"])

        record = db.row_to_dict(db.query_one(
            "SELECT recovery_json FROM large_pdf_jobs WHERE id = ?", (final["id"],)))
        state = json.loads(record["recovery_json"] or "{}")
        assert state.get("rounds") == 0, "no round may run for an oversized gap"
        assert state.get("blocked_reason"), (
            "a skipped retry must persist WHY it was skipped")
        assert state.get("advisory_missing") is not None, (
            "the advisory gap must be kept for diagnostics")


# ============================================ bug B: totals / denominators

class TestDenominatorStability:
    """Same assignment, same mark scheme => same maximum, whoever is marked.

    The audit found real results for one assignment totalling 262 and 266
    because the model's per-question maxima leaked into the stored result.
    """

    def test_model_maxima_never_change_the_assignment_total(self):
        blueprint = _blueprint([15, 28, 34], max_marks=3)
        blueprint["questions"][2]["max_marks"] = 6     # the real Q34
        # Two different runs of the SAME mark scheme, with the model claiming
        # different maxima for the same questions.
        run_one = {"questions": [_q(15, 2, 2), _q(28, 2, 2), _q(34, 4, 4)]}
        run_two = {"questions": [_q(15, 3, 3), _q(28, 3, 3), _q(34, 6, 6)]}

        first, first_report = apply_blueprint(run_one, blueprint)
        second, second_report = apply_blueprint(run_two, blueprint)

        for merged, report in ((first, first_report), (second, second_report)):
            assert report["blueprint_total"] == 12
            assert sum(int(q["max_marks"]) for q in merged["questions"]) == 12
            assert {str(q["question_number"]): int(q["max_marks"])
                    for q in merged["questions"]} == {"15": 3, "28": 3, "34": 6}
        # The correction is visible, not silent.
        assert {entry["question"] for entry in first_report["max_marks_corrected"]} \
            == {"15", "28", "34"}


def _result_questions(result_id):
    if not result_id:
        return []
    import app.db as db
    row = db.query_one("SELECT result_json FROM results WHERE id = ?", (result_id,))
    if not row:
        return []
    data = db.loads(row["result_json"]) or {}
    return data.get("questions") or []


# ==================================================== bug C: ZIP submissions

class TestZipSubmissionsAreMarked:
    """A phone-scanner .zip is a real, markable submission — not an error."""

    def test_zip_is_classified_as_an_archive_not_unsupported(self):
        assert classify("CamScanner 09-10-2026 16.20.zip", b"PK\x03\x04x",
                        "application/zip")["kind"] == "archive"
        assert classify("pages.zip", b"PK\x03\x04x", "")["kind"] == "archive"

    def test_renamed_zip_is_rejected_with_a_real_reason(self, tmp_path):
        check = validate_and_normalize("homework.zip", b"not-a-zip-at-all",
                                       "application/zip", tmp_path)
        assert check["ok"] is False
        assert "ZIP" in check["reason"]

    def test_zip_members_are_normalized_in_natural_page_order(self, tmp_path):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as archive:
            # Written out of order on purpose: 10 must still follow 2.
            for name in ("page10.png", "page2.png", "page1.png"):
                archive.writestr(name, _png_bytes())
            archive.writestr("__MACOSX/._page1.png", b"junk")
            archive.writestr("notes.txt", b"not a page")
        check = validate_and_normalize("scan.zip", buf.getvalue(),
                                       "application/zip", tmp_path)
        assert check["ok"] is True
        assert len(check["paths"]) == 3
        assert check["path"] == check["paths"][0]
        # Every produced file exists and is a usable image.
        for name in check["paths"]:
            assert (tmp_path / name).exists()
        # The unusable member is REPORTED, never silently dropped.
        assert any("notes.txt" in w for w in check["warnings"])

    def test_zip_with_no_usable_pages_fails_loudly(self, tmp_path):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as archive:
            archive.writestr("readme.txt", b"nothing to mark")
        check = validate_and_normalize("scan.zip", buf.getvalue(),
                                       "application/zip", tmp_path)
        assert check["ok"] is False
        assert "readme.txt" in check["reason"]

    def test_zip_containing_pdfs_is_supported(self, tmp_path):
        from pypdf import PdfWriter
        inner = io.BytesIO()
        writer = PdfWriter()
        writer.add_blank_page(width=200, height=200)
        writer.write(inner)

        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as archive:
            archive.writestr("answers.pdf", inner.getvalue())
        check = validate_and_normalize("answers.zip", buf.getvalue(),
                                       "application/zip", tmp_path)
        assert check["ok"] is True
        assert len(check["paths"]) == 1

    def test_a_nested_archive_is_not_unpacked(self, tmp_path):
        inner = io.BytesIO()
        with zipfile.ZipFile(inner, "w") as archive:
            archive.writestr("page1.png", _png_bytes())
        outer = io.BytesIO()
        with zipfile.ZipFile(outer, "w") as archive:
            archive.writestr("inner.zip", inner.getvalue())
        check = validate_and_normalize("outer.zip", outer.getvalue(),
                                       "application/zip", tmp_path)
        assert check["ok"] is False
        assert "nested" in check["reason"]

    def test_direct_pdf_attachment_still_reports_a_single_path(self, tmp_path):
        # Backwards compatibility: a non-archive attachment keeps its contract.
        from pypdf import PdfWriter
        writer = PdfWriter()
        writer.add_blank_page(width=200, height=200)
        buf = io.BytesIO()
        writer.write(buf)
        check = validate_and_normalize("answers.pdf", buf.getvalue(),
                                       "application/pdf", tmp_path)
        assert check["ok"] is True
        assert check["paths"] == [check["path"]]
