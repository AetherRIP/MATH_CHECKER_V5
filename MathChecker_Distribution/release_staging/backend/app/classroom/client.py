"""Classroom + Drive REST calls — preserved from the legacy app, plus grade
write-back attempt with graceful 403 fallback.

Everything here takes an access token string exactly like the friend's
functions did (classroom_request, list_teacher_courses, list_assignments,
list_turned_in_submissions, get_user_profile, download_drive_file) so the
V5 sync layer composes with proven behavior.
"""

from __future__ import annotations

import json
import logging

import requests

from ..errors import classroom_error_message
from ..logging_setup import log_event
from ..net_usage import SMALL_REQUEST_BYTES, record

log = logging.getLogger("classroom.client")

CLASSROOM_BASE = "https://classroom.googleapis.com/v1"
DRIVE_BASE = "https://www.googleapis.com/drive/v3"
DRIVE_UPLOAD_BASE = "https://www.googleapis.com/upload/drive/v3"


def _guarded_write(action: str):
    """Decorator: refuse an external WRITE while TESTING ONLY mode is on.

    Applied to every Drive/Classroom helper that creates, replaces, shares or
    deletes something. Those helpers call ``requests`` directly rather than
    going through :func:`classroom_request`, so they need their own gate — the
    point of the guard is that no execution path can bypass it.
    """
    import functools

    def decorate(func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            from ..testing_guard import assert_publication_allowed

            assert_publication_allowed(action)
            return func(*args, **kwargs)

        return wrapper

    return decorate


def classroom_headers(token: str) -> dict:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
    }


def classroom_request(method: str, url: str, token: str, **kwargs):
    """Call a Google API endpoint with a clear error message (legacy parity).

    Every mutating Classroom call passes through here, so this is where
    TESTING ONLY mode stops the line: while testing is on, a non-GET request
    raises before anything is sent. Read-only access (listings, downloads) is
    untouched — testing still has to see the real submissions.
    """
    from ..testing_guard import assert_publication_allowed

    assert_publication_allowed(f"{method} {url.split('?')[0]}", method=method)
    headers = kwargs.pop("headers", {})
    merged = classroom_headers(token)
    merged.update(headers)

    body = kwargs.get("json")
    response = requests.request(
        method=method, url=url, headers=merged, timeout=60, **kwargs
    )
    record("classroom_api",
           SMALL_REQUEST_BYTES + len(str(body)) if body else SMALL_REQUEST_BYTES,
           len(response.content or b""))

    if response.status_code >= 400:
        raise ClassroomApiError(response.status_code, response.text[:2000])

    if not response.content:
        return {}

    return response.json()


class ClassroomApiError(RuntimeError):
    def __init__(self, status: int, detail: str):
        super().__init__(classroom_error_message(status, detail))
        self.status = status
        self.detail = detail


# -- reads (legacy parity) --------------------------------------------------

def list_teacher_courses(token: str):
    data = classroom_request(
        "GET", f"{CLASSROOM_BASE}/courses", token,
        params={"teacherId": "me", "courseStates": "ACTIVE", "pageSize": 100},
    )
    return data.get("courses", [])


def list_assignments(token: str, course_id: str):
    data = classroom_request(
        "GET", f"{CLASSROOM_BASE}/courses/{course_id}/courseWork", token,
        params={"courseWorkStates": "PUBLISHED", "pageSize": 100},
    )
    assignments = data.get("courseWork", [])
    return [item for item in assignments if item.get("workType") == "ASSIGNMENT"]


def list_turned_in_submissions(token: str, course_id: str, coursework_id: str):
    data = classroom_request(
        "GET",
        f"{CLASSROOM_BASE}/courses/{course_id}/courseWork/{coursework_id}/studentSubmissions",
        token,
        params={"states": "TURNED_IN", "pageSize": 100},
    )
    return data.get("studentSubmissions", [])


def get_user_profile(token: str, user_id: str):
    return classroom_request("GET", f"{CLASSROOM_BASE}/userProfiles/{user_id}", token)


# Official StudentSubmission.state values (§3/§26 of the tracking spec).
SUBMISSION_STATES = (
    "NEW", "CREATED", "TURNED_IN", "RETURNED",
    "RECLAIMED_BY_STUDENT", "STUDENT_EDITED_AFTER_TURN_IN",
)


