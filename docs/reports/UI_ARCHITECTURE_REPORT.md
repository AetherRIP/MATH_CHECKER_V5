# UI architecture correction — separate pages, real Task Manager, workers on the AI page

2026-10-12 · app 5.2.1 · package build `20261012`

---

## 1. What was actually wrong (audit before the change)

| Symptom | Cause found in the code |
|---|---|
| "Every page is inside the AI page" | [`frontend/src/pages/AiPage.tsx`](../../frontend/src/pages/AiPage.tsx) was an 8-tab hub: **Correct, Classroom, Automation, Results, Review, Models & Keys, Workers, Usage** — each tab rendering the component its own page used to render. |
| Missing Assignment page | [`frontend/src/pages/AssignmentsPage.tsx`](../../frontend/src/pages/AssignmentsPage.tsx) still existed and still rendered, but `NAV_GROUPS` in [`frontend/src/App.tsx`](../../frontend/src/App.tsx) listed only **AI, Settings, Storage**, so it had no navigation entry and no default route. |
| No Task Manager | No such page existed anywhere: `WorkersPage` showed worker *configuration*, the Corrections page showed a job log, and nothing unified correction / batched / Classroom / sync work. |
| Confusing "AI keys", "Workers", "Usage" links | `#/keys`, `#/workers` and `#/usage` were aliases that all redirected into the AI hub. |

Nothing had been deleted from the backend or the database: this was purely a
navigation/page-boundary problem, which is what this change repairs.

---

## 2. Where every page lives now

Navigation (5 groups, every page its own entry — developer pages appear
additionally for a developer account):

| Group | Entries |
|---|---|
| Main | Dashboard · **AI** · **Task Manager** · Correct a PDF · **Assignments** |
| Corrections | Results · Review Queue · Large AI Worker · Automatic Correction |
| Google Classroom | Google Classroom · Classes · Students · Reports |
| Data & system | **Storage** · Internet Usage · Privacy & Data · **Settings** |
| Developer | Diagnostics · Gemini Prompts · Accuracy Audit · Multi-School · Application Updates |

Routes: `dashboard, ai, tasks, correct, assignments, results, review, largepdf,
automation, classroom, classes, students, reports, storage, usage, privacy,
settings, diagnostics, prompts, audit, distributed, updates` — plus the legacy
`corrections` page and the `#/keys` → `#/ai`, `#/workers` → `#/ai`,
`#/report` → `#/reports` aliases, so no old bookmark or search result breaks.

### The AI page — four sections, nothing else

| Tab | Content | Data source (all real) |
|---|---|---|
| API & providers | Provider keys, rotation, health, connectivity test | `KeysPage` → `/api/keys`, `/api/keys/test` |
| Normal PDF worker | Status (IDLE/PROCESSING/error), worker count control, per-worker table, throughput, queue, retry count, single-pass page limit | `/api/workers`, `/api/workers/resize`, `/api/settings`, `/api/corrections/pipelines` |
| Large PDF worker | Enabled/threshold/batch size/concurrency, queued-processing-review-failed counts, live batch + page progress, per-job retry/stop, link to the full page | `/api/large-pdf/jobs`, `/api/large-pdf/jobs/{id}/retry|cancel` |
| Network | The backend this window talks to (a real request with its latency), the address actually serving (from `/api/auth/status.local_url`), access mode, and — separately — Public Address with its ON/OFF switch, verification and reasons | `/api/health`, `/api/auth/status`, `/api/settings`, `/api/access/state`, `/api/access/public-address[/recheck]` |

Classroom, Automation, Results, Review, Usage, Assignments, Storage, Settings,
Reports, Classes, Students and the correction workspace are **not** tabs here any
more; they are their own pages again.

### The Task Manager — real work, one row per task

[`frontend/src/pages/TaskManagerPage.tsx`](../../frontend/src/pages/TaskManagerPage.tsx)
unifies, from the backend's own records:

* `/api/jobs` + the `/api/jobs/stream` SSE events → single-pass corrections, with the
  real `status`, `stage_detail`, `progress`, attempts, timestamps and error;
* `/api/large-pdf/jobs` + per-job progress → batched documents (batch X of Y, pages
  being processed, questions marked, stage, retry/stop);
