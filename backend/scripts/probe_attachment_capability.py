"""EMPIRICAL capability probe: will Google let us attach to a submission?

`associatedWithDeveloper` was absent on all 78 Ch1L7 submissions. Absence is
NOT proof, so this actually calls :modifyAttachments and reports what Google
says. It is deliberately reversible:

* uploads a tiny, clearly-labelled probe PDF (never a student report),
* attempts the attachment on ONE submission,
* records the real HTTP outcome,
* if the attach SUCCEEDS it is immediately removed again and the Drive file
  trashed, so no student's submission is left altered.

Never prints student names, emails, or tokens - only Classroom IDs.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))

from app import db
from app.classroom import client as gc
from app.classroom.auth import get_access_token

COURSE_ID = "877926720424"
COURSEWORK_ID = "886161920765"
# The one Ch1L7 submission the app already downloaded a corrected copy for.
TARGET_SUBMISSION = "Cg4I-M73i60XEP2l95rlGQ"


def probe_pdf() -> bytes:
    """A tiny valid PDF that says clearly what it is."""
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas
    import io
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    c.setFont("Helvetica", 14)
    c.drawString(60, 760, "Math Corrector - attachment capability probe")
    c.setFont("Helvetica", 10)
    c.drawString(60, 735, "This is NOT a student report.")
    c.drawString(60, 720, "Created to verify the Classroom attachment API.")
    c.drawString(60, 700, f"UTC {time.strftime('%Y-%m-%d %H:%M:%S')}")
    c.save()
    return buf.getvalue()


def main() -> int:
    db.init_db()
    token = get_access_token()
    print(f"[probe] connected; target submission={TARGET_SUBMISSION}")

    print("\n[probe] step 1: upload probe PDF to Drive")
    uploaded = gc.upload_report_to_drive(
        token, "_capability_probe_DO_NOT_USE.pdf", probe_pdf())
    file_id = uploaded["id"]
    print(f"[probe] drive file id={file_id}")

    outcome = "unknown"
    try:
        print("\n[probe] step 2: pre-flight read of the submission")
        before = gc.get_student_submission(token, COURSE_ID, COURSEWORK_ID,
                                           TARGET_SUBMISSION)
        print(f"[probe] associatedWithDeveloper="
              f"{before.get('associatedWithDeveloper')}")
        print(f"[probe] existing attachments={len(gc.extract_attachments(before))}")

        print("\n[probe] step 3: REAL modifyAttachments call")
        try:
            gc.modify_submission_attachments(
                token, COURSE_ID, COURSEWORK_ID, TARGET_SUBMISSION,
                add_drive_file_ids=[file_id])
            outcome = "ALLOWED"
            print("[probe] Google ACCEPTED the attachment (HTTP 200)")
        except gc.ClassroomApiError as exc:
            outcome = f"REFUSED-{exc.status}"
            print(f"[probe] Google REFUSED it: HTTP {exc.status}")
            print(f"[probe] detail: {(exc.detail or '')[:400]}")

        print("\n[probe] step 4: read the submission back (verification)")
        after = gc.get_student_submission(token, COURSE_ID, COURSEWORK_ID,
                                          TARGET_SUBMISSION)
        ids = {a.get("id") for a in gc.extract_attachments(after)}
        present = file_id in ids
        print(f"[probe] probe file present on submission: {present}")
        print(f"[probe] attachments now: {sorted(ids)}")

        if outcome == "ALLOWED":
            print("\n[probe] step 5: REVERTING - removing the probe attachment")
            gc.modify_submission_attachments(
                token, COURSE_ID, COURSEWORK_ID, TARGET_SUBMISSION,
                remove_drive_file_ids=[file_id])
            final = gc.get_student_submission(token, COURSE_ID,
                                              COURSEWORK_ID, TARGET_SUBMISSION)
            print(f"[probe] reverted; attachments now: "
                  f"{sorted(a.get('id') for a in gc.extract_attachments(final))}")
    finally:
        print("\n[probe] cleanup: trashing the probe Drive file")
        try:
            import requests
            from app.classroom.client import DRIVE_BASE, classroom_headers
            r = requests.delete(f"{DRIVE_BASE}/files/{file_id}",
                                headers=classroom_headers(token), timeout=60)
            print(f"[probe] drive delete HTTP {r.status_code}")
        except Exception as exc:  # noqa: BLE001
            print(f"[probe] drive delete failed: {exc}")

    print(f"\n[probe] RESULT: modifyAttachments {outcome}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())