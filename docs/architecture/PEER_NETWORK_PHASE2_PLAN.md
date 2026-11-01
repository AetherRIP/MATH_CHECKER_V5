# Peer network — Phase 2: proposed plan (not started)

Status: **proposal. No Phase 2 code exists and none has been written for this
document.** It exists so Phase 1 can be handed over with its next step already
specified, reviewed and costed.

Source: `docs/architecture/PEER_NETWORK_RESEARCH_AND_PLAN.md` §13 "Phase 2 — Peer
service process and lifecycle", with the risk register (R-08, R-09, R-10, R-14)
and the §10 review findings in `PEER_NETWORK_PHASE1.md` folded in.

---

## 1. Objective

> A supervised process that can be started, stopped and observed, with no effect
> on the application when it is absent.

Phase 2 ships the *process and its lifecycle only*. It opens no socket, performs
no discovery and speaks to no peer. That is deliberate: it is the cheapest phase
in which to prove the hardest promise in the design — that the peer layer is
inert with respect to marking (plan §10.4, R-10).

## 2. Scope boundary

| In scope | Out of scope (phase) |
|---|---|
| A `peer` service process with a real state machine | Any socket, port or packet (Phase 3+) |
| Supervision, start/stop/restart, crash containment | Discovery beacons and sweeps (Phase 3) |
| An ON/OFF switch that writes nothing when off | TLS sessions and handshakes (Phase 4) |
| A status surface that reports the real state | Presence, messaging, announcements (Phase 5) |
| Loading persisted credentials at start, refusing a broken store | Roster gossip, mailbox rendezvous (Phase 8) |
| A marking-isolation acceptance harness | Firebase/hub retirement (Phase 10) |

## 3. Dependencies on Phase 1 (exact)

Phase 1 is merged code; Phase 2 consumes it, and one review finding becomes a
Phase 2 obligation.

| Phase 1 symbol | Phase 2 use |
|---|---|
| `peer.identity.DeviceIdentity.load()` / `load_or_create()` | The process loads its identity at start. A file that exists but cannot be read **raises**; the service must report `SERVICE_DEGRADED` with the reason, never mint a new key (that would orphan every issued certificate) |
| `peer.invite.PeerCredentials.load()` | Loads certificate, roster and revocations. It now refuses a file whose parts disagree, so a corrupt store is a reported condition, not a silent fresh start |
| `peer.invite.Invitation.accept(..., expected_department_public_key=...)` | **Obligation:** the setup path must pass the key from existing credentials. Without it, a second pasted code silently re-binds the device to another root (§10 of the Phase 1 document) |
| `peer.revocation.SignedRevocationList` / `SignedRoster` | Persisted snapshots; Phase 2 only checks them for presence and epoch, not freshness (no peers yet) |
| `peer.PeerVerificationError.reason` | The reason vocabulary the status surface displays; Phase 2 must not invent a second one |

## 4. Integration points with the existing application

| Surface | What Phase 2 touches | What it must not touch |
|---|---|---|
| Process supervision | Adds a third supervised process beside the backend and the small agent, using the existing supervisor model | No new service manager, no Windows service, no elevation (`asInvoker` stays) |
| ON/OFF switch | The existing agent-control switch pattern, so a teacher can turn peer networking off without deleting anything | No change to account roles, `account_scope`, `teams` or classroom authorisation |
| Health and lifecycle reporting | A peer section in the existing status/lifecycle output, with a reason on failure | No change to the marking, correction, scheme or report paths |
| Desktop shell | A status surface showing running/stopped/degraded with a reason, plus start/stop | No new window, no new page hierarchy |
| Storage | `DATA_DIR/peer/` through the existing DPAPI-backed `secure_store` | No database schema change; phase 13 of the plan owns migration |

## 5. Deliverables (proposed, subject to step-1 confirmation)

- `backend/app/peer/service.py` — state machine (`STOPPED`, `STARTING`,
  `RUNNING`, `DEGRADED`, `STOPPING`), lifecycle hooks, credential load, no I/O
  beyond the local store.
- `backend/app/peer/supervisor.py` *(name to be confirmed against the existing
  supervisor during step 1)* — registration with the current supervision model.
- Status surface in the shell plus the backend status payload.
- `backend/tests/test_peer_service.py`, `backend/tests/test_peer_isolation.py`.
- `docs/architecture/PEER_NETWORK_PHASE2.md` recording what shipped.

No new runtime dependency. No new configuration key beyond the ON/OFF switch.

## 6. Acceptance criteria

Each is measurable and each is a test, not a claim.

1. **Start/stop/restart** through the existing supervisor, with a state
   transition history that matches what actually happened.
2. **Crash containment:** killing the peer process leaves the backend healthy
   (`/api/health` still serves, an in-flight correction still completes).
