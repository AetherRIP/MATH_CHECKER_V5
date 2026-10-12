"""Multi-key rotation simulation (no real quota spent).

Uses a scripted fake generate_content to verify, against a REAL KeyManager
bound to an ISOLATED temp database + secret store (the user's configured
keys are never read, overwritten, or affected by this script):

  1. Round-robin load balance across N healthy keys
  2. Single 429 -> key cooldown -> traffic steers to remaining key
  3. Cooldown expiry -> key returns to rotation
  4. Invalid key (401) -> marked INVALID, excluded until re-enabled
  5. Failure-kind discrimination (429 vs 401 vs 5xx vs timeout vs malformed)
  6. All-keys-down -> WaitForApiError (caller parks; never a crash/loop)

Usage: python scripts/test_rotation_simulation.py
"""

from __future__ import annotations

import os
import sys
import tempfile
import time
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

# ISOLATION: bind config + DB to a throwaway data dir BEFORE importing app
# modules, so the simulation can never touch the user's real secret store
# (a previous non-isolated run overwrote data/secrets.toml with sim keys —
# exactly what this isolation now prevents).
_ISOLATED_DIR = tempfile.mkdtemp(prefix="mc5_rotation_sim_")
os.environ["MC5_DATA_DIR"] = _ISOLATED_DIR

import app.config as _config  # noqa: E402
import app.db as _db  # noqa: E402

_config.DATA_DIR = Path(_ISOLATED_DIR)
_config.SECRETS_PATH = Path(_ISOLATED_DIR) / "secrets.toml"
_config.CONFIG_PATH = Path(_ISOLATED_DIR) / "config.toml"
_db.DB_PATH = Path(_ISOLATED_DIR) / "app.db"
_config._config = None
_config._secrets = _config.SecretStore(_config.SECRETS_PATH)
if getattr(_db._local, "conn", None) is not None:
    _db._local.conn.close()
    _db._local.conn = None
_db.init_db()

from app.config import get_secrets  # noqa: E402
from app.errors import classify_gemini_error  # noqa: E402
from app.gemini.key_manager import KeyManager, WaitForApiError  # noqa: E402
from app.gemini.tester import KeyManagerHolder  # noqa: E402

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, ok, detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))


def fresh_manager(n: int = 3) -> KeyManager:
    keys = [f"AIzaSimulatedKey{'A' * i}1234567890" for i in range(n)]
    get_secrets().set_gemini_keys(keys)
    KeyManagerHolder._instance = None  # fresh singleton per test
    km = KeyManager()
    km.sync_with_secret_store()
    # ISOLATION: simulated key strings repeat across tests, so cooldown /
    # invalid state persisted by an EARLIER test would leak into this one
    # (test 4 leaves "AIzaSimulatedKey1234567890" cooling for ~10 minutes,
    # which made test 5's first acquire raise WaitForApiError). A "fresh
    # manager" must always start with n HEALTHY keys; per-test failure
    # behaviour is exercised explicitly below.
    for fp in km.sync_with_secret_store():
        rec = km._records[fp]
        rec.cooldown_until = 0.0
        rec.cooldown_kind = None
        rec.invalid = False
        rec.disabled = False
        rec.save()
    return km


def test_load_balance():
    print("\n1. Load balance (3 healthy keys, 9 acquisitions)")
    km = fresh_manager(3)
    picks = [km.acquire()[0] for _ in range(9)]
    counts = {fp: picks.count(fp) for fp in set(picks)}
    check("all three keys used", len(counts) == 3, str({k[:6]: v for k, v in counts.items()}))
    check("perfect round-robin distribution", set(counts.values()) == {3})
    check("no concentration", max(counts.values()) - min(counts.values()) <= 1)


