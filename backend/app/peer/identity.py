"""Device identity: a key pair, and an identifier derived from the public half.

A peer proves who it is by signing with an Ed25519 key. The device identifier is
``base32(SHA-256(raw public key))[:26]`` — 130 bits of the key's own hash — so
the identifier and the key that must sign for it are the same fact. Claiming
another device's identifier now requires its private key, which closes finding 7
in the plan (§3): today ``devices.device_id`` is a random UUID and nothing stops
a device from presenting someone else's.

Reuse, rather than a second crypto implementation:

* Ed25519 primitives, PEM loading and detached base64 signatures come from
  :mod:`app.distributed.signing` — the same code that verifies update packages,
  and already a runtime dependency through ``cryptography``.
* Keys at rest use :mod:`app.distributed.secure_store`, which is DPAPI-scoped to
  the current user on Windows. That scope is correct here because the peer
  service runs in the signed-in teacher's session, never as ``SYSTEM`` (§9.6).

What is *not* here: no network, no TLS, no X.509. Phase 1 ends at "this
identifier is bound to this key and this key signed this document".
"""
from __future__ import annotations

import base64
import hashlib
import json
import time
from dataclasses import dataclass, field, replace
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from ..distributed import secure_store, signing
from . import (IDENTIFIER_KEY_MISMATCH, MALFORMED, PROTOCOL_VERSION,
               WRONG_DEPARTMENT, PeerVerificationError)

#: Characters of the base32 digest used as an identifier. 26 base32 characters
#: carry 130 bits, which is longer than the 128-bit target and short enough to
#: read out loud or compare between two screens.
DEVICE_ID_LENGTH = 26

IDENTITY_FORMAT = "mc5-peer-identity-v1"


def canonical_json(value) -> bytes:
    """The one byte encoding every signature in this package covers.

    Sorted keys, no optional whitespace, ASCII-only escapes. Two processes on
    two machines must produce identical bytes for identical data, or a valid
    signature would appear invalid; ``sort_keys`` removes dict ordering from the
    equation and ``ensure_ascii`` removes the encoder's locale from it.
    """
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True).encode("utf-8")


def _b32(digest: bytes) -> str:
    return base64.b32encode(digest).decode("ascii")


def raw_public_bytes(public_key_pem: str) -> bytes:
    """The 32 raw key bytes behind a PEM, rejecting anything not Ed25519.

    Raises :class:`PeerVerificationError` with ``MALFORMED`` for hostile input:
    the caller is usually parsing a document written by another machine.
    """
    if not isinstance(public_key_pem, str) or not public_key_pem.strip():
        raise PeerVerificationError(MALFORMED, "a public key is required")
    try:
        key = serialization.load_pem_public_key(public_key_pem.strip().encode("utf-8"))
    except (ValueError, TypeError) as exc:
        raise PeerVerificationError(MALFORMED, "public key is not a valid PEM") from exc
    if not isinstance(key, Ed25519PublicKey):
        raise PeerVerificationError(MALFORMED, "public key is not Ed25519")
    return key.public_bytes(serialization.Encoding.Raw,
                            serialization.PublicFormat.Raw)


def device_id_for_public_key(public_key_pem: str) -> str:
    """The stable identifier of a device, derived from its public key.

    Hashing the *raw* key rather than the PEM means a re-encoded PEM (different
    line endings, trailing newline, ``BEGIN PUBLIC KEY`` vs ``BEGIN ED25519
    PUBLIC KEY``) cannot silently produce a second identifier for one key.
    """
    return _b32(hashlib.sha256(raw_public_bytes(public_key_pem)).digest())[:DEVICE_ID_LENGTH]


def peer_dir() -> Path:
    """Where peer state lives for this installation (``DATA_DIR/peer``).

    Resolved at call time, not import time, so a test that points
    ``config.DATA_DIR`` at a temporary directory owns every file this package
    writes — the suite must be able to run without touching the real install.
    """
    import app.config as config

    return Path(config.DATA_DIR) / "peer"


