"""The department certificate authority: one root key, device certificates.

The root is the single most valuable secret in the system — whoever holds it can
mint a device — so it never leaves the machine that created it, and it is stored
through the existing DPAPI-backed store (§9.5). What travels instead is:

* the **public** root key, pinned on every device (it arrives inside the
  invitation, which the owner hands over out of band);
* **device certificates** signed by the root; and
* the signed **roster** and **revocation list** (see ``roster``/``revocation``).

A certificate is a signed JSON document rather than X.509 in Phase 1. The plan's
§9.2 lists exactly what it must assert — identity key, optional TLS key,
content-addressed identifier, department, roles, validity window, root signature
— and Phase 4 will mint the X.509 wrapper from these same fields for TLS 1.3.
Doing X.509 now would add encoding surface with nothing yet to consume it.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field, replace
from pathlib import Path

from ..distributed import secure_store, signing
from . import (AUTH_FAILED, CERT_EXPIRED, CERT_NOT_YET_VALID, IDENTIFIER_KEY_MISMATCH,
               MALFORMED, PROTOCOL_VERSION, REVOKED, VERSION_MISMATCH,
               WRONG_DEPARTMENT, PeerError, PeerVerificationError)
from .identity import canonical_json, clean_roles, device_id_for_public_key, peer_dir

CERT_FORMAT = "mc5-peer-cert-v1"
DEPARTMENT_FORMAT = "mc5-peer-department-v1"

#: Recommended certificate lifetime (§9.4). Short enough that a revocation a
#: peer never learned still expires on its own; long enough that re-inviting the
#: staff room is an annual chore rather than a termly one.
CERT_DEFAULT_DAYS = 90
#: Hard ceiling. ``revocation`` cannot protect a peer that never met the revoked
#: device, so certificate lifetime is the real bound (§9.4, known limitation).
CERT_MAX_DAYS = 365

DAY_SECONDS = 86400.0
#: How far into the future a signed document may claim to be from. A document
#: dated further ahead is refused, which stops a replayed signature from
#: outliving its freshness window (§9.3).
CLOCK_SKEW_SECONDS = 300.0


def _moment(now: float | None) -> float:
    return float(now if now is not None else time.time())


def check_not_future_dated(signed_at: float, now: float) -> None:
    """Refuse a document stamped implausibly far in the future."""
    if signed_at > now + CLOCK_SKEW_SECONDS:
        raise PeerVerificationError(
            AUTH_FAILED, "the document is dated in the future")


@dataclass(frozen=True)
class DeviceCertificate:
    """One device admitted to one department, signed by the department root."""

    dept_id: str
    device_id: str
    device_public_key: str
    not_before: float
    not_after: float
    issued_at: float
    signature: str = ""
    roles: tuple[str, ...] = ()
    label: str | None = None
    tls_public_key: str | None = None
    signer: str = ""
    format: str = CERT_FORMAT
    version: int = PROTOCOL_VERSION

    # -- serialisation -------------------------------------------------------

    def to_dict(self) -> dict:
        return {
            "format": self.format,
            "version": self.version,
            "dept_id": self.dept_id,
            "device_id": self.device_id,
            "device_public_key": self.device_public_key,
            "tls_public_key": self.tls_public_key,
            "roles": list(self.roles),
            "label": self.label,
            "not_before": self.not_before,
            "not_after": self.not_after,
            "issued_at": self.issued_at,
            "signer": self.signer,
            "signature": self.signature,
        }

    def signed_payload(self) -> bytes:
        """Exactly what the root signs: every field except the signature."""
        data = self.to_dict()
        data.pop("signature", None)
        return canonical_json(data)

    def to_json(self) -> str:
        return canonical_json(self.to_dict()).decode("utf-8")

    @classmethod
    def from_dict(cls, data) -> "DeviceCertificate":
        if not isinstance(data, dict):
            raise PeerVerificationError(MALFORMED, "a certificate must be an object")
        if data.get("format") != CERT_FORMAT or data.get("version") != PROTOCOL_VERSION:
            raise PeerVerificationError(
                VERSION_MISMATCH,
                f"unsupported certificate format {data.get('format')!r}")
        try:
            roles = data.get("roles") or ()
            if not isinstance(roles, (list, tuple)):
                raise TypeError("roles")
            return cls(
                dept_id=str(data["dept_id"]),
                device_id=str(data["device_id"]),
                device_public_key=str(data["device_public_key"]),
                not_before=float(data["not_before"]),
                not_after=float(data["not_after"]),
                issued_at=float(data["issued_at"]),
                signature=str(data.get("signature") or ""),
                roles=tuple(str(r) for r in roles),
                label=None if data.get("label") is None else str(data["label"]),
                tls_public_key=(None if data.get("tls_public_key") is None
                                else str(data["tls_public_key"])),
                signer=str(data.get("signer") or ""),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise PeerVerificationError(
                MALFORMED, "a certificate field is missing or ill-typed") from exc

    @classmethod
    def from_json(cls, text: str) -> "DeviceCertificate":
        import json

        try:
            return cls.from_dict(json.loads(text))
        except ValueError as exc:
            raise PeerVerificationError(MALFORMED, "certificate is not JSON") from exc

    # -- queries -------------------------------------------------------------

    def is_expired(self, now: float | None = None) -> bool:
        return _moment(now) >= self.not_after

    def covers(self, now: float | None = None) -> bool:
        moment = _moment(now)
        return self.not_before <= moment < self.not_after


@dataclass(frozen=True)
class DepartmentCA:
    """A department's root identity. The private key stays on the owner machine."""

    dept_id: str
    name: str
    public_key_pem: str
    private_key_pem: str | None = field(default=None, repr=False)
    created_at: float = 0.0

    @property
    def key_id(self) -> str:
        """Short, non-secret fingerprint — lets a log name the key that signed."""
        return signing.key_id(self.public_key_pem)

    @property
    def can_issue(self) -> bool:
        return bool(self.private_key_pem)

    # -- construction --------------------------------------------------------

    @classmethod
    def create(cls, dept_id: str, name: str, *,
               now: float | None = None) -> "DepartmentCA":
        if not isinstance(dept_id, str) or not dept_id.strip():
            raise ValueError("dept_id must be a non-empty string")
        if not isinstance(name, str) or not name.strip():
            raise ValueError("name must be a non-empty string")
        if len(dept_id.strip()) > 64:
            raise ValueError("dept_id is too long")
        private_pem, public_pem = signing.generate_keypair()
        return cls(dept_id=dept_id.strip(), name=name.strip(),
                   public_key_pem=public_pem, private_key_pem=private_pem,
                   created_at=_moment(now))

    def public_dict(self) -> dict:
        """Everything a device needs to verify this department. No secrets."""
        return {
            "format": DEPARTMENT_FORMAT,
            "version": PROTOCOL_VERSION,
            "dept_id": self.dept_id,
            "name": self.name,
            "public_key": self.public_key_pem,
            "key_id": self.key_id,
            "created_at": self.created_at,
        }

    # -- issuance ------------------------------------------------------------

    def issue_certificate(self, device_public_key_pem: str, *,
                          device_id: str | None = None,
                          roles=(), label: str | None = None,
                          days: int = CERT_DEFAULT_DAYS,
                          tls_public_key_pem: str | None = None,
                          now: float | None = None) -> DeviceCertificate:
        """Mint a certificate binding ``device_public_key_pem`` to this department.

        ``device_id`` exists so a caller can assert the identifier it *expects*
        and have a mismatch refused at issuance rather than discovered later at
        verification. The certificate is then signed over every other field.
        """
        if not self.private_key_pem:
            raise PeerError(
                "this department has no private key here; only the owner machine "
                "may issue certificates")
        if not isinstance(days, int) or not 1 <= days <= CERT_MAX_DAYS:
            raise ValueError(f"days must be an integer in 1..{CERT_MAX_DAYS}")
        derived = device_id_for_public_key(device_public_key_pem)
        if device_id is not None and device_id != derived:
            raise PeerVerificationError(
                IDENTIFIER_KEY_MISMATCH,
                "the requested device identifier does not belong to that key")
        if tls_public_key_pem is not None and "BEGIN PUBLIC KEY" not in tls_public_key_pem:
            raise ValueError("tls_public_key_pem must be a PEM public key when given")
        moment = _moment(now)
        draft = DeviceCertificate(
            dept_id=self.dept_id,
            device_id=derived,
            device_public_key=device_public_key_pem.strip(),
            not_before=moment,
            not_after=moment + days * DAY_SECONDS,
            issued_at=moment,
            roles=clean_roles(roles),
            label=label,
            tls_public_key=tls_public_key_pem,
            signer=self.key_id,
        )
        signature = signing.sign_package(draft.signed_payload(), self.private_key_pem)
        return replace(draft, signature=signature)

    # -- storage -------------------------------------------------------------

    def save(self, path: str | Path | None = None) -> Path:
        """Persist the root (private key included) through the secure store."""
        if not self.private_key_pem:            # pragma: no cover - programmer error
            raise PeerError("cannot save a department that holds no private key")
        target = Path(path) if path is not None else peer_dir() / "department.json"
        secure_store.save(target, {"private_key_pem": self.private_key_pem,
                                   **self.public_dict()})
        return target

    @classmethod
    def load(cls, path: str | Path | None = None) -> "DepartmentCA | None":
        """Load the department root from this machine; ``None`` when absent."""
        target = Path(path) if path is not None else peer_dir() / "department.json"
        if not target.exists():
            return None
        data = secure_store.load(target)
        if data is None:
            raise PeerError(f"department file exists but cannot be read: {target.name}")
        try:
            return cls(dept_id=str(data["dept_id"]),
                       name=str(data.get("name") or data["dept_id"]),
                       public_key_pem=str(data["public_key"]),
                       private_key_pem=data.get("private_key_pem"),
                       created_at=float(data.get("created_at") or 0.0))
        except (KeyError, TypeError, ValueError) as exc:
            raise PeerError(f"department file is malformed: {target.name}") from exc


