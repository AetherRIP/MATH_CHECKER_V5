# Windows setup, restoration and troubleshooting

This page is the complete procedure for turning a **fresh Windows installation**
into a working Math Checker V5 checkout: what to install, why each piece is
needed, what only you can do, and how to diagnose a step that fails.

It was written from an actual restore on a clean Windows 11 machine, not from
the old requirements files. The versions and results in section 1 were observed
on that machine; the commands are the ones used there.

> **Short version**
>
> ```bat
> winget install --id Microsoft.VCRedist.2015+.x64
> winget install --id Python.Python.3.13 --scope user
> winget install --id OpenJS.NodeJS.LTS  --scope user     :: optional: UI build
> winget install --id Microsoft.DotNet.SDK.10              :: optional desktop; machine-wide, needs admin
> git clone https://github.com/AetherRIP/MATH_CHECKER_V5.git
> cd MATH_CHECKER_V5
> setup.bat
> powershell -ExecutionPolicy Bypass -File scripts\verify_windows.ps1
> start.bat
> ```

## 1. What was verified

| Component | Version used for verification |
| --- | --- |
| Windows | Windows 11 Home 25H2, build 26200, x64 |
| Python | CPython 3.13.15 (64-bit, per-user install) |
| pip | 26.2.2 |
| Node.js / npm | 24.20.0 / 11.19.0 (LTS, per-user install) |
| .NET SDK | 10.0.402 (`net10.0-windows` desktop client) |
| MSVC runtime | Microsoft Visual C++ v14 Redistributable 14.51.36247 (x64) |
| Git | 2.56.0.windows.2 |
| Browser used by UI checks | Microsoft Edge (preinstalled) |

Python 3.12 and later are supported by the code (`setup.bat` says 3.12+).
Node.js 20+ is required only to build or develop the web interface.

## 2. Prerequisites

### Mandatory

| Item | Why the code needs it | How to install |
| --- | --- | --- |
| **Python 3.12+ (64-bit)** | Runs the backend, the tests and every script in `scripts/`. | `winget install --id Python.Python.3.13 --scope user`, or python.org with **Add Python to PATH**. |
| **Microsoft Visual C++ 2015-2022 Redistributable (x64)** | `pymupdf`, `cryptography` and `Pillow` ship compiled `.pyd` files linked against `MSVCP140.dll`. On a fresh Windows the wheel installs fine and then fails at import with `DLL load failed while importing _extra`. PyMuPDF is what reads and renders mark-scheme PDFs. | `winget install --id Microsoft.VCRedist.2015+.x64` (administrator approval required). |
| **Git** | Clone, update, and report the exact revision. | `winget install --id Git.Git`. |

No C/C++ compiler, CUDA, database server, Docker, or OCR engine is required.
The app is a local FastAPI server plus a static React bundle, storing data in
SQLite (`app.db`) inside its data folder.

### Optional

| Item | Needed for | Install |
| --- | --- | --- |
| **Node.js 20+** | Building `frontend/dist` and running the frontend test suite. The backend serves the prebuilt bundle, so a normal run does not need Node — but a fresh clone has no bundle until it is built. | `winget install --id OpenJS.NodeJS.LTS --scope user` |
| **.NET 10 SDK** | Building/testing the WPF desktop client in `desktop/`. The web app does not use it. | `winget install --id Microsoft.DotNet.SDK.10` (machine-wide) or Microsoft's `dotnet-install.ps1` into `%USERPROFILE%\.dotnet` |
| **Inno Setup 6** | Only when *building* the Windows installer (`MathChecker_Distribution/`). | `winget install --id JRSoftware.InnoSetup` |
| **Chrome/Chromium/Edge** | The optional headless UI regression checks. Edge is preinstalled on Windows; otherwise set `CHROME_PATH`. | — |

### Accounts and external services (not installable)

