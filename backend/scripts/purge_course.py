"""Targeted purge of ONE Google Classroom COURSE's data from Math Correct.

Removes every application-owned record and artifact traceable to a single
Classroom course id (all of its coursework, submissions, corrections, reports,
tracking, automation, repair and large-PDF rows), plus the course row itself,
while leaving every other class and all shared/global resources untouched.

Never touches Google Classroom or Google Drive.

The operation is scoped, backed up (a timestamped copy of app.db + every file
scheduled for deletion + manifest.json) and self-verifying, exactly like
``purge_assignment.py``.

Usage::

    python scripts/purge_course.py --course-id 886515702341 \
        --confirm-purge 886515702341 [--data-dir ...] [--dry-run] [--no-backup]

Run it with the application STOPPED (the API/worker processes hold the SQLite
database open and run the automation scheduler).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sqlite3
import sys
import time
from pathlib import Path

# Subquery for every coursework id owned by the course being purged.
_CW = "(SELECT id FROM coursework WHERE course_id = :co)"
_JOBS = "(SELECT id FROM correction_jobs WHERE course_id = :co)"


def _plan() -> list[tuple[str, str]]:
    """Ordered (table, WHERE) deletion plan; children before parents."""
    return [
        ("repair_items", f"coursework_id IN {_CW}"),
        ("repair_runs", f"course_id = :co OR coursework_id IN {_CW}"),
        ("automation_log", f"course_id = :co OR coursework_id IN {_CW}"),
        ("automation_jobs", "course_id = :co"),
        ("automation_assignments", f"coursework_id IN {_CW}"),
        ("job_remote_files", f"job_id IN {_JOBS}"),
        ("job_bandwidth", f"job_id IN {_JOBS}"),
        ("retained_files", f"job_id IN {_JOBS}"),
        ("pending_manual_files", f"job_id IN {_JOBS}"),
        ("canonical_reports", "course_id = :co"),
        ("report_attachments", "course_id = :co"),
        ("result_reviews",
         f"result_id IN (SELECT id FROM results WHERE coursework_id IN {_CW})"),
        ("result_versions",
         f"result_id IN (SELECT id FROM results WHERE coursework_id IN {_CW})"),
        ("results",
         f"coursework_id IN {_CW} OR submission_id IN "
         f"(SELECT id FROM submissions WHERE course_id = :co)"),
        ("correction_jobs", "course_id = :co"),
        ("classroom_state_events",
         f"submission_id IN (SELECT submission_id FROM classroom_submissions "
         f"WHERE course_id = :co) OR submission_id IN "
         f"(SELECT id FROM submissions WHERE course_id = :co)"),
        ("classroom_publish_ops", "course_id = :co"),
        ("classroom_submissions", "course_id = :co"),
        ("submissions", "course_id = :co"),
        ("classroom_managed_assignments", "course_id = :co"),
        ("pending_mark_schemes", f"coursework_id IN {_CW}"),
        ("grade_boundaries", f"coursework_id IN {_CW}"),
        ("coursework", "course_id = :co"),
        ("courses", "id = :co"),
    ]


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def _files_for(data_dir: Path, course_ids: list[str],
               job_ids: list[str]) -> list[Path]:
    found: list[Path] = []
    ms_dir = data_dir / "mark_schemes"
    for cid in course_ids:
        p = ms_dir / f"{cid}.pdf"
        if p.is_file():
            found.append(p)
        if (ms_dir / "_previous").is_dir():
            found.extend(sorted(q for q in (ms_dir / "_previous").glob(f"{cid}-*.pdf")
                                if q.is_file()))
    for jid in job_ids:
        d = data_dir / "tmp" / f"job-{jid}"
        if d.is_dir():
            found.extend(sorted(q for q in d.rglob("*") if q.is_file()))
    return found


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--course-id", required=True)
    ap.add_argument("--confirm-purge", required=True)
    ap.add_argument("--data-dir", default=None)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--no-backup", action="store_true")
    args = ap.parse_args()

    co = args.course_id
    if args.confirm_purge != co:
        print("refusing: --confirm-purge must equal --course-id")
        return 2

    data_dir = Path(args.data_dir) if args.data_dir else \
        Path(__file__).resolve().parents[2] / "data"
    db_path = data_dir / "app.db"
    if not db_path.exists():
        print(f"database not found: {db_path}")
        return 2

    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    course = con.execute("SELECT * FROM courses WHERE id = ?", (co,)).fetchone()
    if course is None:
        print(f"no course row with id {co} — nothing to do")
        return 2

    coursework = [dict(r) for r in con.execute(
        "SELECT id, title FROM coursework WHERE course_id = ? ORDER BY id", (co,))]
    cw_ids = [c["id"] for c in coursework]
    print("== target class ==")
    print(f"  course_id : {co}")
    print(f"  name      : {course['name']!r}  section={course['section']!r}")
    print(f"  coursework: {len(cw_ids)}")
    for c in coursework:
        print(f"     - {c['id']}  {c['title']!r}")

    p = {"co": co}
    plan = _plan()
    counts = {t: con.execute(f"SELECT COUNT(*) FROM {t} WHERE {w}", p).fetchone()[0]
              for t, w in plan}

    job_ids = [r[0] for r in con.execute(
        "SELECT id FROM correction_jobs WHERE course_id = ?", (co,))]
    files = _files_for(data_dir, cw_ids, job_ids)

    print("== rows scheduled for deletion ==")
    for t, n in counts.items():
        if n:
            print(f"  {t:30s} {n}")
    print("  TOTAL", sum(counts.values()))
    print("== files scheduled for deletion ==")
    for f in files:
        print(f"  {f.relative_to(data_dir)}  ({f.stat().st_size} bytes)")

    if args.dry_run:
        print("\n[dry-run] no changes made")
        return 0

    backup_dir = None
    if not args.no_backup:
        stamp = time.strftime("%Y%m%d_%H%M%S")
        backup_dir = data_dir / "purge_backups" / f"{stamp}_course_{co}"
        backup_dir.mkdir(parents=True, exist_ok=True)
        for src in (db_path, db_path.with_name("app.db-wal"),
                    db_path.with_name("app.db-shm")):
            if src.exists():
                shutil.copy2(src, backup_dir / src.name)
        manifest = {
            "generated_at": time.time(), "course_id": co,
            "name": course["name"], "section": course["section"],
            "coursework": coursework, "row_counts": counts, "files": [],
        }
        for f in files:
            rel = f.relative_to(data_dir)
            dest = backup_dir / "files" / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(f, dest)
            manifest["files"].append({"path": str(rel), "bytes": f.stat().st_size})
        (backup_dir / "manifest.json").write_text(
            json.dumps(manifest, indent=2), encoding="utf-8")
        print(f"\nbackup written to {backup_dir}")

    # hashes of the mark schemes we are about to delete (for blueprint GC)
    doomed_shas = {}
    for f in files:
        if f.parent.name == "mark_schemes" or f.parent.name == "_previous":
            doomed_shas.setdefault(_sha256(f), f)

    deleted = {}
    with con:
        for t, w in plan:
            deleted[t] = con.execute(f"DELETE FROM {t} WHERE {w}", p).rowcount
    print("== rows deleted ==")
    for t, n in deleted.items():
        if n:
            print(f"  {t:30s} {n}")

    removed = 0
    for f in files:
        try:
            f.unlink()
            removed += 1
        except OSError as exc:  # noqa: BLE001
            print(f"  WARN could not delete {f}: {exc}")
    print(f"files removed: {removed}")

    # Garbage-collect mark-scheme blueprints that no remaining file uses.
    surviving_shas = set()
    for q in (data_dir / "mark_schemes").glob("*.pdf"):
        surviving_shas.add(_sha256(q))
    bp_dir = data_dir / "blueprints"
    gc_removed = []
    for sha in doomed_shas:
        if sha in surviving_shas:
            continue
        bp = bp_dir / f"{sha}.json"
        if bp.is_file():
            bp.unlink()
            gc_removed.append(bp.name)
    if gc_removed:
        print(f"blueprints removed (now unused): {gc_removed}")

    # ---- verify ---------------------------------------------------------
    print("== verification ==")
    ok = True
    for t, w in plan:
        n = con.execute(f"SELECT COUNT(*) FROM {t} WHERE {w}", p).fetchone()[0]
        if n:
            ok = False
            print(f"  FAIL {t} still has {n} row(s)")
    print(f"  target rows remaining: 0 / {'OK' if ok else 'FAILED'}")

    remaining_courses = con.execute(
        "SELECT COUNT(*) FROM courses").fetchone()[0]
    remaining_cw = con.execute(
        "SELECT COUNT(*) FROM coursework").fetchone()[0]
    print(f"  courses remaining: {remaining_courses} | coursework remaining: {remaining_cw}")
    if con.execute("SELECT COUNT(*) FROM courses WHERE id = ?", (co,)).fetchone()[0]:
        ok = False
        print("  FAIL course row still present")
    con.close()
    print("\nPURGE", "OK" if ok else "INCOMPLETE")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
