"""The unified report workspace (§17-§28, §45-§49, §57-§58).

The preview and the export are the SAME model: ``/api/reports/build`` and
``/api/reports/build/export`` both call ``assemble_report`` with identical
filters, so the numbers a teacher sees on screen are the numbers printed on
paper. These tests lock the filter semantics - class, assignment, status and
low-grade threshold - because a report that quietly includes the wrong
assignment or the wrong student is worse than no report at all.
"""

from __future__ import annotations

import io
import json
import time

import pytest

from app import db

MO = "gcs-mo-001"
SARA = "gcs-sara-002"
AHMED = "gcs-ahmed-003"
DAN = "gcs-dan-004"

CLASS_10A = "10A"
CLASS_10B = "10B"
CLASS_10C = "10C"

COURSE = "c-ws-1"


def _seed() -> None:
    """One course, four assignments, four students across 10A/10B/10C."""
    now = time.time()
    db.execute(
        "INSERT OR REPLACE INTO courses(id, name, section, fetched_at) "
        "VALUES(?,?,?,?)",
        (COURSE, "Mathematics", "O Level", now))
    titles = [
        ("cw-ws-l1", "Chapter 1 Lesson 1 - Algebra"),
        ("cw-ws-l2", "Chapter 1 Lesson 2 - Geometry"),
        ("cw-ws-l3", "Chapter 2 Lesson 3 - Trigonometry"),
        ("cw-ws-l4", "Chapter 2 Lesson 4 - Percentages"),
    ]
    for cid, title in titles:
        db.execute(
            "INSERT OR REPLACE INTO coursework(id, course_id, title, "
            "max_points, state, fetched_at) VALUES(?,?,?,?,?,?)",
            (cid, COURSE, title, 10.0, "PUBLISHED", now))

    for sid, name, nick, cls in (
        (MO, "Mohamed Ahmed Hassan", "Mo", CLASS_10A),
        (SARA, "Sara Mohamed", "Sara", CLASS_10A),
        (AHMED, "Ahmed Ali", "Ahmed", CLASS_10B),
        (DAN, "Dan Doe", None, CLASS_10C),
    ):
        db.execute(
            "INSERT OR REPLACE INTO students(id, name, email, nickname, "
            "class_name, fetched_at) VALUES(?,?,?,?,?,?)",
            (sid, name, f"{sid}@school.edu", nick, cls, now))

    def sub(sid: str, name: str, cw: str, state: str) -> None:
        db.execute(
            "INSERT OR REPLACE INTO classroom_submissions("
            "submission_id, course_id, coursework_id, student_id, "
            "student_name, student_email, current_state, max_points, "
            "last_synced_at, first_seen_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (f"sub-{sid}-{cw}", COURSE, cw, sid, name, f"{sid}@school.edu",
             state, 10.0, now, now))

    def result(sid: str, cw: str, earned: int | None) -> None:
        if earned is None:
            return
        submission = f"sub-{sid}-{cw}"
        job = f"job-{sid}-{cw}"
        db.execute(
            "INSERT INTO correction_jobs(id, submission_id, coursework_id, "
            "course_id, student_id, student_name, status, created_at, "
            "updated_at) VALUES(?,?,?,?,?,?, 'COMPLETED', ?, ?)",
            (job, submission, None, None, sid, "Student", now, now))
        db.execute(
            "INSERT INTO results(id, job_id, coursework_id, submission_id, "
            "student_id, student_name, earned, available, percentage, "
            "provisional_grade, result_json, verification_json, created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (f"res-{sid}-{cw}", job, None, submission, sid, "Student",
             earned, 10, earned * 10.0, "0", json.dumps({"questions": []}),
             json.dumps({"status": "VALID"}), now))

    # L1: Mo 9, Sara 7, Ahmed 1 (grade 1 = a RESULT), Dan never handed in.
    for sid, name, state in ((MO, "Mohamed Ahmed Hassan", "TURNED_IN"),
                             (SARA, "Sara Mohamed", "TURNED_IN"),
                             (AHMED, "Ahmed Ali", "TURNED_IN"),
                             (DAN, "Dan Doe", "MISSING")):
        sub(sid, name, "cw-ws-l1", state)
    result(MO, "cw-ws-l1", 9)
    result(SARA, "cw-ws-l1", 7)
    result(AHMED, "cw-ws-l1", 1)

    # L2: Mo + Sara turned in, Ahmed opened it and never submitted (DRAFT),
    # Dan did not start.
    sub(MO, "Mohamed Ahmed Hassan", "cw-ws-l2", "TURNED_IN")
    sub(SARA, "Sara Mohamed", "cw-ws-l2", "TURNED_IN")
    sub(AHMED, "Ahmed Ali", "cw-ws-l2", "CREATED")
    sub(DAN, "Dan Doe", "cw-ws-l2", "NOT_STARTED")
    result(MO, "cw-ws-l2", 8)
    result(SARA, "cw-ws-l2", 4)

    # L3: nobody has a row for it yet beyond an explicit missing entry.
    sub(DAN, "Dan Doe", "cw-ws-l3", "NOT_STARTED")


