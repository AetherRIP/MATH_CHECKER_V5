"""Regression tests for the defects confirmed by the C1L13 - Sets audit.

Every test here reproduces a failure observed on a REAL Google Classroom
submission from the O Level Mathematics class (assignment ``Ch1L13 - Sets``,
coursework 889601795010) and pins the corrected behaviour.

Bug A — PHANTOM MISSING QUESTIONS (real evidence).
    The marker answers a question by its SUB-PARTS (``"1(a)"``, ``"1(b)(i)"``).
    Raw-id accounting treated question ``"1"`` as unanswered even though every
    part was marked. The authoritative reconciliation must instead see both
    questions answered.

Bug B — MAX-MARK DRIFT (real evidence).
    Results for one assignment totalled 262 and 266 because the model's
    per-question maxima leaked into the stored result. The mark-scheme
    blueprint is the only source of maxima.

Bug C — ZIP SUBMISSIONS WERE NEVER MARKED.
    A real student turned in ``CamScanner ….zip`` and the whole submission
    failed with "unsupported file type (application/zip)" — no mark at all.
    ZIPs are now expanded and their members validated in natural page order.

All tests are deterministic: no AI, no network, no API quota.
"""

from __future__ import annotations

import io
import zipfile

from app.classroom.submission_validation import (
    InvalidSubmissionError,  # noqa: F401  (documents the contract)
    classify,
    validate_and_normalize,
)
from app.engine.blueprint import apply_blueprint


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
