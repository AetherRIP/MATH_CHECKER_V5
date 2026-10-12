"""Read-only report: why is each Gemini key currently sidelined?

Every key in the real database is flagged removed or disabled, so the app
reports "No usable Gemini keys remain". This groups them by CAUSE so we can
tell credentials that genuinely failed from healthy keys that were
sidelined because of the provider-cooldown bug.

Prints fingerprints and labels only — never key material.
"""
from __future__ import annotations

import sqlite3
import sys
from collections import Counter
from pathlib import Path

DB = Path(__file__).resolve().parents[2] / "data" / "app.db"


def classify(successes: int, disabled: int, removed: int,
             err: str) -> str:
    low = err.lower()
    if "cooling down" in low:
        return "sidelined by the cooldown bug (had real successes)"
    if removed:
        return "genuinely rejected by Gemini (auth/access)"
    if disabled and successes:
        return "disabled after other errors (had successes)"
    if disabled:
        return "disabled"
    return "active"


def main() -> int:
    db = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    rows = db.execute("""
        SELECT k.fingerprint, k.label, k.removed, k.disabled,
               COALESCE(s.successes, 0) AS su,
               COALESCE(s.failures, 0)  AS fa,
               COALESCE(s.auth_errors, 0) AS au,
               COALESCE(s.last_error, '') AS e
          FROM gemini_keys k LEFT JOIN key_stats s USING(fingerprint)
    """).fetchall()

    print(f"total key records: {len(rows)}")
    buckets = Counter(classify(r["su"], r["disabled"], r["removed"], r["e"])
                      for r in rows)
    for label, count in buckets.most_common():
        print(f"{count:>3}  {label}")

    recoverable = [r for r in rows if "cooling down" in r["e"].lower()]
    print(f"\nhealthy-but-sidelined keys: {len(recoverable)}")
    total_su = 0
    for r in recoverable:
        total_su += r["su"]
        print(f"  {r['fingerprint']}  {r['label']:<8} "
              f"successes={r['su']:<3} failures={r['fa']}")
    print(f"\ntotal recorded successes those keys ever had: {total_su}")
    return 0


if __name__ == "__main__":
    sys.exit(main())