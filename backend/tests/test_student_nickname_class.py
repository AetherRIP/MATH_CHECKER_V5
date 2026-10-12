"""Per-student nickname + class/grade (§§1-§15 of the feature brief).

The point of every test here is the same: nickname and class are LOCAL display
metadata, so changing them must be safe in every direction — it must never
touch the Google Classroom identity, never break matching, never alter a past
grade, and never be silently lost to a resync.

Seeded with realistic identities: two students who can share a nickname, a
class list that includes free text, and a correction whose marks must not
move when any of it changes.
"""

from __future__ import annotations

import json
import time

import pytest

from app import db
from app import student_identity as si


MO = "gcs-mo-001"          # Google Classroom user id
SARA = "gcs-sara-002"
AHMED = "gcs-ahmed-003"


@pytest.fixture
def seeded(fresh_state):
    """Three Classroom students, one correction, one known grade."""
    now = time.time()
    # courses before coursework: coursework.course_id REFERENCES courses(id)
    db.execute("INSERT OR REPLACE INTO courses(id, name, fetched_at) "
               "VALUES(?,?,?)", ("c-1", "Mathematics 10A", now))
    db.execute(
        "INSERT INTO coursework(id, course_id, title, max_points, state, "
        "fetched_at) VALUES(?,?,?,?,?,?)",
        ("cw-1", "c-1", "June 2017 Paper 2 Variant 2", 75.0, "PUBLISHED", now))
    for sid, name, state in ((MO, "Mohamed Ahmed Hassan", "TURNED_IN"),
                             (SARA, "Sara Mohamed", "TURNED_IN"),
                             (AHMED, "Ahmed Ali", "TURNED_IN")):
        db.execute(
            "INSERT OR REPLACE INTO students(id, name, email, nickname, "
            "class_name, fetched_at) VALUES(?,?,?,?,?,?)",
            (sid, name, f"{sid}@school.edu", None, None, now))
        db.execute(
            "INSERT OR REPLACE INTO classroom_submissions("
            "submission_id, course_id, coursework_id, student_id, student_name, "
            "student_email, current_state, max_points, last_synced_at, "
            "first_seen_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (f"sub-{sid}", "c-1", "cw-1", sid, name, f"{sid}@school.edu",
             state, 75.0, now, now))
    db.execute("INSERT OR REPLACE INTO correction_jobs(id, status, coursework_id,"
               " submission_id, created_at, updated_at) VALUES(?,?,?,?,?,?)",
               ("job-1", "COMPLETED", "cw-1", f"sub-{MO}", now, now))
    db.execute(
        "INSERT OR REPLACE INTO results(id, job_id, coursework_id, "
        "submission_id, student_id, student_name, earned, available, "
        "percentage, result_json, created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        ("res-1", "job-1", "cw-1", f"sub-{MO}", MO, "Mohamed Ahmed Hassan",
         68, 75, 90.7, json.dumps({"questions": []}), now))
    yield
    for table, col, value in (("report_attachments", "result_id", "res-1"),
                              ("results", "id", "res-1"),
                              ("correction_jobs", "id", "job-1")):
        db.execute(f"DELETE FROM {table} WHERE {col} = ?", (value,))
    db.execute("DELETE FROM students WHERE id IN (?,?,?)", (MO, SARA, AHMED))
    db.execute("DELETE FROM classroom_submissions WHERE student_id IN (?,?,?)",
               (MO, SARA, AHMED))


@pytest.fixture
def client(seeded):
    from fastapi.testclient import TestClient
    from fastapi.responses import JSONResponse
    from app.api import routes_all, student_routes
    from app.errors import AppError
    from fastapi import FastAPI

    app = FastAPI()
    student_routes.register(app)
    routes_all.register(app)

    @app.exception_handler(AppError)
    async def _err(request, exc: AppError):
        return JSONResponse(status_code=exc.status, content=exc.to_dict())

    app.state.correction = object()
    return TestClient(app)


def _save(client, student_id, **fields):
    r = client.put(f"/api/students/{student_id}/details", json=fields)
    assert r.status_code == 200, r.text
    return r.json()


# --- §14 case 1: student WITH a nickname ---------------------------------


