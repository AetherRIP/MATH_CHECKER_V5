"""Phase 1 invitations: the one-time contact that admits a device.

The round trip is the acceptance test the plan names: request -> invitation ->
credentials, with a certificate that verifies against the department key alone.
The negative cases matter just as much: an invitation must not be reusable by
another device, must not answer a different request, and must not survive
tampering with the code that carries it.
"""
from __future__ import annotations

import base64
import json
from dataclasses import replace

import pytest

from app.distributed import signing
from app.peer import (AUTH_FAILED, IDENTIFIER_KEY_MISMATCH, INVITE_EXPIRED,
                      INVITE_INVALID, REVOKED, ROSTER_INCOMPLETE, WRONG_DEPARTMENT,
                      PeerVerificationError)
from app.peer import ca as peer_ca
from app.peer import identity as peer_identity
from app.peer import invite as peer_invite
from app.peer import revocation as peer_revocation
from app.peer import roster as peer_roster

NOW = 1_800_000_000.0
DAY = peer_ca.DAY_SECONDS


@pytest.fixture()
def department():
    return peer_ca.DepartmentCA.create("dept-alpha", "Alpha Department", now=NOW)


@pytest.fixture()
def owner_records(department):
    """The owner's roster and revocation list, empty to begin with."""
    return (peer_roster.Roster(department.dept_id),
            peer_revocation.RevocationList(department.dept_id))


@pytest.fixture()
def newcomer(department):
    return peer_identity.DeviceIdentity.generate(department.dept_id,
                                                 label="New laptop", now=NOW)


def _claim(department, newcomer, owner_records, **kwargs):
    """Run the whole round trip and return the accepting device's credentials."""
    roster, revocations = owner_records
    request = peer_invite.InviteRequest.create(newcomer, now=NOW)
    invitation = peer_invite.Invitation.issue(
        request, department=department, roster=roster, revocations=revocations,
        now=NOW, **kwargs)
    return invitation.accept(newcomer, request=request, now=NOW), invitation, request


def _reason(exc_info) -> str:
    return exc_info.value.reason


# ---------------------------------------------------------------------------
# the round trip
# ---------------------------------------------------------------------------

def test_round_trip_invitation_produces_a_working_certificate(
        department, newcomer, owner_records):
    credentials, invitation, request = _claim(department, newcomer, owner_records)

    assert credentials.device_id == newcomer.device_id
    assert credentials.dept_id == department.dept_id
    assert credentials.certificate.device_id == newcomer.device_id
    assert credentials.department_name == "Alpha Department"
    # The certificate verifies against the department key carried in the
    # invitation — no server, no hub, no further contact.
    peer_ca.verify_certificate(credentials.certificate,
                              credentials.department_public_key,
                              expected_dept_id=department.dept_id, now=NOW)
    # And the invited device is already on the roster it received, so it can
    # recognise itself before it ever meets another peer.
    assert credentials.roster.find(newcomer.device_id) is not None
    assert invitation.request_nonce == request.nonce
    # The device can now verify a *second* peer from the same roster alone.
    peer = peer_identity.DeviceIdentity.generate(department.dept_id, now=NOW)
    peer_cert = department.issue_certificate(peer.public_key_pem, now=NOW)
    roster_with_both = credentials.roster.with_device(peer_cert)
    extended = peer_roster.SignedRoster.sign(roster_with_both, department, now=NOW)
    credentials_with_peer = replace(
        credentials, roster=extended.verify(department.public_key_pem, now=NOW))
    assert credentials_with_peer.verify_peer(peer_cert, now=NOW).device_id == peer.device_id


def test_codes_survive_a_copy_paste(department, newcomer, owner_records):
    roster, revocations = owner_records
    request = peer_invite.InviteRequest.create(newcomer, now=NOW)
    invitation = peer_invite.Invitation.issue(
        request, department=department, roster=roster, revocations=revocations,
        now=NOW)

    # A chat client may wrap the code; the whitespace is transport noise.
    wrapped = "\n".join(invitation.to_code()[i:i + 60]
                        for i in range(0, len(invitation.to_code()), 60))
    restored = peer_invite.Invitation.from_code(wrapped)
    assert restored == invitation
    assert peer_invite.InviteRequest.from_code(request.to_code()) == request
    # Nothing in the code is a private key.
    decoded = base64.urlsafe_b64decode(
        invitation.to_code() + "=" * (-len(invitation.to_code()) % 4)).decode()
    assert "PRIVATE" not in decoded

    credentials = restored.accept(newcomer, request=request, now=NOW)
    assert credentials.certificate == invitation.certificate


