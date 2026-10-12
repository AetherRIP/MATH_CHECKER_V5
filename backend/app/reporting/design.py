"""The Math Correct PDF design system.

Why this module exists
----------------------
Before this, every report hand-rolled its own table style, its own header and
its own footer. Five generators, five near-identical ``TableStyle`` blocks, one
shared-navy header, and no way to change the letterhead without editing Python.

This module is the single source of visual truth. A report generator composes
components from here and never touches raw ``TableStyle``:

    theme      - palette, type scale, page geometry
    branding   - who the letterhead belongs to (from Settings, never hard-coded)
    styles     - the paragraph styles derived from the theme
    ReportDoc  - page geometry + running header/footer + "Page X of Y"

Design rules encoded here (not repeated in each report)
------------------------------------------------------
* **Hierarchy over decoration.** A report is a document, not a spreadsheet:
  one dominant title, a few section headings, and tables that support them.
* **Whitespace is a component.** Section spacing is part of the system, so
  reports breathe without any generator inventing its own spacing.
* **Never rely on colour alone.** Every semantic state ships a WORD as well as
  a colour ("Correct", "Partial", "Review"), so a grayscale printout is still
  unambiguous (§13).
* **Print-safe text.** reportlab's base-14 fonts cover WinAnsi only; a stray
  "✓" or emoji becomes a black box. :func:`safe_text` folds the characters
  teachers actually use into ones the built-in fonts can render, and drops what
  they cannot.
* **Nothing strands.** Headings use ``KeepTogether`` with their content, tables
  repeat their header row across pages, and long tables get a lighter running
  header on continuation pages (§16, §17).
"""

from __future__ import annotations

import io
import os
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Iterable, Sequence
from xml.sax.saxutils import escape as _xml_escape

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_LEFT, TA_RIGHT
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.platypus import (
    Flowable,
    Frame,
    KeepTogether,
    PageTemplate,
    Paragraph,
    Spacer,
    Table,
    TableStyle,
)
from reportlab.platypus import BaseDocTemplate

# ---------------------------------------------------------------------------
# Text safety
# ---------------------------------------------------------------------------

#: Characters outside WinAnsi mapped to the nearest one the base-14 fonts can
#: actually draw. Teachers paste these from Word all the time, and an unmapped
#: glyph prints as a filled box - the single most common "why does my PDF look
#: broken" cause.
_GLYPH_FOLD = {
    "\u2018": "'", "\u2019": "'", "\u201a": "'", "\u201b": "'",
    "\u201c": '"', "\u201d": '"', "\u201e": '"', "\u201f": '"',
    "\u2010": "-", "\u2011": "-", "\u2012": "-", "\u2013": "-", "\u2014": "-",
    "\u2212": "-", "\u2043": "-",
    "\u2026": "...", "\u2022": "-", "\u00b7": "-", "\u00a0": " ",
    "\u202f": " ", "\u2009": " ", "\u2007": " ", "\u200a": " ",
    "\u2192": "->", "\u2190": "<-", "\u21d2": "=>", "\u21d0": "<=",
    "\u2713": "OK", "\u2714": "OK", "\u2717": "X", "\u2718": "X",
    "\u26a0": "!", "\u25cf": "*", "\u25cb": "o", "\u25b2": "^",
    "\u25bc": "v", "\u2588": "#", "\u2591": ".", "\u2592": ":",
    "\u2261": "=", "\u2260": "!=", "\u2264": "<=", "\u2265": ">=",
    "\u00d7": "x", "\u00f7": "/", "\u2032": "'", "\u2033": "''",
    "\u00a9": "(c)", "\u00ae": "(r)", "\u2122": "(tm)",
}

#: Anything left that the base-14 fonts cannot draw (emoji, box drawing, CJK
#: without an embedded font) is dropped rather than printed as a black box.
_DROP = re.compile(r"[^\x09\x0a\x0d\x20-\x7e\xa1-\xff]")


#: Separator used in composed page furniture. ASCII on purpose: reportlab's
#: base-14 Type1 fonts have no glyph for the typographic middot, so a real one
#: prints as an empty box.
SEP = "   |   "


def safe_text(value: Any) -> str:
    """Make arbitrary text printable with reportlab's built-in fonts.

    Folds typographic punctuation and common symbols to WinAnsi equivalents and
    drops anything still unrenderable. Never raises, never returns ``None`` -
    a report must not fail to build because a student pasted an emoji into a
    name.
    """
    if value is None:
        return ""
    text = str(value)
    for src, dst in _GLYPH_FOLD.items():
        if src in text:
            text = text.replace(src, dst)
    text = _DROP.sub("", text)
    return text


def esc(value: Any) -> str:
    """XML-escape and print-safe a value for use inside a ``Paragraph``."""
    return _xml_escape(safe_text(value), {'"': "&quot;"})


# ---------------------------------------------------------------------------
# Theme
# ---------------------------------------------------------------------------


def _hex(value: str | None, default: str) -> colors.Color:
    """Parse ``#RRGGBB``; anything else falls back instead of raising."""
    text = str(value or "").strip()
    if re.fullmatch(r"#[0-9a-fA-F]{6}", text):
        return colors.HexColor(text)
    if re.fullmatch(r"#[0-9a-fA-F]{3}", text):
        text = "#" + "".join(c * 2 for c in text[1:])
        return colors.HexColor(text)
    return colors.HexColor(default)


@dataclass(frozen=True)
class Palette:
    """Semantic colours. Names describe MEANING, never appearance."""

    ink: colors.Color          # headings, primary text
    body: colors.Color         # body copy
    faint: colors.Color         # metadata, captions
    primary: colors.Color      # brand navy: table headers, rules
    accent: colors.Color       # single highlight, sparingly
    line: colors.Color         # hairlines and table grid
    zebra: colors.Color         # alternating row tint
    paper: colors.Color         # callout / stat backgrounds
    #: The page itself. The reference certificate is printed on warm ivory
    #: stock, and that single choice is most of why it reads as "official"
    #: rather than as a browser printout. Tables stay white so they lift off
    #: it.
    sheet: colors.Color
    ok: colors.Color
    warn: colors.Color
    err: colors.Color
    info: colors.Color
    mute: colors.Color


#: Ink navy with a single warm accent. Restrained on purpose: an accent used
#: on more than ~5% of a page stops being an accent.
DEFAULT_PALETTE = {
    "ink": "#111C2E",
    "body": "#2A3648",
    "faint": "#6B7A8F",
    "primary": "#1B3A6B",
    "accent": "#B07C24",
    # Warm neutrals, so rules and row tints sit on the ivory page instead of
    # reading as grey blotches on it.
    "line": "#DAD2C2",
    "zebra": "#F7F2E6",
    "paper": "#F4EFE2",
    "sheet": "#FBF7EF",
}

_TONES = {
    "ok": ("#1B6E45", "#E4F2EA"),
    "warn": ("#8A5A00", "#FBF0DA"),
    "err": ("#A32318", "#FBE7E5"),
    "info": ("#1B4F8A", "#E5EDF8"),
    "mute": ("#5B6779", "#EFF1F4"),
    "accent": (None, None),     # resolved from the palette
}

#: Distance from the trimmed page edge to the outer rule of the certificate
#: frame, and the clear space between that rule and the text block.
FRAME_INSET = 9 * mm
FRAME_GUTTER = 5 * mm
#: Length of the gold corner bracket that sits inside the double rule.
FRAME_CORNER = 13 * mm


@dataclass(frozen=True)
class Theme:
    """Page geometry, type scale and spacing for one document."""

    pagesize: tuple[float, float]
    left: float
    right: float
    top: float
    bottom: float
    palette: Palette
    font: str = "Helvetica"
    font_bold: str = "Helvetica-Bold"
    font_italic: str = "Helvetica-Oblique"
    font_serif: str = "Times-Roman"
    font_serif_italic: str = "Times-Italic"
    label: str = "document"
    #: Draw the certificate frame (double rule + corner ornaments). When it is
    #: on, the page margins already leave room for it (§16).
    framed: bool = True

    # -- derived geometry -------------------------------------------------
    @property
    def width(self) -> float:
        return self.pagesize[0] - self.left - self.right

    @property
    def height(self) -> float:
        return self.pagesize[1] - self.top - self.bottom

    @property
    def is_landscape(self) -> bool:
        return self.pagesize[0] > self.pagesize[1]

    def tone(self, name: str) -> tuple[colors.Color, colors.Color]:
        """(ink, background) for a semantic state, never colour alone."""
        key = (name or "mute").lower()
        if key in ("accent", "primary"):
            return (self.palette.accent, self.palette.paper)
        if key not in _TONES:
            key = "mute"
        fg, bg = _TONES[key]
        return (_hex(fg, "#5B6779"), _hex(bg, "#EFF1F4"))


