"""Render a PDF to PNG pages and a self-contained HTML preview.

Design review aid, not a test:

    python scripts/pdf_preview.py some.pdf out_dir
"""

from __future__ import annotations

import base64
import pathlib
import sys

HTML = """<!doctype html><html><head><meta charset="utf-8"/>
<title>%(title)s</title>
<style>body{margin:0;background:#4a5568;font-family:system-ui,sans-serif}
.wrap{display:flex;gap:14px;padding:14px;align-items:flex-start;flex-wrap:wrap}
figure{margin:0;background:#fff;padding:6px;border-radius:3px;
box-shadow:0 2px 10px rgba(0,0,0,.35)}
img{display:block;width:%(width)dpx;height:auto}
figcaption{font-size:11px;color:#333;padding-top:5px}</style>
</head><body><div class="wrap">%(figures)s</div></body></html>"""


def main(argv: list[str]) -> int:
    import pymupdf

    src = pathlib.Path(argv[1]).resolve()
    out = pathlib.Path(argv[2] if len(argv) > 2 else "_pdf_preview").resolve()
    width = int(argv[3]) if len(argv) > 3 else 700
    out.mkdir(parents=True, exist_ok=True)
    for old in out.glob("*.png"):
        old.unlink()
    doc = pymupdf.open(src)
    figures = []
    for index, page in enumerate(doc, 1):
        pix = page.get_pixmap(dpi=120)
        path = out / f"page_{index:02d}.png"
        pix.save(str(path))
        encoded = base64.b64encode(path.read_bytes()).decode("ascii")
        figures.append(
            f'<figure><img src="data:image/png;base64,{encoded}" '
            f'alt="page {index}"/><figcaption>Page {index} of {len(doc)}'
            f'</figcaption></figure>')
    (out / "index.html").write_text(
        HTML % {"title": src.stem, "figures": "".join(figures), "width": width},
        encoding="utf-8")
    print(f"{src.name}: {len(doc)} page(s) -> {out / 'index.html'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
