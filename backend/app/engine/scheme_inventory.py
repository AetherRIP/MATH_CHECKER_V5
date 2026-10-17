"""PAGE-SCOPED VISUAL INVENTORY — what the document itself says it contains.

Why this module exists (measured on this app's own failing mark scheme):

* ``data/mark_schemes/889601795010.pdf`` is a 29-page landscape PDF with ZERO
  extractable text characters on every page (44-57 embedded image tiles per
  page, cover page at 0.2% ink). ``IMAGE_ONLY`` is therefore LEGITIMATE, and it
  also means the local reconstruction has no independent question candidates to
  check the model against: ``candidate_question_numbers()`` returns ``[]``.
* the cached blueprint for that file holds 67 questions / 255 marks although the
  document's own printed index runs to 69. The model SELF-NUMBERS the questions
  ("1. June 05/4H/Q8" ... "67. Jan 23/2HR/Q20"), so a skipped source item simply
  renumbers itself away and ``mark_scheme.sequence_report()`` reports
  ``missing: []``, ``largest_gap: 0``, ``sequential: True``. Every completeness
  check downstream passes while two questions are gone.

So the fix cannot be a better prompt for one giant request. It is:

1. split the document into PAGE chunks with an overlapping page, so a question
   spanning a page boundary is seen whole by at least one chunk;
2. render each page of a chunk as an image at a CONTROLLED DPI and send those
   images inline, instead of relying on the provider's downscaled PDF
   rasterisation — that is what loses the tail of a dense page;
3. require every item to carry the PRINTED ``source_index``. That is the single
   thing that makes the inventory independent of the model's own numbering: if
   index 68 is never returned, the gap is visible no matter how the model
   numbered its output;
4. accept a chunk ONLY when the JSON parses, matches the schema, the finish
   reason is not a truncation/blocking reason, and at least one item carries a
   printed index. A chunk that fails is FAILED and retried on its own — it is
   never partially merged;
5. merge deterministically on the printed index, and when two chunks disagree
   about the same index, report a CONFLICT with both variants. Never discard an
   item because two chunks disagree.

Nothing in this module invents a question, a mark, an index or a page. A gap in
the printed index sequence is reported as a gap; an item whose label or mark
allocation could not be read is kept and flagged, never dropped.
"""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# --------------------------------------------------------------- tuning

#: Pages per request. Six dense landscape pages is comfortably inside the
#: provider's per-request image budget while keeping each page large enough to
#: read its small print.
CHUNK_WINDOW_PAGES = 6
#: One page of overlap: the last page of a chunk is the first page of the next,
#: so a question that continues across the boundary is fully visible in one of
#: them. The merge de-duplicates the re-read by printed index, not by text.
CHUNK_OVERLAP_PAGES = 1
#: Rendered page resolution. The provider downsamples an uploaded PDF page; a
#: page rendered here at 200 dpi and sent as an image does not get that
#: treatment, which is the point.
PAGE_DPI = 200
#: Hard cap, so a pathologically large page cannot blow up one request.
MAX_PAGE_DPI = 300
#: Explicit output budget. Unset, the provider's default silently truncates a
#: long JSON response.
MAX_OUTPUT_TOKENS = 16384
#: How many printed items a single page may plausibly hold, used only to reject
#: a nonsense "highest index" report. Generous on purpose: the real compiled
#: document carries at most four questions per page.
ITEMS_PER_PAGE_ALLOWANCE = 6

# ------------------------------------------------------- chunk statuses

#: The chunk answered and passed every gate.
CHUNK_OK = "ok"
#: The response was cut off by the output-token limit. Never accepted.
CHUNK_TRUNCATED = "truncated"
#: The provider refused to answer (safety/recitation/prohibited content).
CHUNK_BLOCKED = "blocked"
#: A response came back but was not usable (bad JSON, wrong shape, or every
#: item was rejected for lacking a printed index).
CHUNK_MALFORMED = "malformed"
#: The call itself failed (network, quota, model error).
CHUNK_ERROR = "error"
#: The chunk produced no rendered pages (unreadable PDF page range).
CHUNK_UNREADABLE = "unreadable"

FAILED_STATUSES = frozenset({CHUNK_TRUNCATED, CHUNK_BLOCKED, CHUNK_MALFORMED,
                             CHUNK_ERROR, CHUNK_UNREADABLE})

_TRUNCATION_REASONS = frozenset({
    "MAX_TOKENS", "MAX_OUTPUT_TOKENS", "TOKEN_LIMIT", "TRUNCATED",
})
_BLOCKED_REASONS = frozenset({
    "SAFETY", "RECITATION", "BLOCKLIST", "PROHIBITED_CONTENT", "SPII",
    "IMAGE_SAFETY", "LANGUAGE", "MALFORMED_FUNCTION_CALL",
})


class InventoryError(RuntimeError):
    """The document or the request could not be prepared at all."""


# ------------------------------------------------------------ chunk planning

@dataclass(frozen=True)
class PageChunk:
    """One bounded, self-describing slice of the document."""

    id: str
    pages: tuple[int, ...]
    index: int = 0
    sha256: str = ""

    @property
    def first_page(self) -> int:
        return self.pages[0] if self.pages else 0

    @property
    def last_page(self) -> int:
        return self.pages[-1] if self.pages else 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "index": self.index,
            "pages": list(self.pages),
            "page_range": [self.first_page, self.last_page],
            "page_count": len(self.pages),
            "sha256": self.sha256,
        }


