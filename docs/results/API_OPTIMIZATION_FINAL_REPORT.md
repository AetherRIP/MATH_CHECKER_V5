# API efficiency, quota and benchmark work — what was implemented and measured

Date: 2026-10-30
Repository: `AetherRIP/MATH_CHECKER_V5`, branch `main`
Production model: `gemini-3.6-flash`
Companion documents: [`API_OPTIMIZATION_BASELINE.md`](API_OPTIMIZATION_BASELINE.md)
(the definitions every number here uses) and
[`../API_QUOTA_AND_RETRY.md`](../performance/API_QUOTA_AND_RETRY.md) (the reference for
quota, retry ownership and the ledger).

This document reports only what was built and what was observed. Where a number
could not be obtained it says so instead of estimating, and the model sweep
below carries **no accuracy claim** because the benchmark still refuses to score
without teacher-certified references.

---

## 1. Scope

Implemented in this phase:

1. **The usage ledger is connected to the real send path.** Every provider
   request a correction makes now records tokens, duration, model, stage,
   attempt and outcome, and job token totals come from that ledger.
2. **A model capability registry**, which keeps "the listing showed it" apart
   from "this installation actually called it", with explicit, reversible
   approval.
3. **Adaptive worker concurrency** (AIMD) shared through the database, and a
   single, explicit retry owner.
4. **Config honesty**: five switches that advertised features which did not
   exist were deleted; two that now have implementations were kept.
5. **A live candidate-model sweep**, reproducible from a script, reported without
   over-claiming.

Deliberately **not** implemented (documented as deferred, and no longer
advertised by a config switch): stable cache-prefix ordering, an inference-reuse
cache for identical marking, deterministic maths with SymPy, media-resolution
tuning, and an Interactions adapter. See §9.

## 2. The change, commit by commit

| Commit | Subject | What it does |
|---|---|---|
| `b61fa2b` | Add the per-request cost ledger and the teacher-verified accuracy benchmark | `gemini/telemetry.py`, `gemini/usage.py`, `ai_request_log` schema, the benchmark package (`dataset`, `metrics`, `runner`, `store`, `reports`), `/api/benchmark`, desktop benchmark surface |
| `4928f56` | Record what every Gemini request actually cost, on the request itself | Wires `_record_generate` / `_record_upload` into `KeyAwareClient`; job token totals from the ledger; 15 tests |
| `935756b` | Tell a listed Gemini model from one this project can actually call | `gemini/model_registry.py`; registry fed by real requests and by `models.list`; `/api/usage` + desktop Usage page; 12 tests |
| `440e2f3` | Slow the pool down when the provider pushes back, and make one retry owner | `workers/concurrency.py`; `pool` uses the shared limit; SDK retries pinned to one attempt; 10 tests |
| `ab5f00a` | Delete the config switches that never switched anything | removes `math_equiv_enabled`, `reuse_identical_marking`, `cache_prefix_order`, `media_resolution`, `gemini.endpoint` |

## 3. Measurement is now connected (the central defect)

Before this phase `gemini/telemetry.py` existed and nothing called it from a
production path, so the reserved token columns were always empty. Now:

- `KeyAwareClient.generate_content` records one row per provider request that
  actually left the process, parsing `response.usage_metadata` via
  `gemini/usage.py`. A failed request is a row too, otherwise failure rate and
  retry overhead are invisible.
- `_FilesNamespace.upload` records uploads. A **reused** upload sends nothing and
  writes no row, so reuse stays on the existing bandwidth counters rather than
  being counted as a request that never happened.
- Job/stage/prompt/scheme attribution travels in `telemetry.call_context`, so no
  new parameter was threaded through `legacy_marking.py`.
- `api_stats` fills the reserved job token-total columns from
  `telemetry.job_totals(job_id)` — the ledger is the authority, so a retried job
  cannot double-count.
- Every write is best-effort. A statistics failure cannot fail a correction.

