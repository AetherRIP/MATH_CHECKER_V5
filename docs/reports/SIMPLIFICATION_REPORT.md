# Math Checker — simplification, data portability and UI integration

Report on the controlled simplification of the AI Maths Corrector, as delivered.
Everything below was run against the real application and the real database; the
verification section marks untested items as untested rather than assumed.

---

## 1. Active features and navigation

Active main navigation is **AI · Settings · Storage** and nothing else.

| Page | What it now contains |
|---|---|
| **AI** | Tabs: **Correct** (the one unified PDF correction page) · **Classroom** (connect, pick assignment, mark scheme, correct-all, reports) · **Automation** (the live workflow visualizer) · **Results** · **Review** · **Models & Keys** · **Workers** · **Usage & Retries**. A shared **Sync Classroom** action sits in the header. |
| **Settings** | Grouped sections: Access & accounts (Public Address, Host account, Multi-Account Mode, Remote Access) · Google Classroom · Correction preferences · AI provider · Storage & data portability · Reports & identity. Plus the shared **Sync Classroom** action. |
| **Storage** | Real counts, database size, storage status, stored-correction list, **Export data**, **Import data**, and the shared **Sync Classroom** action. |

Every page that disappeared from the navigation still resolves and still renders
(its route is untouched), so existing bookmarks, search results and deep links
keep working. The top bar now names those pages correctly instead of falling
back to "Dashboard".

The default landing route changed from `#/dashboard` to `#/ai`: with three
active pages there is no separate dashboard to open, and an unknown hash must
not silently land on a page the navigation no longer offers.

---

## 2. Disabled features and how to re-enable them

Nothing was deleted. Disabled means *hidden and refused*, with the
implementation, the tables and the records left in place.

| Feature | State | How to bring it back |
|---|---|---|
| **Tiny Agent** | Off in this version. Source (`agent/agent.py`), the control module, the endpoints, the tables and every device/account/sync record are preserved. The auto-start installers refuse to create a boot task while it is off, and a surviving boot task is removed. | Set `[agent] tiny_agent = true` in `config.toml` **or** `MC_AGENT_ENABLED=1`, then restart. The refusal message names exactly this key, so the UI and the docs cannot drift. |
| **Multi-Account Mode** | Defaults OFF (single-account mode); the Settings switch turns it on without a restart and without deleting anything. | Settings → Multi-Account Mode → ON (owner/Developer only). |
| **Class page** | Removed from navigation. Tables, data and class discovery are untouched, because Google Classroom correction still uses them. | Open `#/classes` (route still valid) or re-add it to `NAV_GROUPS` in `frontend/src/App.tsx`. |
| **Legacy Corrections upload form / Large PDF Worker page** | Removed from navigation; the unified page is the single entry point. The pages remain reachable at `#/corrections` and `#/largepdf`, and their reusable pieces (`JobQueue`, `LargePdfJobDetail`) are what the unified page renders. | Re-add a `NAV_GROUPS` entry, or use the existing hashes. |
| **Distributed / Multi-School, Updates, Audit, Diagnostics, Prompts, Privacy, Reports, Students, Assignments, Dashboard** | Hidden from navigation; routes and pages intact. | Re-add to `NAV_GROUPS`. |

**No database migration is required.** No table, column or index was added,
changed or removed by this work.

---

## 3. Source files modified / added

### Backend — added
- `backend/app/portability.py` — the versioned, secrets-free export/import engine.
- `backend/app/api/data_routes.py` — `/api/data/inventory`, `/export`, `/import/preview`, `/import`.
- `backend/tests/test_data_portability.py` — 17 tests, including a real two-installation round trip.
- `backend/tests/test_unified_correction.py` — 8 tests for routing, both pipelines and pipeline attribution.

