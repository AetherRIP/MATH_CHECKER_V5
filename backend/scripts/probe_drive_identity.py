"""Which Google identity actually created the report file?

Compares the connected account's Drive root id with the parent folder recorded
on the report file. In Google Drive a file created inside another user's folder
is owned by that folder's owner, so this distinguishes "uploaded as the teacher"
from "uploaded as / placed under the student".

Read-only. Never prints a token.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import requests  # noqa: E402

from app import db  # noqa: E402
from app.classroom import auth, client as gc  # noqa: E402

FOLDER_FIELDS = "id,name,owners,driveId,mimeType"
PERM_FIELDS = "id,type,role,emailAddress,displayName,deleted,domain"


def _get(token, url, fields, **params):
    resp = requests.get(url, headers=gc.classroom_headers(token),
                        params={"fields": fields, **params}, timeout=30)
    return resp


def main() -> int:
    token = auth.get_access_token()
    account = auth.account_email()
    row = db.query_one(
        "SELECT drive_file_id FROM report_attachments "
        "WHERE drive_file_id IS NOT NULL AND drive_file_id != '' "
        "ORDER BY updated_at DESC LIMIT 1")
    if not row:
        print("no drive file id recorded")
        return 1
    fid = row["drive_file_id"]

    print("=" * 68)
    print("Which identity created the report file?")
    print("=" * 68)
    print("Connected Classroom/Drive account:", account)

    root = _get(token, f"https://www.googleapis.com/drive/v3/files/root",
                FOLDER_FIELDS, supportsAllDrives="true")
    if root.status_code == 200:
        r = root.json()
        print("Account 'My Drive' root id   :", r.get("id"))
        print("  owners                     :",
              [o.get("emailAddress") for o in (r.get("owners") or [])])
    else:
        print("root lookup failed:", root.status_code, root.text[:200])

    meta = _get(token, f"https://www.googleapis.com/drive/v3/files/{fid}",
                "id,name,owners,parents,createdTime,trashed", supportsAllDrives="true")
    if meta.status_code == 200:
        m = meta.json()
        parents = m.get("parents") or []
        print("Report file id                :", m.get("id"))
        print("Report file parents           :", parents)
        print("Report file owner(s)          :",
              [o.get("emailAddress") for o in (m.get("owners") or [])])
        for p in parents:
            pf = _get(token, f"https://www.googleapis.com/drive/v3/files/{p}",
                      FOLDER_FIELDS, supportsAllDrives="true")
            if pf.status_code == 200:
                f = pf.json()
                print(f"  parent {p}: name={f.get('name')!r} owners="
                      f"{[o.get('emailAddress') for o in (f.get('owners') or [])]}")
                if root.status_code == 200 and p == root.json().get("id"):
                    print("    -> this IS the connected account's My Drive root")
            else:
                print(f"  parent {p}: not visible to the connected account "
                      f"(HTTP {pf.status_code})")

    perms = _get(token, f"https://www.googleapis.com/drive/v3/files/{fid}/permissions",
                 PERM_FIELDS, supportsAllDrives="true",
                 fields_override=None) if False else requests.get(
        f"https://www.googleapis.com/drive/v3/files/{fid}/permissions",
        headers=gc.classroom_headers(token),
        params={"fields": PERM_FIELDS, "supportsAllDrives": "true"}, timeout=30)
    if perms.status_code == 200:
        print("Permissions on the report file:")
        for p in perms.json().get("permissions", []):
            print("  -", {k: p.get(k) for k in
                          ("type", "role", "emailAddress", "displayName", "deleted")})
    else:
        print("permissions lookup failed:", perms.status_code, perms.text[:200])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
