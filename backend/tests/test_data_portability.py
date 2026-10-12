"""Data portability tests (§2) — against the real database, not fixtures.

The whole point of export/import is that a teacher's records survive a move to
another computer, so these tests move real rows between two independent
installations (two data directories and two database files) and check that
students, assignment links, marks, grades and question-level feedback all
arrive intact.

Covered:
  * the export carries no credential table or column;
  * a preview counts new / matching / invalid records without writing;
  * a real round trip between two installations preserves identity and links;
  * ``add_only`` never overwrites a local record, ``update_existing`` does, and
    only after a backup was taken;
  * a file that fails mid-import leaves the database untouched;
  * malformed and hostile files are refused.
"""

from __future__ import annotations

import json
import time

import pytest
from fastapi.testclient import TestClient


# ----------------------------------------------------------------- installation

def _point_at(tmp_path, name: str):
    """Re-point the app at a *different* installation (data dir + app.db)."""
    import app.config as config
    import app.db as db

    data_dir = tmp_path / name
    data_dir.mkdir(parents=True, exist_ok=True)
    config.DATA_DIR = data_dir
    config.SECRETS_PATH = data_dir / "secrets.toml"
    config.CONFIG_PATH = data_dir / "config.toml"
    config._config = None
    config._secrets = config.SecretStore(data_dir / "secrets.toml")
    db.DB_PATH = data_dir / "app.db"
    if getattr(db._local, "conn", None) is not None:
        try:
            db._local.conn.close()
        except Exception:
            pass
        db._local.conn = None
    db.init_db()
    return data_dir


def _seed_real_records() -> dict:
    """A course, an assignment, two students and a full correction result."""
    from app import db

    now = time.time()
    db.execute("INSERT INTO courses(id, name, section, fetched_at) "
               "VALUES('course-1','10B Maths','B',?)", (now,))
    db.execute(
        "INSERT INTO coursework(id, course_id, title, max_points, state, "
        "fetched_at, allowlisted, custom_title, sync_state) "
        "VALUES('cw-1','course-1','Algebra Test 3', 12, 'PUBLISHED', ?, 1, "
        "'Algebra Test 3 (Oct)', 'ACTIVE')", (now,))
    db.execute("INSERT INTO students(id, name, email, fetched_at) "
               "VALUES('stu-1','Ada Lovelace','ada@example.org',?)", (now,))
    db.execute("INSERT INTO students(id, name, email, fetched_at) "
               "VALUES('stu-2','Alan Turing','alan@example.org',?)", (now,))
    db.execute(
        "INSERT INTO correction_jobs(id, coursework_id, course_id, student_id, "
        "student_name, status, created_at, updated_at, result_id) "
        "VALUES('job-1','cw-1','course-1','stu-1','Ada Lovelace','COMPLETED',"
        "?,?, 'res-1')", (now, now))
    result_json = {
        "questions": [
            {"number": "1", "earned": 2, "available": 2,
             "feedback": "Correct factorisation."},
            {"number": "2", "earned": 4, "available": 5,
             "feedback": "Slip in the final line."},
            {"number": "3", "earned": 3, "available": 5,
             "feedback": "Method right, arithmetic error."},
        ],
        "total": {"earned": 9, "available": 12},
    }
    db.execute(
        "INSERT INTO results(id, job_id, coursework_id, submission_id, "
        "student_id, student_name, earned, available, percentage, "
        "provisional_grade, marking_model, result_json, created_at, "
        "confidence, review_required) "
        "VALUES('res-1','job-1','cw-1','sub-1','stu-1','Ada Lovelace',9,12,"
        "75.0,'7','gemini-2.5-pro',?,?, 'HIGH', 0)",
        (json.dumps(result_json), now))
    db.execute(
        "INSERT INTO result_reviews(result_id, action, previous_earned, "
        "new_earned, note, created_at) VALUES('res-1','approved',9,9,"
        "'Looks right',?)", (now,))
    db.execute(
        "INSERT INTO report_attachments(result_id, course_id, coursework_id, "
        "submission_id, student_id, report_filename, status, attempts, "
        "updated_at) VALUES('res-1','course-1','cw-1','sub-1','stu-1',"
        "'Ada_Lovelace_report.pdf','ATTACHED',1,?)", (now,))
    db.execute(
        "INSERT INTO grade_boundaries(coursework_id, boundaries_json, "
        "session_label, updated_at) VALUES('cw-1',?, 'June 2026', ?)",
        (json.dumps([["9", 90], ["8", 80], ["7", 70]]), now))
    return {"coursework": "cw-1", "student": "stu-1", "result": "res-1"}


