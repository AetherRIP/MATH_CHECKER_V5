# MathChecker V5 — Automatic peer network: research and plan

Status: **research and design only.** Nothing in this document has been
implemented. No source file, dependency, configuration or database schema was
changed to produce it, and no claim below should be read as a statement that a
feature exists.

Companion documents: the decision itself is recorded in
[ADR-001-PEER-NETWORK.md](ADR-001-PEER-NETWORK.md); the ranked risks live in
[PEER_NETWORK_RISK_REGISTER.md](PEER_NETWORK_RISK_REGISTER.md); the current
component map is [ARCHITECTURE.md](ARCHITECTURE.md).

Every statement is tagged as one of:

- **verified** — read in this repository, or measured on this machine, on the
  date given;
- **sourced** — stated by a primary source listed in §17, with that source's
  check date;
- **assumed** — a belief that the plan depends on and that must be tested
  before code is built on it;
- **recommended** — a design choice made here, not a fact.

---

## 1. Executive summary

Two to six teachers' laptops running MathChecker should find each other, prove
who they are, and exchange a small amount of data securely — without a paid
server, a domain, a VPN subscription, port forwarding, or a machine somebody has
to maintain.

The central finding of this investigation is that **the project should not build
a peer-to-peer network in the tunnel/VPN sense at all.** The requirement is not
packet routing between machines; it is authenticated, confidential
request/response and pub/sub between a handful of MathChecker instances. Framing
it as a *message bus between trusted application instances* rather than a mesh
VPN removes the hardest and most fragile parts of the original brief — TUN/TAP
drivers, WireGuard, IP addressing, routing tables, port forwarding, and
third-party overlays — from the critical path entirely.

The recommended architecture (Option B, §7) is:

1. **LAN first, and genuinely first.** Discovery over signed UDP multicast on
   port 5353, which is the one inbound path Windows already permits to a
   non-elevated application. Data over per-peer UDP using firewall pinholes
   opened by outbound traffic, with an optional TCP listener when a firewall
   rule exists.
2. **Sessions authenticated by TLS 1.3 mutual authentication** against a
   department certificate authority, instead of a hand-rolled X25519/HKDF/AEAD
   handshake.
3. **Remote as best effort, with a pluggable rendezvous plane** whose adapters
   are tried in order: LAN gossip of a signed roster → a peer that happens to be
   reachable → a signed address mailbox in the department's existing Google
   Drive → optionally a public DHT → and otherwise an honest `REMOTE
   UNAVAILABLE`.
4. **Never a mandatory third party.** Every external service is an optional
   adapter; LAN operation must not depend on any of them.

What is genuinely impossible at $0 is stated plainly in §4: two peers *both*
behind endpoint-dependent NAT cannot be connected directly, and automatic
cross-network discovery with no third party reachable by both peers is not a
solved problem at any price. The design therefore promises LAN reliability and
remote best-effort, and makes the difference visible in the interface rather
than hidden in logs.

---

## 2. Confirmed project requirements

These are the requirements the architecture is measured against. They are
restated here so later sections can be checked against them.

| # | Requirement | Consequence for the design |
|---|---|---|
| R1 | $0: no paid domain, VPS, TURN, VPN, cloud database or database hosting | Only free software, existing hardware, and optional free tiers |
| R2 | No central computer to maintain | No always-on coordinator may be *required*; coordination must migrate |
| R3 | No manual IP entry, port forwarding, or network configuration | Discovery must be automatic; configuration limited to a one-time trust invitation |
| R4 | No mandatory third-party VPN or admin install | No TUN/Wintun driver, no system service, no elevation required |
| R5 | LAN preferred over internet paths when both are available | Path selection must rank LAN above WAN explicitly, and prove it |
| R6 | Secure: mutual authentication, confidentiality, revocation | Signatures alone are not enough; sessions must be encrypted |
| R7 | Marking accuracy must never depend on the network | Every peer feature additive; peer failure cannot alter a result |
| R8 | Schools' networks are hostile: firewalls, AP isolation, filtered multicast | LAN discovery needs a non-multicast fallback, and failure must be legible |
| R9 | 2–6 laptops today, perhaps a few more later | No DHT-scale machinery; O(n) gossip is fine |
| R10 | Must not weaken or duplicate existing security | Reuse the existing identity, scope, team and audit models |

**Assumed environment** (must be confirmed by the Phase 0 experiments in §13):
Windows 10/11 laptops, teachers usually but not always local administrators, a
shared school Wi-Fi for at least part of the week, and Google Classroom already
connected for the classes in question.

---

## 3. Existing repository findings

Each row was checked against the current working tree. Where the earlier
investigation's summary differs from what the code now says, the difference is
called out.

| # | Earlier claim | Verdict | Evidence |
|---|---|---|---|
| 1 | `backend/app/distributed/discovery.py` performs account discovery through a known HTTP hub, not LAN peer discovery | **Confirmed** | `register()` issues a 24-hour revocable device credential; `discover()` reads the live `accounts` table and heartbeat-derived availability. No sockets, no multicast, no peer-to-peer code. |
| 2 | Existing Ed25519 signing and verification are reusable | **Confirmed, with a caveat** | [`signing.py`](../../backend/app/distributed/signing.py) signs *update packages* with a pinned developer public key, PEM + base64. The primitives (`cryptography` Ed25519) are reusable; the file format and the single-key trust model are not. |
| 3 | DPAPI-based secure storage already exists | **Confirmed, with a caveat** | [`secure_store.py`](../../backend/app/distributed/secure_store.py) calls `CryptProtectData` with no entropy and **default current-user scope**. A process running as another user (or as `SYSTEM`) cannot decrypt the blob. |
| 4 | Device-link and revocation structures exist | **Confirmed — server-side only** | `device_links`, `device_credentials.revoked_at`, `discovery.revoke_device()`. Every check requires the hub; there is no peer-verifiable revocation. |
| 5 | Presence states include ONLINE, BUSY, PAUSED, OFFLINE | **Confirmed but incomplete** | [`agents.py`](../../backend/app/distributed/agents.py) also defines `ONLINE_CHECKING`, `AVAILABLE`, `ERROR`, `UPDATING`, `RECOVERY`. `HEARTBEAT_INTERVAL_SECONDS = 300`, `OFFLINE_AFTER_SECONDS = 660`. |
| 6 | A separate peer listener is preferable to exposing peer routes through the main API | **Confirmed, for an additional and stronger reason** | The main API is wrapped by `PublicAuthMiddleware`, which exempts `/api/distributed/*` because those routes authenticate themselves. Peer traffic must not inherit that exemption, and a separate process also isolates a peer crash from the correction engine. |
| 7 | *(new)* Device identity is a random UUID, not a key | **Gap** | `devices.device_id TEXT PRIMARY KEY`; [`schema.py`](../../backend/app/distributed/schema.py) has no public-key column. Nothing prevents a device copying another's `device_id`. |
| 8 | *(new)* `/heartbeat` is presence-only and unauthenticated by design | **Confirmed limitation** | Documented in [DISTRIBUTED_DEPLOYMENT.md](../development/DISTRIBUTED_DEPLOYMENT.md) §10.3. Presence must therefore not be trusted when it arrives from the peer layer. |
| 9 | *(new)* The distributed layer is hub-centric by design | **Architectural conflict** | [`distributed/__init__.py`](../../backend/app/distributed/__init__.py): "The shared backend is the source of truth for distributed metadata; the Teacher's computer is just another client." Replacing the hub is a migration, not an addition. |
| 10 | *(new)* New dependencies are a release decision, not a `pip install` | **Constraint** | `MathChecker_Distribution/scripts/release_allowlist.py`, `scripts/size_guard.py`, and [docs/size_optimization/](../size_optimization/README.md) all gate what ships. |
| 11 | *(new)* Crypto needed for the session layer is already a runtime dependency | **Confirmed** | `backend/requirements.txt` allows `cryptography>=41.0`; the environment has `cryptography 50.0.2`, which provides X25519, ChaCha20-Poly1305, HKDF and the X.509 builder. |
| 12 | *(new)* No networking library is installed | **Confirmed** | No `zeroconf`, `aioice`, `aiortc` or `libp2p` in `backend/requirements-lock.txt`. `websockets` is installed only because `google-genai` requires it; it is not a declared runtime requirement of this project. |
| 13 | *(new)* The backend binds loopback by default; LAN is an explicit opt-in | **Confirmed** | `main.py` resolves the bind host from `app.remote_access.resolve_host(cfg.network.access_mode)`; `[network] access_mode` is limited to `local` or `lan`. |
| 14 | *(new)* The desktop never elevates and the installer is per-user | **Constraint** | [`app.manifest`](../../desktop/src/MathChecker.Desktop/app.manifest) requests `asInvoker`; [`MathCheckerDesktop.iss`](../../desktop/installer/MathCheckerDesktop.iss) sets `PrivilegesRequired=lowest`. |
| 15 | *(new)* A free Cloudflare quick tunnel already ships for public access | **Relevant** | [`public_access.py`](../../backend/app/public_access.py) starts and verifies `cloudflared tunnel --url http://127.0.0.1:<port>`. It is a possible opportunistic relay, with real limits (§5). |

