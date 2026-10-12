"""Phase 2 prep — dump the cached question-level results for C1L13 - Sets."""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DB = ROOT / "data" / "app.db"
CW = "889601795010"

conn = sqlite3.connect(str(DB))
conn.row_factory = sqlite3.Row


def anon(name):
    import hashlib

    return "S-" + hashlib.sha1((name or "?").encode()).hexdigest()[:6]


rows = list(conn.execute(
    "SELECT id, submission_id, student_name, earned, available, percentage, "
    "provisional_grade, review_required, manual_override, created_at, result_json, "
    "verification_json FROM results WHERE coursework_id=? ORDER BY created_at",
    (CW,),
))
print("results for C1L13:", len(rows))
for r in rows:
    rj = json.loads(r["result_json"] or "{}")
    qs = rj.get("questions") or []
    print("\n" + "=" * 70)
    print(anon(r["student_name"]), "| earned", r["earned"], "/", r["available"],
          "| pct", round(r["percentage"], 2), "| grade", r["provisional_grade"],
          "| review", r["review_required"], "| override", r["manual_override"])
    print("  result_id:", r["id"], "| questions:", len(qs))
    print("  paper_info:", json.dumps(rj.get("paper_info"))[:200])
    for q in qs:
        num = q.get("question_number")
        print("    Q%-4s awarded=%-3s max=%-3s status=%-10s conf=%-5s %s" % (
            num, q.get("awarded_marks"), q.get("max_marks"),
            q.get("status"), q.get("confidence"),
            str(q.get("mark_scheme_reference") or q.get("notes") or "")[:70]))
    notes = rj.get("overall_notes") or {}
    print("  notes summary:", str(notes.get("summary"))[:200])
    print("  uncertainties:", len(rj.get("uncertainties") or []))

totals = {}
for r in rows:
    rj = json.loads(r["result_json"] or "{}")
    for q in rj.get("questions") or []:
        totals.setdefault(str(q.get("question_number")), []).append(q)
print("\n" + "=" * 70)
print("DISTINCT QUESTION NUMBERS ACROSS ALL RESULTS:", len(totals))
print(sorted(totals, key=lambda s: (len(s), s)))