def plan_page_chunks(page_count: int, *, window: int = CHUNK_WINDOW_PAGES,
                     overlap: int = CHUNK_OVERLAP_PAGES,
                     sha256: str = "") -> list[PageChunk]:
    """Deterministic overlapping page ranges covering every page exactly once.

    For this app's 29-page document with the defaults that is
    ``1-6, 6-11, 11-16, 16-21, 21-26, 26-29``: six bounded requests instead of
    one unbounded read of the whole document, every page in at least one chunk,
    and every interior boundary page seen twice.

    Deliberately arithmetic, not heuristic: the same document always produces
    the same chunks, so a retry of a failed chunk re-reads the same pages and
    the merge stays idempotent.
    """
    page_count = int(page_count or 0)
    if page_count <= 0:
        return []
    window = max(1, int(window or 1))
    overlap = max(0, min(int(overlap or 0), window - 1)) if window > 1 else 0
    step = max(1, window - overlap)

    ranges: list[tuple[int, int]] = []
    lo = 1
    while lo <= page_count:
        hi = min(lo + window - 1, page_count)
        ranges.append((lo, hi))
        if hi >= page_count:
            break
        lo += step

    # A short final window can be wholly contained in the previous one (for
    # example window=6/overlap=1 over 7 pages yields 1-6 then 6-7, which is
    # fine, but 1-6 then 5-6 would be pure repetition). Dropping only ranges
    # already covered keeps the plan minimal without changing coverage.
    deduped: list[tuple[int, int]] = []
    for lo, hi in ranges:
        if any(lo >= plo and hi <= phi for plo, phi in deduped):
            continue
        deduped.append((lo, hi))

    return [PageChunk(id=f"p{lo}-{hi}",
                      pages=tuple(range(lo, hi + 1)),
                      index=i, sha256=sha256)
            for i, (lo, hi) in enumerate(deduped)]


def document_sha256(path: str | Path) -> str:
    """Content hash of the document, used to key chunks and their results."""
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def page_count(path: str | Path) -> int:
    """Pages in the document, or 0 when it cannot be read."""
    from . import pdf_inspect
    return int(pdf_inspect.page_count(path) or 0)


# ------------------------------------------------------------- page rendering

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


def render_pages_png(path: str | Path, pages: list[int] | tuple[int, ...], *,
                     dpi: int = PAGE_DPI) -> list[dict[str, Any]]:
    """Render 1-based ``pages`` to PNG bytes at a controlled resolution.

    Returns ``[{page, png, width, height, bytes}]`` in the order requested.
    A page that cannot be rendered is skipped and reported through ``error`` on
    the returned list's companion (see :func:`render_chunk`); nothing is
    fabricated for it.

    Raises :class:`InventoryError` when the document itself cannot be opened —
    the caller records that as a failed chunk rather than as an empty inventory.
    """
    lib = _pymupdf()
    if lib is None:
        raise InventoryError(
            "no PDF renderer available (pymupdf is not installed)")
    try:
        doc = lib.open(str(path))
    except Exception as exc:  # noqa: BLE001
        raise InventoryError(f"mark scheme unreadable: {exc}") from exc

    dpi = max(72, min(int(dpi or PAGE_DPI), MAX_PAGE_DPI))
    out: list[dict[str, Any]] = []
    try:
        total = int(doc.page_count)
        for page_no in pages:
            page_no = int(page_no)
            if page_no < 1 or page_no > total:
                continue
            try:
                pixmap = doc[page_no - 1].get_pixmap(dpi=dpi)
                png = pixmap.tobytes("png")
            except Exception:  # noqa: BLE001 — one bad page must not lose the rest
                continue
            out.append({
                "page": page_no,
                "png": png,
                "width": pixmap.width,
                "height": pixmap.height,
                "bytes": len(png),
            })
    finally:
        try:
            doc.close()
        except Exception:
            pass
    return out


@dataclass
class RenderedChunk:
    """The page images for one chunk, plus what could not be rendered."""

    chunk: PageChunk
    images: list[dict[str, Any]] = field(default_factory=list)
    missing_pages: list[int] = field(default_factory=list)
    error: str | None = None

    @property
    def total_bytes(self) -> int:
        return sum(int(img.get("bytes") or 0) for img in self.images)

    def to_dict(self) -> dict[str, Any]:
        return {
            "chunk": self.chunk.to_dict(),
            "rendered_pages": [img["page"] for img in self.images],
            "missing_pages": list(self.missing_pages),
            "image_bytes": self.total_bytes,
            "image_sizes": [[img["page"], img["width"], img["height"]]
                            for img in self.images],
            "error": self.error,
        }


def render_chunk(path: str | Path, chunk: PageChunk, *,
                 dpi: int = PAGE_DPI) -> RenderedChunk:
    """Render one chunk's pages, recording every page that did not render."""
    try:
        images = render_pages_png(path, list(chunk.pages), dpi=dpi)
    except InventoryError as exc:
        return RenderedChunk(chunk=chunk, error=str(exc))
    rendered = {img["page"] for img in images}
    missing = [p for p in chunk.pages if p not in rendered]
    return RenderedChunk(chunk=chunk, images=images, missing_pages=missing)


# ------------------------------------------------------------- the request

CHUNK_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "pages": {"type": "array", "items": {"type": "integer"}},
        "last_index_on_page": {"type": "integer"},
        "questions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "source_index": {"type": "integer"},
                    "question_number": {"type": "string"},
                    "page": {"type": "integer"},
                    "max_marks": {"type": "integer"},
                    "criteria": {"type": "string"},
                    "parts": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "number": {"type": "string"},
                                "max_marks": {"type": "integer"},
                                "criteria": {"type": "string"},
                                "page": {"type": "integer"},
                            },
                        },
                    },
                },
                "required": ["source_index"],
            },
        },
    },
    "required": ["questions"],
}

