# Math Checker V5 — System Audit Report

**Date:** 2026-10-16
**Scope:** full end-to-end audit of the existing project — test, diagnose, fix, re-verify.
**Checkout:** `/e/MATH_CHECKER_V5` (branch `main`, app version 5.2.1, build 20261013-rc1)
**Runtime used:** `.venv/Scripts/python.exe` (Python 3.14.7), pytest 9.1.1, Node v24.19.0

---

## A. Overall status

**The application is in good condition and is ready for its intended use in the areas that were
verifiable here.** The backend regression suite is fully green (1894 passed, exit 0), the app
launches through its real startup path and serves its interface and API, the live-publication
safety gate provably blocks every publishing helper, and the stored results in the real database
satisfy their integrity invariants.

Three genuine defects were found and fixed. **None was critical** — no defect caused wrong marks,
wrong student matching, lost results, an unintended upload or data corruption. The most serious was
a test-hygiene defect that made the suite write a test account id into a **tracked** file in the
checkout.

**Not verified in this audit (see E):** anything requiring a live Google Classroom session. No OAuth
token exists in this checkout, so live contact would need your interactive sign-in. Every Classroom
assertion below is against mocked transports and the guard, not against Google.

| Check | Result |
|---|---|
| Backend regression suite | **1894 passed, 0 failed, 2 third-party warnings, exit 0** |
| Frontend type check (`tsc -b`) | **exit 0** |
| Frontend regression tests (`node --test`) | **9 passed, exit 0** |
| App launch via real startup path | **relay + backend up, SPA loaded, 28/28 core routes HTTP 200** |
| Publication guard (runtime) | **6/6 publishing helpers blocked, 0 write egress** |
| Path traversal | **refused** (SPA shell only; secret file never served) |
| Real-database integrity sweep | **0 violations across 157 results** |
| Release staging privacy scan | **3833 files, no findings** |
| Shipped portable archive | **0 secrets / DBs / tests / docs / student data** |
| Production data untouched | **`app.db` + `secrets.toml` byte-identical before/after** |

---

## B. Coverage

### Examined (static inspection)

- **Backend application:** 131 modules imported with **0 failures**; `compileall` clean over
  `backend/app`, `backend/scripts`, `backend/tests`.
- **Route surface, 30 router modules:** `routes_all`, `tracking_routes`, `automation/routes`,
  `scheme_routes`, `product_routes`, `report_routes`, `student_routes`, `setup_routes`,
  `privacy_routes`, `auth_routes`, `access_routes`, `data_routes`, `large_pdf/routes`,
  `lifecycle/routes`, `distributed/routes`, `audit`, `repair`.
- **Engine:** `legacy_marking`, `blueprint`, `verify`, `service`, `option_inventory`,
  `scheme_audit`, `scheme_structure`, `mark_scheme*`.
- **Pipeline:** `jobs/manager`, `workers/pool` + `adapter` + `child`, `automation/{store,runner,
  scheduler,detection,grading,states}`, `large_pdf/{store,runner,scheduler}`, `retry_policy`,
  `gemini/{key_manager,client_proxy}`, `testing_guard`, `reporting/*`, `classroom/*`.
- **Distribution:** `release_allowlist`, `scan_release`, `build_release`, `verify_installer`,
  `verify_portable`, the built `release/` archive.
- **Shipped-copy drift:** `diff -rq backend/app` against `MathChecker_Distribution/release_staging/backend/app`
  and `.../portable/MathChecker_Portable/backend/app` → **identical except `reporting/assets/`**,
  which is deliberately excluded (see the "not defects" note in C).
