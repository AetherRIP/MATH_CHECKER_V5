"""Chapter + lesson identifiers must survive the entire report pipeline.

The reports used to print "L1" for EVERY chapter's first lesson, because the
compact code was built from the lesson number alone (``code = f"L{lesson}"``)
and the chapter the parser had just read was thrown away. CH1L1 and CH2L1 then
became the same string, so they were indistinguishable in the missing-homework
table, in the "Assignments included" key and in the per-student status table -
and the grade matrix, which paired a student's mark with its assignment BY that
code, could show one chapter's mark under both chapters' columns.

These tests pin the full identifier (CH1L1, CH2L1, ...) from the source
Classroom title through parsing, sorting, grouping, the preview model and the
rendered PDF, and pin the safety rules around what the source data cannot
prove: a lesson with no chapter keeps its own code, and an identifier the
parser cannot trust is never given an invented chapter.
"""

from __future__ import annotations

import io
import json
import time

import pytest

from app import db
from app.reporting.class_report import assemble, parse_assignment, preview_model

MO = "gcs-ch-mo-001"
SARA = "gcs-ch-sara-002"

CLASS_10A = "10A"
COURSE = "c-ch-1"

#: Five assignments whose lesson numbers OVERLAP across chapters, one stored in
#: the long "Chapter N Lesson M" shape.
TITLES = [
    ("cw-x-1", "CH1L1 - Algebra Basics"),
    ("cw-x-2", "CH1L2 - Linear Equations"),
    ("cw-x-3", "CH2L1 - Trigonometry Intro"),
    ("cw-x-4", "CH2L2 - Sine Rule"),
    ("cw-x-5", "Chapter 3 Lesson 1 - Circles"),
]

FULL_CODES = ["CH1L1", "CH1L2", "CH2L1", "CH2L2", "CH3L1"]


# --------------------------------------------------------------- seeding


def seed_coursework(titles: list[tuple[str, str]],
                    max_points: float = 10.0) -> None:
    now = time.time()
    db.execute(
        "INSERT OR REPLACE INTO courses(id, name, section, fetched_at) "
        "VALUES(?,?,?,?) ON CONFLICT(id) DO UPDATE SET name=name",
        (COURSE, "Mathematics 10A", "A", now))
    for cid, title in titles:
        db.execute(
            "INSERT OR REPLACE INTO coursework(id, course_id, title, max_points, "
            "state, fetched_at) VALUES(?,?,?,?,?,?) ON CONFLICT(id) DO NOTHING",
            (cid, COURSE, title, max_points, "PUBLISHED", now))


def seed_students(students: list[tuple[str, str, str | None, str | None]]) -> None:
    now = time.time()
    for sid, name, nickname, class_name in students:
        db.execute(
            "INSERT OR REPLACE INTO students(id, name, email, nickname, "
            "class_name, fetched_at) VALUES(?,?,?,?,?,?)",
            (sid, name, f"{sid}@school.edu", nickname, class_name, now))


def seed_submissions(coursework_id: str, rows: list[tuple[str, str, str]]) -> None:
    now = time.time()
    for sid, name, state in rows:
        db.execute(
            "INSERT OR REPLACE INTO classroom_submissions("
            "submission_id, course_id, coursework_id, student_id, student_name, "
            "student_email, current_state, max_points, last_synced_at, "
            "first_seen_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (f"sub-{sid}-{coursework_id}", COURSE, coursework_id, sid, name,
             f"{sid}@school.edu", state, 10.0, now, now))


def add_result(student_id: str, coursework_id: str, earned: int,
               available: int = 10) -> None:
    now = time.time()
    submission_id = f"sub-{student_id}-{coursework_id}"
    db.execute(
        "INSERT INTO correction_jobs(id, submission_id, coursework_id, "
        "course_id, student_id, student_name, status, created_at, "
        "updated_at) VALUES(?,?,?,?,?,?, 'COMPLETED', ?, ?)",
        (f"job-{student_id}-{coursework_id}", submission_id, None, None,
         student_id, "Student", now, now))
    db.execute(
        "INSERT INTO results(id, job_id, coursework_id, submission_id, "
        "student_id, student_name, earned, available, percentage, "
        "provisional_grade, result_json, verification_json, created_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (f"res-{student_id}-{coursework_id}", f"job-{student_id}-{coursework_id}",
         None, submission_id, student_id, "Student", earned, available,
         earned / available * 100, "0", json.dumps({"questions": []}),
         json.dumps({"status": "VALID"}), now))


