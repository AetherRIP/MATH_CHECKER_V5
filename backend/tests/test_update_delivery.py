"""§19/§25/§30/§36: end-to-end update delivery and the agent apply loop.

Covers:
 - the publisher stores the signed package body and the device downloads it
   verbatim, with server-side target enforcement
 - the device reconstructs the Package from bytes and verifies the checksum +
   Ed25519 signature over the EXACT downloaded bytes
 - the agent loop applies a good update, parks a busy worker, and refuses a
   tampered download — reporting each state back to the backend
"""
from __future__ import annotations

import base64
import hashlib
import json

import pytest

from app.distributed import accounts, signing, updates
from app.distributed.device_updater import (
    DeviceUpdater, Package, State, package_from_bytes,
)

_PRIV, _PUB = signing.generate_keypair()
_KEY_ID = signing.key_id(_PUB)


@pytest.fixture(autouse=True)
def reset_dist_tables(fresh_state):
    accounts._TABLES_READY = False
    import app.distributed.schema as schema
    schema.ensure_tables()
    yield
    accounts._TABLES_READY = False


@pytest.fixture()
def dev_account(fresh_state):
    return accounts.create_account(
        username="dev", display_name="Dev", password="sup3rsafe",
        role="DEVELOPER", permissions=["*"])


def _signed_body() -> bytes:
    pkg = Package(target_version="2.0.0", files={"main.py": b"print('v2')\n"})
    return pkg.serialize()


def _request_with_package(dev_account, body: bytes) -> dict:
    return updates.create_request(
        target_version="2.0.0", created_by=dev_account["id"],
        package_sha256=hashlib.sha256(body).hexdigest(),
        package_signature=signing.sign_package(body, _PRIV),
        signing_key_id=_KEY_ID, signing_public_key=_PUB)


# ---------------------------------------------------------------------------
# publisher -> device delivery
# ---------------------------------------------------------------------------

def test_publisher_stores_package_and_device_downloads_verbatim(fresh_state, dev_account):
    from fastapi.testclient import TestClient
    from app.main import app
    client = TestClient(app)
    body = _signed_body()
    request = _request_with_package(dev_account, body)
    update_id = request["update_id"]
    token = accounts.issue_token(dev_account["id"])
    hdr = {"Authorization": f"Bearer {token}", "X-Device-Id": "device-1"}

    r = client.post(f"/api/distributed/updates/{update_id}/package",
                    headers=hdr, json={"data": base64.b64encode(body).decode()})
    assert r.status_code == 200 and r.json()["sha256"] == hashlib.sha256(body).hexdigest()

    r = client.get(f"/api/distributed/updates/{update_id}/package", headers=hdr)
    assert r.status_code == 200
    assert r.content == body                      # byte-for-byte
    assert r.headers["x-package-sha256"] == hashlib.sha256(body).hexdigest()
    assert r.headers["x-package-key-id"] == _KEY_ID


def test_download_requires_authentication(fresh_state, dev_account):
    from fastapi.testclient import TestClient
    from app.main import app
    client = TestClient(app)
    body = _signed_body()
    request = _request_with_package(dev_account, body)
    updates.store_package(request["update_id"], body)
    r = client.get(f"/api/distributed/updates/{request['update_id']}/package")
    assert r.status_code == 401


def test_download_enforces_device_scope(fresh_state, dev_account):
    from fastapi.testclient import TestClient
    from app.main import app
    client = TestClient(app)
    body = _signed_body()
    request = updates.create_request(
        target_version="2.0.0", created_by=dev_account["id"],
        package_sha256=hashlib.sha256(body).hexdigest(),
        package_signature=signing.sign_package(body, _PRIV),
        signing_key_id=_KEY_ID,
        scope={"kind": "DEVICE", "device_ids": ["someone-else"]})
    updates.store_package(request["update_id"], body)
    token = accounts.issue_token(dev_account["id"])
    r = client.get(f"/api/distributed/updates/{request['update_id']}/package",
                   headers={"Authorization": f"Bearer {token}",
                            "X-Device-Id": "device-1"})
    assert r.status_code == 403


def test_store_package_rejects_checksum_mismatch(fresh_state, dev_account):
    request = _request_with_package(dev_account, _signed_body())
    with pytest.raises(ValueError):
        updates.store_package(request["update_id"], b"different bytes")