def make_theme(*, accent: str | None = None, landscape_mode: bool = False,
               label: str = "document", framed: bool = True) -> Theme:
    """Build a theme, honouring the teacher's accent colour when set.

    ``landscape_mode`` still exists for the wide appendix tables, but every
    teacher-facing report is built in A4 PORTRAIT (§4): a landscape page
    squeezes names, lesson codes and marks until they stop being readable.
    """
    pal = dict(DEFAULT_PALETTE)
    if accent:
        pal["accent"] = accent
    pal["sheet"] = DEFAULT_PALETTE["sheet"]
    palette = Palette(**{k: _hex(v, DEFAULT_PALETTE.get(k, "#FBF7EF"))
                         for k, v in pal.items()},
                      ok=_hex(_TONES["ok"][0], "#1B6E45"),
                      warn=_hex(_TONES["warn"][0], "#8A5A00"),
                      err=_hex(_TONES["err"][0], "#A32318"),
                      info=_hex(_TONES["info"][0], "#1B4F8A"),
                      mute=_hex(_TONES["mute"][0], "#5B6779"))
    size = landscape(A4) if landscape_mode else A4
    # The certificate frame lives in the page margin, so a framed document
    # needs a deeper margin than an unframed one to stay clear of it.
    pad = FRAME_INSET + FRAME_GUTTER if framed else 0
    return Theme(
        pagesize=size,
        left=pad + 6 * mm, right=pad + 6 * mm,
        top=pad + (16 * mm if landscape_mode else 14 * mm),
        bottom=pad + 12 * mm,
        palette=palette, label=label, framed=framed)


# ---------------------------------------------------------------------------
# Branding
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Branding:
    """Who this letterhead belongs to. Every field may legitimately be blank."""

    teacher_name: str = ""
    teacher_title: str = ""
    center: str = ""
    year: str = ""
    quote: str = ""
    logo_path: str = ""
    #: Absolute or DATA_DIR-relative path to the teacher's signature image.
    #: Empty falls back to the bundled signature, then to a drawn rule (§20).
    signature_path: str = ""
    monogram: str = ""
    footer_note: str = ""
    document_label: str = ""
    accent: str = ""            # #RRGGBB or "" for the design system default

    @property
    def is_configured(self) -> bool:
        return bool(self.teacher_name or self.center or self.logo_path
                    or self.monogram)

    def subtitle_line(self) -> str:
        bits = [self.center, f"Academic Year {self.year}" if self.year else ""]
        return safe_text(SEP.join(b for b in bits if b))


def _resolve_logo(path: str) -> str:
    """Resolve a configured logo path; return "" when it is not usable.

    A missing or unreadable logo must degrade to the monogram, never take
    the whole report down.
    """
    text = str(path or "").strip()
    if not text:
        return ""
    candidates = [text]
    if not os.path.isabs(text):
        try:
            from ..config import DATA_DIR
            candidates.append(str(DATA_DIR / text))
        except Exception:  # noqa: BLE001 - branding must never break a report
            pass
    for candidate in candidates:
        if os.path.isfile(candidate) and os.path.getsize(candidate) > 0:
            return candidate
    return ""


#: Honorifics skipped when deriving a monogram from the teacher's name.
_HONORIFICS = frozenset({
    "mr", "mrs", "ms", "miss", "mx", "dr", "prof", "professor", "eng",
    "sir", "madam", "madame", "sheikh", "ustaz",
})


#: OPT-IN identity marks that live beside the reporting code.
#:
#: These files are the build owner's personal logo and signature, so they are
#: NEVER used unless the config explicitly asks for them
#: (``[reports] use_bundled_assets = true``), and a release does not ship them
#: at all. A teacher's own ``logo_path`` / ``signature_path`` always wins; with
#: nothing configured, the report uses the drawn monogram and no signature -
#: so nobody else's identity can end up on a report.
ASSET_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets")


def _bundled_asset(name: str) -> str:
    """Absolute path of a shipped branding asset, or "" when unavailable."""
    path = os.path.join(ASSET_DIR, name)
    try:
        if os.path.isfile(path) and os.path.getsize(path) > 0:
            return path
    except OSError:  # noqa: BLE001 - a broken assets folder is not fatal
        pass
    return ""


def _bundled_logo(cfg) -> str:
    """The shipped logo, only when the config opted in. Otherwise ""."""
    if not getattr(cfg.reports, "use_bundled_assets", False):
        return ""
    return _bundled_asset("logo.png")


def _bundled_signature(cfg) -> str:
    """The shipped signature, only when the config opted in. Otherwise ""."""
    if not getattr(cfg.reports, "use_bundled_assets", False):
        return ""
    return _bundled_asset("signature.png")


def load_branding(overrides: dict | None = None) -> Branding:
    """Read the letterhead from Settings, tolerating a missing/broken config."""
    teacher = center = ""
    try:
        from ..config import get_config
        cfg = get_config()
        teacher = str(cfg.teacher.teacher_name or "")
        center = str(cfg.reports.center_name or cfg.teacher.school_name or "")
        brand = Branding(
            teacher_name=teacher,
            teacher_title=cfg.reports.teacher_title or "",
            center=center,
            year=cfg.reports.academic_year or "",
            quote=cfg.reports.quote or "",
            logo_path=_resolve_logo(cfg.reports.logo_path)
            or _bundled_logo(cfg),
            signature_path=_resolve_logo(
                getattr(cfg.reports, "signature_path", ""))
            or _bundled_signature(cfg),
            monogram=cfg.reports.monogram or "",
            footer_note=cfg.reports.footer_note or "",
            document_label=cfg.reports.document_label or "",
            accent=cfg.reports.accent or "",
        )
    except Exception:  # noqa: BLE001 - never block a PDF on config trouble
        brand = Branding()
    if overrides:
        data = {k: v for k, v in overrides.items() if v not in (None, "")}
        brand = Branding(**{**brand.__dict__, **data})
    if not brand.monogram and brand.teacher_name:
        # Default the monogram to the teacher's OWN initial, which is what a
        # one-letter mark almost always is anyway. An honorific is skipped so
        # "Ms. Dana Miller" yields "D" and not "M" - the mark must come from
        # the teacher's name, never from a shipped default.
        words = [w for w in safe_text(brand.teacher_name).split()
                 if w.strip(".,").lower() not in _HONORIFICS]
        initial = (words[0] if words else safe_text(brand.teacher_name)).strip(
            ".,")[:1].upper()
        brand = Branding(**{**brand.__dict__, "monogram": initial})
    return brand


# ---------------------------------------------------------------------------
# Type scale
# ---------------------------------------------------------------------------


