# Multi-Account Mode switch + Public Address switch — delivery report

**Scope delivered.** Multi-account functionality is temporarily switchable OFF
(default **OFF**: a fresh install runs as a single-account install) without
deleting a single table, row, module or route, and the Settings page gained a
**Public Address** ON/OFF control that starts and stops the application's real,
already-shipped public endpoint (a Cloudflare quick tunnel) — verified end to end
against a live tunnel, never simulated.

Nothing about the marking engine, Google Classroom, automation, reports, retry /
approve / sync, the 1–9 grade scale or the distributed/device connectivity was
dismantled or weakened.

---

## 1. The exact configuration flags

| Setting | Where | Default | Override that wins |
|---|---|---|---|
| Multi-account mode | `[accounts] multi_account` in the installation's `config.toml` | `false` | `MULTI_ACCOUNT_ENABLED=1` / `=0` |
| Public Address (switch position) | `[security] enabled` in `config.toml` | `false` | `MC5_PUBLIC=1` (launcher-owned session) |
| Last verified address (not a switch) | `[security] last_public_url` | `""` | `MC5_PUBLIC_URL` (launcher session) |

* **Restart required to change either switch? No.** Both are read per request
  (`app.multi_account.enabled()`, `app.public_access.requested()`), so flipping
  them takes effect immediately. The value is persisted through the normal
  `save_config` path, so it survives restarts and upgrades.
* **Migration required? None, in either direction.** The distributed schema is
  additive and permanent; switching off runs no DDL and touches no rows.
* Config file location: `<data dir>\config.toml` — the same place every other
  setting lives. On an installed copy that is `%LOCALAPPDATA%\MathChecker\`
  (declared `uninsneveruninstall` by the installer, so upgrades and even an
  uninstall do not remove it); a portable copy keeps `Data\` next to the app.

## 2. Multi-account mode — what OFF actually does

* **Hidden (UI)** and **refused (backend)**: account administration, role
  assignment, team management and team-based account discovery. Refusals are
  real HTTP refusals — `403 multi_account_disabled` — enforced by an ASGI
  middleware that sits in front of the routes, so a direct API call with a valid
  Developer bearer token is refused too. Hiding a panel is only a courtesy.
* **Preserved**: every table, record, module, route and setting of the
  distributed layer. Account/role/team/scope/device-link/audit data is never
  deleted, migrated or anonymised.
* **Still working while OFF** (explicitly protected, not merely "untouched"):
  sign-in (app + distributed login/logout/me/change-password), schools, classes
  and course/class mappings, corrections, grade storage (1–9), PDF reports,
  Retry/Approve/Sync, Google Classroom, device registration,
  heartbeats/presence, the sync stream, results, report delivery, updates,
  errors, audit and the shared-backend connection.
* **Re-enabling** requires the same authorisation as disabling (this computer's
  own owner, or a signed-in Developer). Nothing else has to be recreated, and
  the interface and API come back exactly as they were.

## 3. How the Public Address toggle controls the real endpoint

The mechanism is the one this project already ships — `cloudflared tunnel
--no-autoupdate --url http://127.0.0.1:<the running port>` — the same command
`start_public.bat` / `remote.py` use. `app/public_access.py` starts, verifies,
monitors and stops that tunnel from inside the running application.

| Action | What really happens |
|---|---|
| Switch **ON** | The saved preference is written, the tunnel process is spawned against this server's actual port, and the address is only published after **four end-to-end checks through the public hostname**: `/api/health` = ok, the UI is served, `/api/auth/status` reports `public_mode=true`, and an unauthenticated `/api/dashboard` is **refused** (401/403/503). A failed verification rolls the switch back to OFF with the reason. |
| Switch **OFF** | The tunnel **process tree is killed**, the shared status file is rewritten `offline` with `public_url: null`, so nothing in the app advertises the address — including the sign-in screen and the Remote Access panel. The address is kept only as an inactive "last verified" note. |
| Failure | `FAILED` + an actionable reason + **no address shown as active**; the monitor keeps testing and re-establishes or stays failed; a stale online record expires by itself. |
| Someone else's tunnel | If `start_public.bat` / `remote.py` started the session (`MC5_PUBLIC=1`) or another fresh tunnel record exists, the app **adopts nothing and stops nothing** — it says so and refuses to claim it disabled an endpoint it does not control. |