CHUNK_SYSTEM = """
You are reading a scanned/book-compiled MATHEMATICS MARK SCHEME, page by page.

You are given the page images of a bounded page range of one document. The
pages are supplied in order and each one's page number is stated in the
instruction text. You are NOT marking student work and must never look for it.

The document prints an INDEX NUMBER for every compiled question (a running
number that continues across pages and ends at the last question of the
document). Report that printed index EXACTLY as printed.

For every compiled question on these pages, return:
- source_index: the printed index number of that question. This is the identity
  of the question. Copy the number the page prints. Never renumber, never skip,
  never invent an index that is not printed.
- question_number: the question's own label as printed (for a compiled paper
  this is usually a source reference such as "June 05/4H/Q8"; if the page shows
  only a number, use that). Keep it exactly as printed.
- page: the page number the question starts on.
- max_marks: the TOTAL marks the scheme allocates to that question, as printed.
  Use 0 if the allocation is not visible — do not guess.
- criteria: a compact summary of how the marks are earned (M/A/B codes,
  acceptable answers, follow-through notes) — enough for a marker to award the
  marks without re-reading the page.
- parts: every labelled sub-part the scheme prints for that question, each with
  its own "number", "max_marks", "criteria" and "page". Keep (b)(i) and (b)(ii)
  separate and never move an answer or a mark from one part to another.

Also set:
- pages: the page numbers of the images you were given, in order.
- last_index_on_page: the highest printed index you can see anywhere in these
  pages (0 if you cannot see an index at all).

Rules:
- Report EVERY compiled question on the given pages, including one whose
  statement and mark allocation are split across a page boundary.
- A question that starts on the last page you can see and continues beyond it is
  still one question: report it once, with the page it starts on.
- Never report a page number, a mark value, a source-paper code or a table cell
  as a question.
- If you cannot read an index for an item you can otherwise see, still report
  the item and set source_index to 0.
- Return ONLY the requested JSON structure.
"""


def build_prompt(chunk: PageChunk, *, pages_note: str | None = None) -> str:
    """The instruction text that travels with a chunk's page images."""
    pages = ", ".join(str(p) for p in chunk.pages)
    # No trailing punctuation after the page list: it is a machine-readable
    # contract (the image order), and a full stop makes it ambiguous.
    return (
        f"DOCUMENT PAGE RANGE IN THIS REQUEST: pages {chunk.first_page}"
        f"-{chunk.last_page}.\n"
        f"THE {len(chunk.pages)} IMAGES BELOW ARE, IN ORDER, PAGES: {pages}\n"
        + (pages_note or "")
        + "\nReport every compiled question visible on these pages, each with "
          "the printed index number (source_index) the document shows."
    )


def _image_parts(images: list[dict[str, Any]]) -> list[Any]:
    """Render page images into inline parts the provider reads as pictures.

    Inline (rather than an uploaded file) on purpose: no key-scoped upload to
    re-pay on a key rotation, and the bytes are the pages we rendered at our own
    resolution.
    """
    from google.genai import types  # local import: tests may inject a fake
    return [types.Part.from_bytes(data=img["png"], mime_type="image/png")
            for img in images]


def finish_reason_of(response: Any) -> str:
    """The provider's own stop reason, or ``UNKNOWN`` when it does not say.

    Recorded for every chunk because a successful API response is NOT a
    successful read: ``generate_content ok`` in the logs only means the call
    returned, not that the mark scheme was read whole.
    """
    try:
        candidates = getattr(response, "candidates", None) or []
        if not candidates:
            return "UNKNOWN"
        reason = getattr(candidates[0], "finish_reason", None)
        if reason is None:
            return "UNKNOWN"
        return str(getattr(reason, "name", reason)).upper()
    except Exception:  # noqa: BLE001 — diagnostics must never break a chunk
        return "UNKNOWN"


def _as_int(value: Any) -> int | None:
    try:
        if value is None or isinstance(value, bool):
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


def _clean_parts(raw: Any, page_fallback: int | None) -> list[dict[str, Any]]:
    """Sub-parts as printed, with lenient (never destructive) cleaning."""
    if not isinstance(raw, list):
        return []
    out: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        number = str(item.get("number") or item.get("label") or "").strip()
        marks = _as_int(item.get("max_marks"))
        part: dict[str, Any] = {
            "number": number,
            "max_marks": marks if marks is not None and marks > 0 else 0,
        }
        if marks is None or marks <= 0:
            part["marks_missing"] = True
        criteria = str(item.get("criteria") or "").strip()
        if criteria:
            part["criteria"] = criteria
        page = _as_int(item.get("page")) or page_fallback
        if page:
            part["page"] = page
        out.append(part)
    return out


def _normalize_item(raw: Any, chunk: PageChunk) -> tuple[dict[str, Any] | None, str]:
    """One reported question, or ``(None, reason)`` when it cannot be identified.

    The ONLY hard requirement is a printed index, because that is the identity
    that makes the inventory independent of the model's numbering. A missing
    label or a missing mark allocation is preserved as a flagged field, never a
    reason to throw the question away.
    """
    if not isinstance(raw, dict):
        return None, "item was not an object"
    index = _as_int(raw.get("source_index"))
    if index is None or index <= 0:
        return None, "no printed source_index"
    item: dict[str, Any] = {
        "source_index": index,
        "label": str(raw.get("question_number") or "").strip(),
        "page": _as_int(raw.get("page")) or chunk.first_page,
        "max_marks": _as_int(raw.get("max_marks")) or 0,
        "criteria": str(raw.get("criteria") or "").strip(),
        "chunk": chunk.id,
    }
    if not item["label"]:
        item["label_missing"] = True
    if item["max_marks"] <= 0:
        item["marks_missing"] = True
    parts = _clean_parts(raw.get("parts"), item["page"])
    if parts:
        item["parts"] = parts
    return item, ""


