# Math Checker — Teacher Guide

AI-assisted mathematics marking for teachers. This copy contains **only the
application**: your own classes, students, API keys and Google account are
created fresh, for you, the first time you run it.

## Install (Windows)

1. Run **`MathChecker_Setup.exe`**.
2. Follow the setup wizard (it keeps the Math Checker look — no stock Windows
   chrome).
3. Launch **Math Checker** from the Start Menu or desktop.

You do **not** need Python, Node, PowerShell, Git or any developer tools when
the package ships a bundled runtime. If the installer warns that no runtime is
bundled, install Python 3.12+ from python.org and tick *Add Python to PATH*.

## Portable version

Extract `MathChecker_Portable.zip` anywhere (a USB stick works) — the whole
archive, and a folder you can write to. Run **`MathChecker.bat`**; the app opens
in your browser once it is serving. Data is kept in the `Data/` folder next to
the app, so nothing is written to the host machine at all. Everything your
browser needs (including the Python runtime) is inside the folder: no Python,
Node.js, administrator rights or developer tools are required.

`README.txt` beside the launcher is the quick start (extract, first launch,
configuration, where data lives, stopping, troubleshooting).

To stop it again, press Ctrl+C in the console window — or, if there is no
window to close, run **`Stop.bat`**, which stops this copy and its background
workers without touching your data.

`start.sh` remains the launcher for macOS/Linux, where it keeps data in a
`data/` folder beside the app.

## First run

**This copy ships with no password — and none is asked for.** Opening it for the
first time shows the setup wizard below, and it opens straight into the app on
this computer. There is no default password to look up or guess.

The first-run setup asks for:

1. **AI provider & keys** — paste your Gemini API key(s). Keys are stored only
   on this computer, are never displayed again in full, and can be tested with
   a tiny probe (no student data is ever used for a health check).
2. **Google Classroom** — connect *your* Google account. The app stores a
   refresh token for you locally. It never ships or inherits anyone else's.
3. **Storage & retention** — how long original submissions are kept.

### Your own sign-in

Your copy starts with no sign-in account, so nothing is demanded of you up
front. Create your own on **Settings → Access & accounts → Host account**: a
username (leave it blank to keep `teacher`) and a password of at least 10
characters. From then on the app asks for it whenever it starts, and the
account is stored only on this computer as a salted hash — nobody but you knows
it.

That card is also the one to use **before switching Public Address on**: a
public address must serve a login page, and the switch refuses to expose a copy
that nobody could sign in to.

Explore without any setup first: launch **Math Checker (Demo mode)** from the
Start Menu to see the app with synthetic students and assignments - it opens
straight into the app and does not ask for a password. Demo Mode cannot connect
to a real Google Classroom account.

## Settings that decide who can reach the app

**Settings** has two switches. Both change what the installation *is*, so they
are only offered to this computer's own owner (or a signed-in Developer), and
the server enforces that — not just the screen.

### Multi-Account Mode (off by default)

Your copy runs as a **single-account** install, which is why it asks you to
create just one sign-in and nothing else. Switching this ON enables the full
accounts/roles/teams system (shared Identity across several installations) and
creates the administration account that system is managed from; its one-time
password is written to `distributed_bootstrap.txt` in your data folder (see
*Where your data lives*), so keep that file private or change the password
after you first sign in with it. Switching the switch OFF again hides the
system — and the server refuses those operations — **without deleting
anything**: accounts, roles, permissions, teams and audit records are kept
exactly as they are, ready for the next time you switch it on.
Your correction work, Google Classroom connection, reports, retries, approvals,
sync and the 1–9 grade scale are unaffected either way. No restart is needed.

### Public Address (off by default)

This is the optional endpoint other devices reach your copy through. It uses
**Cloudflare Tunnel**, which must be installed once on this computer
(`winget install --id Cloudflare.cloudflared`); if it is missing the switch stays
off and tells you exactly that. While it is ON you get an `https://…` address
that is shown **only after the app has proved it works**, the sign-in screen and
all permissions apply exactly as they do locally, and no key, token or password
is ever part of the address. While it is OFF the tunnel is stopped and the
address stops being advertised everywhere (it is remembered only as an inactive
"last verified" note), while access from this computer and any separately
configured backend keep working.

The setting survives restarts and upgrades. `start_public.bat` remains available
if you prefer to start the whole app with public access in one step — in that
session the app tells you it is not the thing controlling the tunnel.

## Where your data lives

| What | Where |
|---|---|
| Application files (installed copy) | `C:\Program Files\Math Checker\` |
| Your data (installed copy) | `%LOCALAPPDATA%\MathChecker\` |
| Application files (portable copy) | the folder you extracted |
| Your data (portable copy) | `Data\` inside that folder |

Application files and your data are **separate**, so updating or reinstalling
the app never removes your work. Use **Settings → Privacy & Data → Factory
Reset** to remove your data deliberately; the application stays installed.

## Diagnostics

If something misbehaves, use **Privacy & Data → Export Diagnostics**. The file
contains build/environment facts and aggregate counts — **no API keys, no
OAuth tokens, no student names, submissions, emails or reports**.

## Version

Every build carries a version, build number and build date, shown in the app
sidebar and on the Privacy & Data page.
