"""Phase 1 identity: certificates, the roster and revocation.

Every rejection here asserts the *reason*, not merely that it failed. The
interface has to explain them differently (plan §10.2), and \"rejected for the
wrong reason\" is exactly how a security test can pass while the hole it was
meant to cover stays open.
"""
from __future__ import annotations

import json
from dataclasses import replace

import pytest

from app.distributed import accounts, audit as distributed_audit, schema, signing
from app.peer import (AUTH_FAILED, CERT_EXPIRED, CERT_NOT_YET_VALID,
                      DUPLICATE_DEVICE_ID, EPOCH_ROLLBACK, IDENTIFIER_KEY_MISMATCH,
                      REVOKED, VERSION_MISMATCH, WRONG_DEPARTMENT, PeerError,
                      PeerVerificationError)
from app.peer import ca as peer_ca
from app.peer import identity as peer_identity
from app.peer import revocation as peer_revocation
from app.peer import roster as peer_roster

# One fixed clock for the whole file: a certificate test that drifts with the
# wall clock is a test that expires on a Sunday.
NOW = 1_800_000_000.0
DAY = peer_ca.DAY_SECONDS


@pytest.fixture(autouse=True)
def distributed_tables_for_this_database(fresh_state):
    """Rebuild the distributed tables in THIS test's database.

    ``accounts.ensure_tables_ready()`` memoises with a process-global flag, and
    ``fresh_state`` gives each test a new database file without clearing that
    flag. Without this reset the audit write in
    ``revoke_device(..., audit=True)`` reaches a database that has no
    ``audit_log`` table, because the *previous* test's database was the one the
    tables were created in. ``test_device_discovery.py`` resets the same flag for
    the same reason.
    """
    accounts._TABLES_READY = False
    schema.ensure_tables()
    yield
    accounts._TABLES_READY = False


@pytest.fixture()
def department():
    return peer_ca.DepartmentCA.create("dept-alpha", "Alpha Department", now=NOW)


@pytest.fixture()
def other_department():
    return peer_ca.DepartmentCA.create("dept-beta", "Beta Department", now=NOW)


@pytest.fixture()
def device(department):
    return peer_identity.DeviceIdentity.generate(department.dept_id,
                                                 label="Teacher laptop", now=NOW)


@pytest.fixture()
def certificate(department, device):
    return department.issue_certificate(device.public_key_pem,
                                        label=device.label, now=NOW)


def _reason(exc_info) -> str:
    return exc_info.value.reason


# ---------------------------------------------------------------------------
# identity
# ---------------------------------------------------------------------------

def test_device_identifier_is_derived_from_the_public_key(department):
    first = peer_identity.DeviceIdentity.generate(department.dept_id, now=NOW)
    second = peer_identity.DeviceIdentity.generate(department.dept_id, now=NOW)
    assert first.device_id != second.device_id
    assert len(first.device_id) == peer_identity.DEVICE_ID_LENGTH
    # Deterministic: the same key always yields the same identifier, whatever
    # PEM formatting the caller used.
    assert (peer_identity.device_id_for_public_key(first.public_key_pem)
            == first.device_id)
    assert (peer_identity.device_id_for_public_key(
        first.public_key_pem.replace("\n", "\r\n")) == first.device_id)


def test_identity_with_a_foreign_identifier_is_refused(department, device):
    with pytest.raises(PeerVerificationError) as exc:
        replace(device, device_id="A" * peer_identity.DEVICE_ID_LENGTH)
    assert _reason(exc) == IDENTIFIER_KEY_MISMATCH


def test_identity_is_persisted_through_the_secure_store(department):
    identity = peer_identity.DeviceIdentity.generate(department.dept_id,
                                                     label="Room 12", now=NOW)
    path = identity.save()
    loaded = peer_identity.DeviceIdentity.load(path)
    assert loaded is not None
    assert loaded.device_id == identity.device_id
    assert loaded.dept_id == department.dept_id
    assert loaded.label == "Room 12"
    # The private key is needed to sign, so it must survive the round trip…
    assert loaded.sign(b"payload") == identity.sign(b"payload")
    # …but it is never part of the metadata that gets shared.
    assert "PRIVATE" not in json.dumps(identity.to_dict())
    assert "private" not in json.dumps(identity.to_dict())


