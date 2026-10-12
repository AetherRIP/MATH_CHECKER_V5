"""Dump the stored evidence for the zero-scored block Q62-Q69."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
conn = sqlite3.connect(str(ROOT / "data" / "app.db"))
conn.row_factory = sqlite3.Row

for sid, label in (("Cg4Ig4-_kqoJEMLHmIPyGQ", "208/266 submission"),
                   ("Cg4I1K-s65IVEMLHmIPyGQ", "265/266 control")):
    r = conn.execute("SELECT * FROM results WHERE submission_id=?", (sid,)).fetchone()
    if not r:
        continue
    rj = json.loads(r["result_json"] or "{}")
    print("=" * 78)
    print(label, "| earned", r["earned"], "/", r["available"], "| review", r["review_required"])
    for q in rj.get("questions") or []:
        num = str(q.get("question_number"))
        if num not in {str(n) for n in range(60, 70)}:
            continue
        print("\n--- Q%s ---" % num)
        print(json.dumps(q, indent=2, ensure_ascii=False)[:1600])
