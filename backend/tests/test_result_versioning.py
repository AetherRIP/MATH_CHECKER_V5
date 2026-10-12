"""Correction versioning (§D): append-only history + teacher-chosen final."""

import time

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


def _seed_result(result_id="r1", earned=5, available=10):
    import app.db as db
    now = time.time()
    db.execute(
        "INSERT INTO correction_jobs(id, status, created_at, updated_at) "
        "VALUES(?,?,?,?)", (f"j-{result_id}", "COMPLETED", now, now))
    db.execute(
        "INSERT INTO results(id, job_id, student_name, earned, available, "
        "percentage, result_json, created_at) VALUES(?,?,?,?,?,?,?,?)",
        (result_id, f"j-{result_id}", "Ann", earned, available,
         earned / available * 100, "{}", now))


def _record(result_id, earned, available, note):
    import app.db as db
    from app.engine import versions
    with db.transaction() as conn:
        versions.record(
            conn, result_id=result_id, earned=earned, available=available,
            percentage=earned / available * 100, provisional_grade="P",
            marking_model="m", key_fingerprint=None, confidence="HIGH",
            confidence_reasons=["saved"], verification={"status": "VALID"},
            result={"questions": []}, created_at=time.time(), note=note)


class TestVersions:
    def test_record_is_append_only_and_marks_current(self, fresh_state):
        from app.engine import versions
        _seed_result()
        _record("r1", 5, 10, "v1")
        _record("r1", 8, 10, "v2")
        listed = versions.list_versions("r1")
        assert [v["version"] for v in listed] == [2, 1]
        assert listed[0]["is_current"] is True
        assert listed[1]["is_current"] is False

    def test_versions_endpoint_lists_history(self, client, fresh_state):
        _seed_result()
        _record("r1", 5, 10, "v1")
        _record("r1", 8, 10, "v2")
        data = client.get("/api/results/r1/versions").json()
        assert len(data["versions"]) == 2
        assert data["versions"][0]["earned"] == 8

    def test_restore_applies_snapshot_and_appends_choice(self, client, fresh_state):
        import app.db as db
        _seed_result(earned=5)
        _record("r1", 5, 10, "v1")
        # Simulate a recheck that replaced the result with 8/10.
        db.execute("UPDATE results SET earned = 8, percentage = 80 WHERE id = 'r1'")
        _record("r1", 8, 10, "v2")

        response = client.post("/api/results/r1/versions/1/restore")
        assert response.status_code == 200
        row = db.query_one("SELECT earned, manual_override FROM results WHERE id = 'r1'")
        assert row["earned"] == 5
        assert row["manual_override"] == 1

        listed = db.query(
            "SELECT version, note FROM result_versions WHERE result_id = 'r1' "
            "ORDER BY version DESC")
        assert len(listed) == 3
        assert listed[0]["version"] == 3
        assert "version 1" in listed[0]["note"]

    def test_restore_missing_version_is_404(self, client, fresh_state):
        _seed_result()
        response = client.post("/api/results/r1/versions/9/restore")
        assert response.status_code == 404


class TestCorrectionPdf:
    def test_result_pdf_is_a_real_pdf(self, client, fresh_state):
        import io
        from pypdf import PdfReader
        _seed_result()
        response = client.get("/api/results/r1/export?format=pdf&detail=detailed")
        assert response.status_code == 200
        assert response.headers["content-type"] == "application/pdf"
        assert response.content.startswith(b"%PDF")
        assert len(PdfReader(io.BytesIO(response.content)).pages) >= 1

    def test_result_pdf_rejects_bad_detail(self, client, fresh_state):
        _seed_result()
        response = client.get("/api/results/r1/export?format=pdf&detail=tiny")
        assert response.status_code == 400

    def test_result_pdf_with_sparse_question_does_not_crash(self, client, fresh_state):
        """A detailed question with no stored fields must still render (a
        zero-row table used to raise inside reportlab)."""
        import json
        import app.db as db
        now = time.time()
        db.execute(
            "INSERT INTO correction_jobs(id, status, created_at, updated_at) "
            "VALUES('jsp','COMPLETED',?,?)", (now, now))
        db.execute(
            "INSERT INTO results(id, job_id, student_name, earned, available, "
            "percentage, result_json, verification_json, created_at) "
            "VALUES('rsp','jsp','Ada',2,5,40.0,?,'{}',?)",
            (json.dumps({"questions": [
                {"question_number": "1", "max_marks": 5, "awarded_marks": 2}]}), now))
        response = client.get("/api/results/rsp/export?format=pdf&detail=detailed")
        assert response.status_code == 200
        assert response.content.startswith(b"%PDF")


class TestStudentAssignmentPdf:
    def _seed(self):
        import app.db as db
        now = time.time()
        db.execute(
            "INSERT INTO correction_jobs(id, status, created_at, updated_at) "
            "VALUES('js','COMPLETED',?,?)", (now, now))
        db.execute(
            "INSERT INTO results(id, job_id, coursework_id, student_id, "
            "student_name, earned, available, percentage, result_json, "
            "verification_json, created_at) "
            "VALUES('rs','js','w9','u1','Ada',7,10,70.0,'{\"questions\":[]}',"
            "'{}',?)", (now,))

    def test_missing_correction_is_404(self, client, fresh_state):
        response = client.get("/api/students/u1/assignments/w9/report")
        assert response.status_code == 404

    def test_student_assignment_pdf_exports(self, client, fresh_state):
        self._seed()
        response = client.get("/api/students/u1/assignments/w9/report?detail=detailed")
        assert response.status_code == 200
        assert response.headers["content-type"] == "application/pdf"
        assert response.content.startswith(b"%PDF")


class TestQuestionOverride:
    def _seed_with_questions(self):
        import json
        import app.db as db
        now = time.time()
        db.execute(
            "INSERT INTO correction_jobs(id, status, created_at, updated_at) "
            "VALUES('jq','COMPLETED',?,?)", (now, now))
        result = {"questions": [
            {"question_number": "1", "max_marks": 5, "awarded_marks": 2},
            {"question_number": "2", "max_marks": 5, "awarded_marks": 5},
        ]}
        db.execute(
            "INSERT INTO results(id, job_id, student_name, earned, available, "
            "percentage, result_json, verification_json, created_at) "
            "VALUES('rq','jq','Ann',7,10,70.0,?,?,?)",
            (json.dumps(result),
             json.dumps({"status": "REVIEW", "review_questions": ["1"],
                         "anomalies": []}), now))

    def test_override_updates_totals_and_appends_version(self, client, fresh_state):
        import app.db as db
        self._seed_with_questions()
        response = client.post("/api/results/rq/questions/1/mark",
                               json={"awarded": 5, "note": "teacher agreed"})
        assert response.status_code == 200
        data = response.json()
        assert data["previous"] == 2
        assert data["earned"] == 10
        row = db.query_one(
            "SELECT earned, available, manual_override, confidence "
            "FROM results WHERE id = 'rq'")
        assert row["earned"] == 10 and row["manual_override"] == 1
        versions = db.query(
            "SELECT * FROM result_versions WHERE result_id = 'rq'")
        assert len(versions) == 1
        assert versions[0]["earned"] == 10

    def test_override_rejects_over_max(self, client, fresh_state):
        self._seed_with_questions()
        response = client.post("/api/results/rq/questions/1/mark",
                               json={"awarded": 9})
        assert response.status_code == 400

    def test_override_unknown_question_is_404(self, client, fresh_state):
        self._seed_with_questions()
        response = client.post("/api/results/rq/questions/9/mark",
                               json={"awarded": 1})
        assert response.status_code == 404
