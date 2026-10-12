"""Read-only diagnosis of the Classroom attachment 403 (@OwnerAccessRequired).

Answers the question the brief asks: is the Classroom-authenticated account the
OWNER of the Drive file it is trying to attach? Prints only safe identifiers -
never a token, refresh token, or client secret.

    python scripts/probe_drive_ownership.py [drive_file_id]

With no argument it uses the file id recorded for the most recent correction.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import requests  # noqa: E402

from app import db  # noqa: E402
from app.classroom import auth, client as gc  # noqa: E402

DRIVE_FILE_FIELDS = (
    "id,name,mimeType,owners,createdTime,trashed,shared,sharedWithMeTime,"
    "driveId,parents,appProperties,properties,capabilities"
)


def probe(drive_file_id: str) -> int:
    token = auth.get_access_token()          # never printed, never logged
    account = auth.account_email()
    print("=" * 68)
    print("Classroom attachment ownership diagnosis")
    print("=" * 68)
    print("Classroom authenticated account :", account or "(unknown)")
    print("Google OAuth connected at       :", auth.connected_at())
    print("Drive file id                   :", drive_file_id)

    url = f"https://www.googleapis.com/drive/v3/files/{drive_file_id}"
    resp = requests.get(url, headers=gc.classroom_headers(token),
                        params={"fields": DRIVE_FILE_FIELDS,
                                "supportsAllDrives": "true"}, timeout=30)
    if resp.status_code != 200:
        print("Drive lookup failed: HTTP", resp.status_code)
        print(json.dumps(resp.json(), indent=2)[:600])
        return 1

    f = resp.json()
    owners = [o.get("emailAddress") for o in (f.get("owners") or [])]
    caps = f.get("capabilities") or {}
    print("-" * 68)
    print("Drive file name        :", f.get("name"))
    print("Drive file owner(s)    :", owners or "(none reported)")
    print("Drive file created     :", f.get("createdTime"))
    print("Trashed                :", f.get("trashed"))
    print("Shared                 :", f.get("shared"))
    print("Shared drive id        :", f.get("driveId") or "(none - personal Drive)")
    print("Parent folder ids      :", f.get("parents"))
    print("appProperties          :", f.get("appProperties") or "(none)")
    print("custom properties      :", f.get("properties") or "(none)")
    print("canOwnerDrive          :", caps.get("canOwnerDrive"))
    print("canDelete              :", caps.get("canDelete"))
    print("-" * 68)

    owns = bool(account) and account.lower() in {o.lower() for o in owners if o}
    print("VERDICT:", "MATCH - the Classroom account owns this file"
          if owns else
          "MISMATCH - the Classroom account does NOT own this file, which is "
          "exactly what Google reports as @OwnerAccessRequired")
    if not owns:
        print()
        print("The file must be re-created by the currently connected Google")
        print("account. The old file is left alone; it is not deleted here.")
    return 0


def main() -> int:
    arg = sys.argv[1] if len(sys.argv) > 1 else None
    if not arg:
        row = db.query_one(
            "SELECT drive_file_id FROM report_attachments "
            "WHERE drive_file_id IS NOT NULL AND drive_file_id != '' "
            "ORDER BY updated_at DESC LIMIT 1")
        if not row:
            print("No report_attachments row with a Drive file id was found.")
            return 1
        arg = row["drive_file_id"]
    return probe(arg)


if __name__ == "__main__":
    raise SystemExit(main())
