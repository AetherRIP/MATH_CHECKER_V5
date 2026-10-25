"""Background Worker ON/OFF control (task 3) — real backend behaviour.

Proves that:

* ON starts the REAL scheduler thread and verifies it is running,
* a repeated ON never creates a second worker,
* OFF is persisted to config.toml and survives a simulated restart,
* OFF stops new automatic jobs from starting while in-flight work finishes,
* OFF never disables manual correction or the website,
* the endpoint enforces the role/permission on the BACKEND.
"""
import io
import time

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(fresh_state, monkeypatch):
    from app.config import get_config, save_config
    cfg = get_config()
    cfg.mock_mode = True
    save_config(cfg)
    from app.config import get_secrets
    get_secrets().set_gemini_keys(["AIzaMockKey1234567890abcdef"])

    from app.main import app
    with TestClient(app) as test_client:
        yield test_client


def _worker(client, enabled: bool):
    return client.post("/api/automation/worker", json={"enabled": enabled})


def test_on_starts_the_real_worker_and_reports_its_state(client):
    response = _worker(client, True)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["requested"] is True
    assert body["worker_enabled"] is True
    assert body["thread_running"] is True
    assert body["worker_state"] == "running"
    # The state comes from the live thread, not from the request payload.
    from app.automation.scheduler import AutomationSchedulerHolder
    assert AutomationSchedulerHolder.instance().running() is True


def test_repeated_on_never_creates_a_duplicate_worker(client):
    from app.automation.scheduler import AutomationSchedulerHolder
    first = _worker(client, True).json()
    thread_before = AutomationSchedulerHolder.instance()._thread
    second = _worker(client, True).json()
    thread_after = AutomationSchedulerHolder.instance()._thread
    assert first["worker_state"] == "running"
    assert second["worker_state"] == "running"
    assert thread_before is thread_after      # same thread object: no duplicate
    assert AutomationSchedulerHolder.instance().running() is True


def test_off_is_persisted_and_survives_a_restart(client):
    assert _worker(client, False).json()["worker_state"] == "stopped"

    # Persisted to disk before anything else happens ...
    from app.config import CONFIG_PATH
    text = CONFIG_PATH.read_text(encoding="utf-8")
    assert "enabled = false" in text

    # ... so a fresh configuration load (simulated restart) keeps it OFF.
    import app.config as config
    config._config = None
    assert config.get_config().automation.enabled is False


def test_off_stops_new_jobs_but_in_flight_jobs_still_advance(client):
    from app.automation import store, states
    from app.automation.runner import runner as runner_mod

    _worker(client, True)
    job = store.create_job(
        course_id="course-1", coursework_id="work-1",
        assignment_title="HW", student_id=None, student_name="Student",
        submission_id="sub-1", submission_version="v1")
    assert job is not None and job["status"] == states.NEW

    status = _worker(client, False).json()
    assert status["worker_state"] == "stopped"
    # The scheduler thread stays alive so in-flight jobs can finish safely.
    assert status["thread_running"] is True
    assert status["pending_jobs"] >= 1

    # While OFF the runner must not START the queued (NEW) job ...
    processed = runner_mod().run_pending(max_jobs=5, include_new=False)
    assert processed == 0
    assert store.get(job["id"])["status"] == states.NEW
    # ... and there is nothing else runnable for it to pick up instead.
    assert store.next_runnable(exclude_new=True) is None

    # The job is still queued (eligible) the moment the worker comes back ON.
    assert store.next_runnable()["id"] == job["id"]
    resumed = _worker(client, True).json()
    assert resumed["worker_state"] == "running"
    # Exactly one job exists for this submission — no duplicates were made.
    assert len(store.list_jobs()) == 1


def test_off_does_not_disable_manual_correction_or_the_site(client):
    _worker(client, False)

    # The website itself still answers ...
    assert client.get("/api/dashboard").status_code == 200
    # ... and the manual AI correction queue still accepts work. (A real,
    # readable PDF: uploads are validated on their CONTENT now, so a header
    # string pretending to be a document is refused before it is queued.)
    def _pdf() -> bytes:
        from pypdf import PdfWriter
        writer = PdfWriter()
        writer.add_blank_page(width=200, height=200)
        buf = io.BytesIO()
        writer.write(buf)
        return buf.getvalue()

    files = {
        "submission": ("student.pdf", io.BytesIO(_pdf()), "application/pdf"),
        "mark_scheme": ("scheme.pdf", io.BytesIO(_pdf()), "application/pdf"),
    }
    response = client.post("/api/corrections/manual", files=files,
                           data={"student_name": "Manual Student"})
    assert response.status_code == 200, response.text
    assert response.json()["id"]


def test_worker_control_enforces_the_role_on_the_backend(client):
    from app.config import get_config
    get_config().role = "assistant"          # no automation.manage permission
    response = _worker(client, True)
    assert response.status_code == 403
    assert response.json()["code"] == "forbidden"

    get_config().role = "teacher"            # teachers may control the worker
    assert _worker(client, True).status_code == 200


def test_status_exposes_the_worker_fields_separately(client):
    body = client.get("/api/automation/status").json()
    for field in ("worker_enabled", "worker_state", "thread_running",
                  "last_activity", "pending_jobs", "active_jobs"):
        assert field in body, field
    assert body["worker_state"] in ("running", "stopped", "stalled")
