"""LIVE probe: can the app actually attach a report to a real submission?

Read-only up front. It reports:
  1. whether Classroom is connected (and which account),
  2. the OAuth scopes actually granted to the stored token,
  3. for a real assignment, whether Google says this client may modify the
     student submissions (``associatedWithDeveloper``),
and only then, with ``--attach``, performs the real
upload -> modifyAttachments -> verify round trip.

Never prints key material, tokens, or student emails.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))

COURSE_ID = "877926720424"          # WBIS - O Level Mathematics (Mr. Abanoub Akram)
COURSEWORK_ID = "886161920765"      # Ch1L7 - Limits of Accuracy


def banner(t: str) -> None:
    print(f"\n=== {t} ===")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--attach", action="store_true",
                    help="perform the real upload+attach (writes to Classroom)")
    ap.add_argument("--course-id", default=COURSE_ID)
    ap.add_argument("--coursework-id", default=COURSEWORK_ID)
    args = ap.parse_args()

    from app import db
    from app.classroom import client as gc
    from app.classroom.auth import auth_state, get_access_token
    db.init_db()

    banner("classroom connection")
    state = auth_state()
    print(f"connected={state.connected}  account={state.email}  "
          f"client_configured={state.client_configured}")
    if state.problem:
        print(f"problem: {state.problem}")
    if not state.connected:
        print("NOT CONNECTED - cannot test the live path.")
        return 2

    token = get_access_token()

    banner("granted scopes on the stored token")
    try:
        import requests
        info = requests.get(
            "https://www.googleapis.com/oauth2/v3/tokeninfo",
            params={"access_token": token}, timeout=30).json()
        granted = sorted(info.get("scope", "").split())
        for s in granted:
            print(f"  {s}")
        print(f"\nhas drive.file : "
              f"{'https://www.googleapis.com/auth/drive.file' in granted}")
        print(f"has drive.full : "
              f"{'https://www.googleapis.com/auth/drive' in granted}")
    except Exception as exc:  # noqa: BLE001
        print(f"could not read token info: {exc}")

    banner(f"can this app modify submissions of {args.coursework_id}?")
    subs, _page = gc.list_student_submissions(
        token, args.course_id, args.coursework_id)
    print(f"student submissions found: {len(subs)}")
    modifiable = 0
    for s in subs[:5]:
        sid = s.get("id")
        user = s.get("userId")
        assoc = s.get("associatedWithDeveloper")
        modifiable += 1 if assoc else 0
        # IDs only - never names/emails.
        print(f"  submission={sid} user={user} "
              f"associatedWithDeveloper={assoc} state={s.get('state')}")
    total = len(subs)
    print(f"\nmodifiable: {modifiable}/{total} "
          f"(Google only lets THIS OAuth client modify submissions for "
          f"coursework it created)")

    if not args.attach:
        print("\n(read-only probe - pass --attach to do the real round trip)")
        return 0

    if not modifiable:
        print("\nGoogle reports this client may NOT modify these "
              "submissions, so a real attach would be refused. Not attempting.")
        return 3

    banner("real attach round trip")
    from app.classroom import report_attachment as ra
    row = db.query_one(
        "SELECT * FROM results WHERE coursework_id = ? AND submission_id "
        "IS NOT NULL LIMIT 1", (args.coursework_id,))
    if not row:
        print("no corrected result for this assignment yet - run a "
              "correction first.")
        return 4
    from app.engine.service import CorrectionService
    result = CorrectionService().get_result(row["id"])
    outcome = ra.attach_report(token, result, course_id=args.course_id,
                               assignment_title="Ch1L7")
    print(f"status={outcome.status}")
    print(f"message={outcome.message}")
    print(f"drive_file_id={outcome.extra.get('drive_file_id')}")
    return 0 if outcome.status == ra.STATUS_VERIFIED else 1


if __name__ == "__main__":
    raise SystemExit(main())