### Backend — modified
- `backend/app/main.py` — registers the data-portability router; logs the Tiny Agent availability at boot.
- `backend/app/api/routes_all.py` — added `POST /api/corrections/unified` and `GET /api/corrections/pipelines` (the unified page's backend, reusing the existing engine and the existing large-PDF worker).
- `backend/app/api/auth_routes.py` — `POST /api/auth/setup` keeps the existing username when none is supplied (see §5, bug fixed).
- `backend/app/automation/routes.py` — reports which real pipeline handled each automation job (`pipeline`, `pipeline_label`).
- `backend/app/config.py`, `backend/app/distributed/agent_control.py`, `backend/app/distributed/tiny_agent.py`, `backend/app/distributed/routes.py`, `agent/agent.py` — the Tiny Agent product switch (default off, durable, refuses to switch on, refuses to auto-start).
- `scripts/install_autostart.py`, `backend/scripts/setup_agent_autostart.py` — refuse to register an agent auto-start while it is off; remove a stale entry.
- `backend/tests/test_agent_control_and_multi_role.py`, `backend/tests/test_device_discovery.py`, `backend/tests/test_public_access_security.py` — updated/extended for the new default and for two fixed bugs.

### Frontend — added
- `frontend/src/SyncClassroom.tsx` — the one shared `useClassroomSync` hook, status line and `SyncClassroomButton`, plus `SyncStatusLine`.
- `frontend/src/pages/StoragePage.tsx` — storage status, corrections list, export and import.
- `frontend/src/pages/PdfCorrectionPage.tsx` — the unified PDF correction page.

### Frontend — modified
- `frontend/src/App.tsx` — navigation reduced to AI/Settings/Storage, `storage` route, route titles for hidden pages, AI as the landing page.
- `frontend/src/pages/AiPage.tsx` — now the correction workspace (tabs) rather than three engine-health tabs.
- `frontend/src/pages/SettingsPage.tsx` — grouped sections, host account card, corrected “public access” wording, shared sync action.
- `frontend/src/pages/CorrectionsPage.tsx` — job log extracted into the reusable `JobQueue`.
- `frontend/src/pages/LargePdfWorkerPage.tsx` — detail panel exported as `LargePdfJobDetail`; fixed two invalid table headers.
- `frontend/src/AutomationFlow.tsx`, `frontend/src/pages/AutomationPage.tsx` — pipeline badge and real started/finished/duration rows.
- `frontend/src/api.ts` — types and helpers for the new surfaces.
- `frontend/src/distributed/DistributedPage.tsx` — the Tiny Agent switch replaced by an honest "not used in this version" line.
- `MathChecker_Distribution/scripts/release_allowlist.py` — comment corrected (the agent's source ships, but this version does not run it).

---

## 4. Export / import format and compatibility rules

**Format:** plain UTF-8 JSON, downloaded as `math_checker_students_<timestamp>.json`.

```json
{
  "format": "mathchecker.transfer",
  "format_version": 1,
  "content": "student-results",
  "contains_secrets": false,
  "app_version": "5.2.1",
  "database_schema": 5,
  "exported_at": 1760000000.0,
  "exported_at_iso": "2026-10-12T03:30:16Z",
  "counts": { "...": 0 },
  "summary": { "students": 5, "classes": 2, "assignments": 3, "results": 8, "grades": 8 },
  "excluded": { "tables": { "gemini_keys": "...", "...": "..." } },
  "data": {
    "courses": [], "coursework": [], "students": [], "correction_jobs": [],
    "results": [], "result_reviews": [], "canonical_reports": [],
    "report_attachments": [], "grade_boundaries": []
  }
}
```

**Included:** student ids/names/emails/nicknames, classes, assignments (with
max marks and the custom title), the correction job rows the results depend on,
results with raw marks, maximum marks, percentage, the 1–9 grade, marking model,
confidence and reasons, question-level results and feedback (`result_json`),
the deterministic verification report, correction timestamps, review decisions,
report links/filenames and per-assignment grade boundaries.

**Deliberately excluded**, and written into the file itself:
`gemini_keys`, `key_stats`, `gemini_remote_files`, `job_remote_files`,
`retained_files`, `job_bandwidth`, `net_usage`, `app_meta`,
`classroom_submissions`, `classroom_state_events`,
`classroom_managed_assignments`, `classroom_publish_ops`, `submissions`,
`automation_*`, `large_pdf_*`, `repair_*`, `pending_manual_files`,
`pending_mark_schemes` — i.e. credentials, API-key fingerprints, usage
counters, machine-local caches and workflow queues. Report PDFs are not copied;
the package carries the references needed to regenerate them.

**Rules enforced in code:**
- `EXPORT_SPEC` is an allow-list of table/column pairs; an unlisted column can
  never leak.
- `FORBIDDEN_COLUMNS` is checked in **both** directions: a hand-edited file
  carrying `password`, `token`, `api_key` … is refused outright (400
  `forbidden_column`) and nothing is imported.
- Identifiers are preserved and the import order follows the foreign keys
  (courses → coursework → students → jobs → results → …).
- Validation happens entirely before any write; the write runs inside one
  `db.transaction()`, so a failure rolls back completely.
- Policies: **`add_only`** (default — an existing row is kept and reported) and
  **`update_existing`**, which additionally requires `confirm=replace` and takes
  a SQLite-API backup first, returning the backup path.
- Compatibility: any `format_version ≤ 1` is accepted; unknown tables are
  reported and skipped; missing NOT NULL bookkeeping columns get documented
  defaults (never invented academic data). A newer `format_version` is refused
  with an instruction to update the app.
- No code from the file is ever executed — it is parsed as JSON only.

---

## 5. Public Address behaviour (actual)

`Settings → Public Address` is a real ON/OFF switch for the app-managed endpoint
(`app/public_access.py`), not a label:

- **OFF** stops the tunnel process tree and writes the offline status, so the
  application advertises nothing. The saved configuration and the last verified
  address are kept, and the address is shown as *inactive — not being served*.
- **ON** creates the tunnel, verifies the endpoint end to end, and only reports
  **Connected** once it has answered; otherwise it reports **Failed** with the
  real reason and returns the switch to off. Sign-in and permissions stay
  enforced on that endpoint.
- **Documented limitation, shown in the UI:** a reverse proxy, router
  port-forward or tunnel configured outside the application keeps working
  whatever this switch says. The switch controls the endpoint the *application*
  manages.
- Local host access and outbound backend communication are unaffected either
  way; the switch does not touch the Multi-School backend URL.

**Host account** (§5): a real card in Settings, backed by `POST /api/auth/setup`.
- First account: local-only bootstrap, password ≥ 10 characters.
- Later changes require the current password, even on this machine, and are
  rate-limited exactly like sign-in.
- Stored only as a salted hash (`password_salt` + `password_hash` in the secrets
  store); never returned by an API response, never logged, never displayed.
  No universal default account exists.
- **Bug found and fixed:** the endpoint defaulted a *missing* username to
  `teacher`, so a Settings password change silently **renamed** an account
  created under another name (the card deliberately cannot display the stored
  username to an unauthenticated caller). It now keeps the existing username;
  the default applies only to the first account. Two tests cover this.

---

## 6. Test results

Final verified state: backend **`1540 passed`**, frontend `tsc --noEmit` clean and
`npm run build` succeeding. The first run after the agent switch reported 7
failures (see §6.5); the numbers below are from the final run.

### 6.1 Feature preservation

| Test | Result | Evidence |
|---|---|---|
| AI correction still works | **PASS** | `test_api_mock.py` end-to-end mock correction (9/12, verified), plus a live UI upload producing a stored result |
| Ordinary PDFs can be corrected | **PASS** | `test_unified_correction.py::test_auto_routes_a_short_pdf_to_the_standard_pipeline_and_marks_it` (2-page PDF → standard pipeline → 9/12); live UI run on a 1-page PDF |
| Large PDFs can be corrected | **PASS (routing + planning)** | `test_auto_routes_a_long_pdf_to_the_batched_pipeline` (6-page PDF, 3-page limit → batched job inspected and planned). A batched *marking* run needs AI keys; `test_large_pdf.py` covers the engine itself |
| Mark scheme processing correct | **PASS** | `test_mark_scheme_reconstruction.py`, `test_mark_scheme_upload.py`, `test_missing_questions_and_continuation.py` — all green |
| Grade scale stays 1–9 | **PASS** | asserted in `test_unified_correction.py` and observed live (9/12 → grade 7); boundaries are looked up, never invented |
| PDF reports generate and open | **PASS (unchanged code paths)** | `test_reporting*`/report routes green; `report_routes.py` untouched |
| Google Classroom synchronization works | **BLOCKED / NOT TESTED live** | No OAuth credentials in this environment, so no real sync was performed. The shared implementation is wired to the existing `/api/classroom/sync/start` + `/progress` (the same worker the Classroom page streams), `test_sync_monitor.py` is green, and the progress endpoint answers 200 from the running app |
| Student and result data persist | **PASS** | verified across a restart of the running app and by the two-installation import |
| Application works without the Tiny Agent | **PASS** | full suite + live runs with the agent off; `test_this_version_runs_no_tiny_agent_by_default`, `test_the_agent_runs_again_when_the_product_switch_is_turned_on`, `test_agent_state_route_refuses_to_switch_on_an_agent_this_version_does_not_run` |

### 6.2 Data portability

| Test | Result | Evidence |
|---|---|---|
| Export student/result data | **PASS** | live `GET /api/data/export` on a running app: 17 KB, 5 students, 2 classes, 3 assignments, 8 results with grades; no secret table and no secret column present |
| Import into a separate installation | **PASS** | exported from installation A (port 8699), imported into a fresh installation B (port 8698): `added: 26` (2 classes, 3 assignments, 5 students, 8 jobs, 8 results) |
| Identities, associations, marks, grades preserved | **PASS** | installation B returned `Student A 16/20 80% grade 8 · Demo — Algebra Homework` etc. via the ordinary API |
| Duplicate handling | **PASS** | re-import on A: `new 0, matching 26`; `add_only` → `added 0, skipped 26`, local values untouched |
| Invalid-file rejection | **PASS** | non-JSON → 400 `bad_export_file`; wrong format → `bad_export_format`; newer version → `export_too_new`; credential field → `forbidden_column` and nothing written |
| Import never silently overwrites or deletes | **PASS** | `test_add_only_never_overwrites_and_update_existing_only_when_asked`; `update_existing` without `confirm=replace` → 400 `confirm_required`; a conflicting file → 409 `import_conflict` with the database unchanged (full rollback asserted) |

### 6.3 UI, navigation, public address

| Test | Result | Evidence |
|---|---|---|
| Only AI, Settings, Storage in active navigation | **PASS** | browser snapshot + screenshot of the running app |
| Class page no longer a navigation destination | **PASS** | navigation snapshot lists exactly three items; `#/classes` still resolves to keep the data reachable |
| Settings matches the rest of the UI | **PASS** | screenshot: same cards, typography, section headings, badges, switch styling; no overflow or clipped text |
| Sync Classroom present and consistent on every active page | **PASS (UI)** | rendered in the header of AI, Settings and Storage via one shared component; a real sync is BLOCKED (no Classroom account) |
| Smaller window sizes: no overflow or overlap | **PASS (manual)** | checked at the panel width used throughout (~1140 px) and narrower; the tab bar scrolls rather than overflowing the page. A sub-800 px sweep was not systematically performed |
| Public endpoint disabled when OFF | **PASS** | live app reports `Disabled`, nothing advertised, local access and backend communication unaffected |
| Public endpoint reachable when ON | **NOT TESTED** | turning it ON creates a real tunnel to the internet; not exercised here |
| Login required for protected remote access | **PASS** | live app: unauthenticated API calls answered `auth_required`; the sign-in page is served and protects everything |
| Host credentials changeable securely | **PASS** | changed through the real Settings UI ("Account updated for hostteacher"), old password then 401 and the new one 204 |
| No plaintext password exposed after setup | **PASS** | secret scan of `/api/auth/status`, `/api/settings`, `/api/data/inventory` → 0 matches; the store holds only `password_salt` + `password_hash` |

### 6.4 Packaged Windows build (rebuilt and booted here)

| Test | Result | Evidence |
|---|---|---|
| Release pipeline runs end to end | **PASS** | `build_release.py --with-runtime build/runtime` → `RELEASE READY — Math Checker v5.2.1 (build 20261006)`; 3695 files staged, **privacy/secret scan clean (3696 files, no findings)**, 15 dependencies validated, installer compiled by Inno Setup, hashes + manifest written, final validation passed |
| New backend modules ship | **PASS** | `backend/app/portability.py`, `backend/app/api/data_routes.py` and `agent_control.py` are present in the packaged tree (the manifest's allowlist entries went 138 → 143) |
| Packaged app runs with no development machine | **PASS** | ran the portable build with its bundled Python (`runtime/python.exe main.py 8781`) on a **fresh, empty data directory**: database ready, bootstrap accounts created, worker pool started, large-PDF + automation schedulers started, boot recovery complete, frontend served (`/` → 200 with the freshly built `index-BwWQUpyl.js`) |
| Login enforced in the packaged app | **PASS** | unauthenticated protected calls answered `auth_required` / 401, including the new `/api/corrections/unified`, `/api/corrections/pipelines`, `/api/data/export` and `/api/agent/state` (existing routes, not swallowed as 404) |
| No development-machine dependency or personal path | **PASS** | scan of the staged app tree (excluding the bundled library runtime): no `MATH V6.0`, no build-machine user profile, no `AppData`/`OneDrive` path; no `.db`, `.env`, `.pem`, `.key`, PDF/XLSX or `Friend code` file staged; third-party hits are library source inside the bundled runtime, flagged by neither the privacy scan nor the allowlist |
| Artifacts | **PASS** | `MathChecker_Portable.zip` 56 042 370 B, `MathChecker_Setup.exe` 39 321 908 B, both hashed in `release_manifest.json` / `SHA256SUMS.txt` |

Note: this rebuild **replaced the previous release artifacts** (the build script's
clean step clears `portable/`, `release/` and the staging tree first), so the
hashes now in `SHA256SUMS.txt` supersede the earlier ones:

```
af5502dedf9f4868278901666661759cb144947a129095827534655d32e8719b  MathChecker_Setup.exe
ee62caf50bd70556b24ce4c7300ddf2ccb65ab5f22d3bc6f7931cb7289066288  MathChecker_Portable.zip
```

### 6.5 Automation and regression run

| Test | Result | Evidence |
|---|---|---|
| Ordinary PDF job | **PASS** | live UI job: `UI Verify Student COMPLETED — Correction completed 3s`, stored result 9/12 grade 7 |
| Large PDF job | **PASS (routing/planning)** | as §6.1; live batched marking not run (no AI keys) |
| Workflow visualization reflects real stages | **PASS** | the visualizer is driven by persisted job state; jobs now also carry the real `pipeline` / `pipeline_label` (`test_the_batched_pipeline_is_named_in_the_automation_view`) |
| Failure handling and safe retries | **PASS** | `test_repro_retry_approve.py`, `test_retry_architecture.py` green; `states.retry_step` remains the single rule the button and the backend share |
| Hiding the automation page does not disable the engine | **PASS** | the page is reachable as an AI tab and by `#/automation`; the scheduler, worker and endpoints are untouched |
| Full regression suite | **PASS** | `pytest -q` → see below (1540 passed) |

Full suite and frontend build (final run, after every edit above):

```
backend:  python -m pytest -q   → 1540 passed, 5 warnings in 291.71s
frontend: npx tsc --noEmit      → exit 0 (clean)
frontend: npm run build         → built in 1.42s
          dist/assets/index-BwWQUpyl.js 600.26 kB (gzip 161.89 kB)
          dist/assets/index-D4l_sYYh.css 48.61 kB (gzip   9.99 kB)
```

The first full-suite run after the agent switch reported 7 failures — every one
of them a pre-existing test asserting the *old* default (agent enabled /
heartbeat / device registration). They were updated to assert the new shipped
behaviour **and** to keep full coverage of the re-enable path (enable the switch,
then the identical code path runs). No assertion was weakened or removed; two
tests were added for the refusal path and the re-enable path.

---

## 7. Bugs found and fixed (not hidden)

1. **Password change silently renamed the host account** — `POST /api/auth/setup`
   defaulted a missing username to `teacher`. Fixed server-side; two tests added.
2. **A Tiny Agent could come back after a restart or update** — the auto-start
   installers would recreate a boot task for an agent this version does not run.
   They now refuse, remove a stale entry, and explain how to re-enable.
3. **The Settings page contradicted itself** — the Network card claimed "public
   internet access is deliberately not offered" while the Public Address card
   offered exactly that. The copy now describes the one supported mechanism and
   the switch's real limits.
4. **Invalid table headers** — three new tables (and two pre-existing ones on the
   large-PDF page) passed a bare array as the table head, which React renders as
   text outside any cell. Fixed to real `<tr><th>` headers.
5. **Duplicate sync implementations would have been the easy path** — the three
   pages share one hook and one backend run registry instead, so a double click
   cannot duplicate Classroom reads or writes and success is only reported from
   the finished run.
6. **Tiny Agent UI showed a switch that could not take effect** — it now states
   plainly that no agent runs in this version, and the endpoint refuses to
   switch one on.

---

## 8. Remaining requirements, blockers and untested behaviour

- **Classroom synchronization was not executed against Google.** No OAuth
  credentials exist here. The button, the shared service, the progress contract
  and the "success only from the finished run" rule are verified by code,
  backend tests and a 200 from the live endpoint, but **a real sync is unproven**.
- **Public Address ON was not exercised.** Turning it on creates a live internet
  tunnel; OFF, the disabled state and the local path were verified.
- **No live end-to-end batched (large-PDF) marking run** — routing, inspection
  and planning are verified; the AI step needs real keys.
- **The packaged Windows build was rebuilt and booted**, but only with
  `--with-runtime build/runtime`; the **installer's own install/uninstall flow
  was not executed** (no Inno Setup install was run here), and the portable
  build was verified on this machine's Windows, not on a clean recipient
  machine. The release folder's hashes changed with the rebuild — the previous
  artifacts were replaced, so the new `SHA256SUMS.txt` is the current one.
- **No migration script is needed** and none is shipped: no schema change was
  made, and the packaged app bootstrapped a fresh database on its own.
- **Narrow-viewport sweep** below ~800 px was done by hand at a few widths, not
  as a systematic pass.
- Environment/configuration: **none added**. Re-enabling the Tiny Agent needs
  `[agent] tiny_agent = true` or `MC_AGENT_ENABLED=1`; no other change.
