"""Release management (§release management, §4-§13, §25).

Covers the control plane the Developer page drives, and the guarantees a client
depends on: only a DEVELOPER may publish; an invalid release never becomes
available; a withdrawn release stops being offered; and an offline installation
is never reported as up to date.

Everything runs against the isolated conftest database; no network.
"""
from __future__ import annotations

import base64
import base64
import hashlib
import time

import pytest

from app import db
from app.distributed import accounts, agents, signing, updates
from app.distributed.device_updater import Package

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
def dev():
    return accounts.create_account(username="relready", display_name="Dev",
                                   password="sup3rsafe", role="DEVELOPER")


@pytest.fixture()
def teacher():
    return accounts.create_account(username="relteach", display_name="T",
                                   password="t3ach1ng!", role="TEACHER")


@pytest.fixture()
def assistant():
    return accounts.create_account(username="relasst", display_name="A",
                                   password="a5sistant!", role="ASSISTANT")


def _headers(account):
    return {"Authorization": f"Bearer {accounts.issue_token(account['id'])}"}


def _body(version: str = "2.0.0", files=None) -> bytes:
    return Package(target_version=version,
                   files=files or {"main.py": b"print('v2')\n"}).serialize()


def _draft(dev, version="2.0.0", body=None, channel="stable", platform=None,
           signed=True, **kwargs):
    body = body if body is not None else _body(version)
    return updates.create_request(
        target_version=version, created_by=dev["id"], status=updates.STATUS_DRAFT,
        channel=channel, platform=platform,
        package_sha256=hashlib.sha256(body).hexdigest(),
        package_signature=signing.sign_package(body, _PRIV) if signed else None,
        signing_key_id=_KEY_ID if signed else None,
        signing_public_key=_PUB if signed else None, **kwargs)


# ---------------------------------------------------------------------------
# developer-only access
# ---------------------------------------------------------------------------

def test_release_endpoints_are_developer_only(dev, teacher, assistant):
    from fastapi.testclient import TestClient
    from app.main import app
    client = TestClient(app)

    assert client.get("/api/distributed/releases").status_code == 401
    assert client.get("/api/distributed/releases/overview").status_code == 401
    assert client.post("/api/distributed/releases",
                       json={"target_version": "2.0.0"}).status_code == 401

    for account in (teacher, assistant):
        hdr = _headers(account)
        assert client.get("/api/distributed/releases", headers=hdr).status_code == 403
        assert client.get("/api/distributed/releases/overview",
                          headers=hdr).status_code == 403
        assert client.post("/api/distributed/releases", headers=hdr,
                           json={"target_version": "2.0.0"}).status_code == 403
        # Even a direct, hand-crafted publish attempt is refused.
        assert client.post("/api/distributed/releases/upd-nope/publish", headers=hdr,
                           json={"confirm": True}).status_code == 403

    assert client.get("/api/distributed/releases",
                      headers=_headers(dev)).status_code == 200


def test_multi_role_account_with_developer_gains_access():
    """A Developer+Assistant must reach the control plane (§2)."""
    multi = accounts.create_account(username="devassist", display_name="DA",
                                    password="multi12345",
                                    roles=["ASSISTANT", "DEVELOPER"])
    from fastapi.testclient import TestClient
    from app.main import app
    client = TestClient(app)
    assert client.get("/api/distributed/releases",
                      headers=_headers(multi)).status_code == 200


# ---------------------------------------------------------------------------
# draft -> validate -> publish
# ---------------------------------------------------------------------------

def test_draft_is_not_offered_to_clients(dev):
    draft = _draft(dev)
    assert draft["status"] == "draft"
    body = _body()
    updates.store_package(draft["update_id"], body)
    # Even a complete, signed draft is invisible until published.
    assert updates.requests_for_device("device-1", current_version="1.0.0") == []


def test_validate_rejects_a_release_without_a_package(dev):
    draft = _draft(dev)
    result = updates.validate_release(draft["update_id"])
    assert result["ok"] is False
    assert any("no package" in p for p in result["problems"])
    with pytest.raises(ValueError):
        updates.publish_release(draft["update_id"])


