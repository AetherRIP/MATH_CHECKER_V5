#!/usr/bin/env python3
"""Compare marking configurations on the same papers, and report honestly.

    python backend/scripts/benchmark_marking_config.py --ack INFERENCE
    python backend/scripts/benchmark_marking_config.py --ack INFERENCE \
        --config default --config no-thinking

Why this exists
---------------
Recovering throughput is only acceptable if the marks do not move. Thinking
tokens are the largest controllable cost of a marking call (measured
2026-10-30: 149 reasoning tokens to answer "17 x 23"), and disabling them is a
one-line change — so the decision needs a measurement, not an opinion.

Each configuration marks the SAME reference papers with the SAME prompt,
schema, sampling and model. Only the thinking setting differs, so a difference
in marks or duration is attributable to it. Runs go through
``app.benchmark.runner.execute_benchmark``, so every arm produces the same
reproducible artefact (dataset hash, per-paper outcomes, metrics, limitations)
that the accuracy harness produces.

What this does NOT do
---------------------
It does not certify accuracy. The reference marks are scheme-derived and no
teacher has confirmed them (``review.verified_by`` is null), so every run is
reported as NOT CERTIFIED and the agreement figure means "agrees with the
printed scheme", not "a teacher agreed".
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import tempfile
import time
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
                                 resolve_references)
from app.config import get_config  # noqa: E402

ACK_PHRASE = "INFERENCE"

#: One entry per comparison arm. "default" is exactly what production sends.
CONFIGS: dict[str, dict] = {
    "default": {},
    "no-thinking": {"thinking_config": {"thinking_budget": 0}},
    "low-thinking": {"thinking_config": {"thinking_budget": 1024}},
}


def _corrector(client, legacy, arm: str, model: str, timings: list[dict],
               base: Path):
    """A corrector that marks one paper and records what it cost.

    ``base`` is the dataset's directory: reference items store their PDF names
    relative to it, exactly as the accuracy harness resolves them.
    """
    extra = CONFIGS[arm]
    prompt = legacy.marking_user_prompt(None)
    system = legacy.effective_system_prompt()

    def correct(paper, items):
        student = (base / (paper.student_pdf or "")).resolve()
        scheme = (base / (paper.scheme_pdf or "")).resolve()
        if not student.is_file() or not scheme.is_file():
            raise RuntimeError(f"{paper.id}: reference PDFs missing")
        stamp = time.time()
        remote_student = client.files.upload(file=str(student))
        remote_scheme = client.files.upload(file=str(scheme))
        uploaded = time.time()
        response = client.models.generate_content(
            model=model,
            contents=[remote_student, remote_scheme, prompt],
            config={
                "system_instruction": system,
                "response_mime_type": "application/json",
                "response_schema": legacy.OUTPUT_SCHEMA,
                **legacy.DETERMINISTIC_SAMPLING,
                **extra,
            },
        )
        called = time.time()
        from app.gemini import usage as usage_mod
        used = usage_mod.from_response(response)
        text = getattr(response, "text", "") or ""
        payload = legacy.extract_json(text)
        questions = {}
        for row in payload.get("questions") or []:
            if not isinstance(row, dict):
                continue
            number = str(row.get("question_number") or "").strip()
            awarded = legacy.clamp_mark(row.get("awarded_marks"),
                                        row.get("max_marks") or 0)
            if number:
                questions[number] = {"awarded": awarded,
                                     "max": int(row.get("max_marks") or 0)}
        timings.append({
            "paper_id": paper.id,
            "upload_s": round(uploaded - stamp, 1),
            "call_s": round(called - uploaded, 1),
            "tokens": used.to_dict() if used else None,
        })
        return {
            "status": "ok",
            "questions": questions,
            "earned": sum(q["awarded"] for q in questions.values()),
            "available": sum(q["max"] for q in questions.values()),
            "review_required": False,
        }
    return correct


def _summarise(record: dict, timings: list[dict], arm: str) -> dict:
    agreement = record["metrics"]["agreement"]
    calls = [t["call_s"] for t in timings]
    thoughts = [((t["tokens"] or {}).get("thoughts_tokens") or 0) for t in timings]
    return {
        "config": arm,
        "settings": CONFIGS[arm],
        "certified": record["certified"],
        "certification": record["certification"]["reason"],
        "items_exact": agreement["items"]["exact"],
        "items_compared": agreement["items"]["compared"],
        "items_rate": agreement["items"]["rate"],
        "questions_exact": agreement["questions"]["exact"],
        "questions_compared": agreement["questions"]["compared"],
        "mean_absolute_error": record["metrics"]["error"]["mean_absolute_error"],
        "missing_marks": record["metrics"]["coverage"]["missing_mark_count"],
        "call_seconds": calls,
        "call_median_s": round(statistics.median(calls), 1) if calls else None,
        "call_total_s": round(sum(calls), 1),
        "thoughts_tokens": thoughts,
        "thoughts_total": sum(thoughts),
        "upload_seconds": round(sum(t["upload_s"] for t in timings), 1),
        "run_id": record["run_id"],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", action="append", default=[],
                        choices=sorted(CONFIGS),
                        help="comparison arm (repeatable; default: all)")
    parser.add_argument("--model", default=None,
                        help="marking model (default: the configured primary)")
    parser.add_argument("--dataset", type=Path, default=None)
    parser.add_argument("--ack", default="")
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args()

    if args.ack != ACK_PHRASE:
        print(f"REFUSED: this marks real papers; pass --ack {ACK_PHRASE}.")
        return 2

    from app import db
    from app.engine import legacy_marking as legacy
    from app.gemini import model_registry, telemetry
    from app.gemini.client_proxy import KeyAwareClient
    from app.gemini.key_manager import KeyManager

    cfg = get_config()
    if cfg.mock_mode:
        print("REFUSED: mock mode is on; a benchmark there measures nothing.")
        return 2
    db.init_db()

    model = args.model or (list(cfg.gemini.primary_models or [None])[0])
    if not model:
        print("REFUSED: no marking model is configured.")
        return 2
    arms = args.config or list(CONFIGS)

    try:
        dataset = load_dataset(args.dataset)
        store = VerificationStore()
        resolved = resolve_references(dataset, store)
    except (DatasetError, VerificationError) as exc:
        print(f"REFUSED: {exc}")
        return 2

    print(f"dataset : {dataset.version} ({len(dataset.items)} items, "
          f"{len(dataset.papers)} papers)")
    print(f"model   : {model}")
    print(f"arms    : {', '.join(arms)}")
    print("marks   : reference marks are SCHEME-DERIVED and not teacher-"
          "certified; no arm can claim certified accuracy.\n")

    client = KeyAwareClient(KeyManager(), job_id="config-benchmark")
    summary = {
        "measured_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "model": model,
        "dataset": {"version": dataset.version, "sha256": dataset.sha256},
        "certified": False,
        "certification_note": (
            "Reference marks are scheme-derived; review.verified_by is null. "
            "Agreement means agreement with the printed scheme, not a "
            "teacher-certified accuracy figure."),
        "arms": [],
    }

    for arm in arms:
        timings: list[dict] = []
        corrector = _corrector(client, legacy, arm, model, timings,
                               dataset.path.parent)
        with tempfile.TemporaryDirectory(prefix="mc5-config-bench-") as tmp:
            record = execute_benchmark(
                dataset, resolved, corrector,
                partition="all",
                include_unverified=True,   # scheme-derived, never certified
                model=model,
                prompt_version=telemetry.text_version(
                    legacy.marking_user_prompt(None)) or "not recorded",
                config={"model": model, **CONFIGS[arm],
                        "sampling": legacy.DETERMINISTIC_SAMPLING},
                output_dir=Path(tmp),
                mode="config-comparison",
                verification_sha256=store.snapshot_sha256(dataset),
            )
        entry = _summarise(record, timings, arm)
        summary["arms"].append(entry)
        print(f"{arm:14s} items {entry['items_exact']}/{entry['items_compared']} "
              f"({entry['items_rate']})  MAE {entry['mean_absolute_error']}  "
              f"median call {entry['call_median_s']}s  "
              f"thoughts {entry['thoughts_total']}")

    # Honest comparison: the point is whether marks moved, so state it plainly.
    if len(summary["arms"]) > 1:
        first = summary["arms"][0]
        for other in summary["arms"][1:]:
            summary.setdefault("comparisons", []).append({
                "vs": first["config"],
                "config": other["config"],
                "items_exact_delta": (other["items_exact"]
                                      - first["items_exact"]),
                "same_rate": other["items_rate"] == first["items_rate"],
                "median_call_delta_s": (
                    None if other["call_median_s"] is None
                    or first["call_median_s"] is None
                    else round(other["call_median_s"] - first["call_median_s"], 1)),
            })

    out = args.out or (REPO / "docs" / "results"
                       / "MARKING_CONFIG_BENCHMARK.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    print(f"\nwritten: {out}")
    print("NOT CERTIFIED: see the note in the artefact.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
