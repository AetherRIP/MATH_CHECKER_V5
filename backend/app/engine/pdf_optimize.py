"""PDF optimization shared by BOTH correction pipelines (§ compression).

One implementation, two roles:

* ``role="submission"``  — the student's paper (handwriting, small minus signs,
  decimal points, indices, fractions, faint pencil).
* ``role="mark_scheme"`` — the reference document (mark codes M1/A1/B1, small
  allocations, alternative methods, diagrams).

The teacher's original is NEVER modified or replaced. Optimization writes a
separate, verified copy into a content-addressed cache and returns its path;
callers use that copy for uploading/rendering/marking while retention keeps the
original. When optimization is not worthwhile, unsafe, or fails any integrity
check, the ORIGINAL path is returned and the reason is recorded.

Why this order:

1. Lossless structural clean-up (``garbage=4, deflate, clean, objstms``) is
   cheap (≈0.03 s/page) and safe, so it always runs first.
2. Image rewriting (downsample above 250 dpi to 200 dpi, JPEG q≈85) is the
   lossy step and only runs when the images are genuinely oversized AND they
   dominate the file. Measured on the installation's own documents: a 300 dpi
   scanned mark scheme drops 45% with a 0.3/255 mean render difference, while
   a 50-page scan already at ~150 dpi gains 1% and costs 28 s — so the plan
   check is what keeps compression from becoming pure overhead.
3. The candidate is accepted only if it is smaller AND passes the integrity
   checks below; otherwise the original is used.

Cache key = SHA-256 of the source bytes + the settings id, so a retry, a second
student on the same mark scheme, or a restarted job re-preparing its input
reuses one validated artifact instead of compressing the same document again.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from ..config import data_dir
from ..logging_setup import log_event

log = logging.getLogger("engine.pdf_optimize")

#: Bump when the algorithm changes so old cached artifacts are not reused.
ALGORITHM_VERSION = "2"

#: Above 250 dpi an image carries detail a 200 dpi page render cannot use.
DPI_THRESHOLD = 250
DPI_TARGET = 200

#: JPEG quality per role. The mark scheme gets slightly more headroom because
#: its small mark codes and superscripts are the reference truth.
QUALITY = {"submission": 85, "mark_scheme": 88}

#: Only rewrite images when they plausibly hold this share of the file.
MIN_OVERSIZED_SHARE = 0.35
#: Trivial documents are not worth a rewrite pass.
MIN_BYTES_FOR_REWRITE = 200 * 1024

#: Native-resolution JPEG recompression. Phone-scanner PDFs often place a
#: 2355x3477 photo on a page of exactly 2355x3477 POINTS, so by placement they
#: claim 72 dpi and downsampling would destroy real detail — yet each page is
#: a ~1.6 MB JPEG that re-encodes to about half with unchanged pixel
#: dimensions (measured: 22.9 MB -> 11.8 MB, render diff 0.74/255, ink 1.0).
#: This pass therefore never resizes: it only re-encodes JPEG streams that
#: already dominate the file, and the normal integrity gate decides whether
#: the result is acceptable.
RECOMPRESS_MIN_IMAGE_BYTES = 200 * 1024
MIN_RECOMPRESS_SHARE = 0.35

#: Hard limits (memory/time safety). Beyond these only the lossless pass runs.
MAX_BYTES_FOR_REWRITE = 120 * 1024 * 1024
MAX_PAGES_FOR_REWRITE = 250
#: The lossless pass itself is skipped above this size.
MAX_BYTES_FOR_LOSSLESS = 400 * 1024 * 1024

#: A ``.partial`` candidate older than this is debris from a killed process:
#: it is unfinished, so it can never be served, and it is removed by the sweep.
PARTIAL_MAX_AGE_SECONDS = 3600

#: Render comparison: mean absolute difference (0-255) allowed per sampled
#: page, and the worst single page tolerated. Measured baseline for an accepted
#: scan rewrite is 0.0-0.4.
MAX_MEAN_PIXEL_DIFF = 6.0
MAX_PAGE_PIXEL_DIFF = 14.0
SAMPLE_PAGES = 3
SAMPLE_DPI = 96

#: Faint-ink retention. Mean pixel difference alone cannot see a faded page:
#: a nearly-white render of a lightly pencilled page differs by only a few grey
#: levels. So the mid-tone ink a page carries must survive at least this share.
#: Measured on accepted 300->200 dpi rewrites: 0.91-1.00 for a scanned paper
#: and scheme (blurring adds as many mid-tones as it removes). A page with fewer
#: than INK_MIN_SAMPLES counts is too sparse to judge and is skipped, not guessed.
MIN_INK_RATIO = 0.75
INK_THRESHOLD = 200
INK_SAMPLE_STEP = 3
INK_MIN_SAMPLES = 50

#: A saving below this is not worth a separate artifact (and its disk).
MIN_SAVING = 0.03

#: Cache housekeeping.
CACHE_MAX_BYTES = 2 * 1024 * 1024 * 1024
CACHE_MAX_AGE_SECONDS = 30 * 24 * 3600


@dataclass
class OptimizationReport:
    """What happened to one document — recorded, never guessed."""

    role: str
    source_name: str
    original_bytes: int = 0
    optimized_bytes: int = 0
    ratio: float | None = None
    duration_ms: int = 0
    pages_before: int = 0
    pages_after: int = 0
    integrity_ok: bool = False
    used: str = "original"          # "original" | "optimized" | "cached"
    skipped: str | None = None      # reason a step did not run
    lossy: bool = False
    cache_key: str | None = None
    detail: dict = field(default_factory=dict)

    @property
    def reduction_percent(self) -> float | None:
        """Bytes saved by the file that was CHOSEN, as a percentage.

        ``None`` when the original is in use (no compression happened), when
        no size was measured, or when the candidate was not smaller: a
        negative or meaningless percentage is never reported as a saving.
        """
        if self.used not in ("optimized", "cached"):
            return None
        if self.original_bytes <= 0 or self.optimized_bytes <= 0:
            return None
        saved = self.original_bytes - self.optimized_bytes
        if saved <= 0:
            return None
        return round(saved / self.original_bytes * 100, 1)

    def as_dict(self) -> dict:
        return {
            "role": self.role, "source_name": self.source_name,
            "original_bytes": self.original_bytes,
            "optimized_bytes": self.optimized_bytes,
            "ratio": self.ratio, "duration_ms": self.duration_ms,
            "pages_before": self.pages_before,
            "pages_after": self.pages_after,
            "integrity_ok": self.integrity_ok, "used": self.used,
            "skipped": self.skipped, "lossy": self.lossy,
            "cache_key": self.cache_key, **self.detail,
        }


@dataclass
class OptimizedPdf:
    """The path callers should use, plus the report explaining the choice."""

    path: str
    report: OptimizationReport

    @property
    def used_optimized(self) -> bool:
        return self.report.used in ("optimized", "cached")


# ---------------------------------------------------------------- cache paths

def cache_root() -> Path:
    return data_dir() / "tmp" / "pdf_cache"


def _settings(role: str, *, lossy: bool, recompress: bool = False) -> dict:
    return {
        "algorithm": ALGORITHM_VERSION, "role": role, "lossy": lossy,
        "recompress": recompress,
        "dpi_threshold": DPI_THRESHOLD, "dpi_target": DPI_TARGET,
        "quality": QUALITY.get(role, 85),
    }


def _cache_key(source_sha: str, settings: dict) -> str:
    blob = json.dumps(settings, sort_keys=True).encode()
    return f"{source_sha[:20]}-{hashlib.sha256(blob).hexdigest()[:10]}"


def _cache_paths(key: str) -> tuple[Path, Path]:
    folder = cache_root() / key[:2]
    return folder / f"{key}.pdf", folder / f"{key}.json"


# -------------------------------------------------------------- plan / checks

def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _stream_length(doc, xref: int) -> int:
    """Stored byte length of an xref's stream (0 when it cannot be read)."""
    try:
        kind, value = doc.xref_get_key(xref, "Length")
        if kind == "int" and value:
            return int(value)
    except Exception:  # noqa: BLE001
        pass
    return 0


