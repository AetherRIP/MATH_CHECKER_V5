# Assistant Teams, Account Management & Developer Audit Logs

Extension report. The correction engine, grade scale (1–9), Google Classroom,
PDF generation, update system and Tiny Agent were **not** rebuilt — this is an
extension of the existing distributed layer, reusing its accounts, permissions,
sync stream and audit table.

## 1. Assistant teams and report sharing (§1)

* **`backend/app/distributed/teams.py`** (new): create/list teams (one school
  each), add/remove members, and the visibility helpers.
* Membership is the **only** thing that shares records. Removal sets
  `revoked_at` (never deletes), so history survives; access stops immediately
  because membership is recomputed on every read.
* **`sync.list_correction_results`** is now team-aware: a Co-teacher/Assistant
  sees its own scoped schools/classes **plus** teammates' records. The original
  `account_id` stays on every record, so the corrector is always known.
* **`report_delivery.list_deliveries(result_ids=…)`** scopes reports to the
  records a caller may read; Teacher/Developer remain unrestricted.
* Routes: `GET/POST /teams`, `GET /teams/{id}` (with members),
  `POST /teams/{id}/members`, `POST /teams/{id}/members/remove`,
  `POST /teams/{id}/active`, `GET /teams/{id}/results`, `GET /me/teams`.
* **`discovery.discover()`** now returns the teams a *linked* device may join
  (`member` flag), so a newly installed assistant discovers its team with no
  installer change; an unlinked machine learns no team names.

## 2. Account management (§2)

| Action | Route | Developer | Main Teacher | Assistant |
|---|---|---|---|---|
| Rename username | `POST /accounts/{id}/username` | any | Co-teacher/Assistant only | own only |
| Change display name | `POST /accounts/{id}/display-name` | any | Co-teacher/Assistant only | own only |
| Reset password | `POST /accounts/{id}/password-reset` | any | Co-teacher/Assistant only | ✗ |
| Change roles | `POST /accounts/{id}/roles` | any role | Co-teacher/Assistant only | ✗ |
| Activate/deactivate | `POST /accounts/{id}/disable` | any | Co-teacher/Assistant only | ✗ |
| Create account | `POST /accounts` | any role | Co-teacher/Assistant only | ✗ |

`accounts.can_manage_account()` is the single server-side rule: a **Developer**
manages any account; a **Teacher** manages only CO_TEACHER/ASSISTANT accounts
(never another Teacher or a Developer); everyone else only themselves.
`assignable_roles()` stops a Teacher minting a peer. Buttons are never the
control.

## 3. Username / password security (§3)

* `accounts.set_username()` validates length, character set and **uniqueness**,
  records from→to in the audit event.
* `accounts.admin_reset_password()` writes a new hash (PBKDF2, unchanged),
  **revokes every session** for the target, and audits without the password or
  hash. The actor never sees the old password.
* The reset is announced over the existing sync stream
  (`account.password_reset`) — the project has no email/SMS subsystem, so this
  is the supported notification mechanism.

## 4. Developer audit log (§4)

* **`audit.record()`** now stores `event_id` (uuid), `actor_role`,
  `affected_account`, `school_id`, `team_id` and `outcome`
  (success/failure/denied) alongside the existing fields; `details` stays
  sanitized so no password/hash/token can enter the trail.
* **`audit.list_audit()`** accepts `action`, `actor_account`,
  `affected_account`, `school_id`, `team_id`, `outcome`, `since`, `q` (search).
* `GET /api/distributed/audit` exposes those filters; there is **no write
  method**, so a client cannot forge, edit or erase its own history.
* Denied attempts are logged (`outcome="denied"`) for account and team
  operations.

## 5. Database migrations (all additive)

* New tables `teams`, `team_members` (+ indexes) via `CREATE TABLE IF NOT
  EXISTS`.
* New `audit_log` columns via `ALTER TABLE … ADD COLUMN` in
  `schema.ADDITIVE_COLUMNS`; existing rows keep NULL and read as `unknown`.
  No account, report or record is deleted.

## 6. Files changed

**Backend — new:** `backend/app/distributed/teams.py`,
`backend/tests/test_teams_and_account_management.py`.
**Backend — modified:** `schema.py`, `accounts.py`, `audit.py`, `sync.py`,
`report_delivery.py`, `routes.py`, `discovery.py`.
**Frontend — modified:** `frontend/src/distributed/api.ts` (account/team/audit
calls + types), `frontend/src/distributed/panels.tsx` (account edit modal,
`TeamsPanel` + shared-records view, audit panel with search/filters),
`frontend/src/distributed/DistributedPage.tsx` (Teams panel wired in).

## 7. Tests executed (actual results)

* **New:** `tests/test_teams_and_account_management.py` — **17 passed**.
* **Full backend suite:** **1386 passed, 0 failed** (`pytest tests/`, exit 0).
* **Frontend:** `tsc -b` clean; `vite build` clean (66 modules).
* **Live against the running backend** (`127.0.0.1:8642`):
  * Assistant A posts a correction; teammate **B sees it (1)**, an assistant in
    an unrelated school **sees 0**;
  * a linked device **discovers its team** (`TeamA`, `member=True`);
  * the audit trail returns 35 events with `account.created` and
    `team.member_added`, and every structured field
    (event_id / actor / role / affected / school / team / outcome) populated.

## 8. Remaining limitations

1. **No two-physical-machine test** — verified against the real backend with
   real assistants/tokens, but not across two PCs.
2. **No email notification** exists in the project, so password-reset notices
   travel over the sync stream, not email/SMS.
3. A **Main Teacher is global** in this architecture (TEACHER has no school
   binding), so "their authorized school" is enforced as "Co-teacher/Assistant
   roles only" rather than by a per-teacher school row.
4. The teams UI shows a team's shared records but does not page the full
   history; the API supports a `limit`.