* `/api/automation/jobs` + `/api/automation/status` → the Classroom workflow, with
  approve / retry-from-step / retry-report actions that call the real endpoints;
* `/api/classroom/sync/progress` → the live synchronization run;
* `/api/workers` → the pool executing all of it, and the Tiny Agent's real state
  (reported **Off** while the setting is disabled — never started).

Honesty rules implemented in the page: **one row per piece of work** (a batched
job that is also mirrored in `/api/jobs` under the same id, and a Classroom
workflow that owns its correction job, each appear once); **no fabricated
progress** (a percentage is drawn only when the backend reported one — the
Classroom workflow shows its stage instead); filtering by type, state and free
text.

---

## 3. Files changed

| File | Change |
|---|---|
| `frontend/src/App.tsx` | Navigation restored (5 groups, every page its own entry); routes `tasks`, `correct`, `usage` added; `ai` now receives `onNavigate`; aliases `keys`/`workers` → `ai`; titles for the new routes |
| `frontend/src/pages/AiPage.tsx` | Rewritten: exactly four tabs (API & providers, Normal PDF worker, Large PDF worker, Network) |
| `frontend/src/workers/panels.tsx` | **New** — `NormalWorkerPanel` and `LargeWorkerPanel` (moved out of the retired Workers page, same real endpoints, same controls) |
| `frontend/src/pages/TaskManagerPage.tsx` | **New** — the Task Manager |
| `frontend/src/pages/NetworkPanel.tsx` | **New** — network configuration panel |
| `frontend/src/pages/WorkersPage.tsx` | Removed — its two worker panels are the AI page's worker tabs; `#/workers` still resolves (to the AI page) |
| `frontend/src/pages/AssignmentsPage.tsx` | Shared `SyncClassroomButton` added to the header so a sync runs from the Assignment page |

No backend file was changed: the correction engine, both workers, Classroom
sync, reporting, storage/export-import, the 1–9 grade scale, the Public Address
setting and the disabled Tiny Agent are untouched. No page, table, endpoint or
record was deleted.

---

## 4. Verification (PASS / FAIL / BLOCKED / NOT TESTED)

**Environment.** `npm run build` (`tsc -b && vite build`, `strict`,
`noUnusedLocals`) → exit 0. A live instance of this build was run on port 8660
with an isolated demo data directory, driven through its own API and a real
browser; real jobs were created so the pages had genuine records:
standard pipeline → `COMPLETED` with a stored result, and a forced batched run
(`pipeline=large`) → `COMPLETED` with a stored result.

