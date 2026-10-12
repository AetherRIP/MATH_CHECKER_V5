"""Re-enable the configured Gemini keys (teacher-approved).

All 12 configured keys were flagged disabled/removed in the database after
the provider-cooldown bug, leaving the app with "No usable Gemini keys
remain". This re-enables them through the app's OWN KeyManager so the normal
bookkeeping (cooldown reset, stats untouched) is preserved.

Safe: fingerprints and labels only, never key material. Supports --dry-run.
"""
from __future__ import annotations

import argparse
import hashlib
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))

from app import db  # noqa: E402
from app.config import get_secrets  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    db.init_db()
    from app.api.routes_all import _km  # app's singleton KeyManager
    km = _km()

    keys = get_secrets().gemini_keys()
    fps = [hashlib.sha256(k.encode()).hexdigest()[:12] for k in keys]

    before = km.status()
    print(f"before: total={before['total']} healthy={before['healthy']} "
          f"cooldown={before['cooldown']} invalid={before['invalid']} "
          f"disabled={before['disabled']} all_down={before['all_down']}")

    if args.dry_run:
        print("\n[dry-run] would re-enable:")
        for i, fp in enumerate(fps, 1):
            print(f"  key_{i}  {fp}")
        return 0

    for i, fp in enumerate(fps, 1):
        km.re_enable(fp)
        print(f"[keys] key_{i}  {fp}  re-enabled")

    after = km.status()
    print(f"\nafter:  total={after['total']} healthy={after['healthy']} "
          f"cooldown={after['cooldown']} invalid={after['invalid']} "
          f"disabled={after['disabled']} all_down={after['all_down']}")

    # Prove the manager will actually hand a key out now.
    fp, rec = km.acquire()
    print(f"[keys] acquire() -> {fp} (label={rec.label}) — rotation is live")
    km.release_exclusive(fp)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())