# Automatic Account Discovery, Device Registration & Tiny-Agent Startup

Report for the extension brief. Scope was deliberately kept to the parts that
were **genuinely missing**; the existing account system, correction engine,
Classroom integration, reporting and grade scale were reused unchanged.

## 1. What already existed (audited, reused, not rebuilt)

`backend/app/distributed/` already contained: roles DEVELOPER/TEACHER/
CO_TEACHER/ASSISTANT with multi-role support, PBKDF2 passwords, hashed bearer
tokens, server-side scope/permission checks, device+worker rows with
heartbeat-derived presence, an event sync stream with exactly-once
`client_event_id` dedupe, correction-result idempotency, a separate
report-delivery state machine, an audit trail, signed update delivery with
rollback, and the standalone tiny agent with a durable offline queue and the
Tiny Agent ON/OFF switch. None of that was touched.

## 2. What was missing — and is now implemented

A newly installed machine had **no way to identify itself without a human
account**, and **no way to see which Teacher/Developer accounts and devices
exist**. `register-worker`, `/workers` and `/accounts` all required an account
bearer token; there was no device credential at all.

### 2.1 New backend module — `backend/app/distributed/discovery.py`

* `register(device_id, …)` — registers a machine with **no credentials** and
  issues a **short-lived (24 h), revocable DEVICE credential**. Re-registering
  rotates the secret and invalidates the previous one. Only the **SHA-256** of
  the secret is stored.
* `resolve(device_id, credential)` — validates a credential and returns the
  **device** row. It can never return an account.
* `discover(device_id)` — returns every active TEACHER/DEVELOPER account with
  only the fields discovery may reveal: `role(s)`, `display_name`,
  `availability` (online/offline/unknown), `last_heartbeat`, device counts,
  `linked`, `sync_authorized`, `linking_required`. **No username, scope, or
  permission is ever returned.**
* `link_device` / `unlink_device` — explicit authorisation (§5). Discovery alone
  grants nothing; `sync_authorized` stays False until an account links the
  device.
* `revoke_device` — revokes every credential and link for a device.
* `list_devices` / `devices_for_account` — the registry for Teacher/Developer.

Availability is computed from **real heartbeats only** (an account with no
device is `unknown`, never `online`; a stale heartbeat flips to `offline`).

### 2.2 New backend module — `backend/app/distributed/secure_store.py`

The device credential is stored with the OS's own protection where available:
**Windows DPAPI** (`CryptProtectData`, per-user), with a `chmod 600` plaintext
fallback elsewhere. A one-byte scheme marker keeps the two interoperable.

### 2.3 Schema (additive only — `backend/app/distributed/schema.py`)

* `device_credentials` — hashed credential, expiry, revocation, usage counters.
* `device_links` — explicit device↔account authorisation.
* Two indexes. All `CREATE TABLE IF NOT EXISTS`; no destructive migration.

### 2.4 Routes — `backend/app/distributed/routes.py`

| Route | Auth | Purpose |
|---|---|---|
| `POST /api/distributed/device/register` | none (device id only) | issue device credential + first discovery |
| `GET  /api/distributed/device/discovery` | device credential | accounts + availability for this device |
| `GET  /api/distributed/devices` | account (`workers.view`) | device registry |
| `POST /api/distributed/devices/{id}/link` | account (`accounts.manage_non_teacher`) | authorise sync |
| `POST /api/distributed/devices/{id}/revoke` | account (`accounts.manage_non_teacher`) | revoke device |

`POST /api/distributed/heartbeat` now also accepts a device credential: the
device id is verified server-side and any account linkage is read from the
server, never from the client. An unlinked device heartbeats with `account_id`
NULL.

### 2.5 Tiny agent — `backend/app/distributed/tiny_agent.py`, `agent/agent.py`

The agent now auto-registers on startup, persists its credential via
`secure_store`, renews it before expiry, and refreshes the discovery directory —
**all of it inside the existing ON/OFF gate**, so an OFF agent makes zero
network calls (verified live). Still no password or account token is required
for registration/heartbeat/discovery.

### 2.6 UI

`GET /api/agent/discovery` (session-authenticated, beside `/api/agent/state`)
feeds a new **"accounts this computer has discovered"** list in the existing
Background Agent card ([SettingsPage.tsx](../frontend/src/pages/SettingsPage.tsx),
[api.ts](../frontend/src/api.ts)).

## 3. Security posture (each point covered by a test)

* Installer/source ships **no** credentials; registration needs none.
* A device credential **cannot** be used as an account token or run corrections
  (401) — a freshly installed agent cannot impersonate a Teacher/Developer.
* Only credential **hashes** are stored (a DB leak is not replayable).
* Anonymous discovery is refused (401); discovery reveals no username, scope,
  permission, or disabled account.
* Finding an account grants nothing until an explicit link (`sync_authorized`
  False → True).
* Devices are revocable; revocation invalidates credential and links instantly.
* Credentials are short-lived and rotate on re-registration.
* The credential is encrypted at rest (DPAPI) where the OS supports it.

## 4. Tests executed (actual results)

* **New:** `backend/tests/test_device_discovery.py` — **21 passed**.
* **Full backend suite:** **1369 passed, 0 failed** (`pytest tests/`, exit 0).
* **Frontend:** `tsc -b` clean; `vite build` clean (66 modules).
* **Live against the running backend** (`127.0.0.1:8642`):
  * register with no credentials → credential issued, `account_id` None;
  * anonymous discovery → **401**;
  * discovery with credential → DEVELOPER + TEACHER listed;
  * a TEACHER created **after** registration → **immediately discoverable**;
  * device credential used as an account token → **401** (both `/me` and
    `/devices`);
  * link → `sync_authorized` True; revoke → credential **401**;
  * real agent over real HTTP → `registered:true, heartbeat:true, accounts:2`,
    credential persisted and **reused on restart**;
  * agent while the switch is OFF → `registered:false, heartbeat:false`, no
    network calls.

Test accounts/devices created for the live probe were disabled/removed
afterwards; no real student data was touched.

## 5. Bug found and fixed outside the extension

**`pymupdf` was missing from `backend/requirements.txt` and from all three
bundled distribution runtimes.** The PDF engine hard-requires it
(`app/engine/mark_scheme.py` reports *"no PDF text extractor available"*
without it), so on a fresh install mark-scheme text extraction **silently
fails**. 10 tests in `test_mark_scheme_reconstruction.py` were failing for this
reason. Added `pymupdf>=1.24` to `requirements.txt`, installed it → the module
now passes **45/45** and the full suite is green.

## 6. Remaining configuration / blockers

1. **Rebuild the portable/release runtime.** The bundled `runtime/` copies in
   `MathChecker_Distribution/` were made before `pymupdf` was added and still
   lack it. Run `scripts/build_portable.py --with-runtime .venv` (or
   `build_release.bat --with-runtime …`) from a venv that has
   `pip install -r backend/requirements.txt`. Until then, an *existing* built
   artifact will still fail mark-scheme extraction.
2. **A production backend must be deployed** on an always-on machine — the
   architecture and all endpoints exist; the deployment itself
   ([DISTRIBUTED_DEPLOYMENT.md](../DISTRIBUTED_DEPLOYMENT.md)) is an operational
   step, not code.
3. **No live multi-machine proof in this session** — this workspace has one
   machine. The registration/discovery/sync path was verified against the real
   backend with a real agent, but not across two physical PCs.
4. Distributed login still has no rate limiting (pre-existing); keep the backend
   LAN/VPN-only.
