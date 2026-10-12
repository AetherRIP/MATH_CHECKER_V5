"""TEMPORARY diagnostic: why does C1L7 extract 28 questions instead of 33?

The teacher reports 33 questions in the mark scheme but the app detects ~28.
Two candidate causes inside get_blueprint():

  (a) TRUNCATION — no max_output_tokens is set, so a long JSON response can be
      cut off and the tail questions lost;
  (b) SILENT DROP — _clean_questions() `continue`s on any question whose
      max_marks is missing/non-numeric/<=0, with no warning at all.

This replicates the real extraction call and reports the RAW parsed count, the
CLEANED count, the finish reason (truncation) and exactly which questions were
dropped and why (silently).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))

from app import db  # noqa: E402
from app.config import get_config  # noqa: E402
from app.engine import blueprint as bp  # noqa: E402
from app.engine import legacy_marking as legacy  # noqa: E402
from app.gemini.client_proxy import KeyAwareClient  # noqa: E402
from app.gemini.key_manager import KeyManager  # noqa: E402

MARK_SCHEME = Path(sys.argv[1]) if len(sys.argv) > 1 else \
    ROOT / "data" / "mark_schemes" / "886161920765.pdf"


def main() -> int:
    db.init_db()
    client = KeyAwareClient(KeyManager(), job_id="bp-drop-probe")
    remote = client.upload_file(str(MARK_SCHEME), purpose="mark_scheme")
    print(f"[probe] uploaded {remote.name}")

    parsed = None
    used_model = None
    for name in list(get_config().gemini.primary_models):
        try:
            resp = client.models.generate_content(
                model=name,
                contents=[remote, "Extract the mark scheme structure as JSON."],
                config={
                    "system_instruction": bp.BLUEPRINT_SYSTEM,
                    "response_mime_type": "application/json",
                    "response_schema": bp.BLUEPRINT_SCHEMA,
                    **legacy.DETERMINISTIC_SAMPLING,
                },
            )
        except Exception as exc:  # noqa: BLE001
            print(f"[probe] {name} failed: {str(exc)[:110]}")
            continue
        raw = getattr(resp, "text", "") or ""
        # Truncation evidence straight from the provider.
        try:
            fr = resp.candidates[0].finish_reason
            finish = getattr(fr, "name", fr)
        except Exception:
            finish = "?"
        print(f"[probe] model={name} finish_reason={finish} "
              f"raw_chars={len(raw)}")
        if str(finish).upper() in {"MAX_TOKENS", "MAX_OUTPUT_TOKENS"}:
            print("[probe] !! RESPONSE TRUNCATED — tail questions are lost")
        try:
            parsed = legacy.extract_json(raw)
        except Exception as exc:
            print(f"[probe] JSON parse failed: {exc}")
            continue
        if parsed:
            used_model = name
            break

    if not parsed:
        print("[probe] no parseable response")
        return 1

    raw_qs = parsed.get("questions") or []
    cleaned = bp._clean_questions(raw_qs)
    print(f"\n[probe] model used        : {used_model}")
    print(f"[probe] RAW questions     : {len(raw_qs)}")
    print(f"[probe] CLEANED questions : {len(cleaned)}")
    print(f"[probe] declared total    : {parsed.get('total_marks')}")
    print(f"[probe] cleaned total     : "
          f"{sum(int(q['max_marks']) for q in cleaned)}")

    kept = {q["question_number"] for q in cleaned}
    # Save the raw extraction so the duplicates can be analysed offline
    # without paying for another extraction.
    dump = ROOT / "backend" / "scripts" / "_blueprint_raw.json"
    dump.write_text(json.dumps(parsed, indent=1), encoding="utf-8")
    print(f"[probe] raw JSON saved to {dump.name}")

    from collections import Counter, defaultdict
    by_number: dict[str, list] = defaultdict(list)
    for item in raw_qs:
        if isinstance(item, dict):
            by_number[str(item.get("question_number", "")).strip()].append(item)
    dupes = {n: items for n, items in by_number.items() if len(items) > 1}
    print(f"\n[probe] DUPLICATE question numbers: {len(dupes)} "
          f"(accounting for {len(raw_qs) - len(cleaned)} discarded entries)")
    for number, items in dupes.items():
        marks = [it.get("max_marks") for it in items]
        crit = [str(it.get("criteria", ""))[:38] for it in items]
        print(f"   - {number!r} x{len(items)} marks={marks}")
        for c in crit:
            print(f"       criteria: {c}")

    dropped = []
    for item in raw_qs:
        if not isinstance(item, dict):
            continue
        number = str(item.get("question_number", "")).strip()
        if number in kept:
            continue
        reason = "max_marks missing/not a number"
        try:
            mm = int(item.get("max_marks"))
            reason = f"max_marks <= 0 (got {mm})"
        except (TypeError, ValueError):
            reason = f"max_marks missing/not a number (got {item.get('max_marks')!r})"
        if not number:
            reason = "question_number missing"
        dropped.append((number, reason))
    if dropped:
        print(f"\n[probe] SILENTLY DROPPED ({len(dropped)}):")
        for number, reason in dropped:
            print(f"   - {number!r}: {reason}")
    else:
        print("\n[probe] nothing dropped by _clean_questions")
        print("[probe] -> raw count already short: check truncation or the "
              "model's own reading of the document")

    print("\n[probe] cleaned question list:")
    print("   " + ", ".join(q["question_number"] for q in cleaned))
    try:
        client.files_delete(remote, force=True)
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())