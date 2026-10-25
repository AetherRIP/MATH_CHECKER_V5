"""Multi-assignment reports + the People page payload.

Two questions the app previously could not answer correctly:

  * "how did this class do across THESE assignments?" — the Reports page could
    only build one assignment at a time; and
  * "who is this student, what is their latest state, and what did Classroom
    give them?" — the People list carried counts but no latest status, no
    classroom grade and no sync time.

The combined mark is asserted as a MARK FRACTION (sum earned / sum available),
not a mean of percentages: 18/20 (90 %) + 40/50 (80 %) is 58/70 = 82.9 %, and
a mean would wrongly print 85 %. Classroom grades are asserted separately from
Math Checker marks, because they are different numbers from different sources.
"""

from __future__ import annotations

import time

import pytest

from app import db
from app.classroom import submissions as cs

ROSTER = {
    "u1": {"user_id": "u1", "name": "Ada Lovelace", "email": None},
    "u2": {"user_id": "u2", "name": "Ben Bray", "email": None},
    "u3": {"user_id": "u3", "name": "Cleo Chan", "email": None},
}


def _sub(sid: str, user: str, state: str = "TURNED_IN", *, late: bool = False) -> dict:
    return {
        "id": sid,
        "userId": user,
        "state": state,
        "late": late,
        "creationTime": "2026-09-01T08:00:00Z",
        "updateTime": "2026-09-01T09:00:00Z",
        "assignmentSubmission": {"attachments": []},
    }


def _result(submission_id: str, student_id: str, coursework_id: str,
            earned: float, available: float) -> None:
    now = time.time()
    job_id = f"job-{submission_id}"
    with db.transaction() as conn:
        conn.execute(
            "INSERT INTO correction_jobs(id, submission_id, coursework_id, "
            "course_id, student_id, student_name, status, created_at, updated_at) "
            "VALUES(?,?,?,?,?,?, 'COMPLETED', ?, ?)",
            (job_id, submission_id, coursework_id, "c1", student_id, "Student",
             now, now))
        conn.execute(
            "INSERT INTO results(id, job_id, coursework_id, submission_id, "
            "student_id, student_name, earned, available, percentage, "
            "result_json, verification_json, created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (f"res-{submission_id}", job_id, coursework_id, submission_id,
             student_id, "Student", earned, available,
             earned / available * 100, '{"questions": []}',
             '{"status": "VALID"}', now))


def seed_two_assignments() -> None:
    """w1 is out of 20, w2 out of 50 — deliberately different maxima."""
    now = time.time()
    db.execute("INSERT INTO courses(id, name, section, fetched_at) "
               "VALUES('c1','Maths 10A','A',?)", (now,))
    db.execute("INSERT INTO coursework(id, course_id, title, max_points, "
               "due_date, allowlisted, fetched_at) VALUES('w1','c1','Algebra HW',"
               "20,'2026-09-10 18:00',1,?)", (now,))
    db.execute("INSERT INTO coursework(id, course_id, title, max_points, "
               "due_date, allowlisted, fetched_at) VALUES('w2','c1','Fractions "
               "Test',50,'2026-09-20 18:00',1,?)", (now,))

    cs.store_sync_result("c1", "w1", ROSTER,
                         [_sub("s1", "u1"), _sub("s2", "u2", "CREATED")])
    cs.store_sync_result("c1", "w2", ROSTER, [_sub("t1", "u1")])

    # u1: 18/20 on w1 (90 %) and 40/50 on w2 (80 %).
    _result("s1", "u1", "w1", 18, 20)
    _result("t1", "u1", "w2", 40, 50)
    # u2: submitted w1 but scored zero — a result, not a missing paper.
    db.execute("UPDATE classroom_submissions SET current_state = 'TURNED_IN' "
               "WHERE submission_id = 's2'")
    _result("s2", "u2", "w1", 0, 20)
    # Classroom's own grade for u1 on w1 (17/20) — a DIFFERENT source.
    db.execute("UPDATE classroom_submissions SET assigned_grade = 17, "
               "max_points = 20 WHERE submission_id = 's1'")
    # Teacher-assigned classes drive the report grouping. The rows must exist
    # before they can carry a class: the roster only seeded classroom names.
    for sid, name, label in (("u1", "Ada Lovelace", "10A"),
                             ("u2", "Ben Bray", "10B"),
                             ("u3", "Cleo Chan", "10B")):
        db.execute("INSERT INTO students(id, name, class_name, fetched_at) "
                   "VALUES(?,?,?,?) ON CONFLICT(id) DO UPDATE SET "
                   "class_name=excluded.class_name",
                   (sid, name, label, now))


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
    from app.api import report_routes, routes_all, student_routes
    from app.classroom import tracking_routes

    app = FastAPI()
    routes_all.register(app)
    tracking_routes.register(app)
    student_routes.register(app)
    report_routes.register(app)

    from fastapi.responses import JSONResponse
    from app.errors import AppError

    @app.exception_handler(AppError)
    async def _handler(request, exc: AppError):
        return JSONResponse(status_code=exc.status, content=exc.to_dict())

    with TestClient(app) as test_client:
        yield test_client