def _page_images(page) -> dict[int, tuple[int, int, list]]:
    """``{xref: (width_px, height_px, [placement rect, ...])}``.

    Placement is needed because scanned pages are frequently inset (margins,
    two-up scans): judging by the page box alone under-states their DPI and
    skips documents that compress by half. This walk is only paid on a cache
    MISS — a hit is answered from the key alone.
    """
    out: dict[int, tuple[int, int, list]] = {}
    try:
        images = page.get_images(full=True)
    except Exception:  # noqa: BLE001 — malformed page: no claim made
        return out
    for entry in images:
        try:
            xref = int(entry[0])
            width = int(entry[2])
            height = int(entry[3])
        except (IndexError, TypeError, ValueError):
            continue
        if xref <= 0 or width <= 0 or height <= 0:
            continue
        if xref in out:
            continue
        try:
            rects = page.get_image_rects(xref)
        except Exception:  # noqa: BLE001
            rects = []
        out[xref] = (width, height, list(rects))
    return out


def _image_dpi(width: int, rects: list) -> float:
    """Highest resolution the image is displayed at (the safest to judge by)."""
    dpis = [width / max(1e-6, float(rect.width) / 72.0) for rect in rects]
    return max(dpis) if dpis else 0.0


def _image_shares(doc, threshold_dpi: int,
                  min_image_bytes: int) -> dict:
    """One walk over the document's images for every planning decision.

    Returns stored bytes/counts for (a) images displayed above
    ``threshold_dpi`` (downsample candidates) and (b) large JPEG streams that
    could be re-encoded at their existing size. Placement is expensive to
    query (PyMuPDF hashes the decoded image), so this is done ONCE instead of
    once per candidate technique.
    """
    oversized = 0
    oversized_count = 0
    recompress = 0
    recompress_count = 0
    seen: set[int] = set()
    for page in doc:
        for xref, (width, _height, rects) in _page_images(page).items():
            if xref in seen or not rects:
                continue
            seen.add(xref)
            stored = _stream_length(doc, xref)
            if stored <= 0:
                continue
            if _image_dpi(width, rects) >= threshold_dpi:
                oversized += stored
                oversized_count += 1
                continue
            try:
                kind, value = doc.xref_get_key(xref, "Filter")
            except Exception:  # noqa: BLE001
                continue
            if kind == "name" and "DCTDecode" in (value or "") \
                    and stored >= min_image_bytes:
                recompress += stored
                recompress_count += 1
    return {"oversized_bytes": oversized, "oversized_images": oversized_count,
            "recompressible_bytes": recompress,
            "recompressible_images": recompress_count}


