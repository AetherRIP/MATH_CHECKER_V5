"""Per-student report export: correct, unambiguous and free of leakage.

The export is the one artefact a teacher hands to someone else, so it must be
right about more than the marks:

* the filename carries the right student and extension, and a non-ASCII name
  cannot break the HTTP header (RFC 5987 ``filename*``);
* the PDF/CSV contain this student's own rows and marks, not a neighbour's;
* unicode and mathematical text survives the round trip;
* a student with no assignments still exports (an empty report, not a crash);
* an invalid format is a clear 400, an unknown student a 404;
* no credential, key material or internal filesystem path ever appears.

All data here is synthetic: invented rosters and marks, no Classroom calls.
"""

from __future__ import annotations

import csv
import io
import time

import pytest

from app import db
from app.classroom import submissions as cs

ROSTER = {
    "u1": {"user_id": "u1", "name": "Ada Love läuft — ¼ × ½", "email": "ada@example.invalid"},
    "u2": {"user_id": "u2", "name": "Ben Byte", "email": "ben@example.invalid"},
    "u3": {"user_id": "u3", "name": "خالد الاختبار", "email": "khaled@example.invalid"},
    "u4": {"user_id": "u4", "name": "Cleo Empty", "email": "cleo@example.invalid"},
}

ASSIGNMENT_TITLE = "Sets and Venn diagrams — ∫ 2x dx"


def _sub(sid: str, user: str, state: str, late: bool = False) -> dict:
    return {
        "id": sid,
        "userId": user,
        "state": state,
        "late": late,
        "creationTime": "2026-09-01T08:00:00Z",
        "updateTime": "2026-09-01T09:00:00Z",
        "assignmentSubmission": {"attachments": []},
    }


def seed() -> None:
    subs = [
        _sub("s1", "u1", "TURNED_IN"),
        _sub("s2", "u2", "TURNED_IN", late=True),
        _sub("s3", "u3", "CREATED"),
    ]
    cs.store_sync_result("c1", "w1", ROSTER, subs)
    now = time.time()
    db.execute("INSERT INTO courses(id, name, section, fetched_at) "
               "VALUES('c1','Synthetic Maths','10S',?) ON CONFLICT(id) DO NOTHING",
               (now,))
    db.execute(
        "INSERT INTO coursework(id, course_id, title, max_points, due_date, "
        "fetched_at) VALUES('w1','c1',?,10,'2026-10-10 20:00',?) "
        "ON CONFLICT(id) DO NOTHING",
        (ASSIGNMENT_TITLE, now))
    # A roster-only student: known to the app, no assignment rows at all.
    db.execute(
        "INSERT INTO students(id, name, email, fetched_at) VALUES('u-empty',?,?,?) "
        "ON CONFLICT(id) DO NOTHING",
        ("Empty Example", "empty@example.invalid", now))


def add_result(student_id: str, submission_id: str, *, earned: int = 8,
               available: int = 10, name: str | None = None) -> str:
    now = time.time()
    result_id = f"res-{student_id}"
    db.execute(
        "INSERT INTO correction_jobs(id, submission_id, coursework_id, course_id,"
        " student_id, student_name, status, created_at, updated_at)"
        " VALUES(?,?,?,?,?,?,'COMPLETED',?,?)",
        (f"job-{student_id}", submission_id, "w1", "c1", student_id,
         name or ROSTER[student_id]["name"], now, now))
    db.execute(
        "INSERT INTO results(id, job_id, coursework_id, submission_id, student_id,"
        " student_name, earned, available, percentage, provisional_grade,"
        " result_json, verification_json, created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (result_id, f"job-{student_id}", "w1", submission_id, student_id,
         name or ROSTER[student_id]["name"], earned, available,
         earned / available * 100, "8",
         '{"questions":[{"question_number":"Q1","max_marks":4,"awarded_marks":3,'
         '"topic":"Sets — ∩ and ∪"}]}',
         '{"status":"VALID"}', now))
    return result_id


@pytest.fixture
def client(fresh_state):
    from app.config import get_config, save_config
    cfg = get_config()
    cfg.mock_mode = True
    cfg.teacher.teacher_name = "Synthetic Teacher"
    cfg.reports.center_name = "Synthetic Centre"
    save_config(cfg)

    from fastapi import FastAPI
    from fastapi.responses import JSONResponse
    from fastapi.testclient import TestClient
    from app.api import report_routes, routes_all, student_routes
    from app.classroom import tracking_routes
    from app.errors import AppError

    app = FastAPI()
    routes_all.register(app)
    tracking_routes.register(app)
    student_routes.register(app)
    report_routes.register(app)

    @app.exception_handler(AppError)
    async def _app_error(_request, exc: AppError):
        return JSONResponse({"code": exc.code, "message": exc.user_message},
                            status_code=exc.status)

    with TestClient(app) as test_client:
        yield test_client


# ------------------------------------------------------------------ filenames


def test_pdf_export_has_the_right_extension_name_and_content(client):
    seed()
    add_result("u1", "s1")

    response = client.get("/api/students/u1/report?format=pdf")

    assert response.status_code == 200
    assert response.headers["content-type"] == "application/pdf"
    disposition = response.headers["content-disposition"]
    assert disposition.startswith("attachment;")
    assert "student_" in disposition and ".pdf" in disposition
    assert response.content.startswith(b"%PDF-")
    assert b"%%EOF" in response.content


