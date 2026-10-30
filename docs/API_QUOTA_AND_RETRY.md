# API, quota and retry behaviour

Reference for how Math Checker 5 spends Gemini quota, who retries, and what the
application measures. Written after the measurement and reliability work of
2026-10-30; every statement below points at the module that implements it, and
nothing here is a recommendation.

If you only read one paragraph: **one correction is one multimodal request per
marking stage**, the proxy in `gemini/client_proxy.py` is the only thing that
retries, and every request that leaves the process writes one row to
`ai_request_log` with the tokens, the duration and the outcome.

---

## 1. What a correction sends

| Request | Where | Counted by | Reused when identical |
|---|---|---|---|
| Mark-scheme chunk extraction | `engine/scheme_inventory.py` | `call_audit` (`scheme_extraction`) | scheme-hash cache |
| Marking | `engine/legacy_marking.py` | `call_audit` (`marking`) | never |
| Independent double-check | `engine/double_check.py` (off by default) | `call_audit` (`double_check`) | never |
| Targeted recovery | `engine/recover_missing.py` | `call_audit` (`recovery`) | never |
| Files upload | `gemini/client_proxy.py` | `call_audit` (`upload`) | content SHA-256 registry, TTL |

A marking request carries **every page as an image plus the whole scheme**. That
is why tokens, not request count, is the quantity worth watching: one request
per paper is already minimal, but a request can carry 40 page images.

Uploads are the only requests that are genuinely avoidable, and
`gemini/file_registry.py` already avoids them: the baseline recorded 384 reuses
against 258 uploads.

## 2. The one retry owner

`gemini/client_proxy.py` owns every retry:

- `_MAX_ATTEMPTS = 8` — the proxy's own attempt loop, which may rotate to
  another API key between attempts.
- `SDK_RETRY_ATTEMPTS = 1` — passed to `google-genai` through
  `_http_options()`. The SDK retries inside the HTTP layer when it is given
  retry options, and its default is a policy this project does not control; a
  future SDK bump that defaulted to three attempts would silently turn one
  logical retry into 24 provider requests. Pinning it to one attempt means
  "how many times was this sent" is answerable from one file.

Outside the proxy, job-level retries are bounded by the job's own budget
(`correction_jobs.max_attempts`, 3) and classified by `retry_policy.py`.

## 3. Failure classification and what each class does

`retry_policy.FAILURE_CLASSES` has thirteen request classes plus two
orchestration states (`API_UNAVAILABLE`, `WORKER_CRASH`). The policy table
(`_POLICY`) decides three things per class: retryable, how many job-level
retries, and whether to **park** instead of spending attempts.

| Class | Retryable | Job retries | Parks |
|---|---|---|---|
| `TRANSIENT_PROVIDER_ERROR` | yes | 2 | no |
| `RATE_LIMIT` | yes | 2 | **yes** |
| `HIGH_DEMAND` | yes | 2 | **yes** (≥ 60 s) |
| `API_UNAVAILABLE` | yes | 2 | **yes** |
| `TIMEOUT` | yes | 1 | no |
| `NETWORK_ERROR` | yes | 1 | no |
| `AUTHENTICATION_ERROR` | **no** | 0 | no |
| `INVALID_REQUEST` | **no** | 0 | no |
| `CONTEXT_TOO_LARGE` | **no** | 0 | no |
| `MODEL_ERROR` | yes | 1 | no |
| `PARSING_ERROR` | yes | 1 | no |
| `VALIDATION_ERROR` | yes | 1 | no |
| `UNKNOWN_ERROR` | yes | 1 | no |
| `WORKER_CRASH` | yes | 2 | no |

Parking is the important one: a capacity problem waits for capacity instead of
burning the job's attempt budget, so three 503s do not fail a paper. A
`HIGH_DEMAND` park never retries sooner than `HIGH_DEMAND_MIN_PARK_SECONDS = 60`,
because the recorded jobs used to re-run a shed request about two seconds later
and spend the attempt budget on a provider that was already refusing work; a
longer provider cooldown still wins.

A deployment that configures more than one marking model gives every non-primary
entry a rescue budget of 4 minutes and a single attempt (`legacy_marking`,
`FALLBACK_MODEL_DEADLINE_MS`), so a slow model cannot hold a job for the length
of the page-scaled timeout.

Backoff is jittered exponential (`BACKOFF_BASE_SECONDS = 2`,
`BACKOFF_GROWTH = 2`, `BACKOFF_CAP_SECONDS = 30`, jitter 0.8–1.2), and a
provider `Retry-After` is honoured up to `MAX_RETRY_AFTER_SECONDS = 900`.

## 4. Per-minute quota vs an exhausted day

Both arrive as HTTP 429 and they need opposite responses, so they are
distinguished before any cooldown is set:

- `errors.quota_scope()` reads the provider's quota failure details and returns
  `per_minute` or `daily`.
- `gemini/key_manager.py` applies `DAILY_QUOTA_COOLDOWN_FLOOR_SECONDS = 1800`
  and `DAILY_QUOTA_COOLDOWN_CAP_SECONDS = 3600` for `daily`, because a
  per-minute cooldown would retry a key that cannot work again until the quota
  resets.
- `gemini/preflight.py` maps the same distinction to `QUOTA_EXHAUSTED` versus
  `RATE_LIMITED` before a key is used at all.

