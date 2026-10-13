"""Privacy + Demo Mode tests.

Locks two guarantees: a factory reset removes local data but keeps the app, and
a diagnostic export never contains keys, tokens, student names or submissions.
"""

from __future__ import annotations

import json

from fastapi.testclient import TestClient


def test_factory_reset_requires_confirmation(fresh_state):
    from app.main import app

    with TestClient(app) as client:
        r = client.post("/api/privacy/factory-reset", json={})
    assert r.status_code == 400
    assert r.json()["code"] == "confirmation_required"


def test_factory_reset_removes_local_data(fresh_state):
    from app import config, db
    from app.main import app

    data_dir = fresh_state
    (data_dir / "config.toml").write_text("[app]\nrole = \"developer\"\n", encoding="utf-8")
    (data_dir / "secrets.toml").write_text('[gemini]\napi_keys = ["AIzaSyFAKEKEYVALUE1234567890abcdefghij"]\n',
                                           encoding="utf-8")
    (data_dir / "logs").mkdir(exist_ok=True)
    (data_dir / "logs" / "app.log").write_text("hello\n", encoding="utf-8")
    db.set_meta("probe", "value")

    with TestClient(app) as client:
        r = client.post("/api/privacy/factory-reset", json={"confirm": "RESET"})
    body = r.json()
    assert r.status_code == 200 and body["reset"] is True
    assert "secrets.toml" in body["removed"]
    assert "app.db" in body["removed"]
    # the application itself is untouched
    assert (data_dir / "secrets").exists()
    # config reloads as a fresh install
    assert config.get_config().is_first_run() is True


def test_factory_reset_keeps_db_usable_for_other_threads(fresh_state):
    """Regression: close_all() must not leave another thread holding a closed
    database handle. Before the fix, the thread that had served a request
    before the reset raised ProgrammingError on its next use and the pool /
    any endpoint landing on it returned HTTP 500."""
    import threading

    from fastapi.testclient import TestClient

    from app import db
    from app.main import app

    ready = threading.Event()
    proceed = threading.Event()
    result: dict = {}

    def worker():
        db.query("SELECT 1")           # bind a connection to THIS thread
        ready.set()
        proceed.wait(timeout=10)
        try:
            db.execute(
                "INSERT INTO app_meta(key, value) VALUES('post_reset','1') "
                "ON CONFLICT(key) DO UPDATE SET value = '1'")
            result["value"] = db.get_meta("post_reset")
        except Exception as exc:  # noqa: BLE001
            result["error"] = f"{type(exc).__name__}: {exc}"

    with TestClient(app) as client:
        t = threading.Thread(target=worker, daemon=True)
        t.start()
        assert ready.wait(10), "worker thread never opened its connection"
        r = client.post("/api/privacy/factory-reset", json={"confirm": "RESET"})
        assert r.status_code == 200
        proceed.set()
        t.join(timeout=10)

    assert "error" not in result, result.get("error")
    assert result.get("value") == "1"


def test_factory_reset_leaves_app_endpoints_working(fresh_state):
    """Regression: after a reset the running process must be immediately
    usable again and report a clean first-run state (no restart needed)."""
    from fastapi.testclient import TestClient

    from app.main import app

    from app import db

    # a logs dir present at reset time must be removed, not left as a failure
    (fresh_state / "logs").mkdir(exist_ok=True)
    (fresh_state / "logs" / "app.log").write_text("hello\n", encoding="utf-8")
    # real data that MUST be gone after the reset (a locked app.db used to
    # survive deletion, leaving every one of these rows behind)
    db.execute("INSERT INTO courses(id, name, state, fetched_at) "
               "VALUES('c1','Real Class','ACTIVE',1)")

    with TestClient(app) as client:
        r = client.post("/api/privacy/factory-reset", json={"confirm": "RESET"})
        body = r.json()
        assert r.status_code == 200 and body["reset"] is True
        assert "logs" in body["removed"], body["failures"]
        assert "app.db" in body["removed"], body["failures"]
        # the database is fresh: no user rows survive the reset
        assert db.query_one("SELECT COUNT(*) AS n FROM courses")["n"] == 0
        for path in ("/api/setup/status", "/api/dashboard", "/api/results",
                     "/api/privacy/info", "/api/attention", "/api/jobs"):
            resp = client.get(path)
            assert resp.status_code == 200, f"{path} -> {resp.status_code} {resp.text[:200]}"
        assert client.get("/api/dashboard").json()["first_run"] is True


def test_reset_logging_releases_and_rearms_handlers(fresh_state):
    """reset_logging() must close every handler (so Windows can delete the log
    file) and allow setup_logging() to re-attach fresh ones afterwards."""
    import logging

    from app import logging_setup

    logging_setup.setup_logging()
    assert logging.getLogger().handlers
    logging_setup.reset_logging()
    assert logging.getLogger().handlers == []
    logging_setup.setup_logging()
    assert logging.getLogger().handlers