### What can be reused

- `cryptography` for all primitives; no new crypto dependency.
- [`secure_store.py`](../../backend/app/distributed/secure_store.py) for keys at
  rest, with the current-user caveat respected by running the peer process as
  the signed-in user.
- The `agent_control` ON/OFF switch pattern, so a teacher can turn peer
  networking off without deleting anything.
- `accounts`, `account_scope` and `teams` as the *authorisation* model — the
  peer layer authenticates a device, and the application layer still asks
  whether an account may see a class.
- `sync_events` with its `client_event_id` dedupe and monotonic `seq` for
  replication, rather than inventing a second synchronisation model.
- The presence vocabulary and the rule already encoded in `effective_status()`:
  a stored state means nothing without a fresh heartbeat.

### What must be modified

- Device identity: add a keypair and make the identifier derive from the public
  key, so it cannot be copied between machines.
- Revocation: make it verifiable by peers, not only by the hub.
- The desktop shell: supervise a third process, and show its real state.

### What should remain untouched

- The correction engine, marking pipeline, scheme validation and report
  generation. Peer networking is additive and must be provably inert with
  respect to them.
- `auth_gate.py` and the public-access gate. The peer service gets its own
  authentication; it does not add an exemption to the existing one.
- The existing hub routes, until the migration described in §13 makes the peer
  path primary.

---

## 4. What a $0 budget genuinely cannot buy

This section exists because the alternative — quietly promising automatic
remote connectivity — would mislead the person who has to rely on it.

**4.1 Two symmetric-NAT peers cannot connect directly.** Classic UDP hole
punching assumes each NAT reuses one external mapping for different
destinations. RFC 4787 says a UDP NAT *must* use endpoint-independent mapping,
but RFC 5128's measurements of deployed devices found hole punching worked on
roughly 82% of NAT devices, hairpinning on under 25%, and TCP-based techniques
on roughly 64%. When either peer's mapping is address-and-port dependent, the
advertised address a peer publishes is not the address that will work, and port
prediction is brittle. *(sourced: RFC 4787, RFC 5128, RFC 8445; check date
2026-10-29.)*

No free software removes that constraint. Only a relay does. This is why §7
treats a relay as a first-class, optional component rather than an
embarrassment.

**4.2 Automatic discovery with no third party is not solvable.** If neither
peer knows the other's address and both sit behind firewalls that drop
unsolicited inbound traffic, there is no information channel between them. Every
working system solves this with one of: a rendezvous service both can reach, an
always-on peer both can reach, or an out-of-band exchange of an address that is
still valid. "No coordinator at all" therefore means "coincidentally reachable
or nothing".

**4.3 There is no unlimited, SLA-backed, permanently free TURN relay.** The
honest inventory is in §5. Any design that assumes one is relying on a free tier
that can change.

**4.4 IPv6 removes translation, not firewalls.** Roughly 44–50% of measured
clients were IPv6-capable in late 2026 depending on the metric; IPv6 still has
stateful firewalls and host firewalls. It is a valuable *candidate path*, not a
guarantee. *(sourced: APNIC and Google IPv6 statistics; check date 2026-10-29.)*

**4.5 Therefore the contract this design can honestly offer** is:

> On the same reachable network, peers will find and authenticate each other
> automatically. Across networks, peers will connect directly when the network
> allows it, and will say clearly when it does not.

---

## 5. Technology research and free-service comparison

Cost and licence facts below are from primary sources, with the check date
recorded. Free-tier terms change; re-verify before depending on any of them.

### 5.1 Discovery transports

| Technology | Cost | Licence | Windows reality | Verdict |
|---|---|---|---|---|
| mDNS / DNS-SD, UDP 5353 multicast | $0 | IETF RFCs | Windows ships `mDNS (UDP-In)` **enabled** on Private, Public (both `LocalSubnet`) and Domain (`Any`) — measured locally | **Use** |
| `python-zeroconf` | $0 | **LGPL-2.1-or-later**, pure Python | Actively maintained, has Windows-specific socket handling | **Reject**: LGPL in a bundled release, and it solves hostname resolution we do not need |
| Win32 `DnsServiceRegister` / `DnsServiceBrowse` via `ctypes` | $0 | — | Native since Windows 10; `InterfaceIndex = 0` means all interfaces; `unicastEnabled = false` selects mDNS | **Defer**: optional mDNS interop later, not worth the structure marshalling now |
| Custom signed UDP multicast beacon | $0 | — | Same port and firewall rule as mDNS; payload is ours | **Use as primary** |
| Unicast subnet sweep with a signed hello | $0 | — | Only local-link traffic; outbound-initiated | **Use as fallback** when multicast is filtered |
| UDP broadcast to 255.255.255.255 | $0 | — | Often filtered; no interface scoping | Avoid |

**Recommended:** a signed application beacon carried in UDP datagrams sent to the
mDNS multicast group on port 5353, distinguished from real mDNS by a payload
prefix. Rationale: it inherits a firewall rule Windows already enables on every
profile, it lets the announcement carry a signature that mDNS TXT records cannot,
and it needs no dependency. The cost is that the beacon shares a port with
Windows' own responder, which §6.3 handles.

### 5.2 NAT traversal and transport

| Technology | Cost | Licence | Fit | Verdict |
|---|---|---|---|---|
| Custom UDP with firewall pinholes | $0 | — | No NAT on a LAN; Windows' stateful filter is the only obstacle | **Use** for LAN data |
| TCP + TLS 1.3 listener | $0 | — | Best throughput; needs an inbound rule the app cannot create silently | **Use** when a rule exists; optional upgrade |
| STUN (Google `stun.l.google.com:19302`, Cloudflare `stun.cloudflare.com`) | $0 | — | Discovers the reflexive address only | **Use**, optional |
| TURN — Cloudflare Realtime | 1 TB/month free, then $0.05/GB; requires an account | — | Real capacity, but a paid boundary exists | **Optional adapter**, off by default |
| TURN — Metered Open Relay | 20 GB/month free | — | Public shared auth secret, no SLA | **Avoid by default** |
| ICE via `aioice` | $0 | BSD | Light (asyncio + `dnspython` + `ifaddr`); provides connectivity checks, not a data channel | **Defer** to the last phase |
| WebRTC DataChannels via `aiortc` | $0 | BSD-3 | Pulls in PyAV/FFmpeg, `pylibsrtp`, `pyopenssl` | **Reject**: heavy for 2–6 peers |
| WireGuard | $0 | GPL-2.0 kernel module / driver | Needs a driver and administrative rights | **Reject**: solves the problem we removed by not building a VPN |
| Port mapping: UPnP-IGD (`miniupnpc`, BSD, Windows wheels), NAT-PMP (`py-natpmp`, incomplete) | $0 | BSD / MIT | UPnP was enabled on 76% of routers in one 2019 non-representative scan; availability ≠ usable | **Optional upgrade** |
| PCP | $0 | — | `pypcpc` requires Python 2.7 | **Reject** |

