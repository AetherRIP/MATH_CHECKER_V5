# Peer network — risk register

Companion to [PEER_NETWORK_RESEARCH_AND_PLAN.md](PEER_NETWORK_RESEARCH_AND_PLAN.md)
and [ADR-001-PEER-NETWORK.md](ADR-001-PEER-NETWORK.md).

Nothing here is implemented yet. The register exists so that the risks which
could invalidate the design are tracked before code is written, not discovered
after.

**Scoring:** likelihood (L) and impact (I) are High / Medium / Low. Priority is
`H`/`M`/`L` on the combination, not a multiplied score, because a coarse
comparison is more honest than false precision. **Owner** is the role that has to
act, not a person.

---

## Priority summary

| ID | Risk | L | I | Priority | Phase |
|---|---|---|---|---|---|
| R-01 | Windows Firewall blocks non-elevated LAN data paths | H | H | **H** | 0 |
| R-02 | School WLAN blocks multicast or isolates clients | H | H | **H** | 0, 3 |
| R-03 | Remote connectivity expectation exceeds what physics allows | H | H | **H** | 8 |
| R-04 | Hub removal breaks existing multi-machine deployments | M | H | **H** | 10, migration |
| R-05 | Handshake or certificate-validation defect | M | H | **H** | 1, 4 |
| R-06 | Drive mailbox scope triggers OAuth consent or verification work | M | M | M | 8 |
| R-07 | Revocation cannot reach an offline peer | H | M | M | 1 |
| R-08 | DPAPI current-user scope misused by a service context | M | M | M | 1, 2 |
| R-09 | Scope creep turns the bus back into a VPN | M | H | M | all |
| R-10 | Peer service affects marking or correction throughput | L | H | M | 2 |
| R-11 | Reconnect storms when several laptops wake together | M | M | M | 6 |
| R-12 | New dependency fails the release allowlist or size guard | M | M | M | 7, 12 |
| R-13 | Free-tier terms change under the design | M | M | M | 8, 9 |
| R-14 | Duplicate device identity or cloned installation | L | M | L | 1, 3 |
| R-15 | Public-Address SSE gap is real and already shipping | M | L | L | separate |

---

## Detail

### R-01 — Windows Firewall blocks non-elevated LAN data paths

**Why it matters.** The measured constraint is that the desktop runs `asInvoker`
and installs per-user, so it cannot create a firewall rule, and Microsoft
documents that a non-administrator's "Allow" does not create one. The design
assumes a UDP firewall pinhole opens when both peers send to each other. That
assumption is untested.

**Status 2026-10-31 — partially measured.** The half that is settled: a
non-elevated process attempting `netsh advfirewall firewall add rule` is refused
with "requires elevation" and no rule appears (P0-2a). The half that decides the
risk — whether the pinhole opens — still needs two laptops (P0-1), because a
single host's self-addressed traffic never touches the inbound filter.

**Early warning.** Phase 0 experiment P0-1 shows return traffic being dropped.

**Mitigation.**
- Run P0-1 and P0-3 before writing any data-path code.
- If the pinhole does not open: fall back to discovery-only over 5353 plus the
  elevated-rule path, and state in the interface that direct connections need a
  one-time approval.
- Keep the relay adapter available so a blocked pair is not a dead pair.

**Residual risk.** Some school-managed laptops will never allow a rule. Those
devices get a reduced feature set, and the interface must say so plainly.

---

### R-02 — School WLAN blocks multicast or isolates clients

**Why it matters.** AP/client isolation and multicast filtering are common
managed-WLAN features. Both defeat beaconing, and the failure looks identical to
"nobody else is running the app".

**Status 2026-10-31 — partially measured.** Multicast delivery to two co-bound
listeners on one host works (2/2), so the local stack is not the obstacle. Whether
a school WLAN delivers it between two hosts is untested, and no evidence here
speaks to it.

**Early warning.** Beacons sent, none received, while ordinary unicast to the
same host succeeds.

**Mitigation.**
- Unicast subnet sweep as a fallback, rate-limited and local-link only.
- Distinguish "no peers running" from "peers exist but cannot be seen" in the
  interface — the second is actionable, the first is not.
- Document the network requirements in the deployment guide.

**Residual risk.** On a fully isolated guest SSID nothing works, and that is a
correct outcome to report rather than work around.

---

### R-03 — Remote connectivity expectation exceeds what physics allows

**Why it matters.** The brief asks for automatic remote connections. Two peers
behind endpoint-dependent NAT cannot connect directly, and no free software fixes
it. If the interface implies otherwise, a teacher will conclude the feature is
broken rather than the network.

**Early warning.** Support questions of the form "it worked at school but not at
home" with no reason shown.

**Mitigation.**
- `REMOTE_UNAVAILABLE` is a first-class state with a per-adapter reason.
- The research document states the limit in its executive summary, not a
  footnote.
- Never show a spinner where an honest "not possible on this network" belongs.

**Residual risk.** Unavoidable. This is why the ADR records it as a negative
consequence rather than pretending it away.

---

### R-04 — Hub removal breaks existing multi-machine deployments

**Why it matters.** `backend/app/distributed/` is hub-centric by design, and
`DISTRIBUTED_DEPLOYMENT.md` documents an always-on mini-PC as the supported
setup. Removing it is a migration with data and documentation consequences.

**Early warning.** A deployment that upgrades and can no longer see its workers.

**Mitigation.**
- Keep the HTTP-hub path available behind a flag until Phase 10 is proven.
- Migrate the documented deployment guide in the same release that flips the
  default, never before.
- Treat "remove the hub" as its own decision (D6 in the research document).

