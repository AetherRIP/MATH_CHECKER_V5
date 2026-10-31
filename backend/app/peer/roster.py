"""The signed roster: which devices belong to a department, right now.

A roster is a root-signed snapshot of device certificates plus a monotonic
epoch. It is how a peer answers "is this device one of ours?" without asking a
server, and why the hub's ``device_links`` table is no longer the only authority
(plan §3 finding 4).

Two rules matter more than the encoding:

* **One identifier, one key.** If two entries claim one ``device_id`` with
  different public keys, that is not a merge conflict to resolve — it is an
  attack (or a compromised CA) and the roster is refused outright (§9.2).
* **Epochs only move forward.** A peer that has seen epoch *n* refuses *n−1*, so
  replaying an older roster cannot quietly re-admit a removed device (§9.4).

Revocation is deliberately *not* checked here: a roster is a historical
statement about who was admitted, and a valid roster may still list a device
that was revoked afterwards. Trust decisions check both documents, which is why
``verify_certificate`` accepts a revocation list.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass

from ..distributed import signing
from . import (AUTH_FAILED, DUPLICATE_DEVICE_ID, EPOCH_ROLLBACK, MALFORMED,
               PROTOCOL_VERSION, VERSION_MISMATCH, WRONG_DEPARTMENT, PeerError,
               PeerVerificationError)
from .ca import CLOCK_SKEW_SECONDS, DeviceCertificate, check_not_future_dated, verify_certificate
from .identity import canonical_json

ROSTER_FORMAT = "mc5-peer-roster-v1"


@dataclass(frozen=True)
class Roster:
    """An unsigned roster. Sign it before it leaves the machine."""

    dept_id: str
    epoch: int = 0
    devices: tuple[DeviceCertificate, ...] = ()

    def __post_init__(self) -> None:
        if int(self.epoch) != self.epoch or self.epoch < 0:
            raise ValueError("roster epoch must be a non-negative integer")

    # -- building ------------------------------------------------------------

    def with_device(self, certificate: DeviceCertificate) -> "Roster":
        """Return a new roster (epoch + 1) that includes ``certificate``.

        Re-issuing a certificate for a key already present replaces the old
        entry — that is how a renewal or a role change is represented. A second
        key claiming an existing identifier is refused.
        """
        if certificate.dept_id != self.dept_id:
            raise PeerVerificationError(
                WRONG_DEPARTMENT, "the certificate belongs to another department")
        for existing in self.devices:
            if existing.device_id != certificate.device_id:
                continue
            if existing.device_public_key != certificate.device_public_key:
                raise PeerVerificationError(
                    DUPLICATE_DEVICE_ID,
                    "two different keys claim one device identifier")
            if existing == certificate:
                return self
            replaced = tuple(certificate if entry.device_id == certificate.device_id
                             else entry for entry in self.devices)
            return Roster(dept_id=self.dept_id, epoch=self.epoch + 1,
                          devices=replaced)
        return Roster(dept_id=self.dept_id, epoch=self.epoch + 1,
                      devices=self.devices + (certificate,))

    def without_device(self, device_id: str) -> "Roster":
        """Return a new roster (epoch + 1) with ``device_id`` removed."""
        kept = tuple(entry for entry in self.devices if entry.device_id != device_id)
        if len(kept) == len(self.devices):
            return self
        return Roster(dept_id=self.dept_id, epoch=self.epoch + 1, devices=kept)

    # -- queries -------------------------------------------------------------

    def find(self, device_id: str) -> DeviceCertificate | None:
        for entry in self.devices:
            if entry.device_id == device_id:
                return entry
        return None

    def device_ids(self) -> tuple[str, ...]:
        return tuple(entry.device_id for entry in self.devices)

    def __len__(self) -> int:
        return len(self.devices)

    # -- serialisation -------------------------------------------------------

    def to_dict(self) -> dict:
        return {
            "format": ROSTER_FORMAT,
            "version": PROTOCOL_VERSION,
            "dept_id": self.dept_id,
            "epoch": self.epoch,
            "devices": [entry.to_dict() for entry in self.devices],
        }

    @classmethod
    def from_dict(cls, data) -> "Roster":
        if not isinstance(data, dict):
            raise PeerVerificationError(MALFORMED, "a roster must be an object")
        if data.get("format") != ROSTER_FORMAT or data.get("version") != PROTOCOL_VERSION:
            raise PeerVerificationError(VERSION_MISMATCH, "unsupported roster format")
        devices = data.get("devices")
        if not isinstance(devices, list):
            raise PeerVerificationError(MALFORMED, "a roster needs a device list")
        try:
            return cls(dept_id=str(data["dept_id"]),
                       epoch=int(data["epoch"]),
                       devices=tuple(DeviceCertificate.from_dict(d) for d in devices))
        except (KeyError, TypeError, ValueError) as exc:
            raise PeerVerificationError(
                MALFORMED, "a roster field is missing or ill-typed") from exc


@dataclass(frozen=True)
class SignedRoster:
    """A roster plus the root signature that makes it trustworthy."""

    roster: Roster
    signed_at: float
    signer: str
    signature: str
    format: str = ROSTER_FORMAT
    version: int = PROTOCOL_VERSION

    @classmethod
    def sign(cls, roster: Roster, department, *,
             now: float | None = None) -> "SignedRoster":
        """Sign ``roster`` with a :class:`~app.peer.ca.DepartmentCA`.

        Refuses a department that holds no private key, and refuses to sign a
        roster naming a different department than the signer's — either would
        produce a document no peer could correctly verify.
        """
        if not getattr(department, "private_key_pem", None):
            raise PeerError("only the owner machine may sign a roster")
        if roster.dept_id != department.dept_id:
            raise PeerVerificationError(
                WRONG_DEPARTMENT, "a department may only sign its own roster")
        moment = float(now if now is not None else time.time())
        draft = cls(roster=roster, signed_at=moment,
                    signer=department.key_id, signature="")
        signature = signing.sign_package(draft.signed_payload(),
                                         department.private_key_pem)
        return cls(roster=roster, signed_at=moment, signer=department.key_id,
                   signature=signature)

    def signed_payload(self) -> bytes:
        return canonical_json({
            "format": self.format,
            "version": self.version,
            "dept_id": self.roster.dept_id,
            "epoch": self.roster.epoch,
            "signed_at": self.signed_at,
            "signer": self.signer,
            "devices": self.roster.to_dict()["devices"],
        })

    def to_dict(self) -> dict:
        return {**self.roster.to_dict(),
                "signed_at": self.signed_at,
                "signer": self.signer,
                "signature": self.signature}

    def to_json(self) -> str:
        return canonical_json(self.to_dict()).decode("utf-8")

    @classmethod
    def from_dict(cls, data) -> "SignedRoster":
        if not isinstance(data, dict):
            raise PeerVerificationError(MALFORMED, "a signed roster must be an object")
        roster = Roster.from_dict(data)
        try:
            return cls(roster=roster,
                       signed_at=float(data["signed_at"]),
                       signer=str(data["signer"]),
                       signature=str(data["signature"]))
        except (KeyError, TypeError, ValueError) as exc:
            raise PeerVerificationError(
                MALFORMED, "a signed roster field is missing or ill-typed") from exc

    @classmethod
    def from_json(cls, text: str) -> "SignedRoster":
        try:
            return cls.from_dict(json.loads(text))
        except ValueError as exc:
            raise PeerVerificationError(MALFORMED, "roster is not JSON") from exc

    # -- verification --------------------------------------------------------

    def verify(self, department_public_key_pem: str, *,
               expected_dept_id: str | None = None,
               now: float | None = None,
               known_epoch: int | None = None) -> Roster:
        """Verify the signature, the department, the epoch and every certificate.

        Returns the verified :class:`Roster`. The device certificates are
        checked individually against the same department key, so a roster cannot
        smuggle in a certificate from another department or for another key.
        """
        if self.format != ROSTER_FORMAT or self.version != PROTOCOL_VERSION:
            raise PeerVerificationError(VERSION_MISMATCH, "unsupported roster format")
        if expected_dept_id is not None and self.roster.dept_id != expected_dept_id:
            raise PeerVerificationError(WRONG_DEPARTMENT,
                                        "the roster belongs to another department")
        if not self.signature:
            raise PeerVerificationError(AUTH_FAILED, "the roster is unsigned")
        if not signing.verify_package(self.signed_payload(), self.signature,
                                      department_public_key_pem):
            raise PeerVerificationError(AUTH_FAILED,
                                        "the roster signature does not verify")
        moment = float(now if now is not None else time.time())
        check_not_future_dated(self.signed_at, moment)

        if known_epoch is not None and self.roster.epoch < int(known_epoch):
            raise PeerVerificationError(
                EPOCH_ROLLBACK,
                f"roster epoch {self.roster.epoch} is older than {int(known_epoch)}")

        seen: dict[str, str] = {}
        for entry in self.roster.devices:
            verify_certificate(entry, department_public_key_pem,
                               expected_dept_id=self.roster.dept_id, now=moment)
            previous_key = seen.get(entry.device_id)
            if previous_key is not None and previous_key != entry.device_public_key:
                raise PeerVerificationError(
                    DUPLICATE_DEVICE_ID,
                    "two different keys claim one device identifier")
            seen[entry.device_id] = entry.device_public_key
        return self.roster


def is_fresh(signed: SignedRoster, *, max_age_seconds: float,
             now: float | None = None) -> bool:
    """Whether a roster was signed within ``max_age_seconds``.

    A peer decides its own staleness policy (a staff-room LAN can accept hours; a
    WAN path cannot), so this is a helper rather than a rule inside ``verify``.
    """
    moment = float(now if now is not None else time.time())
    return (moment - max_age_seconds) <= signed.signed_at <= moment + CLOCK_SKEW_SECONDS