def list_student_submissions(token: str, course_id: str, coursework_id: str,
                             page_token: str | None = None):
    """One page of StudentSubmissions for a coursework, in EVERY state —
    not just TURNED_IN. A submission resource existing says nothing about
    whether the student actually turned work in; the state field is the
    source of truth and pagination must be followed to the end."""
    params: dict = {"pageSize": 100}
    if page_token:
        params["pageToken"] = page_token
    data = classroom_request(
        "GET",
        f"{CLASSROOM_BASE}/courses/{course_id}/courseWork/{coursework_id}/studentSubmissions",
        token, params=params,
    )
    return data.get("studentSubmissions", []), data.get("nextPageToken")


def list_course_students(token: str, course_id: str) -> list[dict]:
    """Full roster of a course, following pagination (tracking §5).

    Needed so students WITHOUT a submission resource can still appear in the
    tracking view as NOT_STARTED / MISSING instead of silently vanishing."""
    students: list[dict] = []
    page_token: str | None = None
    while True:
        params: dict = {"pageSize": 100}
        if page_token:
            params["pageToken"] = page_token
        data = classroom_request(
            "GET", f"{CLASSROOM_BASE}/courses/{course_id}/students", token,
            params=params,
        )
        students.extend(data.get("students", []))
        page_token = data.get("nextPageToken")
        if not page_token:
            break
    return students


def extract_attachments(submission: dict) -> list[dict]:
    """Normalise the attachment list of a StudentSubmission (driveFile ids +
    titles, or form/link payloads). Empty list = genuinely no attachments."""
    out: list[dict] = []
    for att in (submission.get("assignmentSubmission") or {}).get("attachments", []) or []:
        if att.get("driveFile"):
            df = att["driveFile"]
            out.append({"kind": "drive", "id": df.get("id"),
                        "title": df.get("title") or df.get("id")})
        elif att.get("form"):
            out.append({"kind": "form", "id": att["form"].get("formId"),
                        "title": att["form"].get("title")})
        elif att.get("link"):
            out.append({"kind": "link", "id": att["link"].get("url"),
                        "title": att["link"].get("url")})
        elif att.get("youTubeVideo"):
            out.append({"kind": "youtube", "id": att["youTubeVideo"].get("id"),
                        "title": att["youTubeVideo"].get("title")})
    return out


def extract_state_history(submission: dict) -> list[dict]:
    """Flatten submissionHistory entries into [{state, updateTime, actor}].
    Google documents submissionHistory as stateHistory + gradeHistory."""
    events: list[dict] = []
    for entry in submission.get("submissionHistory", []) or []:
        ts = entry.get("updateTime")
        sh = entry.get("stateHistory")
        if sh and sh.get("state"):
            events.append({"state": sh["state"], "updateTime": ts,
                           "actorUserId": sh.get("actorUserId")})
        gh = entry.get("gradeHistory")
        if gh:
            events.append({"grade": gh.get("grade") or gh.get("pointsEarned"),
                           "updateTime": ts, "actorUserId": gh.get("actorUserId")})
    return events


def download_drive_file(token: str, file_id: str):
    """Download a Drive attachment; Google Docs are exported to PDF (legacy parity)."""
    meta = classroom_request(
        "GET", f"{DRIVE_BASE}/files/{file_id}", token,
        params={"fields": "id,name,mimeType,capabilities"},
    )
    name = meta.get("name", f"drive_file_{file_id}")
    mime = meta.get("mimeType", "application/octet-stream")
    capabilities = meta.get("capabilities", {})
    if capabilities and capabilities.get("canDownload") is False:
        raise RuntimeError(f"Google Drive does not allow downloading '{name}'.")

    if mime == "application/vnd.google-apps.document":
        response = requests.get(
            f"{DRIVE_BASE}/files/{file_id}/export",
            headers=classroom_headers(token),
            params={"mimeType": "application/pdf"},
            timeout=60,
        )
        record("classroom_download", 0, len(response.content or b""))
        if response.status_code >= 400:
            raise RuntimeError(
                f"Could not export Google Doc '{name}' to PDF: "
                f"{response.status_code} {response.text[:1000]}"
            )
        return name.rsplit(".", 1)[0] + ".pdf", response.content, "application/pdf"

    response = requests.get(
        f"{DRIVE_BASE}/files/{file_id}",
        headers=classroom_headers(token),
        params={"alt": "media"},
        timeout=120,
    )
    record("classroom_download", 0, len(response.content or b""))
    if response.status_code >= 400:
        raise RuntimeError(
            f"Could not download '{name}': {response.status_code} {response.text[:1000]}"
        )
    return name, response.content, mime