def build_styles(theme: Theme) -> dict[str, ParagraphStyle]:
    """The one type scale. Sizes are points, leading is ~1.35x for body copy."""
    p = theme.palette
    common = dict(fontName=theme.font, textColor=p.body,
                  spaceAfter=0, spaceBefore=0)
    styles = {
        # Document identity
        "title": ParagraphStyle(
            "mcTitle", fontName=theme.font_bold, fontSize=20, leading=24,
            textColor=p.ink, spaceAfter=2),
        "eyebrow": ParagraphStyle(
            "mcEyebrow", fontName=theme.font_bold, fontSize=7.6, leading=9.6,
            textColor=p.accent, spaceAfter=3),
        "label": ParagraphStyle(
            "mcLabel", fontName=theme.font_bold, fontSize=7.4, leading=9,
            textColor=p.faint, spaceAfter=0),
        # Letterhead
        "teacher": ParagraphStyle(
            "mcTeacher", fontName=theme.font_bold, fontSize=13, leading=15.5,
            textColor=p.ink, spaceAfter=1),
        "teacher_sub": ParagraphStyle(
            "mcTeacherSub", fontName=theme.font, fontSize=7.6, leading=10,
            textColor=p.faint, spaceAfter=0),
        "quote": ParagraphStyle(
            "mcQuote", fontName=theme.font_serif_italic, fontSize=9.2,
            leading=12.4, textColor=p.accent, spaceAfter=0),
        # Section hierarchy
        "h2": ParagraphStyle(
            "mcH2", fontName=theme.font_bold, fontSize=12, leading=15,
            textColor=p.ink, spaceBefore=0, spaceAfter=4),
        "h3": ParagraphStyle(
            "mcH3", fontName=theme.font_bold, fontSize=9.4, leading=12,
            textColor=p.primary, spaceBefore=0, spaceAfter=2.5),
        "h4": ParagraphStyle(
            "mcH4", fontName=theme.font_bold, fontSize=8.6, leading=11,
            textColor=p.body, spaceBefore=0, spaceAfter=2),
        # Body
        "body": ParagraphStyle("mcBody", fontSize=9.2, leading=12.6, **common),
        "body_tight": ParagraphStyle(
            "mcBodyTight", fontName=theme.font, fontSize=8.4, leading=11.2,
            textColor=p.body, spaceAfter=0, spaceBefore=0),
        "meta": ParagraphStyle(
            "mcMeta", fontName=theme.font, fontSize=7.6, leading=10,
            textColor=p.faint, spaceAfter=0, spaceBefore=0),
        "cell": ParagraphStyle(
            "mcCell", fontName=theme.font, fontSize=8.4, leading=11.0,
            textColor=p.body, spaceAfter=0, spaceBefore=0),
        "cell_head": ParagraphStyle(
            "mcCellHead", fontName=theme.font_bold, fontSize=7.8, leading=10,
            textColor=colors.white, spaceAfter=0, spaceBefore=0),
        "cell_key": ParagraphStyle(
            "mcCellKey", fontName=theme.font_bold, fontSize=7.8, leading=10.4,
            textColor=p.faint, spaceAfter=0, spaceBefore=0),
        "stat_value": ParagraphStyle(
            "mcStatValue", fontName=theme.font_bold, fontSize=16.5, leading=19,
            textColor=p.ink, alignment=TA_LEFT, spaceAfter=0, spaceBefore=0),
        "stat_label": ParagraphStyle(
            "mcStatLabel", fontName=theme.font_bold, fontSize=7.0, leading=8.8,
            textColor=p.faint, spaceAfter=0, spaceBefore=0),
        "bullet": ParagraphStyle(
            "mcBullet", fontName=theme.font, fontSize=8.8, leading=12.0,
            textColor=p.body, leftIndent=10, bulletIndent=0, spaceAfter=1.8,
            bulletFontName=theme.font_bold, bulletColor=p.accent,
            spaceBefore=0),
        "footer": ParagraphStyle(
            "mcFooter", fontName=theme.font, fontSize=6.8, leading=8.6,
            textColor=p.faint, spaceAfter=0, spaceBefore=0),
    }
    for name in ("right", "center"):
        styles[f"cell_{name}"] = ParagraphStyle(
            f"mcCell{name.capitalize()}", parent=styles["cell"],
            alignment=TA_RIGHT if name == "right" else TA_CENTER)
    return styles


# ---------------------------------------------------------------------------
# Custom flowables
# ---------------------------------------------------------------------------


class Monogram(Flowable):
    """A small, sharp monogram mark: a solid block with a swept diagonal.

    Drawn rather than shipped as an asset so a teacher always has a real mark
    with no file to upload - and it stays crisp at any size because it is
    vector, not a bitmap.
    """

    def __init__(self, letters: str, theme: Theme, size: float = 13 * mm):
        super().__init__()
        self.letters = safe_text(letters).strip()[:2].upper() or "M"
        self.theme = theme
        self.size = size
        self.width = size
        self.height = size

    def draw(self) -> None:
        c = self.canv
        p = self.theme.palette
        s = self.size
        c.saveState()
        # Swept base: a parallelogram under a solid square, so the mark reads
        # as designed rather than as a letter in a box.
        c.setFillColor(p.primary)
        c.rect(0, s * 0.30, s, s * 0.70, stroke=0, fill=1)
        c.setFillColor(p.paper)
        p_ = c.beginPath()
        p_.moveTo(0, s * 0.30)
        p_.lineTo(s, s * 0.30)
        p_.lineTo(s, s * 0.30 + s * 0.10)
        p_.lineTo(0, s * 0.30 + s * 0.20)
        p_.close()
        c.drawPath(p_, stroke=0, fill=1)
        c.setFillColor(p.paper)
        c.setFont(self.theme.font_bold, s * 0.56)
        c.drawCentredString(s / 2.0, s * 0.44, self.letters)
        c.restoreState()


class ProgressBar(Flowable):
    """A slim progress bar. Always paired with the number beside it.

    Colour alone would be lost in a grayscale print, so the caller is expected
    to print the percentage as text too (§13).
    """

    def __init__(self, theme: Theme, fraction: float, width: float = 46 * mm,
                 height: float = 3.2 * mm, tone: str = "ok"):
        super().__init__()
        self.theme = theme
        try:
            value = float(fraction)
        except (TypeError, ValueError):
            value = 0.0
        self.fraction = max(0.0, min(1.0, value if value <= 1.0 else value / 100.0))
        self.width = width
        self.height = height
        self.tone = tone

    def draw(self) -> None:
        c = self.canv
        fg, _bg = self.theme.tone(self.tone)
        c.saveState()
        c.setFillColor(self.theme.palette.paper)
        c.rect(0, 0, self.width, self.height, stroke=0, fill=1)
        c.setFillColor(fg)
        c.rect(0, 0, self.width * self.fraction, self.height, stroke=0, fill=1)
        c.restoreState()


class DiamondRule(Flowable):
    """A hairline broken by a small filled diamond (§32).

    The divider detail of the reference certificate: it reads as ornament
    rather than as a separator, and because it is drawn it stays perfectly
    sharp at any print resolution.
    """

    def __init__(self, theme: Theme, width: float = 64 * mm, *,
                 thickness: float = 0.6, diamond: float = 1.9 * mm,
                 gap: float = 2.8 * mm, color: colors.Color | None = None):
        super().__init__()
        self.theme = theme
        self.width = width
        self.height = max(5.0, diamond + 3.0)
        self.thickness = thickness
        self.diamond = diamond
        self.gap = gap
        self.color = color or theme.palette.accent

    def draw(self) -> None:
        c = self.canv
        mid = self.height / 2.0
        half = self.width / 2.0
        reach = half - self.gap - self.diamond
        c.saveState()
        c.setStrokeColor(self.color)
        c.setLineWidth(self.thickness)
        if reach > 0:
            c.line(0, mid, reach, mid)
            c.line(self.width - reach, mid, self.width, mid)
        c.setFillColor(self.color)
        path = c.beginPath()
        path.moveTo(half, mid + self.diamond / 2.0)
        path.lineTo(half + self.diamond / 2.0, mid)
        path.lineTo(half, mid - self.diamond / 2.0)
        path.lineTo(half - self.diamond / 2.0, mid)
        path.close()
        c.drawPath(path, stroke=0, fill=1)
        c.restoreState()


class SectionRule(Flowable):
    """A short gold bar followed by a hairline across the measure.

    Section headings get this instead of a plain rule so a new section is
    findable on a dense page without the heading having to shout.
    """

    def __init__(self, theme: Theme, width: float, *,
                 bar: float = 13 * mm, thickness: float = 1.8):
        super().__init__()
        self.theme = theme
        self.width = width
        self.height = max(4.0, thickness + 2.2)
        self.bar = min(bar, width)
        self.thickness = thickness

    def draw(self) -> None:
        c = self.canv
        p = self.theme.palette
        mid = self.height / 2.0
        c.saveState()
        c.setFillColor(p.accent)
        c.rect(0, mid - self.thickness / 2.0, self.bar, self.thickness,
               stroke=0, fill=1)
        c.setStrokeColor(p.line)
        c.setLineWidth(0.5)
        if self.bar + 3 < self.width:
            c.line(self.bar + 3, mid, self.width, mid)
        c.restoreState()


# ---------------------------------------------------------------------------
# Page furniture
# ---------------------------------------------------------------------------


@dataclass
class Chrome:
    """What the running header/footer needs to know about this document."""

    theme: Theme
    branding: Branding
    title: str = "Report"                 # "Student Correction Report"
    context: str = ""                    # "Ada Lovelace - Algebra HW"
    doc_id: str = ""                     # stable document identifier
    generated_at: datetime | None = None

    def footer_left(self) -> str:
        bits = [self.branding.center, self.branding.teacher_name]
        return safe_text(SEP.join(b for b in bits if b)) or "Math Correct"

    def footer_middle(self) -> str:
        return safe_text(self.branding.year)

    def footer_note(self) -> str:
        return safe_text(
            self.branding.footer_note
            or "Generated by Math Correct. Figures reflect the most recent "
               "synchronised Classroom and marking data.")

    def running_header(self) -> str:
        bits = ["Math Correct", self.title]
        if self.context:
            bits.append(self.context)
        return safe_text(SEP.join(b for b in bits if b))


