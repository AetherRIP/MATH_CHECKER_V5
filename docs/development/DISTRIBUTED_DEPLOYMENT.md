# Math Correct — Distributed Deployment Guide

**Shared backend on an always-on Windows mini-PC + tiny agents on every worker**

Date: 2026-10-08 · Applies to Math Correct V5 (app v5.2.1, distributed layer 1.0)

This guide turns the single-computer app into the §1 architecture:

```
DEVELOPER ──► SHARED BACKEND (this mini-PC, always on)
                 │
        ┌────────┼─────────────┐
     TEACHER  CO-TEACHERS   ASSISTANTS ── Tiny Agent (+ correction work)
```

Every step below is grounded in the actual code: the backend is the same FastAPI
app (`backend/app/main.py`), the agent is `agent/agent.py`, and the dashboard is
the **Multi-School** page (`/#/distributed`) of the built React app.

---

## 0. What you need

| Item | Notes |
|---|---|
| 1 mini-PC / small desktop, Windows 10/11 | Always on, wired Ethernet preferred. This is the SHARED BACKEND. |
| Python 3.12+ (64-bit) | Same version family you run today (`py -3 --version`). |
| The project folder | `backend/` and the built `frontend/dist/` (run `npm run build` in `frontend/` once). |
| One worker machine per Assistant | The Assistant's laptop, with the agent installed. |
| 5 minutes of router access | Only if Assistants are outside the school LAN (see §3). |

The mini-PC does **not** need a GPU or big RAM — it stores accounts, school/
class metadata, sync events, correction-result metadata and update state. It
never runs AI correction.

---

## 1. Install the backend on the mini-PC

1. Copy the project folder to the mini-PC, e.g. `C:\MathCorrect\`, so you have:

   ```
   C:\MathCorrect\
     backend\            ← the FastAPI app (source of truth)
     frontend\dist\      ← the built dashboard (served by the backend)
     agent\              ← tiny agent (also copied to workers, §5)
   ```

2. Install Python dependencies once:

   ```bat
   cd C:\MathCorrect\backend
   py -3 -m pip install -r requirements.txt
   ```

   (If `requirements.txt` is missing, install from your working teacher PC:
   `py -3 -m pip freeze > requirements.txt` there first and copy it over.)

3. **Test-boot it once by hand** and leave the window open:

   ```bat
   cd C:\MathCorrect\backend
   py -3 -m uvicorn app.main:app --host 0.0.0.0 --port 8642
   ```

   * `--host 0.0.0.0` is what makes the backend reachable from other machines
     (your daily runs use `127.0.0.1`, which is local-only).
   * On first boot with a fresh database the distributed layer writes one-time
     credentials to `data\distributed_bootstrap.txt` (§2).
   * Verify: open `http://<mini-pc-ip>:8642/api/health` from another PC —
     you should get a JSON response.

---

## 2. Bootstrap credentials (one-time, then secure them)

On first boot the backend creates the two top-level accounts automatically
(`backend/app/distributed/bootstrap.py`):

* **DEVELOPER** — full system control (accounts, schools, updates, diagnostics)
* **TEACHER** — global academic view (creates Co-Teachers/Assistants, assigns scope)

The random passwords land in:

```
C:\MathCorrect\data\distributed_bootstrap.txt
```

**Do this immediately, before anything else:**

```bat
:: show them once and store them in your password manager
type C:\MathCorrect\data\distributed_bootstrap.txt

:: then lock the file down to your admin user only
icacls C:\MathCorrect\data\distributed_bootstrap.txt /inheritance:r /grant:r "%USERNAME%":F

:: once safely stored, delete the file — the accounts already exist
del C:\MathCorrect\data\distributed_bootstrap.txt
```

> There is currently **no "change password" endpoint** for distributed accounts
> (the file is the only record of the bootstrap passwords). Keep a copy in a
> password manager before deleting the file.

**Sign in and set up the organisation:**

1. From any PC on the LAN open `http://<mini-pc-ip>:8642/#/distributed`
2. Sign in as **developer** → create schools/classes (School A → 10A, 10B, …).
   Class names may repeat across schools — they are keyed by stable IDs (§3/§5).
3. Sign in as **teacher** → create Co-Teacher/Assistant accounts (username +
   display name + password + role), then set each account's **Scope** to its
   schools/classes. An Assistant with an empty scope sees nothing until you
   assign them (§10).
4. Assign Classroom mappings when ready: `POST /api/distributed/mappings/course`
   (or via the API with the teacher token) — stable Classroom IDs only (§12).

Passwords are PBKDF2-HMAC-SHA256 (200k iterations) with per-account salts;
tokens are stored only as SHA-256 hashes. Nothing plaintext ever touches disk.

---

## 3. Network exposure & firewall

**Decide the exposure level first** — this is the most important security
choice:

