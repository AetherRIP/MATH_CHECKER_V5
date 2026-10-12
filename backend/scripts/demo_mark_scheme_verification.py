"""Demonstrate the reconstructed mark-scheme verification end to end.

Stage A (local document reconstruction) runs for real on generated PDFs that
reproduce the hard cases. Stage B is then driven with a simulated model
response, so the canonical structure, the independently calculated total and
every cross-check can be inspected without spending an API call.

Read-only: writes nothing outside a temporary directory.
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))
sys.path.insert(0, str(ROOT / "backend" / "tests"))

from fixtures_mark_scheme import (  # noqa: E402
    image_only_mark_scheme, page_break_mark_scheme, page_numbers_mark_scheme,
    table_mark_scheme, two_column_mark_scheme,
)
from app.engine import mark_scheme as ms  # noqa: E402
from app.engine import mark_scheme_verify as msv  # noqa: E402


def show(name: str, path: Path, parsed: dict) -> None:
    evidence = ms.analyse_document(str(path))
    diag = evidence["diagnostics"]
    print(f"\n{'=' * 68}\n{name}")
    print(f"  mode={evidence['mode']}  pages={diag['pages']}  "
          f"blocks={diag['blocks']}  two-column pages={diag['two_column_pages']}  "
          f"printed total={evidence['printed_total']}")
    print(f"  local question candidates: {diag['candidate_numbers']}")
    print(f"  page-number candidates  : {diag['page_number_candidates']}")
    unresolved = [c for c in evidence["candidates"] if c["type"] == ms.CAND_OTHER]
    print(f"  unresolved (surfaced, not guessed): {len(unresolved)}")

    structure = msv.canonicalize(parsed)
    if structure is None:
        print("  -> model returned nothing usable; existing structure preserved")
        return
    result = msv.cross_validate(structure, evidence)

    numbers = [q["question_number"] for q in structure["questions"]]
    print(f"  canonical structure ({len(numbers)} questions): {numbers}")
    print(f"  canonical order correct: {numbers == ms.sort_numbers(numbers)}")
    print(f"  calculated total = {structure['calculated_total']}  "
          f"(printed={structure['printed_total']}, "
          f"model stated={structure['model_stated_total']})")
    print(f"  structural confidence = {result['structural_confidence']}  "
          f"verified={result['verified']}")
    for anomaly in result["anomalies"]:
        print(f"    ! {anomaly}")


def main() -> int:
    tmp = Path(tempfile.mkdtemp())

    # A two-column scheme: naive extraction interleaves 1, 7, 2, 8 ...
    show("TWO-COLUMN MARK SCHEME", two_column_mark_scheme(tmp / "tc.pdf"), {
        "questions": [{"question_number": str(n), "max_marks": 5,
                       "criteria": "B1 correct answer"} for n in range(1, 13)],
        "printed_total": 60, "total_marks": 60, "confidence": 0.96,
    })

    # Page furniture must not become questions.
    show("PAGE-NUMBERED MARK SCHEME", page_numbers_mark_scheme(tmp / "pn.pdf"), {
        "questions": [{"question_number": str(n), "max_marks": 4} for n in (1, 2, 3)],
        "printed_total": 12, "total_marks": 12, "confidence": 0.93,
    })

    # A table full of numbers, plus a stated total that does not add up.
    show("TABLE MARK SCHEME (stated total disagrees)", table_mark_scheme(tmp / "tb.pdf"), {
        "questions": [{"question_number": str(n), "max_marks": m}
                      for n, m in ((1, 3), (2, 5), (3, 2), (4, 6), (5, 4))],
        "printed_total": 25, "total_marks": 20, "confidence": 0.9,
    })

    # One question spanning a page break.
    show("PAGE-BREAK MARK SCHEME", page_break_mark_scheme(tmp / "pb.pdf"), {
        "questions": [
            {"question_number": "5", "max_marks": 5,
             "parts": [{"number": "a", "max_marks": 3}, {"number": "b", "max_marks": 2}]},
            {"question_number": "6", "max_marks": 4},
        ],
        "printed_total": 9, "total_marks": 9, "confidence": 0.95,
    })

    # Model returns the questions shuffled and duplicates one.
    show("OUT-OF-ORDER + DUPLICATE MODEL OUTPUT", two_column_mark_scheme(tmp / "tc2.pdf"), {
        "questions": [{"question_number": str(n), "max_marks": 5}
                      for n in (11, 3, 2, 10, 1, 9, 2, 4, 8, 5, 7, 6, 12)],
        "printed_total": 60, "total_marks": 60, "confidence": 0.88,
    })

    # A worse re-read must not displace a verified 33-question structure.
    good = {"questions": [{"question_number": str(n), "max_marks": 3}
                          for n in range(1, 34)], "calculated_total": 99}
    bad = msv.canonicalize({"questions": [
        {"question_number": str(n), "max_marks": 3} for n in range(1, 23)],
        "confidence": 0.3})
    action, detail = msv.prefer(good, bad, {"structural_confidence": 1.0,
                                             "anomalies": [], "evidence_agrees": True},
                                {"structural_confidence": 0.3,
                                 "anomalies": ["..."], "evidence_agrees": False})
    print(f"\n{'=' * 68}\nPROTECTION: existing 33 questions vs new 22")
    print(f"  action = {action}\n  {detail['reason']}")

    # An image-only PDF must be described honestly.
    show("IMAGE-ONLY MARK SCHEME (no text layer)", image_only_mark_scheme(tmp / "io.pdf"), {
        "questions": [{"question_number": str(n), "max_marks": 3}
                      for n in range(1, 13)],
        "printed_total": 36, "total_marks": 36, "confidence": 0.8,
    })
    return 0


if __name__ == "__main__":
    raise SystemExit(main())