"""Time the deterministic part of the Large PDF pipeline (no AI).

Builds a synthetic 50-page student PDF + 30-page mark scheme, then times
inspection, planning, batch splitting and merge. Run with backend/ on sys.path.
"""
from __future__ import annotations

import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

import app.config as config  # noqa: E402


def _pdf(path: Path, pages: int, with_text: bool) -> str:
    from pypdf import PdfWriter
    writer = PdfWriter()
    for i in range(pages):
        writer.add_blank_page(width=595, height=842)
    with open(path, "wb") as handle:
        writer.write(handle)
    return str(path)


def main() -> None:
    cfg = config.get_config()
    cfg.mock_mode = True
    tmp = Path(tempfile.mkdtemp())
    student = _pdf(tmp / "student.pdf", 50, True)
    scheme = _pdf(tmp / "scheme.pdf", 30, True)

    from app.engine import batching, pdf_inspect
    from app.large_pdf import planner

    def timed(label, fn):
        start = time.perf_counter()
        value = fn()
        print(f"{label:<38} {time.perf_counter() - start:7.3f}s")
        return value

    timed("scheme_evidence (30-page scheme)",
          lambda: pdf_inspect.scheme_evidence(scheme))
    timed("inspect_student_pdf (50 pages)",
          lambda: pdf_inspect.inspect_student_pdf(student, None))
    prepared = timed("planner.prepare (full)",
                     lambda: planner.prepare(student, scheme))
    print("   pages:", prepared.page_count, "batches:", prepared.batch_count)
    ranges = [(b.start, b.end) for b in prepared.plan.batches]
    print("   ranges:", ranges)
    timed("pdf_inspect.split_pdf (all batches)",
          lambda: pdf_inspect.split_pdf(student, ranges, tmp / "split",
                                        prefix="bench"))


if __name__ == "__main__":
    main()