def test_missing_identity_file_is_none_but_a_broken_one_raises(tmp_path):
    assert peer_identity.DeviceIdentity.load(tmp_path / "absent.json") is None
    broken = tmp_path / "identity.json"
    broken.write_bytes(b"not a secure-store file at all")
    with pytest.raises(peer_identity.PeerErrorStoreUnreadable):
        peer_identity.DeviceIdentity.load(broken)


def test_device_already_bound_to_another_department_is_refused(department,
                                                               other_department):
    identity, created = peer_identity.load_or_create(department.dept_id, now=NOW)
    assert created is True
    again, created_again = peer_identity.load_or_create(department.dept_id, now=NOW)
    assert created_again is False and again.device_id == identity.device_id
    with pytest.raises(PeerVerificationError) as exc:
        peer_identity.load_or_create(other_department.dept_id, now=NOW)
    assert _reason(exc) == WRONG_DEPARTMENT


# ---------------------------------------------------------------------------
# certificates
# ---------------------------------------------------------------------------

def test_certificate_round_trips_through_json(certificate, department):
    restored = peer_ca.DeviceCertificate.from_json(certificate.to_json())
    assert restored == certificate
    verified = peer_ca.verify_certificate(restored, department.public_key_pem,
                                          expected_dept_id=department.dept_id,
                                          now=NOW)
    assert verified.device_id == certificate.device_id
    assert verified.covers(NOW) and not verified.covers(NOW + 100 * DAY)


def test_certificate_signature_covers_every_field(certificate, department):
    for tampered in (replace(certificate, label="Someone else"),
                     replace(certificate, not_after=certificate.not_after + DAY),
                     replace(certificate, roles=("revoker",))):
        with pytest.raises(PeerVerificationError) as exc:
            peer_ca.verify_certificate(tampered, department.public_key_pem, now=NOW)
        assert _reason(exc) == AUTH_FAILED


def test_certificate_signed_by_another_key_is_refused(certificate,
                                                      other_department):
    with pytest.raises(PeerVerificationError) as exc:
        peer_ca.verify_certificate(certificate, other_department.public_key_pem,
                                   now=NOW)
    assert _reason(exc) == AUTH_FAILED


def test_certificate_from_another_department_is_refused(certificate,
                                                        other_department):
    # The signature also fails here; the department check runs first so the
    # reason is the specific one a teacher can act on.
    with pytest.raises(PeerVerificationError) as exc:
        peer_ca.verify_certificate(certificate, other_department.public_key_pem,
                                   expected_dept_id=other_department.dept_id,
                                   now=NOW)
    assert _reason(exc) == WRONG_DEPARTMENT


def test_expired_and_not_yet_valid_certificates_are_distinguished(certificate,
                                                                  department):
    with pytest.raises(PeerVerificationError) as exc:
        peer_ca.verify_certificate(certificate, department.public_key_pem,
                                   now=NOW + peer_ca.CERT_DEFAULT_DAYS * DAY + 1)
    assert _reason(exc) == CERT_EXPIRED

    with pytest.raises(PeerVerificationError) as exc:
        peer_ca.verify_certificate(certificate, department.public_key_pem,
                                   now=NOW - 1)
    assert _reason(exc) == CERT_NOT_YET_VALID


def test_certificate_whose_identifier_does_not_match_its_key_is_refused(
        department, device):
    """A CA that signs an inconsistent certificate must not be trusted for it.

    The certificate is re-signed with the real root key, so the *only* thing
    wrong with it is that the identifier names another device's key — the exact
    attack content-addressing exists to stop (plan §9.2).
    """
    stranger = peer_identity.DeviceIdentity.generate(department.dept_id, now=NOW)
    forged = department.issue_certificate(stranger.public_key_pem, now=NOW)
    forged = replace(forged, device_id=device.device_id, signature="")
    forged = replace(forged, signature=signing.sign_package(
        forged.signed_payload(), department.private_key_pem))

    with pytest.raises(PeerVerificationError) as exc:
        peer_ca.verify_certificate(forged, department.public_key_pem, now=NOW)
    assert _reason(exc) == IDENTIFIER_KEY_MISMATCH


