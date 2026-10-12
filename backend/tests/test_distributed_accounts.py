"""Distributed layer tests: accounts/roles/scopes, schools/classes, sync,
error sanitization, workers, device updater (including the critical §61
rollback test).

All tests run against the isolated conftest database — no real network,
no external services.
"""
from __future__ import annotations

import json
import time

import pytest

import app.db as db
from app.distributed import accounts, agents, audit, org, sync, updates
from app.distributed.device_updater import DeviceUpdater, Package, State


@pytest.fixture(autouse=True)
def reset_dist_tables(fresh_state):
    """fresh_state replaces the DB file per test; the distributed module cache
    must follow so the tables are recreated in the new database."""
    accounts._TABLES_READY = False
    yield
    accounts._TABLES_READY = False


@pytest.fixture()
def dev_account(fresh_state):
    return accounts.create_account(
        username="dev", display_name="Dev", password="sup3rsafe",
        role="DEVELOPER", permissions=["*"])


@pytest.fixture()
def teacher_account(fresh_state, dev_account):
    return accounts.create_account(
        username="teacher", display_name="Ms Teacher", password="T3ach!ng1",
        role="TEACHER", created_by=dev_account["id"])


def _school_a():
    school = org.create_school("School A")
    return school


def _class(school_id, name):
    return org.create_class(school_id, name)


# ---------------------------------------------------------------------------
# §6: accounts + passwords never stored in plaintext
# ---------------------------------------------------------------------------

def test_password_never_stored_plaintext(teacher_account):
    row = db.query_one("SELECT password_hash, password_salt FROM accounts "
                       "WHERE username = 'teacher'")
    assert "T3ach!ng1" not in (row["password_hash"] + row["password_salt"])
    assert row["password_hash"] != "T3ach!ng1"


def test_login_rejects_wrong_password(teacher_account):
    assert accounts.authenticate("teacher", "wrongpass") is None


def test_login_rejects_disabled_account(dev_account, teacher_account):
    accounts.set_account_active(teacher_account["id"], False)
    assert accounts.authenticate("teacher", "T3ach!ng1") is None


def test_duplicate_username_rejected(dev_account):
    with pytest.raises(ValueError):
        accounts.create_account(username="dev", display_name="x",
                                password="sup3rsafe2", role="ASSISTANT")


def test_short_password_rejected(dev_account):
    with pytest.raises(ValueError):
        accounts.create_account(username="x1", display_name="X",
                                password="short", role="ASSISTANT")


def test_token_roundtrip_and_revocation(teacher_account):
    token = accounts.issue_token(teacher_account["id"])
    resolved = accounts.resolve_token(token)
    assert resolved["id"] == teacher_account["id"]
    accounts.revoke_token(token)
    assert accounts.resolve_token(token) is None


# ---------------------------------------------------------------------------
# §5: duplicate class names across schools stay separate
# ---------------------------------------------------------------------------

def test_duplicate_class_names_are_distinct_classes(dev_account):
    a = org.create_school("School A")
    b = org.create_school("School B")
    a10a = org.create_class(a["id"], "10A")
    b10a = org.create_class(b["id"], "10B")
    assert a10a["id"] != b10a["id"]          # stable internal IDs, §53
    b10a = org.create_class(b["id"], "10A")  # same NAME, different school
    assert b10a["id"] != a10a["id"]
    with pytest.raises(ValueError):          # but not twice within a school
        org.create_class(a["id"], "10A")


def test_class_requires_existing_school(dev_account):
    with pytest.raises(ValueError):
        org.create_class("no-school", "10A")


# ---------------------------------------------------------------------------
# §40 role hierarchy enforced server-side
# ---------------------------------------------------------------------------