class _NumberedPages:
    """Mixin that defers page furniture so "Page X of Y" can be printed.

    reportlab's ``onPage`` callback runs while a page is being built, so the
    total page count is not known yet. This snapshots each finished page and
    replays it at save time, when the total is available.
    """

    _mc_doc = None

    def showPage(self):  # noqa: N802 - reportlab API
        self._mc_states.append(dict(self.__dict__))
        self._startPage()

    def save(self):
        total = len(self._mc_states)
        for state in self._mc_states:
            self.__dict__.update(state)
            self._draw_furniture(total)
            super().showPage()
        super().save()

    def _draw_furniture(self, total: int) -> None:  # pragma: no cover
        raise NotImplementedError


def draw_page_background(canvas, theme: "Theme") -> None:
    """Lay the warm ivory stock the whole document is printed on (§31).

    This is the single most identity-defining choice in the reference: a
    report on ivory reads as an official document, and the same report on
    plain white reads as a browser printout. It is drawn on the canvas so it
    reaches every page, including pages the generator knows nothing about.
    """
    width, height = theme.pagesize
    canvas.saveState()
    canvas.setFillColor(theme.palette.sheet)
    canvas.rect(0, 0, width, height, stroke=0, fill=1)
    canvas.restoreState()


def draw_certificate_frame(canvas, theme: "Theme") -> None:
    """Draw the report's certificate frame around the whole page (§16).

    A deep navy outer rule with a warm gold inner rule and a small geometric
    bracket in each corner. It is drawn on the canvas rather than as a flowable
    because it has to sit in the page MARGIN on every single page, including
    pages the generator knows nothing about.

    Everything is vector, so it stays sharp at any print resolution, and it is
    deliberately thin: the frame should say "official document" quietly, not
    compete with the content inside it.
    """
    if not theme.framed:
        return
    w, h = theme.pagesize
    p = theme.palette
    outer = FRAME_INSET
    inner = outer + 2.3 * mm
    canvas.saveState()

    # Deep navy outer rule.
    canvas.setStrokeColor(p.primary)
    canvas.setLineWidth(1.15)
    canvas.rect(outer, outer, w - 2 * outer, h - 2 * outer)

    # Warm gold inner rule - the double line of the reference design.
    canvas.setStrokeColor(p.accent)
    canvas.setLineWidth(0.45)
    canvas.rect(inner, inner, w - 2 * inner, h - 2 * inner)

    # Geometric corner brackets, set just INSIDE the inner rule so they read as
    # their own detail instead of thickening a line that is already there.
    canvas.setStrokeColor(p.accent)
    canvas.setLineWidth(0.7)
    span = FRAME_CORNER
    gap = 2.4 * mm
    d = 1.5 * mm
    for cx, cy, sx, sy in ((inner + gap, inner + gap, 1, 1),
                           (w - inner - gap, inner + gap, -1, 1),
                           (inner + gap, h - inner - gap, 1, -1),
                           (w - inner - gap, h - inner - gap, -1, -1)):
        canvas.line(cx, cy, cx + sx * span, cy)
        canvas.line(cx, cy, cx, cy + sy * span)

        # A small filled square on the bracket's diagonal - the gold accent
        # detail that keeps the corner from reading as plain rules.
        canvas.setFillColor(p.accent)
        canvas.rect(cx + sx * (span - d) - (d if sx > 0 else 0),
                    cy + sy * (span - d) - (d if sy > 0 else 0),
                    d, d, stroke=0, fill=1)

    # Small gold diamonds on the midpoint of each side - the accent detail of
    # the reference certificate border. Drawn as a rotated square so it stays
    # vector and prints as sharply as the rules around it.
    dm = 1.9 * mm
    for dx, dy in ((w / 2.0, outer), (w / 2.0, h - outer),
                   (outer, h / 2.0), (w - outer, h / 2.0)):
        path = canvas.beginPath()
        path.moveTo(dx, dy + dm / 2.0)
        path.lineTo(dx + dm / 2.0, dy)
        path.lineTo(dx, dy - dm / 2.0)
        path.lineTo(dx - dm / 2.0, dy)
        path.close()
        canvas.drawPath(path, stroke=0, fill=1)
    canvas.restoreState()


def _numbered_canvas(doc: "ReportDoc"):
    """A ``Canvas`` subclass that knows how to draw THIS document's footer."""
    from reportlab.pdfgen.canvas import Canvas

    class NumberedCanvas(_NumberedPages, Canvas):
        _mc_doc = doc

        def __init__(self, *args, **kwargs):
            self._mc_states: list[dict] = []
            Canvas.__init__(self, *args, **kwargs)

        def _draw_furniture(self, total: int) -> None:
            doc.draw_footer(self, self._pageNumber, total)

    return NumberedCanvas


class ReportDoc(BaseDocTemplate):
    """The document template every Math Correct PDF is built with.

    Owns the page geometry, the running header on continuation pages, and the
    footer with a real "Page X of Y". Generators only add a story.
    """

    def __init__(self, buf, chrome: Chrome):
        self.chrome = chrome
        self.buf_path = buf
        super().__init__(
            buf, pagesize=chrome.theme.pagesize,
            leftMargin=chrome.theme.left, rightMargin=chrome.theme.right,
            topMargin=chrome.theme.top, bottomMargin=chrome.theme.bottom,
            title=chrome.title,
            author=chrome.branding.teacher_name or "Math Correct",
            subject=chrome.context or chrome.title,
        )
        frame = Frame(self.leftMargin, self.bottomMargin, self.width, self.height,
                      id="body", leftPadding=0, rightPadding=0,
                      topPadding=0, bottomPadding=0)
        self.addPageTemplates([PageTemplate(id="mc", frames=[frame],
                                            onPage=self._decorate_page)])

    # -- page decoration --------------------------------------------------
    def _decorate_page(self, canvas, doc) -> None:
        """Frame every page, then add the running header where it belongs."""
        draw_page_background(canvas, self.chrome.theme)
        draw_certificate_frame(canvas, self.chrome.theme)
        self._running_header(canvas, doc)

    def _running_header(self, canvas, doc) -> None:
        """A light identity line on continuation pages only (§17).

        Page 1 already carries the full letterhead and title, so repeating it
        there would push the summary down and waste the page.
        """
        if canvas.getPageNumber() <= 1:
            return
        theme = self.chrome.theme
        p = theme.palette
        y = theme.pagesize[1] - theme.top + 7.2 * mm
        canvas.saveState()
        canvas.setFont(theme.font, 6.9)
        canvas.setFillColor(p.faint)
        canvas.drawString(theme.left, y, self.chrome.running_header())
        if self.chrome.doc_id:
            canvas.drawRightString(theme.pagesize[0] - theme.right, y,
                                   safe_text(self.chrome.doc_id))
        canvas.setStrokeColor(p.line)
        canvas.setLineWidth(0.4)
        canvas.line(theme.left, y - 2.4 * mm,
                    theme.pagesize[0] - theme.right, y - 2.4 * mm)
        canvas.restoreState()

    def draw_footer(self, canvas, page: int, total: int | None = None) -> None:
        theme = self.chrome.theme
        p = theme.palette
        width = theme.width
        y = theme.bottom - 9.4 * mm
        canvas.saveState()
        canvas.setStrokeColor(p.line)
        canvas.setLineWidth(0.4)
        canvas.line(theme.left, y + 4.2 * mm, theme.left + width, y + 4.2 * mm)
        canvas.setFont(theme.font, 6.8)
        canvas.setFillColor(p.faint)
        canvas.drawString(theme.left, y, safe_text(self.chrome.footer_left()))
        middle = safe_text(self.chrome.footer_middle())
        if middle:
            canvas.drawCentredString(theme.left + width / 2.0, y, middle)
        counter = f"Page {page} of {total}" if total else f"Page {page}"
        canvas.drawRightString(theme.left + width, y, counter)
        canvas.restoreState()

    def build(self, story, **kwargs):
        kwargs.setdefault("canvasmaker", _numbered_canvas(self))
        return super().build(story, **kwargs)


# ---------------------------------------------------------------------------
# Story components
# ---------------------------------------------------------------------------


