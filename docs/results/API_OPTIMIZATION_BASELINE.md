# API optimization baseline — measured before any change

Date: 2026-10-29
Repository: `AetherRIP/MATH_CHECKER_V5`, branch `main`
Application: Math Checker 5 (WPF desktop + Python backend + React frontend)
Production model: `gemini-3.6-flash` (`data/config.toml`, `primary_models = ["gemini-3.6-flash"]`)

This document records what the application **already does**, what it **already
measures**, and what it **cannot measure**. Every number below was read from
existing artefacts (the SQLite database, archived test logs, source); nothing
in this document is an estimate unless it says so.

The purpose is to make later claims checkable: any "before/after" comparison in
`API_OPTIMIZATION_FINAL_REPORT.md` uses the definitions established here.

---

## 1. Existing architecture (as found)

```
Google Classroom / manual upload
        │
        ▼
backend/app/api/routes_all.py ── creates ONE correction job per submission
        │                        (jobs/manager.py, SQLite `correction_jobs`)
        ▼
workers/pool.py ── leases one healthy API key per job, spawns a child process
        │           (workers/child.py → workers/adapter.py)
        ▼
engine/service.py ── prepares inputs
        │            · downloads (Drive) with reuse counters
        │            · pdf_optimize.py (source-SHA cache)
        │            · uploads via gemini/client_proxy.py (content-addressed)
        ▼
engine/legacy_marking.py ── ONE multimodal generate_content per paper
        │                   contents = [student…, scheme, question?, prompt]
        │                   config = system_instruction + response_schema
        ▼
engine/blueprint.py → engine/verify.py → engine/result_guard.py
        │            → engine/mark_integrity.py → engine/double_check.py (off)
        ▼
results table ── one row per job; teacher review in result_reviews
```

### Request flow (facts)

- A correction is **one generation call** carrying every page as an image plus
  the whole mark scheme. `call_audit` records the stages that exist.
- Optional second passes: independent double-check, targeted recovery
  (`engine/recover_missing.py`), and (off by default) the audit pass.
- Mark-scheme extraction (`engine/scheme_inventory.py`) and verification
  (`engine/blueprint.py`, `engine/mark_scheme_verify.py`) are separate calls.
- Uploads: one Files API upload per (key, content SHA-256, purpose). Uploads are
  reused across jobs through `gemini/file_registry.py`.

### Reliability machinery already present

| Mechanism | Module | Behaviour |
|---|---|---|
| Failure taxonomy | `retry_policy.py` | 13 classes; park vs retry vs fail; jittered exponential backoff; `Retry-After` parsing |
| Circuit breaker | `gemini/provider_health.py` | per-model cooldown, half-open probing, persisted in `app_meta` |
| Per-correction budget | `gemini/call_audit.py` | 12 generation attempts, counted before send |
| Key leasing | `workers/pool.py` | one exclusive healthy key per job |
| Persistent attempts | `jobs` table | `attempts` / `max_attempts` survive restart |
| Upload reuse | `gemini/file_registry.py` | verify-before-reuse with TTL |
| Structured output | `legacy_marking.py` | `response_schema` + `response_mime_type` |
| Deterministic guards | `result_guard`, `mark_integrity`, `recover_missing` | totals, evidence, impossible marks |

---

## 2. Baseline measurements

Source: `data/app.db`, opened read-only (`file:data/app.db?mode=ro`).

### Jobs and results

| Metric | Value |
|---|---|
| Correction jobs | 35, all `COMPLETED` |
| Results | 35 |
| Teacher review records | 14 |
| Classroom publish operations | 0 |
| Attempts per job | 32 jobs × 1, 2 jobs × 2, 1 job × 3 |
| `max_attempts` configured | 3 on every job |

### Provider requests per paper

Source: `job_bandwidth` (36 rows, per-attempt `call_audit` snapshots).

| Metric | Value |
|---|---|
| Generation attempts, median | **2** |
| Generation attempts, p95 | **6** |
| Generation attempts, max | **8** |
| Generation attempts, total | 86 |
| Stages recorded | marking 31, double_check 2, upload 2 |

A median of 2 (not 1) reflects the optional independent second check and
targeted recovery rounds, not blind retries. The 12-request budget has never
been hit.

### File uploads

| Metric | Value |
|---|---|
| Uploads performed | 258 |
| Bytes uploaded | 694,593,352 (~662 MiB) |
| Uploads reused (no re-upload) | **384** |

Upload reuse is already doing real work: more reuses than uploads.

### Latency

| Metric | Value |
|---|---|
| Per-request latency | **unavailable** — no duration is recorded anywhere |
| Median / p95 job duration | **unavailable** — `job_stage_events` (1,006 rows) record stage transitions with timestamps but no per-request boundaries |

Historic single-request measurements exist only as prose in `config.py`
(230–440 s for a 15-page scan against a 26-page scheme) and are **not**
re-measured here.

### Tokens, cache, cost

| Metric | Value |
|---|---|
| Input / output / cached / total tokens | **unavailable** — `usage_metadata` appears nowhere in `backend/app` |
| Implicit cache hit rate | **unavailable** (see above) |
| Cost | **unavailable** — depends on tokens and on pricing not verified in this run |

