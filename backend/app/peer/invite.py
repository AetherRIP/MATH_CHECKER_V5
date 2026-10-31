"""The one-time invitation: how a new device joins a department.

Plan §15.7 is a real constraint, not an implementation detail: a device that has
never met an authorised machine cannot obtain a certificate. The invitation is
that single contact, and it is designed to be safe to send over a channel the
department already trusts (or to read aloud in a staff room).

The exchange is a round trip, and each half proves something different:

1. **Request** (new device -> owner). The new device generates its *own* key
   pair and sends the public half, signed with the private half. That signature
   proves possession: the owner knows the key it is about to certify belongs to
   whoever will use the certificate, so a request cannot be forwarded to plant a
   certificate on a third party. The private key never leaves the device.
2. **Invitation** (owner -> new device). The owner issues a certificate, adds it
   to the roster, and returns the certificate with the department's *public* key,
   the signed roster and the signed revocation list — all root-signed, and bound
   to the request's nonce.

Both halves are copy-pasteable codes (default) rather than files (plan §16, D3),
because a code survives chat, a scribbled note and a locked-down USB policy
without an install step. Codes are base64url of a canonical JSON document, so a
truncation or a mangled character fails loudly instead of decoding to something
subtly different.
"""
from __future__ import annotations

import base64
import json
import secrets
import time
from dataclasses import dataclass
from pathlib import Path

from ..distributed import secure_store, signing
from . import (AUTH_FAILED, CERT_EXPIRED, IDENTIFIER_KEY_MISMATCH, INVITE_EXPIRED,
               INVITE_INVALID, PROTOCOL_VERSION, ROSTER_INCOMPLETE,
               VERSION_MISMATCH, WRONG_DEPARTMENT, PeerError, PeerVerificationError)
from .ca import (CERT_DEFAULT_DAYS, DeviceCertificate, check_not_future_dated,
                 verify_certificate)
from .identity import DeviceIdentity, canonical_json, peer_dir, raw_public_bytes
from .revocation import RevocationList, SignedRevocationList
from .roster import Roster, SignedRoster

REQUEST_FORMAT = "mc5-peer-invite-request-v1"
INVITATION_FORMAT = "mc5-peer-invitation-v1"

#: How long a request stays usable. Long enough to email it and answer the same
#: day; short enough that a leaked request is not a standing invitation.
REQUEST_TTL_SECONDS = 24 * 60 * 60
#: How long the invitation code stays usable. The certificate inside is valid for
#: ``CERT_DEFAULT_DAYS``; the code only has to survive being delivered.
INVITATION_TTL_SECONDS = 7 * 24 * 60 * 60

#: Guards the decode step: an invitation legitimately carries a roster, but a
#: pasted megabyte is not one.
MAX_CODE_CHARS = 200_000


def _encode_code(payload: dict) -> str:
    return base64.urlsafe_b64encode(canonical_json(payload)).decode("ascii").rstrip("=")


def _decode_code(code: str) -> dict:
    """Decode a copy-paste code, refusing anything that is not one.

    Strips the whitespace a chat client inserts, then requires the remainder to
    be the exact base64url alphabet. A truncated or bit-flipped code is reported
    as ``INVITE_INVALID`` here, before any signature work: the code *is* the
    transport, so an unreadable code is a transport failure, not a forgery.
    """
    if not isinstance(code, str):
        raise PeerVerificationError(INVITE_INVALID, "an invitation code must be text")
    # Trailing base64 padding is redundant (the length is known) and other tools
    # routinely emit it, so it is tolerated rather than reported as corruption.
    text = "".join(code.split()).rstrip("=")
    if not text:
        raise PeerVerificationError(INVITE_INVALID, "the invitation code is empty")
    if len(text) > MAX_CODE_CHARS:
        raise PeerVerificationError(INVITE_INVALID, "the invitation code is too large")
    if any(ch not in ("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_")
           for ch in text):
        raise PeerVerificationError(
            INVITE_INVALID, "the invitation code contains unexpected characters")
    padded = text + "=" * (-len(text) % 4)
    try:
        raw = base64.urlsafe_b64decode(padded.encode("ascii"))
        payload = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise PeerVerificationError(
            INVITE_INVALID, "the invitation code is not valid base64 JSON") from exc
    if not isinstance(payload, dict):
        raise PeerVerificationError(INVITE_INVALID, "the invitation code has no document")
    return payload