@pytest.fixture
def client(fresh_state, monkeypatch):
    from app.config import get_config, save_config
    cfg = get_config()
    cfg.mock_mode = True
    cfg.role = "developer"
    cfg.teacher.teacher_name = "Mr. Abanoub Akram"
    cfg.reports.teacher_title = "IGCSE AND A LEVEL MATHEMATICS TEACHER"
    cfg.reports.center_name = "HLC Center — O Level"
    cfg.reports.academic_year = "2026–2027"
    # The tagline is part of the branding the teacher configures, so the
    # fixture sets it explicitly. It must NOT come from a shipped default:
    # the app used to carry one teacher's name/centre/quote/monogram as its
    # defaults, which put that identity on every install's reports.
    cfg.reports.quote = "Everything you do flourish and succeed"
    save_config(cfg)

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
    async def _handler(request, exc: AppError):
        return JSONResponse(status_code=exc.status, content=exc.to_dict())

    _seed()
    with TestClient(app) as test_client:
        yield test_client


def _build(client, **params):
    query = "&".join(f"{k}={v}" for k, v in params.items())
    response = client.get(f"/api/reports/build?{query}")
    assert response.status_code == 200, response.text
    return response.json()


def _pdf_text(client, **params) -> str:
    from pypdf import PdfReader
    query = "&".join(f"{k}={v}" for k, v in params.items())
    response = client.get(f"/api/reports/build/export?{query}",
                          headers={"accept": "application/pdf"})
    assert response.status_code == 200, response.text
    reader = PdfReader(io.BytesIO(response.content))
    text = "\n".join(page.extract_text() or "" for page in reader.pages)
    return " ".join(text.split())


class TestOptions:
    """The workspace control data (§17)."""

    def test_options_lists_classes_assignments_and_branding(self, client):
        data = client.get("/api/reports/options").json()
        labels = {c["label"] for c in data["classes"]}
        assert {CLASS_10A, CLASS_10B, CLASS_10C} <= labels
        assert len(data["assignments"]) == 4
        assert data["threshold_default"] == 2
        assert 2 in data["threshold_options"]
        assert data["branding"]["teacher_name"] == "Mr. Abanoub Akram"
        assert data["branding"]["center"] == "HLC Center — O Level"

    def test_options_decodes_assignment_codes(self, client):
        data = client.get("/api/reports/options").json()
        by_id = {a["id"]: a for a in data["assignments"]}
        assert by_id["cw-ws-l4"]["code"] == "L4"
        assert by_id["cw-ws-l4"]["chapter"] == "2"
        assert by_id["cw-ws-l4"]["lesson"] == "4"


