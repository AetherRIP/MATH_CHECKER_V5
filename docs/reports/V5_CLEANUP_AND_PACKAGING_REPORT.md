# V5 — Workspace Cleanup and `V5.exe` Launcher

**Date:** 2026-10-23
**Scope:** clean the MATH_CHECKER_V5 workspace, make `V5.exe` the product
executable, rebuild the Windows release, test it, and document what remains
manual. Nothing was committed or pushed.

---

## 1. What the application actually is

V5 is a **local web application with a native Windows shell**, not a Python
desktop app:

| Layer | Where | Role |
|---|---|---|
| Backend | `main.py` + `backend/app/` (FastAPI, port 8642 by default) | the whole product: correction engine, marking rules, SQLite store, Classroom OAuth, dashboard API |
| Web UI | `frontend/` (React + Vite, served from `frontend/dist`) | the teacher-facing pages |
| Desktop shell | `desktop/src/MathChecker.Desktop` (C# / WPF, net10.0-windows) | starts or attaches to the backend and drives it over the same HTTP API; contains no correction logic |
| Bundled runtime | `MathChecker_Distribution/build/runtime` (embeddable CPython 3.13.7) | lets the release run without a Python install |

Correct startup sequence (implemented by the launcher, taken from the existing
source): locate the backend folder (`main.py` + `backend/app`), resolve an
interpreter, choose a port, start `main.py --serve <port>`, poll `/api/health`,
then show the window. Packaging therefore had to wrap the existing WPF shell —
no second implementation was created.

## 2. Workspace cleanup

| Item | Action |
|---|---|
| `__pycache__` folders and `*.pyc` outside the virtualenvs | **686 folders removed** (repo root, `backend/`, `agent/`, `MathChecker_Distribution/build/runtime`, `desktop/artifacts/`). Regenerated on the next run; `.gitignore` already excludes them |
| `.pytest_cache`, `frontend/tsconfig.tsbuildinfo`, Vite cache | removed (regenerable) |
| `desktop/artifacts/` (stale release output from an older layout) | deleted, then rebuilt from source in this session |
| Tracked source, tests, fixtures, model/rules code, `docs/logs/`, `docs/reports/` | untouched |
| `.venv/`, `.venv.foreign/`, `frontend/node_modules/` | left in place — ignored, machine-local, and `.venv.foreign` is not evidence of anything in this task |
| `.gitignore` | already covered venvs, caches, `desktop/artifacts/`, `frontend/dist`, logs, `*.env`; no change needed |

Verification: `git status` reports **no deleted tracked file** (`git status
--short` contains no `D` entries), so the cleanup removed only untracked,
regenerable output. Pre-existing uncommitted modifications were preserved.

## 3. Dependency audit

`backend/requirements.txt` is the runtime manifest; `backend/requirements-dev.txt`
adds `pytest` + `httpx`; `backend/requirements-lock.txt` records the exact
verified versions. **No dependency was removed** — every entry has a real
importer in the backend:

| Requirement | Evidence (importers in `backend/app`, `main.py`) |
|---|---|
| `fastapi` | 19 files |
| `uvicorn` | serves `app.main:app` (`main.py`, `run.py`) |
| `python-multipart` | no direct import, but `Form(...)`/`File(...)` routes (`routes_all.py:738`, `:791`) crash at boot without it — the comment in the manifest is correct |
| `google-genai` | 3 files (Gemini calls) |
| `google-auth`, `google-auth-oauthlib` | Classroom OAuth |
| `requests` | 4 files |
| `openpyxl`, `reportlab` | XLSX / PDF exports |
| `pymupdf` (`fitz`) | 2 files (mark-scheme text extraction). The working tree already constrained it to `<2`; kept, because 2.x adds a new dependency chain and changes the runtime size |
| `pypdf`, `Pillow` | submission validation / image orientation |
| `cryptography` | Ed25519 update signing |

Noted, not changed: `fpdf` is imported only by two fixture scripts
(`backend/scripts/make_fixtures.py`, `make_marking_reference.py`) and is
declared in neither manifest — it is optional tooling for regenerating test
fixtures, not a runtime dependency.

## 4. Packaging

* **Technology:** the existing stack, no new framework — `dotnet publish`
  (Release, `win-x64`, self-contained, single folder) for the WPF client plus the
  existing embeddable Python runtime copied next to it. PyInstaller was rejected:
  it would not wrap the WPF shell, and the backend already ships a working
  bundled runtime.
* **Executable name:** the assembly name was changed to `V5`, so Windows shows
  `V5.exe` while namespaces stay `MathChecker.Desktop`. The csproj, build and
  test scripts, the Inno Setup script and the docs were updated to match.
* **Build fix found while testing:** the runtime copy carried `__pycache__`
  folders from the build machine into the ZIP. `desktop/build_release.ps1` now
  strips them after copying the runtime; the portable ZIP dropped from 134.7 MB
  to **111.7 MB** with no functional change.

**Build command** (from the repo root, needs the .NET 10 SDK):

```powershell
powershell -ExecutionPolicy Bypass -File desktop\build_release.ps1
```

Output in `desktop\artifacts\` (git-ignored):

| Artifact | Size |
|---|---|
| `MATH_CHECKER_V5\desktop\V5.exe` | 199 KB (`sha256 c065698e…d905e5bd`) |
| `MATH_CHECKER_V5\` (client + backend + runtime) | 281 MB |
| `MATH_CHECKER_V5_Portable.zip` | 111.7 MB |
| `MATH_CHECKER_V5_Setup.exe` | **80.3 MB**, per-user installer (`sha256 e34d5439…cf352b`) |

`release_manifest.json` records executable, version, runtime and build time.

## 5. Test results

| Suite | Command | Result |
|---|---|---|
| Backend | `.venv\Scripts\python -m pytest backend/tests -q` | **2122 passed, 1 skipped** (10m21s). The skip is the pre-existing conditional in `test_release_runtime_integrity.py` for a `MathChecker_Distribution/portable` folder that does not exist in this checkout |
| Desktop client | `dotnet test desktop\tests\MathChecker.Desktop.Tests` | **160 passed, 0 failed** |
| Frontend build | `npm run build` in `frontend/` | succeeded (Vite, 3.6 s) |
| Frontend logic tests | `node --test "tests/*.test.ts"` | **84 passed, 0 failed, 3 skipped** — the three browser checks need the app running at `127.0.0.1:8720` plus Chromium; they skip with that reason rather than passing |
| Packaged release | `powershell -File desktop\test_release.ps1` | **19/19** on the final artifact: window opens, bundled `runtime\python.exe` runs the packaged `main.py`, `/api/health` + dashboard OK, demo mode, single instance, upload → `COMPLETED` → stored result `9/12` of 3 questions, PDF export, data in the scratch folder, clean exit, no orphan process, port closed |
| Installer | `dev_local\installer_test.ps1` | **11/11**: silent per-user install into a scratch folder, launcher/backend/runtime present, uninstaller registered, installed window opens, `/api/health` ok, dashboard served, clean close, uninstaller exit 0, install folder removed |
| Failure paths | negative-test script | **15/15**: no bundled runtime and no Python on PATH → window stays open with the reason, no backend, no data folder created; foreign service on the port → moves to the next port, never kills it; fresh data folder with no keys → starts, reports no keys, does **not** pretend to be in demo mode; launched from `C:\Users\…\Temp` → still finds the release backend |

Both release runs were made from a copy of `MATH_CHECKER_V5\` in `%TEMP%`, not
from the checkout, so path resolution outside the project is covered.

## 5a. Installer verification (2026-10-23)

`MATH_CHECKER_V5_Setup.exe` installs per-user (`PrivilegesRequired=lowest`, no
admin prompt), into `%LOCALAPPDATA%\Programs\MATH_CHECKER_V5` by default. The
automated check installed it silently into a scratch folder, launched the
installed `desktop\V5.exe`, confirmed the window (`MATH_CHECKER_V5`),
`/api/health` = ok and the dashboard from the installed backend, closed it,
ran the uninstaller (`exit 0`) and confirmed the folder was removed and no
process or listening port was left behind.

## 6. Still required on a user machine

* **Nothing to install** for the app itself: no .NET, no Python — the release is
  self-contained.
* **Gemini API key** — real corrections need it (Settings → API Keys). It is
  never bundled.
* **Google Classroom OAuth client** — only for Classroom features; stored under
  the app's data folder (`data/secrets/`), never in the executable.
* **Windows SmartScreen** may warn because the binary is unsigned (no code
  signing certificate in this environment).

## 7. Limitations / manual steps

1. **Inno Setup 6** (6.7.3, per-user, installed with `winget install --id JRSoftware.InnoSetup`)
   is required to rebuild the installer. Without it `build_release.ps1` still
   produces the folder and ZIP and prints the exact `iscc` command.
2. **Three browser-level frontend tests were not run** (app + Chromium needed).
3. `V5.exe` is a folder-distributed executable, not a single file: WPF plus a
   bundled Python runtime cannot be one binary. Ship `MATH_CHECKER_V5\` or the
   ZIP.
4. `fpdf` must be installed separately if you want to regenerate fixture PDFs.
5. Changes remain **uncommitted** in the working tree, as requested.

## 8. Files touched for this task

* `desktop/src/MathChecker.Desktop/MathChecker.Desktop.csproj` — assembly name `V5`
* `desktop/build_release.ps1` — `V5.exe` paths, strips runtime caches
* `desktop/test_release.ps1` — `V5.exe` paths
* `desktop/installer/MathCheckerDesktop.iss` — `AppExeName V5.exe`
* `desktop/README.md`, `README.md`, `docs/desktop-migration/03-build-and-release.md`,
  `docs/development-environments.md` — executable name, paths, measured results
* `docs/reports/V5_CLEANUP_AND_PACKAGING_REPORT.md` — this report