def _seed_two_chapters() -> None:
    """Two chapters whose lesson 1 and lesson 2 both exist, plus a third.

    Mo is missing chapter 2's L1 and L2; Sara is missing chapter 1's L1 - so
    the per-lesson missing lists differ and can be told apart.
    """
    seed_coursework(TITLES)
    seed_students([
        (MO, "Mohamed Ahmed Hassan", "Mo", CLASS_10A),
        (SARA, "Sara Mohamed", "Sara", CLASS_10A),
    ])
    # CH1L1: Mo handed in (4/10); Sara never did -> the ONLY thing she misses.
    seed_submissions("cw-x-1", [(MO, "Mohamed Ahmed Hassan", "TURNED_IN"),
                                (SARA, "Sara Mohamed", "MISSING")])
    add_result(MO, "cw-x-1", 4)
    # CH2L1: the other way round - Mo missing, Sara handed in 8/10.
    seed_submissions("cw-x-3", [(MO, "Mohamed Ahmed Hassan", "MISSING"),
                                (SARA, "Sara Mohamed", "TURNED_IN")])
    add_result(SARA, "cw-x-3", 8)
    # CH1L2 / CH2L2: Mo is outstanding on CH2L2 as well, Sara handed both in.
    seed_submissions("cw-x-2", [(MO, "Mohamed Ahmed Hassan", "TURNED_IN"),
                                (SARA, "Sara Mohamed", "TURNED_IN")])
    add_result(MO, "cw-x-2", 6)
    add_result(SARA, "cw-x-2", 2)
    seed_submissions("cw-x-4", [(MO, "Mohamed Ahmed Hassan", "NOT_STARTED"),
                                (SARA, "Sara Mohamed", "TURNED_IN")])
    add_result(SARA, "cw-x-4", 9)
    # A third chapter, so "grouped under the correct chapter" has three to sort.
    seed_submissions("cw-x-5", [(MO, "Mohamed Ahmed Hassan", "TURNED_IN"),
                                (SARA, "Sara Mohamed", "TURNED_IN")])
    add_result(MO, "cw-x-5", 7)
    add_result(SARA, "cw-x-5", 7)


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


def _pdf_text(response) -> str:
    from pypdf import PdfReader
    reader = PdfReader(io.BytesIO(response.content))
    text = "\n".join(page.extract_text() or "" for page in reader.pages)
    return " ".join(text.split())


def _build(client, **params) -> dict:
    query = {"report_type": "class", "assignments": "all", "classes": "all",
             "mode": "detailed"}
    query.update(params)
    response = client.get("/api/reports/build", params=query)
    assert response.status_code == 200, response.text
    return response.json()


def _pdf(client, **params) -> str:
    query = {"report_type": "class", "assignments": "all", "classes": "all",
             "mode": "detailed"}
    query.update(params)
    response = client.get("/api/reports/build/export", params=query)
    assert response.status_code == 200, response.text
    return _pdf_text(response)


# ------------------------------------------- the model, without the database


def _course_model() -> dict:
    return {"id": COURSE,
            "courses": [{"id": cid, "title": title, "max_points": 10.0}
                        for cid, title in TITLES]}


def _report_rows() -> list[dict]:
    """The shared student x assignment rows the API hands ``assemble``.

    Same outcomes as :func:`_seed_two_chapters`, without a database.
    """
    grades = {
        (MO, "cw-x-1"): ("TURNED_IN", 4), (MO, "cw-x-2"): ("TURNED_IN", 6),
        (MO, "cw-x-3"): ("MISSING", None), (MO, "cw-x-4"): ("NOT_STARTED", None),
        (MO, "cw-x-5"): ("TURNED_IN", 7),
        (SARA, "cw-x-1"): ("MISSING", None), (SARA, "cw-x-2"): ("TURNED_IN", 2),
        (SARA, "cw-x-3"): ("TURNED_IN", 8), (SARA, "cw-x-4"): ("TURNED_IN", 9),
        (SARA, "cw-x-5"): ("TURNED_IN", 7),
    }
    rows: list[dict] = []
    for sid, name in ((MO, "Mohamed Ahmed Hassan"), (SARA, "Sara Mohamed")):
        for cid, _title in TITLES:
            status, earned = grades[(sid, cid)]
            rows.append({
                "student_id": sid, "display_name": name,
                "class_label": CLASS_10A, "coursework_id": cid,
                "status": status,
                "mc_earned": earned, "mc_available": 10 if earned else None,
                "mc_percentage": earned * 10.0 if earned else None,
            })
    return rows


