"""FULL END-TO-END test on a real Classroom submission.

Runs the app's OWN path (not a mock): sync -> download the student's work ->
AI mark -> validate -> render report -> upload to Drive -> attach to THAT
StudentSubmission -> read it back and verify.

Then re-runs the attach to prove it does NOT create a duplicate report.

Prints Classroom IDs and filenames only - never student names, emails,
tokens, or report contents.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))

COURSE_ID = "886515702341"
COURSEWORK_ID = "873785733817"
SUBMISSION_ID = "Cg4IqOb1jpESELnlwI23GQ"


def banner(t: str) -> None:
    print(f"\n=== {t} ===", flush=True)


def main() -> int:
    from app import db
    from app.classroom import client as gc
    from app.classroom import sync as classroom_sync
    from app.classroom.auth import get_access_token
    db.init_db()

    token = get_access_token()

    banner("1. sync Classroom so the submission is stored locally")
    try:
        classroom_sync.sync_course(COURSE_ID)
    except Exception as exc:  # noqa: BLE001
        print(f"[e2e] sync warning: {exc}")

    banner("2. confirm the submission we will attach to")
    sub = gc.get_student_submission(token, COURSE_ID, COURSEWORK_ID,
                                    SUBMISSION_ID)
    user_id = sub.get("userId")
    atts = gc.extract_attachments(sub)
    print(f"[e2e] submission={SUBMISSION_ID} user={user_id}")
    print(f"[e2e] state={sub.get('state')} "
          f"associatedWithDeveloper={sub.get('associatedWithDeveloper')}")
    print(f"[e2e] existing attachments={len(atts)}")
    for a in atts:
        print(f"    - {a['kind']}: {a['title']}")

    banner("3. ensure a corrected result exists for this submission")
    result_row = db.query_one(
        "SELECT * FROM results WHERE coursework_id = ? AND submission_id = ?",
        (COURSEWORK_ID, SUBMISSION_ID))
    if not result_row:
        print("[e2e] NO RESULT - the marking job has not completed yet.")
        print("[e2e] Start the app and press 'Correct' on this assignment, "
              "then re-run this script.")
        return 3
    result_id = result_row["id"]
    print(f"[e2e] result_id={result_id} earned={result_row['earned']}"
          f"/{result_row['available']} model={result_row['marking_model']}")

    from app.classroom import report_attachment as ra
    from app.engine.service import CorrectionService
    result = CorrectionService().get_result(result_id)
    title = db.query_one("SELECT title FROM coursework WHERE id = ?",
                         (COURSEWORK_ID,))["title"]

    banner("4. attach the correction report to THIS submission")
    out = ra.attach_report(token, result, course_id=COURSE_ID,
                           assignment_title=title)
    print(f"[e2e] status={out.status}")
    print(f"[e2e] message={out.message}")
    drive_id = out.extra.get("drive_file_id")
    print(f"[e2e] drive_file_id={drive_id}")

    banner("5. VERIFY by reading the StudentSubmission back from Google")
    after = gc.get_student_submission(token, COURSE_ID, COURSEWORK_ID,
                                      SUBMISSION_ID)
    final = gc.extract_attachments(after)
    print(f"[e2e] attachments now: {len(final)}")
    found = False
    for a in final:
        marker = " <== CORRECTION REPORT" if a["id"] == drive_id else ""
        print(f"    - {a['kind']}: {a['title']} (id={str(a['id'])[:44]}){marker}")
        if a["id"] == drive_id:
            found = True
    print(f"\n[e2e] report present on the student's submission: {found}")

    banner("6. re-run attach: must NOT create a duplicate report")
    again = ra.attach_report(token, result, course_id=COURSE_ID,
                             assignment_title=title)
    print(f"[e2e] second run status={again.status}")
    after2 = gc.get_student_submission(token, COURSE_ID, COURSEWORK_ID,
                                       SUBMISSION_ID)
    final2 = gc.extract_attachments(after2)
    print(f"[e2e] attachments after re-run: {len(final2)} "
          f"(was {len(final)})")
    dupes = len([a for a in final2
                 if a["id"] == drive_id]) > 1
    print(f"[e2e] duplicate report created: {dupes}")

    banner("RESULT")
    ok = found and again.status == ra.STATUS_VERIFIED and len(final2) == len(final)
    print(f"[e2e] {'PASS' if ok else 'FAIL'}: the correction report is "
          f"attached to the correct student's Classroom submission.")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())