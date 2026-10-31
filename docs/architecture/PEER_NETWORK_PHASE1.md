# Peer network — Phase 1: identity, department CA, roster, revocation

Status: **implemented.** Phase 1 of the peer-network roadmap is code plus tests,
not a design note. Nothing in this phase opens a socket, and no correction,
marking, scheme or report path imports it.

The peer design itself (research, alternatives, threat model, phased roadmap)
lives in the peer-network plan and `ADR-001-PEER-NETWORK.md`, which are on the
`docs/peer-phase0-results` branch until that documentation is merged. This file
is self-contained: it describes only what Phase 1 ships, so a reviewer does not
need the plan to read the code.

---

## 1. What Phase 1 answers

> A device can prove who it is, and a peer can verify it **without any server**.

That is the whole objective. Discovery (Phase 3), sessions (Phase 4) and every
shared feature (Phases 5–10) need this answer to exist first; none of them is
started here.

## 2. Module map

| File | Responsibility |
|---|---|
| `backend/app/peer/__init__.py` | Wire version, the verification-reason vocabulary, `PeerError` / `PeerVerificationError` |
| `backend/app/peer/identity.py` | Ed25519 device key pair; content-addressed `device_id`; canonical JSON; secure storage |
| `backend/app/peer/ca.py` | Department root key; certificate issuance; `verify_certificate()` |
| `backend/app/peer/roster.py` | Signed roster of admitted devices with a monotonic epoch |
| `backend/app/peer/revocation.py` | Signed revocation list with a monotonic epoch, and the audited revoke operation |
| `backend/app/peer/invite.py` | Invitation request → invitation → credentials round trip |

Tests: `backend/tests/test_peer_identity.py`, `backend/tests/test_peer_invite.py`.

No new runtime dependency. Ed25519, PEM handling and detached signatures are
reused from `app/distributed/signing.py`; key-at-rest protection is reused from
`app/distributed/secure_store.py` (DPAPI, current user). `cryptography` was
already a declared runtime requirement.

## 3. Trust model

```
department root (Ed25519, owner machine only)
   ├── signs a device certificate        → binds device key ⇄ department
   ├── signs the roster                  → who is in the department, epoch n
   └── signs the revocation list         → who was removed, epoch m

device identity (Ed25519, one per machine)
   └── device_id = base32(SHA-256(raw public key))[0:26]
```

* **The identifier is derived from the key.** A device cannot present another
  device's identifier without that device's private key. This closes the gap
  where `devices.device_id` is a random UUID that anything could claim.
* **The root private key never travels.** What moves between machines is the
  root *public* key, device certificates, the signed roster and the signed
  revocation list. No document in this package can contain a private key, and a
  test asserts it for every shared document.
* **A peer verifies offline.** Certificate + roster + revocation list + pinned
  department public key is sufficient. There is no hub call on the verification
  path.

## 4. Wire documents

All documents are canonical JSON (`sort_keys`, compact separators, ASCII
escapes) so two machines sign identical bytes. Each carries `format` and
`version`; anything else is refused as `VERSION_MISMATCH` rather than guessed at.

| Document | `format` | Signed by | Contents |
|---|---|---|---|
| Certificate | `mc5-peer-cert-v1` | department root | `dept_id`, content-addressed `device_id`, identity key, optional `tls_public_key`, `roles`, `label`, validity window, `signer` |
| Roster | `mc5-peer-roster-v1` | department root | `epoch`, the certificates |
| Revocation list | `mc5-peer-revocations-v1` | department root | `epoch`, `(device_id, revoked_at, reason, actor)` |
| Invitation request | `mc5-peer-invite-request-v1` | **the new device's own key** | public key, `label`, `nonce`, expiry |
| Invitation | `mc5-peer-invitation-v1` | department root | department public key + name, certificate, signed roster, signed revocations, request `nonce`, expiry |

Invitation codes are base64url of the canonical JSON, with whitespace tolerated
on decode. They are designed to be pasted into a chat or read out; a truncated
or altered code fails loudly instead of decoding to something subtly different.