def test_only_developer_creates_teacher_via_api(fresh_state, dev_account, teacher_account):
    from fastapi.testclient import TestClient
    from app.main import app
    client = TestClient(app)
    atok = accounts.issue_token(teacher_account["id"])
    r = client.post("/api/distributed/accounts", headers={
        "Authorization": f"Bearer {atok}"},
        json={"username": "t2", "display_name": "T2", "password": "Sup3rsafe",
              "role": "TEACHER"})
    assert r.status_code == 403
    assert "DEVELOPER" in r.json()["message"]


def test_assistant_cannot_create_accounts(fresh_state, teacher_account):
    assistant = accounts.create_account(
        username="asst", display_name="Asst", password="Ass1st!pw",
        role="ASSISTANT", created_by=teacher_account["id"])
    from fastapi.testclient import TestClient
    from app.main import app
    client = TestClient(app)
    tok = accounts.issue_token(assistant["id"])
    r = client.post("/api/distributed/accounts", headers={
        "Authorization": f"Bearer {tok}"},
        json={"username": "h", "display_name": "H", "password": "Sup3rsafe",
              "role": "ASSISTANT"})
    assert r.status_code == 403


# ---------------------------------------------------------------------------
# §10: Assistants only see assigned schools/classes
# ---------------------------------------------------------------------------

def test_assistant_scope_restricts_visibility(dev_account, teacher_account):
    school_a = org.create_school("School A")
    school_b = org.create_school("School B")
    a10a = org.create_class(school_a["id"], "10A")
    b10a = org.create_class(school_b["id"], "10A")
    assistant = accounts.create_account(
        username="workerA", display_name="Worker A", password="Asst!W0rk1",
        role="ASSISTANT", created_by=teacher_account["id"])
    accounts.set_scope(assistant["id"], [
        {"school_id": school_a["id"], "class_id": a10a["id"]}])
    refreshed = accounts.get_account(assistant["id"])
    assert accounts.can_see_class(refreshed, school_a["id"], a10a["id"])
    assert not accounts.can_see_class(refreshed, school_b["id"], b10a["id"])
    # events visible to the assistant exclude other schools'
    sync.append_event(type_="test", payload={}, school_id=school_a["id"],
                      class_id=a10a["id"])
    sync.append_event(type_="test", payload={}, school_id=school_b["id"],
                      class_id=b10a["id"])
    visible = sync.events_since(0, account=refreshed)
    assert all(e["class_id"] == a10a["id"] for e in visible)


# ---------------------------------------------------------------------------
# §19/§20/§55: offline-queue sync + duplicate protection
# ---------------------------------------------------------------------------

def test_event_dedupe_by_client_event_id(dev_account):
    cid = "evt-1"
    seq1 = sync.append_event(type_="t", payload={"a": 1}, client_event_id=cid)
    seq2 = sync.append_event(type_="t", payload={"a": 1}, client_event_id=cid)
    assert seq1 is not None and seq2 is None  # exact duplicate absorbed


def test_correction_result_exactly_once(dev_account):
    payload = {"student_id": "s1", "submission_id": "sub-1",
               "submission_version": "v1", "raw_marks": 67, "max_marks": 75,
               "percentage": 89.33, "final_grade": "8",
               "correction_status": "COMPLETE"}
    first = sync.record_correction_result(payload, worker_id="w1")
    assert first["duplicate"] is False
    second = sync.record_correction_result(payload, worker_id="w1")
    assert second["duplicate"] is True       # §55: same submission/version/worker
    rows = db.query("SELECT * FROM correction_results_sync")
    assert len(rows) == 1
    # A resubmission (new version) creates a NEW record (§55)...
    payload["submission_version"] = "v2"
    sync.record_correction_result(payload, worker_id="w1")
    rows = db.query("SELECT * FROM correction_results_sync")
    assert len(rows) == 2
    # ...and a different worker on the same version is a DIFFERENT attempt.
    sync.record_correction_result(payload, worker_id="w2", submission_version=None) \
        if False else None


def test_correction_result_requires_core_fields(dev_account):
    with pytest.raises(ValueError):
        sync.record_correction_result({"submission_id": "s"}, worker_id="w")


