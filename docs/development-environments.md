# Development environments: desktop, browser, isolated test

Math Checker V5 has three places to run the same UI, and they read different
data on purpose. This page is the contract between them. The short version:

> **A window must always be able to say which backend it is showing, and the
> browser must never guess.**

Port numbers do not define an environment. A demo backend answering on 8642 is
still demo data. Only the process that answers knows, and it says so:

| Field | Where | Meaning |
| --- | --- | --- |
| `demo_mode` | `/api/dashboard` | synthetic demo data (own folder, Classroom off, mock AI) |
| `mock_mode` | `/api/dashboard` | AI engine in mock mode (no key needed) |
| `environment.demo` / `environment.mock_mode` | `/api/privacy/info` | same two facts for the desktop shell |
| `data_directory` | `/api/privacy/info` | redacted folder the backend is actually using |

## The three environments

| | 1. Production desktop | 2. Browser development | 3. Isolated test |
| --- | --- | --- | --- |
| Started by | `desktop\...\V5.exe` (or `build_release.ps1`) | `npm run dev` in `frontend/` | a backend with `MC5_DATA_DIR` pointed at a throwaway folder |
| Backend | its own local backend on its configured port, or an attached one | **the origin in `MC5_DEV_API`** | its own backend on its own port |
| Data | the teacher's real data folder | whatever that backend uses | a separate folder under `dev_local/` |
| Classroom / Gemini | configured in the app | inherited from the backend | usually absent (deliberately) |
| Labelling | `Backend ready` / `Backend — check data source` + warning banner | `DEMO` / `DEV` / `LIVE` / `No backend` chip + `DEMO` banner | same as browser, showing `DEMO` if demo mode is on |

### A. Production desktop

The desktop starts (or attaches to) the Python backend and shows its data
folder. It is not redirected anywhere: `BackendLocator`/`BackendHost` read the
configured folder and port from
`%APPDATA%\MathCheckerDesktop\settings.json`.

After attaching, the shell reads `/api/privacy/info` and compares what the
backend says with what it asked for. If they disagree, a warning banner is
shown (and the status dot reads **Backend — check data source**):

* attached to a **DEMO** backend while the desktop was not started in demo mode;
* configured for demo mode, but the backend is serving **real** data;
* a different data folder than `dataDirectory`.

The warning is informational: nothing is written, and stopping/starting stays
in the teacher's hands.

### B. Browser development

`vite` proxies `/api` to **one explicitly configured backend**:

```bat
cd frontend
copy .env.development.example .env.local
:: edit MC5_DEV_API, for example http://127.0.0.1:8642
npm run dev
```

* `MC5_DEV_API` is the only place the browser's backend is named
  (`frontend/vite.config.ts` → `frontend/scripts/dev-backend.mjs`).
* Missing, blank, malformed, or path-bearing values **stop `npm run dev`** with
  an explanation. There is no fallback to 8642, and no silent switch to demo.
* A non-loopback target is accepted but logged as a warning: class data would
  cross the network.
* `vite build` needs no configuration: the bundle the backend serves always
  talks to its own origin.

Practical cases (all read the same local database, so they never diverge):

```bat
:: real data, normal backend
python main.py 8642            &  set MC5_DEV_API=http://127.0.0.1:8642

:: synthetic demo data — the UI shows a DEMO chip and banner
python main.py --demo 8642     &  set MC5_DEV_API=http://127.0.0.1:8642

:: a second, isolated install on another port
python main.py 8643            &  set MC5_DEV_API=http://127.0.0.1:8643
```

The UI's chip comes from the backend (`demo_mode`), not from the port or a build
flag, so `http://127.0.0.1:8642` shows **DEMO** when the demo process owns it and
**DEV** when the real backend does. If the backend cannot be reached the shell
shows **No backend** and an error banner — never a page of plausible zeroes.

The desktop's backend is loopback-only by default. Do not expose it with
`--host 0.0.0.0`, a tunnel, or LAN mode just to open the browser UI: run the
browser against a backend on the same machine, or use the existing, deliberate
LAN/public configuration (`docs/SERVER_LIFECYCLE.md`, `remote.py`) and its
session protections.

### C. Isolated test environment

A snapshot is a backend plus a data folder, nothing else:

```bat
set MC5_DATA_DIR=E:\MATH_CHECKER_V5\dev_local\verify2\synth_data
.venv\Scripts\python.exe main.py 8660
```

Rules for snapshots:

* keep the folder outside the real data folder (and outside `%APPDATA%`);
* never copy `secrets.toml`, OAuth tokens, or API keys into it — copy the
  database and `config.toml` only, or let it start fresh;
* records created there stay there: stop the process and delete the folder when
  done (never the real one);
* Google Classroom and Gemini are expected to be **absent**; the UI must say
  "not configured"/unavailable rather than pretending.

The desktop can be pointed at a snapshot for testing (`dataDirectory` in
`settings.json`, or File → Backend settings). It then reports that folder as its
own, and writes only there.

## Classroom and Gemini

Classroom OAuth and Gemini keys belong to the backend's data folder
(`secrets.toml`), never to the browser, `frontend/.env*`, or source.

* **Not configured** means the credential is missing: dashboard reports
  `classroom.configured = false` / `api_health.has_keys = false`, and the UI
  shows "Not configured" instead of an error. That is the normal state for a
  demo or test snapshot and is *not* a failure.
* **Configured but failing** is a service error: the UI shows the error, not
  "not configured".
* An authorized operator configures both in the production app (Settings →
  Classroom, Settings → AI keys). Demo mode refuses Classroom connections by
  design, so demo can never inherit a real token.

## Screenshot helpers

`dev_local\shot.ps1` and `dev_local\navshot.ps1` share
`dev_local\lib\desktop-window.ps1`, which finds the real process
(`MATH_CHECKER_V5`, plus the legacy `MathChecker.Desktop`), prefers the expected
window title, picks the newest instance when several are open (and says so),
and fails with instructions when no window exists. Use them only on synthetic
or redacted data.

```bat
dev_local\shot.ps1 -Name desk
dev_local\navshot.ps1 -Label Students -Name students
dev_local\shot.ps1 -ProcessId 12345 -Name pinned
```
