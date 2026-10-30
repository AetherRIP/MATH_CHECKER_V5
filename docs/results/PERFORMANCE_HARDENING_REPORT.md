# MathChecker V5 — Performance Hardening: Audit, Risks and Changes

Status: **implementation complete, benchmark evidence in
[PERFORMANCE_BENCHMARK_REPORT.md](PERFORMANCE_BENCHMARK_REPORT.md)**, tests in
[RELIABILITY_TEST_REPORT.md](RELIABILITY_TEST_REPORT.md).

This document is the audit the task asks for *before* the changes: how a
correction actually flows, where the retry/timeout/cancellation boundaries are,
what was wrong with them, what was changed and what remains. Every number here
comes from the repository's own database, code or a test run — none is
estimated.

---

## 1. How one correction actually executes

```
API /api/corrections            jobs.manager  → row in correction_jobs (QUEUED)
        ↓
workers.pool  (one background thread, 0.5 s wake)
   _dispatch_queued
     gate 1  net_usage      hourly/daily internet budget
     gate 2  provider_health.available()   every configured model cooling?
     gate 3  _keys_ready()  is any API key usable?
     gate 4  _preflight_ready()  one cached provider probe (multi-job starts)
     claim   db.transaction: QUEUED → STARTING, attempts += 1
     _stage_inputs   Drive download + PDF normalise + scheme resolve (cached per job)
     _launch → CorrectionWorker.start_job
        ↓
workers.child   (separate OS process, stdin job JSON, stdout events/result)
   legacy_marking.grade_with_model
     per model in chain:  1-2 attempts (primary 2, rescue 1)
        ↓
gemini.client_proxy.KeyAwareClient.generate_content
     provider_health gate → up to 8 key attempts → SDK call (retry_pinned to 1)
        ↓
google-genai → httpx  (request timeout = 120 s + 15 s/page, cap 30 min)
        ↓
stdout result → pool._collect_finished → classification → retry / park / fail
```

Every retry owner and its budget:

| Layer | Owner | Budget | Consequence of exhaustion |
|---|---|---|---|
| HTTP/SDK | `google-genai` | pinned to **1 attempt** (`SDK_RETRY_ATTEMPTS`) | none — it never retries |
| Key rotation | `client_proxy` | up to 8 attempts, **2** for provider-wide overload | error surfaced to the model loop |
| Model chain | `legacy_marking` | 2 attempts primary, 1 per rescue model | error surfaced to the job |
| Job | `workers/pool` + `retry_policy` | `AUTO_RETRY_SUSPEND_AFTER = 2` retries, 4 parks, 15 min waiting budget | job FAILED with evidence kept |
| Auth | `key_manager` | cool-down per key, honouring `Retry-After` | key marked unusable, rotation |

The multiplication of the first three columns is what turned one 503 into an
hour of re-sent 41-page papers — the historical regression documented in
[CORRECTION_PERFORMANCE_RECOVERY.md](CORRECTION_PERFORMANCE_RECOVERY.md).

---

## 2. Where the wall clock goes (archive evidence)

`backend/scripts/job_timing_breakdown.py` attributes every second of every job
to the phase it was in, from `job_stage_events` (1 006 events, 35 jobs). Nothing
is left in a residual: the phase totals sum to the jobs' wall clock.

| Phase | Median | p95 | Max | Total |
|---|---|---|---|---|
| queue (created → first stage) | 1 205 s | 13 585 s | 13 892 s | **43.74 h** |
| preparing (PDFs, scheme) | 11 s | 32 s | 1 846 s | 0.77 h |
| uploading | 10 s | 28 s | 119 s | 0.14 h |
| marking (provider) | 668 s | 8 235 s | 10 550 s | **18.89 h** |
| post (totals, double-check, save) | 0 s | 481 s | 1 618 s | 0.83 h |
| idle | 0 s | 0 s | 0 s | 0.00 h |

Two conclusions follow, and they are *not* the ones the brief assumed:

1. **The queue, not the model, was the largest cost.** 43.74 h of the 64.37 h
   total was spent waiting for a worker. Capacity waits (parks) account for
   **one** job, 15 s — so the queue was not an outage queue.
2. **The queue is arithmetic, not a scheduling defect.** A FIFO model fed the
   *measured* service time of each job predicts 44.06 h of queue wait at
   4 workers against 43.74 h observed (0.7 % error). No worker sat idle while a
   job waited; the queue was `depth × service time / width`.

The same model with the recovered service time (~160 s per paper) predicts
2.58 h at 4 workers for the identical workload — a 17× reduction — **without
touching concurrency**. That is why this work does not raise worker counts: more
workers on a provider that sheds requests makes the outage worse, while cutting
service time fixes both halves of the problem at once.

---

## 3. Risks found, and what was done

### R1 — Nested retries (fixed earlier, verified here)

`google-genai` retries inside its HTTP layer when given retry options, and its
default is not something this application controls. The proxy pins it:

