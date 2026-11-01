"""R-08 regression tests: the secure store must fail closed, and say what it did.

Each test states the behaviour a caller relies on, not the implementation:

* a write that cannot be encrypted writes nothing and does not damage what is
  already at the path;
* plaintext only happens when the caller asked for it, and the mode is reported;
* a protected file that cannot be decrypted is *corrupt*, never "absent";
* legacy unmarked data keeps loading (no migration trap);
* the peer service refuses to start on an unprotected store, because D1 puts the
  department root key in it.
"""
from __future__ import annotations

import json
import os

import pytest

from app.distributed import secure_store as ss
from app.peer import control
from app.peer import service as peer_service
from app.peer import identity as identity_mod
from app.peer.service import CREDENTIALS_UNAVAILABLE, UNHEALTHY, PeerService, StoreProbe

WINDOWS = os.name == "nt"


def _no_dpapi(monkeypatch):
    monkeypatch.setattr(ss, "_can_use_dpapi", lambda: False)


def _dpapi_that_fails(monkeypatch):
    monkeypatch.setattr(ss, "_can_use_dpapi", lambda: True)

    def boom(data, *, protect):
        raise OSError("the user profile is not loaded for this account")

    monkeypatch.setattr(ss, "_dpapi", boom)


# --------------------------------------------------------------- absent vs broken

def test_a_missing_file_is_absent_and_not_an_error(tmp_path):
    assert ss.load(tmp_path / "nothing.json") is None
    assert ss.status(tmp_path / "nothing.json") == ss.MODE_ABSENT


def test_a_protected_file_that_cannot_be_decrypted_is_corrupt_not_absent(tmp_path):
    if not WINDOWS:
        pytest.skip("DPAPI blobs only exist on Windows")
    path = tmp_path / "credentials.json"
    path.write_bytes(ss._MARKER_DPAPI + b"not a real DPAPI blob")
    with pytest.raises(ss.SecureStoreCorrupt) as excinfo:
        ss.load(path)
    # The message has to be actionable, not just "None".
    assert "DPAPI" in str(excinfo.value)
    assert path.exists()          # and the file is left alone


def test_unreadable_json_is_reported_as_corrupt(tmp_path):
    path = tmp_path / "state.json"
    path.write_bytes(ss._MARKER_PLAIN + b"{not json")
    with pytest.raises(ss.SecureStoreCorrupt):
        ss.load(path)


# ------------------------------------------------------------------ fail closed

def test_save_refuses_to_write_plaintext_key_material(tmp_path, monkeypatch):
    _no_dpapi(monkeypatch)
    path = tmp_path / "department.json"
    with pytest.raises(ss.SecureStoreUnavailable) as excinfo:
        ss.save(path, {"private_key_pem": "SECRET"})
    assert not path.exists(), "a refused write must not leave a downgraded file"
    assert "Nothing was written" in str(excinfo.value)
    # Actionable: names the cause and where the protection comes from.
    assert ("signed-in Windows account" in str(excinfo.value)
            or "Windows-only" in str(excinfo.value))


def test_a_failed_dpapi_call_raises_instead_of_downgrading(tmp_path, monkeypatch):
    _dpapi_that_fails(monkeypatch)
    path = tmp_path / "identity.json"
    with pytest.raises(ss.SecureStoreUnavailable):
        ss.save(path, {"private_key_pem": "SECRET"})
    assert not path.exists()


def test_a_failed_write_does_not_destroy_the_existing_file(tmp_path, monkeypatch):
    path = tmp_path / "credentials.json"
    ss.save(path, {"v": "old"}, require_encryption=False)
    before = path.read_bytes()
    _dpapi_that_fails(monkeypatch)
    with pytest.raises(ss.SecureStoreUnavailable):
        ss.save(path, {"v": "new"})
    assert path.read_bytes() == before