| # | Requirement | Result | Evidence |
|---|---|---|---|
| 1 | AI page contains API, normal worker, large worker, network | **PASS** | Exactly 4 tabs `API & providers · Normal PDF worker · Large PDF worker · Network` |
| 2 | Normal worker shows real status/config/controls | **PASS** | `0/2 active`, `W1/W2 IDLE` from `/api/workers`; worker count segmented control calls `/api/workers/resize`; `Retries per submission: 2` from `/api/settings`; throughput `0.4s avg / 1 measured` from the real job |
| 3 | Large worker shown separately, with batching | **PASS** | `ENABLED`, `2 batches at a time`, batch/page progress + retry/stop from `/api/large-pdf/jobs` |
| 4 | Network configuration, backend vs Public Address separated | **PASS** | Backend probe `HTTP 200 in 17 ms`, address `http://127.0.0.1:8660` (live port from `auth/status.local_url`, not the configured 8642), access-mode selector; Public Address card shows `Off` with its real state and separate ON/OFF + Re-check |
| 5 | Task Manager is a separate page with real jobs and progress | **PASS** | 11 real records; chips `Correction 10 · Large PDF 1`; the batched job shows its real 100% bar, `1 pages · 1 batches`; SSE stream connected (`/api/jobs/stream` 200) |
| 6 | No duplicated task records | **PASS** | The batched job mirrored in `/api/jobs` under the same id is listed once; row count dropped 12 → 11 after the dedupe by id (and submission for Classroom workflow jobs) |
| 7 | No fabricated progress/stages | **PASS** | Percentage rendered only when the backend reports one; the Classroom workflow row shows its stage; Tiny Agent reads `Off (disabled in Settings)` |
| 8 | Assignment page restored and functional | **PASS** | `#/assignments` renders (filters All/Managed/External/Local, Create Assignment, search) and its header now carries the shared **Sync Classroom** button |
| 9 | All other pages have separate navigation entries | **PASS** | Teacher: 17 entries in 4 groups; developer: 22 entries in 5 groups (Dashboard, AI, Task Manager, Correct a PDF, Assignments, Results, Review Queue, Large AI Worker, Automatic Correction, Google Classroom, Classes, Students, Reports, Storage, Internet Usage, Privacy & Data, Settings + Diagnostics, Prompts, Accuracy Audit, Multi-School, Updates) |
| 10 | Navigation works between pages, correct component, no state loss | **PASS** | All 22 routes + legacy `#/corrections`, `#/keys`, `#/workers`, `#/report` visited; every one rendered its own heading with no crash and no wrong component |
| 11 | Normal and large correction work independently | **PASS** | Two real jobs through `/api/corrections/unified`: `auto` → single-pass `COMPLETED`; `large` → batched `COMPLETED` (1 page, 1 batch), each with its own stored result |
| 12 | Task Manager reflects actual worker activity | **PASS** | Live pool row `0/2`, queue counts and per-job states come from the same endpoints the workers update; the completed probe job appeared with its real duration (0.4 s avg) |
| 13 | Settings, Storage, export/import still work | **PASS (unchanged code path)** | Both pages render from the same components and endpoints as before (`/api/settings` 200, `/api/data/inventory` 200); export/import itself was verified end-to-end by the packaged verifier (see the packaging report) |
| 14 | App launches with no routing errors / console exceptions | **PASS** | No React errors; the only console entries are the pre-sign-in 401 and 403s from developer-only endpoints `/api/diagnostics`, `/api/diagnostics/db`, `/api/dev/prompts`, which I deliberately opened by hash while signed in as a teacher (the role gate correctly refuses them). Re-checked on the **packaged** copy: see §4.1 |
| 15 | Google Classroom synchronization and assignment operations | **BLOCKED** | No Google OAuth client exists in this environment, so the sync cannot be run for real. The shared sync button is present and wired on the Assignment page; the sync row appears in the Task Manager from `/api/classroom/sync/progress` when a run exists. Same limitation as before this change. |
| 16 | Automated tests | **PASS (no backend change)** | `cd backend && python -m pytest -q` → **1550 passed** (run after the last backend edit). The frontend has no test runner in this project; its checks are the strict TypeScript build plus the live browser audit above. |
| 17 | Packaged build re-verified after the UI change | **PASS** | Release rebuilt from source and re-verified: **36/36** packaged checks (`MathChecker_Distribution/scripts/verify_portable.py`), including "user interface is served — bundle referenced" on the new bundle |

### 4.1 The corrected UI, checked inside the delivered package

The rebuilt `MathChecker_Portable.zip` was extracted to a temp folder and started
with its own launcher (port 8670, demo mode, isolated data), then driven in a
real browser:

* the packaged `frontend/dist` serves the corrected bundle
  (`assets/index-BM-mpZrv.js`, the same hash as the local build);
* a genuinely fresh extract shows the **first-run wizard** first (expected), and
  after setup the shell renders with the full navigation — 17 page entries plus
  the collapse control for the teacher role;
* `#/tasks` renders the **Task Manager** as its own page;
* the **AI** page shows exactly the four tabs, and the Normal PDF worker section
  renders its real state;
* `#/assignments` renders with the **Sync Classroom** button in its header.

(The packaged copy used for this check was stopped afterwards with its own
shutdown path — `Stop.bat` itself is a PASS item in the packaged verification,
which also proves data survives the stop and a restart.)

---

## 5. Limitations

* **Classroom sync / real Google account** — not exercised here (no OAuth client
  available); needs a real account on the teacher's machine.
* **Developer-only pages** (Diagnostics, Prompts, Audit, Multi-School, Updates)
  are visible to a developer account and hidden from a teacher account by the
  existing role rule; I verified both roles.
