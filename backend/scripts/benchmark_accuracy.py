#!/usr/bin/env python3
"""Run the teacher-certified marking benchmark.

    # refuse to spend provider quota when nothing is certified
    python backend/scripts/benchmark_accuracy.py --partition all

    # replay archived provider outcomes against scheme-derived references
    python backend/scripts/benchmark_accuracy.py --allow-unverified \\
        --mode replay \\
        --replay paper-a=docs/logs/paper_a_outcome.json \\
        --replay paper-b=docs/logs/paper_b_fixed_outcome.json

    # real pipeline (spends provider quota; requires the acknowledgement)
    python backend/scripts/benchmark_accuracy.py --mode live --ack INFERENCE

Why the live run is gated
-------------------------
The runner must never turn "no teacher has verified anything yet" into an
expensive provider sweep. Without eligible references the script exits 4 and
makes no request. ``--allow-unverified`` is for measurement against
scheme-derived references and is always reported as NOT CERTIFIED.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):  # pragma: no cover
        pass

REPO = Path(__file__).resolve().parents[2]
BACKEND = REPO / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from app.benchmark.dataset import DatasetError, load_dataset  # noqa: E402
from app.benchmark.runner import execute_benchmark  # noqa: E402
from app.benchmark.store import (VerificationError, VerificationStore,  # noqa: E402
                                 resolution_summary, resolve_references)
from app.config import data_dir, get_config  # noqa: E402

EXIT_CERTIFIED, EXIT_RUN_FAILED, EXIT_REFUSED = 0, 1, 2
EXIT_NOT_CERTIFIED, EXIT_UNAVAILABLE = 3, 4
LIVE_HARNESS = (REPO / "MathChecker_Distribution" / "scripts"
                / "verify_live_marking.py")
ACK_PHRASE = "INFERENCE"


def _upstream_model(args, config: dict) -> str:
    if args.model:
        return args.model
    if args.mode == "live":
        return (config.get("primary_models") or ["not recorded"])[0]
    return ""


def _config_snapshot() -> dict:
    cfg = get_config()
    return {
        "mock_mode": bool(cfg.mock_mode),
        "primary_models": list(cfg.gemini.primary_models or []),
        "request_timeout_ms": cfg.gemini.request_timeout_ms,
        "double_check_threshold_percentage":
            cfg.double_check.threshold_percentage,
    }


def _write_expected(path: Path, dataset, paper, items) -> None:
    marks = {item.question: int(item.scheme_mark) for item in items}
    path.write_text(json.dumps({
        "questions": marks,
        "total": sum(marks.values()),
        "total_marks": sum(item.max_marks for item in items),
        "tolerance": 0,
        "source": (f"{dataset.path}#{paper.id} "
                   "(benchmark reference; see the run record for certification)"),
    }, indent=2), encoding="utf-8")


def _live_corrector(dataset, args):
    def correct(paper, items):
        if not args.ack == ACK_PHRASE:
            raise RuntimeError(
                f"live inference requires --ack {ACK_PHRASE}; refusing to spend "
                "provider quota")
        if not LIVE_HARNESS.is_file():
            raise RuntimeError(f"live harness not found: {LIVE_HARNESS}")
        student = (dataset.path.parent / (paper.student_pdf or "")).resolve()
        scheme = (dataset.path.parent / (paper.scheme_pdf or "")).resolve()
        if not student.is_file() or not scheme.is_file():
            raise RuntimeError(
                f"{paper.id}: student/scheme PDF missing ({student}, {scheme})")
        with tempfile.TemporaryDirectory(prefix="mc5-benchmark-") as tmp:
            expected = Path(tmp) / f"{paper.id}-expected.json"
            outcome_path = Path(tmp) / f"{paper.id}-outcome.json"
            report = Path(tmp) / f"{paper.id}-harness.txt"
            _write_expected(expected, dataset, paper, items)
            command = [
                sys.executable, str(LIVE_HARNESS), "--live",
                "--student-pdf", str(student),
                "--scheme-pdf", str(scheme),
                "--expected", str(expected),
                "--outcome-json", str(outcome_path),
                "--report", str(report),
                "--timeout", str(args.timeout),
            ]
            if args.data_dir:
                command += ["--data-dir", args.data_dir]
            if args.key_file:
                command += ["--key-file", args.key_file]
            completed = subprocess.run(
                command, capture_output=True, text=True,
                encoding="utf-8", errors="replace",
                timeout=args.timeout + 300, cwd=REPO)
            if not outcome_path.is_file():
                tail = (completed.stderr or completed.stdout or "")[-1200:]
                raise RuntimeError(
                    f"{paper.id}: harness exited {completed.returncode} without "
                    f"an outcome. {tail}")
            outcome = json.loads(outcome_path.read_text(encoding="utf-8"))
            outcome.setdefault("status", "ok")
            outcome["harness_exit_code"] = completed.returncode
            return outcome
    return correct


def _replay_corrector(paths: dict[str, Path]):
    def correct(paper, items):
        path = paths.get(paper.id)
        if path is None:
            raise RuntimeError(
                f"{paper.id}: no replay outcome supplied (use --replay "
                f"{paper.id}=PATH)")
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        payload.setdefault("status", "ok")
        payload["replay_source"] = str(path)
        return payload
    return correct


def _stub_corrector():
    """Deterministic infrastructure check: awards exactly the reference mark."""
    def correct(paper, items):
        return {
            "status": "ok",
            "questions": {item.question: {"awarded": item.scheme_mark,
                                          "max": item.max_marks}
                          for item in items},
            "earned": sum(item.scheme_mark for item in items),
            "available": sum(item.max_marks for item in items),
            "review_required": False,
            "stub": True,
        }
    return correct


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, default=None)
    parser.add_argument("--partition", choices=("all", "development", "holdout"),
                        default="all")
    parser.add_argument("--mode", choices=("live", "replay", "stub"),
                        default="live")
    parser.add_argument("--replay", action="append", default=[],
                        metavar="PAPER_ID=PATH")
    parser.add_argument("--ack", default="")
    parser.add_argument("--allow-unverified", action="store_true",
                        help="measure scheme-derived references (never certified)")
    parser.add_argument("--model", default="")
    parser.add_argument("--prompt-version", default="not recorded")
    parser.add_argument("--run-id", default=None)
    parser.add_argument("--out", type=Path, default=None)
    parser.add_argument("--data-dir", default=None)
    parser.add_argument("--key-file", default=None)
    parser.add_argument("--timeout", type=int, default=900)
    args = parser.parse_args()

    try:
        dataset = load_dataset(args.dataset)
    except DatasetError as exc:
        print(f"REFUSED: {exc}")
        return EXIT_REFUSED
    try:
        store = VerificationStore()
        resolved = resolve_references(dataset, store)
    except VerificationError as exc:
        print(f"REFUSED: {exc}")
        return EXIT_REFUSED

    summary = resolution_summary(dataset, resolved)
    print(f"dataset   : version {dataset.version} sha256 {dataset.sha256[:16]}")
    print(f"references: {summary['total_references']} total, "
          f"{summary['eligible']} teacher-verified")
    print(f"partition : {args.partition}")

    if summary["eligible"] == 0 and not args.allow_unverified:
        print("REFUSED: no teacher-verified references are available. No provider "
              "request was made and no certified score can be produced.")
        print("A teacher must confirm reference marks first (see the Accuracy "
              "page). --allow-unverified measures against the printed scheme but "
              "is never certified.")
        return EXIT_UNAVAILABLE
    if args.mode == "live" and args.ack != ACK_PHRASE:
        print(f"REFUSED: live mode requires --ack {ACK_PHRASE}. No request made.")
        return EXIT_REFUSED

    if args.mode == "live":
        corrector = _live_corrector(dataset, args)
    elif args.mode == "replay":
        paths: dict[str, Path] = {}
        for entry in args.replay:
            if "=" not in entry:
                print(f"REFUSED: --replay expects PAPER_ID=PATH, got {entry!r}")
                return EXIT_REFUSED
            paper_id, raw = entry.split("=", 1)
            path = Path(raw)
            if not path.is_absolute():
                path = (REPO / path).resolve()
            if not path.is_file():
                print(f"REFUSED: replay outcome not found: {path}")
                return EXIT_REFUSED
            paths[paper_id] = path
        corrector = _replay_corrector(paths)
    else:
        corrector = _stub_corrector()

    config = _config_snapshot()
    if upstream_model := _upstream_model(args, config):
        model = upstream_model
    elif args.mode == "stub":
        model = "deterministic-stub (no provider)"
    elif args.mode == "replay":
        model = "archived-provider-outcome (replay)"
    else:
        model = "not recorded"
    record = execute_benchmark(
        dataset, resolved, corrector,
        partition=args.partition,
        include_unverified=args.allow_unverified,
        model=model,
        prompt_version=args.prompt_version,
        config=config,
        output_dir=args.out or (data_dir() / "benchmark" / "runs"),
        run_id=args.run_id,
        mode=args.mode,
        verification_sha256=store.snapshot_sha256(dataset),
    )

    metrics = record["metrics"]
    agreement = metrics["agreement"]
    print(f"run       : {record['run_id']}  status={record['status']}  "
          f"certified={record['certified']}")
    print(f"items     : {agreement['items']['exact']}/"
          f"{agreement['items']['compared']} exact "
          f"({agreement['items']['rate']})")
    print(f"questions : {agreement['questions']['exact']}/"
          f"{agreement['questions']['compared']}")
    print(f"subquest. : {agreement['subquestions']['exact']}/"
          f"{agreement['subquestions']['compared']}")
    print(f"totals    : {agreement['total']['exact']}/"
          f"{agreement['total']['compared']}")
    print(f"MAE       : {metrics['error']['mean_absolute_error']}")
    print(f"coverage  : missing={metrics['coverage']['missing_mark_count']} "
          f"invalid={metrics['coverage']['invalid_result_count']} "
          f"failed_scripts={metrics['coverage']['scripts_failed']}")
    print(f"report    : {record.get('artifacts', {}).get('report_markdown', '-')}")
    if record["certified"]:
        return EXIT_CERTIFIED
    if record["status"] in {"incomplete", "not_eligible"}:
        return EXIT_NOT_CERTIFIED
    return EXIT_NOT_CERTIFIED


if __name__ == "__main__":
    sys.exit(main())
