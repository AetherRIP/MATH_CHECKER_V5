"""Diagnostic: what does a mark scheme PDF actually look like structurally?

Read-only. Prints page count, block geometry, detected columns, and how the
naive "extract text per page" order compares against coordinate-sorted reading
order. No AI call, no student data, no key material.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))

TARGET = ROOT / "data" / "mark_schemes" / "873785733817.pdf"


def probe() -> int:
    if not TARGET.exists():
        print(f"[probe] not found: {TARGET}")
        return 1
    import pymupdf

    doc = pymupdf.open(str(TARGET))
    print(f"[probe] pages={doc.page_count}")
    two_col_pages = 0
    for pno in range(min(doc.page_count, 8)):
        page = doc[pno]
        w = page.rect.width
        blocks = [b for b in page.get_text("blocks") if b[4].strip()]
        # x-centre of each block tells us which column it sits in.
        centres = [(b[0] + b[2]) / 2 for b in blocks]
        left = [c for c in centres if c < w / 2 - 5]
        right = [c for c in centres if c > w / 2 + 5]
        multicol = bool(left and right)
        two_col_pages += multicol
        print(f"\n--- page {pno + 1}: width={w:.0f} blocks={len(blocks)} "
              f"two_column={multicol} (left={len(left)} right={len(right)})")
        for b in blocks[:6]:
            x0, y0, x1, y1, text = b[0], b[1], b[2], b[3], b[4]
            head = " ".join(text.split())[:70]
            print(f"    box=({x0:6.1f},{y0:6.1f})-({x1:6.1f},{y1:6.1f}) | {head}")
        # Naive order vs reading order (y then x) for the first few blocks.
        naive = [b[4].split()[0] for b in blocks[:8] if b[4].split()]
        reading = [b[4].split()[0]
                   for b in sorted(blocks, key=lambda b: (round(b[1] / 8), b[0]))[:8]
                   if b[4].split()]
        if naive or reading:
            print(f"    naive-first-tokens  : {naive}")
            print(f"    reading-order-tokens: {reading}")
        try:
            tables = page.find_tables()
            if tables.tables:
                print(f"    tables detected: {len(tables.tables)}")
        except Exception:
            pass
    print(f"\n[probe] two-column pages among first 8: {two_col_pages}")
    return 0


if __name__ == "__main__":
    raise SystemExit(probe())