# Peer network — Phase 2: the supervised peer process

Status: **implemented** on `phase-2/supervised-peer-process`. It opens no socket,
performs no discovery and speaks to no peer. Networking begins in Phase 3.

This is the implementation record for plan §13 "Phase 2 — Peer service process
and lifecycle". The proposal it implements is
[PEER_NETWORK_PHASE2_PLAN.md](PEER_NETWORK_PHASE2_PLAN.md); the identity rules it
consumes are in [PEER_NETWORK_PHASE1.md](PEER_NETWORK_PHASE1.md); the approved
product decisions are in [PEER_NETWORK_DECISIONS.md](PEER_NETWORK_DECISIONS.md).

---

## 1. What this phase answers

> A peer process can be started, stopped and observed, and its absence cannot
> change a marking result.

That second clause is the reason the phase exists. Everything else — the state
machine, the switch, the protocol — is in service of proving that peer
networking is *additive*.

## 2. Architecture and the trust boundary

```
backend (signed-in user, owns DPAPI)
  └── {service.PeerService}  ── supervises exactly one child ──┐
        • durable ON/OFF switch (control.py)                   │ JSON lines
        • secure-store probe, credential verification          │ over the
        • lock file, crash detection, restart backoff          │ child's own
        • truthful status snapshot                             │ stdin/stdout
  ┌────────────────────────────────────────────────────────────┘
  └── child: `python -m app.peer.service_main --credentials <path>`
        • re-verifies the chain itself before saying "ready"
        • serves ping / status / shutdown, and nothing else
        • exits when its stdin closes (orphan guard)
```

**Why a separate process.** A peer bug — an import-time crash, a runaway loop, a
leaked handle — cannot reach the marking engine's memory, and `stop()` is a real
process boundary rather than a flag. This is the mechanism behind plan §10.4 and
R-10.

**Why stdin/stdout and not a named pipe or a socket.** The child's pipes are
created by the supervisor's own `CreateProcess` and are unreachable by any other
process without that process already running code as this user. A named pipe
would need an explicit ACL and a name that a second supervisor could race for; a
socket would be network-shaped exposure in a phase that has no network. Pipes also
give the orphan guard for free: end-of-file means the supervisor is gone.

**Trust boundary, stated plainly.** The supervisor can read the private key; the
child cannot ask it to. The child receives a *path* and re-runs the Phase 1
verification itself, so a supervised-process bug cannot make it serve with
material it was never allowed to hold. Neither side logs, transmits or returns
key material: `status()` is assembled from public certificate fields only, and a
test asserts the snapshot contains no key.

## 3. Lifecycle

| State | Meaning |
|---|---|
| `DISABLED` | The switch is off. Nothing was started, and nothing was written. |
| `STARTING` | Store probe, credential load, or child handshake in progress. |
| `RUNNING` | The child answered `ready` **after** verifying the chain itself. |
| `STOPPING` | `shutdown` was sent; waiting for exit, then terminating. |
| `STOPPED` | No child, lock released. |
| `UNHEALTHY` | A check or the process failed: store not writable, duplicate supervisor, crash, start timeout. |
| `AUTH_FAILED` | Credentials exist but do not verify: expired, revoked, wrong department, bad signature, roster missing the device. `reason` carries the Phase 1 code. |
| `CREDENTIALS_UNAVAILABLE` | No credential file, or one that cannot be read. |

`RUNNING` is never set before the child's own verification succeeds, so a
partially initialised process cannot be reported as healthy. Transitions are
held under one re-entrant lock, and a status snapshot is a frozen value.

## 4. Secure startup

The order is deliberate; each step has one failure state.

1. **Switch** — off means `DISABLED` before anything else happens.
2. **Secure-store probe** — write a random token, read it back, compare, and
   inspect the file's own marker byte. `MC1D` means DPAPI-protected for this
   user; `MC1P` means the store fell back to plaintext, which is reported in
   `store_detail` rather than assumed away (D1, R-08). The probe is the only
   caller allowed to request plaintext, because its job is to report which mode
   this process gets. Every other write fails closed: `secure_store.save()`
   raises `SecureStoreUnavailable` and writes nothing when DPAPI cannot protect
   the payload, leaving any existing file untouched, and `start()` refuses to
   run on an unprotected store unless a caller passes
   `require_store_encryption=False` explicitly.