def test_validate_rejects_an_unsigned_release(dev):
    draft = _draft(dev, signed=False)
    updates.store_package(draft["update_id"], _body())
    result = updates.validate_release(draft["update_id"])
    assert result["ok"] is False
    assert any("not signed" in p for p in result["problems"])
    with pytest.raises(ValueError):
        updates.publish_release(draft["update_id"])


def test_validate_rejects_a_manifest_version_mismatch(dev):
    # The release says 2.0.0 but the package manifest says 3.0.0.
    body = _body("3.0.0")
    draft = _draft(dev, version="2.0.0", body=body)
    updates.store_package(draft["update_id"], body)
    result = updates.validate_release(draft["update_id"])
    assert result["ok"] is False
    assert any("manifest version differs" in p for p in result["problems"])


def test_invalid_release_cannot_be_published(dev):
    draft = _draft(dev)
    updates.store_package(draft["update_id"], _body())
    # Corrupt the release record's declared checksum so validation fails.
    db.execute("UPDATE update_requests SET package_sha256 = ? WHERE update_id = ?",
               ("0" * 64, draft["update_id"]))
    assert updates.validate_release(draft["update_id"])["ok"] is False
    with pytest.raises(ValueError):
        updates.publish_release(draft["update_id"])


def test_publish_persists_and_requires_confirmation_via_route(dev):
    from fastapi.testclient import TestClient
    from app.main import app
    client = TestClient(app)
    draft = _draft(dev)
    updates.store_package(draft["update_id"], _body())
    hdr = _headers(dev)

    # Without explicit confirmation nothing is published.
    r = client.post(f"/api/distributed/releases/{draft['update_id']}/publish",
                    headers=hdr, json={})
    assert r.status_code == 400 and r.json()["code"] == "confirmation_required"
    assert updates.get_request(draft["update_id"])["status"] == "draft"

    r = client.post(f"/api/distributed/releases/{draft['update_id']}/publish",
                    headers=hdr, json={"confirm": True})
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "published"
    assert r.json()["published_at"]

    # The record persists and the history endpoint shows it.
    releases = client.get("/api/distributed/releases", headers=hdr).json()["releases"]
    assert any(x["update_id"] == draft["update_id"]
               and x["status"] == "published" for x in releases)


def test_legacy_update_endpoint_cannot_publish_directly(dev):
    """POST /updates must only ever create a DRAFT.

    Otherwise a direct API call could push an unvalidated, unsigned package to
    clients without passing validate -> publish.
    """
    from fastapi.testclient import TestClient
    from app.main import app
    client = TestClient(app)
    r = client.post("/api/distributed/updates", headers=_headers(dev),
                    json={"target_version": "3.0.0"})
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "draft"
    assert updates.requests_for_device("d", current_version="1.0.0") == []


def test_publish_refuses_to_overwrite_a_published_version(dev):
    first = _draft(dev)
    updates.store_package(first["update_id"], _body())
    updates.publish_release(first["update_id"])
    # A second release claiming the SAME version on the same channel is refused
    # rather than silently replacing the first.
    second = _draft(dev)
    updates.store_package(second["update_id"], _body())
    with pytest.raises(ValueError):
        updates.publish_release(second["update_id"])
    assert updates.get_request(first["update_id"])["status"] == "published"


# ---------------------------------------------------------------------------
# discovery: a separate client finds the right release
# ---------------------------------------------------------------------------

def test_published_release_is_discovered_by_a_client(dev):
    draft = _draft(dev)
    updates.store_package(draft["update_id"], _body())
    updates.publish_release(draft["update_id"])

    offers = updates.requests_for_device("device-1", current_version="1.0.0")
    assert [o["update_id"] for o in offers] == [draft["update_id"]]
    # Already on the target version -> nothing to do.
    assert updates.requests_for_device("device-2", current_version="2.0.0") == []