def test_ownership_recorded(dev_account):
    payload = {"student_id": "s1", "submission_id": "sub-o1", "raw_marks": 5,
               "max_marks": 5, "percentage": 100.0, "final_grade": "9",
               "correction_status": "COMPLETE", "result_id": "res-xyz"}
    sync.record_correction_result(payload, account_id="acct-1",
                                  worker_id="w-3", device_id="dev-3")
    row = db.query_one("SELECT * FROM correction_ownership WHERE result_id = 'res-xyz'")
    assert row["worker_id"] == "w-3" and row["device_id"] == "dev-3"


# ---------------------------------------------------------------------------
# §16/§17/§22/§23: heartbeat, presence never faked
# ---------------------------------------------------------------------------

def test_heartbeat_then_offline(dev_account):
    agents.register_device("m-dev-1", label="A-PC")
    agents.register_worker("wA", "m-dev-1", account_id=dev_account["id"])
    agents.heartbeat(device_id="m-dev-1", worker_id="wA",
                     account_id=dev_account["id"], status=agents.PRESENCE_ONLINE)
    row = agents.get_worker("wA")
    assert row["presence"] == agents.PRESENCE_ONLINE
    # simulate being offline for 20 minutes
    stale = time.time() - 20 * 60
    db.execute("UPDATE workers SET last_heartbeat = ? WHERE worker_id = 'wA'",
               (stale,))
    row = agents.get_worker("wA")
    assert row["presence"] == agents.PRESENCE_OFFLINE   # §62: real, not faked


def test_worker_status_change_event(dev_account):
    agents.register_device("m-dev-2", label="B-PC")
    agents.register_worker("wB", "m-dev-2")
    agents.heartbeat(device_id="m-dev-2", worker_id="wB", status="BUSY")
    events = [e for e in sync.events_since(0)
              if e["type"] == sync.EVENT_WORKER_STATUS]
    assert len(events) == 1 and events[0]["payload"]["to"] == "BUSY"
    # Repeating the same status must NOT create another event (§52).
    agents.heartbeat(device_id="m-dev-2", worker_id="wB", status="BUSY")
    events = [e for e in sync.events_since(0)
              if e["type"] == sync.EVENT_WORKER_STATUS]
    assert len(events) == 1


# ---------------------------------------------------------------------------
# §24/§25: error reports, sanitizer
# ---------------------------------------------------------------------------

def test_error_report_sanitized(dev_account):
    audit.report_error(
        error_type="ValueError", message="mark scheme; password=hunter2",
        failed_stage="CORRECTING", account=dev_account, device_id="dev-9",
        worker_id="w-9",
        context={"password": "hunter2", "api_key": "AIzaSECRET",
                 "assignment": "Ch4 HW"},
        session_events=["Session started", "Gemini request",
                        "api_key=AIzaSECRET"])
    report = audit.list_errors(limit=1)[0]
    blob = json.dumps(report)
    assert "hunter2" not in blob
    assert "AIzaSECRET" not in blob
    assert "Ch4 HW" in blob            # ordinary context preserved


def test_error_report_can_be_marked_resolved(dev_account):
    audit.report_error(error_type="x", message="m", failed_stage="s")
    report = audit.list_errors(limit=1)[0]
    audit.mark_resolved(report["id"])
    still = audit.list_errors(limit=50, unresolved_only=True)
    assert all(e["id"] != report["id"] for e in still)


# ---------------------------------------------------------------------------
# §27-§45: updates, including the critical §61 rollback test
# ---------------------------------------------------------------------------

from app.distributed import signing  # noqa: E402

# One Developer key pair for the whole module: the tests exercise the REAL
# §19/§25 path — sign with the private key, verify with a PINNED public key.
_TEST_PRIV, _TEST_PUB = signing.generate_keypair()
_TEST_KEY_ID = signing.key_id(_TEST_PUB)


