"""Deterministic PDF inspection helpers.

Nothing here calls a model. This module answers, offline and reproducibly:

* how many pages a file has (used by the read-only viewer and inventories);
* which question numbers appear on which page of a student answer PDF;
* how to slice a page range out into its own PDF.

Text extraction prefers PyMuPDF (already used by the mark-scheme analyser) and
falls back to pypdf, which is also a hard dependency. When neither can read a
text layer the page/question evidence is simply empty and the planner produces
a valid page-count-only partition — the correction never fails because
inspection could not read text.
"""

from __future__ import annotations

import os
import re
import threading
from dataclasses import dataclass, field
from pathlib import Path

# A question marker is a small integer at the start of a line, optionally
# followed by '.' or ')'. Sub-parts (1a, 1(a)) collapse to their top-level
# number so a batch boundary can keep the whole question together.
_QUESTION_LINE = re.compile(r"^\s*(\d{1,2})\s*(?:[\.\)]|\s|$)")


@dataclass
class PdfInspection:
    path: str
    page_count: int = 0
    mode: str = "NONE"                    # TEXT | IMAGE_ONLY | NONE
    pages_with_text: int = 0
    per_page_questions: dict[int, list[str]] = field(default_factory=dict)
    questions: list[str] = field(default_factory=list)
    question_pages: dict[str, list[int]] = field(default_factory=dict)
    error: str | None = None

    def to_dict(self) -> dict:
        return {
            "path": self.path,
            "page_count": self.page_count,
            "mode": self.mode,
            "pages_with_text": self.pages_with_text,
            "per_page_questions": {str(k): v for k, v in
                                   self.per_page_questions.items()},
            "questions": list(self.questions),
            "question_pages": {k: list(v) for k, v in self.question_pages.items()},
            "error": self.error,
        }


def _pymupdf():
    for name in ("pymupdf", "fitz"):
        try:
            return __import__(name)
        except Exception:
            continue
    return None


def page_count(path: str | Path) -> int:
    """Number of pages, or 0 when the file cannot be read."""
    lib = _pymupdf()
    if lib is not None:
        try:
            doc = lib.open(str(path))
            try:
                return int(doc.page_count)
            finally:
                doc.close()
        except Exception:
            pass
    try:
        from pypdf import PdfReader
        return len(PdfReader(str(path)).pages)
    except Exception:
        return 0


def _page_texts(path: str | Path) -> tuple[list[str], str | None]:
    """Return per-page text (index 0 == page 1) plus an error, if any."""
    lib = _pymupdf()
    if lib is not None:
        try:
            doc = lib.open(str(path))
        except Exception as exc:  # noqa: BLE001
            return [], f"unreadable PDF: {exc}"
        try:
            texts = []
            for pno in range(doc.page_count):
                try:
                    texts.append(doc[pno].get_text() or "")
                except Exception:
                    texts.append("")
            return texts, None
        finally:
            try:
                doc.close()
            except Exception:
                pass
    try:
        from pypdf import PdfReader
        reader = PdfReader(str(path))
        texts = []
        for page in reader.pages:
            try:
                texts.append(page.extract_text() or "")
            except Exception:
                texts.append("")
        return texts, None
    except Exception as exc:  # noqa: BLE001
        return [], f"unreadable PDF: {exc}"


def normalise_question(token: str) -> str | None:
    """Collapse a marker to its top-level question number."""
    match = re.match(r"\s*(\d{1,2})", str(token))
    if not match:
        return None
    try:
        return str(int(match.group(1)))
    except ValueError:
        return None


def _questions_on_page(text: str, expected: set[str] | None) -> list[str]:
    found: list[str] = []
    seen: set[str] = set()
    for line in (text or "").splitlines():
        match = _QUESTION_LINE.match(line)
        if not match:
            continue
        qid = normalise_question(match.group(1))
        if not qid or qid in seen:
            continue
        if expected is not None and qid not in expected:
            continue
        seen.add(qid)
        found.append(qid)
    return found


