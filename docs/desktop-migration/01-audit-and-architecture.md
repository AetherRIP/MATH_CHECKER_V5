# MATH_CHECKER_V5 → native Windows desktop: audit and architecture

Date: 2026-10-21
Scope: full migration of the existing V5 application to a C# native Windows
desktop client, preserving the correction engine and backend.

Everything below was read out of this checkout. Counts are from the commands
shown, not from documentation.

---

## 1. What V5 actually is

| Fact | Evidence |
|---|---|
| Backend: FastAPI (Python 3.12), served by uvicorn | `backend/app/main.py`, `main.py` (`uvicorn.Config("app.main:app")`) |
| 138 Python modules / ~59 000 lines in `backend/app` | `find backend/app -name '*.py' \| wc -l`, `xargs wc -l` |
| 262 route declarations (229 unique paths) | `dev_local/route_dump.py`, see §4 |
| Frontend: React 18 + TypeScript + Vite, 26 page modules, ~16 200 lines | `frontend/src/pages/*.tsx` |
| Storage: SQLite at `<data>/app.db` via `backend/app/db.py` | `data/app.db`, `app.db.bak-*` |
| Secrets/settings: TOML under `<data>/` (`secrets.toml`, `config.toml`) | `backend/app/config.py` |
| Tests: 112 files, **2 189 tests** collected | `pytest backend/tests --collect-only -q` |
| Entry point: `main.py` — config → DB → key manager → worker pool → Classroom (lazy) → UI | `main.py` docstring + `main()` |
| Single-instance rule: one backend per port, proven via `/api/health` | `main.py: probe_port`, `stop_running_instance` |
| Data-folder resolution: `MC5_DATA_DIR` > beside app > checkout `data/` > per-user | `backend/app/data_location.py`, `backend/app/config.py` |
| Optional bundled Python runtime for recipients | `MathChecker_Distribution/scripts/build_runtime.py` |

**The application is a local server plus a browser UI.** The engine, the
database, the Classroom integration, the report renderer and the automation
worker all live behind the HTTP API on `127.0.0.1`.

## 2. Current architecture (as implemented)

```
                    ┌──────────────────────────────┐
 browser  ─────────▶│  FastAPI app (backend/app)   │
 (React SPA,        │  /api/*  + static dist/      │
  frontend/dist)    └──────────────┬───────────────┘
                                   │
        ┌──────────────┬───────────┼─────────────┐
        │              │           │             │
   engine/*       jobs/manager  workers/pool  automation/*
 (blueprint,     (queue, SSE)  (orchestrator) (Classroom
  legacy_marking,                             workflows)
  verify, batching,
  pdf_optimize)
        │
   gemini/client_proxy + key_manager   ──▶ Gemini API
   classroom/*                        ──▶ Google Classroom / Drive
   reporting/class_report             ──▶ PDF/CSV/MD
   db.py                              ──▶ SQLite  <data>/app.db
```

Trust boundary: everything sensitive (keys, OAuth tokens, student rows) stays
inside the Python process and its data folder. The browser UI holds only a
session cookie (public mode) or nothing (local mode).

## 3. Required desktop behaviour that the audit changed

The migration brief asks for a native client. The audit shows the *only*
supported way to run the engine is the Python backend:

* the engine is ~40 modules of Python with PyMuPDF, Pillow, pydantic and
  cryptography dependencies (`backend/requirements.txt`);
* `verification_json` / `result_json` contracts are produced by
  `engine/legacy_marking.py` and validated by `engine/verify.py`;
* prompts, model rotation and retry policy live in `gemini/*`;
* the packaged distribution already ships a Python runtime.

Therefore porting the engine to C# would be a rewrite of the one thing the
brief says to protect. **Decision: the desktop app is a native C# shell that
owns the window, drives the existing HTTP API, and supervises the existing
backend process.** No engine, prompt, marking rule, schema or table is
reimplemented.

