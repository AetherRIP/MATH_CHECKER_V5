"""LIVE page-scoped inventory of a real mark scheme (one bounded pass).

Runs ``app.engine.scheme_inventory.extract_inventory`` against an actual
document and prints, chunk by chunk, what came back: the page range, the gate
result, the finish reason, the printed indices each chunk reported and the
highest index it could see. Then it merges them and prints the reconciled
inventory, the missing indices and the verdict.

Optionally (``--baseline``) it also performs the OLD single-request read of the
whole document, so the loss is measured rather than asserted.

Usage::

    python scripts/probe_scheme_inventory.py [path] [--window 6] [--overlap 1]
        [--dpi 200] [--models m1,m2] [--baseline] [--out report.json]

No secret is ever printed. Uses the configured keys and the configured model
chain; nothing is persisted to the blueprint cache.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))

from app import db  # noqa: E402
from app.config import get_config  # noqa: E402
from app.engine import scheme_inventory as si  # noqa: E402
from app.gemini.client_proxy import KeyAwareClient  # noqa: E402
from app.gemini.key_manager import KeyManager  # noqa: E402

DEFAULT_SCHEME = ROOT / "data" / "mark_schemes" / "889601795010.pdf"


def _progress(result: dict) -> None:
    chunk = result.get("chunk") or {}
    print(f"[chunk] {chunk.get('id'):>8}  pages={chunk.get('pages')}  "
          f"status={result.get('status')}  items={len(result.get('items') or [])}  "
          f"rejected={len(result.get('rejected') or [])}  "
          f"last_index={result.get('last_index_on_page')}  "
          f"finish={result.get('finish_reason')}  "
          f"model={result.get('model')}  {result.get('elapsed_ms')}ms", flush=True)
    if result.get("echoed_pages"):
        print(f"[chunk] {chunk.get('id'):>8}  echoed pages={result['echoed_pages']}",
              flush=True)
    if result.get("error"):
        print(f"[chunk] {chunk.get('id'):>8}  ERROR: {result['error']}", flush=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", nargs="?", default=str(DEFAULT_SCHEME))
    parser.add_argument("--window", type=int, default=si.CHUNK_WINDOW_PAGES)
    parser.add_argument("--overlap", type=int, default=si.CHUNK_OVERLAP_PAGES)
    parser.add_argument("--dpi", type=int, default=si.PAGE_DPI)
    parser.add_argument("--attempts", type=int, default=1,
                        help="attempts per model for a chunk that fails a gate")
    parser.add_argument("--models", default="",
                        help="comma separated model chain (default: configured)")
    parser.add_argument("--baseline", action="store_true",
                        help="also run the whole-document single request")
    parser.add_argument("--out", default="", help="write the JSON report here")
    parser.add_argument("--pages", default="",
                        help="page numbers for a focused index probe, e.g. 26,27,28,29")
    parser.add_argument("--probe-only", action="store_true",
                        help="only run the focused index probe (no chunked pass)")
    parser.add_argument("--probe-dpi", type=int, default=250)
    args = parser.parse_args(argv)

    path = Path(args.path)
    if not path.exists():
        print(f"[probe] mark scheme not found: {path}")
        return 2
    db.init_db()
    models = [m.strip() for m in args.models.split(",") if m.strip()] or None
    print(f"[probe] document : {path}")
    print(f"[probe] bytes    : {path.stat().st_size}")
    print(f"[probe] window   : {args.window} pages, overlap {args.overlap}, "
          f"dpi {args.dpi}")
    print(f"[probe] sha256   : {si.document_sha256(path)}")
    print(f"[probe] models   : {models or si.candidate_models()}")
    print(f"[probe] pages    : {si.page_count(path)}")

    client = KeyAwareClient(KeyManager(), job_id="scheme-inventory-probe")

    probe_pages = [int(p) for p in args.pages.split(",") if p.strip()]
    if args.probe_only:
        if not probe_pages:
            print("[probe] --probe-only needs --pages")
            return 2
        probed = si.probe_page_indices(
            client, path, probe_pages,
            model=(args.models.split(",")[0] if args.models else None),
            dpi=args.probe_dpi)
        print(f"\n[probe] index probe pages={probed['pages']} "
              f"status={probed['status']} model={probed['model']} "
              f"finish={probed['finish_reason']} bytes={probed['request_bytes']}")
        print(f"[probe] indices seen   : {probed['indices']}")
        print(f"[probe] highest index  : {probed['highest_index']}")
        print(f"[probe] stated total   : {probed['stated_total_marks']}")
        print(f"[probe] notes          : {probed['notes']}")
        if probed.get("error"):
            print(f"[probe] error          : {probed['error']}")
        if args.out:
            Path(args.out).write_text(json.dumps({"document": str(path),
                                                  "index_probe": probed},
                                                 indent=2), encoding="utf-8")
            print(f"[probe] JSON written to {args.out}")
        return 0 if probed["status"] == si.CHUNK_OK else 1

    try:
        outcome = si.extract_inventory(
            client, path, window=args.window, overlap=args.overlap, dpi=args.dpi,
            models=models, attempts_per_model=args.attempts, on_chunk=_progress)
    except Exception as exc:  # noqa: BLE001 - report the real reason
        print(f"[probe] FAILED: {type(exc).__name__}: {exc}")
        return 1

    report = outcome["report"]
    print("\n[probe] merged inventory "
          f"({report['detected_count']} item(s) with a printed index):")
    for item in outcome["items"]:
        flag = "  <-- CONFLICT" if item.get("conflict") else ""
        marks = item.get("max_marks")
        marks = f"{marks}" if marks else "?"
        print(f"   {item['source_index']:>4}  page {item.get('page'):>3}  "
              f"{marks:>3} marks  {item.get('label') or '<no label>'}"
              f"{flag}")

    print("\n[probe] report:")
    for key in ("detected_count", "expected_count", "expected_basis",
                "observed_last_index", "missing_indices", "conflicting_indices",
                "reread_indices", "unkeyed_items", "items_total_marks",
                "marks_missing_indices", "labels_missing_indices",
                "failed_chunks", "pages_without_items", "page_count",
                "implausible_last_index", "last_index_disagreements",
                "unresolved", "verdict"):
        print(f"   {key}: {report.get(key)}")

    print("\n[probe] chunks:")
    for chunk in report["chunks"]:
        print(f"   {chunk['id']:>8}  {chunk['status']:<10} "
              f"items={chunk['items']:<4} last_index={chunk['last_index_on_page']} "
              f"finish={chunk['finish_reason']} attempts={chunk['attempts']} "
              f"bytes={chunk['request_bytes']} "
              f"elapsed={chunk['elapsed_ms']}ms  {chunk['error'] or ''}")

    if report["conflicting_indices"]:
        print("\n[probe] conflicts (both readings retained):")
        for conflict in outcome["merge"]["conflicts"]:
            print(f"   index {conflict['source_index']}: {conflict['variants']}")

    payload: dict = {
        "document": str(path),
        "sha256": outcome["sha256"],
        "page_count": outcome["page_count"],
        "window": args.window,
        "overlap": args.overlap,
        "dpi": args.dpi,
        "report": report,
        "items": outcome["items"],
        "conflicts": outcome["merge"]["conflicts"],
        "chunk_runs": [
            {k: (r.get(k) if k != "items" else len(r.get("items") or []))
             for k in ("chunk", "status", "model", "finish_reason", "items",
                       "rejected", "raw_chars", "request_bytes", "elapsed_ms",
                       "last_index_on_page", "echoed_pages", "error",
                       "attempts")}
            for r in outcome["results"]
        ],
    }

    if probe_pages:
        print("\n[probe] focused index probe on the given pages "
              "(transcribe every printed index):")
        probed = si.probe_page_indices(client, path, probe_pages, dpi=args.probe_dpi)
        payload["index_probe"] = probed
        print(f"[probe] probe status={probed['status']} "
              f"highest={probed['highest_index']} "
              f"total={probed['stated_total_marks']} "
              f"finish={probed['finish_reason']} error={probed['error']}")
        print(f"[probe] probe indices={probed['indices']}")

    if args.baseline:
        print("\n[probe] baseline: the WHOLE document in ONE request "
              "(the shape that produced the cached blueprint)…")
        baseline = si.baseline_full_document_read(client, path)
        payload["baseline"] = baseline
        print(f"[probe] baseline model={baseline['model']} "
              f"items={baseline['item_count']} "
              f"max_index={baseline['max_index']} "
              f"finish={baseline['finish_reason']} "
              f"raw_chars={baseline['raw_chars']} error={baseline['error']}")
        if baseline.get("indices"):
            print(f"[probe] baseline indices: {baseline['indices']}")

    print(f"\n[probe] VERDICT: {report['verdict']}")
    if args.out:
        Path(args.out).write_text(json.dumps(payload, indent=2),
                                  encoding="utf-8")
        print(f"[probe] JSON report written to {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