def test_diagnostic_export_is_sanitized(fresh_state):
    from app import config, db
    from app.main import app

    # Plant material that MUST NOT appear in an export.
    secrets = config.get_secrets()
    secrets.set_gemini_keys(["AIzaSySUPERSECRETKEY1234567890abcdefghij"])
    db.execute(
        "INSERT INTO courses(id, name, state, fetched_at) VALUES('c1','Real Class','ACTIVE',1)")
    db.execute(
        "INSERT INTO coursework(id, course_id, title, allowlisted, fetched_at) "
        "VALUES('w1','c1','Real Assignment',1,1)")
    db.execute(
        "INSERT INTO correction_jobs(id, coursework_id, student_name, status, "
        "progress, attempts, max_attempts, error_code, created_at, updated_at) "
        "VALUES('j1','w1','Jane RealStudent','FAILED',0,3,3,'rate_limit',1,1)")

    with TestClient(app) as client:
        r = client.get("/api/privacy/diagnostic-export")
    assert r.status_code == 200
    text = r.text
    assert "AIzaSySUPERSECRETKEY" not in text
    assert "Jane RealStudent" not in text
    assert "Real Assignment" not in text
    payload = json.loads(text)
    assert payload["build_number"]
    assert payload["integrations"]["ai_keys_configured"] is True
    assert payload["error_codes"].get("rate_limit") == 1


def test_demo_activation_isolates_data_dir(monkeypatch, tmp_path):
    import os

    from app import demo

    base = tmp_path / "base"
    activated = demo.activate(str(base))
    try:
        assert activated == str(base / "demo")
        assert os.environ["MC5_DATA_DIR"] == str(base / "demo")
        assert demo.is_demo() is True
    finally:
        os.environ.pop(demo.DEMO_ENV, None)
        os.environ.pop("MC5_DATA_DIR", None)


def test_demo_seed_creates_synthetic_data(fresh_state):
    from app import db
    from app import demo

    summary = demo.seed()
    assert summary["seeded"] is True
    names = {r["name"] for r in db.query("SELECT name FROM students")}
    assert names == {"Student A", "Student B", "Student C", "Student D", "Student E"}
    results = db.query_one("SELECT COUNT(*) AS n FROM results")["n"]
    assert results > 0
    # idempotent
    assert demo.seed()["seeded"] is False


def test_demo_blocks_real_classroom(monkeypatch):
    import os

    from app import demo

    monkeypatch.setenv(demo.DEMO_ENV, "1")
    import pytest
    with pytest.raises(demo.DemoClassroomBlocked):
        demo.guard_classroom()
    # and passes cleanly when demo is off
    os.environ.pop(demo.DEMO_ENV, None)
    demo.guard_classroom()


def test_demo_mode_opens_without_a_sign_in(monkeypatch):
    """Demo Mode is the no-setup showcase, so it must not demand an account.

    It runs on this computer only, in its own data directory, with synthetic
    students and Google Classroom hard-disabled - there is nothing behind the
    gate to protect, and asking for a password there defeats the whole point of
    the Start-menu shortcut. An explicit request for a local sign-in is still
    honoured, and public access still always requires one.
    """
    from app import auth_gate as ag
    from app import demo

    monkeypatch.delenv("MC5_LOGIN_REQUIRED", raising=False)
    monkeypatch.delenv("MC5_PUBLIC", raising=False)
    monkeypatch.setenv(demo.DEMO_ENV, "1")
    assert ag.login_required_enabled() is False
    assert ag.gate_enabled() is False

    # An explicit MC5_LOGIN_REQUIRED=1 is an instruction, not a default.
    monkeypatch.setenv("MC5_LOGIN_REQUIRED", "1")
    assert ag.login_required_enabled() is True
    assert ag.gate_enabled() is True

    # Public access keeps the gate up whatever mode is running.
    monkeypatch.delenv("MC5_LOGIN_REQUIRED", raising=False)
    monkeypatch.setenv("MC5_PUBLIC", "1")
    assert ag.gate_enabled() is True


def test_demo_activation_is_idempotent_for_the_supervised_backend(monkeypatch):
    """The on-demand relay starts the backend as a child process that inherits
    demo mode AND the already-demo data directory, then runs ``activate`` again.

    Appending ``"demo"`` a second time moved the app to ``<base>/demo/demo``
    while the launcher, ``--stop`` and the packaged verifier all use
    ``<base>/demo`` - so the demo database, its credentials and the mock engine
    were not the ones the running app used.
    """
    import os

    from app import demo

    # ``activate`` writes these two environment variables itself, so restore
    # them explicitly: leaving MC5_DEMO=1 behind would put every test that runs
    # afterwards into demo mode (Classroom disabled), which is a whole-suite
    # failure caused by one test's leftovers.
    monkeypatch.delenv(demo.DEMO_ENV, raising=False)
    monkeypatch.delenv("MC5_DATA_DIR", raising=False)
    try:
        first = demo.activate("/tmp/mc5-demo-base").replace("\\", "/")
        assert first.endswith("/tmp/mc5-demo-base/demo")
        assert demo.is_demo()

        # What the supervisor's child sees: demo already active, data dir
        # already the demo directory. Activating must not nest a second
        # "demo" level.
        again = demo.activate(os.environ["MC5_DATA_DIR"]).replace("\\", "/")
        assert again == first
        assert "/demo/demo" not in again
    finally:
        os.environ.pop(demo.DEMO_ENV, None)
        os.environ.pop("MC5_DATA_DIR", None)