States shown: `Disabled` / `Starting` / `Connected` / `Failed`, always separate
from the switch position (`requested`). The switch is disabled while a change is
in flight, so repeated clicks cannot start two tunnels (the manager is
idempotent under a lock as well).

**Public Address ≠ Backend URL.** Turning it off does not touch local access or
the separately configured shared-backend address (Multi-School page).

## 4. Security posture

* HTTPS only (Cloudflare terminates TLS); the session cookie is marked `Secure`
  when public; sign-in and every permission/scope check stay enforced through
  the tunnel — tested: unauthenticated callers get `401 auth_required`.
* `/docs`, `/redoc`, `/openapi.json` are hidden while public (verified 404).
* No tunnel credentials exist to leak (a quick tunnel has none), nothing is
  published from the truncated local tunnel log, and the address carries no
  keys, tokens or passwords.
* The endpoint is bound to `127.0.0.1` + an outbound tunnel — no firewall port
  is opened and the app is never bound to every interface for this feature.
* Owner-only enforcement is server-side (`is_genuinely_local` or a Developer
  session), so a LAN peer or a public caller cannot flip either switch.

## 5. Files changed (and why)

**Backend**

| File | Purpose |
|---|---|
| [app/multi_account.py](../../backend/app/multi_account.py) *(new)* | The flag (`[accounts] multi_account` + `MULTI_ACCOUNT_ENABLED`), the explicit rule table of disabled operations, the `403 multi_account_disabled` middleware, and the public `capabilities()` payload. |
| [app/public_access.py](../../backend/app/public_access.py) *(new)* | The app-managed tunnel: start/verify/publish/monitor/stop, the five states, honest failure reporting, restart autostart, shutdown. Never publishes an unverified address. |
| [app/api/access_routes.py](../../backend/app/api/access_routes.py) *(new)* | `GET /api/access/state`, `POST /api/access/public-address`, `POST /api/access/public-address/recheck`, `POST /api/access/multi-account` — owner-only. |
| [app/main.py](../../backend/app/main.py) | Registers the access routes; installs `MultiAccountGuard` inside the public auth gate; brings the endpoint back at boot when the switch is ON and stops it on exit; serves the SPA shell `no-store` so an in-place upgrade cannot keep serving an old bundle. |
| [app/distributed/routes.py](../../backend/app/distributed/routes.py) | Public `GET /api/distributed/capabilities` (no token) so the UI adapts before sign-in. |
| [app/remote_access.py](../../backend/app/remote_access.py) | The status file/`public_url()` path now reflects the switch, and the "stale verification" reason points at the Settings switch as well as `start_public.bat`. |
| [app/config.py](../../backend/app/config.py) | `[accounts] multi_account` and `[security] last_public_url` (the remembered, inactive address). |
| [agent/agent.py](../../agent/agent.py) | Backend-address validation/warnings for the shipped agent, so a device keeps connecting correctly in single-account mode. |

**Frontend**

| File | Purpose |
|---|---|
| [src/api.ts](../../frontend/src/api.ts) | Typed client for the access endpoints (`accessState`, `setPublicAddress`, `recheckPublicAddress`, `setMultiAccount`). |
| [src/pages/SettingsPage.tsx](../../frontend/src/pages/SettingsPage.tsx) | The **Public Address** card (ON/OFF, state badge, address only when verified, Open/Copy/Re-check, honest busy + failure messages) and the **Multi-Account Mode** card (state, explanation, owner-gated switch). Account discovery is no longer requested while the layer is off. |
| [src/distributed/api.ts](../../frontend/src/distributed/api.ts) | `distributedCapabilities()` — the client's source of truth for the mode. |
| [src/distributed/DistributedPage.tsx](../../frontend/src/distributed/DistributedPage.tsx) | Hides the Accounts and Assistant-teams panels while the mode is off and shows why, with everything else (sync monitor, schools/classes, workers, results, updates, errors, audit) untouched. |