def test_unknown_certificate_version_is_refused(certificate, department):
    with pytest.raises(PeerVerificationError) as exc:
        peer_ca.verify_certificate(replace(certificate, version=99),
                                   department.public_key_pem, now=NOW)
    assert _reason(exc) == VERSION_MISMATCH
    with pytest.raises(PeerVerificationError) as exc:
        peer_ca.DeviceCertificate.from_dict({**certificate.to_dict(),
                                             "format": "something-else"})
    assert _reason(exc) == VERSION_MISMATCH


def test_issuance_refuses_an_identifier_that_belongs_to_another_key(
        department, device):
    stranger = peer_identity.DeviceIdentity.generate(department.dept_id, now=NOW)
    with pytest.raises(PeerVerificationError) as exc:
        department.issue_certificate(stranger.public_key_pem,
                                     device_id=device.device_id, now=NOW)
    assert _reason(exc) == IDENTIFIER_KEY_MISMATCH


def test_only_the_owner_machine_can_issue(department, device):
    public_only = replace(department, private_key_pem=None)
    assert public_only.can_issue is False
    with pytest.raises(PeerError):
        public_only.issue_certificate(device.public_key_pem, now=NOW)


def test_certificate_lifetime_is_bounded(department, device):
    with pytest.raises(ValueError):
        department.issue_certificate(device.public_key_pem,
                                     days=peer_ca.CERT_MAX_DAYS + 1, now=NOW)
    with pytest.raises(ValueError):
        department.issue_certificate(device.public_key_pem, days=0, now=NOW)


# ---------------------------------------------------------------------------
# roster
# ---------------------------------------------------------------------------

def test_roster_lists_and_removes_devices(department, device, certificate):
    second = peer_identity.DeviceIdentity.generate(department.dept_id, now=NOW)
    second_cert = department.issue_certificate(second.public_key_pem, now=NOW)

    roster = peer_roster.Roster(department.dept_id)
    roster = roster.with_device(certificate)
    assert roster.epoch == 1 and len(roster) == 1
    roster = roster.with_device(second_cert)
    assert roster.epoch == 2 and set(roster.device_ids()) == {device.device_id,
                                                              second.device_id}
    signed = peer_roster.SignedRoster.sign(roster, department, now=NOW)
    verified = signed.verify(department.public_key_pem,
                             expected_dept_id=department.dept_id, now=NOW)
    assert verified.find(device.device_id) is not None

    trimmed = roster.without_device(device.device_id)
    assert trimmed.epoch == 3 and trimmed.find(device.device_id) is None
    # Removing an absent device is a no-op, not a new epoch.
    assert trimmed.without_device("nonexistent").epoch == 3


def test_roster_rejects_a_duplicate_identifier_with_a_different_key(
        department, certificate):
    stranger = peer_identity.DeviceIdentity.generate(department.dept_id, now=NOW)
    impostor = replace(certificate, device_public_key=stranger.public_key_pem)
    roster = peer_roster.Roster(department.dept_id).with_device(certificate)
    with pytest.raises(PeerVerificationError) as exc:
        roster.with_device(impostor)
    assert _reason(exc) == DUPLICATE_DEVICE_ID


def test_roster_refuses_to_hold_another_departments_certificate(
        department, other_department):
    foreign = other_department.issue_certificate(
        peer_identity.DeviceIdentity.generate(other_department.dept_id,
                                              now=NOW).public_key_pem, now=NOW)
    with pytest.raises(PeerVerificationError) as exc:
        peer_roster.Roster(department.dept_id).with_device(foreign)
    assert _reason(exc) == WRONG_DEPARTMENT


def test_tampered_roster_is_refused(department, certificate):
    signed = peer_roster.SignedRoster.sign(
        peer_roster.Roster(department.dept_id).with_device(certificate),
        department, now=NOW)
    document = signed.to_dict()
    document["epoch"] = 99
    with pytest.raises(PeerVerificationError) as exc:
        peer_roster.SignedRoster.from_dict(document).verify(
            department.public_key_pem, now=NOW)
    assert _reason(exc) == AUTH_FAILED


def test_roster_epoch_rollback_is_refused(department, certificate):
    roster = peer_roster.Roster(department.dept_id).with_device(certificate)
    signed = peer_roster.SignedRoster.sign(roster, department, now=NOW)
    with pytest.raises(PeerVerificationError) as exc:
        signed.verify(department.public_key_pem, now=NOW, known_epoch=roster.epoch + 1)
    assert _reason(exc) == EPOCH_ROLLBACK