### Option A — LAN only (simplest, recommended to start)
Assistants' PCs are on the same network as the mini-PC. Open the port only to
the local subnet (adjust the subnet to yours):

```bat
:: run in an ADMINISTRATOR Command Prompt on the mini-PC
netsh advfirewall firewall add rule ^
  name="Math Correct Backend (LAN only)" ^
  dir=in action=allow protocol=TCP localport=8642 ^
  remoteip=192.168.1.0/24 profile=private,domain
```

Nothing is exposed to the internet. If you use a "Public" network profile on
that PC, either switch it to Private or add `profile=any` knowingly.

### Option B — Assistants over the internet
Do **NOT** raw port-forward 8642 to the internet — traffic is plain HTTP and
the correction APIs were built for a trusted LAN. Instead put a free VPN mesh
in front (no budget needed):

* Install **Tailscale** (free tier) on the mini-PC and every worker PC.
* The backend then reaches every worker over the tailnet; firewall stays
  closed to the public internet. Use the tailnet IP (100.x.y.z) as the
  `backend_url` in §5.

The backend's own public-mode gate still applies to everything **except**
`/api/distributed/*` (which enforces its own per-account bearer tokens, and
was verified to keep rejecting unauthenticated calls). The teacher dashboard
sign-in screen keeps its rate-limited password.

### Either way — always-on behaviour
If the mini-PC's IP is DHCP-assigned, reserve it in the router (DHCP
reservation for the mini-PC's MAC) so worker configs never break.

---

## 3.1 The two access switches in Settings (multi-account, public address)

