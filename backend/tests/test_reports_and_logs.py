"""Teacher report API + log management tests (spec §§16-§22).

Covers the endpoints added for the Report page, the Clear Log workflow and
the whole-assignment correction gate:

  * assignment report: class/assignment info + statistics + student rows,
    with Classroom and Math Checker figures kept separate,
  * "not graded"/"not available" never rendered as a fake zero (§26),
  * class report aggregation by stable student id (§24),
  * PDF exports produce a real, paginated PDF (not a stub),
  * clear-log needs confirmation and never touches database records,
  * correct-all refuses non-allowlisted assignments and missing schemes.
"""

from __future__ import annotations

import time

import pytest

from app import db
from app.classroom import submissions as cs


ROSTER = {
    "u1": {"user_id": "u1", "name": "Ada Lovelace", "email": "ada@school.org"},
    "u2": {"user_id": "u2", "name": "Ben Bray", "email": "ben@school.org"},
    "u3": {"user_id": "u3", "name": "Cleo Chan", "email": "cleo@school.org"},
}


def _sub(sid: str, user: str, state: str, attachments: list[dict] | None = None,
         late: bool = False) -> dict:
    return {
        "id": sid,
        "userId": user,
        "state": state,
        "late": late,
        "creationTime": "2026-09-01T08:00:00Z",
        "updateTime": "2026-09-01T09:00:00Z",
        "assignmentSubmission": {"attachments": attachments or []},
    }


def seed_classroom() -> None:
    """u1 turned in (graded in Classroom), u2 draft, u3 roster-only."""
    subs = [
        _sub("s1", "u1", "TURNED_IN",
             [{"driveFile": {"id": "f1", "title": "hw.pdf"}}]),
        _sub("s2", "u2", "CREATED"),
    ]
    cs.store_sync_result("c1", "w1", ROSTER, subs)
    now = time.time()
    db.execute(
        "INSERT INTO courses(id, name, section, fetched_at) "
        "VALUES('c1','Maths 10A','A',?) ON CONFLICT(id) DO NOTHING", (now,))
    db.execute(
        "INSERT INTO coursework(id, course_id, title, max_points, due_date, "
        "allowlisted, fetched_at) VALUES('w1','c1','Algebra HW',20,"
        "'2026-09-10 18:00',1,?) ON CONFLICT(id) DO NOTHING", (now,))
    # Classroom grade ONLY for u1 — u2/u3 must stay "Not graded", never 0.
    db.execute(
        "UPDATE classroom_submissions SET assigned_grade = 17, max_points = 20 "
        "WHERE submission_id = 's1'")


def add_result(student_id: str, submission_id: str | None = None,
               earned: int = 16, available: int = 20) -> None:
    now = time.time()
    with db.transaction() as conn:
        job_id = f"job-{student_id}-{now}"
        conn.execute(
            "INSERT INTO correction_jobs(id, submission_id, coursework_id, "
            "course_id, student_id, student_name, status, created_at, "
            "updated_at) VALUES(?,?,?,?,?,?, 'COMPLETED', ?, ?)",
            (job_id, submission_id, "w1", "c1", student_id, "Student",
             now, now))
        conn.execute(
            "INSERT INTO results(id, job_id, coursework_id, submission_id, "
            "student_id, student_name, earned, available, percentage, "
            "provisional_grade, result_json, verification_json, created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (f"res-{student_id}-{now}", job_id, "w1", submission_id,
             student_id, "Student", earned, available,
             earned / available * 100, "16", '{"questions": []}',
             '{"status": "VALID"}', now))


@pytest.fixture
def client(fresh_state, monkeypatch):
    from app.config import get_config, save_config
    cfg = get_config()
    cfg.mock_mode = True
    cfg.role = "developer"
    save_config(cfg)
    from app.config import get_secrets
    get_secrets().set_gemini_keys(["AIzaMockKey1234567890abcdef"])

    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app.api import routes_all, student_routes, report_routes
    from app.classroom import tracking_routes

    app = FastAPI()
    routes_all.register(app)
    tracking_routes.register(app)
    student_routes.register(app)
    report_routes.register(app)

    from fastapi.responses import JSONResponse
    from app.errors import AppError

    @app.exception_handler(AppError)
    async def _app_error_handler(request, exc: AppError):
        return JSONResponse(status_code=exc.status, content=exc.to_dict())

    with TestClient(app) as test_client:
        yield test_client