@pytest.fixture
def client(fresh_state):
    from app.main import app
    with TestClient(app) as c:
        yield c


# -------------------------------------------------------------------- exporting

def test_export_carries_no_secret_table_or_column(client):
    _seed_real_records()
    response = client.get("/api/data/export")
    assert response.status_code == 200
    assert "attachment" in response.headers["content-disposition"]
    package = response.json()

    assert package["format"] == "mathchecker.transfer"
    assert package["contains_secrets"] is False
    assert isinstance(package["exported_at"], float)

    # No credential-bearing table appears anywhere in the file.
    for table in ("gemini_keys", "key_stats", "app_meta", "net_usage",
                  "classroom_submissions", "device_credentials"):
        assert table not in package["data"]
    text = json.dumps(package)
    for needle in ("AIza", "refresh_token", "password", "api_key",
                   "device_credential"):
        assert needle not in text
    # The exclusion list is part of the file, not just of the docs.
    assert "gemini_keys" in package["excluded"]["tables"]


def test_export_is_a_faithful_copy_of_the_database(client):
    from app import db
    _seed_real_records()
    package = client.get("/api/data/export").json()

    assert package["summary"]["students"] == 2
    assert package["summary"]["assignments"] == 1
    assert package["summary"]["results"] == 1

    row = [r for r in package["data"]["results"] if r["id"] == "res-1"][0]
    assert row["earned"] == 9 and row["available"] == 12
    assert row["percentage"] == 75.0
    assert row["provisional_grade"] == "7"
    # question-level marks and feedback survive verbatim
    questions = json.loads(row["result_json"])["questions"]
    assert [q["number"] for q in questions] == ["1", "2", "3"]
    assert questions[1]["feedback"] == "Slip in the final line."
    assert row["created_at"] == db.query_one(
        "SELECT created_at FROM results WHERE id='res-1'")["created_at"]

    # relationships, not just rows: the assignment points at its class
    cw = package["data"]["coursework"][0]
    assert cw["course_id"] == "course-1"
    assert cw["max_points"] == 12


# ---------------------------------------------------------------------- preview

def test_preview_counts_new_records_and_writes_nothing(client):
    from app import db
    _seed_real_records()
    package = client.get("/api/data/export").json()

    before = db.query_one("SELECT COUNT(*) AS n FROM students")["n"]
    response = client.post(
        "/api/data/import/preview",
        files={"file": ("export.json", json.dumps(package).encode(),
                        "application/json")})
    assert response.status_code == 200, response.text
    preview = response.json()
    assert preview["totals"]["new"] == 0        # everything already exists here
    assert preview["totals"]["matching"] >= 5
    assert preview["totals"]["invalid"] == 0
    labels = {s["table"] for s in preview["sections"]}
    assert {"students", "coursework", "results"} <= labels
    # read-only: the count is unchanged
    assert db.query_one("SELECT COUNT(*) AS n FROM students")["n"] == before


def test_preview_reports_invalid_rows_without_importing_them(client):
    package = client.get("/api/data/export").json()
    package["data"]["students"] = [
        {"id": "stu-9", "name": "Valid Student", "email": None, "nickname": None},
        {"id": "", "name": "No Id", "email": None, "nickname": None},
    ]
    preview = client.post(
        "/api/data/import/preview",
        files={"file": ("export.json", json.dumps(package).encode(),
                        "application/json")}).json()
    assert preview["totals"]["new"] == 1
    assert preview["totals"]["invalid"] == 1
    assert preview["invalid_rows"][0]["reason"].startswith("missing id")


# ------------------------------------------------------ the real cross-install move

