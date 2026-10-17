"""Realistic malformed mark-scheme PDFs, built locally, for testing Stage A.

These reproduce the failure modes the brief calls out: two-column layouts whose
naive extraction order interleaves, page numbers that look exactly like
questions, tables full of numbers, questions split across pages, and a total
that disagrees with the sum of the questions.

No network, no AI, no student data — just reportlab writing PDFs that exercise
the layout reconstruction.
"""

from __future__ import annotations

from pathlib import Path

from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas

W, H = A4


def _mark(c: canvas.Canvas, x: float, y: float, text: str, size: int = 9) -> None:
    c.setFont("Helvetica", size)
    c.drawString(x, y, text)


def two_column_mark_scheme(path: Path, *, questions: int = 12,
                           per_column: int = 6) -> Path:
    """A two-column mark scheme where naive extraction interleaves Q1/Q12.

    Left column holds the first half of the questions, right column the rest -
    visually correct, but naive top-to-bottom extraction produces
    ``1, 7, 2, 8, 3, 9...`` which is exactly the failure we must survive.
    """
    c = canvas.Canvas(str(path), pagesize=A4)
    _mark(c, 40, H - 50, "MARK SCHEME", 14)
    _mark(c, 40, H - 70, "Total: 60 marks", 10)
    c.showPage()

    left = list(range(1, per_column + 1))
    right = list(range(per_column + 1, questions + 1))
    top = H - 60
    for i, q in enumerate(left):
        _mark(c, 40, top - i * 30, f"{q}   Working shown   [5 marks]")
    for i, q in enumerate(right):
        _mark(c, 320, top - i * 30, f"{q}   Working shown   [5 marks]")
    _mark(c, 40, 40, "Page 2")
    c.showPage()
    c.save()
    return path


def page_numbers_mark_scheme(path: Path) -> Path:
    """Page furniture: bare page numbers that must never become questions."""
    c = canvas.Canvas(str(path), pagesize=A4)
    for page in range(1, 4):
        _mark(c, 40, H - 40, "MARK SCHEME", 13)
        _mark(c, 40, H - 70, "Question 1   [4 marks]")
        _mark(c, 40, H - 95, "Question 2   [4 marks]")
        _mark(c, 40, H - 120, "Question 3   [4 marks]")
        _mark(c, 40, 30, "Page 2")
        c.drawRightString(W - 40, 30, "2 of 3")
        c.showPage()
    c.save()
    return path


def table_mark_scheme(path: Path) -> Path:
    """A table full of numbers that are marks, not question numbers."""
    c = canvas.Canvas(str(path), pagesize=A4)
    _mark(c, 40, H - 50, "MARK SCHEME", 13)
    _mark(c, 40, H - 80, "Q  Answer  Marks")
    for i, (q, m) in enumerate([(1, 3), (2, 5), (3, 2), (4, 6), (5, 4)]):
        y = H - 100 - i * 20
        _mark(c, 60, y, f"{q}")
        _mark(c, 140, y, "Acceptable answers listed")
        _mark(c, 380, y, f"{m}")
    _mark(c, 40, H - 220, "Examiner note: 2 marks for each correct statement (max 6)")
    c.showPage()
    c.save()
    return path


def page_break_mark_scheme(path: Path) -> Path:
    """One question starting on page 1 and continuing on page 2."""
    c = canvas.Canvas(str(path), pagesize=A4)
    _mark(c, 40, H - 50, "MARK SCHEME", 13)
    _mark(c, 40, H - 80, "Question 5   Exact value required")
    _mark(c, 40, H - 95, "M1 a correct exact value   [3 marks]")
    c.showPage()
    _mark(c, 40, H - 50, "MARK SCHEME (continued)", 13)
    _mark(c, 40, H - 80, "Question 5   continued")
    _mark(c, 40, H - 95, "A2 correctly rounded answer   [2 marks]")
    _mark(c, 40, H - 130, "Question 6   [4 marks]")
    c.showPage()
    c.save()
    return path


def compiled_image_only_mark_scheme(path: Path, *, pages: int = 9,
                                    per_page: int = 2) -> Path:
    """A multi-page, image-only compiled scheme whose questions are indexed.

    Mirrors the real document that exposed the 67-vs-69 loss: landscape pages,
    NO text layer at all, and a printed index that runs continuously across the
    pages (page N holds ``(N-1)*per_page + 1 .. N*per_page``). Built with PIL so
    the pages really are images - there is no text to extract, which is exactly
    why the whole-document read has to be verified page by page.
    """
    from PIL import Image, ImageDraw

    rendered: list[Image.Image] = []
    for page in range(1, pages + 1):
        img = Image.new("RGB", (1100, 850), "white")
        draw = ImageDraw.Draw(img)
        draw.text((30, 30), f"CLASSIFIED MARK SCHEME - page {page} of {pages}",
                  fill="black")
        for offset in range(per_page):
            index = (page - 1) * per_page + offset + 1
            top = 100 + offset * 260
            draw.text((40, top), f"{index}. June 05/4H/Q{index}   [4 marks]",
                      fill="black")
            draw.text((40, top + 40),
                      "M1 correct method   A1 answer   B1 units", fill="black")
            draw.text((40, top + 80), "Accept equivalent fractions.", fill="black")
        rendered.append(img)

    first, rest = rendered[0], rendered[1:]
    first.save(str(path), "PDF", resolution=150, save_all=True,
               append_images=rest)
    return path


def image_only_mark_scheme(path: Path) -> Path:
    """A PDF with no text layer at all - mirrors the real Ch1L7 scheme."""
    from PIL import Image, ImageDraw

    img = Image.new("RGB", (600, 800), "white")
    draw = ImageDraw.Draw(img)
    draw.text((30, 30), "MARK SCHEME (scanned)", fill="black")
    for i in range(12):
        draw.text((40, 80 + i * 40), f"Question {i + 1}   [3 marks]", fill="black")
    img.save(str(path), "PDF", resolution=150)
    return path