# ------------------------------------------------------------- assignment


def test_assignment_report_separates_sources(client):
    seed_classroom()
    add_result("u1", "s1")

    data = client.get("/api/reports/assignment/w1").json()
    assert data["class"]["name"] == "Maths 10A"
    assert data["class"]["students_total"] == 3
    assert data["assignment"]["title"] == "Algebra HW"
    assert data["assignment"]["max_points"] == 20

    rows = {r["student_id"]: r for r in data["rows"]}
    # u1: Classroom 17/20 AND Math Checker 16/20 — kept separate
    assert rows["u1"]["classroom_graded"] is True
    assert rows["u1"]["assigned_grade"] == 17
    assert rows["u1"]["mc_earned"] == 16 and rows["u1"]["mc_available"] == 20
    # u2: draft, no grade — must be Not graded (never 0)
    assert rows["u2"]["classroom_graded"] is False
    assert rows["u2"]["mc_status"] == "NOT_PROCESSED"
    # u3: roster-only, not submitted
    assert rows["u3"]["status"] == "NOT_STARTED"
    assert rows["u3"]["classroom_graded"] is False

    stats = data["stats"]
    assert stats["submission"]["not_submitted"] >= 2  # u2 draft + u3 missing
    assert stats["submission"]["draft"] == 1
    assert stats["classroom_grading"]["graded"] == 1
    assert stats["math_checker"]["corrected"] == 1
    # Only ONE classroom grade exists (17/20 = 85%) — average must be 85,
    # not dragged down by fake zeros from ungraded students.
    assert stats["classroom_grading"]["average"] == 85.0
    # u2/u3 never submitted, so nothing is "awaiting a grade" from them:
    # ungraded counts SUBMITTED work without a grade, not absent work.
    assert stats["classroom_grading"]["ungraded"] == 0


def test_assignment_report_404(client):
    response = client.get("/api/reports/assignment/nope")
    assert response.status_code == 404


def test_assignment_pdf_is_a_real_pdf(client):
    seed_classroom()
    add_result("u1", "s1")
    response = client.get("/api/reports/assignment/w1/export?format=pdf")
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/pdf"
    body = response.content
    assert body.startswith(b"%PDF"), "not a real PDF"
    assert b"/Type /Page" in body or b"/Type/Page" in body
    assert b"assignment_" in response.headers["content-disposition"].encode()

    # Actually parse the PDF and read its text (reportlab compresses
    # content streams, so raw byte search would prove nothing).
    from pypdf import PdfReader
    import io as _io
    reader = PdfReader(_io.BytesIO(body))
    assert len(reader.pages) >= 1
    text = "\n".join(page.extract_text() or "" for page in reader.pages)
    # A cell that wraps prints as two lines, so compare on collapsed
    # whitespace -- that is extraction behaviour, not a content change.
    flat = " ".join(text.split())
    assert "Assignment Correction Report" in flat
    assert "Algebra HW" in flat
    # u2/u3 have no Classroom grade — the PDF must say so, not print 0.
    assert "Not graded" in flat
    # u1's real grade appears
    assert "17/20" in flat


def test_assignment_report_rejects_unknown_format(client):
    seed_classroom()
    response = client.get("/api/reports/assignment/w1/export?format=xlsx")
    assert response.status_code == 400


# ------------------------------------------------------------------ class


def test_class_report_aggregates_by_student_id(client):
    seed_classroom()
    add_result("u1", "s1")

    data = client.get("/api/reports/class/c1").json()
    assert data["class"]["course_id"] == "c1"
    assert data["class"]["assignments_total"] == 1
    assert len(data["students"]) == 3
    by_id = {s["student_id"]: s for s in data["students"]}
    # Repeated syncs must not duplicate a student (stable-id identity §24)
    assert by_id["u1"]["submitted"] == 1
    assert by_id["u1"]["corrected"] == 1
    assert by_id["u1"]["mc_total_earned"] == 16
    assert by_id["u2"]["submitted"] == 0          # draft is not submitted
    assert by_id["u2"]["classroom_average"] is None  # not "0%"
    assert by_id["u3"]["not_submitted"] == 1


