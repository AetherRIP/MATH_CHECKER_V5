"""API tests for the marking-workspace viewer + dashboard analytics (§UI).

The viewer's contract is honesty: it serves the ACTUAL stored submission page,
or says the original is not stored. It must never 500 on a purged original,
never read outside the data dir, and its analytics must report only what the
stored rows support.
"""

from __future__ import annotations

import time

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from app import db
from app.api import analytics_routes, viewer_routes
from app.errors import AppError


def _client() -> TestClient:
    app = FastAPI()
    viewer_routes.register(app)
    analytics_routes.register(app)

    @app.exception_handler(AppError)
    async def _handler(request, exc: AppError):
        return JSONResponse(status_code=exc.status, content=exc.to_dict())

    return TestClient(app)


def _make_pdf(pages: int = 2) -> bytes:
    import pymupdf
    doc = pymupdf.open()
    for i in range(pages):
        page = doc.new_page()
        page.insert_text((72, 720), f"Student work page {i + 1}")
    data = doc.tobytes()
    doc.close()
    return data


def _make_png() -> bytes:
    import io
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (40, 40), (200, 210, 255)).save(buf, format="PNG")
    return buf.getvalue()


def _insert_result(result_id: str, job_id: str, *, created_at: float,
                   percentage: float = 75.0, review: int = 0,
                   override: int = 0, processing: float | None = None,
                   coursework_id: str | None = None) -> None:
    with db.transaction() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO correction_jobs"
            "(id, status, created_at, updated_at) VALUES(?,?,?,?)",
            (job_id, "COMPLETED", created_at, created_at))
        conn.execute(
            "INSERT OR REPLACE INTO results"
            "(id, job_id, coursework_id, earned, available, percentage, "
            " provisional_grade, marking_model, result_json, "
            " processing_seconds, review_required, manual_override, created_at)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (result_id, job_id, coursework_id, 15, 20, percentage, "7",
             "test-model", '{"questions": []}', processing, review, override,
             created_at))


def test_pages_reports_retained_original(tmp_path):
    data_dir = tmp_path / "data"
    (data_dir / "retained" / "job-1").mkdir(parents=True)
    pdf = data_dir / "retained" / "job-1" / "submission.pdf"
    pdf.write_bytes(_make_pdf(2))

    with db.transaction() as conn:
        conn.execute(
            "INSERT INTO retained_files(job_id, path, kind, filename, "
            "retain_until, created_at) VALUES(?,?,?,?,?,?)",
            ("job-1", str(pdf), "submission", "submission.pdf", 0.0, 1.0))
    _insert_result("r-1", "job-1", created_at=time.time())

    r = _client().get("/api/results/r-1/pages")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["available"] is True
    assert body["page_count"] == 2
    assert body["file_kind"] == "pdf"
    assert body["source"] == "retained"
    assert body["file_name"] == "submission.pdf"


def _set_retention(mode: str, days: int = 30) -> None:
    from app.config import get_config, save_config
    cfg = get_config()
    cfg.retention.keep_submissions = mode
    cfg.retention.keep_days = days
    save_config(cfg)


def test_a_classroom_paper_stays_viewable_after_marking_when_retention_is_on(
        tmp_path):
    """Acceptance: retention ON -> the original survives job cleanup.

    A Classroom job never registers a pending_manual_files row; its only local
    copy is the staged PDF under data/tmp/job-<id>/.
    """
    _set_retention("days", 30)
    data_dir = tmp_path / "data"
    job_dir = data_dir / "tmp" / "job-keep"
    job_dir.mkdir(parents=True)
    (job_dir / "submission_0_0_ab12_normalized.pdf").write_bytes(_make_pdf(3))
    _insert_result("r-keep", "keep", created_at=time.time())

    from app.engine.service import cleanup_job_files
    cleanup_job_files("keep")             # the real terminal cleanup

    client = _client()
    body = client.get("/api/results/r-keep/pages").json()
    assert body["available"] is True, body
    assert body["reason"] is None
    assert body["source"] == "retained"
    assert body["page_count"] == 3
    assert body["retention"]["keeps"] is True
    page = client.get("/api/results/r-keep/pages/1.png")
    assert page.status_code == 200
    assert page.content.startswith(b"\x89PNG")


