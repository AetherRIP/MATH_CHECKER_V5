"""Render a Markdown document from ``documentation/`` as a shareable PDF.

Deliberately small and dependency-light: reportlab only (already required by the
report generator), no network, no browser. It understands the subset of
Markdown the project's own documentation uses — headings, paragraphs, bullet and
numbered lists, ``- [ ]`` checklists, pipe tables, fenced code blocks, ``**bold**``,
``*italic*`` and `` `code` `` — because a documentation pipeline that silently
drops tables or checklists is worse than no pipeline.

The standard PDF fonts (Helvetica) cover WinAnsi only, so characters outside it
(a right arrow, for example) are transliterated and reported instead of being
rendered as empty boxes.

Usage:
    python scripts/make_guide_pdf.py documentation/MATH_CHECKER_ASSISTANT_GUIDE.md

Writes the PDF beside the Markdown file (same stem) unless --out is given.
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import (BaseDocTemplate, Frame, KeepTogether, PageTemplate,
                                Paragraph, Preformatted, Spacer, Table, TableStyle)

# ---------------------------------------------------------------- transliteration

#: Characters WinAnsi cannot encode, mapped to a plain equivalent. Anything else
#: outside Latin-1 is replaced with a space and reported, so a glyph can never
#: silently become an empty box in the PDF.
_TRANSLITERATE = {
    "\u2192": "->",   # right arrow
    "\u2190": "<-",   # left arrow
    "\u2194": "<->",  # left-right arrow
    "\u21d2": "=>",
    "\u2264": "<=",
    "\u2265": ">=",
    "\u2260": "!=",
    "\u2713": "[ok]",
    "\u2714": "[ok]",
    "\u2717": "[x]",
    "\u2610": "[ ]",
    "\u2022": "-",
    "\u2018": "'", "\u2019": "'", "\u201c": '"', "\u201d": '"',
    "\u2013": "-", "\u2014": "-",   # en/em dash stay readable as hyphens
    "\u2026": "...",
    "\u00a0": " ",
}


def transliterate(text: str) -> tuple[str, list[str]]:
    """Make ``text`` encodable by the standard PDF fonts."""
    dropped: list[str] = []
    out: list[str] = []
    for char in text:
        if char in _TRANSLITERATE:
            out.append(_TRANSLITERATE[char])
            continue
        try:
            char.encode("cp1252")
        except UnicodeEncodeError:
            dropped.append(char)
            out.append(" ")
            continue
        out.append(char)
    return "".join(out), dropped


# ---------------------------------------------------------------- inline markup

def inline(text: str) -> str:
    """Escape XML, then apply the inline Markdown this project uses."""
    text = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    text = re.sub(r"`([^`]+)`", r'<font face="Courier" size="8.6">\1</font>', text)
    text = re.sub(r"\*\*([^*]+)\*\*", r"<b>\1</b>", text)
    text = re.sub(r"(?<!\*)\*([^*\s][^*]*?)\*(?!\*)", r"<i>\1</i>", text)
    return text


# ---------------------------------------------------------------- block parsing

HEADING = re.compile(r"^(#{1,6})\s+(.*)$")
BULLET = re.compile(r"^(\s*)[-*]\s+(.*)$")
CHECKBOX = re.compile(r"^(\s*)[-*]\s+\[( |x|X)\]\s+(.*)$")
NUMBERED = re.compile(r"^(\s*)(\d+)\.\s+(.*)$")
TABLE_ROW = re.compile(r"^\|(.*)\|\s*$")
RULE = re.compile(r"^\s*(-{3,}|\*{3,}|_{3,})\s*$")


def split_table_row(line: str) -> list[str]:
    cells = line.strip().strip("|").split("|")
    return [cell.strip() for cell in cells]


def build_story(markdown: str, styles) -> tuple[list, list[str]]:
    story: list = []
    dropped: list[str] = []
    lines = markdown.splitlines()
    index = 0
    paragraph: list[str] = []
    bullets: list[tuple[str, str]] = []      # (marker, text)
    numbered: list[tuple[str, str]] = []

    def flush_paragraph():
        if paragraph:
            text = " ".join(part.strip() for part in paragraph).strip()
            if text:
                story.append(Paragraph(inline(text), styles["Body"]))
                story.append(Spacer(1, 2.4 * mm))
            paragraph.clear()

    def flush_lists():
        for marker, text in bullets + numbered:
            story.append(Paragraph(f"{marker}&nbsp;&nbsp;{inline(text)}",
                                   styles["Item"]))
        if bullets or numbered:
            story.append(Spacer(1, 2.2 * mm))
        bullets.clear()
        numbered.clear()

    def flush_all():
        flush_paragraph()
        flush_lists()

    while index < len(lines):
        raw = lines[index]
        line = raw.rstrip()
        stripped = line.strip()

        # fenced code block -------------------------------------------------
        if stripped.startswith("```"):
            flush_all()
            index += 1
            block: list[str] = []
            while index < len(lines) and not lines[index].strip().startswith("```"):
                block.append(lines[index])
                index += 1
            index += 1
            story.append(Preformatted("\n".join(block), styles["Code"]))
            story.append(Spacer(1, 3 * mm))
            continue

        # table -------------------------------------------------------------
        if TABLE_ROW.match(line):
            flush_all()
            rows: list[list[str]] = []
            while index < len(lines) and TABLE_ROW.match(lines[index].rstrip()):
                rows.append(split_table_row(lines[index]))
                index += 1
            # drop the |---|---| separator row
            if len(rows) >= 2 and all(set(c) <= set("-: ") for c in rows[1]):
                rows.pop(1)
            if rows:
                width = max(len(r) for r in rows)
                rows = [r + [""] * (width - len(r)) for r in rows]
                data = []
                for row_number, row in enumerate(rows):
                    style = styles["TableHead"] if row_number == 0 else styles["TableCell"]
                    data.append([Paragraph(inline(cell), style) for cell in row])
                available = A4[0] - 2 * 18 * mm
                first = min(available * 0.30, 52 * mm)
                rest = (available - first) / max(1, width - 1)
                widths = [first] + [rest] * (width - 1)
                table = Table(data, colWidths=widths, repeatRows=1, hAlign="LEFT")
                table.setStyle(TableStyle([
                    ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#eef1f7")),
                    ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#b9c0cc")),
                    ("VALIGN", (0, 0), (-1, -1), "TOP"),
                    ("LEFTPADDING", (0, 0), (-1, -1), 4),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 4),
                    ("TOPPADDING", (0, 0), (-1, -1), 3),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
                ]))
                story.append(table)
                story.append(Spacer(1, 3.4 * mm))
            continue

        # horizontal rule ----------------------------------------------------
        if RULE.match(line):
            flush_all()
            story.append(Spacer(1, 2 * mm))
            index += 1
            continue

        # heading ------------------------------------------------------------
        match = HEADING.match(line)
        if match:
            flush_all()
            level = len(match.group(1))
            text = match.group(2).strip()
            story.append(Paragraph(inline(text), styles[f"H{min(level, 4)}"]))
            story.append(Spacer(1, 1.4 * mm if level >= 3 else 2.6 * mm))
            index += 1
            continue

        # lists --------------------------------------------------------------
        box = CHECKBOX.match(line)
        if box:
            flush_paragraph()
            bullets.append(("[ ]", box.group(3)))
            index += 1
            continue
        bullet = BULLET.match(line)
        if bullet:
            flush_paragraph()
            bullets.append(("-", bullet.group(2)))
            index += 1
            continue
        numbered_item = NUMBERED.match(line)
        if numbered_item:
            flush_paragraph()
            numbered.append((f"{numbered_item.group(2)}.", numbered_item.group(3)))
            index += 1
            continue

        # blank / paragraph ---------------------------------------------------
        if not stripped:
            flush_all()
            index += 1
            continue
        paragraph.append(line)
        index += 1

    flush_all()
    return story, dropped


# ---------------------------------------------------------------- rendering

def make_styles():
    base = getSampleStyleSheet()
    styles = {
        "H1": ParagraphStyle("H1", parent=base["Heading1"], fontName="Helvetica-Bold",
                             fontSize=19, leading=23, spaceBefore=2, spaceAfter=4,
                             textColor=colors.HexColor("#16233b")),
        "H2": ParagraphStyle("H2", parent=base["Heading2"], fontName="Helvetica-Bold",
                             fontSize=14.5, leading=18, spaceBefore=8, spaceAfter=3,
                             textColor=colors.HexColor("#1c2b47")),
        "H3": ParagraphStyle("H3", parent=base["Heading3"], fontName="Helvetica-Bold",
                             fontSize=11.8, leading=15, spaceBefore=6, spaceAfter=2,
                             textColor=colors.HexColor("#22345a")),
        "H4": ParagraphStyle("H4", parent=base["Heading4"], fontName="Helvetica-Bold",
                             fontSize=10.8, leading=14, spaceBefore=5, spaceAfter=2),
        "Body": ParagraphStyle("Body", parent=base["BodyText"], fontName="Helvetica",
                               fontSize=10.2, leading=14.2, alignment=TA_LEFT,
                               spaceAfter=1),
        "Item": ParagraphStyle("Item", parent=base["BodyText"], fontName="Helvetica",
                               fontSize=10.2, leading=13.8, leftIndent=6, spaceAfter=1.6),
        "Code": ParagraphStyle("Code", parent=base["Code"], fontName="Courier",
                               fontSize=8.6, leading=11, backColor=colors.HexColor("#f4f6fa"),
                               borderPadding=4, leftIndent=4),
        "TableHead": ParagraphStyle("TableHead", parent=base["BodyText"],
                                    fontName="Helvetica-Bold", fontSize=9.2, leading=12),
        "TableCell": ParagraphStyle("TableCell", parent=base["BodyText"],
                                    fontName="Helvetica", fontSize=9.2, leading=12),
    }
    return styles


def decorate(canvas, doc):
    canvas.saveState()
    canvas.setFont("Helvetica", 7.6)
    canvas.setFillColor(colors.HexColor("#66708a"))
    canvas.drawString(18 * mm, 11 * mm, "Math Checker - Assistant User Guide")
    canvas.drawRightString(A4[0] - 18 * mm, 11 * mm, f"Page {doc.page}")
    canvas.setStrokeColor(colors.HexColor("#d6dbe6"))
    canvas.setLineWidth(0.4)
    canvas.line(18 * mm, 14.5 * mm, A4[0] - 18 * mm, 14.5 * mm)
    canvas.restoreState()


def render(markdown_path: Path, pdf_path: Path) -> tuple[int, list[str]]:
    from reportlab.platypus import PageBreak  # noqa: F401  (kept for future use)

    text = markdown_path.read_text(encoding="utf-8")
    text, dropped_block = transliterate(text)
    styles = make_styles()
    story, dropped_inline = build_story(text, styles)

    doc = BaseDocTemplate(
        str(pdf_path), pagesize=A4,
        leftMargin=18 * mm, rightMargin=18 * mm, topMargin=16 * mm, bottomMargin=18 * mm,
        title="Math Checker - Assistant User Guide",
        author="Math Checker", subject="How to correct homework safely with Math Checker")
    frame = Frame(doc.leftMargin, doc.bottomMargin, doc.width, doc.height, id="body")
    doc.addPageTemplates([PageTemplate(id="main", frames=[frame], onPage=decorate)])
    doc.build(story)
    return doc.page, sorted(set(dropped_block + dropped_inline))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("markdown", help="path to the Markdown file")
    parser.add_argument("--out", default=None, help="output PDF path")
    args = parser.parse_args()

    source = Path(args.markdown)
    if not source.is_file():
        print(f"no such file: {source}")
        return 2
    target = Path(args.out) if args.out else source.with_suffix(".pdf")
    pages, dropped = render(source, target)
    size_kb = target.stat().st_size / 1024
    print(f"wrote {target} ({pages} page(s), {size_kb:.0f} KB)")
    if dropped:
        print("characters replaced (no glyph in the PDF font):",
              " ".join(f"U+{ord(c):04X}" for c in dropped))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