def _require_str(value, field_name: str, *, required: bool = True,
                 limit: int = 512) -> str | None:
    if value is None:
        if required:
            raise PeerVerificationError(MALFORMED, f"{field_name} is required")
        return None
    if not isinstance(value, str) or (required and not value.strip()):
        raise PeerVerificationError(MALFORMED, f"{field_name} must be a string")
    text = value.strip()
    if len(text) > limit:
        raise PeerVerificationError(MALFORMED, f"{field_name} is too long")
    return text


def clean_roles(roles) -> tuple[str, ...]:
    """Department-level roles, de-duplicated and order-stable.

    These are *not* application account roles (``TEACHER``/``DEVELOPER``/…):
    those remain the authorisation model in the application layer (§9.1). A peer
    certificate only records department-scoped capabilities such as who may
    revoke a device.
    """
    if roles is None:
        return ()
    if isinstance(roles, str):
        roles = [roles]
    cleaned: list[str] = []
    for role in roles:
        if not isinstance(role, str) or not role.strip():
            raise ValueError(f"invalid peer role: {role!r}")
        name = role.strip()
        if len(name) > 64:
            raise ValueError(f"peer role is too long: {name[:32]!r}…")
        if name not in cleaned:
            cleaned.append(name)
    return tuple(cleaned)


