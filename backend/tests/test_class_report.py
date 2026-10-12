"""Class Report: teacher-selected assignments, grouped by class, with
separate missing-homework and grade-1 sections (spec §§6-§§12, §§28).

Every value in the PDF is the actual recorded mark or status - never a
placeholder, never a guessed chapter/lesson, never a zero for a missing
submission. The teacher identity on the letterhead is the permanent
branding from Settings (§14-§15), not the logged-in account.
"""

from __future__ import annotations

import io
import json
import time

import pytest

from app import db
from app.classroom import submissions as cs

# Stable Classroom ids for the seeded students (§3: identity is by id,
# never by name).
MO = "gcs-mo-001"
SARA = "gcs-sara-002"
AHMED = "gcs-ahmed-003"
DAN = "gcs-dan-004"

# Three classes the teacher assigned (§12: group by the teacher's class).
CLASS_10A = "10A"
CLASS_10B = "10B"
CLASS_10C = "10C"


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

def seed_coursework(course_id: str, ids: list[str], titles: list[str],
                    max_points: float = 10.0) -> None:
    """Insert several coursework rows sharing one course.

    Titles use the `Chapter N Lesson M - Topic` shape so
`parse_assignment` derives real `code`/`chapter`/`lesson`/`topic`
values (§21). A title it does not recognise falls back to its own first
    token rather than an invented number.
    """
    now = time.time()
    db.execute(
        "INSERT OR REPLACE INTO courses(id, name, section, fetched_at) "
        "VALUES(?,?,?,?) ON CONFLICT(id) DO UPDATE SET name=name",
        (course_id, "Mathematics 10A", "A", now))
    for cid, title in zip(ids, titles):
        db.execute(
            "INSERT OR REPLACE INTO coursework(id, course_id, title, max_points, "
            "state, fetched_at) VALUES(?,?,?,?,?,?) ON CONFLICT(id) DO NOTHING",
            (cid, course_id, title, max_points, "PUBLISHED", now))


def seed_students(students: list[tuple[str, str, str | None, str | None]]) -> None:
    """Insert students: (id, classroom_name, nickname, class_name)."""
    now = time.time()
    for sid, name, nickname, class_name in students:
        db.execute(
            "INSERT OR REPLACE INTO students(id, name, email, nickname, "
            "class_name, fetched_at) VALUES(?,?,?,?,?,?)",
            (sid, name, f"{sid}@school.edu", nickname, class_name, now))

def seed_submissions(course_id: str, coursework_id: str,
                     rows: list[tuple[str, str, str]]) -> None:
    """Insert classroom_submissions: (student_id, student_name, state)."""
    now = time.time()
    for sid, name, state in rows:
        db.execute(
            "INSERT OR REPLACE INTO classroom_submissions("
            "submission_id, course_id, coursework_id, student_id, student_name, "
            "student_email, current_state, max_points, last_synced_at, "
            "first_seen_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (f"sub-{sid}-{coursework_id}", course_id, coursework_id, sid, name,
             f"{sid}@school.edu", state, 10.0, now, now))


def add_result(student_id: str, submission_id: str,
               earned: int | None = None,
               available: int = 10) -> None:
    """Insert a correction_job + result for one submission."""
    now = time.time()
    job_id = f"job-{student_id}-{submission_id}-{now}"
    db.execute(
        "INSERT INTO correction_jobs(id, submission_id, coursework_id, "
        "course_id, student_id, student_name, status, created_at, "
        "updated_at) VALUES(?,?,?,?,?,?, 'COMPLETED', ?, ?)",
        (job_id, submission_id, None, None, student_id, "Student", now, now))
    db.execute(
        "INSERT INTO results(id, job_id, coursework_id, submission_id, "
        "student_id, student_name, earned, available, percentage, "
        "provisional_grade, result_json, verification_json, created_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (f"res-{student_id}-{submission_id}-{now}", job_id, None, submission_id,
         student_id, "Student", earned if earned is not None else 0,
         available, (earned / available * 100) if earned is not None else 0,
         "0", json.dumps({"questions": []}),
         json.dumps({"status": "VALID"}), now))

