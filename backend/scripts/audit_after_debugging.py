"""Read-only integrity audit after the debugging session.

Confirms, from a FRESH process (so nothing is cached in memory):
* the key re-enable actually persisted to the database,
* no stray probe/backup files were left in the data directory,
* the mark scheme file is untouched,
* the file registry holds no dangling rows from the probes,
* the production fix files still contain the model-scoped gate.

Prints fingerprints and paths only - never key material.
"""
from __future__ import annotations

import hashlib
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))

problems: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"[{'OK  ' if ok else 'FAIL'}] {label}{(' - ' + detail) if detail else ''}")
    if not ok:
        problems.append(label)


def main() -> int:
    from app.config import get_secrets

    db = sqlite3.connect(f"file:{ROOT / 'data' / 'app.db'}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row

    # 1. key re-enable persisted?
    keys = get_secrets().gemini_keys()
    fps = {hashlib.sha256(k.encode()).hexdigest()[:12] for k in keys}
    rows = {r["fingerprint"]: r for r in db.execute(
        "SELECT fingerprint, removed, disabled FROM gemini_keys")}
    usable = [f for f in fps
              if f in rows and not rows[f]["removed"] and not rows[f]["disabled"]]
    check("all configured keys persisted as enabled", len(usable) == len(fps),
          f"{len(usable)}/{len(fps)} enabled")

    # 2. no stray files in data/
    strays = [p.name for p in (ROOT / "data" / "mark_schemes").glob("*")
              if not p.name.endswith(".pdf")]
    check("no stray probe/backup files in data/mark_schemes", not strays,
          ", ".join(strays))
    dump = ROOT / "backend" / "scripts" / "_blueprint_raw.json"
    check("raw blueprint dump removed", not dump.exists())

    # 3. mark scheme intact
    ms = ROOT / "data" / "mark_schemes" / "886161920765.pdf"
    check("official mark scheme present and non-empty",
          ms.exists() and ms.stat().st_size > 1_000_000,
          f"{ms.stat().st_size:,} bytes" if ms.exists() else "missing")

    # 4. no dangling remote-file rows from the probes
    try:
        dangling = db.execute(
            "SELECT COUNT(*) FROM gemini_remote_files WHERE remote_name LIKE 'bp-%'"
        ).fetchone()[0]
        check("no dangling file-registry rows from probes", not dangling,
              f"{dangling} rows")
    except sqlite3.OperationalError:
        print("[SKIP] gemini_remote_files table not present")

    # 5. production fix still in place
    ph = (ROOT / "backend" / "app" / "gemini" / "provider_health.py").read_text(
        encoding="utf-8")
    cp = (ROOT / "backend" / "app" / "gemini" / "client_proxy.py").read_text(
        encoding="utf-8")
    check("provider gate is model-scoped",
          "available(model)" in cp and "def available(self, model" in ph)
    check("cooldown cap bounded to 240s", "COOLDOWN_CAP_SECONDS = 240.0" in ph)
    check("half-open recovery present",
          "streak.consecutive >= HIGH_DEMAND_THRESHOLD" in ph)

    print(f"\n{'ALL CHECKS PASSED' if not problems else 'PROBLEMS: ' + ', '.join(problems)}")
    return 0 if not problems else 1


if __name__ == "__main__":
    sys.exit(main())