# ---------------------------------------------------------------- combined mark

def test_combined_percentage_is_a_mark_fraction_not_a_mean(client):
    seed_two_assignments()

    data = client.get("/api/reports/assignments?ids=w1,w2").json()
    stats = data["stats"]

    # 18/20 + 40/50 = 58/70 = 82.9 %, never (90 + 80) / 2 = 85 %.
    ada = next(s for s in data["students"] if s["student_id"] == "u1")
    assert ada["earned"] == 58 and ada["available"] == 70
    assert ada["percentage"] == 82.9
    assert ada["percentage"] != 85.0

    # The report-wide total also includes Ben's corrected zero on w1, so it is
    # 58/90 — a mark fraction across every student, not an average of averages.
    assert stats["earned"] == 58
    assert stats["available"] == 90
    assert stats["percentage"] == 64.4


def test_every_selected_assignment_stays_visible(client):
    seed_two_assignments()

    data = client.get("/api/reports/assignments?ids=w1,w2").json()
    assert [a["title"] for a in data["assignments"]] == ["Algebra HW",
                                                         "Fractions Test"]
    assert all(a["stats"]["total_students"] >= 1 for a in data["assignments"])

    ada = next(s for s in data["students"] if s["student_id"] == "u1")
    cells = {c["coursework_id"]: c for c in ada["by_assignment"]}
    assert cells["w1"]["mc_earned"] == 18 and cells["w1"]["mc_available"] == 20
    assert cells["w1"]["mc_percentage"] == 90.0
    assert cells["w2"]["mc_earned"] == 40 and cells["w2"]["mc_percentage"] == 80.0


def test_repeated_ids_are_not_counted_twice(client):
    seed_two_assignments()

    data = client.get("/api/reports/assignments?ids=w1,w1,w2,w1").json()
    assert data["filters"]["ids"] == ["w1", "w2"]
    assert len(data["assignments"]) == 2

    ada = next(s for s in data["students"] if s["student_id"] == "u1")
    # A duplicate id must not double a student's marks.
    assert ada["earned"] == 58 and ada["available"] == 70
    assert len(ada["by_assignment"]) == 2


def test_selection_is_validated(client):
    seed_two_assignments()

    empty = client.get("/api/reports/assignments?ids=")
    assert empty.status_code == 400
    assert empty.json()["code"] == "no_assignments_selected"

    missing = client.get("/api/reports/assignments?ids=nope")
    assert missing.status_code == 404

    partial = client.get("/api/reports/assignments?ids=w1,nope").json()
    assert partial["filters"]["ignored_ids"] == ["nope"]
    assert [a["id"] for a in partial["assignments"]] == ["w1"]


def test_missing_submission_is_not_a_zero(client):
    seed_two_assignments()

    data = client.get("/api/reports/assignments?ids=w1,w2").json()
    stats = data["stats"]
    # u2 scored 0 on a paper that WAS submitted; u3 never handed work in.
    assert stats["zero_marks"] == 1
    assert stats["missing"] >= 2          # u3 on w1 and w2, u2 on w2
    cleo = next(s for s in data["students"] if s["student_id"] == "u3")
    assert cleo["missing"] == 2
    assert cleo["corrected"] == 0
    assert cleo["percentage"] is None     # no marks, not 0 %