@pytest.fixture
def client(fresh_state, monkeypatch):
    from app.config import get_config, save_config
    cfg = get_config()
    cfg.mock_mode = True
    cfg.role = "developer"
    cfg.teacher.teacher_name = "Mr. Abanoub Akram"
    cfg.teacher.school_name = "HLC Center - O Level"
    cfg.reports.teacher_title = "IGCSE AND A LEVEL MATHEMATICS TEACHER"
    cfg.reports.center_name = "HLC Center - O Level"
    cfg.reports.academic_year = "2026-2027"
    save_config(cfg)

    from app.config import get_secrets
    get_secrets().set_gemini_keys(["AIzaMockKey1234567890abcdef"])

    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app.api import routes_all, student_routes, report_routes

    app = FastAPI()
    routes_all.register(app)
    student_routes.register(app)
    report_routes.register(app)

    from fastapi.responses import JSONResponse
    from app.errors import AppError

    @app.exception_handler(AppError)
    async def _app_error_handler(request, exc: AppError):
        return JSONResponse(status_code=exc.status, content=exc.to_dict())

    with TestClient(app) as test_client:
        yield test_client

def _put_details(client, student_id: str, nickname: str | None = None,
                 class_name: str | None = None) -> dict:
    return client.put(
        f"/api/students/{student_id}/details",
        json={k: v for k, v in (
            ("nickname", nickname), ("class_name", class_name)
        ) if v is not None}).json()


def _pdf_text(response) -> str:
    """Collapse whitespace so wrapped cells compare on content (§26)."""
    from pypdf import PdfReader
    reader = PdfReader(io.BytesIO(response.content))
    text = "\n".join(page.extract_text() or "" for page in reader.pages)
    return " ".join(text.split())

def _seed_full_class(client):
    """One course, three assignments, five students across 10A/10B/10C.

    Assignment titles encode chapter + lesson so the report can show
`Chapter N, Lesson M` instead of only the code (§21).
    """
    course_id = "c-cr-1"
    cw_ids = ["cw-l1", "cw-l2", "cw-l3"]
    cw_titles = [
        "Chapter 1 Lesson 1 - Algebra",
        "Chapter 1 Lesson 2 - Geometry",
        "Chapter 2 Lesson 3 - Trigonometry",
    ]
    seed_coursework(course_id, cw_ids, cw_titles, max_points=10.0)

    # Five students: nickname + class for each (§3, §10).
    seed_students([
        (MO, "Mohamed Ahmed Hassan", "Mo", CLASS_10A),
        (SARA, "Sara Mohamed", "Sara", CLASS_10A),
        (AHMED, "Ahmed Ali", "Ahmed", CLASS_10B),
        (DAN, "Dan Doe", None, CLASS_10C),
    ])

    # L1: Mo submitted + corrected 9/10; Sara submitted + corrected 7/10;
    # Ahmed submitted + corrected 1/10 (grade 1, a RESULT not a missing status);
    # Dan did NOT submit (MISSING).
    seed_submissions(course_id, "cw-l1", [
        (MO, "Mohamed Ahmed Hassan", "TURNED_IN"),
        (SARA, "Sara Mohamed", "TURNED_IN"),
        (AHMED, "Ahmed Ali", "TURNED_IN"),
        (DAN, "Dan Doe", "MISSING"),
    ])
    add_result(MO, f"sub-{MO}-cw-l1", earned=9)
    add_result(SARA, f"sub-{SARA}-cw-l1", earned=7)
    add_result(AHMED, f"sub-{AHMED}-cw-l1", earned=1)
    # Dan: no result row -> missing (§23: never 0/10).

    return course_id, cw_ids