3. **Credential load** — `PeerCredentials.load`, which refuses a file whose
   identity, certificate and department disagree.
4. **Chain verification** — `verify_certificate` against the department key with
   `expected_dept_id`, so a certificate from another department cannot ride in.
5. **Pinning** — an optional `pinned_department_key` must match the stored
   department key (`WRONG_DEPARTMENT` otherwise). Phase 2 accepts the pin; the
   setup path that supplies it from a previous credential is the remaining wiring
   item (§11).
6. **Expiry** — `CERT_EXPIRED` and `CERT_NOT_YET_VALID` are distinct reasons
   (D4). The snapshot also carries `certificate_days_remaining` and
   `warn_renewal` (true inside 15 days) so a surface can warn before it fails.
7. **Revocation and roster** — the stored revocation list is consulted by
   `verify_certificate`, and the device must appear on its own roster
   (`ROSTER_INCOMPLETE`).
8. **Duplicate prevention** — a lock file containing the owner's pid. A live
   owner refuses the second supervisor; a dead owner's lock is replaced.
9. **Child start and handshake** — the child re-verifies, then emits
   `{"event":"ready"}` or `{"event":"failed","reason":...}`; a timeout is
   `UNHEALTHY`, never a hang.

## 5. The switch

`backend/app/peer/control.py`, modelled on `distributed/agent_control.py`:
`DATA_DIR/peer/peer_state.json`, atomic replace, **off by default**, and a corrupt
file reported as unknown rather than guessed at. `describe()` combines the stored
preference with the *observed* state, so a switched-on service that is stopped is
labelled "Peer service stopped" — a preference is never presented as an
observation.

## 6. Local protocol

One JSON object per line in each direction. There is no operation that takes a
path, a command or a host.

Request:

```json
{"protocol": "mc5-peer-ipc-v1", "request_id": "<32 hex>", "operation": "ping"}
```

Response:

```json
{"request_id": "<same>", "ok": true, "result": {"pong": 1793..., "uptime_seconds": 12.4}}
{"request_id": "<same>", "ok": false, "error": {"code": "unsupported_operation", "detail": "..."}}
```

Events (unsolicited, before requests): `{"event": "ready"|"failed"|"malformed_output"}`.

Validation, in this order, with a stable code for each: size (`request_too_large`,
64 KiB), emptiness (`empty_request`), JSON (`malformed_request`), protocol
(`unsupported_protocol`), `request_id` shape (`malformed_request`), operation
(`unsupported_operation`). Size is checked *before* content, so an oversized line
is refused unparsed. Operations are exactly `ping`, `status`, `shutdown`.

Timeouts: `request_timeout` (default 5 s) bounds every reply; `stop_timeout`
(6 s) bounds graceful exit before termination; `start_timeout` (25 s) bounds the
handshake. A reply carrying a different `request_id` is discarded, so events
cannot be mistaken for answers.

## 7. Duplicate prevention, crash handling, orphan guard

* **One supervisor per installation:** the lock file names the owner's pid. A
  live owner → second start reports `UNHEALTHY` with the pid, not a silent second
  process. A dead owner's lock is replaced.
* **Crash:** the reader thread notices end-of-stream, reaps the exit code, and
  reports `UNHEALTHY` with it. Restarts are bounded (`max_restarts`, default 3)
  and spaced (`restart_delay`), so a permanently broken install cannot become a
  start/stop loop.
* **Orphan guard:** the child exits when its stdin reaches end-of-file. A killed
  supervisor cannot leave an unreachable peer process behind; a test starts a
  real child, closes stdin, and asserts exit code 0.

## 8. Measured resources

`backend/scripts/peer_phase2_measure.py` starts a real supervised child with
throw-away credentials in a temporary directory and samples it through
`psapi`/`GetProcessTimes`.

Observed on 2026-10-31, development machine, project venv (Python 3.13 +
`cryptography`), 15-second idle window:

| Metric | Observed | Target |
|---|---|---|
| Working set | **4.1 MB** | ≤ 40 MB |
| Idle CPU | **0.0 %** | ≤ 0.5 % |
| Store encryption | DPAPI-protected (`MC1D`) | DPAPI (D1) |
| Ping during window | answered | — |
| Stop | `STOPPED`, lock released | clean |

Reproduce with:

```
PYTHONPATH=backend .venv/Scripts/python.exe backend/scripts/peer_phase2_measure.py --seconds 15
```

**Limitations, not hidden:** a packaged runtime bundles its own interpreter and
will differ; the process is idle by construction because Phase 2 has no timers
and no networking, so this is the floor rather than a workload; and the numbers
are from one machine.

## 9. Marking-path isolation

* No marking, correction, scheme, reporting, classroom, job or worker module
  imports `app.peer` — asserted by walking the AST of every file in those
  subtrees (`test_peer_isolation.py`), so the invariant fails loudly if someone
  adds an import later.
* The peer package imports no marking internal, and `import app.peer.service`
  starts nothing and writes nothing.
* With the switch off, `PeerService.start()` returns `DISABLED` having created no
  state file, no lock and no process.
* Separation is also physical: the peer runs in its own interpreter, so a crash
  cannot occupy the marking process.

**Measured, not asserted.** `backend/scripts/peer_marking_independence.py` runs
the real `grade_with_model` -> `normalise_result` -> `apply_blueprint` ->
`calculate_totals` path over one fixed paper in six peer states: switched off,
running, stopped, child killed from outside, credentials unavailable, and IPC
unavailable under a live supervisor. It hashes the bytes the app would persist
(`json.dumps(result, sort_keys=True)`). All six produce the same digest,
`ff3bcadfdc2c3e46...`, 1156 bytes, 6/7 marks, three questions, with the observed
peer state recorded beside each hash so the table shows what was running as well
as what came out (`docs/results/PEER_PHASE2_MARKING_INDEPENDENCE.json`). Nothing
is masked: a timestamp or identifier appearing in the marking payload would
change the hash and fail the script, which is exactly why the comparison is over
bytes and not over parsed objects.

## 10. Tests

```
.venv/Scripts/python.exe -m pytest backend/tests/test_secure_store_hardening.py \
    backend/tests/test_peer_service.py backend/tests/test_peer_identity.py \
    backend/tests/test_peer_invite.py backend/tests/test_peer_isolation.py \
    backend/tests/test_device_discovery.py -q
```

37 passed, 1 skipped (a subtree that does not exist in this layout). Coverage:
switch default/durability/corruption, truthful labels, store probe, credential
snapshot without key material, expiry, wrong pin, roster missing the device,
`DISABLED` without a process, missing credentials, expired credentials, duplicate
supervisor, stale lock replacement, eight invalid-request cases, a real child
(start → ping → status → stop), a killed child reported `UNHEALTHY`, orphan
guard, child refusing unverifiable credentials, and measurable resource use.

The real-process tests are not mocked: they spawn `python -m app.peer.service_main`
and read its stdout.

`test_secure_store_hardening.py` adds 15 regression tests for R-08: a required
write that DPAPI cannot protect raises and leaves nothing behind (no file, no
temporary file, and an existing payload byte-for-byte untouched), an explicitly
relaxed write is the only path to plaintext and reports the mode it wrote, a
corrupt or truncated store raises `SecureStoreCorrupt` instead of looking like a
first run, an unmarked legacy file still loads and is not rewritten, a missing
directory and a read-only permission failure both surface as actionable errors,
the probe reports the mode it actually got, and the supervisor refuses an
unprotected store while an explicit per-instance opt-out still starts.

## 11. Not implemented here

* **Networking.** No socket, no discovery, no session, no relay. Phases 3–4.
* **Desktop surface.** The switch and status are backend-side (`control.describe()`,
  `PeerService.status_dict()`); binding them to a desktop page/route is the next
  step and is not done. No route was added, so nothing new is reachable over
  HTTP.
* **D1 offline export.** Approved in principle; deliberately not built yet —
  an export is a root-key-equivalent and needs its own review.
* **Renewal action.** D4's 90/365-day policy is enforced and a warning flag is
  computed, but the "renew this device" flow is not implemented.
* **Packaged-runtime measurement.** The resource numbers in §8 come from a
  development interpreter.

## 12. Remaining limitations

1. The peer layer is inert but incomplete: no peer can be reached yet, by design.
2. Restart backoff is bounded, not exponential; Phase 6 owns the real policy.
3. One supervisor per data directory, enforced by a lock file — a second
   *installation* with its own data directory is a separate peer, which is
   correct, but not tested across two Windows users here.
4. Idle measurements are for an idle process only.
