"""Unified PDF correction (§3) — one entry point, the real two pipelines.

The page that merged "PDF Correction" and "Large PDF Correction" must not have
created a third marking engine: it picks one of the two EXISTING paths using the
one routing rule in ``app.engine.router``. These tests prove that:

  * the automatic choice follows the student PDF's real page count;
  * the standard path really marks the submission (mock engine) and produces a
    1–9 grade;
  * an explicit Advanced-Options override reaches the batched pipeline;
  * an unreadable PDF is refused instead of being quietly treated as short.
"""

from __future__ import annotations

import io
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


def test_pipelines_endpoint_reports_the_real_rule(client):
    body = client.get("/api/corrections/pipelines").json()
    assert body["threshold_pages"] >= 1
    values = [o["value"] for o in body["options"]]
    assert values == ["auto", "standard", "large"]
    assert all(o["detail"] for o in body["options"])


def test_auto_routes_a_short_pdf_to_the_standard_pipeline_and_marks_it(client):
    files = {
        "submission": ("student.pdf", io.BytesIO(_pdf_bytes(2)), "application/pdf"),
        "mark_scheme": ("scheme.pdf", io.BytesIO(_pdf_bytes(1)), "application/pdf"),
    }
    response = client.post("/api/corrections/unified", files=files,
                           data={"student_name": "Unified Student"})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["applied_pipeline"] == "standard"
    assert body["page_count"] == 2
    assert body["reason"]
    assert body["files"]["submission"]["bytes"] > 0

    job_id = body["job_id"]
    deadline = time.time() + 25
    job = None
    while time.time() < deadline:
        job = client.get(f"/api/jobs/{job_id}").json()
        if job["status"] in ("COMPLETED", "FAILED", "CANCELLED"):
            break
        time.sleep(0.2)
    assert job and job["status"] == "COMPLETED", job

    result = client.get(f"/api/results/{job['result_id']}").json()
    assert result["earned"] == 9 and result["available"] == 12
    assert result["provisional_grade"] is not None
    grade = int(result["provisional_grade"])
    assert 1 <= grade <= 9            # the grade scale stays 1-9


def test_auto_routes_a_long_pdf_to_the_batched_pipeline(client, monkeypatch):
    from app.config import get_config
    cfg = get_config()
    cfg.large_pdf.enabled = True
    cfg.large_pdf.threshold_pages = 3
    from app.config import save_config
    save_config(cfg)

    files = {
        "submission": ("big.pdf", io.BytesIO(_pdf_bytes(6)), "application/pdf"),
        "mark_scheme": ("scheme.pdf", io.BytesIO(_pdf_bytes(1)), "application/pdf"),
    }
    response = client.post("/api/corrections/unified", files=files,
                           data={"student_name": "Long Document"})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["applied_pipeline"] == "large"
    assert body["page_count"] == 6
    assert body["page_limit"] == 3
    assert body["large_pdf_job_id"]

    listing = client.get("/api/large-pdf/jobs").json()
    ids = [j["id"] for j in listing["jobs"]]
    assert body["large_pdf_job_id"] in ids
    # the batched job was really inspected and planned, not stubbed
    assert body["job"]["page_count"] == 6
    assert body["job"]["batch_count"] >= 1


def test_advanced_override_can_force_the_batched_pipeline_early(client):
    from app.config import get_config, save_config
    cfg = get_config()
    cfg.large_pdf.enabled = True
    save_config(cfg)
    files = {
        "submission": ("small.pdf", io.BytesIO(_pdf_bytes(1)), "application/pdf"),
        "mark_scheme": ("scheme.pdf", io.BytesIO(_pdf_bytes(1)), "application/pdf"),
    }
    response = client.post("/api/corrections/unified", files=files,
                           data={"student_name": "Forced", "pipeline": "large"})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["applied_pipeline"] == "large"
    # the reason says the teacher overrode the automatic rule, and why
    assert "manually" in body["reason"]