```python
SDK_RETRY_ATTEMPTS = 1
types.HttpOptions(timeout=timeout_ms, retry_options=types.HttpRetryOptions(attempts=1))
```

and the fallback-model override in `legacy_marking` re-pins the same policy,
because a request-level `http_options` does not inherit the client's.
**Verified:** `test_retry_amplification.py` counts actual sends — a provider-wide
503 produces exactly `MAX_OVERLOAD_KEY_ATTEMPTS` sends, not 8.

### R2 — A 503 was re-sent through every key (fixed)

A 503 says the *model* is shedding work; it is not a property of the credential.
Rotating keys re-sent the same 41-page request up to 8 times, each charged
whether or not it was answered. `MAX_OVERLOAD_KEY_ATTEMPTS = 2` bounds that.
Rate-limit and dead-key paths still rotate fully, because those really are per
key. **Measured in the live log of this session:** a 503 now fails the request
in 13.9 s with `key rotation skipped (re-upload too large)` instead of grinding
through the pool.

### R3 — A fixed 60 s park (fixed)

A fleet parked for exactly 60 s resumes in lockstep against a provider that is
still shedding. `compute_park_backoff(parks)` now waits 60 s, 120 s, 240 s, 480 s
— capped at 10 min, jittered, never below the 60 s minimum — and the existing
15-minute waiting budget and 4-park ceiling still end the wait in a clean
`FAILED` with evidence kept.

### R4 — A wedged child held a worker forever (fixed)

This was the most serious reliability gap and it had no bound at all. Every
provider request is bounded by the SDK timeout, and a crashed child is detected
by exit code — but a child that stops *running our code* (deadlock, hung native
call, process the OS suspended) produced no exit and no error. It kept:

* its worker slot (the pool only collects finished processes),
* its exclusively leased API key,
* a `PROCESSING` job row.

The only way out was a teacher pressing Cancel.

**Fix.** The child now writes `{"type":"heartbeat","ts":…}` every 5 s from a
daemon thread (`child._start_heartbeat`), under the same stdout lock as stage
events so a line can never interleave with the result. The parent records the
time of every line it reads (`CorrectionWorker._last_signal`) and, when a child
has written *nothing* for `WORKER_STALL_SECONDS = 300`, terminates it
(`force_stop`: terminate → wait 10 s → kill → reap → close stderr) and reports a
normal `worker_stalled` result. That result is classified as `WORKER_CRASH`
(infrastructure, retryable twice, never a re-sent provider request), so it flows
through the existing park/retry/suspend logic rather than a new code path.

A slow provider call keeps heartbeating, so it is never killed by this: it is
bounded by its own request timeout, which is the thing that should bound it.

### R5 — What a timeout actually terminates

| Question | Answer | Evidence |
|---|---|---|
| Does the timeout cancel only the coroutine, or the HTTP request? | The request. `HttpOptions(timeout=…)` becomes the httpx timeout; the SDK call is synchronous, so it raises and closes the socket before the caller can retry. | `client_proxy._http_options`, `timeout_for_pages` |
| Does the SDK keep processing after we consider it timed out? | Not observable from this process. It may have *completed* the request server-side. | documented limitation, see below |
| Does a timed-out request still consume quota? | Possibly, if the provider finished it. We cannot observe it, so it is stated and mitigated, not hidden. | mitigation: 1 attempt per request, generous page-scaled budget, rescue-model deadline |
| Is the worker slot released safely? | Yes: the child that owns the request is the unit of work; `force_stop`/`cancel` terminate it, and the pool then marks the worker idle. | `adapter.force_stop`, `test_a_wedged_child_is_terminated_and_reported` |
| Can an abandoned request keep occupying resources? | No client-side: the process and its sockets die with it. Provider-side consumption is the unobservable part. | same test asserts `proc.poll() is not None` |
| Can cancellation leave corrupt job records? | No new state is invented: the failure path writes `FAILED`/`RETRYING`/`WAITING_FOR_API` exactly as for any error, and the idempotency guard runs first. | `_handle_failure` ordering |
| Can a retry start while the previous request is still active? | Not in this process: the SDK call is synchronous and the parent kills the child before requeueing. Provider-side overlap is bounded by the single-attempt policy. | `MAX_OVERLOAD_KEY_ATTEMPTS`, `force_stop` |

**Timeout policy.** The four-minute `FALLBACK_MODEL_DEADLINE_MS` was **not**
reduced. Measured healthy request durations in this repository: 97 s for a
15-page scan plus a 26-page scheme, 139 s for a real 41-page call, 48 s for the
26-page paper measured in this session. Four minutes is ≥ 1.7× the slowest
healthy observation, so a rescue model that cannot answer in that time is not
merely slow, while a working rescue is unaffected. The primary model keeps its
page-scaled budget (120 s + 15 s/page, capped at 30 min), which is what a large
paper legitimately needs.

### R6 — Preparation blocks the scheduler loop (found, documented, not changed)

