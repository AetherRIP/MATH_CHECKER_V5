"""Render the shared design system's new pieces to PNG for visual review.

Draws a framed portrait page with the certificate frame, a signature block and
an empty-state note, so the frame geometry, the corner ornaments and the
signature rule can be checked against the reference design by eye.
"""

from __future__ import annotations

import io
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from app.reporting import design as D  # noqa: E402

OUT = (pathlib.Path(__file__).resolve().parents[2]
       / "dev_local" / "reports" / "_pdf_preview")


def build() -> bytes:
    brand = D.load_branding({
        "teacher_name": "Mr. Abanoub Akram",
        "teacher_title": "IGCSE AND A LEVEL MATHEMATICS TEACHER",
        "center": "HLC Center — O Level",
        "year": "2026–2027",
        "quote": "Everything you do flourish and succeed",
        "monogram": "A",
    })
    theme = D.make_theme(accent=brand.accent or None, label="preview")
    styles = D.build_styles(theme)
    story: list = []

    D.add(story, D.stat_strip(theme, styles, [
        ("Students", "12"), ("Submitted", "11"), ("Missing", "1"),
        ("Grade 1", "2"),
    ]))
    D.add(story, D.section(theme, styles, "MISSING HOMEWORK",
                           subtitle="Grouped by the class each student is "
                                    "assigned to."))
    D.add(story, D.group_divider(theme, styles, "CLASS 10A",
                                 meta="3 students"))
    D.add(story, D.data_table(
        theme, styles, ["Student Name", "Missing Homework"],
        [["Mo", "L2 / L4"], ["Ahmed", "L4"], ["Sara Mohamed", "L1"]],
        widths=D.fit_widths(theme, [40, 60])))
    D.add(story, D.group_divider(theme, styles, "CLASS 10B", meta="1 student"))
    D.add(story, D.data_table(
        theme, styles, ["Student Name", "Missing Homework"],
        [["Mariam", "L3 / L5"]], widths=D.fit_widths(theme, [40, 60])))

    D.add(story, D.section(theme, styles, "STUDENTS WITH GRADE 1"))
    D.add(story, D.empty_state(
        theme, styles,
        "No students recorded a grade of 1 for the selected assignments."))

    D.add(story, D.signature_block(theme, styles, brand))

    buf = D.Report(theme=theme, branding=brand,
                   title="MISSING HOMEWORK REPORT", story=story,
                   context="3 assignments selected · 12 students",
                   label="FOR SIGNATURE",
                   doc_id="MC-demo001").build()
    return buf.getvalue()


def main() -> None:
    import fitz  # PyMuPDF

    OUT.mkdir(parents=True, exist_ok=True)
    data = build()
    (OUT / "class_report_preview.pdf").write_bytes(data)
    doc = fitz.open(stream=data, filetype="pdf")
    page = doc[0]
    print(f"pages={doc.page_count} "
          f"size={page.rect.width:.0f}x{page.rect.height:.0f}pt")
    print(f"orientation={'LANDSCAPE' if page.rect.width > page.rect.height else 'PORTRAIT'}")
    pix = page.get_pixmap(dpi=110)
    pix.save(str(OUT / "class_report_preview.png"))
    print("wrote", OUT / "class_report_preview.png")


if __name__ == "__main__":
    main()