Live evidence that this works, from the sweep in §4: the production model's two
probes produced **four** ledger rows — two successful requests and two failed
attempts where a key entered cooldown and the proxy retried on another key. A
model that needed no failover produced exactly two rows for two probes.
Request counting is therefore measured, not asserted.

## 4. Live candidate-model sweep

Method (`backend/scripts/sweep_models.py`, artifact
[`MODEL_SWEEP_RESULTS.json`](MODEL_SWEEP_RESULTS.json)):

- Runs through `KeyAwareClient`, i.e. the production path, so the sweep's own
  rows exercise the ledger, the registry and the request budget.
- Refuses to run in mock mode (a sweep there would measure nothing) and without
  `--ack INFERENCE`.
- Two probes per model: a plain text reply, then the same OpenAPI-subset
  `response_schema` shape the marking path sends, validated field by field.
- 90 s per probe, one SDK attempt, and the cross-check comes from
  `ai_request_log` rather than from the script's own bookkeeping.
- If the plain probe fails, the schema probe is **not sent** — the schema
  question stays "not tested" instead of being answered "false" for a model that
  never replied.

### 4.1 Results (90 s deadline)

| Model | Access | Plain latency | Plain tokens (in/out/thoughts = total) | Schema latency | Schema valid | Schema tokens |
|---|---|---|---|---|---|---|
| `gemini-3.6-flash` (production) | verified | 3.9 s | 12/1/100 = **113** | 10.8 s (2 key failovers) | yes | 52/30/540 = **622** |
| `gemini-3.5-flash` | verified | 2.1 s | 12/1/169 = **182** | 3.1 s | yes | 52/37/350 = **439** |
| `gemini-3.5-flash-lite` | verified | 5.3 s | 12/1/– = **13** | 6.8 s | yes | 52/53/– = **105** |
| `gemini-3.1-flash-lite` | verified | 1.2 s | 12/1/– = **13** | 1.1 s | yes | 52/38/– = **90** |
| `gemini-3.7-flash` | **failed** — 504 DEADLINE_EXCEEDED at 88.7 s | — | not reported | not sent | — | — |
| `gemini-3.8-flash` | **failed** — 504 at 110.4 s (3 attempts) | — | not reported | not sent | — | — |
| `gemini-4-argon` | **failed** — 429 RESOURCE_EXHAUSTED in 0.3 s | — | not reported | not sent | — | — |
| `gemini-3.1-pro-preview` | **failed** — 429 RESOURCE_EXHAUSTED in 0.3 s | — | not reported | not sent | — | — |

Verified: **4 of 8**.

### 4.2 The two slow models, re-probed with a 240 s deadline

A 90 s cut-off is a deadline, not proof that a model cannot answer, so the two
timeout failures were re-probed:

| Model | Result at 240 s |
|---|---|
| `gemini-3.7-flash` | plain probe **answered** in **238 s** (12/1/113 = 126 tokens); schema probe 504 at 239 s |
| `gemini-3.8-flash` | both probes 504 at ~240 s |

One earlier direct-SDK probe (outside the sweep, not in the artifact) answered
`gemini-3.8-flash` in **53.6 s** and `gemini-3.6-flash` in **2.0 s** for the same
one-line prompt. Latency for this model therefore varies by an order of
magnitude across runs, which is itself the finding: it cannot be a marking model
whose per-page timeouts assume minutes, not tens of minutes.

### 4.3 What the sweep establishes

1. **Four models are callable from this account**, and four are not right now.
   The two 429s are a quota state (`RESOURCE_EXHAUSTED`), not evidence that those
   models can never be used; `key_manager` treats a `daily`-scope quota failure
   with a 30–60 minute cooldown precisely so a key is not retried into a wall.