def classroom_attachment_ids(submission: dict) -> list[str]:
    attachments = submission.get("assignmentSubmission", {}).get("attachments", [])
    ids = []
    for attachment in attachments:
        drive_file = attachment.get("driveFile")
        if drive_file and drive_file.get("id"):
            ids.append(drive_file["id"])
    return ids


# -- write-back (new in V5, graceful 403 fallback per plan §5) ----------------

# -- correction-report attachment (P0) ---------------------------------
#
# Google Classroom only lets you attach a file to a SPECIFIC student's
# submission through
#   POST /v1/courses/{courseId}/courseWork/{id}/studentSubmissions/{sid}
#        :modifyAttachments
# with {"addAttachments": [{"driveFile": {"id": "<driveFileId>"}}]}.
# Adding a Material to the CourseWork object would attach the file to the
# ASSIGNMENT, not to one student's submission, so it is NOT used here.
#
# Two constraints from Google's documentation that shape this code:
#   * the request must come from the Developer Console project that CREATED
#     the coursework (same restriction as draft grades), and
#   * a student submission may hold at most 20 attachments.


@_guarded_write("upload report to Drive")
def upload_report_to_drive(token: str, filename: str, content: bytes,
                           *, folder_id: str | None = None,
                           app_properties: dict | None = None) -> dict:
    """Upload a generated correction report to the teacher's Drive.

    Uses the ``drive.file`` scope, so the token only ever grants access to
    files this app creates - we cannot touch any of the teacher's other
    Drive content. The file is created PRIVATE (no sharing flags are set);
    access for the student is granted afterwards by Classroom when the
    submission is returned, or explicitly via :func:`share_drive_file`.

    ``app_properties`` are stamped onto the file so later cleanup can PROVE
    the file is one this app created and is temporary (§16-§17). Cleanup never
    relies on the filename.

    Returns the Drive file resource (id + name).
    """
    metadata: dict = {"name": filename, "mimeType": "application/pdf"}
    if app_properties:
        metadata["appProperties"] = {
            str(k): str(v)[:200] for k, v in app_properties.items()}
    if folder_id:
        metadata["parents"] = [folder_id]
    # multipart/related: JSON metadata part + binary content part.
    boundary = "mathcorrector-report-boundary"
    body = (
        f"--{boundary}\r\n"
        "Content-Type: application/json; charset=UTF-8\r\n\r\n"
        f"{json.dumps(metadata)}\r\n"
        f"--{boundary}\r\n"
        "Content-Type: application/pdf\r\n\r\n"
    ).encode("utf-8") + content + f"\r\n--{boundary}--\r\n".encode("utf-8")

    headers = classroom_headers(token)
    headers["Content-Type"] = f"multipart/related; boundary={boundary}"
    response = requests.post(
        f"{DRIVE_UPLOAD_BASE}/files",
        headers=headers,
        params={"uploadType": "multipart",
                "fields": "id,name,mimeType,owners,createdTime,appProperties"},
        data=body,
        timeout=180,
    )
    record("classroom_upload", len(body), len(response.content or b""))
    if response.status_code >= 400:
        raise ClassroomApiError(response.status_code, response.text[:2000])
    return response.json()


@_guarded_write("replace a Drive file")
def replace_drive_file_content(token: str, file_id: str, content: bytes) -> None:
    """Overwrite the bytes of an existing Drive file.

    Used when a submission is re-corrected: updating the SAME file keeps one
    attachment per submission instead of a pile of "Report (1).pdf" clones.

    NOTE: Drive v3 ``files.update`` is a **PATCH**. A POST here is the
    ``files.create`` verb and answers 404 "Not Found" - confirmed live, not
    assumed.
    """
    headers = classroom_headers(token)
    headers["Content-Type"] = "application/pdf"
    response = requests.patch(
        f"{DRIVE_UPLOAD_BASE}/files/{file_id}",
        headers=headers,
        params={"uploadType": "media", "fields": "id,name,size"},
        data=content,
        timeout=180,
    )
    record("classroom_upload", len(content), len(response.content or b""))
    if response.status_code >= 400:
        raise ClassroomApiError(response.status_code, response.text[:2000])