def inspect_student_pdf(path: str | Path,
                        expected_questions: list[str] | None = None) -> PdfInspection:
    """Deterministically map the student answer PDF's questions to pages.

    ``expected_questions`` (from the official mark scheme) restricts the
    evidence to real question numbers, so page numbers and "marks" values do
    not create phantom questions. When it is omitted every line-leading
    integer is considered.
    """
    path = str(path)
    inspection = PdfInspection(path=path)
    expected: set[str] | None = None
    if expected_questions:
        expected = {normalise_question(q) for q in expected_questions if q}
        expected.discard(None)

    texts, error = _page_texts(path)
    if error:
        inspection.error = error
    inspection.page_count = len(texts)
    inspection.mode = "TEXT" if any(t.strip() for t in texts) else (
        "IMAGE_ONLY" if texts else "NONE")

    per_page: dict[int, list[str]] = {}
    for index, text in enumerate(texts, start=1):
        if text.strip():
            inspection.pages_with_text += 1
        per_page[index] = _questions_on_page(text, expected)
    inspection.per_page_questions = per_page

    question_pages: dict[str, list[int]] = {}
    for page, qids in per_page.items():
        for qid in qids:
            question_pages.setdefault(qid, []).append(page)
    inspection.question_pages = {
        q: sorted(pages) for q, pages in question_pages.items()}

    from .batching import _question_sort_key
    inspection.questions = sorted(question_pages, key=_question_sort_key)
    return inspection


# Derived mark-scheme evidence, keyed by FILE IDENTITY (absolute path, size,
# mtime_ns), never by time. Analysing a multi-page scheme's text layer is the
# expensive deterministic step, and every long-document job for the same
# assignment asks the same question about the same file; it used to be redone
# for every student. Only the tiny derived result is kept (ids + page count),
# never the page/block evidence, so memory stays small.
#
# Invalidation is implicit and exact: a replaced or edited file has a different
# size/mtime and is analysed again. A failed analysis is NOT cached, so a
# transient read problem cannot stick to the file for the rest of the process.
_SCHEME_CACHE: dict[tuple[str, int, int], tuple[tuple[str, ...], int]] = {}
_SCHEME_CACHE_LOCK = threading.Lock()
_SCHEME_CACHE_MAX = 16


def scheme_evidence(mark_scheme_path: str | Path) -> tuple[list[str], int]:
    """``(official question ids, page count)`` from ONE analysis of a scheme.

    Deterministic and model-free: uses the reconstruction the app already
    trusts (``mark_scheme.analyse_document`` -> ``candidate_question_numbers``).
    An unreadable or image-only scheme yields ``([], page_count)``, which the
    planner treats as "structure unknown" rather than guessing. This is also
    the only place callers should need: the page count and the question ids
    come from the same read instead of opening the PDF twice.
    """
    path = str(mark_scheme_path)
    try:
        stat = os.stat(path)
        key = (os.path.abspath(path), int(stat.st_size), int(stat.st_mtime_ns))
    except OSError:
        key = None
    if key is not None:
        with _SCHEME_CACHE_LOCK:
            hit = _SCHEME_CACHE.get(key)
        if hit is not None:
            return list(hit[0]), hit[1]

    from . import mark_scheme as ms
    try:
        evidence = ms.analyse_document(path)
    except Exception:  # noqa: BLE001 — never fail a plan over read-only evidence
        # Page count is still useful for the plan/report; only the question ids
        # are unknown. This path is not cached (see below).
        return [], page_count(path)
    try:
        questions = tuple(ms.candidate_question_numbers(evidence))
    except Exception:  # noqa: BLE001
        questions = ()
    pages = int(evidence.get("page_count") or 0)

    if key is not None and evidence.get("mode") not in (None, "NONE"):
        with _SCHEME_CACHE_LOCK:
            if len(_SCHEME_CACHE) >= _SCHEME_CACHE_MAX:
                # Drop the oldest insertion; entries are cheap to rebuild.
                _SCHEME_CACHE.pop(next(iter(_SCHEME_CACHE)))
            _SCHEME_CACHE[key] = (questions, pages)
    return list(questions), pages


def expected_question_numbers(mark_scheme_path: str | Path) -> list[str]:
    """Official question ids from a mark-scheme PDF, with NO AI call."""
    return scheme_evidence(mark_scheme_path)[0]


def split_pdf(source: str | Path, ranges: list[tuple[int, int]],
              out_dir: str | Path, *, prefix: str = "batch") -> list[str]:
    """Write each inclusive 1-based page range to its own PDF.

    Returns the list of written file paths, in the order the ranges were
    given. A missing page in the source is skipped rather than fabricated.
    """
    from pypdf import PdfReader, PdfWriter

    source = str(source)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    reader = PdfReader(source)
    total = len(reader.pages)

    written: list[str] = []
    for index, (lo, hi) in enumerate(ranges):
        lo = max(1, int(lo))
        hi = min(total, int(hi))
        writer = PdfWriter()
        if hi >= lo:
            for page in range(lo - 1, hi):
                writer.add_page(reader.pages[page])
        else:  # nothing to extract — still produce a readable placeholder
            writer.add_blank_page(width=200, height=200)
        target = out_dir / f"{prefix}_{index:03d}_{lo}-{hi}.pdf"
        with open(target, "wb") as handle:
            writer.write(handle)
        written.append(str(target))
    return written
