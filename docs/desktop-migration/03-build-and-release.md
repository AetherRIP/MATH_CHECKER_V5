# Desktop build and release

The desktop client is a WPF application (`net10.0-windows`). It never contains a
copy of the correction engine: it starts, attaches to, and talks to the existing
Python backend over HTTP. The release therefore ships the client, the unchanged
V5 backend and a bundled Python runtime in one folder; double-clicking
`V5.exe` starts everything.

## 1. What a release contains

`desktop\build_release.ps1` stages the following tree:

```
desktop\artifacts\MATH_CHECKER_V5\
  main.py                     existing V5 entry point (unmodified)
  backend\                    existing backend package (unmodified)
  runtime\python.exe          bundled Python + the backend's dependencies
  desktop\                    published WPF client (self-contained .NET)
    V5.exe                    the launcher people double-click
    ...                       .NET runtime and client DLLs (no .NET install needed)
```

The client searches for `main.py` starting at its own folder and walking up to
eight parents, then uses `runtime\python.exe` from that same root
(`BackendLocator.FindBackendDirectory`, `ResolvePython`). The installed and
portable layouts are therefore identical; nothing about a developer checkout is
referenced at runtime.

`V5.exe` is a normal WPF process, not a console wrapper: it has no
terminal window, enforces one instance per user session (a second double-click
raises the existing window), waits for `/api/health` before showing the backend
as ready, and stops the backend process tree it started when the window closes.

## 2. Prerequisites

| Purpose | Requirement | Notes |
|---|---|---|
| Build the client | .NET SDK 10 (`dotnet --version` = 10.x) | Windows only (WPF) |
| Build the bundle | A prepared runtime folder containing `python.exe` | Default `MathChecker_Distribution\build\runtime`, produced by `MathChecker_Distribution\build_release.bat` (`scripts\build_runtime.py`) |
| Build the installer | Inno Setup 6 (`ISCC.exe`) | Detected in `Program Files`, `Program Files (x86)`, the per-user `%LOCALAPPDATA%\Programs\Inno Setup 6`, or on `PATH` |
| Run the portable/installed build | Windows 10/11 x64 | No .NET and no Python installation needed |
| Build only the bundle | `-SkipInstaller` | Prints the exact `iscc` command instead |

## 3. Build commands

```powershell
# From the repository root: client + backend + runtime + ZIP + installer
powershell -ExecutionPolicy Bypass -File desktop\build_release.ps1

# Optional arguments
#   -Configuration Release
#   -Runtime win-x64
#   -BackendRuntime <folder containing python.exe>
#   -Output <folder>            (default desktop\artifacts)
#   -SkipInstaller
```

The script:

1. `dotnet publish` of `desktop\src\MathChecker.Desktop` — self-contained,
   `win-x64`, single folder (no trimming: WPF does not support it), producing
   `desktop\V5.exe`;
