"""TEMPORARY diagnostic: tiny per-key Gemini probe (Step 3/4).

Sends a 1-token, no-PDF request per key against candidate models and prints
ONLY safe identifiers (key_N). Never prints key material.

Usage:  python backend/scripts/probe_gemini.py [--keys 1,2,3] [--models a,b]
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import google.genai as genai  # noqa: E402

from app.config import get_config, get_secrets  # noqa: E402
from app.errors import classify_gemini_error  # noqa: E402
from app.retry_policy import classify_failure  # noqa: E402

PROMPT = "Return exactly: API_TEST_OK"


def describe(exc: Exception) -> dict:
    status = None
    for attr in ("code", "status_code"):
        value = getattr(exc, attr, None)
        if isinstance(value, int):
            status = value
            break
    info = classify_gemini_error(exc)
    try:
        cls = classify_failure(exc).cls
    except Exception:
        cls = "UNKNOWN_ERROR"
    return {
        "status": status,
        "kind": info["kind"],
        "cls": cls,
        "type": type(exc).__name__,
        "msg": str(exc)[:180].replace("\n", " "),
    }


def probe(key: str, model: str, timeout_ms: int) -> dict:
    client = genai.Client(
        api_key=key,
        http_options={"timeout": timeout_ms},
    )
    started = time.time()
    try:
        resp = client.models.generate_content(
            model=model, contents=PROMPT, config={"max_output_tokens": 5},
        )
        text = (getattr(resp, "text", "") or "").strip()
        return {"ok": True, "latency_ms": int((time.time() - started) * 1000),
                "text": text[:40]}
    except Exception as exc:  # noqa: BLE001
        out = describe(exc)
        out["ok"] = False
        out["latency_ms"] = int((time.time() - started) * 1000)
        return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--keys", default="")
    ap.add_argument("--models", default="")
    ap.add_argument("--delay", type=float, default=3.0)
    args = ap.parse_args()

    cfg = get_config()
    keys = get_secrets().gemini_keys()
    models = [m.strip() for m in args.models.split(",") if m.strip()] \
        or list(cfg.gemini.primary_models)

    if args.keys:
        wanted = [int(x) for x in args.keys.split(",") if x.strip()]
        keys = [keys[i - 1] for i in wanted if 1 <= i <= len(keys)]

    print(f"total configured keys: {len(get_secrets().gemini_keys())}")
    print(f"models under test: {models}\n")

    rows = []
    for i, key in enumerate(keys, 1):
        for model in models:
            r = probe(key, model, cfg.gemini.request_timeout_ms)
            row = {"key_id": f"key_{i}", "model": model, **r}
            rows.append(row)
            if r["ok"]:
                print(f"[{row['key_id']}] model={model} OK "
                      f"latency={r['latency_ms']}ms text={r.get('text')!r}")
            else:
                print(f"[{row['key_id']}] model={model} FAIL "
                      f"status={r['status']} type={r['type']} "
                      f"kind={r['kind']} cls={r['cls']} "
                      f"latency={r['latency_ms']}ms")
                print(f"    msg: {r['msg']}")
            time.sleep(args.delay)

    ok = [r for r in rows if r["ok"]]
    print(f"\nsummary: {len(ok)}/{len(rows)} succeeded")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())