### 5.3 Free overlay networks (all rejected as dependencies)

| Service | Free tier | Personal-use restriction | Licence / bundling | Windows requirements | Verdict |
|---|---|---|---|---|---|
| Tailscale | 6 users, unlimited user devices (Pricing v4, 2026-04-08) | **Personal is non-commercial only**; education route is a 50% discount | BSD client source, closed-source GUI, proprietary control plane | Service + virtual adapter, admin install | Rejected as dependency; optional user-installed overlay |
| ZeroTier | 10 users, 10 devices, 1 network for new signups (New Central, 2025-11-05) | **Personal is non-commercial**; the controller licence's definition of commercial use includes a non-profit organisation | MPL-2.0 agent; `nonfree/` controller | Service running as admin, virtual adapter | **Rejected**: device cap and licence both fit badly |
| NetBird Cloud | 5 users, 100 machines | Not stated as personal-only; terms are drafted for business customers | BSD-3 client, AGPLv3 server | Service + Wintun driver, elevation | Optional only; unclear school terms |
| Headscale (self-hosted) | Software free | None in the BSD-3 licence | BSD-3 | Requires a publicly reachable Linux/BSD host; Windows client is Tailscale's | **Not $0 in practice** |

*(sourced: vendor pricing, terms and licence pages as listed in §17; check date
2026-06-18, with the caveat that some pages carried revision dates after that
check.)*

**Consequence for R4 and R8:** a school department does not obviously qualify as
"personal" use for Tailscale or ZeroTier, and both need administrative
installation. Neither can be a dependency of a feature the app claims to have.

### 5.4 Rendezvous options

| Option | Cost | Trust | Latency | Verdict |
|---|---|---|---|---|
| LAN gossip of a signed roster | $0 | Peers only | Seconds | **Primary** |
| A reachable peer (public IPv6, mapped port, or the existing quick tunnel) | $0 | Peers only | Seconds | **Primary when available** |
| Signed address mailbox in the department's Google Drive | $0 (existing account) | Google sees metadata, never plaintext | Seconds to a minute | **Recommended WAN rendezvous** |
| Public mainline DHT (BEP 44 `put`/`get`) | $0, no account | Untrusted public infrastructure | Seconds to minutes | **Optional, experimental** |
| Cloudflare quick tunnel as a published endpoint | $0 | Cloudflare terminates TLS at the edge | Poor | **Qualified**: dev-grade |

**Why a Google Drive mailbox is the recommended remote rendezvous.** The
department already has a Google account connected to this app, already grants it
Classroom scopes, and already trusts Google with student work. A small file
containing *signed, opaque* candidate records — protocol version, device
identifier hash, addresses, expiry — tells each peer where the others can be
reached, using infrastructure that costs nothing extra and needs no new account.
It is store-and-forward, so it cannot carry a live session; it only bootstraps
one. Two caveats are recorded in §15 and §16: it likely adds a Drive scope and
its consent implications, and Google can see the metadata even though every
record is signed and every session is separately encrypted.

### 5.5 Cloudflare quick tunnels, including the shipped Public Address feature

Cloudflare describes Quick Tunnels as intended for testing and development:
the hostname changes each time, there is no uptime guarantee, up to 200
in-flight requests are allowed before HTTP 429, and **Server-Sent Events are not
supported**. The current Self-Serve Subscription Agreement (updated 2025-09-12)
no longer contains the old §2.8 non-HTML clause, but it does prohibit using the
services "to provide a VPN or similar proxy service" (§2.2.1(j)) and prohibits
circumventing service limits. *(sourced: Cloudflare Tunnel and terms pages;
check date 2026-10-30.)*

Two consequences:

1. **For this project:** a quick tunnel may be used as an *opportunistic,
   clearly-labelled* way to make one laptop reachable, never as a general relay
   for other people's traffic, and never as the only remote path.
2. **An incidental finding about the feature that already ships:** the app
   streams job progress with Server-Sent Events, which quick tunnels do not
   support, and
   [`public_access.py`](../../backend/app/public_access.py) verifies `/api/health`,
   the UI, the auth status and a refused unauthenticated API call — but no SSE
   route. **Assumption to verify:** some progress displays may already be broken
   through a Public Address. This is worth a separate, small investigation; it is
   not part of the peer-network work.

---

## 6. Measured constraints on Windows

These were measured on the development machine on **2026-10-31**. They are the
load-bearing facts of the LAN design, which is why they were measured rather
than assumed.

**6.1 The application cannot create a firewall rule silently.** The desktop
manifest requests `asInvoker` and the installer is `PrivilegesRequired=lowest`,
so nothing runs elevated by default. A custom inbound TCP port therefore has no
rule, and an inbound connection from a peer is blocked until someone with
administrative rights approves it. Microsoft's documentation adds a detail that
matters: if a *non-administrator* is shown the firewall prompt and clicks Allow,
the rule is **not** created.

*Measured 2026-10-31:* a non-elevated process running the exact command the
design would need — `netsh advfirewall firewall add rule ... dir=in
protocol=UDP localport=<port>` — was refused with "The requested operation
requires elevation (Run as administrator)." and the rule inventory was unchanged
afterwards. This is now evidence rather than documentation: the application
cannot open a port for itself.

**6.2 Windows already permits UDP 5353 inbound to any program.**
`netsh advfirewall firewall show rule name="mDNS (UDP-In)"` reports the rule
**Enabled** on all three profiles: Public and Private scoped to `LocalSubnet`,
Domain scoped to `Any`, UDP, local port 5353, action Allow. This is the only
inbound path a non-elevated application gets for free, on a machine whose
firewall policy is `BlockInbound,AllowOutbound` on every profile — which is also
what was measured here.

**6.3 Port 5353 is shared, and unicast delivery to a shared socket is not
reliable.** Three processes already held `0.0.0.0:5353`. Binding a fourth
succeeds **only** with `SO_REUSEADDR=1`; without it the bind fails with
`WSAEADDRINUSE`.

*Measured 2026-10-31*, two sockets co-bound to 5353 and joined to
`224.0.0.251`, five rounds per case. Each round drains both sockets and then
waits for a marker unique to that round, so ambient mDNS traffic can neither
create nor hide a delivery:

| Datagram sent to | Sockets reached, per round |
|---|---|
| multicast `224.0.0.251:5353` | **2, 2, 2, 2, 2** |
| unicast, this machine's LAN address | **0, 0, 0, 0, 0** |
| unicast, `127.0.0.1` | **0, 0, 0, 0, 0** |

So multicast reaches every co-bound listener, while a unicast datagram addressed
to 5353 reached neither of ours — Windows delivered it elsewhere, presumably to
its own DNS-SD responder, which already holds the port. **Recommendation
confirmed:** use 5353 for multicast discovery only; carry peer data on per-peer
UDP ports.