def _left(table: Table) -> Table:
    """Left-align a table.

    reportlab's ``Table`` ignores an ``hAlign`` constructor kwarg - the
    attribute has to be set on the instance - and the default (CENTRE) makes
    every block-sized table sit in the middle of the page, which is what makes
    a hand-built report look hand-built.
    """
    table.hAlign = "LEFT"
    return table


def letterhead(theme: Theme, styles: dict, brand: Branding) -> list:
    """The teacher/centre identity block: mark, name, title, centre, year.

    Skipped entirely when the teacher has configured nothing, so an
    unconfigured install gets a clean document instead of an empty band.
    """
    if not brand.is_configured:
        return []
    mark = None
    if brand.logo_path:
        try:
            from reportlab.platypus import Image
            img = Image(brand.logo_path)
            ratio = img.imageHeight / max(1.0, float(img.imageWidth))
            img.drawWidth = 13 * mm
            img.drawHeight = min(13 * mm, 13 * mm * ratio)
            img.hAlign = "LEFT"
            mark = img
        except Exception:  # noqa: BLE001 - a bad image must not break the PDF
            mark = None
    if mark is None and brand.monogram:
        mark = Monogram(brand.monogram, theme, 13 * mm)

    text = []
    if brand.teacher_name:
        text.append(Paragraph(esc(brand.teacher_name), styles["teacher"]))
    if brand.teacher_title:
        text.append(Paragraph(esc(brand.teacher_title.upper()), styles["teacher_sub"]))
    subtitle = brand.subtitle_line()
    if subtitle:
        text.append(Spacer(1, 1.6 * mm))
        text.append(Paragraph(esc(subtitle), styles["teacher_sub"]))

    gap = 4.4 * mm
    row = [mark if mark is not None else "",
           text] if mark is not None else text
    widths = ([13 * mm, theme.width - 13 * mm - gap] if mark is not None
              else [theme.width])
    table = _left(Table([row], colWidths=widths))
    table.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 0),
        ("TOPPADDING", (0, 0), (-1, -1), 0),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
    ] + ([("LEFTPADDING", (1, 0), (1, 0), gap)] if mark is not None else [])))
    out = [table, Spacer(1, 4.2 * mm)]

    if brand.quote:
        # Deliberately small and italic: a motto, not a headline (§4). The
        # quote marks are plain ASCII on purpose - a curly quote is fine in
        # Word but becomes a box or a replacement glyph in the base-14 PDF
        # fonts, and a letterhead must never print that.
        quote = _left(Table([[Paragraph(f'"{esc(brand.quote)}"',
                                   styles["quote"])]], colWidths=[theme.width]))
        quote.setStyle(TableStyle([
            ("LEFTPADDING", (0, 0), (-1, -1), 0),
            ("RIGHTPADDING", (0, 0), (-1, -1), 0),
            ("TOPPADDING", (0, 0), (-1, -1), 0),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
            ("LINEBEFORE", (0, 0), (0, -1), 1.4, theme.palette.accent),
            ("LEFTPADDING", (0, 0), (0, -1), 6),
        ]))
        out += [quote, Spacer(1, 4.6 * mm)]
    return out


def centered_masthead(theme: Theme, styles: dict, brand: Branding) -> list:
    """The official-report masthead: centred mark, tagline, centre, year.

    This is the certificate treatment (§32-§33): a prominent centred logo,
    an elegant italic-serif tagline underneath, then the centre and academic
    year. It replaces :func:`letterhead` for formal reports; the letterhead
    remains the default for everyday exports so nothing changes silently.
    """
    if not brand.is_configured:
        return []
    out: list = []

    mark: Any = None
    if brand.logo_path:
        try:
            from reportlab.platypus import Image
            img = Image(brand.logo_path)
            ratio = img.imageHeight / max(1.0, float(img.imageWidth))
            img.drawWidth = 21 * mm
            img.drawHeight = min(28 * mm, 21 * mm * ratio)
            img.hAlign = "CENTER"
            mark = img
        except Exception:  # noqa: BLE001 - a bad image must not break the PDF
            mark = None
    if mark is None:
        # No logo: fall back to the drawn monogram. An EMPTY monogram renders
        # as the design system's own neutral mark (Monogram's own fallback) -
        # never as a letter borrowed from whoever built the app.
        mark = Monogram(brand.monogram, theme, 19 * mm)

    holder = Table([[mark]], colWidths=[theme.width])
    holder.hAlign = "CENTER"
    holder.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("ALIGN", (0, 0), (-1, -1), "CENTER"),
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 0),
        ("TOPPADDING", (0, 0), (-1, -1), 0),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
    ]))
    out.append(holder)
    out.append(Spacer(1, 3.0 * mm))

    # The diamond divider sits directly under the mark, exactly as it does in
    # the reference, and the motto hangs off it.
    rule = DiamondRule(theme, 64 * mm)
    rule.hAlign = "CENTER"
    out.append(rule)
    out.append(Spacer(1, 3.2 * mm))

    tagline = safe_text(brand.quote)
    if tagline:
        # Elegant italic serif, centred - a motto, never a headline (§33).
        out.append(Paragraph(esc(tagline), ParagraphStyle(
            "mcTagline", parent=styles["quote"], alignment=TA_CENTER,
            fontSize=11.2, leading=14.6, textColor=theme.palette.ink)))
    out.append(Spacer(1, 5.0 * mm))
    return out


def centered_identity(theme: Theme, styles: dict, brand: Branding) -> list:
    """Centre, academic year and teacher, set small under the title (§33).

    Deliberately quiet: the title is the headline of the certificate, and
    this is the administrative line that tells a parent whose document it is.
    """
    if not brand.is_configured:
        return []
    out: list = []
    subtitle = brand.subtitle_line()
    if subtitle:
        out.append(Paragraph(esc(subtitle), ParagraphStyle(
            "mcCenterLine", parent=styles["teacher_sub"], alignment=TA_CENTER,
            fontSize=9.0, leading=12, textColor=theme.palette.accent)))
    if brand.teacher_name:
        out.append(Spacer(1, 2.0 * mm))
        out.append(Paragraph(esc(brand.teacher_name), ParagraphStyle(
            "mcCenterTeacher", parent=styles["teacher"], alignment=TA_CENTER)))
        if brand.teacher_title:
            out.append(Paragraph(esc(brand.teacher_title.upper()), ParagraphStyle(
                "mcCenterTitle", parent=styles["teacher_sub"],
                alignment=TA_CENTER)))
    out.append(Spacer(1, 4.0 * mm))
    return out


def document_title(theme: Theme, styles: dict, title: str, *,
                   eyebrow: str = "", label: str = "",
                   context: str = "", centered: bool = False) -> list:
    """Title + optional administrative label + accent rule (§6).

    ``centered`` is the formal-report treatment: the title reads as the
    headline of a certificate rather than the heading of a document.
    """
    out = []
    align = TA_CENTER if centered else TA_LEFT

    def _style(name: str, base: str) -> ParagraphStyle:
        return ParagraphStyle(f"{name}C", parent=styles[base], alignment=align)

    if eyebrow:
        out.append(Paragraph(esc(eyebrow.upper()),
                             _style("mcEyebrowC", "eyebrow")
                             if centered else styles["eyebrow"]))
    out.append(Paragraph(esc(title),
                         _style("mcTitleC", "title")
                         if centered else styles["title"]))
    if context:
        out.append(Spacer(1, 1.2 * mm))
        out.append(Paragraph(esc(context),
                             _style("mcMetaC", "meta")
                             if centered else styles["meta"]))
    if label:
        out.append(Spacer(1, 2.6 * mm))
        badge_flow = badge(label, theme, "accent")
        if centered and hasattr(badge_flow, "hAlign"):
            badge_flow.hAlign = "CENTER"
        out.append(badge_flow)
    out.append(Spacer(1, 2.4 * mm))
    rule = accent_rule(theme, 22 * mm if centered else 30 * mm)
    if centered:
        rule.hAlign = "CENTER"
    out.append(rule)
    out.append(Spacer(1, 5.0 * mm))
    return out


def accent_rule(theme: Theme, width: float = 26 * mm,
                color: colors.Color | None = None) -> Flowable:
    """A short accent rule under the title: one strong line, not a box."""
    from reportlab.platypus import HRFlowable
    return HRFlowable(
        width=width, thickness=2.0, color=color or theme.palette.accent,
        spaceBefore=0, spaceAfter=0, hAlign="LEFT", lineCap="butt")