This is the central gap: without token data, no token-saving claim can be made,
and no per-model efficiency comparison is possible.

### Failures and retries

| Metric | Value |
|---|---|
| Failed attempts in the archived log cited by `call_audit` | 3,922 during one provider storm |
| Current failure rate by class | **unavailable from the DB**; `api_stats.by_class` exists but is a lifetime counter with no time window |
| Timeout rate | **unavailable** (no per-request outcome rows) |

### Test and build status (from archived logs, not re-run in this phase)

| Suite | Result | Log |
|---|---|---|
| Backend (`pytest`) | **2411 passed, 1 skipped** | `final-backend.log` (2026-10-28) |
| Desktop (`dotnet test`) | **309 passed** in one run; **294 passed / 1 failed + test-host crash** in a later run | `desktop-suite2.log`, `final-desktop.log` |
| End-to-end harness | **44 passed, 0 failed** | `e2e.log` |
| WPF UI audit | **473 checks passed, 35 journeys** | `ui-audit.log` |

The desktop discrepancy is recorded as an open item: it is either a flake or a
regression and must be diagnosed, not suppressed.

### Benchmark dataset

| Metric | Value |
|---|---|
| Reference items | 16 (2 synthetic papers, 36 available marks) |
| Teacher-verified items | **0** (`manifest.json` → `review.verified_by = null`) |
| Certified accuracy | **unavailable, not 0%** |

---

## 3. Risks discovered

1. **No token telemetry.** The only quantity that can still be optimised —
   tokens per successful correction — is unmeasured. Any efficiency claim
   without it would be fabricated.
2. **Single-model production chain.** `primary_models = ["gemini-3.6-flash"]`.
   The fallback machinery in `legacy_marking.py` exists but has nothing to fall
   back to, so one 503 parks every job until cooldown.
3. **Cache-hostile attachment order.** `contents_base` places the per-student
   files *before* the shared mark scheme. Implicit caching keys on the request
   prefix, so the shared scheme can never be part of a stable prefix. This is a
   plausible free win, but it sits on the marking path and must be proven
   equivalent before adoption.
4. **Deployed timeout drift.** `data/config.toml` sets
   `request_timeout_ms = 180000` while `config.py` documents 600000 as the
   value that stopped mid-answer 504s. `client_proxy.timeout_for_pages`
   compensates (floor + 15 s/page), so large papers are still bounded — but the
   two values disagree and should be reconciled deliberately.
5. **Concurrency is static.** `workers/pool.py` reads
   `cfg.worker.concurrency` once; nothing reduces parallelism when the provider
   starts returning 429/503, so every worker can discover the same outage.
6. **No deterministic maths.** SymPy is not installed or declared; equivalence
   checking for algebraic answers is therefore model-only.
7. **Undocumented architecture.** Source comments cite a "spec §N" that does not
   exist in the repository; there is no API/quota/retry documentation.
8. **`-lite` models are unsafe for marking** (documented in `config.py` from a
   live run: a blank question was awarded full marks). Any candidate-model
   comparison must include this as a negative control.

---

## 4. Proposed changes by phase

| Phase | Change | Why it is justified |
|---|---|---|
| 0 | This document | Makes later claims checkable |
| 1 | Per-request usage ledger (tokens, latency, stage, model, outcome), job token totals, p50/p95 | Unblocks every later measurement |
| 2 | Model capability registry, discovery, governed promotion + rollback | Model knowledge is scattered; promotion is currently informal |
| 3 | Adaptive concurrency; daily-quota vs per-minute handling; single retry owner | Static concurrency amplifies outages |
| 4 | Upload single-flight + expiry-aware reuse; document/page checks | Reuse exists but races and expiry are unhandled |
| 5 | Stable-prefix ordering, prompt inventory, measured output trimming | Caching and context are the remaining token levers |
| 6 | Candidate model sweep on the existing benchmark | Model choice must be measured, not assumed |
| 7 | Deterministic maths (`engine/math_equiv.py`) as review signals | Removes work from the model where code is reliable |
| 8 | Escalation calibration | Avoids a blanket second call per answer |
| 9 | Opt-in inference cache with dependency-based invalidation | Reuse only where correctness is provable |
| 10 | Files/context-cache/batch/Interactions decisions | Must be evidence-based, not speculative |
| 11 | Privacy and prompt-injection hardening | Student content is untrusted input |
| 12 | Backend + WPF + React read models | The desktop must show its own processing state |
| 13–15 | Tests, final report, documentation | Reproducibility |

## 5. Measurements that remain unavailable, and how to obtain them

| Metric | How it will become available |
|---|---|
| Tokens per request/paper/model | Phase 1 usage ledger, read from `response.usage_metadata` |
| Per-request latency and p95 | Phase 1 (duration recorded around each send) |
| Cache hits | Phase 1 (`cached_content_token_count`), Phase 5 (prefix ordering) |
| Failure rate over time | Phase 1 (`outcome`, `error_class`, `http_status` per row) |
| Cost | Phase 1 tokens × pricing verified in Phase 10 (only if verified) |
| Per-question token cost | **Will stay unavailable while a paper is one call**; reported as such rather than apportioned |
| Certified accuracy | Requires a teacher to verify the 16 reference items; not obtainable by code |
