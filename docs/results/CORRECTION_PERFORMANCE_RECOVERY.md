# Correction performance recovery — investigation, changes and measurements

Date: 2026-10-30
Repository: `AetherRIP/MATH_CHECKER_V5`, branch `main`
Production model at the start of this work: `gemini-3.6-flash`

Reported symptom: ~130 s per paper and ~120 papers/day historically, versus
~600 s per paper and ~40 papers/day now (≈4.62× slower, ≈66.7% lower
throughput).

**Headline finding: the correction engine is not slower. The failure path is.**
A healthy correction measured **109 s** in the recorded archive and a full
41-page marking call measured **139 s** today — both in line with the historical
130 s. The 600 s average comes from transient provider 503s being escalated to a
model that needs 12–140 minutes per paper, then retried repeatedly.

---

## A. Historical investigation

### What is recoverable

There are **no tags and no releases** in this repository, so the
"5.2.1 / build 20261012-2" baseline can only be identified by commit date. That
is unambiguous enough here:

| Date | Commit | Significance |
|---|---|---|
| 2026-10-12 | `feaa1ce` "V5" | the original web application (Vite + React + Python), the period the 130 s figure comes from |
| 2026-10-13 | `6e23a5b` | mark-scheme error detection (`blueprint` / verification calls) |
| 2026-10-17 | `2a03321` | mark scheme read **page by page** (`scheme_inventory`: 6-page chunks with 1-page overlap, rendered at a controlled DPI) |
| 2026-10-21 | `91b531d` | compress and verify documents before marking |
| 2026-10-23 | `871868d` | **retire the Large PDF Worker**; every submission follows one pipeline |
| 2026-10-24 | `a5c6e0f` | compress PDFs before upload (measured 19% and 47% smaller in the archive) |
| 2026-10-24 | `db7a8d9` | replace deprecated model aliases; `DEFAULT_PRIMARY_MODELS = ("gemini-3.8-flash", "gemini-3.6-flash")` |
| 2026-10-25 | `89b0f43` | page-scaled request timeout ("stop a big paper from timing out") |

### The Large PDF Worker: why it existed, and whether its removal caused this

It inspected the documents, split a long script into page batches, marked each
batch with a dedicated prompt that told the model it held *one batch of a longer
paper*, and merged by printed index (`backend/app/large_pdf/*` at `feaa1ce`,
deleted in `871868d`). It existed because a single request over a very long PDF
produced incomplete marks — the prompt's own docstring records a 49-page
submission where 32 questions (128 marks) were never marked and reported as
46.6%.

It was therefore a **correctness** mechanism that made **more requests**, not
fewer: one call per batch rather than one call per paper. Removing it did not
remove parallelism or add per-paper work; it removed a merge step. On its own it
cannot explain a 4.6× slowdown, and the measurement below confirms it didn't:
the current single-call path is as fast as the historical figure.

(The batching machinery is still present — `backend/app/engine/batching.py` —
so it can be reused if the provider's handling of large requests makes batching
worthwhile again. That is a follow-up experiment, not a conclusion: see §D.)

### Likely causes of the regression

1. **A pathologically slow model in the default fallback chain.** Measured
   2026-10-30: `gemini-3.8-flash` answered a one-line prompt in 53.6 s, failed to
   answer another inside 240 s, and in production needed **720 s, 1 129 s,
   2 451 s and 8 400 s** for single marking calls. It was the first entry of
   `DEFAULT_PRIMARY_MODELS` and remained in every chain as the fallback.
2. **The page-scaled timeout made escalation expensive.** `120 s + 15 s/page`
   gives a 41-page paper ~12 min per attempt; `grade_with_model` allowed two
   attempts per model and the job allowed three attempts, so one 503 could
   commit an hour to a doomed model.
3. **"High demand" retried after ~2 s.** The archive shows four full attempts,
   each diagnosed `HIGH_DEMAND`, re-run about two seconds apart, because the
   circuit breaker had no cooldown recorded when the parent asked. That burns
   the job's attempt budget during a storm instead of waiting for capacity.
4. **Reasoning tokens dominate a call.** A 41-page marking call spent
   **13 817 of its 49 530 tokens** on thinking; "What is 17 × 23?" cost 149
   reasoning tokens to answer. This is not a bug, but it is the largest
   controllable cost in the call.

None of these is "the new architecture is slower". The engine's healthy path is
unchanged; what changed is what happens when the provider pushes back.

---

## B. Performance comparison (measured)

### B.1 Archive: 35 recorded jobs (`data/app.db`, 26–29 Oct)

| Metric | Value |
|---|---|
| Jobs | 35, all `COMPLETED` |
| **Fastest (a completely healthy job)** | **109 s** |
| Median | 693 s |
| Mean | 2 367 s |
| p95 | 8 507 s |
| Maximum | 11 490 s |

The healthy job, second by second: prepare 4.0 s, student PDF compressed
3.3 → 2.7 MB in 3.3 s, scheme compressed 8.1 → 4.3 MB, upload 3.5 s, mark
scheme read 0.0 s (cache hit), **marking with `gemini-3.6-flash` 97.4 s**, save
0.4 s.

