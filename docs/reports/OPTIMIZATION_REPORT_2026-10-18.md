# Optimization pass — 2026-10-18

A second, evidence-based simplification pass over MATH_CHECKER_V5. The first
pass (2026-10-12) is recorded in [SIMPLIFICATION_REPORT.md](SIMPLIFICATION_REPORT.md).
This one follows the same rule: **a smaller codebase is only an improvement if
it remains equally capable and at least as correct**.

Every number below was measured in this checkout. Items that were not measured
are labelled **not measured** rather than assumed. Nothing in the marking
engine's scoring policy, prompts, verification passes or safeguards was removed.

---

## 1. Baseline (recorded before changes)

| Metric | Baseline |
|---|---|
| Backend application Python | 134 modules, 56,875 lines (`backend/app`, excl. `__pycache__`) |
| Backend tests | 2083 passed, 490.57 s (Python 3.14.7) |
| Frontend source | 25,730 lines; 38 Node tests passing |
| Frontend production bundle | `index.js` 678.10 kB (183.63 kB gzip) + CSS 50.38 kB (10.29 kB gzip) |
| App import time | ~0.89 s warm (`import app.main`) |
| Runtime requirements | 15 packages |
| Tracked repository files | 15,667 (15,201 under `MathChecker_Distribution/`) |
| Unique API method+path pairs | 266 |
| Long-document plan, 40-page student + 12-page scheme | 4 PDF opens, 14.2 ms warm |

The fixed mathematical evaluation set is the existing mocked/synthetic suite:
`test_subquestion_scheme.py`, `test_mark_scheme_*`, `test_missing_questions_and_continuation.py`,
`test_engine_parity.py`, `test_result_guard.py`, `test_double_check.py`,
`test_determinism_blueprint.py`, `test_large_pdf*`. No live Gemini or Classroom
credentials were available, so no live re-marking was performed in this pass.

---

## 2. Audit inventory

### 2.1 Duplicated logic (10 categories from the brief)

| # | Finding | Classification | Action |
|---|---|---|---|
| 1 | Four identical percentage helpers: `remark._pct`, `result_guard._pct`, `submission_validation._pct`, `double_check.percentage_of` | Proven duplication | Consolidated into `result_guard.percentage_of` |
| 2 | Five private `_data_dir()` wrappers plus one direct `config.DATA_DIR` read | Proven duplication of a correctness-sensitive rule | One `config.data_dir()`; callers updated |
| 3 | `config._parse_boundaries` vs `automation/store._parse_boundaries` | Near-duplicate, but **different semantics** (config clamps 0–100 and drops empty grades; store does not) | Left alone — merging would silently change stored legacy boundary handling |
| 4 | `_job_public`/`list_jobs`/`retry_job` in correction, automation and large-PDF layers | Same names, different tables/contracts | Left alone — not shared behaviour |
| 5 | Retry/backoff logic in `retry_policy`, `automation/runner`, `gemini/key_manager` | Overlapping concepts, distinct scopes (job policy vs workflow step vs key cooldown) | Left alone |
| 6 | `_parse_boundaries` in automation/grading vs engine/grading grade lookup | Different layers, both tested | Left alone |

### 2.2 Unused code and obsolete abstractions

| Finding | Evidence | Action |
|---|---|---|
| `backend/app/distributed/bridge.py` (72 lines) | Zero imports/callers in app, tests, scripts or frontend; `DISTRIBUTED_REPORT.md` itself says the hook was left **un-wired** | Removed |
| `submission_validation._pct` | Defined, never called | Removed |
| `planner._forced_batching()` | Constant `return False`; condition could never be true | Removed |
| `components.tsx` `MetricCard`, `Ring` | No references in `frontend/src` or `frontend/tests` | Removed |
| Unused `api.ts` wrappers (`agentDiscovery`, `setAgentEnabled`, `fetchSchemeSummary`, …) | No current caller | **Kept** — they describe endpoints of disabled/hidden features that are deliberately preserved |

### 2.3 Dependencies

Every runtime requirement was checked for a real import: fastapi, uvicorn,
python-multipart, google-genai, google-auth, google-auth-oauthlib, requests,
openpyxl, reportlab, pymupdf, pypdf, Pillow, cryptography. All are used.

`pytest` and `httpx` are imported **only** by `backend/tests/` (Starlette's
TestClient needs httpx). They were being installed into the bundled runtime and
shipped to teachers. Moved to `backend/requirements-dev.txt`.