def test_only_the_owner_machine_can_sign_a_roster(department, certificate):
    roster = peer_roster.Roster(department.dept_id).with_device(certificate)
    with pytest.raises(PeerError):
        peer_roster.SignedRoster.sign(roster, replace(department,
                                                      private_key_pem=None), now=NOW)


# ---------------------------------------------------------------------------
# revocation
# ---------------------------------------------------------------------------

def test_revoked_device_is_refused(department, device, certificate):
    revocations = peer_revocation.RevocationList(department.dept_id).revoke(
        device.device_id, reason="left the school", actor="owner", now=NOW)
    signed = peer_revocation.SignedRevocationList.sign(revocations, department,
                                                       now=NOW)
    verified = signed.verify(department.public_key_pem,
                             expected_dept_id=department.dept_id, now=NOW)
    assert verified.is_revoked(device.device_id)

    with pytest.raises(PeerVerificationError) as exc:
        peer_ca.verify_certificate(certificate, department.public_key_pem,
                                   now=NOW, revoked=verified)
    assert _reason(exc) == REVOKED


def test_unrelated_devices_are_not_revoked_by_proxy(department, device,
                                                    certificate):
    stranger = peer_identity.DeviceIdentity.generate(department.dept_id, now=NOW)
    revocations = peer_revocation.RevocationList(department.dept_id).revoke(
        stranger.device_id, now=NOW)
    peer_ca.verify_certificate(certificate, department.public_key_pem, now=NOW,
                               revoked=revocations)


def test_revocation_epoch_rollback_is_refused(department, device):
    revocations = (peer_revocation.RevocationList(department.dept_id)
                   .revoke(device.device_id, now=NOW).revoke("other-device", now=NOW))
    signed = peer_revocation.SignedRevocationList.sign(revocations, department,
                                                       now=NOW)
    with pytest.raises(PeerVerificationError) as exc:
        signed.verify(department.public_key_pem, now=NOW,
                      known_epoch=revocations.epoch + 1)
    assert _reason(exc) == EPOCH_ROLLBACK


def test_revoking_twice_does_not_inflate_the_epoch(department, device):
    once = peer_revocation.RevocationList(department.dept_id).revoke(
        device.device_id, now=NOW)
    twice = once.revoke(device.device_id, now=NOW + 60)
    assert twice.epoch == once.epoch
    assert len(twice) == 1


def test_revoke_device_verifies_before_extending_and_audits(department, device):
    signed = peer_revocation.SignedRevocationList.sign(
        peer_revocation.RevocationList(department.dept_id), department, now=NOW)
    updated = peer_revocation.revoke_device(
        signed, device.device_id, department=department, reason="retired",
        actor="owner", now=NOW)
    assert updated.revocations.epoch == 1
    assert updated.revocations.is_revoked(device.device_id)

    events = distributed_audit.list_audit(action="peer.device_revoked")
    assert len(events) == 1
    assert events[0]["target_id"] == device.device_id
    assert events[0]["details"]["epoch"] == 1
    assert "retired" in json.dumps(events[0]["details"])


def test_revoke_device_refuses_a_tampered_local_list(department, device):
    signed = peer_revocation.SignedRevocationList.sign(
        peer_revocation.RevocationList(department.dept_id), department, now=NOW)
    document = signed.to_dict()
    document["epoch"] = 7
    tampered = peer_revocation.SignedRevocationList.from_dict(document)
    with pytest.raises(PeerVerificationError) as exc:
        peer_revocation.revoke_device(tampered, device.device_id,
                                      department=department, now=NOW, audit=False)
    assert _reason(exc) == AUTH_FAILED


# ---------------------------------------------------------------------------
# key hygiene
# ---------------------------------------------------------------------------

def test_no_private_key_reaches_a_shared_document(department, device, certificate):
    roster = peer_roster.SignedRoster.sign(
        peer_roster.Roster(department.dept_id).with_device(certificate),
        department, now=NOW)
    revocations = peer_revocation.SignedRevocationList.sign(
        peer_revocation.RevocationList(department.dept_id), department, now=NOW)
    for document in (json.dumps(department.public_dict()),
                     json.dumps(certificate.to_dict()),
                     roster.to_json(),
                     revocations.to_json()):
        assert "PRIVATE KEY" not in document
        assert "private_key" not in document