Both switches live on **Settings**, both are **owner-only** (a request straight
from the installation's own machine, or a signed-in **Developer**), and both are
enforced by the backend — the interface merely reflects them.

### 3.1.1 Multi-Account Mode (default OFF)

**What it is.** One switch that turns the multi-user layer *off* without
deleting anything: `[accounts] multi_account` in the installation's
`config.toml` (environment override `MULTI_ACCOUNT_ENABLED=1|0`, which wins over
the file). A fresh install is a **single-account** install.

**While it is OFF:**

* account administration, role assignment, team management and team-based
  device discovery are hidden **and refused** — the backend answers
  `403 multi_account_disabled` even when a valid Developer token is used, so
  hiding a button is never mistaken for protection;
* accounts, roles, permissions, teams, scopes, device↔account links and audit
  records are untouched (no migration runs, nothing is truncated);
* everything a single-account install needs keeps working: sign-in, schools and
  classes, corrections, grade storage (1–9 scale), reports, Retry/Approve/Sync,
  Google Classroom, device registration, heartbeats/presence, results and report
delivery, updates, errors, audit and the shared-backend connection.

**Turning it back ON** restores the full system as it was — no rewrite, no
migration, no data loss — and requires the same owner/Developer authorisation.
No restart is needed in either direction (the middleware and the UI read the
flag per request). Verify from the laptop:

```bat
:: 403 multi_account_disabled while off, the real answer while on
curl -H "Authorization: Bearer <developer-token>" http://127.0.0.1:8642/api/distributed/accounts
curl http://127.0.0.1:8642/api/distributed/capabilities
```

### 3.1.2 Public Address (default OFF)

The **Public Address** card controls the app-managed public endpoint. The
mechanism is the one this project already ships — a Cloudflare **quick tunnel**
(`cloudflared tunnel --no-autoupdate --url http://127.0.0.1:<this port>`), the
same command `start_public.bat` / `remote.py` use. The switch starts, verifies
and stops that tunnel from inside the running application; it is not a second,
parallel mechanism and it does not open a firewall port or bind the app to every
interface.

**Prerequisite — `cloudflared`.** The app finds it in this order: `MC5_CLOUDFLARED`
(explicit path) → `tools\cloudflared.exe` inside the application folder →
`PATH` → the standard install locations. Install it once on that machine:

```bat
winget install --id Cloudflare.cloudflared
```

Without it the switch stays OFF and says so ("cloudflared was not found…"), and
**a sign-in account must exist** first — the app refuses to publish an endpoint
nobody could sign in to.

**What you see, and what it means:**

| Shown | Meaning |
|---|---|
| `Disabled` | switched off; nothing is published (default) |
| `Starting` | the tunnel exists but the address is not proven yet — no address is displayed |
| `Connected` | the address answered **all** checks THROUGH the public hostname: `/api/health`, the UI, `/api/auth/status` in public mode, and an unauthenticated API call being refused (401). Only then is the URL shown |
| `Failed` | the tunnel did not come up or did not verify; the reason is displayed and **no address is shown as active** |
| "started outside the application" | `start_public.bat` / `remote.py` owns this session (`MC5_PUBLIC=1`); the app does not control that tunnel and says so instead of pretending |

**While it is ON:** HTTPS only; the sign-in gate and all permissions stay
enforced; `/docs`, `/redoc` and `/openapi.json` are hidden; no key, token or
password ever appears in the address, the panel or the (local, truncated) tunnel
log.

**While it is OFF:** the tunnel process tree is stopped, the status file goes
offline so nothing in the app advertises the address (including the sign-in
screen), and the address is kept only as an *inactive* "last verified" note.
Local access, LAN access and the separately configured **shared backend URL**
(Multi-School page) are untouched — the public address is not the backend URL.

**Restarts and upgrades.** The preference is saved in `config.toml` and
survives restarts and upgrades (`%LOCALAPPDATA%\MathChecker` on an installed
copy, `Data\` next to a portable copy); when it is ON the endpoint is brought
back automatically at the next launch. Each installation obeys its own setting,
and no address is ever baked into the installer.

**If you use your own proxy/tunnel** (Tailscale Funnel, nginx, a named
Cloudflare tunnel, a VPS) the application cannot start or stop it — nothing in
the app claims otherwise, and switching the Public Address off will not close
something it does not own.

**Live evidence.** `python backend/scripts/live_verify_access_switches.py`
starts the real application on an isolated data directory and walks the whole
story with a real tunnel: it writes its verdicts to
`LIVE_ACCESS_SWITCHES_RESULTS.json` (no mocks anywhere in that script).

---

## 4. Run the backend automatically at boot (always-on, §2)

The backend must survive reboots and crashes without anyone logging in. Use
**Task Scheduler** (no extra software):

```bat
:: run in an ADMINISTRATOR Command Prompt, adjust paths/user
schtasks /Create /TN "MathCorrect Backend" /RU SYSTEM /SC ONSTART /DELAY 0001:00 /F ^
  /TR "cmd /c cd /d C:\MathCorrect\backend && py -3 -m uvicorn app.main:app --host 0.0.0.0 --port 8642 >> C:\MathCorrect\backend.log 2>&1"
```

* Runs as SYSTEM at every boot, 1 minute delay (lets networking come up).
* Logs append to `C:\MathCorrect\backend.log` — check there when in doubt.
* To restart after a crash without rebooting:

  ```bat
  schtasks /Run /TN "MathCorrect Backend"
  ```

* Add **Windows Update guard**: mini-PCs that auto-reboot at 3 am are fine —
  the task brings the backend back; workers reconnect automatically (§46).

**Verify:** reboot the mini-PC, wait ~2 minutes, then from a worker PC:

```bat
curl http://<mini-pc-ip>:8642/api/health
```

---

## 5. Install the tiny agent on every worker machine (§14–§16)

The agent is a few KB of Python that identifies the machine, heartbeats every
5 minutes, queues events offline and reports errors/updates. It is **not** the
correction engine.

On each Assistant's PC:

1. Copy the `agent\` folder (and `backend\` if you want it to import the
   shared implementation) to e.g. `C:\MathCorrectAgent\`.

2. Create `C:\MathCorrectAgent\agent\agent.json` pointing at the backend:

   ```json
   {
     "backend_url": "http://<mini-pc-ip>:8642",
     "worker_id": "ASSISTANT-A-PC",
     "interval_seconds": 300
   }
   ```

   (`MC_BACKEND_URL` / `MC_WORKER_ID` env vars also work; `agent.json` wins
   for team deployments because it survives reboots and user changes.)

3. **Test once by hand:**

   ```bat
   cd C:\MathCorrectAgent
   py -3 agent\agent.py --once
   ```

   Expected: `{"device_id": "...", "heartbeat": true, "flushed": 0}`.
   `heartbeat: false` means the backend was unreachable — check §3/§4.
   Unplug the network and run it again: it must still exit cleanly with
   `heartbeat: false` and keep its queue intact (§20).

4. **Auto-start silently with Windows** — the generator writes a silent
   launcher (a `.vbs` that runs the agent with **no console window**) plus
   instructions; it deliberately does not touch the registry itself:

   ```bat
   cd C:\MathCorrectAgent\backend
   py -3 scripts\setup_agent_autostart.py --target C:\MathCorrectAgent\agent
   ```

   Then follow the generated `AUTOSTART_INSTRUCTIONS.txt`:
   press **Win+R** → `shell:startup` → Enter → copy
   `mathcorrect_agent_startup.vbs` there. (The robust alternative it
   describes: a Scheduled Task with an "At log on" trigger running
   `wscript.exe C:\MathCorrectAgent\agent\mathcorrect_agent_startup.vbs
   "C:\MathCorrectAgent\agent\agent.py"`.)

5. **Verify presence:** open the Multi-School dashboard → **Workers** — the
   device appears with `ONLINE` presence within ~5 minutes. Presence is
   **only ever real heartbeat data** (§62): stop the agent and it flips to
   `OFFLINE`; nothing fakes it.

Repeat on every worker machine. Each machine gets a persistent random
`device_id` (stored in `agent\device_id`) — never an IP/hostname (§21).

---

## 6. Daily operating model

| Who | Does what | Where |
|---|---|---|
| **Teacher** | Signs into `http://<mini-pc>:8642` (teacher dashboard for academics + Multi-School for assignments/scope). Never needs to stay online for corrections to happen. | Any PC |
| **Assistants** | Their worker runs corrections; the tiny agent keeps the device visible; the Multi-School page shows their own scoped schools/classes, results and update status. | Their laptops |
| **Developer** | Multi-School → Accounts/Updates/Diagnostics. Creates update requests; every install is backup-verified with automatic rollback (§31–§41). | Any PC |

The **Teacher's PC can be off** — that is the point of §2. All state lives on
the mini-PC.

---

## 7. Backups of the backend (small but critical)

The mini-PC's `C:\MathCorrect\data\` folder holds the SQLite database. It is
small (metadata only — reports/PDFs live in Drive/Classroom, §19/§57). Schedule
a nightly copy:

```bat
schtasks /Create /TN "MathCorrect DB Backup" /RU SYSTEM /SC DAILY /ST 03:30 /F ^
  /TR "cmd /c robocopy C:\MathCorrect\data D:\Backups\MathCorrect\data /MIR"
```

Restore = stop the backend task, copy the folder back, start the task.

---

## 8. Updates to worker machines (§27–§45)

1. Developer creates an update request in Multi-School → Updates
   (target version, component, priority, required/optional/auto-install).
2. Each worker's updater downloads **only when the version differs**, verifies
   the SHA-256 checksum and available disk space, waits for an idle worker
   (never interrupts DOWNLOADING/CORRECTING/UPLOADING, §35), takes a verified
   backup, installs, health-checks — and **rolls back automatically** to the
   preserved previous version if the health check fails (§38). Rollback keeps
   all correction records, pending sync events and accounts (§39).
3. Failures land in the append-only update history + audit trail with the
   failure stage and rollback result (§40); a failed rollback is reported as
   `RECOVERY_REQUIRED` and the tiny agent stays alive to receive a recovery
   request (§41).

> Note: the update *package builder* (creating signed installers) is a
> separate, later deliverable. The receiving/verification/install/rollback
> infrastructure documented above is live and tested, including the §61
> critical rollback test.

---

## 9. Post-deployment checklist

- [ ] `http://<mini-pc>:8642/api/health` answers from a worker PC
- [ ] `data\distributed_bootstrap.txt` secured/deleted after storing passwords
- [ ] Developer + Teacher logins work on `/#/distributed`
- [ ] Schools/classes created; Assistant accounts created **with scope**
- [ ] Firewall rule active (subnet-scoped) or tailnet up
- [ ] `schtasks /Query /TN "MathCorrect Backend"` = Ready; survives reboot
- [ ] Every worker: `agent.py --once` succeeds → appears in **Workers** as ONLINE
- [ ] Unplug a worker's network → agent queues events → replays on reconnect,
      no duplicate corrections (§20, exactly-once client_event_id dedupe)
- [ ] Nightly DB backup task ran once (check `D:\Backups\...`)
- [ ] Settings: **Multi-Account Mode** shows the state you want (`OFF` for a
      single-account install; nothing is deleted either way)
- [ ] Settings: **Public Address** shows the state you want, and the address it
      displays (if any) opens the app in a browser

## 10. Known limitations (honest list, §66)

1. **No password-change endpoint for distributed accounts yet** — keep the
   bootstrap passwords safe; recreating an account is the current remedy.
2. **Distributed login has no rate limiting yet** (the app sign-in does).
   Behind a VPN or LAN-only firewall this is acceptable; do not raw-expose
   8642 to the internet.
3. **/heartbeat is presence-only and unauthenticated by design** — it can
   never claim an account, but a malicious LAN peer could send fake presence.
   The VPN option (§3B) removes this surface.
4. **Plain HTTP on the LAN** — use the tailnet/VPN path for anything beyond a
   trusted school network.
5. Multi-machine failover of the *backend itself* is out of scope; the nightly
   backup (§7) plus reinstall covers mini-PC death.
6. **The Public Address switch needs `cloudflared` on that machine** (§3.1.2).
   It is not shipped in the installer; without it the switch stays off and
   explains why. A tunnel started *outside* the application
   (`start_public.bat` / `remote.py`, or your own proxy) is not controlled by
   the app, and the panel says so rather than implying it was stopped.
7. **A public quick-tunnel address is not a secret and not a substitute for
   authentication.** It is protected by the sign-in gate, not by being
   hard to guess; anyone with the URL still has to sign in.
