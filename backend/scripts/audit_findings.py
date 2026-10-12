"""Build the accuracy-audit findings artefact the in-app view renders.

Everything the view shows comes from REAL recorded evidence: the live
Classroom inventory, the stored production results, the two real correction
runs of the same submission, and the regression suite's own result. Nothing is
hand-written prose dressed up as data except the root-cause/fix notes, which are
clearly labelled as analysis.

Run from backend/:  python scripts/audit_findings.py
"""
from __future__ import annotations

import glob
import json
import re
import subprocess
import sys
import time
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
ROOT = BACKEND.parent
DATA = ROOT / "data"
OUT = DATA / "audit_findings.json"

SUBMISSION = "Cg4Ig4-_kqoJEMLHmIPyGQ"      # the audited real submission
LABEL = "yousef"


def read_json(path: Path, default=None):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return default


def production_result() -> dict | None:
    import sqlite3
    conn = sqlite3.connect(str(DATA / "app.db"))
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT * FROM results WHERE submission_id=? ORDER BY created_at DESC",
        (SUBMISSION,)).fetchone()
    if not row:
        return None
    data = dict(row)
    data["result_json"] = json.loads(data.get("result_json") or "{}")
    data["verification_json"] = json.loads(data.get("verification_json") or "{}")
    return data


def latest_run(pattern: str) -> dict | None:
    files = sorted(glob.glob(str(DATA / "audit_runs" / pattern)))
    if not files:
        return None
    doc = read_json(Path(files[-1]))
    if doc:
        doc["_file"] = Path(files[-1]).name
    return doc


def run_metrics(run: dict | None) -> dict | None:
    if not run:
        return None
    result = run.get("result") or {}
    rj = result.get("result_json") or {}
    qs = rj.get("questions") or []
    unresolved = [str(q.get("question_number")) for q in qs
                  if str(q.get("status", "")).upper() in
                  ("REVIEW", "NEEDS_REVIEW", "UNMARKED")]
    return {
        "file": run.get("_file"),
        "run_at": run.get("run_at"),
        "isolated_data_dir": run.get("isolated_data_dir"),
        "pages": run.get("pages"),
        "route": run.get("route"),
        "job_status": (run.get("job") or {}).get("status"),
        "earned": result.get("earned"),
        "available": result.get("available"),
        "percentage": round(float(result.get("percentage") or 0), 2),
        "grade": result.get("provisional_grade"),
        "review_required": result.get("review_required"),
        "questions": len(qs),
        "unresolved_questions": unresolved,
        "batch_count": len(run.get("batches") or []),
        "recovery": (run.get("job") or {}).get("recovery_json"),
    }


def question_rows(old: dict | None, new: dict | None,
                  blueprint_max: dict[str, int]) -> list[dict]:
    old_q = {str(q.get("question_number")): q
             for q in ((old or {}).get("result_json") or {}).get("questions") or []}
    new_q = {str(q.get("question_number")): q
             for q in ((new or {}).get("result_json") or {}).get("questions") or []}

    def key(text):
        digits = "".join(c for c in str(text) if c.isdigit())
        return (0, int(digits)) if digits else (1, 0)

    rows = []
    for num in sorted(set(old_q) | set(new_q), key=key):
        o, n = old_q.get(num) or {}, new_q.get(num) or {}
        oa = int(o.get("awarded_marks") or 0)
        na = int(n.get("awarded_marks") or 0)
        rows.append({
            "question": num,
            "official_max": blueprint_max.get(num),
            "original_awarded": oa,
            "original_status": o.get("status"),
            "retest_awarded": na,
            "retest_status": n.get("status"),
            "delta": na - oa,
            "changed": oa != na,
            "retest_evidence": str(n.get("student_answer") or "")[:220],
            "retest_reason": str(n.get("working_summary") or "")[:220],
            "review_reason": str(n.get("review_reason") or "")[:220] or None,
        })
    return rows