*Methodology note.* A first version of the probe accepted any arriving datagram
and reported unicast delivery inconsistently (0 of 2 sockets in one run, 2 of 2
in the next). Matching the probe's own marker after draining the queue made the
result stable across every round. The correction is recorded because a
single-sample measurement on a port that carries other software's traffic is not
worth trusting, and the first answer was wrong.

**6.4 A firewall pinhole is a hypothesis, not yet a fact.** The design assumes
that when peer A sends a UDP datagram to peer B's port, and B simultaneously
sends to A's port, Windows' stateful filter permits the return traffic in both
directions without any rule. Windows Filtering Platform does maintain UDP flow
state, so this is expected to work, but it has **not** been measured: it needs
two hosts, and this investigation had one.

A control run explains why the single-machine result cannot be substituted for
it: UDP sent from this machine to *its own* LAN address was delivered with no
firewall rule at all, because self-addressed traffic never traverses the inbound
filter. A green one-host test would therefore prove nothing.

**Status: open.** `backend/scripts/peer_phase0_probe.py pair` performs the real
measurement on two laptops; until it is run, no data path may assume the pinhole
opens. See §6.5.

**6.5 Phase 0 results, as measured on 2026-10-31.** The probe is
[`backend/scripts/peer_phase0_probe.py`](../../backend/scripts/peer_phase0_probe.py);
its machine-readable output is
[`docs/results/PEER_PHASE0_RESULTS.json`](../results/PEER_PHASE0_RESULTS.json).

| Experiment | Status | Result |
|---|---|---|
| **P0-3** co-bind UDP 5353, multicast vs unicast | **Measured, 5 rounds** | Multicast reached both sockets in 5 of 5 rounds; unicast reached neither socket in any round, on both the LAN address and loopback. 5353 is multicast-only. |
| **P0-2a** can a non-elevated process create the inbound rule? | **Measured** | No. Refused with "requires elevation"; rule inventory unchanged. |
| **P0-2b** does an inbound TCP connection to a non-elevated listener arrive? | **Pending** | Needs two hosts. |
| **P0-1** does simultaneous UDP create a two-way pinhole? | **Pending** | Needs two hosts. |
| **Control** is a single-host test valid? | **Measured** | No. Self-addressed UDP succeeds with no rule, and a self-to-self run of the harness delivered 40 of 40 datagrams and accepted its own TCP connection — with no rule in place. One host cannot prove a pinhole. |

Two of the four questions are answered and the transport choice in §6.3 is now
evidence-based. The two that remain are the ones that decide whether LAN data can
run without an elevated rule, and they are the reason Phase 4 stays unstarted.

To finish them, run this on each of two laptops on the same Wi-Fi, within a few
seconds of each other, then copy both JSON outputs into
`docs/results/PEER_PHASE0_PAIR_<A|B>.json`:

```bat
python backend\scripts\peer_phase0_probe.py pair --name A --peer <B-ipv4>
python backend\scripts\peer_phase0_probe.py pair --name B --peer <A-ipv4>
```

A `p0_1_udp_return_traffic` of `arrived` confirms the pinhole and keeps §7.2 as
written. `none` on both sides means LAN data needs the elevated rule or a relay,
and Phase 4 must be revised before it is built.

---

## 7. Architecture alternatives

### 7.1 Option A — pure peer-to-peer, no coordinator

```text
  ┌──────────────┐        multicast beacon        ┌──────────────┐
  │   Peer A     │◄──────────────────────────────►│   Peer B     │
  │ Ed25519 keys │        TLS session             │ Ed25519 keys │
  └──────────────┘                                └──────────────┘
        │                                                │
        └────────── no rendezvous, no relay ─────────────┘
```

- **Discovery:** multicast beacon, then unicast sweep.
- **Connection:** direct only.
- **Security:** department CA, mutual TLS.
- **Dependencies:** Python standard library plus existing `cryptography`.
- **Cost:** $0.
- **Advantages:** smallest possible system; no third party can break it; no
  privacy exposure beyond the LAN itself.
- **Disadvantages:** remote works only when a peer is independently reachable;
  no relay at all, so restrictive networks are simply dead.
- **Failure modes:** filtered multicast ⇒ nothing; two remote peers ⇒ nothing.
- **Maintenance:** lowest of the three.
- **Suitability for 2–6 laptops:** excellent for a shared staff-room Wi-Fi,
  insufficient for teachers working from home.

### 7.2 Option B — hybrid, LAN-first with pluggable rendezvous (recommended)

```text
───────────────────────── LAN plane (always on, zero infrastructure) ─────────────────────────
  ┌──────────────┐   1. signed beacon, UDP 5353 multicast    ┌──────────────┐
  │   Peer A     │◄─────────────────────────────────────────►│   Peer B     │
  │ Ed25519 keys │   2. unicast sweep fallback               │ Ed25519 keys │
  │ department CA│   3. TLS 1.3 mutual auth over UDP/TCP     │ department CA│
  └──────┬───────┘                                           └──────┬───────┘
         │                                                          │
─────────┼──────────────── WAN plane (best effort, in order) ───────┼───────────────────────
         │   (a) LAN gossip of signed roster + candidates           │
         │   (b) a reachable peer anchor                            │
         │   (c) Google Drive mailbox: signed opaque candidates     │
         │   (d) optional public DHT (experimental)                 │
         │   (e) otherwise REMOTE_UNAVAILABLE — stated, not hidden  │
         ▼                                                          ▼
   candidate probing ──► direct TLS ──► else peer relay / tunnel (ciphertext only)
```

- **Discovery:** as Option A on the LAN; on the WAN, a roster plus an ordered
  list of mailbox adapters.
- **Connection:** candidate probing, preferring LAN > direct IPv6 > punched
  IPv4 > peer relay > opportunistic tunnel.
- **Security:** one model everywhere — department CA, device certificates,
  mutual TLS, signed roster and revocation epochs.
- **Dependencies:** none for phases 1–7; `miniupnpc` optional in phase 7;
  `aioice` optional and last in phase 12.
- **Cost:** $0 for everything required.
- **Advantages:** LAN always works; remote works when the network allows; every
  external service is replaceable; failure is legible.
- **Disadvantages:** more components than A; the rendezvous adapters are the
  most likely part to rot; the mailbox adds a Google dependency for the WAN
  case only.
- **Failure modes:** each adapter can fail independently without affecting the
  others, and the reason is surfaced per adapter.
- **Maintenance:** moderate — the adapter interface is the only thing that must
  stay stable.
- **Suitability for 2–6 laptops:** the best fit; the cost is proportional to the
  number of peers because gossip is O(n).

### 7.3 Option C — free third-party overlay

```text
  Peer A ──┐                        ┌── Peer B
           ├── Tailscale / ZeroTier ┤
  Peer C ──┘      free tier         └── Peer D
```

- **Discovery / connection / NAT traversal:** entirely the vendor's.
- **Security:** vendor's, layered under the app's own TLS if the peer protocol
  keeps one.
- **Cost:** $0 within the free tier, at the vendor's discretion and terms.
- **Advantages:** least code; NAT traversal is somebody else's solved problem.
- **Disadvantages:** personal-use-only terms for the two best options; device
  caps; requires administrative installation and a driver; the core feature can
  be withdrawn by a third party; and the department may not qualify as
  "personal" use at all.
- **Failure modes:** silent, total, and outside the app's control.
- **Suitability:** acceptable as a *user-installed accelerator* — detect a
  tailnet or ZeroTier interface and use it if it is already there — never as a
  requirement.

### 7.4 Selection

**Option B.** It is the only option where the LAN path needs no configuration,
no administrative rights and no internet; where remote connectivity exists when
the network permits it and is reported honestly when it does not; and where no
third party can remove a capability the application claims. Option A is retained
as the guaranteed floor beneath it, and Option C survives as an optional adapter
that costs no code until someone already has an overlay installed.