---

### R-05 — Handshake or certificate-validation defect

**Why it matters.** This is the component where a mistake is silent and total.
The ADR deliberately replaces a bespoke handshake with TLS 1.3 for this reason.

**Early warning.** Negative test cases passing that should fail, or reason codes
collapsing into a generic "auth failed".

**Mitigation.**
- TLS 1.3 mutual authentication, no bespoke state machine.
- Explicit negative tests: wrong CA, other department, expired, revoked, downgrade
  attempt, identifier/key mismatch, epoch rollback.
- Distinct reason per failure so a defect is visible rather than masked.
- No custom cipher-suite selection; no fallback below TLS 1.3.

---

### R-06 — Drive mailbox scope triggers consent or verification work

**Why it matters.** The recommended remote rendezvous reuses the department's
Google account. Adding a Drive scope may require a new consent screen and, for
restricted scopes, verification — which is process work with a calendar attached.

**Early warning.** The consent screen lists a new scope, or the OAuth client is
rejected for a scope it did not previously request.

**Mitigation.**
- Confirm before Phase 8; LAN phases are unaffected either way (decision D5).
- If the scope is unacceptable, the anchor and DHT adapters still exist, degraded
  but functional.
- Keep the mailbox interface narrow enough that a different store can replace it.

---

### R-07 — Revocation cannot reach an offline peer

**Why it matters.** A revoked device that never comes online can still reach a
peer that has never learned the revocation. This is true of every decentralised
scheme and must not be described as solved.

**Mitigation.**
- Monotonic revocation epoch prevents rolling the list back.
- Short certificate lifetimes (90 days recommended) bound the window.
- Existing audit trail records who revoked what, and when.
- The interface warns that a revoked device may remain reachable until devices
  next meet.

---

### R-08 — DPAPI current-user scope misused by a service context

**Why it matters.** `secure_store.py` protects for the current user and silently
falls back to a plaintext marker (`MC1P`) when DPAPI is unavailable. A peer
process running as another account or as `SYSTEM` would lose encryption without
saying so.

**Mitigation.**
- The peer process runs as the signed-in user, like `tiny_agent`.
- At startup, write and read back a probe blob and refuse to start in a degraded
  mode without reporting it.
- Never move the peer service into a machine-wide service without revisiting
  this.

---

### R-09 — Scope creep turns the bus back into a VPN

**Why it matters.** Every incremental request ("just let me reach the shared
folder", "just add a tunnel") pulls the design back toward drivers, routing and
administrative installs — the things this ADR removed on purpose.

**Early warning.** A proposal to add a TUN device, an IP address, a route, a
system service, or a mandatory third-party overlay.

**Mitigation.**
- The ADR is the guardrail: a change that reintroduces any of those needs a new
  ADR, not a pull request.
- Application services sit above the bus and never change its transport.

---

### R-10 — Peer service affects marking or correction throughput

**Why it matters.** Marking accuracy is the product. Networking must be provably
inert.

**Mitigation.**
- Separate process, separate port, separate log.
- Nothing in the correction path imports the peer layer.
- Acceptance test: a correction run with the peer service killed mid-run produces
  a byte-identical result.
- Idle budget measured: ≤0.5% CPU, ≤40 MB RSS.

---

### R-11 — Reconnect storms

**Why it matters.** Six laptops waking at 08:30 on the same Wi-Fi can synchronise
their retries and their beacons, which is exactly when the network is busiest.

**Mitigation.** Full jitter on both beacon and reconnect schedules; per-peer
token buckets; a global cap on concurrent handshakes; randomised start delay
after a network change.

---

### R-12 — New dependency fails the release allowlist or size guard

**Why it matters.** Dependencies are a release decision here, gated by
`MathChecker_Distribution/scripts/release_allowlist.py` and
`scripts/size_guard.py`. A library that works in development can still fail the
build.

**Mitigation.** Phases 1–6 add nothing. Any addition is proposed with its licence,
size and Windows-wheel status, and goes through the pipeline before it is relied
on. `python-zeroconf` (LGPL) and `aiortc` (PyAV) are already rejected on these
grounds.

---

### R-13 — Free-tier terms change under the design

**Why it matters.** The research found terms already in flux: Tailscale's pricing
changed in April 2026, ZeroTier's free plan changed for new signups in November
2025, and Cloudflare removed and relocated a terms clause. Any of them can change
again.

**Mitigation.** No required path depends on a free tier. Adapters are optional and
replaceable; LAN operation is unaffected by any provider decision. Re-verify
terms before each phase that touches one.

---

### R-14 — Duplicate device identity or cloned installation

**Why it matters.** The current model uses a random UUID with no key binding, so
copying a `device_id` is trivial. The new design derives the identifier from the
public key, which makes cloning detectable but not impossible (a copied *private
key* is a different problem).

**Mitigation.** Identifier/key mismatch is treated as an attack and reported;
duplicate identifiers with different keys are refused; the interface surfaces it
instead of silently picking one.

---

### R-15 — Public-Address SSE gap

**Why it matters.** Cloudflare documents that Quick Tunnels do not support
Server-Sent Events, and `public_access.py` verifies health, UI, auth status and a
refused API call — but no SSE route. Some progress displays may already be broken
through a Public Address.

**Mitigation.** Investigate separately; it is an existing feature and outside the
peer-network scope. Recorded here so it is not lost.

---

## Review cadence

Re-score after Phase 0 (R-01, R-02), after Phase 1 (R-05, R-07, R-08), and after
Phase 8 (R-03, R-06, R-13). A risk that changes priority changes the plan, not
just this table.
