# Desktop build and release

The desktop client is a WPF application (`net10.0-windows`). It never contains a
copy of the correction engine: it starts, attaches to, and talks to the existing
Python backend over HTTP.

## 1. What a release contains

`desktop\build_release.ps1` stages one folder that the client can run from
without any developer environment:

```
MathCheckerDesktop\
  main.py                     existing V5 entry point (unmodified)
  backend\                    existing backend package (unmodified)
  runtime\python.exe          bundled Python + the backend's dependencies
  desktop\                    published WPF client (self-contained .NET)
    MathChecker.Desktop.exe
    ...
```

The client searches for `main.py` starting at its own folder and walking up to
eight parents, then uses `runtime\python.exe` from that same root
(`BackendLocator.FindBackendDirectory`, `ResolvePython`). Nothing about a
developer checkout is required or referenced at runtime.

## 2. Prerequisites

| Purpose | Requirement | Notes |
|---|---|---|
| Build the client | .NET SDK 10 (`dotnet --version` = 10.x) | Windows only (WPF) |
| Build the bundle | A prepared runtime folder containing `python.exe` | Default `MathChecker_Distribution\build\runtime`, produced by `MathChecker_Distribution\build_release.bat` (`scripts\build_runtime.py`) |
| Build the installer | Inno Setup 6 (`ISCC.exe`) | **Not installed on this machine** — see §5 |
| Run an installed build | Windows 10/11 x64 | No .NET and no Python installation needed |

## 3. Build commands

```powershell
# From the repository root
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
   `win-x64`, single folder (no trimming: WPF does not support it);
2. copies `main.py`, `backend\` and `requirements.txt` from the checkout,
   removing `__pycache__`;
3. copies the bundled Python runtime;
4. asserts the layout (`main.py`, `backend\app\main.py`, `runtime\python.exe`,
   `desktop\MathChecker.Desktop.exe`);
5. writes `desktop\artifacts\MathChecker_Desktop_Portable.zip`;
6. builds `MathChecker_Desktop_Setup.exe` when `ISCC.exe` is found, and prints
   the exact command when it is not;
7. writes `desktop\artifacts\release_manifest.json`.

Developer-only build commands:

```powershell
dotnet build  desktop\src\MathChecker.Desktop\MathChecker.Desktop.csproj
dotnet test   desktop\tests\MathChecker.Desktop.Tests\MathChecker.Desktop.Tests.csproj
dotnet run --project desktop\tools\MathChecker.Desktop.E2E
```

## 4. Verified on this machine (2026-10-21)

| Check | Result |
|---|---|
| `dotnet publish` (Release, win-x64, self-contained) | succeeded, 0 warnings, 0 errors |
| Layout assertions | all four required paths present |
| Portable ZIP | `MathChecker_Desktop_Portable.zip`, **128.3 MB** (134 553 777 bytes) |
| Launch from the staged bundle with `backendDirectory` cleared | client auto-discovered `artifacts\MathCheckerDesktop\main.py` |
| Interpreter actually used | `artifacts\MathCheckerDesktop\runtime\python.exe` (bundled, not the checkout's `.venv`) |
| `/api/health` from the packaged run | `200 {"status":"ok"}` |
| Window and navigation | `Math Checker`, 26 navigation items, pages rendered |
| Close | process exited, backend tree stopped, **no orphan `python.exe`** |

The run used Demo Mode with a scratch data folder, so no real student record was
touched. `%APPDATA%\MathCheckerDesktop\settings.json` was backed up before the
test and restored afterwards.

## 5. Installer

`desktop\installer\MathCheckerDesktop.iss` (Inno Setup 6) installs the staged
tree into `%ProgramFiles%\Math Checker Desktop`, creates Start Menu entries
(application + desktop log folder), offers an optional desktop shortcut, and
registers an uninstaller. It does not delete the client's `%APPDATA%` settings or
the backend's data folder.

Build it (requires Inno Setup 6):

```powershell
# after build_release.ps1 has staged the tree
iscc /DSourceDir="E:\MATH_CHECKER_V5\desktop\artifacts\MathCheckerDesktop" `
     /DOutputDir="E:\MATH_CHECKER_V5\desktop\artifacts" `
     "E:\MATH_CHECKER_V5\desktop\installer\MathCheckerDesktop.iss"
```

**Status: not built and not installed here.** `ISCC.exe` is not present on this
machine (neither `C:\Program Files\Inno Setup 6` nor PATH), so the installer
script is delivered but its install/uninstall/Start-Menu/upgrade behaviour is
**unverified**. Do not claim a tested installer until someone with Inno Setup 6
runs the command above and installs the result on a clean Windows machine.

Checks that remain to be done with the installer:

1. install as administrator, confirm the Start Menu shortcut launches the app;
2. confirm the first start finds the bundled backend and reaches
   "Backend ready" without any Python/.NET install;
3. run a correction and export a PDF from the installed copy;
4. uninstall and confirm no program files remain while `%APPDATA%` settings and
   the backend data folder are untouched;
5. install over the previous version (same `AppId`) and confirm settings and data
   survive.

## 6. Versioning and updates

* Client identity: `desktop\src\MathChecker.Desktop\MathChecker.Desktop.csproj`
  (`Version`, `FileVersion`, `AssemblyVersion` — currently `1.0.0`), icon
  `desktop\installer\app.ico`.
* Installer version: `AppVersion` in `MathCheckerDesktop.iss`; the `AppId` must
  stay stable so upgrades replace rather than duplicate an installation.
* Backend version is the existing product version (`5.2.1` in the running app);
  the desktop does not version the engine independently.
* There is no auto-update channel for the client. Updates are a new installer
  over the old one, or a new portable ZIP.

## 7. Signing and distribution

Builds are unsigned. Windows SmartScreen will warn on an unknown publisher; for
distribution, sign both the executable and the installer with an Authenticode
certificate:

```powershell
signtool sign /fd SHA256 /tr http://timestamp.digicert.com /td SHA256 `
    /f certificate.pfx /p <password> MathChecker.Desktop.exe
signtool sign /fd SHA256 /tr http://timestamp.digicert.com /td SHA256 `
    /f certificate.pfx /p <password> MathChecker_Desktop_Setup.exe
```

Do not put certificates, passwords, `.env` files, backend `data\secrets.toml` or
the `data\` folder into a release or a repository.
