"""Confirm the correction report is visible on the correct student submission.

Read-only verification against live Google Classroom + Drive. Prints only
opaque IDs and file titles; no tokens, key material, or student identifiers.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))

from app.classroom import client as gc
from app.classroom.auth import get_access_token

COURSE_ID = "886515702341"
CW_ID = "873785733817"
SUBMISSION_ID = "Cg4IqOb1jpESELnlwI23GQ"


def main() -> int:
    token = get_access_token()
    sub = gc.get_student_submission(token, COURSE_ID, CW_ID, SUBMISSION_ID)
    print(f"[verify] submission state={sub.get('state')} "
          f"associatedWithDeveloper={sub.get('associatedWithDeveloper')}")

    attachments = (sub.get("assignmentSubmission") or {}).get("attachments") or []
    print(f"[verify] {len(attachments)} attachment(s) on the submission")
    found_report = False
    for att in attachments:
        df = att.get("driveFile") or {}
        fid = df.get("id")
        title = df.get("title", "")
        print(f"  - driveFile {fid}  title={title!r}")
        if "Correction_Report" not in title:
            continue
        found_report = True
        meta = gc.classroom_request(
            "GET", f"{gc.DRIVE_BASE}/files/{fid}", token,
            params={"fields": "id,name,mimeType,permissions"})
        print(f"    [drive] name={meta.get('name')!r} "
              f"mime={meta.get('mimeType')!r}")
        for perm in meta.get("permissions", []) or []:
            print(f"    [perm] type={perm.get('type')} "
                  f"role={perm.get('role')} "
                  f"domain={perm.get('domain')} "
                  f"anyone_with_link={bool(perm.get('anyoneWithLink'))}")

    print(f"[verify] correction report present: {found_report}")
    return 0 if found_report else 1


if __name__ == "__main__":
    raise SystemExit(main())