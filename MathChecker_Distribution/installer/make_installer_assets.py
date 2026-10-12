#!/usr/bin/env python3
"""Generate branded installer assets (wizard images + icon) from the palette.

Uses Pillow, which the application already depends on, so the installer keeps
the Math Checker look (accent #6d8dff → #8a6cff, dark surface #0f131c) instead
of the default Windows wizard chrome.

Outputs (installer/assets/):
    wizard-large.bmp   164x314  modern wizard sidebar
    wizard-small.bmp    55x55   header mark
    mathchecker.ico    multi-size application icon
"""

from __future__ import annotations

import sys
from pathlib import Path

ASSETS = Path(__file__).resolve().parent / "assets"

ACCENT = (109, 141, 255)
ACCENT_2 = (138, 108, 255)
BG_TOP = (15, 19, 28)
BG_BOTTOM = (10, 13, 19)
TEXT = (233, 236, 243)
DIM = (154, 163, 182)


def _lerp(a: tuple[int, int, int], b: tuple[int, int, int], t: float):
    return tuple(round(a[i] + (b[i] - a[i]) * t) for i in range(3))


def _gradient(size: tuple[int, int], top, bottom):
    from PIL import Image
    w, h = size
    img = Image.new("RGB", size)
    px = img.load()
    for y in range(h):
        color = _lerp(top, bottom, y / max(1, h - 1))
        for x in range(w):
            px[x, y] = color
    return img


def _draw_sigma(draw, cx: int, cy: int, size: int, color=TEXT):
    """A simple Σ glyph drawn with lines (keeps us font-independent)."""
    half = size // 2
    thickness = max(2, size // 8)
    left = cx - half
    right = cx + half
    top = cy - half
    bottom = cy + half
    draw.line([(left, top), (right, top)], fill=color, width=thickness)
    draw.line([(left, top), (cx, cy)], fill=color, width=thickness)
    draw.line([(cx, cy), (left, bottom)], fill=color, width=thickness)
    draw.line([(left, bottom), (right, bottom)], fill=color, width=thickness)


def make_wizard_large() -> Path:
    from PIL import Image, ImageDraw
    img = _gradient((164, 314), BG_TOP, BG_BOTTOM)
    # accent glow at the top
    glow = Image.new("RGB", img.size, (0, 0, 0))
    gd = ImageDraw.Draw(glow)
    for r in range(120, 0, -6):
        t = 1 - r / 120
        color = _lerp(BG_TOP, ACCENT, 0.35 * t)
        gd.ellipse([82 - r, 80 - r, 82 + r, 80 + r], fill=color)
    img = Image.blend(img, glow, 0.5)
    d = ImageDraw.Draw(img)
    _draw_sigma(d, 82, 116, 52, (255, 255, 255))
    d.text((82, 200), "MATH", fill=TEXT, anchor="mm")
    d.text((82, 220), "CHECKER", fill=DIM, anchor="mm")
    out = ASSETS / "wizard-large.bmp"
    img.convert("RGB").save(out, format="BMP")
    return out


def make_wizard_small() -> Path:
    from PIL import Image, ImageDraw
    img = _gradient((55, 55), ACCENT, ACCENT_2)
    d = ImageDraw.Draw(img)
    _draw_sigma(d, 27, 27, 24, (255, 255, 255))
    out = ASSETS / "wizard-small.bmp"
    img.convert("RGB").save(out, format="BMP")
    return out


def make_icon() -> Path:
    from PIL import Image, ImageDraw
    sizes = [16, 24, 32, 48, 64, 128, 256]
    base = Image.new("RGBA", (256, 256), (0, 0, 0, 0))
    d = ImageDraw.Draw(base)
    # rounded accent square
    d.rounded_rectangle([8, 8, 248, 248], radius=56, fill=ACCENT + (255,))
    for y in range(8, 249):
        t = (y - 8) / 240
        color = _lerp(ACCENT, ACCENT_2, t)
        d.line([(8, y), (248, y)], fill=color + (255,))
    _draw_sigma(d, 128, 128, 96, (255, 255, 255))
    out = ASSETS / "mathchecker.ico"
    base.save(out, format="ICO", sizes=[(s, s) for s in sizes])
    return out


def main() -> int:
    try:
        import PIL  # noqa: F401
    except ImportError:
        print("Pillow is required to generate installer assets.", file=sys.stderr)
        return 1
    ASSETS.mkdir(parents=True, exist_ok=True)
    for maker in (make_wizard_large, make_wizard_small, make_icon):
        print(f"  generated {maker().name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
