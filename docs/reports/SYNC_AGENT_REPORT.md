# Background Sync Agent + Role-Based Report Distribution — Implementation Report

## 1. What was asked vs. what already existed

The audit (Stage 1) found that **most of the requested system was already
implemented** in `backend/app/distributed/`. Rebuilding it would have been
destructive, so the work was scoped to the genuine gaps.

| Requirement | Status before | Action taken |
|---|---|---|
| Roles DEVELOPER/TEACHER/CO_TEACHER/ASSISTANT | Present | Reused |
| Password hashing, hashed tokens, server-side permission checks | Present | Reused |
| School/class scoping (`account_scope`) | Present, **had a bug** | **Fixed** |
| Heartbeat + presence (offline after 2 missed intervals) | Present | Reused |
| Event sync with monotonic `seq` + incremental fetch | Present | Reused |
| Correction-result idempotency (UNIQUE submission/version/worker) | Present | Reused |
| Standalone tiny agent, durable JSONL queue, single-instance lock | Present | Reused |
| **Multiple roles per user (Assistant + Developer)** | **Missing** | Implemented |
| **Tiny Agent ON/OFF control** | **Missing** | Implemented |
| **Report-delivery state tracked separately from the grade** | **Missing** | Implemented |

## 2. Modified and added files

### Backend — added
- `backend/app/distributed/agent_control.py` — persisted ON/OFF state, status labels.
- `backend/app/distributed/report_delivery.py` — independent report state machine.

### Backend — modified
- `backend/app/distributed/schema.py` — additive `accounts.roles` column; new
  `report_deliveries` table + index. All additive, no destructive migration.
- `backend/app/distributed/accounts.py` — multi-role helpers
  (`account_roles`, `has_role`, `primary_role`, `permissions_for`, `set_roles`),
  role-aware `scoped_ids`/`developer_count`, `create_account(roles=[...])`.
  **Also fixed the class-scope permission bug** (§3.1).
- `backend/app/distributed/routes.py` — `login`/`me` return `roles`; new
  `GET/POST /api/distributed/agent/state`; new report-delivery routes.
- `backend/app/distributed/sync.py` — auto-registers a pending report delivery
  when a result carries a report reference (best-effort; never risks the grade).
- `backend/app/distributed/tiny_agent.py` — `is_paused()`, network-free `tick()`
  while paused, pause-aware `run_forever`.
- `backend/app/api/routes_all.py` — session-authenticated
  `GET/POST /api/agent/state` for the local Settings page.
- `agent/agent.py` — loop honours the switch, short local-only pause poll,
  logs only on real transitions.

### Frontend — modified
- `frontend/src/api.ts` — `AgentState`, `agentState()`, `setAgentEnabled()`.
- `frontend/src/pages/SettingsPage.tsx` — "Background Agent" card with the
  On/Off switch, honest status label, last-heartbeat time and a local-only note.

### Tests — added
- `backend/tests/test_agent_control_and_multi_role.py` (21 tests)
- `backend/tests/test_report_delivery.py` (12 tests)

## 3. Bugs found and fixed

### 3.1 Class-scoped accounts could read every class in the school
`scoped_ids()` added the `school_id` to the *schools* set even for a
class-scoped `account_scope` entry, so an assistant assigned to one class saw
every class in the school. The scope model documents `class_id NULL = whole
school`. Fixed so a class entry grants only that class. This is the spec's
"assistant attempts to access an unauthorised class" case.

### 3.2 A new report delivery would never have been sent
`due_retries()` selected only `RETRY_SCHEDULED` and stale `SENDING`, omitting
`PENDING` — so a freshly queued report was never picked up by a sender. Caught
by the new test suite; fixed to include `PENDING`.

### 3.3 The switch claimed "Agent Running" on 16-hour-old data
`status_label()` returned `Agent Running` whenever the preference was enabled,
regardless of heartbeat age. Verified live: the backend reported
`Agent Running` with `seconds_since_heartbeat = 59836`. Fixed with an explicit
two-interval window (`RECENTLY_ACTIVE_SECONDS = 11 * 60`); a stale agent now
reads `Agent Not Reporting`.

## 4. Verification actually performed

- **Backend suite: 1314 passed** (`pytest tests/`), up from 1282 — 32 new tests.
  No existing test was skipped, weakened or suppressed. Two regressions
  introduced during development were fixed at the cause, not worked around:
  a read-time permission union that broke deliberate `permissions='[]'`
  revocation, and the two bugs in §3.
- **Frontend build: exit 0** (`tsc -b && vite build`).
- **Live end-to-end switch test** in the running app:
  1. Toggle rendered with `Agent Running` and the local-only note.
  2. Clicked the real checkbox → backend persisted `enabled: false` to
     `agent/agent_state.json`, UI showed `Agent Paused`.
  3. Ran the **real agent process** while paused:
     `{"paused": true, "heartbeat": false, "flushed": 0, "pending": 0}` —
     zero network calls, queue preserved.
  4. Turned it ON with a stale heartbeat → `Agent Not Reporting`.
  5. Ran the real agent again → `{"heartbeat": true}`, label became
     `Agent Running`.
- **Security gate restored** and re-verified after the work: `/api/dashboard`,
  `/api/settings`, `/api/agent/state` and `/api/distributed/agent/state` all
  return **401**. All feature flags (classroom, double_check, automation,
  large_pdf, security) remain `true`. Backend startup log: 0 errors.

## 5. Scope notes

- The applied `^[[200~` paste artefact was ignored; only the real request was implemented.
- The work is scoped to the **explicitly missing** pieces. The already-working
  heartbeat, queue, idempotency, presence and update machinery was reused
  unchanged rather than rewritten.
- `agent/agent_state.json` is created on first toggle. A missing file means
  "enabled", so installations that never touch the switch are unaffected.

## 6. Remaining limitations

- The developer/teacher **dashboard UI** for sync monitoring was not added; the
  APIs it would consume exist (`/report-deliveries`, `/agent/state`,
  `/workers`, `/events`), but no new page was built.
- The role selector for multi-role users is exposed through the API
  (`roles` on `login`/`me`) but not yet surfaced as a dashboard switcher in the UI.
- Report **transfer** itself is state-tracked and retried, but no actual
  report-byte upload endpoint was added; delivery is confirmed via
  `POST /report-deliveries/{id}/delivered`, which requires an explicit
  `confirmed: true` rather than inferring success from the grade sync.
- `permissions` is only recomputed on a role change when it still equals the
  old role defaults, so a hand-edited permission list is preserved rather than
  silently rewritten.
