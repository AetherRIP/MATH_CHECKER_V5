"""Stage A: local, deterministic reconstruction of a mark scheme's structure.

Why this module exists
----------------------
The previous design uploaded the mark-scheme PDF straight to Gemini and asked
it to "extract the mark scheme structure". That made the model do two jobs at
once: reading a PDF (OCR + layout inference) AND reasoning about structure.

Measured on this app's own Chapter 1 Lesson 7 mark scheme, the PDF has
**zero extractable text characters and 740 embedded images across 15 pages** -
it is image-only. In that case the model is the *only* reader, there is no
local evidence to improve, and any structural loss is unrecoverable. In the
other case (a normal digital PDF with a text layer) local evidence is both
cheap and far more reliable than asking a model to count numbers.

So this module does three things, all deterministic and free:

1. **Tells the truth about the document** - text-layer vs image-only, page
   count, image count. The caller uses that to decide what to ask Gemini.
2. **Reconstructs layout when there IS a text layer** - blocks with bounding
   boxes, column detection, and a reading order that is *not* the naive
   extraction order (which interleaves two-column pages).
3. **Classifies numbers by context** - a "22" that is a page number or a mark
   allocation must never become Question 22.

Nothing here calls a model, touches a key, or reads student data.

Ordering note
-------------
Canonical order is numeric everywhere: Q1, Q2, ... Q9, Q10, Q11. Plain string
sorting produces Q1, Q10, Q11, Q2 and this module exists partly to stop that.
"""

from __future__ import annotations

import re
from typing import Any

# Extraction modes, in increasing order of how much evidence we have.
MODE_NONE = "NONE"          # unreadable file
MODE_IMAGE_ONLY = "IMAGE_ONLY"   # no text layer - the model must OCR it
MODE_TEXT = "TEXT"           # usable text layer with block geometry

# Candidate types (requirement §5). Only TOP_LEVEL and SUB_PART may ever
# become questions; the rest exist to be explicitly rejected.
CAND_TOP_LEVEL = "top_level_question"
CAND_SUB_PART = "sub_part"
CAND_PAGE_NUMBER = "page_number"
CAND_MARK_VALUE = "mark_value"
CAND_ANSWER_VALUE = "answer_value"
CAND_OTHER = "other"

# Numeric-looking tokens with the context that tells them apart.
_NUM = re.compile(r"\d+")
# "12" / "Page 12" / "12 of 30" / "Turn over" style page furniture.
_PAGE_CONTEXT = re.compile(
    r"(?:\bpage\b|\bof\b\s*\d+\s*$|page\s+\d+|"
    r"turn\s+over|©|copyright|www\.|\.com)",
    re.IGNORECASE)
# A running header such as a repeated paper title (long enough that a bare
# digit never matches it).
_RUNNING_HEADER = re.compile(r"^[A-Z0-9][A-Z0-9 &/()'.,:+-]{3,60}$")
# A question label at the start of a line: "1", "1(a)", "Q1", "12.", "(b)"
_QUESTION_LABEL = re.compile(
    r"^\s*(?:q(?:uestion)?\s*)?(\d{1,3})\s*(?:[.:)]|\s)?(?=$|[\s(])", re.IGNORECASE)
_SUB_PART = re.compile(r"^\s*[\(\[]?([a-z]|(?:i{1,3}|iv|v|vi{0,3}|ix|x))[\)\]]?$",
                       re.IGNORECASE)
# "… 5 marks", "(5)", "marks: 5", "max 6"
_MARKS = re.compile(
    r"\(?\b(\d{1,3})\s*(?:marks?|mks?)\b\)?|\[(\d{1,3})\]|\bmax\.?\s*(\d{1,3})\b",
    re.IGNORECASE)
# Stated paper totals we can cross-check against.
_PRINTED_TOTAL = re.compile(
    r"(?:total|marks?\s*available)\D{0,20}?(\d{1,3})", re.IGNORECASE)

_ROMAN = {"i": 1, "ii": 2, "iii": 3, "iv": 4, "v": 5, "vi": 6, "vii": 7,
          "viii": 8, "ix": 9, "x": 10, "xi": 11, "xii": 12}


# --------------------------------------------------------------- ordering

def _part_rank(token: str) -> tuple[int, str]:
    """Sort key for one sub-part token: 'a' < 'b' < ... < 'i' < 'ii'."""
    t = token.strip().lower()
    if t in _ROMAN:
        return (2, f"{_ROMAN[t]:03d}")
    return (1, t)