def test_discovery_never_offers_a_downgrade(dev):
    draft = _draft(dev, version="1.5.0")
    updates.store_package(draft["update_id"], _body("1.5.0"))
    updates.publish_release(draft["update_id"])
    # A device already on 2.0.0 must not be pushed back down to 1.5.0.
    assert updates.requests_for_device("device-1", current_version="2.0.0") == []


def test_discovery_respects_platform(dev):
    win = _draft(dev, version="2.0.0", platform="windows")
    updates.store_package(win["update_id"], _body("2.0.0"))
    updates.publish_release(win["update_id"])
    assert updates.requests_for_device("d", current_version="1.0.0",
                                       platform="linux") == []
    assert updates.requests_for_device("d", current_version="1.0.0",
                                       platform="windows")


def test_discovery_prefers_the_newest_offered_version(dev):
    for version in ("1.9.0", "2.0.0"):
        draft = _draft(dev, version=version)
        updates.store_package(draft["update_id"], _body(version))
        updates.publish_release(draft["update_id"])
    offers = updates.requests_for_device("d", current_version="1.0.0")
    assert [o["target_version"] for o in offers] == ["2.0.0", "1.9.0"]


# ---------------------------------------------------------------------------
# withdraw, edit, rollback
# ---------------------------------------------------------------------------

def test_withdraw_stops_future_offers_without_deleting_history(dev):
    draft = _draft(dev)
    updates.store_package(draft["update_id"], _body())
    updates.publish_release(draft["update_id"])
    assert updates.requests_for_device("d", current_version="1.0.0")

    updates.withdraw_release(draft["update_id"], reason="bad build")
    assert updates.requests_for_device("d", current_version="1.0.0") == []
    # History survives — the row is withdrawn, never deleted.
    row = updates.get_request(draft["update_id"])
    assert row is not None and row["status"] == "withdrawn" and row["withdrawn_at"]


def test_withdraw_requires_confirmation_via_route(dev):
    from fastapi.testclient import TestClient
    from app.main import app
    client = TestClient(app)
    draft = _draft(dev)
    updates.store_package(draft["update_id"], _body())
    updates.publish_release(draft["update_id"])
    hdr = _headers(dev)
    r = client.post(f"/api/distributed/releases/{draft['update_id']}/withdraw",
                    headers=hdr, json={})
    assert r.status_code == 400 and r.json()["code"] == "confirmation_required"
    r = client.post(f"/api/distributed/releases/{draft['update_id']}/withdraw",
                    headers=hdr, json={"confirm": True, "reason": "regression"})
    assert r.status_code == 200 and r.json()["status"] == "withdrawn"


def test_edit_release_notes_keeps_version_and_package(dev):
    draft = _draft(dev)
    updates.store_package(draft["update_id"], _body())
    sha_before = updates.get_package(draft["update_id"])["sha256"]
    updates.edit_release(draft["update_id"], release_notes="fixed rounding")
    row = updates.get_request(draft["update_id"])
    assert row["release_notes"] == "fixed rounding"
    assert row["target_version"] == "2.0.0"
    assert updates.get_package(draft["update_id"])["sha256"] == sha_before


def test_rollback_recommends_an_earlier_release(dev):
    old = _draft(dev, version="1.5.0")
    updates.store_package(old["update_id"], _body("1.5.0"))
    updates.publish_release(old["update_id"])
    new = _draft(dev, version="2.0.0")
    updates.store_package(new["update_id"], _body("2.0.0"))
    updates.publish_release(new["update_id"])
    assert updates.recommended_version() == "2.0.0"

    outcome = updates.rollback_recommended(old["update_id"])
    assert outcome["recommended"] == "1.5.0"
    assert new["update_id"] in outcome["withdrew"]
    assert updates.recommended_version() == "1.5.0"
    # The newer release is withdrawn from offers, not destroyed.
    assert updates.get_request(new["update_id"])["status"] == "withdrawn"


# ---------------------------------------------------------------------------
# overview + installation status honesty
# ---------------------------------------------------------------------------