### 2.4 Repeated file processing

`large_pdf.planner.prepare` opened the mark scheme twice (page count, then full
text analysis) and the student PDF twice (page count, then per-page question
evidence) for every long-document job. The mark-scheme analysis was also redone
for every student in the same class.

### 2.5 Frontend

The shell imported all 25 page modules eagerly, so opening the app transferred
every page's code before the first screen. No page duplicated another page's
behaviour; the remaining repeated `setInterval` calls have deliberately different
cadences and data sources and were left untouched.

### 2.6 Repository size and generated artifacts

`MathChecker_Distribution/` holds 15,201 tracked files (~1.4 GB), including
`build/`, `portable/`, `release_staging/`, `release/` and `previous_builds/`.
`docs/PROJECT_STRUCTURE.md` explicitly classifies `previous_builds/` as
deliberate rollback material and warns that a release build replaces the live
pipeline folders. **No distribution artifact was deleted or rebuilt in this
pass**; this is reported as remaining size debt, not silently "cleaned".

---

## 3. Changes made

### 3.1 Backend

| File | Change | Reason |
|---|---|---|
| `distributed/bridge.py` | Deleted | No caller; documented as never wired |
| `config.py` | Added `data_dir()` | One live accessor for the runtime data root |
| `api/routes_all.py`, `api/setup_routes.py`, `api/privacy_routes.py`, `large_pdf/routes.py`, `api/product_routes.py`, `workers/pool.py`, `engine/service.py` | Use `config.data_dir()`; six local wrappers removed | Removes duplicated correctness rule (frozen import vs live path). `service.py` now also resolves retention/temp paths live, matching its own cleanup code |
| `engine/result_guard.py` | Added `percentage_of()`; internal `_pct` replaced | One definition of the engine percentage |
| `engine/remark.py`, `engine/submission_validation.py`, `engine/double_check.py` | Use the shared helper; dead `_pct` removed; `double_check.percentage_of` re-exported | No behaviour change; `double_check.percentage_of` remains importable |
| `engine/pdf_inspect.py` | Added `scheme_evidence()` with a file-identity cache; `expected_question_numbers()` delegates | One read of a mark scheme; derived evidence reused |
| `large_pdf/planner.py` | One read per PDF; constant-false helper removed | Removes duplicate parsing and dead indirection |

The scheme-evidence cache keeps only `(question ids, page count)` — no page text
or block geometry — for at most 16 files. It is keyed by **absolute path, size and
`mtime_ns`**, so a replaced or edited scheme is analysed again, and a failed
analysis is never cached. There is no time-based expiry and no cross-file key.

### 3.2 Dependencies / packaging

- `backend/requirements.txt`: runtime only (15 → 13 packages).
- `backend/requirements-dev.txt`: `-r requirements.txt` + `pytest` + `httpx`.
- `setup.bat` installs the dev file for a source checkout.
- README and `docs/PROJECT_STRUCTURE.md` updated; the canonical release path
  still installs `requirements.txt` only (`build_runtime.py` unchanged).

### 3.3 Frontend

- Route-level code splitting in `App.tsx`: the shell, sign-in, setup wizard and
  AI landing page stay eager; the other 23 pages load on first visit inside one
  `<Suspense>` boundary.
- A failed chunk load (a tab left open across an in-place upgrade) triggers one
  reload, rate-limited to once per minute via `sessionStorage`.
- Removed the two unused presentational components.

### 3.4 Tests added

`backend/tests/test_pdf_evidence_cache.py` (4 tests) pins:
one analysis per unchanged file; a replaced file is analysed again; a failed
read is not cached; `planner.prepare` invokes each read path exactly once.

---

## 4. Before / after measurements