---

## 8. Discovery and connection flows

### 8.1 LAN discovery and session

```text
Peer A                                    Peer B
  │  beacon(dept, hash(device_id), v, port, ts, nonce, sig_A)  │
  ├─────────────────────── UDP 5353 multicast ────────────────►│
  │◄─────────────────────── beacon(sig_B) ─────────────────────┤
  │                                                            │
  │  1. verify signature against the department roster          │
  │  2. reject: wrong dept, revoked, expired, replayed, unknown │
  │  3. rate-limit per source                                   │
  ▼                                                            ▼
  send UDP probe to B's ephemeral port        send UDP probe to A's port
  │◄──────────────── stateful firewall pinhole ────────────────►│
  │                                                            │
  │  TLS 1.3 ClientHello (mutual auth, department CA)          │
  ├───────────────────────────────────────────────────────────►│
  │◄──────────────── ServerHello + certificate + verify ───────┤
  │  both sides: chain to dept root, epoch not rolled back,    │
  │             device not revoked, protocol version acceptable │
  ▼                                                            ▼
  CONNECTED — presence, messaging, service calls
```

Notes: the beacon carries no display name, no account identifier and no class
metadata. The `device_id` in the beacon is a hash, so a passive observer on the
Wi-Fi learns that *a* department device is present, not which teacher it belongs
to.

### 8.2 Remote path selection

```text
start
  │
  ├─ LAN beacon received? ──────────────► LAN direct (preferred)
  │
  ├─ roster has a reachable anchor? ────► direct TLS to anchor
  │
  ├─ mailbox adapter has fresh candidates?
  │     ├─ candidate probe succeeds ────► direct TLS
  │     └─ all candidates fail ─────────► try relay
  │
  ├─ a connected peer agrees to relay? ─► relayed TLS (ciphertext only)
  │
  └─ nothing ───────────────────────────► REMOTE_UNAVAILABLE
                                          with the reason for each adapter
```

Each step records why it was skipped, so the interface can say *"two peers are
visible on your network; three remote peers are unreachable because this
network blocks direct connections"* rather than showing an empty list.

### 8.3 Connection lifecycle

```text
UNKNOWN ──beacon──► DISCOVERED ──probe──► AUTHENTICATING ──ok──► CONNECTED
                        │                       │                  │
                        │ stale                 │ auth failed      │ idle/error
                        ▼                       ▼                  ▼
                   DISCONNECTED            DISCONNECTED        DEGRADED
                        │                                          │
                        └────────── backoff + retry ───────────────┘
```

---

## 9. Security architecture

### 9.1 What is reused, what is new, and what is rejected

| Concern | Reuse | New | Rejected |
|---|---|---|---|
| Device identity | `cryptography` Ed25519 primitives from [`signing.py`](../../backend/app/distributed/signing.py) | Content-addressed `device_id`, keypair generation, certificate issuance | Random-UUID identity (copyable) |
| Department trust | — | Ed25519 department root, device certificates, signed roster | Trusting the hub's database as the only authority |
| Session | `cryptography` primitives | TLS 1.3 mutual authentication with a department CA | Hand-rolled X25519/HKDF/ChaCha20 handshake |
| Keys at rest | [`secure_store.py`](../../backend/app/distributed/secure_store.py) (DPAPI) | A peer-specific store path | A machine-wide service reading a user's DPAPI blob |
| Authorisation | `accounts.scoped_ids()`, `teams`, `account_scope` | Per-message authorisation checks | A second permission system |
| Replication | `sync_events`, `client_event_id` dedupe | Peer transport for the same events | A CRDT for grades |
| Revocation | Server-side `revoked_at` | Signed revocation list with a monotonic epoch | CRL/OCSP infrastructure |
| Presence | Existing vocabulary and `effective_status()` rule | Peer-session-derived freshness | Trusting `/heartbeat` |

### 9.2 Assessment of the proposed cryptographic table

The brief proposed: Ed25519 identity, department-signed certificates, X25519
key exchange, HKDF-SHA-256, ChaCha20-Poly1305, DPAPI at rest, sequence numbers,
signed revocation with epochs. **The primitives are the right ones.** Two
corrections are recommended.

**Correction 1 — do not hand-roll the handshake.** That list describes, almost
exactly, what TLS 1.3 already does: X25519 ECDHE key exchange, HKDF-SHA-256 key
schedule, AEAD record protection (ChaCha20-Poly1305 is a standard TLS 1.3
cipher suite), transcript authentication, downgrade protection and rekeying.
Writing that state machine by hand is the single highest-risk component in a
project of this size, and the failure mode is silent and total. **Recommended:**
TLS 1.3 mutual authentication, with the department root as a private CA. If a
UDP-native session is later required, Noise_IK is the documented alternative,
validated against the published test vectors rather than against itself.

**Correction 2 — separate the identity key from the TLS key.** Use Ed25519 for
identity, because it is compact and already available, and bind the TLS
certificate's key to that identity with a signature. A device certificate then
asserts:

```text
device_pubkey (Ed25519)  — the identity
tls_pubkey               — the key presented in the handshake
device_id = base32(SHA-256(device_pubkey))[:26]
dept_id, roles, not_before, not_after
signature by the department root
```

Because `device_id` is derived from the public key, a device cannot claim
another's identifier; two certificates claiming one identifier with different
keys is detectable and is treated as an attack, not a merge case. This closes
finding 7 in §3.

### 9.3 Threat model

| Threat | Control |
|---|---|
| Malicious LAN announcement | Beacon must carry a valid signature over `(dept, device_id, version, port, timestamp, nonce)`; verified before any state change; per-source rate limit; unknown signer ignored |
| Replay of an old announcement | Timestamp window plus a nonce cache; a replayed beacon cannot advance state |
| Device impersonation | Proof of possession of the private key is required by mutual TLS; the certificate binds identity to the presented key |
| Rogue device claiming an existing identifier | `device_id` is content-addressed; a mismatch between identifier and key is rejected and reported |
| Certificate from another department | Chain validation against the pinned department root; `dept_id` inside the certificate |
| Expired certificate | Validity window checked at handshake; expiry is a distinct, reported reason |
| Revoked device | Signed revocation list with a monotonic epoch, exchanged at handshake; a peer that knows a higher epoch refuses a peer presenting a lower one, so revocation cannot be rolled back by replay |
| Downgrade to a weaker protocol | Minimum protocol version pinned in the certificate and enforced; TLS 1.3 only |
| Relay tampering | The relay carries ciphertext in an end-to-end TLS session between peers; it cannot read or forge content, only drop or delay it, which is reported as a failure |
| Metadata leakage in discovery | Beacons and mailbox records carry only department, hashed device identifier, version, addresses and timestamps — never teacher names, Google account identifiers, class names or assignment metadata |
| Flooding / resource exhaustion | Per-source token buckets; bounded candidate lists; connection caps; discovery rate limits |
| Session replay across reconnects | TLS 1.3 record nonces plus an application-level request identifier and timestamp window for idempotent operations |
| Duplicate sessions | Newest session for a `(device_id, session_id)` wins; the older one is closed |

### 9.4 Revocation, honestly

Revocation is the weakest guarantee in any decentralised system, and the
document should not pretend otherwise.

- A revocation record is signed by the department root (or a delegated revoker),
  carries a monotonic epoch, and is gossiped during every handshake.
- Peers that have seen epoch *n* refuse to trust a peer that presents epoch
  *n−1*, which stops an attacker replaying an older list to resurrect a revoked
  device.
