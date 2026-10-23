"""The student-work viewer's honesty contract (marking desk, §UI redesign).

The desk reported "This paper's original file is missing." for a paper that was
marked while submission retention was still OFF (the default). It was not a
corrupt file or a broken identifier: terminal cleanup deleted the staged input
at job completion, exactly as the setting asked. The viewer then reported the
gap — but the message could not say which of several causes applied.

These tests pin every outcome of ``GET /api/results/{id}/pages``:

* retention off            → the setting deleted it (``retention_disabled``)
* retained copy expired    → ``retention_expired``, with the date and window
* never stored             → ``file_missing`` (finished inside the window)
* job still active         → ``processing`` (retryable)
* result with no job link  → ``no_link``
* stored copy              → real pages, real PNG bytes, ETag 304, range errors
* surviving staging file   → served (a crashed cleanup must not hide a paper)
* path outside the data dir → refused, never served
* image submission         → one page, the original bytes

Everything is synthetic: reportlab draws the "paper" and the only student name
in the database is a literal "Synthetic Student" used to prove it never leaks.
No live data, no Google calls, no AI calls.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from app import db
from app.api import viewer_routes
from app.config import data_dir, get_config, save_config
from app.errors import AppError


# --------------------------------------------------------------------- helpers


def synthetic_paper(path: Path, pages: int = 2) -> Path:
    """A real, readable PDF with mathematical notation (no student content)."""
    from reportlab.pdfgen import canvas

    path.parent.mkdir(parents=True, exist_ok=True)
    pdf = canvas.Canvas(str(path), pagesize=(595, 842))
    for page in range(1, pages + 1):
        pdf.setFont("Helvetica-Bold", 14)
        pdf.drawString(60, 780, f"SYNTHETIC PAPER - page {page}")
        pdf.setFont("Helvetica", 12)
        pdf.drawString(60, 740, "Q1  Solve  3x + 7 = 22   =>   x = 5")
        pdf.drawString(60, 715, "Q2  Factorise  x^2 + 5x + 6 = (x + 2)(x + 3)")
        pdf.drawString(60, 690, "Q3  sin(30 deg) = 1/2    cos(60 deg) = 1/2")
        pdf.line(60, 675, 240, 675)
        pdf.drawString(60, 655, "5 / 6")
        pdf.showPage()
    pdf.save()
    return path


def seed_result(
    result_id: str = "res-viewer-1",
    job_id: str | None = "job-viewer-1",
    *,
    status: str = "COMPLETED",
    created_at: float | None = None,
    finished_at: float | None = None,
    student_name: str = "Synthetic Student",
) -> None:
    """One stored result plus the job row that produced it."""
    now = time.time()
    created = created_at if created_at is not None else now - 3600
    # results.job_id is NOT NULL REFERENCES correction_jobs(id), so the job row
    # has to exist first — the same order the real engine writes them in.
    db.execute(
        "INSERT INTO correction_jobs(id, status, progress, attempts, "
        "max_attempts, created_at, started_at, finished_at, updated_at, "
        "result_id) VALUES(?,?,?,?,?,?,?,?,?,?)",
        (job_id, status, 1.0, 1, 3, created,
         created + 5, finished_at if finished_at is not None else now - 1800,
         now, result_id),
    )
    db.execute(
        "INSERT INTO results(id, job_id, coursework_id, submission_id, "
        "student_id, student_name, earned, available, percentage, "
        "provisional_grade, result_json, verification_json, created_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (result_id, job_id, "cw1", "sub1", "stu1", student_name, 5, 6, 83.3,
         "7", json.dumps({"questions": []}),
         json.dumps({"status": "VALID"}), created),
    )


def retention(mode: str, days: int = 30) -> None:
    cfg = get_config()
    cfg.retention.keep_submissions = mode
    cfg.retention.keep_days = days
    save_config(cfg)


def retain(job_id: str, path: Path, *, filename: str | None = None) -> None:
    db.execute(
        "INSERT OR REPLACE INTO retained_files"
        "(job_id, path, kind, filename, retain_until, created_at)"
        " VALUES(?,?,?,?,?,?)",
        (job_id, str(path), "submission", filename or path.name,
         time.time() + 86400 * 30, time.time()),
    )


@pytest.fixture
def client(fresh_state):
    cfg = get_config()
    cfg.mock_mode = True
    save_config(cfg)

    app = FastAPI()
    viewer_routes.register(app)

    @app.exception_handler(AppError)
    async def _app_error(_request, exc: AppError):
        return JSONResponse({"code": exc.code, "message": exc.user_message},
                            status_code=exc.status)

    with TestClient(app) as test_client:
        yield test_client


# ------------------------------------------------------------------ missing


def test_unknown_result_is_a_404(client):
    response = client.get("/api/results/does-not-exist/pages")

    assert response.status_code == 404
    assert response.json()["code"] == "result_not_found"


def test_retention_off_says_the_setting_deleted_the_original(client):
    retention("until_complete")
    seed_result()

    body = client.get("/api/results/res-viewer-1/pages").json()

    assert body["available"] is False
    assert body["reason"] == "retention_disabled"
    assert body["retryable"] is False
    assert "retention is OFF" in body["message"]
    assert body["page_count"] == 0
    assert body["file_name"] is None


def test_retention_on_but_never_stored_reports_the_real_gap(client):
    # Finished two days ago, kept for 30: an expiry cannot explain the gap,
    # so the copy was never made (the setting was off when the job ran).
    retention("days", 30)
    seed_result(finished_at=time.time() - 2 * 86400)

    body = client.get("/api/results/res-viewer-1/pages").json()

    assert body["reason"] == "file_missing"
    assert body["retryable"] is True
    assert "before retention was enabled" in body["message"]


def test_expired_window_is_named_with_the_date_and_window(client):
    retention("days", 30)
    finished = time.time() - 40 * 86400
    seed_result(finished_at=finished)

    body = client.get("/api/results/res-viewer-1/pages").json()

    assert body["reason"] == "retention_expired"
    assert body["retryable"] is False
    assert "30-day" in body["message"]
    assert time.strftime("%d %b %Y", time.localtime(finished)) in body["message"]


def test_always_keep_never_claims_the_window_expired(client):
    retention("always")
    seed_result(finished_at=time.time() - 400 * 86400)

    body = client.get("/api/results/res-viewer-1/pages").json()

    assert body["reason"] == "file_missing"
    assert "always keep" in body["message"]


def test_active_job_reports_processing_and_can_be_retried(client):
    retention("until_complete")
    seed_result(status="PROCESSING")

    body = client.get("/api/results/res-viewer-1/pages").json()

    assert body["reason"] == "processing"
    assert body["retryable"] is True
    assert body["job_status"] == "PROCESSING"


def test_result_without_a_job_link_says_so(client):
    # The schema makes results.job_id NOT NULL, so this row cannot be produced
    # by the app; the viewer still answers instead of throwing if an old or
    # hand-edited database contains one.
    retention("until_complete")

    body = viewer_routes._missing_payload("res-orphan",
                                          {"id": "res-orphan", "job_id": None})

    assert body["reason"] == "no_link"
    assert body["job_status"] is None
    assert body["retryable"] is False


def test_missing_payload_never_leaks_student_identity(client):
    retention("days", 30)
    seed_result(finished_at=time.time() - 2 * 86400)

    raw = client.get("/api/results/res-viewer-1/pages").text

    assert "Synthetic Student" not in raw
    assert "stu1" not in raw
    assert str(data_dir()) not in raw


def test_page_image_for_a_missing_original_is_a_404_not_a_blank_page(client):
    retention("until_complete")
    seed_result()

    response = client.get("/api/results/res-viewer-1/pages/1.png")

    assert response.status_code == 404
    assert response.json()["code"] == "original_not_stored"


# ------------------------------------------------------------------ stored


def test_retained_copy_serves_real_pages_with_etag_and_range_errors(client):
    retention("days", 30)
    seed_result()
    paper = synthetic_paper(data_dir() / "retained" / "job-viewer-1" / "paper.pdf")
    retain("job-viewer-1", paper, filename="synthetic-paper.pdf")

    body = client.get("/api/results/res-viewer-1/pages").json()

    assert body["available"] is True
    assert body["reason"] is None
    assert body["page_count"] == 2
    assert body["file_kind"] == "pdf"
    assert body["source"] == "retained"
    assert body["file_name"] == "synthetic-paper.pdf"

    first = client.get("/api/results/res-viewer-1/pages/1.png")
    assert first.status_code == 200
    assert first.headers["content-type"] == "image/png"
    assert first.content.startswith(b"\x89PNG")
    etag = first.headers["etag"]
    assert etag

    again = client.get("/api/results/res-viewer-1/pages/1.png",
                       headers={"If-None-Match": etag})
    assert again.status_code == 304
    assert again.content == b""

    third = client.get("/api/results/res-viewer-1/pages/3.png")
    assert third.status_code == 404
    assert third.json()["code"] == "page_out_of_range"

    zoomed = client.get("/api/results/res-viewer-1/pages/1.png?dpi=300")
    assert zoomed.status_code == 200
    assert zoomed.content.startswith(b"\x89PNG")
    assert zoomed.headers["etag"] != etag


def test_unreadable_copy_is_reported_as_damaged_not_available(client):
    retention("days", 30)
    seed_result()
    broken = data_dir() / "retained" / "job-viewer-1" / "paper.pdf"
    broken.parent.mkdir(parents=True, exist_ok=True)
    broken.write_bytes(b"this is not a pdf at all")
    retain("job-viewer-1", broken)

    body = client.get("/api/results/res-viewer-1/pages").json()

    assert body["available"] is False
    assert body["reason"] == "file_unreadable"
    assert body["retryable"] is False


def test_surviving_staging_file_is_served_instead_of_reported_missing(client):
    # Cleanup never ran (crash/kill): the file the marker saw is still there.
    retention("until_complete")
    seed_result()
    paper = synthetic_paper(
        data_dir() / "tmp" / "job-job-viewer-1" / "submission_0_0_normalized.pdf")

    body = client.get("/api/results/res-viewer-1/pages").json()

    assert body["available"] is True
    assert body["source"] == "job_staging"
    assert body["page_count"] == 2
    assert paper.is_file()


def test_a_path_outside_the_data_dir_is_never_served(client, tmp_path):
    retention("days", 30)
    seed_result()
    outside = synthetic_paper(tmp_path / "outside" / "paper.pdf")
    retain("job-viewer-1", outside)

    body = client.get("/api/results/res-viewer-1/pages").json()

    assert body["available"] is False
    assert body["reason"] == "file_missing"
    image = client.get("/api/results/res-viewer-1/pages/1.png")
    assert image.status_code == 404


def test_image_submission_is_one_page_of_the_original_bytes(client):
    from PIL import Image

    retention("days", 30)
    seed_result()
    png = data_dir() / "retained" / "job-viewer-1" / "submission.png"
    png.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (60, 80), (250, 250, 250)).save(png)
    expected = png.read_bytes()
    retain("job-viewer-1", png)

    body = client.get("/api/results/res-viewer-1/pages").json()
    assert body["available"] is True
    assert body["file_kind"] == "image"
    assert body["page_count"] == 1

    first = client.get("/api/results/res-viewer-1/pages/1.png")
    assert first.status_code == 200
    assert first.content == expected

    second = client.get("/api/results/res-viewer-1/pages/2.png")
    assert second.status_code == 404
    assert second.json()["code"] == "page_out_of_range"