- **Targeted hazard greps:** `shell=True` (0), hardcoded absolute paths (only the two standard
  `C:\Program Files\cloudflared\` lookups), raw HTTP verbs (**only** `classroom/client.py`).

### Test types executed

| Level | What ran |
|---|---|
| Static | compileall, 131-module import sweep, hazard greps, shipped-copy diff, tsc type check |
| Unit + integration | full `backend/tests` suite (90 files, 1894 tests), plus targeted module re-runs |
| Independent data check | read-only SQL invariant sweep over the real `data/app.db` |
| Runtime security | guard proof driving every publishing helper with a rigged transport |
| End-to-end | real launch (`main.py`), relay + backend, browser-loaded SPA, 28 API routes |
| Distribution | `scan_release` over staging, archive entry analysis of the shipped zip |
| Regression | 1 new test added and proven to fail without its fix |

### Not tested / unverified

- **Live Google Classroom (read-only or otherwise)** — blocked, no OAuth token (see E).
- **Marking accuracy against human-verified marks** — no reference marking set exists here.
- **Windows installer execution** (`MathChecker_Setup.exe` was not run).
- **Interactive UI flows** — page load and API responses were verified; clicking Retry, bulk
  upload, and similar interactions were verified only at the API/test level, not by browser
  automation.
- **Multi-machine distributed deployment** — out of scope for a single checkout.
- 3 modules (`api_stats`, `audit`, `repair`) were inspected and exercised by suite tests but not
  driven through their own end-to-end flows in this pass.

---

## C. Bugs discovered and fixed

### C1 — The test suite wrote into a tracked file in the checkout (Medium)

**Root cause.** `distributed/agent_control.agent_dir()` returns the **repository's** `agent/`
folder by default (next to the standalone `agent.py` and its `device_id`), and
`backend/tests/conftest.py` never set the module's own `MC_AGENT_DIR` override. Any test exercising
`/api/distributed/agent/state` therefore called `save_state()`, which wrote
`<checkout>/agent/agent_state.json` regardless of the per-test data directory.

Reproduced deterministically:

```
$ pytest tests/test_sync_monitor.py      → 11 passed, exit 0
$ git diff --stat -- agent/agent_state.json
 agent/agent_state.json | 4 ++--        ← rewritten with a test account id
```

**User-visible impact.** Every full test run left the checkout dirty. `agent/agent_state.json` is
**tracked by git**, so a developer could commit a test account id as the installation identity; the
suite was not hermetic and silently contradicted the guarantee in
`test_agent_control_and_multi_role.py`'s own docstring ("nothing is written to a real
installation"). It also meant the suite could not be used to prove the checkout was clean.

**Files.** `backend/tests/conftest.py` (missing isolation), `backend/app/distributed/agent_control.py`
(`agent_dir`, unchanged — its default is intentional), `backend/tests/test_sync_monitor.py:185` and
`backend/tests/test_multi_account_mode.py:234` (the paths that triggered it).

**Fix.** The autouse `fresh_state` fixture now points `MC_AGENT_DIR` into that test's temporary data
directory. This confines the write for the whole suite at its source rather than patching the two
known call sites, and a test that needs its own agent directory still overrides it afterwards.
No production behaviour changed.

**Regression test.** `test_agent_state_write_is_confined_to_the_test_dir`
(`test_agent_control_and_multi_role.py`) deliberately does *not* set `MC_AGENT_DIR` itself, so it
exercises the conftest default. It asserts both halves — the tracked file is byte-identical **and**
the state was really persisted elsewhere, so it cannot pass by the write simply not happening.
**Verified it bites:** with the conftest line removed the test fails (`AssertionError: save_state()
modified the tracked file …`, exit 1); with the fix it passes.

**Also repaired:** `agent/agent_state.json` was restored to its pre-audit content (the change was a
side effect of my own run, not of the user's work).

---

### C2 — The frontend regression suite could not be run as documented (Medium)

**Root cause.** The test's own header said to run it with `node --test tests/`. On Node v24.19.0 a
bare directory argument is not resolved by the test runner:

```
$ node --test tests/
Error: Cannot find module 'E:\MATH_CHECKER_V5\frontend\tests'
✖ tests (96ms)   pass 0   fail 1   exit 1
```

The 9 tests inside it are fine — the *documented command* was wrong, and the file's comment was the
only in-repo instruction.

**User-visible impact.** The project's single frontend regression suite reported a failure no matter
what the code did. A developer would reasonably conclude the frontend tests were broken and ignore
or delete them, losing the only coverage for the large-PDF poller (terminal states, backoff,
stop/restart, the `AWAITING_REVIEW` terminal case).

**Files.** `frontend/tests/largePdfPolling.test.ts` (header comment).

**Fix.** The header now documents `node --test "tests/*.test.ts"` — the glob is resolved by Node
itself, so the identical command works in `cmd.exe` and in a POSIX shell. The root README's
single-file form (`node --test --experimental-strip-types tests\largePdfPolling.test.ts`, run from
`frontend/`) was **verified working** before changing anything and was left alone.

**Verification.** `node --test "tests/*.test.ts"` → **9 passed, exit 0**; the previously documented
form still fails on Node 24, and the README form was re-confirmed green via `cmd.exe`.

---

### C3 — Invalid escape sequence in a test file (Low)

**Root cause.** `backend/tests/test_report_attachment_api.py:440` used `'/\:*?"<>|'`; `\:` is an
invalid escape sequence.

**Impact.** Python emits `SyntaxWarning: "\:" is an invalid escape sequence` on every compile —
console noise on every run, and a **hard `SyntaxError` in a future Python release** (the sequence is
slated to become invalid), which would break the suite outright. The assertion itself was still
checking the intended character set, so behaviour was correct today.

**Fix.** Made it a raw string (`r'/\:*?"<>|'`), which preserves the exact intended characters.

**Verification.** `python -W error::SyntaxWarning -m py_compile` → clean; the file's 19 tests still
pass.

---

### Inspected and found *not* to be defects

Recording these so they are not re-raised as bugs later:

- **`backend/app/reporting/assets/` is absent from both packaged trees.** Deliberate and
  documented in `release_allowlist.py`: those files are the build owner's personal logo and
  signature; a distributed build renders a drawn monogram and the teacher configures their own.
  `design._bundled_asset()` tolerates absence (returns `""`) and only opt-in config
  (`[reports] use_bundled_assets`) would ever consult it — and it is never shipped.
  `backend/tests/test_branding_neutrality.py` covers this.
- **`/api/classroom/status` reporting "OAuth client JSON missing"** in my run was correct: the app
  was pointed at an isolated data directory with no credentials. The real `data/secrets/` **does**
  hold the client secret, and `config.resolve_oauth_client_path()` accepts Google's own
  `client_secret_*.json` download name as well as `google_oauth_client.json` — so onboarding works
  without renaming the file.
- **The 2 suite warnings are third-party deprecations** (`httpx`/`starlette.testclient`, and
  `google-genai`'s `_UnionGenericAlias`), not app code.

---

## D. Test results (actual counts)

### Automated — backend

| Run | Result |
|---|---|
| Baseline (before any change) | **1893 passed, 2 warnings, exit 0** — 466.93 s |
| After all fixes | **1894 passed, 2 warnings, exit 0** — 476.19 s |
| Failures / errors | **0** |
| Delta | +1 = the new regression test |

Targeted re-runs after each fix (all exit 0):

| Suite | Result |
|---|---|
| `test_sync_monitor` + `test_agent_control_and_multi_role` | 35 passed |
| + `test_multi_account_mode`, `test_device_discovery`, `test_agent_config` | 132 passed |
| `test_report_attachment_api` (after the raw-string fix) | 19 passed |
| Regression test against the un-fixed conftest (expected to fail) | **1 failed, exit 1** |

### Automated — frontend

| Check | Result |
|---|---|
| `node --test "tests/*.test.ts"` | 9 passed, exit 0 |
| `npx tsc -b` (type check) | exit 0, no diagnostics |

### Static

| Check | Result |
|---|---|
| `compileall` over app/scripts/tests | exit 0 (one SyntaxWarning — C3, since fixed) |
| Import sweep, 131 modules | 0 failures |
| Shipped copies vs `backend/app` | identical except the deliberately-excluded assets |

### Runtime, against the real running app

| Check | Result |
|---|---|
| Launch via `main.py` (relay-supervised backend) | boot recovery, worker pool, schedulers, lifecycle monitor all started |
| 28 core API routes | 28/28 HTTP 200 |
| SPA + hashed assets served | 200 (`/`, `/assets/index-*.js`, `.css`, favicon) |
| Developer-only routes as `role=teacher` | 403 `developer_only` (`/api/dev/prompts`, `/api/diagnostics/logs`, `/api/diagnostics/db`) |
| Path traversal (3 variants) | refused — 400-byte SPA shell returned, secret file never served |

### Mocks vs real external services

- **Mocked/offline:** all Classroom and Drive behaviour, the marking engine's provider calls, the
  publication guard proof, key rotation, the whole regression suite.
- **Real, but local:** the app process, its HTTP server, its SQLite database, the report/PDF code
  paths, the release archive on disk.
- **Real external service:** **none.** No Google API call was made during this audit.

### Independent integrity sweep (real `data/app.db`, opened read-only)

| Invariant | Violations |
|---|---|
| Awarded marks exceed available marks | **0** |
| Marks awarded where available is missing/≤0 | **0** |
| `percentage` inconsistent with `earned/available` (>1 pt) | **0** |
| Duplicate results for the same submission (idempotency) | **0** |
| Rows inspected | 157 results across 7 coursework, 66 tables |

### Distribution

| Check | Result |
|---|---|
| `scan_release.py release_staging` | **3833 files, no findings, exit 0** |
| Shipped `MathChecker_Portable.zip` (161 app entries, runtime excluded) | secrets **0**, DB files **0**, app tests **0**, `docs/` **0**, `.git`/`.venv`/`node_modules` **0**, student data **0**; `Data/` holds only `config.toml.example` + `README.txt` |

---

## E. Remaining risks

1. **Live Classroom integration is unverified in this audit.** No OAuth **token** exists in this
   checkout (only the client secret), so the app correctly reported `connected:false` and live
   contact would require your interactive Google sign-in. Everything asserted about Classroom
   writes here is proven against the guard and mocked transports — **a passing mocked test is not
   evidence that the live integration works.** This is the single largest gap in this report.
2. **Marking accuracy has no human-verified reference set here.** Invariants (marks ≤ available, totalled
   consistently, correctly associated) hold, but "is this mark *right*?" was not measurable. A prior
   session recorded a real instability on a live paper (Q9 marked 0 on one run and 2 on another).
   **That remains the top accuracy risk** and is the thing I would chase first.
3. **No browser automation of interactive UI flows.** The page loads and the API responds; clicking
   Retry / Approve / bulk-upload was verified at API and test level only.
4. **`MathChecker_Setup.exe` was not executed** — installer behaviour (shortcuts, `%LOCALAPPDATA%`
   data paths, uninstall) is unverified in this pass. The staging tree and portable archive were
   scanned and are clean.
5. **156 pre-existing uncommitted changes** (from earlier sessions) remain in the working tree,
   including deletions under `previous_builds/` and modified release artifacts. They are **not mine**
   and I left them exactly as found. Nothing in the audit depends on them.
6. **~200 stale temporary correction reports remain in Drive** from earlier sessions. Deliberately
   not touched — they cannot be reliably distinguished from real data, so they are reported for
   manual review rather than swept.
7. **The publish endpoint probe did not reach the guard.** A `POST` to
   `/api/classroom/assignments/{id}/publish` returned `400 classroom_not_connected` because this
   isolated environment has no Classroom credentials — refused *before* the guard, and nothing left
   the machine. The guard itself is proven separately (F) and by `test_testing_only_guard.py`.
8. **Distributed multi-machine deployment** (schools, device linking, update delivery) was exercised
   only by its own test suite, not across two real machines.

---

## F. Data-safety confirmation

**No live Classroom write was attempted, because no live Classroom connection existed.** The app
reported `connected:false` throughout; every Classroom call in this audit ran against rigged
transports or mocks.

How student-facing writes were prevented, in layers:

1. **No credentials.** No OAuth token in the checkout → no authenticated session to write with.
2. **Isolated data root.** Every process ran with `MC5_DATA_DIR` pointed at a throwaway directory;
   the real `data/` was never the app's data dir.
3. **Testing-only mode armed** (`[testing] only = true`) in the isolated config, confirmed at runtime:
   `/api/settings` returned
   `testing = {'only': True, 'reason': 'Testing only mode is ON: …'}`.
4. **Runtime proof, not a promise.** `dev_local/audit/guard_proof.py` loaded the app's own guard
   against that armed config, replaced every outbound transport with a recorder, and drove 6
   publishing helpers:

   | Result | Value |
   |---|---|
   | Publishing helpers driven | 6 |
   | **Blocked** | **6/6** |
   | **Write-phase egress attempts** | **0** |
   | Reads still allowed | yes (1 expected read attempt) |

   Blocked: `upload_report_to_drive`, `replace_drive_file_content`, `share_drive_file`,
   `delete_drive_file`, `POST …studentSubmissions/{id}:return`, `PATCH …studentSubmissions/{id}`
   (grade). Exit 0.
5. **Structural check.** `classroom/client.py` is the **only** module in the app issuing raw HTTP
   write verbs (`requests.post|patch|put|delete`); every write there is behind `@_guarded_write` or
   goes through `classroom_request`, which refuses anything non-GET. The `report_attachment` and
   `managed` publishing paths route through it.

**Was any original data modified?** No.

```
$ sha256sum -c data_hashes_before.txt
data/app.db: OK
data/secrets.toml: OK
```

Recorded before any test ran and re-checked after every run, including the end-to-end launch.

**Test-only artifacts removed:** the throwaway E2E data directory (`dev_local/audit/e2e_data`,
containing only a generated test `config.toml` and throwaway DB), and the raw run logs (now
consolidated under `docs/results/audit/`). **No leftover listeners** on the audit ports and **no
stray processes** (the app was stopped with its full process tree — 8 processes).

**Retained deliberately:** the recoverable checkpoint and the untracked-file archive
(`dev_local/audit/`, gitignored), and the evidence under `docs/results/audit/` (`docs/` is excluded
from every shipped build by the release allowlist).

**Left untouched, reported for manual review:** `data/` (real DB, secrets, blueprints, results), and
the ~200 pre-existing Drive temp reports.

---

## G. Final instructions

### Launch the application

```bash
# Windows
start.bat                      # first time: setup.bat

# Linux / macOS
./start.sh                     # default http://127.0.0.1:8642
./start.sh 9000                # custom port

# Directly
python main.py                 # or: python main.py 9000
```

### Run the tests

```bash
# Backend regression suite (from backend/)
python -m pytest tests -q

# Frontend regression tests (from frontend/) — Node resolves the glob itself
node --test "tests/*.test.ts"

# Frontend type check + production build (from frontend/)
npx tsc -b
npm run build
```

> The frontend suite deliberately has no `npm test` script. Use the command above; a bare
> directory argument (`node --test tests/`) is **not** accepted by Node 24.

### Run the app safely against a throwaway data root

```bash
mkdir -p /tmp/mc5_sandbox
printf '[testing]\nonly = true\n' > /tmp/mc5_sandbox/config.toml
MC5_DATA_DIR=/tmp/mc5_sandbox python main.py 8799
```

`[testing] only = true` blocks every grade write, report upload/attachment and return at the HTTP
chokepoint, while reads (courses, rosters, submissions, mark schemes) keep working.

### Build and verify the distributable

```bash
# Full build (Windows)
build_release.bat

# Or directly
python MathChecker_Distribution/scripts/build_release.py

# Verify the staged tree for secrets / private data
python MathChecker_Distribution/scripts/scan_release.py MathChecker_Distribution/release_staging

# Verify the installer / portable output
python MathChecker_Distribution/scripts/verify_installer.py
python MathChecker_Distribution/scripts/verify_portable.py
```

### Evidence produced by this audit

| File | Contents |
|---|---|
| `docs/results/audit/baseline_suite.log` | full suite before any change (1893 passed, exit 0) |
| `docs/results/audit/final_suite.log` | full suite after all fixes (1894 passed, exit 0) |
| `docs/results/audit/guard_proof.py` | the runtime publication-guard proof (re-runnable) |
| `docs/results/audit/guard_proof_result.json` | its output: 6/6 blocked, 0 egress |
| `docs/results/audit/e2e_launch.log` | real launch: relay, boot recovery, SPA, route calls (local-only transcript; `*.log` is ignored) |
| `docs/results/audit/data_hashes_before.txt` | production-data hashes for the before/after check |

Suite transcripts (`baseline_suite.log`, `final_suite.log`, `e2e_launch.log`) are
local evidence only and are not present in a clean checkout; the re-runnable
scripts and JSON/txt evidence above are tracked.

### Files changed by this audit

| File | Change |
|---|---|
| `backend/tests/conftest.py` | isolate `MC_AGENT_DIR` per test — fixes C1 |
| `backend/tests/test_agent_control_and_multi_role.py` | regression test for C1 |
| `backend/tests/test_report_attachment_api.py` | raw string — fixes C3 |
| `frontend/tests/largePdfPolling.test.ts` | corrected documented run command — fixes C2 |

Nothing was committed, as requested. The pre-audit checkpoint is
`08deb325a040408850e398f2c0bd043f0d6175ed` (`git stash create`; the working tree was never
modified by it), with untracked files archived at `dev_local/audit/untracked_backup.tar.gz`.