def hairline(theme: Theme, width: float | None = None) -> Flowable:
    from reportlab.platypus import HRFlowable
    return HRFlowable(
        width=width or theme.width, thickness=0.5,
        color=theme.palette.line, spaceBefore=0, spaceAfter=0, hAlign="LEFT")


def section(theme: Theme, styles: dict, title: str, *,
            subtitle: str = "") -> list:
    """A section heading kept together with the first line of its content.

    Returns a list so callers can do ``story += section(...)``; the heading and
    the rule are wrapped so reportlab can never strand the title at the foot of
    a page (§16).
    """
    parts = [Spacer(1, 4.6 * mm),
             Paragraph(esc(title), styles["h2"])]
    if subtitle:
        parts.append(Spacer(1, 0.8 * mm))
        parts.append(Paragraph(esc(subtitle), styles["meta"]))
    parts.append(Spacer(1, 1.4 * mm))
    parts.append(SectionRule(theme, theme.width))
    parts.append(Spacer(1, 3.2 * mm))
    return parts


def subsection(theme: Theme, styles: dict, title: str, *,
               detail: str = "") -> list:
    parts = [Paragraph(esc(title), styles["h3"])]
    if detail:
        parts.append(Spacer(1, 0.6 * mm))
        parts.append(Paragraph(esc(detail), styles["meta"]))
    parts.append(Spacer(1, 1.8 * mm))
    return parts


def _text_width(text: str, font: str, size: float) -> float:
    """Natural width of a single line of text.

    ``Paragraph.wrap`` returns the width it was GIVEN, not the width the text
    needs, so it cannot be used to size a table to its content. Measuring the
    string directly is exact and avoids the full-width-bar artefact.
    """
    from reportlab.pdfbase import pdfmetrics
    try:
        return pdfmetrics.stringWidth(text, font, size)
    except Exception:  # noqa: BLE001 - an unmeasurable font must not break a PDF
        return len(text) * size * 0.5


def badge(text: str, theme: Theme, tone: str = "mute",
          size: float | None = None) -> Table:
    """A small filled label. The WORD carries the meaning; colour reinforces it.

    Rendered as a one-cell table because reportlab has no border radius, and a
    squared label actually reads more "administrative" than a rounded pill.
    """
    fg, bg = theme.tone(tone)
    point = size or 7.0
    style = ParagraphStyle(
        "mcBadge", fontName=theme.font_bold,
        fontSize=point, leading=point + 2.2,
        textColor=fg, spaceAfter=0, spaceBefore=0)
    label = esc(text).upper()
    # A one-cell table expands to the full frame width, which turns a label
    # into a full-width bar. Size the column to the text so it reads as a tag.
    width = _text_width(label, theme.font_bold, point) + 10.5
    table = _left(Table([[Paragraph(label, style)]], colWidths=[width]))
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), bg),
        ("TOPPADDING", (0, 0), (-1, -1), 2.4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 2.4),
        ("LEFTPADDING", (0, 0), (-1, -1), 5.0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 5.0),
    ]))
    return table


def badges(items: Sequence[tuple[str, str]], theme: Theme,
           gap: float = 3.0 * mm) -> Table:
    """A row of badges, e.g. a status plus a review flag.

    Each cell is sized to its own text, so a row of short labels reads as tags
    rather than as one full-width tinted band.
    """
    if not items:
        return Table([[""]], colWidths=[1])
    cells = [badge(text, theme, tone) for text, tone in items]
    widths = [cell._colWidths[0] for cell in cells]
    table = _left(Table([cells], colWidths=widths))
    table.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), gap),
        ("TOPPADDING", (0, 0), (-1, -1), 0),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
    ]))
    return table


def callout(theme: Theme, styles: dict, title: str,
            lines: Iterable[str] = (), *, tone: str = "info",
            note: str = "") -> Table:
    """A dedicated block for things that need action or attention (§7).

    Severity is carried by the label and the tone name, not by shouting: a
    missing homework list and a failed correction do not look alike.
    """
    fg, bg = theme.tone(tone)
    content = [Paragraph(esc(title), ParagraphStyle(
        "mcCalloutTitle", fontName=theme.font_bold, fontSize=8.8, leading=11.4,
        textColor=fg, spaceAfter=0, spaceBefore=0))]
    body_lines = [safe_text(x) for x in lines if safe_text(x)]
    if body_lines:
        content.append(Spacer(1, 1.4 * mm))
        for line in body_lines:
            content.append(Paragraph(esc(line), styles["body_tight"]))
    if note:
        content.append(Spacer(1, 1.4 * mm))
        content.append(Paragraph(esc(note), styles["meta"]))
    table = _left(Table([[content]], colWidths=[theme.width]))
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), bg),
        ("LINEBEFORE", (0, 0), (0, -1), 2.0, fg),
        ("LEFTPADDING", (0, 0), (-1, -1), 7),
        ("RIGHTPADDING", (0, 0), (-1, -1), 7),
        ("TOPPADDING", (0, 0), (-1, -1), 6),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6.5),
    ]))
    return table


def stat_strip(theme: Theme, styles: dict,
               items: Sequence[tuple[str, Any]],
               *, tone: str | None = None,
               bar_for: int | None = None) -> Table:
    """A row of headline figures with real visual weight.

    ``items`` are ``(label, value)`` pairs. This replaces the "tiny boxes"
    pattern: the number is the largest thing on the line and the label is
    small caps underneath it, so the eye lands on the figure first.
    """
    if not items:
        return Table([[""]], colWidths=[1])
    p = theme.palette
    cells = []
    for index, (label, value) in enumerate(items):
        block = [Paragraph(esc(value), styles["stat_value"])]
        if bar_for is not None and index == bar_for:
            block.append(Spacer(1, 1.6 * mm))
            block.append(ProgressBar(theme, _as_fraction(value),
                                     width=theme.width / len(items) - 8))
        block.append(Spacer(1, 0.8 * mm))
        block.append(Paragraph(esc(label).upper(), styles["stat_label"]))
        cells.append(block)
    col = theme.width / len(items)
    table = _left(Table([cells], colWidths=[col] * len(items)))
    # A white card with hairline separators and one gold rule under the
    # figures: the number is still the largest thing on the line, but the
    # strip now reads as a score summary rather than as loose text.
    pad = 9 if len(items) > 1 else 4
    style = [
        ("BACKGROUND", (0, 0), (-1, -1), colors.white),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), pad),
        ("RIGHTPADDING", (0, 0), (-1, -1), 8),
        ("TOPPADDING", (0, 0), (-1, -1), 6.5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6.5),
        ("LINEBELOW", (0, 0), (-1, -1), 1.8,
         theme.tone(tone or "accent")[0] if tone else theme.palette.accent),
    ]
    for column in range(1, len(items)):
        style.append(("LINEBEFORE", (column, 0), (column, -1),
                      0.4, p.line))
    table.setStyle(TableStyle(style))
    return table


def _as_fraction(value: Any) -> float:
    text = str(value or "").strip().rstrip("%")
    try:
        number = float(text)
    except (TypeError, ValueError):
        return 0.0
    if number > 1.0:
        number = number / 100.0
    return max(0.0, min(1.0, number))


def _cell(value: Any, styles: dict, align: str = "left",
          bold: bool = False) -> Paragraph:
    style = styles["cell"] if align == "left" else styles[f"cell_{align}"]
    text = esc(value)
    if bold:
        text = f"<b>{text}</b>"
    return Paragraph(text or "&nbsp;", style)


def fit_widths(theme: Theme, weights: Sequence[float],
               *, min_col: float = 14 * mm) -> list[float]:
    """Turn relative column weights into widths that EXACTLY fill the measure.

    Hand-written millimetre widths are the classic way a report ends up with
    columns squeezed until "Students" wraps as "STU / DENTS": either they
    overflow the margin (reportlab silently shrinks them) or they waste half
    the page. Weights always add up to the printable width instead.
    """
    total = sum(weights) or 1.0
    widths = [theme.width * w / total for w in weights]
    # A very wide table can push one column under the legible minimum; give
    # the shortfall back to the widest columns rather than overflowing.
    while widths and min(widths) < min_col and max(widths) > min_col * 1.5:
        short = widths.index(min(widths))
        widest = widths.index(max(widths))
        need = min_col - widths[short]
        if widths[widest] - need < min_col:
            break
        widths[short] += need
        widths[widest] -= need
    return widths