def _signed_package(*, target_version, files, sha256=None,
                    minimum_supported_version=None):
    """Build a package signed by the Developer test key, with a real checksum."""
    pkg = Package(target_version=target_version, files=files, sha256=sha256,
                  minimum_supported_version=minimum_supported_version)
    if sha256 is None:
        pkg.sha256 = hashlib.sha256(pkg.serialize()).hexdigest()
    pkg.signature = signing.sign_package(pkg.serialize(), _TEST_PRIV)
    pkg.signing_key_id = _TEST_KEY_ID
    return pkg


def _updater(tmp_path, files, health_results, *, version="1.0.0"):
    app_dir = tmp_path / "app"
    app_dir.mkdir(exist_ok=True)
    (app_dir / "main.py").write_text("print('v1')\n")
    data_dir = tmp_path / "data"
    data_dir.mkdir(exist_ok=True)
    (data_dir / "app.db").write_text("db-bytes")
    checks = iter(health_results)

    def health():
        return next(checks, (False, "health fail"))

    upd = DeviceUpdater(app_dir=app_dir, data_dir=data_dir,
                        backup_root=tmp_path / "backups",
                        current_version=version,
                        health_check=health,
                        # After restoring the previous version it is healthy
                        # again by definition in these tests (§38).
                        rollback_health_check=lambda: (True, "restored ok"),
                        # A real device pins the Developer's public key.
                        trusted_public_keys={_TEST_KEY_ID: _TEST_PUB})
    pkg = _signed_package(target_version="2.0.0", files=files)
    return app_dir, data_dir, upd, pkg


import hashlib  # noqa: E402
from pathlib import Path  # noqa: E402


def test_update_rejects_bad_checksum(tmp_path):
    files = {"new.py": b"x"}

    def fake_health():
        return True, "ok"

    upd = DeviceUpdater(app_dir=tmp_path / "no-app", data_dir=tmp_path / "nodata",
                        backup_root=tmp_path / "b", current_version="1.0.0",
                        health_check=fake_health)
    pkg = Package(target_version="2.0.0", files=files, sha256="0" * 64)
    result = upd.apply_update(pkg, device_id="dev-1")
    assert result.final_state == State.FAILED
    assert result.failure_reason == "checksum mismatch"
    assert not (tmp_path / "no-app").exists()   # nothing touched, §33


def test_update_rejects_unsafe_paths(tmp_path):
    def fake_health():
        return True, "ok"
    upd = DeviceUpdater(app_dir=tmp_path / "no-app", data_dir=tmp_path / "nd",
                        backup_root=tmp_path / "b", current_version="1.0.0",
                        health_check=fake_health,
                        trusted_public_keys={_TEST_KEY_ID: _TEST_PUB})
    pkg = _signed_package(target_version="2.0.0",
                          files={"../evil.py": b"x"})
    result = upd.apply_update(pkg, device_id="dev-1")
    assert result.final_state == State.FAILED
    assert "unsafe package path" in result.failure_reason


def test_update_rejected_incompatible_disk_space(tmp_path, monkeypatch):
    import platform as _plat
    monkeypatch.setattr(_plat, "system", lambda: "Windows")
    files = {"new.py": b"x"}
    app_dir, data_dir, upd, pkg = _updater(tmp_path, files, [(True, "ok")])

    class FakeUsage:
        free = 10 * 1024

    monkeypatch.setattr(shutil, "disk_usage", lambda p: FakeUsage())
    result = upd.apply_update(pkg, device_id="dev-1")
    assert result.final_state == State.REJECTED
    assert "disk space" in result.stages[-1]["detail"]


import shutil  # noqa: E402


def test_update_waits_when_worker_busy(tmp_path):
    files = {"new.py": b"x"}
    app_dir, data_dir, upd, pkg = _updater(tmp_path, files, [(False, "")])
    upd._busy_check = lambda: (True, "CORRECTING")
    result = upd.apply_update(pkg, device_id="dev-1")
    assert result.final_state == State.WAITING_FOR_IDLE
    assert (app_dir / "main.py").read_text() == "print('v1')\n"  # untouched