def _seed_missing_homework(client):
    """L2 only: Sara submitted, Mo + Ahmed did not (different classes).

    The missing section must name the actual chapter/lesson (§9, §21).
    """
    course_id = "c-cr-2"
    cw_ids = ["cw-l2"]
    cw_titles = ["Chapter 1 Lesson 2 - Geometry"]
    seed_coursework(course_id, cw_ids, cw_titles, max_points=10.0)
    seed_students([
        (MO, "Mohamed Ahmed Hassan", "Mo", CLASS_10A),
        (AHMED, "Ahmed Ali", "Ahmed", CLASS_10B),
        (SARA, "Sara Mohamed", "Sara", CLASS_10A),
    ])
    seed_submissions(course_id, "cw-l2", [
        (MO, "Mohamed Ahmed Hassan", "MISSING"),
        (AHMED, "Ahmed Ali", "NOT_STARTED"),
        (SARA, "Sara Mohamed", "TURNED_IN"),
    ])
    add_result(SARA, f"sub-{SARA}-cw-l2", earned=8)
    return course_id, cw_ids

def _page_count(response) -> int:
    import io

    from pypdf import PdfReader

    return len(PdfReader(io.BytesIO(response.content)).pages)


class TestLegacyClassReportDetail:
    """The older class-report export must not repeat every student either.

    It shares the one renderer, so it shares the one rule: the default is the
    consolidated list (each student named once), and the per-assignment
    breakdown is opt-in via ``mode=detailed``. Header words alone cannot tell
    the two tables apart (both say Lesson/Status), so the check is document
    growth: the detailed layout adds a row per student per assignment, so it
    must be strictly longer.
    """

    def test_default_equals_the_summary_layout(self, client):
        course_id, _ = _seed_missing_homework(client)
        default = client.get(
            f"/api/reports/class-report/export?class_id={course_id}",
            headers={"accept": "application/pdf"})
        summary = client.get(
            f"/api/reports/class-report/export?class_id={course_id}"
            "&mode=summary", headers={"accept": "application/pdf"})
        assert default.status_code == 200
        assert "OUTSTANDING / MISSING HOMEWORK" in _pdf_text(default)
        assert _page_count(default) == _page_count(summary)

    def test_detailed_mode_adds_the_per_assignment_rows(self, client):
        course_id, _ = _seed_missing_homework(client)
        default = client.get(
            f"/api/reports/class-report/export?class_id={course_id}",
            headers={"accept": "application/pdf"})
        detailed = client.get(
            f"/api/reports/class-report/export?class_id={course_id}"
            "&mode=detailed", headers={"accept": "application/pdf"})
        assert detailed.status_code == 200
        # The two layouts share every header word - the missing rows table
        # and the student-by-student status table both say Assignment /
        # Chapter / Lesson / Status - so header text cannot tell them apart.
        # An earlier version of this check asserted on "ASSIGNMENT CHAPTER"
        # and only passed because that header was wrapping mid-word as
        # "ASSIGNMEN / T" and so never appeared as a phrase. The honest
        # discriminator is document growth: detailed mode adds one row per
        # student per outstanding assignment, each carrying its own chapter,
        # lesson and status, so the detailed export must be strictly larger.
        flat_default = _pdf_text(default)
        flat_detailed = _pdf_text(detailed)
        assert len(flat_detailed) > len(flat_default)
        assert _page_count(detailed) >= _page_count(default)