def test_classroom_grade_is_kept_apart_from_the_correction(client):
    seed_two_assignments()

    data = client.get("/api/reports/assignments?ids=w1,w2").json()
    ada = next(s for s in data["students"] if s["student_id"] == "u1")

    # Classroom: 17/20 on w1 only.
    assert ada["classroom_earned"] == 17
    assert ada["classroom_available"] == 20
    assert ada["classroom_percentage"] == 85.0
    # Math Checker: 58/70 across both assignments.
    assert ada["percentage"] == 82.9
    # And the Classroom number never leaks into the MC total.
    assert ada["earned"] != ada["classroom_earned"]

    cell = next(c for c in ada["by_assignment"] if c["coursework_id"] == "w1")
    assert cell["assigned_grade"] == 17
    assert cell["max_points"] == 20
    assert cell["classroom_percentage"] == 85.0


def test_class_filter_narrows_without_dropping_assignments(client):
    seed_two_assignments()

    data = client.get("/api/reports/assignments?ids=w1,w2&class_label=10A").json()
    assert {s["student_id"] for s in data["students"]} == {"u1"}
    assert len(data["assignments"]) == 2          # the selection is untouched
    assert data["filters"]["class_label"] == "10A"

    other = client.get("/api/reports/assignments?ids=w1,w2&class_label=10B").json()
    assert {s["student_id"] for s in other["students"]} == {"u2", "u3"}
    # Cleo never submitted: she must still be listed, with nothing to mark.
    cleo = next(s for s in other["students"] if s["student_id"] == "u3")
    assert cleo["submitted"] == 0 and cleo["missing"] == 2
    assert cleo["earned"] == 0 and cleo["percentage"] is None


def test_student_filter_matches_name_or_id(client):
    seed_two_assignments()

    by_name = client.get("/api/reports/assignments?ids=w1,w2&student=Ada").json()
    assert [s["student_id"] for s in by_name["students"]] == ["u1"]

    by_id = client.get("/api/reports/assignments?ids=w1,w2&student=u2").json()
    assert [s["student_id"] for s in by_id["students"]] == ["u2"]


def test_csv_export_carries_the_same_numbers(client):
    seed_two_assignments()

    resp = client.get("/api/reports/assignments/export?ids=w1,w2&format=csv")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/csv")
    text = resp.text
    assert "COMBINED" in text
    ada_row = next(line for line in text.splitlines()
                   if line.startswith("Ada Lovelace") and "COMBINED" in line)
    fields = ada_row.split(",")
    assert float(fields[9]) == 85.0        # classroom percentage
    assert float(fields[12]) == 82.9       # combined Math Checker percentage
    assert float(fields[10]) == 58 and float(fields[11]) == 70


def test_pdf_export_is_a_real_pdf(client):
    seed_two_assignments()

    resp = client.get("/api/reports/assignments/export?ids=w1,w2&format=pdf")
    assert resp.status_code == 200
    assert resp.content[:5] == b"%PDF-"
    assert len(resp.content) > 5000

    bad = client.get("/api/reports/assignments/export?ids=w1&format=xlsx")
    assert bad.status_code == 400


# ---------------------------------------------------------------- People page

def test_people_payload_carries_latest_state_and_classroom_grade(client):
    seed_two_assignments()
    db.execute("INSERT INTO students(id, name, fetched_at) VALUES('u1',"
               "'Ada Lovelace', ?) ON CONFLICT(id) DO UPDATE SET name="
               "'Ada Lovelace'", (time.time(),))

    data = client.get("/api/students").json()
    assert "last_sync" in data
    # Google returned no e-mail addresses for this roster; say so.
    assert data["source"]["email"] == "not provided by Classroom"

    ada = next(s for s in data["students"] if s["student_id"] == "u1")
    assert ada["role"] == "student"
    assert ada["class_label"] == "10A"
    assert ada["assignments"] == 2
    assert ada["latest_status_label"]          # a real Classroom status
    assert ada["latest_correction"] == "Corrected"
    assert ada["latest_result"]                 # e.g. "40/50 (80.0%)"
    assert ada["latest_classroom_grade"].startswith("17/20 (85.0%")
    assert ada["classroom_courses"]             # the course they are in
    assert ada["last_synced_at"]


def test_people_payload_never_invents_an_email_or_a_grade(client):
    seed_two_assignments()

    data = client.get("/api/students").json()
    ben = next(s for s in data["students"] if s["student_id"] == "u2")
    assert ben["email"] is None
    assert ben["latest_classroom_grade"] is None      # nothing graded by hand
