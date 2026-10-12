"""Read-only: are the cooldown-sidelined keys still in the configured set?

Computes SHA-256 fingerprint prefixes of the configured keys and matches them
against the records sidelined by the cooldown bug, so we know whether
re-enabling them is even possible. Fingerprints only — never key material.
"""
from __future__ import annotations

import hashlib
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))

from app.config import get_secrets  # noqa: E402


def main() -> int:
    keys = get_secrets().gemini_keys()
    fps = {hashlib.sha256(k.encode()).hexdigest()[:12]: i + 1
           for i, k in enumerate(keys)}
    print(f"configured keys: {len(keys)}")

    db = sqlite3.connect(f"file:{ROOT / 'data' / 'app.db'}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    sidelined = [dict(r) for r in db.execute("""
        SELECT k.fingerprint, k.label, k.removed, k.disabled,
               COALESCE(s.successes,0) su, COALESCE(s.last_error,'') e
          FROM gemini_keys k LEFT JOIN key_stats s USING(fingerprint)
    """)]
    for r in sidelined:
        pos = fps.get(r["fingerprint"])
        tag = (f"key_{pos} (configured)" if pos
               else "NOT in configured secret store")
        cause = "cooldown bug" if "cooling down" in r["e"].lower() \
            else "genuine rejection" if r["removed"] else "other"
        print(f"  {r['fingerprint']}  {r['label']:<8} successes={r['su']:<3} "
              f"{cause:<17} -> {tag}")
    return 0


if __name__ == "__main__":
    sys.exit(main())