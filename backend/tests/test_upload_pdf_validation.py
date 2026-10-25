"""An unreadable PDF is refused at the door, not queued.

The end-to-end test uploaded a corrupt PDF through ``/api/corrections/unified``
and received HTTP 200 with ``page_count: 0`` plus a queued job. The job could
only fail later, with a message the teacher cannot act on. These tests pin the
corrected behaviour at the real endpoint: a clear 4xx, no job row, no pending
file, no staged temp file — and completely unchanged behaviour for a valid PDF.
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


def _post(client, submission: bytes, scheme: bytes | None = None,
          name: str = "student.pdf", endpoint: str = "/api/corrections/unified"):
    files = {
        "submission": (name, io.BytesIO(submission), "application/pdf"),
        "mark_scheme": ("scheme.pdf", io.BytesIO(scheme if scheme is not None
                                                 else _pdf_bytes(1)),
                        "application/pdf"),
    }
    return client.post(endpoint, files=files, data={"student_name": "Refused"})


def _job_count() -> int:
    import app.db as db
    return len(db.query("SELECT id FROM correction_jobs"))


def _pending_count() -> int:
    import app.db as db
    return len(db.query("SELECT job_id FROM pending_manual_files"))


def _assert_refused(client, response):
    assert response.status_code == 400, response.text
    body = response.json()
    assert body["code"] == "corrupt_pdf"
    assert "cannot be marked" in body["message"]
    assert "job_id" not in body
    # Nothing was queued, nothing was left half-written.
    assert _job_count() == 0
    assert _pending_count() == 0
    from app.config import data_dir
    staging = data_dir() / "tmp" / "unified_uploads"
    assert not staging.exists() or not list(staging.glob("*"))


def test_a_corrupt_pdf_is_refused_and_not_queued(client):
    _assert_refused(client, _post(
        client, b"%PDF-1.7\n" + b"\x00\xff not really a pdf body" * 40))


def test_a_truncated_pdf_is_refused(client):
    good = _pdf_bytes(1)
    _assert_refused(client, _post(client, good[: len(good) // 3]))


def test_an_empty_pdf_is_refused(client):
    _assert_refused(client, _post(client, b""))


def test_a_file_that_is_not_a_pdf_at_all_is_refused(client):
    _assert_refused(client, _post(client, b"just some text pretending to be a PDF"))


def test_a_corrupt_mark_scheme_is_refused_too(client):
    response = _post(client, _pdf_bytes(1),
                     scheme=b"%PDF-1.7\n" + b"\x00garbage" * 20)
    _assert_refused(client, response)
    assert "mark_scheme" in response.json()["message"]


def test_the_manual_upload_refuses_a_corrupt_pdf_as_well(client):
    _assert_refused(client, _post(
        client, b"%PDF-1.7\n" + b"\x00garbage" * 20,
        endpoint="/api/corrections/manual"))


def test_a_valid_pdf_still_follows_the_normal_workflow(client):
    response = _post(client, _pdf_bytes(2))
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["page_count"] == 2
    assert body["job_id"]
    assert _job_count() == 1

    deadline = time.time() + 90
    job = {}
    while time.time() < deadline:
        job = client.get(f"/api/jobs/{body['job_id']}").json()
        if job["status"] in ("COMPLETED", "FAILED", "CANCELLED"):
            break
        time.sleep(0.2)
    assert job.get("status") == "COMPLETED", job
    assert job.get("result_id")