## 5. Verification order and reason codes

Each rejection has exactly one reason, because the interface has to explain them
differently and because "rejected for the wrong reason" is how a test passes
while the hole stays open.

Certificate (`ca.verify_certificate`):

| Order | Check | Reason |
|---|---|---|
| 1 | format / protocol version | `VERSION_MISMATCH` |
| 2 | department matches the expected one | `WRONG_DEPARTMENT` |
| 3 | root signature over every field | `AUTH_FAILED` |
| 4 | `device_id` derives from `device_public_key` | `IDENTIFIER_KEY_MISMATCH` |
| 5 | not issued in the future, inside validity window | `AUTH_FAILED` / `CERT_NOT_YET_VALID` / `CERT_EXPIRED` |
| 6 | not on the revocation list | `REVOKED` |

Roster adds `EPOCH_ROLLBACK` (older than an epoch already seen) and
`DUPLICATE_DEVICE_ID` (two keys claiming one identifier — treated as an attack,
never merged). Revocation lists carry the same epoch rule. Invitations add
`INVITE_INVALID` (unreadable code, or an answer to a different request),
`INVITE_EXPIRED`, and `ROSTER_INCOMPLETE`.

## 6. The invitation round trip

```
new device                                  owner machine (has the root key)
  identity = DeviceIdentity.generate()
  request  = InviteRequest.create(identity)   ── signed with the device key ──►
                                              request.verify()           (possession)
                                              department.issue_certificate()
                                              roster.with_device()
                                              SignedRoster.sign()
                                              SignedRevocationList.sign()
  ◄── Invitation.issue(...)  ── signed with the root key ──────────────────────
  invitation.accept(identity, request=request)
      → PeerCredentials (identity, certificate, roster, revocations, dept key)
```

The request's self-signature is what stops a forwarded request planting a
certificate on a machine that never asked for one. The invitation's nonce binds
the answer to the request. `PeerCredentials` is what Phases 3–5 will load: it
carries everything needed to verify a peer it meets later, and nothing that
should not be saved (`public_dict()` is the loggable/log-safe view).

## 7. Storage and audit

* `identity.save()` / `DepartmentCA.save()` / `PeerCredentials.save()` write
  under `DATA_DIR/peer/` through `secure_store` (DPAPI on Windows, atomic write,
  `0600` fallback elsewhere).
* An existing but unreadable state file raises instead of returning "no
  identity": silently minting a new key would invalidate every certificate
  already issued for the device.
* `revocation.revoke_device(..., audit=True)` appends the existing audit trail
  (`peer.device_revoked`, with department, epoch, reason, actor). Tooling that
  runs without the application database passes `audit=False`.
* **No database schema change.** The hub tables are untouched; migrating the
  peer path to primary is a later phase and needs its own plan.

## 8. Acceptance evidence

| Phase 1 requirement | Where |
|---|---|
| Forged signature rejected, distinct reason | `test_certificate_signature_covers_every_field`, `test_certificate_signed_by_another_key_is_refused`, `test_tampered_roster_is_refused` |
| Wrong department rejected | `test_certificate_from_another_department_is_refused`, `test_roster_refuses_to_hold_another_departments_certificate`, `test_request_naming_another_department_is_refused` |
| Expired certificate rejected | `test_expired_and_not_yet_valid_certificates_are_distinguished`, `test_credentials_refuse_an_expired_own_certificate` |
| Revoked device rejected | `test_revoked_device_is_refused`, `test_revoked_device_cannot_accept_an_invitation` |
| Identifier / key mismatch rejected | `test_certificate_whose_identifier_does_not_match_its_key_is_refused`, `test_identity_with_a_foreign_identifier_is_refused`, `test_certificate_cannot_be_accepted_by_another_device` |
| Epoch rollback rejected | `test_roster_epoch_rollback_is_refused`, `test_revocation_epoch_rollback_is_refused` |
| Invitation tampering rejected | `test_tampered_invitation_is_refused`, `test_garbled_code_is_refused`, `test_invitation_is_bound_to_the_request_it_answers` |
| Round-trip invitation yields a working certificate | `test_round_trip_invitation_produces_a_working_certificate` |
| Root key never leaves the owner machine | `test_no_private_key_reaches_a_shared_document`, `test_codes_survive_a_copy_paste` |

