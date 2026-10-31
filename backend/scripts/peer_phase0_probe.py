#!/usr/bin/env python3
"""Phase 0 experiments for the peer network — measured, never assumed.

The peer-network plan rests on three Windows behaviours that were reasoned about
but not tested. This script settles them on real hardware. It is standard
library only, because it has to run on a teacher's laptop with nothing installed.

Two modes:

``local``  — everything one machine can answer honestly:
    P0-3  two sockets co-binding UDP 5353 with SO_REUSEADDR: does multicast reach
          both of them, and does a unicast datagram reach the intended one?
    P0-2a can a NON-ELEVATED process create the inbound firewall rule the design
          would need? (run with ``--try-firewall-rule``)
    plus the interface enumeration the LAN discovery depends on, and a control
    that shows why a single-machine test cannot answer P0-1.

``pair``   — the two-machine experiment, run on ONE laptop each:
    P0-1  both peers send UDP to each other's port simultaneously. Does the
          return traffic arrive with no firewall rule and no elevation?
    P0-2b does an inbound TCP connection to a non-elevated listener arrive?

Usage
-----
    python backend/scripts/peer_phase0_probe.py local
    python backend/scripts/peer_phase0_probe.py local --try-firewall-rule
    python backend/scripts/peer_phase0_probe.py pair --name A --peer 192.168.1.31
    python backend/scripts/peer_phase0_probe.py pair --name B --peer 192.168.1.30

Start both ``pair`` runs within a few seconds of each other; they discover each
other by exchanging addresses on the command line, deliberately, so a failure in
multicast discovery cannot be confused with a failure of the pinhole itself.

Exit code 0 = the experiment ran to completion (its RESULT may still be "blocked";
read the output). Exit code 2 = the experiment could not be run here.
Results are written to ``docs/results/PEER_PHASE0_RESULTS.json`` unless ``--out``
names another path.

What this script does NOT do: it changes no application code, no configuration
and no firewall rule. ``--try-firewall-rule`` only attempts the command the
design says the app cannot run, and reports the refusal; it never retries with
elevation.
"""

from __future__ import annotations

import argparse
import ctypes
import json
import socket
import struct
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

MDNS_GROUP = "224.0.0.251"
MDNS_PORT = 5353
DEFAULT_UDP_PORT = 45871
DEFAULT_TCP_PORT = 45872

REPO = Path(__file__).resolve().parents[2]
DEFAULT_OUT = REPO / "docs" / "results" / "PEER_PHASE0_RESULTS.json"


# --------------------------------------------------------------------- helpers

def now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def is_elevated() -> bool:
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())  # type: ignore[attr-defined]
    except Exception:
        return False


def primary_ipv4() -> str | None:
    """The address this machine would use to reach the internet.

    The UDP-connect trick: no packet is sent, but the OS picks the interface it
    would use. This is what the plan proposes instead of adding ``psutil``.
    """
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
        finally:
            s.close()
    except OSError:
        return None


def interfaces() -> list[dict]:
    """Local IPv4 addresses, via ``socket.if_nameindex`` plus name resolution.

    Deliberately dependency-free: the plan claims this is enough for LAN
    discovery, so the probe proves it rather than asserting it. Windows returns
    interface NAMES from if_nameindex but not their addresses, so the addresses
    come from the host name; the primary interface is added explicitly because
    a multi-homed host does not always list it.
    """
    try:
        names = [name for _index, name in socket.if_nameindex()]
    except OSError:
        names = []
    out: list[dict] = [
        {"name": name, "loopback": False} for name in names
    ]
    out.append({"name": "(addresses)", "loopback": False,
                "addresses": _host_ipv4s()})
    return out


def _host_ipv4s() -> list[str]:
    """Local IPv4 addresses from the OS host name, without extra packages."""
    found: list[str] = []
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            addr = info[4][0]
            if addr not in found:
                found.append(addr)
    except OSError:
        pass
    primary = primary_ipv4()
    if primary and primary not in found:
        found.append(primary)
    return found


