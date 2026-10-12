import io
import time

import pytest
from fastapi.testclient import TestClient


def _pdf_bytes(pages: int = 1) -> bytes:
    """A REAL, readable PDF — routing must be able to read the page count."""
    from pypdf import PdfWriter
    writer = PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(width=200, height=200)
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


@pytest.fixture
def client(fresh_state, monkeypatch):
    from app.config import get_config, save_config
    cfg = get_config()
    cfg.mock_mode = True
    save_config(cfg)

    # Register a fake Gemini key so the key manager has material.
    from app.config import get_secrets
    get_secrets().set_gemini_keys(["AIzaMockKey1234567890abcdef"])

    from app.main import app
    with TestClient(app) as test_client:
        yield test_client


def test_dashboard_empty(client):
    from app.version import APP_VERSION
    data = client.get("/api/dashboard").json()
    assert data["version"] == APP_VERSION
    assert data["completed"] == 0
    assert data["api_health"]["total"] == 1
    assert data["role"] in ("teacher", "coteacher", "developer")


def test_manual_correction_end_to_end(client):
    """Upload PDFs → job runs through the pipeline in mock mode → result."""
    files = {
        "submission": ("student.pdf", io.BytesIO(_pdf_bytes()), "application/pdf"),
        "mark_scheme": ("scheme.pdf", io.BytesIO(_pdf_bytes()), "application/pdf"),
    }
    response = client.post("/api/corrections/manual",
                           files=files, data={"student_name": "Test Student"})
    assert response.status_code == 200, response.text
    job = response.json()
    job_id = job["id"]

    deadline = time.time() + 20
    status = None
    while time.time() < deadline:
        job = client.get(f"/api/jobs/{job_id}").json()
        status = job["status"]
        if status in ("COMPLETED", "FAILED", "CANCELLED"):
            break
        time.sleep(0.2)
    assert status == "COMPLETED", job

    result = client.get(f"/api/results/{job['result_id']}").json()
    assert result["student_name"] == "Test Student"
    assert result["earned"] == 9  # 2+4+3 from the mock marking payload
    assert result["available"] == 12
    # In mock mode the pipeline is exercised end-to-end; the marking model
    # recorded is whichever model name the legacy fallback chain picked first.
    assert result["marking_model"]
    verification = result["verification_json"]
    assert verification["verified"] is True
    # temp files must be gone (storage rule)
    import app.db as db
    pending = db.query("SELECT * FROM pending_manual_files")
    for row in pending:
        from pathlib import Path
        assert not Path(row["path"]).exists()


def test_key_endpoints_never_leak(client):
    data = client.get("/api/keys").json()
    import json
    text = json.dumps(data)
    assert "AIzaMockKey" not in text
    assert data["total"] == 1


def test_settings_roundtrip(client):
    """Ordinary settings round-trip; the role is NOT one of them."""
    before = client.get("/api/settings").json()["role"]
    data = client.post("/api/settings", json={"mock_mode": True}).json()
    assert data["mock_mode"] is True
    assert data["role"] == before          # unchanged by the request


def test_settings_cannot_change_the_role(client):
    """Roles belong to the account/permission system (§9/§59).

    Settings may READ the effective role but must never be able to grant one:
    a request that tries is refused outright and changes nothing.
    """
    before = client.get("/api/settings").json()["role"]
    response = client.post("/api/settings", json={"role": "developer"})
    assert response.status_code == 400
    assert response.json()["code"] == "role_managed_elsewhere"
    # The attempt left the effective role exactly as it was.
    assert client.get("/api/settings").json()["role"] == before
    assert client.get("/api/dashboard").json()["role"] == before


def test_diagnostics_requires_developer(client):
    from app.config import get_config
    response = client.get("/api/diagnostics")
    assert response.status_code == 403
    # Settings cannot unlock it — that is not how a role is granted.
    assert client.post("/api/settings",
                       json={"role": "developer"}).status_code == 400
    assert client.get("/api/diagnostics").status_code == 403
    # The role is provisioned by the account/config system instead.
    get_config().role = "developer"
    response = client.get("/api/diagnostics")
    assert response.status_code == 200
    assert "system" in response.json()
    assert "gemini" in response.json()


def test_correct_all_requires_allowlist(client, fresh_state):
    import app.db as db
    from datetime import time as _t
    db.execute(
        "INSERT INTO courses(id, name, fetched_at) VALUES('c1','Maths',?)", (time.time(),))
    db.execute(
        "INSERT INTO coursework(id, course_id, title, fetched_at, allowlisted) "
        "VALUES('w1','c1','HW 1',?,0)", (time.time(),))
    response = client.post("/api/classroom/courses/c1/assignments/w1/correct-all")
    assert response.status_code == 403
    body = response.json()
    assert "allowlisted" in body["message"].lower() or "allow" in body["message"].lower()


def test_spa_fallback_serves_index_or_503(client):
    response = client.get("/")
    assert response.status_code in (200, 503)


def test_classroom_requires_connection(client):
    response = client.get("/api/classroom/courses")
    assert response.status_code == 400


@pytest.fixture
def client_demo_no_keys(fresh_state, monkeypatch):
    """Demo-mode client: mock mode in MEMORY ONLY (exactly like --demo),
    with no API keys configured and mock_mode NOT written to config.toml."""
    from app.config import get_config
    cfg = get_config()
    cfg.mock_mode = True

    from app.main import app
    with TestClient(app) as test_client:
        yield test_client


def test_mock_correction_works_without_any_api_key(client_demo_no_keys):
    """Regression: the documented key-free demo must be able to correct.

    The worker child is a separate interpreter that re-reads config.toml; if
    mock mode only lives in the parent's memory (as `--demo` sets it), the
    child used to try real Gemini and fail with "No usable Gemini API keys"."""
    files = {
        "submission": ("student.pdf", io.BytesIO(_pdf_bytes()), "application/pdf"),
        "mark_scheme": ("scheme.pdf", io.BytesIO(_pdf_bytes()), "application/pdf"),
    }
    response = client_demo_no_keys.post(
        "/api/corrections/manual", files=files, data={"student_name": "No Key"})
    assert response.status_code == 200, response.text
    job_id = response.json()["id"]

    deadline = time.time() + 30
    job = None
    while time.time() < deadline:
        job = client_demo_no_keys.get(f"/api/jobs/{job_id}").json()
        if job["status"] in ("COMPLETED", "FAILED", "CANCELLED"):
            break
        time.sleep(0.2)
    assert job and job["status"] == "COMPLETED", job

    result = client_demo_no_keys.get(f"/api/results/{job['result_id']}").json()
    assert result["earned"] == 9 and result["available"] == 12


def test_jobs_stream_route_is_registered_before_job_detail(client):
    """Regression: a literal path must precede a parameterised sibling.

    "/jobs/stream" was declared AFTER "/jobs/{job_id}", so FastAPI parsed
    "stream" as a job id and the live-activity SSE endpoint answered 404 —
    silently breaking the dashboard's real-time updates. OpenAPI preserves
    registration order, which is the order FastAPI matches routes in.

    (A raw request is deliberately avoided here: the SSE body is an open
    stream, so a buffering test client would never finish reading it.)"""
    paths = list(client.get("/openapi.json").json()["paths"].keys())
    assert "/api/jobs/stream" in paths
    assert paths.index("/api/jobs/stream") < paths.index("/api/jobs/{job_id}")

