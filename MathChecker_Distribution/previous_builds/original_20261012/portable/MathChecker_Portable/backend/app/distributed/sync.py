"""Event-based synchronisation (§18-§20, §55) + correction ownership (§51).

* Events carry a monotonic ``seq``; clients fetch ``GET since=last_seq`` —
  unchanged records are never re-downloaded.
* Client events are deduplicated by ``client_event_id`` (offline replays can
  never create duplicates).
* Correction results are additionally stored with a UNIQUE
  (submission_id, submission_version, worker_id) constraint: the same
  submission/version is never corrected-into-the-record twice (§55).
"""
from __future__ import annotations

import json
import time
import uuid

from .. import db
from . import accounts

# Correction-result event types carried in the lightweight sync stream (§19).
EVENT_CORRECTION_RESULT = "correction.result"
EVENT_WORKER_STATUS = "worker.status"
EVENT_ERROR = "error.reported"


def append_event(*, type_: str, payload: dict, school_id: str | None = None,   # noqa: E501
                 class_id: str | None = None, account_id: str | None = None,
                 device_id: str | None = None, worker_id: str | None = None,
                 client_event_id: str | None = None) -> int | None:
    """Append one event; returns the seq, or None for an exact duplicate.

    ``client_event_id`` makes offline replays idempotent (§20).
    """
    if client_event_id:
        existing = db.query_one(
            "SELECT seq FROM sync_events WHERE client_event_id = ?",
            (client_event_id,))
        if existing:
            return None
    cur = db.execute(
        """INSERT INTO sync_events(client_event_id, ts, type, school_id,
           class_id, account_id, device_id, worker_id, payload)
           VALUES(?,?,?,?,?,?,?,?,?)""",
        (client_event_id, time.time(), type_, school_id, class_id, account_id,
         device_id, worker_id, json.dumps(payload, ensure_ascii=False)))
    row = db.query_one("SELECT seq FROM sync_events ORDER BY seq DESC LIMIT 1")
    return int(row["seq"]) if row else None


def latest_seq() -> int:
    row = db.query_one("SELECT COALESCE(MAX(seq), 0) AS m FROM sync_events")
    return int(row["m"]) if row else 0


def events_since(since: int, *, limit: int = 500,
                 account: dict | None = None) -> list[dict]:
    """Events after ``since``, oldest first, optionally scope-filtered.

    CO_TEACHER/ASSISTANT accounts only receive events for their assigned
    schools/classes (§10: never automatically see other schools).
    """
    limit = max(1, min(int(limit), 2000))
    sql = ("SELECT seq, ts, type, school_id, class_id, payload "
           "FROM sync_events WHERE seq > ?")
    params: list = [int(since)]
    if account is not None:
        schools, classes = accounts.scoped_ids(account)
        if schools or classes:
            # Build the visible-predicate: class visible, or school visible
            # (whole-school scope) for events without a class.
            clauses = []
            if classes:
                clauses.append("class_id IN (%s)" % ",".join("?" * len(classes)))
                params.extend(classes)
            if schools:
                clauses.append(
                    "(class_id IS NULL AND school_id IN (%s))"
                    % ",".join("?" * len(schools)))
                params.extend(schools)
            sql += " AND (" + " OR ".join(clauses) + ")"
    rows = db.query(sql + " ORDER BY seq ASC LIMIT ?", (*params, limit))
    out = []
    for row in rows:
        out.append({
            "seq": row["seq"], "ts": row["ts"], "type": row["type"],
            "school_id": row["school_id"], "class_id": row["class_id"],
            "payload": json.loads(row["payload"] or "{}"),
        })
    return out


# -- correction results (§19/§51) ------------------------------------------------

REQUIRED_RESULT_FIELDS = (
    "student_id", "submission_id", "raw_marks", "max_marks", "percentage",
    "final_grade", "correction_status",
)