def extract_chunk(client: Any, chunk: PageChunk, *,
                  rendered: RenderedChunk | None = None,
                  model: str,
                  path: str | Path | None = None,
                  dpi: int = PAGE_DPI,
                  config_extra: dict[str, Any] | None = None) -> dict[str, Any]:
    """Read ONE page chunk and enforce every acceptance gate on the answer.

    Returns a result record carrying the status, the accepted items, the
    rejected items with their reasons, the finish reason, the model, the page
    range, the bytes sent and the parse outcome — everything needed to explain
    what happened without re-running the request.
    """
    started = time.time()
    result: dict[str, Any] = {
        "chunk": chunk.to_dict(),
        "model": model,
        "status": CHUNK_ERROR,
        "pages": list(chunk.pages),
        "items": [],
        "rejected": [],
        "finish_reason": None,
        "raw_chars": 0,
        "request_bytes": 0,
        "elapsed_ms": 0,
        "error": None,
        "echoed_pages": None,
        "last_index_on_page": None,
    }

    if rendered is None:
        if path is None:
            result["status"] = CHUNK_UNREADABLE
            result["error"] = "no rendered pages and no document path"
            return result
        rendered = render_chunk(path, chunk, dpi=dpi)
    result["render"] = rendered.to_dict()
    if rendered.error:
        result["status"] = CHUNK_UNREADABLE
        result["error"] = rendered.error
        return result
    if not rendered.images:
        result["status"] = CHUNK_UNREADABLE
        result["error"] = "no page images could be rendered for this range"
        return result
    if rendered.missing_pages:
        result["pages_missing_from_request"] = list(rendered.missing_pages)

    try:
        parts = _image_parts(rendered.images)
    except Exception as exc:  # noqa: BLE001
        result["status"] = CHUNK_UNREADABLE
        result["error"] = f"page images could not be prepared: {exc}"
        return result

    prompt = build_prompt(chunk)
    if rendered.missing_pages:
        prompt += ("\nNOTE: pages "
                   + ", ".join(str(p) for p in rendered.missing_pages)
                   + " could not be included in this request. Report nothing "
                     "you cannot see.")
    contents = [prompt, *parts]
    result["request_bytes"] = rendered.total_bytes + len(prompt)

    try:
        response = client.models.generate_content(
            model=model,
            contents=contents,
            config={
                "system_instruction": CHUNK_SYSTEM,
                "response_mime_type": "application/json",
                "response_schema": CHUNK_SCHEMA,
                # Explicit, because the unset default is what silently cut a
                # long JSON response short.
                "max_output_tokens": MAX_OUTPUT_TOKENS,
                **(config_extra or {}),
            },
        )
    except Exception as exc:  # noqa: BLE001 — recorded, then retried elsewhere
        result["status"] = CHUNK_ERROR
        result["error"] = f"{type(exc).__name__}: {exc}"[:400]
        result["elapsed_ms"] = int((time.time() - started) * 1000)
        return result

    result["finish_reason"] = finish_reason_of(response)
    raw = getattr(response, "text", None) or ""
    result["raw_chars"] = len(raw)

    # Gate 1: the provider must not have cut the answer off or refused it.
    if result["finish_reason"] in _TRUNCATION_REASONS:
        result["status"] = CHUNK_TRUNCATED
        result["error"] = ("the response hit the output-token limit; the tail "
                           "of this page range was never returned")
        result["elapsed_ms"] = int((time.time() - started) * 1000)
        return result
    if result["finish_reason"] in _BLOCKED_REASONS:
        result["status"] = CHUNK_BLOCKED
        result["error"] = f"the provider refused this page range ({result['finish_reason']})"
        result["elapsed_ms"] = int((time.time() - started) * 1000)
        return result

    # Gate 2: strict JSON. extract_json() raises rather than guessing, so a
    # partial answer can never be mistaken for a complete one.
    from . import legacy_marking as legacy
    try:
        parsed = legacy.extract_json(raw)
    except Exception as exc:  # noqa: BLE001
        result["status"] = CHUNK_MALFORMED
        result["error"] = f"unparseable response: {exc}"[:300]
        result["elapsed_ms"] = int((time.time() - started) * 1000)
        return result
    if not isinstance(parsed, dict) or not isinstance(parsed.get("questions"), list):
        result["status"] = CHUNK_MALFORMED
        result["error"] = "response did not contain a 'questions' list"
        result["elapsed_ms"] = int((time.time() - started) * 1000)
        return result

    echoed = parsed.get("pages")
    result["echoed_pages"] = [_as_int(p) for p in echoed] if isinstance(echoed, list) else None
    result["last_index_on_page"] = _as_int(parsed.get("last_index_on_page"))

    # Gate 3: every item must be identifiable by its printed index.
    accepted: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    for raw_item in parsed["questions"]:
        item, reason = _normalize_item(raw_item, chunk)
        if item is None:
            rejected.append({"reason": reason,
                             "raw": _short(raw_item)})
        else:
            accepted.append(item)

    result["items"] = accepted
    result["rejected"] = rejected

    if not accepted:
        if rejected:
            # Items were returned but none could be identified: the page range
            # was read without its numbering, so the answer is not usable.
            result["status"] = CHUNK_MALFORMED
            result["error"] = (
                f"every one of the {len(rejected)} reported item(s) lacked a "
                "printed index — the page range was not read with its numbering")
            result["elapsed_ms"] = int((time.time() - started) * 1000)
            return result
        # An empty answer is NOT a failure: a cover page or a page of contents
        # legitimately holds no question. It is recorded as a page range that
        # produced nothing, which the report lists for review instead of
        # pretending the pages were read.
        result["note"] = "no compiled question is visible on this page range"

    result["status"] = CHUNK_OK
    result["elapsed_ms"] = int((time.time() - started) * 1000)
    return result


def _short(value: Any, limit: int = 200) -> str:
    try:
        text = str(value)
    except Exception:  # noqa: BLE001
        return "<unprintable>"
    return text[:limit]


def chunk_ok(result: dict[str, Any] | None) -> bool:
    """Is this chunk result usable, i.e. did it pass every gate?"""
    return bool(result) and result.get("status") == CHUNK_OK


def candidate_models(extra: list[str] | None = None) -> list[str]:
    """The configured model chain, in order, with no duplicates."""
    models: list[str] = []
    try:
        from ..config import get_config
        models = [str(m) for m in (get_config().gemini.primary_models or []) if m]
    except Exception:  # noqa: BLE001 — a missing config is not fatal here
        models = []
    for name in (extra or []):
        if name and name not in models:
            models.append(str(name))
    return models