* **Tiny Agent** remains disabled exactly as configured — the Task Manager only
  reports its state and never starts it.
* The AI page intentionally does **not** host job history: corrections, batches,
  syncs and workflows are monitored in the Task Manager, and the correction
  history log stays on the legacy `#/corrections` page.

---

## 6. Round 2 — three systemic defects, found by measuring the rendered app

This round did not start from a screenshot review; it started from a script that
asked the live DOM two questions about every page at several window sizes:
*does anything extend past the page container, and does any container report
more scrollable width than it can show?* That is what surfaced three defects that
are invisible on a wide screen and obvious on a small one.

### 6.1 Tooltips were clipped, and caused phantom horizontal overflow on every page

**Measured cause.** `[data-tooltip]::after` in [`frontend/src/styles.css`](../../frontend/src/styles.css)
rendered the hint as an absolutely positioned pseudo-element inside the page.
`.main-scroll` carries `overflow-x: hidden`, so at 800 px wide the Settings page
reported `main-scroll.scrollWidth = 1025` against `clientWidth = 790`: **235 px of
content the user could not reach, while the Sync Classroom tooltip itself was
being cut off by exactly that rule** (`button.btn.secondary::after`, 626 px wide,
`white-space: nowrap`, no wrapping and no viewport awareness).

**Fix** — [`frontend/src/TooltipLayer.tsx`](../../frontend/src/TooltipLayer.tsx), one delegated
layer mounted once in [`frontend/src/App.tsx`](../../frontend/src/App.tsx):
portalled to `<body>`, positioned `fixed` (so it is outside every scrolling or
clipping ancestor), wrapped with `max-width: min(340px, 100vw - 20px)`, flipped to
whichever side has room (honouring `data-tooltip-side="right"` for the collapsed
rail), clamped to an 8 px viewport margin, and linked to its trigger with
`aria-describedby` while shown. Pages keep writing the same `data-tooltip` /
`tooltip=` attributes, so no page code changed.

**Measured after the fix** (viewport 720×520, Settings header):

| Check | Before | After |
|---|---|---|
| `.main-scroll` overflow | 235 px | **0 px** |
| Tooltip inside the viewport | no (clipped) | **yes** — `side=top`, clamped to `left 372 → right 712`, 340 px wide, 3 lines |
| Shown only after a deliberate hover | n/a (CSS `:hover`) | **yes** — nothing at 120 ms, shown at 320 ms |
| Keyboard focus shows it immediately | no | **yes** — `focusin` → `role="tooltip"` + `aria-describedby="mc-tooltip-layer"` |
| Escape / scroll / pointer-down / blur dismiss it | partial | **yes** — all four tested, `aria-describedby` removed again |
| Collapsed rail (`68 px`) tooltip | decorative CSS only | **yes** — `side=right`, `left 68 → right 149`, inside the viewport |

### 6.2 Controls that forgot `className="input"` fell back to the browser's widget — and could not shrink

**Measured cause.** The Settings page still overflowed 13 px after 6.1. The
offender was the classless Session-label `<input>`: inside a 143 px label it kept
its **intrinsic 170 px** width, because the shared look was attached only to
`.input`. The same page had 10 such controls plus a bare `<select>` — 60 classless
inputs, 15 selects and 4 textareas exist across 22 files, so in the dark theme they
rendered as light native widgets, which is both an overflow risk and an
inconsistency against every styled field next to them.

**Fix** — the input block in [`frontend/src/styles.css`](../../frontend/src/styles.css) now
also matches `input/select/textarea:not([class])` (excluding checkbox, radio,
range, file, color, hidden and the button types), so the shared look and the
`width: 100%; min-width: 0` shrink rule apply to a field that forgot the class,
while anything a component styled on purpose (search box, filter bars) is
untouched because it carries a class.

**Measured after the fix** — Settings at 800/1024/1366 px: `documentElement`,
`.main-scroll` and `.main-inner` overflow are all **0 px**; of the 10 classless
controls on the page, **10/10** now report the shared `8px 11px` padding, theme
background, border and radius, and every field is inside its card and the
viewport (`allInside: true`, widest right edge 1300 of 1325 available). Verified
in both themes (light: `rgba(255,255,255,.72)` background, `#d3dae4` border).