3. **Marking is unaffected:** a correction run with the peer service killed
   mid-run produces a result byte-identical to the same run without it, compared
   by content hash (plan §10.4). This is the phase's real acceptance test.
4. **OFF means nothing:** with the switch off, no peer process starts, no state
   file is written, no socket is created.
5. **Import isolation:** no module on a marking, correction, scheme or report
   path imports `app.peer` — asserted by a test that walks those modules, so it
   cannot regress silently (R-10).
6. **Broken store is reported, not replaced:** an unreadable identity or
   credentials file yields `DEGRADED` with the reason and leaves the file intact.
7. **Idle budget:** ≤0.5 % CPU and ≤40 MB RSS over an hour, measured and
   recorded, not asserted from judgement.
8. **Truthful status:** the surface never shows `RUNNING` without a live process;
   every degraded state carries one of the Phase 1 reason codes or one documented
   lifecycle reason.

## 7. Test plan by component

| Component | Unit | Integration | Regression |
|---|---|---|---|
| State machine | Transitions, illegal transitions, restart from `DEGRADED` | Supervisor start/stop/restart, double start | Status never ahead of reality |
| Credential load | Missing file, broken file, credentials for another department | Load then restart keeps the same device id | No new key is ever minted over an existing store |
| ON/OFF switch | Off writes nothing | Off then on starts cleanly | Switch does not touch `agent_control` state for other features |
| Isolation | Import-graph walk over marking modules | Kill peer mid-correction; compare hashes | Same hash with the peer service absent entirely |
| Status surface | Payload shape, reason passthrough | Desktop renders each state | No `RUNNING` without process |
| Budget | — | One-hour idle sample | Recorded in the phase document |

CI already runs `backend/tests`, so these run there. The mid-correction harness
needs a local marking run and is therefore **not** a CI gate; the phase document
must record the run and the hashes.

## 8. Risks and architectural conflicts

| Risk | Why it applies here | Mitigation in this phase |
|---|---|---|
| **R-08** DPAPI current-user scope | The root key and device key are DPAPI-scoped. A service context that is not the signed-in user loses encryption, and `secure_store` falls back to plaintext silently | Run in the user session only; at startup write and read back a probe blob and refuse to claim encryption if it fails (this is the phase that must fix §10 item 1) |
| **R-10** marking throughput | The product's value is marking accuracy | Separate process, separate log, import-isolation test, hash acceptance test, measured idle budget |
| **R-09** scope creep | Phase 2 is where "while we're here, add discovery" appears | Non-goals in §2 and a review rule: a socket in this phase is a rejected change |
| **R-14** duplicate identity | Two installs could copy a key pair | Content-addressed id already detects it; Phase 2 only has to *report* a duplicate rather than resolve it |
| Architectural conflict | The distributed layer is hub-centric and the desktop already supervises two processes; a third process changes the failure surface | Reuse the existing supervisor; when the peer process dies, the shell must degrade gracefully and never restart-loop |

Phase 0's two pending measurements (P0-1 UDP pinhole, P0-2b inbound TCP) do **not**
block Phase 2: the process starts, loads credentials and reports state without
sending a packet. They block Phase 3/4 and must be run before those begin.

## 9. Proposed sequence

Five reviewable steps, none of which is a socket:

1. **State machine + credential load** (`service.py`, unit tests). Confirm the
   exact supervisor API in this step and record it.
2. **Supervision wiring** and the ON/OFF switch, with the "off writes nothing"
   test.
3. **Status surface** in the shell and the backend status payload, with the
   truthful-status test.
4. **Isolation proof:** import-graph test, mid-correction hash harness, one-hour
   idle measurement.
5. **Documentation:** `PEER_NETWORK_PHASE2.md`, plus the DPAPI probe decision from
   §10 item 1 of the Phase 1 document.

Each step is its own pull request against `main`, in that order. Phase 3 (LAN
discovery) starts only after step 5, and only after P0-1 and P0-2b are measured.

## 10. Open questions for the owner

1. **D1 (root-key backup)** must be answered before the setup path mints
   certificates at scale; it does not block steps 1–4.
2. Should the peer process be started by the desktop shell or by the backend
   host? The shell matches "runs in the signed-in user's session" and survives a
   backend restart; the backend host makes the status API trivial. **Recommendation:
   shell**, because DPAPI scope is the constraint that cannot be worked around.
3. Is a visible ON/OFF switch in scope for Phase 2, or does the feature stay dark
   until Phase 5 gives it something to show? **Recommendation:** ship the switch
   in Phase 2 — a feature you cannot turn off is harder to trust.
4. D4 (certificate lifetime number) should be settled before the status surface
   shows expiry, so the warning threshold has one source.
