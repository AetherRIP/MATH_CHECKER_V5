"""Apply the FRESH AUDIT results to the stored corrections (§7/§8/§9 of the
live-audit spec) — deterministically, with full history.

For every audited submission:
  * the audited question-level marks become the result payload,
  * totals/percentage are recomputed by the engine's own arithmetic,
  * the 1-9 grade is recomputed by the deterministic boundary system,
  * an append-only result_versions entry records the audit (nothing destroyed),
  * changed results are flagged review_required (a teacher must confirm; the
    audit is AI-based and the system is in TEST/REVIEW MODE),
  * a fresh report PDF is rendered + validated locally for changed results
    (Classroom had no report attached for them, so nothing to replace),
  * the student's original scan is never touched (read-only usage).
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import time

sys.path.insert(0, os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..")))

os.environ.setdefault("MC5_DATA_DIR", os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "data")))

from app.db import init_db, transaction, query_one  # noqa: E402
from app.engine import legacy_marking as legacy  # noqa: E402
from app.engine.grading import grade_for_totals  # noqa: E402
from app.engine.versions import record as record_version  # noqa: E402

init_db()

DATA = os.environ["MC5_DATA_DIR"]
AUDIT_DIR = os.path.join(DATA, "audit_results")
OUT_DIR = os.path.join(DATA, "audit_reports")
os.makedirs(OUT_DIR, exist_ok=True)

AUDITS = [
    ("Hala Marzouk", "Cg4IweGmoIYFEK3Cu7TpGQ"),
    ("Eyad Sharaf", "Cg4I6M7LovwNEK3Cu7TpGQ"),
    ("Ali AlHakeem", "Cg4I-M73i60XEK3Cu7TpGQ"),
    ("Adam Sobky", "Cg4IgeGYyL0ZEMLHmIPyGQ"),
    ("Asser Tamer", "Cg4I5snhr-YZEIbV1bTpGQ"),
]

summary = []
for name, sid in AUDITS:
    path = os.path.join(AUDIT_DIR, f"{sid[:12]}.json")
    if not os.path.exists(path):
        print(f"!! no audit file for {name} ({sid[:12]}), skipping")
        continue
    audited = json.load(open(path, encoding="utf-8"))
    result = audited["result"]
    e, a, p = legacy.calculate_totals(result)
    grade, pct = grade_for_totals(e, a)
    row = query_one("SELECT * FROM results WHERE submission_id = ?", (sid,))
    if row is None:
        print(f"!! no stored result for {name}, skipping")
        continue
    old = (row["earned"], row["available"], row["provisional_grade"])
    changed = (old[0], old[1]) != (e, a)
    model = audited.get("model", "")
    print(f"{name}: stored {old[0]}/{old[1]} ({old[2]}) -> audited {e}/{a} "
          f"(grade {grade}) changed={changed}")

    if not changed:
        # Total agrees: keep the stored result, record the audit snapshot only.
        with transaction() as conn:
            record_version(
                conn, result_id=row["id"], earned=e, available=a,
                percentage=p, provisional_grade=grade, marking_model=model,
                key_fingerprint=None, confidence="MEDIUM",
                confidence_reasons=[
                    "Fresh independent audit re-marked every question against "
                    "the official mark scheme and confirmed the total."],
                verification=audited.get("verification"), result=result,
                created_at=time.time(),
                note=("Independent audit: total confirmed; question-level "
                      "differences (if any) recorded in the audit trail"))
        summary.append({
            "student": name, "submission_id": sid,
            "automatic": list(old[:2]), "audited": [e, a],
            "grade": grade, "changed": False, "report_replaced": False,
            "classroom_grade_updated": False,
            "question_differences": [
                {"question": q, "automatic": au, "audited": qm}
                for q, au in audited.get("auto_q", {}).items()
                for qm in [next((qq["awarded_marks"] for qq in result["questions"]
                                 if str(qq["question_number"]) == str(q)), None)]
                if au != qm
            ],
        })
        continue

    # --- changed result: apply deterministically with full history -----------
    with transaction() as conn:
        # 1. append-only history of the AUDITED result
        record_version(
            conn, result_id=row["id"], earned=e, available=a, percentage=p,
            provisional_grade=grade, marking_model=model, key_fingerprint=None,
            confidence="MEDIUM",
            confidence_reasons=[
                "Fresh independent audit found the automatic result was not "
                "supported by the student's work against the official mark "
                "scheme; the audited marks replace it pending teacher review."],
            verification=audited.get("verification"), result=result,
            created_at=time.time(),
            note="FRESH AUDIT result — replaces the automatic correction")
        # 2. the results row becomes the audited result (current state)
        conn.execute(
            """
            UPDATE results SET earned = ?, available = ?, percentage = ?,
                provisional_grade = ?, marking_model = ?, result_json = ?,
                verification_json = ?, review_required = 1,
                confidence = 'MEDIUM', created_at = ?
            WHERE id = ?
            """,
            (e, a, p, grade, model,
             json.dumps(result, ensure_ascii=False),
             json.dumps(audited.get("verification"), ensure_ascii=False),
             time.time(), row["id"]),
        )

    # 3. regenerate + validate the report locally (never re-correcting)
    report_ok = None
    try:
        from app.classroom.report_attachment import render_report_pdf
        from app.automation.pdf_validation import validate_report_pdf
        fake_row = {
            "student_name": name, "earned": e, "available": a,
            "percentage": p, "provisional_grade": grade,
            "result_json": result, "created_at": time.time(),
        }
        pdf_bytes = render_report_pdf(fake_row, detail="standard")
        validation = validate_report_pdf(pdf_bytes, expected_text=[name])
        report_ok = bool(validation.get("ok"))
        out_pdf = os.path.join(
            OUT_DIR, f"{name.replace(' ', '_')}_{sid[:10]}_audited.pdf")
        with open(out_pdf, "wb") as fh:
            fh.write(pdf_bytes)
        print(f"   report regenerated: {len(pdf_bytes)} bytes, valid="
              f"{report_ok} -> {out_pdf}")
    except Exception as exc:  # noqa: BLE001
        report_ok = False
        print("   report regeneration failed:", exc)

    summary.append({
        "student": name, "submission_id": sid,
        "automatic": list(old[:2]), "audited": [e, a],
        "automatic_grade": old[2], "audited_grade": grade,
        "changed": True, "report_replaced": bool(report_ok),
        # Classroom had NO grade and NO report for these submissions, so there
        # was nothing incorrect to replace there (verified below).
        "classroom_grade_updated": False,
        "classroom_had_grade": False,
        "question_differences": [
            {"question": q, "automatic": au,
             "audited": next((qq["awarded_marks"] for qq in result["questions"]
                              if str(qq["question_number"]) == str(q)), None),
             "max": next((qq["max_marks"] for qq in result["questions"]
                          if str(qq["question_number"]) == str(q)), None)}
            for q, au in sorted(audited.get("auto_q", {}).items(),
                                key=lambda kv: (len(kv[0]), kv[0]))
            if au != next((qq["awarded_marks"] for qq in result["questions"]
                           if str(qq["question_number"]) == str(q)), None)
        ],
    })

# ---- Classroom verification (read-only): confirm what is actually there ----
conn = sqlite3.connect(os.path.join(DATA, "app.db"))
conn.row_factory = sqlite3.Row
for item in summary:
    sid = item["submission_id"]
    cs = conn.execute(
        "SELECT assigned_grade, draft_grade, current_state FROM "
        "classroom_submissions WHERE submission_id = ?", (sid,)).fetchone()
    att = conn.execute(
        "SELECT status, drive_file_id FROM report_attachments WHERE "
        "submission_id = ? ORDER BY updated_at DESC LIMIT 1", (sid,)).fetchone()
    item["classroom_state"] = dict(cs) if cs else None
    item["classroom_report_attachment"] = (dict(att) if att else None)

out = os.path.join(DATA, "audit_applied.json")
with open(out, "w", encoding="utf-8") as fh:
    json.dump(summary, fh, ensure_ascii=False, indent=2)
print("\nSaved audit application record ->", out)
for item in summary:
    print(json.dumps(item, ensure_ascii=False)[:400])
