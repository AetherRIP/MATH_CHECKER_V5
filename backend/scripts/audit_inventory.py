"""Phase 1 — test inventory for Ch1L13 - Sets (O Level Mathematics).

Read-only against Google Classroom. Writes a privacy-safe inventory JSON to
``data/audit_inventory.json`` keyed by an internal student code; real names are
kept only in the local DB, never in this artefact.

Run from the backend directory:  python scripts/audit_inventory.py
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
ROOT = BACKEND.parent
sys.path.insert(0, str(BACKEND))

from app import config, db  # noqa: E402
from app.classroom import auth as cauth  # noqa: E402
from app.classroom import client as cclient  # noqa: E402

COURSEWORK_ID = "889601795010"
MARK_SCHEME = ROOT / "data" / "mark_schemes" / f"{COURSEWORK_ID}.pdf"
OUT = ROOT / "data" / "audit_inventory.json"

REPORT_HINT = re.compile(r"correction[_ ]report", re.I)


def anonymise(name: str) -> str:
    import hashlib

    return "S-" + hashlib.sha1((name or "?").encode("utf-8")).hexdigest()[:6]


def main() -> int:
    token = cauth.get_access_token()
    print("classroom token: OK")

    courses = cclient.list_teacher_courses(token)
    print("\nCOURSES (%d):" % len(courses))
    target_course = None
    for c in courses:
        print("  -", c.get("id"), "|", c.get("name"), "|", c.get("section"))
        if str(c.get("id")) == "877926720424":
            target_course = c
    if target_course is None:
        print("!! O Level course 877926720424 not in teacher course list")
        return 1

    assignments = cclient.list_assignments(token, "877926720424")
    print("\nASSIGNMENTS (%d):" % len(assignments))
    cw = None
    for a in assignments:
        if str(a.get("id")) == COURSEWORK_ID:
            cw = a
        print("  -", a.get("id"), "|", a.get("title"), "|", a.get("state"),
              "| maxPoints=", a.get("maxPoints"))
    if cw is None:
        print("!! coursework %s not found live" % COURSEWORK_ID)
        return 1

    print("\nTARGET:", cw.get("title"), "| id", cw.get("id"),
          "| maxPoints", cw.get("maxPoints"), "| due", cw.get("dueDate"))

    subs: list[dict] = []
    page_token = None
    while True:
        batch, page_token = cclient.list_student_submissions(
            token, "877926720424", COURSEWORK_ID, page_token)
        subs.extend(batch)
        if not page_token:
            break
    print("submissions returned live:", len(subs))

    rows = []
    for s in subs:
        uid = s.get("userId") or ""
        try:
            name = cclient.get_user_profile(token, uid).get("name", {}).get("fullName", "") if uid else ""
        except Exception:
            name = ""
        atts = cclient.extract_attachments(s)
        answer_files, report_files = [], []
        for a in atts:
            title = a.get("title") or ""
            entry = {
                "title": title,
                "file_id": a.get("id"),
                "type": a.get("kind"),
            }
            if REPORT_HINT.search(title):
                report_files.append(entry)
            else:
                answer_files.append(entry)
        rows.append({
            "student": anonymise(name),
            "student_key": anonymise(name),
            "submission_id": s.get("id"),
            "state": s.get("state"),
            "late": bool(s.get("late")),
            "assigned_grade": s.get("assignedGrade"),
            "draft_grade": s.get("draftGrade"),
            "answer_files": answer_files,
            "report_files": report_files,
        })

    states: dict[str, int] = {}
    for r in rows:
        states[r["state"]] = states.get(r["state"], 0) + 1

    accessible = [r for r in rows if r["answer_files"]]
    print("\nSTATE COUNTS:", states)
    print("rows with answer files:", len(accessible))

    # Cached local processing status
    for r in rows:
        try:
            job = db.query_one(
                "SELECT status, progress, stage_detail, error_code, error "
                "FROM correction_jobs WHERE submission_id = ?", (r["submission_id"],))
        except Exception:
            job = None
        try:
            res = db.query_one(
                "SELECT earned, available, percentage, provisional_grade, review_required, id "
                "FROM results WHERE submission_id = ?", (r["submission_id"],))
        except Exception:
            res = None
        r["cached_job"] = dict(job) if job else None
        r["cached_result"] = dict(res) if res else None

    artefact = {
        "course_id": "877926720424",
        "course_name": target_course.get("name"),
        "coursework_id": COURSEWORK_ID,
        "coursework_title": cw.get("title"),
        "coursework_max_points": cw.get("maxPoints"),
        "mark_scheme_local": str(MARK_SCHEME),
        "mark_scheme_present": MARK_SCHEME.exists(),
        "mark_scheme_bytes": MARK_SCHEME.stat().st_size if MARK_SCHEME.exists() else 0,
        "submission_count": len(rows),
        "state_counts": states,
        "with_answer_files": len(accessible),
        "submissions": rows,
    }
    OUT.write_text(json.dumps(artefact, indent=2), encoding="utf-8")
    print("\nwrote", OUT)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
