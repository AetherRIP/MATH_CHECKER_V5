"""Read-only: what does the TESTING course contain?

Needed to decide whether a full end-to-end attachment test can be run
safely (app-created coursework + a submission that is not real student data).
Prints COUNTS and opaque IDs only - no names, no emails.
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

COURSE_ID = "886515702341"     # TESTING


def main() -> int:
    db.init_db()
    token = get_access_token()

    courses = {c["id"]: c for c in gc.list_teacher_courses(token)}
    print(f"teacher courses: {len(courses)}")
    if COURSE_ID not in courses:
        print(f"TESTING course {COURSE_ID} not found / not accessible")
        return 1

    students = gc.list_course_students(token, COURSE_ID)
    print(f"\nTESTING roster size: {len(students)}")
    for s in students[:10]:
        prof = s.get("profile") or {}
        # Opaque id + a shape hint only: never the name or email.
        print(f"  userId={s.get('userId')} "
              f"hasEmail={bool(prof.get('emailAddress'))} "
              f"name_len={len(prof.get('name') or {})}")

    cws = gc.list_assignments(token, COURSE_ID)
    print(f"\nTESTING assignments: {len(cws)}")
    for cw in cws:
        cid = cw.get("id")
        print(f"  id={cid} managed_by_app={mg.is_managed(cid)} "
              f"state={cw.get('state')} workType={cw.get('workType')}")

    mmap = mg.managed_map()
    print(f"\ncoursework this app created (across all courses): {len(mmap)}")
    for cid, info in list(mmap.items())[:10]:
        print(f"  id={cid} course={info.get('course_id')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())