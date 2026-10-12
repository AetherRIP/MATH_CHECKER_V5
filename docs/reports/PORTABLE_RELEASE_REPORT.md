# Math Checker — portable Windows release: build + verification report

Build `20261012` (app 5.2.1) · prepared 2026-10-12 · master project `E:\MATH V6.0 AUTOMATION\MATH V5`

> **Rebuilt 2026-10-12 after the UI architecture correction** (separate pages,
> Task Manager, workers on the AI page — see [UI_ARCHITECTURE_REPORT.md](UI_ARCHITECTURE_REPORT.md)).
> The hashes below are this build's; the package was re-verified after the rebuild.

---

## 1. Deliverable

Everything is in [`MathChecker_Distribution/release/`](../MathChecker_Distribution/release):

| Artifact | Size | SHA-256 |
|---|---|---|
| `MathChecker_Portable.zip` | 76,419,753 B (74.6 MB) | `286c4c4c8f7b68f37d91d0ade74c7f2e55e2ed0c9cd58676bd23091a15a00f6b` |
| `MathChecker_Setup.exe` (Inno Setup installer) | 53,501,931 B (51.0 MB) | `01031316398f070c88f0ea451c425658303c276ef296a524c9845fed4438dfc3` |
| `SHA256SUMS.txt`, `release_manifest.json` | — | hashes re-computed and confirmed against the files on disk |

The ZIP contains the **runnable package**, not source: `MathChecker.bat` (launcher),
`Stop.bat`, `main.py`, `backend/`, a built `frontend/dist/`, `assets/`, `agent/`,
`tools/cloudflared.exe`, `portable.flag`, an empty `Data/` (plus
`Data/README.txt` and `Data/config.toml.example`), and a bundled CPython
runtime in `runtime/` — 3,822 files, 15 runtime dependencies, nothing else
needed on the target machine. Recipient instructions:
[README.txt](../MathChecker_Distribution/documentation/README_PORTABLE.txt) (shipped at the root of the extract).

Build identity is recorded in
[backend/app/version.py](../backend/app/version.py) (`5.2.1`, build `20261012`,
2026-10-12) and in `release_manifest.json`
(`build_timestamp 2026-10-12T02:45:13Z`, `self_contained: true`).

---

## 2. The defect that made the packaged app unusable (root cause)

**Symptom.** In the packaged build every correction failed: the UI showed a
failed job and the log said `Worker process died unexpectedly (exit code 1)`.
The same code worked from the source checkout.

**Cause.** `CorrectionWorker.start_job` launched the correction child with
`sys.executable -m app.workers.child`, relying on `PYTHONPATH` (which it set to
the backend root) to make the `app` package importable. The bundled runtime is
CPython's **embeddable** distribution
(`MathChecker_Distribution/portable/MathChecker_Portable/runtime/python313._pth`),
and an existing `*._pth` file makes CPython **ignore `PYTHONPATH` and the
launch directory**. In the package the child therefore could not import `app`,
died before reading its job, and every correction came back as a crash. A
development checkout never showed it because a system Python has no `._pth`.