| Metric | Before | After | Evidence |
|---|---|---|---|
| Backend application Python | 134 modules / 56,875 lines | 133 modules / 56,811 lines | `find`/`wc`; net −64 lines, of which 72 are a deleted dead module |
| Backend tests | 2083 passed / 490.57 s | 2088 passed / 498.36 s | pytest (5 tests added, no failures) |
| Frontend source | 25,730 lines | 25,734 lines | `wc` (App.tsx +66/−25 for lazy routing, components.tsx −37 for dead components) |
| Initial JS bundle | 678.10 kB / 183.63 kB gzip | 239.70 kB / 75.16 kB gzip | Vite build |
| Initial JS+CSS (raw) | 728.5 kB | 290.1 kB | Vite build |
| Browser first load (measured transfer) | not measured before | 240,168 B JS + 50,677 B CSS + visited page chunk; FCP 116 ms | DevTools performance entries |
| Runtime requirements | 15 | 13 | requirements files |
| Test-only packages no longer shipped | — | 5.7 MB on disk (pytest, httpx, httpcore, h11, pluggy, iniconfig) | `du` in the dev venv |
| PDF opens per long-document plan | 4 (scheme ×2, student ×2) | 2 cold, 1 warm | Instrumented `fitz.open` |
| `planner.prepare` warm | 14.2 ms | 7.9 ms | Same synthetic 40-page student + 12-page scheme |
| Unique API method+path pairs | 266 | 266 | FastAPI route walk |
| App import (warm) | 0.89 s | 0.88 s | `import app.main` ×3 |
| Tracked repository files | 15,667 | 15,667 | unchanged; distribution artifacts deliberately untouched |

The PDF timings use a **synthetic text-layer PDF**. Real scanned schemes cost
more in `analyse_document`, so the absolute saving per class will be larger than
the synthetic figure, but that has **not** been measured on real papers here.

---

## 5. Correctness and safety review

Deliberately unchanged (no edit in this pass):

- marking prompts, model order/fallback, sampling, JSON extraction, clamping;
- mark-scheme extraction, subquestion structure, M/A/B allocation, blueprint
  validation and caching;
- deterministic verification, double-check trigger/combine rules, result guard,
  overwrite protection, submission-quality validation;
- manual overrides, remark/version history, audit and discrepancy records;
- job persistence, recovery, cancellation, retries, idempotency records;
- Classroom auth/publishing and report generation;
- authentication, CSRF, path containment, CORS and release allowlisting.

The helper consolidations are byte-equivalent formulas. The `data_dir()` change
does alter one thing by design: paths are now resolved at call time everywhere
(several call sites already did this). In production the value is unchanged; the
effect is that tests/overrides/reloads cannot write to a stale path.

---

## 6. Validation results

- Backend suite: **2088 passed**, 2 warnings, 498.36 s. That is the 2083
  baseline tests plus 4 new PDF-evidence tests and 1 new `data_dir()` test.
  After one final defensive edit to the analysis-failure path, the affected
  files were rerun: 84 passed (`test_pdf_evidence_cache`, `test_large_pdf*`,
  `test_config_loading`).
- Frontend `tsc -b`: clean.
- Frontend `npm run build`: succeeded; chunk list in §4.
- Frontend Node tests (`node --test "tests/*.test.ts"`): 38 passed.
- Browser check (local uvicorn, fresh data dir): all 19 routes opened through
  the lazy chunks with HTTP 200; headings matched every page. The only console
  errors were the expected `403` responses for developer-only endpoints while
  signed in as a teacher, and the Prompts page correctly said
  "This page is Developer-role only."
- Portable distribution: **not rebuilt**. `docs/PROJECT_STRUCTURE.md` marks a
  release build as destructive to live pipeline folders; the release safeguard
  tests (`test_release_*`, `test_portable_launch_and_stop`) are part of the
  backend suite. A full portable build + installer verification remains
  unverified in this pass.
- Live Gemini / live Google Classroom: **not tested** — no credentials.

---

## 7. Remaining debt and risks

1. **Repository size.** ~1.4 GB of tracked release artifacts. The documented
   retention policy forbids ad-hoc deletion; reducing it needs a deliberate
   decision about whether `build/`, `release_staging/`, `release/` and
   `previous_builds/` belong in git at all.
2. **`_parse_boundaries` duplication.** Two implementations with different
   clamping. Merging needs a migration decision for rows already stored.
3. **Dependency lock.** Python requirements are minimum-version ranges, not a
   lockfile; a fresh install can resolve newer versions than the tested venv.
4. **`data/` is untracked and 733 MB in this checkout.** Not touched, but it is
   the largest local directory.
5. **Lazy chunks after an upgrade.** Mitigated by one reload, not eliminated;
   an offline client that cannot fetch the new chunk still sees an error.
6. **Live marking accuracy.** Not re-measured against real papers in this pass;
   the mocked evaluation suite is the evidence available here.

## 8. Untested claims

- "Class-wide PDF work is faster" — only the synthetic single-plan measurement
  was taken; the per-class saving is inferred, not measured.
- "Portable runtime is smaller" — the 5.7 MB package saving is measured in the
  dev venv, not in a rebuilt bundled runtime.
- No claim is made about Wall-clock speedups for Gemini calls; none were changed.