class TestClassFilter:
    """Class: All / 10A / 10B / 10C (§49)."""

    def test_all_classes_shows_every_class(self, client):
        model = _build(client, classes="all")
        assert set(model["class_names"].split(", ")) == {
            CLASS_10A, CLASS_10B, CLASS_10C}

    def test_single_class_shows_only_that_class(self, client):
        model = _build(client, classes=CLASS_10B)
        assert model["class_names"] == CLASS_10B
        labels = [g["class_label"] for g in model["groups"]]
        assert labels == [CLASS_10B]
        names = [s["display_name"] for g in model["groups"]
                 for s in g["students"]]
        assert names == ["Ahmed"]

    def test_pdf_respects_the_class_filter(self, client):
        flat = _pdf_text(client, classes=CLASS_10A, assignments="cw-ws-l1")
        assert "Sara" in flat
        assert "Dan Doe" not in flat


class TestAssignmentFilter:
    """One assignment, several assignments, or all (§18, §49, §57)."""

    def test_single_assignment(self, client):
        model = _build(client, assignments="cw-ws-l1")
        assert [a["code"] for a in model["assignments"]] == ["L1"]

    def test_multiple_assignments_keep_their_order_and_codes(self, client):
        model = _build(client, assignments="cw-ws-l1,cw-ws-l4")
        assert sorted(a["code"] for a in model["assignments"]) == ["L1", "L4"]

    def test_all_assignments(self, client):
        model = _build(client, assignments="all")
        assert model["summary"]["assignments"] == 4

    def test_unknown_assignment_is_rejected(self, client):
        response = client.get("/api/reports/build?assignments=nope")
        assert response.status_code == 404
        assert response.json()["code"] == "assignment_not_found"

    def test_pdf_covers_several_assignments_at_once(self, client):
        flat = _pdf_text(client, assignments="cw-ws-l1,cw-ws-l2")
        assert "L1" in flat and "L2" in flat
        assert "L3" not in flat


class TestLowGradeThreshold:
    """§24: the threshold is configurable and never hard-coded."""

    def test_default_threshold_is_two(self, client):
        model = _build(client, assignments="cw-ws-l1")
        assert model["threshold"] == 2
        # Ahmed scored 1/10 -> grade 1 -> below 2. Mo 9/10 -> grade 9.
        assert model["summary"]["low_grade"] == 1
        low_names = {row["display_name"] for g in model["groups"]
                     for row in g["low_grade"]}
        assert low_names == {"Ahmed"}

    def test_threshold_one_excludes_grade_one(self, client):
        model = _build(client, assignments="cw-ws-l1", threshold=1)
        assert model["threshold"] == 1
        assert model["summary"]["low_grade"] == 0

    def test_threshold_three_includes_more(self, client):
        # Sara scored 4/10 -> 40% -> grade 4. Below 3? No. Below 5? Yes.
        model = _build(client, assignments="cw-ws-l2", threshold=5)
        assert model["summary"]["low_grade"] >= 1

    def test_threshold_off_disables_the_section(self, client):
        model = _build(client, assignments="cw-ws-l1", threshold="off")
        assert model["threshold"] is None
        assert model["summary"]["low_grade"] == 0
        assert "low_grade" not in model["sections"]

    def test_missing_work_is_never_a_low_grade(self, client):
        # Every handed-in grade is below 9, but Dan never handed L1 in, so
        # he must land in MISSING, never in the low-grade section (§9, §25).
        model = _build(client, assignments="cw-ws-l1", threshold=9)
        low_names = {row["display_name"] for g in model["groups"]
                     for row in g["low_grade"]}
        assert "Dan Doe" not in low_names
        assert model["summary"]["missing"] == 1
        # Mo scored 9/10 -> grade 9, which is NOT below 9.
        assert low_names == {"Sara", "Ahmed"}