def test_csv_export_has_the_right_extension_and_header(client):
    seed()
    add_result("u1", "s1")

    response = client.get("/api/students/u1/report?format=csv")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/csv")
    assert ".csv" in response.headers["content-disposition"]
    header = response.text.splitlines()[0]
    for column in ("student", "official_name", "assignment", "v5_earned",
                   "quality", "last_synced"):
        assert column in header


def test_a_non_ascii_name_cannot_break_the_header(client):
    """Arabic names are not latin-1 encodable: the header must still be valid
    (RFC 5987 ``filename*`` with an ASCII fallback), not a 500."""
    seed()

    response = client.get("/api/students/u3/report?format=pdf")

    assert response.status_code == 200
    disposition = response.headers["content-disposition"]
    disposition.encode("latin-1")          # must be a legal HTTP header
    if "filename*" in disposition:
        assert "UTF-8''" in disposition
    assert response.content.startswith(b"%PDF-")


def test_a_unicode_csv_export_keeps_the_assignment_title(client):
    seed()
    add_result("u1", "s1")

    response = client.get("/api/students/u1/report?format=csv")

    assert response.status_code == 200
    assert ASSIGNMENT_TITLE in response.text
    assert "Sets — ∩ and ∪" not in response.text  # that is the question detail


# ------------------------------------------------------------------- content


def test_the_export_contains_only_this_students_rows(client):
    seed()
    add_result("u1", "s1", earned=9)
    add_result("u2", "s2", earned=3)

    csv_body = client.get("/api/students/u1/report?format=csv").text

    assert "Ada Love" in csv_body
    assert "Ben Byte" not in csv_body
    rows = list(csv.DictReader(io.StringIO(csv_body)))
    assert len(rows) == 1
    assert rows[0]["student"].startswith("Ada Love")
    assert rows[0]["v5_earned"] == "9"
    assert "ben@example.invalid" not in csv_body


def test_the_pdf_export_does_not_carry_another_students_name(client):
    seed()
    add_result("u1", "s1")
    add_result("u2", "s2")

    pdf_bytes = client.get("/api/students/u1/report?format=pdf").content

    assert b"Ada Love" in pdf_bytes or b"Ada" in pdf_bytes
    assert b"Ben Byte" not in pdf_bytes


def test_the_export_never_contains_keys_or_internal_paths(client):
    seed()
    add_result("u1", "s1")
    from app.config import get_secrets
    get_secrets().set_gemini_keys(["AIzaLeakCheckKey1234567890"])

    pdf_bytes = client.get("/api/students/u1/report?format=pdf").content
    csv_body = client.get("/api/students/u1/report?format=csv").text

    for payload in (pdf_bytes, csv_body.encode("utf-8")):
        assert b"AIzaLeakCheckKey" not in payload
        assert b"data" + b"\\\\" not in payload   # no Windows data-dir path
        assert b"/retained/" not in payload


def test_a_student_with_no_assignments_still_exports(client):
    seed()

    pdf = client.get("/api/students/u-empty/report?format=pdf")
    csv_response = client.get("/api/students/u-empty/report?format=csv")

    assert pdf.status_code == 200
    assert pdf.content.startswith(b"%PDF-")
    assert csv_response.status_code == 200
    assert len(csv_response.text.strip().splitlines()) == 1   # header only


def test_a_tracked_but_unmarked_assignment_exports_no_invented_mark(client):
    seed()

    body = client.get("/api/students/u4/report?format=csv").text

    assert "Not started" in body
    rows = body.strip().splitlines()
    assert len(rows) == 2                       # header + the tracked assignment
    assert ",no,,10.0,,,VERIFIED" in rows[1]     # no grade, no V5 mark, no 0


# -------------------------------------------------------------------- errors


def test_an_unknown_format_is_a_clear_400(client):
    seed()

    response = client.get("/api/students/u1/report?format=docx")

    assert response.status_code == 400
    assert response.json()["code"] == "bad_report_format"
    assert "pdf" in response.json()["message"].lower()


def test_an_unknown_student_is_a_404(client):
    seed()

    response = client.get("/api/students/nobody/report?format=pdf")

    assert response.status_code == 404


# --------------------------------------------------------- assignment report


def test_assignment_report_needs_a_stored_correction(client):
    seed()

    missing = client.get("/api/students/u1/assignments/w1/report?format=pdf")
    assert missing.status_code == 404
    assert missing.json()["code"] == "no_correction"

    add_result("u1", "s1")
    ok = client.get("/api/students/u1/assignments/w1/report?format=pdf")
    assert ok.status_code == 200
    assert ok.content.startswith(b"%PDF-")
    disposition = ok.headers["content-disposition"]
    assert ".pdf" in disposition and "w1" in disposition


def test_assignment_report_rejects_other_formats_and_detail_values(client):
    seed()
    add_result("u1", "s1")

    bad_format = client.get("/api/students/u1/assignments/w1/report?format=csv")
    assert bad_format.status_code == 400
    assert bad_format.json()["code"] == "bad_report_format"

    bad_detail = client.get(
        "/api/students/u1/assignments/w1/report?detail=everything")
    assert bad_detail.status_code == 400
    assert bad_detail.json()["code"] == "bad_detail"


def test_assignment_report_summary_and_detailed_both_render(client):
    seed()
    add_result("u1", "s1")

    for detail in ("summary", "standard", "detailed"):
        response = client.get(
            f"/api/students/u1/assignments/w1/report?detail={detail}")
        assert response.status_code == 200, detail
        assert response.content.startswith(b"%PDF-")