def split_number(question_number: str) -> tuple[int | None, list[str]]:
    """``"15(a)(ii)"`` -> ``(15, ["a", "ii"])``. ``"Q7"`` -> ``(7, [])``."""
    text = str(question_number or "").strip().lower()
    m = _NUM.search(text)
    if not m:
        return None, []
    leading = int(m.group())
    rest = text[m.end():]
    parts = [p for p in re.findall(r"[a-z]+", rest)]
    return leading, parts


def order_key(question_number: str) -> tuple:
    """Deterministic numeric ordering: Q1, Q2, ..., Q10, Q11.

    Purely numeric, so Q2 always precedes Q10 and Q9 always precedes Q10 -
    never the alphabetical Q1, Q10, Q11, Q2 ordering.
    """
    leading, parts = split_number(question_number)
    if leading is None:
        # Unnumbered entries sort last but deterministically.
        return (1, 0, (), str(question_number or ""))
    return (0, leading, tuple(_part_rank(p) for p in parts), str(question_number))


def sort_numbers(numbers: list[str]) -> list[str]:
    """Canonically ordered question numbers (deduplicated, first wins)."""
    seen: set[str] = set()
    out: list[str] = []
    for n in numbers:
        key = str(n).strip()
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(key)
    return sorted(out, key=order_key)


# --------------------------------------------------------- classification

def looks_like_furniture(text: str) -> bool:
    """Does this line read as page furniture rather than body content?

    Position alone is NOT enough: the first ~6% of a page routinely holds real
    question rows, and treating those as furniture silently deletes questions
    (the exact failure that dropped Q1 and Q7 from a two-column scheme). So
    being near the edge is necessary but not sufficient - the wording has to
    agree too.
    """
    stripped = str(text or "").strip()
    if not stripped:
        return True
    if _PAGE_CONTEXT.search(stripped) and len(stripped) <= 40:
        return True
    # A bare digit is NOT furniture on its own: in a mark-scheme table the
    # question number is very often its own cell, and calling every lone digit
    # a page number would delete exactly those questions.
    if _RUNNING_HEADER.match(stripped) and len(stripped) <= 60:
        return True
    return False


def classify_number(value: str, context: str, *, is_header_or_footer: bool = False,
                    page: int = 0) -> tuple[str, float]:
    """Decide what a bare number in the document actually *is*.

    Returns ``(type, confidence)``. Context is the text of the block/line the
    number was found in; page furniture is flagged by position, not guessed.

    This is the difference between Question 22 and "Page 22" - the single most
    dangerous confusion for a mark scheme, because it silently invents a
    question with a made-up mark allocation.

    A number whose entire context is itself (``"6"`` as a lone table cell) is
    genuinely ambiguous, so it is reported UNRESOLVED at low confidence rather
    than promoted. That keeps a mark allocation out of the question list
    instead of inventing a question that does not exist.
    """
    text = str(value or "").strip()
    ctx = str(context or "")
    bare = ctx.strip() == text

    if not text.isdigit():
        return CAND_OTHER, 0.0
    n = int(text)

    # A standalone number in the header/footer band is page furniture unless it
    # is also the page it sits on, in which case it is the page number.
    if is_header_or_footer:
        if n == page or _PAGE_CONTEXT.search(ctx):
            return CAND_PAGE_NUMBER, 0.99
        # Header/footer numbers are never questions.
        return CAND_PAGE_NUMBER if n < 1000 else CAND_OTHER, 0.8

    if _PAGE_CONTEXT.search(ctx):
        return CAND_PAGE_NUMBER, 0.97

    # A number that is the ENTIRE line (a lone table cell) cannot be placed
    # from text alone. Checked before the question-label rules, because those
    # would otherwise "match" it and invent a question out of a mark value.
    if bare:
        return CAND_OTHER, 0.3

    # A question label at the very start of a block/line.
    m = _QUESTION_LABEL.match(ctx.strip())
    if m and m.group(1) == text:
        return CAND_TOP_LEVEL, 0.9

    # "1(a)" style.
    if _SUB_PART.match(text):
        return CAND_SUB_PART, 0.75

    # A marks allocation near the words "marks"/"max" - never a question.
    for m_marks in _MARKS.finditer(ctx):
        if text in [g for g in m_marks.groups() if g]:
            return CAND_MARK_VALUE, 0.85

    if _PRINTED_TOTAL.search(ctx):
        return CAND_MARK_VALUE, 0.6

    return CAND_OTHER, 0.3