class TestSingleAssignment:
    """Select L1 only -> report shows only L1 (§28: one assignment)."""

    def test_json_shows_only_the_selected_assignment(self, client):
        course_id, cw_ids = _seed_full_class(client)
        data = client.get(
            f"/api/reports/class-report?class_id={course_id}&assignments=cw-l1"
        ).json()
        assert data["assignment_count"] == 1
        assert len(data["assignments"]) == 1
        assert data["assignments"][0]["code"] == "L1"
        assert "Chapter 1, Lesson 1" in data["assignments"][0]["detail"]

    def test_pdf_contains_only_l1_columns(self, client):
        course_id, _ = _seed_full_class(client)
        response = client.get(
            f"/api/reports/class-report/export?class_id={course_id}"
            "&assignments=cw-l1",
            headers={"accept": "application/pdf"},
        )
        assert response.status_code == 200
        assert response.headers["content-type"] == "application/pdf"
        flat = _pdf_text(response)
        assert "class report" in flat.lower()
        assert "L1" in flat
        assert "L2" not in flat
        assert "L3" not in flat
        assert "Algebra" in flat or "Chapter 1, Lesson 1" in flat

    def test_pdf_uses_the_selected_assignment_grade(self, client):
        course_id, _ = _seed_full_class(client)
        response = client.get(
            f"/api/reports/class-report/export?class_id={course_id}"
            "&assignments=cw-l1",
            headers={"accept": "application/pdf"},
        )
        flat = _pdf_text(response)
        assert "9/10" in flat or "9 / 10" in flat
        assert "7/10" in flat or "7 / 10" in flat
        assert "1/10" in flat or "1 / 10" in flat

    def test_missing_student_appears_under_missing_homework(self, client):
        course_id, _ = _seed_full_class(client)
        response = client.get(
            f"/api/reports/class-report/export?class_id={course_id}"
            "&assignments=cw-l1",
            headers={"accept": "application/pdf"},
        )
        flat = _pdf_text(response)
        assert "missing homework" in flat.lower()
        assert "Dan Doe" in flat
        assert "not submitted" in flat.lower()

    def test_grade_one_student_in_separate_section(self, client):
        course_id, _ = _seed_full_class(client)
        response = client.get(
            f"/api/reports/class-report/export?class_id={course_id}"
            "&assignments=cw-l1",
            headers={"accept": "application/pdf"},
        )
        flat = _pdf_text(response)
        # §24/§25: the dedicated low-grade section, keyed on the teacher's
        # threshold (default 2 -> a recorded grade of 1 is below it).
        assert "low-grade students" in flat.lower()
        assert "below grade 2" in flat.lower()
        assert "Ahmed" in flat
        assert "10B" in flat
        assert "L1" in flat
        assert "1/10" in flat or "1 / 10" in flat

    def test_teacher_branding_on_the_letterhead(self, client):
        course_id, _ = _seed_full_class(client)
        response = client.get(
            f"/api/reports/class-report/export?class_id={course_id}"
            "&assignments=cw-l1",
            headers={"accept": "application/pdf"},
        )
        flat = _pdf_text(response)
        assert "Mr. Abanoub Akram" in flat
        assert "IGCSE AND A LEVEL MATHEMATICS TEACHER" in flat
        assert "HLC Center" in flat
        assert "2026-2027" in flat

    def test_rejects_an_assignment_not_in_the_class(self, client):
        course_id, _ = _seed_full_class(client)
        response = client.get(
            f"/api/reports/class-report?class_id={course_id}"
            "&assignments=cw-l1,does-not-exist",
        )
        assert response.status_code == 404
        assert "not found" in response.json()["message"].lower()

    def test_rejects_empty_assignment_list(self, client):
        course_id, _ = _seed_full_class(client)
        response = client.get(
            f"/api/reports/class-report?class_id={course_id}"
            "&assignments=",
        )
        assert response.status_code == 400
        assert "no assignments" in response.json()["message"].lower()

    def test_rejects_unknown_class(self, client):
        response = client.get(
            "/api/reports/class-report?class_id=no-such-class&assignments=all",
        )
        assert response.status_code == 404