def test_429_cooldown_and_steering():
    print("\n2. 429 rate-limit -> cooldown -> steering -> recovery")
    km = fresh_manager(3)
    fps = [km.acquire()[0] for _ in range(3)]
    victim = fps[0]

    km.record_failure(victim, Exception("429 RESOURCE_EXHAUSTED: quota exceeded"))
    status = km.status()
    victim_rec = next(k for k in status["keys"] if k["fingerprint"] == victim)
    check("429 detected as rate_limited", victim_rec["rate_limit_events"] == 1)
    check("429 key entered COOLDOWN", victim_rec["status"] == "COOLDOWN")
    check("cooldown is temporary (not invalid)", victim_rec["cooldown_remaining"] > 0)

    picks = {km.acquire()[0] for _ in range(8)}
    check("traffic avoids cooled key", victim not in picks)
    check("traffic uses healthy keys", picks == set(fps[1:]))

    # Recovery: expire the cooldown
    km._records[victim].cooldown_until = time.time() - 1
    picks_after = {km.acquire()[0] for _ in range(9)}
    check("key returns after cooldown", victim in picks_after)


def test_invalid_key_discrimination():
    print("\n3. Invalid key (401) vs other failure kinds")
    km = fresh_manager(3)
    fps = [km.acquire()[0] for _ in range(3)]
    bad = fps[0]
    km.record_failure(bad, Exception("401 Unauthorized: API key not valid"))
    status = km.status()
    bad_rec = next(k for k in status["keys"] if k["fingerprint"] == bad)
    check("401 marked INVALID (not cooldown)", bad_rec["status"] == "INVALID")
    check("invalid key excluded from rotation",
          all(km.acquire()[0] != bad for _ in range(6)))
    km.re_enable(bad)
    check("re-enable returns key to rotation",
          any(km.acquire()[0] == bad for _ in range(6)))

    print("\n   Failure-kind discrimination (classify_gemini_error)")
    cases = {
        "429 RESOURCE_EXHAUSTED": "rate_limited",
        "401 API key not valid": "invalid_key",
        "API key reported as leaked": "invalid_key",
        "503 Service Unavailable": "temporary",
        "Request timed out after 180000ms": "timeout",
        "500 Internal Server Error": "unknown",
        "The model response was not valid JSON.": "unknown",
    }
    for message, expected in cases.items():
        got = classify_gemini_error(Exception(message))["kind"]
        check(f"{message[:38]!r:42} -> {got}", got == expected)


def test_exponential_cooldown_cap():
    print("\n4. Exponential cooldown with cap (no retry storm)")
    km = fresh_manager(1)
    fp, _ = km.acquire()
    rec = km._records[fp]
    gaps = []
    for _ in range(6):
        before = rec.cooldown_until
        km.record_failure(fp, Exception("429 quota"))
        gaps.append(round(rec.cooldown_until - time.time()))
    check("cooldown grows then caps", gaps[0] <= gaps[-1] <= 601, f"gaps={gaps}")
    check("cap at ~10 minutes", gaps[-1] <= 605, f"final={gaps[-1]}s")


def test_all_keys_down():
    print("\n5. All keys cooling -> WaitForApiError (job parks, no hammering)")
    km = fresh_manager(2)
    for _ in range(2):
        fp, _ = km.acquire()
        km.record_failure(fp, Exception("429 quota"))
    try:
        km.acquire()
        check("WaitForApiError raised", False)
    except WaitForApiError:
        check("WaitForApiError raised", True)
    # and after expiry, recoverable:
    for rec in km._records.values():
        rec.cooldown_until = time.time() - 1
    try:
        km.acquire()
        check("recovers after cooldown expiry", True)
    except WaitForApiError:
        check("recovers after cooldown expiry", False)
    # Invalid-key variant: all keys 401'd -> also parks (not crashes)
    km = fresh_manager(1)
    fp, _ = km.acquire()
    km.record_failure(fp, Exception("401 invalid api key"))
    try:
        km.acquire()
        check("all-invalid raises WaitForApiError (not crash)", False)
    except WaitForApiError:
        check("all-invalid raises WaitForApiError (not crash)", True)


def main() -> int:
    print("Multi-key rotation simulation (fake responses, real KeyManager)")
    print("=" * 62)
    test_load_balance()
    test_429_cooldown_and_steering()
    test_invalid_key_discrimination()
    test_exponential_cooldown_cap()
    test_all_keys_down()

    passed = sum(1 for _, ok, _ in RESULTS if ok)
    total = len(RESULTS)
    print()
    print("=" * 62)
    print(f"SIMULATION RESULT: {passed}/{total} checks passed")
    return 0 if passed == total else 1


if __name__ == "__main__":
    raise SystemExit(main())