def test_a_permission_failure_is_reported_and_leaves_no_debris(tmp_path, monkeypatch):
    def denied(*args, **kwargs):
        raise PermissionError("access denied")

    monkeypatch.setattr(ss.os, "replace", denied)
    path = tmp_path / "creds.json"
    with pytest.raises(ss.SecureStoreError) as excinfo:
        ss.save(path, {"a": 1}, require_encryption=False)
    assert "could not write" in str(excinfo.value)
    assert not path.exists()
    assert not (tmp_path / "creds.json.tmp").exists()


# ------------------------------------------------------------- explicit plaintext

def test_plaintext_happens_only_when_asked_for_and_is_reported(tmp_path, monkeypatch):
    _no_dpapi(monkeypatch)
    path = tmp_path / "device.json"
    mode = ss.save(path, {"credential": "TOKEN"}, require_encryption=False)
    assert mode == ss.MODE_PLAINTEXT
    assert ss.status(path) == ss.MODE_PLAINTEXT
    assert ss.load(path) == {"credential": "TOKEN"}


@pytest.mark.skipif(not WINDOWS, reason="DPAPI is Windows-only")
def test_encrypted_round_trip_reports_dpapi_mode(tmp_path):
    path = tmp_path / "device.json"
    mode = ss.save(path, {"credential": "TOKEN"})
    assert mode == ss.MODE_DPAPI
    assert ss.status(path) == ss.MODE_DPAPI
    assert b"TOKEN" not in path.read_bytes(), "the payload must not be readable on disk"
    assert ss.load(path) == {"credential": "TOKEN"}


def test_legacy_unmarked_file_still_loads(tmp_path):
    path = tmp_path / "old_credential.json"
    path.write_text(json.dumps({"a": 1}), encoding="utf-8")
    assert ss.status(path) == ss.MODE_LEGACY
    assert ss.load(path) == {"a": 1}


# ------------------------------------------------------- peer startup must refuse

def _service(tmp_path, **kwargs):
    return PeerService(directory=tmp_path / "peer",
                       credentials_path=tmp_path / "peer" / "credentials.json",
                       **kwargs)


def test_probe_reports_an_unprotected_store_instead_of_assuming_dpapi(tmp_path, monkeypatch):
    _no_dpapi(monkeypatch)
    probe = peer_service.probe_secure_store(tmp_path / "probe.json")
    assert probe.writable is True
    assert probe.encrypted is False
    assert "WITHOUT encryption" in probe.detail


def test_the_service_refuses_to_start_on_an_unprotected_store(tmp_path, monkeypatch):
    monkeypatch.setattr(control, "is_enabled", lambda *a, **k: True)
    monkeypatch.setattr(peer_service, "probe_secure_store",
                        lambda *a, **k: StoreProbe(True, False,
                                                   "stored WITHOUT encryption"))
    service = _service(tmp_path)
    status = service.start()
    assert status.state == UNHEALTHY
    assert "refusing to start" in status.detail
    assert status.encrypted_store is False
    assert service._proc is None, "no child process may be spawned on a refusal"


def test_the_requirement_can_be_relaxed_explicitly_per_instance(tmp_path, monkeypatch):
    monkeypatch.setattr(control, "is_enabled", lambda *a, **k: True)
    monkeypatch.setattr(peer_service, "probe_secure_store",
                        lambda *a, **k: StoreProbe(True, False, "plaintext"))
    service = _service(tmp_path, require_store_encryption=False)
    status = service.start()
    # Past the store gate, and stopped by the next honest check: no credentials.
    assert status.state == CREDENTIALS_UNAVAILABLE


def test_a_corrupt_identity_file_raises_rather_than_looking_like_a_first_run(tmp_path):
    if not WINDOWS:
        pytest.skip("DPAPI blobs only exist on Windows")
    path = tmp_path / "identity.json"
    path.write_bytes(ss._MARKER_DPAPI + b"garbage")
    with pytest.raises(Exception) as excinfo:
        identity_mod.DeviceIdentity.load(path)
    assert "cannot be read" in str(excinfo.value)
