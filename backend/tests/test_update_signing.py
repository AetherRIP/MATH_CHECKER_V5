"""§19/§25: Ed25519 signing of update packages.

Covers the whole trust chain:
 - key generation / fingerprint stability
 - sign → verify round trip, and every tamper/bad-key failure
 - the DeviceUpdater REFUSES (a) unsigned packages, (b) packages signed by an
   untrusted key, (c) packages whose bytes changed after signing — and does so
   BEFORE touching the installation
 - the control plane (update_requests) stores and validates the signature
"""
from __future__ import annotations

import base64

import pytest

from app.distributed import signing
from app.distributed.device_updater import DeviceUpdater, Package, State


# ---------------------------------------------------------------------------
# signing module
# ---------------------------------------------------------------------------

def test_generate_keypair_sign_verify_roundtrip():
    priv, pub = signing.generate_keypair()
    payload = b'{"target_version":"2.0.0","files":{}}'
    sig = signing.sign_package(payload, priv)
    assert signing.verify_package(payload, sig, pub) is True


def test_key_id_is_stable_and_matches_public_key():
    _, pub = signing.generate_keypair()
    assert signing.key_id(pub) == signing.key_id(pub)
    assert len(signing.key_id(pub)) == 16


def test_tampered_payload_fails_verification():
    priv, pub = signing.generate_keypair()
    sig = signing.sign_package(b"original", priv)
    assert signing.verify_package(b"tampered", sig, pub) is False


def test_wrong_public_key_fails_verification():
    priv, _ = signing.generate_keypair()
    _, other_pub = signing.generate_keypair()
    sig = signing.sign_package(b"payload", priv)
    assert signing.verify_package(b"payload", sig, other_pub) is False


def test_malformed_signature_never_raises():
    _, pub = signing.generate_keypair()
    assert signing.verify_package(b"payload", "not-base64!!", pub) is False
    assert signing.verify_package(b"payload", "", pub) is False
    assert signing.verify_package(b"payload", base64.b64encode(b"xy").decode(), pub) is False


# ---------------------------------------------------------------------------
# DeviceUpdater enforcement
# ---------------------------------------------------------------------------

_PRIV, _PUB = signing.generate_keypair()
_KEY_ID = signing.key_id(_PUB)


def _updater(tmp_path, *, trusted=True, require=True):
    app_dir = tmp_path / "mc_app"
    app_dir.mkdir(exist_ok=True)
    (app_dir / "main.py").write_text("print('v1')\n")
    data_dir = tmp_path / "mc_data"
    data_dir.mkdir(exist_ok=True)
    (data_dir / "app.db").write_text("db")
    return DeviceUpdater(
        app_dir=app_dir, data_dir=data_dir, backup_root=tmp_path / "mc_backups",
        current_version="1.0.0",
        health_check=lambda: (True, "ok"),
        rollback_health_check=lambda: (True, "ok"),
        trusted_public_keys=({_KEY_ID: _PUB} if trusted else {}),
        require_signature=require)


def _package(*, signed=True, key_id=_KEY_ID, files=None):
    import hashlib
    pkg = Package(target_version="2.0.0", files=files or {"main.py": b"print('v2')\n"})
    pkg.sha256 = hashlib.sha256(pkg.serialize()).hexdigest()
    if signed:
        pkg.signature = signing.sign_package(pkg.serialize(), _PRIV)
        pkg.signing_key_id = key_id
    return pkg


def test_unsigned_package_is_refused_before_install(tmp_path):
    upd = _updater(tmp_path)
    result = upd.apply_update(_package(signed=False), device_id="d1")
    assert result.final_state == State.FAILED
    assert result.failure_reason == "package is not signed"
    # Nothing was touched and no backup was made (§33).
    assert (upd.app_dir / "main.py").read_text() == "print('v1')\n"
    assert not result.backup_dir


def test_untrusted_key_is_refused(tmp_path):
    upd = _updater(tmp_path)
    result = upd.apply_update(_package(key_id="deadbeefdeadbeef"), device_id="d1")
    assert result.final_state == State.FAILED
    assert "not trusted" in result.failure_reason
    assert (upd.app_dir / "main.py").read_text() == "print('v1')\n"