# ------------------------------------------------------- document evidence

def _pymupdf():
    """PyMuPDF if importable, else None (never a hard dependency)."""
    try:
        import pymupdf  # noqa: PLC0415
        return pymupdf
    except Exception:
        try:
            import fitz  # noqa: PLC0415
            return fitz
        except Exception:
            return None


def _column_index(bbox: tuple[float, ...], page_width: float) -> int:
    """Which half of the page a block sits in (0 = left, 1 = right)."""
    centre = (bbox[0] + bbox[2]) / 2
    return 0 if centre < page_width / 2 else 1


def _detect_columns(blocks: list[dict], page_width: float) -> int:
    """How many columns this page actually uses (1 or 2).

    Determined by clustering the LEFT EDGE of each line and looking for a real
    gutter, not by line width: a mark-scheme column is often narrower than a
    third of the page, so any width-based test silently reports one column and
    hands the model an interleaved reading order.
    """
    if len(blocks) < 4:
        return 1
    xs = sorted(round(b["bbox"][0], 1) for b in blocks)
    # Biggest jump between adjacent left edges = the gutter between columns.
    best_gap, split = 0.0, None
    for i in range(1, len(xs)):
        gap = xs[i] - xs[i - 1]
        if gap > best_gap:
            best_gap, split = gap, i
    if best_gap < page_width * 0.06 or split is None:
        return 1
    left = [b for b in blocks if b["bbox"][0] <= xs[split - 1] + 1]
    right = [b for b in blocks if b["bbox"][0] >= xs[split] - 1]
    if len(left) >= 2 and len(right) >= 2:
        left_max = max(b["bbox"][2] for b in left)
        right_min = min(b["bbox"][0] for b in right)
        if right_min - left_max > page_width * 0.02:
            return 2
    return 1


def _reading_order(blocks: list[dict], page_width: float) -> list[dict]:
    """Put blocks into true reading order.

    For a single column that is top-to-bottom. For two columns it is the whole
    left column, then the whole right column - NOT interleaved by y, which is
    what naive extraction gives you for ``COLUMN_1 | COLUMN_2`` layouts.
    """
    if _detect_columns(blocks, page_width) < 2:
        return sorted(blocks, key=lambda b: (round(b["bbox"][1], 1), b["bbox"][0]))
    left = [b for b in blocks if _column_index(b["bbox"], page_width) == 0]
    right = [b for b in blocks if _column_index(b["bbox"], page_width) == 1]
    left.sort(key=lambda b: (round(b["bbox"][1], 1), b["bbox"][0]))
    right.sort(key=lambda b: (round(b["bbox"][1], 1), b["bbox"][0]))
    return left + right


def _row_contexts(blocks: list[dict], page_width: float) -> list[str]:
    """Group cells that sit on the same visual row into one context string.

    Mark schemes are full of tables, and a table cell holding a number is
    usually extracted as its own line: the ``6`` of "(max 6)" arrives with no
    context of its own and would otherwise look like Question 6. Rejoining
    cells that share a baseline restores the context needed to tell a mark
    value from a question number.

    Rows are computed WITHIN a column. Joining a left-column cell to a
    right-column cell on the same baseline would glue "Q6" and "Q7" into one
    string and destroy the very context we are trying to recover.
    """
    if not blocks:
        return []
    contexts = [b["text"] for b in blocks]
    columns: dict[int, list[int]] = {}
    for i, b in enumerate(blocks):
        columns.setdefault(_column_index(b["bbox"], page_width), []).append(i)

    for _col, indices in columns.items():
        ordered = sorted(indices, key=lambda i: blocks[i]["bbox"][1])
        rows: list[list[int]] = []
        current: list[int] = []
        anchor: tuple[float, float] | None = None
        for idx in ordered:
            top, bottom = blocks[idx]["bbox"][1], blocks[idx]["bbox"][3]
            if anchor is not None:
                # Same row when the vertical spans genuinely overlap, which is
                # how a table cell aligns with its neighbours whatever the
                # font metrics did to the individual boxes.
                overlap = min(bottom, anchor[1]) - max(top, anchor[0])
                smallest = min(bottom - top, anchor[1] - anchor[0])
                if overlap > smallest * 0.4:
                    current.append(idx)
                    anchor = (min(anchor[0], top), max(anchor[1], bottom))
                    continue
                rows.append(current)
            current, anchor = [idx], (top, bottom)
        if current:
            rows.append(current)
        for row in rows:
            if len(row) < 2:
                continue
            joined = "  ".join(blocks[i]["text"] for i in sorted(
                row, key=lambda i: blocks[i]["bbox"][0]))
            for i in row:
                contexts[i] = joined
    return contexts