def test_round_trip_between_two_installations(client, tmp_path):
    """Export from installation A, import into a separate installation B.

    This is the actual migration the feature exists for: B starts empty, so
    every record is new, and the imported result must still be linked to the
    same student and assignment with the same marks and grade.
    """
    from app import db
    _seed_real_records()
    package = client.get("/api/data/export").content

    # ---- installation B: a different data directory and database file
    _point_at(tmp_path, "install-b")
    assert db.query_one("SELECT COUNT(*) AS n FROM students")["n"] == 0

    from app.main import app
    with TestClient(app) as b:
        applied = b.post(
            "/api/data/import",
            files={"file": ("export.json", package, "application/json")},
            data={"mode": "add_only"})
        assert applied.status_code == 200, applied.text
        report = applied.json()
        assert report["applied"] is True
        assert report["totals"]["added"] >= 6
        assert report["totals"]["invalid"] == 0

        # the imported records are readable through the ordinary API
        results = b.get("/api/results").json()
        rows = results["results"] if isinstance(results, dict) else results
        imported = [r for r in rows if r["id"] == "res-1"]
        assert imported, results
        assert imported[0]["earned"] == 9
        assert imported[0]["available"] == 12
        assert imported[0]["student_name"] == "Ada Lovelace"

        detail = b.get("/api/results/res-1").json()
        assert detail["earned"] == 9 and detail["available"] == 12
        assert detail["provisional_grade"] == "7"

        students = b.get("/api/students").json()
        rows = students["students"] if isinstance(students, dict) else students
        names = {s.get("display_name") or s.get("name") for s in rows}
        assert {"Ada Lovelace", "Alan Turing"} <= names, rows

    # foreign keys resolved: the result's assignment exists in B
    assert db.query_one("SELECT title FROM coursework WHERE id='cw-1'")["title"] \
        == "Algebra Test 3"
    assert db.query_one("SELECT COUNT(*) AS n FROM report_attachments")["n"] == 1
    assert db.query_one("SELECT COUNT(*) AS n FROM result_reviews")["n"] == 1


def test_add_only_never_overwrites_and_update_existing_only_when_asked(
        client, tmp_path):
    from app import db
    _seed_real_records()
    package = client.get("/api/data/export").json()

    # A local correction the teacher has since changed.
    db.execute("UPDATE results SET earned=11, provisional_grade='9' "
               "WHERE id='res-1'")

    preview = client.post(
        "/api/data/import/preview",
        files={"file": ("e.json", json.dumps(package).encode(),
                        "application/json")}).json()
    result_section = [s for s in preview["sections"] if s["table"] == "results"][0]
    assert result_section["matching"] == 1 and result_section["new"] == 0

    # default policy: refused-to-overwrite is reported, not silently applied
    applied = client.post(
        "/api/data/import",
        files={"file": ("e.json", json.dumps(package).encode(),
                        "application/json")},
        data={"mode": "add_only"}).json()
    assert applied["totals"]["added"] == 0
    assert applied["totals"]["skipped"] >= 6
    assert db.query_one("SELECT earned FROM results WHERE id='res-1'")["earned"] == 11

    # overwriting needs the explicit mode AND the explicit confirmation
    refused = client.post(
        "/api/data/import",
        files={"file": ("e.json", json.dumps(package).encode(),
                        "application/json")},
        data={"mode": "update_existing"})
    assert refused.status_code == 400
    assert refused.json()["code"] == "confirm_required"
    assert db.query_one("SELECT earned FROM results WHERE id='res-1'")["earned"] == 11

    replaced = client.post(
        "/api/data/import",
        files={"file": ("e.json", json.dumps(package).encode(),
                        "application/json")},
        data={"mode": "update_existing", "confirm": "replace"})
    assert replaced.status_code == 200, replaced.text
    body = replaced.json()
    assert body["totals"]["updated"] >= 6
    assert body["backup_path"], "an overwriting import must back up first"
    from pathlib import Path
    assert Path(body["backup_path"]).exists()
    assert db.query_one("SELECT earned FROM results WHERE id='res-1'")["earned"] == 9


def test_a_failing_import_leaves_the_database_untouched(client, tmp_path):
    """Transactionality: a file that violates a constraint changes nothing."""
    from app import db
    _seed_real_records()
    package = client.get("/api/data/export").json()

    # Two result rows with the same (coursework_id, submission_id) violate the
    # unique index, so the INSERT raises mid-import.
    duplicate = dict(package["data"]["results"][0])
    duplicate["id"] = "res-2"
    duplicate["job_id"] = "job-1"
    package["data"]["results"] = [package["data"]["results"][0], duplicate]
    package["data"]["students"] = [
        {"id": "stu-new", "name": "Brand New", "email": None, "nickname": None}]

    before = db.query_one("SELECT COUNT(*) AS n FROM results")["n"]
    response = client.post(
        "/api/data/import",
        files={"file": ("e.json", json.dumps(package).encode(),
                        "application/json")},
        data={"mode": "add_only"})
    assert response.status_code >= 400
    assert response.json()["code"] == "import_conflict"
    # the students insert that preceded the failing row was rolled back too
    assert db.query_one("SELECT COUNT(*) AS n FROM results")["n"] == before
    assert db.query_one(
        "SELECT COUNT(*) AS n FROM students WHERE id='stu-new'")["n"] == 0
    assert db.query_one(
        "SELECT COUNT(*) AS n FROM results WHERE id='res-2'")["n"] == 0


# ------------------------------------------------------------ rejecting bad files

