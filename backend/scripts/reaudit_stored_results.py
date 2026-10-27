"""Re-decide the stored corrections with today's rules, without calling any AI.

This replays every stored result through the layers that decide whether a paper
needs a teacher (mark_integrity -> verify -> result_guard) using the marking
those papers already have. Nothing is written, no model is called, and no mark
is changed by this script: it prints what the engine WOULD decide now, next to
what it decided then, so an improvement in the review rate can be checked
against the same papers instead of a claim.

    python -m scripts.reaudit_stored_results            # summary
    python -m scripts.reaudit_stored_results --papers   # per-paper detail

Read-only by construction: it opens the database with a query-only connection.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import statistics
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app import db  # noqa: E402  (path set above)
from app.engine import mark_integrity, result_guard, verify  # noqa: E402
from app.engine import legacy_marking as legacy  # noqa: E402

#: The reasons mark_integrity itself writes. Only these are cleared before the
#: replay, so the script re-decides the SAME papers rather than stacking a
#: second pass on top of the first one's flags.
_INTEGRITY_MARKERS = (
    "The marker awarded fewer than the maximum marks but described the question",
    "Full marks were awarded although the marker's own solution",
    "Full marks were awarded even though the marker's own mark breakdown",
    "The marker recorded no readable student answer",
)

_REVIEW_LEVEL_CONFIDENCE = {
    result_guard.LOW, result_guard.VALIDATION_FAILED,
    result_guard.LARGE_SCORE_CHANGE, result_guard.SUBMISSION_REVIEW_REQUIRED,
}


def _connection() -> sqlite3.Connection:
    path = Path(db.DB_PATH)
    if not path.exists():
        raise SystemExit(f"No database at {path}.")
    # Query-only: this report must never be able to change a stored mark.
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _clear_integrity_flags(result: dict) -> None:
    for question in result.get("questions") or []:
        reason = str(question.get("review_reason") or "")
        if not reason or not any(marker in reason for marker in _INTEGRITY_MARKERS):
            continue
        question.pop("review_reason", None)
        if question.get("status") == mark_integrity.STATUS_REVIEW:
            question.pop("status", None)


def _switched_to_fallback(row: sqlite3.Row) -> bool:
    reasons = json.loads(row["confidence_reasons"] or "[]")
    return any("instead of the configured primary" in str(r) for r in reasons)


def _payload(conn: sqlite3.Connection, row: sqlite3.Row) -> tuple[dict, bool]:
    """The result payload to replay, and whether the row's own copy was stale.

    Rows saved after a high-score double-check stored the FIRST pass's questions
    with the FINAL pass's totals (the save path serialised the wrong object).
    That is fixed for new corrections; for the affected old rows the current
    version holds the marks that belong to the stored score, so the replay uses
    it and says so instead of silently replaying the wrong marks.
    """
    result = json.loads(row["result_json"] or "{}")
    questions = result.get("questions") or []
    earned = sum(int(q.get("awarded_marks") or 0) for q in questions)
    available = sum(int(q.get("max_marks") or 0) for q in questions)
    if (earned, available) == (int(row["earned"] or 0), int(row["available"] or 0)):
        return result, False
    current = conn.execute(
        "SELECT result_json FROM result_versions WHERE result_id = ? AND is_current = 1",
        (row["id"],)).fetchone()
    if current and current["result_json"]:
        alternative = json.loads(current["result_json"])
        alt_questions = alternative.get("questions") or []
        alt_earned = sum(int(q.get("awarded_marks") or 0) for q in alt_questions)
        alt_available = sum(int(q.get("max_marks") or 0) for q in alt_questions)
        if (alt_earned, alt_available) == (int(row["earned"] or 0),
                                           int(row["available"] or 0)):
            return alternative, True
    return result, False


def _decide(conn: sqlite3.Connection, row: sqlite3.Row) -> dict:
    result, stale = _payload(conn, row)
    verification = json.loads(row["verification_json"] or "{}")
    blueprint_report = verification.get("blueprint") or None

    _clear_integrity_flags(result)
    before_earned = int(row["earned"] or 0)
    before_available = int(row["available"] or 0)

    result, integrity = mark_integrity.enforce_evidence(result)
    report = verify.verify_result(result, blueprint_report)
    earned, available, _ = legacy.calculate_totals(result)

    switched = _switched_to_fallback(row)
    level, reasons = result_guard.assess_confidence(
        result, report, earned, available,
        marking_model="fallback-model" if switched else None,
        primary_model="primary-model" if switched else None,
    )
    return {
        "id": row["id"],
        "student": row["student_id"],
        "then_confidence": row["confidence"],
        "then_review": bool(row["review_required"]),
        "now_confidence": level,
        "now_review": level in _REVIEW_LEVEL_CONFIDENCE,
        "now_status": report["status"],
        "codes": report["review_reason_codes"],
        "advisories": report["advisory_count"],
        "integrity_changes": integrity["changed"],
        "earned_then": before_earned,
        "earned_now": earned,
        "available_then": before_available,
        "available_now": available,
        "confidence_reasons": reasons,
        "reason_details": report["review_reasons"],
        "stale_payload": stale,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--papers", action="store_true",
                        help="print one block per paper instead of only the summary")
    args = parser.parse_args()

    with _connection() as conn:
        rows = conn.execute(
            "SELECT id, student_id, earned, available, confidence, "
            "confidence_reasons, review_required, verification_json, result_json "
            "FROM results ORDER BY created_at"
        ).fetchall()

    if not rows:
        print("No stored results to replay.")
        return 0

    decisions = [_decide(conn, row) for row in rows]
    then_reviews = sum(1 for d in decisions if d["then_review"])
    now_reviews = sum(1 for d in decisions if d["now_review"])
    # A review that disappeared AND whose marks are untouched is the win this
    # script exists to measure; a review that disappeared while a mark moved
    # would be its own problem.
    dropped = [d for d in decisions if d["then_review"] and not d["now_review"]]
    gained = [d for d in decisions if not d["then_review"] and d["now_review"]]
    mark_moves = [d for d in decisions
                  if (d["earned_then"], d["available_then"]) != (d["earned_now"], d["available_now"])]

    print(f"papers replayed: {len(decisions)}")
    print(f"review required  then: {then_reviews}/{len(decisions)}"
          f"  ({then_reviews / len(decisions):.0%})")
    print(f"review required  now : {now_reviews}/{len(decisions)}"
          f"  ({now_reviews / len(decisions):.0%})")
    print(f"reviews removed: {len(dropped)}   newly required: {len(gained)}")
    print(f"papers whose marks moved under today's rules: {len(mark_moves)}")
    stale = [d for d in decisions if d["stale_payload"]]
    if stale:
        print(f"papers whose stored payload disagreed with their own score "
              f"(pre-fix double-check save): {len(stale)}")

    if args.papers:
        print()
        for decision in decisions:
            flag = "REVIEW" if decision["now_review"] else "auto"
            print(f"- {decision['id'][:8]} student {decision['student']}: "
                  f"{decision['then_confidence']} -> {decision['now_confidence']} "
                  f"({flag})  {decision['earned_now']}/{decision['available_now']}")
            for code in decision["codes"]:
                detail = next(r["detail"] for r in decision["reason_details"]
                              if r["code"] == code)
                print(f"    {code}: {detail}")
            if decision["integrity_changes"]:
                for change in decision["integrity_changes"]:
                    print(f"    changed Q{change['question_number']}: "
                          f"{change['from']} -> {change['to']}")

    if mark_moves:
        print("\nWARNING: today's rules change marks on the stored papers:")
        for decision in mark_moves:
            print(f"  {decision['id'][:8]}: {decision['earned_then']}/{decision['available_then']}"
                  f" -> {decision['earned_now']}/{decision['available_now']}")

    # The commonest remaining reasons, so the next cause is named rather than
    # guessed at.
    counts: dict[str, int] = {}
    for decision in decisions:
        for code in decision["codes"]:
            counts[code] = counts.get(code, 0) + 1
    if counts:
        print("\nremaining reason codes:")
        for code, count in sorted(counts.items(), key=lambda item: -item[1]):
            print(f"  {count:3d}  {code}")

    levels = [d["now_confidence"] for d in decisions]
    print("\nconfidence now:", ", ".join(
        f"{level}={levels.count(level)}" for level in sorted(set(levels))))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
