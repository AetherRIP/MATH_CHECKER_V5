"""Render the PDF design system to a file so it can be looked at.

Not a test - a design review aid. Run it, open the PDF, look at it.

    python scripts/preview_pdf_design.py [outfile.pdf]
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.reporting import design as D  # noqa: E402

MM = D.mm


def main(argv: list[str]) -> int:
    out = Path(argv[1] if len(argv) > 1 else "preview_design.pdf").resolve()
    theme = D.make_theme(accent="#B07C24", label="preview")
    brand = D.Branding(
        teacher_name="Mr. Abanoub Akram",
        teacher_title="IGCSE AND A LEVEL MATHEMATICS TEACHER",
        center="HLC Center - O Level",
        year="2026-2027",
        quote="Everything you do flourish and succeed",
        monogram="A",
        document_label="TEACHER COPY",
    )
    styles = D.build_styles(theme)
    story: list = []
    D.add(story, D.stat_strip(theme, styles, [
        ("Score", "68 / 75"), ("Percentage", "90.7%"),
        ("Questions", "12"), ("Marks lost", "7")], tone="accent"))
    D.add(story, D.Spacer(1, 6))
    D.add(story, D.section(theme, styles, "Student",
                           subtitle="Who this report is about"))
    D.add(story, D.key_values(theme, styles, [
        ("Student", "Mo"),
        ("Class", "10A"),
        ("Assignment", "Chapter 1 Lesson 7 Homework"),
        ("Date", "2026-03-14"),
        ("Marked by", "Gemini 2.5 Flash"),
    ]))
    D.add(story, D.Spacer(1, 6))
    D.add(story, D.section(theme, styles, "Action required"))
    D.add(story, D.callout(
        theme, styles, "Students Not Yet Joined Google Classroom",
        ["10B - Menatullah", "10C - Tala"],
        tone="warn",
        note="These students are on the local roster but have no Classroom "
             "record yet."))
    D.add(story, D.Spacer(1, 6))
    D.add(story, D.section(theme, styles, "Question breakdown"))
    rows = [
        ["Q1", "8 / 8", "Correct", "Fully correct working"],
        ["Q2", "5 / 7", "Partial", "Sign error in factorisation"],
        ["Q3", "10 / 10", "Correct", "Method clearly set out"],
        ["Q4", "0 / 5", "Incorrect", "Question not attempted"],
        ["Q5", "3 / 5", "Partial", "Correct setup, no conclusion"],
    ]
    tones = [D.status_tone(r[2]) for r in rows]
    D.add(story, D.data_table(
        theme, styles,
        ["Question", "Score", "Status", "Comment"], rows,
        widths=[22 * MM, 22 * MM, 26 * MM, theme.width - 70 * MM],
        aligns=["left", "right", "left", "left"], row_tones=tones))
    D.add(story, D.Spacer(1, 6))
    D.add(story, D.group_divider(theme, styles, "Class 10A", meta="6 students"))
    D.add(story, D.data_table(
        theme, styles, ["Student", "Missing homework"],
        [["Abdulrahman Mohammed-Ali", "CH1L1 / CH1L2 / CH1L3"],
         ["Tala", "CH1L4"],
         ["Menatullah", "CH1L2 / CH2L1"]],
        widths=[theme.width * 0.45, theme.width * 0.55]))
    # Enough rows to force a page break, so the running header, the repeated
    # column header and "Page X of Y" are all exercised.
    long_rows = [
        [f"Student {i}", f"CH{i % 7 + 1}L{i % 12 + 1} Homework",
         f"{i * 3 % 9 + 1} / {i * 7 % 13 + 1}"]
        for i in range(1, 46)
    ]
    D.add(story, D.Spacer(1, 6))
    D.add(story, D.section(theme, styles, "Class 10B",
                           subtitle="A deliberately long table"))
    D.add(story, D.data_table(
        theme, styles,
        ["Student", "Missing homework", "Marks"], long_rows,
        widths=[theme.width * 0.36, theme.width * 0.40,
                theme.width * 0.24]))

    report = D.Report(theme=theme, branding=brand,
                      title="Student Correction Report", story=story,
                      context="Mo - Chapter 1 Lesson 7 Homework",
                      doc_id="MC-2dee7a17")
    buf = report.build()
    out.write_bytes(buf.getvalue())
    print(f"wrote {out} ({out.stat().st_size} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
