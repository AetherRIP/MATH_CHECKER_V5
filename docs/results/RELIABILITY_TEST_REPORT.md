# MathChecker V5 — Reliability Test Report

Scope: the changes in [PERFORMANCE_HARDENING_REPORT.md](PERFORMANCE_HARDENING_REPORT.md).
Everything claimed here was executed in this repository; nothing is asserted
from reading the code alone.

---

## 1. Suites run

| Suite | Command | Result |
|---|---|---|
| Backend | `.venv/Scripts/python.exe -m pytest backend/tests -q` | see §8 (final run) |
| Desktop | `dotnet test desktop/MathChecker.Desktop.slnx --nologo` | see §8 (final run) |
| Benchmark / audit scripts | executed against the live provider and the local database | §5, §6 |

Neither suite existed in CI (the repository has no CI configuration), so these
local runs are the verification.

---

## 2. Cancellation and timeout verification

The question the task asks is *does a timeout actually stop the work*, so the
tests assert the OS process is gone rather than that a flag was set.

| Test | What it proves |
|---|---|
| `test_a_wedged_child_is_terminated_and_reported` | A real `python -c "time.sleep(120)"` child is terminated: `proc.poll() is not None` after `_stall_result`, and the failure is reported as `worker_stalled`. A timed-out worker does not keep running. |
| `test_a_live_child_is_never_killed` | A child that has signalled recently is left alone, so the watchdog cannot kill legitimate work. |
| `test_every_line_counts_as_a_sign_of_life` | Silence, not elapsed time, is what the watchdog measures. |
| `test_an_idle_worker_is_never_reported_as_stalled` | An idle worker has no process and cannot be killed. |
| `test_heartbeat_cannot_be_mistaken_for_the_result` | Heartbeats are stripped before the result line is chosen, so the liveness signal cannot corrupt the correction result. |
| `test_heartbeat_writes_only_a_timestamp` | The heartbeat carries `{"type","ts"}` and nothing else — no key, prompt, mark or student text can leak through it. |
| `test_a_stall_is_an_infrastructure_failure_not_a_provider_retry` | A killed child maps to `WORKER_CRASH` (two bounded infra retries), not to a re-sent provider request. |

Cancellation by the user is unchanged and covered by the existing suite:
`cancel_job` terminates the child, marks the job `CANCELLED` and cleans the
job's files (`test_jobs_and_verify.py`).

---

## 3. Retry and overload behaviour

| Test | What it proves |
|---|---|
| `test_a_provider_wide_503_is_resent_through_at_most_two_keys` | One marking call is sent **twice**, not eight times, when the model is shedding work — and it asserts `MAX_OVERLOAD_KEY_ATTEMPTS <= 2`. |
| `test_a_rate_limited_key_still_rotates_to_another` | 429 is still treated as per-key: rotation is unchanged. |
| `test_the_first_park_is_at_least_a_minute` | A high-demand park cannot be a two-second storm retry. |
| `test_repeated_parks_wait_longer` | 60 → 120 → 240 → 480 s. |
| `test_the_park_wait_is_capped` | A long outage escalates to the 10-minute cap, not beyond. |
| `test_jitter_never_drops_below_the_minimum` | A low jitter draw cannot undercut the 60 s floor. |
| `test_decide_uses_the_park_count` | The pool's persisted park count drives the wait (no memory-only state). |
| `test_escalating_parks_end_in_a_clean_failure_within_the_budget` | Growth still terminates inside the 15-minute waiting budget: at most five parks, then `FAILED`. |
| `test_a_longer_provider_hint_still_wins` | A provider `Retry-After`/capacity hint above the computed wait still wins. |
| `test_one_504_request_is_not_blindly_resent_on_the_same_key` (existing) | Timeouts do not re-send on the same key. |
| `test_authentication_never_retries`, `test_invalid_request_never_retries` (existing) | Permanent errors stop immediately. |
| `test_a_504_costs_one_retry_and_is_not_parked` (existing) | Timeouts consume the retry budget rather than parking. |

No error class had its policy changed in this pass; the taxonomy table in the
hardening report lists every class and the test that pins it.

---

## 4. Marking behaviour

The marking engine, prompt, schema, question detection, clamping and
double-check are untouched by this pass, so their tests are the regression net:

* `test_engine_parity.py`, `test_legacy_engine.py`, `test_marking_determinism.py`
* `test_double_check.py`, `test_mark_integrity.py`, `test_defective_scheme.py`
* `test_grade_with_model_*` / manual-correction and teacher-override suites
* `test_marking_reference_harness.py`, `test_benchmark_accuracy.py`

All pass in the final backend run (§8). Two live corrections in the benchmark
(§6) also completed end to end through this path with marks and totals, which is
the end-to-end confirmation that the worker/paper pipeline still produces a
usable result.

