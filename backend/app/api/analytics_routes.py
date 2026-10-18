"""Read-only analytics aggregates for the redesigned dashboard (§UI redesign).

Three questions the dashboard asks that no existing endpoint answers in one
place:

* marking throughput over time   -> ``GET /api/analytics/activity``
* processing-time health         -> folded into ``/analytics/activity``
* is a class improving           -> ``GET /api/analytics/class/{course_id}/trend``

Everything is a plain aggregate over rows the app already stores (``results``,
``correction_jobs``, ``coursework``). No schema change, no new write path, no
model involvement. Numbers that cannot be supported by stored rows are not
returned — an empty day is an empty day.

Timestamps are bucketed in the server's local time so "today" matches what the
teacher sees on the Dashboard's existing ``corrections_today`` counter.
"""

from __future__ import annotations

import time
from datetime import date, timedelta

from fastapi import APIRouter

from .. import db

router = APIRouter(prefix="/api/analytics")


def register(app) -> None:
    app.include_router(router)


def _day_key(ts: float) -> str:
    return date.fromtimestamp(float(ts)).isoformat()


def _day_window(days: int) -> tuple[float, list[str]]:
    """``(window_start_epoch, [iso day keys])``, oldest first, today last."""
    today = date.fromtimestamp(time.time())
    keys = [(today - timedelta(days=i)).isoformat() for i in range(days - 1, -1, -1)]
    start = time.mktime(time.strptime(keys[0], "%Y-%m-%d"))
    return start, keys


@router.get("/activity")
def activity(days: int = 30):
    """Marking throughput and processing health, one bucket per day.

    ``days`` is clamped to 1..365. Each bucket reports only counts derivable
    from stored rows: results created (papers marked), how many of those need
    review, jobs started, jobs failed, the average mark percentage scored that
    day and the average processing time. A day with no work shows zeros and a
    null average — never a fabricated value.
    """
    days = max(1, min(int(days or 30), 365))
    start, keys = _day_window(days)

    buckets = {k: {
        "day": k,
        "results": 0,
        "needs_review": 0,
        "manual_overrides": 0,
        "jobs_started": 0,
        "jobs_failed": 0,
        "avg_percentage": None,
        "avg_processing_seconds": None,
        "_pct_sum": 0.0, "_pct_n": 0,
        "_sec_sum": 0.0, "_sec_n": 0,
    } for k in keys}

    for row in db.query(
            "SELECT created_at, percentage, review_required, manual_override, "
            "processing_seconds FROM results WHERE created_at >= ?", (start,)):
        b = buckets.get(_day_key(row["created_at"]))
        if b is None:
            continue
        b["results"] += 1
        b["needs_review"] += 1 if row["review_required"] else 0
        b["manual_overrides"] += 1 if row["manual_override"] else 0
        b["_pct_sum"] += float(row["percentage"] or 0.0)
        b["_pct_n"] += 1
        if row["processing_seconds"] is not None:
            b["_sec_sum"] += float(row["processing_seconds"])
            b["_sec_n"] += 1

    for row in db.query(
            "SELECT created_at, finished_at, status FROM correction_jobs "
            "WHERE created_at >= ?", (start,)):
        b = buckets.get(_day_key(row["created_at"]))
        if b is not None:
            b["jobs_started"] += 1
        stamp = row["finished_at"] or row["created_at"]
        b2 = buckets.get(_day_key(stamp))
        if b2 is not None and str(row["status"]).upper() in ("FAILED", "CANCELLED"):
            b2["jobs_failed"] += 1

    out = []
    for k in keys:
        b = buckets[k]
        b["avg_percentage"] = round(b["_pct_sum"] / b["_pct_n"], 2) if b["_pct_n"] else None
        b["avg_processing_seconds"] = (
            round(b["_sec_sum"] / b["_sec_n"], 1) if b["_sec_n"] else None)
        del b["_pct_sum"], b["_pct_n"], b["_sec_sum"], b["_sec_n"]
        out.append(b)

    totals = {
        "results": sum(b["results"] for b in out),
        "needs_review": sum(b["needs_review"] for b in out),
        "manual_overrides": sum(b["manual_overrides"] for b in out),
        "jobs_started": sum(b["jobs_started"] for b in out),
        "jobs_failed": sum(b["jobs_failed"] for b in out),
    }
    return {"days": out, "window_days": days, "totals": totals,
            "generated_at": time.time()}


@router.get("/class/{course_id}/trend")
def class_trend(course_id: str):
    """Per-assignment performance for one class, oldest first.

    One point per assignment the class has corrected work for: papers checked,
    the average Math Checker percentage, and how many need teacher review.
    Ordered by each assignment's first result so the chart reads as a
    chronological trend without inventing a time axis. Assignments with no
    corrected work are omitted rather than shown as zero performance.
    """
    rows = db.query(
        "SELECT r.coursework_id AS coursework_id, "
        "       MIN(r.created_at) AS first_at, "
        "       COUNT(*) AS checked, "
        "       AVG(r.percentage) AS average, "
        "       SUM(CASE WHEN r.review_required = 1 THEN 1 ELSE 0 END) AS needs_review, "
        "       SUM(CASE WHEN r.manual_override = 1 THEN 1 ELSE 0 END) AS manual_overrides "
        "FROM results r JOIN coursework w ON w.id = r.coursework_id "
        "WHERE w.course_id = ? AND r.coursework_id IS NOT NULL "
        "GROUP BY r.coursework_id ORDER BY first_at ASC",
        (course_id,))

    titles = {r["id"]: r["title"] for r in db.query(
        "SELECT id, title FROM coursework WHERE course_id = ?", (course_id,))}

    points = []
    for row in rows:
        points.append({
            "coursework_id": row["coursework_id"],
            "title": titles.get(row["coursework_id"]) or row["coursework_id"],
            "checked": int(row["checked"] or 0),
            "average": round(float(row["average"]), 2) if row["average"] is not None else None,
            "needs_review": int(row["needs_review"] or 0),
            "manual_overrides": int(row["manual_overrides"] or 0),
            "first_result_at": float(row["first_at"]),
        })

    overall = None
    if points:
        weighted = sum((p["average"] or 0.0) * p["checked"] for p in points)
        checked = sum(p["checked"] for p in points)
        overall = round(weighted / checked, 2) if checked else None

    return {"course_id": course_id, "assignments": points,
            "overall_average": overall, "generated_at": time.time()}
