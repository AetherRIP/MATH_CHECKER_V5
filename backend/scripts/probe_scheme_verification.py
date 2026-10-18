"""LIVE end-to-end check of the verification pipeline on a real mark scheme.

Where ``probe_scheme_inventory.py`` measures the *reading* (chunks, gates,
merge), this script checks the *product behaviour* that knowledge drives:

1. the fingerprint gate — is a second verification of an unchanged document
   correctly refused without spending a request?
2. the page-scoped pass — every page read, every gate reported, the merged
   inventory and its verdict;
3. persistence — does the cache record now carry the inventory, the completeness
   verdict, the verification state, the evidence kind, the structured anomalies
   and the marks reconciliation?
4. the gates the API/marking path reads — ``completeness_of``,
   ``review_requirement`` and ``reconcile_marks`` answering from the stored
   record with no further AI call;
5. the review surface — the page list the viewer would show (which pages
   contributed which printed indices).

Usage::

    python scripts/probe_scheme_verification.py [path] [--window 6]
        [--overlap 1] [--dpi 200] [--reuse] [--out report.json]

``--reuse`` skips the AI pass and only re-prints what is already persisted
(useful after a previous run, and proves the record is self-sufficient).

No secret is printed. Only the mark-scheme cache record for THIS document is
written; nothing else is touched.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))

from app import db  # noqa: E402
from app.config import get_config  # noqa: E402
from app.engine import blueprint as bp  # noqa: E402
from app.engine import scheme_inventory as si  # noqa: E402

DEFAULT_SCHEME = ROOT / "data" / "mark_schemes" / "889601795010.pdf"


def _print_record(path: Path) -> dict:
    sha = bp.file_hash(path)
    record = bp.load_record(sha) or {}
    completeness = record.get("completeness") or {}
    inventory = record.get("inventory") or {}
    reconciliation = record.get("reconciliation") or {}
    review = bp.review_requirement(path)
    pages = [p for p in (inventory.get("page_coverage") or [])]
    print("\n--- persisted record ---", flush=True)
    print(f"state               : {bp.verification_status(record)}", flush=True)
    print(f"evidence_kind       : {record.get('evidence_kind')}", flush=True)
    print(f"fingerprint         : {record.get('fingerprint')}", flush=True)
    print(f"verified_fingerprint: {record.get('verified_fingerprint')}", flush=True)
    print(f"questions stored    : {len((record.get('blueprint') or {}).get('questions') or [])}",
          flush=True)
    print(f"detected/expected   : {completeness.get('detected_count')}"
          f" / {completeness.get('expected_count')}"
          f"  ({completeness.get('expected_basis')})", flush=True)
    print(f"missing_indices     : {completeness.get('missing_indices')}", flush=True)
    print(f"conflicting_indices : {completeness.get('conflicting_indices')}", flush=True)
    print(f"failed_chunks       : {completeness.get('failed_chunks')}", flush=True)
    print(f"pages_without_items : {completeness.get('pages_without_items')}", flush=True)
    print(f"items_total_marks   : {completeness.get('items_total_marks')}", flush=True)
    print(f"verdict / unresolved: {completeness.get('verdict')} / "
          f"{completeness.get('unresolved')}", flush=True)
    print(f"reconciliation      : total={reconciliation.get('document_total')} "
          f"source={reconciliation.get('inventory_total')} "
          f"duplicates={reconciliation.get('duplicate_keys')} "
          f"unread={reconciliation.get('unread_marks')} "
          f"unresolved={reconciliation.get('unresolved')}", flush=True)
    records = record.get("anomalies") or []
    print(f"anomaly records     : {len(records)}", flush=True)
    for row in records[:12]:
        print(f"  - [{row.get('severity')}] {row.get('kind')}: {row.get('observed')}",
              flush=True)
    print(f"review_requirement  : known={review.get('known')} "
          f"incomplete={review.get('incomplete')} warnings={len(review.get('warnings') or [])}",
          flush=True)
    for warning in review.get("warnings") or []:
        print(f"  ! {warning}", flush=True)
    if pages:
        print(f"page coverage       : {len(pages)} page(s) reported", flush=True)
    return record


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", nargs="?", default=str(DEFAULT_SCHEME))
    parser.add_argument("--window", type=int, default=si.CHUNK_WINDOW_PAGES)
    parser.add_argument("--overlap", type=int, default=si.CHUNK_OVERLAP_PAGES)
    parser.add_argument("--dpi", type=int, default=si.PAGE_DPI)
    parser.add_argument("--reuse", action="store_true",
                        help="skip the AI pass; only re-print the stored record")
    parser.add_argument("--out", default="")
    args = parser.parse_args(argv)

    path = Path(args.path)
    if not path.exists():
        print(f"mark scheme not found: {path}", file=sys.stderr)
        return 2

    db.init_db()
    config = get_config()
    sha = bp.file_hash(path)
    print(f"document : {path.name}  sha={sha[:12]}…  model chain: "
          f"{' -> '.join(config.gemini.primary_models[:3])}", flush=True)

    outcome: dict = {}
    if not args.reuse:
        run, reason, state = bp.recheck_decision(path)
        print(f"\nfingerprint gate : run={run} state={state} reason={reason}", flush=True)
        if not run:
            print("nothing to do: the stored verification already concluded", flush=True)
            _print_record(path)
            return 0

        from app.gemini.client_proxy import KeyAwareClient
        from app.gemini.key_manager import KeyManager

        client = KeyAwareClient(KeyManager(), job_id="scheme-verification-probe")
        started = time.time()
        outcome = bp.verify_with_inventory(
            client, path, window=args.window, overlap=args.overlap, dpi=args.dpi,
            status_callback=lambda message: print(f"[status] {message}", flush=True))
        elapsed = time.time() - started
        print(f"\npage-scoped pass : ok={outcome.get('ok')} "
              f"state={outcome.get('state')} replaced={outcome.get('replaced')} "
              f"in {elapsed:.1f}s", flush=True)
        if outcome.get("error"):
            print(f"error            : {outcome['error']}", flush=True)
        report = outcome.get("inventory") or {}
        for chunk in (outcome.get("inventory") or {}).get("chunks") or []:
            print(f"  chunk {chunk.get('id')}: pages {chunk.get('pages')}", flush=True)
        if report.get("merge"):
            print(f"merge            : rereads={report['merge'].get('reread_indices')} "
                  f"conflicts={report['merge'].get('conflicts')}", flush=True)

    record = _print_record(path)

    # The gates must answer from the record alone: no client is constructed here.
    run_again, reason_again, state_again = bp.recheck_decision(path)
    print(f"\nsecond verification needed? run={run_again} "
          f"state={state_again} reason={reason_again}", flush=True)
    completeness = record.get("completeness") or {}
    inventory = record.get("inventory") or {}
    pages = []
    by_page: dict[int, list] = {}
    for item in inventory.get("items") or []:
        page = item.get("page")
        if page:
            by_page.setdefault(int(page), []).append(
                {"source_index": item.get("source_index"),
                 "label": item.get("label"),
                 "max_marks": item.get("max_marks")})
    for number in range(1, int(inventory.get("page_count") or 0) + 1):
        items = by_page.get(number, [])
        pages.append({"page": number, "item_count": len(items),
                      "indices": [i["source_index"] for i in items],
                      "needs_review": (not items) and number in (
                          completeness.get("pages_without_items") or [])})
    print(f"review page list : {len(pages)} page(s); "
          f"empty={[p['page'] for p in pages if not p['item_count']]}", flush=True)

    if args.out:
        payload = {
            "document": str(path),
            "sha256": sha,
            "outcome": {k: v for k, v in outcome.items() if k != "inventory"},
            "record": record,
            "review_requirement": bp.review_requirement(path),
            "recheck_decision": {"run": run_again, "reason": reason_again,
                                 "state": state_again},
            "pages": pages,
        }
        Path(args.out).write_text(json.dumps(payload, indent=2, default=str),
                                  encoding="utf-8")
        print(f"report written to {args.out}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