def _check_format(payload: dict, fmt: str) -> None:
    if payload.get("format") != fmt or payload.get("version") != PROTOCOL_VERSION:
        raise PeerVerificationError(VERSION_MISMATCH,
                                    f"unsupported document format {payload.get('format')!r}")


@dataclass(frozen=True)
class InviteRequest:
    """A new device asking to be admitted, signed with its own key."""

    dept_id: str
    device_public_key: str
    nonce: str
    created_at: float
    expires_at: float
    signature: str
    label: str | None = None
    format: str = REQUEST_FORMAT
    version: int = PROTOCOL_VERSION

    @classmethod
    def create(cls, identity: DeviceIdentity, *, label: str | None = None,
               ttl_seconds: float = REQUEST_TTL_SECONDS,
               now: float | None = None) -> "InviteRequest":
        """Build a request for ``identity`` (the device's own key pair)."""
        moment = float(now if now is not None else time.time())
        draft = cls(dept_id=identity.dept_id,
                    device_public_key=identity.public_key_pem,
                    nonce=secrets.token_hex(16),
                    created_at=moment,
                    expires_at=moment + float(ttl_seconds),
                    signature="",
                    label=label if label is not None else identity.label)
        signature = identity.sign(draft.signed_payload())
        return cls(**{**draft.__dict__, "signature": signature})

    def signed_payload(self) -> bytes:
        return canonical_json({
            "format": self.format,
            "version": self.version,
            "dept_id": self.dept_id,
            "device_public_key": self.device_public_key,
            "label": self.label,
            "nonce": self.nonce,
            "created_at": self.created_at,
            "expires_at": self.expires_at,
        })

    def to_dict(self) -> dict:
        return {**json.loads(self.signed_payload().decode("utf-8")),
                "signature": self.signature}

    def to_code(self) -> str:
        return _encode_code(self.to_dict())

    @classmethod
    def from_dict(cls, data) -> "InviteRequest":
        _check_format(data, REQUEST_FORMAT)
        try:
            return cls(dept_id=str(data["dept_id"]),
                       device_public_key=str(data["device_public_key"]),
                       nonce=str(data["nonce"]),
                       created_at=float(data["created_at"]),
                       expires_at=float(data["expires_at"]),
                       signature=str(data["signature"]),
                       label=None if data.get("label") is None else str(data["label"]))
        except (KeyError, TypeError, ValueError) as exc:
            raise PeerVerificationError(
                INVITE_INVALID, "the invitation request is missing a field") from exc

    @classmethod
    def from_code(cls, code: str) -> "InviteRequest":
        return cls.from_dict(_decode_code(code))

    def verify(self, *, now: float | None = None) -> "InviteRequest":
        """Verify the self-signature and the expiry, or raise.

        The signature is checked before the expiry so a tampered code is
        reported as tampered rather than as old.
        """
        if not self.signature:
            raise PeerVerificationError(AUTH_FAILED, "the invitation request is unsigned")
        if not signing.verify_package(self.signed_payload(), self.signature,
                                      self.device_public_key):
            raise PeerVerificationError(
                AUTH_FAILED, "the invitation request signature does not verify")
        raw_public_bytes(self.device_public_key)
        moment = float(now if now is not None else time.time())
        check_not_future_dated(self.created_at, moment)
        if moment >= self.expires_at:
            raise PeerVerificationError(INVITE_EXPIRED, "the invitation request has expired")
        return self

    @property
    def device_id(self) -> str:
        from .identity import device_id_for_public_key

        return device_id_for_public_key(self.device_public_key)