2. **Reasoning tokens dominate cost on the production model.** Emitting the two
   characters `OK` cost 113 tokens (12 in, 1 out, **100 thoughts**); a small
   schema-constrained answer cost 622 (52 in, 30 out, **540 thoughts**). The
   `-lite` models reported no thinking tokens at all (13 and 105 total). This is
   the first time the application can see that split; it was invisible before
   the ledger existed.
3. **A cheaper model is not a better model.** `-lite` models are the cheapest per
   probe, and the baseline already records that a `-lite` model awarded full
   marks to a blank question in a live run. Price without certified accuracy is
   not a reason to switch production.
4. **Failover really happens.** The production model's plain probe is 3.9 s
   *including* two failed attempts where a key entered cooldown first. The
   reliability machinery is not theoretical; it is in the ledger as failed rows.

### 4.4 What the sweep does **not** establish

- **No accuracy comparison.** `benchmark_accuracy.py` exits 4 — no provider
  request — while no reference item is teacher-certified (0 of 16). See
  [`TEACHER_CERTIFIED_ACCURACY_REPORT.md`](TEACHER_CERTIFIED_ACCURACY_REPORT.md).
- **No per-paper cost.** The probes are one line of text, not a 15-page script
  plus a 26-page scheme, so per-page latency, long-context handling and the cost
  of a real correction remain unmeasured.
- **No cost in currency.** Token counts are reported; prices are not verified
  here, so no money figure is implied.

## 5. Reliability work

### 5.1 One retry owner, made explicit

`client_proxy.generate_content` already had its own eight-attempt loop and set no
SDK retry options, so an SDK default could have multiplied it. `_http_options()`
now pins `types.HttpRetryOptions(attempts=1)` on every client the proxy builds,
including the text-generation client, so "how many times was this sent" is
answerable from one file. A test drives the real SDK client and asserts the
recorded attempt count is 1.

### 5.2 Adaptive concurrency

`workers/concurrency.py`: `worker.concurrency` is a **ceiling**. A throttling
outcome (`RATE_LIMIT`, `HIGH_DEMAND`, `TRANSIENT_PROVIDER_ERROR`) halves the
limit to a floor of 1; five successes after a 120 s hold grow it by one. State
lives in `app_meta` (`worker.concurrency_control`), so a worker child's 429 slows
the parent's dispatching within 5 s, and a restart mid-outage does not recreate
the storm. A non-capacity failure — a malformed paper, a 400 — never shrinks the
pool.

Two defects were found and fixed while building this, both by tests rather than
by review:

- `limit()` initially treated a temporarily low ceiling as if it were a throttle
  reduction, so the pool stayed clamped at 1 after unrelated tests lowered the
  ceiling. The state model was rewritten so a low ceiling is never remembered as
  a provider problem.
- The controller's in-memory state is process-global, and the shared test fixture
  reset every other singleton but not this one. One test's simulated 429 then
  throttled the next test's pool and two real-concurrency tests failed with
  `assert 1 == 2`. The fixture now installs a fresh controller per test; the
  assertions were not weakened, and the tests pass.

### 5.3 Quota classes

`errors.quota_scope()` separates `per_minute` from `daily`; a daily exhaustion
gets a 30–60 minute key cooldown instead of a per-minute retry, and
`gemini/preflight.py` maps the same distinction before a key is used. A 429 during
the sweep is visible in the ledger with its own `error_class`.

## 6. Config honesty

Seven switches documented behaviour that did not exist. Five were removed, and
`config.py` no longer claims them:

| Removed switch | What it promised | Reality |
|---|---|---|
| `gemini.math_equiv_enabled` | deterministic maths equivalence | `engine/math_equiv.py` does not exist |
| `gemini.reuse_identical_marking` | reuse identical marking | no implementation |
| `gemini.cache_prefix_order` | cache-friendly attachment order | no implementation |
| `gemini.media_resolution` | media-resolution tuning | no implementation |
| `gemini.endpoint` | an alternative endpoint | no Interactions adapter exists |