def test_pages_with_retention_off_says_so():
    _set_retention("until_complete")
    _insert_result("r-2", "job-2", created_at=time.time())
    r = _client().get("/api/results/r-2/pages")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["available"] is False
    assert body["reason"] == "retention_disabled"
    assert body["page_count"] == 0
    assert body["retention"]["keeps"] is False
    assert "retention is off" in body["message"].lower()


def test_pages_with_retention_on_never_claims_the_setting_deleted_it():
    """Retention ON but no stored copy: a genuine storage gap, not "disabled"."""
    _set_retention("days", 30)
    _insert_result("r-2b", "job-2b", created_at=time.time())
    body = _client().get("/api/results/r-2b/pages").json()
    assert body["available"] is False
    assert body["reason"] == "file_missing"
    assert body["retention"]["keeps"] is True
    assert "retention is on" in body["message"].lower()
    assert "deleted when a correction finishes" not in body["message"]
    # A legacy result (marked before retention existed) gets this explanation.
    assert "before retention was enabled" in body["message"]
    assert body["retryable"] is True


def test_pages_while_the_job_is_still_running_is_a_processing_state():
    _set_retention("always")
    _insert_result("r-run", "job-run", created_at=time.time())
    db.execute("UPDATE correction_jobs SET status = 'MARKING' WHERE id = 'job-run'")
    body = _client().get("/api/results/r-run/pages").json()
    assert body["available"] is False
    assert body["reason"] == "processing"
    assert body["job_status"] == "MARKING"
    assert "still being processed" in body["message"]


def test_a_retained_copy_that_cannot_be_read_is_not_reported_available(tmp_path):
    data_dir = tmp_path / "data"
    (data_dir / "retained" / "job-bad").mkdir(parents=True)
    broken = data_dir / "retained" / "job-bad" / "broken.pdf"
    broken.write_bytes(b"not a pdf at all")
    with db.transaction() as conn:
        conn.execute(
            "INSERT INTO retained_files(job_id, path, kind, filename, "
            "retain_until, created_at) VALUES(?,?,?,?,?,?)",
            ("job-bad", str(broken), "submission", "broken.pdf", 0.0, 1.0))
    _insert_result("r-bad", "job-bad", created_at=time.time())
    body = _client().get("/api/results/r-bad/pages").json()
    assert body["available"] is False
    assert body["reason"] == "file_unreadable"


def test_pages_unknown_result_is_404():
    r = _client().get("/api/results/nope/pages")
    assert r.status_code == 404
    assert r.json()["code"] == "result_not_found"


def test_page_image_renders_png_and_revalidates(tmp_path):
    data_dir = tmp_path / "data"
    (data_dir / "retained" / "job-3").mkdir(parents=True)
    pdf = data_dir / "retained" / "job-3" / "sub.pdf"
    pdf.write_bytes(_make_pdf(2))
    with db.transaction() as conn:
        conn.execute(
            "INSERT INTO retained_files(job_id, path, kind, filename, "
            "retain_until, created_at) VALUES(?,?,?,?,?,?)",
            ("job-3", str(pdf), "submission", "sub.pdf", 0.0, 1.0))
    _insert_result("r-3", "job-3", created_at=time.time())

    client = _client()
    r = client.get("/api/results/r-3/pages/1.png")
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("image/png")
    assert r.content.startswith(b"\x89PNG")

    etag = r.headers["etag"]
    r2 = client.get("/api/results/r-3/pages/1.png",
                    headers={"If-None-Match": etag})
    assert r2.status_code == 304
    assert r2.content == b""

    out = client.get("/api/results/r-3/pages/9.png")
    assert out.status_code == 404
    assert out.json()["code"] == "page_out_of_range"


def test_viewer_refuses_paths_outside_the_data_dir(tmp_path):
    # A row pointing OUTSIDE the data dir must be ignored (containment).
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    evil = outside / "secret.pdf"
    evil.write_bytes(_make_pdf(1))
    with db.transaction() as conn:
        conn.execute(
            "INSERT INTO retained_files(job_id, path, kind, filename, "
            "retain_until, created_at) VALUES(?,?,?,?,?,?)",
            ("job-4", str(evil), "submission", "secret.pdf", 0.0, 1.0))
    _insert_result("r-4", "job-4", created_at=time.time())

    r = _client().get("/api/results/r-4/pages")
    assert r.status_code == 200, r.text
    assert r.json()["available"] is False
    img = _client().get("/api/results/r-4/pages/1.png")
    assert img.status_code == 404
    assert img.json()["code"] == "original_not_stored"