def test_a_pdf_that_cannot_be_read_is_refused(client):
    files = {
        "submission": ("broken.pdf", io.BytesIO(b"%PDF-1.4 not really a pdf"),
                       "application/pdf"),
        "mark_scheme": ("scheme.pdf", io.BytesIO(_pdf_bytes(1)), "application/pdf"),
    }
    response = client.post("/api/corrections/unified", files=files,
                           data={"student_name": "Broken"})
    assert response.status_code == 400
    assert response.json()["code"] == "unreadable_pdf"


def test_an_unknown_pipeline_value_is_refused(client):
    files = {
        "submission": ("s.pdf", io.BytesIO(_pdf_bytes(1)), "application/pdf"),
        "mark_scheme": ("m.pdf", io.BytesIO(_pdf_bytes(1)), "application/pdf"),
    }
    response = client.post("/api/corrections/unified", files=files,
                           data={"pipeline": "turbo"})
    assert response.status_code == 400
    assert response.json()["code"] == "bad_pipeline"


def test_the_batched_pipeline_is_named_in_the_automation_view(client):
    """§8: the workflow view says which REAL pipeline is doing the work.

    The attribution is read from the large-PDF jobs table, so a submission the
    router sent to the batched worker is named as batched, an ordinary job is
    named as standard, and a job that has not reached correction yet claims
    neither.
    """
    import time

    from app import db

    now = time.time()

    def automation_job(job_id: str, submission_id: str, *, correction_job: str | None):
        db.execute(
            "INSERT INTO automation_jobs(id, course_id, coursework_id, "
            "student_name, submission_id, submission_version, status, "
            "correction_job_id, created_at, updated_at, next_attempt_at) "
            "VALUES(?,?,?,?,?,?,'NEW',?,?,?,0)",
            (job_id, "course-1", "cw-1", "Student " + job_id, submission_id,
             "v1", correction_job, now, now))

    automation_job("aj-new", "sub-new", correction_job=None)
    automation_job("aj-std", "sub-std", correction_job="cj-std")
    automation_job("aj-batched", "sub-batched", correction_job="cj-batched")

    # The batched worker really handled this submission.
    db.execute(
        "INSERT INTO large_pdf_jobs(id, submission_id, coursework_id, "
        "course_id, student_name, student_pdf_path, mark_scheme_path, "
        "processing_version, page_count, batch_count, status, created_at, "
        "updated_at) VALUES('lpj-1','sub-batched','cw-1','course-1','Student',"
        "'/tmp/a.pdf','/tmp/b.pdf','pv1',54,3,'PROCESSING',?,?)", (now, now))

    jobs = {j["id"]: j for j in client.get("/api/automation/jobs").json()["jobs"]}
    assert jobs["aj-batched"]["pipeline"] == "batched"
    assert "atched" in jobs["aj-batched"]["pipeline_label"]
    assert jobs["aj-std"]["pipeline"] == "standard"
    assert "ing" in jobs["aj-std"]["pipeline_label"]
    # Nothing has corrected it yet, so no pipeline is claimed.
    assert jobs["aj-new"]["pipeline"] is None
    assert jobs["aj-new"]["pipeline_label"] is None


def test_the_old_single_purpose_endpoints_still_work(client):
    """Nothing was removed: deep links and integrations keep their endpoints."""
    files = {
        "submission": ("student.pdf", io.BytesIO(_pdf_bytes(1)), "application/pdf"),
        "mark_scheme": ("scheme.pdf", io.BytesIO(_pdf_bytes(1)), "application/pdf"),
    }
    assert client.post("/api/corrections/manual", files=files,
                       data={"student_name": "Legacy"}).status_code == 200
    large = client.post("/api/large-pdf/jobs", files={
        "submission": ("s.pdf", io.BytesIO(_pdf_bytes(1)), "application/pdf"),
        "mark_scheme": ("m.pdf", io.BytesIO(_pdf_bytes(1)), "application/pdf"),
    }, data={"student_name": "Legacy Large"})
    assert large.status_code == 200, large.text