def _page_geometry(doc) -> list[tuple[float, float, int]]:
    out = []
    for page in doc:
        rect = page.rect
        out.append((round(float(rect.width), 1), round(float(rect.height), 1),
                    int(page.rotation or 0) % 360))
    return out


def _text_pages(doc) -> list[str]:
    try:
        return [(page.get_text() or "") for page in doc]
    except Exception:  # noqa: BLE001 — no text layer to compare
        return []


def _render_sample(doc, pages: int) -> list[bytes] | None:
    import pymupdf
    shots = []
    for index in range(min(pages, doc.page_count)):
        try:
            pix = doc[index].get_pixmap(dpi=SAMPLE_DPI,
                                        colorspace=pymupdf.csGRAY)
            shots.append(pix.samples)
        except Exception:  # noqa: BLE001 — a page that will not render fails
            return None          # the integrity check, never "assume fine"
    return shots


def _ink_marks(samples_pages: list[bytes]) -> list[int]:
    """Mid-tone ink count for pages ALREADY rendered by the sample pass.

    Threshold 200 keeps pencil (≈92-140) and printed symbols while excluding
    white paper and pale rules, so a faded or blanked scan scores near zero.
    Reusing the rendered pages matters: a second render of every sampled page
    doubled verification time on large phone scans for no extra evidence.
    """
    return [sum(1 for pos in range(0, len(samples), INK_SAMPLE_STEP)
                if samples[pos] < INK_THRESHOLD)
            for samples in samples_pages]


def _mean_abs_diff(a: bytes, b: bytes) -> float:
    if not a or len(a) != len(b):
        return 999.0
    total = 0
    for index in range(0, len(a), 7):        # sparse sample: fast and adequate
        total += abs(a[index] - b[index])
    return total / max(1, len(range(0, len(a), 7)))