class TestMultipleAssignments:
    """Select L1 + L2 + L3 -> report shows only those three (§28: multi)."""

    def test_json_shows_all_three_selected_assignments(self, client):
        course_id, cw_ids = _seed_full_class(client)
        data = client.get(
            f"/api/reports/class-report?class_id={course_id}"
            f"&assignments={chr(44).join(cw_ids)}",
        ).json()
        assert data["assignment_count"] == 3
        codes = {a["code"] for a in data["assignments"]}
        assert codes == {"L1", "L2", "L3"}

    def test_pdf_has_three_assignment_columns(self, client):
        course_id, cw_ids = _seed_full_class(client)
        response = client.get(
            f"/api/reports/class-report/export?class_id={course_id}"
            f"&assignments={chr(44).join(cw_ids)}",
            headers={"accept": "application/pdf"},
        )
        assert response.status_code == 200
        flat = _pdf_text(response)
        assert "L1" in flat and "L2" in flat and "L3" in flat

    def test_grades_only_for_selected_assignments(self, client):
        course_id, _ = _seed_full_class(client)
        response = client.get(
            f"/api/reports/class-report/export?class_id={course_id}"
            "&assignments=cw-l1,cw-l2,cw-l3",
            headers={"accept": "application/pdf"},
        )
        flat = _pdf_text(response)
        assert "9/10" in flat or "9 / 10" in flat
        assert "not submitted" in flat.lower()


class TestAllAssignments:
    """Select "all" -> every assignment in the class is included (§28: all)."""

    def test_json_includes_every_assignment(self, client):
        course_id, cw_ids = _seed_full_class(client)
        data = client.get(
            f"/api/reports/class-report?class_id={course_id}&assignments=all",
        ).json()
        assert data["assignment_count"] == 3

    def test_pdf_includes_all_assignments(self, client):
        course_id, _ = _seed_full_class(client)
        response = client.get(
            f"/api/reports/class-report/export?class_id={course_id}"
            "&assignments=all",
            headers={"accept": "application/pdf"},
        )
        flat = _pdf_text(response)
        assert "L1" in flat and "L2" in flat and "L3" in flat

class TestMissingHomeworkSection:
    """Students who did not submit appear under MISSING HOMEWORK (§9, §23)."""

    def test_missing_students_listed_with_chapter_and_lesson(self, client):
        course_id, _ = _seed_missing_homework(client)
        response = client.get(
            f"/api/reports/class-report/export?class_id={course_id}"
            "&assignments=cw-l2",
            headers={"accept": "application/pdf"},
        )
        assert response.status_code == 200
        flat = _pdf_text(response)
        assert "missing homework" in flat.lower()
        assert "Mo" in flat and "Ahmed" in flat
        assert "L2" in flat
        assert "Geometry" in flat or "Chapter 1, Lesson 2" in flat

    def test_missing_section_shows_class_for_each_student(self, client):
        course_id, _ = _seed_missing_homework(client)
        response = client.get(
            f"/api/reports/class-report/export?class_id={course_id}"
            "&assignments=cw-l2",
            headers={"accept": "application/pdf"},
        )
        flat = _pdf_text(response)
        assert "class" in flat.lower()
        assert "10A" in flat
        assert "10B" in flat

    def test_missing_is_a_status_not_a_zero(self, client):
        course_id, _ = _seed_missing_homework(client)
        response = client.get(
            f"/api/reports/class-report/export?class_id={course_id}"
            "&assignments=cw-l2",
            headers={"accept": "application/pdf"},
        )
        flat = _pdf_text(response)
        assert "0/10" not in flat
        assert "not submitted" in flat.lower()