def extract_chunk_with_retries(client: Any, chunk: PageChunk, *,
                               models: list[str],
                               path: str | Path | None = None,
                               attempts_per_model: int = 1,
                               dpi: int = PAGE_DPI,
                               rendered: RenderedChunk | None = None,
                               config_extra: dict[str, Any] | None = None) -> dict[str, Any]:
    """Read a chunk, retrying ONLY this chunk until a model passes the gates.

    Retrying a bounded page range is the whole point of chunking: a failed chunk
    costs one small request, not the entire document. The retry is idempotent
    because the merge is keyed on the printed index — re-reading pages 6-11 can
    never create a second copy of question 8.

    ``status`` is the LAST attempt's status; every attempt is recorded so a
    chunk that fails for a real reason is distinguishable from one that failed
    on a cooling-down provider.
    """
    attempts: list[dict[str, Any]] = []
    if not models:
        failed = {
            "chunk": chunk.to_dict(), "status": CHUNK_ERROR, "items": [],
            "rejected": [], "model": None, "error": "no model configured",
            "finish_reason": None, "raw_chars": 0, "request_bytes": 0,
            "elapsed_ms": 0, "pages": list(chunk.pages),
        }
        failed["attempts"] = attempts
        return failed

    last: dict[str, Any] | None = None
    for model in models:
        for _ in range(max(1, int(attempts_per_model))):
            result = extract_chunk(client, chunk, rendered=rendered, model=model,
                                   path=path, dpi=dpi, config_extra=config_extra)
            attempts.append({k: result.get(k) for k in
                             ("model", "status", "finish_reason", "raw_chars",
                              "elapsed_ms", "error")})
            last = result
            if chunk_ok(result):
                result["attempts"] = attempts
                return result
    assert last is not None
    last["attempts"] = attempts
    return last


# ------------------------------------------------------------------- merging

def _item_fingerprint(item: dict[str, Any]) -> tuple:
    """What two reads of the SAME printed question must agree on."""
    parts = tuple(sorted(
        (str(p.get("number") or "").strip().lower(),
         int(p.get("max_marks") or 0))
        for p in (item.get("parts") or [])))
    return (str(item.get("label") or "").strip().lower(),
            int(item.get("max_marks") or 0), parts)


def _completeness_score(item: dict[str, Any]) -> tuple:
    """Prefer the richer read when two chunks agree on identity."""
    return (
        1 if item.get("parts") else 0,
        len(item.get("parts") or []),
        len(str(item.get("criteria") or "")),
        1 if item.get("page") else 0,
        1 if not item.get("label_missing") else 0,
    )


def merge_chunks(results: list[dict[str, Any]] | None) -> dict[str, Any]:
    """Merge chunk results into ONE inventory, keyed on the printed index.

    * the same index seen in two chunks (the deliberate page overlap) is
      de-duplicated when both reads agree — recorded as ``reread``, not as a
      change;
    * the same index seen differently in two chunks becomes a CONFLICT, and BOTH
      variants are kept: the richer one is provisional, the other is retained in
      ``variants`` so a human can see the disagreement;
    * an item with no printed index was already rejected by the chunk gate and
      is counted, never silently dropped;
    * nothing is dropped because a chunk failed — a failed chunk leaves a gap in
      coverage, which the report states.
    """
    results = [r for r in (results or []) if isinstance(r, dict)]
    by_index: dict[int, list[dict[str, Any]]] = {}
    unkeyed: list[dict[str, Any]] = []
    conflicts: list[dict[str, Any]] = []
    reread: list[int] = []

    for result in results:
        chunk_id = (result.get("chunk") or {}).get("id") or "?"
        for item in (result.get("items") or []):
            index = _as_int(item.get("source_index"))
            if index is None or index <= 0:
                unkeyed.append({"chunk": chunk_id, "item": item})
                continue
            copy = dict(item)
            copy.setdefault("chunk", chunk_id)
            by_index.setdefault(index, []).append(copy)

    items: list[dict[str, Any]] = []
    for index in sorted(by_index):
        occurrences = by_index[index]
        groups: dict[tuple, list[dict[str, Any]]] = {}
        for item in occurrences:
            groups.setdefault(_item_fingerprint(item), []).append(item)
        seen_in = sorted({occ.get("chunk") for occ in occurrences})
        if len(groups) > 1:
            # The summary has to be specific enough for a human to act on:
            # "two chunks disagree" is useless, "(b)(ii) is worth 2 in one read
            # and 1 in the other" is a decision. So each variant carries its
            # sub-part numbers and marks, not just how many parts it had.
            variants = [
                {"chunk": group[0].get("chunk"),
                 "label": group[0].get("label"),
                 "max_marks": group[0].get("max_marks"),
                 "parts": len(group[0].get("parts") or []),
                 "parts_detail": [[str(p.get("number") or ""),
                                   int(p.get("max_marks") or 0)]
                                  for p in (group[0].get("parts") or [])],
                 "page": group[0].get("page")}
                for group in groups.values()
            ]
            best = max(occurrences, key=_completeness_score)
            entry = dict(best)
            entry["seen_in_chunks"] = seen_in
            entry["conflict"] = True
            entry["variants"] = variants
            items.append(entry)
            conflicts.append({
                "source_index": index,
                "kind": "chunks_disagree",
                "chunks": seen_in,
                "variants": variants,
                "provisional_from_chunk": best.get("chunk"),
            })
            continue
        if len(occurrences) > 1:
            reread.append(index)
        best = max(occurrences, key=_completeness_score)
        entry = dict(best)
        entry["seen_in_chunks"] = seen_in
        items.append(entry)

    return {
        "items": items,
        "indices": sorted(by_index),
        "conflicts": conflicts,
        "reread_indices": reread,
        "unkeyed": unkeyed,
    }


# ------------------------------------------------------------------ reporting

def _chunk_pages(results: list[dict[str, Any]], chunk_id: Any) -> list[int]:
    """The pages a chunk record covered (for overlap comparison)."""
    for result in results:
        chunk = result.get("chunk") or {}
        if chunk.get("id") == chunk_id:
            return [int(p) for p in (chunk.get("pages") or [])]
    return []