- **Known limitation:** a revoked device that stays offline cannot be prevented
  from reaching a peer that has never learned the revocation. Short certificate
  lifetimes (90 days recommended, 365 maximum) bound the window; the practical
  answer is that revocation takes effect as soon as the department's devices
  next meet, which for a shared staff room is the same day.
- Revocation is recorded in the existing audit trail with the actor and time, so
  it is reviewable rather than silent.

### 9.5 Key custody

The department root private key is the most valuable secret in the system: it
can mint a device. **Recommended:** it is created on the owner's machine, kept
in the DPAPI-protected store, never copied to a peer by the application, and
backed up only by explicit operator action with a written warning. Device
certificate issuance happens on the owner's machine during the invitation step.

### 9.6 DPAPI scope

`secure_store.py` protects data for the **current user**. That is correct for a
peer process started in the teacher's session, and wrong for a `SYSTEM` service,
which would silently fall back to the `MC1P` plaintext path. **Requirement:** the
peer service runs as the signed-in user, like `tiny_agent`, and its startup
checks that a written blob can be read back rather than assuming encryption
worked.

### 9.7 What signatures do not do

Stated explicitly because it is the most common error in designs of this kind:
a signature proves who wrote a record. It does not hide its contents. Everything
that leaves a machine — beacons, mailbox records, relay traffic — is either
inside a TLS session or is deliberately non-sensitive metadata. The rendezvous
mechanism and any relay must be assumed to read, retain and correlate the
metadata they carry.

---

## 10. Failure-handling design

### 10.1 Distinguishing six different "offline"s

| Situation | How it is known | What is shown |
|---|---|---|
| Device offline | No beacon or session, last-seen is stale | `OFFLINE`, with last seen |
| Application closed | Clean `BYE` received before the peer disappeared | `APP_CLOSED`, distinct from a crash |
| Internet disconnected | No default route / probe failure, but beacons continue | `NO_INTERNET`; LAN features keep working |
| Peer service unavailable | Process not running or not answering locally | `SERVICE_STOPPED`, with a restart action |
| Peer busy | Authenticated session reports `BUSY` or `PAUSED` | `BUSY` — a state, not an error |
| Connection failed | Probe or handshake returned a reason | `CONNECTION_FAILED` with the reason |

### 10.2 Reason codes

`NO_NETWORK`, `NO_LAN`, `NO_ROUTE`, `NAT_INCOMPATIBLE`, `RELAY_UNAVAILABLE`,
`AUTH_FAILED`, `REVOKED`, `CERT_EXPIRED`, `VERSION_MISMATCH`, `SERVICE_STOPPED`,
`APP_CLOSED`, `MAILBOX_UNAVAILABLE`, `TIMEOUT`.

Every one is surfaced in the interface in a sentence a teacher can act on, and
none is only a log line — the same standard the existing backend already applies
to lifecycle and sync failures.

### 10.3 Backoff and reconnection

- LAN beacon interval 10–20 s with jitter.
- Reconnect backoff: exponential from 2 s to 60 s on the LAN, to 5 minutes on the
  WAN, with full jitter so six laptops waking at 8:30 do not synchronise.
- Per-peer token bucket and a global cap on concurrent handshakes.
- On network change (interface up/down, address change), discovery restarts once
  after a short randomised delay rather than immediately.
- Multiple simultaneous connections resolve to the newest session; the older one
  is closed with a reason.

### 10.4 Isolation from marking

This is a hard requirement, not a preference:

- The peer service is a separate process with its own port and its own log.
- No correction, marking, scheme-validation or report path may import it.
- Every peer feature is additive and read-only by default.
- **Acceptance test:** a correction run with the peer service killed mid-run must
  produce a result identical to the same run without the peer service, compared
  by content hash.

### 10.5 Graceful degradation

| Lost | Still works |
|---|---|
| Internet | LAN discovery, LAN sessions, local correction |
| LAN multicast | Unicast sweep, existing sessions, local correction |
| Mailbox adapter | LAN and anchor paths |
| All remote paths | LAN, with `REMOTE_UNAVAILABLE` stated once, not repeatedly |
| Peer service | The entire application, unchanged |

---

## 11. Cost analysis

| Item | Cost | Notes |
|---|---|---|
| Software | $0 | Python standard library plus the already-declared `cryptography` |
| LAN discovery and data | $0 | UDP multicast and per-peer UDP on the school network |
| Remote rendezvous (Drive mailbox) | $0 | Uses the department's existing Google account |
| Peer relay | $0 | An already-connected laptop forwards ciphertext |
| Optional elevated firewall rule | $0 | One-time UAC approval where teachers are administrators |
| Optional Cloudflare TURN | 1 TB/month free, then $0.05/GB | Off by default; requires an account |
| Optional public DHT | $0 | No account; untrusted; experimental |
| Teacher bandwidth | Existing internet | Control traffic only; no student files without explicit action |

**Total required recurring cost: $0.** The only priced paths are optional,
disabled by default, and removable without losing LAN functionality.

---

## 12. Dependencies

**Phases 1–6 need no new runtime dependency.** Everything is `socket`, `ssl`,
`asyncio`, `hashlib`, `hmac`, `secrets`, `struct` and the already-present
`cryptography`.

| Phase | Possible addition | Licence | Justification |
|---|---|---|---|
| 7 | `miniupnpc` | BSD-3 | UPnP port mapping; has Windows wheels; only if the reachability upgrade is wanted |
| 7 | `py-natpmp` | MIT | NAT-PMP; small and pure Python, but its own docs call it incomplete |
| 12 | `aioice` | BSD | ICE connectivity checks; adds `dnspython` and `ifaddr` |

**Explicitly rejected dependencies:** `python-zeroconf` (LGPL-2.1+),
`aiortc` (PyAV/FFmpeg weight), `py-libp2p` (immaturity), any WireGuard binding
(driver and administrative rights).

Any addition must pass `MathChecker_Distribution/scripts/release_allowlist.py`
and `scripts/size_guard.py`, and be recorded in `backend/requirements.txt` and
the lock file — the release pipeline, not the developer's environment, decides
what ships.

---

## 13. Phased implementation roadmap

Order differs from the brief's suggestion in two places, deliberately:
**reconnection and honest presence move earlier** (a network that cannot report
its state is worse than one that does less), and **file transfer moves last**
because it is the only feature that truly needs the fast path.

### Phase 0 — Measured experiments (do this first)

- **Objective:** replace three assumptions with measurements before any design is
  frozen.
- **Status 2026-10-31: started, not finished.** Two of four questions are
  answered; see §6.5 for the table and the run instructions.
- **Experiments:**
  - **P0-1** Two laptops, no firewall change: each sends UDP to the other's high
    port simultaneously. Does the return traffic arrive? *(Tests §6.4.)* —
    **PENDING: needs a second laptop.**
  - **P0-2a** Can a non-elevated process create the inbound rule? — **MEASURED:
    no**, refused with "requires elevation".
  - **P0-2b** Does an inbound TCP connection to a non-elevated listener arrive?
    — **PENDING: needs a second laptop.**
  - **P0-3** Two applications co-binding UDP 5353 with `SO_REUSEADDR`: is
    multicast received by both, and is unicast delivered to the intended socket?
    — **MEASURED:** multicast 2/2, unicast 0/2.
- **Files:**
  [`backend/scripts/peer_phase0_probe.py`](../../backend/scripts/peer_phase0_probe.py)
  (stdlib only, so it runs on a bare laptop); results in
  [`docs/results/PEER_PHASE0_RESULTS.json`](../results/PEER_PHASE0_RESULTS.json).
- **Tests:** the experiments are the test.
- **Acceptance:** each of the four has a recorded pass/fail, and the plan's
  transport choice is either confirmed or revised in an addendum to this file.
  **Not yet met** — the two pending items are the ones that decide whether LAN
  data needs an elevated rule.
