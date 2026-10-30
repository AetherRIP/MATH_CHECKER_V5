#!/usr/bin/env python3
"""Multi-paper marking benchmark: does the faster configuration stay correct?

    python backend/scripts/benchmark_hardening.py --ack INFERENCE --dry-run
    python backend/scripts/benchmark_hardening.py --ack INFERENCE --papers 5

Why this exists
---------------
``benchmark_marking_config.py`` compares configurations on the repository's
reference set (two short, scheme-derived papers). That answers "do the marks
move on a synthetic paper", not "is this faster on a real 30-page scan". This
script runs the SAME papers through two (or more) configurations with identical
prompt, schema, sampling and model, so the only difference is the arm:

* ``production``   — exactly what the application sends today.
* ``no-thinking``  — the same request with ``thinking_budget = 0``.

Method rules, so the numbers mean something
-------------------------------------------
* identical inputs per paper, one arm at a time, **order randomised** per paper
  (a provider that gets faster or slower during the run hits both arms).
* every outcome is recorded: a refusal, a timeout and a 503 are data, not
  something to delete. An arm that fails is never silently dropped.
* tokens come from the provider's own ``usage_metadata``; a value the provider
  omits stays ``null`` instead of being invented.
* accuracy is judged *between arms* on the same paper (question-level agreement)
  and, where a reference exists, against it. No teacher has certified these
  papers, so nothing here is called certified accuracy.
* privacy: papers are identified as ``paper-1``… with a SHA-256 of their bytes.
  File names (which contain student names) are never written to the artefact.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import random
import statistics
import sys
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

ACK_PHRASE = "INFERENCE"

ARMS: dict[str, dict] = {
    "production": {},
    "no-thinking": {"thinking_config": {"thinking_budget": 0}},
}

#: The real papers this installation actually marks, chosen for a spread of
#: sizes. Kept as a list of PATHS only; the artefact records an anonymous label
#: and a content hash, never the file name.
DEFAULT_PAPERS = [
    ("data/audit_downloads/Eyad_p2_1Wjy9iYW35.pdf", "short"),
    ("data/audit_downloads/Asser_Tamer_scan.pdf", "medium"),
    ("data/fixtures/real/Student Answer.pdf", "medium"),
    ("data/audit_downloads/Eyad_p3_1ZUmBinAKp.pdf", "long"),
    ("data/audit_downloads/Ali AlHakeem__1Roaia1r2R__CamScanner 02-10-2026 19.59.pdf",
     "long"),
    ("data/audit_downloads/Hala Marzouk__1QcGPZT7P2__Hala Marzouk - 10A - Ch1L9 - Ratio.pdf",
     "long"),
]

DEFAULT_SCHEME = "data/mark_schemes/891651209745.pdf"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _pages(path: Path) -> int:
    from app.engine.pdf_inspect import page_images
    try:
        return int(page_images(path))
    except Exception:  # noqa: BLE001 — a size label must never stop a benchmark
        return 0


def _questions(payload: dict, legacy) -> dict:
    found: dict[str, dict] = {}
    for row in payload.get("questions") or []:
        if not isinstance(row, dict):
            continue
        number = str(row.get("question_number") or "").strip()
        if not number:
            continue
        found[number] = {
            "awarded": legacy.clamp_mark(row.get("awarded_marks"),
                                         row.get("max_marks") or 0),
            "max": int(row.get("max_marks") or 0),
        }
    return found


def _wait_for_capacity(model: str, max_seconds: float) -> float:
    """Block until the model gate opens, and RETURN the time waited.

    Waiting here is not a retry: no request is sent while the provider is
    known to be shedding work, which is exactly what the application does. The
    wait is recorded separately and never counted as call time, so a healthy
    call is never reported as slow.
    """
    from app.gemini.provider_health import provider_health
    waited = 0.0
    while waited < max_seconds:
        allowed, remaining = provider_health().available(model)
        if allowed:
            return waited
        pause = min(max(5.0, remaining + 2.0), max_seconds - waited)
        print(f"  provider cooling for {model}: waiting {pause:.0f}s"
              f" (recorded separately from call time)", flush=True)
        time.sleep(pause)
        waited += pause
    return waited


def _run_arm(client, legacy, arm: str, model: str, student: Path, scheme: Path,
             pages: int, timeout_s: float,
             capacity_wait_s: float = 600.0) -> dict:
    """One paper through one configuration. Never raises: failure is data."""
    extra = ARMS[arm]
    started = time.time()
    capacity_waited = _wait_for_capacity(model, capacity_wait_s)
    result: dict = {
        "arm": arm,
        "settings": extra,
        "model": model,
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(started)),
        "capacity_wait_s": round(capacity_waited, 1),
        "upload_s": None,
        "call_s": None,
        "wall_s": None,
        "status": "error",
        "error": None,
        "tokens": None,
        "questions_detected": 0,
        "marks_awarded": None,
        "questions": {},
    }
    try:
        remote_student = client.files.upload(file=str(student))
        remote_scheme = client.files.upload(file=str(scheme))
        uploaded = time.time()
        result["upload_s"] = round(uploaded - started, 1)

        request_started = time.time()
        response = client.models.generate_content(
            model=model,
            contents=[remote_student, remote_scheme,
                      legacy.marking_user_prompt(None)],
            config={
                "system_instruction": legacy.effective_system_prompt(),
                "response_mime_type": "application/json",
                "response_schema": legacy.OUTPUT_SCHEMA,
                **legacy.DETERMINISTIC_SAMPLING,
                **extra,
            },
        )
        called = time.time()
        result["call_s"] = round(called - request_started, 1)
        if called - request_started > timeout_s:      # recorded, never dropped
            result["over_deadline_s"] = round(called - request_started - timeout_s, 1)

        from app.gemini import usage as usage_mod
        used = usage_mod.from_response(response)
        result["tokens"] = used.to_dict() if used else None

        payload = legacy.extract_json((getattr(response, "text", "") or ""))
        questions = _questions(payload, legacy)
        result["questions"] = questions
        result["questions_detected"] = len(questions)
        result["marks_awarded"] = sum(q["awarded"] for q in questions.values())
        result["marks_available"] = sum(q["max"] for q in questions.values())
        result["status"] = "ok" if questions else "empty"
    except Exception as exc:  # noqa: BLE001 — an outage is a measurement
        result["error"] = f"{type(exc).__name__}: {str(exc)[:400]}"
    finally:
        result["wall_s"] = round(time.time() - started, 1)
    return result


def _compare(records: list[dict], arm_a: str, arm_b: str) -> dict:
    """Question-level agreement between two arms on the same paper."""
    by_paper: dict[str, dict[str, dict]] = {}
    for record in records:
        if record["status"] != "ok":
            continue
        by_paper.setdefault(record["paper"], {})[record["arm"]] = \
            record["questions"]
    compared = exact = 0
    deltas = []
    disagreements = []
    for paper, arms in by_paper.items():
        a, b = arms.get(arm_a), arms.get(arm_b)
        if not a or not b:
            continue
        for number in sorted(set(a) & set(b)):
            compared += 1
            delta = b[number]["awarded"] - a[number]["awarded"]
            deltas.append(abs(delta))
            if delta == 0:
                exact += 1
            else:
                disagreements.append({
                    "paper": paper, "question": number,
                    f"{arm_a}": a[number]["awarded"],
                    f"{arm_b}": b[number]["awarded"], "delta": delta,
                })
    return {
        "arm_a": arm_a,
        "arm_b": arm_b,
        "questions_compared": compared,
        "questions_exact": exact,
        "exact_rate": round(exact / compared, 4) if compared else None,
        "mean_absolute_error": (round(sum(deltas) / len(deltas), 4)
                                if deltas else None),
        "disagreements": disagreements,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ack", default="")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--papers", type=int, default=5,
                        help="how many papers to run (default 5)")
    parser.add_argument("--arm", action="append", default=[],
                        choices=sorted(ARMS),
                        help="arm to run (repeatable; default: all)")
    parser.add_argument("--model", default=None)
    parser.add_argument("--scheme", type=Path, default=REPO / DEFAULT_SCHEME)
    parser.add_argument("--seed", type=int, default=None,
                        help="make the arm order reproducible")
    parser.add_argument("--timeout-s", type=float, default=600.0,
                        help="record (do not enforce) calls slower than this")
    parser.add_argument("--capacity-wait-s", type=float, default=600.0,
                        help="wait this long for a provider cooldown to lift "
                             "before recording a refusal (default 600)")
    parser.add_argument("--pause-s", type=float, default=20.0,
                        help="pause between papers (default 20)")
    parser.add_argument("--out", type=Path,
                        default=REPO / "docs" / "results" / "PERFORMANCE_BENCHMARK.json")
    args = parser.parse_args()

    papers = [(REPO / path, label) for path, label in DEFAULT_PAPERS
              if (REPO / path).is_file()][:args.papers]
    arms = args.arm or list(ARMS)
    if not papers:
        print("REFUSED: none of the benchmark papers exist on this machine.")
        return 2
    if not args.scheme.is_file():
        print(f"REFUSED: mark scheme not found: {args.scheme}")
        return 2

    print(f"papers : {len(papers)}")
    for index, (path, label) in enumerate(papers, start=1):
        print(f"  paper-{index}: {label}, {_pages(path)} pages, "
              f"{path.stat().st_size // 1024} KB")
    print(f"scheme : {_pages(args.scheme)} pages")
    print(f"arms   : {', '.join(arms)}")

    if args.dry_run:
        print("\ndry run: nothing sent.")
        return 0
    if args.ack != ACK_PHRASE:
        print(f"\nREFUSED: this marks real papers; pass --ack {ACK_PHRASE}.")
        return 2

    from app import db
    from app.config import get_config
    from app.engine import legacy_marking as legacy
    from app.gemini import telemetry
    from app.gemini import call_audit
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

    rng = random.Random(args.seed)
    client = KeyAwareClient(KeyManager(), job_id="hardening-benchmark")
    scheme_pages = _pages(args.scheme)
    records: list[dict] = []

    for index, (path, label) in enumerate(papers, start=1):
        paper_id = f"paper-{index}"
        pages = _pages(path) + scheme_pages
        order = list(arms)
        rng.shuffle(order)
        print(f"\n{paper_id} ({label}, {pages} pages total) — order: "
              f"{' then '.join(order)}", flush=True)
        for arm in order:
            call_audit.begin(f"{paper_id}-{arm}")
            record = _run_arm(client, legacy, arm, model, path, args.scheme,
                              pages, args.timeout_s, args.capacity_wait_s)
            snapshot = call_audit.snapshot()
            record.update({
                "paper": paper_id,
                "paper_class": label,
                "paper_pages": pages,
                "paper_sha256": _sha256(path),
                "requests": (snapshot or {}).get("requests"),
                "requests_total": (snapshot or {}).get("total_requests"),
                "requests_by_kind": (snapshot or {}).get("by_kind"),
            })
            records.append(record)
            line = (f"  {arm:12s} {record['status']:5s} "
                    f"call {record['call_s']}s wall {record['wall_s']}s "
                    f"questions {record['questions_detected']} "
                    f"marks {record['marks_awarded']}")
            if record["error"]:
                line += f"  ERROR {record['error'][:120]}"
            print(line, flush=True)
        if args.pause_s > 0:
            time.sleep(args.pause_s)

    def _arm_summary(arm: str) -> dict:
        rows = [r for r in records if r["arm"] == arm]
        ok = [r for r in rows if r["status"] == "ok"]
        calls = [r["call_s"] for r in ok if r["call_s"] is not None]
        tokens = [r["tokens"] for r in ok if r["tokens"]]
        return {
            "arm": arm,
            "settings": ARMS[arm],
            "papers_attempted": len(rows),
            "papers_ok": len(ok),
            "papers_failed": len(rows) - len(ok),
            "call_seconds": calls,
            "call_median_s": round(statistics.median(calls), 1) if calls else None,
            "call_total_s": round(sum(calls), 1) if calls else None,
            "input_tokens": sum((t.get("input_tokens") or 0) for t in tokens),
            "output_tokens": sum((t.get("output_tokens") or 0) for t in tokens),
            "thoughts_tokens": sum((t.get("thoughts_tokens") or 0) for t in tokens),
            "tokens_measured": len(tokens),
        }

    summary = {
        "measured_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "model": model,
        "certified": False,
        "certification_note": (
            "No teacher has certified these papers. Marks are compared between "
            "configurations on identical input; they are not an accuracy claim "
            "against a marking scheme."),
        "method": {
            "prompt_version": telemetry.text_version(
                legacy.marking_user_prompt(None)) or "not recorded",
            "sampling": legacy.DETERMINISTIC_SAMPLING,
            "scheme_pages": scheme_pages,
            "arm_order": "randomised per paper",
            "seed": args.seed,
        },
        "arms": [_arm_summary(arm) for arm in arms],
        "records": records,
    }
    if len(arms) > 1:
        summary["comparisons"] = [_compare(records, arms[0], arms[1])]

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")

    print("\nsummary")
    for arm in summary["arms"]:
        print(f"  {arm['arm']:12s} ok {arm['papers_ok']}/{arm['papers_attempted']}"
              f"  median call {arm['call_median_s']}s"
              f"  thoughts {arm['thoughts_tokens']}")
    for comparison in summary.get("comparisons", []):
        print(f"  agreement {comparison['arm_a']} vs {comparison['arm_b']}: "
              f"{comparison['questions_exact']}/"
              f"{comparison['questions_compared']} questions, MAE "
              f"{comparison['mean_absolute_error']}")
    print(f"\nwritten: {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