class TestDraftVsMissing:
    """§31-§33: a draft IS outstanding, a low grade of 2+ is NOT."""

    def test_draft_is_counted_separately(self, client):
        model = _build(client, assignments="cw-ws-l2")
        assert model["summary"]["draft"] == 1
        draft_names = [row["display_name"] for g in model["groups"]
                       for row in g["draft"]]
        assert draft_names == ["Ahmed"]

    def test_draft_student_is_in_the_outstanding_section_labelled_draft(self, client):
        # §31: DRAFT -> OUTSTANDING, but §32: the original state survives.
        model = _build(client, assignments="cw-ws-l2")
        missing_names = [row["display_name"] for g in model["groups"]
                         for row in g["missing"]]
        assert "Ahmed" in missing_names
        assert "Dan Doe" in missing_names
        ahmed = [row for g in model["groups"] for row in g["missing"]
                 if row["display_name"] == "Ahmed"][0]
        entry = ahmed["assignments"][0]
        assert entry["tracking"] == "OUTSTANDING"
        assert entry["status"] == "CREATED"
        assert entry["status_label"] == "Draft"

    def test_missing_uses_the_classroom_status(self, client):
        model = _build(client, assignments="cw-ws-l1")
        dan = [row for g in model["groups"] for row in g["missing"]
               if row["display_name"] == "Dan Doe"]
        assert dan and dan[0]["assignments"][0]["status"] == "MISSING"
        assert dan[0]["assignments"][0]["status_label"] == "Missing"

    def test_missing_is_never_rendered_as_zero(self, client):
        flat = _pdf_text(client, assignments="cw-ws-l1", classes=CLASS_10C)
        # The matrix abbreviates (NS) and the status table spells it out.
        assert "NS" in flat
        assert "not submitted" in flat.lower()
        assert "0/10" not in flat


class TestStatusFilters:
    """§17/§57: Missing only, Draft only, Missing + low-grade, all."""

    def test_missing_only_keeps_only_the_missing_section(self, client):
        model = _build(client, statuses="missing")
        assert set(model["sections"]) == {"summary", "assignments", "missing",
                                          "signoff"}

    def test_draft_only_keeps_only_the_draft_section(self, client):
        model = _build(client, statuses="draft")
        assert "draft" in model["sections"]
        assert "missing" not in model["sections"]

    def test_missing_and_low_grade_together(self, client):
        model = _build(client, statuses="missing,low_grade")
        assert set(model["sections"]) == {"summary", "assignments", "missing",
                                          "low_grade", "signoff"}

    def test_no_status_filter_uses_the_report_type(self, client):
        model = _build(client, report_type="class")
        assert "status" in model["sections"] and "grades" in model["sections"]

    def test_preview_and_pdf_agree_on_sections(self, client):
        model = _build(client, report_type="missing",
                       statuses="missing,draft")
        flat = _pdf_text(client, report_type="missing",
                         statuses="missing,draft")
        for phrase, section in (("OUTSTANDING / MISSING HOMEWORK", "missing"),
                                ("DRAFT / INCOMPLETE SUBMISSIONS", "draft")):
            assert (section in model["sections"]) == (phrase in flat)
        assert "LOW-GRADE STUDENTS" not in flat
        assert "low_grade" not in model["sections"]


class TestReportTypes:
    """§48: five report types, one shared pipeline."""

    @pytest.mark.parametrize("rtype,title", [
        ("class", "CLASS REPORT"),
        ("missing", "MISSING HOMEWORK REPORT"),
        ("assignment", "ASSIGNMENT REPORT"),
        ("performance", "PERFORMANCE / GRADE REPORT"),
    ])
    def test_title_matches_the_report_type(self, client, rtype, title):
        model = _build(client, report_type=rtype)
        assert model["title"] == title
        flat = _pdf_text(client, report_type=rtype)
        assert title in flat

    def test_unknown_type_is_rejected(self, client):
        response = client.get("/api/reports/build?report_type=nonsense")
        assert response.status_code == 400
        assert response.json()["code"] == "bad_report_type"


