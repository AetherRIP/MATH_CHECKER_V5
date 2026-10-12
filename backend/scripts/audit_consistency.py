"""Phase 3D — arithmetic & result-consistency audit on real C1L13 results.

Checks, per submission:
  * awarded <= max for every question
  * no duplicate question numbers
  * sum(question awarded) == stored earned
  * sum(question max) == stored available
  * percentage == earned/available
and across submissions:
  * per-question max_marks must be identical for the same question number
  * the assignment mark total must be identical for every student

Writes data/audit_consistency.json and prints a readable summary.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DB = ROOT / "data" / "app.db"
CW = "889601795010"
OUT = ROOT / "data" / "audit_consistency.json"


def anon(name: str) -> str:
    return "S-" + hashlib.sha1((name or "?").encode()).hexdigest()[:6]


conn = sqlite3.connect(str(DB))
conn.row_factory = sqlite3.Row
rows = list(conn.execute(
    "SELECT id, submission_id, student_name, earned, available, percentage, "
    "provisional_grade, review_required, result_json FROM results "
    "WHERE coursework_id=? ORDER BY created_at", (CW,)))

report = {"coursework_id": CW, "submissions": [], "cross_submission": {}}
max_by_q: dict[str, set] = defaultdict(set)
available_by_sub: dict[str, int] = {}

for r in rows:
    rj = json.loads(r["result_json"] or "{}")
    qs = rj.get("questions") or []
    problems: list[str] = []
    seen: dict[str, int] = {}
    sum_awarded = 0
    sum_max = 0
    for q in qs:
        num = str(q.get("question_number"))
        aw = int(q.get("awarded_marks") or 0)
        mx = int(q.get("max_marks") or 0)
        if num in seen:
            problems.append(f"DUPLICATE_QUESTION Q{num} appears "
                            f"{seen[num] + 1} times")
        seen[num] = seen.get(num, 0) + 1
        if aw > mx:
            problems.append(f"AWARDED_OVER_MAX Q{num}: {aw}>{mx}")
        sum_awarded += aw
        sum_max += mx
        max_by_q[num].add(mx)

    earned = r["earned"]
    available = r["available"]
    available_by_sub[anon(r["student_name"])] = available
    if sum_awarded != earned:
        problems.append(f"TOTAL_MISMATCH sum(questions)={sum_awarded} "
                        f"!= stored earned={earned}")
    if sum_max != available:
        problems.append(f"MAX_MISMATCH sum(max)={sum_max} != stored "
                        f"available={available}")
    if available:
        pct = round(100.0 * earned / available, 4)
        if abs(pct - round(float(r["percentage"]), 4)) > 0.01:
            problems.append(f"PERCENTAGE_MISMATCH stored={r['percentage']} "
                            f"computed={pct}")

    report["submissions"].append({
        "student": anon(r["student_name"]),
        "result_id": r["id"],
        "questions": len(qs),
        "sum_awarded": sum_awarded,
        "sum_max": sum_max,
        "stored_earned": earned,
        "stored_available": available,
        "problems": problems,
    })

inconsistent = {q: sorted(v) for q, v in sorted(
    max_by_q.items(), key=lambda kv: (len(kv[0]), kv[0])) if len(v) > 1}
report["cross_submission"] = {
    "per_question_max_inconsistent": inconsistent,
    "available_totals": sorted(set(available_by_sub.values())),
    "available_by_student": available_by_sub,
}

OUT.write_text(json.dumps(report, indent=2), encoding="utf-8")

print("=" * 72)
print("PER-SUBMISSION CHECKS")
for s in report["submissions"]:
    flag = "OK " if not s["problems"] else "!! "
    print(f"{flag}{s['student']}  q={s['questions']:>3}  "
          f"sum_awarded={s['sum_awarded']:>4} stored_earned={s['stored_earned']:>4}  "
          f"sum_max={s['sum_max']:>4} stored_available={s['stored_available']:>4}")
    for p in s["problems"]:
        print("        -", p)

print("\n" + "=" * 72)
print("CROSS-SUBMISSION: questions whose max differs between students")
for q, vals in inconsistent.items():
    print(f"  Q{q}: max marks seen = {vals}")
print("\ntotal questions with unstable max:", len(inconsistent))
print("distinct assignment totals 'available':", sorted(set(available_by_sub.values())))
print("\nwrote", OUT)