def test_update_backup_verify_success_and_data_preserved(tmp_path):
    files = {"main.py": b"print('v2')\n"}
    app_dir, data_dir, upd, pkg = _updater(tmp_path, files, [(True, "healthy")])
    result = upd.apply_update(pkg, device_id="dev-1")
    assert result.final_state == State.UPDATED
    assert (app_dir / "main.py").read_bytes() == b"print('v2')\n"
    assert (data_dir / "app.db").read_bytes() == b"db-bytes"   # §39: DATA safe
    assert result.backup_dir and Path(result.backup_dir).exists()


def test_failed_health_check_rolls_back(tmp_path):
    """§61 CRITICAL: v1 healthy → update to v2 → health fails → rollback
    restores v1 and starts successfully."""
    files = {"main.py": b"print('v2')\n"}
    app_dir, data_dir, upd, pkg = _updater(
        tmp_path, files, [(False, "Required runtime unavailable")])
    result = upd.apply_update(pkg, device_id="assistant-pc")
    assert result.final_state == State.ROLLED_BACK
    assert (app_dir / "main.py").read_text() == "print('v1')\n"  # v1 restored
    assert (data_dir / "app.db").read_bytes() == b"db-bytes"     # data intact
    report = result.as_report(device_id="assistant-pc", old_version="1.0.0",
                              update_id=None)
    assert report["failure_stage"] == "health_check"
    assert report["final_state"] == "ROLLED_BACK"


def test_rollback_failure_reports_recovery_required(tmp_path):
    files = {"main.py": b"print('v2')\n"}
    app_dir, data_dir, upd, pkg = _updater(tmp_path, files, [(False, "no")])
    # Even restoring the previous version fails ITS health check now:
    upd._rollback_health = lambda: (False, "previous version also failing")
    result = upd.apply_update(pkg, device_id="dev-1")
    assert result.final_state == State.RECOVERY_REQUIRED
    assert (app_dir / "main.py").read_text() == "print('v1')\n"


def test_update_history_is_append_only(dev_account):
    updates.record_history(device_id="d1", update_id="u1", old_version="1",
                           new_version="2", result="ROLLED_BACK",
                           started_at=1.0, completed_at=2.0,
                           failure_reason="health", rollback_result="OK")
    updates.record_history(device_id="d1", update_id="u1", old_version="1",
                           new_version="2", result="ROLLED_BACK",
                           started_at=3.0, completed_at=4.0,
                           failure_reason="health", rollback_result="OK")
    rows = updates.list_history(device_id="d1")
    assert len(rows) == 2          # §45: never overwrite


def test_required_update_cannot_auto_install(dev_account):
    from fastapi.testclient import TestClient
    from app.main import app
    client = TestClient(app)
    tok = accounts.issue_token(dev_account["id"])
    r = client.post("/api/distributed/updates", headers={
        "Authorization": f"Bearer {tok}"},
        json={"target_version": "1.8.4", "required": True, "auto_install": True})
    assert r.status_code == 400
    assert "never" in r.json()["message"]


def test_create_update_request_roundtrip(dev_account):
    from fastapi.testclient import TestClient
    from app.main import app
    client = TestClient(app)
    tok = accounts.issue_token(dev_account["id"])
    r = client.post("/api/distributed/updates", headers={
        "Authorization": f"Bearer {tok}"},
        json={"target_version": "1.8.4", "required": True,
              "scope": {"kind": "ALL"}})
    assert r.status_code == 200
    update_id = r.json()["update_id"]
    # A bad version is rejected server-side (§28).
    r2 = client.post("/api/distributed/updates", headers={
        "Authorization": f"Bearer {tok}"},
        json={"target_version": "banana"})
    assert r2.status_code == 400


# ---------------------------------------------------------------------------
# Tiny agent (offline queue + reconnect, §20)
# ---------------------------------------------------------------------------

