#!/usr/bin/env python3
"""Measure candidate Gemini models on what a sweep can actually prove.

    python backend/scripts/sweep_models.py --ack INFERENCE

What IS measured
----------------
* access      — a real ``generateContent`` request from this installation
                succeeded (the only thing that proves a model is callable;
                appearing in the model listing proves nothing).
* latency     — one timed request per probe, reported as a single sample.
* tokens      — the provider's own ``usage_metadata``, never an estimate.
* requests    — the ledger rows the request produced, so a hidden SDK retry
                would show up as more than one row per probe.
* schema      — whether a ``response_schema`` object came back as JSON that
                validates against that schema.

What is NOT measured
--------------------
Marking accuracy. The benchmark refuses to score without teacher-certified
references (``benchmark_accuracy.py`` exits 4), so this sweep makes no claim
about which model marks IGCSE papers better. A cheap model that answers a
one-line prompt quickly may still mark worse; nothing here says otherwise.

The sweep runs through :class:`KeyAwareClient`, so the rows it produces are
ordinary production traffic: the same path a correction takes, including the
usage ledger, the model registry and the request budget.
"""

from __future__ import annotations

import argparse
import json
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

from app.gemini import usage as usage_mod  # noqa: E402

ACK_PHRASE = "INFERENCE"

#: One probe may hold the sweep for this long and no longer. The marking path
#: allows 180 s per request because a real paper sends dozens of page images;
#: these probes are one line of text, so a minute is already generous and a
#: stalled model must not eat the whole sweep's budget.
PROBE_TIMEOUT_MS = 90_000

#: Models worth comparing for marking, with the reason each is here. Every one
#: is a text-generation model this account's listing advertises; listing alone
#: is why they are CANDIDATES and not conclusions.
CANDIDATES: tuple[tuple[str, str], ...] = (
    ("gemini-3.6-flash", "current production baseline"),
    ("gemini-3.7-flash", "next flash in the same family"),
    ("gemini-3.8-flash", "latest flash in the same family"),
    ("gemini-4-argon", "newest general model in the listing"),
    ("gemini-3.5-flash", "one generation behind the baseline"),
    ("gemini-3.5-flash-lite", "lighter flash: candidate for cheap stages"),
    ("gemini-3.1-flash-lite", "oldest lite model still listed"),
    ("gemini-3.1-pro-preview", "pro comparator: accuracy ceiling, higher cost"),
)

_TEXT_PROMPT = "Reply with exactly the two characters OK and nothing else."

#: The same OpenAPI-subset shape the marking path sends (a plain dict, as
#: ``legacy_marking`` does), small enough to cost a handful of tokens.
_MARK_SCHEMA = {
    "type": "object",
    "properties": {
        "mark": {"type": "integer"},
        "reason": {"type": "string"},
    },
    "required": ["mark", "reason"],
}
_MARK_PROMPT = (
    "A mark scheme awards 1 mark for a correct method and 1 mark for the "
    "correct answer (maximum 2). The student wrote: '7 x 8 = 54'. "
    "Award a mark out of 2 and give one short reason."
)


def _text_of(response: object) -> str:
    try:
        value = getattr(response, "text", None)
    except Exception:  # noqa: BLE001
        return ""
    return str(value or "").strip()


def _schema_verdict(text: str) -> tuple[bool, str]:
    """Validate the reply against ``_MARK_SCHEMA`` without a JSON-schema lib."""
    try:
        payload = json.loads(text)
    except ValueError:
        return False, "reply was not JSON"
    if not isinstance(payload, dict):
        return False, "reply was not an object"
    mark = payload.get("mark")
    if isinstance(mark, bool) or not isinstance(mark, int):
        return False, f"mark was {type(mark).__name__}, not integer"
    if not 0 <= mark <= 2:
        return False, f"mark {mark} outside 0..2"
    reason = payload.get("reason")
    if not isinstance(reason, str) or not reason.strip():
        return False, "reason missing or empty"
    return True, "valid"


def _probe(client, model: str, *, prompt: str, config, expect_json: bool) -> dict:
    started = time.perf_counter()
    try:
        response = client.models.generate_content(
            model=model, contents=prompt, config=config)
    except Exception as exc:  # noqa: BLE001 — a probe failure is a datapoint
        return {
            "ok": False,
            "error": f"{type(exc).__name__}: {exc}"[:300],
            "latency_ms": int((time.perf_counter() - started) * 1000),
            "tokens": None,
            "schema_valid": None,
            "schema_note": "",
            "reply_chars": 0,
        }
    elapsed = int((time.perf_counter() - started) * 1000)
    text = _text_of(response)
    parsed = usage_mod.from_response(response)
    valid, note = (_schema_verdict(text) if expect_json else (None, ""))
    return {
        "ok": True,
        "error": "",
        "latency_ms": elapsed,
        "tokens": parsed.to_dict() if parsed else None,
        "schema_valid": valid,
        "schema_note": note,
        "reply_chars": len(text),
    }


