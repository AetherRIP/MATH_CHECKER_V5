# Complete Page Inventory

Every addressable screen in Math Checker, what it does today, how it is composed, and how it
was inspected. **29 of 30 screens were rendered in a real browser and DOM-measured**; 5 were
also captured as screenshots. Nothing here is inferred from filenames alone.

Inspection legend: **V** = visually inspected (screenshot) · **D** = rendered and DOM-measured ·
**S** = source inspected only. All measurements at 1440×900 unless stated.

---

## 0. Summary table

| # | Hash route | Screen | Source | Inspect | Scroll height | Findings |
|---|---|---|---|---|---:|---|
| 1 | `#/dashboard` | Dashboard | `pages/DashboardPage.tsx` | V D | 2,640 px | RD-003, RD-018, RD-029 |
| 2 | `#/ai` | AI (4 tabs) | `pages/AiPage.tsx` + `KeysPage`, `NetworkPanel`, `workers/panels.tsx` | V D | 1,155 px | RD-003, RD-012, RD-031 |
| 3 | `#/tasks` | Task Manager | `pages/TaskManagerPage.tsx` | D | 1,114 px | RD-027, RD-032 |
| 4 | `#/correct` | Correct a PDF | `pages/PdfCorrectionPage.tsx` | V D | 975 px | RD-002, RD-016, RD-017 |
| 5 | `#/assignments` | Assignments | `pages/AssignmentsPage.tsx` | D | 1,631 px | RD-027, RD-031 |
| 6 | `#/assignments/{id}` | Assignment detail | `pages/AssignmentsPage.tsx:930-1000` | D | 1,034 px | RD-013 |
| 7 | `#/results` | Results | `pages/ResultsPage.tsx:1580+` | V D | 3,083 px | RD-025, RD-027 |
| 8 | `#/results/{id}` | Correction detail (marking workspace) | `pages/ResultsPage.tsx:1560+` | V D | 6,255 px | RD-020, RD-007, RD-006 |
| 9 | `#/review` | Review Queue | `pages/ReviewPage.tsx` | D | 840 px | RD-021, RD-022 |
| 10 | `#/scheme-review` | Mark-Scheme Review | `pages/SchemeReviewPage.tsx` | D | 840 px | RD-001, RD-022 |
| 11 | `#/scheme-review/{id}` | Discrepancy detail | `pages/SchemeReviewPage.tsx` | S | — | UNVERIFIED |
| 12 | `#/largepdf` | Large PDF Worker | `pages/LargePdfWorkerPage.tsx` | D | 840 px | RD-017 |
| 13 | `#/automation` | Automatic Correction | `pages/AutomationPage.tsx` | D | **24,493 px** | RD-023 |
| 14 | `#/classroom` | Google Classroom | `pages/ClassroomPage.tsx` | D | 840 px | RD-019 |
| 15 | `#/classroom/{c}/{w}` | Classroom tracking | `pages/ClassroomPage.tsx:600+` | D | 4,562 px | RD-027 |
| 16 | `#/classes` | Classes | `pages/ClassesPage.tsx` | D | 840 px | RD-019 |
| 17 | `#/students` | Students | `pages/StudentsPage.tsx` | D | 4,299 px | RD-026, RD-027 |
| 18 | `#/students/{id}` | Student profile | `pages/StudentsPage.tsx:970+` | D | 2,275 px | RD-013 |
| 19 | `#/reports` | Reports | `pages/ReportPage.tsx` | D | 840 px | RD-028 |
| 20 | `#/reports/{c}/{w}` | Report preview | `pages/ReportPage.tsx:350+` | D | **11,514 px** | RD-028, RD-015 |
| 21 | `#/storage` | Storage | `pages/StoragePage.tsx` | D | 2,679 px | RD-032 |
| 22 | `#/usage` | Internet Usage | `pages/UsagePage.tsx` | D | 1,568 px | RD-024 |
| 23 | `#/privacy` | Privacy & Data | `pages/PrivacyPage.tsx` | D | 844 px | RD-022 |
| 24 | `#/settings` | Settings | `pages/SettingsPage.tsx` | D | **4,357 px** | RD-024, RD-031 |
| 25 | `#/diagnostics` | Diagnostics | `pages/DiagnosticsPage.tsx` | D | 840 px | RD-008, RD-019 |
| 26 | `#/prompts` | Gemini Prompts | `pages/PromptsPage.tsx` | D | 840 px | RD-008, RD-035 |
| 27 | `#/audit` | Accuracy Audit | `pages/AuditPage.tsx` | D | 840 px | RD-019 |
| 28 | `#/distributed` | Multi-School (WIP) | `distributed/DistributedPage.tsx` | D | 840 px | RD-008 |
| 29 | `#/updates` | Application Updates | `distributed/UpdatesPage.tsx` | D | 840 px | RD-008 |
| 30 | `#/corrections` | Queue a correction (legacy) | `pages/CorrectionsPage.tsx` | D | 943 px | RD-014 |