| Service | Used for | Configuration |
| --- | --- | --- |
| **Google Gemini API key(s)** | AI marking. Without a key the app runs in mock/demo mode only. | Created in Google AI Studio, entered in the app under **Settings → AI keys**. Stored in `data/secrets.toml`, never in this repository. |
| **Google Classroom OAuth client** | Importing coursework/submissions and writing drafts back. | A Google Cloud project with the Classroom API enabled and an OAuth *Desktop* client; put the client JSON in `data/secrets/google_oauth_client.json` (or connect via **Settings → Classroom**). Sign-in is a browser flow you complete yourself. |
| Billing/quota on the Gemini project | Live marking beyond the free tier. | Google Cloud console. |

Nothing in the repository fabricates or borrows credentials. With no keys the
app tells you it is not configured; that is the expected state for a test or
demo snapshot.

## 3. Restoring after reinstalling Windows

1. **Install the prerequisites** (section 2). Reopen the terminal afterwards so
   PATH changes take effect; `winget` installs into the user profile, and a
   terminal opened before the install will not see `python`, `node` or `dotnet`.
2. **Clone the repository** (any folder without spaces is simplest):
   ```bat
   git clone https://github.com/AetherRIP/MATH_CHECKER_V5.git
   cd MATH_CHECKER_V5
   ```
   There are **no Git submodules and no Git LFS assets** to fetch (`.gitattributes`
   only normalises line endings), and no model weights to download.