@dataclass(frozen=True)
class Invitation:
    """The owner's answer: a certificate, the department key, roster, revocations."""

    dept_id: str
    department_name: str | None
    department_public_key: str
    certificate: DeviceCertificate
    roster: SignedRoster
    revocations: SignedRevocationList
    request_nonce: str
    issued_at: float
    expires_at: float
    signature: str
    format: str = INVITATION_FORMAT
    version: int = PROTOCOL_VERSION

    # -- issuance ------------------------------------------------------------

    @classmethod
    def issue(cls, request: InviteRequest, *, department, roster: Roster,
              revocations: RevocationList, roles=(),
              days: int = CERT_DEFAULT_DAYS,
              ttl_seconds: float = INVITATION_TTL_SECONDS,
              now: float | None = None) -> "Invitation":
        """Answer a verified request with a signed invitation.

        Everything the new device needs is inside, and nothing it does not: the
        root's *public* key, its own certificate, and the two signed documents
        that let it verify any other peer it later meets.
        """
        request.verify(now=now)
        if request.dept_id != department.dept_id:
            raise PeerVerificationError(
                WRONG_DEPARTMENT, "the request names a different department")
        if not getattr(department, "private_key_pem", None):
            raise PeerError("only the owner machine may issue an invitation")
        moment = float(now if now is not None else time.time())

        certificate = department.issue_certificate(
            request.device_public_key, device_id=request.device_id,
            roles=roles, label=request.label, days=days, now=moment)
        # Add the new device to the roster *before* signing it, so the invited
        # device's own roster already contains itself — otherwise it would hold
        # a certificate no roster vouches for until the owner gossiped an update.
        updated_roster = roster.with_device(certificate)
        signed_roster = SignedRoster.sign(updated_roster, department, now=moment)
        signed_revocations = SignedRevocationList.sign(revocations, department, now=moment)

        draft = cls(dept_id=department.dept_id,
                    department_name=department.name,
                    department_public_key=department.public_key_pem,
                    certificate=certificate,
                    roster=signed_roster,
                    revocations=signed_revocations,
                    request_nonce=request.nonce,
                    issued_at=moment,
                    expires_at=moment + float(ttl_seconds),
                    signature="")
        signature = signing.sign_package(draft.signed_payload(),
                                         department.private_key_pem)
        return cls(**{**draft.__dict__, "signature": signature})

    def signed_payload(self) -> bytes:
        return canonical_json({
            "format": self.format,
            "version": self.version,
            "dept_id": self.dept_id,
            "department_name": self.department_name,
            "department_public_key": self.department_public_key,
            "certificate": self.certificate.to_dict(),
            "roster": self.roster.to_dict(),
            "revocations": self.revocations.to_dict(),
            "request_nonce": self.request_nonce,
            "issued_at": self.issued_at,
            "expires_at": self.expires_at,
        })

    def to_dict(self) -> dict:
        return {**json.loads(self.signed_payload().decode("utf-8")),
                "signature": self.signature}

    def to_code(self) -> str:
        return _encode_code(self.to_dict())

    @classmethod
    def from_dict(cls, data) -> "Invitation":
        _check_format(data, INVITATION_FORMAT)
        try:
            return cls(dept_id=str(data["dept_id"]),
                       department_name=(None if data.get("department_name") is None
                                        else str(data["department_name"])),
                       department_public_key=str(data["department_public_key"]),
                       certificate=DeviceCertificate.from_dict(data["certificate"]),
                       roster=SignedRoster.from_dict(data["roster"]),
                       revocations=SignedRevocationList.from_dict(data["revocations"]),
                       request_nonce=str(data["request_nonce"]),
                       issued_at=float(data["issued_at"]),
                       expires_at=float(data["expires_at"]),
                       signature=str(data.get("signature") or ""))
        except (KeyError, TypeError, ValueError) as exc:
            raise PeerVerificationError(
                INVITE_INVALID, "the invitation is missing a field") from exc

    @classmethod
    def from_code(cls, code: str) -> "Invitation":
        return cls.from_dict(_decode_code(code))

    # -- acceptance ----------------------------------------------------------

    def accept(self, identity: DeviceIdentity, *,
               request: InviteRequest | None = None,
               expected_department_public_key: str | None = None,
               known_revocation_epoch: int | None = None,
               known_roster_epoch: int | None = None,
               now: float | None = None) -> "PeerCredentials":
        """Verify the whole chain and return this device's credentials.

        Verification order is fixed so each failure has one reason:
        envelope signature -> pinned department key -> expiry -> certificate
        chain -> identity binding -> request binding -> roster -> revocation.

        Pass ``expected_department_public_key`` whenever this device already
        trusts a department key (``PeerCredentials.department_public_key``).
        The envelope is signed only by the key it carries, so without pinning a
        second code signed by a different root would quietly re-bind the device
        to that root, leaving the teacher to notice as the only defence.
        """
        if not self.signature:
            raise PeerVerificationError(AUTH_FAILED, "the invitation is unsigned")
        if not signing.verify_package(self.signed_payload(), self.signature,
                                      self.department_public_key):
            raise PeerVerificationError(
                AUTH_FAILED, "the invitation signature does not verify")

        if expected_department_public_key is not None:
            if (raw_public_bytes(self.department_public_key)
                    != raw_public_bytes(expected_department_public_key)):
                raise PeerVerificationError(
                    WRONG_DEPARTMENT,
                    "this invitation was signed by a different department key")

        moment = float(now if now is not None else time.time())
        check_not_future_dated(self.issued_at, moment)
        if moment >= self.expires_at:
            raise PeerVerificationError(INVITE_EXPIRED, "the invitation has expired")

        # Both signed documents are verified BEFORE they are used to make a
        # decision: a revocation list whose own signature is bad must not be
        # allowed to answer "is this device revoked?" either way.
        revocations = self.revocations.verify(
            self.department_public_key, expected_dept_id=self.dept_id,
            known_epoch=known_revocation_epoch, now=moment)
        roster = self.roster.verify(self.department_public_key,
                                    expected_dept_id=self.dept_id, now=moment,
                                    known_epoch=known_roster_epoch)
        verify_certificate(self.certificate, self.department_public_key,
                           expected_dept_id=self.dept_id, now=moment,
                           revoked=revocations)

        if (self.certificate.device_id != identity.device_id
                or self.certificate.device_public_key.strip() != identity.public_key_pem.strip()):
            raise PeerVerificationError(
                IDENTIFIER_KEY_MISMATCH,
                "this certificate was issued to a different key")

        if request is not None:
            if request.nonce != self.request_nonce:
                raise PeerVerificationError(
                    INVITE_INVALID, "the invitation answers a different request")
            if request.device_id != identity.device_id:
                raise PeerVerificationError(
                    IDENTIFIER_KEY_MISMATCH,
                    "the request was made by a different device")

        if roster.find(self.certificate.device_id) is None:
            raise PeerVerificationError(
                ROSTER_INCOMPLETE, "the roster does not list this device")

        return PeerCredentials(identity=identity, certificate=self.certificate,
                               roster=roster, revocations=revocations,
                               dept_id=self.dept_id,
                               department_name=self.department_name,
                               department_public_key=self.department_public_key,
                               accepted_at=moment)

    def __str__(self) -> str:                 # pragma: no cover - debugging aid
        return (f"Invitation(dept={self.dept_id!r}, "
                f"device={self.certificate.device_id}, nonce={self.request_nonce[:8]}…)")


