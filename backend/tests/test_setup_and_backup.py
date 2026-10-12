"""First-run setup, self-check, backup/restore and version tests (§3/§31/§35/§44)."""

import json

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(fresh_state, monkeypatch):
    from app.config import get_config, save_config
    cfg = get_config()
    cfg.mock_mode = True
    save_config(cfg)
    from app.main import app
    with TestClient(app) as test_client:
        yield test_client


class TestVersion:
    def test_single_source_of_truth(self):
        import app
        from app.version import APP_VERSION, SCHEMA_VERSION
        assert app.__version__ == APP_VERSION
        assert isinstance(SCHEMA_VERSION, int)

    def test_schema_version_recorded(self, fresh_state):
        import app.db as db
        from app.version import SCHEMA_VERSION
        assert db.get_meta("schema_version") == str(SCHEMA_VERSION)


class TestFirstRun:
    def test_status_is_first_run_initially(self, client):
        data = client.get("/api/setup/status").json()
        # The wizard still runs on a fresh install...
        assert data["first_run"] is True
        # ...and the teacher profile is genuinely EMPTY: a shipped build must
        # never carry somebody else's name/letterhead, so first-run detection
        # asks the teacher for their own details (§14, §42).
        assert data["checks"]["teacher_profile"] is False
        assert data["teacher"]["teacher_name"] == ""
        assert data["checks"]["storage_writable"] is True

    def test_complete_setup_persists_and_clears_first_run(self, client):
        response = client.post("/api/setup/complete", json={
            "teacher_name": "Ms Smith",
            "school_name": "Example School",
            "network": {"access_mode": "local"},
            "grades": {"boundaries": [["1", 0], ["5", 50], ["9", 90]],
                       "session_label": "June 2026"},
        })
        assert response.status_code == 200
        data = response.json()
        assert data["first_run"] is False
        assert data["teacher"]["teacher_name"] == "Ms Smith"
        assert data["checks"]["teacher_profile"] is True

        # Survives a reload of the config from disk.
        from app.config import get_config
        get_config(reload=True)
        assert get_config().teacher.teacher_name == "Ms Smith"
        assert get_config().grades.session_label == "June 2026"

    def test_dashboard_exposes_first_run_flag(self, client):
        data = client.get("/api/dashboard").json()
        assert data["first_run"] is True


class TestSelfCheck:
    def test_selfcheck_has_no_silent_gaps(self, client):
        data = client.get("/api/diagnostics/selfcheck").json()
        assert data["overall"] in ("pass", "warn", "fail")
        names = {c["name"] for c in data["checks"]}
        assert {"Application", "Storage", "AI keys", "Google Classroom",
                "Network access"} <= names
        for check in data["checks"]:
            assert check["status"] in ("pass", "warn", "fail")
            assert check["detail"]

    def test_selfcheck_warns_without_keys(self, client):
        data = client.get("/api/diagnostics/selfcheck").json()
        keys = next(c for c in data["checks"] if c["name"] == "AI keys")
        assert keys["status"] == "fail"
        assert keys["fix"]


class TestBackup:
    def test_export_excludes_secrets_by_default(self, client):
        response = client.get("/api/backup/config")
        assert response.status_code == 200
        payload = json.loads(response.content)
        assert payload["contains_secrets"] is False
        assert "secrets" not in payload
        assert "config" in payload

    def test_export_includes_secrets_on_request(self, client):
        from app.config import get_secrets
        get_secrets().set_gemini_keys(["AIzaBackupTestKey123456"])
        payload = json.loads(client.get("/api/backup/config?include_secrets=true").content)
        assert payload["contains_secrets"] is True
        assert payload["secrets"]["gemini_api_keys"] == ["AIzaBackupTestKey123456"]
        assert "secrets_warning" in payload

    def test_import_restores_config(self, client):
        client.post("/api/setup/complete", json={
            "teacher_name": "Original", "school_name": "School A"})
        backup = json.loads(client.get("/api/backup/config").content)
        # Simulate a fresh install being restored on a new machine.
        from app.config import get_config, save_config
        cfg = get_config()
        cfg.teacher.teacher_name = ""
        cfg.teacher.school_name = ""
        save_config(cfg)
        response = client.post("/api/backup/config", json=backup)
        assert response.status_code == 200
        assert response.json()["restored"] is True
        get_config(reload=True)
        assert get_config().teacher.teacher_name == "Original"

    def test_bad_backup_rejected(self, client):
        response = client.post("/api/backup/config", json={"nope": 1})
        assert response.status_code == 400

    def test_database_backup_is_downloadable(self, client):
        response = client.get("/api/backup/database")
        assert response.status_code == 200
        assert response.content[:16].startswith(b"SQLite format 3")