@_guarded_write("share a Drive file")
def share_drive_file(token: str, file_id: str, email: str,
                     role: str = "reader") -> dict:
    """Grant ONE named user read access to a report file.

    Deliberately narrow: a single reader permission for the student's own
    account. Never used to make a report public or link-shared.

    ``role="owner"`` is refused outright. Handing a report to the student
    would strip this app of the ownership that
    ``courseWork.studentSubmissions.modifyAttachments`` requires - it is the
    exact cause of a ``@OwnerAccessRequired`` 403, so it can never be the
    right thing to do here.
    """
    if role == "owner":
        raise ClassroomApiError(
            400, "Refusing to transfer report ownership to a student: "
                 "Classroom attachment requires this app to own the file.")
    return classroom_request(
        "POST", f"{DRIVE_BASE}/files/{file_id}/permissions", token,
        json={"type": "user", "role": role, "emailAddress": email},
    )


# ------------------------------------------------- ownership / temporary file

#: Markers stamped on every Drive file this app creates as a report. Cleanup
#: refuses to delete anything that does not carry all of them (§17).
APP_PROP_TEMP = "mathcorrectTemp"
APP_PROP_RESULT = "mathcorrectResultId"
APP_PROP_TOKEN = "mathcorrectToken"
APP_PROP_CREATED = "mathcorrectCreatedAt"

_FILE_FIELDS = ("id,name,mimeType,owners,createdTime,trashed,driveId,parents,"
                "appProperties,capabilities")


def get_drive_file(token: str, file_id: str) -> dict | None:
    """Metadata for one Drive file, or ``None`` if it is gone/unreadable."""
    response = requests.get(
        f"{DRIVE_BASE}/files/{file_id}", headers=classroom_headers(token),
        params={"fields": _FILE_FIELDS, "supportsAllDrives": "true"},
        timeout=30)
    if response.status_code == 404:
        return None
    if response.status_code != 200:
        raise ClassroomApiError(response.status_code, response.text[:500])
    return response.json()


def drive_file_owners(meta: dict | None) -> list[str]:
    return [str(o.get("emailAddress", "")).lower()
            for o in ((meta or {}).get("owners") or []) if o.get("emailAddress")]


def is_our_temp_report(meta: dict | None, *, result_id: str | None = None,
                       token_value: str | None = None) -> bool:
    """Can we PROVE this Drive file is a temporary report this app created?

    Every condition must hold: it exists, is not trashed, carries our
    temporary marker, and either matches this result's id and token. A filename
    match is never sufficient - a teacher has files called "Report.pdf" too.
    """
    if not meta or meta.get("trashed"):
        return False
    props = meta.get("appProperties") or {}
    if str(props.get(APP_PROP_TEMP)).lower() != "true":
        return False
    if result_id and str(props.get(APP_PROP_RESULT) or "") != str(result_id):
        return False
    if token_value and str(props.get(APP_PROP_TOKEN) or "") != str(token_value):
        return False
    return True


def verify_drive_ownership(token: str, file_id: str, *,
                            account_email: str | None) -> dict:
    """Does the authenticated user OWN this file? (§3)

    ``modifyAttachments`` requires the caller to be an owner. A mere writer or
    reader gets ``@OwnerAccessRequired``. Checking this BEFORE attaching turns
    a blind, permanently-failing 403 into one clear diagnosis.

    Returns ``{ok, reason, file_id, owners, account, can_delete, exists,
    is_our_temp}``. Only safe identifiers are returned - never a token.
    """
    account = (account_email or "").strip().lower() or None
    try:
        meta = get_drive_file(token, file_id)
    except ClassroomApiError as exc:
        return {"ok": False, "reason": "drive_lookup_failed",
                "detail": exc.detail, "file_id": file_id,
                "account": account, "owners": [], "exists": None,
                "can_delete": False, "is_our_temp": False}
    if meta is None:
        return {"ok": False, "reason": "file_not_found", "file_id": file_id,
                "account": account, "owners": [], "exists": False,
                "can_delete": False, "is_our_temp": False}

    owners = drive_file_owners(meta)
    caps = meta.get("capabilities") or {}
    owns = bool(account) and account in owners
    return {
        "ok": owns,
        "reason": "owner" if owns else (
            "not_owner" if account else "unknown_account"),
        "file_id": meta.get("id"),
        "owners": owners,
        "account": account,
        "created_time": meta.get("createdTime"),
        "parents": meta.get("parents") or [],
        "can_delete": bool(caps.get("canDelete")),
        "exists": True,
        "is_our_temp": is_our_temp_report(meta),
    }