Aliases: `#/report → #/reports`, `#/keys → #/ai`, `#/workers → #/ai`, `#/resultsPage → #/results`
(all verified in `App.tsx:214-228`). Non-route surfaces: sign-in, setup wizard, attention drawer,
reconnect banner (below).

---

## 1. `#/dashboard` — Dashboard

**Purpose:** daily overview: what needs marking, what needs attention, throughput, system health.
**Composition:** hand-built header (`<h1>Dashboard</h1>` + 3 actions) → hero band ("Corrections
completed today 0", with Pending/Processing/Completed 199/Failed) → two-column grid: *Marking
queue* (6 assignment rows, progress + badges + due date) | *What needs my attention?* (9 items)
→ *Marking throughput* line chart | *Pipeline right now* donut → *Live job activity* | *System
health* (Classroom + key stats, two usage gauges) → *Class performance* bar chart.
**Components:** `Button`, `Badge`, `Progress`, `Empty`, `Stat`, `LineChart`, `Donut`, `BarChart`,
`Gauge`, `AttentionPanel`, SSE + 3 pollers.
**Observed:** 7 cards, 3 hidden chart tables, 42 buttons, 2,640 px scroll.
**Problems:** duplicate title (topbar + h1) and no `PageHeader` (RD-003/RD-013); attention feed
prints raw provider payloads `PERMISSION_DENIED {'error': {'code': 403…}}` and
`DEADLINE_EXCEEDED … 504` (RD-018); "0 completed today" sits beside "199 Completed" with no scope
label (RD-029); every queue row reads "0/78 checked" beside "1 to mark", mixing roster size with
submission count (RD-026 family).

## 2. `#/ai` — AI (keys, workers, network)

**Purpose:** API key pool, the two worker configurations, and the network this install uses.
**Composition:** `PageHeader("AI")` → `Tabs` (API & providers / Normal PDF worker / Large PDF
worker / Network) → the selected panel. Tab 1 renders **`KeysPage`**, which renders its **own**
`PageHeader("API Keys")` + 4 stat cards (Total 12 / Healthy 12 / Cooling down 0 / Invalid 0) + a
key-health table (12 rows: KEY_n, status pill, req/ok/fail/429s, last used, cooldown, Disable /
Test / Remove).
**Observed:** 79 buttons on one screen, two `<h1>`s, one table, 4 tabs.
**Problems:** nested page header (RD-012); `KEY_8` appears twice in the key column with different
counters — labels are not unique identifiers (noted, data-level); the stat row and the table
disagree in tone (12 healthy beside 140/189 failures per key) — the reader cannot tell what
"healthy" means (RD-025 family, semantics).

## 3. `#/tasks` — Task Manager

**Purpose:** every correction, batched document, Classroom workflow and sync the backend records.
**Composition:** `PageHeader` → 5 stat tiles (active workers cap 6, queue, batched docs, in
flight, tiny agents) → filter row (All types 50 / Correction 50 / any state / Running 1 /
Completed 47 / Cancelled 2) → task rows with Cancel / Open result.
**Observed:** 1 card, **0 `<table>` elements** (custom rows), 83 buttons, 1,114 px.
**Problems:** the densest operational surface is built from `div`s rather than a table, so
columns do not align across rows and there is no sort (RD-027); 83 buttons on one screen with
equal weight (RD-032).

## 4. `#/correct` — Correct a PDF

**Purpose:** the primary workflow — upload a submission + mark scheme, inspect, queue.
**Composition:** a `Section` card whose `<h3>` **is the page title** → 3-column form (Student
name / Student submission / Official mark scheme) using **native `input[type=file]`** → "Advanced
options" disclosure (pipeline radio) → primary "Inspect and correct" (disabled until both files)
→ `JobQueue` card ("Job log · live via SSE", chips All 50 / Completed 47 / Cancelled 2).
**Observed:** **0 `<h1>`**, 4 inputs, 33 buttons, 975 px.
**Problems:** missing page heading (RD-002); native "Choose file" control renders as a light OS
widget inside the dark UI and there is no drop zone, no per-file progress, no cancel, no remove
button (RD-016, RD-017); file selection is the only validation — a wrong file is discovered after
the POST (RD-016); the job log below the form is a second page's worth of content on the same
screen.

## 5. `#/corrections` — Queue a correction (legacy)

Kept for old links; states so in copy ("The everyday correction flow is AI → Correct; this older
form is kept for existing links"). Same form + job log. **Problem:** two upload screens with
different headers and different copy for the same task (RD-014) — a canonical-hash redirect
would remove the duplicate.

## 6. `#/assignments` — Assignments

**Purpose:** every assignment, its submissions, Classroom state.
**Composition:** `PageHeader` + Sync Classroom / Refresh / Create Assignment → filter chips (All /
Managed by Math Checker / External Classroom / Local only) → table with an "Open" action per row.
**Observed:** 1,631 px, 32 buttons, 106 inline styles (most of any file).
**Problems:** filter chips are hand-built (the shared `FilterBar` is used once in the codebase)
(RD-032); status vocabulary mixes "Managed / External / Local" (ownership) with "Corrected /
Needs review" (progress) in adjacent controls (RD-025 family).

## 7. `#/assignments/{id}` — Assignment detail

`PageHeader` with the assignment title, breadcrumb "Assignments / Assignment detail", 5 stat
tiles, submissions table, actions (Update Classroom Assignment, Submissions, Publish to
Classroom, Correct all submitted). **Observed:** 1,034 px, 2 rows. **Problem:** "Correct all
submitted" is a destructive-scope bulk action rendered as an ordinary button with no summary of
what will be queued (RD-022).

## 8. `#/results` — Results

**Purpose:** every completed correction, searchable and filterable.
**Composition:** `PageHeader` → search field + segmented All / Corrected / Needs review /
Overridden + "All classes" + "All assignments" dropdowns → table (Student, Assignment, Class,
Score, %, Status, Classroom, Updated) → "Showing 50 of 169 correction(s)" → "Load 50 more" →
per-row "Open".
**Observed:** 3,083 px, 50 rows, 82 buttons, table has no horizontal scroll (1,128 px wide).
**Problems:** Assignment, Class and Classroom all render the same truncated string
("Ch2L1 - Constructing Algebra…", "WBIS - O Level Mathematics …") — three columns, one fact
(RD-025); no column sorting; load-more has no position indicator or total-page model (RD-027);
truncation has no tooltip (RD-030).

## 9. `#/results/{id}` — Correction detail (the marking workspace)

**Purpose:** judge one result question by question, with the student's work and the mark scheme.
**Composition:** breadcrumb "Results / Correction detail" → `<h1>Correction detail</h1>` →
`WorkspacePanes` (3 panes, widths **260 / 380 / 456 px** of a 1,128 px workspace):
1. **Questions** — student name, unit, "Verified" badge, questions grouped by topic with per-question
   marks (`Q1 3/3`, `Q3 0/3`).
2. **Student work** — a large empty state: *"The original work is not stored on this device"*.
3. **Marking result** — name, unit, model + key, `VALID` / `mark scheme: cached blueprint` /
   `Confidence: high`, **44/49 · 89.80% · provisional band 8**, Prev/Next, Export, confidence
   reasons disclosure, Classroom delivery checklist.
Below the panes: remark, question groups, overview, question breakdown, detailed correction,
recovery (each an `H3`).
**Observed:** 1 `<h1>`, `<h2>` for the right pane only, 7 `<h3>` in a flat sequence; no overflow.
**Problems:** the centre pane — the reason the workspace exists — is empty by default and offers
no action (RD-020); headings do not follow the visual structure (RD-007); the left pane's question
rows do not surface triage (0-mark and flagged questions look like the rest); Prev/Next has no
"12 of 50" position; Export sits inside the summary rather than the page frame.

At 390 px the workspace becomes **three tabs** (Questions / Student work / Marking result) — the
correct responsive strategy, and worth keeping (RD-040 notes the 899 px JS breakpoint that drives
it).

## 10. `#/review` — Review Queue

**Purpose:** results the engine flagged for a human decision.
**Composition:** `PageHeader` + Refresh + "5 awaiting review" badge → table (Student, Assignment,
Mark, Confidence, Reason, Flagged, Action) → Accept / Override per row → override modal (mark +
note + permanence notice).
**Observed:** 840 px (fits), 5 rows.
**Problems:** no sorting or filtering at 5 items and no bulk accept at 50 (RD-021); "Accept" is
optimistic with a toast and no undo (RD-021/RD-022); confidence is a badge while the *reason* is
a bulleted list in a table cell — the most decision-relevant content is the hardest to scan.

## 11. `#/scheme-review` — Mark-Scheme Review

**Purpose:** disagreements between the supplied mark scheme and the engine's independent check.
**Composition:** `PageHeader` + Refresh + "Open discrepancies 2" → table (Assignment, Question,
Type, State, Awarded, Updated) → row opens a **drawer** ("Q11 — evidence", "Proposed bulk
correction") with 5 decision buttons: *Confirm mark scheme is incorrect*, *Accept the student's
alternative answer*, *Award partial credit*, *Confirm the mark scheme is correct*, *Request
further investigation*.
**Observed:** hash does not change when the drawer opens; 2 rows; **two icon-only row buttons
have no accessible name** (RD-001).
**Problems:** five actions of equal visual weight for mutually exclusive decisions (RD-022 family);
icon-only controls (RD-001); detail is a drawer rather than a route, so `#/scheme-review/{id}`
exists but is never generated (RD-014).

## 12. `#/scheme-review/{id}` — Discrepancy detail — **UNVERIFIED**

Source supports `discrepancyId` and renders the same page with that item selected
(`App.tsx:560-570`, `SchemeReviewPage.tsx:139`). No id is exposed in the UI, so the deep link
could not be exercised without fabricating one. **Verify before implementing:** open
`#/scheme-review/<real id>` and confirm the drawer or selection state appears.

## 13. `#/largepdf` — Large PDF Worker

`PageHeader` + 4 stat tiles (Queued / Processing / Awaiting review / Completed) + "Add a long
document" card (same native file input) + empty state when idle. **Problems:** it duplicates the
job-monitoring vocabulary of Task Manager and the upload affordance of Correct a PDF, with no
cross-link explaining which to use (RD-011 family).

## 14. `#/automation` — Automatic Correction

**Purpose:** unattended pipeline; per-student live progress.
**Composition:** `PageHeader` + status actions (Review mode ON, Disable automation, Check now,
Turn OFF) → "Live pipeline" visualiser (source → spine → student cards) → "Live students" →
"System activity" → "Polling interval" / "Assignments monitored" → "Recent jobs (2 need
attention)" → "Bulk Upload Grades & Reports" → "Recent activity".
**Observed:** **24,493 px tall**, 9 cards, 3 tables, 44 buttons — the longest page in the app by an
order of magnitude. **Problem:** four different jobs (configure, watch, triage, bulk-operate) on one
scroll; reaching "Recent jobs" requires ~20,000 px of scrolling (RD-023).

## 15. `#/classroom` — Google Classroom

`PageHeader` + connection card (state, last sync, Sync / Check permissions / Disconnect) + a
numbered stepper "1 · CLASS Choose a class… / 2 · ASSIGNMENT Choose an assignment…".
**Observed:** 840 px, 3 inputs. **Problem:** during a backend restart this page showed a bare
"Failed to fetch" and no retry (RD-019).

## 16. `#/classroom/{c}/{w}` — Classroom tracking

"Tracking · 78 students", permission banner, table of 78 students (4,562 px), actions (Allow for
correction, Correct Entire Assignment, Sync assignment, Reload, Diagnostics, Export).
**Problem:** 78 rows with no filter/search in the table header — the filter chips are elsewhere
(RD-027).

## 17. `#/classes` — Classes

`PageHeader` + Refresh / Sync + class cards (`TESTING`, 12 students, 2 assignments, "Submitted
0/2", "1 outstanding", "no results", "Last synced 3d ago"). Renders correctly; the "no results"
line is plain text inside a card rather than an empty state (RD-019 family).

## 18. `#/students` — Students

`PageHeader` + search + "Select students" + table (Student, Nickname, Class, Submissions,
Corrections, Score, Pending) with **80 rows** and 4,299 px of scroll.
**Problems:** Score shows *raw fractions with different denominators* — `428/508`, `169/255`,
`—` — so the column cannot be compared or ranked (RD-026); no sort (RD-027); 80 rows in one page.

## 19. `#/students/{id}` — Student profile

Back link, "Edit student information", `<h1>` = student name, filter chips (All / Submitted /
Outstanding / Late / Corrected), then Student Information, Current status, Performance, At a
glance, Recent activity, Recent corrections — 4 tables, 28 rows, 2,275 px.
**Problem:** six independent cards with no summary hierarchy; the same numbers appear in
"Performance" and "Recent corrections" (RD-013 family).

## 20. `#/reports` — Reports

`PageHeader` + "Build, preview and export official reports" + builder (report type, class,
assignments) + preview + Generate preview / Export All / Export PDF.
**Problem:** report *configuration*, *preview* and *export* share one scroll with no step model
(RD-028).

## 21. `#/reports/{c}/{w}` — Report preview

Deep link renders the builder plus a full class report: **11 tables, 199 rows, 11,514 px**.
**Problems:** duplicate action sets (two "Generate preview", two "Export PDF"); **no breadcrumb**
while every other drill-down has one (RD-015); preview and controls are interleaved.

## 22. `#/storage` — Storage

Storage status (app v5.2.1 · schema 5), Export data (`mathchecker.transfer v1`, button labelled
with a timestamp), Import data (file + "Preview import"), Stored corrections (169 total, 25
shown). 2,679 px. **Problem:** "Export" is a primary-looking button whose label changes length
constantly (timestamp), shifting layout (RD-031 family).

## 23. `#/usage` — Internet Usage

Limits card (0 = unlimited + Save limits), API requests & retries, Today by category, Last 24
hours chart. 1,568 px. **Problem:** the page's primary control ("Save limits") is the only button
in the main area and sits at the top of a section about *limits*, while the charts below are
read-only context (RD-024 family).

## 24. `#/privacy` — Privacy & Data

Three cards: Your local data / Factory Reset / About, plus Export Diagnostics and "Factory
Reset…". 844 px. **Good:** the destructive action is suffixed with an ellipsis and confirmed.
**Problem:** this is the only page in the app that follows that convention (RD-022).

## 25. `#/settings` — Settings

`PageHeader` + **17 sections / 17 cards** on one scroll: Public Address, Host account,
Multi-Account Mode, Remote Access, Connection, Background workers, Student file retention, Grade
boundaries, Offline demo, Testing only, Server lifecycle, Gemini model chain, Data portability.
**Observed:** 4,357 px, 26 inputs, 4 buttons, 17 cards.
**Problems:** no section index or sub-navigation; high-risk controls (Host account, Multi-Account,
Testing mode) sit inline with routine ones (RD-024); 22 unlabelled-looking inputs rely on the
fallback CSS rule (RD-032).

## 26. `#/diagnostics` — Diagnostics

Developer-only. For a teacher: *"Diagnostics are available in Developer role only."* — bare text,
no heading, no card, no link back (RD-008). Developer view: System / Gemini / Classroom / jobs /
storage / logs with Refresh and Export.
**Also observed during the backend restart:** permanent "Loading diagnostics…" with no timeout
(RD-019).

## 27. `#/prompts` — Gemini Prompts

Developer-only. For a teacher: the bare string **"This page is Developer-role only."** — no page
title, no card, nothing else on screen (RD-008). Developer view uses `var(--bg)` and `var(--line)`,
both **undefined**, so its container has no background and no border (RD-035). During the backend
restart this page rendered the raw string **"Failed to fetch"** in place of the gate (RD-018).

## 28. `#/audit` — Accuracy Audit

`PageHeader("Accuracy Audit", "Testing-only record")` + empty state: "No audit recorded yet — Run
`scripts/audit_inventory.py`, `scripts/audit_run.py` and `scripts/audit_findings.py` to produce
one." **Good:** a model empty state that tells you how to produce data. Keep this pattern.

## 29. `#/distributed` — Multi-School (WIP)

Non-developer: an `<h2>` card "Multi-School — Work in Progress" explaining that cross-school use
has not completed release verification. Developer: full console (accounts, schools, classes,
sync) in `distributed/*`. **Problem:** the WIP card has no navigation and mismatched heading level
(RD-008 family).

## 30. `#/updates` — Application Updates

`PageHeader` + "Sign in required" card + "Go to Multi-School sign-in". Developer: release list,
publish controls. **Problem:** the page's only action leaves the app for a different sign-in with
no explanation of how to come back (RD-008 family).

---

## Non-route surfaces

| Surface | Source | Inspection | Notes |
|---|---|---|---|
| **Sign-in** | `SignIn.tsx` | S | Brand says **"Math Correct"** here and "Math Checker" everywhere else (RD-039). Show/hide password button uses `btn ghost size-sm` — `size-sm` does not exist in CSS, so it is not compact (RD-038). No password recovery exists. |
| **First-run setup wizard** | `pages/SetupWizard.tsx` | S | 6 steps, all skippable, step buttons allow jumping; no validation on "Next". Uses `var(--line)`, undefined (RD-035). |
| **Attention drawer** | `attention.tsx` + `Drawer` | D (inline panel) | Same content as the dashboard panel; inherits RD-018. |
| **Reconnect banner** | `lifecycle.tsx:150-175` | D | Good: explains what happened and that queued work resumes. **Contrast:** `--warn` on `--warn-soft` = 4.29:1 in light (RD-004 family). |

## Screens that do **not** exist (do not invent them)

No marketing/landing page · no registration or password-reset flow · no pricing/billing · no
help centre or docs page · no 404 page (unknown hashes silently fall back to `#/ai`) · no
notification centre · no per-user profile page beyond the account dropdown. These are absences,
not omissions in this audit.

## Screens that exist but are unreachable by navigation

`#/corrections` (legacy), `#/scheme-review/{id}` (drawer used instead), `#/reports/{c}/{w}` and
`#/classroom/{c}/{w}` (reached from tables), `#/students/{id}`, `#/assignments/{id}`,
`#/results/{id}`, `#/audit`, `#/diagnostics`, `#/prompts`, `#/updates` (role-gated). The redesign
must keep every one of them addressable (constraint C1).