# --------------------------------------------------- identifier derivation


class TestIdentifierDerivation:
    """The code IS the chapter-and-lesson identifier (§21)."""

    def test_ch1l1_and_ch2l1_have_different_codes(self):
        first = parse_assignment("CH1L1 - Algebra")
        second = parse_assignment("CH2L1 - Trigonometry")
        assert first.code == "CH1L1"
        assert second.code == "CH2L1"
        assert first.code != second.code
        assert (first.chapter, first.lesson) == ("1", "1")
        assert (second.chapter, second.lesson) == ("2", "1")

    def test_ch1l2_and_ch2l2_have_different_codes(self):
        assert parse_assignment("CH1L2 - Linear").code == "CH1L2"
        assert parse_assignment("CH2L2 - Sine Rule").code == "CH2L2"

    def test_chapter_is_kept_from_the_long_form_title(self):
        code = parse_assignment("Chapter 2 Lesson 1 - Geometry")
        assert code.code == "CH2L1"
        assert code.detail == "Chapter 2, Lesson 1 - Geometry"

    def test_the_identifier_is_the_lesson_key(self):
        code = parse_assignment("CH1L1 - Algebra")
        assert code.key == "CH1L1"
        assert code.row_key == code.code  # no coursework id was given

    def test_existing_identifiers_still_read_the_same(self):
        # Well-formed titles keep their own numbers, and the fallbacks other
        # report shapes already relied on are unchanged.
        assert parse_assignment("Ch1L4 - Percentages").code == "CH1L4"
        assert parse_assignment("Quiz 2 and Answers").code == "Q2"
        assert parse_assignment("P1 - Worksheet").code == "P1"
        # A chapter with no lesson marker stays a chapter: no lesson is
        # invented for it, so the title itself remains the label.
        assert parse_assignment("Chapter 5").code == "Chapter 5"
        assert parse_assignment("Chapter 5").lesson == ""

    def test_lesson_without_a_chapter_is_not_given_one(self):
        code = parse_assignment("Lesson 4")
        assert code.code == "L4"
        assert code.chapter == ""
        assert code.detail == "Lesson 4"

    def test_unrecognised_identifier_never_invents_a_chapter(self):
        # Looks like an identifier, but its chapter cannot be trusted.
        broken = parse_assignment("CHXL2 - Something")
        assert broken.chapter == ""
        assert broken.lesson == ""
        assert broken.code == "CHXL2 - Something"
        # A plain title is not a chapter either.
        plain = parse_assignment("Homework 5")
        assert plain.chapter == ""
        assert plain.code == "Homework 5"


# ------------------------------------------------------ sorting / grouping


class TestSortingAndGrouping:
    """Lessons with the same number in different chapters stay separate."""

    def test_sorting_orders_by_chapter_then_lesson(self):
        codes = [parse_assignment(t) for _cid, t in TITLES]
        codes.sort(key=lambda c: c.sort_key)
        assert [c.code for c in codes] == FULL_CODES
        # Sorting must not merge two lessons that share a lesson number.
        assert len({c.code for c in codes}) == len(codes)

    def test_grade_matrix_pairs_each_mark_with_its_own_assignment(self):
        data = assemble(_course_model(), _report_rows(),
                        [cid for cid, _ in TITLES])
        assert [c.code for c in data.codes] == FULL_CODES
        student = data.groups[0].students[0]          # Mo
        by_row = {g["coursework_id"]: g for g in student["grades"]}
        # Same lesson number, two chapters, two different truths.
        assert by_row["cw-x-1"]["code"] == "CH1L1"
        assert by_row["cw-x-1"]["grade"] == "4/10"
        assert by_row["cw-x-3"]["code"] == "CH2L1"
        assert by_row["cw-x-3"]["submitted"] is False
        # Every column resolves to its own assignment, never to a namesake.
        assert len({g["coursework_id"] for g in student["grades"]}) == \
            len(FULL_CODES)


