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


### The desktop surface

The page is a view over the backend supervisor, not a second state machine. Two
routes, both refusing any caller that is not on this machine:

* `GET /api/peer/status` returns the stored preference and the observed state side
  by side. It constructs no supervisor, so opening the page cannot start a process.
* `POST /api/peer/preference` records `enabled` and answers with a fresh
  observation plus `confirmed`, computed from that observation. An enable that
  ended in `CREDENTIALS_UNAVAILABLE` or `UNHEALTHY` comes back unconfirmed with its
  real reason, and the preference stays on so the next launch retries. A second
  enable does not spawn a second child, and a failed switch write starts nothing.

`PeerPageTests` (8 tests) pins what makes the page honest: off on first run because
the backend says so, loading performs one GET and no POST, the running state
appears only after the backend reports it, a refused start shows the backend reason
instead of a success message, a refresh replaces a stale `RUNNING`, a 500 surfaces
as an error without inventing a state, and the diagnostics carry no key material.
The page deliberately does not use `CommandWithReload`, whose success text is
written after the reload and would have overwritten a failed-start explanation.

`test_peer_routes.py` (18 tests) covers the same contract on the backend: off by
default with no supervisor constructed, a corrupt switch file reading as unknown
and off, malformed bodies and non-loopback callers refused without touching the
switch, a failed write starting nothing, and repeated enables and disables staying
idempotent.

## 11. Not implemented here

* **Networking.** No socket, no discovery, no session, no relay. Phases 3–4.
* **Desktop surface.** Implemented on `phase-2/desktop-peer-integration`:
  `GET /api/peer/status` and `POST /api/peer/preference` in
  `backend/app/api/peer_routes.py`, and the *Peer network* page
  (`PeerViewModel` / `PeerView`). See the section above.

## 11a. Final acceptance audit, 2026-11-01

Statuses are the ones this repository uses: IMPLEMENTED means code exists,
VERIFIED LOCALLY means a command was run here and passed, VERIFIED LIVE means a
real backend and a real child process were exercised, VERIFIED IN CI means the
check passed on the exact head SHA, NOT VERIFIED means it was not measured, and
BLOCKED means something outside this work stopped it.

| Requirement | Status | Evidence |
|---|---|---|
| Supervisor, state machine, switch, secure-store refusal | IMPLEMENTED | `peer/service.py`, `peer/control.py`, `test_peer_service.py` |
| Loopback-only status and preference routes | IMPLEMENTED + VERIFIED LIVE | see below |
| Desktop page, navigation, DTO, client calls | IMPLEMENTED | `PeerViewModel`, `PeerView`, PR #11 |
| Desktop page driven in a running WPF window | **VERIFIED LIVE** | real window, real buttons: `On` + `RUNNING` with a live in-tree supervisor pid, then `DISABLED` + `STOPPED` with no orphan (see below) |
| Marking independence, six states byte-identical | VERIFIED LOCALLY | harness re-run on the integrated tree: one hash, `ff3bcadfdc2c3e46...`, 1156 bytes, 6/7 marks, 3 questions |
| Backend suite | VERIFIED LOCALLY | 2 657 passed, 2 skipped, 702 s, exit 0 |
| Desktop suite | VERIFIED LOCALLY | 343 passed, 3 m 31 s |
| Peer route tests | VERIFIED LOCALLY | 18 passed |
| Peer page tests | VERIFIED LOCALLY | 8 passed |
| Strict documentation links | VERIFIED LOCALLY | 101 files, 201 links, clean |
| PR #9 four checks | VERIFIED IN CI | all success on `a0778687` |
| PR #11 checks | VERIFIED IN CI | all four success on `9a420132` |
| PR #8 Backend check | VERIFIED IN CI | success on attempt 3 on `2f9eb693` (48 min) |
| PR #10 Backend check | FAILED IN CI, cause proven and fixed on PR #12 | same test on both attempts; not Phase 2 code (see below) |

### What the live exercise actually showed

A real `main.py --serve` backend in demo mode, a real supervisor, and a real
child process, driven over HTTP exactly as the page drives them:

* Opening the page (three `GET`s) wrote no peer file and spawned no peer child.
* Enabling with no credentials returned `confirmed: false`,
  `CREDENTIALS_UNAVAILABLE` and a stated reason, kept the preference ON, and
  created no process.
* Malformed bodies (`"yes"`, `1`, `null`, `{}`) were refused with 400.
* Enabling with credentials reached `RUNNING` with a reported pid; the process
  tree contained exactly one supervisor; a repeated enable kept the same pid.