- **Risks:** if P0-1 fails, LAN data needs the elevated firewall rule or the
  tunnel, which changes Phase 4 and the product's minimum requirements.
- **Complexity:** XS.

### Phase 1 — Identity, department CA, roster, revocation

- **Objective:** a device can prove who it is, and a peer can verify it without
  any server.
- **Reuse:** [`signing.py`](../../backend/app/distributed/signing.py) primitives,
  [`secure_store.py`](../../backend/app/distributed/secure_store.py),
  `audit.record`.
- **Likely files:** `backend/app/peer/__init__.py`, `identity.py`, `ca.py`,
  `roster.py`, `revocation.py`, `invite.py`; tests under `backend/tests/`.
- **Dependencies:** none new.
- **Security:** content-addressed `device_id`; root key never leaves the owner's
  machine; signed roster and revocation with monotonic epochs.
- **Tests:** forged signature, wrong department, expired certificate, revoked
  device, identifier/key mismatch, epoch rollback, invitation tampering.
- **Acceptance:** every one of those cases is rejected with a distinct reason,
  and a round-trip invitation produces a working certificate.
- **Complexity:** M.

### Phase 2 — Peer service process and lifecycle

- **Objective:** a supervised process that can be started, stopped and observed,
  with no effect on the application when it is absent.
- **Reuse:** the `agent_control` ON/OFF switch pattern; the `BackendHost`
  supervision model; `lifecycle` reporting.
- **Likely files:** `backend/app/peer/service.py`, `protocol.py`;
  `desktop/src/MathChecker.Desktop/Core/Peer/PeerHost.cs`; a status surface in
  the shell.
- **Tests:** start/stop/restart; crash leaves the backend healthy; OFF writes no
  state.
- **Acceptance:** killing the peer service mid-correction leaves the result
  byte-identical; the shell shows real state, never an assumed one.
- **Complexity:** S.

### Phase 3 — LAN discovery

- **Objective:** automatic mutual discovery on a reachable network.
- **Likely files:** `backend/app/peer/discovery.py` (multicast beacon, unicast
  sweep, interface enumeration), tests with an injected transport.
- **Dependencies:** none new; enumerate interfaces with
  `socket.if_nameindex()` and the UDP-connect trick rather than adding `psutil`.
- **Security:** signed beacons, nonce and timestamp window, per-source rate
  limit, duplicate-identifier detection.
- **Tests:** two services discover within the target time; forged and replayed
  beacons ignored; multicast-filtered environment falls back to sweep; interface
  change re-binds.
- **Acceptance:** ≤5 s p95 on the same Wi-Fi; forged beacon never advances state.
- **Complexity:** M.

### Phase 4 — Session layer

- **Objective:** mutual authentication and confidentiality per §9.
- **Likely files:** `backend/app/peer/session.py` (TLS 1.3 context, certificate
  verification, epoch exchange), `framing.py`.
- **Tests:** handshake success, wrong CA, expired, revoked, downgrade attempt,
  rekey, large frame, truncated frame, replay of an application request.
- **Acceptance:** every negative case rejected with its own reason; session
  establishment ≤300 ms on the LAN.
- **Complexity:** M.

### Phase 5 — LAN services

- **Objective:** presence, messaging and announcements on top of the session.
- **Reuse:** presence vocabulary; `effective_status()` rule.
- **Tests:** presence is never `ONLINE` without a live session; a message is
  delivered once; an announcement reaches every connected peer.
- **Acceptance:** presence is truthful within one beacon interval of a change.
- **Complexity:** M.

### Phase 6 — Reconnect, backoff, diagnostics

- **Objective:** the network tells the truth about itself.
- **Tests:** Wi-Fi toggle; sleep/wake; IP change; simultaneous wake of six peers;
  duplicate sessions; every reason code reachable.
- **Acceptance:** recovery ≤10 s p95 after a network change; no reconnect storm.
- **Complexity:** S.

### Phase 7 — Reachability upgrade

- **Objective:** make a peer reachable from outside where possible.
- **Likely files:** `backend/app/peer/reachability.py` (IPv6 detection,
  UPnP/NAT-PMP, STUN, optional firewall rule helper).
- **Dependencies:** `miniupnpc` optional.
- **Security:** a mapping must be requested only for the peer port, with a short
  lease, and withdrawn on shutdown.
- **Acceptance:** capability is detected and reported; absence degrades cleanly.
- **Complexity:** M.

### Phase 8 — Remote rendezvous

- **Objective:** peers on different networks discover each other through the
  mailbox adapter.
- **Reuse:** existing Google OAuth and Drive/Classroom client boundaries.
- **Likely files:** `backend/app/peer/rendezvous/__init__.py`, `lan.py`,
  `anchor.py`, `drive_mailbox.py`.
- **Security:** records are signed and opaque; expiry enforced; no account
  identifiers; the mailbox is never trusted for identity.
- **Tests:** two peers behind different networks discover within 60 s; a tampered
  record is rejected; an expired record is ignored; adapter failure falls through
  to the next.
- **Acceptance:** discovery succeeds or reports a specific reason per adapter.
- **Complexity:** M.

### Phase 9 — Relay fallback

- **Objective:** connectivity when direct paths are impossible.
- **Mostly a decision:** an optional adapter, using a connected peer that
  volunteers, or the existing tunnel machinery, with the Cloudflare terms
  caveat in §5.5 respected.
- **Tests:** relay cannot read payload (search captured bytes for identifiers);
  relay loss reconnects or degrades; relay is chosen only after direct fails.
- **Acceptance:** no plaintext, no silent dependence, clearly labelled.
- **Complexity:** M.

### Phase 10 — Assignment metadata and progress sync

- **Objective:** the first genuinely shared feature.
- **Reuse:** `sync_events`, `accounts.scoped_ids()`, `teams`.
- **Tests:** scope enforced per message; a peer cannot read another school's
  class; conflicting edits resolve deterministically; duplicate events dedupe.
- **Acceptance:** no data crosses a scope boundary; results remain owned by the
  correcting device.
- **Complexity:** L.

### Phase 11 — Report and file transfer

- **Objective:** authorised transfer of a completed report.
- **Tests:** resumable transfer, integrity check, cancellation, size limits,
  refusal without authorisation.
- **Acceptance:** byte-identical file, transferred only with explicit permission.
- **Complexity:** L.

### Phase 12 — Optional ICE and DHT (experimental)

- **Objective:** measure whether `aioice`-based punching or a DHT rendezvous
  beats Phases 7–9 in practice.
- **Acceptance:** a measured improvement, or it is not shipped.
- **Complexity:** L.

### Migration

The peer path becomes primary while the existing HTTP-hub discovery stays
available as a fallback, behind a flag, until Phase 10 is proven. Removing the
hub is a separate, later decision with its own migration plan — §3 finding 9 is
the reason not to treat it as a detail.

---

## 14. Testing strategy

### 14.1 Physical lab (2–6 laptops)

| Scenario | Pass criterion |
|---|---|
| Two devices, same Wi-Fi | Discovered ≤5 s p95; direct LAN session; no internet traffic |
| Devices on different Wi-Fi, same subnet | Same as above, or sweep fallback used and reported |
| Different networks, both reachable | Direct session, or mailbox used and reported |
| Different providers, symmetric NAT | `NAT_INCOMPATIBLE` or relay used; never a silent hang |
| IPv4 only / IPv6 available | Correct candidate chosen; preference recorded |
| Windows Firewall on, no rule | Discovery works; data path matches the Phase 0 result |
| Application restart | Peers re-discover without duplicate sessions |
| Sleep / wake | Recovery ≤10 s p95 |
| Internet interruption | LAN unaffected; WAN reasons surfaced |
| Mailbox unavailable | Falls through to next adapter |
| Forged LAN announcement | Ignored; no state change |
| Certificate from another department | Rejected |
| Revoked device | Rejected once the epoch is known |
| Simultaneous discovery of six peers | No storm; all sessions within 30 s |
| Direct vs relay selection | LAN preferred; relay only after direct fails |
| CPU and memory | Idle ≤0.5% CPU, ≤40 MB RSS; measured over an hour |
| Marking isolation | Identical result hash with the peer service killed mid-run |