class TestReviewQueue:
    def test_empty_queue(self, client):
        data = client.get("/api/review").json()
        assert data["items"] == []

    def test_low_confidence_result_appears(self, client, fresh_state):
        import app.db as db
        import time
        db.execute(
            "INSERT INTO correction_jobs(id, status, created_at, updated_at) "
            "VALUES('j1','COMPLETED',?,?)", (time.time(), time.time()))
        db.execute(
            "INSERT INTO results(id, job_id, student_name, earned, available, "
            "percentage, result_json, confidence, confidence_reasons, "
            "review_required, created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            ("r1", "j1", "Alice", 4, 10, 40.0, "{}", "LOW",
             json.dumps(["verification requested review"]), 1, time.time()))
        data = client.get("/api/review").json()
        assert data["total"] == 1
        assert data["items"][0]["student_name"] == "Alice"
        assert data["items"][0]["confidence_reasons"]

    def test_manual_override_sets_flag(self, client, fresh_state):
        import app.db as db
        import time
        db.execute(
            "INSERT INTO correction_jobs(id, status, created_at, updated_at) "
            "VALUES('j2','COMPLETED',?,?)", (time.time(), time.time()))
        db.execute(
            "INSERT INTO results(id, job_id, student_name, earned, available, "
            "percentage, result_json, review_required, created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            ("r2", "j2", "Bob", 3, 10, 30.0, "{}", 1, time.time()))
        response = client.post("/api/results/r2/override",
                               json={"earned": 7, "note": "rechecked"})
        assert response.status_code == 200
        row = db.query_one("SELECT * FROM results WHERE id = 'r2'")
        assert row["earned"] == 7
        assert row["manual_override"] == 1
        assert row["review_required"] == 0

    def test_override_rejects_out_of_range(self, client, fresh_state):
        import app.db as db
        import time
        db.execute(
            "INSERT INTO correction_jobs(id, status, created_at, updated_at) "
            "VALUES('j3','COMPLETED',?,?)", (time.time(), time.time()))
        db.execute(
            "INSERT INTO results(id, job_id, student_name, earned, available, "
            "percentage, result_json, created_at) VALUES(?,?,?,?,?,?,?,?)",
            ("r3", "j3", "Cara", 3, 10, 30.0, "{}", time.time()))
        response = client.post("/api/results/r3/override", json={"earned": 99})
        assert response.status_code == 400

    def test_approval_removes_low_confidence_result_from_queue(
            self, client, fresh_state):
        """Accepting a result must clear the confidence flags that put it in
        the queue, so the row actually leaves the review page (§E)."""
        import app.db as db
        import time
        db.execute(
            "INSERT INTO correction_jobs(id, status, created_at, updated_at) "
            "VALUES('j4','COMPLETED',?,?)", (time.time(), time.time()))
        db.execute(
            "INSERT INTO results(id, job_id, student_name, earned, available, "
            "percentage, result_json, confidence, confidence_reasons, "
            "review_required, created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            ("r4", "j4", "Dana", 4, 10, 40.0, "{}", "LOW",
             json.dumps(["verification requested review"]), 1, time.time()))
        assert client.get("/api/review").json()["total"] == 1

        response = client.post("/api/results/r4/override",
                               json={"note": "Accepted by teacher"})
        assert response.status_code == 200
        assert response.json()["action"] == "approved"

        # The row is gone from the queue and no longer confidence-flagged.
        assert client.get("/api/review").json()["total"] == 0
        row = db.query_one("SELECT * FROM results WHERE id = 'r4'")
        assert row["manual_override"] == 1
        assert row["review_required"] == 0
        assert row["confidence"] == "HIGH"
        assert "Approved" in row["confidence_reasons"]

    def test_override_records_review_history(self, client, fresh_state):
        import app.db as db
        import time
        db.execute(
            "INSERT INTO correction_jobs(id, status, created_at, updated_at) "
            "VALUES('j5','COMPLETED',?,?)", (time.time(), time.time()))
        db.execute(
            "INSERT INTO results(id, job_id, student_name, earned, available, "
            "percentage, result_json, review_required, created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            ("r5", "j5", "Eve", 5, 10, 50.0, "{}", 1, time.time()))
        client.post("/api/results/r5/override",
                    json={"earned": 8, "note": "rechecked"})
        history = client.get("/api/results/r5/reviews").json()["history"]
        assert len(history) == 1
        assert history[0]["action"] == "overridden"
        assert history[0]["previous_earned"] == 5
        assert history[0]["new_earned"] == 8