def test_credentials_survive_a_restart(department, newcomer, owner_records):
    credentials, _, _ = _claim(department, newcomer, owner_records)
    path = credentials.save()
    loaded = peer_invite.PeerCredentials.load(path)
    assert loaded is not None
    assert loaded.device_id == newcomer.device_id
    assert loaded.certificate == credentials.certificate
    assert loaded.roster.epoch == credentials.roster.epoch
    # The private key is needed to keep signing after a restart.
    assert loaded.identity.sign(b"payload") == newcomer.sign(b"payload")
    assert "PRIVATE" not in json.dumps(loaded.public_dict())
    assert peer_invite.PeerCredentials.load(path.with_name("absent.json")) is None


# ---------------------------------------------------------------------------
# negatives — each with its own reason
# ---------------------------------------------------------------------------

def test_expired_request_is_refused(department, newcomer, owner_records):
    roster, revocations = owner_records
    stale = peer_invite.InviteRequest.create(newcomer, ttl_seconds=-1, now=NOW)
    with pytest.raises(PeerVerificationError) as exc:
        peer_invite.Invitation.issue(stale, department=department, roster=roster,
                                     revocations=revocations, now=NOW)
    assert _reason(exc) == INVITE_EXPIRED


def test_expired_invitation_is_refused(department, newcomer, owner_records):
    roster, revocations = owner_records
    request = peer_invite.InviteRequest.create(newcomer, now=NOW)
    invitation = peer_invite.Invitation.issue(
        request, department=department, roster=roster, revocations=revocations,
        ttl_seconds=-1, now=NOW)
    with pytest.raises(PeerVerificationError) as exc:
        invitation.accept(newcomer, request=request, now=NOW)
    assert _reason(exc) == INVITE_EXPIRED


def test_request_naming_another_department_is_refused(department, owner_records):
    roster, revocations = owner_records
    outsider = peer_identity.DeviceIdentity.generate("dept-beta", now=NOW)
    request = peer_invite.InviteRequest.create(outsider, now=NOW)
    with pytest.raises(PeerVerificationError) as exc:
        peer_invite.Invitation.issue(request, department=department, roster=roster,
                                     revocations=revocations, now=NOW)
    assert _reason(exc) == WRONG_DEPARTMENT


def test_invitation_cannot_be_accepted_by_another_device(department, owner_records):
    roster, revocations = owner_records
    invited = peer_identity.DeviceIdentity.generate(department.dept_id, now=NOW)
    impostor = peer_identity.DeviceIdentity.generate(department.dept_id, now=NOW)
    request = peer_invite.InviteRequest.create(invited, now=NOW)
    invitation = peer_invite.Invitation.issue(
        request, department=department, roster=roster, revocations=revocations,
        now=NOW)
    with pytest.raises(PeerVerificationError) as exc:
        invitation.accept(impostor, request=request, now=NOW)
    assert _reason(exc) == IDENTIFIER_KEY_MISMATCH


def test_invitation_is_bound_to_the_request_it_answers(department, newcomer,
                                                       owner_records):
    roster, revocations = owner_records
    answered = peer_invite.InviteRequest.create(newcomer, now=NOW)
    other = peer_invite.InviteRequest.create(newcomer, now=NOW)
    assert answered.nonce != other.nonce
    invitation = peer_invite.Invitation.issue(
        answered, department=department, roster=roster, revocations=revocations,
        now=NOW)
    with pytest.raises(PeerVerificationError) as exc:
        invitation.accept(newcomer, request=other, now=NOW)
    assert _reason(exc) == INVITE_INVALID


def test_tampered_invitation_is_refused(department, newcomer, owner_records):
    _, invitation, request = _claim(department, newcomer, owner_records)
    document = invitation.to_dict()
    document["expires_at"] = invitation.expires_at + 10 * DAY
    tampered = peer_invite.Invitation.from_code(
        base64.urlsafe_b64encode(json.dumps(document).encode()).decode().rstrip("="))
    with pytest.raises(PeerVerificationError) as exc:
        tampered.accept(newcomer, request=request, now=NOW)
    assert _reason(exc) == AUTH_FAILED


