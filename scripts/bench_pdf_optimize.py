"""Benchmark the shared PDF optimizer on representative maths documents.

Measures, for a STUDENT submission and a MARK SCHEME separately:

* original and optimized size (bytes) and the reduction;
* compression duration (cold = first run, warm = cached retry);
* page count before/after, integrity result, pixels changed;
* text-extraction and page-rendering time for the copy that would be used;
* the Large PDF batch plan for the original vs the optimized copy (page count,
  batch count, scheme questions) so question coverage can be compared.

The documents are synthetic but representative: 300 dpi photo-like scans with
faint short strokes, dots and tiny marks (handwriting/mark codes), and digitally
generated text with mathematical symbols. No AI keys are needed; every number
below is measured locally.

Run:  python scripts/bench_pdf_optimize.py [--out docs/evidence/pdf-compression.json]
"""
from __future__ import annotations

import argparse
import io
import json
import os
import random
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

# Keep the benchmark out of the real installation: the optimizer caches under
# data_dir()/tmp/pdf_cache, so point it at scratch space BEFORE app.config loads.
_WORK = Path(tempfile.mkdtemp(prefix="mc5_pdf_bench_"))
os.environ["MC5_DATA_DIR"] = str(_WORK / "data")


# ------------------------------------------------------------------ documents

def _scan(path: Path, *, pages: int, dpi: int, scheme: bool,
          quality: int = 92) -> Path:
    """A photo-like page: near-white paper, faint pencil strokes, tiny marks."""
    import pymupdf
    from PIL import Image, ImageDraw

    rng = random.Random(20261021 if scheme else 424242)
    width_in, height_in = 8.27, 11.69
    px_w, px_h = int(width_in * dpi), int(height_in * dpi)
    doc = pymupdf.open()
    for page_no in range(pages):
        image = Image.new("L", (px_w, px_h), 250)
        draw = ImageDraw.Draw(image)
        # Ruled answer lines, like a real exam booklet.
        for row in range(14):
            y = int(px_h * 0.12) + row * int(px_h * 0.055)
            draw.line((int(px_w * 0.08), y, int(px_w * 0.92), y), fill=214)
        # "Handwriting": short wobbly strokes in faint pencil (100-140 grey).
        for _ in range(340 if not scheme else 260):
            x = rng.randrange(int(px_w * 0.08), int(px_w * 0.88))
            y = rng.randrange(int(px_h * 0.12), int(px_h * 0.94))
            length = rng.randrange(12, 44)
            tone = rng.choice((92, 110, 128, 140))
            draw.line((x, y, x + length, y + rng.randrange(-6, 7)),
                      fill=tone, width=2 if dpi >= 250 else 1)
            if rng.random() < 0.35:            # decimal points / dots on i's
                draw.ellipse((x + length + 6, y, x + length + 9, y + 3),
                             fill=tone)
        label = "M1  A1  B1   2/3   x^2" if scheme else "Q3   x^2 - 3x = -2"
        draw.text((int(px_w * 0.08), int(px_h * 0.05)), label, fill=40)
        draw.text((int(px_w * 0.08), int(px_h * 0.085)),
                  "1.75   -1/2   sqrt(9)=3   dy/dx", fill=70)
        buffer = io.BytesIO()
        image.convert("RGB").save(buffer, "JPEG", quality=quality)
        page = doc.new_page(width=width_in * 72, height=height_in * 72)
        page.insert_image(page.rect, stream=buffer.getvalue())
    data = doc.tobytes(garbage=1)
    doc.close()
    path.write_bytes(data)
    return path


def _text_pdf(path: Path, *, pages: int, scheme: bool) -> Path:
    """A digitally generated paper: real text layer, no scans."""
    import pymupdf

    doc = pymupdf.open()
    for index in range(pages):
        page = doc.new_page()
        # A leading question number on the page is what the deterministic
        # planner reads to map questions -> pages; the same shape as a real
        # paper, so the coverage comparison is meaningful.
        if scheme:
            lines = [f"{index + 1} (a)  x = 1   M1 (2 marks)",
                     "       x = 2   A1  B1 correct substitution",
                     "Allow ±0.01; cao. Alternative: completing the square."]
        else:
            lines = [f"{index + 1}  solve x\u00b2 \u2212 3x + 2 = 0",
                     "Working: (x \u2212 1)(x \u2212 2) = 0",
                     "Answer: x = 1 or x = 2"]
        for offset, line in enumerate(lines):
            page.insert_text((72, 90 + offset * 26), line, fontsize=13)
        page.insert_text((72, 220), "\u222b\u2080\u00b9 2x dx = 1   "
                                    "\u221a16 = 4   \u00bd + \u00bc = \u00be",
                         fontsize=12)
    data = doc.tobytes()
    doc.close()
    path.write_bytes(data)
    return path


