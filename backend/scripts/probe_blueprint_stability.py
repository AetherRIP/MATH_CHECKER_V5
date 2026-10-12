"""TEMPORARY diagnostic: is the mark-scheme question COUNT reproducible?

The teacher reported that once the app "did not read the amount of question
correctly" for Ch1L7. Blueprints are cached by the mark scheme's SHA-256, so
drift can only enter through a FRESH extraction (the very first run, or the
"Recheck mark scheme" action, which passes force=True).

This runs several independent, uncached extractions and compares the question
count and total marks, so we know whether the structure is stable or a lottery.
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))

# Isolate writes: the real DB carries persisted key state that must not affect
# a measurement. Real credentials are still used (copied, never printed).
_ISOLATED = Path(tempfile.mkdtemp(prefix="mc_bp_"))
shutil.copy2(ROOT / "data" / "secrets.toml", _ISOLATED / "secrets.toml")
if (ROOT / "data" / "config.toml").exists():
    shutil.copy2(ROOT / "data" / "config.toml", _ISOLATED / "config.toml")
os.environ["MC5_DATA_DIR"] = str(_ISOLATED)

from app import db  # noqa: E402
from app.config import get_config  # noqa: E402
from app.engine.blueprint import get_blueprint  # noqa: E402
from app.gemini.client_proxy import KeyAwareClient  # noqa: E402
from app.gemini.key_manager import KeyManager  # noqa: E402

MARK_SCHEME = Path(sys.argv[1]) if len(sys.argv) > 1 else \
    ROOT / "data" / "mark_schemes" / "886161920765.pdf"
RUNS = int(sys.argv[2]) if len(sys.argv) > 2 else 3


def main() -> int:
    db.init_db()
    cfg = get_config()
    results = []
    for run in range(RUNS):
        # A fresh KeyAwareClient per run; force=True bypasses the blueprint
        # cache so this really is a fresh extraction.
        client = KeyAwareClient(KeyManager(), job_id=f"bp-probe-{run}")
        blueprint, meta = get_blueprint(client, str(MARK_SCHEME), force=True)
        if not blueprint:
            print(f"[bp] run {run + 1}: FAILED meta={meta}")
            results.append(None)
            continue
        questions = blueprint.get("questions", [])
        total = sum(int(q["max_marks"]) for q in questions)
        nums = [q["question_number"] for q in questions]
        print(f"[bp] run {run + 1}: source={meta.get('source')} "
              f"questions={len(questions)} total_marks={total}")
        print(f"      numbers: {', '.join(nums[:14])}"
              f"{' …' if len(nums) > 14 else ''}")
        results.append((len(questions), total))
        try:
            client.cleanup_files()
        except Exception:
            pass

    ok = [r for r in results if r]
    counts = {r[0] for r in ok}
    totals = {r[1] for r in ok}
    print(f"\n[bp] question counts seen: {sorted(counts)}")
    print(f"[bp] total marks seen:     {sorted(totals)}")
    stable = len(counts) == 1 and len(totals) == 1
    print(f"[bp] {'STABLE' if stable else 'UNSTABLE'}: "
          f"{'same structure every run' if stable else 'structure drifts per run'}")
    return 0 if stable else 1


if __name__ == "__main__":
    raise SystemExit(main())