**Tests, tools, docs**

| File | Purpose |
|---|---|
| [tests/test_multi_account_mode.py](../../backend/tests/test_multi_account_mode.py) *(new)* | Default OFF; every blocked operation refused with the right code; the essential operations allowed; data preserved across OFF→ON; middleware cannot be bypassed. |
| [tests/test_public_access_toggle.py](../../backend/tests/test_public_access_toggle.py) *(new)* | Switch semantics, state machine, owner-only enforcement, failure honesty, no double tunnel, restart persistence (with a fake tunnel process — clearly marked as mocked). |
| [tests/test_public_access_hardening.py](../../backend/tests/test_public_access_hardening.py) | Added: the SPA shell must never be cached (found in a real browser). |
| [tests/test_remote_access.py](../../backend/tests/test_remote_access.py), [tests/conftest.py](../../backend/tests/conftest.py) | Status-file/`public_url` behaviour with the switch; the pre-existing suites keep their multi-account default. |
| [scripts/live_verify_access_switches.py](../../backend/scripts/live_verify_access_switches.py) *(new)* | The live, mock-free end-to-end verification (real app, real tunnel, real HTTP). |
| [DISTRIBUTED_DEPLOYMENT.md](../development/DISTRIBUTED_DEPLOYMENT.md) | New §3.1 (both switches, prerequisites, states, re-enabling), checklist entries and two new known limitations. |
| [documentation/README_RECIPIENT.md](../../MathChecker_Distribution/documentation/README_RECIPIENT.md) | Teacher-facing explanation of both switches and the `cloudflared` prerequisite. |

## 6. Additional infrastructure required

* **`cloudflared` on the machine that offers the public address.** Not bundled:
  `winget install --id Cloudflare.cloudflared`, or drop `cloudflared.exe` into
  the application's `tools\` folder, or point `MC5_CLOUDFLARED` at it. Without
  it the switch stays OFF and explains exactly that. Outbound access to
  Cloudflare is the only network requirement — no inbound port, no router
  change, no static IP, no account.
* A **sign-in account must exist** before the endpoint can be published (the app
  refuses to expose an endpoint nobody could sign into).
* If an operator runs their own tunnel/proxy outside the application, the app
  neither starts nor stops it and says so — do not read "Public Address OFF" as
  "external access closed" in that case.

## 7. Test results

| # | Check | Result |
|---|---|---|
| 1 | Full backend suite `python -m pytest -q` (incl. the new suites) | **PASS** — 1511 passed, 0 failed |
| 2 | Frontend typecheck + production build (`tsc -b`, `vite build`) | **PASS** |
| 3 | Live end-to-end with a real tunnel — [LIVE_ACCESS_SWITCHES_RESULTS.json](../results/LIVE_ACCESS_SWITCHES_RESULTS.json) (57 checks, `"mocked": false`) | **PASS** — 57 pass / 0 fail |
| 4 | Two-installation harness — [two_env_e2e_results.json](../results/two_env_e2e_results.json) | **PASS** — 31 pass / 0 fail / 0 blocked |
| 5 | Browser verification of the two cards, both switch directions, in the real running app | **PASS** (see §8) |
| 5b | Browser verification of the "process started before this feature" fallback | **PASS** — dependency stubbed in-page (the endpoint was made to fail); the branch, its text and the absence of both skeletons were checked |
| 6 | Packaged installer build (`build_release.bat` → Setup.exe, portable zip) | **NOT TESTED** — see §9 |
| 7 | Second physical machine reaching the address over the internet | **NOT TESTED** — the remote client was a separate process on the same host going through the Cloudflare edge (real internet round trip, not a second PC) |
| 8 | Named Cloudflare tunnel / customer proxy | **NOT TESTED** — out of scope (only the shipped quick-tunnel mechanism is controlled) |

What the live run (3) proves, all against the real endpoint:

* default multi-account state is OFF (`source=default`), no restart needed, key
  name as documented;
* an account, a school and a team created with the layer ON are **still there**
  after OFF → ON, while during OFF every one of seven account/team/device
  operations returned `403 multi_account_disabled` (with a valid Developer
  token), and with the layer ON an unauthenticated caller gets the ordinary
  `401`;
