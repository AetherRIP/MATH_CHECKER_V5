"""Seed a throwaway data directory with a fully POPULATED Dashboard.

Why this exists: the Dashboard has a card for every kind of work the app can
hold (marking queue, throughput over time, pipeline, live jobs, system health,
class performance). Those cards only render their populated branches once real
rows exist, so a stock install — and any browser test against it — exercises the
empty states instead. This fixture creates the real rows through the app's own
schema and config so the browser tests can assert what the cards draw *and* that
nothing escapes the shell while they do it.

Safety: it never touches the live ``data/`` directory. It writes into the
directory given by ``--data-dir`` (or a fresh temp directory) and refuses to run
against a directory that already holds a real database unless the database
carries the fixture marker.

Usage
-----
Seed only (prints the directory, exits)::

    python backend/scripts/seed_dashboard_fixture.py --data-dir /tmp/mc5-fixture

Seed and serve, then wait (what the browser tests do — ONE line of JSON on
stdout tells the caller everything it needs)::

    FIXTURE_READY {"data_dir": "...", "url": "http://127.0.0.1:53121", ...}

The rows are deterministic: 2 courses, 4 assignments, 18 students, 14 results
spread over 27 days, 16 terminal jobs, 2 in-flight jobs (inserted AFTER boot so
the worker pool never claims them, which is what makes the live-activity card
deterministically populated), 4 MB of metered traffic and 2 synthetic API keys.
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))

HOUR = 3600.0
DAY = 24 * HOUR
READY_PREFIX = "FIXTURE_READY "
REFUSED_PREFIX = "FIXTURE_FAILED "

# ---------------------------------------------------------------- fixture data

COURSES = [
    ("fix-course-1", "Fixture — Year 10 Mathematics", "10A"),
    ("fix-course-2", "Fixture — Year 11 Mathematics", "11B"),
]

#: One entry per assignment. ``results`` is
#: ``(student_name, earned, available, review_required, seconds_ago)``;
#: ``unmarked`` are students who handed work in that has not been corrected.
#: ``cancelled``/``failed`` count jobs in those terminal states.
ASSIGNMENTS = [
    {
        "id": "fix-work-1", "course_id": "fix-course-1",
        "title": "Fixture — Algebra Homework", "due": "2026-06-12",
        "results": [
            ("Fixture Student A", 16, 20, 0, 1 * HOUR),
            ("Fixture Student B", 13, 20, 0, 2 * DAY),
            ("Fixture Student C", 18, 20, 0, 5 * DAY),
            ("Fixture Student D", 9, 20, 0, 27 * DAY),
        ],
        "unmarked": ["Fixture Student E", "Fixture Student F"],
        "failed": 0, "cancelled": 1,
    },
    {
        "id": "fix-work-2", "course_id": "fix-course-2",
        "title": "Fixture — Quadratics Paper", "due": "2026-06-19",
        "results": [
            ("Fixture Student G", 25, 25, 0, 2 * HOUR),
            ("Fixture Student H", 23, 25, 0, 3 * HOUR),
            ("Fixture Student I", 8, 25, 1, 12 * DAY),
            ("Fixture Student J", 14, 25, 0, 2 * DAY),
            ("Fixture Student K", 19, 25, 0, 5 * DAY),
        ],
        "unmarked": [],
        "failed": 0, "cancelled": 0,
    },
    {
        "id": "fix-work-3", "course_id": "fix-course-1",
        "title": "Fixture — Trigonometry HW", "due": "2026-06-26",
        "results": [
            ("Fixture Student L", 7, 12, 0, 12 * DAY),
            ("Fixture Student M", 9, 12, 0, 27 * DAY),
        ],
        "unmarked": ["Fixture Student N", "Fixture Student O"],
        "failed": 1, "cancelled": 0,
    },
    {
        "id": "fix-work-4", "course_id": "fix-course-2",
        "title": "Fixture — Fractions Review", "due": "2026-07-03",
        # Nothing outstanding: must NOT appear in the marking queue.
        "results": [
            ("Fixture Student P", 20, 25, 0, 5 * DAY),
            ("Fixture Student Q", 18, 25, 0, 12 * DAY),
            ("Fixture Student R", 24, 25, 0, 2 * DAY),
        ],
        "unmarked": [],
        "failed": 0, "cancelled": 0,
    },
]

#: In-flight work, inserted once the app answers health. The pool only claims
#: QUEUED rows, so MARKING/PREPARING rows written after boot stay exactly as
#: seeded — the live-activity card then has real, stable content.
LIVE_JOBS = [
    {
        "id": "fix-live-1", "status": "MARKING", "progress": 0.42,
        "stage_detail": "Marking question 3 of 7",
        "student_name": "Fixture Student E", "coursework_id": "fix-work-1",
        "course_id": "fix-course-1", "student_id": "fix-stu-e",
        "created_offset": 90.0, "started_offset": 80.0,
    },
    {
        "id": "fix-live-2", "status": "PREPARING", "progress": 0.12,
        "stage_detail": "Preparing 12-page submission",
        "student_name": "Fixture Student F", "coursework_id": "fix-work-1",
        "course_id": "fix-course-1", "student_id": "fix-stu-f",
        "created_offset": 30.0, "started_offset": 20.0,
    },
]

METERED_BYTES_UP = 2 * 1024 * 1024
METERED_BYTES_DOWN = 4 * 1024 * 1024

SYNTHETIC_KEYS = [f"AIzaSyFIXTURE{'0' * 22}{n:04d}" for n in (1, 2)]

CONFIG_TOML = """# Written by backend/scripts/seed_dashboard_fixture.py — a throwaway data
# directory for the browser tests. Never points at a real install.
[teacher]
teacher_name = "Fixture Teacher"
school_name = "Fixture School"
onboarded = true
onboarded_at = {onboarded_at:.0f}
"""


def _student_id(name: str) -> str:
    return "fix-stu-" + name.rsplit(" ", 1)[-1].lower()


def _submission_id(name: str) -> str:
    return "fix-sub-" + name.rsplit(" ", 1)[-1].lower()


def _percentage(earned: float, available: float) -> float:
    return round(earned / available * 100, 2) if available else 0.0


# ------------------------------------------------------------------- seeding


def _write_settings(data_dir: Path) -> None:
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "config.toml").write_text(
        CONFIG_TOML.format(onboarded_at=time.time()), encoding="utf-8")
    keys = ", ".join(f'"{k}"' for k in SYNTHETIC_KEYS)
    (data_dir / "secrets.toml").write_text(
        "# Synthetic keys for the fixture only - no network call is made.\n"
        f"[gemini]\napi_keys = [{keys}]\n",
        encoding="utf-8")


def _guard(data_dir: Path) -> None:
    """Refuse to seed a directory that already holds a non-fixture database."""
    data_dir.mkdir(parents=True, exist_ok=True)
    marker = data_dir / ".dashboard-fixture"
    db_file = data_dir / "app.db"
    if db_file.exists() and not marker.exists():
        print(REFUSED_PREFIX + json.dumps({
            "reason": "app.db already exists and is not a fixture",
            "data_dir": str(data_dir)}), flush=True)
        raise SystemExit(1)
    marker.write_text("seeded by seed_dashboard_fixture.py\n", encoding="utf-8")


def seed(data_dir: Path) -> dict:
    """Create the fixture rows. Idempotent: any previous fixture rows go first."""
    from app import db
    from app.config import ensure_dirs

    _guard(data_dir)
    _write_settings(data_dir)
    ensure_dirs()
    db.init_db()

    with db.transaction() as conn:
        conn.execute("DELETE FROM results WHERE id LIKE 'fix-%'")
        conn.execute("DELETE FROM correction_jobs WHERE id LIKE 'fix-%'")
        conn.execute("DELETE FROM classroom_submissions WHERE submission_id LIKE 'fix-%'")
        conn.execute("DELETE FROM submissions WHERE id LIKE 'fix-%'")
        conn.execute("DELETE FROM students WHERE id LIKE 'fix-%'")
        conn.execute("DELETE FROM coursework WHERE id LIKE 'fix-%'")
        conn.execute("DELETE FROM courses WHERE id LIKE 'fix-%'")
        conn.execute("DELETE FROM net_usage")

    now = time.time()
    results = 0
    jobs = 0
    with db.transaction() as conn:
        for course_id, name, section in COURSES:
            conn.execute(
                "INSERT INTO courses(id, name, section, state, update_time, fetched_at) "
                "VALUES(?,?,?,?,?,?)",
                (course_id, name, section, "ACTIVE", "2026-06-01T09:00:00Z", now))

        for a in ASSIGNMENTS:
            conn.execute(
                "INSERT INTO coursework(id, course_id, title, max_points, state, "
                "due_date, allowlisted, update_time, fetched_at, sync_state) "
                "VALUES(?,?,?,?,?,?,?,?,?,?)",
                (a["id"], a["course_id"], a["title"], 25.0, "PUBLISHED",
                 a["due"], 1, "2026-06-01T09:00:00Z", now, "ACTIVE"))

            roster = [r[0] for r in a["results"]] + list(a["unmarked"])
            for name in roster:
                sid, sub_id = _student_id(name), _submission_id(name)
                conn.execute(
                    "INSERT INTO students(id, name, email, fetched_at) "
                    "VALUES(?,?,?,?) ON CONFLICT(id) DO UPDATE SET name=excluded.name",
                    (sid, name, f"{sid}@fixture.invalid", now))
                conn.execute(
                    "INSERT INTO classroom_submissions(submission_id, course_id, "
                    "coursework_id, student_id, student_name, student_email, "
                    "current_state, late, assigned_grade, max_points, data_quality, "
                    "first_seen_at, last_synced_at, sync_version) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (sub_id, a["course_id"], a["id"], sid, name,
                     f"{sid}@fixture.invalid", "TURNED_IN", 0, None, 25.0,
                     "VERIFIED", now, now, 1))
                if name in a["unmarked"]:
                    # Handed in, not corrected: the Classroom sync table the
                    # dashboard counts "pending" from.
                    conn.execute(
                        "INSERT INTO submissions(id, coursework_id, course_id, "
                        "user_id, state, late, update_time, attachment_ids, fetched_at) "
                        "VALUES(?,?,?,?,?,?,?,?,?)",
                        (sub_id, a["id"], a["course_id"], sid, "TURNED_IN", 0,
                         "2026-06-01T09:00:00Z", json.dumps(["fix-drive-file"]), now))

            for idx, (name, earned, available, review, ago) in enumerate(a["results"]):
                sid, sub_id = _student_id(name), _submission_id(name)
                job_id = f"fix-job-{a['id'][-1]}-{idx + 1}"
                result_id = f"fix-res-{a['id'][-1]}-{idx + 1}"
                created = now - ago
                conn.execute(
                    "INSERT INTO correction_jobs(id, submission_id, coursework_id, "
                    "course_id, student_id, student_name, status, stage_detail, "
                    "progress, attempts, max_attempts, worker_id, created_at, "
                    "started_at, finished_at, updated_at, result_id) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (job_id, sub_id, a["id"], a["course_id"], sid, name, "COMPLETED",
                     "Correction complete", 1.0, 1, 3, "fixture-worker",
                     created, created + 1.0, created + 45.0, created + 45.0, result_id))
                jobs += 1
                conn.execute(
                    "INSERT INTO results(id, job_id, coursework_id, submission_id, "
                    "student_id, student_name, earned, available, percentage, "
                    "confidence, confidence_reasons, review_required, manual_override, "
                    "result_json, processing_seconds, created_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (result_id, job_id, a["id"], sub_id, sid, name, earned, available,
                     _percentage(earned, available),
                     "LOW" if review else "HIGH",
                     json.dumps(["Confidence low"]) if review else None,
                     review, 0,
                     json.dumps({"earned": earned, "available": available,
                                 "percentage": _percentage(earned, available),
                                 "questions": []}),
                     45.0, created))
                results += 1

            for n in range(a["failed"]):
                target = a["unmarked"][n] if n < len(a["unmarked"]) else "Fixture Student N"
                conn.execute(
                    "INSERT INTO correction_jobs(id, submission_id, coursework_id, "
                    "course_id, student_id, student_name, status, stage_detail, "
                    "progress, attempts, max_attempts, error, created_at, started_at, "
                    "finished_at, updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (f"fix-failed-{a['id'][-1]}-{n + 1}", _submission_id(target), a["id"],
                     a["course_id"], _student_id(target), target, "FAILED",
                     "Correction failed", 0.3, 3, 3,
                     "The provider returned 429 while marking question 4.",
                     now - 6 * HOUR, now - 6 * HOUR, now - 5 * HOUR, now - 5 * HOUR))
                jobs += 1

            for n in range(a["cancelled"]):
                conn.execute(
                    "INSERT INTO correction_jobs(id, submission_id, coursework_id, "
                    "course_id, student_id, student_name, status, progress, attempts, "
                    "max_attempts, created_at, started_at, finished_at, updated_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (f"fix-cancelled-{a['id'][-1]}-{n + 1}", None, a["id"],
                     a["course_id"], None, "Fixture Student A", "CANCELLED", 0.1, 1, 3,
                     now - 20 * HOUR, now - 20 * HOUR, now - 19 * HOUR, now - 19 * HOUR))
                jobs += 1

        from app.net_usage import bucket as hour_bucket
        for hour in range(4):
            conn.execute(
                "INSERT INTO net_usage(bucket, category, bytes_up, bytes_down) "
                "VALUES(?,?,?,?)",
                (hour_bucket(now) - hour * 3600, "gemini_generate",
                 METERED_BYTES_UP // 4, METERED_BYTES_DOWN // 4))

    students = sum(len(a["results"]) + len(a["unmarked"]) for a in ASSIGNMENTS)
    return {
        "data_dir": str(data_dir),
        "courses": len(COURSES),
        "assignments": len(ASSIGNMENTS),
        "students": students,
        "results": results,
        "jobs": jobs,
        "api_keys": len(SYNTHETIC_KEYS),
    }


def seed_live_jobs() -> int:
    """Insert the in-flight jobs. Called only once the app is serving."""
    from app import db
    now = time.time()
    with db.transaction() as conn:
        conn.execute("DELETE FROM correction_jobs WHERE id LIKE 'fix-live-%'")
        for job in LIVE_JOBS:
            conn.execute(
                "INSERT INTO correction_jobs(id, submission_id, coursework_id, "
                "course_id, student_id, student_name, status, stage_detail, progress, "
                "attempts, max_attempts, worker_id, created_at, started_at, updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (job["id"], None, job["coursework_id"], job["course_id"],
                 job["student_id"], job["student_name"], job["status"],
                 job["stage_detail"], job["progress"], 1, 3, "fixture-worker",
                 now - job["created_offset"], now - job["started_offset"], now))
    return len(LIVE_JOBS)


# ------------------------------------------------------------------- serving


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _wait_for_health(url: str, timeout: float = 90.0) -> None:
    import urllib.error
    import urllib.request
    deadline = time.time() + timeout
    last = "no attempt"
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"{url}/api/health", timeout=5) as res:
                if res.status < 500:
                    return
                last = f"HTTP {res.status}"
        except urllib.error.HTTPError as exc:  # answered, but not healthy yet
            last = f"HTTP {exc.code}"
        except Exception as exc:  # noqa: BLE001 — the server may not be listening yet
            last = str(exc)
        time.sleep(0.4)
    raise SystemExit(f"{REFUSED_PREFIX}{json.dumps({'reason': f'app never became healthy: {last}'})}")


def _serve(port: int, data_dir: Path, summary: dict) -> None:
    """Run the real app in-process on a free port until the caller kills us."""
    import uvicorn

    from app.main import app as fastapi_app

    server = uvicorn.Server(uvicorn.Config(
        fastapi_app, host="127.0.0.1", port=port, log_level="warning",
        access_log=False, timeout_graceful_shutdown=2))

    def run() -> None:  # uvicorn installs signal handlers only on the main thread
        try:
            server.run()
        except Exception as exc:  # noqa: BLE001 — surface the reason on stdout
            print(f"{REFUSED_PREFIX}{json.dumps({'reason': repr(exc)})}", flush=True)

    threading.Thread(target=run, name="fixture-server", daemon=True).start()
    url = f"http://127.0.0.1:{port}"
    _wait_for_health(url)
    # Post-boot on purpose: the pool only claims QUEUED rows, so a job written
    # here stays in the state the test asserts instead of being worked on.
    live = seed_live_jobs()
    print(f"{READY_PREFIX}{json.dumps({'data_dir': str(data_dir), 'url': url, 'summary': summary, 'live_jobs': live})}",
          flush=True)
    print(f"seeded {summary['results']} results and {live} in-flight jobs into "
          f"{data_dir}\nDashboard fixture serving {url} (Ctrl+C to stop)", flush=True)
    try:
        while not server.should_exit:
            time.sleep(0.3)
    except KeyboardInterrupt:  # pragma: no cover — Ctrl+C on a manual run
        server.should_exit = True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--data-dir", default=None,
                        help="where to seed (default: a new temp directory)")
    parser.add_argument("--serve", action="store_true",
                        help="run the app against the fixture and wait")
    parser.add_argument("--port", type=int, default=0,
                        help="port for --serve (0 = pick a free one)")
    args = parser.parse_args(argv)

    data_dir = Path(args.data_dir).resolve() if args.data_dir else \
        Path(tempfile.mkdtemp(prefix="mc5-dashboard-fixture-"))
    # MUST happen before app.config is imported anywhere.
    os.environ["MC5_DATA_DIR"] = str(data_dir)
    os.environ.pop("MC5_DEMO", None)   # a plain seeded dir, not demo mode

    summary = seed(data_dir)
    if not args.serve:
        print(f"{READY_PREFIX}{json.dumps({'data_dir': str(data_dir), 'url': None, 'summary': summary})}",
              flush=True)
        return 0
    _serve(args.port or _free_port(), data_dir, summary)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