**Fix** ([backend/app/workers/adapter.py](../backend/app/workers/adapter.py#L197),
[backend/app/workers/child.py](../backend/app/workers/child.py#L1-L26)):

* the child is launched **by script path** (`python <backend>/app/workers/child.py`)
  — `sys.executable`, no `-m`, no dependence on `PYTHONPATH`;
* `child.py` puts its own backend root on `sys.path` at import time, so it works
  regardless of cwd, launcher or `._pth`;
* the child's stderr is captured instead of discarded, so a future crash is
  diagnosable from the log rather than silent;
* the class docstring that still described the old `-m` launch was corrected.

Regression test: [backend/tests/test_worker_child_launch.py](../backend/tests/test_worker_child_launch.py)
pins both the behaviour (a child launched the way the adapter launches it
completes a job) and the adapter's launch contract.

---

## 3. What the packaged application does when a teacher double-clicks it

`MathChecker.bat` and `Stop.bat` are distribution-owned files
([installer/launcher.bat](../MathChecker_Distribution/installer/launcher.bat), `installer/Stop.bat`):

* **Data location** — `portable.flag` present → `Data\` beside the app; else an
  existing `Data\config.toml`; else `%LOCALAPPDATA%\MathChecker`. Decided before
  anything touches the machine, so a portable copy never writes to the user
  profile (verified: `%LOCALAPPDATA%` unchanged).
* **Spaces / non-ASCII safe** — the runtime path is quoted inside the variable,
  and the checks use labels instead of `( )` blocks; verified from a folder
  named `Math Checkër Portable (v5.2)`.
* **Single instance** — a second launch probes the port, proves identity through
  `/api/health`, and attaches instead of starting a second backend (verified:
  one PID owns the port; "already running" message, exit 0).
* **Port conflicts** — a foreign holder or a non-HTTP holder is reported in a
  sentence (`PORT CONFLICT…`) instead of an opaque uvicorn bind error; wrong
  arguments print usage rather than a traceback.
* **UI auto-open** (`main.py: open_ui_when_ready`) — the browser opens only
  after `/api/health` answers; suppressed for boot/autostart runs and by
  `MC5_NO_BROWSER=1`.
* **Clean shutdown** — Ctrl+C or `Stop.bat` → `main.py --stop`: identity is
  proven through the health endpoint *before* anything is killed, then the whole
  process tree (workers, tunnel) goes down, `Data\instance.json` is swept, and
  no orphaned process is left.
* **Configuration** — everything is configured in the app. `Data\config.toml.example`
  is a new, secret-free reference of all 62 settings keys; it is never read by the
  app (only `Data\config.toml` is), and a verifier check keeps it in sync with
  the keys the app actually writes.

---

## 4. Test results (all on the packaged build, not the source tree)

### 4.1 Packaged end-to-end verification — `36/36 PASS`
`MathChecker_Distribution/scripts/verify_portable.py --zip release/MathChecker_Portable.zip`
extracts the ZIP into a temp folder with spaces and an umlaut, then drives the
real launchers. Full output: [PORTABLE_VERIFY_REPORT_20261012.txt](../logs/PORTABLE_VERIFY_REPORT_20261012.txt)
(this is the run against the final, UI-corrected package: `36/36`, and the
served bundle in the ZIP is the corrected one, `assets/index-BM-mpZrv.js`).

| Group | Checks | Result |
|---|---|---|
| Package contents | tree complete (16 required paths); no `.env/.pem/.key/.db/.pdf/credential` file; no build-machine paths in app code | 3/3 |
| Configuration template | shipped; no API-key/token material; documents **every** setting (62 keys, none undocumented, none unknown) | 3/3 |
| Bundled runtime | 18 modules import both from the app folder and from an unrelated cwd | 2/2 |
| Launch | launcher → `/api/health` 200; UI served; `Data\` created beside the app; `%LOCALAPPDATA%` untouched | 4/4 |
| Second launch | duplicate refused, not started twice; exactly one PID owns the port | 2/2 |
| Auth | anonymous API call → 401; generated bootstrap credentials sign in; session accepted | 3/3 |
| Demo copy | starts; isolated `Data\demo` | 2/2 |
| Correction | PyMuPDF native libs load; upload accepted; job `COMPLETED`; stored result `earned=9/12 percentage=75.0 provisional_grade=7` | 4/4 |
| Report PDF | generated (217 KB, `application/pdf`); opens with the packaged runtime, 4 pages | 2/2 |
| Export/import | export package (students=5, results=9); import into the second copy adds 28 rows | 2/2 |
| Logs / background | generated passwords never appear in logs; Tiny Agent reported unavailable and **not** started (`enabled=false`); one worker pool per copy; demo copy stops | 4/4 |
| Stop → restart | `Stop.bat` frees the port; database survives (4096 → 4096 B); relaunch → health 200; imported records still present | 4/4 |
| Cleanup | no orphaned processes after stopping both copies | 1/1 |

The grade scale remained **1–9** in the stored result, and marks/grades/report
bytes come from the packaged runtime, not from a locally installed Python.

### 4.2 Backend regression suite — `1550 passed`, exit 0 (5 min 3 s)
`cd backend && python -m pytest -q` → `1550 passed, 5 warnings`.
The only warnings are third-party deprecation notices and a duplicate-Operation-ID
warning already present in the source.

### 4.3 Targeted re-run of the touched areas — `19 passed`
`tests/test_portable_launch_and_stop.py`, `tests/test_worker_child_launch.py`,
`tests/test_worker_pool.py` (15.8 s) — the launcher/stop contract, the new child
launch contract and the worker pool.

### 4.4 What could not be tested here

* **AI marking against the real provider** — the packaged build was exercised
  with the built-in mock/demo engine, because the release deliberately contains
  no API key. Real Gemini marking needs the recipient's own key.
* **Google Classroom** — no OAuth client file ships; the verifier only confirms
  the app reports Classroom as unavailable instead of failing, and the source
  suite covers the OAuth/sync paths with mocks. A real `Sync Classroom` run is
  still outstanding.
* **Public Address / tunnel** — cloudflared is bundled and its lookup is covered
  by tests, but no tunnel was opened during verification (it publishes the machine).
* **A second physical computer** — everything above ran on this build machine,
  including from a clean temp folder with a hostile path. **Cross-computer
  compatibility remains unverified until the ZIP is run on another Windows PC.**
* **Installer** — `MathChecker_Setup.exe` compiled successfully (Inno Setup 6),
  but its wizard was not clicked through; the ZIP path is the one verified above.

---

## 5. Files changed for this release

Application (private project):

* `backend/app/workers/adapter.py` — script-path child launch; stderr captured; docstring corrected
* `backend/app/workers/child.py` — cwd/`PYTHONPATH`-independent import bootstrap
* `backend/app/version.py` — build identity `20261012` / `2026-10-12`
* `main.py` — `--stop` (identity-checked stop of the whole tree, stale-record sweep), UI auto-open once healthy, single-instance / port-conflict / bad-argument messages
* `backend/tests/test_worker_child_launch.py` (new), `backend/tests/test_portable_launch_and_stop.py` (new)

Distribution (`MathChecker_Distribution/`):

* `scripts/build_release.py` — ships `Data/config.toml.example`, updated `Data/README.txt`
* `scripts/verify_portable.py` — config-template checks; `.\`-prefixed launcher invocation; hardened HTTP helper
* `documentation/config.toml.example` (new), `documentation/README_PORTABLE.txt`, `documentation/README_RECIPIENT.md`
* `installer/launcher.bat`, `installer/Stop.bat`, `installer/MathChecker.iss`

The private project has **no** version-control metadata in this workspace, so this
list is the record of the packaging work rather than a `git diff`.

---

## 6. Rebuilding and re-verifying

```bat
:: from MathChecker_Distribution\
build_release.bat                       :: stages, scans, builds zip + installer
:: or, step by step:
python scripts\build_release.py --with-runtime build\runtime
python scripts\verify_portable.py --zip release\MathChecker_Portable.zip --report verify_report.txt
```

`build_release.py` cleans `release_staging/`, builds the frontend, copies only
allowlisted files, runs the secret/privacy scan (blocks on any finding),
validates the bundled runtime's imports, then produces the ZIP, the installer,
`SHA256SUMS.txt` and `release_manifest.json`. The verification run must be green
(`36/36`) before the artifact is handed to anyone.

---

## 7. Second computer: what is still needed

1. Copy `MathChecker_Portable.zip` to the target PC and extract it **whole** to a
   writable folder (Desktop, Documents, USB stick; not `C:\Program Files`).
   Windows SmartScreen will warn because the package is not code-signed —
   "More info" → "Run anyway". Nothing in Windows security is disabled.
2. No Python, Node.js, administrator rights or developer tools are required; the
   runtime is inside the package.
3. Configure per machine: Gemini API key(s) (AI page → Models & Keys), and, only
   if Classroom is wanted, your own OAuth client file at
   `Data\secrets\google_oauth_client.json`. Without these the app still runs, and
   says precisely what is missing instead of crashing.
4. To carry settings/students across, copy the `Data\` folder — **except**
   `secrets.toml` and `secrets\`, which belong to the machine that created them —
   or use Storage → Export data / Export configuration.
5. Anything that must still be proven on that machine: first launch, a real
   correction with a real key, a report, `Stop.bat`, a restart, and (if used)
   Classroom sync and Public Address.
