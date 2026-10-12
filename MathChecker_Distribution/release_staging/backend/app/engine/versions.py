"""Correction versioning (§D) — append-only history of accepted results.

The ``results`` table always holds the CURRENT (final) correction state; this
module keeps every accepted snapshot alongside it so that:

* a recheck NEVER silently destroys the previous correction,
* the teacher can see the previous score and compare versions,
* the teacher can explicitly choose which version becomes final,
* historical corrections stay readable with no AI call (offline, §M).

Nothing here changes how a mark is produced — it only records what was saved.
"""

from __future__ import annotations

import json
from typing import Any


def _dump(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False)


def record(conn, *, result_id: str, earned: int, available: int,
           percentage: float, provisional_grade: str | None,
           marking_model: str | None, key_fingerprint: str | None,
           confidence: str | None, confidence_reasons: Any,
           verification: Any, result: Any, created_at: float,
           note: str = "") -> int:
    """Append a snapshot of the CURRENT result state and mark it current.

    Must be called inside the same transaction that wrote ``results`` so the
    history can never drift from the current row. Returns the new version
    number. The caller supplies the exact values just persisted.
    """
    row = conn.execute(
        "SELECT COALESCE(MAX(version), 0) AS v FROM result_versions WHERE result_id = ?",
        (result_id,)).fetchone()
    version = int(row["v"] if row is not None else 0) + 1
    conn.execute("UPDATE result_versions SET is_current = 0 WHERE result_id = ?",
                 (result_id,))
    conn.execute(
        """
        INSERT INTO result_versions(
            result_id, version, is_current, earned, available, percentage,
            provisional_grade, marking_model, key_fingerprint, confidence,
            confidence_reasons, verification_json, result_json, note, created_at)
        VALUES(?,?,1,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (result_id, version, earned, available, percentage, provisional_grade,
         marking_model, key_fingerprint, confidence, _dump(confidence_reasons),
         _dump(verification), _dump(result), note[:500], created_at),
    )
    return version


def record_pending(conn, *, result_id: str, earned: int, available: int,
                   percentage: float, provisional_grade: str | None,
                   marking_model: str | None, key_fingerprint: str | None,
                   confidence: str | None, confidence_reasons: Any,
                   verification: Any, result: Any, created_at: float,
                   note: str = "Remark attempt") -> int:
    """Record a REMARK attempt WITHOUT making it the active correction.

    This is the whole safety story of remarking in one function: the new attempt
    is stored in full (marks, question detail, model, validation, confidence) so
    the teacher can compare it, but the currently active version is left exactly
    as it was. Nothing is deleted and nothing is applied, so a re-mark cannot
    destroy a valid correction even if the new run is worse or invalid - it is
    simply surfaced with its confidence for review.
    """
    row = conn.execute(
        "SELECT COALESCE(MAX(version), 0) AS v FROM result_versions WHERE result_id = ?",
        (result_id,)).fetchone()
    version = int(row["v"] if row is not None else 0) + 1
    conn.execute(
        """
        INSERT INTO result_versions(
            result_id, version, is_current, pending, earned, available,
            percentage, provisional_grade, marking_model, key_fingerprint,
            confidence, confidence_reasons, verification_json, result_json,
            note, created_at)
        VALUES(?,?,0,1,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (result_id, version, earned, available, percentage, provisional_grade,
         marking_model, key_fingerprint, confidence, _dump(confidence_reasons),
         _dump(verification), _dump(result), note[:500], created_at),
    )
    return version


def list_versions(result_id: str, *, include_payload: bool = False) -> list[dict]:
    from .. import db
    columns = ("id, result_id, version, is_current, pending, earned, available, "
               "percentage, provisional_grade, marking_model, key_fingerprint, "
               "confidence, confidence_reasons, note, created_at"
               + (", verification_json, result_json" if include_payload else ""))
    rows = db.query(
        f"SELECT {columns} FROM result_versions WHERE result_id = ? "
        "ORDER BY version DESC", (result_id,))
    out: list[dict] = []
    for row in rows:
        item = dict(row)
        item["confidence_reasons"] = db.loads(item.get("confidence_reasons"), [])
        item["is_current"] = bool(item.get("is_current"))
        item["pending"] = bool(item.get("pending"))
        out.append(item)
    return out


def get_version(result_id: str, version: int) -> dict | None:
    from .. import db
    row = db.query_one(
        "SELECT * FROM result_versions WHERE result_id = ? AND version = ?",
        (result_id, int(version)))
    return dict(row) if row else None