def verify_candidate(original_path: Path, candidate_path: Path) -> tuple[bool, dict]:
    """Independent integrity gate for an optimized copy.

    Checks that the candidate is the SAME document: same pages, same geometry
    and rotation, identical text layer, every sampled page renders, and the
    rendering has not changed beyond noise. A candidate that fails any check is
    rejected and the original is used.
    """
    import pymupdf

    detail: dict = {}
    try:
        src = pymupdf.open(str(original_path))
        out = pymupdf.open(str(candidate_path))
    except Exception as exc:  # noqa: BLE001
        return False, {"reason": f"could not be reopened ({type(exc).__name__})"}

    try:
        if out.page_count != src.page_count:
            return False, {"reason": "page count changed",
                           "pages_before": src.page_count,
                           "pages_after": out.page_count}
        if _page_geometry(out) != _page_geometry(src):
            return False, {"reason": "page size/rotation changed"}
        if src.page_count == 0:
            return False, {"reason": "document has no pages"}

        src_text = _text_pages(src)
        if any(src_text) and _text_pages(out) != src_text:
            return False, {"reason": "text layer changed"}

        left = _render_sample(src, SAMPLE_PAGES)
        right = _render_sample(out, SAMPLE_PAGES)
        if left is None or right is None:
            return False, {"reason": "a sampled page would not render"}
        if len(left) != len(right):
            return False, {"reason": "sample size mismatch"}
        worst = 0.0
        for a, b in zip(left, right):
            worst = max(worst, _mean_abs_diff(a, b))
            if worst > MAX_PAGE_PIXEL_DIFF:
                return False, {"reason": "rendering changed too much",
                               "page_pixel_diff": round(worst, 2)}
        detail["page_pixel_diff"] = round(worst, 2)

        # Ink retention: a candidate that fades or blanks the writing fails even
        # when its mean pixel difference is tiny.
        try:
            src_ink = _ink_marks(left)
            out_ink = _ink_marks(right)
        except Exception:  # noqa: BLE001
            return False, {"reason": "a sampled page would not render"}
        ratios = [after / before if before else 1.0
                  for before, after in zip(src_ink, out_ink)]
        detail["min_ink_ratio"] = round(min(ratios), 3) if ratios else None
        for page_no, (before, after) in enumerate(zip(src_ink, out_ink)):
            if before >= INK_MIN_SAMPLES and after < before * MIN_INK_RATIO:
                return False, {"reason": "marks/ink lost",
                               "ink_page": page_no + 1,
                               "ink_ratio": round(after / before, 3),
                               **detail}
        return True, detail
    finally:
        try:
            src.close()
            out.close()
        except Exception:  # noqa: BLE001
            pass


# ------------------------------------------------------------------ main entry