def test_class_report_pdf_export(client):
    seed_classroom()
    add_result("u1", "s1")
    response = client.get("/api/reports/class/c1/export?format=pdf")
    assert response.status_code == 200
    assert response.content.startswith(b"%PDF")
    assert "class_" in response.headers["content-disposition"]

    # Parse it: the class report must contain the real class, students and
    # separate source columns.
    from pypdf import PdfReader
    import io as _io
    reader = PdfReader(_io.BytesIO(response.content))
    text = "\n".join(page.extract_text() or "" for page in reader.pages)
    flat = " ".join(text.split())
    # Column headers are set in caps by the design system, so compare the
    # content rather than its casing.
    assert "class performance report" in flat.lower()
    assert "Maths 10A" in flat
    assert "Ada Lovelace" in flat
    assert "math checker avg" in flat.lower()


def test_double_sync_does_not_duplicate_rows(client):
    """Sync twice — report row count stays stable (§24)."""
    seed_classroom()
    seed_classroom()
    data = client.get("/api/reports/assignment/w1").json()
    assert len(data["rows"]) == 3
    data2 = client.get("/api/reports/class/c1").json()
    assert len(data2["students"]) == 3


# ------------------------------------------------------------- clear log


def test_clear_log_requires_confirmation(client):
    response = client.post("/api/diagnostics/logs/clear", json={})
    assert response.status_code == 400
    assert "confirm" in response.json()["message"].lower()


def test_clear_log_truncates_file_but_never_db(client):
    from app.config import DATA_DIR
    log_path = DATA_DIR / "logs" / "app.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text('{"ts":"2026-01-01T00:00:00Z","level":"INFO",'
                        '"component":"test","event":"old entry"}\n' * 50,
                        encoding="utf-8")

    # Seed rows that a buggy "clear" might destroy.
    seed_classroom()
    add_result("u1", "s1")
    jobs_before = db.query_one("SELECT COUNT(*) AS n FROM correction_jobs")["n"]
    results_before = db.query_one("SELECT COUNT(*) AS n FROM results")["n"]
    subs_before = db.query_one(
        "SELECT COUNT(*) AS n FROM classroom_submissions")["n"]

    response = client.post("/api/diagnostics/logs/clear", json={"confirm": True})
    assert response.status_code == 200
    body = response.json()
    assert body["cleared"] is True
    assert body["bytes_removed"] > 0
    assert "untouched" in body["note"]

    # file cleared (only the clear event remains)
    content = log_path.read_text(encoding="utf-8")
    assert "old entry" not in content
    assert "log file cleared by user" in content

    # database untouched
    assert db.query_one("SELECT COUNT(*) AS n FROM correction_jobs")["n"] == jobs_before
    assert db.query_one("SELECT COUNT(*) AS n FROM results")["n"] == results_before
    assert db.query_one(
        "SELECT COUNT(*) AS n FROM classroom_submissions")["n"] == subs_before


def test_log_endpoint_returns_categories(client):
    from app.config import DATA_DIR
    log_path = DATA_DIR / "logs" / "app.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text(
        '{"ts":"2026-01-01T00:00:01Z","level":"INFO","component":"workers",'
        '"event":"job completed"}\n'
        '{"ts":"2026-01-01T00:00:02Z","level":"ERROR","component":"workers",'
        '"event":"job failed permanently"}\n'
        '{"ts":"2026-01-01T00:00:03Z","level":"WARNING","component":"gemini",'
        '"event":"key entered cooldown"}\n'
        '{"ts":"2026-01-01T00:00:04Z","level":"INFO","component":"app",'
        '"event":"Math Checker V5 started"}\n',
        encoding="utf-8")

    data = client.get("/api/diagnostics/logs").json()
    categories = {e["event"]: e["category"] for e in data["entries"]}
    assert categories["job completed"] == "completed"
    assert categories["job failed permanently"] == "failed"
    assert categories["key entered cooldown"] == "warning"
    assert categories["Math Checker V5 started"] == "system"

    only_failed = client.get("/api/diagnostics/logs?category=failed").json()
    assert all(e["category"] == "failed" for e in only_failed["entries"])