def inventory_report(results: list[dict[str, Any]] | None, *,
                     page_count_total: int = 0,
                     merge: dict[str, Any] | None = None) -> dict[str, Any]:
    """Everything the caller needs to decide whether the inventory is complete.

    ``expected_count`` is derived from the document's own printed index only
    when that index starts at 1 and is otherwise contiguous up to its maximum:
    that is the case where a missing index is unambiguously a lost question. It
    is ``None`` (with a reason) when the numbering does not support that
    inference — the caller must then treat the count as unestablished instead of
    assuming any particular total.
    """
    results = [r for r in (results or []) if isinstance(r, dict)]
    merged = merge if merge is not None else merge_chunks(results)

    indices = list(merged.get("indices") or [])
    items = list(merged.get("items") or [])
    chunks: list[dict[str, Any]] = []
    for result in results:
        chunk = result.get("chunk") or {}
        chunks.append({
            "id": chunk.get("id"),
            "pages": chunk.get("pages"),
            "status": result.get("status"),
            "items": len(result.get("items") or []),
            "rejected": len(result.get("rejected") or []),
            "model": result.get("model"),
            "finish_reason": result.get("finish_reason"),
            "attempts": len(result.get("attempts") or []) or 1,
            "elapsed_ms": result.get("elapsed_ms"),
            "request_bytes": result.get("request_bytes"),
            "raw_chars": result.get("raw_chars"),
            "echoed_pages": result.get("echoed_pages"),
            "last_index_on_page": result.get("last_index_on_page"),
            "pages_missing_from_request": result.get("pages_missing_from_request"),
            "error": result.get("error"),
        })

    failed = [c for c in chunks if c["status"] != CHUNK_OK]

    # The document's OWN evidence of how far its numbering goes: a chunk also
    # reports the highest printed index it could SEE, which is independent of
    # how many items it managed to list. Without this, 67 items ending at index
    # 67 look perfect even when the last page visibly prints "69."
    reported_last = [
        {"chunk": (r.get("chunk") or {}).get("id"),
         "last_index": _as_int(r.get("last_index_on_page"))}
        for r in results
        if r.get("status") == CHUNK_OK and (_as_int(r.get("last_index_on_page")) or 0) > 0
    ]
    # A "highest index" is only evidence when the chunk that reported it is
    # consistent with itself: it must not claim to have seen less than an item
    # it actually listed, and it must not claim a number far beyond the items it
    # listed on its own pages (which is how a page number or a cell value would
    # masquerade as the end of the document). Everything else is recorded as
    # implausible and NOT used to set the expected count.
    plausible_last: list[dict[str, Any]] = []
    implausible_last: list[dict[str, Any]] = []
    for entry in reported_last:
        value = int(entry["last_index"])
        chunk_items = [int(i.get("source_index") or 0) for i in items
                       if entry["chunk"] in (i.get("seen_in_chunks") or [])]
        highest_listed = max(chunk_items, default=0)
        allowance = ITEMS_PER_PAGE_ALLOWANCE * max(
            1, len(_chunk_pages(results, entry["chunk"])))
        if value < highest_listed:
            implausible_last.append({**entry,
                                     "reason": "lower than an item it listed"})
        elif value > highest_listed + allowance:
            implausible_last.append({**entry,
                                     "reason": "far above the items it listed"})
        else:
            plausible_last.append(entry)
    observed_last = max((int(e["last_index"]) for e in plausible_last), default=0)

    expected: int | None = None
    expected_reason = None
    if indices:
        if indices[0] != 1:
            expected_reason = (f"the printed index starts at {indices[0]}, so a "
                               "missing index cannot be inferred from the range")
        else:
            expected = max(max(indices), observed_last)
            gaps = [n for n in range(1, expected + 1) if n not in set(indices)]
            if not gaps:
                expected_reason = "the printed index is contiguous from 1"
            elif observed_last > max(indices):
                expected_reason = (
                    f"the pages report a printed index up to {observed_last} but "
                    f"only {len(indices)} item(s) came back; index "
                    + ", ".join(str(g) for g in gaps) + " is missing from the read")
            else:
                expected_reason = (f"the printed index reaches {expected} but "
                                   + ", ".join(str(g) for g in gaps)
                                   + " did not come back from any chunk")
    else:
        expected_reason = "no chunk returned an item with a printed index"

    present = set(indices)
    missing_indices = ([n for n in range(1, expected + 1) if n not in present]
                       if expected else [])

    # Chunks are ordered by page, and each one ends where the next begins, so
    # the highest printed index they report must never go BACKWARDS: a later
    # page range cannot have seen less of the document than an earlier one that
    # it already contains. A decrease means one of the two reads missed content
    # on the shared page, which must not be averaged away.
    # (Comparing the numbers directly between neighbours would be meaningless —
    # they cover different ranges by design — which is exactly why this check is
    # about the ORDER of the numbers, not their equality.)
    last_index_regressions: list[dict[str, Any]] = []
    ordered_last = sorted(plausible_last,
                          key=lambda e: (_chunk_pages(results, e["chunk"]) or [0])[0])
    for left, right in zip(ordered_last, ordered_last[1:]):
        if int(right["last_index"]) < int(left["last_index"]):
            last_index_regressions.append({
                "chunks": [left["chunk"], right["chunk"]],
                "last_index": [left["last_index"], right["last_index"]],
            })

    marks_missing = [item.get("source_index") for item in items
                     if item.get("marks_missing")]
    labels_missing = [item.get("source_index") for item in items
                      if item.get("label_missing")]
    total_marks = sum(int(item.get("max_marks") or 0) for item in items)

    pages_with_items: set[int] = set()
    for item in items:
        if item.get("page"):
            pages_with_items.add(int(item["page"]))
        for part in (item.get("parts") or []):
            if part.get("page"):
                pages_with_items.add(int(part["page"]))
    planned_pages = sorted({p for c in chunks for p in (c.get("pages") or [])})
    pages_without_items = [p for p in planned_pages if p not in pages_with_items]

    # A total that could not be established is itself unresolved: a structure
    # whose expected count is unknown must not be reported as verified.
    unresolved = bool(failed or missing_indices or merged.get("conflicts")
                      or merged.get("unkeyed") or marks_missing
                      or last_index_regressions or expected is None)

    return {
        "detected_count": len(items),
        "expected_count": expected,
        "expected_basis": expected_reason,
        "missing_indices": missing_indices,
        "conflicting_indices": [c["source_index"] for c in merged.get("conflicts") or []],
        "reread_indices": merged.get("reread_indices") or [],
        "unkeyed_items": len(merged.get("unkeyed") or []),
        "items_total_marks": total_marks,
        "marks_missing_indices": marks_missing,
        "labels_missing_indices": labels_missing,
        "reported_last_index": ordered_last,
        "observed_last_index": observed_last or None,
        "implausible_last_index": implausible_last,
        "last_index_regressions": last_index_regressions,
        "chunks": chunks,
        "failed_chunks": [c["id"] for c in failed],
        "planned_pages": planned_pages,
        "pages_with_items": sorted(pages_with_items),
        "pages_without_items": pages_without_items,
        "page_count": int(page_count_total or 0),
        "unresolved": unresolved,
        "verdict": _verdict(expected, len(items), failed, missing_indices,
                            merged.get("conflicts") or [],
                            merged.get("unkeyed") or []),
    }