def analyse_document(path: str) -> dict[str, Any]:
    """Reconstruct what we can, deterministically, from a mark-scheme PDF.

    Never raises: an unreadable file returns ``mode=NONE`` with an error so the
    caller can fall back rather than fail a correction.
    """
    evidence: dict[str, Any] = {
        "mode": MODE_NONE,
        "error": None,
        "page_count": 0,
        "pages_with_text": 0,
        "image_count": 0,
        "two_column_pages": 0,
        "block_count": 0,
        "pages": [],
        "candidates": [],
        "printed_total": None,
        "diagnostics": {},
    }

    doc_lib = _pymupdf()
    if doc_lib is None:
        evidence["error"] = "no PDF text extractor available (pymupdf not installed)"
        return evidence

    try:
        doc = doc_lib.open(str(path))
    except Exception as exc:  # noqa: BLE001
        evidence["error"] = f"mark scheme unreadable: {exc}"
        return evidence

    try:
        evidence["page_count"] = doc.page_count
        candidates: list[dict] = []
        page_records: list[dict] = []
        header_hits = footer_hits = 0

        for pno in range(doc.page_count):
            page = doc[pno]
            width = float(page.rect.width)
            height = float(page.rect.height)
            try:
                images = page.get_images(full=True)
            except Exception:
                images = []
            evidence["image_count"] += len(images)

            raw_blocks = []
            try:
                # Spans -> lines, NOT page.get_text("blocks"): the block API
                # merges a left column and a right column into one box when
                # they sit on the same horizontal band, which is precisely the
                # two-column mark scheme we must not interleave.
                page_dict = page.get_text("dict")
                band = height * 0.07
                for block in page_dict.get("blocks", []):
                    if block.get("type") != 0:      # 0 = text, 1 = image
                        continue
                    for line in block.get("lines", []):
                        spans = line.get("spans", [])
                        text = "".join(s.get("text", "") for s in spans).strip()
                        if not text:
                            continue
                        bbox = tuple(float(v) for v in line["bbox"])
                        in_band = bbox[1] < band or bbox[3] > height - band
                        # Near the edge AND worded like furniture. Requiring
                        # both keeps real question rows that happen to start
                        # high on the page from being thrown away.
                        is_furniture = in_band and looks_like_furniture(text)
                        raw_blocks.append({
                            "bbox": bbox, "text": text,
                            "is_furniture": is_furniture,
                        })
            except Exception as exc:  # noqa: BLE001
                evidence.setdefault("warnings", []).append(
                    f"page {pno + 1}: block extraction failed ({exc})")
                continue

            body = [b for b in raw_blocks if not b["is_furniture"]]
            header_hits += len(raw_blocks) - len(body)
            if body:
                evidence["pages_with_text"] += 1
            columns = _detect_columns(body, width)
            if columns >= 2:
                evidence["two_column_pages"] += 1

            ordered = _reading_order(body, width)
            # Header/footer blocks are ordered too (after the body) so page
            # numbers can be TYPED as page numbers instead of silently
            # disappearing - and so they can never be mistaken for questions.
            furniture = [b for b in raw_blocks if b["is_furniture"]]
            all_blocks = ordered + furniture
            page_text = "\n".join(b["text"] for b in all_blocks)
            for i, block in enumerate(all_blocks):
                block["id"] = f"p{pno + 1}b{i}"
                block["column"] = _column_index(block["bbox"], width)
            blocks_out = [{
                "id": b["id"],
                "page": pno + 1,
                "column": b["column"],
                "bbox": [round(v, 1) for v in b["bbox"]],
                "is_header_or_footer": b["is_furniture"],
                "text": b["text"][:600],
            } for b in all_blocks]
            page_records.append({
                "page": pno + 1,
                "columns": columns,
                "block_count": len(blocks_out),
                "blocks": blocks_out,
            })
            evidence["block_count"] += len(blocks_out)

            # Numeric candidates, typed by context.
            row_ctx = _row_contexts(body, width)
            ctx_by_id = {id(b): c for b, c in zip(body, row_ctx)}
            for block in all_blocks:
                text = block["text"]
                for line in text.splitlines():
                    stripped = line.strip()
                    if not stripped:
                        continue
                    for token in re.findall(r"\d{1,3}", stripped[:40]):
                        # A lone table cell inherits its row's wording, which is
                        # what distinguishes a mark allocation from a question.
                        context = ctx_by_id.get(id(block), stripped)
                        kind, confidence = classify_number(
                            token, context,
                            is_header_or_footer=block["is_furniture"],
                            page=pno + 1)
                        if kind == CAND_OTHER and not stripped.startswith(token):
                            continue
                        candidates.append({
                            "value": token,
                            "type": kind,
                            "confidence": confidence,
                            "page": pno + 1,
                            "block": block["id"],
                            "context": stripped[:160],
                        })
            m = _PRINTED_TOTAL.search(page_text)
            if m:
                try:
                    evidence["printed_total"] = int(m.group(1))
                except (TypeError, ValueError):
                    pass
    finally:
        try:
            doc.close()
        except Exception:
            pass

    # De-duplicate candidates: the same number can repeat across pages.
    seen: set[tuple] = set()
    unique: list[dict] = []
    for c in candidates:
        key = (c["value"], c["type"], c["page"])
        if key in seen:
            continue
        seen.add(key)
        unique.append(c)

    evidence["candidates"] = unique
    evidence["pages"] = page_records
    evidence["header_footer_blocks"] = header_hits

    if evidence["pages_with_text"] == 0:
        evidence["mode"] = MODE_IMAGE_ONLY
    elif evidence["pages_with_text"] < max(1, evidence["page_count"] // 2):
        evidence["mode"] = MODE_IMAGE_ONLY
    else:
        evidence["mode"] = MODE_TEXT

    question_like = [c for c in unique if c["type"] == CAND_TOP_LEVEL]
    evidence["diagnostics"] = {
        "pages": evidence["page_count"],
        "pages_with_text": evidence["pages_with_text"],
        "image_count": evidence["image_count"],
        "blocks": evidence["block_count"],
        "two_column_pages": evidence["two_column_pages"],
        "question_candidates": len(question_like),
        "candidate_numbers": sorted({c["value"] for c in question_like},
                                    key=lambda v: int(v)),
        "page_number_candidates": len([c for c in unique
                                       if c["type"] == CAND_PAGE_NUMBER]),
    }
    return evidence


def candidate_question_numbers(evidence: dict[str, Any]) -> list[str]:
    """Canonically ordered question numbers suggested by local evidence.

    Only ``top_level_question`` candidates qualify. Page numbers, mark values
    and bare numbers are excluded by construction - that is the whole point of
    typing them.
    """
    values = [c["value"] for c in evidence.get("candidates") or []
              if c.get("type") == CAND_TOP_LEVEL
              and float(c.get("confidence") or 0) >= 0.85]
    return sort_numbers(values)


def sequence_report(numbers: list[str]) -> dict[str, Any]:
    """Missing / duplicate / out-of-order analysis of a question sequence."""
    from .blueprint import _leading_number

    # Duplicates must be measured on the RAW input: sort_numbers() deduplicates,
    # so counting afterwards would report every structure as clean and hide the
    # duplicated-extraction bug we are specifically looking for.
    counts: dict[str, int] = {}
    for raw in (str(x).strip() for x in numbers):
        if raw:
            counts[raw] = counts.get(raw, 0) + 1
    duplicates = sorted(n for n, c in counts.items() if c > 1)

    cleaned = sort_numbers(numbers)
    leading = [(n, _leading_number(n)) for n in cleaned]
    numeric = sorted({v for _, v in leading if v is not None})

    missing: list[int] = []
    if len(numeric) >= 3:
        present = set(numeric)
        missing = [n for n in range(numeric[0], numeric[-1] + 1)
                   if n not in present]
    gaps = [missing[i] - missing[i - 1] for i in range(1, len(missing))] \
        if len(missing) > 1 else []
    return {
        "count": len(cleaned),
        "numbers": cleaned,
        "first": numeric[0] if numeric else None,
        "last": numeric[-1] if numeric else None,
        "missing": missing,
        "duplicates": duplicates,
        "largest_gap": max(gaps) if gaps else 0,
        "sequential": not missing and not duplicates,
    }