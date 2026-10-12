"""Create small, deterministic fixture PDFs for real correction testing.

Produces data/fixtures/student_submission.pdf and mark_scheme.pdf — a tiny
4-question Edexcel-style quiz with an official-style mark scheme, so the
friend's engine (and V5) have genuinely markable input without consuming
large PDF processing.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import DATA_DIR  # noqa: E402
from fpdf import FPDF  # noqa: E402


def write_pdf(path: Path, title: str, lines: list[str]) -> None:
    pdf = FPDF()
    pdf.add_page()
    pdf.set_font("Helvetica", "B", 14)
    pdf.cell(0, 10, title, new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Helvetica", "", 11)
    pdf.ln(2)
    for line in lines:
        pdf.set_x(pdf.l_margin)
        pdf.multi_cell(190, 6.5, line)
    path.parent.mkdir(parents=True, exist_ok=True)
    pdf.output(str(path))


SCHEME = [
    "",
    "Total: 10 marks",
    "",
    "Question 1 (3 marks): Solve 4x + 7 = 31.",
    "  M1: Subtract 7 from both sides (4x = 24 seen or implied).",
    "  M1: Divide by 4.",
    "  A1: x = 6 (cao).",
    "",
    "Question 2 (2 marks): Write 0.36 as a fraction in its simplest form.",
    "  M1: 36/100 seen.",
    "  A1: 9/25 (cao).",
    "",
    "Question 3 (3 marks): The nth term of a sequence is 3n - 2.",
    "  B1: 1st term = 1 (or sequence 1, 4, 7, 10 seen).",
    "  M1: Sets 3n - 2 = 40.",
    "  A1: n = 14 (cao).",
    "",
    "Question 4 (2 marks): Work out (2^3)^2.",
    "  M1: 2^6 seen (or 8 squared).",
    "  A1: 64 (cao).",
]

STUDENT = [
    "",
    "Name: Fixture Student",
    "",
    "Question 1:",
    "4x + 7 = 31",
    "4x = 24",
    "x = 6",
    "",
    "Question 2:",
    "0.36 = 36/100 = 9/25",
    "",
    "Question 3:",
    "First term: 3(1) - 2 = 1",
    "3n - 2 = 40",
    "3n = 40",
    "n = 13.33",
    "",
    "Question 4:",
    "(2^3)^2 = 8^2 = 64",
]


def main() -> None:
    fixture_dir = DATA_DIR / "fixtures"
    write_pdf(fixture_dir / "mark_scheme.pdf",
              "Fixture Mark Scheme - Edexcel-style quiz (10 marks)", SCHEME)
    write_pdf(fixture_dir / "student_submission.pdf",
              "Fixture Student Submission", STUDENT)
    print("fixtures written:")
    print(" ", fixture_dir / "mark_scheme.pdf")
    print(" ", fixture_dir / "student_submission.pdf")


if __name__ == "__main__":
    main()
