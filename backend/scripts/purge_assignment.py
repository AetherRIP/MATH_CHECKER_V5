"""Targeted purge of ONE assignment's data from Math Corrector.

Removes every application-owned record and artifact traceable to a single
Google Classroom coursework id, leaving every other assignment (including a
same-titled assignment in a different class) untouched, and never touching
Google Classroom, Drive or live grades.

The operation is:
  * scoped     - only rows keyed to the given coursework id (and ids derived
                 from it) are removed; shared tables (students, gemini_keys,
                 net_usage, shared mark-scheme blueprints) are left in place;
  * backed up  - a timestamped copy of app.db plus every file scheduled for
                 deletion is written to data/purge_backups/<ts>/ with a
                 manifest.json, so the deletion is reversible;
  * verified   - after deleting, the script re-queries every table and asserts
                 the target is gone while a reference assignment is unchanged.

Usage::

    python scripts/purge_assignment.py --coursework-id 889601795010 \
        --confirm-purge 889601795010 [--data-dir ...] [--dry-run] [--no-backup]

Run it with the application STOPPED: the API/worker processes hold the SQLite
database open and could otherwise rewrite rows during the transaction.
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

# ---------------------------------------------------------------- table plan
# (table, where-clause, params-builder) — children before parents so no
# dangling references are ever introduced.
SUBMISSION_SCOPED = (
    "coursework_id = :cw OR submission_id IN (SELECT submission_id FROM "
    "classroom_submissions WHERE coursework_id = :cw) OR submission_id IN "
    "(SELECT id FROM submissions WHERE coursework_id = :cw)"
)
JOB_SCOPED = (
    "coursework_id = :cw OR job_id IN (SELECT id FROM correction_jobs "
    "WHERE coursework_id = :cw)"
)


def _tables(cw: str):
    """Ordered (table, SQL, params) deletion plan for one coursework."""
    return [
        # repair session (child then parent)
        ("repair_items", "coursework_id = :cw", {"cw": cw}),
        ("repair_runs", "coursework_id = :cw", {"cw": cw}),
        # automation
        ("automation_log", SUBMISSION_SCOPED, {"cw": cw}),
        ("automation_jobs", "coursework_id = :cw", {"cw": cw}),
        ("automation_assignments", "coursework_id = :cw", {"cw": cw}),
        # per-job bookkeeping
        ("job_remote_files",
         "job_id IN (SELECT id FROM correction_jobs WHERE coursework_id = :cw)",
         {"cw": cw}),
        ("job_bandwidth",
         "job_id IN (SELECT id FROM correction_jobs WHERE coursework_id = :cw)",
         {"cw": cw}),
        ("retained_files",
         "job_id IN (SELECT id FROM correction_jobs WHERE coursework_id = :cw)",
         {"cw": cw}),
        ("pending_manual_files",
         "job_id IN (SELECT id FROM correction_jobs WHERE coursework_id = :cw)",
         {"cw": cw}),
        # reports / canonical mapping
        ("canonical_reports", "coursework_id = :cw", {"cw": cw}),
        ("report_attachments", "coursework_id = :cw", {"cw": cw}),
        # results and their append-only history
        ("result_reviews",
         "result_id IN (SELECT id FROM results WHERE coursework_id = :cw)",
         {"cw": cw}),
        ("result_versions",
         "result_id IN (SELECT id FROM results WHERE coursework_id = :cw)",
         {"cw": cw}),
        ("results", "coursework_id = :cw OR submission_id IN "
                    "(SELECT id FROM submissions WHERE coursework_id = :cw)",
         {"cw": cw}),
        ("correction_jobs", "coursework_id = :cw OR submission_id IN "
                            "(SELECT submission_id FROM classroom_submissions "
                            "WHERE coursework_id = :cw)",
         {"cw": cw}),
        # classroom tracking + publication queue
        ("classroom_state_events",
         "submission_id IN (SELECT submission_id FROM classroom_submissions "
         "WHERE coursework_id = :cw) OR submission_id IN "
         "(SELECT id FROM submissions WHERE coursework_id = :cw)",
         {"cw": cw}),
        ("classroom_publish_ops", "coursework_id = :cw", {"cw": cw}),
        ("classroom_submissions", "coursework_id = :cw", {"cw": cw}),
        ("submissions", "coursework_id = :cw", {"cw": cw}),
        ("classroom_managed_assignments", "coursework_id = :cw", {"cw": cw}),
        ("pending_mark_schemes", "coursework_id = :cw", {"cw": cw}),
        ("grade_boundaries", "coursework_id = :cw", {"cw": cw}),
        # the coursework row itself, last
        ("coursework", "id = :cw", {"cw": cw}),
    ]


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def _collect_files(data_dir: Path, cw: str, job_ids: list[str]) -> list[Path]:
    """Every assignment-owned file we can trace, in deterministic order."""
    found: list[Path] = []

    def add(p: Path):
        if p.exists() and p.is_file():
            found.append(p)

    add(data_dir / "mark_schemes" / f"{cw}.pdf")
    prev = data_dir / "mark_schemes" / "_previous"
    if prev.is_dir():
        found.extend(sorted(p for p in prev.glob(f"{cw}-*.pdf") if p.is_file()))
    for jid in job_ids:
        d = data_dir / "tmp" / f"job-{jid}"
        if d.is_dir():
            found.extend(sorted(p for p in d.rglob("*") if p.is_file()))
    # audit artifacts that are exclusively this assignment
    for name in ("audit_findings.json", "audit_inventory.json",
                 "audit_consistency.json", "audit_findings_notes.json",
                 "repair_safety_snapshot.json"):
        add(data_dir / name)
    runs = data_dir / "audit_runs"
    if runs.is_dir():
        found.extend(sorted(p for p in runs.rglob("*") if p.is_file()))
    return found


def _dirs_to_prune(data_dir: Path, run_ids: list[str]) -> list[Path]:
    out = []
    for rid in run_ids:
        d = data_dir / "repairs" / rid
        if d.is_dir():
            out.append(d)
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--coursework-id", required=True)
    ap.add_argument("--confirm-purge", required=True,
                    help="must equal --coursework-id (safety interlock)")
    ap.add_argument("--data-dir", default=None)
    ap.add_argument("--reference-id", default=None,
                    help="a same-titled assignment whose rows must stay intact")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--no-backup", action="store_true")
    args = ap.parse_args()

    cw = args.coursework_id
    if args.confirm_purge != cw:
        print("refusing: --confirm-purge must equal --coursework-id")
        return 2

    data_dir = Path(args.data_dir) if args.data_dir else \
        Path(__file__).resolve().parents[2] / "data"
    db_path = data_dir / "app.db"
    if not db_path.exists():
        print(f"database not found: {db_path}")
        return 2

    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row

    cw_row = con.execute("SELECT * FROM coursework WHERE id = ?", (cw,)).fetchone()
    if cw_row is None:
        print(f"no coursework row with id {cw} — nothing to do")
        return 2
    course = con.execute("SELECT * FROM courses WHERE id = ?",
                         (cw_row["course_id"],)).fetchone()
    print("== target assignment ==")
    print(f"  coursework_id : {cw}")
    print(f"  title         : {cw_row['title']!r}")
    print(f"  course_id     : {cw_row['course_id']}")
    print(f"  class         : {course['name'] if course else '?'}")

    # Reference counts so verification can prove unrelated data survived.
    def count_ref(rid: str) -> dict:
        out = {}
        for t, w, p in _tables(rid):
            out[t] = con.execute(
                f"SELECT COUNT(*) FROM {t} WHERE {w}", {**p, "cw": rid}
            ).fetchone()[0]
        return out

    ref_before = count_ref(args.reference_id) if args.reference_id else None

    # ---- enumerate -------------------------------------------------------
    plan = _tables(cw)
    counts = {}
    for t, w, p in plan:
        counts[t] = con.execute(
            f"SELECT COUNT(*) FROM {t} WHERE {w}", p).fetchone()[0]

    job_ids = [r[0] for r in con.execute(
        "SELECT id FROM correction_jobs WHERE coursework_id = ?", (cw,))]
    run_ids = [r[0] for r in con.execute(
        "SELECT id FROM repair_runs WHERE coursework_id = ?", (cw,))]

    files = _collect_files(data_dir, cw, job_ids)
    dirs = _dirs_to_prune(data_dir, run_ids)

    print("== rows scheduled for deletion ==")
    for t, n in counts.items():
        if n:
            print(f"  {t:34s} {n}")
    print("== files scheduled for deletion ==")
    for p in files:
        print(f"  {p.relative_to(data_dir)}  ({p.stat().st_size} bytes)")
    for d in dirs:
        n = len([x for x in d.rglob('*') if x.is_file()])
        print(f"  {d.relative_to(data_dir)}/  ({n} files)")

    if args.dry_run:
        print("\n[dry-run] no changes made")
        return 0

    # ---- backup ----------------------------------------------------------
    backup_dir = None
    if not args.no_backup:
        stamp = time.strftime("%Y%m%d_%H%M%S")
        backup_dir = data_dir / "purge_backups" / f"{stamp}_{cw}"
        backup_dir.mkdir(parents=True, exist_ok=True)
        for src in (db_path, db_path.with_name("app.db-wal"),
                    db_path.with_name("app.db-shm")):
            if src.exists():
                shutil.copy2(src, backup_dir / src.name)
        manifest = {
            "generated_at": time.time(),
            "coursework_id": cw,
            "course_id": cw_row["course_id"],
            "title": cw_row["title"],
            "class": course["name"] if course else None,
            "coursework_row": dict(cw_row),
            "row_counts": counts,
            "files": [],
            "directories": [str(d.relative_to(data_dir)) for d in dirs],
        }
        for p in files:
            rel = p.relative_to(data_dir)
            dest = backup_dir / "files" / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(p, dest)
            manifest["files"].append(
                {"path": str(rel), "bytes": p.stat().st_size})
        (backup_dir / "manifest.json").write_text(
            json.dumps(manifest, indent=2), encoding="utf-8")
        print(f"\nbackup written to {backup_dir}")

    # ---- delete ----------------------------------------------------------
    deleted = {}
    with con:  # single transaction
        for t, w, p in plan:
            cur = con.execute(f"DELETE FROM {t} WHERE {w}", p)
            deleted[t] = cur.rowcount
    print("== rows deleted ==")
    for t, n in deleted.items():
        if n:
            print(f"  {t:34s} {n}")

    removed_files = []
    for p in files:
        try:
            p.unlink()
            removed_files.append(p)
        except OSError as exc:  # noqa: BLE001
            print(f"  WARN could not delete {p}: {exc}")
    for d in dirs:
        try:
            shutil.rmtree(d)
        except OSError as exc:  # noqa: BLE001
            print(f"  WARN could not delete {d}: {exc}")
    print(f"files removed: {len(removed_files)}")

    # ---- verify ----------------------------------------------------------
    print("== verification ==")
    ok = True
    plan2 = _tables(cw)
    for t, w, p in plan2:
        n = con.execute(f"SELECT COUNT(*) FROM {t} WHERE {w}", p).fetchone()[0]
        if n:
            ok = False
            print(f"  FAIL {t} still has {n} row(s)")
    print(f"  target rows remaining: 0 / "
          f"{'OK' if ok else 'FAILED'}")

    if args.reference_id and ref_before is not None:
        ref_after = count_ref(args.reference_id)
        diff = {t: (ref_before[t], ref_after[t])
                for t in ref_before if ref_before[t] != ref_after[t]}
        if diff:
            ok = False
            print(f"  FAIL reference assignment changed: {diff}")
        else:
            print(f"  reference {args.reference_id} unchanged: OK")

    # report-level sanity: no result/publish/job left referencing the target
    leftovers = con.execute(
        "SELECT (SELECT COUNT(*) FROM results WHERE coursework_id=?) r, "
        "(SELECT COUNT(*) FROM classroom_publish_ops WHERE coursework_id=?) p, "
        "(SELECT COUNT(*) FROM correction_jobs WHERE coursework_id=?) j, "
        "(SELECT COUNT(*) FROM classroom_submissions WHERE coursework_id=?) c",
        (cw, cw, cw, cw)).fetchone()
    print(f"  residual results/publish/jobs/tracking rows: {tuple(leftovers)}")

    con.close()
    print("\nPURGE", "OK" if ok else "INCOMPLETE")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