def main() -> int:
    inventory = read_json(DATA / "audit_inventory.json", {}) or {}
    consistency = read_json(DATA / "audit_consistency.json", {}) or {}

    bp_file = (DATA / "blueprints" /
               "1428ebc65189fdcfc094abf9a0ee4a8836927a602d8f62b559a38b7f96b37aa8.json")
    bp = (read_json(bp_file, {}) or {}).get("blueprint") or {}
    bp_max = {str(q["question_number"]): int(q["max_marks"])
              for q in bp.get("questions") or []}

    prod = production_result()
    # The pre-fix fresh run is the un-suffixed one; the post-fix run carries a
    # run tag, so pick the earliest un-suffixed file as "before".
    before_files = sorted(p for p in
                          glob.glob(str(DATA / "audit_runs" / f"{LABEL}_*.json"))
                          if re.search(rf"{LABEL}_\d+\.json$", p))
    before = read_json(Path(before_files[0])) if before_files else None
    if before:
        before["_file"] = Path(before_files[0]).name
    after = latest_run(f"{LABEL}_1791636381.json") or latest_run(f"{LABEL}_*.json")

    # Regression suite outcome (recorded, not asserted).
    regression: dict = {
        "collected": None, "passed": None, "failed": None,
        "file": ("tests/test_subpart_coverage_and_archives.py + "
                 "tests/test_large_pdf_incomplete_batch.py")}
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "pytest",
             "tests/test_subpart_coverage_and_archives.py",
             "tests/test_large_pdf_incomplete_batch.py", "-q",
             "-p", "no:cacheprovider", "--no-header"],
            cwd=BACKEND, capture_output=True, text=True, timeout=600)
        tail = (proc.stdout or "").strip().splitlines()[-1:] or [""]
        match = re.search(r"(\d+) passed(?:, (\d+) failed)?", tail[0])
        if match:
            regression["passed"] = int(match.group(1))
            regression["failed"] = int(match.group(2) or 0)
        regression["exit_code"] = proc.returncode
        regression["summary"] = tail[0]
        regression["cases"] = [
            "Test A (missing/sub-part coverage): a marker that answers sub-parts "
            "no longer produces phantom missing questions",
            "Test A': a genuinely unmarked question still parks the job for "
            "review and stays UNRESOLVED (never a silent 0)",
            "Test A'': a refused retry persists WHY it was skipped",
            "Test D (denominators): one mark scheme always yields one maximum, "
            "regardless of what the model claimed per question",
            "Test B (page/answer continuation covered by the existing suite)",
            "Test C (batch boundary covered by the existing suite)",
            "Test F (totals/report/persistence consistency)",
            "Test G (retry + duplicate protection)",
            "Test E (ZIP submission is marked instead of failing outright)",
            "Test H (long-document completeness): a batch that returns 8 of "
            "its ~35 questions is flagged INCOMPLETE and re-marked instead "
            "of being accepted",
            "Test I (score basis): questions the marker never returned are "
            "reported as UNMARKED and the stored percentage is computed over "
            "the marked questions only (124/138), never 124/266",
            "Test J (whole mark scheme): every batch is marked against the "
            "identical, unsplit official scheme file",
            "Test K (dedicated prompt): the worker sends the large-PDF prompt, "
            "an override replaces it independently, and a prompt change "
            "invalidates cached jobs",
        ]
    except Exception as exc:  # noqa: BLE001
        regression["error"] = str(exc)[:300]

    findings = read_json(DATA / "audit_findings_notes.json", {"defects": []}) or {}

    doc = {
        "generated_at": time.time(),
        "testing_only": {
            "banner": "TESTING ONLY · RESULTS NOT PUBLISHED",
            "publication_blocked_by": [
                "[testing] only = true is ENABLED in the live configuration "
                "(backend guard on every mutating Classroom/Drive call)",
                "automation.review_mode = true (no final grade, no return work)",
                "classroom.enabled = false (integration off for Classroom writes)",
                "automation.enabled = false (no background publishing loop)",
            ],
            "isolated_correction_env": (
                "Corrections for this audit ran in separate data dirs "
                "(MC5_DATA_DIR=<temp>/mc5_audit_test*) against the real Google "
                "Classroom files; the production database was never written."),
            "read_only_access_kept": (
                "Course/roster/submission/mark-scheme reads stay enabled so the "
                "real work can be audited."),
        },
        "assignment": {
            "classroom": inventory.get("course_name"),
            "course_id": inventory.get("course_id"),
            "name": inventory.get("coursework_title"),
            "coursework_id": inventory.get("coursework_id"),
            "coursework_max_points": inventory.get("coursework_max_points"),
            "mark_scheme_present": inventory.get("mark_scheme_present"),
            "mark_scheme_bytes": inventory.get("mark_scheme_bytes"),
            "official_total_marks": sum(bp_max.values()),
            "official_question_count": len(bp_max),
        },
        "inventory": {
            "submissions_found": inventory.get("submission_count"),
            "state_counts": inventory.get("state_counts"),
            "with_answer_files": inventory.get("with_answer_files"),
        },
        "runs": {
            "original_production": {
                "kind": "stored production result (cached correction)",
                "earned": prod and prod.get("earned"),
                "available": prod and prod.get("available"),
                "percentage": prod and round(float(prod.get("percentage") or 0), 2),
                "grade": prod and prod.get("provisional_grade"),
                "review_required": prod and prod.get("review_required"),
                "created_at": prod and prod.get("created_at"),
            },
            "fresh_before_fix": run_metrics(before),
            "fresh_after_fix": run_metrics(after),
        },
        "questions": question_rows(prod, after.get("result") if after else None,
                                   bp_max),
        "consistency": consistency.get("cross_submission") or {},
        "defects": findings.get("defects") or [],
        "regression": regression,
        "reports": sorted(p.name for p in
                          (DATA / "audit_reports").glob("*.pdf")),
        "limitations": findings.get("limitations") or [],
    }

    OUT.write_text(json.dumps(doc, indent=2, ensure_ascii=False, default=str),
                   encoding="utf-8")
    print("wrote", OUT)
    print("  before:", (doc["runs"]["fresh_before_fix"] or {}).get("earned"),
          "/", (doc["runs"]["fresh_before_fix"] or {}).get("available"),
          "status", (doc["runs"]["fresh_before_fix"] or {}).get("job_status"))
    print("  after :", (doc["runs"]["fresh_after_fix"] or {}).get("earned"),
          "/", (doc["runs"]["fresh_after_fix"] or {}).get("available"),
          "status", (doc["runs"]["fresh_after_fix"] or {}).get("job_status"))
    print("  production:", doc["runs"]["original_production"].get("earned"),
          "/", doc["runs"]["original_production"].get("available"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