def test_overview_reports_real_counts(dev):
    accounts._TABLES_READY = False
    draft = _draft(dev)
    updates.store_package(draft["update_id"], _body())
    updates.publish_release(draft["update_id"])
    agents.heartbeat(device_id="dev-old", worker_id="w-old",
                     account_id=dev["id"], app_version="1.0.0")
    agents.heartbeat(device_id="dev-new", worker_id="w-new",
                     account_id=dev["id"], app_version="2.0.0")

    snap = updates.overview()
    assert snap["latest"]["version"] == "2.0.0"
    assert snap["stable_version"] == "2.0.0"
    assert snap["counts"]["registered_installations"] == 2
    assert snap["counts"]["outdated"] == 1
    assert snap["counts"]["up_to_date"] == 1


# ---------------------------------------------------------------------------
# acceptance: publish -> a separate client discovers, downloads, verifies,
# installs, and reports the new version
# ---------------------------------------------------------------------------

def test_end_to_end_publish_discover_download_install(dev, tmp_path):
    """The acceptance criterion, exercised through the real routes.

    A Developer publishes a valid signed release; a DIFFERENT authorised
    installation discovers it, downloads it, verifies checksum + signature,
    installs it safely, and ends up running the new version with its data
    untouched.
    """
    from fastapi.testclient import TestClient
    from app.main import app
    from app.distributed.device_updater import (
        DeviceUpdater, State, package_from_bytes,
    )
    client = TestClient(app)
    hdr = {**_headers(dev), "X-Device-Id": "device-1"}

    # The client has reported itself, so its version is known server-side.
    agents.heartbeat(device_id="device-1", worker_id="worker-1",
                     account_id=dev["id"], app_version="1.0.0")

    body = _body("1.9.0", {"main.py": b"print('v1.9')\n"})
    signature = signing.sign_package(body, _PRIV)

    # 1. Developer creates a DRAFT release.
    r = client.post("/api/distributed/releases", headers=hdr, json={
        "target_version": "1.9.0", "channel": "stable",
        "target_component": "app", "release_notes": "Adds sub-parts",
        "package_signature": signature, "signing_key_id": _KEY_ID,
        "signing_public_key": _PUB,
    })
    assert r.status_code == 200, r.text
    update_id = r.json()["update_id"]
    assert r.json()["status"] == "draft"

    # 2. Upload the package body, then validate.
    r = client.post(f"/api/distributed/updates/{update_id}/package", headers=hdr,
                    json={"data": base64.b64encode(body).decode()})
    assert r.status_code == 200, r.text
    r = client.post(f"/api/distributed/releases/{update_id}/validate", headers=hdr)
    assert r.status_code == 200 and r.json()["ok"] is True, r.text

    # 3. A draft is NOT yet discoverable.
    assert client.get("/api/distributed/updates/for-device",
                      headers=hdr).json()["updates"] == []

    # 4. Publish (with confirmation).
    r = client.post(f"/api/distributed/releases/{update_id}/publish", headers=hdr,
                    json={"confirm": True})
    assert r.status_code == 200 and r.json()["status"] == "published", r.text

    # 5. The separate client now discovers it, newest first.
    offers = client.get("/api/distributed/updates/for-device",
                        headers=hdr).json()["updates"]
    assert [o["update_id"] for o in offers] == [update_id]
    request = offers[0]

    # 6. It downloads the package VERBATIM.
    r = client.get(f"/api/distributed/updates/{update_id}/package", headers=hdr)
    assert r.status_code == 200 and r.content == body

    # 7. Integrity: tampered bytes are refused before anything is touched.
    app_dir = tmp_path / "mc_app"
    app_dir.mkdir()
    (app_dir / "main.py").write_text("print('v1.0')\n")
    data_dir = tmp_path / "mc_data"
    data_dir.mkdir()
    (data_dir / "grades.json").write_text('{"kept": true}')
    updater = DeviceUpdater(
        app_dir=app_dir, data_dir=data_dir,
        backup_root=tmp_path / "mc_backups", current_version="1.0.0",
        trusted_public_keys={_KEY_ID: _PUB},
        health_check=lambda: (True, "ok"),
        rollback_health_check=lambda: (True, "ok"))
    ok, why = updater.verify_download(b"tampered" + body, request)
    assert ok is False

    ok, why = updater.verify_download(r.content, request)
    assert ok is True, why

    # 8. Install it safely.
    package = package_from_bytes(request, r.content)
    result = updater.apply_update(package, update_id=update_id,
                                 device_id="device-1")
    assert result.final_state == State.UPDATED, result.failure_reason
    assert (app_dir / "main.py").read_bytes() == b"print('v1.9')\n"
    # The update replaced CODE only — user data is untouched (§39).
    assert (data_dir / "grades.json").read_text() == '{"kept": true}'

    # 9. The device reports the outcome and the new version.
    r = client.post(f"/api/distributed/updates/{update_id}/status", headers=hdr,
                    json={"state": "UPDATED"})
    assert r.status_code == 200
    client.post("/api/distributed/updates/history", headers=hdr, json={
        "update_id": update_id, "old_version": "1.0.0",
        "new_version": "1.9.0", "result": "UPDATED"})
    agents.heartbeat(device_id="device-1", worker_id="worker-1",
                     account_id=dev["id"], app_version="1.9.0")
    snap = updates.overview()
    assert snap["counts"]["up_to_date"] == 1
    assert snap["counts"]["outdated"] == 0
    # And the device is no longer offered the version it already runs.
    assert client.get("/api/distributed/updates/for-device",
                      headers=hdr).json()["updates"] == []


