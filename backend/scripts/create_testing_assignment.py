"""Create the C1L7 test assignment in the TESTING class.

The teacher supplies the student work themselves, so this only:
  1. removes any leftover ZZ_TEMP probe assignment,
  2. creates a PUBLISHED assignment "Chapter 1 Lesson 7" in TESTING,
  3. registers the existing Ch1L7 mark scheme for it (the app reads mark
     schemes from data/mark_schemes/<coursework_id>.pdf).

The assignment is created BY THIS APP, which is what makes its student
submissions modifiable by this OAuth client - the thing the teacher's own
Ch1L7 refused with 403 @ProjectPermissionDenied.
"""
from __future__ import annotations

import shutil
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))

from app import db
from app.classroom import client as gc
from app.classroom import managed as mg
from app.classroom.auth import get_access_token

COURSE_ID = "886515702341"          # TESTING
TITLE = "Chapter 1 Lesson 7"
MAX_POINTS = 9.0                    # the 1-9 grade scale used by this course
SOURCE_SCHEME = ROOT / "data" / "mark_schemes" / "886161920765.pdf"


def cleanup_temp(token: str) -> None:
    for cw in gc.list_assignments(token, COURSE_ID):
        if str(cw.get("title", "")).startswith("ZZ_TEMP"):
            cw_id = cw.get("id")
            print(f"[setup] removing leftover temp assignment {cw_id}")
            try:
                gc.classroom_request(
                    "DELETE",
                    f"{gc.CLASSROOM_BASE}/courses/{COURSE_ID}"
                    f"/courseWork/{cw_id}", token)
                mg.mark_removed(cw_id)
                print("[setup]   deleted")
            except Exception as exc:  # noqa: BLE001
                print(f"[setup]   delete failed: {exc}")


def main() -> int:
    db.init_db()
    token = get_access_token()
    cleanup_temp(token)

    existing = [c for c in gc.list_assignments(token, COURSE_ID)
                if c.get("title") == TITLE]
    if existing:
        cw_id = existing[0]["id"]
        print(f"[setup] '{TITLE}' already exists: {cw_id}")
    else:
        created = mg.create_coursework(
            COURSE_ID, title=TITLE,
            description="Limits of Accuracy - test run of the correction report "
                        "attachment.",
            max_points=MAX_POINTS, state="PUBLISHED")
        cw_id = created.get("id")
        print(f"[setup] created '{TITLE}' id={cw_id} "
              f"state={created.get('state')} managed={mg.is_managed(cw_id)}")

    # Register the mark scheme for this coursework (same path the upload
    # endpoint writes: data/mark_schemes/<coursework_id>.pdf).
    target = ROOT / "data" / "mark_schemes" / f"{cw_id}.pdf"
    if SOURCE_SCHEME.exists():
        shutil.copy2(SOURCE_SCHEME, target)
        db.execute(
            "INSERT INTO pending_mark_schemes(coursework_id, path, "
            "uploaded_at) VALUES(?,?,?) ON CONFLICT(coursework_id) DO UPDATE "
            "SET path=excluded.path, uploaded_at=excluded.uploaded_at",
            (cw_id, str(target), time.time()))
        print(f"[setup] mark scheme registered -> {target.name}")
    else:
        print(f"[setup] WARNING source mark scheme missing: {SOURCE_SCHEME}")

    row = db.query_one("SELECT * FROM coursework WHERE id = ?", (cw_id,))
    print(f"\n[setup] local coursework row: "
          f"{'present' if row else 'MISSING (run a Classroom sync)'}")
    print(f"[setup] coursework_id = {cw_id}")
    print(f"[setup] classroom link = "
          f"https://classroom.google.com/c/ODg2NTE1NzAyMzQx/a/"
          f"{_b64(cw_id)}/details")
    return 0


def _b64(text: str) -> str:
    """Classroom URLs use URL-safe base64 of the numeric id (as Google does)."""
    import base64
    return base64.urlsafe_b64encode(text.encode()).decode().rstrip("=")


if __name__ == "__main__":
    raise SystemExit(main())