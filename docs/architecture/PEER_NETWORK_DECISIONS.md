# Peer network — decisions D1, D3 and D4: recommendations

Status: **approved 2026-10-31** in the terms recommended here (see the approval
record at the end). The recommendations, options and rejected alternatives below
are kept as written: they are the rationale for the decision, not a draft of it.
Where the implementation has not caught up with an approval, the approval record
says so explicitly rather than implying the work is done.

Register context: the decisions were raised in
`docs/architecture/PEER_NETWORK_RESEARCH_AND_PLAN.md` §16 (table D1–D8) on the
`docs/peer-phase0-results` branch, and the risks they carry are tracked in
`docs/architecture/PEER_NETWORK_RISK_REGISTER.md` (R-08, R-07, R-14). This file
exists because those documents are not on `main` yet and the answers are needed
before Phase 2 is built.

| ID | Question | Recommended default | Needs your approval |
|---|---|---|---|
| D1 | Who holds the department root key, and how is it backed up? | Owner machine, DPAPI store, one explicit encrypted offline export | **Yes** — irreversible operational choice |
| D3 | Invitation as copy-paste code, file, or QR? | Keep the code as primary, add file import/export in Phase 2, defer QR | Only if you want QR |
| D4 | How long may a revoked device keep working where the revocation never arrives? | 90-day certificates, 365-day ceiling, visible expiry + renew action | **Yes** on the number; 90 is the safe default |

---

## D1 — Department root-key custody and backup

**What it decides.** Whoever holds the root private key can mint a device for the
department, and can re-sign the roster and the revocation list. Losing it is not
a repair: every certificate already issued stops verifying against the new root,
so recovery means re-inviting every device into a new department. There is no
escrow and no recovery key in Phase 1 by design.

**Options**