class TestBrandingAndLayout:
    """§29-§43: permanent branding, centred mark, tagline, A4 portrait."""

    def test_report_carries_the_permanent_branding(self, client):
        flat = _pdf_text(client, report_type="missing")
        assert "Mr. Abanoub Akram" in flat
        assert "IGCSE AND A LEVEL MATHEMATICS TEACHER" in flat
        assert "HLC Center" in flat
        assert "2026" in flat

    def test_masthead_has_the_mark_and_the_tagline(self, client):
        flat = _pdf_text(client, report_type="missing")
        assert "Everything you do flourish and succeed" in flat

    def test_pdf_is_a4_portrait(self, client):
        from pypdf import PdfReader
        from app.api import report_routes
        data = report_routes.assemble_report(report_type="missing")
        from app.reporting import design as D
        from app.reporting.class_report import render_pdf
        buf = render_pdf(data, D.load_branding(),
                         title="MISSING HOMEWORK REPORT",
                         report_type="missing")
        page = PdfReader(buf).pages[0]
        width = float(page.mediabox.width)
        height = float(page.mediabox.height)
        assert width < height, "formal reports must be A4 portrait (§43)"

    def test_preview_model_matches_the_exported_sections(self, client):
        model = _build(client, report_type="class", classes=CLASS_10A)
        flat = _pdf_text(client, report_type="class", classes=CLASS_10A)
        assert "GRADE OVERVIEW" in flat
        assert "STUDENT-BY-STUDENT STATUS" in flat
        assert "VERIFICATION / SIGN-OFF" in flat
        assert model["summary"]["students"] >= 1
        assert "Sara" in flat


class TestMissingOnlyMode:
    """The consolidated "Missing only" layout must not repeat a student.

    The complaint was concrete: the exported report said
    ``Student A - Missing: L1, L2, L3`` and then printed Student A again for
    every one of those assignments. The teacher asked for the first line and
    nothing after it, with the per-assignment breakdown kept as a separate,
    explicitly chosen mode.
    """

    def test_default_is_the_consolidated_student_list(self, client):
        model = _build(client, report_type="missing", statuses="missing")
        assert model["mode"] == "summary"
        assert model["detailed"] is False
        assert set(model["sections"]) == {"summary", "assignments",
                                          "missing", "signoff"}

    def test_missing_only_names_each_student_once(self, client):
        model = _build(client, report_type="missing", statuses="missing")
        names = [row["display_name"] for g in model["groups"]
                 for row in g["missing"]]
        assert len(names) == len(set(names))
        assert "Dan Doe" in names

    def test_missing_only_pdf_has_no_per_assignment_detail_table(self, client):
        flat = _pdf_text(client, report_type="missing", statuses="missing")
        assert "OUTSTANDING / MISSING HOMEWORK" in flat
        # The detail table's only column set - repeated students per
        # assignment - must not be printed in this mode.
        assert "LESSON STATUS" not in flat
        assert "Ahmed Ali" not in flat  # a scored 1 is not missing work

    def test_detailed_mode_keeps_the_per_assignment_breakdown(self, client):
        flat = _pdf_text(client, report_type="missing", statuses="missing",
                         mode="detailed")
        assert "LESSON STATUS" in flat

    def test_missing_only_respects_the_assignment_selection(self, client):
        model = _build(client, report_type="missing", statuses="missing",
                       assignments="cw-ws-l1")
        codes = {a["code"] for g in model["groups"]
                 for row in g["missing"] for a in row["assignments"]}
        assert codes == {"L1"}

    def test_missing_only_groups_by_class(self, client):
        model = _build(client, report_type="missing", statuses="missing")
        labels = [g["class_label"] for g in model["groups"] if g["missing"]]
        assert labels == sorted(labels, key=str)