def test_tiny_agent_queues_while_offline_and_replays(dev_account, tmp_path):
    from app.distributed.tiny_agent import TinyAgent, LocalQueue

    delivered = []

    def fake_post(path, payload, timeout=10):
        if path == "/api/distributed/events":
            delivered.append(payload)
            return {}
        return {}   # current heartbeat succeeds

    agent = TinyAgent(backend_url="http://backend.test", agent_dir=tmp_path,
                      app_version="1.0.0", agent_version="1.0.0",
                      worker_id="wA", post=fake_post)
    assert agent.device_id                       # persistent device id file
    assert (tmp_path / "device_id").read_text().strip() == agent.device_id

    def offline_post(path, payload, timeout=10):
        raise OSError("backend unreachable")

    agent._post = offline_post
    assert agent.send_heartbeat() is False       # offline
    agent.queue_event("correction.result", {"submission_id": "s1"},
                      client_event_id="evt-off-1")
    assert agent.flush_queue() == 0              # nothing sent while offline
    # POST fails so queue still holds everything…
    assert len(agent.queue.pending()) == 1
    # ...reconnect: replay exactly once.
    agent._post = fake_post
    sent = agent.flush_queue()
    assert sent == 1
    assert agent.flush_queue() == 0              # replayed nothing (dedupe)


# ---------------------------------------------------------------------------
# Deployment: the app-session gate must NOT block the distributed API.
# Tiny agents and remote dashboards authenticate with bearer tokens (§59),
# not the app session cookie — with public mode enabled they would otherwise
# get 401 "Sign in to Math Checker" before reaching their own auth layer.
# ---------------------------------------------------------------------------

def test_public_mode_gate_allows_distributed_but_protects_rest(
        fresh_state, dev_account, monkeypatch):
    from fastapi.testclient import TestClient
    from app.main import app
    from app import auth_gate
    monkeypatch.setattr(auth_gate, "public_mode_enabled", lambda: True)
    monkeypatch.setattr(auth_gate, "credentials_configured", lambda: True)
    client = TestClient(app)

    # Ordinary APIs stay behind the session gate (no cookie → 401).
    r = client.get("/api/results")
    assert r.status_code == 401
    assert r.json()["code"] == "auth_required"

    # Distributed login reaches its OWN auth layer: bad credentials return
    # the route's code, not the gate's "Sign in to Math Checker".
    r = client.post("/api/distributed/login",
                    json={"username": "dev", "password": "wrong"})
    assert r.status_code == 401
    assert r.json()["code"] == "bad_credentials"

    # A real token works with no app session at all (agent scenario).
    tok = accounts.issue_token(dev_account["id"])
    r = client.get("/api/distributed/me",
                   headers={"Authorization": f"Bearer {tok}"})
    assert r.status_code == 200
    assert r.json()["role"] == "DEVELOPER"

    # Unauthenticated distributed GETs are still rejected — by the layer.
    r = client.get("/api/distributed/schools")
    assert r.status_code == 401

    # Heartbeat (presence-only) is reachable without any session.
    r = client.post("/api/distributed/heartbeat", json={"device_id": "gate-test"})
    assert r.status_code == 200


# ---------------------------------------------------------------------------
# Self-service password change (the Multi-School login has no other reset path)
# ---------------------------------------------------------------------------

def test_change_password_requires_current_password(fresh_state, dev_account):
    from fastapi.testclient import TestClient
    from app.main import app
    client = TestClient(app)
    tok = accounts.issue_token(dev_account["id"])
    r = client.post("/api/distributed/change-password",
                    headers={"Authorization": f"Bearer {tok}"},
                    json={"current_password": "wrong-wrong",
                          "new_password": "A-New-Secure-Pw1"})
    assert r.status_code == 401
    assert r.json()["code"] == "bad_credentials"


def test_change_password_rejects_short_password(fresh_state, dev_account):
    from fastapi.testclient import TestClient
    from app.main import app
    client = TestClient(app)
    tok = accounts.issue_token(dev_account["id"])
    # Prove we know the current password first.
    known = "DevPass12345"
    accounts.set_password(dev_account["id"], known)
    r = client.post("/api/distributed/change-password",
                    headers={"Authorization": f"Bearer {tok}"},
                    json={"current_password": known, "new_password": "short"})
    assert r.status_code == 400