def _verdict(expected: int | None, detected: int, failed: list[dict],
             missing: list[int], conflicts: list[dict],
             unkeyed: list[dict]) -> str:
    """One honest sentence about whether the inventory can be trusted.

    Order matters: an unread page range makes every other statement unsafe, so
    it is reported first and the count is never called complete over a gap.
    """
    if failed:
        return (f"INCOMPLETE: {len(failed)} chunk(s) failed their gates, so "
                "their pages were not read")
    if missing:
        return (f"INCOMPLETE: the document prints an index up to {expected} but "
                f"{len(missing)} index/indices did not come back "
                f"({', '.join(str(m) for m in missing)})")
    if conflicts:
        return (f"UNRESOLVED: {len(conflicts)} index/indices were read "
                "differently by overlapping chunks")
    if unkeyed:
        return f"UNRESOLVED: {len(unkeyed)} item(s) carried no printed index"
    if expected is None:
        return (f"UNESTABLISHED: {detected} item(s) were read, but the "
                "document's own numbering does not establish how many it "
                "should contain")
    return (f"COMPLETE: all {detected} item(s) accounted for, printed index "
            f"1-{expected} with no gaps")


# -------------------------------------------------------------- orchestration

def extract_inventory(client: Any, path: str | Path, *,
                      window: int = CHUNK_WINDOW_PAGES,
                      overlap: int = CHUNK_OVERLAP_PAGES,
                      dpi: int = PAGE_DPI,
                      models: list[str] | None = None,
                      attempts_per_model: int = 1,
                      config_extra: dict[str, Any] | None = None,
                      on_chunk=None) -> dict[str, Any]:
    """Read the WHOLE document page-by-page and report what it contains.

    Every page of the document is read by at least one bounded request; a chunk
    that fails is retried on its own; the merge is deterministic on the printed
    index. The return value carries the per-chunk result records, the merged
    inventory, the printed-index report and the document diagnostics — enough to
    decide, with evidence, whether the structure can be treated as verified.
    """
    path = Path(path)
    sha = document_sha256(path)
    total_pages = page_count(path)
    if total_pages <= 0:
        raise InventoryError(f"no pages could be read from {path.name}")
    chunks = plan_page_chunks(total_pages, window=window, overlap=overlap,
                              sha256=sha)
    chain = list(models or candidate_models())

    rendered: dict[str, RenderedChunk] = {}
    results: list[dict[str, Any]] = []
    for chunk in chunks:
        image = render_chunk(path, chunk, dpi=dpi)
        rendered[chunk.id] = image
        result = extract_chunk_with_retries(
            client, chunk, models=chain, path=path, dpi=dpi,
            attempts_per_model=attempts_per_model, rendered=image,
            config_extra=config_extra)
        results.append(result)
        if on_chunk:
            try:
                on_chunk(result)
            except Exception:  # noqa: BLE001 — progress reporting is optional
                pass

    merged = merge_chunks(results)
    report = inventory_report(results, page_count_total=total_pages, merge=merged)
    return {
        "sha256": sha,
        "page_count": total_pages,
        "chunks": [r.get("chunk") for r in results],
        "results": results,
        "items": merged["items"],
        "merge": {
            "conflicts": merged["conflicts"],
            "reread_indices": merged["reread_indices"],
            "unkeyed": merged["unkeyed"],
        },
        "report": report,
        "rendered": {cid: img.to_dict() for cid, img in rendered.items()},
    }


#: A focused question about SPECIFIC pages: which printed indices are on them.
#: Used to settle a tail the chunked read did not return, and to check whether
#: the document's numbering really stops where a chunk said it did.
INDEX_PROBE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "pages": {"type": "array", "items": {"type": "integer"}},
        "indices": {"type": "array", "items": {"type": "integer"}},
        "highest_index": {"type": "integer"},
        "stated_total_marks": {"type": "integer"},
        "notes": {"type": "string"},
    },
    "required": ["indices"],
}

INDEX_PROBE_SYSTEM = """
You are checking the NUMBERING of specific pages of a compiled mathematics mark
scheme. You are NOT marking student work and must never look for it.

Transcribe, do not summarise:
- "indices": EVERY printed index number you can see on the given pages, in the
  order they appear, including any on the last visible line or in the footer,
  and including any that appear without a full question beside them. One entry
  per printed number. Do not de-duplicate, do not skip, do not infer.
- "highest_index": the largest printed index number visible anywhere on these
  pages (0 if none is visible).
- "stated_total_marks": the total marks the pages state, if they state one
  (0 if not).
- "pages": the page numbers of the images you were given, in order.
- "notes": anything that made a number hard to read.

If a printed index appears at the very bottom edge of a page, it is still
printed: report it. Return ONLY the requested JSON structure.
"""


