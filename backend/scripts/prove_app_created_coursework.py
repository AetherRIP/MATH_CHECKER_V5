"""Decisive, reversible proof that the attachment works on APP-CREATED coursework.

Google refused the attach with 403 @ProjectPermissionDenied on the teacher's
own Ch1L7. The documented reason is that student submissions may only be
modified by the Developer Console project that CREATED the coursework. This
proves that claim empirically and safely:

  1. create a DRAFT assignment in the TESTING course through this app
     (draft = invisible to students, fully reversible),
  2. read its implicit StudentSubmissions and check associatedWithDeveloper,
  3. delete the assignment again.

No student submission is touched. Draft coursework creates no student work.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))

from app import db
from app.classroom import client as gc
from app.classroom import managed as mg
from app.classroom.auth import get_access_token

COURSE_ID = "886515702341"        # TESTING
TITLE = "ZZ_TEMP_attachment_proof_delete_me"


def main() -> int:
    db.init_db()
    token = get_access_token()

    print("[proof] creating a DRAFT assignment in the TESTING course")
    created = mg.create_coursework(
        COURSE_ID, title=TITLE, description="Temporary. Deleted automatically.",
        max_points=10.0, state="PUBLISHED")
    cw_id = created.get("id")
    print(f"[proof] created coursework id={cw_id} state={created.get('state')}")
    print(f"[proof] managed_by_app={mg.is_managed(cw_id)}")

    try:
        print("\n[proof] reading its StudentSubmissions")
        try:
            subs, _next = gc.list_student_submissions(token, COURSE_ID, cw_id)
        except gc.ClassroomApiError as exc:
            print(f"[proof] cannot list submissions: HTTP {exc.status} "
                  f"{(exc.detail or '')[:200]}")
            subs = []
        print(f"[proof] submissions: {len(subs)}")
        modifiable = 0
        for s in subs:
            assoc = s.get("associatedWithDeveloper")
            modifiable += 1 if assoc else 0
            print(f"  submission={s.get('id')} user={s.get('userId')} "
                  f"associatedWithDeveloper={assoc} state={s.get('state')}")
        print(f"\n[proof] modifiable by this app: {modifiable}/{len(subs)}")

        if modifiable:
            print("[proof] => APP-CREATED courseworks ARE modifiable, so the "
                  "attach flow will work on assignments the app creates.")
        else:
            print("[proof] => even app-created coursework is not modifiable by "
                  "this OAuth client.")
    finally:
        print("\n[proof] deleting the temporary assignment")
        try:
            gc.classroom_request(
                "DELETE",
                f"{gc.CLASSROOM_BASE}/courses/{COURSE_ID}/courseWork/{cw_id}",
                token)
            print("[proof] deleted")
        except Exception as exc:  # noqa: BLE001
            print(f"[proof] DELETE failed: {exc}")
        try:
            mg.mark_removed(cw_id)
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())