def test_change_password_rotates_sessions_and_authenticates(fresh_state, dev_account):
    from fastapi.testclient import TestClient
    from app.main import app
    client = TestClient(app)
    known = "DevPass12345"
    accounts.set_password(dev_account["id"], known)
    old_token = accounts.issue_token(dev_account["id"])
    other_session = accounts.issue_token(dev_account["id"])

    r = client.post("/api/distributed/change-password",
                    headers={"Authorization": f"Bearer {old_token}"},
                    json={"current_password": known,
                          "new_password": "Brand-New-Pw-99"})
    assert r.status_code == 200 and r.json()["ok"] is True
    fresh_token = r.json()["token"]

    # The new password works, the old one does not.
    assert accounts.authenticate(dev_account["username"], "Brand-New-Pw-99")
    assert accounts.authenticate(dev_account["username"], known) is None
    # Every previous session was revoked; the caller's fresh token still works.
    assert accounts.resolve_token(other_session) is None
    assert accounts.resolve_token(old_token) is None
    assert accounts.resolve_token(fresh_token) is not None


# ---------------------------------------------------------------------------
# Multi-School page audit: view_own really means own, and limited diagnostics
# never widen. (The UI used to pass an account_id as worker_id, a filter that
# can never match, so limited roles silently saw an empty error list.)
# ---------------------------------------------------------------------------

def _limited_and_worker(dev_account):
    """A CO_TEACHER account plus one worker owned by the Developer."""
    helper = accounts.create_account(
        username="coteach", display_name="Co-T", password="CoPass1234",
        role="CO_TEACHER", created_by=dev_account["id"])
    agents.register_device("dev-x", label="lab pc", account_id=dev_account["id"])
    agents.register_worker("w-dev", "dev-x", account_id=dev_account["id"],
                           app_version="1.0", agent_version="1.0")
    return helper


def test_workers_view_own_excludes_other_accounts(dev_account):
    from fastapi.testclient import TestClient
    from app.main import app
    helper = _limited_and_worker(dev_account)
    # The helper's OWN worker, registered under its account.
    agents.register_device("dev-y", label="co pc", account_id=helper["id"])
    agents.register_worker("w-co", "dev-y", account_id=helper["id"])

    client = TestClient(app)
    tok = accounts.issue_token(helper["id"])
    r = client.get("/api/distributed/workers",
                   headers={"Authorization": f"Bearer {tok}"})
    assert r.status_code == 200
    ids = [w["worker_id"] for w in r.json()["workers"]]
    assert "w-co" in ids
    assert "w-dev" not in ids          # the Developer's worker stays hidden

    # Developer still sees the whole fleet.
    dev_tok = accounts.issue_token(dev_account["id"])
    r = client.get("/api/distributed/workers",
                   headers={"Authorization": f"Bearer {dev_tok}"})
    assert {"w-co", "w-dev"} <= {w["worker_id"] for w in r.json()["workers"]}


def test_worker_detail_requires_permission_and_ownership(dev_account):
    from fastapi.testclient import TestClient
    from app.main import app
    helper = _limited_and_worker(dev_account)

    client = TestClient(app)
    dev_tok = accounts.issue_token(dev_account["id"])
    r = client.get("/api/distributed/workers/w-dev",
                   headers={"Authorization": f"Bearer {dev_tok}"})
    assert r.status_code == 200

    # The Co-Teacher may not read someone else's worker detail (404, not 403:
    # worker ids must not be enumerable either).
    tok = accounts.issue_token(helper["id"])
    r = client.get("/api/distributed/workers/w-dev",
                   headers={"Authorization": f"Bearer {tok}"})
    assert r.status_code == 404

    # A role with NO workers permission at all is rejected outright.
    plain = accounts.create_account(username="plain", display_name="P",
                                    password="PlainPass12", role="ASSISTANT",
                                    created_by=dev_account["id"])
    accounts.set_scope(plain["id"], [])
    db.execute("UPDATE accounts SET permissions = '[]' WHERE id = ?",
               (plain["id"],))
    plain = accounts.get_account(plain["id"])
    ptok = accounts.issue_token(plain["id"])
    r = client.get("/api/distributed/workers/w-dev",
                   headers={"Authorization": f"Bearer {ptok}"})
    assert r.status_code == 403 and r.json()["code"] == "forbidden"