def test_staged_image_submission_is_served_as_one_page(tmp_path):
    data_dir = tmp_path / "data"
    (data_dir / "tmp").mkdir(parents=True)
    png = data_dir / "tmp" / "scan.png"
    png.write_bytes(_make_png())
    with db.transaction() as conn:
        conn.execute(
            "INSERT INTO pending_manual_files(job_id, kind, path, filename) "
            "VALUES(?,?,?,?)", ("job-5", "submission", str(png), "scan.png"))
    _insert_result("r-5", "job-5", created_at=time.time())

    client = _client()
    body = client.get("/api/results/r-5/pages").json()
    assert body["available"] is True
    assert body["file_kind"] == "image"
    assert body["page_count"] == 1
    r = client.get("/api/results/r-5/pages/1.png")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("image/png")
    assert r.content.startswith(b"\x89PNG")


def test_activity_buckets_and_clamps():
    now = time.time()
    yesterday = now - 86400
    _insert_result("a-1", "aj-1", created_at=now, percentage=80.0, review=1,
                   processing=10.0)
    _insert_result("a-2", "aj-2", created_at=now, percentage=60.0,
                   processing=20.0)
    _insert_result("a-3", "aj-3", created_at=yesterday, percentage=50.0)
    with db.transaction() as conn:
        conn.execute(
            "INSERT INTO correction_jobs(id, status, created_at, updated_at) "
            "VALUES(?,?,?,?)", ("aj-fail", "FAILED", now, now))

    client = _client()
    r = client.get("/api/analytics/activity?days=2")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["window_days"] == 2
    today, prev = body["days"][-1], body["days"][-2]
    assert today["results"] == 2
    assert today["needs_review"] == 1
    assert today["jobs_failed"] == 1
    assert abs(today["avg_percentage"] - 70.0) < 0.01
    assert abs(today["avg_processing_seconds"] - 15.0) < 0.01
    assert prev["results"] == 1
    assert body["totals"]["results"] == 3

    # A wild days value is clamped, not an error and not a huge scan.
    r2 = client.get("/api/analytics/activity?days=9999")
    assert r2.status_code == 200
    assert r2.json()["window_days"] == 365


def test_class_trend_orders_by_first_result():
    now = time.time()
    with db.transaction() as conn:
        conn.execute(
            "INSERT INTO courses(id, name, fetched_at) VALUES(?,?,?)",
            ("c-1", "10B", now))
        conn.execute(
            "INSERT INTO coursework(id, course_id, title, fetched_at) "
            "VALUES(?,?,?,?)", ("cw-1", "c-1", "Algebra", now))
        conn.execute(
            "INSERT INTO coursework(id, course_id, title, fetched_at) "
            "VALUES(?,?,?,?)", ("cw-2", "c-1", "Geometry", now))
    # cw-2 finished BEFORE cw-1 chronologically -> trend must start at cw-2.
    _insert_result("t-1", "tj-1", created_at=now, percentage=70.0,
                   coursework_id="cw-1")
    _insert_result("t-2", "tj-2", created_at=now, percentage=90.0,
                   coursework_id="cw-1")
    _insert_result("t-3", "tj-3", created_at=now - 86400, percentage=50.0,
                   coursework_id="cw-2")

    r = _client().get("/api/analytics/class/c-1/trend")
    assert r.status_code == 200, r.text
    body = r.json()
    ids = [p["coursework_id"] for p in body["assignments"]]
    assert ids == ["cw-2", "cw-1"]
    first, second = body["assignments"]
    assert first["title"] == "Geometry"
    assert first["checked"] == 1
    assert first["average"] == 50.0
    assert second["checked"] == 2
    assert second["average"] == 80.0
    # overall = (50*1 + 80*2) / 3
    assert abs(body["overall_average"] - 70.0) < 0.01