Time attributed to model calls across all 35 jobs:

| Model | Time in calls | Role |
|---|---|---|
| `gemini-3.6-flash` | 8.15 h | primary |
| `gemini-3.8-flash` | **6.67 h** | fallback, entered only after a primary failure |

A fallback that consumes 45% of the primary's compute — and is entered only
because a transient error occurred — is the defect. Individual jobs show it
plainly: 8 400 s, 7 408 s, 2 451 s + 720 s in single-model call buckets.

### B.2 Live: the real 41-page fixture, today

`data/fixtures/real/Student Answer.pdf` (15 pages) + `Answer Sheet.pdf`
(26 pages), production prompt, production schema, temperature 0:

| Configuration | Marking call | Total tokens | Reasoning tokens | Marks |
|---|---|---|---|---|
| Default (thinking on) | **139.3 s** | 49 530 | 13 817 | 78 |
| `thinking_budget = 0` | **63.8 s** | 37 366 | 0 | 77 |

139 s for a full paper matches the historical 130 s. Disabling thinking halves
it, but the two runs did not produce the same result: the total moved by one
mark and the subquestion decomposition differed (31 vs 35 entries). **That is
why thinking is opt-in, not the default.**

### B.3 Live: reference-paper benchmark, 16 items

`backend/scripts/benchmark_marking_config.py` (artefact
[`MARKING_CONFIG_BENCHMARK.json`](MARKING_CONFIG_BENCHMARK.json)); the marks are
**scheme-derived and not teacher-certified**, so this measures agreement with
the printed scheme:

Three runs were made on different provider conditions; the marks are stable and
the durations are not:

| Run | Default (thinking on) | `thinking_budget = 0` | `thinking_budget = 1024` |
|---|---|---|---|
| 1 | 16/16, 0 MAE, 55.5 s, 5 661 thoughts | 16/16, 0 MAE, **12.5 s**, 0 thoughts | — |
| 2 | 16/16, 0 MAE, 37.1 s, 6 674 thoughts | — | 16/16, 0 MAE, 32.6 s |
| 3 (artefact) | 16/16, 0 MAE, 34.0 s, 6 674 thoughts | 16/16, 0 MAE, 54.4 s, 0 thoughts | 15/16, 0.0625 MAE, 45.1 s |

What this supports, and what it does not:

- **Marks:** with thinking on and with thinking off, every one of the 16 items
  matched the printed scheme in all three runs. A bounded budget of 1 024 was
  **not** safer: it lost one item in run 3.
- **Latency: not attributable from this benchmark.** Run 3 inverted the result
  (54.4 s without thinking against 34.0 s with it) because the provider was
  shedding requests during that run, so a "call duration" there includes
  waiting and key rotation. Only the same-input pair in §B.2 is usable as a
  latency measurement, and it is one paper.
- Therefore no speed-up is claimed for the reference set, and `thinking_budget`
  stays off by default. The honest reason to try it is §B.2 (2.2× on a real
  paper) together with the risk §B.2 also shows (one mark moved).

### B.4 API calls, retries and quota

| Metric | Archive (35 jobs, 26–29 Oct) | Healthy paper today | Where it comes from |
|---|---|---|---|
| Generation attempts per paper | mean 2.46, median 2, p95 6, max 8 | 1 marking call (+ 0 scheme calls when the scheme is cached) | `job_bandwidth.ai_attempts`, stage events |
| Jobs needing more than one attempt | 3 of 35 (8.6%): two needed 2, one needed 3 | — | `correction_jobs.attempts` |
| Uploads per paper | 258 uploads against 384 reuses | 2 (student + scheme), reused across jobs by content hash | `gemini/file_registry.py` counters |
| Tokens per paper | **unavailable** — the ledger postdates these jobs | 49 530 total (22 672 in, 13 041 out, 13 817 thoughts) | `ai_request_log` |
| Failure rate | every job eventually `COMPLETED`; 4 rapid `HIGH_DEMAND` re-runs inside one job | — | stage events |
| Quota consumption | not recorded before this work | 1 generation request + 2 uploads per paper (uploads are not billed like generation) | ledger |

A paper needs **one** generation request when nothing goes wrong, which is the
floor any optimisation has to work from: the recoverable cost is not the number
of calls, it is the calls a failure adds.

### B.5 What could not be measured

- **A 41-page A/B under load.** While this work ran, the provider repeatedly
  returned `503 UNAVAILABLE` for the 41-page request while 2-page requests
  succeeded in 1–3 s. Only one full-paper pair completed (B.2), so the
  full-paper comparison rests on a single paper and is reported as such.
- **Certified accuracy for any configuration.** No reference item is
  teacher-verified (0 of 16), so no arm can claim certified accuracy.
- **Teacher correction rate.** Requires a teacher marking real scripts under
  each configuration; not obtainable by code.

---

## C. Implementation