* while OFF: settings load, the **1–9 grade scale is intact**, the dashboard
  serves, `/api/distributed/ping` answers, heartbeats/registration work;
* Public Address ON creates a **real** `https://…trycloudflare.com` endpoint that
  answers all four verification checks; unauthenticated callers get 401 through
  it, `/docs` is 404, an authorised remote client signs in and reads data and
  heartbeats through it;
* killing the tunnel process makes the app report **FAILED with no active
  address** and an actionable reason, keeping the address only as *last
  verified*;
* switching OFF stops the process (a leftover-tunnel sweep found **0**), the old
  URL stops serving (`HTTP 530`), local access and the backend connection keep
  working, and the address is remembered;
* both settings survive a restart (Public Address `disabled`, multi-account
  `default`).

Browser checks (5), in the running app served by the backend: both cards render
with the right state; the multi-account switch turns ON and OFF (server config
follows); with it OFF the Multi-School page drops the Accounts and Assistant
teams panels and explains why, while everything else remains; the Public Address
switch shows `Working…` + "Switching … ON…" + a "takes up to a minute" note
while the tunnel is created, then `Connected` with the actual address, and
turning it off yields `Disabled` plus the inactive last-verified address and an
externally verified `530` on the old URL.

## 8. Bugs found and fixed during verification

1. **A stale address could stay on screen after a failed re-check** — `state()`
   returned the last URL even when the status was `failed`. Now the address is
   published only while a verification stands, and the two failure paths clear
   it explicitly. (Caught by a test, then re-verified live.)
2. **The SPA shell was cacheable, so an in-place upgrade kept serving the old
   interface** (observed in the browser: the page ran the previous bundle).
   `index.html` is now served `no-store`, with a regression test.
3. **A docstring claimed ordering that was not the real ordering** for
   `/api/distributed/*` (the session gate deliberately passes that prefix
   through to bearer auth, so a blocked call there answers 403 rather than 401).
   The comment now states the measured behaviour; no security bypass exists
   (403 refuses too, and with the layer ON the ordinary 401 applies).
4. **An application process started before this change showed two endless
   skeletons** (the new interface against a backend that has no
   `/api/access/state`). Found on the machine's own long-running instance.
   Both cards now explain it and point at the fix — reopen the app; no reinstall
   and no configuration change — instead of pretending to load. Verified by
   stubbing that one endpoint in the browser (the branch is real, the failing
   dependency was simulated); the healthy path is unaffected.

## 9. Limitations and residual risks

* **Packaging is not rebuilt here.** `backend/app` ships recursively (the new
  modules are included automatically) and `frontend/dist` is rebuilt by
  `build_release.bat`, but producing and smoke-testing `MathChecker_Setup.exe` /
  the portable zip was **not run** in this session. Run `build_release.bat`
  before distributing.
* **Browsers can throttle a hidden tab.** The ON transition can take up to ~60 s
  (tunnel + verification); the panel always shows the backend's measured state
  and catches up on the next poll/foreground, but a user watching a *hidden* tab
  may see "Working…" until they return to it.
* **A quick tunnel has no uptime guarantee** and no custom hostname (Cloudflare
  terms). For a permanent hostname, use a named tunnel or a VPN — but then the
  app does not control the endpoint and will say so.
* **`/heartbeat` remains presence-only and unauthenticated by design** (it can
  never claim an account). Unchanged by this work.
* **Single-account mode is not the same as "no accounts".** The distributed
  layer's login/`me`/`change-password` stay available (they are needed for device
  and sync identity); only the *management* operations are refused.
* The URL of a quick tunnel is public by nature: authentication, not obscurity,
  is what protects the data.

## 10. How to re-enable multi-account mode later

1. Settings → **Multi-Account Mode** → ON (or set `[accounts] multi_account =
   true` in `config.toml`, or `MULTI_ACCOUNT_ENABLED=1`).
2. No migration, restart or data step is required; existing accounts, roles and
   teams are exactly as they were. Verify with
   `GET /api/distributed/capabilities` and a Developer sign-in on
   `/#/distributed`.