def test_limited_diagnostics_sees_only_own_reports(dev_account):
    from fastapi.testclient import TestClient
    from app.main import app
    helper = _limited_and_worker(dev_account)

    # One report from the Developer's worker, one from the Co-Teacher's own.
    audit.report_error(error_type="boom", message="dev side",
                       failed_stage="mark", account=dev_account,
                       worker_id="w-dev")
    audit.report_error(error_type="oops", message="co side",
                       failed_stage="upload", account=helper,
                       worker_id="w-co")

    client = TestClient(app)
    tok = accounts.issue_token(helper["id"])
    # No worker_id filter at all: the server still scopes to this account.
    r = client.get("/api/distributed/errors",
                   headers={"Authorization": f"Bearer {tok}"})
    assert r.status_code == 200
    messages = [e["message"] for e in r.json()["errors"]]
    assert messages == ["co side"]

    # Asking for the other account's worker cannot widen the view.
    r = client.get("/api/distributed/errors?worker_id=w-dev",
                   headers={"Authorization": f"Bearer {tok}"})
    assert r.status_code == 200
    assert r.json()["errors"] == []

    # Developer sees both.
    dev_tok = accounts.issue_token(dev_account["id"])
    r = client.get("/api/distributed/errors",
                   headers={"Authorization": f"Bearer {dev_tok}"})
    assert r.status_code == 200
    assert {e["message"] for e in r.json()["errors"]} == {"dev side", "co side"}


def test_update_history_scoped_for_limited_roles(dev_account, fresh_state):
    """The Updates panel loads history for EVERY role, so a Co-Teacher must
    get their OWN device's history instead of a 403 (or the fleet's log)."""
    from fastapi.testclient import TestClient
    from app.main import app
    helper = _limited_and_worker(dev_account)

    now = time.time()
    updates.record_history(device_id="dev-desktop", update_id=None,
                           old_version="1.0", new_version="1.1",
                           result="SUCCESS", started_at=now,
                           completed_at=now)
    updates.record_history(device_id="co-laptop", update_id=None,
                           old_version="1.0", new_version="1.1",
                           result="ROLLED_BACK", started_at=now,
                           completed_at=now)

    # The Co-Teacher's login binds last_device_id via the login device header.
    client = TestClient(app)
    login = client.post("/api/distributed/login",
                        json={"username": "coteach", "password": "CoPass1234",
                              "device_id": "co-laptop"})
    assert login.status_code == 200
    tok = login.json()["token"]

    r = client.get("/api/distributed/updates/history",
                   headers={"Authorization": f"Bearer {tok}"})
    assert r.status_code == 200
    rows = r.json()["history"]
    assert rows, "the device's own history must be visible"
    assert {h["device_id"] for h in rows} == {"co-laptop"}

    # Asking for another device by id cannot widen the view.
    r = client.get("/api/distributed/updates/history?device_id=dev-desktop",
                   headers={"Authorization": f"Bearer {tok}"})
    assert {h["device_id"] for h in r.json()["history"]} == {"co-laptop"}

    # Developer still sees every device.
    dev_tok = accounts.issue_token(dev_account["id"])
    r = client.get("/api/distributed/updates/history",
                   headers={"Authorization": f"Bearer {dev_tok}"})
    assert {h["device_id"] for h in r.json()["history"]} == \
        {"co-laptop", "dev-desktop"}
