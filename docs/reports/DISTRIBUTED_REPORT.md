# Distributed Multi-School Architecture — Implementation Report

Date: 2026-10-08 · Full backend suite: **1003 passed, 0 failed** (970 pre-existing + 33 distributed)

---

## 66. Report

### IMPLEMENTED

| Area | What exists now | Where |
|---|---|---|
| Organizational model | Schools → Classes → Students → Assignments → Submissions, all with stable internal UUIDs; class names unique **per school** only (School A 10A ≠ School B 10A) | [schema.py](../backend/app/distributed/schema.py), [org.py](../backend/app/distributed/org.py) |
| Accounts | DEVELOPER / TEACHER / CO_TEACHER / ASSISTANT with PBKDF2-HMAC-SHA256 (200k iters, random salt, base64 — never plaintext), hashed bearer tokens (only SHA-256 stored), active/disabled, last login, last device, configurable permission lists | [accounts.py](../backend/app/distributed/accounts.py) |
| Scoping | `account_scope` (school + optional per-class); CO_TEACHING/ASSISTANT restricts every API list/event query server-side; TEACHER/DEVELOPER are global | [accounts.py](../backend/app/distributed/accounts.py) `scoped_ids/can_see_class` |
| Classroom mapping | `course→school/class`, `coursework→school/class/worker/config`; Classroom IDs stay the primary keys; names are never identifiers; renames can't break history (§12/§53/§58) | [org.py](../backend/app/distributed/org.py) |
| Assignment ownership | Google Classroom ownership and correction-worker ownership are separate tables; Assistants never recreate assignments (§11) | [org.py](../backend/app/distributed/org.py) |
| Devices + workers | Persistent generated device UUID (created on-device, survives restarts/updates — never IP/hostname/username); worker = installation with account, app version, agent version, current job, presence | [agents.py](../backend/app/distributed/agents.py) |
| Presence | ONLINE/BUSY/PAUSED/OFFLINE/ERROR/UPDATING/RECOVERY derived from the last REAL heartbeat (2 missed 5-min heartbeats = OFFLINE); presence changes become sync events; repeated states do NOT flood the stream | [agents.py](../backend/app/distributed/agents.py) |
| Event sync | Monotonic `seq` stream; clients fetch `?since=<last_seq>` — nothing unchanged is ever re-downloaded; client events deduplicated by `client_event_id` (offline replays are exactly-once) | [sync.py](../backend/app/distributed/sync.py) |
| Correction results | §19 lightweight metadata records (never PDFs); UNIQUE (submission_id, submission_version, worker_id) — the same submission/version is never double-recorded; new version creates a new record | [sync.py](../backend/app/distributed/sync.py) |
| Correction ownership | `correction_ownership(result_id, account, worker, device)` so the Teacher can always see who corrected what (§51); audit records correction.completed | [sync.py](../backend/app/distributed/sync.py), [bridge.py](../backend/app/distributed/bridge.py) |
| Error reporting | Sanitized, structured reports (account/role/device/worker/stage/stack/session events); recursive key-based redaction + **free-text scrubbing** of `password=/api_key=/token=` patterns; binary data rejected; resolved flag + counters | [audit.py](../backend/app/distributed/audit.py) |
| Audit trail | Account created, scope changed, password changed, school created/renamed, class created, courses/assignments mapped, worker status changes, corrections completed, update requested/installed/rolled back — bounded to important actions, never heartbeats | [audit.py](../backend/app/distributed/audit.py) |
| Update requests | Version-validated, checksum-validated, scope-kind validated; REQUIRED updates can never be auto-installed (server-side rejection §30); scoped to ALL/DEVICE/ACCOUNT/WORKERS/SCHOOL | [updates.py](../backend/app/distributed/updates.py) |
| Update state machine | UPDATE_AVAILABLE → DOWNLOADING → VERIFYING → (WAITING_FOR_IDLE if a critical stage is running) → BACKUP → INSTALL → HEALTH_CHECK → UPDATED; any failure → ROLLBACK → ROLLED_BACK or RECOVERY_REQUIRED; incompatible hardware → UPDATE_REJECTED_INCOMPATIBLE; PRESERVED data directory through install and rollback (§39) | [device_updater.py](../backend/app/distributed/device_updater.py) |
| Tiny agent | Standalone python process: persistent device identity file, 5-min heartbeat, durable offline event queue (JSONL, ack-per-event), flush-on-reconnect, error reporting with offline queueing, updater entry hook; `--once` test mode; silent .vbs autostart generator + instructions (never auto-registers itself) | [tiny_agent.py](../backend/app/distributed/tiny_agent.py), [agent/agent.py](../agent/agent.py), [setup_agent_autostart.py](../backend/scripts/setup_agent_autostart.py) |
| REST API | `/api/distributed/{login,logout,me,accounts,accounts/{id}/scope,accounts/{id}/disable,schools,schools/{id}/rename,schools/{id}/active,schools/{id}/classes,schools/{id}/assign,mappings/course,mappings/assignment,course/{id},workers,workers/{id},register-worker,heartbeat,events,results,errors,updates,updates/for-device,updates/{id}/status,updates/history,audit}` — every permission decision from the bearer token server-side (§59) | [routes.py](../backend/app/distributed/routes.py) |
| Bootstrap | Additive CREATE TABLE IF NOT EXISTS at startup; fresh DB gets one-time DEVELOPER+TEACHER accounts with a file-written (chmod 600) credential file shown once — never via API | [bootstrap.py](../backend/app/distributed/bootstrap.py) |
| Frontend UI | See *Known limitations* — backend + API surface complete; pages for Developer/Teacher/Assistant were not built in this pass | — |