def record_correction_result(payload: dict, *, account_id: str | None = None,
                             device_id: str | None = None,
                             worker_id: str | None = None) -> dict:
    """Persist one distributed correction-result record.

    Duplicate protection (§55): a UNIQUE (submission_id, submission_version,
    worker_id) index means the same worker re-uploading the same completed
    submission is an UPSERT of the same row, never a second correction.
    """
    missing = [f for f in REQUIRED_RESULT_FIELDS if payload.get(f) is None]
    if missing:
        raise ValueError(f"correction result missing fields: {missing}")
    submission_id = str(payload["submission_id"])
    submission_version = str(payload.get("submission_version") or "v1")
    worker = str(worker_id or payload.get("worker_id") or "unknown")
    record_id = uuid.uuid4().hex

    existing = db.query_one(
        """SELECT id FROM correction_results_sync
           WHERE submission_id = ? AND submission_version = ? AND worker_id = ?""",
        (submission_id, submission_version, worker))
    if existing:
        record_id = existing["id"]

    school_id = payload.get("school_id")
    class_id = payload.get("class_id")
    db.execute(
        """INSERT INTO correction_results_sync(id, submission_id,
           submission_version, worker_id, device_id, account_id, school_id,
           class_id, payload, received_at)
           VALUES(?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(submission_id, submission_version, worker_id) DO UPDATE SET
             payload = excluded.payload, received_at = excluded.received_at,
             device_id = excluded.device_id, account_id = excluded.account_id""",
        (record_id, submission_id, submission_version, worker, device_id,
         account_id, school_id, class_id,
         json.dumps(accounts.sanitize_for_log(payload), ensure_ascii=False),
         time.time()))

    # Ownership (§51): which account/worker/device produced the correction.
    result_id = payload.get("result_id") or record_id
    db.execute(
        """INSERT INTO correction_ownership(result_id, account_id, worker_id,
           device_id, recorded_at) VALUES(?,?,?,?,?)
           ON CONFLICT(result_id) DO UPDATE SET account_id = excluded.account_id,
             worker_id = excluded.worker_id, device_id = excluded.device_id,
             recorded_at = excluded.recorded_at""",
        (str(result_id), account_id, worker, device_id, time.time()))

    # Publish on the sync stream (small metadata only, never PDFs, §19/§57).
    seq = append_event(
        type_=EVENT_CORRECTION_RESULT, payload=payload, school_id=school_id,
        class_id=class_id, account_id=account_id, device_id=device_id,
        worker_id=worker, client_event_id=payload.get("client_event_id"))

    # Reports are tracked SEPARATELY from the grade (§5): the grade is now
    # acknowledged, but the PDF has not been transferred yet. Failing to open
    # this row must never lose the grade, so it is deliberately best-effort.
    delivery = None
    report_ref = payload.get("report_filename") or payload.get("report_ref")
    if report_ref:
        try:
            from . import report_delivery
            delivery = report_delivery.record_pending(
                result_record_id=record_id,
                submission_id=submission_id,
                report_filename=payload.get("report_filename"),
                report_sha256=payload.get("report_sha256"),
                size_bytes=payload.get("report_size_bytes"))
        except Exception:  # noqa: BLE001 — never fail a grade over report metadata
            delivery = None

    # Duplicate = this (submission, version, worker) was already recorded.
    return {"record_id": record_id, "seq": seq,
            "duplicate": existing is not None,
            "report_delivery": (delivery or {}).get("state") if delivery else None}


def list_correction_results(*, account: dict | None = None, limit: int = 200,
                            school_id: str | None = None,
                            class_id: str | None = None) -> list[dict]:
    sql = ("SELECT * FROM correction_results_sync")
    clauses, params = [], []
    if class_id:
        clauses.append("class_id = ?")
        params.append(class_id)
    elif school_id:
        clauses.append("school_id = ?")
        params.append(school_id)
    if account is not None:
        schools, classes = accounts.scoped_ids(account)
        if schools or classes:
            # A limited role (CO_TEACHER/ASSISTANT) sees its own scoped
            # schools/classes PLUS the records shared by its assistant team
            # (§1). Team membership is resolved server-side on every read, so
            # a removed teammate immediately stops being visible and a new one
            # is visible at once. Unrelated schools stay invisible: they are
            # neither in scope nor a teammate.
            from . import teams
            shared = teams.shareable_account_ids(account)
            parts, inner = [], []
            if classes:
                parts.append("class_id IN (%s)" % ",".join("?" * len(classes)))
                inner.extend(classes)
            if schools:
                parts.append("(class_id IS NULL AND school_id IN (%s))"
                             % ",".join("?" * len(schools)))
                inner.extend(schools)
            if shared:
                parts.append("account_id IN (%s)" % ",".join("?" * len(shared)))
                inner.extend(sorted(shared))
            clauses.append("(" + " OR ".join(parts) + ")")
            params.extend(inner)
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    rows = db.query(sql + " ORDER BY received_at DESC LIMIT ?",
                    (*params, max(1, min(limit, 1000))))
    out = []
    for row in rows:
        item = dict(row)
        item["payload"] = json.loads(item.get("payload") or "{}")
        out.append(item)
    return out