def verify_certificate(certificate: DeviceCertificate, department_public_key_pem: str,
                       *, expected_dept_id: str | None = None,
                       now: float | None = None,
                       revoked=None) -> DeviceCertificate:
    """Verify a certificate against the pinned department key, or raise.

    The checks run in a fixed order so each failure has one predictable reason:

    1. format/version        -> ``VERSION_MISMATCH``
    2. department            -> ``WRONG_DEPARTMENT``
    3. root signature        -> ``AUTH_FAILED``
    4. identifier ↔ key      -> ``IDENTIFIER_KEY_MISMATCH``
    5. validity window       -> ``CERT_NOT_YET_VALID`` / ``CERT_EXPIRED``
    6. revocation            -> ``REVOKED``

    ``revoked`` may be a :class:`~app.peer.revocation.RevocationList`, anything
    with ``is_revoked(device_id)``, or a callable taking the identifier.
    """
    if not isinstance(certificate, DeviceCertificate):
        raise PeerVerificationError(MALFORMED, "not a device certificate")
    if certificate.format != CERT_FORMAT or certificate.version != PROTOCOL_VERSION:
        raise PeerVerificationError(VERSION_MISMATCH,
                                    "unsupported certificate format")
    if expected_dept_id is not None and certificate.dept_id != expected_dept_id:
        raise PeerVerificationError(
            WRONG_DEPARTMENT,
            "the certificate belongs to a different department")
    if not certificate.signature:
        raise PeerVerificationError(AUTH_FAILED, "the certificate is unsigned")
    if not signing.verify_package(certificate.signed_payload(),
                                  certificate.signature,
                                  department_public_key_pem):
        raise PeerVerificationError(
            AUTH_FAILED, "the certificate signature does not verify")
    if device_id_for_public_key(certificate.device_public_key) != certificate.device_id:
        raise PeerVerificationError(
            IDENTIFIER_KEY_MISMATCH,
            "the certificate's identifier does not derive from its key")

    moment = _moment(now)
    check_not_future_dated(certificate.issued_at, moment)
    if moment < certificate.not_before:
        raise PeerVerificationError(CERT_NOT_YET_VALID,
                                    "the certificate is not valid yet")
    if moment >= certificate.not_after:
        raise PeerVerificationError(CERT_EXPIRED, "the certificate has expired")

    if revoked is not None:
        is_revoked = revoked if callable(revoked) else revoked.is_revoked
        if is_revoked(certificate.device_id):
            raise PeerVerificationError(REVOKED, "the device has been revoked")
    return certificate
