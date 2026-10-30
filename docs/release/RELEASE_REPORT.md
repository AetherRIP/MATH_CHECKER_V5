# Release report — Math Checker 1.0.0 (Windows x64)

Built from this repository with `desktop\build_release.ps1`. Every number below
comes from the artefacts in `release\` and from the verification runs listed in
*Tests performed*; nothing here is estimated.

## Artefacts

| Item | Path | Size | SHA-256 |
|---|---|---|---|
| Installer | `release\MathChecker-Setup-x64-1.0.0.exe` | 78.0 MB | `5f2d35ff65c1851dfcecc7fec570395322925e5c6595c8120999b4279cc6c43e` |
| Portable ZIP | `release\MathChecker-Portable-x64-1.0.0.zip` | 107.3 MB | `f72c2e54f3cf01b97b79b300447c2b6cbe036c154b3a68efdc70ecea3baee01f` |
| Staged tree (input to both) | `desktop\artifacts\MATH_CHECKER_V5` | 258.5 MB | — |
| Build record | `release\release_manifest.json` | — | — |

* Built **2026-10-27 00:42** from the working tree that contains the readable
  dialog surfaces, the Google Classroom connect card and the Create assignment
  page; the previous artefacts (installer `611ffb41…`) are superseded.
* Version **1.0.0** — read from `<Version>` in
  `desktop\src\MathChecker.Desktop\MathChecker.Desktop.csproj`, so the app, the
  Start Menu entry, Installed apps and the installer filename cannot drift apart.
* Target **win-x64** (x64compatible), Windows 10/11.
* Installer technology **Inno Setup 6.7**, `PrivilegesRequired=lowest` — a
  per-user install into `%LOCALAPPDATA%\Programs\MathChecker`, no administrator
  account needed; an administrator can still pick an all-users folder in the
  wizard. lzma2/solid compression.

## What is bundled

| Component | Detail |
|---|---|
| WPF client | `dotnet publish -c Release -r win-x64 --self-contained true` → `desktop\V5.exe` + .NET 10 runtime + WPF natives. No .NET install required. |
| Python | CPython **3.13.7** (MSC v.1944, 64-bit) from `MathChecker_Distribution\build\runtime`, with its `.pyd` extensions (`_socket`, `_sqlite3`, `_ssl`, `_ctypes`, `_hashlib`, `_decimal`, `_multiprocessing`, …). |
| Python packages | fastapi, uvicorn, python-multipart, google-genai, google-auth(-oauthlib), requests, openpyxl, reportlab, PyMuPDF, pypdf, Pillow, cryptography — versions as pinned in `backend\requirements-lock.txt`. `pip`, `pytest`, `pygments` and the runtime's `Scripts\` entry points are stripped. |
| Backend | `main.py` + `backend\app` (+ `backend\scripts`), tests excluded. |
| PDF tooling | PyMuPDF / pypdf / Pillow, compiled into the runtime — no external executable. |
| Assets | compiled XAML, fonts, icons, launcher (`start_desktop.bat`), `README.txt`, `VERSION`. |

Deliberately **not** in the package: the repository's `data\` folder,
`config.toml`, `secrets.toml`, `.env`, `.git`, any `*.db`, any student `*.pdf`,
`__pycache__`, `*.pyc`, `*.pdb`, `desktop\artifacts`. The build fails if any of
them (or this developer's absolute paths) appear in the staged tree.

## External requirements (not bundled, and not claimed to be)

* **Internet access + an AI provider key** (Gemini) for corrections. The user
  enters it in Settings → AI keys after installation; it is stored in
  `%LOCALAPPDATA%\MathChecker`, never in the package or a log.
* **A Google Classroom account**, optional, only for classroom/assignment sync.
  Manual upload, mark schemes, results, reports and manual marking work with no
  account.
* Installation and first launch need no network access.

## Installation layout and data

| What | Where |
|---|---|
| Program | `{app}` = `%LOCALAPPDATA%\Programs\MathChecker` (default, user-choosable) |
| Database, mark schemes, papers, results, keys | `%LOCALAPPDATA%\MathChecker` |
| Window state, desktop settings, logs | `%APPDATA%\MathCheckerDesktop` |

The installed backend resolves its data folder itself
(`backend\app\data_location.py`): an installed copy under `Programs\` has no
`data\` beside it and no `.git`, so it lands on the per-user folder — never in
`{app}`, which the next install replaces. Uninstall removes the program,
shortcuts and registry entry and keeps user data; a reinstall finds the same
classes, papers and marks.

## Tests performed

### Build-time checks — `desktop\build_release.ps1`, 28 checks, 0 failed

Recorded in `release\release_manifest.json`:

* `runtime\python.exe` present, all required `.pyd` extensions present, all
  required packages present;
* the bundled interpreter imports the whole backend stack
  (`fastapi, uvicorn, fitz, PIL, pydantic_core, cryptography, google.genai,
  pypdf, openpyxl, reportlab, multipart, sqlite3, ssl`) from a sanitised
  environment;
* desktop test suite passes;
* `V5.exe` published, every required file staged;
* no forbidden file (`data\`, `.env`, `config.toml`, `secrets.toml`, `.git`,
  `*.db`, `*.pdf`, `pip`, artefacts) and no developer absolute path in the
  shipped binaries;
* the bundled backend starts from the staged tree with a system-only `PATH`,
  answers `/api/health`, and initialises its database;
* no bytecode caches remain in the package;
* the installer was produced and its payload is plausible (78.0 MB).

### Desktop test suite

`dotnet test desktop\tests\MathChecker.Desktop.Tests` → **265 passed, 0 failed,
0 skipped** (2 m 9 s).

### Installer verification — `desktop\verify_installer.ps1`, 33 checks, 0 failed

Silent install of `MathChecker-Setup-x64-1.0.0.exe` into a fresh temp folder,
with `%LOCALAPPDATA%\MathChecker` moved aside first so the launch is a genuine
first launch:

* installer exit code 0; client, uninstaller, bundled backend and bundled
  Python all present; Start Menu entry created; listed in Installed apps as
  “Math Checker 1.0.0” with the matching registry version; no `data\` folder
  inside the installation; no developer path inside the installed binaries;
* launched through `cmd` with `PATH` reduced to `System32` and `DOTNET_ROOT`,
  `MC5_DATA_DIR`, `MC5_DEMO`, `MC5_LOGIN_REQUIRED` cleared — the window opened
  with the main window on top and **no sign-in prompt** (a fresh install has no
  account; an earlier build gated the UI on the raw `login_required` setting
  and asked for a password that could not exist),
  its backend came from the installed folder
  (`…\runtime\python.exe main.py --serve 8642`), answered `/api/health`, created
  `%LOCALAPPDATA%\MathChecker\app.db`, reported the per-user data folder, and
  reported demo mode **off**;
* restart: window came back, backend restarted, data survived, and closing the
  client stopped its backend;
* uninstall: exit 0, program files, Start Menu entry and registry entry removed,
  **user data kept**;
* reinstall: exit 0, client back, user data still present; test installation
  removed afterwards and the machine's own data folder restored.

### Portable tree launched with no developer tools

The staged tree was copied outside the repository and `desktop\V5.exe` started
from `cmd` with `PATH=System32;Windows` and `DOTNET_ROOT`/`MC5_DATA_DIR`
cleared: window opened, backend ran as
`<copy>\runtime\python.exe main.py --serve 8642`, `/api/health` returned `ok`,
and `/api/privacy/info` reported a freshly created
`%USERPROFILE%\AppData\Local\MathChecker`.

### Not verified (limitations)

* **No pristine Windows installation was tested.** This machine has no Windows
  Sandbox (`C:\Windows\System32\WindowsSandbox.exe` is absent and enabling the
  optional feature needs elevation) and no separate VM. The closest available
  proof is the sanitised-`PATH`, no-`MC5_*`, fresh-data-folder launch above; a
  clean-machine install remains unproven until someone runs the installer on a
  PC that never had Python, .NET or this repository. Note that the client is
  self-contained .NET and the backend runs the bundled interpreter, so the
  remaining risk is an unnoticed OS component, not a missing runtime.
* **No live correction was run from the installed copy.** Doing so would mean
  copying this developer's private Gemini key into the test installation, which
  the release process deliberately never does. The correction pipeline itself is
  unchanged by this packaging work and is covered by the backend test suite and
  the existing marking verification scripts.
* **Google Classroom OAuth** was not exercised from the installed copy (no
  account was connected); the installed app starts and works without it.

## Rebuilding

```powershell
powershell -ExecutionPolicy Bypass -File desktop\build_release.ps1
```

Cleans the staged tree, resolves a real .NET SDK (a runtime-only `dotnet` on
`PATH` is rejected), validates the bundled runtime, runs the desktop tests,
publishes the client, stages backend + runtime + launcher + guide, validates the
tree (required files, forbidden files, developer paths), smoke-tests the backend
from the staged tree, writes the portable ZIP, compiles the installer, and
writes `release_manifest.json` and `SHA256SUMS.txt`. `-SkipTests`,
`-SkipInstaller`, `-Version` and `-Output` exist for partial runs.

Build machine needs: .NET 10 SDK, Inno Setup 6, PowerShell (7+ recommended for
the sanitised-environment smoke test). The bundled runtime is an input
(`MathChecker_Distribution\build\runtime`); regenerating it is the separate
`MathChecker_Distribution\build_release.bat` pipeline.

Then verify the artefact itself:

```powershell
powershell -ExecutionPolicy Bypass -File desktop\verify_installer.ps1
```

It exits 0 only if all 33 checks pass and leaves a timestamped report in
`docs\logs\INSTALLER_VERIFY_*.txt`.

## Installer size

78.0 MB compressed from a 258.5 MB staged tree. Main contributors: the
self-contained WPF client with the .NET runtime (~130 MB before compression) and
the pruned CPython runtime with site-packages (~127 MB). Both are required for
the “no Python, no .NET” promise; the portable ZIP is larger (107.3 MB) because
`Compress-Archive` uses deflate rather than lzma2.