def firewall_rules_for(*names: str) -> list[dict]:
    """Read-only inventory of the named inbound rules (never modifies them)."""
    rules: list[dict] = []
    for name in names:
        try:
            proc = subprocess.run(
                ["netsh", "advfirewall", "firewall", "show", "rule",
                 f"name={name}"],
                capture_output=True, text=True, timeout=20)
        except (OSError, subprocess.SubprocessError):
            continue
        block: dict = {}
        for line in proc.stdout.splitlines():
            if ":" not in line:
                continue
            key, _, value = line.partition(":")
            key, value = key.strip(), value.strip()
            if not key:
                continue
            if key == "Rule Name":
                if block:
                    rules.append(block)
                block = {"name": value}
            elif block is not None:
                block[key] = value
        if block:
            rules.append(block)
    return rules


def try_create_rule(port: int) -> dict:
    """Attempt the rule the design says a non-elevated app cannot create.

    P0-2a. A refusal is the EXPECTED and useful result: it proves the app cannot
    silently open a port, which is why the design needs a path that works without
    one. The command is never retried with elevation.
    """
    cmd = ["netsh", "advfirewall", "firewall", "add", "rule",
           "name=MathChecker Peer Phase0 Probe",
           "dir=in", "action=allow", "protocol=UDP",
           f"localport={port}", "profile=private,domain"]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.SubprocessError) as exc:
        return {"attempted": True, "created": False, "detail": str(exc)}
    created = proc.returncode == 0
    detail = (proc.stdout or proc.stderr or "").strip().splitlines()
    return {"attempted": True, "created": created,
            "returncode": proc.returncode,
            "detail": " ".join(detail)[:400] or "(no output)"}


# ------------------------------------------------------------- P0-3: 5353 share

def _drain(sock: socket.socket, quiet: float = 0.15) -> int:
    """Discard everything already queued, returning how much was thrown away.

    Port 5353 carries constant ambient mDNS traffic from Windows itself and any
    Bonjour-style software. Without this, a receiver can be satisfied by a
    datagram it did not send, which makes the experiment report a delivery that
    never happened.
    """
    dropped = 0
    sock.settimeout(quiet)
    while True:
        try:
            sock.recvfrom(4096)
            dropped += 1
        except (socket.timeout, BlockingIOError):
            return dropped
        except OSError:
            return dropped


def _await_marker(sock: socket.socket, marker: bytes, timeout: float = 1.0) -> bool:
    """True only when a datagram carrying OUR marker arrives in the window."""
    sock.settimeout(timeout)
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            data, _addr = sock.recvfrom(4096)
        except (socket.timeout, BlockingIOError):
            return False
        except OSError:
            return False
        if data.startswith(marker):
            return True
    return False


def probe_shared_5353(rounds: int = 5) -> dict:
    """Two sockets co-bind 5353; measure multicast and unicast delivery.

    Windows lets several sockets bind one UDP port only with SO_REUSEADDR, and
    documents no delivery rule for a unicast datagram arriving on a port with
    several holders. That is the whole reason the plan restricts 5353 to
    multicast discovery, so it is measured rather than assumed.

    Every round drains both sockets first and then waits for a marker unique to
    that round, so ambient mDNS traffic can neither create nor hide a delivery.
    Each case is repeated, because a single sample cannot distinguish "always"
    from "sometimes".
    """
    result: dict = {"port": MDNS_PORT, "sockets": 2, "rounds": rounds}
    socks: list[socket.socket] = []
    try:
        for _ in range(2):
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind(("", MDNS_PORT))
            s.settimeout(1.0)
            mreq = struct.pack("4sl", socket.inet_aton(MDNS_GROUP),
                               socket.INADDR_ANY)
            s.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
            s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_LOOP, 1)
            socks.append(s)
        result["co_bind_with_reuseaddr"] = True
    except OSError as exc:
        result["co_bind_with_reuseaddr"] = False
        result["error"] = f"{type(exc).__name__}: {exc}"
        for s in socks:
            s.close()
        return result

    def run_case(label: str, target: str | None, multicast: bool) -> None:
        if not target:
            result[label] = "no-address"
            return
        per_round: list[int] = []
        drained = 0
        for round_index in range(rounds):
            marker = f"phase0-{label}-{round_index}-{time.time_ns()}".encode()
            for s in socks:
                drained += _drain(s)
            try:
                sender = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                if multicast:
                    sender.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 1)
                    sender.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_LOOP, 1)
                sender.sendto(marker, (target, MDNS_PORT))
                sender.close()
            except OSError as exc:
                result[f"{label}_error"] = f"{type(exc).__name__}: {exc}"
                return
            delivered = sum(1 for s in socks if _await_marker(s, marker))
            per_round.append(delivered)
        result[label] = {
            "target": target,
            "sockets_reached_per_round": per_round,
            "always_both": all(n == 2 for n in per_round),
            "ever_both": any(n == 2 for n in per_round),
            "never_any": all(n == 0 for n in per_round),
            "ambient_datagrams_discarded": drained,
        }

    run_case("multicast", MDNS_GROUP, multicast=True)
    run_case("unicast_lan", primary_ipv4(), multicast=False)
    run_case("unicast_loopback", "127.0.0.1", multicast=False)

    for s in socks:
        s.close()
    return result