def test_installation_status_uses_what_the_device_reported(dev):
    """Failed and Downloading come from the device's OWN progress rows, not
    from guessing at its version."""
    accounts._TABLES_READY = False
    draft = _draft(dev)
    updates.store_package(draft["update_id"], _body())
    updates.publish_release(draft["update_id"])
    agents.heartbeat(device_id="dev-a", worker_id="w-a",
                     account_id=dev["id"], app_version="1.0.0")
    agents.heartbeat(device_id="dev-b", worker_id="w-b",
                     account_id=dev["id"], app_version="1.0.0")
    updates.set_device_status(draft["update_id"], "dev-a", "FAILED",
                              "health check failed")
    updates.set_device_status(draft["update_id"], "dev-b", "DOWNLOADING")

    snap = updates.install_status()
    assert snap["counts"].get("FAILED") == 1
    assert snap["counts"].get("DOWNLOADING") == 1
    assert snap["installations"]["FAILED"][0]["device_id"] == "dev-a"
    assert snap["installations"]["FAILED"][0]["reported_state"] == "FAILED"
    # And the overview surfaces it as a failed install.
    assert updates.overview()["counts"]["failed"] >= 1

    # A device that has since gone silent is OFFLINE, not stuck mid-download.
    db.execute("UPDATE workers SET last_heartbeat = ? WHERE worker_id = ?",
               (time.time() - agents.OFFLINE_AFTER_SECONDS - 60, "w-a"))
    after = updates.install_status()["counts"]
    assert after.get("OFFLINE") == 1
    assert "FAILED" not in after


def test_offline_installation_is_not_reported_as_up_to_date(dev):
    accounts._TABLES_READY = False
    draft = _draft(dev)
    updates.store_package(draft["update_id"], _body())
    updates.publish_release(draft["update_id"])
    agents.heartbeat(device_id="dev-quiet", worker_id="w-quiet",
                     account_id=dev["id"], app_version="2.0.0")
    # The version MATCHES the latest, but the heartbeat is ancient.
    db.execute("UPDATE workers SET last_heartbeat = ? WHERE worker_id = ?",
               (time.time() - agents.OFFLINE_AFTER_SECONDS - 60, "w-quiet"))
    snapshot = updates.overview()
    assert snapshot["counts"]["offline"] == 1
    assert snapshot["counts"]["up_to_date"] == 0
    assert snapshot["install_status"]["counts"].get("OFFLINE") == 1
    assert "UP_TO_DATE" not in snapshot["install_status"]["counts"]