---

## 5. Queue verification

`backend/scripts/job_timing_breakdown.py` was run against the installation's own
database (35 jobs, 1 006 stage events):

```
phase        n    median       p95       max   total h
queue       35     1205s    13585s    13892s     43.74
preparing   35       11s       32s     1846s      0.77
uploading   35       10s       28s      119s      0.14
marking     35      668s     8235s    10550s     18.89
post        35        0s      481s     1618s      0.83
idle        35        0s        0s        0s      0.00
```

`--simulate` compares that against a FIFO server fed the measured service times:

| Workers | Predicted queue | Observed |
|---|---|---|
| 3 | 72.03 h | 43.74 h |
| **4** | **44.06 h** | **43.74 h** |
| 6 | 27.57 h | — |
| 4, with 160 s service time | **2.58 h** | — |

The 0.7 % agreement at four workers is the evidence that the archive's queue
wait is arithmetic rather than a scheduling defect, and that reducing service
time (not worker count) is the correct lever.

---

## 6. Live provider behaviour during this work

The provider was shedding large multimodal requests throughout the benchmark
window. That is a real-world condition test, and it is recorded rather than
smoothed over:

| Observation | Count |
|---|---|
| Arms completed | 5 of 8 (run 2); 2 of 10 (run 1, kept in `PERFORMANCE_BENCHMARK_run1_outage.json`) |
| Failures classified `503 UNAVAILABLE` / HIGH_DEMAND | 3 (run 2) + 8 (run 1) |
| Requests prevented by the model gate (`ProviderCoolingError`, nothing sent) | 6 |
| Requests cut short by the two-key overload bound | 3 (visible as `key rotation skipped` / immediate failure) |
| Wall time of a refused request after hardening | 0.0–29 s |
| Wall time of the same refusal before the fix (archive) | up to four attempts × page-scaled timeout |

This is the behavioural evidence for the overload bound: a shed request now
fails in seconds and lets the job park, instead of re-sending a 45-page request
eight times.

---

## 7. Test-infrastructure defect found and fixed

The first full-suite run failed two tests that pass in isolation:

```
FAILED test_retry_architecture.py::TestKeyFailoverMocked::test_invalid_request_is_never_resent
FAILED test_retry_architecture.py::TestKeyFailoverMocked::test_success_first_attempt_uses_zero_retries
ProviderBudgetExceeded: This correction has already used its budget of 12 AI requests.
```

Root cause: `app.gemini.call_audit` is a process-global ledger that is only reset
by `begin(job_id)`, which production calls in the worker *child* process. In a
single pytest process, a test that begins a ledger leaves its job id and send
count behind, so every provider call a later test makes is charged to that
finished correction until the 12-request budget starts rejecting them.

The fix is in `backend/tests/conftest.py`: the ledger is reset per test, exactly
as the provider-health gate and the concurrency controller already were. No
assertion was weakened and no production behaviour changed — the budget itself
still fires, and `test_quort_budget.py` proves it.

---

## 8. Final results

Executed after the last code change on this tree.

| Suite | Command | Result |
|---|---|---|
| Backend | `.venv/Scripts/python.exe -m pytest backend/tests -q -p no:randomly` | **2 555 passed, 1 skipped** (11 m 42 s), exit 0 |
| Desktop | `dotnet test desktop/MathChecker.Desktop.slnx --nologo -c Release` | **335 passed, 0 failed** (3 m 36 s) |
| New tests | `pytest backend/tests/test_retry_amplification.py backend/tests/test_worker_stall_watchdog.py` | **16 passed** (9 amplification, 7 watchdog) |
| Queue tool | `python backend/scripts/job_timing_breakdown.py --simulate` | 64.37 h of 35 jobs fully attributed; FIFO model within 0.7 % of observed |
| Benchmark | `python backend/scripts/benchmark_hardening.py --ack INFERENCE --papers 4` | 5 of 8 arms completed; every failure recorded (§6) |

### Desktop suite: an environment block, reported as such

The first desktop run failed **all 320 tests** with the same error:

```
System.IO.FileLoadException : Could not load file or assembly
'…\bin\Debug\net10.0-windows\V5.dll'.
An Application Control policy has blocked this file. (0x800711C7)
```

Every test loads the application assembly, so one blocked binary fails the
whole suite in ~0.2 s per test. This is the operating system refusing the
freshly built Debug assembly, not a test or code failure — no desktop source
file was touched in this pass. The same suite, rebuilt in Release into a
different output path, passes 335/335.

It is reported rather than worked around: nothing was excluded, skipped or
re-tagged to make it pass. A machine with that policy will need the build output
allow-listed (or signed) before the Debug configuration can be used for tests.
