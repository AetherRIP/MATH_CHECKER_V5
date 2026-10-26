# Math Checker — install and first run

This copy of Math Checker is self-contained. You do **not** need Python, .NET,
Git, Visual Studio, Node.js or any other development tool.

## Install (Windows 10/11, 64-bit)

1. Run `MathChecker-Setup-x64-<version>.exe`.
2. Accept the default folder, or choose your own. The default installs for the
   current Windows user, so no administrator password is needed.
3. Leave "Launch Math Checker" ticked and finish.

You get a Start Menu entry ("Math Checker"), an optional desktop shortcut (the
installer asks), an entry in Windows **Installed apps**, and a normal uninstaller.

## First run

The window opens and starts its own local backend — there is no server to start
and no browser to open. The status bar shows it moving from *starting* to
*ready*; if it cannot start, it says why in plain language instead of failing
silently.

Then:

1. Open **Settings → AI keys** and paste your Gemini API key (create one at
   https://aistudio.google.com/apikey). The key is stored in your own Windows
   profile, not in the program folder and never in a log.
2. Optional: **Classroom** settings connect a Google Classroom account for
   assignment and student sync. Math Checker works without it (manual upload).
3. Start a correction: **Auto-correct** → choose an assignment → tick papers →
   **Auto-Correct**. Progress and stages appear in **Correction monitor**.

Internet access is required for correction (the AI provider is online). It is
not required to install or to open the app.

## Where your data lives

| What | Where |
|---|---|
| Database, mark schemes, papers, results, API keys | `%LOCALAPPDATA%\MathChecker` |
| Window state, desktop preferences, logs | `%APPDATA%\MathCheckerDesktop` |
| The program itself | `%LOCALAPPDATA%\Programs\MathChecker` (default) |

Nothing is written into the program folder, so **upgrading** (running a newer
installer) keeps every class, paper and mark, and **uninstalling** removes only
the application. Your data stays until you delete it yourself.

## What needs an account or a key

| Feature | Requirement |
|---|---|
| Correcting papers | Internet + a Gemini API key |
| Classroom assignment/student sync | A connected Google Classroom account |
| Reports, mark schemes, results, manual marking | Nothing extra |

## If the app does not start

* **"The Math Checker backend was not found"** — the installation is incomplete;
  reinstall with the same installer.
* **"A Math Checker backend is already running on this port"** — another window
  is already open (or a second copy). Close it, or enable *attach to a running
  backend* in Settings.
* **Port in use by another program** — Math Checker automatically moves to the
  next free port.
* Logs: `%APPDATA%\MathCheckerDesktop\logs`. They contain diagnostics, never your
  API key.

## Uninstall

Windows **Installed apps → Math Checker → Uninstall**, or the Start Menu
"Uninstall Math Checker" entry. Your data in `%LOCALAPPDATA%\MathChecker` is
deliberately kept; delete that folder yourself if you want it gone.