## 9. Deliberate limits and open decisions

1. **Not X.509 yet.** The certificate is signed JSON. It already contains the
   fields the plan requires (identity key, TLS key, identifier, department,
   roles, validity), and Phase 4 mints the X.509 wrapper from them for TLS 1.3.
   Nothing here should be extended into a hand-rolled handshake.
2. **`tls_public_key` is reserved, unused.** Issuance accepts it; Phase 4 sets it.
3. **Certificates are bounded (default 90 days, maximum 365).** Revocation cannot
   reach a peer that never meets the revoked device, so certificate lifetime is
   the real bound on that window.
4. **Department roles are not application roles.** A certificate can say "may
   revoke"; it never says which classes an account may see. That stays with
   `accounts.scoped_ids()`, `teams` and `account_scope`.
5. **An invitation code is a few kilobytes** because it carries the roster.
   Acceptable for a one-time paste; a QR/compact form is a Phase 2+ decision
   (plan D3).
6. **Open question for the owner** (plan D1/D4): who holds the root private key,
   how is it backed up offline, and who may revoke. Phase 1 provides the
   mechanics, not the policy.
7. **Not wired into the application yet.** No route, no desktop surface, no
   process supervision; those are Phase 2. That is deliberate: Phase 1 must be
   provably inert with respect to marking.

## 10. Independent review, 2026-10-31

A review of this package against the plan's §9 and §15 found two defects, both
fixed here with regression tests, and four findings recorded rather than quietly
accepted.

**Fixed — a second invitation could replace the department key.**
`Invitation.accept()` verified the envelope against the department key *inside*
it and never compared that key with the one the device already trusted. A device
that pasted a second code — signed by a different root, for its own key — was
re-bound to that root with no error. `accept()` now takes
`expected_department_public_key` and reports `WRONG_DEPARTMENT` on a mismatch.
It is optional because no Phase 1 caller holds a key yet; **Phase 2 must pass
`PeerCredentials.department_public_key` whenever one exists.**

**Fixed — a saved credential could mix devices or departments.**
`PeerCredentials.load()` rebuilt the identity, certificate, roster and revocation
list without checking that they belong together, and never checked that the
stored private key matches the stored public key. Every document was
individually valid, so nothing complained. `DeviceIdentity.verify_binding()` now
checks the key pair as well as the identifier, and `load()` refuses a file whose
`dept_id`, identity and certificate disagree — matching the rule in §7 that a
broken store raises instead of looking like a first run.

**Recorded, not changed**

1. **`secure_store` falls back to plaintext silently.** If DPAPI fails on
   Windows the file is written unencrypted with the `MC1P` marker. That is
   deliberate for a device credential and wrong for the root private key. Phase 2
   must probe at startup and refuse to run degraded without saying so (R-08);
   changing the shared store here would alter behaviour the update and
   device-credential paths already depend on.
2. **Certificate lifetime is bounded at issuance, not at verification.** The
   365-day ceiling is enforced by `DepartmentCA.issue_certificate`; a verifier
   accepts any window the root signed. That only matters with a compromised root,
   which can mint anything anyway, and a verifier-side policy needs the Phase 4
   handshake to report the reason to a user.
3. **A requester chooses its own expiry.** `InviteRequest.verify()` enforces the
   expiry in the document, not a maximum, so a request can be made to last years.
   It cannot mint anything: the owner still has to act, and the invitation has
   its own seven-day expiry.
4. **Equal epochs are not distinguished.** Rollback protection refuses an epoch
   *older* than one already seen, so two different rosters at the same epoch are
   both acceptable. Only a second signing machine or a compromised CA can produce
   that; Phase 1 has exactly one owner machine, and fork detection belongs with
   whoever gossips rosters in Phase 3.
