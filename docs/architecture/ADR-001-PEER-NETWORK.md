# ADR-001: An authenticated peer message bus, not a mesh VPN

- **Status:** Accepted (design only — nothing implemented)
- **Date:** 2026-10-31
- **Deciders:** project owner
- **Supersedes:** none
- **Related:** [PEER_NETWORK_RESEARCH_AND_PLAN.md](PEER_NETWORK_RESEARCH_AND_PLAN.md),
  [PEER_NETWORK_RISK_REGISTER.md](PEER_NETWORK_RISK_REGISTER.md)

## Context

Two to six teachers' laptops running MathChecker should discover each other,
authenticate, and exchange a small amount of data — presence, messages,
announcements, assignment metadata, correction progress — with no paid
infrastructure, no domain, no port forwarding, no VPN subscription and no
machine anybody has to maintain.

The brief for this work described that as "automatic peer-to-peer networking",
which naturally suggests a mesh VPN: WireGuard, Tailscale, ZeroTier, libp2p, TURN
servers and hole punching. Investigating those options produced four findings
that argue against building one.

1. **The free overlays that solve NAT traversal are not usable as a dependency.**
   Tailscale's free Personal plan is non-commercial only; ZeroTier's free plan is
   capped at ten devices for new signups and its controller licence counts
   non-profit organisational use as commercial. Both require administrative
   installation and a virtual adapter. A school department does not obviously
   qualify for either. *(sourced: vendor pricing, terms and licence pages,
   checked 2026-06-18.)*
2. **The physics is against it.** Two peers both behind endpoint-dependent NAT
   cannot be connected directly at any price without a relay. RFC 5128 measured
   hole punching working on roughly 82% of NAT devices, hairpinning below 25%,
   and TCP techniques near 64%. A design that promises automatic remote
   connectivity is promising something it cannot deliver.
3. **The platform forbids the obvious implementation.** MathChecker's desktop
   runs `asInvoker` and installs per-user, so it cannot create a firewall rule.
   The only inbound path Windows permits by default is UDP 5353 via the `mDNS
   (UDP-In)` rule, which is enabled on Private, Public and Domain on the
   development machine — measured, not assumed.
4. **The actual requirement is narrower than a network.** The application does
   not need IP connectivity between machines; it needs signed, encrypted
   request/response and pub/sub between a handful of app instances. Routing,
   addressing, TUN devices and network drivers are all incidental to that.

There is also an existing architectural conflict to respect: the current
`backend/app/distributed/` layer is deliberately hub-centric — "the shared
backend is the source of truth" — and its discovery is an HTTP directory, not
peer discovery.

## Decision

**Build an authenticated peer message bus between MathChecker instances, and do
not build a mesh VPN.**

Specifically:

1. **Scope:** a peer protocol between application instances. No TUN/TAP device,
   no WireGuard, no IP routing, no system service, no administrative install.
2. **LAN transport:** signed UDP multicast beacons on port 5353 for discovery —
   inheriting the firewall rule Windows already enables — with a rate-limited
   unicast sweep as the fallback when multicast is filtered. Peer data travels on
   per-peer UDP using firewall pinholes opened by outbound traffic, with an
   optional TCP listener when a firewall rule exists.
3. **Session security:** TLS 1.3 with mutual authentication against a department
   certificate authority, rather than a hand-rolled X25519/HKDF/ChaCha20
   handshake. Ed25519 remains the identity key; the TLS key is bound to it by
   signature; `device_id` is derived from the public key so it cannot be copied
   between machines.
4. **Rendezvous:** pluggable, tried in order — LAN gossip of a signed roster, a
   reachable peer anchor, a signed address mailbox in the department's existing
   Google Drive, an optional public DHT, and otherwise an explicit
   `REMOTE_UNAVAILABLE`.
5. **External services are adapters, never dependencies.** LAN operation must
   work with no internet, no Google account and no third party of any kind.
6. **Process isolation:** the peer service is a separate supervised process. No
   marking, correction, scheme-validation or reporting path may import it.

## Consequences

**Positive**

- No driver, no elevation, no administrative install — the feature works for a
  teacher who is not a local administrator, on the LAN.
- No third party can withdraw the core capability.
- One security model for every transport, including relays and tunnels.
- The riskiest component (a bespoke handshake) is replaced by a vetted one.
- LAN functionality is genuinely independent of the internet.

**Negative**

- Remote connectivity is best effort. This must be visible in the interface, not
  buried in logs, and some reviewers will read it as an incomplete feature.
- A Drive scope is likely needed for automatic cross-network discovery.
- The mailbox adapter and the certificate authority are new maintenance surfaces.
- The hub model must coexist during migration, so for a period there are two
  paths to a peer.
- Some capabilities (large TCP transfers) still need a firewall rule.

**Neutral**

- Roughly 44–50% of measured clients were IPv6-capable in late 2026, so IPv6 is
  a useful candidate path but not a solution.
- An installed Tailscale or ZeroTier overlay can still be detected and used as an
  optional accelerator; nothing depends on it.

## Alternatives considered

| Alternative | Why it was rejected |
|---|---|
| **Pure peer-to-peer, no coordinator** | Correct but insufficient: remote works only when a peer is independently reachable, and there is no relay at all |
| **Third-party overlay as the primary mechanism** | Personal-use terms, device caps, administrative install, and a third party able to remove the feature |
| **WireGuard mesh with a self-hosted coordinator** | Needs a host and a driver; solves a routing problem this design removes |
| **libp2p / Kademlia as the foundation** | Immature Python implementation; DHT reliability and etiquette unsuitable for a 6-peer private group |
| **WebRTC DataChannels (aiortc)** | PyAV/FFmpeg, `pylibsrtp`, `pyopenssl` — disproportionate weight for the benefit |
| **Hand-rolled X25519 + HKDF + ChaCha20 handshake** | The highest-risk component for no gain over TLS 1.3, which supplies the same primitives |
| **WireGuard via the Cloudflare quick tunnel already shipped** | Dev-grade: no SLA, 200 in-flight requests, no SSE, and terms restrictions on proxy-like use |

## Verification

This decision is only sound if its load-bearing assumption holds. Phase 0 of the
implementation plan tests it directly: whether two non-elevated Windows
applications can exchange UDP through the stateful firewall without any rule. If
that fails, LAN data requires the elevated rule or a relay, and this ADR must be
revisited in an addendum rather than quietly worked around.

**Status 2026-10-31 — partially verified.** Two of the three measurements are in
(see §6.5 of the plan): a non-elevated process cannot create the inbound
firewall rule, and UDP 5353 delivers multicast to every co-bound listener while
unicast reaches none of them. The decisive one — whether simultaneous UDP opens
a two-way pinhole — still needs a second laptop, so the LAN data path remains
designed but unstarted.

## References

- [PEER_NETWORK_RESEARCH_AND_PLAN.md](PEER_NETWORK_RESEARCH_AND_PLAN.md) — full
  research, three architectures, security design, phased roadmap, sources.
- [PEER_NETWORK_RISK_REGISTER.md](PEER_NETWORK_RISK_REGISTER.md) — ranked risks.
- [ARCHITECTURE.md](../ARCHITECTURE.md) — the current component map.
- RFC 4787, RFC 5128, RFC 8445, RFC 8656; vendor pricing and terms pages as
  listed in the research document, checked 2026-06-18 and 2026-10-30.
