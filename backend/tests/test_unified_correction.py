"""One correction entry point — the standard single-pass pipeline.

These tests prove that:

  * an upload is accepted, queued and really marked by the standard engine
    (mock mode) and produces a 1–9 grade;
  * a document larger than the removed batching threshold still follows the
    SAME standard pipeline — nothing delegates it to another worker;
  * the removed Large PDF endpoints are no longer registered;
  * legacy large-PDF tables in an existing database are left untouched.
"""

from __future__ import annotations

import io
import sys
import time

import pytest
from fastapi.testclient import TestClient


def _pdf_bytes(pages: int = 1) -> bytes:
    from pypdf import PdfWriter
    writer = PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(width=200, height=200)
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


@pytest.fixture
def client(fresh_state):
    from app.config import get_config, get_secrets, save_config
    cfg = get_config()
    cfg.mock_mode = True
    save_config(cfg)
    get_secrets().set_gemini_keys(["AIzaMockKey1234567890abcdef"])
    from app.main import app
    with TestClient(app) as c:
        yield c


def _wait_terminal(client: TestClient, job_id: str, timeout: float = 60.0) -> dict:
    deadline = time.time() + timeout
    job = None
    while time.time() < deadline:
        job = client.get(f"/api/jobs/{job_id}").json()
        if job["status"] in ("COMPLETED", "FAILED", "CANCELLED"):
            return job
        time.sleep(0.2)
    return job or {}


def test_standard_correction_marks_the_submission(client):
    files = {
        "submission": ("student.pdf", io.BytesIO(_pdf_bytes(2)), "application/pdf"),
        "mark_scheme": ("scheme.pdf", io.BytesIO(_pdf_bytes(1)), "application/pdf"),
    }
    response = client.post("/api/corrections/unified", files=files,
                           data={"student_name": "Unified Student"})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["job_id"]
    assert body["page_count"] == 2
    assert body["reason"]
    assert body["files"]["submission"]["bytes"] > 0
    # The removed pipeline is not named anywhere in the response.
    assert "applied_pipeline" not in body
    assert "large_pdf_job_id" not in body

    job = _wait_terminal(client, body["job_id"], timeout=30)
    assert job["status"] == "COMPLETED", job

    result = client.get(f"/api/results/{job['result_id']}").json()
    assert result["earned"] == 9 and result["available"] == 12
    assert result["provisional_grade"] is not None
    grade = int(result["provisional_grade"])
    assert 1 <= grade <= 9            # the grade scale stays 1-9


def test_a_long_pdf_still_uses_the_standard_pipeline(client):
    """31 pages exceeded the removed threshold; nothing may delegate it."""
    files = {
        "submission": ("big.pdf", io.BytesIO(_pdf_bytes(31)), "application/pdf"),
        "mark_scheme": ("scheme.pdf", io.BytesIO(_pdf_bytes(1)), "application/pdf"),
    }
    response = client.post("/api/corrections/unified", files=files,
                           data={"student_name": "Long Document"})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["job_id"]
    assert body["page_count"] == 31
    assert "large_pdf_job_id" not in body

    job = _wait_terminal(client, body["job_id"], timeout=120)
    assert job["status"] == "COMPLETED", job
    assert job["result_id"]

    # No batched-job table exists in a fresh database, and no module of the
    # removed pipeline was imported to run this correction.
    import app.db as db
    row = db.query_one(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
        ("large_pdf_jobs",))
    assert row is None
    assert "app.large_pdf" not in sys.modules


def test_removed_large_pdf_endpoints_are_not_registered(client):
    from app.main import app

    for path in ("/api/large-pdf/jobs", "/api/large-pdf/config",
                 "/api/corrections/pipelines"):
        assert client.get(path).status_code == 404, path
    # No POST handler is registered for the removed upload endpoint.
    assert client.post("/api/large-pdf/jobs").status_code in (404, 405)
    assert not hasattr(app.state, "large_pdf")


def test_legacy_large_pdf_tables_survive_an_existing_database(client):
    """An existing install keeps its rows; startup must not drop them."""
    import app.db as db

    db.execute(
        "CREATE TABLE IF NOT EXISTS large_pdf_jobs ("
        "id TEXT PRIMARY KEY, coursework_id TEXT, status TEXT)")
    db.execute(
        "CREATE TABLE IF NOT EXISTS large_pdf_batches ("
        "id TEXT PRIMARY KEY, job_id TEXT)")
    db.execute(
        "INSERT OR REPLACE INTO large_pdf_jobs(id, coursework_id, status) "
        "VALUES('legacy-job', 'legacy-cw', 'COMPLETED')")
    db.execute(
        "INSERT OR REPLACE INTO large_pdf_batches(id, job_id) "
        "VALUES('legacy-batch', 'legacy-job')")

    db.init_db()

    jobs = db.query("SELECT id, status FROM large_pdf_jobs")
    batches = db.query("SELECT id FROM large_pdf_batches")
    assert [dict(r) for r in jobs] == [
        {"id": "legacy-job", "status": "COMPLETED"}]
    assert [dict(r) for r in batches] == [{"id": "legacy-batch"}]
