"""Extract the real C1L13 - Sets mark scheme structure (ground truth attempt)."""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MS = ROOT / "data" / "mark_schemes" / "889601795010.pdf"

print("mark scheme:", MS, "exists", MS.exists(),
      "bytes", MS.stat().st_size if MS.exists() else 0)

from pypdf import PdfReader  # noqa: E402

reader = PdfReader(str(MS))
print("pages:", len(reader.pages))

text_pages = []
chars = 0
for i, page in enumerate(reader.pages):
    try:
        t = page.extract_text() or ""
    except Exception as exc:  # noqa: BLE001
        t = ""
        print("  page", i + 1, "extract failed:", exc)
    chars += len(t.strip())
    text_pages.append(t)

print("total extractable characters:", chars)

if chars < 200:
    print("\n--> mark scheme has essentially NO text layer (scanned images)")
    # report size of images / page dims
    p0 = reader.pages[0]
    print("page 1 mediabox:", p0.mediabox)
    try:
        res = p0.get("/Resources") or {}
        xobj = res.get("/XObject")
        if xobj:
            xobj = xobj.get_object()
            for name in list(xobj.keys())[:5]:
                o = xobj[name].get_object()
                print("   xobject", name, o.get("/Subtype"),
                      o.get("/Width"), o.get("/Height"))
    except Exception as exc:  # noqa: BLE001
        print("  resource probe failed:", exc)
else:
    all_text = "\n".join(text_pages)
    (ROOT / "data" / "audit_markscheme.txt").write_text(all_text, encoding="utf-8")
    print("wrote data/audit_markscheme.txt")
    nums = re.findall(r"\b(?:Q(?:uestion)?\s*)?(\d{1,2})\s*[\.\)]?\s*(\[\d+\]|\d+\s*marks?)",
                      all_text, re.I)
    print("sample question/mark hits:", nums[:40])