Kept, because both are now real: `gemini.usage_telemetry` (read by
`telemetry._enabled`, so `false` genuinely stops writes) and
`gemini.adaptive_concurrency` (read by `concurrency._enabled`). Retention
(`usage_retention_days`, `usage_max_rows`) is read by the pruner.

## 7. Verification

| Check | Command | Result |
|---|---|---|
| Backend suite | `.venv/Scripts/python.exe -m pytest backend/tests -q` | **2 525 passed, 1 skipped** |
| Desktop suite, run 1 | `dotnet test desktop/MathChecker.Desktop.slnx` | **335 passed, 0 failed** |
| Desktop suite, run 2 | same | **335 passed, 0 failed** |
| Frontend build | `npm run build` (in `frontend/`) | **exit 0**, 90 modules, `dist/` emitted |
| Code graph | `graphify update .` | rebuilt: 19 490 nodes, 45 189 edges |

### 7.1 Backend suite

The first full run of this phase reported **2 failed, 2 523 passed, 1 skipped**.
Both failures were `TestRealConcurrency` in `test_worker_pool.py`, caused by the
concurrency-state leak described in §5.2 — a real isolation defect in the new
code, not a flaky assertion. After the fixture fix the two affected files passed
(21 passed) and the **full suite was re-run on the fixed tree: 2 525 passed, 1
skipped, in 11 m 51 s**.

### 7.2 Not re-run

- **End-to-end harness** (`backend/scripts/e2e_check.py`) needs a running backend
  on a port and performs real marking against live Gemini. It was not re-run in
  this phase, so no e2e claim is made here.
- **Desktop flake from the baseline** (309 in one archived run, 294 + 1 failure +
  test-host crash in another) did **not** reproduce: two consecutive runs both
  passed 335/335. It therefore remains an open observation rather than a fixed
  bug, and this document does not claim it was diagnosed.

## 8. Numbers that remain unavailable

| Metric | Why, and what would change it |
|---|---|
| Certified accuracy | 0 of 16 reference items are teacher-certified; the benchmark refuses rather than scoring 0 % |
| Accuracy of any candidate model | Same reason; a sweep cannot substitute |
| Cost per correction in currency | Tokens are measured; pricing is not verified here, so no figure is given |
| Per-question token cost | A paper is one request; apportioning tokens per question would be an invention |
| Cache-hit rate | No `cached_content_token_count` was reported in any sweep row, so the rate is unavailable, not zero |
| Per-page latency | The probes are text-only; a real paper sends page images |
| Real per-paper token cost | Requires a live marking run with the ledger on; that is now possible but was not part of this phase |

## 9. Deferred, with the reason

| Deferred item | Why it is not implemented now |
|---|---|
| Stable cache-prefix ordering (scheme before student pages) | Sits directly on the marking path; needs an equivalence proof on real papers first. No sweep row observed a cached-token count, so there is no evidence yet that the ordering would pay. |
| Inference-reuse cache for identical marking | Correctness requires provable input identity and dependency-based invalidation; neither exists |
| Deterministic maths (`engine/math_equiv.py`, SymPy) | Engine decided (SymPy), code not written; adding the dependency first would be an unused import |
| Media-resolution tuning | No measurement yet showing savings at the accuracy marking needs |
| Interactions adapter | No adapter, and no evidence marking needs one |
| Thinking-budget control on the production model | **New finding, not implemented**: 100–540 reasoning tokens for trivial replies suggests a real lever, but capping thinking on a marking model changes marks and must be validated against certified accuracy first |

## 10. What a teacher or operator can do next

1. Teacher-certify the reference items (Accuracy page) — this is the only path to
   a real accuracy number and to any model comparison on accuracy.
2. Run one live correction with the ledger on, then read `/api/usage`: the
   `ledger` block reports tokens, p50/p95 latency and per-stage splits for
   per-correction numbers. That is the measurement this phase made possible.
3. Consider a candidate model only through `model_registry.promote`, which
   refuses without certified evidence and can be rolled back.