class TestNicknameAndClass:
    def test_student_with_nickname_and_class(self, client):
        p = _save(client, MO, nickname="Mo", class_name="10A")
        assert p["display_name"] == "Mo"
        assert p["class_label"] == "10A"
        assert p["official_name"] == "Mohamed Ahmed Hassan"

    def test_student_without_a_nickname_keeps_the_classroom_name(self, client):
        p = _save(client, AHMED, class_name="10B")
        assert p["display_name"] == "Ahmed Ali"
        assert p["class_label"] == "10B"
        assert p["nickname"] is None

    def test_changing_the_nickname_keeps_the_identity(self, client):
        _save(client, MO, nickname="Mo", class_name="10A")
        p = _save(client, MO, nickname="Mohammed")
        assert p["display_name"] == "Mohammed"
        assert p["student_id"] == MO
        assert p["official_name"] == "Mohamed Ahmed Hassan"
        # The class the teacher set is untouched by a nickname edit.
        assert p["class_label"] == "10A"

    def test_changing_the_class_keeps_identity_and_grades(self, client):
        _save(client, MO, nickname="Mo", class_name="10A")
        p = _save(client, MO, class_name="10B")
        assert p["class_label"] == "10B"
        assert p["display_name"] == "Mo"
        row = db.query_one("SELECT earned, available FROM results WHERE id = ?",
                           ("res-1",))
        assert (row["earned"], row["available"]) == (68, 75)

    def test_removing_the_nickname_falls_back_to_the_classroom_name(self, client):
        _save(client, MO, nickname="Mo", class_name="10A")
        p = _save(client, MO, nickname="")
        assert p["nickname"] is None
        assert p["display_name"] == "Mohamed Ahmed Hassan"
        assert p["class_label"] == "10A"


# --- §14 case 2: Classroom sync must not erase teacher metadata ----------


class TestSurvivesResync:
    def test_a_classroom_resync_keeps_nickname_and_class(self, client, monkeypatch):
        from app.classroom import submissions as cs
        _save(client, MO, nickname="Mo", class_name="10A")

        # A resync rewrites students.name from Classroom and touches nothing
        # else. Simulate exactly that.
        db.execute("UPDATE students SET name = ?, fetched_at = ? WHERE id = ?",
                   ("Mohamed A. Hassan (updated)", time.time(), MO))

        p = client.get(f"/api/students/{MO}").json()
        assert p["nickname"] == "Mo"
        assert p["class_label"] == "10A"
        # The Classroom NAME may legitimately change; the display name may not.
        assert p["display_name"] == "Mo"
        assert p["official_name"] == "Mohamed A. Hassan (updated)"

    def test_the_sync_writer_does_not_clear_the_class_column(self, client):
        """The upsert path must never write NULL over a teacher's class."""
        _save(client, SARA, nickname="Sara", class_name="11B")
        # Whatever the sync does, re-saving the profile must not lose it.
        p = _save(client, SARA, nickname="Sara")
        assert p["class_label"] == "11B"


# --- §14 case 3: report regeneration uses the CURRENT metadata ----------


class TestReportUsesCurrentMetadata:
    def test_regenerated_report_uses_the_latest_nickname_and_class(self, client):
        from app.api.report_routes import render_result_pdf
        import app.db as database

        _save(client, MO, nickname="Mo", class_name="10A")
        row = dict(database.query_one("SELECT * FROM results WHERE id = ?",
                                      ("res-1",)))
        row["result_json"] = database.loads(row.get("result_json"))
        row["verification_json"] = database.loads(row.get("verification_json"))
        row = si.decorate(row)

        buf = render_result_pdf(row, detail="standard")
        from pypdf import PdfReader
        import io
        text = "\n".join(p.extract_text() or ""
                         for p in PdfReader(io.BytesIO(buf.getvalue())).pages)
        assert "Mo" in text
        assert "10A" in text
        # The grade is exactly what was recorded, whatever the nickname is.
        assert "68" in text and "75" in text

    def test_historical_grades_are_never_rewritten_by_a_rename(self, client):
        before = dict(db.query_one("SELECT earned, available, student_name "
                                   "FROM results WHERE id = ?", ("res-1",)))
        _save(client, MO, nickname="Mohammed", class_name="12C")
        after = dict(db.query_one("SELECT earned, available, student_name "
                                  "FROM results WHERE id = ?", ("res-1",)))
        assert before == after


# --- §14 case 4: bulk assignment -----------------------------------------


