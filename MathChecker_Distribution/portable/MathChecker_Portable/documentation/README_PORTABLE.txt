MATH CHECKER — PORTABLE QUICK START
==================================
AI-assisted mathematics marking for teachers.

This folder is a complete copy of the application, including the Python runtime
it needs. Nothing is installed on the computer: no Python, no Node.js, no
developer tools, no administrator rights. Delete the folder and nothing is left
behind except the data you chose to keep (see section 4).

Application version: see Data\config.toml or the bottom of the app sidebar.
Build: see release_manifest.json in the folder you extracted from.


1. EXTRACT
----------
Right-click MathChecker_Portable.zip -> "Extract All…" and choose a folder you
can write to: the Desktop, Documents, or a USB stick.

  * Extract the WHOLE archive. Do not pull single files out of the ZIP.
  * Paths with spaces or accented characters are fine
    ("C:\Users\You\Desktop\Math Checker (2026)" works).
  * On first run Windows may show "Windows protected your PC" (SmartScreen)
    because this application is not code-signed. Choose "More info" ->
    "Run anyway". Nothing in Windows security is disabled by doing this.
  * Do not extract into C:\Program Files: that folder is read-only for a
    normal user and the app keeps its data beside itself.


2. FIRST LAUNCH
---------------
Double-click  MathChecker.bat

  * A black console window opens and stays open. That window IS the
    application — it is the local server the user interface talks to. It also
    prints the address it serves (http://127.0.0.1:8642 by default).
  * Your browser opens the app automatically once it is ready. If it does not,
    open http://127.0.0.1:8642 yourself.
  * First run creates the database and the initial accounts inside Data\.
  * The copy listens on THIS computer only. Nobody else can reach it until you
    switch on Public Address yourself (section 3).
  * To explore without touching real data, double-click the launcher with demo
    mode:   MathChecker.bat --demo


3. CONFIGURATION (optional, and stored only on this computer)
-------------------------------------------------------------
All of it is done inside the app. No file editing, and no key of ours ships in
this folder.

  * AI provider keys      AI page -> "Models & Keys" tab
                          (or Settings -> "Gemini model chain")
                          Paste your own Gemini API key(s). Without a key the
                          app still runs: corrections cannot be marked.
  * Google Classroom      Settings -> "Connection" (or AI page -> Classroom)
                          Needs your own Google OAuth client file, placed at
                          Data\secrets\google_oauth_client.json. Without it,
                          Classroom stays disabled and the app says so instead
                          of failing.
  * Reports & letterhead  Settings -> "Teacher & school" / "Report branding"
  * Public Address        Settings -> "Public Address" (OFF by default)
                          Turning it ON publishes this copy at an https://…
                          address through Cloudflare Tunnel (the helper is
                          bundled in tools\). Anyone reaching that address
                          still has to sign in.
  * Access & accounts     Settings -> "Host account", "Multi-Account Mode",
                          "Remote Access"

Data\config.toml.example is a read-only reference: every setting the app keeps
in Data\config.toml, in plain text, with a comment explaining each one. Open it
in Notepad to see what can be changed (and what should not be). The app never
reads that file, so editing it changes nothing — use the app's Settings pages,
or edit Data\config.toml while the app is stopped. A configuration lives only
on the machine that created it: it is not shared and it is not part of the ZIP.

Data\distributed_bootstrap.txt holds the initial "developer" and "teacher"
passwords that were generated on this machine during the first launch. Keep it
private, and delete it once you have signed in and set your own password under
Settings -> "Host account".


4. WHERE YOUR DATA LIVES
------------------------
Everything is in the Data\ folder beside MathChecker.bat:

    Data\config.toml            settings (no secrets)
    Data\secrets.toml           your AI keys and account password hash
    Data\mathchecker.db         students, assignments, corrections, grades
    Data\logs\                  logs (safe to send to support)
    Data\reports\               generated PDF reports
    Data\retained\              originals kept by your retention setting
    Data\mark_schemes\          cached mark schemes

Copy the whole folder to move your setup to another machine; delete it and the
copy is unconfigured again. Nothing is written to %LOCALAPPDATA% or anywhere
else on the host machine as long as this folder contains portable.flag (it
does) — if you delete portable.flag and the Data folder, the launcher falls
back to the normal installed location, %LOCALAPPDATA%\MathChecker.

Backup:
  * Storage page -> "Download full database backup (.db)" for a restorable copy
  * Storage page -> "Export data" / "Import data" to move student and
    correction records between computers (never carries keys)
  * Storage page -> "Export configuration"


5. STOPPING THE APPLICATION
---------------------------
Normal use: press Ctrl+C in the console window, or close it. Math Checker then
shuts down cleanly — it stops its background correction workers and any tunnel
it started, and anything it was marking resumes the next time you start it.

If there is no console window to close (the copy was started by a boot task or
by another user), double-click:

    Stop.bat

It checks that the program on the port really is this copy of Math Checker
before stopping anything, then stops it together with its worker processes.
Your data, keys and reports are never touched by either method. If you started
the app on a port of your own (section 7), name it:  Stop.bat 8643


6. UPDATING
-----------
Extract the new version into a NEW folder, then copy your Data folder into it
(section 4). Keep the old folder until you are happy with the new one. No
updater runs by itself, and no data is ever deleted by an update.


7. IF SOMETHING GOES WRONG
--------------------------
"PORT CONFLICT: another program already uses port 8642"
    Another program — or another copy of Math Checker — holds the port.
    Start on a different one:      MathChecker.bat 8643
    …and open http://127.0.0.1:8643

"Math Correct is already running at http://127.0.0.1:8642"
    This copy was already started; nothing was started twice. A second window,
    worker pool or job queue is exactly what the launcher refuses to create.

"Math Checker could not find its Python runtime"
    The bundled runtime\ folder is missing or incomplete: the extraction was
    partial. Extract the ZIP again and run from the fresh folder.

The page is blank or will not connect
    Wait a few seconds and reload. The console window must still be open — it
    is the server. If it printed an error, that text is the reason.

"… is not a port number, so nothing was started"
    The launcher takes a port number or --demo and nothing else. Run
    MathChecker.bat with no arguments for the default port.

The AI page says no key is configured
    Expected on a fresh copy: add your own key in section 3. Nothing is marked
    without one.

Antivirus flags the launcher
    The package is not code-signed. The .bat files contain only the commands
    you can read in this folder; the tunnel helper tools\cloudflared.exe is the
    official Cloudflare binary. If your organisation blocks unsigned binaries,
    ask IT to allow the folder rather than disabling protection.

Diagnostics
    Data\logs\ holds the full log. The console window shows the same
    information live. Neither contains your API keys, your password, or your
    students' names and work.


8. WHAT IS *NOT* INCLUDED (by design)
-------------------------------------
  * No API keys, no Google account, no passwords, no student data of any kind.
  * No database from the machine this package was built on.
  * No source control history, build caches or developer tooling.
Everything above is created by you, for you, on this computer — or not at all.