@_guarded_write("delete a Drive file")
def delete_drive_file(token: str, file_id: str) -> None:
    """Delete one Drive file. Only ever called on a proven temporary file."""
    response = requests.delete(
        f"{DRIVE_BASE}/files/{file_id}", headers=classroom_headers(token),
        params={"supportsAllDrives": "true"}, timeout=60)
    if response.status_code in (200, 204, 404):
        return          # 404: already gone, which is the desired end state
    raise ClassroomApiError(response.status_code, response.text[:500])


def find_temp_reports(token: str, *, app_id: str = "mathchecker") -> list[dict]:
    """Every Drive file this app created as a temporary report (§16).

    Selected by our own ``appProperties`` marker only, so a teacher's own
    files, student files and mark schemes can never be returned.
    """
    query_str = (
        f"appProperties has {{key='{APP_PROP_TEMP}' and value='true'}} "
        f"and trashed = false"
    )
    response = requests.get(
        f"{DRIVE_BASE}/files", headers=classroom_headers(token),
        params={"q": query_str, "fields": f"files({_FILE_FIELDS})",
                "spaces": "drive", "pageSize": 200,
                "supportsAllDrives": "true"},
        timeout=60)
    if response.status_code != 200:
        raise ClassroomApiError(response.status_code, response.text[:500])
    return list(response.json().get("files") or [])


def get_student_submission(token: str, course_id: str, coursework_id: str,
                           submission_id: str) -> dict:
    """Read ONE StudentSubmission back (used to VERIFY an attachment)."""
    return classroom_request(
        "GET",
        f"{CLASSROOM_BASE}/courses/{course_id}/courseWork/{coursework_id}"
        f"/studentSubmissions/{submission_id}",
        token,
    )


@_guarded_write("modify submission attachments")
def modify_submission_attachments(token: str, course_id: str,
                                  coursework_id: str, submission_id: str,
                                  *, add_drive_file_ids: list[str] | None = None,
                                  ) -> dict:
    """Add Drive attachments to a specific student's submission.

    Returns the updated StudentSubmission. Raises ClassroomApiError on
    refusal - notably PERMISSION_DENIED (403) when the coursework was not
    created by this app's Developer Console project.

    NOTE: ModifyAttachmentsRequest has NO ``removeAttachments`` field - only
    ``addAttachments`` (and ``previewVersion``). Sending a removal field is
    rejected with 400 "Unknown name \\"removeAttachments\\": Cannot find
    field", which a live test confirmed. Duplicate suppression therefore has
    to happen BEFORE the call (skip when our file id is already attached),
    not by removing an old attachment afterwards.
    """
    if not add_drive_file_ids:
        return get_student_submission(token, course_id, coursework_id,
                                      submission_id)
    body = {"addAttachments": [
        {"driveFile": {"id": fid}} for fid in add_drive_file_ids]}
    url = (f"{CLASSROOM_BASE}/courses/{course_id}/courseWork/{coursework_id}"
           f"/studentSubmissions/{submission_id}:modifyAttachments")
    return classroom_request("POST", url, token, json=body)


def submission_can_be_modified(submission: dict) -> bool:
    """Google sets ``associatedWithDeveloper`` when this OAuth client is
    allowed to modify the submission. Checking it up-front lets the UI say
    'this assignment cannot be modified by the app' BEFORE spending an
    upload, instead of failing after the PDF is already in Drive."""
    return bool(submission.get("associatedWithDeveloper"))


@_guarded_write("write a draft grade")
def patch_draft_grade(token: str, course_id: str, coursework_id: str,
                      submission_id: str, draft_grade) -> dict:
    """Attempt a draft-grade write-back.

    Google only lets an OAuth project grade coursework it created; for
    teacher-created coursework this typically 403s (ProjectPermissionDenied),
    which the legacy app documented. V5 attempts it when configured and
    reports a clean fallback message instead of failing the job.
    """
    url = (f"{CLASSROOM_BASE}/courses/{course_id}/courseWork/{coursework_id}"
           f"/studentSubmissions/{submission_id}")
    body = {"draftGrade": draft_grade}
    try:
        return classroom_request("PATCH", url, token,
                                 params={"updateMask": "draftGrade"}, json=body)
    except ClassroomApiError as exc:
        if exc.status == 403:
            log_event("classroom.client",
                      "grade write-back refused (coursework not created by this app)",
                      level=logging.WARNING,
                      coursework_id=coursework_id)
            return {"refused": True, "reason": (
                "Google only allows an app to grade coursework it created. "
                "Enter the suggested grade in Classroom manually."
            )}
        raise
