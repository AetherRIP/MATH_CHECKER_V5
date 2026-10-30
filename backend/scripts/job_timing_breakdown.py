#!/usr/bin/env python3
"""Break a correction job's wall clock into queue, preparation, upload,
provider and post-processing time — from the database, not from opinion.

    python backend/scripts/job_timing_breakdown.py
    python backend/scripts/job_timing_breakdown.py --json docs/results/QUEUE_BREAKDOWN.json

Why this exists
---------------
"Queue waiting time was 8 008 seconds" is not actionable on its own: it can mean
a scheduler that leaves workers idle, a batch that is deeper than the pool is
wide, or jobs that are simply slow. The job row alone cannot tell them apart —
it records ``created_at``, ``started_at`` and ``finished_at``, which lumps
preparation (#3) and provider time (#5) into one "processing" number.

``job_stage_events`` already records every real stage transition. This script
attributes every second between two stage events to the phase the job was in,
so the total is accounted for and nothing hides in a residual:

    queue      created -> first stage       (waiting for a worker)
    preparing  PDFs: download, normalise, page count, mark scheme
    uploading  files sent to the provider
    marking    the model's request(s), including in-process retries
    post       totals, double-check, result persistence
    idle       anything left over (explicitly reported, never hidden)

Privacy: reads no names, no marks and no student text. The optional JSON keeps
job ids (opaque UUIDs) and timings only, which is what makes a report
reproducible without publishing a student's paper.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import statistics
import sys
from collections import defaultdict
from pathlib import Path

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):  # pragma: no cover
        pass

REPO = Path(__file__).resolve().parents[2]

#: Stage status -> phase. Anything unlisted is attributed to the phase of the
#: status it followed, so a new stage name is accounted for instead of dropped.
PHASE_OF_STATUS = {
    "QUEUED": "queue",
    "WAITING_FOR_API": "queue",
    "RETRYING": "queue",
    "STARTING": "preparing",
    "PREPARING": "preparing",
    "UPLOADING": "uploading",
    "MARKING": "marking",
    "VERIFYING": "post",
    "COMPLETED": "post",
    "FAILED": "post",
    "CANCELLED": "post",
}

PHASES = ("queue", "preparing", "uploading", "marking", "post", "idle")


def _percentile(values: list[float], fraction: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int(round(fraction * (len(ordered) - 1)))))
    return ordered[index]


def _stats(values: list[float]) -> dict:
    if not values:
        return {"n": 0}
    return {
        "n": len(values),
        "median_s": round(statistics.median(values), 1),
        "p95_s": round(_percentile(values, 0.95), 1),
        "max_s": round(max(values), 1),
        "total_h": round(sum(values) / 3600, 2),
    }


def breakdown(db_path: Path) -> dict:
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    jobs = conn.execute(
        "SELECT id, created_at, started_at, finished_at, status, attempts, "
        "park_count, waited_seconds FROM correction_jobs "
        "WHERE created_at IS NOT NULL AND finished_at IS NOT NULL"
    ).fetchall()
    events: dict[str, list[tuple[float, str]]] = defaultdict(list)
    for row in conn.execute(
            "SELECT job_id, status, at FROM job_stage_events "
            "WHERE at IS NOT NULL ORDER BY at"):
        events[row["job_id"]].append((float(row["at"]), row["status"] or ""))
    conn.close()

    per_job: list[dict] = []
    for job in jobs:
        created = float(job["created_at"])
        finished = float(job["finished_at"])
        timeline = events.get(job["id"], [])
        spent = {phase: 0.0 for phase in PHASES}
        if not timeline:
            spent["idle"] = max(0.0, finished - created)
            per_job.append({"job_id": job["id"], "status": job["status"],
                            "queued_at": round(created, 3),
                            "seconds": spent, "events": 0})
            continue

        # Queue = nothing has started yet.
        spent["queue"] += max(0.0, timeline[0][0] - created)
        current = PHASE_OF_STATUS.get(timeline[0][1], "queue")
        for (stamp, _status), (next_stamp, next_status) in zip(
                timeline, timeline[1:]):
            gap = max(0.0, next_stamp - stamp)
            spent[current] += gap
            current = PHASE_OF_STATUS.get(next_status, current)
        spent[current] += max(0.0, finished - timeline[-1][0])
        per_job.append({"job_id": job["id"], "status": job["status"],
                        "queued_at": round(created, 3),
                        "seconds": {k: round(v, 1) for k, v in spent.items()},
                        "events": len(timeline),
                        "attempts": job["attempts"],
                        "parks": job["park_count"] or 0})

    phases = {phase: _stats([j["seconds"][phase] for j in per_job])
              for phase in PHASES}
    totals = {phase: round(sum(j["seconds"][phase] for j in per_job) / 3600, 2)
              for phase in PHASES}
    accounted = sum(totals.values())
    return {
        "jobs": len(per_job),
        "phases": phases,
        "hours_by_phase": totals,
        "hours_total": round(accounted, 2),
        "parks": sum(j.get("parks", 0) for j in per_job),
        "jobs_with_parks": sum(1 for j in per_job if j.get("parks")),
        "per_job": per_job,
    }


def simulate(report: dict, widths: range = range(1, 9),
             service_seconds: float | None = None) -> list[dict]:
    """FIFO queue wait this workload produces at each pool width.

    The point is to answer "is the queue a scheduling bug or arithmetic?" with
    a number: jobs are served first-in-first-out by ``w`` workers, each taking
    the service time actually measured for it, and the predicted total wait is
    compared with the observed one. If the observed wait matches a width the
    pool really ran at, no worker sat idle while a job was queued — the queue
    was simply ``depth x service time / width``.
    """
    jobs = sorted(
        ((entry.get("queued_at") or 0.0, entry) for entry in report["per_job"]),
        key=lambda pair: pair[0])
    base = jobs[0][0] if jobs else 0.0
    observed = round(sum((entry.get("seconds") or {}).get("queue", 0.0)
                         for _, entry in jobs) / 3600, 2)
    rows = []
    for width in widths:
        free: list[float] = []
        waited = 0.0
        for queued_at, entry in jobs:
            seconds = entry.get("seconds") or {}
            service = (service_seconds if service_seconds is not None else
                       sum(seconds.get(p, 0.0) for p in
                           ("preparing", "uploading", "marking", "post")))
            created = queued_at - base      # relative clock, same differences
            free.sort()
            if len(free) < width:
                start = created
            else:
                start = max(created, free.pop(0))
            waited += start - created
            free.append(start + service)
        rows.append({"workers": width, "predicted_queue_h": round(waited / 3600, 2)})
    return rows, observed


def _print(report: dict) -> None:
    print(f"jobs: {report['jobs']}   total wall clock: "
          f"{report['hours_total']} h across all jobs\n")
    print(f"{'phase':<10}{'n':>4}{'median':>10}{'p95':>10}{'max':>10}{'total h':>10}")
    for phase in PHASES:
        stats = report["phases"][phase]
        if not stats.get("n"):
            continue
        print(f"{phase:<10}{stats['n']:>4}{stats['median_s']:>9.0f}s"
              f"{stats['p95_s']:>9.0f}s{stats['max_s']:>9.0f}s"
              f"{stats['total_h']:>10.2f}")
    print(f"\ncapacity waits (parks): {report['parks']} across "
          f"{report['jobs_with_parks']} job(s)")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path,
                        default=REPO / "data" / "app.db",
                        help="path to app.db (default: ./data/app.db)")
    parser.add_argument("--json", type=Path, default=None,
                        help="also write the raw breakdown here")
    parser.add_argument("--simulate", action="store_true",
                        help="also predict queue wait for 1..8 workers")
    parser.add_argument("--service-seconds", type=float, default=None,
                        help="with --simulate: use this service time for every "
                             "job (e.g. a measured healthy paper) instead of the "
                             "measured archive times")
    args = parser.parse_args()

    if not args.db.is_file():
        print(f"no database at {args.db}")
        return 2
    report = breakdown(args.db)
    _print(report)
    if args.simulate:
        rows, observed = simulate(report, service_seconds=args.service_seconds)
        label = ("uniform service time" if args.service_seconds is not None
                 else "measured service times")
        print(f"\nFIFO model ({label}); observed queue wait = {observed} h")
        print(f"{'workers':>8}{'predicted queue h':>20}")
        for row in rows:
            print(f"{row['workers']:>8}{row['predicted_queue_h']:>20.2f}")
        report["queue_model"] = {"observed_queue_h": observed,
                                "prediction": rows,
                                "service_seconds": args.service_seconds}
    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps(report, indent=2) + "\n",
                             encoding="utf-8")
        print(f"\nwritten: {args.json}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