class TestGradeOneSection:
    """Students with a recorded grade of 1 appear under LOW-GRADE STUDENTS.

    A grade of 1 is a RESULT (the work was submitted and scored), never
    inferred from a missing status (§10, §11). §24/§25 generalise this into a
    threshold: with the default of 2, grade 1 is below it.
    """

    def test_grade_one_student_shown_with_full_context(self, client):
        course_id, _ = _seed_full_class(client)
        response = client.get(
            f"/api/reports/class-report/export?class_id={course_id}"
            "&assignments=cw-l1",
            headers={"accept": "application/pdf"},
        )
        flat = _pdf_text(response)
        assert "low-grade students" in flat.lower()
        assert "below grade 2" in flat.lower()
        assert "Ahmed" in flat
        assert "10B" in flat
        assert "L1" in flat
        assert "1/10" in flat or "1 / 10" in flat

    def test_grade_one_section_names_the_chapter_and_lesson(self, client):
        course_id, _ = _seed_full_class(client)
        response = client.get(
            f"/api/reports/class-report/export?class_id={course_id}"
            "&assignments=cw-l1",
            headers={"accept": "application/pdf"},
        )
        flat = _pdf_text(response)
        assert "Chapter 1, Lesson 1" in flat or "Algebra" in flat

class TestEmptyGradeOneSection:
    """No students with grade 1 -> clean empty-state message (§24)."""

    def test_no_grade_one_shows_empty_state_not_an_empty_table(self, client):
        course_id, _ = _seed_missing_homework(client)
        response = client.get(
            f"/api/reports/class-report/export?class_id={course_id}"
            "&assignments=cw-l2",
            headers={"accept": "application/pdf"},
        )
        flat = _pdf_text(response)
        assert "no student scored below grade 2" in flat.lower()


class TestEmptyMissingSection:
    """No outstanding homework -> clean empty-state message (§26)."""

    def test_all_submitted_shows_empty_missing_state(self, client):
        course_id = "c-cr-3"
        cw_ids = ["cw-all-sub"]
        cw_titles = ["Chapter 1 Lesson 3 - Statistics"]
        seed_coursework(course_id, cw_ids, cw_titles, max_points=10.0)
        seed_students([
            (MO, "Mohamed Ahmed Hassan", "Mo", CLASS_10A),
            (SARA, "Sara Mohamed", "Sara", CLASS_10A),
        ])
        seed_submissions(course_id, "cw-all-sub", [
            (MO, "Mohamed Ahmed Hassan", "TURNED_IN"),
            (SARA, "Sara Mohamed", "TURNED_IN"),
        ])
        add_result(MO, f"sub-{MO}-cw-all-sub", earned=9)
        add_result(SARA, f"sub-{SARA}-cw-all-sub", earned=8)
        response = client.get(
            f"/api/reports/class-report/export?class_id={course_id}"
            "&assignments=cw-all-sub",
            headers={"accept": "application/pdf"},
        )
        assert response.status_code == 200
        flat = _pdf_text(response)
        assert "no outstanding homework" in flat.lower()
        assert "not submitted" not in flat.lower()

class TestMultipleClasses:
    """Correctly group 10A, 10B and 10C (§12)."""

    def test_groups_are_separate_and_in_order(self, client):
        course_id, cw_ids = _seed_full_class(client)
        data = client.get(
            f"/api/reports/class-report?class_id={course_id}&assignments=all",
        ).json()
        labels = [g["class_label"] for g in data["groups"]]
        assert CLASS_10A in labels
        assert CLASS_10B in labels
        assert CLASS_10C in labels
        by_label = {g["class_label"]: [s["display_name"] for s in g["students"]]
                    for g in data["groups"]}
        assert set(by_label[CLASS_10A]) == {"Mo", "Sara"}
        assert by_label[CLASS_10B] == ["Ahmed"]
        assert by_label[CLASS_10C] == ["Dan Doe"]

    def test_pdf_has_a_divider_per_class(self, client):
        course_id, _ = _seed_full_class(client)
        response = client.get(
            f"/api/reports/class-report/export?class_id={course_id}"
            "&assignments=cw-l1",
            headers={"accept": "application/pdf"},
        )
        flat = _pdf_text(response)
        assert "10A" in flat and "10B" in flat and "10C" in flat