def test_garbled_code_is_refused(department, newcomer, owner_records):
    _, invitation, _ = _claim(department, newcomer, owner_records)
    truncated = invitation.to_code()[:-40]
    for code in (truncated, "", "  ", "not a code!", invitation.to_code() + "AAAA"):
        with pytest.raises(PeerVerificationError) as exc:
            peer_invite.Invitation.from_code(code)
        assert _reason(exc) == INVITE_INVALID


def test_request_signature_must_verify(department, newcomer, owner_records):
    """A request that was not signed by the key it carries is refused.

    This is what stops an attacker forwarding a request to have a certificate
    planted on a device that never asked for one.
    """
    roster, revocations = owner_records
    request = peer_invite.InviteRequest.create(newcomer, now=NOW)
    stranger = peer_identity.DeviceIdentity.generate(department.dept_id, now=NOW)
    forged = replace(request, device_public_key=stranger.public_key_pem)
    with pytest.raises(PeerVerificationError) as exc:
        peer_invite.Invitation.issue(forged, department=department, roster=roster,
                                     revocations=revocations, now=NOW)
    assert _reason(exc) == AUTH_FAILED


def test_revoked_device_cannot_accept_an_invitation(department, newcomer,
                                                    owner_records):
    roster, revocations = owner_records
    revocations = revocations.revoke(newcomer.device_id, reason="retired", now=NOW)
    request = peer_invite.InviteRequest.create(newcomer, now=NOW)
    invitation = peer_invite.Invitation.issue(
        request, department=department, roster=roster, revocations=revocations,
        now=NOW)
    with pytest.raises(PeerVerificationError) as exc:
        invitation.accept(newcomer, request=request, now=NOW)
    assert _reason(exc) == REVOKED


def test_credentials_refuse_a_peer_that_is_not_on_the_roster(department,
                                                             newcomer,
                                                             owner_records):
    credentials, _, _ = _claim(department, newcomer, owner_records)
    stranger = peer_identity.DeviceIdentity.generate(department.dept_id, now=NOW)
    stranger_cert = department.issue_certificate(stranger.public_key_pem, now=NOW)
    with pytest.raises(PeerVerificationError) as exc:
        credentials.verify_peer(stranger_cert, now=NOW)
    assert _reason(exc) == ROSTER_INCOMPLETE


def test_credentials_refuse_an_expired_own_certificate(department, newcomer,
                                                       owner_records):
    credentials, _, _ = _claim(department, newcomer, owner_records)
    credentials.assert_certificate_current(now=NOW)
    with pytest.raises(PeerVerificationError) as exc:
        credentials.assert_certificate_current(
            now=NOW + peer_ca.CERT_DEFAULT_DAYS * DAY + 1)
    assert _reason(exc) == "CERT_EXPIRED"


def test_invitation_contains_the_roster_and_revocations_it_was_issued_with(
        department, newcomer, owner_records):
    roster, revocations = owner_records
    other = peer_identity.DeviceIdentity.generate(department.dept_id, now=NOW)
    roster = roster.with_device(department.issue_certificate(other.public_key_pem,
                                                             now=NOW))
    revocations = revocations.revoke("a-retired-device", now=NOW)
    request = peer_invite.InviteRequest.create(newcomer, now=NOW)
    invitation = peer_invite.Invitation.issue(
        request, department=department, roster=roster, revocations=revocations,
        now=NOW)
    credentials = invitation.accept(newcomer, request=request, now=NOW)
    assert credentials.roster.find(other.device_id) is not None
    assert credentials.revocations.is_revoked("a-retired-device")
    assert credentials.revocations.epoch == revocations.epoch


def test_invitation_signature_is_not_transferable_to_another_department(
        department):
    """An invitation's department key is what verifies it; swapping it fails."""
    beta = peer_ca.DepartmentCA.create("dept-beta", "Beta", now=NOW)
    newcomer = peer_identity.DeviceIdentity.generate(department.dept_id, now=NOW)
    request = peer_invite.InviteRequest.create(newcomer, now=NOW)
    invitation = peer_invite.Invitation.issue(
        request, department=department,
        roster=peer_roster.Roster(department.dept_id),
        revocations=peer_revocation.RevocationList(department.dept_id), now=NOW)
    swapped = replace(invitation, department_public_key=beta.public_key_pem)
    with pytest.raises(PeerVerificationError) as exc:
        swapped.accept(newcomer, request=request, now=NOW)
    assert _reason(exc) == AUTH_FAILED
