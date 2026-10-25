# Math Checker Desktop (WPF)

The native Windows client for MATH_CHECKER_V5. It contains no correction logic:
it starts or attaches to the existing Python backend and uses its HTTP API, so
the engine, marking rules, prompts, database and Classroom integration stay
exactly as they are.

## Run

The one-step launcher, from the project root, rebuilds the client from the code
on disk (when a .NET SDK is installed) and starts it:

```bat
start_desktop.bat
```

It always opens the interface the current source describes. An already-open
window is offered for closing first, because a running window keeps the
interface it was started with - starting the exe again only raises that older
window. A build failure is reported instead of falling back to a previous
binary, and the SDK is probed with `dotnet --list-sdks` (including the per-user
install in `%USERPROFILE%\.dotnet`) so a runtime-only `dotnet` on `PATH` no
longer makes the launcher skip the rebuild.

Or by hand:

```powershell
dotnet build src\MathChecker.Desktop\MathChecker.Desktop.csproj
dotnet run   --project src\MathChecker.Desktop\MathChecker.Desktop.csproj
```

The executable is **`V5.exe`** (the assembly name is `V5`), so a published or
staged release is started by double-clicking it. Nothing has to be started
beside it — no launcher script for the backend: this process locates
`main.py`, chooses the interpreter (bundled `runtime\python.exe`, the
checkout's `.venv`, or PATH), starts `main.py --serve <port>`, waits for
`/api/health`, and stops only the backend it started. `start_desktop.bat` only
chooses which `V5.exe` to start: the one built from this checkout, or the
staged package in `artifacts\` when no SDK is installed.

The client finds `main.py` beside the executable or in a parent folder, and uses
a bundled `runtime\python.exe`, the checkout's `.venv`, or Python from PATH.

## Test

```powershell
dotnet test tests\MathChecker.Desktop.Tests\MathChecker.Desktop.Tests.csproj
dotnet run --project tools\MathChecker.Desktop.E2E      # real backend, demo mode, temp data
```

## Package

```powershell
powershell -ExecutionPolicy Bypass -File build_release.ps1
```

Produces, in `desktop\artifacts`:

* `MATH_CHECKER_V5\` — client + backend + bundled Python runtime (no install,
  no .NET/Python needed); double-click `start_desktop.bat` in its root, or
  `desktop\V5.exe` inside it;
* `MATH_CHECKER_V5_Portable.zip` — the same folder zipped;
* `MATH_CHECKER_V5_Setup.exe` — per-user installer (no admin rights), built when
  Inno Setup 6 is installed;
* `release_manifest.json` — what the build contains.

To hand the app to someone, send `MATH_CHECKER_V5_Portable.zip` (one file) and
tell them to extract it and double-click **`start_desktop.bat`**. Their machine
needs no Python, no .NET, no Node and no admin rights: the client carries its
own .NET runtime and `runtime\python.exe` is the backend's interpreter. Windows
may warn that the `.exe` is from an unknown publisher because the build is not
code-signed; the same applies to `MATH_CHECKER_V5_Setup.exe`, which is the
one-file alternative for people who would rather install than unzip.

`test_release.ps1` runs the staged build the way a user does: window, bundled
backend, a real correction, PDF export, single instance, and shutdown with no
leftover process or port. `-KeepTemp` keeps its logs and PDFs.

## Documentation

* [Audit and architecture](../docs/desktop-migration/01-audit-and-architecture.md)
* [Feature inventory](../docs/desktop-migration/02-feature-inventory.md)
* [Build and release](../docs/desktop-migration/03-build-and-release.md)
* [Migration status and verification](../docs/desktop-migration/04-migration-status.md)
* [Setup and development](../docs/desktop-migration/05-setup-and-development.md)

The interface has been redesigned on top of the same view models, API client and
backend, so a change of appearance never requires a change in the marking
engine.