@pytest.mark.parametrize("payload,code", [
    (b"not json at all", "bad_export_file"),
    (b"[]", "bad_export_file"),
    (json.dumps({"format": "something-else", "format_version": 1,
                 "data": {}}).encode(), "bad_export_format"),
    (json.dumps({"format": "mathchecker.transfer", "format_version": 99,
                 "data": {}}).encode(), "export_too_new"),
    (json.dumps({"format": "mathchecker.transfer", "format_version": 1,
                 "data": []}).encode(), "bad_export_file"),
])
def test_bad_files_are_refused_with_a_reason(client, payload, code):
    response = client.post(
        "/api/data/import/preview",
        files={"file": ("bad.json", payload, "application/json")})
    assert response.status_code == 400
    assert response.json()["code"] == code
    assert response.json()["message"]


def test_a_file_carrying_a_credential_field_is_refused_entirely(client):
    from app import db
    package = client.get("/api/data/export").json()
    package["data"]["students"] = [
        {"id": "stu-x", "name": "Smuggler", "password": "hunter2"}]
    response = client.post(
        "/api/data/import",
        files={"file": ("e.json", json.dumps(package).encode(),
                        "application/json")},
        data={"mode": "add_only"})
    assert response.status_code == 400
    assert response.json()["code"] == "forbidden_column"
    assert db.query_one("SELECT * FROM students WHERE id='stu-x'") is None


def test_import_never_executes_code_from_the_file(client):
    """A payload that looks like a callable is stored as text, not run."""
    payload = {
        "format": "mathchecker.transfer", "format_version": 1,
        "content": "student-results", "contains_secrets": False,
        "data": {"students": [{"id": "stu-exec", "name": "__import__('os')",
                               "email": None, "nickname": "__import__('os')"}]},
    }
    applied = client.post(
        "/api/data/import",
        files={"file": ("e.json", json.dumps(payload).encode(),
                        "application/json")},
        data={"mode": "add_only"})
    assert applied.status_code == 200, applied.text
    from app import db
    row = db.query_one("SELECT name FROM students WHERE id='stu-exec'")
    assert row["name"] == "__import__('os')"      # stored verbatim as a string


def test_a_duplicate_name_is_flagged_but_kept_as_two_students(client):
    payload = {
        "format": "mathchecker.transfer", "format_version": 1,
        "contains_secrets": False,
        "data": {"students": [
            {"id": "stu-a", "name": "Ada Lovelace", "email": None},
            {"id": "stu-b", "name": "Ada Lovelace", "email": None}]},
    }
    preview = client.post(
        "/api/data/import/preview",
        files={"file": ("e.json", json.dumps(payload).encode(),
                        "application/json")}).json()
    assert any("share the name" in w for w in preview["warnings"])
    applied = client.post(
        "/api/data/import",
        files={"file": ("e.json", json.dumps(payload).encode(),
                        "application/json")},
        data={"mode": "add_only"}).json()
    assert applied["totals"]["added"] == 2
    from app import db
    assert db.query_one(
        "SELECT COUNT(*) AS n FROM students WHERE name='Ada Lovelace'")["n"] == 2


# ------------------------------------------------------------------- inventory

def test_inventory_reports_the_real_counts_and_exclusions(client):
    _seed_real_records()
    body = client.get("/api/data/inventory").json()
    assert body["counts"]["students"] == 2
    assert body["counts"]["assignments"] == 1
    assert body["counts"]["results"] == 1
    assert body["counts"]["graded_results"] == 1
    assert body["export"]["format"] == "mathchecker.transfer"
    assert body["export"]["format_version"] == 1
    assert "Students" in body["export"]["sections"]
    assert "gemini_keys" in body["export"]["excluded_tables"]
    assert body["database_bytes"] > 0


def test_export_from_an_older_format_version_is_accepted(client):
    """Compatibility: an older package (version 1, fewer sections) imports."""
    _seed_real_records()
    from app import db
    db.execute("DELETE FROM results")
    db.execute("DELETE FROM correction_jobs")
    old = {
        "format": "mathchecker.transfer", "format_version": 1,
        "content": "student-results", "contains_secrets": False,
        "app_version": "5.0.0", "database_schema": 1,
        "data": {"students": [{"id": "stu-old", "name": "Old Student"}]},
    }
    applied = client.post(
        "/api/data/import",
        files={"file": ("old.json", json.dumps(old).encode(),
                        "application/json")},
        data={"mode": "add_only"})
    assert applied.status_code == 200, applied.text
    assert db.query_one("SELECT name FROM students WHERE id='stu-old'")["name"] \
        == "Old Student"