2. copies `main.py`, `backend\` and `requirements.txt` from the checkout,
   removing `__pycache__`;
3. copies the bundled Python runtime and strips its `__pycache__` folders, so
   caches from the build machine never ship;
4. asserts the layout (`main.py`, `backend\app\main.py`, `runtime\python.exe`,
   `desktop\V5.exe`);
5. writes `desktop\artifacts\MATH_CHECKER_V5_Portable.zip`;
6. builds `desktop\artifacts\MATH_CHECKER_V5_Setup.exe` when `ISCC.exe` is found;
7. writes `desktop\artifacts\release_manifest.json`.

Developer-only build commands:

```powershell
dotnet build  desktop\src\MathChecker.Desktop\MathChecker.Desktop.csproj
dotnet test   desktop\tests\MathChecker.Desktop.Tests\MathChecker.Desktop.Tests.csproj
dotnet run --project desktop\tools\MathChecker.Desktop.E2E
```

## 4. Release tests

`desktop\test_release.ps1` exercises a staged build the way a user does: it
copies the release outside the repository, starts `V5.exe`,
confirms the window and the *bundled* interpreter, uploads one correction,
waits for the job and its stored result, exports a PDF, launches a second copy
to check the single-instance guard, closes the window and confirms no backend
process or listening port is left behind.

```powershell
powershell -ExecutionPolicy Bypass -File desktop\test_release.ps1
powershell -ExecutionPolicy Bypass -File desktop\test_release.ps1 -KeepTemp   # keep logs/PDFs
```

It backs up and restores `%APPDATA%\MathCheckerDesktop\settings.json` and runs
in Demo Mode with a scratch data folder, so no real student record is touched.

## 5. Verified on this machine (2026-10-22)

| Check | Result |
|---|---|
| `dotnet publish` (Release, win-x64, self-contained) | succeeded |
| `dotnet test desktop\tests\MathChecker.Desktop.Tests` | **137 passed, 0 failed** |
| `desktop\test_release.ps1` | **19/19 checks passed** |
| Portable folder | `desktop\artifacts\MATH_CHECKER_V5\` — client, backend and runtime (328 MB on disk) |
| Portable ZIP | `MATH_CHECKER_V5_Portable.zip`, **128.6 MB** |
| Installer | `MATH_CHECKER_V5_Setup.exe`, **93.7 MB**, Inno Setup 6.7.3 |
| Launcher actually run | `V5.exe` from a copy in `%TEMP%`, window title `MATH_CHECKER_V5` |
| Interpreter actually used | the copied `runtime\python.exe main.py --serve <port>` — not the checkout's `.venv` |
| `/api/health` and `/api/dashboard` | `status=ok`, Demo Mode on |
| Real correction through the packaged backend | job `COMPLETED` (`stage=Correction completed`), stored result, `9/12`, 3 questions |
| PDF export from the packaged backend | 4 553 bytes, `%PDF` header |
| Single instance | second launch exited and only one client process existed |
| Close / shutdown | client exited, backend tree stopped, port closed |
| Startup failure | with `main.py` removed the packaged app showed the banner *"The correction backend is not available."* + the reason and a **Retry** button instead of crashing |
| Data location | the packaged backend used the configured data folder, not the repository |
| Installer install → run → uninstall | **11/11 checks passed**: silent per-user install, launcher exists, backend + runtime present, installed window opened, `/api/health` ok, dashboard served, clean exit, uninstaller exit 0, program folder removed |

The verification runs used Demo Mode and scratch data folders.

## 5a. Re-verified after the `V5.exe` rename (2026-10-23)

| Check | Result |
|---|---|
| `dotnet publish` + `build_release.ps1` | succeeded |
| `dotnet test desktop\tests\MathChecker.Desktop.Tests` | **160 passed, 0 failed** |
| `desktop\test_release.ps1` | **19/19 checks passed** |
| Missing-interpreter / foreign-port / no-keys / other-cwd tests | **15/15 checks passed** |
| Launcher | `desktop\artifacts\MATH_CHECKER_V5\desktop\V5.exe`, 199 KB, PE32+ |
| Portable folder / ZIP | 281 MB on disk / **111.7 MB** (was 128.6 MB before runtime caches were stripped) |
| Installer | `MATH_CHECKER_V5_Setup.exe`, **80.3 MB**, built with Inno Setup 6.7.3 |
| Installer install → run → uninstall | **11/11 checks passed**: silent per-user install into a scratch folder, launcher/backend/runtime present, uninstaller registered, installed window opened, `/api/health` ok, dashboard served, clean close, uninstaller exit 0, folder removed |

The Python runtime caches that used to travel inside the ZIP were removed from
the build, which is where the size drop comes from. Nothing else in the layout
changed. Inno Setup 6 (`ISCC.exe`) is found automatically in
`%LOCALAPPDATA%\Programs\Inno Setup 6`; without it the script still builds the
folder and ZIP and prints the exact `iscc` command.

## 6. Installer

`desktop\installer\MathCheckerDesktop.iss` (Inno Setup 6) is a **per-user**
install: `PrivilegesRequired=lowest`, default folder
`%LOCALAPPDATA%\Programs\MATH_CHECKER_V5`, Start Menu entries (application +
desktop log folder), an optional desktop shortcut, and an uninstaller registered
in *Apps & features*. No administrator rights are needed.

The uninstaller removes the program folders (`desktop\`, `backend\`, `runtime\`,
generated `__pycache__`) but deliberately keeps:

* `%APPDATA%\MathCheckerDesktop\settings.json` and the logs in
  `%LOCALAPPDATA%\MathCheckerDesktop\logs` — window state and diagnostics;
* the backend's data folder (`%LOCALAPPDATA%\Programs\MATH_CHECKER_V5\data` or
  one the teacher configured) — classes, marked papers, API keys.

Build it (after `build_release.ps1` has staged the tree):

```powershell
iscc /DSourceDir="E:\MATH_CHECKER_V5\desktop\artifacts\MATH_CHECKER_V5" `
     /DOutputDir="E:\MATH_CHECKER_V5\desktop\artifacts" `
     "E:\MATH_CHECKER_V5\desktop\installer\MathCheckerDesktop.iss"
```

`build_release.ps1` runs exactly this when it finds `ISCC.exe`, so normally no
separate command is needed. Silent install/uninstall for a scripted check:

```powershell
MATH_CHECKER_V5_Setup.exe /VERYSILENT /SUPPRESSMSGBOXES /NORESTART /NOICONS
& "$env:LOCALAPPDATA\Programs\MATH_CHECKER_V5\unins000.exe" /VERYSILENT /SUPPRESSMSGBOXES
```

**Status: built and tested here** (install → launch installed app → health and
dashboard → clean exit → uninstall → folder removed). Upgrade-in-place over an
older version shares the same `AppId` but was not exercised; the settings and
data folders live outside `{app}`, so an upgrade preserves them.

## 7. Versioning and updates

* Client identity: `desktop\src\MathChecker.Desktop\MathChecker.Desktop.csproj`
  (`Version`, `FileVersion`, `AssemblyVersion` — currently `1.0.0`), icon
  `desktop\installer\app.ico`, assembly name `MATH_CHECKER_V5`.
* Installer version: `AppVersion` in `MathCheckerDesktop.iss`; the `AppId` must
  stay stable so upgrades replace rather than duplicate an installation.
* Backend version is the existing product version (`5.2.1` in the running app);
  the desktop does not version the engine independently.
* There is no auto-update channel for the client. Updates are a new installer
  over the old one, or a new portable ZIP.

## 8. Signing and distribution

Builds are unsigned. Windows SmartScreen will warn on an unknown publisher; for
distribution, sign both the executable and the installer with an Authenticode
certificate:

```powershell
signtool sign /fd SHA256 /tr http://timestamp.digicert.com /td SHA256 `
    /f certificate.pfx /p <password> V5.exe
signtool sign /fd SHA256 /tr http://timestamp.digicert.com /td SHA256 `
    /f certificate.pfx /p <password> MATH_CHECKER_V5_Setup.exe
```

Do not put certificates, passwords, `.env` files, backend `data\secrets.toml` or
the `data\` folder into a release or a repository.
