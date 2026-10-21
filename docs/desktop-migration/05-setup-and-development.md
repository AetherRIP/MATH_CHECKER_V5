# Desktop development and setup

## 1. Tools

| Tool | Version used | Needed for |
|---|---|---|
| Windows | 10/11 x64 | WPF, UI Automation probes |
| .NET SDK | 10.0.402 (`net10.0-windows` target) | building/running the client and tests |
| Python | 3.13 (bundled runtime) or the checkout's `.venv` | the existing V5 backend |
| Inno Setup 6 | optional | building the installer |

The client has no NuGet package dependencies outside the .NET/WPF framework
itself, so `dotnet build` needs no extra feed.

## 2. Repository layout

```
desktop\
  MathChecker.Desktop.slnx           solution: app, tests, E2E tool
  src\MathChecker.Desktop\
    App.xaml(.cs)                    startup, unhandled-exception handling
    MainWindow.xaml(.cs)             window, navigation, backend supervision
    Views\                           one UserControl per feature area
    ViewModels\                      one view model per page (MVVM)
    Models\ApiModels.cs              DTOs matching the backend's JSON
    Core\
      Api\MathCheckerApiClient.cs    every HTTP call, SSE reader, uploads
      Backend\BackendHost.cs         locate/start/attach/stop the Python backend
      Backend\BackendLocator.cs      backend folder + interpreter discovery
      Configuration\DesktopSettings.cs  %APPDATA% settings file
      Diagnostics\FileLog.cs         rolling, redacted log
      AppSession.cs                  services handed to every view model
    Converters\, Mvvm\               binding helpers, commands
  tests\MathChecker.Desktop.Tests\   xUnit
  tools\MathChecker.Desktop.E2E\     end-to-end run against the real backend
  installer\                         app.ico + Inno Setup script
  build_release.ps1                  release build (§03)
```

## 3. First run from the checkout

```powershell
# backend dependencies (once)
.\.venv\Scripts\python.exe -m pip install -r backend\requirements-dev.txt

# client
dotnet build desktop\src\MathChecker.Desktop\MathChecker.Desktop.csproj
dotnet run   --project desktop\src\MathChecker.Desktop\MathChecker.Desktop.csproj
```

On start the client:

1. reads `%APPDATA%\MathCheckerDesktop\settings.json` (created on first close);
2. probes `127.0.0.1:<port>` `/api/health`;
   * answers as Math Checker → attaches (does not start a second backend);
   * something else answers → picks a nearby free port and starts its own;
   * nothing answers → starts `main.py --serve <port>` from the backend folder;
3. waits for readiness (default 120 s) and reads the real data folder from
   `/api/privacy/info`;
4. shows the sign-in window only when the backend says login is required
   (public mode); local mode needs nothing;
5. opens the first-run wizard only when `/api/setup/status` reports `first_run`
   and the backend is not in Demo Mode.

If the backend cannot be found the window still opens and the banner names the
exact problem, with a Retry button.

## 4. Settings file

`%APPDATA%\MathCheckerDesktop\settings.json` — paths and window state only, no
credentials:

| Key | Meaning |
|---|---|
| `backendDirectory` | folder containing `main.py`; empty = auto-discover |
| `pythonExecutable` | explicit interpreter; empty = bundled runtime, `.venv`, then PATH |
| `dataDirectory` | passed to the backend as `MC5_DATA_DIR`; empty = backend default |
| `port` | expected backend port (default 8642) |
| `startBackendAutomatically` / `attachToRunningBackend` | startup policy |
| `startupTimeoutSeconds` | readiness timeout (default 120) |
| `environmentVariables` | extra backend environment (e.g. `MC5_DEMO=1` for demo mode) |
| `lastExportDirectory`, `lastPage`, window size/maximised | UI state |

Gemini keys, OAuth tokens and the database live in the backend's own data folder
(`secrets.toml`, `config.toml`, `app.db`). The desktop never reads or stores them.

## 5. Tests

```powershell
# unit/integration tests for the client (xUnit)
dotnet test desktop\tests\MathChecker.Desktop.Tests\MathChecker.Desktop.Tests.csproj

# end-to-end against a real backend, isolated temp data, demo mode
dotnet run --project desktop\tools\MathChecker.Desktop.E2E

# backend regression suite (unchanged code, run to prove it)
.\.venv\Scripts\python.exe -m pytest backend\tests -q
```

`MathChecker.Desktop.E2E` needs no credentials: it sets `MC5_DEMO=1`, creates a
temporary data folder, starts the backend, submits a real unified correction
(mock marking is forced by Demo Mode), exports PDF/CSV, checks version history
and settings, then stops the backend and asserts the port is free.

## 6. Adding or changing a page

1. add a view model deriving from `PageViewModel` (set `Group` for the sidebar);
2. register it in `MainViewModel.BuildNavigation()`;
3. add a `UserControl` under `Views\`;
4. map the view model to the view with a `DataTemplate` in `App.xaml`.

Keep HTTP calls in the view model through `Session.Api`; never call HTTP from
code-behind. Display-only values bound to `TextBox.Text` must be
`Mode=OneWay` — `TextBox.Text` is two-way by default and WPF throws at layout
time for a read-only property (this was the cause of several blank pages before
it was fixed).

## 7. Troubleshooting

| Symptom | Cause / action |
|---|---|
| Banner: "backend was not found" | set `backendDirectory` or run from a folder whose parent contains `main.py` |
| Banner: "No Python interpreter" | install Python 3.12+, or set `pythonExecutable`, or ship the bundled `runtime\` |
| "A Math Checker backend is already running on this port" | close the other instance or enable `attachToRunningBackend` |
| Banner: "The backend stopped unexpectedly" | read the tail shown in the banner and the backend log (`Diagnostics` page) |
| Pages show `HTTP 403` | developer-only endpoints (`/diagnostics`, `/dev/prompts`) with a non-developer role |
| Every page fails after a long uptime | use "Restart backend" in the toolbar; the client restarts only the process it owns |
| Client log | `%LOCALAPPDATA%\MathCheckerDesktop\logs\desktop-YYYYMMDD.log` (redacted) |
| Backend log | path shown on the `Diagnostics` page, under the backend data folder |

## 8. Security notes for developers

* Never commit `data\`, `secrets.toml`, `%APPDATA%\MathCheckerDesktop`, or any
  `.env` file.
* The desktop forwards the backend's CSRF header when the session provides one
  and keeps the session cookie in memory only.
* Do not log request bodies or file paths that contain student names; `FileLog`
  redacts secrets but not personal data — keep new log lines minimal.