A per-model circuit breaker (`gemini/provider_health.py`) sits in front of the
send: while a model is cooling after `HIGH_DEMAND` failures, the request is
**not sent**, and `ProviderCoolingError` tells the caller to park or try the
next model in the chain. The gate is per model on purpose — a 503 on one model
says nothing about the others.

## 5. The per-correction budget

`gemini/call_audit.py` allows `DEFAULT_MAX_REQUESTS = 12` generation attempts per
correction and counts **before** the send, so the budget stops a storm before a
request leaves the process. Requests are attributed to the job that caused them;
a request without a job (the API's in-process mark-scheme recheck) is not
charged. The baseline recorded a median of 2 and a maximum of 8 attempts per
paper, and the budget has never been hit.

## 6. Adaptive concurrency

`workers/concurrency.py` makes `worker.concurrency` a **ceiling** rather than a
fixed target:

| Rule | Value |
|---|---|
| Shrink | halve the limit on `RATE_LIMIT`, `HIGH_DEMAND`, `TRANSIENT_PROVIDER_ERROR` |
| Grow | +1 after 5 successes, and only once 120 s have passed since the last reduction |
| Floor | 1 |
| Shared state | `app_meta` key `worker.concurrency_control`, adopted by other processes within 5 s |
| Persistence | survives a restart, so restarting mid-outage does not recreate the storm |

A non-capacity failure (a malformed paper, a 400) never shrinks the pool, and a
ceiling that is simply low is never remembered as a provider problem.

## 7. What is measured, and where

`gemini/telemetry.py` writes one row per provider request that actually left the
process into `ai_request_log`:

| Column | Meaning |
|---|---|
| `kind` | `generate` or `upload` |
| `job_id`, `stage`, `purpose` | attribution, from `call_audit` and `telemetry.call_context` |
| `model`, `endpoint` | what answered |
| `input_tokens`, `output_tokens`, `cached_tokens`, `thoughts_tokens`, `total_tokens` | the provider's own `usage_metadata` |
| `duration_ms`, `attempt`, `error_class`, `http_status` | latency and outcome |
| `upload_count`, `reused` | upload sizing |
| `outcome`, `estimated`, `created_at` | result label and whether any number is an estimate |

Rules that hold for every row:

- **No student content.** No prompt text, no answer, no name, no key. A row
  carries identifiers, counts, a model name, a duration and a label.
- **Nothing invented.** A field the provider did not report stays NULL and the
  row keeps `estimated = 0`. `usage.estimate()` exists but no correction path
  calls it, so an unavailable measurement is never replaced by a plausible one.
- **Never fatal.** Every write is best-effort; a statistics failure cannot fail
  a correction.
- **Bounded.** Retention defaults are 30 days and 20 000 rows
  (`DEFAULT_RETENTION_DAYS`, `DEFAULT_RETENTION_ROWS`), pruned opportunistically
  every 50 inserts.
- **Deletable.** `gemini.usage_telemetry = false` stops all writes.

Aggregates (`telemetry.summary`) report p50/p95 latency, cache hit rate,
per-model and per-stage splits, and per-correction averages — plus an
`unavailable` list naming every figure that could not be computed, so a zero is
never mistaken for "measured nothing".

## 8. Reading it

- Backend: `GET /api/usage` returns `ai` (per-job request counts from
  `api_stats.ai_usage_summary`), `ai.ledger` (the token/latency summary above),
  `ai.models` (the model registry snapshot) and `concurrency` (the controller's
  state).
- Desktop: **Usage → AI requests per correction** and the AI-provider-traffic
  line. An unmeasured job says nothing rather than "0 requests".
- Per job: `telemetry.job_totals(job_id)` and `telemetry.for_job(job_id)`.

## 8a. Reasoning (thinking) budget

`gemini.thinking_budget` controls the marking call's reasoning budget. It is
`None` by default, which sends exactly what the application has always sent.
Measured on 2026-10-30 (see `docs/results/CORRECTION_PERFORMANCE_RECOVERY.md`):
on the reference papers no mark moved with thinking off, while a real 41-page
call halved (139 s → 64 s) and its total moved by one mark. Reproduce with
`backend/scripts/benchmark_marking_config.py`; enable per deployment only after
a teacher checks real scripts marked under it.

## 9. Model registry

`gemini/model_registry.py` keeps two facts apart:

- **discovered** — the model listing returned the model. Proves the name exists,
  nothing about whether this account may call it.
- **verified** — a real request from this installation succeeded.

Promotion to production requires teacher-certified benchmark evidence and an
explicit actor, is recorded with the previous configuration, and is reversible
(`rollback`). Nothing is promoted automatically, and a faster model is not a
reason to promote anything: see `docs/results/TEACHER_CERTIFIED_ACCURACY_REPORT.md`.

## 10. Deliberately not implemented

These are documented as deferred rather than half-built. `config.py` no longer
exposes a switch for any of them:

| Idea | Why it is deferred |
|---|---|
| Stable cache-prefix ordering (scheme before student pages) | Sits on the marking path; needs an equivalence proof on real papers before adoption |
| Inference-reuse cache for identical marking | Correctness depends on provable input identity; no dependency-based invalidation exists yet |
| Deterministic maths (`engine/math_equiv.py`, SymPy) | Decided (SymPy), not implemented; adding the dependency before the code would be an unused import |
| Media-resolution tuning | No measurement yet showing per-page savings at the accuracy required |
| Interactions endpoint adapter | No adapter exists and no evidence it is needed for marking |
