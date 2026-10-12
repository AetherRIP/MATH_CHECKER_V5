"""Automatic device registration + account/device discovery (§extension 3-5).

These tests exercise the REAL FastAPI app and the REAL agent against it — there
is no mock backend and no simulated online status. Every presence value comes
from a heartbeat that was actually recorded, or it is OFFLINE/unknown.
"""
from __future__ import annotations

import json
import time

import pytest
from fastapi.testclient import TestClient

import app.db as db
from app.distributed import accounts, agent_control, agents, discovery, secure_store
from app.main import app


@pytest.fixture(autouse=True)
def reset_dist_tables(fresh_state):
    accounts._TABLES_READY = False
    from app.distributed import schema
    schema.ensure_tables()
    yield
    accounts._TABLES_READY = False


@pytest.fixture()
def client():
    return TestClient(app)


@pytest.fixture()
def dev_account(fresh_state):
    return accounts.create_account(
        username="dev", display_name="Dev", password="sup3rsafe",
        role="DEVELOPER", permissions=["*"])


# ---------------------------------------------------------------------------
# §1/§3: register with NO credentials in hand
# ---------------------------------------------------------------------------

def test_installer_carries_no_credentials_and_registration_needs_none(client):
    # A brand-new machine registers with only a generated device id.
    r = client.post("/api/distributed/device/register",
                    json={"device_id": "a1b2c3d4e5f6", "label": "ASSISTANT-PC"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["credential"]
    assert body["account_id"] is None            # registration grants no account
    assert body["credential_hours"] >= 1
    # The response never contains a username or password.
    text = json.dumps(body)
    assert "password" not in text and "sup3rsafe" not in text


def test_registration_requires_a_plausible_generated_id(client):
    assert client.post("/api/distributed/device/register",
                       json={"device_id": ""}).status_code == 400
    assert client.post("/api/distributed/device/register",
                       json={"device_id": "abc"}).status_code == 400


# ---------------------------------------------------------------------------
# §8: a device credential can NEVER impersonate a Teacher/Developer
# ---------------------------------------------------------------------------

def test_device_credential_cannot_be_used_as_an_account_token(client, dev_account):
    reg = client.post("/api/distributed/device/register",
                      json={"device_id": "dev0credential1"}).json()
    cred = reg["credential"]
    # Leading/trailing whitespace and casing must not matter.
    for header in (f"Bearer {cred}", f"Bearer {cred.upper()}"):
        r = client.get("/api/distributed/me", headers={"Authorization": header})
        assert r.status_code == 401
    # And it cannot run a correction either.
    r = client.post("/api/distributed/results", headers={"Authorization": f"Bearer {cred}"},
                    json={"submission_id": "s1", "submission_version": "v1"})
    assert r.status_code == 401


def test_discovery_requires_a_valid_device_credential(client):
    # Anonymous: nothing.
    assert client.get("/api/distributed/device/discovery").status_code == 401
    reg = client.post("/api/distributed/device/register",
                      json={"device_id": "node00000001"}).json()
    hdr = {"X-Device-Id": "node00000001", "X-Device-Credential": reg["credential"]}
    assert client.get("/api/distributed/device/discovery", headers=hdr).status_code == 200
    # Wrong device id with a valid credential is refused.
    bad = {"X-Device-Id": "node00000002", "X-Device-Credential": reg["credential"]}
    assert client.get("/api/distributed/device/discovery", headers=bad).status_code == 401


# ---------------------------------------------------------------------------
# §3/§4: accounts created AFTER the installer shipped are discoverable
# ---------------------------------------------------------------------------

def test_account_created_after_registration_is_discovered(client):
    reg = client.post("/api/distributed/device/register",
                      json={"device_id": "freshnode0001"}).json()
    before = reg["discovery"]["accounts"]
    assert before == []                       # nothing exists yet

    teacher = accounts.create_account(username="late_teacher",
                                      display_name="Late Teacher",
                                      password="T3ach!ng1", role="TEACHER")
    hdr = {"X-Device-Id": "freshnode0001", "X-Device-Credential": reg["credential"]}
    after = client.get("/api/distributed/device/discovery", headers=hdr).json()
    names = {a["display_name"]: a for a in after["accounts"]}
    assert "Late Teacher" in names            # discovered with no reinstall
    entry = names["Late Teacher"]
    assert entry["role"] == "TEACHER"
    assert entry["availability"] == "unknown"  # no device ever heartbeated
    assert entry["linking_required"] is True
    assert entry["sync_authorized"] is False   # finding ≠ permission (§5)


def test_a_newly_created_developer_is_also_discoverable(client, dev_account):
    reg = client.post("/api/distributed/device/register",
                      json={"device_id": "freshnode0002"}).json()
    accounts.create_account(username="late_dev", display_name="Late Dev",
                            password="D3v!lop3r", role="DEVELOPER",
                            created_by=dev_account["id"])
    hdr = {"X-Device-Id": "freshnode0002", "X-Device-Credential": reg["credential"]}
    data = client.get("/api/distributed/device/discovery", headers=hdr).json()
    assert any(a["role"] == "DEVELOPER" and a["display_name"] == "Late Dev"
               for a in data["accounts"])


def test_discovery_omits_username_scope_and_permissions(client):
    accounts.create_account(username="secretuser", display_name="Visible Name",
                            password="T3ach!ng1", role="TEACHER")
    reg = client.post("/api/distributed/device/register",
                      json={"device_id": "redactnode0001"}).json()
    hdr = {"X-Device-Id": "redactnode0001", "X-Device-Credential": reg["credential"]}
    data = client.get("/api/distributed/device/discovery", headers=hdr).json()
    text = json.dumps(data)
    assert "secretuser" not in text
    assert "password" not in text and "permissions" not in text and "scope" not in text
    # Only the permitted fields are present.
    allowed = {"account_id", "display_name", "role", "roles", "availability",
               "last_heartbeat", "online_devices", "registered_devices",
               "linked", "sync_authorized", "linking_required"}
    for entry in data["accounts"]:
        assert set(entry) == allowed


def test_disabled_accounts_are_not_discovered(client):
    acct = accounts.create_account(username="gone", display_name="Gone",
                                   password="T3ach!ng1", role="TEACHER")
    accounts.set_account_active(acct["id"], False)
    reg = client.post("/api/distributed/device/register",
                      json={"device_id": "disablednode01"}).json()
    hdr = {"X-Device-Id": "disablednode01", "X-Device-Credential": reg["credential"]}
    data = client.get("/api/distributed/device/discovery", headers=hdr).json()
    assert all(a["account_id"] != acct["id"] for a in data["accounts"])


# ---------------------------------------------------------------------------
# §4: availability comes from REAL heartbeats, never from the network
# ---------------------------------------------------------------------------

def test_availability_reflects_real_heartbeats(client):
    teacher = accounts.create_account(username="hb_teacher", display_name="HB Teacher",
                                      password="T3ach!ng1", role="TEACHER")
    reg = client.post("/api/distributed/device/register",
                      json={"device_id": "hbnode00000001"}).json()
    hdr = {"X-Device-Id": "hbnode00000001", "X-Device-Credential": reg["credential"]}

    def _entry(data):
        return next(a for a in data["accounts"] if a["account_id"] == teacher["id"])

    offline = _entry(client.get("/api/distributed/device/discovery", headers=hdr).json())
    assert offline["availability"] == "unknown"

    # Record a REAL heartbeat for one of the teacher's workers, signed with
    # that account's token (the device credential is not an account).
    tok = accounts.issue_token(teacher["id"])
    client.post("/api/distributed/heartbeat", json={
        "device_id": "teacher-pc", "worker_id": "teacher-w1", "token": tok,
        "status": "ONLINE"})
    online = _entry(client.get("/api/distributed/device/discovery", headers=hdr).json())
    assert online["availability"] == "online"
    assert online["online_devices"] == 1
    assert online["last_heartbeat"] is not None

    # Age the heartbeat past the offline window and it must flip to offline.
    stale = time.time() - agents.OFFLINE_AFTER_SECONDS - 5
    db.execute("UPDATE workers SET last_heartbeat = ? WHERE worker_id = 'teacher-w1'",
               (stale,))
    gone = _entry(client.get("/api/distributed/device/discovery", headers=hdr).json())
    assert gone["availability"] == "offline"


# ---------------------------------------------------------------------------
# §3/§8: short-lived + rotatable + revocable credentials
# ---------------------------------------------------------------------------

def test_credential_hard_part_is_never_stored(client):
    reg = client.post("/api/distributed/device/register",
                      json={"device_id": "hashnode0000001"}).json()
    rows = db.query("SELECT credential_hash FROM device_credentials")
    hashes = [r["credential_hash"] for r in rows]
    assert reg["credential"] not in hashes       # only the SHA-256 is stored
    assert len(hashes[0]) == 64


def test_reregistration_rotates_and_invalidates_the_old_credential(client):
    first = client.post("/api/distributed/device/register",
                        json={"device_id": "rotatenode00001"}).json()["credential"]
    second = client.post("/api/distributed/device/register",
                         json={"device_id": "rotatenode00001"}).json()["credential"]
    assert first != second
    ok = {"X-Device-Id": "rotatenode00001", "X-Device-Credential": second}
    old = {"X-Device-Id": "rotatenode00001", "X-Device-Credential": first}
    assert client.get("/api/distributed/device/discovery", headers=ok).status_code == 200
    assert client.get("/api/distributed/device/discovery", headers=old).status_code == 401


def test_expired_credential_is_rejected(client):
    reg = client.post("/api/distributed/device/register",
                      json={"device_id": "expirednode0001"}).json()
    db.execute("UPDATE device_credentials SET expires_at = ? WHERE device_id = ?",
               (time.time() - 1, "expirednode0001"))
    hdr = {"X-Device-Id": "expirednode0001", "X-Device-Credential": reg["credential"]}
    assert client.get("/api/distributed/device/discovery", headers=hdr).status_code == 401


def test_revoking_a_device_kills_its_credential(client, dev_account):
    reg = client.post("/api/distributed/device/register",
                      json={"device_id": "revokenode00001"}).json()
    hdr = {"X-Device-Id": "revokenode00001", "X-Device-Credential": reg["credential"]}
    assert client.get("/api/distributed/device/discovery", headers=hdr).status_code == 200

    tok = accounts.issue_token(dev_account["id"])
    rv = client.post("/api/distributed/devices/revokenode00001/revoke",
                     headers={"Authorization": f"Bearer {tok}"})
    assert rv.status_code == 200 and rv.json()["credentials_revoked"] == 1
    assert client.get("/api/distributed/device/discovery", headers=hdr).status_code == 401


def test_only_managers_can_revoke_a_device(client, dev_account):
    assistant = accounts.create_account(username="asst", display_name="Asst",
                                        password="Ass!stant1", role="ASSISTANT")
    tok = accounts.issue_token(assistant["id"])
    r = client.post("/api/distributed/devices/whatever/revoke",
                    headers={"Authorization": f"Bearer {tok}"})
    assert r.status_code == 403


# ---------------------------------------------------------------------------
# §5: explicit linking gates synchronisation, never the discovery itself
# ---------------------------------------------------------------------------

def test_linking_authorises_but_is_not_granted_by_discovery_alone(client, dev_account):
    teacher = accounts.create_account(username="link_teacher",
                                      display_name="Link Teacher",
                                      password="T3ach!ng1", role="TEACHER")
    reg = client.post("/api/distributed/device/register",
                      json={"device_id": "linknode0000001"}).json()
    device_id = "linknode0000001"
    hdr = {"X-Device-Id": device_id, "X-Device-Credential": reg["credential"]}

    entry = next(a for a in client.get(
        "/api/distributed/device/discovery", headers=hdr).json()["accounts"]
        if a["account_id"] == teacher["id"])
    assert entry["sync_authorized"] is False and entry["linking_required"] is True

    tok = accounts.issue_token(dev_account["id"])
    link = client.post(f"/api/distributed/devices/{device_id}/link",
                       headers={"Authorization": f"Bearer {tok}"},
                       json={"account_id": teacher["id"]})
    assert link.status_code == 200 and link.json()["sync_authorized"] is True

    entry = next(a for a in client.get(
        "/api/distributed/device/discovery", headers=hdr).json()["accounts"]
        if a["account_id"] == teacher["id"])
    assert entry["linked"] is True and entry["sync_authorized"] is True
    assert entry["linking_required"] is False


def test_a_linked_device_heartbeats_under_its_account(client, dev_account):
    teacher = accounts.create_account(username="hb_link", display_name="HB Link",
                                      password="T3ach!ng1", role="TEACHER")
    reg = client.post("/api/distributed/device/register",
                      json={"device_id": "heartbeatlink01"}).json()
    device_id = "heartbeatlink01"
    tok = accounts.issue_token(dev_account["id"])
    client.post(f"/api/distributed/devices/{device_id}/link",
                headers={"Authorization": f"Bearer {tok}"},
                json={"account_id": teacher["id"]})
    # Heartbeat with the DEVICE credential: the backend (not the client)
    # supplies the account linkage.
    client.post("/api/distributed/heartbeat", json={
        "device_id": device_id, "device_credential": reg["credential"],
        "worker_id": "linked-w1", "status": "ONLINE"})
    row = db.query_one("SELECT account_id FROM workers WHERE worker_id = 'linked-w1'")
    assert row["account_id"] == teacher["id"]


def test_an_unlinked_device_heartbeats_with_no_account(client):
    reg = client.post("/api/distributed/device/register",
                      json={"device_id": "unlinkednode001"}).json()
    client.post("/api/distributed/heartbeat", json={
        "device_id": "unlinkednode001", "device_credential": reg["credential"],
        "worker_id": "unlinked-w1", "status": "ONLINE"})
    row = db.query_one("SELECT account_id FROM workers WHERE worker_id = 'unlinked-w1'")
    assert row["account_id"] is None


# ---------------------------------------------------------------------------
# §8: only Teacher/Developer accounts manage the device registry
# ---------------------------------------------------------------------------

def test_device_registry_requires_a_viewer_permission(client, dev_account):
    assistant = accounts.create_account(username="asst2", display_name="Asst2",
                                        password="Ass!stant1", role="ASSISTANT")
    tok = accounts.issue_token(assistant["id"])
    r = client.get("/api/distributed/devices",
                   headers={"Authorization": f"Bearer {tok}"})
    assert r.status_code == 403
    tok = accounts.issue_token(dev_account["id"])
    r = client.get("/api/distributed/devices",
                   headers={"Authorization": f"Bearer {tok}"})
    assert r.status_code == 200 and "devices" in r.json()


# ---------------------------------------------------------------------------
# Agent integration: a real TinyAgent against the real backend
# ---------------------------------------------------------------------------

def test_tiny_agent_registers_discovers_and_persists(client, tmp_path, monkeypatch):
    # The agent's whole runtime is preserved, but a version that does not run
    # one must never start it (§1). Turning the version switch on exercises the
    # identical code path this test has always covered.
    from app.distributed import agent_control
    monkeypatch.setenv(agent_control.ENV_VAR, "1")
    accounts.create_account(username="agent_teacher", display_name="Agent Teacher",
                            password="T3ach!ng1", role="TEACHER")

    def post(path, payload, timeout=10):
        assert payload.get("token") is None     # the agent holds no password
        return client.post(path, json=payload).json()

    def get(path, timeout=10, headers=None):
        return client.get(path, headers=headers or {}).json()

    from app.distributed.tiny_agent import TinyAgent
    agent = TinyAgent(backend_url="http://test", agent_dir=tmp_path,
                      app_version="9.9.9", post=post, get=get)

    result = agent.tick()
    assert result["registered"] is True
    assert result["heartbeat"] is True
    assert result["accounts"] == 1
    assert agent.discovery["accounts"][0]["display_name"] == "Agent Teacher"

    # The credential was persisted (encrypted where the OS supports it) in the
    # agent directory, never in source or an installer.
    assert (tmp_path / "device_credential.json").exists()
    stored = secure_store.load(tmp_path / "device_credential.json")
    assert stored and stored["credential"] == agent._credential

    # A restart reuses the stored credential instead of re-registering.
    agent2 = TinyAgent(backend_url="http://test", agent_dir=tmp_path,
                       post=post, get=get)
    assert agent2._credential == agent._credential
    assert agent2.ensure_credential() is True


def test_agent_does_no_background_work_while_paused(tmp_path):
    calls = {"n": 0}

    def post(path, payload, timeout=10):
        calls["n"] += 1
        return {}

    def get(path, timeout=10, headers=None):
        calls["n"] += 1
        return {}

    from app.distributed.tiny_agent import TinyAgent
    agent = TinyAgent(backend_url="http://test", agent_dir=tmp_path,
                      post=post, get=get, state_loader=lambda: False)  # OFF
    out = agent.tick()
    assert out["paused"] is True and out["heartbeat"] is False
    assert out["registered"] is False
    assert calls["n"] == 0        # no registration, discovery or heartbeat


def test_device_platform_is_recorded_and_filters_releases(client):
    """Regression: a device must record its OS so a platform-specific release
    is never offered to the wrong operating system.

    Before this fix the ``devices`` table had no platform column, so
    ``updates.device_context`` always returned None and a Linux-only release
    was offered to a Windows installation.
    """
    client.post("/api/distributed/heartbeat", json={
        "device_id": "platnode0000001", "worker_id": "plat-w1",
        "status": "ONLINE", "platform": "win32", "arch": "AMD64"})
    row = db.query_one("SELECT platform, arch FROM devices WHERE device_id = ?",
                       ("platnode0000001",))
    assert row["platform"] == "windows"          # win32 normalised
    assert row["arch"] == "AMD64"

    from app.distributed import agents, updates
    assert agents.normalize_platform("darwin") == "darwin"
    assert agents.normalize_platform("macOS") == "darwin"
    assert agents.normalize_platform("nonsense") is None

    # A published, valid linux-only release must NOT apply to this device.
    updates.create_request(target_version="9.9.9", created_by="dev",
                           platform="linux", status=updates.STATUS_PUBLISHED)
    current, platform = updates.device_context("platnode0000001")
    assert platform == "windows"
    offered = updates.requests_for_device("platnode0000001")
    assert all(r.get("platform") != "linux" for r in offered)

    # A windows release for the same device is still offered.
    win = updates.create_request(target_version="9.9.8", created_by="dev",
                                 platform="windows", status=updates.STATUS_PUBLISHED)
    offered = updates.requests_for_device("platnode0000001")
    assert any(r["update_id"] == win["update_id"] for r in offered)


def test_secure_store_roundtrip(tmp_path):
    path = tmp_path / "secret.json"
    secure_store.save(path, {"credential": "SECRET-VALUE", "expires_at": 123.0})
    assert secure_store.load(path) == {"credential": "SECRET-VALUE",
                                       "expires_at": 123.0}
    # A missing file is not an error.
    assert secure_store.load(tmp_path / "nope.json") is None