def probe_page_indices(client: Any, path: str | Path, pages: list[int], *,
                      model: str | None = None,
                      dpi: int = 250) -> dict[str, Any]:
    """Which printed indices does the document actually show on these pages?

    A bounded, single-purpose read used to settle exactly the question a chunk
    merge cannot answer on its own: whether the numbering continues past the
    last index that came back. Same acceptance gates as a chunk (finish reason,
    strict JSON), minus the inventory merge — this answers about NUMBERING, not
    about marks. Nothing is persisted.
    """
    chunk = PageChunk(id=f"probe-p{pages[0]}-{pages[-1]}",
                      pages=tuple(int(p) for p in pages))
    out: dict[str, Any] = {
        "pages": list(chunk.pages),
        "model": model,
        "status": CHUNK_ERROR,
        "indices": [],
        "highest_index": None,
        "stated_total_marks": None,
        "notes": "",
        "finish_reason": None,
        "raw_chars": 0,
        "request_bytes": 0,
        "error": None,
    }
    chosen = model or (candidate_models() or [None])[0]
    out["model"] = chosen
    if not chosen:
        out["error"] = "no model configured"
        return out

    rendered = render_chunk(path, chunk, dpi=dpi)
    if rendered.error or not rendered.images:
        out["status"] = CHUNK_UNREADABLE
        out["error"] = rendered.error or "no page images could be rendered"
        return out
    out["request_bytes"] = rendered.total_bytes
    try:
        parts = _image_parts(rendered.images)
    except Exception as exc:  # noqa: BLE001
        out["status"] = CHUNK_UNREADABLE
        out["error"] = f"page images could not be prepared: {exc}"
        return out

    prompt = (build_prompt(chunk) + "\nTranscribe every printed index number "
              "visible on these pages, plus the highest one and any stated "
              "total, exactly as printed.")
    try:
        response = client.models.generate_content(
            model=chosen,
            contents=[prompt, *parts],
            config={
                "system_instruction": INDEX_PROBE_SYSTEM,
                "response_mime_type": "application/json",
                "response_schema": INDEX_PROBE_SCHEMA,
                "max_output_tokens": MAX_OUTPUT_TOKENS,
            },
        )
    except Exception as exc:  # noqa: BLE001
        out["error"] = f"{type(exc).__name__}: {exc}"[:300]
        return out

    out["finish_reason"] = finish_reason_of(response)
    raw = getattr(response, "text", None) or ""
    out["raw_chars"] = len(raw)
    if out["finish_reason"] in _TRUNCATION_REASONS:
        out["status"] = CHUNK_TRUNCATED
        out["error"] = "the response hit the output-token limit"
        return out
    if out["finish_reason"] in _BLOCKED_REASONS:
        out["status"] = CHUNK_BLOCKED
        out["error"] = f"the provider refused this page range ({out['finish_reason']})"
        return out

    from . import legacy_marking as legacy
    try:
        parsed = legacy.extract_json(raw)
    except Exception as exc:  # noqa: BLE001
        out["status"] = CHUNK_MALFORMED
        out["error"] = f"unparseable response: {exc}"[:300]
        return out
    if not isinstance(parsed, dict):
        out["status"] = CHUNK_MALFORMED
        out["error"] = "response was not an object"
        return out

    raw_indices = parsed.get("indices")
    indices = [_as_int(v) for v in raw_indices] if isinstance(raw_indices, list) else None
    if indices is None:
        out["status"] = CHUNK_MALFORMED
        out["error"] = "response did not contain an 'indices' list"
        return out
    out["indices"] = sorted({i for i in indices if i and i > 0})
    highest = _as_int(parsed.get("highest_index"))
    out["highest_index"] = highest or (max(out["indices"], default=None))
    out["stated_total_marks"] = _as_int(parsed.get("stated_total_marks"))
    out["notes"] = _short(parsed.get("notes") or "")
    out["status"] = CHUNK_OK
    return out


def baseline_full_document_read(client: Any, path: str | Path, *,
                                model: str | None = None,
                                upload_purpose: str = "mark_scheme") -> dict[str, Any]:
    """The single-request read of the WHOLE document, for comparison only.

    This is the shape of request that produced the 67-question blueprint. It is
    measured here so the loss is demonstrated rather than asserted: same
    document, same question, whole file in one request versus page-scoped reads.
    It deliberately does not persist anything.
    """
    path = Path(path)
    chain = candidate_models()
    chosen = model or (chain[0] if chain else None)
    out: dict[str, Any] = {"model": chosen, "item_count": None,
                           "max_index": None, "indices": [],
                           "finish_reason": None, "error": None,
                           "request_bytes": 0, "raw_chars": 0}
    if not chosen:
        out["error"] = "no model configured"
        return out
    try:
        remote = client.upload_file(str(path), purpose=upload_purpose)
    except Exception as exc:  # noqa: BLE001
        out["error"] = f"upload failed: {exc}"[:300]
        return out
    try:
        total = page_count(path)
        chunk = PageChunk(id=f"p1-{total or 1}",
                          pages=tuple(range(1, (total or 1) + 1)))
        response = client.models.generate_content(
            model=chosen,
            contents=[remote, build_prompt(chunk)],
            config={
                "system_instruction": CHUNK_SYSTEM,
                "response_mime_type": "application/json",
                "response_schema": CHUNK_SCHEMA,
                "max_output_tokens": MAX_OUTPUT_TOKENS,
            },
        )
        out["finish_reason"] = finish_reason_of(response)
        raw = getattr(response, "text", None) or ""
        out["raw_chars"] = len(raw)
        from . import legacy_marking as legacy
        parsed = legacy.extract_json(raw)
        questions = parsed.get("questions") if isinstance(parsed, dict) else None
        if isinstance(questions, list):
            indices = []
            for item in questions:
                index = _as_int((item or {}).get("source_index")) if isinstance(item, dict) else None
                if index:
                    indices.append(index)
            out["indices"] = sorted(set(indices))
            out["item_count"] = len(questions)
            out["max_index"] = max(indices) if indices else None
    except Exception as exc:  # noqa: BLE001
        out["error"] = f"{type(exc).__name__}: {exc}"[:300]
    return out