def self_udp_control(port: int) -> dict:
    """Control: UDP to this machine's OWN address always works, rule or no rule.

    This is why the pinhole test needs two hosts. Sending to yourself does not
    traverse the inbound filter, so a green result here would prove nothing —
    the probe records it precisely so that mistake is not made later.
    """
    srv = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    srv.bind(("", port))
    srv.settimeout(1.5)
    target = primary_ipv4()
    if not target:
        srv.close()
        return {"ran": False, "reason": "no LAN address"}
    try:
        srv.sendto(b"self", (target, port))
        srv.recvfrom(64)
        received = True
    except (socket.timeout, OSError):
        received = False
    finally:
        srv.close()
    return {"ran": True, "received": received, "address": target,
            "note": "self-addressed traffic does not exercise the inbound filter"}


# ------------------------------------------------------- P0-1/P0-2: two hosts

def run_pair(args) -> dict:
    """Two-machine experiment. One laptop runs this; run it on both."""
    peer = args.peer
    result: dict = {
        "mode": "pair", "name": args.name, "peer": peer,
        "own_address": primary_ipv4(), "elevated": is_elevated(),
        "seconds": args.seconds,
    }

    udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    udp.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    udp.bind(("", args.udp_port))
    udp.setblocking(False)

    tcp_srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    tcp_srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        tcp_srv.bind(("", args.tcp_port))
        tcp_srv.listen(4)
        tcp_bound = True
    except OSError as exc:
        tcp_bound = False
        result["tcp_bind_error"] = f"{type(exc).__name__}: {exc}"
    tcp_srv.setblocking(False)
    result["tcp_listener_bound"] = tcp_bound

    # An outbound TCP attempt to the peer. Recorded separately from the inbound
    # result: "I could not connect" and "nobody connected to me" are different
    # facts, and only the second one is blocked by the firewall.
    connect_error = None
    try:
        probe = socket.create_connection((peer, args.tcp_port), timeout=3)
        probe.close()
        result["tcp_outbound_connect"] = "connected"
    except OSError as exc:
        connect_error = f"{type(exc).__name__}: {exc}"
        result["tcp_outbound_connect"] = connect_error

    deadline = time.time() + args.seconds
    sent = received = 0
    inbound_tcp = False
    while time.time() < deadline:
        payload = f"phase0:{args.name}:{received}".encode()
        try:
            udp.sendto(payload, (peer, args.udp_port))
            sent += 1
        except OSError:
            pass
        while True:
            try:
                data, _addr = udp.recvfrom(2048)
                if data.startswith(b"phase0:"):
                    received += 1
            except BlockingIOError:
                break
            except OSError:
                break
        if tcp_bound and not inbound_tcp:
            try:
                conn, _addr = tcp_srv.accept()
                conn.close()
                inbound_tcp = True
            except BlockingIOError:
                pass
            except OSError:
                pass
        time.sleep(0.1)

    udp.close()
    tcp_srv.close()

    result["udp_sent"] = sent
    result["udp_received"] = received
    # The pinhole verdict: did ANY return datagram arrive? One proves the state
    # is open; zero means the firewall or the network dropped everything.
    result["p0_1_udp_return_traffic"] = "arrived" if received else "none"
    result["p0_2_inbound_tcp"] = "accepted" if inbound_tcp else "not-observed"
    return result


