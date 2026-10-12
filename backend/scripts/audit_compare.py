"""Phase 3/4 — before/after comparison + evidence audit for one real submission."""
from __future__ import annotations

import glob
import json
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PROD = ROOT / "data"
BP_FILE = PROD / "blueprints" / \
    "1428ebc65189fdcfc094abf9a0ee4a8836927a602d8f62b559a38b7f96b37aa8.json"
BP = json.loads(BP_FILE.read_text(encoding="utf-8"))["blueprint"]
bp_max = {str(q["question_number"]): int(q["max_marks"]) for q in BP["questions"]}
BP_TOTAL = sum(bp_max.values())

SUBMISSION = "Cg4Ig4-_kqoJEMLHmIPyGQ"
LABEL = "yousef"

runs = sorted(glob.glob(str(PROD / "audit_runs" / f"{LABEL}_*.json")))
fresh = json.loads(Path(runs[-1]).read_text(encoding="utf-8"))
print("fresh run file:", Path(runs[-1]).name)

conn = sqlite3.connect(str(PROD / "app.db"))
conn.row_factory = sqlite3.Row
row = conn.execute("SELECT * FROM results WHERE submission_id=? ORDER BY created_at DESC",
                   (SUBMISSION,)).fetchone()
old = json.loads(row["result_json"])

old_q = {str(q.get("question_number")): q for q in old.get("questions") or []}
new_q = {str(q.get("question_number")): q for q in
         (fresh["result"] or {}).get("result_json", {}).get("questions") or []}


def sortkey(s):
    d = "".join(ch for ch in s if ch.isdigit())
    return (0, int(d)) if d else (1, 0)


print("\n" + "=" * 96)
print("GROUND TRUTH: mark-scheme blueprint total =", BP_TOTAL,
      "| questions =", len(bp_max))
print("STORED PRODUCTION RESULT :", row["earned"], "/", row["available"],
      f"({row['percentage']:.2f}%) grade", row["provisional_grade"],
      "review", row["review_required"])
fr = fresh["result"]
print("FRESH REAL RUN           :", fr["earned"], "/", fr["available"],
      f"({fr['percentage']:.2f}%) grade", fr["provisional_grade"],
      "review", fr["review_required"])

print("\n" + "=" * 96)
print("PER-QUESTION DIFF  (max = official blueprint max)")
print(f"{'Q':<5}{'bp_max':>7}{'old':>6}{'new':>6}{'delta':>7}  {'old_status':<14}{'new_status':<16}note")
rows_changed = []
for q in sorted(set(old_q) | set(new_q), key=sortkey):
    o, n = old_q.get(q), new_q.get(q)
    oa = int((o or {}).get("awarded_marks") or 0)
    na = int((n or {}).get("awarded_marks") or 0)
    om = int((o or {}).get("max_marks") or 0)
    nm = int((n or {}).get("max_marks") or 0)
    os_, ns = str((o or {}).get("status", "-")), str((n or {}).get("status", "-"))
    note = []
    if om != nm:
        note.append(f"max {om}->{nm}")
    if nm and bp_max.get(q) and nm != bp_max[q]:
        note.append(f"MAX!=blueprint({bp_max[q]})")
    if oa != na:
        note.append(f"award {oa}->{na}")
    marker = "  <<<" if note else ""
    if oa != na or om != nm:
        rows_changed.append(q)
    print(f"{q:<5}{bp_max.get(q, '-'):>7}{oa:>6}{na:>6}{na - oa:>7}  "
          f"{os_:<14}{ns:<16}{'; '.join(note)}{marker}")

print("\nquestions with a changed award/max:", rows_changed)
print("count:", len(rows_changed))

# --- evidence audit for the previously-unmarked block -----------------------
print("\n" + "=" * 96)
print("EVIDENCE AUDIT — questions 62-69 (previously unmarked, 34 marks)")
for q in [str(i) for i in range(62, 70)]:
    n = new_q.get(q)
    if not n:
        print(f"Q{q}: STILL ABSENT from fresh run")
        continue
    print(f"\nQ{q}  awarded={n.get('awarded_marks')}/{n.get('max_marks')} "
          f"status={n.get('status')}")
    print("   answer        :", str(n.get("student_answer"))[:160])
    print("   working       :", str(n.get("working_summary"))[:160])
    print("   breakdown     :", json.dumps(n.get("mark_breakdown"))[:300])
    print("   solution      :", str(n.get("full_mark_solution"))[:120])

print("\n" + "=" * 96)
print("UNRESOLVED / REVIEW ROWS IN FRESH RUN")
for q in sorted(new_q, key=sortkey):
    n = new_q[q]
    st = str(n.get("status", "")).upper()
    if st in ("REVIEW", "NEEDS_REVIEW", "UNMARKED"):
        print("  Q%-4s %s :: %s" % (q, st, str(n.get("review_reason"))[:140]))

v = fr.get("verification_json")
print("\nverification:", json.dumps(v)[:600] if v else None)
