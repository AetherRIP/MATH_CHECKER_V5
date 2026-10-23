# Math Checker Desktop (WPF)

The native Windows client for MATH_CHECKER_V5. It contains no correction logic:
it starts or attaches to the existing Python backend and uses its HTTP API, so
the engine, marking rules, prompts, database and Classroom integration stay
exactly as they are.

## Run

```powershell
dotnet build src\MathChecker.Desktop\MathChecker.Desktop.csproj
dotnet run   --project src\MathChecker.Desktop\MathChecker.Desktop.csproj
```

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
  no .NET/Python needed); double-click `desktop\MATH_CHECKER_V5.exe`;
* `MATH_CHECKER_V5_Portable.zip` — the same folder zipped;
* `MATH_CHECKER_V5_Setup.exe` — per-user installer (no admin rights), built when
  Inno Setup 6 is installed;
* `release_manifest.json` — what the build contains.

`test_release.ps1` runs the staged build the way a user does: window, bundled
backend, a real correction, PDF export, single instance, and shutdown with no
leftover process or port. `-KeepTemp` keeps its logs and PDFs.

## Documentation

* [Audit and architecture](../docs/desktop-migration/01-audit-and-architecture.md)
* [Feature inventory](../docs/desktop-migration/02-feature-inventory.md)
* [Build and release](../docs/desktop-migration/03-build-and-release.md)
* [Migration status and verification](../docs/desktop-migration/04-migration-status.md)
* [Setup and development](../docs/desktop-migration/05-setup-and-development.md)

The UI is intentionally a functional placeholder; it is ready to be replaced by
the design phase without touching the view models, API client or backend.
