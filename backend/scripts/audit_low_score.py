"""Phase 3B — dissect the low-scoring real submission against the blueprint."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
conn = sqlite3.connect(str(ROOT / "data" / "app.db"))
conn.row_factory = sqlite3.Row

BP = json.loads((ROOT / "data" / "blueprints" /
                 "1428ebc65189fdcfc094abf9a0ee4a8836927a602d8f62b559a38b7f96b37aa8.json"
                 ).read_text(encoding="utf-8"))["blueprint"]
bp_max = {str(q["question_number"]): int(q["max_marks"]) for q in BP["questions"]}
print("blueprint total:", sum(bp_max.values()), "questions:", len(bp_max))
print("Q15/Q28/Q34 blueprint max:", bp_max.get("15"), bp_max.get("28"), bp_max.get("34"))

SUBS = ["Cg4Ig4-_kqoJEMLHmIPyGQ", "Cg4I5r-H58cZEMLHmIPyGQ"]
for sid in SUBS:
    r = conn.execute("SELECT * FROM results WHERE submission_id=?", (sid,)).fetchone()
    if not r:
        print("no result for", sid[-8:])
        continue
    rj = json.loads(r["result_json"] or "{}")
    qs = {str(q.get("question_number")): q for q in (rj.get("questions") or [])}
    lost = []
    for num, q in qs.items():
        aw = int(q.get("awarded_marks") or 0)
        mx = int(q.get("max_marks") or 0)
        expected = bp_max.get(num)
        if aw < mx or (expected is not None and mx != expected):
            lost.append((num, aw, mx, expected))
    print("\n" + "=" * 72)
    print(sid[-8:], "earned", r["earned"], "/", r["available"], "review", r["review_required"])
    print("  questions with lost marks or max mismatch:", len(lost))
    def key(t):
        try:
            return (0, int(t[0]))
        except ValueError:
            return (1, t[0])
    for num, aw, mx, exp in sorted(lost, key=key):
        print(f"    Q{num:<4} awarded={aw} max={mx} blueprint_max={exp}"
              f" {'<< MAX MISMATCH' if exp is not None and mx != exp else ''}")

    jobs = list(conn.execute(
        "SELECT id,status,page_count,batch_count,plan_json,step_detail,error "
        "FROM large_pdf_jobs WHERE submission_id=?", (sid,)))
    for j in jobs:
        print("\n  JOB", j["id"], j["status"], "pages", j["page_count"],
              "batches", j["batch_count"], "detail", j["step_detail"])
        plan = json.loads(j["plan_json"] or "{}")
        for b in plan.get("batches") or []:
            print("     batch", b.get("index"), "pages", b.get("page_start"),
                  "-", b.get("page_end"))
        batches = list(conn.execute(
            "SELECT batch_index,page_start,page_end,status,attempts,error,error_code,"
            "validation_json FROM large_pdf_batches WHERE job_id=?", (j["id"],)))
        for b in batches:
            v = json.loads(b["validation_json"] or "{}")
            print(f"     [db] batch {b['batch_index']} pages {b['page_start']}-{b['page_end']}"
                  f" status={b['status']} attempts={b['attempts']} err={b['error_code']}"
                  f" validated_questions={v.get('question_count')}"
                  f" missing={v.get('missing_questions')}")
