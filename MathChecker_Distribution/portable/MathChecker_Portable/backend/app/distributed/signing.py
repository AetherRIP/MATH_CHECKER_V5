"""Ed25519 signing for update packages (§19 VERIFY_CHECKSUM/SIGNATURE, §25).

The checksum proves a download was not *corrupted in transit*; the signature
proves the package was **authored by the Developer** and was not *substituted*
by host that serves it. Both are required before an update may touch an
installation (see ``device_updater.verify_package``).

Key model
---------
* The Developer generates ONE Ed25519 key pair (``generate_keypair``) and
  keeps the PRIVATE key off every other machine.
* The PUBLIC key is pinned on each device (the agent's config). A device only
  ever trusts signatures made by a key it was given out-of-band — it never
  trusts a key that arrived inside the update request itself.
* ``key_id`` is a short, non-secret fingerprint of the public key used to
  select which pinned key verified a package (and for display in logs/UI).

Nothing here logs or returns private key material.
"""
from __future__ import annotations

import base64
import hashlib

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


def _private_pem(key: Ed25519PrivateKey) -> str:
    return key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("ascii")


def _public_pem(key) -> str:
    return key.public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode("ascii")


def generate_keypair() -> tuple[str, str]:
    """Create an Ed25519 key pair. Returns ``(private_pem, public_pem)``.

    Developer-side only. The private key must never be shipped to a device.
    """
    private = Ed25519PrivateKey.generate()
    return _private_pem(private), _public_pem(private.public_key())


def key_id(public_key_pem: str) -> str:
    """A short, stable, non-secret fingerprint of a public key (16 hex chars)."""
    return hashlib.sha256((public_key_pem or "").strip().encode("utf-8")).hexdigest()[:16]


def public_pem_from_private(private_key_pem: str) -> str:
    """Derive the public PEM from a private key (developer tooling only).

    Lets the signing helper report the key id that devices must have pinned
    without the Developer having to keep the two files in step by hand.
    """
    key = serialization.load_pem_private_key(
        (private_key_pem or "").encode("utf-8"), password=None)
    return _public_pem(key.public_key())


def sign_package(payload: bytes, private_key_pem: str) -> str:
    """Detached Ed25519 signature over the exact package bytes, base64-encoded.

    ``payload`` is the SAME canonical byte string the checksum is computed
    over (``Package.serialize()``), so signature and checksum cover identical
    content.
    """
    key = serialization.load_pem_private_key(
        (private_key_pem or "").encode("utf-8"), password=None)
    signature = key.sign(payload)
    return base64.b64encode(signature).decode("ascii")


def verify_package(payload: bytes, signature_b64: str, public_key_pem: str) -> bool:
    """True only for a valid signature over ``payload`` by the given public key.

    Every failure mode (bad base64, wrong key type, tampered payload) returns
    False — never raises, so a hostile package cannot crash the updater.
    """
    if not signature_b64 or not public_key_pem:
        return False
    try:
        public = serialization.load_pem_public_key(
            public_key_pem.strip().encode("utf-8"))
        signature = base64.b64decode(signature_b64, validate=True)
        public.verify(signature, payload)
        return True
    except (InvalidSignature, ValueError, TypeError):
        return False