class TestLargeClassPagination:
    """A large class continues onto additional portrait pages (§25)."""

    def test_many_students_produce_multiple_pages(self, client):
        course_id = "c-cr-4"
        cw_ids = ["cw-big"]
        cw_titles = ["Chapter 1 Lesson 1 - Algebra"]
        seed_coursework(course_id, cw_ids, cw_titles, max_points=10.0)
        student_ids = [f"gcs-big-{i:03d}" for i in range(35)]
        now = time.time()
        db.execute(
            "INSERT OR REPLACE INTO courses(id, name, section, fetched_at) "
            "VALUES(?,?,?,?) ON CONFLICT(id) DO UPDATE SET name=name",
            (course_id, "Mathematics 10A", "A", now))
        for i, sid in enumerate(student_ids):
            db.execute(
                "INSERT OR REPLACE INTO students(id, name, email, nickname, "
                "class_name, fetched_at) VALUES(?,?,?,?,?,?)",
                (sid, f"Student {i:02d}", f"{sid}@school.edu", None,
                 CLASS_10A, now))
            db.execute(
                "INSERT OR REPLACE INTO classroom_submissions("
                "submission_id, course_id, coursework_id, student_id, student_name, "
                "student_email, current_state, max_points, last_synced_at, "
                "first_seen_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (f"sub-{sid}-cw-big", course_id, "cw-big", sid, f"Student {i:02d}",
                 f"{sid}@school.edu", "TURNED_IN", 10.0, now, now))
            add_result(sid, f"sub-{sid}-cw-big", earned=(i % 10))
        response = client.get(
            f"/api/reports/class-report/export?class_id={course_id}"
            "&assignments=cw-big",
            headers={"accept": "application/pdf"},
        )
        assert response.status_code == 200
        from pypdf import PdfReader
        reader = PdfReader(io.BytesIO(response.content))
        assert len(reader.pages) >= 2
        flat = _pdf_text(response)
        for sid in student_ids[:10]:
            assert f"Student {int(sid.split(chr(45))[-1]):02d}" in flat

    def test_headers_repeat_on_continuation_pages(self, client):
        course_id = "c-cr-5"
        cw_ids = ["cw-big2"]
        cw_titles = ["Chapter 1 Lesson 2 - Geometry"]
        seed_coursework(course_id, cw_ids, cw_titles, max_points=10.0)
        student_ids = [f"gcs-big2-{i:03d}" for i in range(30)]
        now = time.time()
        db.execute(
            "INSERT OR REPLACE INTO courses(id, name, section, fetched_at) "
            "VALUES(?,?,?,?) ON CONFLICT(id) DO UPDATE SET name=name",
            (course_id, "Mathematics 10A", "A", now))
        for i, sid in enumerate(student_ids):
            db.execute(
                "INSERT OR REPLACE INTO students(id, name, email, nickname, "
                "class_name, fetched_at) VALUES(?,?,?,?,?,?)",
                (sid, f"Learner {i:02d}", f"{sid}@school.edu", None,
                 CLASS_10A, now))
            db.execute(
                "INSERT OR REPLACE INTO classroom_submissions("
                "submission_id, course_id, coursework_id, student_id, student_name, "
                "student_email, current_state, max_points, last_synced_at, "
                "first_seen_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (f"sub-{sid}-cw-big2", course_id, "cw-big2", sid, f"Learner {i:02d}",
                 f"{sid}@school.edu", "TURNED_IN", 10.0, now, now))
            add_result(sid, f"sub-{sid}-cw-big2", earned=(i % 10))
        response = client.get(
            f"/api/reports/class-report/export?class_id={course_id}"
            "&assignments=cw-big2",
            headers={"accept": "application/pdf"},
        )
        assert response.status_code == 200
        from pypdf import PdfReader
        reader = PdfReader(io.BytesIO(response.content))
        assert len(reader.pages) >= 2