# ------------------------------------------------------- report contents


class TestReportsKeepBothChapters:
    """The rendered report and its preview name every lesson in full."""

    def test_missing_homework_keeps_each_lesson_apart(self, client):
        _seed_two_chapters()
        model = _build(client, report_type="missing")
        by_student = {row["student_id"]: row for g in model["groups"]
                      for row in g["missing"]}
        assert [a["code"] for a in by_student[MO]["assignments"]] == \
            ["CH2L1", "CH2L2"]
        assert [a["code"] for a in by_student[SARA]["assignments"]] == ["CH1L1"]

    def test_missing_counts_are_per_lesson(self, client):
        _seed_two_chapters()
        model = _build(client, report_type="missing")
        per_student = {row["student_id"]: len(row["assignments"])
                       for g in model["groups"] for row in g["missing"]}
        assert per_student == {MO: 2, SARA: 1}
        missing_entries = sum(len(row["assignments"]) for g in model["groups"]
                              for row in g["missing"])
        assert missing_entries == 3
        # The summary counts students with outstanding work, not assignments.
        assert model["summary"]["missing"] == 2
        # ...and the assignment count still counts assignments.
        assert model["summary"]["assignments"] == len(FULL_CODES)

    def test_pdf_shows_the_full_identifier_for_every_chapter(self, client):
        _seed_two_chapters()
        flat = _pdf(client)
        for code in FULL_CODES:
            assert code in flat, f"{code} missing from the report"

    def test_pdf_grade_matrix_pairs_each_column_with_its_own_mark(self, client):
        _seed_two_chapters()
        flat = _pdf(client)
        # The matrix header names every lesson in full...
        assert "STUDENT CH1L1 CH1L2 CH2L1 CH2L2 CH3L1" in flat, flat[:1200]
        # ...and each student's row carries HIS OWN mark in each chapter's
        # column. Mo handed chapter 1's L1 in (4/10) and missed chapter 2's L1;
        # Sara is the mirror image (NS under CH1L1, 8/10 under CH2L1). Under
        # the old lesson-only code both L1 columns resolved to the same cell,
        # so one of these two rows could not have been right.
        assert "Mo 4/10 6/10 NS NS 7/10" in flat, flat[:2000]
        assert "Sara NS 2/10 8/10 9/10 7/10" in flat, flat[:2000]

    def test_options_and_report_agree_on_the_identifier(self, client):
        _seed_two_chapters()
        options = client.get("/api/reports/options").json()["assignments"]
        by_id = {a["id"]: a for a in options}
        assert by_id["cw-x-1"]["code"] == "CH1L1"
        assert by_id["cw-x-3"]["code"] == "CH2L1"
        assert by_id["cw-x-3"]["chapter"] == "2"
        assert by_id["cw-x-3"]["lesson"] == "1"
        assert by_id["cw-x-5"]["code"] == "CH3L1"

    def test_preview_lists_every_lesson_exactly_once(self, client):
        _seed_two_chapters()
        model = _build(client)
        codes = [a["code"] for a in model["assignments"]]
        assert codes == FULL_CODES
        assert len(set(codes)) == len(codes)

    def test_selecting_one_lesson_does_not_drag_its_namesake_in(self, client):
        _seed_two_chapters()
        model = _build(client, assignments="cw-x-3")
        assert [a["code"] for a in model["assignments"]] == ["CH2L1"]
        flat = _pdf(client, assignments="cw-x-3")
        assert "CH2L1" in flat
        assert "CH1L1" not in flat

    def test_preview_model_is_a_pure_function_of_the_identifiers(self):
        data = assemble(_course_model(), _report_rows(),
                        [cid for cid, _ in TITLES])
        model = preview_model(data, report_type="missing")
        codes = [a["code"] for a in model["assignments"]]
        assert codes == FULL_CODES
        assert model["summary"]["assignments"] == len(FULL_CODES)
        # The identifier the JSON exposes is the one the title carries.
        assert all(a["key"] == a["code"] for a in model["assignments"])
        assert all(a["chapter"] and a["lesson"] for a in model["assignments"])