def _fit_headers(theme: Theme, headers: Sequence[str],
                 widths: Sequence[float]) -> list[float]:
    """Widen any column too narrow for its own uppercased header.

    Column widths are derived from content weights, which know nothing about
    the words labelling them: a column sized for short data can end up
    narrower than its header, and a bold-caps header then wraps mid-word and
    prints as "ASSIGNMEN / T". That artefact is the single most obvious sign
    of a hand-built table, so the shortfall is stolen from the widest column
    here rather than left for a reader to puzzle over.

    Only ever moves width BETWEEN columns - the total still fills the measure
    exactly, and the loop stops as soon as nothing more can be helped.
    """
    if not widths or not headers:
        return list(widths)
    widths = list(widths)
    # Cell padding, plus a safety margin: sizing a column to land EXACTLY on
    # the header's width still wraps the last letter, because reportlab wraps
    # when the text is not strictly narrower than the space it has.
    padding = 10.4 + 2.5
    needed = [(_text_width(safe_text(h).upper(), theme.font_bold, 7.8)
               + padding) if index < len(widths) else 0.0
              for index, h in enumerate(headers)]
    for _ in range(len(widths) + 1):
        short = [i for i, w in enumerate(widths) if needed[i] > w]
        if not short:
            break
        widest = max(range(len(widths)), key=lambda i: widths[i])
        moved = False
        for index in short:
            if index == widest:
                continue
            slack = widths[widest] - widths[index]
            if slack <= 0:
                continue
            take = min(needed[index] - widths[index], slack)
            widths[index] += take
            widths[widest] -= take
            moved = True
        if not moved:
            break
    return widths


def data_table(theme: Theme, styles: dict, headers: Sequence[str],
               rows: Sequence[Sequence[Any]], *,
               widths: Sequence[float] | None = None,
               aligns: Sequence[str] | None = None,
               zebra: bool = True,
               row_tones: Sequence[str | None] | None = None,
               emphasis: Sequence[int] | None = None) -> Table:
    """The one table in the system (§8).

    Every cell is a ``Paragraph``, so a long student name or assignment title
    WRAPS instead of being clipped or stretching the column past the margin.
    ``repeatRows=1`` reprints the header when the table continues onto the
    next page (§16, §17). ``emphasis`` bolds whole columns - used for the
    marks column, so the number a teacher reads first is the one that is
    heaviest on the line.
    """
    p = theme.palette
    align = list(aligns or ["left"] * len(headers))
    heavy = set(emphasis or ())
    head = [Paragraph(esc(h).upper(), styles["cell_head"]) for h in headers]
    body = [[_cell(v, styles, align[i] if i < len(align) else "left",
                   bold=i in heavy)
             for i, v in enumerate(row)] for row in rows]
    table = _left(Table([head] + body,
                          colWidths=(
                              _fit_headers(theme, headers, widths)
                              if widths else None),
                          repeatRows=1))
    style = [
        ("BACKGROUND", (0, 0), (-1, 0), p.primary),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        # A gold rule under the navy header is the table's one accent: it
        # ties the grid to the frame without adding another box.
        ("LINEBELOW", (0, 0), (-1, 0), 1.1, p.accent),
        ("GRID", (0, 0), (-1, -1), 0.35, p.line),
        ("LINEBELOW", (0, -1), (-1, -1), 0.6, p.line),
        ("LEFTPADDING", (0, 0), (-1, -1), 5.2),
        ("RIGHTPADDING", (0, 0), (-1, -1), 5.2),
        ("TOPPADDING", (0, 0), (-1, -1), 4.4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4.4),
    ]
    if zebra:
        style.append(("ROWBACKGROUNDS", (0, 1), (-1, -1),
                      [colors.white, p.zebra]))
    # Per-row emphasis: a tinted band + a stronger leading edge, so a problem
    # row is findable without turning the table into a warning page.
    for index, tone in enumerate(row_tones or []):
        if not tone or tone == "mute":
            continue
        row = index + 1
        fg, bg = theme.tone(tone)
        style.append(("BACKGROUND", (0, row), (-1, row), bg))
        style.append(("LINEBEFORE", (0, row), (0, row), 1.6, fg))
    table.setStyle(TableStyle(style))
    return table


def key_values(theme: Theme, styles: dict, items: Sequence[tuple[str, Any]],
               *, columns: int = 2) -> Table:
    """Label/value metadata, laid out in columns to use the width well.

    Replaces the "four stacked two-column tables" pattern: the same information
    now reads as one scannable block instead of four.
    """
    if not items:
        return Table([[""]], colWidths=[1])
    p = theme.palette
    columns = max(1, min(2, columns))
    pairs_per_col = -(-len(items) // columns)
    grid: list[list[Any]] = []
    for row_index in range(pairs_per_col):
        row: list[Any] = []
        for col in range(columns):
            index = col * pairs_per_col + row_index
            if index >= len(items):
                row.append("")
                continue
            key, value = items[index]
            row.append([
                Paragraph(esc(key).upper(), styles["cell_key"]),
                Spacer(1, 0.6 * mm),
                Paragraph(esc(value) or "&nbsp;", styles["cell"]),
            ])
        grid.append(row)
    # One cell per column: the label and its value are stacked INSIDE that
    # cell, so the grid has `columns` columns, not `columns * 2`. Getting this
    # wrong silently halves every rule and squeezes the block.
    pair_w = theme.width / columns
    table = _left(Table(grid, colWidths=[pair_w] * columns))
    # A white card on the ivory page with one gold edge: the block reads as
    # a deliberate part of the document instead of as loose text (§31).
    style = [
        ("BACKGROUND", (0, 0), (-1, -1), colors.white),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LINEBEFORE", (0, 0), (0, -1), 2.0, p.accent),
        ("LEFTPADDING", (0, 0), (-1, -1), 8),
        ("RIGHTPADDING", (0, 0), (-1, -1), 6),
        ("TOPPADDING", (0, 0), (-1, -1), 5.0),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5.0),
    ]
    # One continuous rule between rows, spanning the measure - per-cell rules
    # leave ragged stubs where the column widths change.
    for row in range(len(grid) - 1):
        style.append(("LINEBELOW", (0, row), (-1, row), 0.3, p.line))
    table.setStyle(TableStyle(style))
    return table


def group_divider(theme: Theme, styles: dict, label: str,
                  *, meta: str = "", tone: str = "primary") -> list:
    """A class/section divider: obvious, but not a page of its own (§9).

    ``tone="gold"`` renders the warm-gold banner used for CLASS 10A style
    dividers in the official report; ``primary`` is the everyday navy rule.
    """
    p = theme.palette
    if tone == "gold":
        band_bg, band_fg, band_rule = p.accent, colors.white, p.primary
    else:
        band_bg, band_fg, band_rule = colors.white, p.primary, p.primary
    title = Paragraph(esc(label).upper(), ParagraphStyle(
        "mcGroup", fontName=theme.font_bold, fontSize=8.8, leading=11,
        textColor=band_fg, spaceAfter=0, spaceBefore=0))
    right = Paragraph(esc(meta), ParagraphStyle(
        "mcGroupMeta", parent=styles["meta"],
        textColor=colors.white if tone == "gold" else p.faint)) if meta else ""
    table = _left(Table([[title, right]],
                          colWidths=[theme.width * 0.6, theme.width * 0.4]))
    table.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "BOTTOM"),
        ("BACKGROUND", (0, 0), (-1, -1), band_bg),
        ("LINEBELOW", (0, 0), (-1, -1), 1.2, band_rule),
        ("LEFTPADDING", (0, 0), (-1, -1), 6),
        ("RIGHTPADDING", (0, 0), (-1, -1), 6),
        ("TOPPADDING", (0, 0), (-1, -1), 4.0),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4.0),
    ]))
    return [Spacer(1, 3.2 * mm), table, Spacer(1, 2.6 * mm)]


def empty_state(theme: Theme, styles: dict, text: str) -> list:
    """A clean one-line note for a section that has nothing to show (§24).

    An empty table with header rules and no rows reads as a bug; this reads as
    a considered report that had nothing to report.
    """
    p = theme.palette
    body = Paragraph(esc(safe_text(text)), ParagraphStyle(
        "mcEmpty", fontName=theme.font_italic, fontSize=8.4, leading=11.4,
        textColor=p.faint, spaceAfter=0, spaceBefore=0))
    table = _left(Table([[body]], colWidths=[theme.width]))
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), colors.white),
        ("LINEBEFORE", (0, 0), (-1, -1), 2.0, p.accent),
        ("LEFTPADDING", (0, 0), (-1, -1), 8),
        ("RIGHTPADDING", (0, 0), (-1, -1), 8),
        ("TOPPADDING", (0, 0), (-1, -1), 6),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
    ]))
    return [Spacer(1, 1.4 * mm), table]