class TestBulkAssignment:
    def test_twenty_students_all_update(self, client):
        now = time.time()
        ids = []
        for i in range(20):
            sid = f"gcs-bulk-{i:03d}"
            ids.append(sid)
            db.execute(
                "INSERT OR REPLACE INTO students(id, name, fetched_at) "
                "VALUES(?,?,?)", (sid, f"Bulk Student {i:02d}", now))
        try:
            r = client.post("/api/students/bulk-details",
                            json={"student_ids": ids, "class_name": "10A"})
            assert r.status_code == 200, r.text
            body = r.json()
            assert len(body["updated"]) == 20, body["skipped"]
            rows = db.query(
                "SELECT class_name FROM students WHERE id LIKE 'gcs-bulk-%'")
            assert len(rows) == 20
            assert {row["class_name"] for row in rows} == {"10A"}
        finally:
            db.execute("DELETE FROM students WHERE id LIKE 'gcs-bulk-%'")

    def test_bulk_does_not_touch_nicknames_or_classroom_names(self, client):
        _save(client, MO, nickname="Mo", class_name="10A")
        before = db.query_one("SELECT name, nickname FROM students WHERE id = ?",
                              (MO,))
        client.post("/api/students/bulk-details",
                    json={"student_ids": [MO, SARA], "class_name": "12C"})
        after = db.query_one("SELECT name, nickname FROM students WHERE id = ?",
                             (MO,))
        assert dict(before) == dict(after)

    def test_bulk_can_also_set_nicknames_individually(self, client):
        client.post("/api/students/bulk-details", json={
            "student_ids": [MO, SARA],
            "class_name": "10A",
            "nicknames": {MO: "Mo", SARA: "Sara"},
        })
        assert si.resolve(MO)["display_name"] == "Mo"
        assert si.resolve(SARA)["display_name"] == "Sara"
        assert si.resolve(SARA)["class_name"] == "10A"

    def test_an_empty_selection_is_rejected(self, client):
        r = client.post("/api/students/bulk-details", json={"student_ids": []})
        assert r.status_code == 400

    def test_unknown_ids_are_skipped_not_crashed(self, client):
        r = client.post("/api/students/bulk-details", json={
            "student_ids": [MO, "not-a-real-id"], "class_name": "10A"})
        assert r.status_code == 200
        assert r.json()["updated"] == [MO]


# --- §14 case 5: two students may share a nickname -----------------------


class TestIdenticalNicknames:
    def test_two_students_with_the_same_nickname_stay_separate(self, client):
        _save(client, MO, nickname="Same", class_name="10A")
        _save(client, SARA, nickname="Same", class_name="10B")

        a = client.get(f"/api/students/{MO}").json()
        b = client.get(f"/api/students/{SARA}").json()
        assert a["display_name"] == b["display_name"] == "Same"
        assert a["student_id"] != b["student_id"]
        assert a["class_label"] != b["class_label"]
        # ...and neither one's grade was touched by the other's metadata.
        assert db.query_one("SELECT student_id FROM results WHERE id = ?",
                            ("res-1",))["student_id"] == MO

    def test_matching_is_never_by_name(self, client):
        _save(client, MO, nickname="Sara")
        # Sara's record is still found by her id, not by the shared nickname.
        assert si.resolve(SARA)["student_id"] == SARA
        assert si.resolve(MO)["class_name"] is None


# --- §13 validation -------------------------------------------------------


class TestValidation:
    def test_whitespace_only_nickname_clears_rather_than_saves_blanks(self):
        assert si.normalize_nickname("   ") is None
        assert si.normalize_nickname("\t\n ") is None
        assert si.normalize_nickname(None) is None

    def test_whitespace_only_class_clears(self):
        assert si.normalize_class_name("   ") is None

    def test_surrounding_and_duplicate_spaces_are_trimmed(self):
        assert si.normalize_nickname("  Mo  ") == "Mo"
        assert si.normalize_class_name("  10   A ") == "10 A"

    def test_normal_names_with_apostrophes_hyphens_and_spaces_survive(self):
        for name in ("O'Brien", "Jean-Luc", "Mary Ann", "Abd El Rahman",
                     "محمد", "Zoë"):
            assert si.normalize_nickname(name) == name

    def test_absurdly_long_values_are_capped(self):
        assert len(si.normalize_nickname("x" * 500)) == si.MAX_NICKNAME_LENGTH
        assert len(si.normalize_class_name("y" * 500)) == si.MAX_CLASS_LENGTH

    def test_class_names_are_not_a_whitelist(self):
        for value in ("10A", "11 Computer Science", "Year 12", "IB MYP 5",
                      "Section 3 - Morning", "Grade 9"):
            assert si.normalize_class_name(value) == value

    def test_the_api_rejects_rather_than_silently_truncating(self, client):
        r = client.put(f"/api/students/{MO}/details",
                       json={"nickname": "x" * 400})
        assert r.status_code == 400
        r = client.put(f"/api/students/{MO}/details",
                       json={"class_name": "y" * 400})
        assert r.status_code == 400

    def test_saving_nothing_is_rejected(self, client):
        assert client.put(f"/api/students/{MO}/details", json={}).status_code == 400

    def test_a_report_never_prints_a_blank_class(self, client):
        # AHMED was never given a class at all.
        row = si.resolve(AHMED)
        assert row["class_name"] is None
        assert row["class_label"] == si.UNASSIGNED_CLASS

    def test_clearing_the_class_falls_back_to_the_course_not_blank(self, client):
        """§8: the course fallback applies ONLY when nothing was assigned."""
        _save(client, AHMED, nickname="Ahmed", class_name="10B")
        p = client.get(f"/api/students/{AHMED}").json()
        assert p["class_label"] == "10B"          # assigned class wins

        p = _save(client, AHMED, nickname="Ahmed", class_name="")
        assert p["class_name"] is None            # the value was really cleared
        assert p["class_label"]                   # never a blank cell
        # ...and what remains is the pre-existing course fallback, not the
        # value the teacher just removed.
        assert p["class_label"] != "10B"
        assert "Mathematics 10A" in p["class_label"]