def optimize_pdf(path: str | Path, *, role: str = "submission",
                 allow_lossy: bool = True,
                 job_id: str | None = None) -> OptimizedPdf:
    """Return the path that should be used for processing + its report.

    Never raises: any failure returns the ORIGINAL path with ``skipped`` set,
    because a compression problem must never lose or block a paper.
    """
    source = Path(path)
    report = OptimizationReport(role=role, source_name=source.name)
    try:
        report.original_bytes = source.stat().st_size
    except OSError:
        report.skipped = "source missing"
        return OptimizedPdf(str(source), report)

    # A document this large is a memory/time hazard: hashing and rewriting it
    # would add minutes to the correction for a file the model may not even
    # accept. The original is uploaded unchanged, which is always safe.
    if report.original_bytes > MAX_BYTES_FOR_LOSSLESS:
        report.skipped = "larger than the safe optimization limit"
        return OptimizedPdf(str(source), report)

    lossy_planned = False
    recompress_planned = False
    try:
        import pymupdf
    except Exception:  # noqa: BLE001 — dependency missing: keep original
        report.skipped = "pdf library unavailable"
        return OptimizedPdf(str(source), report)

    started = time.monotonic()
    try:
        source_sha = _sha256_file(source)
    except OSError as exc:
        report.skipped = f"could not be read ({type(exc).__name__})"
        return OptimizedPdf(str(source), report)

    # FAST PATH: a cached artifact is found from the key alone, so a retry (or
    # a second student on the same mark scheme) never pays the planning walk.
    cache_modes = ((True, False), (False, True), (False, False)) \
        if allow_lossy else ((False, False),)
    for lossy_flag, recompress_flag in cache_modes:
        key = _cache_key(source_sha, _settings(role, lossy=lossy_flag,
                                               recompress=recompress_flag))
        cached_pdf, cached_json = _cache_paths(key)
        if not (cached_pdf.is_file() and cached_pdf.stat().st_size > 0):
            continue
        report.cache_key = key
        try:
            saved = json.loads(cached_json.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            saved = {}
        report.used = "cached"
        report.optimized_bytes = cached_pdf.stat().st_size
        report.ratio = _ratio(report.original_bytes, report.optimized_bytes)
        report.integrity_ok = bool(saved.get("integrity_ok", True))
        report.pages_before = int(saved.get("pages_before") or 0)
        report.pages_after = int(saved.get("pages_after") or 0)
        report.lossy = bool(saved.get("lossy", lossy_flag))
        report.detail.update({k: v for k, v in saved.items()
                              if k not in ("source_name", "role", "job_ids")})
        report.detail.setdefault("recompressed", bool(saved.get("recompressed")))
        if job_id:
            _note_job(cached_json, job_id)      # cleanup must know it was used
        report.duration_ms = int((time.monotonic() - started) * 1000)
        return OptimizedPdf(str(cached_pdf), report)

    try:
        with pymupdf.open(str(source)) as doc:
            if doc.needs_pass:
                report.skipped = "encrypted PDF"
                return OptimizedPdf(str(source), report)
            report.pages_before = doc.page_count
            if doc.page_count == 0:
                report.skipped = "no pages"
                return OptimizedPdf(str(source), report)
            if allow_lossy and report.original_bytes <= MAX_BYTES_FOR_REWRITE \
                    and doc.page_count <= MAX_PAGES_FOR_REWRITE \
                    and report.original_bytes >= MIN_BYTES_FOR_REWRITE:
                plan = _image_shares(doc, DPI_THRESHOLD,
                                     RECOMPRESS_MIN_IMAGE_BYTES)
                share = (plan["oversized_bytes"]
                         / max(1, report.original_bytes))
                report.detail.update({
                    "oversized_image_bytes": plan["oversized_bytes"],
                    "oversized_images": plan["oversized_images"],
                    "oversized_share": round(share, 3)})
                lossy_planned = share >= MIN_OVERSIZED_SHARE
                if not lossy_planned:
                    # No image is oversized relative to its placement. Before
                    # giving up, consider re-encoding large JPEGs WITHOUT
                    # resizing (phone-scanner pages, see the constants above).
                    jshare = (plan["recompressible_bytes"]
                              / max(1, report.original_bytes))
                    report.detail.update({
                        "recompressible_image_bytes": plan["recompressible_bytes"],
                        "recompressible_images": plan["recompressible_images"],
                        "recompressible_share": round(jshare, 3)})
                    recompress_planned = jshare >= MIN_RECOMPRESS_SHARE
            elif allow_lossy and (
                    report.original_bytes > MAX_BYTES_FOR_REWRITE
                    or doc.page_count > MAX_PAGES_FOR_REWRITE):
                report.skipped = "over the safe rewrite limits"
    except Exception as exc:  # noqa: BLE001 — malformed/unsupported input
        report.skipped = f"could not be read ({type(exc).__name__})"
        return OptimizedPdf(str(source), report)

    settings = _settings(role, lossy=lossy_planned,
                         recompress=recompress_planned)
    key = _cache_key(source_sha, settings)
    cached_pdf, cached_json = _cache_paths(key)
    report.cache_key = key

    try:
        result = _build_candidate(source, role=role, lossy=lossy_planned,
                                  recompress=recompress_planned)
    except Exception as exc:  # noqa: BLE001 — never block a correction
        report.skipped = f"optimization failed ({type(exc).__name__})"
        report.duration_ms = int((time.monotonic() - started) * 1000)
        _log(report, source)
        return OptimizedPdf(str(source), report)

    if result is None:
        report.skipped = report.skipped or "no size benefit"
        report.duration_ms = int((time.monotonic() - started) * 1000)
        _log(report, source)
        return OptimizedPdf(str(source), report)

    candidate_data, built = result
    candidate_bytes = len(candidate_data)
    # Re-encoding a JPEG is lossy too, even though the pixel dimensions are
    # unchanged: the report must say so.
    lossy_used = bool(built.get("lossy")) or bool(built.get("recompressed"))
    report.pages_after = report.pages_before
    report.lossy = lossy_used
    report.detail.update({k: v for k, v in built.items() if k != "lossy"})

    if candidate_bytes >= report.original_bytes:
        report.optimized_bytes = candidate_bytes
        report.skipped = "result was not smaller"
        report.duration_ms = int((time.monotonic() - started) * 1000)
        _log(report, source)
        return OptimizedPdf(str(source), report)
    if candidate_bytes > report.original_bytes * (1 - MIN_SAVING):
        report.optimized_bytes = candidate_bytes
        report.skipped = "no meaningful size benefit"
        report.duration_ms = int((time.monotonic() - started) * 1000)
        _log(report, source)
        return OptimizedPdf(str(source), report)

    cached_pdf.parent.mkdir(parents=True, exist_ok=True)
    # Unique per writer: two jobs on the same paper (for example two students
    # sharing one mark scheme) must never write over each other's candidate.
    # The atomic replace below then publishes whichever finished intact.
    tmp_path = cached_pdf.with_name(
        f"{cached_pdf.name}.{uuid.uuid4().hex[:8]}.partial")
    tmp_path.write_bytes(candidate_data)
    ok, check = verify_candidate(source, tmp_path)
    report.integrity_ok = ok
    report.detail.update(check)
    if not ok:
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass
        report.skipped = f"integrity check failed: {check.get('reason')}"
        report.optimized_bytes = candidate_bytes
        report.duration_ms = int((time.monotonic() - started) * 1000)
        _log(report, source)
        return OptimizedPdf(str(source), report)

    os.replace(tmp_path, cached_pdf)
    report.optimized_bytes = cached_pdf.stat().st_size
    report.ratio = _ratio(report.original_bytes, report.optimized_bytes)
    report.used = "optimized"
    report.duration_ms = int((time.monotonic() - started) * 1000)
    try:
        cached_json.write_text(
            json.dumps({**report.as_dict(), "created_at": time.time(),
                        "source_sha": source_sha, "job_ids": [job_id] if job_id else []},
                       indent=1),
            encoding="utf-8")
        if job_id:
            _note_job(cached_json, job_id)
    except OSError:
        pass
    _log(report, source)
    return OptimizedPdf(str(cached_pdf), report)


def _ratio(original: int, optimized: int) -> float | None:
    if original <= 0:
        return None
    return round(optimized / original, 4)


def _log(report: OptimizationReport, source: Path) -> None:
    """Record the outcome WITHOUT paths or document contents."""
    try:
        log_event(
            "engine.pdf_optimize", "pdf optimisation",
            role=report.role, original_bytes=report.original_bytes,
            optimized_bytes=report.optimized_bytes, ratio=report.ratio,
            duration_ms=report.duration_ms, pages=report.pages_before,
            integrity_ok=report.integrity_ok, used=report.used,
            lossy=report.lossy, skipped=report.skipped,
            oversized_share=report.detail.get("oversized_share"),
            page_pixel_diff=report.detail.get("page_pixel_diff"))
    except Exception:  # noqa: BLE001 — logging must never fail a job
        log.debug("pdf optimisation report not logged")


def _downsample_images(doc, role: str) -> int:
    """Re-encode oversized page images and return how many were replaced.

    PyMuPDF's bulk ``rewrite_images`` skips ICCBased photo scans (measured on a
    real 15-page handwritten paper: 297 dpi pages, 0% reduction), so the
    replacement is explicit: decode → LANCZOS resize to the target DPI for the
    LARGEST placement → JPEG at the role's quality → replace. Images with an
    alpha mask, image masks, tiny images, and absurd pixel counts are left
    untouched: a mask is not a photo and thumbnails carry no paper detail.
    """
    import io as _io

    from PIL import Image

    quality = QUALITY.get(role, 85)
    replaced: list[int] = []
    for page in doc:
        for xref, (width, height, rects) in _page_images(page).items():
            if xref in replaced:
                continue                      # same xref, already replaced
            if not rects:
                continue
            info = None
            try:
                info = doc.extract_image(xref)
            except Exception:  # noqa: BLE001
                continue
            if not info or info.get("smask"):
                continue
            if width < 64 or height < 64:
                continue
            if width * height > 40_000_000:
                continue
            if _image_dpi(width, rects) < DPI_THRESHOLD:
                continue
            # Size for the LARGEST placement: the least aggressive choice, so
            # a smaller placement can never be downsampled below its need.
            target_inches = max(float(r.width) for r in rects) / 72.0
            target_px = int(max(1, round(target_inches * DPI_TARGET)))
            if target_px >= width:
                continue
            try:
                with Image.open(_io.BytesIO(info["image"])) as img:
                    img.load()
                    scale = target_px / float(width)
                    size = (target_px, max(1, int(round(height * scale))))
                    resized = img.resize(size, Image.LANCZOS)
                    if resized.mode not in ("RGB", "L"):
                        resized = resized.convert("RGB")
                    buffer = _io.BytesIO()
                    resized.save(buffer, "JPEG", quality=quality,
                                 optimize=True)
            except Exception:  # noqa: BLE001 — leave an unreadable image alone
                continue
            try:
                page.replace_image(xref, stream=buffer.getvalue())
                replaced.append(xref)
            except Exception:  # noqa: BLE001
                continue
    return len(replaced)


def _recompress_jpeg_images(doc, role: str) -> int:
    """Re-encode large JPEG streams at their EXISTING pixel size.

    No resize, no crop, no colour change beyond what JPEG requires: the page
    still holds exactly the same number of pixels, so fine handwriting is not
    thrown away. A stream is only replaced when the new bytes are at least 10%
    smaller; the caller's integrity gate then compares the rendered pages and
    the retained ink before the candidate may be used.
    """
    import io as _io

    from PIL import Image

    quality = QUALITY.get(role, 85)
    replaced: list[int] = []
    for page in doc:
        # No placement query here: this path never resizes, so the image's
        # on-page rectangle is irrelevant. get_image_rects is expensive (it
        # hashes the decoded image) and was the largest single cost on scans.
        for entry in page.get_images(full=True):
            xref = int(entry[0])
            width = int(entry[2])
            height = int(entry[3])
            if xref in replaced:
                continue
            if width < 64 or height < 64 or width * height > 40_000_000:
                continue
            try:
                info = doc.extract_image(xref)
            except Exception:  # noqa: BLE001
                continue
            if not info or info.get("smask") or info.get("ext") != "jpeg":
                continue
            original = info.get("image") or b""
            if len(original) < RECOMPRESS_MIN_IMAGE_BYTES:
                continue
            try:
                with Image.open(_io.BytesIO(original)) as img:
                    img.load()
                    if img.mode not in ("RGB", "L"):
                        img = img.convert("RGB")
                    buffer = _io.BytesIO()
                    img.save(buffer, "JPEG", quality=quality, optimize=True)
            except Exception:  # noqa: BLE001 — leave an unreadable image alone
                continue
            if len(buffer.getvalue()) >= len(original) * 0.9:
                continue
            try:
                page.replace_image(xref, stream=buffer.getvalue())
                replaced.append(xref)
            except Exception:  # noqa: BLE001
                continue
    return len(replaced)


def _build_candidate(source: Path, *, role: str,
                     lossy: bool,
                     recompress: bool = False) -> tuple[bytes, dict] | None:
    """Produce candidate bytes; ``None`` when nothing was worth keeping."""
    import pymupdf

    started = time.monotonic()
    detail: dict = {}
    doc = pymupdf.open(str(source))
    try:
        if lossy:
            detail["images_rewritten"] = _downsample_images(doc, role)
            if not detail["images_rewritten"]:
                lossy = False              # nothing oversized was found
        if not lossy and recompress:
            detail["images_recompressed"] = _recompress_jpeg_images(doc, role)
            if not detail["images_recompressed"]:
                recompress = False         # nothing worth re-encoding
        buffer = doc.tobytes(garbage=4, deflate=True, clean=True,
                             use_objstms=1)
    finally:
        doc.close()
    elapsed = time.monotonic() - started
    detail["build_ms"] = int(elapsed * 1000)
    if elapsed > 300:  # wall-clock guard: absurdly slow is a failure signal
        log.warning("pdf optimisation took %.1fs", elapsed)
    return buffer, {"lossy": lossy, "recompressed": recompress, **detail}


def _note_job(sidecar: Path, job_id: str) -> None:
    """Record that this artifact was used by ``job_id`` (for cleanup)."""
    try:
        data = json.loads(sidecar.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    ids = list(data.get("job_ids") or [])
    if job_id not in ids:
        ids.append(job_id)
    data["job_ids"] = ids
    try:
        sidecar.write_text(json.dumps(data, indent=1), encoding="utf-8")
    except OSError:
        pass


def release_job(job_id: str) -> int:
    """Delete this job's SUBMISSION artifacts (personal data), keeping schemes.

    Called at terminal cleanup, so a compressed copy of a student's paper does
    not outlive the job in the cache. Mark-scheme artifacts are shared
    reference documents and stay until the cache's own age/size sweep.
    """
    root = cache_root()
    if not root.is_dir() or not job_id:
        return 0
    removed = 0
    for sidecar in root.glob("*/*.json"):
        try:
            data = json.loads(sidecar.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if data.get("role") != "submission":
            continue
        if job_id not in (data.get("job_ids") or []):
            continue
        for path in (sidecar.with_suffix(".pdf"), sidecar,
                     sidecar.with_suffix(".partial")):
            try:
                path.unlink(missing_ok=True)
            except OSError:
                continue
        removed += 1
    if removed:
        log_event("engine.pdf_optimize", "job submission copies released",
                  job_id=job_id, removed=removed)
    return removed


# -------------------------------------------------------------- housekeeping

def cache_stats() -> dict:
    """How much the cache holds and how much it saved (for diagnostics)."""
    root = cache_root()
    files = list(root.glob("*/*.pdf")) if root.is_dir() else []
    total = 0
    saved = 0
    entries = 0
    for path in files:
        try:
            total += path.stat().st_size
            sidecar = path.with_suffix(".json")
            if sidecar.is_file():
                data = json.loads(sidecar.read_text(encoding="utf-8"))
                saved += max(0, int(data.get("original_bytes") or 0)
                             - int(data.get("optimized_bytes") or 0))
                entries += 1
        except (OSError, ValueError):
            continue
    return {"entries": entries, "cached_bytes": total, "bytes_saved": saved,
            "max_bytes": CACHE_MAX_BYTES}


def sweep_cache(max_bytes: int | None = None,
                max_age_seconds: int = CACHE_MAX_AGE_SECONDS) -> int:
    """Drop stale cache entries, then oldest-first until under the cap."""
    root = cache_root()
    if not root.is_dir():
        return 0
    now = time.time()
    removed = 0
    entries: list[tuple[float, int, Path]] = []
    for path in root.glob("*/*"):
        try:
            stat = path.stat()
        except OSError:
            continue
        if not path.name.endswith((".pdf", ".json", ".partial")):
            continue
        # An unfinished candidate is never served, so a stale one is debris
        # from an interrupted run; do not keep it for the normal cache age.
        if path.name.endswith(".partial") \
                and now - stat.st_mtime > PARTIAL_MAX_AGE_SECONDS:
            try:
                path.unlink()
                removed += 1
            except OSError:
                pass
            continue
        if now - stat.st_mtime > max_age_seconds:
            try:
                path.unlink()
                removed += 1
            except OSError:
                pass
            continue
        entries.append((stat.st_mtime, stat.st_size, path))
    cap = CACHE_MAX_BYTES if max_bytes is None else max_bytes
    total = sum(size for _m, size, p in entries if p.name.endswith(".pdf"))
    if total > cap:
        for _mtime, size, path in sorted(entries):
            if total <= cap:
                break
            if not path.name.endswith(".pdf"):
                continue
            try:
                path.unlink()
                path.with_suffix(".json").unlink(missing_ok=True)
                total -= size
                removed += 1
            except OSError:
                continue
    # Remove folders left empty.
    for folder in root.glob("*"):
        try:
            if folder.is_dir() and not any(folder.iterdir()):
                shutil.rmtree(folder, ignore_errors=True)
        except OSError:
            continue
    if removed:
        log_event("engine.pdf_optimize", "pdf cache swept", removed=removed)
    return removed