@dataclass(frozen=True)
class DeviceIdentity:
    """One device's key pair plus the identifier derived from it.

    The private key is excluded from ``repr`` so an accidental log line or an
    exception traceback cannot print it; :meth:`to_dict` never includes it at
    all, because that method feeds rosters, invitations and audit details.
    """

    dept_id: str
    device_id: str
    public_key_pem: str
    private_key_pem: str = field(repr=False)
    label: str | None = None
    created_at: float = 0.0

    # -- construction --------------------------------------------------------

    @classmethod
    def generate(cls, dept_id: str, *, label: str | None = None,
                 now: float | None = None) -> "DeviceIdentity":
        """Create a fresh identity for ``dept_id`` on this machine."""
        dept = _require_str(dept_id, "dept_id")
        private_pem, public_pem = signing.generate_keypair()
        return cls(
            dept_id=dept,
            device_id=device_id_for_public_key(public_pem),
            public_key_pem=public_pem,
            private_key_pem=private_pem,
            label=_require_str(label, "label", required=False, limit=128),
            created_at=float(now if now is not None else time.time()),
        )

    def __post_init__(self) -> None:
        # An identity built by hand (or loaded from a tampered file) must still
        # satisfy the binding the whole model rests on.
        if self.dept_id != _require_str(self.dept_id, "dept_id"):
            raise ValueError("dept_id must be a non-empty string")
        derived = device_id_for_public_key(self.public_key_pem)
        if derived != self.device_id:
            raise PeerVerificationError(
                IDENTIFIER_KEY_MISMATCH,
                "the device identifier does not derive from its public key")

    def verify_binding(self) -> None:
        """Re-check every binding this identity claims, key pair included.

        Used after loading, where a file can be internally consistent and
        still wrong: a public key paired with a *different* private key signs
        bytes nothing can verify, and the device would present a certificate it
        cannot prove possession of (plan §9.2). Cheap enough for every load.
        """
        if device_id_for_public_key(self.public_key_pem) != self.device_id:
            raise PeerVerificationError(
                IDENTIFIER_KEY_MISMATCH,
                "the device identifier does not derive from its public key")
        if not self.private_key_pem:
            return
        try:
            derived_public = signing.public_pem_from_private(self.private_key_pem)
        except (ValueError, TypeError) as exc:
            raise PeerVerificationError(
                MALFORMED, "the stored private key cannot be read") from exc
        if raw_public_bytes(derived_public) != raw_public_bytes(self.public_key_pem):
            raise PeerVerificationError(
                MALFORMED,
                "the stored private key does not belong to the public key")

    # -- signing -------------------------------------------------------------

    def sign(self, payload: bytes) -> str:
        """Detached base64 Ed25519 signature over ``payload``."""
        return signing.sign_package(payload, self.private_key_pem)

    def verify(self, payload: bytes, signature_b64: str) -> bool:
        """True only for a valid signature by this identity's key."""
        return signing.verify_package(payload, signature_b64, self.public_key_pem)

    # -- serialisation -------------------------------------------------------

    def to_dict(self) -> dict:
        """Public metadata. Never contains private key material."""
        return {
            "format": IDENTITY_FORMAT,
            "version": PROTOCOL_VERSION,
            "dept_id": self.dept_id,
            "device_id": self.device_id,
            "public_key": self.public_key_pem,
            "label": self.label,
            "created_at": self.created_at,
        }

    def save(self, path: str | Path | None = None) -> Path:
        """Persist the identity (including the private key) securely.

        DPAPI on Windows, owner-only permissions elsewhere — the same store the
        device credential uses. The write is atomic, so a crash cannot leave a
        half-written key file behind.
        """
        target = Path(path) if path is not None else peer_dir() / "identity.json"
        secure_store.save(target, {"private_key_pem": self.private_key_pem,
                                   **self.to_dict()})
        return target

    @classmethod
    def load(cls, path: str | Path | None = None) -> "DeviceIdentity | None":
        """Read an identity written by :meth:`save`; ``None`` when absent.

        An unreadable file is *not* silently treated as "no identity": a file
        that exists but cannot be decrypted means the store is broken (wrong
        user, lost DPAPI scope), and starting fresh would silently orphan every
        certificate already issued for this device. Callers get ``None`` only
        for a genuinely missing file; corruption raises.
        """
        target = Path(path) if path is not None else peer_dir() / "identity.json"
        if not target.exists():
            return None
        try:
            data = secure_store.load(target)
        except OSError as exc:                  # unreadable is not absent (R-08)
            raise PeerErrorStoreUnreadable(
                f"identity file exists but cannot be read: {target.name}: {exc}") from exc
        if data is None:
            raise PeerErrorStoreUnreadable(
                f"identity file exists but cannot be read: {target.name}")
        try:
            identity = cls(
                dept_id=data["dept_id"],
                device_id=data["device_id"],
                public_key_pem=data["public_key"],
                private_key_pem=data["private_key_pem"],
                label=data.get("label"),
                created_at=float(data.get("created_at") or 0.0),
            )
            identity.verify_binding()
        except (KeyError, TypeError, ValueError, PeerVerificationError) as exc:
            raise PeerErrorStoreUnreadable(
                f"identity file is malformed or inconsistent: {target.name}") from exc
        return identity

    def adopt_department(self, dept_id: str) -> "DeviceIdentity":
        """A copy of this identity bound to another department.

        Used when a machine is re-invited after a department is re-created. The
        key — and therefore the identifier — is unchanged, because an identifier
        that changed when a department did would break every roster that lists
        it.
        """
        return replace(self, dept_id=_require_str(dept_id, "dept_id"))


class PeerErrorStoreUnreadable(Exception):
    """The peer state file exists but cannot be used.

    Deliberately *not* a verification error: no peer was involved. Raising
    instead of returning ``None`` keeps a broken store from looking like a
    first run, which would mint a new key and quietly invalidate every
    certificate already issued for the device.
    """


def load_or_create(dept_id: str, *, label: str | None = None,
                   path: str | Path | None = None,
                   now: float | None = None) -> tuple[DeviceIdentity, bool]:
    """Return this machine's identity for ``dept_id``, creating it if needed.

    Returns ``(identity, created)``. An identity stored for a *different*
    department is refused rather than silently re-bound: adopting a key into a
    department it was not invited to would defeat the point of a department
    boundary.
    """
    existing = DeviceIdentity.load(path)
    if existing is None:
        identity = DeviceIdentity.generate(dept_id, label=label, now=now)
        identity.save(path)
        return identity, True
    if existing.dept_id != dept_id:
        raise PeerVerificationError(
            WRONG_DEPARTMENT,
            "this device is already registered to a different department")
    return existing, False