def _assert_credentials_are_consistent(credentials: "PeerCredentials", *,
                                       source: str) -> None:
    """Refuse a saved credential whose parts describe different devices.

    ``load`` cannot get this from a signature: every document in the file is
    individually valid, and the question is whether they belong together. A
    file naming one department in ``dept_id`` and another in its certificate,
    or pairing this device's certificate with another device's key, would
    otherwise load silently and fail later -- at a handshake, or never (§7).
    """
    if (credentials.identity.dept_id != credentials.dept_id
            or credentials.certificate.dept_id != credentials.dept_id):
        raise PeerError(
            f"credentials file names more than one department: {source}")
    try:
        certificate_key = raw_public_bytes(
            credentials.certificate.device_public_key)
    except PeerVerificationError as exc:
        raise PeerError(
            f"credentials file holds an unreadable certificate key: "
            f"{source}") from exc
    if (credentials.certificate.device_id != credentials.identity.device_id
            or certificate_key
            != raw_public_bytes(credentials.identity.public_key_pem)):
        raise PeerError(
            f"credentials file names a certificate for another device: {source}")


@dataclass(frozen=True)
class PeerCredentials:
    """What an admitted device holds: its identity and the department's records.

    ``certificate`` is this device's own; ``roster`` and ``revocations`` are the
    signed snapshots it received, which are exactly what it needs to verify a
    peer it meets later on the LAN without contacting anyone (Phase 3/4).
    """

    identity: DeviceIdentity
    certificate: DeviceCertificate
    roster: Roster
    revocations: RevocationList
    dept_id: str
    department_public_key: str
    department_name: str | None = None
    accepted_at: float = 0.0

    @property
    def device_id(self) -> str:
        return self.identity.device_id

    def verify_peer(self, certificate: DeviceCertificate, *,
                    now: float | None = None) -> DeviceCertificate:
        """Verify another device using only what this credential carries."""
        if self.roster.find(certificate.device_id) is None:
            raise PeerVerificationError(
                ROSTER_INCOMPLETE, "the peer is not on this department's roster")
        return verify_certificate(certificate, self.department_public_key,
                                  expected_dept_id=self.dept_id, now=now,
                                  revoked=self.revocations)

    def public_dict(self) -> dict:
        """Metadata safe to log or show in the interface (no key material)."""
        return {
            "dept_id": self.dept_id,
            "department_name": self.department_name,
            "device_id": self.device_id,
            "label": self.identity.label,
            "certificate_expires_at": self.certificate.not_after,
            "roster_epoch": self.roster.epoch,
            "revocation_epoch": self.revocations.epoch,
            "accepted_at": self.accepted_at,
        }

    def save(self, path: str | Path | None = None) -> Path:
        """Persist the credentials (private key included) through the secure store."""
        target = Path(path) if path is not None else peer_dir() / "credentials.json"
        secure_store.save(target, {
            "private_key_pem": self.identity.private_key_pem,
            "identity": self.identity.to_dict(),
            "certificate": self.certificate.to_dict(),
            "roster": self.roster.to_dict(),
            "revocations": self.revocations.to_dict(),
            "dept_id": self.dept_id,
            "department_name": self.department_name,
            "department_public_key": self.department_public_key,
            "accepted_at": self.accepted_at,
        })
        return target

    @classmethod
    def load(cls, path: str | Path | None = None) -> "PeerCredentials | None":
        """Load saved credentials; ``None`` only when no file exists."""
        target = Path(path) if path is not None else peer_dir() / "credentials.json"
        if not target.exists():
            return None
        data = secure_store.load(target)
        if data is None:
            raise PeerError(f"credentials file exists but cannot be read: {target.name}")
        try:
            identity_data = data["identity"]
            identity = DeviceIdentity(
                dept_id=identity_data["dept_id"],
                device_id=identity_data["device_id"],
                public_key_pem=identity_data["public_key"],
                private_key_pem=data["private_key_pem"],
                label=identity_data.get("label"),
                created_at=float(identity_data.get("created_at") or 0.0),
            )
            identity.verify_binding()
            credentials = cls(
                identity=identity,
                certificate=DeviceCertificate.from_dict(data["certificate"]),
                roster=Roster.from_dict(data["roster"]),
                revocations=RevocationList.from_dict(data["revocations"]),
                dept_id=str(data["dept_id"]),
                department_public_key=str(data["department_public_key"]),
                department_name=(None if data.get("department_name") is None
                                 else str(data["department_name"])),
                accepted_at=float(data.get("accepted_at") or 0.0))
        except (KeyError, TypeError, ValueError, PeerVerificationError) as exc:
            raise PeerError(f"credentials file is malformed: {target.name}") from exc
        _assert_credentials_are_consistent(credentials, source=target.name)
        return credentials

    def assert_certificate_current(self, *, now: float | None = None) -> None:
        """Raise ``CERT_EXPIRED`` when this device's own certificate has lapsed."""
        moment = float(now if now is not None else time.time())
        if moment >= self.certificate.not_after:
            raise PeerVerificationError(CERT_EXPIRED, "this device's certificate has expired")