def test_tampered_after_signing_is_refused(tmp_path):
    upd = _updater(tmp_path)
    pkg = _package()
    # Swap the payload bytes AFTER signing, then re-fix the checksum so only
    # the SIGNATURE can catch the substitution.
    import hashlib
    pkg.files = {"main.py": b"print('evil')\n"}
    pkg.sha256 = hashlib.sha256(pkg.serialize()).hexdigest()
    result = upd.apply_update(pkg, device_id="d1")
    assert result.final_state == State.FAILED
    assert result.failure_reason == "invalid package signature"
    assert (upd.app_dir / "main.py").read_text() == "print('v1')\n"


def test_properly_signed_package_installs(tmp_path):
    upd = _updater(tmp_path)
    result = upd.apply_update(_package(), device_id="d1")
    assert result.final_state == State.UPDATED
    assert (upd.app_dir / "main.py").read_bytes() == b"print('v2')\n"
    assert result.backup_dir


def test_opt_out_allows_unsigned_for_local_testing(tmp_path):
    upd = _updater(tmp_path, trusted=False, require=False)
    result = upd.apply_update(_package(signed=False), device_id="d1")
    assert result.final_state == State.UPDATED


# ---------------------------------------------------------------------------
# control plane: update_requests stores + validates the signature
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def reset_dist_tables(fresh_state):
    from app.distributed import accounts
    accounts._TABLES_READY = False
    import app.distributed.schema as schema
    schema.ensure_tables()
    yield
    accounts._TABLES_READY = False


@pytest.fixture()
def dev_account(fresh_state):
    from app.distributed import accounts
    return accounts.create_account(
        username="dev", display_name="Dev", password="sup3rsafe",
        role="DEVELOPER", permissions=["*"])


def test_create_request_stores_valid_signature(dev_account):
    from app.distributed import updates
    sig = signing.sign_package(b"payload", _PRIV)
    req = updates.create_request(
        target_version="2.0.0", created_by=dev_account["id"],
        package_signature=sig, signing_key_id=_KEY_ID,
        signing_public_key=_PUB)
    assert req["package_signature"] == sig
    assert req["signing_key_id"] == _KEY_ID


def test_create_request_rejects_bad_signature_base64(dev_account):
    from app.distributed import updates
    with pytest.raises(ValueError):
        updates.create_request(
            target_version="2.0.0", created_by=dev_account["id"],
            package_signature="!!!not base64!!!")


def test_create_request_rejects_key_id_mismatch(dev_account):
    from app.distributed import updates
    with pytest.raises(ValueError):
        updates.create_request(
            target_version="2.0.0", created_by=dev_account["id"],
            signing_public_key=_PUB, signing_key_id="0000000000000000")


# ---------------------------------------------------------------------------
# control plane -> device bridge (package_from_request)
# ---------------------------------------------------------------------------

def test_package_from_request_preserves_signature_fields():
    from app.distributed.device_updater import package_from_request
    sig = signing.sign_package(b"x", _PRIV)
    req = {"target_version": "2.0.0", "package_sha256": "a" * 64,
           "minimum_supported_version": "1.0.0",
           "package_signature": sig, "signing_key_id": _KEY_ID}
    pkg = package_from_request(req, {"main.py": b"print('v2')\n"})
    assert pkg.target_version == "2.0.0"
    assert pkg.signature == sig and pkg.signing_key_id == _KEY_ID
    assert pkg.minimum_supported_version == "1.0.0"


def test_request_without_signature_is_refused_by_updater(tmp_path):
    """§25: a control-plane request that names no signature cannot install."""
    import hashlib
    from app.distributed.device_updater import package_from_request
    files = {"main.py": b"print('v2')\n"}
    req = {"target_version": "2.0.0", "signing_key_id": _KEY_ID}
    pkg = package_from_request(req, files)
    pkg.sha256 = hashlib.sha256(pkg.serialize()).hexdigest()
    upd = _updater(tmp_path)
    result = upd.apply_update(pkg, device_id="d1")
    assert result.final_state == State.FAILED
    assert result.failure_reason == "package is not signed"