`_stage_inputs` (Drive download, normalise, scheme resolve) runs in the pool's
scheduling thread, so while one job is being prepared no other result is
collected and no other job is claimed. The archive puts preparation at 11 s
median (32 s p95, 1 846 s worst case — one pathological download). At the tail
this is a real head-of-line cost: a single 30-minute staging pass delays every
other worker.

It is **not** fixed here because moving staging off the loop touches the claim
transaction, the prepare cache and the park accounting at once — exactly the kind
of change this task forbids making without separate validation. It is recorded as
the first candidate for the next hardening pass, with the measurement above as
the justification.

### R7 — Superfluous or missing retries in the taxonomy

Audited and left as-is (existing tests cover each class):

| Error | Behaviour | Test |
|---|---|---|
| 503 / UNAVAILABLE | `HIGH_DEMAND` → park, ≤ 2 key sends | `test_retry_amplification`, `test_provider_outage` |
| 429 / RESOURCE_EXHAUSTED | `RATE_LIMIT` → key cool-down honouring `Retry-After` | `test_retry_architecture` |
| authentication / invalid key | `AUTHENTICATION_ERROR` → **never** retried | `test_retry_architecture` |
| malformed request | `INVALID_REQUEST` → fail fast | same |
| invalid model | `MODEL_ERROR` → fail fast, no alias re-send | `test_model_migration` |
| network timeout | `TIMEOUT` → one retry | `test_provider_outage` |
| user cancellation | `CANCELLED`, nothing requeued | `test_jobs_and_verify` |
| invalid model response | validation policy, one controlled retry | `test_retry_architecture` |
| stalled child (new) | `WORKER_CRASH`, two infra retries | `test_worker_stall_watchdog` |

No policy was invented for a class that already had one.

### R8 — Batching is a planner, not a path (investigated, not enabled)

`engine/batching.py` builds and validates a page partition (`plan_batches`,
`validate_plan`) with question-aware boundary adjustment and explicit
`flagged_questions` when a boundary cannot be moved safely. **Nothing in the
marking path calls it** — no merge step exists, so there is no second execution
path to benchmark and no silent behaviour change in this release.

Enabling it would need a merge layer that attributes each batch's marks to its
own pages, discards overlap pages that were never scored, and flags straddling
questions — a change to the correction engine, which this task excludes. The
cost model that decides whether it is ever worth building is in the benchmark
report (API calls scale linearly with batches; tokens include the scheme once per
call).

### R9 — Duplicate concurrent execution

Three independent guards, all pre-existing and unchanged:

1. the claim is a single write transaction that re-checks `status='QUEUED'`;
2. a retry whose result row already exists is completed instead of re-run;
3. `_existing_result_id` runs **before** any retry decision on a failure.

A retry is only scheduled after the child process has terminated, so two
attempts of the same job cannot be alive at once in this parent.

---

## 4. Changes made in this pass

| File | Change |
|---|---|
| `backend/app/gemini/client_proxy.py` | `MAX_OVERLOAD_KEY_ATTEMPTS = 2`; timeout diagnostics (deadline in force, pages, duration) |
| `backend/app/retry_policy.py` | `compute_park_backoff` (60 → 120 → 240 → 480 s, 10 min cap, jittered); `decide(…, parks=…)`; `worker_stalled` → `WORKER_CRASH` |
| `backend/app/workers/pool.py` | stall watchdog in `_collect_finished`; park count passed to `decide` |
| `backend/app/workers/child.py` | heartbeat thread, stdout lock, result written last under the lock |
| `backend/app/workers/adapter.py` | `_last_signal`, `seconds_since_signal`, `force_stop` (terminate → wait → kill → reap) |
| `backend/scripts/job_timing_breakdown.py` | new: phase decomposition + FIFO queue model |
| `backend/scripts/benchmark_hardening.py` | new: multi-paper, order-randomised, failure-recording benchmark |
| `backend/tests/test_retry_amplification.py` | new: 9 tests (key-send bound, park growth, budget termination) |
| `backend/tests/test_worker_stall_watchdog.py` | new: 7 tests (liveness, no false kill, real termination, classification) |

No prompt, schema, sampling, question-detection or double-check logic was
touched. `grade_with_model` keeps its signature and failure semantics.

---

## 5. Remaining limitations

1. **Provider-side completion of an abandoned request is unobservable.** A
   timeout stops *our* wait; the provider may still finish (and charge) the
   request. Mitigated by single-attempt requests and generous budgets, not
   eliminated.
2. **Preparation still runs in the scheduling thread** (R6).
3. **No teacher-certified accuracy set exists** (0 of 16 items are
   teacher-verified), so every accuracy statement in this repository is
   agreement with a scheme, never certification.
4. **A single-provider dependency.** Every mitigation above reduces the cost of
   an outage; none removes the outage.
5. **Batching is unbuilt** (R8): the planner exists, the merge layer does not.
6. **The 300 s stall threshold** is a judgement, not a measurement: it is 60×
   the heartbeat interval and well above the slowest legitimate silent period
   observed (a 139 s marking call), but a machine that freezes the child's
   interpreter for >300 s in native code would be killed and retried.