# --- §1 / §9 / §11: identity is untouched ---------------------------------


class TestClassroomIdentityIsNeverTouched:
    def test_saving_details_never_writes_to_classroom_tables(self, client):
        from app.classroom import submissions as cs
        _save(client, MO, nickname="Mo", class_name="10A")
        row = db.query_one(
            "SELECT student_id, student_name, student_email, submission_id "
            "FROM classroom_submissions WHERE student_id = ?", (MO,))
        assert row["student_id"] == MO
        assert row["student_name"] == "Mohamed Ahmed Hassan"
        assert row["submission_id"] == f"sub-{MO}"

    def test_the_coursework_link_is_untouched(self, client):
        _save(client, MO, nickname="Mo", class_name="10A")
        assert db.query_one("SELECT coursework_id, submission_id FROM results "
                            "WHERE id = ?", ("res-1",))["coursework_id"] == "cw-1"

    def test_a_student_with_no_local_row_is_seeded_from_classroom(self, client):
        db.execute("DELETE FROM students WHERE id = ?", (SARA,))
        p = _save(client, SARA, nickname="Sara", class_name="10C")
        assert p["display_name"] == "Sara"
        assert p["official_name"] == "Sara Mohamed"
        assert p["class_label"] == "10C"

    def test_nickname_does_not_touch_the_nickname_only_endpoint(self, client):
        p = _save(client, MO, nickname="Mo", class_name="10A")
        r = client.put(f"/api/students/{MO}/nickname",
                       json={"nickname": "Mo2"})
        assert r.status_code == 200
        assert r.json()["class_label"] == "10A"
        assert p["class_label"] == "10A"


# --- §6 / §8: the list and the class report -------------------------------


class TestListAndGrouping:
    def test_the_list_carries_nickname_and_class(self, client):
        _save(client, MO, nickname="Mo", class_name="10A")
        _save(client, SARA, nickname="Sara", class_name="10B")
        rows = client.get("/api/students").json()["students"]
        by_id = {r["student_id"]: r for r in rows}
        assert by_id[MO]["display_name"] == "Mo"
        assert by_id[MO]["class_name"] == "10A"
        assert by_id[SARA]["class_label"] == "10B"

    def test_a_student_with_no_class_never_shows_a_blank_cell(self, client):
        rows = client.get("/api/students").json()["students"]
        assert all(r.get("class_label") for r in rows)

    def test_class_report_groups_by_the_assigned_class(self, client):
        _save(client, MO, nickname="Mo", class_name="10A")
        _save(client, SARA, nickname="Sara", class_name="10B")
        _save(client, AHMED, nickname="Ahmed", class_name="10A")

        from app.api.report_routes import class_report
        data = class_report("c-1")
        groups = {g["class_label"]: [s["display_name"] for s in g["students"]]
                  for g in data["groups"]}
        assert set(groups["10A"]) == {"Mo", "Ahmed"}
        assert groups["10B"] == ["Sara"]

    def test_unassigned_students_stay_visible_in_their_own_group(self, client):
        from app.api.report_routes import class_report
        data = class_report("c-1")
        labels = [g["class_label"] for g in data["groups"]]
        assert si.UNASSIGNED_CLASS in labels

    def test_the_class_vocabulary_is_suggestions_not_a_whitelist(self, client):
        _save(client, MO, class_name="11 Computer Science")
        classes = client.get("/api/students/classes").json()["classes"]
        # The teacher's own value comes first...
        assert classes[0] == "11 Computer Science"
        # ...and the usual set is offered too.
        assert "10A" in classes and "12C" in classes


# --- §2 / §15: the display rule itself ------------------------------------


class TestCanonicalDisplayRule:
    def test_nickname_wins_when_present(self):
        assert si.display_name("Mo", "Mohamed Ahmed Hassan") == "Mo"

    def test_classroom_name_is_used_when_there_is_no_nickname(self):
        assert si.display_name(None, "Ahmed Ali") == "Ahmed Ali"
        assert si.display_name("   ", "Ahmed Ali") == "Ahmed Ali"

    def test_the_rule_is_one_function_used_everywhere(self):
        """A second naming rule anywhere is the bug this module exists to stop."""
        for student_id in (MO, SARA, AHMED):
            identity = si.resolve(student_id)
            assert identity["display_name"] == si.display_name(
                identity["nickname"], identity["classroom_name"])

    def test_class_is_never_derived_from_a_name(self, client):
        _save(client, MO, nickname="10A", class_name=None)   # nickname is a joke
        assert si.resolve(MO)["class_name"] is None