| Option | Advantage | Consequence |
|---|---|---|
| **A. One owner machine, DPAPI store, no backup** (today's behaviour, by omission) | Nothing to steal, nothing to leak, no procedure to get wrong | One dead disk ends the department: every device is re-invited under a new root |
| **B. A plus one explicit encrypted offline export** (recommended) | Recoverable, and the export is a deliberate act with a warning attached | The export file *is* the root key: it must be passphrase-encrypted, stored off-machine, and treated as a credential, not a document |
| **C. Split the key across two staff machines** | No single point of loss, and minting needs two people | New ceremony, a second trusted person, and a sharding implementation Phase 1 does not have |

**Recommendation: B.** Keep the key where it is — DPAPI-scoped to the owner's
Windows account, never copied by the application — and add one owner-side action
that writes a passphrase-encrypted archive plus a written restore procedure.
Forbid copying `department.json` by hand in the interface text, because that file
is DPAPI-scoped to one account and will not open on another machine anyway.

**Why not C now:** it doubles the operational surface before anyone has run the
LAN path end to end, and a shard holder who loses interest is a worse failure
mode than a backup file nobody tested.

**Dependencies and consequences**

- Phase 2 must keep the peer process in the signed-in user's session. A
  machine-wide service cannot read a current-user DPAPI blob; `secure_store`
  would silently fall back to the plaintext `MC1P` marker (R-08, and §10 item 1
  of `PEER_NETWORK_PHASE1.md`). This is the single most likely way to lose the
  key's protection without noticing.
- Phase 4 mints TLS material from the same root, so a root replacement invalidates
  sessions as well as certificates.
- Phase 10 (retiring the hub) assumes the root is the only authority; if the root
  is backed up badly, that phase inherits the problem.

**What I need from you:** whether a backup should exist at all, who may restore
it, and where it lives. I have not implemented any export: it is irreversible to
get wrong (an unencrypted export in a shared folder is worse than no backup).

---

## D3 — Invitation code format

**What it decides.** How a new device receives its certificate. The invitation is
the only contact a device has with an authorised machine before it can join
(plan §15.7), so its format sets the setup experience.

**What Phase 1 ships.** A copy-paste code: base64url of canonical JSON holding
the department public key, the device certificate, the signed roster, the signed
revocation list, the request nonce and an expiry. Whitespace and padding are
tolerated on decode; a truncated or altered code is refused before any signature
work. The code is a few kilobytes because it carries the roster.

**Trade-offs**

| Format | Advantage | Cost |
|---|---|---|
| **Copy-paste code** (shipped) | Survives chat, a scribbled note, and a locked-down USB policy; no install step; no file handling | Kilobyte-scale string; a human cannot proof-read it, only retry it |
| **File** (`.json`, same bytes) | Attaches to mail, drag-and-drop, easy to keep and re-open | Blocked by some mail filters and USB policies; needs a file picker and a store location; easier to leave lying around |
| **QR** | No typing at all on the receiving device | Needs a camera, a renderer, and a payload that fits: ~2.9 KB is the ceiling for a dense QR, so the current roster would need chunking — a protocol of its own |

**Recommendation.** Keep the code as primary. In Phase 2 add file export/import
of the *same* canonical JSON as an equal citizen, because it costs one file dialog
and removes the copy-paste failure mode; both paths go through the same
verification, so the format changes no trust property. Defer QR until the
payload is small — Phase 3 could exchange a truncated roster and fetch the rest
in-session, which would bring it inside a single symbol. Chunked QR should not
be built without a decision, because chunk reassembly is a new attack surface.

**Security consequence of any format.** The document is signed; the format only
decides how it travels. A file is easier to keep, which makes the seven-day
invitation expiry matter more — it is enforced today, and Phase 2 should say the
expiry out loud when it is close.

**Dependencies:** Phase 2 (setup surface), Phase 3 (roster size drives the code
length), and nothing in Phases 4–10.

---

## D4 — Certificate lifetime policy

**What it decides.** How long a revoked device that never meets a peer that
learned about the revocation can keep working. Revocation is eventually
consistent — that is stated, not solved (plan §9.4, R-07) — and certificate
lifetime is the only hard bound on the window.

**What Phase 1 ships.** `CERT_DEFAULT_DAYS = 90`, `CERT_MAX_DAYS = 365`, enforced
at issuance. A verifier accepts any window the root signed, which is fine while
there is one owner machine and would need revisiting only if the root were
compromised (recorded as item 2 in §10 of `PEER_NETWORK_PHASE1.md`).

**Trade-offs**

| Lifetime | Effect on the revocation window | Operational cost |
|---|---|---|
| 30 days | ≤30 days exposure | An invitation every month for every device — rejected |
| **90 days** (recommended default) | ≤90 days exposure | About three renewals per device per year, aligned with a term |
| 365 days (ceiling) | Up to a year of exposure | One renewal a year, and the warning is easy to ignore |

Renewal also has a cost shape: with no server, renewal is either a fresh
invitation or a short-lived signed token from the owner. Whichever is chosen, it
runs through the same round trip Phase 1 already tests.

**Recommendation.** Keep 90 days, keep 365 as a ceiling, and make the consequence
visible: Phase 2 should show each device's expiry, warn from 75 days, and offer a
"renew" action that issues a new invitation for the *same key* (identifier
unchanged, roster epoch + 1, no re-enrolment of other devices). State in the
interface that a revoked device may remain reachable until the department's
devices next meet, and at most until its certificate expires.

**Why not shorter:** the teacher who has to re-invite six laptops four times a
year will disable the feature, and a disabled feature protects nothing.

**Dependencies:** Phase 2 (expiry surface and renewal), Phase 4 (the TLS
certificate lifetime must match this policy), Phase 6 (diagnostics must report
expiry as a reason, not a surprise), Phase 10 (migration must keep renewals
working).

**What I need from you:** whether 90 days fits the school's term structure, or
whether you would rather align renewal with term boundaries at, say, 120 days.
The mechanics do not change; only the constant and the warning threshold do.

---

## Decisions not covered here

D2 (do teachers actually work from home at the same time?), D5 (is a Google Drive
scope acceptable?), D6 (when the hub is removed), D7 (is a peer relay
acceptable?), and D8 (the Public-Address SSE gap) are unchanged and still listed
in plan §16. D2, D5, D7 and D8 gate Phases 8–9 and do not block Phase 2.

---

## Approval record — 2026-10-31

The project owner approved all three decisions in the terms recommended below.
This section is the register entry; the rationale above is unchanged because it
is what the decision rests on.

| ID | Decision | Approved term | Where it is implemented |
|---|---|---|---|
| D1 | Root-key custody | DPAPI-protected owner store, one passphrase-encrypted offline export, root key never leaving the machine otherwise, restoration deliberate and restricted, never serialised into shared department documents | `peer/ca.py` + `distributed/secure_store.py`; the export is **approved but not built** |
| D4 | Certificate lifetime | 90 days standard, 365 days maximum, expiry enforced and reported, renewal failures reported clearly | `ca.CERT_DEFAULT_DAYS` / `CERT_MAX_DAYS`; `CERT_EXPIRED` / `CERT_NOT_YET_VALID`; Phase 2 startup refuses an expired certificate and warns inside 15 days |
| D3 | Invitation format | Copy-paste codes, file import/export in Phase 2, QR deferred | Copy-paste codes in `peer/invite.py`; file transport and QR are **not shipped** |

### What approval changes

- These are no longer open questions. Changing one needs a new decision record
  with its own rationale, not a preference expressed in code or a pull request.
- D1's export is approved **in principle and deliberately unimplemented**. The
  archive is root-key-equivalent, so it needs a review of its own before it
  exists; Phase 2 does not add it and its record says so (§11).
- D4's enforcement point moves earlier: Phase 1 enforced the lifetime at
  issuance, and Phase 2 additionally refuses a stored certificate that has
  since expired or become revoked when the process starts.
- D3's file import/export is the approved Phase 2 direction; only the code path
  is implemented so far.

### Not yet done under these decisions

- **D1 offline export** — passphrase-encrypted archive plus a written restore
  procedure. Unreviewed, unbuilt.
- **D4 renewal action** — the warning flag is computed; the "renew this device"
  flow is not implemented.
- **D3 file import/export** — copy-paste codes exist; file transport does not.
- **Recorded but unfixed** from the Phase 1 review (`PEER_NETWORK_PHASE1.md`
  §10): the silent plaintext fallback in `secure_store`, requester-chosen
  invitation expiry, and equal-epoch roster forks.
