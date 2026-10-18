"""One read per PDF: mark-scheme evidence reuse and planner file handling.

The planner used to open the mark scheme twice (page count, then full text
analysis) and the student PDF twice (page count, then per-page question
evidence) for every long-document job. Both are read-only deterministic
operations, so the fix is to derive everything from ONE read and to reuse the
derived mark-scheme evidence for the SAME file (keyed by path/size/mtime, so a
replaced scheme is always re-read).

These tests pin that behaviour, including explicit invalidation. They use
synthetic PDFs only.
"""

from __future__ import annotations

import os

import pytest
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas

from app.engine import pdf_inspect
from app.engine import mark_scheme as mark_scheme_mod
from app.large_pdf import planner


def _make_pdf(path, pages: int, *, prefix: str = "1.") -> str:
    # Deliberately no page-number "furniture" in the text: the analyser types
    # header/footer lines as page numbers and would (correctly) exclude them.
    doc = canvas.Canvas(str(path), pagesize=A4)
    for i in range(pages):
        doc.drawString(60, 700, f"{prefix} answer x = 7 (M1 A1)")
        doc.showPage()
    doc.save()
    return str(path)


@pytest.fixture(autouse=True)
def _clear_cache():
    pdf_inspect._SCHEME_CACHE.clear()
    yield
    pdf_inspect._SCHEME_CACHE.clear()


def test_same_file_is_analysed_once(tmp_path, monkeypatch):
    scheme = _make_pdf(tmp_path / "scheme.pdf", 3)
    calls = []
    real = mark_scheme_mod.analyse_document

    def counting(path):
        calls.append(path)
        return real(path)

    monkeypatch.setattr(mark_scheme_mod, "analyse_document", counting)

    first_questions, first_pages = pdf_inspect.scheme_evidence(scheme)
    second_questions, second_pages = pdf_inspect.scheme_evidence(scheme)

    assert len(calls) == 1
    assert first_pages == 3 == second_pages
    assert first_questions == second_questions
    assert first_questions  # the synthetic scheme declares question numbers


def test_replaced_file_is_analysed_again(tmp_path, monkeypatch):
    scheme = _make_pdf(tmp_path / "scheme.pdf", 2)
    calls = []
    real = mark_scheme_mod.analyse_document

    def counting(path):
        calls.append(path)
        return real(path)

    monkeypatch.setattr(mark_scheme_mod, "analyse_document", counting)
    pdf_inspect.scheme_evidence(scheme)

    # Rewrite the SAME path with different content and an explicitly newer
    # mtime: a replaced mark scheme must never be served from the old cache.
    _make_pdf(tmp_path / "scheme.pdf", 5)
    stat = os.stat(scheme)
    os.utime(scheme, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000))

    questions, pages = pdf_inspect.scheme_evidence(scheme)
    assert len(calls) == 2
    assert pages == 5
    assert questions


def test_unreadable_scheme_is_not_cached(tmp_path, monkeypatch):
    missing = str(tmp_path / "absent.pdf")
    calls = []
    real = mark_scheme_mod.analyse_document

    def counting(path):
        calls.append(path)
        return real(path)

    monkeypatch.setattr(mark_scheme_mod, "analyse_document", counting)

    assert pdf_inspect.scheme_evidence(missing) == ([], 0)
    assert pdf_inspect.scheme_evidence(missing) == ([], 0)
    # A transient read failure must not stick to the path.
    assert len(calls) == 2
    assert pdf_inspect._SCHEME_CACHE == {}


def test_planner_opens_each_pdf_once(tmp_path, monkeypatch):
    student = _make_pdf(tmp_path / "student.pdf", 40, prefix="1.")
    scheme = _make_pdf(tmp_path / "scheme.pdf", 3)

    analyses = []
    inspections = []
    real_analyse = mark_scheme_mod.analyse_document
    real_inspect = pdf_inspect.inspect_student_pdf

    def counting_analyse(path):
        analyses.append(path)
        return real_analyse(path)

    def counting_inspect(path, expected=None):
        inspections.append(path)
        return real_inspect(path, expected)

    monkeypatch.setattr(mark_scheme_mod, "analyse_document", counting_analyse)
    monkeypatch.setattr(pdf_inspect, "inspect_student_pdf", counting_inspect)

    prepared = planner.prepare(student, scheme)

    assert prepared.blocked is False
    assert prepared.page_count == 40
    assert prepared.batch_count >= 2
    assert len(analyses) == 1
    assert len(inspections) == 1