# ------------------------------------------------------------------------ main

def run_local(args) -> dict:
    result: dict = {
        "mode": "local",
        "checked_at": now(),
        "host": socket.gethostname(),
        "elevated": is_elevated(),
        "primary_ipv4": primary_ipv4(),
        "interfaces": interfaces(),
        "platform": sys.platform,
        "python": sys.version.split()[0],
    }
    result["firewall_existing_rules"] = firewall_rules_for(
        "mDNS (UDP-In)", "MathChecker Peer Phase0 Probe")
    result["p0_3_shared_5353"] = probe_shared_5353()
    result["control_self_udp"] = self_udp_control(args.udp_port)
    if args.try_firewall_rule:
        result["p0_2a_create_rule"] = try_create_rule(args.udp_port)
        # Immediately after: is the rule there now? Reading is always allowed.
        result["firewall_after_attempt"] = firewall_rules_for(
            "MathChecker Peer Phase0 Probe")
    result["verdict"] = _local_verdict(result)
    return result


def _local_verdict(result: dict) -> dict:
    shared = result.get("p0_3_shared_5353", {})
    verdict: dict = {}
    if shared.get("co_bind_with_reuseaddr"):
        multicast = shared.get("multicast") or {}
        verdict["p0_3_multicast"] = (
            "multicast reached both co-bound sockets in every round"
            if multicast.get("always_both")
            else f"multicast rounds: {multicast.get('sockets_reached_per_round')}")
        for label, text in (("unicast_lan", "LAN-address unicast"),
                            ("unicast_loopback", "loopback unicast")):
            case = shared.get(label)
            if not isinstance(case, dict):
                continue
            rounds = case.get("sockets_reached_per_round")
            if case.get("never_any"):
                verdict[label] = f"{text} never reached our sockets ({rounds})"
            elif case.get("always_both"):
                verdict[label] = f"{text} reached both sockets every round ({rounds})"
            else:
                verdict[label] = f"{text} varied across rounds ({rounds})"
    else:
        verdict["p0_3"] = "could not co-bind 5353 here"
    created = result.get("p0_2a_create_rule")
    if created is not None:
        verdict["p0_2a"] = (
            "a non-elevated process DID create the rule (unexpected)"
            if created.get("created")
            else "a non-elevated process cannot create the inbound rule")
    verdict["p0_1"] = "not answerable on one machine; run `pair` on two laptops"
    return verdict


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("mode", choices=("local", "pair"))
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    parser.add_argument("--udp-port", type=int, default=DEFAULT_UDP_PORT)
    parser.add_argument("--tcp-port", type=int, default=DEFAULT_TCP_PORT)
    parser.add_argument("--try-firewall-rule", action="store_true",
                        help="local mode: attempt the rule a non-elevated app cannot create")
    parser.add_argument("--name", default=socket.gethostname(), help="pair mode: this peer's label")
    parser.add_argument("--peer", help="pair mode: the other laptop's IPv4 address")
    parser.add_argument("--seconds", type=int, default=15, help="pair mode: exchange window")
    args = parser.parse_args()

    if args.mode == "pair":
        if not args.peer:
            print("pair mode needs --peer <other laptop's IPv4 address>", file=sys.stderr)
            return 2
        result = run_pair(args)
    else:
        result = run_local(args)

    print(json.dumps(result, indent=2, ensure_ascii=False))
    try:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n",
                       encoding="utf-8")
        print(f"\nwritten: {out}")
    except OSError as exc:
        print(f"\n(could not write results: {exc})", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