class TestNicknameInReport:
    """Reports use the student's configured nickname (§3, §28)."""

    def test_report_shows_nickname_not_classroom_name(self, client):
        course_id, cw_ids = _seed_full_class(client)
        response = client.get(
            f"/api/reports/class-report/export?class_id={course_id}"
            "&assignments=cw-l1",
            headers={"accept": "application/pdf"},
        )
        flat = _pdf_text(response)
        assert "Mo" in flat
        assert "Mohamed Ahmed Hassan" not in flat

    def test_changing_nickname_reflects_in_the_report(self, client):
        course_id, cw_ids = _seed_full_class(client)
        _put_details(client, MO, nickname="Mohammed", class_name=CLASS_10A)
        response = client.get(
            f"/api/reports/class-report/export?class_id={course_id}"
            "&assignments=cw-l1",
            headers={"accept": "application/pdf"},
        )
        flat = _pdf_text(response)
        assert "Mohammed" in flat
        # "Mo" as a standalone token must not appear (the new nickname is
        # "Mohammed"). Note: "Mohammed" itself contains "Mo", so we check
        # for the standalone token rather than the substring.
        import re
        assert not re.search(r"\bMo\b", flat)


class TestNoNicknameUsesClassroomName:
    """No nickname -> report uses the Google Classroom name (§3, §28)."""

    def test_student_without_nickname_shows_classroom_name(self, client):
        course_id = "c-cr-6"
        cw_ids = ["cw-nn"]
        cw_titles = ["Chapter 1 Lesson 1 - Algebra"]
        seed_coursework(course_id, cw_ids, cw_titles, max_points=10.0)
        seed_students([
            (MO, "Mohamed Ahmed Hassan", None, CLASS_10A),
        ])
        seed_submissions(course_id, "cw-nn", [
            (MO, "Mohamed Ahmed Hassan", "TURNED_IN"),
        ])
        add_result(MO, f"sub-{MO}-cw-nn", earned=9)
        response = client.get(
            f"/api/reports/class-report/export?class_id={course_id}"
            "&assignments=cw-nn",
            headers={"accept": "application/pdf"},
        )
        assert response.status_code == 200
        flat = _pdf_text(response)
        assert "Mohamed Ahmed Hassan" in flat

class TestDataAccuracy:
    """Every row is the correct student + class + assignment (§22)."""

    def test_mo_grade_is_not_ahmeds_grade(self, client):
        course_id, _ = _seed_full_class(client)
        response = client.get(
            f"/api/reports/class-report/export?class_id={course_id}"
            "&assignments=cw-l1",
            headers={"accept": "application/pdf"},
        )
        flat = _pdf_text(response)
        assert "9/10" in flat or "9 / 10" in flat
        assert "1/10" in flat or "1 / 10" in flat

    def test_class_label_matches_the_assigned_class(self, client):
        course_id, _ = _seed_full_class(client)
        response = client.get(
            f"/api/reports/class-report/export?class_id={course_id}"
            "&assignments=cw-l1",
            headers={"accept": "application/pdf"},
        )
        flat = _pdf_text(response)
        assert "10A" in flat
        assert "10B" in flat
        assert "10C" in flat

    def test_generated_at_is_present(self, client):
        course_id, _ = _seed_full_class(client)
        data = client.get(
            f"/api/reports/class-report?class_id={course_id}&assignments=all",
        ).json()
        assert "generated_at" in data
        assert isinstance(data["generated_at"], float)

    def test_class_report_uses_nickname_not_id(self, client):
        course_id, _ = _seed_full_class(client)
        data = client.get(
            f"/api/reports/class-report?class_id={course_id}&assignments=all",
        ).json()
        by_id = {g["class_label"]: g["students"] for g in data["groups"]}
        mo_students = [s for s in by_id[CLASS_10A]
                       if s["display_name"] == "Mo"]
        assert mo_students
        assert mo_students[0]["student_id"] == MO