def test_package_from_bytes_round_trips(fresh_state, dev_account):
    body = _signed_body()
    request = _request_with_package(dev_account, body)
    pkg = package_from_bytes(request, body)
    assert pkg.target_version == "2.0.0"
    assert pkg.files == {"main.py": b"print('v2')\n"}
    assert pkg.serialize() == body                # canonical, byte-stable
    assert pkg.signature == request["package_signature"]


# ---------------------------------------------------------------------------
# the agent apply loop
# ---------------------------------------------------------------------------

def _agent(tmp_path, request, body, *, busy=False):
    from app.distributed.tiny_agent import TinyAgent
    agent_dir = tmp_path / "agent"
    agent_dir.mkdir(exist_ok=True)
    app_dir = tmp_path / "mc_app"
    app_dir.mkdir(exist_ok=True)
    (app_dir / "main.py").write_text("print('v1')\n")
    calls = {"status": [], "history": [], "post": []}

    def factory(req):
        return DeviceUpdater(
            app_dir=app_dir, data_dir=tmp_path / "mc_data",
            backup_root=tmp_path / "mc_backups", current_version="1.0.0",
            trusted_public_keys={_KEY_ID: _PUB},
            busy_check=(lambda: (True, "CORRECTING")) if busy else (lambda: (False, "")),
            health_check=lambda: (True, "ok"),
            rollback_health_check=lambda: (True, "ok"))

    def get(path):
        if path.endswith("/updates/for-device"):
            return {"updates": [request]}
        return {}

    def get_bytes(path):
        return body

    def post(path, payload):
        if "/status" in path:
            calls["status"].append((request["update_id"], payload.get("state"),
                                    payload.get("detail")))
        elif "history" in path:
            calls["history"].append(payload)
        else:
            calls["post"].append((path, payload))
        return {"ok": True}

    agent = TinyAgent(backend_url="http://backend", agent_dir=agent_dir,
                      auth_token="tok", updater_factory=factory,
                      get=get, get_bytes=get_bytes, post=post)
    return agent, app_dir, calls


def test_agent_applies_signed_update_and_reports(tmp_path):
    body = _signed_body()
    request = {"update_id": "upd-1", "target_version": "2.0.0",
               "package_sha256": hashlib.sha256(body).hexdigest(),
               "package_signature": signing.sign_package(body, _PRIV),
               "signing_key_id": _KEY_ID}
    agent, app_dir, calls = _agent(tmp_path, request, body)
    out = agent.apply_pending_updates()
    assert out == {"checked": 1, "applied": 1,
                   "results": [{"update_id": "upd-1", "state": "UPDATED"}]}
    assert (app_dir / "main.py").read_bytes() == b"print('v2')\n"
    states = [s for _, s, _ in calls["status"]]
    assert states[0] == "DOWNLOADING" and states[-1] == "UPDATED"
    assert calls["history"][-1]["result"] == "UPDATED"


def test_agent_parks_update_while_worker_busy(tmp_path):
    body = _signed_body()
    request = {"update_id": "upd-2", "target_version": "2.0.0",
               "package_sha256": hashlib.sha256(body).hexdigest(),
               "package_signature": signing.sign_package(body, _PRIV),
               "signing_key_id": _KEY_ID}
    agent, app_dir, calls = _agent(tmp_path, request, body, busy=True)
    out = agent.apply_pending_updates()
    assert out["results"][0]["state"] == State.WAITING_FOR_IDLE.value
    assert (app_dir / "main.py").read_text() == "print('v1')\n"   # untouched


def test_agent_refuses_tampered_download(tmp_path):
    body = _signed_body()
    request = {"update_id": "upd-3", "target_version": "2.0.0",
               "package_sha256": hashlib.sha256(body).hexdigest(),
               "package_signature": signing.sign_package(body, _PRIV),
               "signing_key_id": _KEY_ID}
    agent, app_dir, calls = _agent(tmp_path, request, b"evil bytes")
    out = agent.apply_pending_updates()
    assert out["results"][0]["state"] == "FAILED"
    assert out["results"][0]["reason"] == "checksum mismatch"
    assert calls["history"][-1]["result"] == "FAILED"
    assert (app_dir / "main.py").read_text() == "print('v1')\n"


def test_agent_without_token_does_not_check_updates(tmp_path):
    from app.distributed.tiny_agent import TinyAgent
    agent_dir = tmp_path / "agent"
    agent_dir.mkdir()
    agent = TinyAgent(backend_url="http://backend", agent_dir=agent_dir)
    assert agent.apply_pending_updates() == {"checked": 0, "applied": 0,
                                             "results": []}