# ------------------------------------------------------------------ measuring

def _timed(fn):
    start = time.perf_counter()
    value = fn()
    return value, (time.perf_counter() - start) * 1000.0


def _text_ms(path: str) -> float:
    import pymupdf
    def run():
        with pymupdf.open(path) as doc:
            return [page.get_text() for page in doc]
    return round(_timed(run)[1], 1)


def _render_ms(path: str, dpi: int = 150) -> float:
    import pymupdf

    def run():
        with pymupdf.open(path) as doc:
            for page in doc:
                page.get_pixmap(dpi=dpi, colorspace=pymupdf.csGRAY)
    return round(_timed(run)[1], 1)


def _text_pages(path: str) -> list[str]:
    import pymupdf
    with pymupdf.open(path) as doc:
        return [page.get_text() for page in doc]


def _ink(path: str) -> int:
    """Mid-tone pixel count of page 1 at 120 dpi: did any ink disappear?

    Threshold 200 keeps faint pencil (92-140) and printed symbols while
    excluding the 214-grey answer rules; the strokes are thin, so sampling
    every 5th byte keeps the count large enough to be meaningful.
    """
    import pymupdf
    with pymupdf.open(path) as doc:
        samples = doc[0].get_pixmap(dpi=120,
                                    colorspace=pymupdf.csGRAY).samples
    return sum(1 for value in samples[::5] if value < 200)


def _plan(student: str, scheme: str) -> dict:
    """The deterministic Large PDF plan + the question evidence it used."""
    from app.engine import pdf_inspect
    from app.large_pdf import planner

    prepared = planner.prepare(student, scheme)
    inspection = pdf_inspect.inspect_student_pdf(student)
    return {"pages": prepared.page_count,
            "batches": prepared.batch_count,
            "scheme_questions": len(prepared.scheme_questions),
            "questions_detected": len(inspection.question_pages),
            "question_pages": sum(len(v) for v in
                                  inspection.question_pages.values()),
            "batch_sizes": ([b.pages for b in prepared.plan.batches]
                            if prepared.plan else []),
            "blocked": prepared.blocked}


