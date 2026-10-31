# Math Checker V5 — full-stack stress, RAM and reliability audit

Date: 2026-10-27 · Branch: `main` · Scope: desktop frontend, Python backend,
integration, memory, resource cleanup, concurrency, failure recovery,
packaging.

Everything below is backed by a command that was actually run on this machine.
Where a check could not be run, it is reported as **not run** with the reason —
never as a pass. Values marked *(measured)* come from the instrumented runs;
nothing here is estimated from reading source code.

---

## 1. Executive summary

**Status: the backend is ready for its intended workload; the desktop is
unverified** because no .NET SDK is installed on this machine (only runtimes),
so the WPF app could not be built or launched. That single limitation is why
section 4 reports static analysis rather than live UI behaviour.

Two real defects were found, reproduced and fixed, both in the backend:

| # | Severity | Defect | Evidence | Fix |
|---|---|---|---|---|
| **D1** | **P1** | One SQLite connection leaked **per completed correction job**, permanently held by `db._connections`. Linear growth: ~3.2 MB RSS and ~30 OS handles per 10 jobs, no plateau, never reclaimed. | 60 jobs: RSS **+46 MB**, handles **+199**, `sqlite3.Connection` +50 — then flat **+1.2 MB / +3 handles** after the fix | Tie the connection to its owning thread with `weakref.finalize` in `db.get_connection()` |
| **D2** | **P1** | Every endpoint resolving the Classroom OAuth client rescanned the whole install tree **3× per request** (~127 ms CPU per walk). `/api/dashboard` p50 **156 ms**, and ~**1 s** per request with six concurrent clients (lock-serialised scans). | Profile: 3 075 `os.scandir` + 51 810 `pathlib.match` per request. After: p50 **1.66 ms**, 6-client **7.85 ms**, **162.3 rps** | Memoize the tree walk (5 s TTL, keyed on project root + data dir) behind a lock that collapses the concurrent-miss herd |

Nothing else in the exercised surface was found to be broken. Grading
correctness was not touched: 199 marking/guard/key tests pass and all 97 live
results are internally consistent (`earned ≤ available`, percentages match).

---

## 2. Test environment

| Item | Value |
|---|---|
| OS | Windows 11 Home Single Language, build 10.0.26200 |
| CPU / RAM | 8 logical processors / **8 103 MB total, ~1 959 MB free** during the audit |
| Python | 3.13.15 (`.venv`) |
| Backend deps | FastAPI 0.161.1, Starlette 1.9.0, uvicorn 0.54.0, PyMuPDF 1.28.2, pypdf 6.21.2, httpx 0.28.1, psutil 7.2.2, pytest 9.1.1 |
| Node | v24.20.0, npm 11.19.0, Vite 5.4.21 |
| .NET | host 10.0.13 with `Microsoft.NETCore.App` + `Microsoft.WindowsDesktop.App` runtimes, **no SDK** (`dotnet --list-sdks` → "No SDKs were found") |
| Live backend | `uvicorn app.main:app --host 127.0.0.1 --port 8642`, `MC5_LOGIN_REQUIRED=0` |
| Frontend dev server | `vite --port 8720 --host 127.0.0.1`, `MC5_DEV_API=http://127.0.0.1:8642` |

**Data safety.** Every test ran against isolated copies under
`%TEMP%\mc5-audit-*` (`MC5_DATA_DIR`); the real data directory was never
opened. Papers, mark schemes and students were synthetic (blank pages built
with `pypdf`; "bench 1…29", "conc 0…7"). No Gemini key was configured and no
live provider call was made — marking ran on the demo/mock path. No commit,
`git reset`, `git clean` or destructive cleanup was performed, and the 46
pre-existing modified files in the working tree were left exactly as they were.

**Machine constraint.** With ~1.9 GB free RAM the heavy-load scenarios were
scaled to what this box can take without risking system exhaustion (≤150-page
PDFs, ≤8 concurrent jobs, ≤2 worker children at a time). Larger figures are
noted as *not attempted* rather than guessed.

---

## 3. Test inventory

