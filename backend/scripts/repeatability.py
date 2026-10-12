"""Repeatability harness: run the SAME student PDF + SAME mark scheme N times.

Answers "why do identical inputs produce different marks?" with measured
evidence instead of speculation.

Each run:
  * uses a FRESH KeyAwareClient -> KeyManager round-robin picks the next key,
    so the run also proves more than one key is really used,
  * reuses the cached mark-scheme BLUEPRINT (built on run 1),
  * marks with temperature 0 + fixed seed through the preserved engine,
  * reconciles against the blueprint and verifies.

Prints a per-question matrix across runs, totals, models, key fingerprints and
ends with a VERDICT: identical / minor / materially different.

Usage (from backend/):
    python scripts/repeatability.py [--runs 3]
        [--student PATH] [--scheme PATH] [--question PATH] [--out FILE]
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

from app.config import DATA_DIR, get_config, get_secrets  # noqa: E402
from app.db import init_db  # noqa: E402
from app.engine import blueprint as bp  # noqa: E402
from app.engine import legacy_marking as legacy  # noqa: E402
from app.engine.verify import verify_result  # noqa: E402
from app.gemini.client_proxy import KeyAwareClient  # noqa: E402
from app.gemini.key_manager import KeyManager, fingerprint  # noqa: E402

FIXTURES = DATA_DIR / "fixtures" / "real"


def one_run(run_no: int, student: Path, scheme: Path, question: Path | None,
            model_chain: list[str]) -> dict:
    km = KeyManager()
    client = KeyAwareClient(km)          # fresh -> round-robin next key
    started = time.time()

    blueprint, bp_meta = bp.get_blueprint(client, scheme)
    remote_students = [client.upload_file(str(student))]
    remote_scheme = client.upload_file(str(scheme))
    remote_question = client.upload_file(str(question)) if question else None
    try:
        raw, model = legacy.grade_with_model(
            client=client,
            model_names=model_chain,
            student_file=remote_students,
            mark_scheme_file=remote_scheme,
            question_file=remote_question,
            blueprint=blueprint,
        )
    finally:
        for remote in [r for r in
                       [*remote_students, remote_scheme, remote_question]
                       if r is not None]:
            client.files_delete(remote)
    result = legacy.normalise_result(raw)
    result, bp_report = bp.apply_blueprint(result, blueprint)
    bp_report["source"] = bp_meta.get("source", "unavailable")
    verification = verify_result(result, bp_report)
    earned, available, percentage = legacy.calculate_totals(result)

    record = {
        "run": run_no,
        "model": model,
        "key": client.current_fingerprint(),
        "blueprint_source": bp_meta.get("source"),
        "blueprint_total": bp_report.get("blueprint_total"),
        "earned": earned,
        "available": available,
        "percentage": round(percentage, 2),
        "status": verification["status"],
        "questions": {q["question_number"]: [q["awarded_marks"], q["max_marks"]]
                      for q in result["questions"]},
        "missing": bp_report.get("missing_questions"),
        "extra": bp_report.get("extra_questions"),
        "elapsed_s": round(time.time() - started, 1),
    }
    return record


def print_matrix(records: list[dict]) -> None:
    numbers: list[str] = []
    for r in records:
        for n in r["questions"]:
            if n not in numbers:
                numbers.append(n)

    header = f"{'Q':>6} | " + " | ".join(f"run{r['run']}" for r in records) + "  verdict"
    print("\n" + header)
    print("-" * len(header))
    differing = []
    for n in numbers:
        cells = []
        for r in records:
            pair = r["questions"].get(n)
            cells.append(f"{pair[0]}/{pair[1]}" if pair else "—MISSING—")
        same = len(set(cells)) == 1 and "—MISSING—" not in cells
        if not same:
            differing.append(n)
        print(f"{n:>6} | " + " | ".join(f"{c:>9}" for c in cells)
              + "  " + ("same" if same else "<< DIFFERS"))

    print("\nTotals:")
    for r in records:
        print(f"  run {r['run']}: {r['earned']}/{r['available']} "
              f"({r['percentage']}%)  status={r['status']}  "
              f"model={r['model']}  key={r['key']}  "
              f"blueprint={r['blueprint_source']}  {r['elapsed_s']}s")

    totals = {(r["earned"], r["available"]) for r in records}
    models = {r["model"] for r in records}
    keys = {r["key"] for r in records}
    print("\nDistinct totals :", sorted(totals))
    print("Distinct models :", sorted(models))
    print("Distinct keys   :", sorted(k or "?" for k in keys))
    if len(totals) == 1 and not differing:
        print("\nVERDICT: IDENTICAL — same questions, same marks, same totals.")
    elif len(totals) == 1:
        print(f"\nVERDICT: SAME TOTAL, different on {len(differing)} question(s): "
              f"{differing}")
    else:
        print(f"\nVERDICT: DIFFERENT TOTALS across runs "
              f"(differing questions: {differing})")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--student", default=str(FIXTURES / "Student Answer.pdf"))
    parser.add_argument("--scheme", default=str(FIXTURES / "Answer Sheet.pdf"))
    parser.add_argument("--question", default=str(FIXTURES / "Question paper.pdf"))
    parser.add_argument("--out", default=str(DATA_DIR / "repeatability.json"))
    parser.add_argument("--append", action="store_true",
                        help="continue a previous session recorded in --out")
    args = parser.parse_args()

    init_db()
    cfg = get_config()
    if cfg.mock_mode:
        print("mock mode is ON — turn it off for a real repeatability test.")
        return 2

    student, scheme = Path(args.student), Path(args.scheme)
    question = Path(args.question) if Path(args.question).exists() else None
    if not student.exists() or not scheme.exists():
        print(f"missing inputs: {student} / {scheme}")
        return 2

    print(f"runs={args.runs}\nstudent={student}\nscheme={scheme}\n"
          f"models={cfg.gemini.primary_models}\n"
          f"keys={len(get_secrets().gemini_keys())} configured")

    out_path = Path(args.out)
    records: list[dict] = []
    if args.append and out_path.exists():
        try:
            records = json.loads(out_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            records = []

    for i in range(len(records) + 1, len(records) + args.runs + 1):
        print(f"\n=== run {i} ===", flush=True)
        try:
            record = one_run(i, student, scheme, question,
                             list(cfg.gemini.primary_models))
        except Exception as exc:  # noqa: BLE001
            print(f"run {i} FAILED: {exc}")
            return 3
        records.append(record)
        print(f"  {record['earned']}/{record['available']} "
              f"({record['percentage']}%) model={record['model']} "
              f"key={record['key']} blueprint={record['blueprint_source']} "
              f"questions={len(record['questions'])} "
              f"missing={record['missing']} {record['elapsed_s']}s", flush=True)
        out_path.write_text(json.dumps(records, indent=2), encoding="utf-8")

    print_matrix(records)

    print("\nKey usage (safe fingerprints only):")
    from app import db
    for row in db.query(
            "SELECT fingerprint, requests, successes, failures, "
            "rate_limit_events, cooldown_until FROM key_stats ORDER BY fingerprint"):
        label = next((k.get("label") or f"KEY_?"
                      for k in KeyManager().status()["keys"]
                      if k["fingerprint"] == row["fingerprint"]), "?")
        print(f"  {label} [{row['fingerprint']}] requests={row['requests']} "
              f"successes={row['successes']} failures={row['failures']} "
              f"rate_limited={row['rate_limit_events']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