def signature_mark(theme: Theme, brand: Branding, width: float = 46 * mm) -> list:
    """The signature image (or a drawn rule) with its FOR SIGNATURE caption.

    The teacher's own signature is part of the identity, so it is printed
    above the rule they would sign on. A missing or unreadable file degrades
    to the bare rule - the report must never fail because an image moved.
    """
    out: list = []
    path = str(getattr(brand, "signature_path", "") or "")
    if path:
        try:
            from reportlab.platypus import Image
            img = Image(path)
            ratio = img.imageHeight / max(1.0, float(img.imageWidth))
            draw_w = width
            draw_h = width * ratio
            if draw_h > 24 * mm:            # keep the block a fixed height
                draw_w, draw_h = 24 * mm / ratio, 24 * mm
            img.drawWidth = draw_w
            img.drawHeight = draw_h
            img.hAlign = "CENTER"
            out += [img, Spacer(1, 1.4 * mm)]
        except Exception:  # noqa: BLE001 - a bad image must not break the PDF
            pass
    rule = Table([[""]], colWidths=[width], rowHeights=[0.1])
    rule.hAlign = "CENTER"
    rule.setStyle(TableStyle([
        ("LINEABOVE", (0, 0), (-1, -1), 0.7, theme.palette.accent),
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 0),
        ("TOPPADDING", (0, 0), (-1, -1), 0),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
    ]))
    out.append(rule)
    out.append(Spacer(1, 1.6 * mm))
    caption = Paragraph("FOR SIGNATURE", ParagraphStyle(
        "mcSignCap", fontName=theme.font_bold, fontSize=6.6, leading=8.4,
        alignment=TA_CENTER, textColor=theme.palette.faint, spaceAfter=0))
    caption.hAlign = "CENTER"
    out.append(caption)
    return out


def signature_block(theme: Theme, styles: dict, brand: Branding, *,
                    note: str = "", extra: Sequence[str] = ()) -> list:
    """Verification note, the teacher's details, and a signature rule (§20).

    Reports that a parent or an administrator will sign need to say who
    produced the document and leave a clean line to sign on. The identity
    sits left and the signature right, exactly as it does in the reference,
    so the block reads as one piece of stationery.
    """
    out: list = []
    note_text = note or ("Homework status recorded based on the latest "
                         "homework check.")
    out.append(Spacer(1, 5 * mm))
    out.append(hairline(theme))
    out.append(Spacer(1, 3 * mm))
    out.append(Paragraph(esc(safe_text(note_text)), styles["footer"]))

    ident = [b for b in (brand.teacher_name, brand.teacher_title,
                         brand.center) if b]
    lines = list(ident) + [safe_text(e) for e in extra if e]
    if not lines:
        return out

    left: list = []
    for index, line in enumerate(lines):
        left.append(Paragraph(esc(safe_text(line)),
                              styles["teacher"] if (index == 0 and ident)
                              else styles["footer"]))
        if index == 0 and len(lines) > 1:
            left.append(Spacer(1, 1.2 * mm))

    right = signature_mark(theme, brand, theme.width * 0.38)
    grid = _left(Table([[left, right]],
                       colWidths=[theme.width * 0.58, theme.width * 0.42]))
    grid.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "BOTTOM"),
        ("ALIGN", (1, 0), (1, 0), "RIGHT"),
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 0),
        ("TOPPADDING", (0, 0), (-1, -1), 0),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
    ]))
    out.append(Spacer(1, 4 * mm))
    out.append(grid)
    return out


def bullets(theme: Theme, styles: dict, items: Iterable[str]) -> list:
    out = []
    for item in items:
        text = safe_text(item)
        if not text:
            continue
        out.append(Paragraph(esc(text), styles["bullet"], bulletText="•"))
    return out


def note(theme: Theme, styles: dict, text: str) -> Paragraph:
    """Small print for a caveat ("student images are not stored ...")."""
    return Paragraph(esc(text), styles["meta"])


def add(story: list, *parts: Any) -> list:
    """Append components to a story, flattening any that are fragments.

    Most components return a single flowable; the multi-part ones (letterhead,
    a section heading) return a list. ``add`` accepts either, so a generator
    never has to remember which is which.
    """
    for part in parts:
        if part is None:
            continue
        if isinstance(part, (list, tuple)):
            story.extend(item for item in part if item is not None)
        else:
            story.append(part)
    return story


def flatten(story: Iterable[Any]) -> list:
    """Flatten nested fragments into a single reportlab story."""
    out: list = []
    for item in story:
        if item is None:
            continue
        if isinstance(item, (list, tuple)):
            out.extend(flatten(item))
        else:
            out.append(item)
    return out


# ---------------------------------------------------------------------------
# Document assembly
# ---------------------------------------------------------------------------


@dataclass
class Report:
    """Everything a report needs to render, and a ``build()`` that renders it.

    Generators fill ``story`` then call :meth:`build`. The template owns the
    letterhead, the title, the running header and the footer, so a new report
    cannot forget them.
    """

    theme: Theme
    branding: Branding
    title: str
    story: list = field(default_factory=list)
    context: str = ""
    eyebrow: str = "Math Correct"
    label: str | None = None
    doc_id: str = ""
    generated_at: datetime | None = None
    #: Formal certificate masthead (centred mark + tagline) instead of the
    #: everyday left-aligned letterhead.
    masthead: bool = False

    def build(self) -> io.BytesIO:
        label = self.label if self.label is not None else self.branding.document_label
        chrome = Chrome(
            theme=self.theme, branding=self.branding, title=self.title,
            context=self.context, doc_id=self.doc_id,
            generated_at=self.generated_at or datetime.now())
        doc = ReportDoc(io.BytesIO(), chrome)
        styles = build_styles(self.theme)
        story: list = []
        head = (centered_masthead(self.theme, styles, self.branding)
                if self.masthead
                else letterhead(self.theme, styles, self.branding))
        add(story, head)
        add(story, document_title(self.theme, styles, self.title,
                                  eyebrow=self.eyebrow, label=label,
                                  context=self.context,
                                  centered=self.masthead))
        if self.masthead:
            add(story, centered_identity(self.theme, styles, self.branding))
        add(story, flatten(self.story))
        doc.build(story)
        doc.buf_path.seek(0)
        return doc.buf_path


def status_tone(value: str) -> str:
    """Map a status word to a semantic tone, with a text label to match.

    Colour is never the only signal: callers pair this with a word such as
    "Correct" / "Partial" / "Needs review" (§13).
    """
    text = str(value or "").strip().lower().replace(" ", "_")
    if text in ("correct", "full_marks", "graded", "returned", "submitted",
                "corrected", "published", "ok", "pass", "complete", "done"):
        return "ok"
    if text in ("partial", "warning", "late", "submitted_late", "edited",
                "reclaimed", "draft", "pending", "processing", "needs_review",
                "review"):
        return "warn"
    if text in ("incorrect", "missing", "failed", "invalid", "not_submitted",
                "not_started", "error", "absent"):
        return "err"
    return "mute"


def fmt_num(value: Any) -> str:
    """17.0 -> '17', 17.5 -> '17.5'. Grades must read cleanly."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return safe_text(value) or "—"
    return str(int(number)) if number == int(number) else str(number)


def fmt_score(earned: Any, available: Any) -> str:
    """Never render a missing mark as a zero (§26 of the report contract)."""
    if earned is None or available is None:
        return "Not available"
    return f"{fmt_num(earned)} / {fmt_num(available)}"


def fmt_pct(value: Any) -> str:
    if value is None:
        return "—"
    try:
        return f"{round(float(value), 1):g}%"
    except (TypeError, ValueError):
        return "—"


def performance_word(percentage: Any) -> str:
    """A plain-language headline for a score, matching the grading bands."""
    try:
        pct = float(percentage)
    except (TypeError, ValueError):
        return "Not marked"
    if pct >= 90:
        return "Strong performance"
    if pct >= 80:
        return "Good performance"
    if pct >= 70:
        return "Satisfactory performance"
    if pct >= 50:
        return "Developing"
    if pct > 0:
        return "Needs improvement"
    return "Not attempted"