## 4. HTTP surface

Grouped by module (`dev_local/route_dump.py` prints all 262):

| Group | Routes | Examples |
|---|---|---|
| `routes_all.py` | 92 | `/api/dashboard`, `/api/jobs`, `/api/results`, `/api/corrections/unified`, `/api/keys/*`, `/api/settings`, `/api/diagnostics*` |
| `distributed/routes.py` | 61 | `/api/distributed/schools`, `/teams`, `/devices`, `/releases`, `/workers`, `/audit` |
| `automation/routes.py` | 24 | `/api/automation/status`, `/jobs`, `/assignments`, `/run-now`, `/bulk-upload` |
| `student_routes.py` | 8 | `/api/students`, `/students/{id}/details`, `/students/{id}/report` |
| `product_routes.py` | 18 | `/api/classes`, `/api/assignments`, `/api/attention`, mark-scheme endpoints |
| `report_routes.py` | 9 | `/api/reports/class/{id}`, `/reports/assignment/{id}`, `/export` |
| `classroom/tracking_routes.py` | 11 | `/api/classroom/sync`, `/tracking`, `/permissions` |
| `auth_routes.py` | 7 | `/api/health`, `/api/auth/*`, `/api/remote/status` |
| `setup_routes.py` | 13 | `/api/setup/status`, `/api/review`, `/api/results/{id}/override` |
| `scheme_routes.py` | 9 | `/api/scheme/discrepancies`, `/proposals` |
| `privacy/data/access/analytics/viewer/audit/repair/lifecycle` | 41 | inventory, export/import, factory reset, diagnostics |

