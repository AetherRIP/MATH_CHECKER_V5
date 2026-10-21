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

Produces `artifacts\MathChecker_Desktop_Portable.zip` (client + backend +
bundled Python) and, when Inno Setup 6 is installed, the installer.

## Documentation

* [Audit and architecture](../docs/desktop-migration/01-audit-and-architecture.md)
* [Feature inventory](../docs/desktop-migration/02-feature-inventory.md)
* [Build and release](../docs/desktop-migration/03-build-and-release.md)
* [Migration status and verification](../docs/desktop-migration/04-migration-status.md)
* [Setup and development](../docs/desktop-migration/05-setup-and-development.md)

The UI is intentionally a functional placeholder; it is ready to be replaced by
the design phase without touching the view models, API client or backend.