# --------------------------------------------------------- correct-all §14


def test_correct_all_refuses_when_not_allowlisted(client):
    seed_classroom()
    db.execute("UPDATE coursework SET allowlisted = 0 WHERE id = 'w1'")
    response = client.post(
        "/api/classroom/courses/c1/assignments/w1/correct-all")
    assert response.status_code == 403
    assert "allow" in response.json()["message"].lower()


def test_correct_all_requires_mark_scheme(client):
    seed_classroom()
    response = client.post(
        "/api/classroom/courses/c1/assignments/w1/correct-all")
    assert response.status_code == 400
    assert response.json()["code"] == "mark_scheme_missing"


def test_correct_all_classifies_and_skips_safely(client, monkeypatch):
    """Draft/missing students are skipped; eligible ones enqueued; a failure
    for one student never stops the rest (§14)."""
    seed_classroom()
    # u4: handed in WITH an attachment (second eligible student).
    cs.store_sync_result("c1", "w1",
                         {**ROSTER, "u4": {"user_id": "u4",
                                          "name": "Dan Doe",
                                          "email": "dan@school.org"}}, [
        _sub("s4", "u4", "TURNED_IN",
             [{"driveFile": {"id": "f4", "title": "hw4.pdf"}}]),
    ])
    # mark scheme present
    from app.config import DATA_DIR
    scheme_dir = DATA_DIR / "mark_schemes"
    scheme_dir.mkdir(parents=True, exist_ok=True)
    (scheme_dir / "w1.pdf").write_bytes(b"%PDF-1.4 scheme")

    enqueued = []

    class FakeService:
        def enqueue_classroom_submission(self, **kwargs):
            if kwargs["student_id"] == "u4":
                raise RuntimeError("simulated per-student failure")
            enqueued.append(kwargs["student_id"])
            return {"id": f"job-{kwargs['student_id']}"}

    import app.api.routes_all as ra
    monkeypatch.setattr(ra, "_correction_service", lambda: FakeService())

    # Avoid network: skip the refresh step.
    response = client.post(
        "/api/classroom/courses/c1/assignments/w1/correct-all",
        json={"refresh": False})
    assert response.status_code == 200, response.text
    body = response.json()

    # u1 eligible (has attachment) -> enqueued; u4's failure is REPORTED,
    # not fatal; u2 draft -> skipped safely; u3 roster-only -> skipped.
    assert body["total_students"] == 4
    assert "u1" in enqueued
    assert any(f["student"] == "Dan Doe" for f in body["enqueue_failed"])
    assert "Ben Bray" in body["skipped_draft"]
    assert "Cleo Chan" in body["skipped_missing"]
    # The response reports every bucket instead of dying half-way.
    for key in ("enqueued", "skipped_missing", "skipped_draft", "invalid",
                "unknown", "enqueue_failed"):
        assert key in body


def test_correct_all_reports_sync_warning_when_refresh_fails(client, monkeypatch):
    seed_classroom()
    from app.config import DATA_DIR
    scheme_dir = DATA_DIR / "mark_schemes"
    scheme_dir.mkdir(parents=True, exist_ok=True)
    (scheme_dir / "w1.pdf").write_bytes(b"%PDF-1.4 scheme")

    from app.classroom import submissions as cs
    monkeypatch.setattr(
        cs, "sync_submission_tracking",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("401 unauthorized")))

    import app.api.routes_all as ra
    monkeypatch.setattr(ra, "_correction_service", lambda: type(
        "S", (), {"enqueue_classroom_submission":
                  lambda self, **k: {"id": "job-x"}})())

    response = client.post(
        "/api/classroom/courses/c1/assignments/w1/correct-all",
        json={"refresh": True})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["refreshed"] is False
    assert body["sync_warning"] and "continuing" in body["sync_warning"]