def _ledger_rows_since(moment: float, model: str) -> dict:
    """What the production ledger recorded for this model since ``moment``."""
    try:
        from app import db
        row = db.query_one(
            "SELECT COUNT(*) AS rows, "
            "SUM(CASE WHEN error_class IS NULL THEN 1 ELSE 0 END) AS ok, "
            "SUM(CASE WHEN error_class IS NOT NULL THEN 1 ELSE 0 END) AS failed, "
            "SUM(CASE WHEN input_tokens IS NULL THEN 1 ELSE 0 END) AS no_tokens "
            "FROM ai_request_log WHERE created_at >= ? AND model = ?",
            (moment, model))
        if not row:
            return {"rows": 0, "ok": 0, "failed": 0, "rows_without_tokens": 0}
        return {
            "rows": int(row["rows"] or 0),
            "ok": int(row["ok"] or 0),
            "failed": int(row["failed"] or 0),
            "rows_without_tokens": int(row["no_tokens"] or 0),
        }
    except Exception as exc:  # noqa: BLE001 — the ledger is not a dependency
        return {"error": f"{type(exc).__name__}: {exc}"[:200]}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", action="append", default=[],
                        help="model id to sweep (repeatable; default: the "
                             "built-in candidate list)")
    parser.add_argument("--ack", default="",
                        help=f"must be {ACK_PHRASE} — real requests cost quota")
    parser.add_argument("--out", type=Path, default=None)
    parser.add_argument("--probe-timeout", type=int, default=PROBE_TIMEOUT_MS // 1000)
    args = parser.parse_args()

    if args.ack != ACK_PHRASE:
        print(f"REFUSED: the sweep sends real requests; pass --ack {ACK_PHRASE}.")
        return 2

    from app.config import get_config
    from app.gemini.client_proxy import KeyAwareClient
    from app.gemini.key_manager import KeyManager

    from app import db

    # The ledger table ships in schema.sql and is created at application start;
    # a standalone sweep must do the same or its request counts would be read
    # from a table that does not exist yet.
    db.init_db()

    cfg = get_config()
    if cfg.mock_mode:
        print("REFUSED: mock mode is on, so a sweep would measure nothing. "
              "Turn it off first.")
        return 2
    if not getattr(cfg.gemini, "usage_telemetry", True):
        print("REFUSED: usage telemetry is disabled, so request and token "
              "counts could not be checked against the ledger.")
        return 2

    from google.genai import types  # local import: mock mode never gets here

    # Both probes cap their own duration and pin one attempt, so "requests" in
    # this artifact means requests actually sent, not attempts the SDK made.
    http_options = types.HttpOptions(
        timeout=args.probe_timeout * 1000,
        retry_options=types.HttpRetryOptions(attempts=1))

    targets = [(m, "named on the command line") for m in args.model] \
        if args.model else list(CANDIDATES)
    print(f"sweeping {len(targets)} model(s), 2 requests each:\n")

    client = KeyAwareClient(KeyManager(), pages=0)
    results: list[dict] = []
    sweep_started = time.time()

    for model, why in targets:
        started = time.time()
        print(f"  {model} ({why})")
        plain = _probe(client, model, prompt=_TEXT_PROMPT,
                       config=types.GenerateContentConfig(
                           temperature=0, http_options=http_options),
                       expect_json=False)
        if plain["ok"]:
            marked = _probe(
                client, model, prompt=_MARK_PROMPT,
                config=types.GenerateContentConfig(
                    temperature=0,
                    response_mime_type="application/json",
                    response_schema=_MARK_SCHEMA,
                    http_options=http_options),
                expect_json=True)
        else:
            # Access already failed. A second request would only re-prove that,
            # so the schema question stays genuinely untested rather than
            # being answered "false" for a model that never replied.
            marked = {
                "ok": False,
                "error": "not attempted: the plain probe failed",
                "latency_ms": None,
                "tokens": None,
                "schema_valid": None,
                "schema_note": "not tested (no reply)",
                "reply_chars": 0,
            }
        ledger = _ledger_rows_since(started, model)
        entry = {
            "model": model,
            "why_candidate": why,
            "access": ("verified" if plain["ok"] and marked["ok"]
                       else "failed"),
            "error": plain["error"] or marked["error"],
            "plain_probe": plain,
            "schema_probe": marked,
            "ledger": ledger,
            "accuracy": "not measured (no teacher-certified references)",
        }
        results.append(entry)
        state = "ok" if entry["access"] == "verified" else "FAILED"
        latency = marked["latency_ms"] or plain["latency_ms"]
        tokens = (plain["tokens"] or {}).get("total_tokens")
        print(f"    {state}  schema={marked['schema_valid']}  "
              f"mark-latency={latency}ms  plain-total-tokens={tokens}  "
              f"ledger-rows={ledger.get('rows', '?')}")
        if entry["error"]:
            print(f"    error: {entry['error']}")

    verified = [r for r in results if r["access"] == "verified"]
    artifact = {
        "swept_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(sweep_started)),
        "probe_timeout_ms": args.probe_timeout * 1000,
        "requests_if_both_probes_ran": 2,
        "models_swept": len(results),
        "models_verified": len(verified),
        "production_model": (list(cfg.gemini.primary_models or []) or [None])[0],
        "environment": {
            "python": sys.version.split()[0],
            "repo": str(REPO),
        },
        "results": results,
        "not_measured": [
            "Marking accuracy: no teacher-certified references are available, "
            "so no model can be compared on accuracy. See "
            "docs/results/TEACHER_CERTIFIED_ACCURACY_REPORT.md.",
            "Multi-page behaviour: the probes are one line of text, not a "
            "15-page script plus a 26-page scheme, so per-page latency and "
            "long-context handling are not covered.",
            "Cost: token counts are reported, prices are not, so no currency "
            "figure is implied.",
        ],
    }
    out = args.out or (REPO / "docs" / "results" / "MODEL_SWEEP_RESULTS.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(artifact, indent=2) + "\n", encoding="utf-8")

    print(f"\nverified {len(verified)}/{len(results)} · "
          f"production model: {artifact['production_model']}")
    print(f"accuracy: NOT measured — see the note in the artifact.")
    print(f"written: {out}")
    return 0 if verified else 1


if __name__ == "__main__":
    sys.exit(main())