| File | Change |
|---|---|
| `backend/app/config.py` | `DEFAULT_PRIMARY_MODELS = ("gemini-3.6-flash",)`; `DEFAULT_AUDIT_MODELS` likewise; new `gemini.thinking_budget` (default `None` = unchanged), loaded/saved with validation |
| `backend/app/engine/legacy_marking.py` | `FALLBACK_MODEL_DEADLINE_MS = 240_000` applied to every non-primary model; a rescue model gets one attempt instead of two; `_configured_thinking_budget()` passes the configured budget into the marking request |
| `backend/app/retry_policy.py` | `HIGH_DEMAND_MIN_PARK_SECONDS = 60.0`; a `HIGH_DEMAND` park never retries faster than a minute, and a longer provider cooldown still wins |
| `backend/scripts/benchmark_marking_config.py` | new reproducible benchmark: same papers, prompt, schema and sampling across configurations, through the existing benchmark runner |
| `backend/tests/test_marking_chain_budget.py` | 14 tests pinning the chain, the rescue deadline, the single rescue attempt, the park floor, and the thinking plumbing/round-trip |
| `backend/tests/test_model_migration.py` | the default-chain assertion updated to the measured decision (its intent — no aliases, no lite models — unchanged) |
| `backend/tests/conftest.py` | the provider circuit breaker is now reset per test; it is process-global, so a test that simulated a 503 used to bench the model for later tests |

Deliberately **not** changed: the marking prompt, the output schema, the
verification pipeline (`blueprint`, `result_guard`, `mark_integrity`), the
double-check logic, and every accuracy guard. Nothing about *how* marks are
decided was touched.

### Why these changes are safe

- The default chain now names the model the installation already marks with in
  practice; removing the slow model removes only the escalation, not a working
  path. A deployment that wants a fallback can name one explicitly, and it now
  gets a bounded rescue deadline.
- The rescue deadline (4 min) is far above a healthy 41-page call (139 s), so a
  working fallback is unaffected.
- A `HIGH_DEMAND` park still fits inside the existing 15-minute parking budget
  and still defers to a longer provider cooldown.
- `thinking_budget` defaults to `None`, i.e. exactly what the application sent
  before this change. Turning it on is a deliberate act with a measurement
  attached.

---

## D. Recommendation

**What caused the slowdown:** a transient-error path that escalated a 503 on
`gemini-3.6-flash` to `gemini-3.8-flash`, which needs 12–140 minutes per paper,
with a page-scaled patience that let each escalation run to that length, twice
per model and again per job attempt — plus a "high demand" park that retried
after two seconds. The unified pipeline is not the cause and the Large PDF
Worker's removal did not remove throughput.

**What improved it:** removing the slow model from the default chain and giving
any explicitly configured fallback a four-minute, single-attempt rescue budget;
making a shed request wait a minute instead of two seconds. Healthy papers were
already ~109–139 s and are unchanged; the gain is that a provider storm now
parks and recovers instead of turning into a 10–140 minute job.

**What remains slower than the historical version:** when the provider sheds
large multimodal requests, a job still waits for capacity. That is a provider
condition, not a code path — measured directly today (the 41-page request was
refused repeatedly while small requests succeeded).

**Is further optimisation worth it?** Two candidates, in order:

1. **Thinking budget.** Up to 4.4× on short papers, 2.2× on a real one, with a
   one-mark change on the single full paper measured. Worth pursuing through
   `benchmark_marking_config.py` plus a teacher review of real scripts, then
   enabling `thinking_budget` per deployment. Do not enable it blind.
2. **Batching large papers** (`engine/batching.py` still exists). If the
   provider keeps shedding 41-page requests, several smaller calls that can be
   retried individually may be more robust than one large one — the historical
   Large PDF Worker existed for a related reason. This needs the same
   measurement discipline before any code is written.

Flow control (C.1) was not addressed: the archive shows queue waits of hours
(`start` bucket 2.39 h across 35 jobs, one job waiting 8 008 s before starting),
which is a scheduling and key-capacity question rather than a correction-engine
one.

---

## E. Deployment readiness

| Check | Result |
|---|---|
| Backend suite (`pytest backend/tests -q`) | **2 539 passed, 1 skipped** (12 m 03 s) on the final tree |
| Desktop suite (`dotnet test`) | **335 passed, 0 failed** (3 m 31 s) |
| Frontend build (`npm run build`) | **exit 0**, 3.09 s (no frontend change) |
| Code graph | `graphify update .` rebuilt |
| Manual correction | untouched path; `grade_with_model` keeps its signature and failure semantics |
| Marking accuracy | prompt, schema, verification and guards untouched; the only behavioural lever (thinking) is off by default |
| Student data | this document contains no student names, marks or submissions; the full-paper A/B worked on the repository's own fixture and only aggregates are published |
| Teacher use | a teacher sees the same marks from the same model; failures now park instead of marking 20× slower with a different model |

Rollout note: `data/config.toml` on this machine already pins
`primary_models = ["gemini-3.6-flash"]`, which is now also the built-in default.
An installation that currently relies on the old default chain will simply stop
escalating to `gemini-3.8-flash`; if it wants a fallback it should name one, and
it will be bounded by the rescue deadline.