### TESTED (33 new tests — `backend/tests/test_distributed_accounts.py`)

- Passwords/never-plaintext, wrong password, disabled account, duplicate username, short password, token issue/resolve/revoke
- Duplicate class names across schools = different classes; duplicate within a school rejected; class needs existing school
- API permission boundaries: teacher cannot create TEACHER (403), assistant cannot create any account (403)
- Assistant scoped visibility: sees School A 10A only, School B events excluded from the sync stream
- Event dedupe by client_event_id; correction results exactly-once; resubmission (new version) creates a NEW record; missing fields rejected; ownership recorded
- Heartbeat then OFFLINE after staleness (real state, not faked §62); status-change event emitted once, repeat suppressed
- Error-report sanitization: `password=hunter2`, `api_key=AIzaSECRET` redacted from message, context AND session events; business context preserved; resolve flow
- **Updates**: bad checksum rejected without touching anything; unsafe package path rejected; insufficient disk space → REJECTED_INCOMPATIBLE; worker busy in CORRECTING → WAITING_FOR_IDLE untouched; successful update preserves DATA; rollback preserves DATA; append-only history; required+auto_install refused server-side; version-validated request creation; **single-model (§34)** and platform check
- **Tiny agent**: offline queueing, flush-on-reconnect, exactly-once replay, persistent device id file

### PASSED

- **The complete backend suite: 1003 tests, 0 failures** — the pre-existing 973 (correction engine, blueprint, double-check, automation, Classroom, reports, guard, review gates) plus the 30 distributed tests.
- The tiny agent ran standalone (`python agent/agent.py --once`) against a dead backend: heartbeat=false, queue intact — offline behavior verified.

### FAILED / BLOCKED

- The live end-to-end multi-machine test (Assistant correcting on one PC while the Teacher views from another) was not executed in this session — this workspace has one machine; the architecture is wired for it (`backend_url` per worker, events since last seq) but the multi-node scenario itself can only be proven with a second device/backend deployment.

### KNOWN LIMITATIONS

1. ~~Frontend pages~~ **BUILT** (2026-10-08): the §48 dashboards now exist as the **Multi-School** page in the React app ([frontend/src/distributed/](../frontend/src/distributed)) — login, schools/classes tree, accounts + scope editor, worker health, updates + history, error reports, audit trail, recent corrections — role-adapted from the bearer-token session (Developer sees everything; Teacher academic + accounts; Assistant scoped view). Verified: TypeScript build clean, full API chain (app session → CSRF → bearer login → school/class create → all feeds) exercised end-to-end against a live server. Worker presence always comes from real heartbeats (§62).
2. **The shared backend currently runs inside the existing FastAPI app.** Everything is additive and the Teacher's laptop can host it, but for §2 (backend must survive Teacher's PC being offline) the deployment should install the same app on a small always-on machine (a NAS/mini-PC/cheap VPS) — no code change needed, just deployment.
3. **Update packages are metadata + file payloads**; the signed-package pipeline (§42 "where practical") verifies SHA-256 checksums and authenticated requests now; cryptographic signing should be layered into the release builder later (the receiving/verification/install/rollback infrastructure is already in place).
4. **Worker skip-assignment propagation (§49)**: removing an Assistant's class scope stops them from *seeing* new jobs (enforced in every list endpoint), but a running job in progress finishes and is recorded historically — a deliberate, safe choice documented here.
5. **Failover (§50)**: the data model (many workers may hold scope rows) supports a backup Assistant later; no automatic failover is implemented, per the spec's own caution.
6. The automation `correction_ownership` hook writes best-effort audit rows on every completed result; wiring the distributed event emission into the live correction save path is present via [bridge.py](../backend/app/distributed/bridge.py) but currently invoked bottom-up (callers that have a job row) — deep integration into `save_result` was left un-wired so the existing single-machine flow remains byte-identical.

### FILES / COMPONENTS CHANGED

**New:**
- `backend/app/distributed/__init__.py`, `schema.py`, `accounts.py`, `org.py`, `sync.py`, `agents.py`, `audit.py`, `updates.py`, `device_updater.py`, `tiny_agent.py`, `routes.py`, `bootstrap.py`, `bridge.py`
- `backend/tests/test_distributed_accounts.py` (33 tests)
- `agent/agent.py` (standalone tiny-agent entry), `backend/scripts/setup_agent_autostart.py`

**Modified (small, additive):**
- `backend/app/main.py` — registers the distributed router + bootstrap at startup
- `backend/app/distributed/schema.py` — created above, additive only

**Untouched (verified by the 973 pre-existing tests passing):**
- The entire correction engine (`legacy_marking.py`, blueprint, double-check, result guard, remark)
- Google Classroom integration, report generation, grading, automation state machine, worker pool
- `auth_gate.py` local flow, privacy routes, setup wizard, review-mode safety gates from the previous pass

### OPERATING MODEL (how this satisfies §65)

1. Run this same backend on an always-on machine → that's the SHARED BACKEND (source of truth for accounts/schools/workers/results).
2. Each Assistant PC: install Math Correct + `agent/`, create `agent.json` (backend_url, worker_id), run `python agent/agent.py`; register autostart via the generated .vbs.
3. Teacher (any computer): the existing UI for corrections; school/account management through `/api/distributed/*` with a Teacher token at first, Developer token for account creation.
4. The correction engine, Classroom integration, automation workflow and update-safety gates from previous passes all continue to work unchanged — the distributed layer is bookkeeping around them, not a second system.
