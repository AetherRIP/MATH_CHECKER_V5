"""Phase 2 — run the REAL correction pipeline on a REAL C1L13 submission.

TESTING ONLY. This harness:
  * runs against an ISOLATED data dir (``MC5_DATA_DIR``), never ``data/``;
  * never publishes a grade, attaches a report, or returns work to Classroom
    (``classroom.enabled`` and ``automation.enabled`` are forced off here and
    re-asserted after import);
  * drives the existing Large PDF / normal pipeline unchanged.

Usage (from backend/):
    python scripts/audit_run.py --student-label yousef --pages-limit 49

Writes ``data/audit_runs/<label>_<ts>.json`` for the audit view.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
ROOT = BACKEND.parent

# --- isolation MUST happen before the app package is imported ----------------
TEST_DATA = Path(os.environ.get("MC5_AUDIT_DATA_DIR",
                                Path(os.environ.get("TEMP", "/tmp")) / "mc5_audit_test"))
os.environ["MC5_DATA_DIR"] = str(TEST_DATA)
os.environ["MC5_LOGIN_REQUIRED"] = "0"
sys.path.insert(0, str(BACKEND))

COURSEWORK_ID = "889601795010"
MARK_SCHEME_SRC = ROOT / "data" / "mark_schemes" / f"{COURSEWORK_ID}.pdf"
PROD_DATA = ROOT / "data"
OUT_DIR = ROOT / "data" / "audit_runs"

#: Real student answer PDFs retained from real Classroom downloads.
REAL_PDFS = {
    "yousef": PROD_DATA / "tmp" / "job-3a16b471a888407a83a3f908a88939f5" /
              "submission_0_0df0c018_normalized_a235634a.pdf",
    "daniel": PROD_DATA / "tmp" / "job-08dd92601038410abaef24ffb230c2b1" /
              "submission_0_b7580f7f_normalized_f4482d13.pdf",
}


def seed_test_env() -> None:
    """Create the isolated data dir with only what a real run needs."""
    (TEST_DATA / "mark_schemes").mkdir(parents=True, exist_ok=True)
    (TEST_DATA / "tmp").mkdir(parents=True, exist_ok=True)
    (TEST_DATA / "logs").mkdir(parents=True, exist_ok=True)
    for name in ("config.toml", "secrets.toml"):
        src = PROD_DATA / name
        if src.exists():
            shutil.copy2(src, TEST_DATA / name)
    # The cached, teacher-verified mark-scheme structure (no secrets in it).
    bp_src = PROD_DATA / "blueprints"
    if bp_src.exists():
        shutil.copytree(bp_src, TEST_DATA / "blueprints", dirs_exist_ok=True)
    if MARK_SCHEME_SRC.exists():
        shutil.copy2(MARK_SCHEME_SRC, TEST_DATA / "mark_schemes" /
                     f"{COURSEWORK_ID}.pdf")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--student-label", default="yousef")
    ap.add_argument("--max-seconds", type=int, default=1800)
    # Each correction must be a FRESH run: enqueue reuses an existing job for
    # the same (student pdf sha, mark scheme sha), so a distinct run tag gives
    # a distinct isolated data dir and therefore a genuine new correction.
    ap.add_argument("--run-tag", default="")
    args = ap.parse_args()

    if args.run_tag:
        global TEST_DATA
        TEST_DATA = TEST_DATA.parent / f"{TEST_DATA.name}_{args.run_tag}"
        os.environ["MC5_DATA_DIR"] = str(TEST_DATA)

    seed_test_env()

    from app import config, db  # noqa: E402
    from app.config import DATA_DIR  # noqa: E402

    # Bring the isolated database up to the production schema.
    db.init_db()

    # --- hard isolation assertions -------------------------------------------
    assert Path(DATA_DIR).resolve() != PROD_DATA.resolve(), "DATA_DIR is production!"
    assert str(Path(db.DB_PATH).resolve()).startswith(str(TEST_DATA.resolve())), \
        "DB is not in the isolated data dir"
    cfg = config.get_config(reload=True)
    cfg.classroom.enabled = False
    cfg.classroom.write_draft_grades = False
    cfg.automation.enabled = False
    if cfg.mock_mode:
        print("!! mock_mode is ON — this would NOT be a real AI run")
        return 2

    student_pdf = REAL_PDFS.get(args.student_label)
    if student_pdf is None:
        print("unknown label; known:", sorted(REAL_PDFS))
        return 2
    if not student_pdf.exists():
        print("missing real student pdf:", student_pdf)
        return 2

    from app.engine import pdf_inspect  # noqa: E402
    from app.engine.router import route_student_pdf  # noqa: E402

    pages = pdf_inspect.page_count(str(student_pdf))
    decision = route_student_pdf(str(student_pdf))
    print(f"TESTING ONLY — isolated data dir: {TEST_DATA}")
    print(f"student pdf : {student_pdf.name}  pages={pages}")
    print(f"route       : {decision.kind} ({decision.reason})")

    from app.large_pdf import states as lstates  # noqa: E402
    from app.large_pdf.runner import LargePdfRunnerHolder  # noqa: E402

    runner = LargePdfRunnerHolder.instance()
    scheme = str(TEST_DATA / "mark_schemes" / f"{COURSEWORK_ID}.pdf")
    job = runner.enqueue_manual(student_name=f"TEST-{args.student_label}",
                                student_pdf_path=str(student_pdf),
                                mark_scheme_path=scheme)
    job_id = job["id"]
    print("job id      :", job_id)

    deadline = time.time() + args.max_seconds
    last = None
    while time.time() < deadline:
        row = db.query_one("SELECT status, step_detail FROM large_pdf_jobs WHERE id=?",
                           (job_id,))
        cur = (row["status"], row["step_detail"])
        if cur != last:
            print(f"  [{time.strftime('%H:%M:%S')}] {cur[0]} — {cur[1]}")
            last = cur
        if row["status"] in lstates.TERMINAL:
            break
        runner.process(job_id)
        time.sleep(1.0)

    job = db.row_to_dict(db.query_one("SELECT * FROM large_pdf_jobs WHERE id=?",
                                      (job_id,)))
    print("\nfinal status:", job["status"], "|", job["step_detail"])
    batches = [dict(b) for b in db.query(
        "SELECT batch_index,page_start,page_end,status,attempts,error_code,"
        "validation_json FROM large_pdf_batches WHERE job_id=? ORDER BY batch_index",
        (job_id,))]
    for b in batches:
        v = db.loads(b.pop("validation_json"), {}) or {}
        print("  batch", b["batch_index"], "pages", b["page_start"], "-", b["page_end"],
              b["status"], "| validated_questions=", v.get("question_count"),
              "missing=", v.get("missing_questions"))

    result = None
    if job.get("result_id"):
        row = db.query_one("SELECT * FROM results WHERE id=?", (job["result_id"],))
        result = dict(row)
        result["result_json"] = db.loads(result["result_json"])

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out = OUT_DIR / f"{args.student_label}_{int(time.time())}.json"
    out.write_text(json.dumps({
        "label": args.student_label,
        "run_at": time.time(),
        "isolated_data_dir": str(TEST_DATA),
        "pages": pages,
        "route": decision.kind,
        "job": {k: v for k, v in job.items() if k not in ("plan_json",)},
        "batches": batches,
        "result": result,
    }, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    print("\nwrote", out)

    if result:
        rj = result["result_json"] or {}
        qs = rj.get("questions") or []
        print(f"\nSCORE: {result['earned']}/{result['available']} "
              f"({result['percentage']:.2f}%) grade {result['provisional_grade']} "
              f"review={result['review_required']} questions={len(qs)}")
        unresolved = [q.get("question_number") for q in qs
                      if str(q.get("status", "")).upper() in
                      ("REVIEW", "NEEDS_REVIEW", "UNMARKED")]
        print("unresolved questions:", unresolved)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