### 14.2 Automated tests

- **Unit:** signature verification, certificate chain, roster validation,
  revocation epoch rollback, replay window, beacon parsing, framing, backoff.
- **Integration:** two peer services in one process over an injected transport —
  handshake, messaging, reconnect, duplicate session, reason codes.
- **Security assertions:** a captured-traffic scan asserting that no teacher
  name, Google account identifier, student name or assignment title appears in
  beacons, mailbox records or relay bytes.
- **One slow real-multicast smoke test**, excluded from the default run because
  CI has no second host.

The existing suite convention applies: external services are mocked unless a test
says otherwise, and the peer layer must be testable with no network at all.

---

## 15. Known limitations

1. **Remote connectivity is best effort.** Two peers behind endpoint-dependent
   NAT cannot connect directly, and no free software changes that.
2. **Revocation is eventually consistent.** An offline revoked device can reach a
   peer that never learned the revocation.
3. **Discovery depends on the network.** AP/client isolation and multicast
   filtering defeat beaconing; the sweep fallback helps, and neither is
   guaranteed.
4. **The mailbox exposes metadata to Google.** Records are signed and opaque, and
   sessions are separately encrypted, but timing and size are visible.
5. **Quick tunnels are dev-grade.** No SLA, 200 in-flight requests, no SSE, and
   terms restrictions on proxy-like use.
6. **Some features need a firewall rule.** Large transfers over TCP do; LAN
   discovery does not.
7. **No offline transfer of a certificate.** A new device needs one contact with
   an authorised machine — the invitation — before it can join.
8. **The hub model still exists during migration**, so for a period there are two
   ways to reach a peer and the interface must say which one is in use.
9. **The peer layer does not make the app a backup system.** Data ownership and
   retention are unchanged.
10. **Windows-only for now.** The mechanisms chosen are portable in principle,
    but only Windows was measured.

---

## 16. Unresolved decisions requiring a human

| # | Decision | Why it matters | Recommendation |
|---|---|---|---|
| D1 | Who holds the department root key, and how is it backed up? | Whoever holds it can mint a device; losing it means re-inviting everyone | The department owner, DPAPI-protected, with one explicit offline backup |
| D2 | Do teachers actually work from home at the same time? | If not, Phases 8–9 are scope that can wait | Answer before Phase 8 |
| D3 | Is a one-time invitation file acceptable, or is a copy-paste code or QR needed? | Determines the setup experience for R3 | Copy-paste code first, file second |
| D4 | How long may a revoked device remain able to reach peers that never learned the revocation? | Sets certificate lifetime and how loudly the interface warns | 90-day certificates; warn in the interface |
| D5 | Is adding a Google Drive scope acceptable? | Triggers consent-screen and verification work | Confirm before Phase 8; LAN work is unaffected |
| D6 | Should the existing hub be removed, and when? | Changes the migration and the interface | Only after Phase 10 is proven |
| D7 | Is a peer relay acceptable as a privacy trade? | A peer sees metadata, never plaintext | Offer it, default off |
| D8 | Is the SSE/Public-Address gap from §5.5 real? | An existing feature may be partly broken | Investigate separately; out of scope here |

---

## 17. Official sources and references

Check dates are recorded per source group. Web research was performed in four
passes on different days, so re-verify free-tier terms before relying on them;
local Windows behaviour was measured on 2026-10-31 and is reported as measured,
not sourced.

**Standards (checked 2026-10-29)**

- RFC 4787 — Network Address Translation Behavioral Requirements for Unicast UDP.
- RFC 5128 — State of Peer-to-Peer Communication across Network Address
  Translators (hole-punching success figures, hairpinning, TCP results).
- RFC 5382 — NAT Behavioral Requirements for TCP.
- RFC 6544 — TCP Candidates with Interactive Connectivity Establishment.
- RFC 6888 — Common Requirements for Carrier-Grade NATs.
- RFC 7857 — Updates to NAT Behavioral Requirements.
- RFC 8445 — Interactive Connectivity Establishment.
- RFC 8489 — Session Traversal Utilities for NAT.
- RFC 8656 — Traversal Using Relays around NAT.
- RFC 6762 — Multicast DNS; RFC 6763 — DNS-Based Service Discovery.
- RFC 7558 — Requirements for Scalable DNS-SD/mDNS; RFC 9119 — Multicast
  Considerations over IEEE 802 Wireless Media.
- Guha & Francis, *Characterization and Measurement of TCP Traversal through NATs
  and Firewalls*, USENIX IMC 2005.

**Windows platform (checked 2025-06-25 for the APIs; measured 2026-10-31 here)**

- Microsoft, "mDNS in the Enterprise" (networking blog).
- Microsoft Learn: `DnsServiceRegister`, `DnsServiceBrowse`,
  `DNS_SERVICE_REGISTER_REQUEST`, Windows Firewall rules and configuration.
- Microsoft, Windows 10 release information (version 1703 / build 15063).

**Cloudflare (checked 2026-10-30)**

- Cloudflare Tunnel — Quick Tunnels documentation (development-only, 200
  in-flight requests, no SSE, ephemeral hostname).
- Cloudflare WebSockets documentation; Tunnel routing concepts.
- Cloudflare Self-Serve Subscription Agreement (updated 2025-09-12), including
  §2.2.1(j) on VPN/proxy use.
- Cloudflare blog, "Updated Terms of Service" (2023) on the removed §2.8.
- Cloudflare Realtime TURN documentation and FAQ (1 TB/month free, then
  $0.05/GB; per-allocation limits).
- Cloudflare service-specific terms for application services (CDN content
  restrictions).

**Free tier and licensing (checked 2026-06-18)**

- Tailscale: pricing, Pricing v4 announcement (2026-04-08), free plans and
  discounts, Special Terms, DERP documentation, Open Source page, repository
  LICENSE.
- ZeroTier: pricing, New Central announcement (2025-11-05), protocol and
  bandwidth FAQ, `nonfree/LICENSE.md`, Terms of Service, MPL-2.0 licence.
- NetBird: pricing, "How NetBird Works", Windows installation, repository
  LICENSE, AGPL announcement, Wintun.
- Headscale: overview, setup requirements, DERP reference, Windows guide,
  LICENSE.
- Metered Open Relay: tool page, Terms, Privacy Policy.
- Twilio Network Traversal Service: documentation, pricing, FAQ.

**Libraries (checked 2026-10-29)**

- `python-zeroconf` repository and documentation (LGPL-2.1-or-later, pure
  Python).
- `aioice` repository and changelog (BSD, 0.10.2).
- `aiortc` PyPI and repository (BSD-3, 1.15.0, dependency list).
- `miniupnpc` PyPI (BSD-3, Windows wheels); `py-natpmp` PyPI.
- `pypcpc` (Python 2.7 only — rejected).

**Measurement data (checked 2026-10-29)**

- APNIC IPv6 measurement dashboard; Google IPv6 statistics; APNIC blog
  discussion of the differing 2026 measurements.
- Trend Micro, 2019 home-router scanner report on UPnP exposure (not a
  representative sample).