Error contract (used by the C# client): `app/errors.py` returns
`{"code": "...", "message": "..."}` with an honest HTTP status; 403 is
`{"code": "forbidden", ...}` for developer-only endpoints.

## 5. Framework decision: **WPF on .NET 10**

Measured on this machine (`dotnet --info`, `dotnet --list-runtimes`):

| Requirement | WPF | WinUI 3 |
|---|---|---|
| SDK installed | .NET SDK 10.0.402 | same |
| Desktop framework runtime present | **Microsoft.WindowsDesktop.App 8/9/10.0.13** | needs Windows App SDK package + bootstrapper |
| Windows 10 SDK / MSIX tooling present | not needed | **absent** (`C:\Program Files (x86)\Windows Kits\10\bin` missing) |
| Build with only `dotnet build` | yes | no (needs WinAppSDK + MSIX packaging project for a full app) |
| Native file dialogs, drag-drop, clipboard, ShellExecute | built in | present but requires the extra runtime |
| Runs on Windows 10 1809+ | yes | yes |
| Single-file publish | yes | unpackaged only, with runtime deployment caveat |

WinUI 3 was evaluated first because it is the modern stack, but it cannot be
built or verified on this machine without installing the Windows SDK and the
Windows App SDK packaging tooling — and an unverified framework choice would
violate the brief's "do not claim a feature works unless tested". WPF is
shipped by the already-installed desktop runtime, so every claim below is
backed by a real build and a real process launch.

Consequences of choosing WPF (accepted):
* No Fluent/WinUI controls; the floor is the WPF control set.
* No MSIX path here (no Windows SDK). Packaging is `dotnet publish` +
  Inno Setup, matching what `MathChecker_Distribution` already uses.

## 6. Proposed desktop architecture

```
MathChecker.Desktop.exe (WPF, .NET 10)
├─ App/                startup, DI composition, unhandled-error policy
├─ Shell/              MainWindow, navigation, status bar (backend state)
├─ Core/
│  ├─ Backend/         BackendHost: locate → launch → health → monitor → stop
│  ├─ Api/             MathCheckerApiClient (typed, one method per endpoint)
│  ├─ Configuration/   DesktopSettings (%APPDATA%), env overrides
│  ├─ Diagnostics/     rolling file log + secret redaction
│  └─ Mvvm/            ObservableObject, RelayCommand, AsyncCommand
├─ Models/             DTOs mirroring the real JSON (tolerant to unknown fields)
├─ ViewModels/         one per feature area, no HTTP inside views
└─ Views/              one UserControl per feature area (functional only)
```

Process model:

```
Desktop app ──starts──▶ python main.py --serve <port>   (child, hidden console)
     │                        │
     │  GET /api/health       │  owns SQLite, workers, Gemini, Classroom
     └────────────────────────┘
     on exit: kill only the process tree it started
```

* If `/api/health` already answers as Math Checker on the configured port, the
  app **attaches** instead of starting a second backend (same rule as
  `main.py`; two worker pools would double-mark work).
* If the port belongs to something else, the app reports the conflict and
  offers another port; it never kills a foreign process.
* Startup, readiness, exit code, last stderr lines and the data folder are
  surfaced in the UI.

## 7. Migration risks and security concerns

| Risk | Mitigation |
|---|---|
| Two backends fighting over one DB | attach-when-healthy, single-instance check before spawn, kill only own PID tree |
| Desktop executable embedding credentials | no keys in the client; keys stay in `data/secrets.toml`; settings file holds only paths/port |
| Logging student data | log lines carry job/result ids and counts; a redactor strips `AIza…`/Bearer tokens; no student names written by the desktop log |
| Session cookie in public mode | `HttpClientHandler.CookieContainer` in memory only; never persisted; CSRF header forwarded from `/api/auth/status` |
| Backend crashes mid-correction | the backend already recovers in-flight jobs on boot (`jobs/manager.recover_on_boot`); the client shows the outage and reconnects |
| Duplicate correction on double-click | buttons disable while a request is in flight; backend rejects duplicate jobs (`DuplicateJobError`) |
| `result_json`/`verification_json` shape drift | DTOs ignore unknown fields; per-question view reads an optional list and shows "no per-question breakdown" when absent |
| Python runtime missing on a clean machine | backend locator tries bundled `runtime\python.exe`, then repo `.venv`, then PATH `py`/`python`; a missing runtime is a named, actionable error, not a silent failure |
| Developer-only endpoints (diagnostics, prompts, audit) | 403 is displayed as "requires developer role" — not retried, not faked |

## 8. Phased plan (this work)

1. Audit + inventory (this document, `02-feature-inventory.md`).
2. Solution scaffold, MVVM base, settings, logging, API client, backend host.
3. Shell + navigation + dashboard.
4. Correction workflow (unified upload, live jobs, retry/cancel).
5. Results, review queue, remark/override/export, reports.
6. Classes, students, assignments, Classroom sync, automation.
7. System area: settings, AI keys, storage, privacy, diagnostics, prompts,
   audit, multi-school.
8. Tests: unit (client/DTO/host/redaction) + live backend end-to-end on demo
   data + full Python suite baseline.
9. Packaging (`dotnet publish` + installer script) and documentation.

## 9. Questions the codebase had to answer (evidence)

| Question | Answer from source |
|---|---|
| Is there a real API contract to consume? | yes, 262 routes; error shape in `app/errors.py` |
| Can the desktop start the backend alone? | yes, `main.py --serve PORT` runs the API on loopback with `MC5_SUPERVISED` set |
| Does local mode need a login? | no — `auth_gate.gate_enabled()` is false unless public mode or `security.login_required` |
| Where does student data live? | `<data>/app.db`; desktop never copies it |
| Are there developer-only APIs? | yes; `/api/diagnostics` returned 403 in a teacher-role run |
| Can a 30+ page PDF be corrected through one call? | yes; `/api/corrections/unified` uses the same standard pipeline for every page count |
| Is there demo/synthetic data for E2E testing? | yes; `--demo` seeds an isolated `data/demo` DB with mock marking (`app/demo.py`) |
| Does the backend recover after a crash? | yes; boot recovery for correction jobs and automation |