### 6.3 Live pages polled the API forever, busy or not

**Measured cause.** The Task Manager page alone ran three fixed timers that never
adapted to the work: `/api/jobs?limit=50` every 4 s, `/api/large-pdf/jobs` and
`/api/workers` every 2.5 s, `/api/automation/*` every 5 s, `/api/classroom/sync/progress`
every 2 s — roughly **90 requests per 45 s on a completely idle page**, on the same
machine that is doing the correcting.

**Fix** — [`frontend/src/useAdaptivePoll.ts`](../../frontend/src/useAdaptivePoll.ts): one
cadence rule (fast while work is in flight, slow when idle, silent while the window
is hidden, with the next timeout chosen after each run), applied to the worker
panels (AI page), the Large PDF page, the correction page's queue summary, the
Task Manager's three sources and the shared `useJobQueue`. The SSE stream stays the
primary channel for correction jobs; the poll is only the safety net, and it runs
fast whenever that stream is not delivering.

**Measured after the fix** (Task Manager, viewport 1366, log cleared, 45 s idle):
**18 requests instead of ~90**, in two rounds (≈20 s apart) instead of eleven, with
live data still rendering — the page shows its real 8 records, 4 state tiles and
the per-task actions. The idle round and the immediate first load both fired, so
the page is never stale on open.

### 6.4 Result of round 2

| Check | Result | Evidence |
|---|---|---|
| Strict TypeScript build | **PASS** | `npx tsc -b --force` → exit 0; `npm run build` → exit 0 (`index-5ynp92cM.js`, `index-BP0ZVPxU.css`) |
| No horizontal overflow at 800 / 1024 / 1366 px | **PASS** | `documentElement`, `.main-scroll`, `.main-inner` = 0 px on Settings, AI, Task Manager, Correct a PDF, Assignments, Results, Reports, Students, Classes, Review, Usage, Automation, Large PDF, Google Classroom, Privacy |
| Tooltip behaviour (hover, keyboard, Escape, scroll, blur, rail, right-edge flip) | **PASS** | §6.1 table, measured in the live app |
| No clipped/unreachable control | **PASS** | Every input/select/textarea is inside its card and the viewport; longest right edge 1300 < 1325 available < 1366 viewport |
| Console clean across the pages | **PASS** | No console entries while visiting all routes; API log shows 200s only |
| Polling reduced while idle | **PASS** | 90 → 18 requests per 45 s on an idle Task Manager |
| Fast cadence still used while work runs | **NOT MEASURED** | The branch is three lines of the same scheduler and every busy source was verified to report its real counts, but no job was started this round to time it, so the claim rests on code inspection, not a measurement |
| Silent while the window is hidden | **NOT MEASURED** | Implemented (the tick is skipped and re-scheduled when `document.hidden`); the harness keeps every tab `hidden: false`, so it could not be exercised here |
| Backend regression | **PASS (no backend change)** | `python -m pytest backend/tests/test_public_access_hardening.py -q` → **24 passed**; the backend source was not touched this round |
| Frontend unit tests for the new hook | **NOT POSSIBLE** | This project has no frontend test runner (`package.json` has `dev`/`build`/`preview` only); adding one would be a new dependency, so the hook is covered by the live measurements above |

### 6.5 What round 2 did not do

* `MathChecker_Distribution/release/` still holds the **previous** package: its
  embedded `frontend/dist` carries `index-BM-mpZrv.js` / `index-D4l_sYYh.css`,
  while the source now builds `index-5ynp92cM.js` / `index-BP0ZVPxU.css`. The
  release needs a rebuild for the ZIP to contain the fixes in §6.1–6.3; nothing in
  the source is out of date.
* The remaining fixed pollers that are *not* on the pages touched here
  (Diagnostics 8 s/10 s, Usage 5 s, Automation 5 s, Classroom batch 2 s while a
  batch is tracked, `Settings`/`NetworkPanel` 10–15 s, the App's own 25–30 s
  shell/attention refresh) keep their current cadence; the adaptive hook is ready
  to replace them but they were left as they are in this round.
