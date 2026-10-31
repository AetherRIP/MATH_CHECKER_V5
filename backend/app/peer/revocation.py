"""Revocation, stated honestly.

The plan (§9.4) is blunt about what revocation can and cannot promise: a peer
that has never learned a revocation will accept the revoked device, and nothing
in a decentralised system fixes that without a server. What this module does
instead:

* every revocation record is root-signed and carries a timestamp, so it is
  reviewable rather than silent;
* the list carries a **monotonic epoch**, and a peer that has seen epoch *n*
  refuses *n−1* — a replayed older list cannot resurrect a revoked device;
* revocation is written to the existing audit trail, so who removed a device and
  when survives on the owner's machine (§9.4, last bullet).

The bound on the exposure is certificate lifetime, not this list: with 90-day
certificates a device that nobody ever meets again simply expires.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass

from ..distributed import signing
from . import (AUTH_FAILED, EPOCH_ROLLBACK, MALFORMED, PROTOCOL_VERSION, REVOKED,
               VERSION_MISMATCH, WRONG_DEPARTMENT, PeerError, PeerVerificationError)
from .ca import check_not_future_dated
from .identity import canonical_json

REVOCATION_FORMAT = "mc5-peer-revocations-v1"


@dataclass(frozen=True)
class RevocationRecord:
    """One revoked device. ``actor`` is a label (a person or device), not a key."""

    device_id: str
    revoked_at: float
    reason: str | None = None
    actor: str | None = None

    def to_dict(self) -> dict:
        return {"device_id": self.device_id, "revoked_at": self.revoked_at,
                "reason": self.reason, "actor": self.actor}

    @classmethod
    def from_dict(cls, data) -> "RevocationRecord":
        if not isinstance(data, dict):
            raise PeerVerificationError(MALFORMED, "a revocation record must be an object")
        try:
            return cls(device_id=str(data["device_id"]),
                       revoked_at=float(data["revoked_at"]),
                       reason=None if data.get("reason") is None else str(data["reason"]),
                       actor=None if data.get("actor") is None else str(data["actor"]))
        except (KeyError, TypeError, ValueError) as exc:
            raise PeerVerificationError(
                MALFORMED, "a revocation record is missing or ill-typed") from exc


@dataclass(frozen=True)
class RevocationList:
    """An unsigned revocation list for one department."""

    dept_id: str
    epoch: int = 0
    records: tuple[RevocationRecord, ...] = ()

    def __post_init__(self) -> None:
        if int(self.epoch) != self.epoch or self.epoch < 0:
            raise ValueError("revocation epoch must be a non-negative integer")

    # -- queries -------------------------------------------------------------

    def is_revoked(self, device_id: str) -> bool:
        """Callable form expected by ``verify_certificate(revoked=...)``."""
        return any(record.device_id == device_id for record in self.records)

    def record_for(self, device_id: str) -> RevocationRecord | None:
        for record in self.records:
            if record.device_id == device_id:
                return record
        return None

    def __len__(self) -> int:
        return len(self.records)

    # -- building ------------------------------------------------------------

    def revoke(self, device_id: str, *, reason: str | None = None,
               actor: str | None = None, now: float | None = None) -> "RevocationList":
        """Return a new list (epoch + 1) that revokes ``device_id``.

        Revoking an already-revoked device is a no-op that keeps the epoch: the
        epoch is a version of the *document*, and inflating it for a repeated
        request would make peers reject honest, healthy lists as stale.
        """
        if not isinstance(device_id, str) or not device_id.strip():
            raise ValueError("device_id must be a non-empty string")
        clean_id = device_id.strip()
        if self.is_revoked(clean_id):
            return self
        record = RevocationRecord(device_id=clean_id,
                                  revoked_at=float(now if now is not None else time.time()),
                                  reason=(reason.strip() if isinstance(reason, str) and reason.strip() else None),
                                  actor=(actor.strip() if isinstance(actor, str) and actor.strip() else None))
        return RevocationList(dept_id=self.dept_id, epoch=self.epoch + 1,
                              records=self.records + (record,))

    # -- serialisation -------------------------------------------------------

    def to_dict(self) -> dict:
        return {
            "format": REVOCATION_FORMAT,
            "version": PROTOCOL_VERSION,
            "dept_id": self.dept_id,
            "epoch": self.epoch,
            "records": [record.to_dict() for record in self.records],
        }

    @classmethod
    def from_dict(cls, data) -> "RevocationList":
        if not isinstance(data, dict):
            raise PeerVerificationError(MALFORMED, "a revocation list must be an object")
        if data.get("format") != REVOCATION_FORMAT or data.get("version") != PROTOCOL_VERSION:
            raise PeerVerificationError(VERSION_MISMATCH,
                                        "unsupported revocation list format")
        records = data.get("records")
        if not isinstance(records, list):
            raise PeerVerificationError(MALFORMED, "a revocation list needs records")
        try:
            return cls(dept_id=str(data["dept_id"]),
                       epoch=int(data["epoch"]),
                       records=tuple(RevocationRecord.from_dict(r) for r in records))
        except (KeyError, TypeError, ValueError) as exc:
            raise PeerVerificationError(
                MALFORMED, "a revocation list field is missing or ill-typed") from exc


@dataclass(frozen=True)
class SignedRevocationList:
    """A revocation list plus the root signature over it."""

    revocations: RevocationList
    signed_at: float
    signer: str
    signature: str
    format: str = REVOCATION_FORMAT
    version: int = PROTOCOL_VERSION

    @classmethod
    def sign(cls, revocations: RevocationList, department, *,
             now: float | None = None) -> "SignedRevocationList":
        if not getattr(department, "private_key_pem", None):
            raise PeerError("only the owner machine may sign a revocation list")
        if revocations.dept_id != department.dept_id:
            raise PeerVerificationError(
                WRONG_DEPARTMENT, "a department may only sign its own revocation list")
        moment = float(now if now is not None else time.time())
        draft = cls(revocations=revocations, signed_at=moment,
                    signer=department.key_id, signature="")
        signature = signing.sign_package(draft.signed_payload(),
                                         department.private_key_pem)
        return cls(revocations=revocations, signed_at=moment,
                   signer=department.key_id, signature=signature)

    def signed_payload(self) -> bytes:
        return canonical_json({
            "format": self.format,
            "version": self.version,
            "dept_id": self.revocations.dept_id,
            "epoch": self.revocations.epoch,
            "signed_at": self.signed_at,
            "signer": self.signer,
            "records": [record.to_dict() for record in self.revocations.records],
        })

    def to_dict(self) -> dict:
        return {**self.revocations.to_dict(), "signed_at": self.signed_at,
                "signer": self.signer, "signature": self.signature}

    def to_json(self) -> str:
        return canonical_json(self.to_dict()).decode("utf-8")

    @classmethod
    def from_dict(cls, data) -> "SignedRevocationList":
        if not isinstance(data, dict):
            raise PeerVerificationError(MALFORMED,
                                        "a signed revocation list must be an object")
        revocations = RevocationList.from_dict(data)
        try:
            return cls(revocations=revocations,
                       signed_at=float(data["signed_at"]),
                       signer=str(data["signer"]),
                       signature=str(data["signature"]))
        except (KeyError, TypeError, ValueError) as exc:
            raise PeerVerificationError(
                MALFORMED, "a signed revocation list field is missing or ill-typed") from exc

    @classmethod
    def from_json(cls, text: str) -> "SignedRevocationList":
        try:
            return cls.from_dict(json.loads(text))
        except ValueError as exc:
            raise PeerVerificationError(MALFORMED, "revocation list is not JSON") from exc

    # -- verification --------------------------------------------------------

    def verify(self, department_public_key_pem: str, *,
               expected_dept_id: str | None = None,
               known_epoch: int | None = None,
               now: float | None = None) -> RevocationList:
        """Verify the signature and refuse an epoch older than ``known_epoch``.

        The rollback check is the whole point of the epoch: without it an
        attacker could replay the list from before a device was revoked and the
        peer would believe it.
        """
        if self.format != REVOCATION_FORMAT or self.version != PROTOCOL_VERSION:
            raise PeerVerificationError(VERSION_MISMATCH,
                                        "unsupported revocation list format")
        if expected_dept_id is not None and self.revocations.dept_id != expected_dept_id:
            raise PeerVerificationError(
                WRONG_DEPARTMENT, "the revocation list belongs to another department")
        if not self.signature:
            raise PeerVerificationError(AUTH_FAILED, "the revocation list is unsigned")
        if not signing.verify_package(self.signed_payload(), self.signature,
                                      department_public_key_pem):
            raise PeerVerificationError(
                AUTH_FAILED, "the revocation list signature does not verify")
        moment = float(now if now is not None else time.time())
        check_not_future_dated(self.signed_at, moment)
        if known_epoch is not None and self.revocations.epoch < int(known_epoch):
            raise PeerVerificationError(
                EPOCH_ROLLBACK,
                f"revocation epoch {self.revocations.epoch} is older than "
                f"{int(known_epoch)}")
        return self.revocations


def revoke_device(signed: SignedRevocationList, device_id: str, *,
                  department, reason: str | None = None,
                  actor: str | None = None, now: float | None = None,
                  audit: bool = True) -> SignedRevocationList:
    """Revoke one device and re-sign the list — the owner-side operation.

    The incoming list is verified before it is extended, so a tampered local file
    cannot be promoted into a freshly signed one. ``audit=True`` writes the
    existing audit trail (``audit.record``), which needs the application
    database; tooling that runs without it passes ``audit=False``.
    """
    signed.verify(department.public_key_pem,
                  expected_dept_id=department.dept_id, now=now)
    updated = signed.revocations.revoke(device_id, reason=reason, actor=actor, now=now)
    if updated is signed.revocations or updated.epoch == signed.revocations.epoch:
        return signed
    signed_updated = SignedRevocationList.sign(updated, department, now=now)
    if audit:
        # Imported lazily: importing app.distributed.audit pulls in the database
        # layer, and the verification path above must stay usable without it.
        from ..distributed import audit as distributed_audit

        distributed_audit.record(
            action="peer.device_revoked", target_type="peer_device",
            target_id=updated.records[-1].device_id,
            outcome="success",
            details={"dept_id": department.dept_id,
                     "epoch": updated.epoch,
                     "reason": updated.records[-1].reason,
                     "actor": updated.records[-1].actor})
    return signed_updated


def assert_not_revoked(signed: SignedRevocationList, device_id: str, *,
                       department_public_key_pem: str,
                       expected_dept_id: str | None = None,
                       known_epoch: int | None = None,
                       now: float | None = None) -> None:
    """Verify the list and raise ``REVOKED`` if it names ``device_id``.

    A single call so a caller cannot verify the signature and then forget to ask
    the question the list exists to answer.
    """
    revocations = signed.verify(department_public_key_pem,
                                expected_dept_id=expected_dept_id,
                                known_epoch=known_epoch, now=now)
    if revocations.is_revoked(device_id):
        raise PeerVerificationError(REVOKED, "the device has been revoked")
