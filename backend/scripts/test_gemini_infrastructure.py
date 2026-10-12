"""Real staged connectivity test for the Gemini infrastructure.

Runs an actual Gemini request through the V5 KeyAwareClient (so the key
manager records the outcome) and prints live progress. Never prints keys.

Usage:  python scripts/test_gemini_infrastructure.py
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import get_config, get_secrets  # noqa: E402
from app.gemini.client_proxy import KeyAwareClient  # noqa: E402
from app.gemini.tester import KeyManagerHolder  # noqa: E402


def stage(n: int, total: int, name: str, ok: bool, detail: str = "") -> None:
    mark = "PASS" if ok else "FAIL"
    print(f"[{n}/{total}] {name}... {mark}" + (f"  ({detail})" if detail else ""))


def main() -> int:
    total = 6
    print("Gemini API Test")
    print("-" * 40)
    started = time.time()

    # 1. Configuration
    cfg = get_config()
    ok = cfg is not None and not cfg.mock_mode
    stage(1, total, "Loading configuration", ok, f"mock_mode={cfg.mock_mode}")
    if not ok:
        return 1

    # 2. Key present (fingerprint only, never the material)
    keys = get_secrets().gemini_keys()
    stage(2, total, "API key detected", bool(keys), f"{len(keys)} key(s) in secret store")
    if not keys:
        return 1

    # 3. Client construction + authentication (via tester: models.list)
    from app.gemini.tester import test_key
    from app.gemini.key_manager import fingerprint
    fp = fingerprint(keys[0])
    state = test_key(1, keys[0], fp)
    stage(3, total, "Connecting to Gemini", bool(state.auth_ok),
          f"models visible: {'yes' if state.auth_ok else 'no'}")

    # 4. Real generation request through the rotating client
    km = KeyManagerHolder.instance()
    client = KeyAwareClient(km)
    request_started = time.time()
    try:
        response = client.models.generate_content(
            model=cfg.gemini.primary_models[-1],
            contents="What is 17 * 23? Reply with the number only.",
        )
        stage(4, total, "Sending test request", True,
              f"model={cfg.gemini.primary_models[-1]}")
    except Exception as exc:
        stage(4, total, "Sending test request", False, str(exc)[:120])
        print("\nSTATUS: FAILED")
        return 1

    # 5. Response parsing
    text = (response.text or "").strip() if response else ""
    parsed_ok = "391" in text
    stage(5, total, "Parsing response", parsed_ok, f"answer extracted: {text[:20]!r}")

    # 6. Key manager bookkeeping
    rec = km.status()["keys"]
    stage(6, total, "Key manager recorded outcome",
          any(k["fingerprint"] == fp and k["successes"] >= 1 for k in rec))

    elapsed = time.time() - started
    gen_elapsed = time.time() - request_started
    print("-" * 40)
    print(f"STATUS: {'HEALTHY' if (state.auth_ok and parsed_ok) else 'DEGRADED'}")
    print(f"Key: GEMINI_KEY_1 (fingerprint {fp})")
    print(f"Total test time: {elapsed:.2f}s | Generation round-trip: {gen_elapsed:.2f}s")
    return 0 if (state.auth_ok and parsed_ok) else 1


if __name__ == "__main__":
    raise SystemExit(main())