* Disabling answered `confirmed: true`, the child exited, and no orphan remained.
  With the preference off the reported state is `DISABLED`, which is
  `describe()`'s documented rule, not a claim the process is still stopping.
* After a restart the preference was still ON and the page reported the observed
  state (not a stale `RUNNING`) without starting anything.

The apparent "two children" in an early run were one tree: the venv interpreter
re-executes `-m app.peer.service_main`, so the grandchild shares the same
supervised process. Counting processes recursively without checking the parent
misreads this, which is why the check now reports parentage.

### CI failures on PR #8 and PR #10, and why they are not Phase 2

Both failed the same job step (`Run the suite`) on `windows-latest`:

* PR #10 (`e0f3db92`): `test_retry_architecture.py::
  test_successful_job_completes_with_zero_retries` — the per-job prepared-input
  directory still existed at the instant the job was observed terminal. 1 failed,
  2 638 passed, 44 min run.
* PR #8 (`2f9eb693`): `test_worker_pool.py::
  test_metrics_recorded_after_completion` — `avg_seconds` was still absent when
  the terminal state was observed. 1 failed, 2 538 passed.

Neither test touches the peer package, and PR #8 changes no backend code at all
(documentation and one standalone script), so the failure cannot be attributed to
Phase 2. Both are ordering assertions between "terminal status is visible" and
"the worker's post-completion bookkeeping is visible", which invert under a
loaded 40-minute run. Reruns were requested on both PRs; the outcome is recorded
in the audit report rather than assumed here.

### Desktop page driven in a real window, and the CI outcome (2026-11-01)

The page is no longer "compiled only". It was driven in the real WPF window on
this machine through UI Automation, against the app's own backend (loopback
`:8642`) with real department credentials on disk:

* Initial: `Stored preference Off`, `Observed state DISABLED`, credentials
  present, no peer child.
* **Enable** through the real button: `Stored preference On`,
  `Observed state RUNNING`, "Peer service running", backend detail
  "peer service is running", `service.pid` alive and inside the app's own process
  tree. The venv interpreter re-execs the module, so that tree holds a shim and
  the child — one supervisor, not two.
* **Enable again** 3 s later: the same pid (`16716`), `restarts=0`, so no second
  supervisor.
* **Disable** through the real button: `DISABLED`, `service.state=STOPPED`, the
  pid gone from the process tree, no orphan.
* **Restart**: the window and every backend/peer process it owned were killed and
  the app relaunched — the stored preference was still `On`, the reported state
  was `STOPPED` (never a stale `RUNNING`), and no peer child existed until Enable
  was pressed again. An earlier attempt that killed only the window left an
  orphaned backend which the next window attached to; that is a bad probe, not a
  product defect, and it is why the probe now kills the backend too.
* Layout: 1600x1020 and 1280x800 — 64 text/button elements, no zero-sized element
  and no horizontal overflow at either size. Content below the fold is the page's
  own scroll region; the first "outside the window" readings were those elements,
  not clipping.

CI at close of audit: PR #11 `9a420132` has all four checks green, PR #8 passed
its Backend job on the third attempt (`2f9eb693`, 48 min), and PR #9 is unchanged
and green.

PR #10's Backend job failed on **both** attempts on the same test. The cause is
now proven rather than suspected: the pool committed the terminal row and only
afterwards removed the prepared-input cache and recorded the duration, so a
reader polling for a terminal row can see `COMPLETED` first. Slowing that
post-commit step (`_record_scheme_audit`) made both PR #10's retry test and
PR #8's metrics test fail 5/5 with the exact CI assertions. The ordering fix and
a deterministic regression test live on their own branch as PR #12
(`investigation/worker-pool-ci-races`); it contains no peer code, is not part of
this phase, and PR #10 itself was left untouched.

Marking independence was re-measured on `9a420132` after all of the above: the six
states (`disabled`, `running`, `stopped`, `terminated`, `credentials-unavailable`,
`ipc-unavailable`) produced one output hash,
`ff3bcadfdc2c3e46ac2446ac0aa671fbd6acace3322090acc061180af602b8bc`, 1 156 bytes,
6/7 marks and 3 questions — identical to the earlier measurement, harness exit 0.

## 12. Remaining limitations

1. The peer layer is inert but incomplete: no peer can be reached yet, by design.
2. Restart backoff is bounded, not exponential; Phase 6 owns the real policy.
3. One supervisor per data directory, enforced by a lock file — a second
   *installation* with its own data directory is a separate peer, which is
   correct, but not tested across two Windows users here.
4. Idle measurements are for an idle process only.
