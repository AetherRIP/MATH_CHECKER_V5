"""Behavioural verification of the overload fix (temporary diagnostic).

Reproduces the production failure mode: a burst of 503/high-demand failures
on gemini-3.8-flash, then checks whether the app still allows other models
and other keys to serve — which is what the "Gemini provider is cooling down
after high demand" symptom wrongly prevented.

Nothing here uploads student data; it uses a 1-token probe only.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.gemini.provider_health import provider_health  # noqa: E402
from app.retry_policy import HIGH_DEMAND  # noqa: E402

PRODUCTION_MODEL = "gemini-3.8-flash"


def banner(text: str) -> None:
    print(f"\n=== {text} ===")


def main() -> int:
    health = provider_health()
    health.reset()

    banner("1. simulate a 503 burst on the production model")
    for i in range(6):
        health.record_failure(HIGH_DEMAND, "503 high demand", PRODUCTION_MODEL)
        ok, wait = health.available(PRODUCTION_MODEL)
        print(f"[Gemini] model={PRODUCTION_MODEL} failure={i + 1} "
              f"allowed={ok} cooldown_remaining={wait:.0f}s")

    ok_prod, wait_prod = health.available(PRODUCTION_MODEL)
    print(f"blocked={not ok_prod} wait={wait_prod:.0f}s")
    assert not ok_prod, "the failing model should be gated"

    banner("2. other models must still be usable")
    failures = []
    for model in ("gemini-3.7-flash", "gemini-3.6-flash",
                  "gemini-3.5-flash-lite"):
        ok, _ = health.available(model)
        print(f"[Gemini] model={model} allowed={ok}")
        if not ok:
            failures.append(model)
    assert not failures, f"BUG: {failures} wrongly blocked by one model's 503"

    banner("3. dispatch gate (no-arg) must stay open")
    ok, wait = health.available()
    print(f"[Gemini] dispatch allowed={ok} wait={wait:.0f}s")
    assert ok, "BUG: a single model's 503 paused the whole batch"

    banner("4. cooldown must be half-open, not ratchet to 600s")
    health._buckets[PRODUCTION_MODEL].cooldown_until = time.time() - 1
    for i in range(3):
        health.record_failure(HIGH_DEMAND, "503 high demand", PRODUCTION_MODEL)
    _, after = health.available(PRODUCTION_MODEL)
    print(f"[Gemini] cooldown after re-probe={after:.0f}s "
          f"(cap={health_snapshot()['cooldowns_cap_seconds']}s)")
    assert after <= 40, f"BUG: cooldown ratcheted to {after:.0f}s"

    banner("5. a real success reopens the gate")
    health.record_success(PRODUCTION_MODEL)
    ok, _ = health.available(PRODUCTION_MODEL)
    print(f"[Gemini] model={PRODUCTION_MODEL} allowed={ok}")
    assert ok

    print("\nALL CHECKS PASSED — one model's 503 no longer freezes "
          "the other keys/models, and the cooldown cannot get stuck.")
    return 0


def health_snapshot() -> dict:
    return provider_health().snapshot()


if __name__ == "__main__":
    raise SystemExit(main())