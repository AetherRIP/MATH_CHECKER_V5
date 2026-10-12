"""TEMPORARY controlled end-to-end marking test (Step 9).

Runs the REAL pipeline against the REAL student submission + REAL mark scheme
in an isolated database:

    PDF -> blueprint (mark scheme) -> Gemini -> AI marking -> validation
        -> totals -> groups/report

Safety:
* isolated SQLite DB + temp dir — real grades, results and Classroom records
  are never read or written;
* no Classroom/network calls beyond the Gemini API the app already uses;
* uploads are released and the remotes deleted afterwards;
* the fixture PDFs are only READ, never modified.
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
BACKEND = ROOT / "backend"

ISOLATED = Path(tempfile.mkdtemp(prefix="mc_e2e_"))
# Isolate writes (DB, results, grades) while keeping the real API credentials
# so the pipeline talks to Gemini exactly as in production.
shutil.copy2(ROOT / "data" / "secrets.toml", ISOLATED / "secrets.toml")
if (ROOT / "data" / "config.toml").exists():
    shutil.copy2(ROOT / "data" / "config.toml", ISOLATED / "config.toml")
os.environ["MC5_DATA_DIR"] = str(ISOLATED)

sys.path.insert(0, str(BACKEND))

DEFAULT_SUBMISSION = ROOT / "data" / "fixtures" / "student_submission.pdf"
DEFAULT_QUESTION = None
DEFAULT_MARK_SCHEME = ROOT / "data" / "fixtures" / "mark_scheme.pdf"


def _parse_args():
    import argparse
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--submission", default=str(DEFAULT_SUBMISSION))
    ap.add_argument("--mark-scheme", dest="mark_scheme",
                    default=str(DEFAULT_MARK_SCHEME))
    ap.add_argument("--question", default=str(DEFAULT_QUESTION or ""))
    ap.add_argument("--show-questions", type=int, default=10)
    return ap.parse_args()


def _mb(path: Path) -> float:
    try:
        return path.stat().st_size / (1024 * 1024)
    except OSError:
        return 0.0


def banner(text: str) -> None:
    print(f"\n=== {text} ===", flush=True)


def main() -> int:
    args = _parse_args()
    submission = Path(args.submission)
    mark_scheme = Path(args.mark_scheme)
    question = Path(args.question) if args.question else None

    banner("isolated environment")
    print(f"[e2e] data_dir={ISOLATED}")

    for path in (submission, mark_scheme):
        if not path.exists():
            print(f"[e2e] MISSING fixture: {path}")
            return 2

    banner("inputs")
    print(f"[e2e] submission   = {submission.name} ({_mb(submission):.1f} MB)")
    print(f"[e2e] mark_scheme  = {mark_scheme.name} ({_mb(mark_scheme):.1f} MB)")
    if question:
        print(f"[e2e] question_paper = {question.name} ({_mb(question):.1f} MB)")
    else:
        print("[e2e] question_paper = (none)")

    from app.config import get_config
    from app.gemini.provider_health import provider_health
    from app.workers.child import _run

    # Build the isolated schema (real DB untouched).
    from app import db
    db.init_db()

    cfg = get_config()
    provider_health().reset()

    job = {
        "job_id": "e2e-probe-1",
        "worker_id": "e2e-worker",
        "submission_paths": [str(submission)],
        "mark_scheme_path": str(mark_scheme),
        "question_paper_path": str(question) if question else None,
        "primary_models": list(cfg.gemini.primary_models),
        "audit_enabled": False,
        "leased_fingerprint": None,
        "mock": False,
    }

    banner("run pipeline (real submission + real mark scheme)")
    started = time.time()
    outcome = _run(job)
    elapsed = time.time() - started

    banner("result")
    print(f"[e2e] status={outcome['status']} ok={outcome['ok']}")
    print(f"[e2e] marking_model={outcome['marking_model']}")
    print(f"[e2e] totals={json.dumps(outcome['totals'])}")
    print(f"[e2e] blueprint_source={outcome['blueprint_report'].get('source')}")
    print(f"[e2e] questions_marked={len(outcome['result']['questions'])}")
    print(f"[e2e] groups={len(outcome['groups'])}")
    print(f"[e2e] elapsed={elapsed:.1f}s")

    banner("provider gate state after a successful run")
    snap = provider_health().snapshot()
    print(f"[e2e] available={snap['available']} "
          f"cooldowns={snap['cooldowns']} "
          f"consecutive_high_demand={snap['consecutive_high_demand']}")

    questions = outcome["result"]["questions"]
    print(f"\n[e2e] first {args.show_questions} questions:")
    for q in questions[: args.show_questions]:
        text = (q.get("student_answer") or q.get("response") or "")[:70]
        text = str(text).replace("\n", " ")
        print(f"  Q{q.get('question_number')}: "
              f"awarded={q.get('awarded_marks')}/{q.get('max_marks')} | {text}")

    ok = outcome["ok"] and outcome["status"] == "COMPLETED" \
        and outcome["totals"]["available"] > 0 and bool(questions)
    banner("RESULT")
    print(f"[e2e] {'PASS' if ok else 'FAIL'}: pipeline completed end to end")
    return 0 if ok else 1


def _cleanup() -> None:
    shutil.rmtree(ISOLATED, ignore_errors=True)


if __name__ == "__main__":
    try:
        code = main()
    finally:
        _cleanup()
    raise SystemExit(code)