3. **Recover your data**, if you kept it from the old machine. Copy the *contents*
   of the old data folder (`app.db`, `config.toml`, `secrets.toml`,
   `secrets\`, `mark_schemes\`, optionally `blueprints\`) into the new one.
   The app resolves its data folder in this order: `MC5_DATA_DIR`, then data
   beside the app, then the checkout's own `data\`, then
   `%LOCALAPPDATA%\MathChecker\`. A source checkout therefore keeps using
   `data\` — so restore into `data\` (or set `MC5_DATA_DIR`). Never commit it.
4. **Create the Python environment and install dependencies**:
   ```bat
   setup.bat
   ```
   or, with prerequisite checks and clearer output:
   ```bat
   powershell -ExecutionPolicy Bypass -File scripts\setup_windows.ps1
   ```
   Add `-UseLockedDependencies` to install the exact versions in
   `backend/requirements-lock.txt` instead of the ranges. Set
   `MC5_NONINTERACTIVE=1` when another script drives `setup.bat`.
5. **Build the web interface** (skipped automatically when `frontend\dist`
   already exists):
   ```bat
   cd frontend
   npm ci
   npm run build
   cd ..
   ```
   `npm ci` uses the checked-in `package-lock.json`; do not substitute
   `npm install` when you want a reproducible tree.
6. **Configure secrets and services**: launch the app and complete the setup
   wizard (teacher name, retention/network behaviour, AI keys, optional
   Classroom). See section 5 — these values are not environment variables and
   must not be committed.
7. **Verify**:
   ```bat
   powershell -ExecutionPolicy Bypass -File scripts\verify_windows.ps1
   ```
   A fast pass: add `-Quick -SkipDesktop`. Exit code 0 means every required
   check passed; the report still lists skipped checks separately.
8. **Launch**:
   ```bat
   start.bat
   ```
   It serves `http://127.0.0.1:8642` and opens the browser. `start.bat 9000`
   selects another port; `start.bat --demo` runs with synthetic data and no keys.

## 4. Dependency inventory

### Backend (`backend/requirements.txt`, runtime)

| Package | Used by |
| --- | --- |
| `fastapi`, `uvicorn`, `python-multipart` | HTTP API, server, form/file uploads (FastAPI refuses to register upload routes without `python-multipart`). |
| `google-genai` | Gemini inference (`backend/app/gemini/`). |
| `google-auth`, `google-auth-oauthlib`, `requests` | Classroom OAuth and REST calls. |
| `openpyxl`, `reportlab` | XLSX and PDF report exports. |
| `pymupdf` (pinned `<2`) | PDF text extraction and page rasterisation for mark schemes. |
| `pypdf` | Submission validation and page-count/orientation checks. |
| `Pillow` | Image normalisation for submissions. |
| `cryptography` | Ed25519 signing/verification of update packages. |

Test-only (`backend/requirements-dev.txt`): `pytest`, `httpx` (Starlette
`TestClient` transport).

`backend/requirements-lock.txt` pins the exact verified versions of both sets,
including transitive packages. Regenerate it deliberately with
`pip freeze` after changing a dependency, and re-run the suite.

### Frontend (`frontend/package.json` + `package-lock.json`)

`react`, `react-dom`, `ui-style-kit-css`; dev: `typescript`, `vite`,
`@vitejs/plugin-react`, `@types/react`, `@types/react-dom`. The frontend tests
use Node's built-in test runner and a hand-written Chrome DevTools Protocol
driver — there is intentionally no Playwright/Puppeteer dependency.

### Desktop (`desktop/`)

WPF on `net10.0-windows`. Test project uses `xunit`, `Microsoft.NET.Test.Sdk`,
`xunit.runner.visualstudio`, `coverlet.collector` (restored by NuGet).

### Python packages that are *not* required

`numpy`, `onnxruntime`, `pymupdf4llm` and `pymupdf-layout` are dependencies of
PyMuPDF 2.x. The app does not use them; the `pymupdf<2` bound keeps them out of
the runtime and out of the portable release.

## 5. Configuration and secrets

The app is configured through its own wizard and Settings, not through `.env`.
`.env.example` in the repository root documents the handful of process
environment variables (`MC5_DATA_DIR`, `MC5_DEMO`, `MC5_NO_BROWSER`,
`MC5_PUBLIC`, `MC5_LOGIN_REQUIRED`, `MC5_ALLOWED_ORIGINS`, `MC5_LOG_LEVEL`,
`MC5_PORT`, and the frontend-only `MC5_DEV_API`). Math Checker does not load
`.env` files; set them in the shell that launches the app.

| Setting | Where it lives | Notes |
| --- | --- | --- |
| Teacher, network, retention, role | `data\config.toml` (or `%LOCALAPPDATA%\MathChecker\config.toml` for an installed copy) | Written by the wizard/Settings. |
| Gemini keys, OAuth tokens | `data\secrets.toml` | Git-ignored, written with restrictive permissions. |
| Google OAuth client | `data\secrets\google_oauth_client.json` | You create it in Google Cloud; it is not in the repository. |
| Database | `data\app.db` (SQLite) | Created on first launch from `backend\app\schema.sql`. No migration tool is needed; schema versioning is applied at startup. |
| Reports, mark schemes, logs | `data\reports\`, `data\mark_schemes\`, `data\logs\` | Git-ignored. |

`frontend\.env.development.example` is the *only* `.env` in the repository; it
configures the Vite dev server's backend origin (`MC5_DEV_API`) and holds no
secrets. Copy it to `frontend\.env.local` for `npm run dev`.

Checks before committing anything:

```bat
git status --short
git check-ignore -v data\secrets.toml data\app.db frontend\dist
```

## 6. Tests and verification

| Command | What it covers |
| --- | --- |
| `powershell -ExecutionPolicy Bypass -File scripts\verify_windows.ps1` | Everything below plus a real launch and HTTP probes. |
| `.venv\Scripts\python.exe -m pytest backend\tests -q` | 2087 backend tests (engine, blueprint, keys, jobs, tracking, reports, API, retry/failover; provider calls mocked). |
| `cd frontend && node --test "tests/*.test.ts"` | 87 frontend tests; 84 run without an app, 3 browser checks need a serving app. |
| `.venv\Scripts\python.exe scripts\run_all_tests.py` | Builds the UI, then runs the frontend and backend suites with one summary. |
| `dotnet build desktop\MathChecker.Desktop.slnx` + `dotnet test desktop\tests\...` | Desktop client build and 159 unit tests. |

Browser-driven UI checks discover Edge/Chrome and otherwise skip with a reason.
They need an app whose data contains an openable result:
set `WORKSPACE_LAYOUT_URL`/`WORKSPACE_RESULT_ID` and `PAGE_SCROLL_URL`, or leave
them unset and accept the skips.

## 7. Troubleshooting

| Symptom | Cause | Fix |
| --- | --- | --- |
| `ImportError: DLL load failed while importing _extra` | MSVC runtime missing. | `winget install --id Microsoft.VCRedist.2015+.x64`, then `scripts\verify_windows.ps1`. |
| `Python was not found; run without arguments to install from the Microsoft Store` | Windows' App Execution Alias stub is on PATH and no real Python is installed, or PATH is stale. | Install Python 3.12+, reopen the terminal, or disable the alias under *Settings → Apps → Advanced app settings → App execution aliases*. |
| `The system cannot find the path specified` pointing at a `python.exe` under another user name | `.venv` was copied from the old installation/another OS. | Delete or rename `.venv`, run `setup.bat` (it moves a foreign venv to `.venv.foreign` rather than deleting it). |
| Browser shows an empty page, server is up | `frontend\dist` is missing (it is build output, not committed). | `cd frontend && npm ci && npm run build`, restart `start.bat`. |
| `npm ci` warns that `esbuild` install scripts were not run | npm's script-approval prompt on newer npm versions. | The build still works (esbuild ships prebuilt platform binaries as optional dependencies). If it ever fails, run `npm install-scripts approve esbuild` and rebuild. |
| `vite: command not found` / `npm` not found | Node.js installed after the terminal opened. | Open a new terminal. |
| Port already in use | A copy of the app (or another program) is listening. | `start.bat` offers to restart its own copy; `python main.py --stop <port>` stops a verified instance. Never kill an unknown process. |
| Tests fail copying the project tree with `[WinError 5] Access is denied` | A cache folder (`__pycache__`, `.pytest_cache`) inherited ACLs from another Windows profile. | Delete the affected cache folders (they are regenerable) or take ownership: `takeown /f <folder> /r /d y`. |
| `fatal: detected dubious ownership in repository` | The folder was created by another Windows account/SID. | `git config --global --add safe.directory <path>` (or re-clone into a folder you own). |
| `powershell ... cannot be loaded because running scripts is disabled` | Execution policy. | Run with `powershell -ExecutionPolicy Bypass -File ...` (used in every command above). |
| Backend starts but Classroom says "not configured" | No OAuth client/token, or demo mode. | Expected without credentials; configure under **Settings → Classroom**. |
| `dotnet` not found after using `dotnet-install.ps1` | The SDK went to `%USERPROFILE%\.dotnet`, which is not on PATH. | Add `%USERPROFILE%\.dotnet` to the user PATH, or install the SDK with winget. |

## 8. What is not verifiable locally

These need credentials, an account, or another machine; they are documented
rather than tested:

- **Live Gemini marking** — needs a real API key and spends quota.
- **Google Classroom import/write-back** — needs an OAuth client and a signed-in
  Google account with Classroom data.
- **Portable release / installer build** — needs Inno Setup and the packaged
  runtime (`MathChecker_Distribution/build_release.bat`); it is a separate
  pipeline from running the source checkout.
- **Consistency of the suite on a truly fresh clone** — the suite was run in this
  restored checkout. A `git clone` into an empty folder removes every leftover
  file the old installation could have contributed, but the state of an
  untouched Windows image cannot be reproduced from here.

## 9. Data safety

- `data\` is git-ignored and holds the live class database. Never commit, copy
  into a test fixture, or share it.
- `data\secrets.toml` and `data\secrets\` are git-ignored and must stay out of
  screenshots, logs and reports.
- Test runs use throw-away folders (`%TEMP%\pytest-*`, `MC5_DATA_DIR`,
  `DASHBOARD_FIXTURE_URL`) and never write to `data\`.
- A factory reset exists in the UI (**Privacy & Data**) and deletes local data;
  it is not part of installation.