class TestOutstandingAndGradeOne:
    """§24-§26, §31-§33: one outstanding section, one GRADE 1 section."""

    def test_grade_one_section_lists_the_actual_student(self, client):
        # Grade 1 is a SCORED result, not missing work, so it appears only in
        # the detailed layout - the consolidated one is missing-only by name.
        model = _build(client, report_type="missing", mode="detailed")
        assert "grade_one" in model["sections"]
        names = [row["display_name"] for g in model["groups"]
                 for row in g["grade_one"]]
        assert names == ["Ahmed"]
        flat = _pdf_text(client, report_type="missing", mode="detailed")
        assert "GRADE 1" in flat
        assert "Ahmed" in flat

    def test_grade_one_student_is_not_in_the_outstanding_section(self, client):
        # §33: a scored 1 belongs to GRADE 1, never to missing.
        model = _build(client, assignments="cw-ws-l1")
        missing_names = [row["display_name"] for g in model["groups"]
                         for row in g["missing"]]
        assert "Ahmed" not in missing_names
        assert "Dan Doe" in missing_names
        grade_one = [row["display_name"] for g in model["groups"]
                     for row in g["grade_one"]]
        assert grade_one == ["Ahmed"]

    def test_outstanding_section_title_is_in_the_pdf(self, client):
        flat = _pdf_text(client, assignments="cw-ws-l2")
        assert "OUTSTANDING / MISSING HOMEWORK" in flat

    def test_summary_exposes_the_outstanding_count(self, client):
        model = _build(client, assignments="cw-ws-l2")
        # Ahmed (draft) + Dan (not started) are BOTH outstanding (§31).
        assert model["summary"]["outstanding"] == 2
        assert model["summary"]["missing"] == 2

    def test_classroom_note_lists_students_without_a_record(self, client):
        # §19/§31: an administrative note above the tables, never a table row.
        db.execute(
            "INSERT OR REPLACE INTO students(id, name, email, nickname, "
            "class_name, fetched_at) VALUES(?,?,?,?,?,?)",
            ("gcs-ghost-009", "Ghost Girl", "ghost@school.edu", None,
             CLASS_10B, time.time()))
        model = _build(client, classes=CLASS_10B)
        assert model["classroom_note"] == [
            {"student_id": "gcs-ghost-009", "display_name": "Ghost Girl",
             "class_label": CLASS_10B}]
        flat = _pdf_text(client, classes=CLASS_10B)
        assert "Google Classroom note" in flat
        assert "Ghost Girl" in flat


class TestEmptyStates:
    """§59: a class with nothing to report must not render a blank table."""

    def test_no_missing_work_reads_cleanly(self, client):
        # Every L1 student except Dan handed in; restrict to 10A, where both
        # students submitted, so the missing section is genuinely empty.
        flat = _pdf_text(client, assignments="cw-ws-l1", classes=CLASS_10A)
        assert "No outstanding homework for the selected assignments." in flat

    def test_no_drafts_reads_cleanly(self, client):
        flat = _pdf_text(client, assignments="cw-ws-l1")
        assert "No draft submissions for the selected assignments." in flat


class TestPermanentBranding:
    """§42: the letterhead changes only through Settings, never through sync."""

    def test_settings_exposes_and_updates_report_branding(self, client):
        before = client.get("/api/settings").json()["reports"]
        assert before["center_name"]
        assert before["teacher_title"]

        response = client.post("/api/settings",
                               json={"reports": {"quote": "Keep going"}})
        assert response.status_code == 200
        after = response.json()["reports"]
        assert after["quote"] == "Keep going"
        # Every other branding field survives an unrelated edit.
        assert after["teacher_title"] == before["teacher_title"]
        assert after["center_name"] == before["center_name"]
        assert after["academic_year"] == before["academic_year"]

    def test_report_branding_is_the_permanent_identity(self, client):
        branding = client.get("/api/reports/options").json()["branding"]
        assert branding["teacher_name"] == "Mr. Abanoub Akram"
        assert branding["teacher_title"] == (
            "IGCSE AND A LEVEL MATHEMATICS TEACHER")
        assert branding["quote"] == "Everything you do flourish and succeed"
        assert branding["monogram"] == "A"