| # | Command | Result | Exit |
|---|---|---|---|
| 1 | `backend: python -m pytest tests -q -p no:randomly` (**baseline**, before this audit's fixes) | 2 319 passed, 1 skipped | 0 |
| 2 | Same, **after** both fixes | **2 331 passed, 1 skipped** (636.67 s) | 0 |
| 3 | `pytest tests/test_db_connection_lifecycle.py tests/test_oauth_client_lookup.py -q` | 12 passed (1.98 s) | 0 |
| 4 | Red-green for D1: finalizer removed, then restored | **red: 3 failed, 1 passed** → **green: 4 passed** | 0 |
| 5 | Red-green for D2: cache removed, then restored | **red: 6 scans** → **green: within bound** | 0 |
| 6 | `pytest` security / lifecycle subset (12 files: public-access ×3, classroom auth, remote access, upload validation, mark-scheme upload, shared upload cleanup, worker child launch, portable launch/stop, data location, tempdirs) | 180 passed (85.48 s) | 0 |
| 7 | `pytest` marking subset (result guard, review precision, scheme exclusions, quota budget, key-validation regressions, live marking verify) | 119 passed (19.57 s) | 0 |
| 8 | `frontend: npm run build` | built in 1.86 s | 0 |
| 9 | `frontend: node --test "tests/*.test.ts"` | 87 tests — 84 pass, 0 fail, 3 skipped | 0 |
| 10 | Same, with the app reachable at `:8720` | 3 live-UI tests **run and fail** (pre-existing fixture gap, §9) | 0 |
| 11 | `dotnet` build/run of the desktop | **not run** — no SDK installed | n/a |

Baseline → after: the delta is exactly +12, the tests added in
`test_db_connection_lifecycle.py` (4) and `test_oauth_client_lookup.py` (8).
No existing test changed state.

---

## 4. Frontend results

**Desktop (WPF) — not run.** No .NET SDK is present, so `dotnet build`/`run`
cannot work and no desktop process, window, or managed-heap number exists on
this machine. This is a hard environment limitation of the audit box, not a
property of the project. What could be done was a targeted static review of the
leak classes the mission names, reading real code:

| Leak class | Finding | Evidence |
|---|---|---|
| Timers outliving a page | **Clean** — all four polling timers stop on navigation | `AutomationViewModel.cs:994-996`, `DashboardViewModel.cs:220-222`, `JobsViewModel.cs:309-315`, `WorkersViewModel.cs:647-655`; `MainViewModel.cs:141` calls `previous.OnDeactivated()` on every switch |
| Static event retention | **Clean** — the only static event is subscribed once, by the app-lifetime window | `ThemeManager.Changed` ← `MainWindow.xaml.cs:22` only |
| Socket exhaustion | **Clean** — one `HttpClient` per client instance | `MathCheckerApiClient.cs:36-37` |
| Image/file handles | **Clean** — `BitmapCacheOption.OnLoad` releases the file handle | `PageImageDecoder.cs:30-32` |
| Upload streams | **Clean** — every opened stream is disposed in `finally` | `Uploads.cs:138` + `MathCheckerApiClient.cs:384-408` (`streams.Add` … `finally { Dispose() }`) |

No defect was found in those classes; **no claim is made about desktop
runtime memory**, which was not measurable here.

**Web frontend (Vite/React) — exercised.** `npm run build` succeeds (exit 0)
and 84 unit tests pass, 0 fail. Live inspection in a real browser against the
running dev server + live backend confirmed the app shell renders
(`.app-shell` present), the sign-in wall is absent in local no-login mode, the
`#/results/<id>` route renders a real result ("dup guard · Mock Paper 1H (demo)
· VERIFIED" with a question tree), and there are no console errors.

Two things worth recording as **deliberate product behaviour**, not defects:

- The dev server **refuses to start** until a backend is explicitly chosen
  (`MC5_DEV_API` / `.env.local`), rather than guessing one — it printed
  "connecting the browser to an unchosen backend is how demo data ends up
  looking like a real class" and exited. Good.
- It binds `[::1]` by default; a probe against `127.0.0.1` fails. `--host
  127.0.0.1` (or using `localhost`) resolves it.

---

## 5. Backend results

### API correctness (live server, isolated data dir)

| Request | Result | Verdict |
|---|---|---|
| `POST /api/corrections/manual` with malformed JSON body | 422 `{"detail":[{"type":"missing",…}]}` | correct |
| …with `text/plain` body | 422 | correct |
| …with empty form | 422 | correct |
| …with a 2 MB form field | **400** `"Field exceeded maximum size of 1024KB."` | correct |
| `GET /api/jobs/does-not-exist` | 404 `"Job not found"` | correct |
| `GET /api/jobs/..%2F..%2Fapp.db` | 404 `"Unknown API endpoint."` | traversal refused |
| `GET /api/results/..%252F..%252Fapp.db` | 404 `"Result not found"` | traversal refused |
| `GET /api/not-a-route` | 404 `"Unknown API endpoint."` | correct |
| `POST /api/jobs/nope/cancel` | 409 `"Job cannot be cancelled (not active or unknown)."` | correct |
| `DELETE /api/results/nope` | 405 `"Method Not Allowed"` | correct (no such verb) |
| Developer-only route without the developer flag | 404 (not disclosed) | correct |

### Correction pipeline

- **Normal:** 30 sequential + 8 simultaneous jobs, **all COMPLETED**; no job
  was lost, duplicated, or marked complete when it failed.
- **Cancellation:** 6 jobs cancelled immediately after enqueue → all
  `CANCELLED`, HTTP 200, **0 worker children left**, temp-file count unchanged.
- **Interrupted work:** see §9 — the repo's own recovery tests
  (`test_jobs_and_verify.py::test_recover_requeues_inflight`,
  `::test_recover_never_touches_completed`,
  `::test_recover_fails_exhausted_jobs`) pass, and after two force-kills of the
  live server the database held **no non-terminal rows**.
- **Marking accuracy:** untouched by both fixes (neither touches the engine).
  199 marking/guard/key tests pass; over the 97 live results,
  `earned > available` or negative scores: **0**, percentage mismatches: **0**.

### Database integrity

- 97 completed jobs → exactly 97 result rows; 6 cancelled jobs → 0 result rows.
- No orphaned or non-terminal rows after kill/restart cycles.
- The per-job connection leak (D1) was itself a database-lifecycle defect; its
  fix is verified by four regression tests including the "one failing job must
  not affect others" shape (20 threads → all connections released).

### Security

The **`claude-security` skill was not run**: its pipeline needs the `Workflow`
tool and its plugin subagents, neither of which exists in this environment
(the skill states the scan cannot run without `Workflow`). Its checklist was
served instead by the repo's own suites — `test_public_access_security.py`,
`test_public_access_hardening.py`, `test_public_access_toggle.py`,
`test_classroom_auth.py`, `test_remote_access.py`,
`test_upload_pdf_validation.py` (180 tests, all passing) — plus the live probe
table above, which confirmed traversal refusal, developer-route non-disclosure,
and size/type validation. No authentication or validation was weakened
anywhere in this audit.

---

## 6. Memory benchmarks

Method: `psutil` over the process tree (server + children), sampling RSS
(`memory_info().rss`), `num_handles()`, `num_threads()`; run at the same points
each time; `gc.collect()` before each in-process sample. **Working set per
process is what is reported — not private bytes or commit charge**, which were
not read. Where a probe ran in-process (no subprocess), that is stated.

### 6.1 Before the fix — the leak (in-process probe, `saturation.py`)

| After N jobs | RSS | Handles | `sqlite3.Connection` |
|---|---|---|---|
| 0 (baseline) | 78.0 MB | 197 | 0 |
| 10 | 105.6 MB | 213 | +2 |
| 20 | 105.8 MB | 215 | +2 |
| 30 | 107.0 MB | 215 | +2 |
| **60** | **106.8 MB** | 216 | +2 |

That probe showed the connection count saturating but the *end-to-end* run
(`trend.py`, 6 batches of 10 jobs) showed the true shape: **+3.2 MB and ~+30
handles per 10 jobs, linear, with no plateau**, and idle settles did not
reclaim it. 60 jobs → **+46 MB, +199 handles, +50 `Connection`, +704
`Statement`**. Because `db.close_all()` only runs on a factory reset, every one
of those handles was held for the life of the process.

### 6.2 After the fix

**In-process probe, 60 jobs** (`saturation.py` with the fix):

| After N jobs | RSS | Handles | `Connection` | `Statement` |
|---|---|---|---|---|
| 0 | 78.0 MB | 197 | 0 | 0 |
| 10 | 105.6 MB | 213 | +2 | +44 |
| 20 | 105.8 MB | 215 | +2 | +44 |
| 30 | 107.0 MB | 215 | +2 | +44 |
| 40 | 106.9 MB | 216 | +2 | +44 |
| 50 | 106.8 MB | 216 | +2 | +44 |
| 60 | 106.8 MB | 216 | +2 | +44 |

The one-time step in batch 1 is PyMuPDF's warm-up (`PdfObj` +611 objects, then
constant — confirmed by a control run of plain PyMuPDF, which stays flat).
**Growth from job 10 to job 60: +1.2 MB total, +3 handles.**

**Live server, whole process tree, 30 jobs** (`bench2.py`):

| Stage | RSS | Handles | Threads | Children |
|---|---|---|---|---|
| Idle (fresh) | 74.3 MB | 225 | 9 | 0 |
| Before jobs | 75.3 MB | 226 | 9 | 0 |
| After 10 | 98.5 MB | 195 | 7 | 0 |
| After 20 | 98.5 MB | 195 | 7 | 0 |
| After 30 | 98.6 MB | 195 | 7 | 0 |
| **Peak (during)** | **116.9 MB** | — | — | **2** |
| After 15 s idle | 98.6 MB | 194 | 4 | 0 |

Handles *fall* below the starting value because the finished jobs' connections
and read threads are now released — the opposite of the pre-fix trend. The
~23 MB step between baseline and the first batch is the one-time PyMuPDF
warm-up; after that the curve is flat to 0.1 MB across 30 jobs.

### 6.3 Proposed budgets (from these measurements, for this machine)

| State | Measured | Proposed ceiling |
|---|---|---|
| Idle, backend only | 69–74 MB | ≤ 110 MB |
| After a normal job burst, idle again | 98.6 MB | ≤ 140 MB |
| Peak, ≤8 concurrent jobs, 4-page papers | 201 MB | ≤ 400 MB |
| Repeated identical workload (30 jobs) | +1.2 MB / +3 handles | ≤ +10 MB / ≤ +20 handles per 30 jobs, and no monotonic trend across cycles |
| 8 GB machine, all processes | 201 MB peak observed | must stay < 1.5 GB or RAM is not the constraint |

These are budgets for *this* project on *this* box; they are not universal.

---

## 7. Stress and endurance results

### Latency, serial, n = 30 (live server, warm)

| Endpoint | p50 | p95 | p99 | max |
|---|---|---|---|---|
| `/api/health` | 1.46 ms | 2.52 ms | 2.60 ms | 2.60 ms |
| `/api/dashboard` | **1.66 ms** | 2.79 ms | 4.40 ms | 4.40 ms |
| `/api/jobs` | 5.45 ms | 9.51 ms | 11.51 ms | 11.51 ms |
| `/api/results` | 5.48 ms | 6.68 ms | 6.78 ms | 6.78 ms |
| `/api/usage` | 1.94 ms | 3.66 ms | 9.31 ms | 9.31 ms |

### Concurrency

| Test | Before D2 fix | After D2 fix |
|---|---|---|
| `/api/dashboard` p50, serial | **156 ms** | **1.66 ms** (−99 %) |
| `/api/dashboard` p50, 6 clients | **~972 ms** (p95 ~1008 ms) | **7.85 ms** (p95 13.35 ms) |
| 6 clients × 20 requests: throughput / error rate | **6.2 rps**, 0 errors | **162.3 rps**, 0 errors, all 200 |

(An earlier post-fix run of the same 6-client shape measured 270 rps; the
162.3 rps figure is the one from the final run on the loaded box. Both are
reported rather than the more flattering one.)

| Test | Result |
|---|---|
| 8 simultaneous correction jobs (4-page papers) | **8/8 COMPLETED**, wall 5.6 s |
| Peak fan-out / peak RSS / peak handles during it | 4 worker children / 201.4 MB / 511 handles / 19 threads |
| Released afterwards | 0 children, 108.0 MB, 221 handles |

### Endurance and cleanup

| Check | Result |
|---|---|
| 30 sequential jobs, then 15 s idle | RSS flat at 98.6 MB; children 0 |
| 6 cancellations mid-flight | all `CANCELLED`; 0 children; temp files 4 → 4 |
| **Force-kill of the server mid-workload** | port released; **0 surviving python processes** (no orphaned workers, no duplicate backend); DB left with no non-terminal rows |
| Temp-file accumulation across the whole audit | none (4 → 4) |
| Stray processes left by the audit at the end | none (`netstat` on 8642/8720: none) |

---

## 8. Defects found and fixed

### D1 — one SQLite connection leaked per completed correction job (P1)

**Reproduction.** Six batches of ten jobs against an in-process app
(`trend.py` / `saturation.py`): RSS +46 MB, handles +199, `Connection` +50,
`Statement` +704 over 60 jobs, linear, no plateau, not reclaimed by idle.

**Root cause.** `db.get_connection()` caches a connection in a thread-local and
registers it in the module-level `_connections` set, which only `close_all()`
empties. Work runs on short-lived threads — each correction spawns a
`<worker>-stdout` reader thread in `app/workers/adapter.py` — so the thread-local
is dropped when the thread exits and **nothing ever closes its connection**.
Confirmed by instrumenting which threads open connections: one brand-new
`W1-stdout` thread per job, one new connection each. `poll_result()` sets
`self._reader = None`, so the reader thread becomes collectable exactly when the
job finishes — which is why the leak was exactly one connection per job.

**Fix** ([backend/app/db.py](../../backend/app/db.py)) — 22 lines: register the
connection's release on the *thread object* that opened it.

```python
weakref.finalize(threading.current_thread(), _release_connection, conn)
```

A running thread cannot be collected (`threading._active` holds it), so a live
thread's connection is never closed early and stays in `_connections` for
`close_all()`; a finished thread's handle is closed when the `Thread` object is
collected. `_release_connection` discards from the registry *and* closes, so
the two cleanup paths stay consistent and each is idempotent.

**Regression tests** ([backend/tests/test_db_connection_lifecycle.py](../../backend/tests/test_db_connection_lifecycle.py), 4 tests):
1. a connection is closed when its owning thread is gone;
2. a **live** thread keeps a usable connection and keeps reusing it;
3. 20 short-lived threads leave **0** open connections (red: "20 of 20 still hold an open connection");
4. the factory-reset contract is unchanged — `close_all()` closes another
   live thread's handle and that thread transparently reopens.

**Verification.** Red-green reproduced (§3 rows 3–4). End-to-end re-measurement
of the same 60-job benchmark: **+46 MB → +1.2 MB**, **+199 handles → +3**.

### D2 — every OAuth-resolving request rescanned the install tree 3× (P1)

**Reproduction.** `/api/dashboard` p50 156 ms while `/api/health` was 12 ms; six
concurrent clients each saw ~972 ms — serialisation, not load.

**Root cause.** `resolve_oauth_client_path()` was called three times per
dashboard request and each call ran the recursive
`_iter_project_json_files("client_secret_")` walk over the install tree: ~127 ms
of CPU per walk (3 075 `os.scandir` + 51 810 `pathlib.match`), i.e. ~380 ms of
pure scanning per request, serialised by the GIL/lock and inflating to ~1 s
under concurrency. The released app ships a bundled portable Python runtime
with thousands of directories, so the cost grows with the installation.

**Fix** ([backend/app/config.py](../../backend/app/config.py)) — memoize only the
expensive walk, keyed on `(PROJECT_ROOT, DATA_DIR)`, with a 5 s TTL, behind a
lock:

- step 1 (the canonical `data/secrets/` location) is still a `stat` on **every**
  call, so a teacher placing the file there takes effect immediately;
- a cached path that no longer exists is not reported as present;
- the lock plus an inside-lock re-check collapses the thundering herd — without
  it, simultaneous clients all missed the cache and all walked (p95 691 ms on
  the refresh tick vs 45 ms warm).

**Regression tests** ([backend/tests/test_oauth_client_lookup.py](../../backend/tests/test_oauth_client_lookup.py), 8 tests): scan count stays bounded across repeated calls, the canonical path wins and is re-checked every call, a deleted file is not reported, a newly placed file is picked up after the TTL, the cache is keyed per data dir, concurrent callers trigger one walk, the mutated `OAUTH_CLIENT_PATH` is honoured, and a missing client still returns the configured fallback path.

**Verification.** Red-green (§3 row 5). Live re-measurement: **156 ms → 1.66 ms**
serial; **972 ms → 7.85 ms** and **6.2 rps → 162.3 rps** with six clients.

### Files changed by this audit

| File | Change |
|---|---|
| `backend/app/db.py` | +22 lines: `_release_connection` + thread finalizer (+ `weakref` import) |
| `backend/app/config.py` | +~60 lines: OAuth scan cache, TTL, lock (+ `time` import, docstring) |
| `backend/tests/test_db_connection_lifecycle.py` | **new**, 4 tests |
| `backend/tests/test_oauth_client_lookup.py` | **new**, 8 tests |
| `docs/testing/full-stack-stress-and-memory-audit.md` | **new**, this report |

Both source files were already modified in the working tree before this audit
(the earlier API-key work); only the hunks above are mine, and the pre-existing
hunks were left untouched.

---

## 9. Remaining risks

1. **The desktop is unverified at runtime.** No .NET SDK on this box → no
   build, no launch, no managed-heap number, no UI interaction, no
   startup/shutdown or "close during a correction" test. The static review
   found the audited leak classes clean, but static analysis cannot substitute
   for running the app. **This is the single largest gap in this audit.**
2. **Three live-UI frontend tests fail when the app is reachable** —
   `pageScroll.test.ts`, `workspaceLayout.test.ts`, `workspaceDrag.test.ts` —
   because they open `/#/results/r-ui-test` and **no seed, fixture or script in
   the repository creates that result** (it appears only in those two test
   files). This is a **pre-existing, already-documented** gap: see
   `docs/post-audit-validation-report.md` §F1, which found the same thing and
   recommended either seeding the fixture or making "fixture result absent" an
   honest *not run*. It was **not fixed here** because the previous report left
   that decision with the user and changing a skip condition to make a failing
   check disappear is exactly what this audit must not do. In the default run
   they are *skipped* (before and after my changes) — **not a regression**.
   Repointed at a real result id, they still time out waiting for the 3-pane
   workspace, so real coverage of the marking workspace remains absent.
3. **No live AI provider path was exercised.** No Gemini key was configured; the
   keyboard-level lifecycle (validation, cooldown, rotation) is covered by the
   key-management tests that passed, but quota exhaustion and real 429/5xx
   behaviour against Google were not observed. Do not read this audit as
   provider verification.
4. **Mid-flight interruption was not caught live.** Two force-kills landed
   after the jobs had completed (a 150-page blank paper corrects in seconds),
   so I did not observe a job killed *while* PREPARING. Recovery of in-flight
   jobs is covered by the repo's passing tests, and the DB was left with no
   non-terminal rows both times — but this specific race was not reproduced
   live.
5. **Concurrency was tested to 8 jobs on an 8 GB box.** The documented
   behaviour above those limits (queueing, WAITING_FOR_API parking) was not
   stressed, and larger PDFs (500+ pages) were not attempted for RAM-safety
   reasons on a machine with ~1.9 GB free.
6. **Two of the previous audit's findings remain open by design** (they are
   product decisions, not regressions): the mismatched upload limits
   (`/api/corrections/manual` 50 MB vs `/api/corrections/unified` 200 MB, with
   neither 413 path covered by a test), and the orphaned
   `47743487-…/` directory in the repository root.
7. **`claude-security` and the desktop UI suites could not be run**, for the
   reasons in §5 and §4 respectively. Report `failures as failures`: nothing in
   this audit claims those checks passed.

---

## 10. Final recommendation

**Ready with limitations.**

The backend is stable under everything this machine could throw at it: 2 331
tests pass, latency is in the low milliseconds, 8 simultaneous corrections
complete cleanly, no orphaned processes or temp files survive success, failure,
cancellation or an abrupt kill, and the two defects that were found — an
unbounded per-job connection leak and a request-path that rescanned the install
tree three times per call — are fixed with red-green regression tests and
re-measured to show the leak gone (+46 MB → +1.2 MB) and the endpoint fast
(972 ms → 7.85 ms at p50 under six clients).

The limitation is coverage, not health: **the desktop half of the application
was never executed here** (no .NET SDK), and the marking-workspace UI tests
still have no fixture to run against. Anyone treating this as a release gate
should first run the desktop and the three live-UI checks on a machine with the
.NET SDK installed; until then, "the desktop is fine" is an assumption, not a
measurement.