def _measure(label: str, role: str, path: Path) -> dict:
    from app.engine import pdf_optimize as po

    cold = po.optimize_pdf(path, role=role, job_id=f"bench-{label}")
    warm = po.optimize_pdf(path, role=role, job_id=f"bench-{label}")
    used = cold.path
    original_text = _text_pages(str(path))
    row = {
        "document": label,
        "role": role,
        "pages": cold.report.pages_before,
        "original_bytes": cold.report.original_bytes,
        "optimized_bytes": cold.report.optimized_bytes or cold.report.original_bytes,
        "used": cold.report.used,
        "ratio": cold.report.ratio,
        "lossy": cold.report.lossy,
        "integrity_ok": cold.report.integrity_ok,
        "pixel_diff": cold.report.detail.get("page_pixel_diff"),
        "min_ink_ratio": cold.report.detail.get("min_ink_ratio"),
        "cold_ms": cold.report.duration_ms,
        "warm_ms": warm.report.duration_ms,
        "warm_used": warm.report.used,
        "skipped": cold.report.skipped,
        "pages_after": cold.report.pages_after,
        "text_layer_identical": _text_pages(used) == original_text,
        "text_ms_original": _text_ms(str(path)),
        "text_ms_optimized": _text_ms(used),
        "render_ms_original": _render_ms(str(path)),
        "render_ms_optimized": _render_ms(used),
        "ink_original": _ink(str(path)),
        "ink_optimized": _ink(used),
    }
    original_bytes = row["original_bytes"]
    row["saving_pct"] = (round(100.0 * (original_bytes - row["optimized_bytes"])
                               / original_bytes, 1) if original_bytes else 0.0)
    assert row["text_layer_identical"], f"{label}: text layer changed!"
    assert row["pages"] == row["pages_after"], f"{label}: page count changed!"
    return row


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", help="write the raw measurements as JSON")
    args = parser.parse_args()

    import app.config as config

    cfg = config.get_config()
    cfg.mock_mode = True

    docs = {
        "student scanned (300 dpi)": _scan(_WORK / "student_scan.pdf", pages=6,
                                           dpi=300, scheme=False),
        "student digital (text)": _text_pdf(_WORK / "student_text.pdf", pages=6,
                                            scheme=False),
        "student scan (150 dpi, already lean)": _scan(
            _WORK / "student_lean.pdf", pages=2, dpi=150, scheme=False,
            quality=80),
        "mark scheme scanned (300 dpi)": _scan(_WORK / "scheme_scan.pdf",
                                               pages=3, dpi=300, scheme=True),
        "mark scheme digital (text)": _text_pdf(_WORK / "scheme_text.pdf",
                                                pages=3, scheme=True),
        "mark scheme scan (150 dpi, already lean)": _scan(
            _WORK / "scheme_lean.pdf", pages=2, dpi=150, scheme=True,
            quality=80),
    }

    rows = []
    for label, path in docs.items():
        role = "mark_scheme" if label.startswith("mark scheme") else "submission"
        row = _measure(label, role, path)
        row["plan"] = _plan(str(path), str(docs["mark scheme digital (text)"]))
        rows.append(row)
        print(f"measured {label}: {row['original_bytes']} -> "
              f"{row['optimized_bytes']} ({row['used']})")

    # Coverage: a 60-page paper (so the plan really batches) marked against a
    # scheme with question ids — original plan vs optimized-copy plan.
    from app.engine import pdf_optimize as po
    long_student = _text_pdf(_WORK / "student_long.pdf", pages=60,
                             scheme=False)
    long_scheme = _text_pdf(_WORK / "scheme_long.pdf", pages=30, scheme=True)
    student_copy = po.optimize_pdf(long_student, role="submission").path
    scheme_copy = po.optimize_pdf(long_scheme, role="mark_scheme").path
    coverage = {
        "original": _plan(str(long_student), str(long_scheme)),
        "optimized": _plan(student_copy, scheme_copy),
        "bytes": {"original": long_student.stat().st_size,
                  "optimized": Path(student_copy).stat().st_size,
                  "scheme_original": long_scheme.stat().st_size,
                  "scheme_optimized": Path(scheme_copy).stat().st_size},
    }
    coverage["identical"] = coverage["original"] == coverage["optimized"]

    print()
    header = ("| document | pages | original | optimized | saved | used | "
              "cold ms | warm ms | integrity | pixel diff |")
    print(header)
    print("|---|---:|---:|---:|---:|---|---:|---:|---|---:|")
    for row in rows:
        print(f"| {row['document']} | {row['pages']} | "
              f"{row['original_bytes'] / 1024:.0f} KiB | "
              f"{row['optimized_bytes'] / 1024:.0f} KiB | "
              f"{row['saving_pct']:.1f}% | {row['used']} | "
              f"{row['cold_ms']} | {row['warm_ms']} | "
              f"{'yes' if row['integrity_ok'] else 'n/a'} | "
              f"{row['pixel_diff'] if row['pixel_diff'] is not None else '-'} |")
    print()
    for role in ("submission", "mark_scheme"):
        group = [r for r in rows if r["role"] == role]
        total_in = sum(r["original_bytes"] for r in group)
        total_out = sum(r["optimized_bytes"] for r in group)
        print(f"{role}: {total_in / 1024:.0f} KiB -> {total_out / 1024:.0f} KiB "
              f"({100.0 * (total_in - total_out) / total_in:.1f}% saved, "
              f"{sum(r['cold_ms'] for r in group)} ms total cold)")
    print(f"large-pdf plan identical on optimized copy: "
          f"{coverage['identical']} ({coverage['optimized']})")
    print()
    print("| document | text layer | text ms (orig/opt) | render ms (orig/opt) "
          "| ink (orig/opt) |")
    print("|---|---|---:|---:|---:|")
    for row in rows:
        print(f"| {row['document']} | "
              f"{'identical' if row['text_layer_identical'] else 'CHANGED'} | "
              f"{row['text_ms_original']}/{row['text_ms_optimized']} | "
              f"{row['render_ms_original']}/{row['render_ms_optimized']} | "
              f"{row['ink_original']}/{row['ink_optimized']} "
              f"({100.0 * (row['ink_optimized'] - row['ink_original'])
                 / max(1, row['ink_original']):+.1f}%) |")

    payload = {"measured_at": time.strftime("%Y-%m-%d %H:%M:%S"),
               "python": sys.version.split()[0], "rows": rows,
               "coverage": coverage}
    if args.out:
        target = Path(args.out)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(payload, indent=1), encoding="utf-8")
        print(f"\nwrote {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
