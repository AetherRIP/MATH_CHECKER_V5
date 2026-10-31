"""Peer network — Phase 1: device identity, department CA, roster, revocation.

The peer layer answers one question before any packet is sent: *is the device on
the other end who it claims to be, and is it still allowed to be here?* Nothing
in this package opens a socket. Phase 1 is deliberately network-free so that the
identity rules can be tested exhaustively without a second host, and so that a
failure here cannot affect marking: no correction, marking, scheme or report
path imports this package (plan §10.4).

The trust model, in one paragraph: a department owns one Ed25519 root key. A
device owns its own Ed25519 key, and its identifier is *derived* from that key
(``identity.device_id_for_public_key``), so an identifier cannot be copied
between machines. The root signs a certificate for a device; the root also signs
the roster (who is in the department) and the revocation list (who has been
removed). A peer verifies a certificate, a roster and a revocation list against
the pinned department public key alone — no server, no hub, no network.

Deliberate scope limits, recorded rather than hidden:

* **No TLS here.** The certificate binds the identity key, and reserves a
  ``tls_public_key`` field, but the X.509/TLS 1.3 session material is Phase 4
  (plan §9.2, correction 1). Do not hand-roll a handshake on top of this module.
* **No application roles here.** A certificate carries department-level roles
  (for example who may revoke). Whether an *account* may see a class stays with
  ``accounts.scoped_ids()``/``teams``/``account_scope`` (plan §9.1). Duplicating
  that model in the peer layer would create a second permission system.
* **No database schema change.** Phase 1 state lives in files under
  ``DATA_DIR/peer`` (keys via the existing DPAPI-backed store) and in the
  existing audit trail. The hub tables are untouched until the migration in
  plan §13.
"""
from __future__ import annotations

#: Wire version of every signed document in this package. A document carrying a
#: different ``format`` or ``version`` is rejected as ``VERSION_MISMATCH``
#: rather than guessed at: a peer that speaks a newer dialect must be refused
#: loudly, not silently half-understood (plan §9.3, downgrade).
PROTOCOL_VERSION = 1

# -- verification reasons ----------------------------------------------------
#
# The plan (§10.2) fixes a reason vocabulary so the interface can explain a
# failure in a sentence a teacher can act on. Phase 1 reuses those names where
# they exist and adds the ones the connection layer does not have. Every
# rejection carries exactly one reason, and the tests assert the *distinct*
# reason for each case rather than merely "it failed".

#: A signature over the payload does not verify against the expected key.
AUTH_FAILED = "AUTH_FAILED"
#: The device is on the department's revocation list.
REVOKED = "REVOKED"
#: The certificate's validity window has closed.
CERT_EXPIRED = "CERT_EXPIRED"
#: The certificate's validity window has not opened yet.
CERT_NOT_YET_VALID = "CERT_NOT_YET_VALID"
#: The document belongs to a different department than the one expected.
WRONG_DEPARTMENT = "WRONG_DEPARTMENT"
#: The identifier does not derive from the public key it is presented with.
IDENTIFIER_KEY_MISMATCH = "IDENTIFIER_KEY_MISMATCH"
#: Two different keys claim one identifier inside one roster.
DUPLICATE_DEVICE_ID = "DUPLICATE_DEVICE_ID"
#: Unknown document format, or a protocol version this build does not speak.
VERSION_MISMATCH = "VERSION_MISMATCH"
#: A revocation or roster epoch older than one already seen (replay).
EPOCH_ROLLBACK = "EPOCH_ROLLBACK"
#: An invitation code that cannot be decoded, is structurally invalid, or
#: answers a different invitation request.
INVITE_INVALID = "INVITE_INVALID"
#: A request or invitation whose expiry has passed.
INVITE_EXPIRED = "INVITE_EXPIRED"
#: The invitation's roster does not list the device it issued a certificate to.
ROSTER_INCOMPLETE = "ROSTER_INCOMPLETE"
#: A document that cannot be parsed at all (missing/ill-typed fields).
MALFORMED = "MALFORMED"

#: Every reason this package can raise, so a caller (and a test) can assert that
#: a rejection reason is one the user interface is able to explain.
REASONS = frozenset({
    AUTH_FAILED, REVOKED, CERT_EXPIRED, CERT_NOT_YET_VALID, WRONG_DEPARTMENT,
    IDENTIFIER_KEY_MISMATCH, DUPLICATE_DEVICE_ID, VERSION_MISMATCH,
    EPOCH_ROLLBACK, INVITE_INVALID, INVITE_EXPIRED, ROSTER_INCOMPLETE,
    MALFORMED,
})


class PeerError(Exception):
    """Base class for peer-layer failures.

    Raised for *local* misconfiguration (for example asking a certificate
    authority that holds no private key to issue a certificate). A rejected
    peer is never reported with this type: that is
    :class:`PeerVerificationError`, because the two need different handling —
    one is a bug in the calling code, the other is untrusted input.
    """


class PeerVerificationError(PeerError):
    """Untrusted input was rejected, with one machine-readable reason.

    ``reason`` is always one of :data:`REASONS`; ``detail`` is a short
    human sentence suitable for a log line and for the interface. Neither ever
    contains key material or a payload echo.
    """

    def __init__(self, reason: str, detail: str = "") -> None:
        if reason not in REASONS:            # pragma: no cover - programmer error
            raise ValueError(f"unknown peer verification reason: {reason!r}")
        self.reason = reason
        self.detail = detail or ""
        super().__init__(f"{reason}: {self.detail}" if self.detail else reason)

    def as_dict(self) -> dict:
        """The shape the interface consumes: a reason plus a sentence."""
        return {"reason": self.reason, "detail": self.detail}
