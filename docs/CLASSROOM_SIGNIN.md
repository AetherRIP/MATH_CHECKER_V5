# Google Classroom sign-in — the exact steps

This document records **how the interactive sign-in works** in this checkout, so
that a live read-only verification can be repeated by anyone with the teacher's
authorisation. It contains no credentials and none are needed to read it: the
app never echoes a client secret or a refresh token, and neither does this file.

Read it top to bottom before running anything that touches real Classroom data.

## 1. What the app needs before a sign-in is possible

An OAuth **client** (not a token) must be present on disk. `config.resolve_oauth_client_path()`
is the single source of truth and resolves, first existing file wins:

1. `data/secrets/google_oauth_client.json` — the canonical V5 location
   (installed builds use `%LOCALAPPDATA%\MathChecker\secrets\`);
2. a teacher-placed `client_secret_*.json` (or `google_oauth_client*.json`)
   found in the project — auto-detected, never copied, never displayed, and
   git-ignored.

The client must be an OAuth client of type **Desktop app**. The flow below
starts a loopback server on a random port (`InstalledAppFlow.run_local_server`),
which is exactly what a Desktop-app client is allowed to do; a "Web
application" client with a fixed redirect URI will be rejected by Google.

The Google account being connected must be the **teacher account that owns the
classes**, and its Google Cloud project must have the Classroom API enabled.
If the OAuth consent screen is still in *Testing*, that same account has to be
listed under **Test users**, or Google refuses the consent with
`access_denied`.

## 2. The interactive sign-in itself

1. Start the app (`start.bat`, or `python main.py` in development) and open the
   app in a browser.
2. Go to **Classroom** (the Setup wizard and the Report page offer the same
   button, so any of the three works) and press **Connect Google Classroom**.
3. The button calls `POST /api/classroom/connect`, which runs
   `start_device_or_loopback_flow()` on a worker thread. The UI switches to
   *"Waiting for Google — finish the sign-in in your browser…"*.
4. Your **default browser opens by itself** on Google's consent screen. If the
   machine has no browser (a headless or remote box), the flow cannot open one:
   forward the loopback port and open the printed URL from a browser on the
   machine that can reach it, within the five-minute window.
5. Sign in with the teacher account and **approve every requested permission**.
   Partial approval is the single most common cause of a half-connected app —
   see §4.
6. Google redirects back to `http://127.0.0.1:<port>/`, the app exchanges the
   code, and the Classroom page shows the connected account's email address.

The whole exchange must finish within **300 seconds**; after that the loopback
server stops listening and `POST /api/classroom/connect` fails. Simply press the
button again.

## 3. What is stored, and where

| What | Where | Notes |
| --- | --- | --- |
| Refresh token | `data/secrets/secrets.toml` → `[classroom] refresh_token` | Never echoed by any API response |
| Connected account email | local `app_meta` | Display only; never treated as a secret |
| Connected-at timestamp | local `app_meta` | Rendered in the Classroom header |

The request uses `access_type="offline"` **and** `prompt="consent"`. Both matter:
the first is what makes Google issue a *refresh* token at all, the second forces
Google to re-issue one even for an account that would otherwise skip the consent
screen. Without them the sign-in can look successful while leaving the app with
no refresh token, which only shows up as a broken connection on the next call.

Scopes requested (narrowest set that supports the feature):

- `classroom.courses.readonly` — list the teacher's courses
- `classroom.coursework.students` — read coursework and (only when explicitly
  approved) write grades
- `classroom.rosters.readonly` — read the student roster
- `drive.readonly` — read the submitted files
- `drive.file` — create the correction report in the app's *own* Drive area;
  this scope cannot see or touch any other Drive file
- `openid`, `userinfo.email` — show which account is connected

## 4. Verifying the connection without exposing anything

```powershell
cd backend
python scripts/auth_probe.py
```

It prints booleans only — client JSON present, client id present, refresh token
stored, and whether a live call succeeds. The same state is available to the UI
at `GET /api/classroom/status`, whose public payload is
`{connected, email, connected_at, problem, client_configured}`.

A refresh token bound to an **older scope set** is a special case: Google
answers `invalid_scope` rather than `invalid_grant`. The app reports that as
"reconnect needed" instead of "expired", because the fix is a fresh sign-in, not
a new client.

To disconnect, press **Disconnect** (`POST /api/classroom/disconnect`), which
clears the stored refresh token. This does not touch a single Classroom record.

## 5. Signing in is not the same as being allowed to write

Signing in grants the *scope* for grade uploads and report attachments. The
application guard is a separate switch and is independent of the connection:

- **Testing-only mode ON** (`[testing] only = true`) makes every publishing
  helper raise `PublicationBlocked` — draft grade, final grade, submission
  return, Drive upload/replace/share/delete, and report attachment. Verified
  live: eight helpers, eight refusals, **zero** writes reaching the HTTP
  transport, with the negative control confirming the gate is a switch and not
  a wall (see `docs/results/LIVE_CLASSROOM_CERTIFICATION.json`).
- **Demo mode** (`--demo`) runs in its own data directory with Classroom
  hard-disabled, so a demo can never inherit the real token.

Any read-only verification must therefore be run with testing-only forced on.
`backend/scripts/verify_classroom_readonly.py` does this by building a throwaway
data directory from the real config (so the real installation is never opened
for writing), copying the refresh token into it, driving the app's own guard
before fetching a single student byte, and re-hashing every submission it
touched at the end. It refuses to run without `--live --ack READ-ONLY`:

```powershell
python backend/scripts/verify_classroom_readonly.py --live --ack READ-ONLY
```

Its exit codes are deliberately loud: `2` refused (no acknowledgement), `3` a
safety check failed (a write escaped, or a real submission changed), `4` it ran
cleanly but found a defect on the read surface.
