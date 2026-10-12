"""Assignment repair session — audit every correction, repair what is broken.

TESTING ONLY · REPAIRED RESULTS NOT PUBLISHED.

For the given assignment (default: C1L13 - Sets, O Level Math) this driver:

1. inventories EVERY submission and EVERY stored correction (uncorrected and
   inaccessible submissions are listed explicitly, never silently dropped);
2. audits each stored correction against the AUTHORITATIVE mark scheme with
   the deterministic audit (maxima, denominators, unmarked-as-zero, silent
   zeros, duplicates, totals);
3. re-marks every accessible submission through the CORRECTED pipeline in an
   ISOLATED data dir (subprocess + ``MC5_DATA_DIR``) — a genuine second pass,
   never touching ``results`` or any Google write surface;
4. stores the repaired results as dedicated test records
   (``repair_runs``/``repair_items`` + ``data/repairs/``) with a per-question
   diff, evidence, criteria and validation status;
5. renders a repaired report PDF per submission for in-app preview.

Usage (from backend/):
    python scripts/repair_assignment.py                      # whole assignment
    python scripts/repair_assignment.py --only "Alia Darwish"
    python scripts/repair_assignment.py --skip-marking       # audit-only
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
ROOT = BACKEND.parent
sys.path.insert(0, str(BACKEND))
os.environ.setdefault("MC5_LOGIN_REQUIRED", "0")

DEFAULT_COURSEWORK = "889601795010"   # Ch1L13 - Sets (O Level Math)
DEFAULT_COURSE = "877926720424"       # WBIS - O Level Mathematics


# --------------------------------------------------------------- downloads
def _download(sub: dict, dest_dir: Path) -> tuple[Path | None, list[str]]:
    """Download the student's work from Drive (READ-ONLY GET). Returns the
    merged PDF path and an explicit list of anything that could not be used."""
    from app.classroom.auth import get_access_token
    from app.classroom.client import download_drive_file

    notes: list[str] = []
    blobs: list[tuple[str, bytes]] = []
    seen: set[str] = set()
    token = get_access_token()
    for att in sub.get("attachments") or []:
        kind = att.get("kind")
        title = str(att.get("title") or "")
        if kind == "link":
            notes.append(f"link-only attachment not downloadable: {title[:80]}")
            continue
        if title.lower().endswith(".zip"):
            notes.append(f"invalid submission type (archive): {title[:80]}")
            continue
        file_id = att.get("id")
        if not file_id:
            continue
        try:
            name, content, _mime = download_drive_file(token, file_id)
        except Exception as exc:  # noqa: BLE001 — record, never drop silently
            notes.append(f"download failed for {title[:60]}: {str(exc)[:150]}")
            continue
        digest = hashlib.sha256(content).hexdigest()
        if digest in seen:
            notes.append(f"duplicate file skipped: {title[:60]}")
            continue
        seen.add(digest)
        blobs.append((name or title, content))

    if not blobs:
        return None, notes
    dest_dir.mkdir(parents=True, exist_ok=True)
    # Multi-file scans (page-per-file uploads) merge in file-name order —
    # the timestamps in the titles are ascending and page order must hold.
    blobs.sort(key=lambda pair: pair[0])
    if len(blobs) == 1:
        out = dest_dir / "student.pdf"
        out.write_bytes(blobs[0][1])
        return out, notes
    from io import BytesIO

    from pypdf import PdfReader, PdfWriter
    writer = PdfWriter()
    pages = 0
    for _name, content in blobs:
        try:
            reader = PdfReader(BytesIO(content))
        except Exception as exc:  # noqa: BLE001
            notes.append(f"unreadable file skipped: {str(exc)[:120]}")
            continue
        for page in reader.pages:
            writer.add_page(page)
            pages += 1
    out = dest_dir / "student.pdf"
    with open(out, "wb") as handle:
        writer.write(handle)
    notes.append(f"merged {len(blobs)} file(s) into {pages} page(s)")
    return out, notes


# ------------------------------------------------------------- isolated run
def _seed_env(env_dir: Path, coursework_id: str) -> Path:
    import shutil

    from app.config import DATA_DIR as PROD_DATA
    env_dir.mkdir(parents=True, exist_ok=True)
    for name in ("config.toml", "secrets.toml"):
        src = Path(PROD_DATA) / name
        if src.exists():
            shutil.copy2(src, env_dir / name)
    bp = Path(PROD_DATA) / "blueprints"
    if bp.exists():
        shutil.copytree(bp, env_dir / "blueprints", dirs_exist_ok=True)
    (env_dir / "mark_schemes").mkdir(exist_ok=True)
    shutil.copy2(Path(PROD_DATA) / "mark_schemes" / f"{coursework_id}.pdf",
                 env_dir / "mark_schemes" / f"{coursework_id}.pdf")
    return env_dir / "mark_schemes" / f"{coursework_id}.pdf"


def _mark(env_dir: Path, student_pdf: Path, scheme: Path, label: str,
          out_path: Path, max_seconds: int) -> dict:
    cmd = [sys.executable, str(BACKEND / "scripts" / "repair_mark_one.py"),
           "--data-dir", str(env_dir), "--student-name", label,
           "--student-pdf", str(student_pdf), "--mark-scheme", str(scheme),
           "--out", str(out_path), "--max-seconds", str(max_seconds)]
    proc = subprocess.run(cmd, capture_output=True, text=True,
                          timeout=max_seconds + 120)
    if proc.returncode != 0:
        return {"error": (proc.stderr or proc.stdout or "")[-800:] or
                f"marker exited {proc.returncode}"}
    return json.loads(out_path.read_text(encoding="utf-8"))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--coursework-id", default=DEFAULT_COURSEWORK)
    ap.add_argument("--course-id", default=DEFAULT_COURSE)
    ap.add_argument("--only", default="",
                    help="comma-separated student names to process")
    ap.add_argument("--skip-marking", action="store_true",
                    help="audit + inventory only (no API calls)")
    ap.add_argument("--max-seconds", type=int, default=2400)
    args = ap.parse_args()
    only = {s.strip() for s in args.only.split(",") if s.strip()}

    from app import db
    from app.repair import pipeline, store

    db.init_db()
    cw = args.coursework_id

    # ---- 1. inventory ------------------------------------------------------
    inventory = pipeline.assignment_inventory(cw)
    print(f"TESTING ONLY — assignment {cw}")
    print(f"submissions: {inventory['counts']}")
    for row in inventory["submissions"]:
        if row["submitted"]:
            print(f"  {row['student_name']:<24} {row['state']:<10} "
                  f"correction={'YES' if row['has_correction'] else 'no ':<3} "
                  f"accessible={row['accessible']} {row['note']}")

    # ---- 2. deterministic audit of every stored correction -----------------
    from app.repair.routes import blueprint_for_coursework
    blueprint = blueprint_for_coursework(cw)
    if blueprint is None:
        print("!! no authoritative mark-scheme blueprint — cannot validate")
        return 2
    results = [db.row_to_dict(r) for r in db.query(
        "SELECT * FROM results WHERE coursework_id = ?", (cw,))]
    originals = {str(r["student_id"]): r for r in results}
    print("\n== deterministic audit of stored corrections ==")
    audit_by_student: dict[str, list] = {}
    for row in results:
        findings = pipeline.audit_stored_result(row, blueprint)
        audit_by_student[str(row["student_id"])] = findings
        flags = ", ".join(sorted({f.code for f in findings})) or "clean"
        print(f"  {row['student_name']:<24} {row['earned']}/{row['available']} "
              f"— {flags}")

    run = store.create_run(
        coursework_id=cw, course_id=args.course_id,
        assignment_title=_title(cw), class_name=_class_name(args.course_id))
    print("\nrepair run:", run["id"])

    # ---- 3-4. re-mark + repair records ------------------------------------
    summary_rows = []
    for sub in inventory["submissions"]:
        if not sub["submitted"]:
            continue
        if only and sub["student_name"] not in only:
            continue
        name = sub["student_name"]
        original = originals.get(str(sub["student_id"]))
        base = {
            "coursework_id": cw,
            "submission_id": sub["submission_id"],
            "student_id": sub["student_id"],
            "student_name": name,
            "original_result_id": original["id"] if original else None,
            "original_earned": original["earned"] if original else None,
            "original_available": original["available"] if original else None,
            "original_percentage": original["percentage"] if original else None,
            "questions_total": len(blueprint.get("questions") or []),
        }
        orig_rows = None
        if original is not None:
            orig_rows = (db.loads(original.get("result_json") or "{}")
                         or {}).get("questions") or []
        base["audit_findings"] = [
            {"code": f.code, "severity": f.severity, "question": f.question,
             "detail": f.detail}
            for f in audit_by_student.get(str(sub["student_id"]), [])]

        # ---- blocked sources are recorded, never silently skipped ----------
        if not sub["accessible"]:
            item = {**base, "mode": "BLOCKED", "status": "BLOCKED",
                    "error": sub["note"], "diff": [], "validation": {},
                    "artifact": {"note": sub["note"]}}
            store.add_item(run["id"], item)
            summary_rows.append((name, "BLOCKED", None, None, sub["note"]))
            print(f"  {name:<24} BLOCKED — {sub['note']}")
            continue

        dest = store.repairs_dir() / run["id"] / "source" / _slug(name)
        pdf, notes = _download(sub, dest)
        if pdf is None:
            item = {**base, "mode": "BLOCKED", "status": "BLOCKED",
                    "error": "; ".join(notes) or "no usable attachment",
                    "diff": [], "validation": {},
                    "artifact": {"notes": notes}}
            store.add_item(run["id"], item)
            summary_rows.append((name, "BLOCKED", None, None,
                                 "; ".join(notes)))
            print(f"  {name:<24} BLOCKED — {'; '.join(notes)}")
            continue

        if args.skip_marking:
            summary_rows.append((name, "AUDITED", None, None,
                                 "audit only (--skip-marking)"))
            continue

        # ---- re-mark through the corrected pipeline (isolated) -------------
        env_dir = Path(os.environ.get("TEMP") or "/tmp") / \
            f"mc5_repair_{run['id'][:8]}_{_slug(name)}"
        scheme = _seed_env(env_dir, cw)
        print(f"  {name:<24} marking {pdf.name} ({pdf.stat().st_size} bytes) "
              f"in {env_dir.name} …", flush=True)
        dump_path = env_dir / "result_dump.json"
        dump = _mark(env_dir, pdf, scheme, f"REPAIR-{name}", dump_path,
                     args.max_seconds)
        error = dump.get("error")
        repaired_result = (dump or {}).get("result") or {}
        rep_rows = ((repaired_result.get("result_json") or {})
                    .get("questions") or [])
        if error and not rep_rows:
            item = {**base, "mode": "SECOND_PASS" if original else "NEW",
                    "status": "FAILED", "error": str(error)[:800],
                    "diff": [], "validation": {}, "artifact": dump}
            store.add_item(run["id"], item)
            summary_rows.append((name, "FAILED", None, None, str(error)[:60]))
            print(f"  {name:<24} FAILED — {str(error)[:120]}")
            continue

        diff = pipeline.compare_rows(original=orig_rows, repaired=rep_rows,
                                     blueprint=blueprint)
        totals = (repaired_result.get("result_json") or {}).get("totals") or {}
        validation = pipeline.validate_repair(
            questions=rep_rows,
            earned=repaired_result.get("earned"),
            available=repaired_result.get("available"),
            blueprint=blueprint, score_basis=totals.get("score_basis"),
            unresolved_marks=totals.get("unresolved_marks"))
        status = pipeline.item_status(diff, validation, None)
        changed = sum(1 for d in diff if d["change"] == "CHANGED")
        added = sum(1 for d in diff if d["change"] == "ADDED")
        flagged = sum(1 for d in diff if d["validation"] ==
                      pipeline.TEACHER_CONFIRM)
        fixed = sum(1 for d in diff if d["change"] == pipeline.DETERMINISTIC_FIX)
        missing_fixed = sum(1 for d in diff if d["change"] == "ADDED")
        zeros_fixed = sum(1 for d in diff
                          if d["change"] == "ADDED"
                          and (d.get("original_status") in
                               ("REVIEW", "NEEDS_REVIEW", "UNMARKED", "MISSING")
                               or d.get("original_awarded") == 0))
        unresolved = len(validation.get("unresolved_questions") or [])

        report_name = None
        try:
            from app.classroom.report_attachment import render_report_pdf
            content = render_report_pdf(
                {**repaired_result, "result_json": repaired_result.get(
                    "result_json") or {}}, detail="standard")
            rep_dir = store.repairs_dir() / run["id"] / "reports"
            rep_dir.mkdir(parents=True, exist_ok=True)
            report_name = f"{_slug(name)}_repaired_report.pdf"
            (rep_dir / report_name).write_bytes(content)
        except Exception as exc:  # noqa: BLE001 — report preview is optional
            notes.append(f"report render failed: {str(exc)[:120]}")

        item = {
            **base,
            "mode": "SECOND_PASS" if original else "NEW",
            "status": status,
            "repaired_earned": repaired_result.get("earned"),
            "repaired_available": repaired_result.get("available"),
            "repaired_percentage": repaired_result.get("percentage"),
            "repaired_grade": repaired_result.get("provisional_grade"),
            "score_basis": totals.get("score_basis"),
            "questions_added": added,
            "questions_changed": changed,
            "questions_flagged": flagged,
            "missing_fixed": missing_fixed,
            "zeros_fixed": zeros_fixed,
            "unresolved_count": unresolved,
            "diff": diff,
            "validation": validation,
            "report_filename": report_name,
            "artifact": {
                "notes": notes,
                "marker_dump": {k: dump.get(k) for k in
                                ("job_id", "status", "recovery", "batches")},
                "original_result_json": (original or {}).get("result_json"),
                "repaired_result_json": repaired_result.get("result_json"),
                "repaired_verification": repaired_result.get(
                    "verification_json"),
            },
        }
        store.add_item(run["id"], item)
        summary_rows.append((name, status,
                             f"{base['original_earned']}/{base['original_available']}"
                             if original else "—",
                             f"{repaired_result.get('earned')}/{repaired_result.get('available')}",
                             f"+{added} added, {changed} changed, "
                             f"{unresolved} unresolved"))
        print(f"  {name:<24} {status:<12} "
              f"{base['original_earned']}/{base['original_available']} -> "
              f"{repaired_result.get('earned')}/{repaired_result.get('available')} "
              f"(+{added} added, {changed} changed, {unresolved} unresolved)")

    # ---- 5. summary --------------------------------------------------------
    summary = {
        "counts": inventory["counts"],
        "rows": [{"student": s, "status": st, "original": o,
                  "repaired": r, "detail": d}
                 for s, st, o, r, d in summary_rows],
        "scheme": {"questions": len(blueprint.get("questions") or []),
                   "total_marks": sum(int(q.get("max_marks") or 0)
                                      for q in blueprint["questions"])},
    }
    store.finish_run(run["id"], summary=summary,
                     status="PARTIAL" if args.skip_marking else "COMPLETE")
    print("\n== repair session summary ==")
    for s, st, o, r, d in summary_rows:
        print(f"  {s:<24} {st:<12} {str(o):>9} -> {str(r):>9}  {d}")
    print("\nrun id:", run["id"])
    return 0


def _title(coursework_id: str) -> str | None:
    from app import db
    row = db.query_one("SELECT title FROM coursework WHERE id = ?",
                       (coursework_id,))
    return row["title"] if row else None


def _class_name(course_id: str) -> str | None:
    from app import db
    row = db.query_one("SELECT name FROM courses WHERE id = ?", (course_id,))
    return row["name"] if row else None


def _slug(name: str) -> str:
    return "".join(c if c.isalnum() or c in "-_" else "_" for c in name)[:60]


if __name__ == "__main__":
    raise SystemExit(main())
