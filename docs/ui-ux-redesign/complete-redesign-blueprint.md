# Complete Redesign Blueprint

The new interface for **every screen**. This is the specification an implementation agent follows;
it is not a mood board. Component behaviour is defined in
[component-and-interaction-system.md](component-and-interaction-system.md), tokens in
[design-system-specification.md](design-system-specification.md), and each page's issues in
[redesign-findings-register.md](redesign-findings-register.md).

Design direction: **Chalk & Ink** (`design-directions.md §6`) — Desk and Ledger registers.

---

## 1. The shell

### 1.1 Structure

```
┌────────┬─────────────────────────────────────────────────────────────────────┐
│  rail  │  header: breadcrumb/title · search (⌘K) · jobs · theme · account    │
│ 72/236 │─────────────────────────────────────────────────────────────────────│
│        │  section tabs (only where a destination owns >1 page)               │
│ Home   │─────────────────────────────────────────────────────────────────────│
│ Correct│                                                                     │
│ Review │  page content                                                       │
│ Results│                                                                     │
│ Classes│                                                                     │
│ System │                                                                     │
└────────┴─────────────────────────────────────────────────────────────────────┘
```

* **Rail** width 236 px expanded, 72 px collapsed (icons + 11 px labels). State persists, but the
  collapsed state is per-device and keyboard-toggleable (`[`), because today only a mouse click on
  a sidebar button can change it.
* **Six destinations**, printed in full in §1.2. The rail never scrolls at ≥ 600 px height: six
  items plus the account block fit in 420 px.
* **Header** is 56 px: page title (one line, 18 px/600), breadcrumb replacing the title on detail
  views, a 280–360 px search field that collapses to an icon below 1024 px, a **jobs pill**
  (`Idle` / `3 correcting`) that opens the jobs drawer, theme control, account menu.
  Today's header adds a second sub-line ("synced 4m ago") and two status chips; those move into
  the Home page's status strip and the jobs drawer, because they are not page-level facts.
* **Section tabs** (28 px tall, underlined, not pills) appear only inside a destination with
  multiple pages — e.g. *Correct · Tasks · Large PDF · Automation*, *Results · Reports*,
  *Classroom · Classes · Students · Assignments*. The active tab is `aria-current="page"`.
* **No nested page headers.** One `<h1>` per screen, always rendered by the page frame, never by a
  tab panel's child page (RD-003, RD-012).

### 1.2 Information architecture

| Destination | Default page | Children (section tabs) | Old routes mapped |
|---|---|---|---|
| **Home** | `#/home` | — | `#/dashboard` → `#/home` |
| **Correct** | `#/correct` | Correct · Tasks · Large PDF · Automation | `#/correct`, `#/corrections`, `#/tasks`, `#/largepdf`, `#/automation` |
| **Review** | `#/review` | Review queue · Mark-scheme review | `#/review`, `#/scheme-review` |
| **Results** | `#/results` | Results · Reports · Usage | `#/results`, `#/results/{id}`, `#/reports`, `#/reports/{c}/{w}`, `#/usage` |
| **Classes** | `#/classroom` | Classroom · Classes · Students · Assignments | `#/classroom`, `#/classes`, `#/students`, `#/assignments` (+ details) |
| **System** | `#/settings` | Settings · AI & keys · Storage · Privacy · Diagnostics | `#/settings`, `#/ai`, `#/storage`, `#/privacy`, `#/diagnostics`, `#/prompts`, `#/audit`, `#/updates`, `#/distributed` |

Every legacy hash must **redirect** (replace, not push) to its canonical equivalent, so one page
has one URL and the back button never lands on a duplicate (RD-014). Aliases keep working:
`report → #/reports`, `keys → #/ai`, `workers → #/ai`, `resultsPage → #/results`.

### 1.3 Search and command palette

`⌘K` / `Ctrl K` opens one palette that covers: pages ("review queue"), students, assignments,
courses, and actions ("retry failed jobs", "add API key"). Results are grouped, keyboard-navigable,
and announce the active item with `aria-activedescendant`. The header search field is the same
palette with an input, not a second search implementation (today `SearchBox.tsx` has its own
document click/Escape handling and its own result list).

### 1.4 Page frame

Every page renders:

```
<Page>
  <PageHeader title breadcrumb actions meta? />
  <PageBody>…</PageBody>
</Page>
```

* `title` is the only `<h1>`; `actions` holds at most **three** controls — one primary, the rest
  ghost/secondary; anything beyond three goes into an overflow menu.
* `meta` is a single line of subdued facts (e.g. "5 awaiting review · synced 4m ago"), never a
  badge cluster.
* Destructive actions never appear in the header; they live in the page's danger section or an
  overflow menu, always behind `ConfirmDialog` (RD-022).

### 1.5 The Desk / Ledger switch

A page declares its register (`<Page register="desk">` or `"ledger"`). It changes only spacing,
row height, and default type family — not tokens, colour or navigation. No page mixes registers
inside one card.

---

## 2. Home

### 2.1 `#/home` — Home

**Purpose:** answer three questions in five seconds — *what needs me, what is running, how is this
class doing?*

**Layout (Ledger):**
1. **Greeting + needs-you line** (not a hero): "Good afternoon. **5 results need you** · 2 failed
   jobs." The needs-you phrase is the only link-styled text and jumps to Review.
2. **Needs you** (left, 7/12): a ranked list of at most 5 items — review items grouped by
   assignment ("Ch2L1 — 3 results flagged"), failed jobs grouped by *cause* ("2 submissions had no
   Drive attachment"), mark-scheme disputes. Each row: one plain sentence, count, age, single
   action. **No raw provider payloads** (RD-018).
3. **Today** (right, 5/12): three numbers — marked, queued, failed — each with its scope label, and
   a 30-day sparkline. The old "0 completed today / 199 Completed" pair disappears (RD-029).
4. **Next up**: the assignment furthest behind, with `n to mark` and a primary "Open queue".
5. **Classes** strip: up to 4 class cards with `checked / total` and a thin progress rule.

**States:** first-run (no classes) → "Connect Google Classroom or upload a paper directly" with
two equal actions; no work today → a single calm line, not an empty illustration; failed fetch →
`ErrorState` with cause + Retry (RD-019).

**Responsive:** < 1024 px the two columns stack in the order Needs you → Today → Next up → Classes.
< 768 px the greeting and needs-you line merge into one card.

**Preserve:** review count, processing/pending/completed/failed, classroom state, key health,
usage gauges (moved to System → Usage), class averages (moved to the Classes strip's detail).

**Files:** `pages/DashboardPage.tsx` (replaced), chart components kept.

---

## 3. Correct

### 3.1 `#/correct` — Correct a PDF

**Purpose:** get two files in, one decision made, one job queued — with visible progress.

**Layout (Desk):** two-step frame, not a 3-column form (GOV.UK "one thing per page", source #26):

* **Step 1 — Files.** Two large drop zones side by side (stacked < 768 px):
  *Submission* accepts `.pdf .png .jpg .jpeg .webp`; *Mark scheme* accepts `.pdf`.
  Each zone: dashed `--rule-strong` border, 96 px tall, icon + "Drop the student's paper here" +
  "or **Choose file**" button + format/size hint. On drop/choose it becomes a **FileCard**: file
  name (truncated middle), size, page count once inspected, `Replace` and `Remove` buttons.
  Wrong type or > limit → inline error under that zone, with the accepted formats and one action
  (RD-016, RD-017).
* **Step 2 — Details.** Student name (prefilled from the filename, editable, helper: "used on the
  report"), pipeline (Automatic · Single-pass · Batched, with the applied rule explained),
  then the primary **"Inspect and correct"**.
* **Upload progress** replaces the primary button in place: a determinate bar with
  `Uploading 34% · 12.4 / 36.1 MB`, then `Inspecting 30 pages…`, then `Queued · job 1234`. A
  `Cancel` button is present until the job is accepted by the backend (RD-016).
* **Result disclosure** below (existing behaviour): applied pipeline, page count, batches, reason,
  and a link to the job.

**Right rail (≥ 1280 px):** the live queue — 5 most recent jobs with status, stage and elapsed;
"View all" → Tasks. Below 1280 px it becomes a "Queue (3)" disclosure under the form.

**States:** no keys configured → banner at top with "Add an API key" (and the form disabled with a
reason, not a silent disabled button); batched pipeline disabled → inline notice only if a large
document is selected (today it is always shown — RD-016 family); job failed → `ErrorState` in the
job row with the plain-language cause.

**Preserve:** both inputs, name fallback, pipeline override + threshold, applied-pipeline naming,
`JobQueue`, the "grade from configured boundaries" note.

**Files:** `pages/PdfCorrectionPage.tsx` (rewritten), new `FileDrop`, `FileCard`, `UploadProgress`.

### 3.2 `#/corrections` — legacy upload

**Removed as a screen.** The hash redirects to `#/correct` (RD-014). Nothing is lost: the legacy
form posts to the same endpoint with the same fields.

### 3.3 `#/tasks` — Tasks

**Layout (Ledger):** pinned summary strip (`Running 1 · Queued 0 · Failed 2 · Done 47`) → filter
bar (`Type`, `State`, `Search`) → **a real table**: Job · Student · Assignment · Type · State ·
Progress · Attempts · Started · Duration · Actions. Frozen first column, sticky header, 32 px rows,
sortable headers with `aria-sort`, keyboard row activation.

Row actions: `Open`, `Cancel` (with confirm), `Retry` (failed only). Bulk select for cancel/retry.
Progress cells use a 96 px bar plus `3/5` text, never colour alone.

**States:** empty → "No jobs yet. Correct a paper to see it here."; filtered-empty → "No jobs match
these filters" + Clear filters.

**Preserve:** all job fields, cancel, open result, filters, live SSE updates.

**Files:** `pages/TaskManagerPage.tsx` (rewritten around `DataTable`).

### 3.4 `#/largepdf` — Large PDF

Keep as a tab, but re-framed as **"Batched documents"**: a table of batched jobs (Document ·
Pages · Batches · State · Progress · Actions) with a primary "Add a long document" that opens the
same two-zone drop dialog as Correct. The four stat tiles become the summary strip.

**Preserve:** queue/processing/awaiting/completed counts, per-batch retry/cancel, job detail,
open result.

### 3.5 `#/automation` — Automation

**Problem being fixed:** 24,493 px of mixed configure/watch/triage/bulk (RD-023).

**Layout (Ledger):** a settings band + three sections on **separate section tabs** inside the page
(*Overview · Students · Jobs · Bulk actions*), with the run state always pinned:

* **Run strip (always visible, sticky):** `Automation ON · Review mode ON · polling 60 s ·
  3 assignments monitored` + `Pause` / `Check now`.
* **Overview:** the pipeline visualiser, but capped at the last 8 events and scrollable in a
  420 px frame; counts by state; last error.
* **Students:** the live student cards as a **table** (Student · Detected · Corrected · Hold ·
  Delivered · Last update) with a filter.
* **Jobs:** recent jobs with the same table as Tasks (shared component).
* **Bulk actions:** "Bulk upload grades & reports" isolated on its own tab with an explicit
  confirmation and a preview of affected students (no accidental bulk publish).

**Preserve:** every automation setting, review mode, holds, delivery states, bulk upload.

---

## 4. Review

### 4.1 `#/review` — Review queue

**Layout (Ledger):** summary (`5 awaiting · oldest 1d`) → toolbar (Search · Confidence filter ·
Assignment filter · Sort: oldest/confidence/score change) → table:
Student · Assignment · Mark · Confidence · Reason (one-line summary + "3 reasons" disclosure) ·
Flagged · Action.

* Row expands **in place** (not a modal) to show all reasons, the question list with marks, and a
  link to the full correction.
* Actions: `Accept` (primary, one click), `Set mark` (opens a compact popover with number input +
  note + permanence notice), `Open` (detail).
* Bulk: select-all → "Accept 4 selected" with a confirmation listing count. Every accept shows an
  **Undo** toast for 8 s; the optimistic removal is reversed on failure with the row restored
  (RD-021/RD-022).

**Preserve:** accept, override, note, permanence, automation hold routing, refresh.

### 4.2 `#/scheme-review` — Mark-scheme review

**Layout (Desk):** list of open discrepancies (Assignment · Question · Type · State · Awarded ·
Updated) + a **detail pane** on the right at ≥ 1280 px, or a route at `#/scheme-review/{id}` below
that (which also fixes RD-001/RD-014: the row control becomes a labelled `Open` button).

**Detail:** evidence (the engine's reasoning, the scheme's claim, the student's answer) then **one
primary decision** with three secondary options under "Other decisions", replacing five equal
buttons:
* Primary: `Confirm the mark scheme is incorrect` (with the proposed correction preview).
* Secondary: `Accept the student's alternative`, `Award partial credit`, `Confirm the scheme is
  correct`, `Request further investigation`.
Destructive/irreversible choices use `ConfirmDialog` naming the affected results.

**Preserve:** all five decisions, evidence, proposed bulk correction, approval requirement.

### 4.3 `#/scheme-review/{id}` — Discrepancy detail

A real route: same detail pane as §4.2 with the item selected, breadcrumb "Review / Discrepancy",
and a working deep link. **UNVERIFIED today** — confirm the existing deep link renders before
implementing.

---

## 5. Results

### 5.1 `#/results` — Results

**Layout (Ledger):** toolbar (search · status segmented All/Corrected/Review/Overridden · class ·
assignment · **Columns** chooser · Export) → table:

| Column | Change |
|---|---|
| Student | frozen, name + class as a subdued second line |
| Assignment | full name on hover / two-line wrap at ≥ 1280 px, not mid-word ellipsis |
| Score | `44/49` with a 3 px inline micro-bar and `89.8%` as the primary figure |
| Status | text + icon + tone |
| Classroom | `Published` / `—` with a delivery icon |
| Updated | relative + absolute on hover |

**Removed:** the separate `Class` column (identical to the assignment's class — RD-025) and the
duplicate `%` column. Columns become user-configurable with a saved preference.

**Pagination:** explicit `1–50 of 169` with Prev/Next and page size (50/100), not just "Load 50
more" (RD-027). Row click opens the correction; `Open` moves to a trailing icon button with an
accessible name.

**Preserve:** search, filters, export, open detail, counts.

### 5.2 `#/results/{id}` — Correction detail (marking workspace)

**This is the most important screen in the product.** Today's three panes are right; their
proportions, states and chrome are not (RD-020, RD-007).

**Layout (Desk), ≥ 1024 px:**

```
┌──────────────────────────────────────────────────────────────────────────────┐
│ ← Results   Amin · Ch2L1               44/49 · 89.8% · band 8   [Export ▾]   │
├──────────┬────────────────────────────────────┬──────────────────────────────┤
│ MARGIN   │  ANSWER                            │  MARK                        │
│ Q1  3/3  │  ┌──────────────────────────────┐  │  Q7  ·  2 / 3                │
│ Q2  3/3  │  │  rendered PDF page / image   │  │  Scheme: "2 marks for …"     │
│ Q3  0/3  │  │  (zoom, fit, page x/y)       │  │  AI: 2 · verified ✓          │
│ Q4  2/3  │  └──────────────────────────────┘  │  [ 0 ][ 1 ][ 2 ][ 3 ]        │
│ …        │  ── or, when not retained ──       │  Note (optional)             │
│ 12 of 28 │  "Original not stored."            │  [ Save & next ]             │
│ checked  │  Extracted answer text + link to   │  Confidence: high            │
│          │  the Classroom submission          │  Why? ▸                      │
├──────────┴────────────────────────────────────┴──────────────────────────────┤
│ LEDGER  44 / 49 · 89.8% · band 8 · 12 of 28 checked · 3 flagged  [Prev][Next]│
└──────────────────────────────────────────────────────────────────────────────┘
```

* **Margin column (56 px)** — question numbers with marks as typographic glyphs (`3/3`, `0/3`,
  `½`), grouped by topic with a sticky topic label. Zero-mark and flagged questions carry a 2 px
  left rule in mark red and an icon; triage is visible without opening anything.
* **Answer pane** — the document when it exists: slim toolbar (page x/y, zoom −/+, fit, open
  original, download), page canvas on `--surface-sunken`, keyboard `+/−` and arrow paging.
  **When it does not exist** (the default retention), the pane becomes a real state: one sentence,
  a **"Show extracted answer"** disclosure (the corrected text already exists in the result), a
  "Open in Classroom" link when a Drive file is known, and a one-line link to System → Privacy to
  change retention for future papers. It never occupies 34% of the screen with an icon and a
  sentence (RD-020).
* **Mark pane** — the selected question: scheme excerpt, AI mark, verification state, a **mark
  control** (segmented 0…max with the current value), an optional note, `Save & next`, and a
  collapsed "Why this mark" disclosure with the confidence reasons.
* **Ledger strip** — fixed at the bottom of the workspace: total, percentage, band, progress
  ("12 of 28 checked"), flagged count, Prev/Next. It is the only place totals live, so they never
  scroll away.

**Keyboard:** `J/K` next/previous question, `0–9` set mark, `Enter` save & next, `Z` undo last
mark, `⌘K` palette, `?` shortcut sheet.

**Below the workspace:** remark, double-check differences, versions, recovery — as **collapsible
sections**, not a second page of H3s (RD-007). Headings become `h2` for the workspace and `h2` per
section, with `h3` inside.

**Mobile (< 1024 px):** tabs *Questions · Answer · Mark* (keep the current strategy), with the
ledger strip pinned above the tab bar. Marks set on the Mark tab return to Questions with the row
updated.

**Preserve:** per-question mark edit, subquestions, unmarked subquestions, remark, versions and
restore, double-check diffs, provenance, confidence reasons, export, prev/next across results,
Classroom delivery checklist, "original not retained" honesty.

**Files:** `pages/ResultsPage.tsx` split into `workspace/QuestionMargin.tsx`,
`workspace/AnswerPane.tsx`, `workspace/MarkPane.tsx`, `workspace/LedgerStrip.tsx`,
`workspace/DetailSections.tsx`.

### 5.3 `#/reports` — Reports

**Layout (Desk → two columns at ≥ 1280 px):** left 360 px = **builder** (report type, class,
assignments, date range, options) with a sticky `Generate preview`; right = preview canvas with
its own toolbar (`Export PDF`, `Export CSV`, `Print`).

The builder and the preview never share a scroll (RD-028). Preview is paginated by section, not
11,514 px of nested tables; each section has an anchor index.

**Preserve:** all report types (incl. missing homework), class/assignment selection, preview, PDF
and CSV export, print stylesheet.

### 5.4 `#/reports/{courseId}/{workId}` — Report preview

The deep link opens the builder pre-filled **and** the preview rendered, with a breadcrumb
"Results / Reports / Ch2L1" (RD-015). One action set only: the preview toolbar owns export.

### 5.5 `#/usage` — Internet usage

**Layout (Ledger):** limits card (hourly/daily, `0 = unlimited`, `Save` disabled until changed),
then totals (today, last 24 h, by category) as a summary strip, then one chart (MB/hour) with a
table fallback. The page's primary action is the only primary button on screen.

---

## 6. Classes

### 6.1 `#/classroom` — Google Classroom

**Layout (Ledger):** connection card (state, account, last sync, `Sync now`, `Check permissions`,
`Disconnect` behind confirm) → **assignment picker** as two labelled selects with a "Continue"
primary (today they are steps 1 and 2 with no affordance for what continues) → recent tracking
table.

**States:** disconnected → "Connect Google Classroom" primary + what access is requested;
permission error → cause + `Recheck`; sync running → determinate progress from
`/classroom/sync/progress`, not a spinner.

**Preserve:** connect/disconnect, sync, permission check, tracking table, per-assignment actions.

### 6.2 `#/classroom/{courseId}/{workId}` — Tracking

**Layout:** header (`Tracking · 78 students`, assignment, sync state, `Correct entire assignment`)
→ filter chips (All · Stale · Missing · Late · Corrected) + search → table (Student · State ·
Classroom grade · Math Checker mark · Report · Updated · Action). Sticky header, 32 px rows,
paginated at 100.

`Correct entire assignment` opens a confirmation listing the exact count and excludes already
corrected submissions.

**Preserve:** 78-row tracking, permission banner, correct-all, sync, reload, diagnostics, export.

### 6.3 `#/classes` — Classes

Grid of class cards: name, students, assignments, `checked/submitted`, a 4 px progress rule,
"no results" as a subdued line (not the string "no results" — RD-049), last sync. Card actions:
`Open students`, `Open assignments`.

### 6.4 `#/students` — Students

**Layout (Ledger):** search + class filter + sort → table: Student (frozen) · Nickname · Class ·
Submissions (`15/18` + micro-bar) · Corrections · **Score** (`89.8%` primary, `428/508` on hover)
· Pending. Score becomes a percentage with the fraction as secondary — the fix for RD-026.

Pagination 100/page; bulk selection kept (for report/export actions that already exist).

### 6.5 `#/students/{id}` — Student profile

Two-column at ≥ 1280 px: left = identity card (name, nickname, class, Classroom id, `Edit`) +
**at-a-glance** numbers (4 stats); right = tabs *Overview · Assignments · Corrections · Classroom*.
Overview shows a 12-week trend line + the 5 most recent corrections. Ends the six-stacked-cards
scroll (RD-013 family).

**Preserve:** metadata edit, assignment history, grades, corrections, Classroom ids.

### 6.6 `#/assignments` — Assignments

**Layout (Ledger):** toolbar (search · scope chips All/Managed/External/Local · `Create`) →
table: Assignment · Class · Due · Submitted · Checked · Needs review · Average · Status ·
Action. Row opens the detail. `Create assignment` opens a modal form (today it is a page-level
flow with no cancel semantics visible).

**Preserve:** filters, create, publish, sync, per-assignment stats.

### 6.7 `#/assignments/{id}` — Assignment detail

Header: title, class, due date, `Correct all submitted` (primary), overflow (`Publish to
Classroom`, `Update Classroom assignment`, `Sync`). Summary strip: students, submitted, checked,
needs review, outstanding, average. Submissions table with the same columns as tracking.

Bulk `Correct all submitted` shows a confirmation with counts and an estimate ("15 papers, ~8
min"), because it starts a long-running job (RD-022).

---

## 7. System

### 7.1 `#/settings` — Settings

**Problem being fixed:** 17 unrelated sections on one 4,357 px scroll (RD-024).

**Layout:** a **two-column settings frame** at ≥ 1024 px — left 240 px **section index** (sticky,
with anchors and `aria-current`), right = the sections. Sections and their risk levels:

| Section | Contents | Risk |
|---|---|---|
| Access | Public address, host account, multi-account, remote access | high (grouped, with explanations) |
| Processing | Background workers, model chain | medium |
| Privacy | Student file retention, data portability | high |
| Grading | Grade boundaries, session label | medium |
| Behaviour | Offline demo, testing-only mode, server lifecycle | high (danger zone styling) |
| Connection | Classroom connection, network status | low |

Every toggle states its current value in words as well as position; every high-risk control sits
in a bordered "Changes how the app behaves" block and requires `ConfirmDialog`. A sticky
`Unsaved changes` bar appears if a setting is edited but not saved (today changes apply
immediately with no undo — keep immediate-apply where the backend already does, but show a
`Saved` inline ack and an Undo where the API supports reverting).

**Preserve:** every setting and its endpoint, immediate-apply semantics, developer-only items.

### 7.2 `#/ai` — AI & keys

Keep four sections as **section tabs**, but **no nested page header**: the tab content starts with
a section heading (`h2`), not `PageHeader` (RD-012). Key table columns: Label · Status · Requests
· Success · Failures · 429s · Last used · Cooldown · Actions. `Add keys` opens a modal with a
textarea (one per line) and a per-key test result list.

`Label` becomes editable and unique — today two rows both read `KEY_8`, which makes the table
unusable as a reference.

**Preserve:** add/test/disable/remove, rotation, health, network/public address, worker config.

### 7.3 `#/storage` — Storage

Status card (app version, schema, disk used by category) → export card (`Export data`, no
timestamp in the button label — RD-031 family) → import card (file → `Preview import` → a
before/after diff table → `Apply`) → stored corrections table (paginated, not "showing 25").

Import stays a two-step preview-then-apply flow; the diff is the safety mechanism and should be
the visual centre of the card.

### 7.4 `#/privacy` — Privacy & data

Three sections as cards: *What is stored* (a table: data, where, retention, how to remove),
*Export diagnostics*, *Factory reset* (danger zone, requires typing the word). Keep the existing
"Factory Reset…" ellipsis convention and extend it to every destructive action in the app.

### 7.5 `#/diagnostics` — Diagnostics

Developer-only. Replace the bare string with a **standard gated page**: title, one sentence
("Diagnostics are available to the developer role"), what it contains, and a `Back to Home`
secondary action (RD-008). Developer view: tabs *System · Gemini · Classroom · Jobs · Storage ·
Logs*; logs get a monospace viewer with level filter, search, and copy.

### 7.6 `#/prompts` — Gemini Prompts

Developer-only; same gated page pattern. Fix the undefined `--bg`/`--line` tokens (RD-035).
Editor: file list + textarea + "diff against default" + save with confirm.

### 7.7 `#/audit` — Accuracy audit

Keep as-is conceptually: title, "Testing-only record" tag, empty state with the exact commands.
Restyle to `EmptyState` with a copyable command block. This page is the model for every empty
state in the redesign.

### 7.8 `#/updates` — Application updates

Gated page pattern; when signed out, explain *why* sign-in is needed and what happens after
("You will return here automatically"), with one `Sign in` primary — not a bare link (RD-008).

### 7.9 `#/distributed` — Multi-School (WIP)

Gated page pattern with the existing WIP explanation, a clear `Developer diagnostics` secondary,
and no dead ends. Preserve developer console behind the role check.

---

## 8. Flow surfaces

### 8.1 Sign-in

Centred 400 px card on canvas (no sidebar): brand (**Math Checker** — RD-039), one-sentence
context, username, password with a labelled show/hide **inside** the field, remember-me, primary
`Sign in`, error summary above the form with field-level messages (GOV.UK pattern, source #25).
First-run variant: create account (username, password, confirm) with a visible rule ("10+
characters") and inline match validation. Public-mode notice and the public address block stay.

**Preserve:** setup-then-login in one step, remember-me cookie semantics, CSRF handling, the
remote-no-setup explanation.

### 8.2 First-run setup wizard

Six steps → **four**: *Welcome · Your details · AI keys · Ready*. Classroom connection and
behaviour move to "Finish" as recommended-but-skippable checkboxes, because they are the two steps
most likely to block a first run. Progress is a labelled stepper (`Step 2 of 4`) with
`Back`/`Next`, per-step validation, and `Skip setup` always available. The health check runs once
on the final step and lists pass/warn/fail with fixes (existing behaviour).

**Preserve:** every setting, skippability, self-check.

### 8.3 Jobs drawer & reconnect banner

The jobs pill opens a 400 px drawer (same content as Tasks' summary, filtered to active). The
reconnect banner stays, restyled to `--warn` at ≥ 4.5:1, with `Reload now` and the existing
"nothing queued is lost" sentence.

---

## 9. Cross-page rules

1. **One `<h1>`**, always from `PageHeader`; sections use `h2`, cards `h3`. No heading skips.
2. **Every list** supports sort + explicit pagination or virtualisation; every table has a
   `caption` (visually hidden) and `scope` on headers.
3. **Every empty state** has a title, one sentence, and one action where an action exists.
4. **Every error state** has a plain-language cause, a retry, and a path to Diagnostics.
5. **Every destructive action** uses `ConfirmDialog` with the object named; every reversible
   action offers Undo in a toast.
6. **Every count** states its scope ("today", "all time", "awaiting review").
7. **Every truncation** has a `title` or an expandable row (RD-030).
8. **Nothing important lives below 1,200 px** on an operational page: summaries are sticky.

---

## Appendix A — Route → files → components → responsive → preserve

Every screen in one table, so no page can be skipped and no implementation task has to guess
which file changes. Paths are relative to `frontend/src/`.

| # | Route | Files likely to change | New / changed components | Responsive rule | Must preserve |
|---|---|---|---|---|---|
| 1 | `#/home` | `pages/DashboardPage.tsx`, `attention.tsx`, `charts/*` | `Page`, `Metric`, `MetricRow`, `StatusBadge`, `EmptyState`, `LineChart`, `StackedBar` | Needs-you → Today → Next up → Classes stacked < 1024; bottom bar < 768 | All dashboard counts, Classroom state, key health, class averages, SSE job feed |
| 2 | `#/correct` | `pages/PdfCorrectionPage.tsx`, `pages/CorrectionsPage.tsx` (`JobQueue`) | `FileDrop`, `FileCard`, `UploadProgress`, `Field`, `QueueList` | Zones stack < 768; queue rail → disclosure < 1280 | Two files, name fallback, pipeline override + threshold, applied pipeline, job queue |
| 3 | `#/correct/tasks` | `pages/TaskManagerPage.tsx` | `DataTable`, `Pagination`, `FilterBar`, `Progress`, `ConfirmDialog` | H-scroll with frozen Job column ≥ 768; cards < 768 | Cancel, retry, open result, filters, live updates |
| 4 | `#/correct/large` | `pages/LargePdfWorkerPage.tsx` | `DataTable`, `LargePdfJobDetail`, `FileDrop` | Same as Tasks | Counts, per-batch retry/cancel, merge, open result |
| 5 | `#/correct/automation` | `pages/AutomationPage.tsx`, `AutomationFlow.tsx` | `SectionTabs`, `RunStrip`, `DataTable`, `ConfirmDialog` | Tabs at all widths; student table cards < 768 | Run state, review mode, polling, monitored assignments, holds, delivery, bulk upload |
| 6 | `#/review` | `pages/ReviewPage.tsx` | `DataTable`, `FilterBar`, `Popover`, `Toast` (undo), `ConfirmDialog` | Table cards < 768 | Accept, override + note, permanence, automation hold routing |
| 7 | `#/review/scheme` | `pages/SchemeReviewPage.tsx` | `DataTable`, `SplitView`, `ConfirmDialog` | Side pane ≥ 1280; route below | Five decisions, evidence, proposed bulk correction |
| 8 | `#/review/scheme/{id}` | `pages/SchemeReviewPage.tsx`, `SchemeSourceViewer.tsx` | `Breadcrumb`, `SplitView` | Detail full width < 1280 | Deep link, evidence, decisions |
| 9 | `#/results` | `pages/ResultsPage.tsx` (list) | `DataTable`, `Pagination`, `FilterBar`, `Menu`, `InlineBar` | H-scroll + frozen Student ≥ 768; cards < 768 | Search, status/class/assignment filters, export, open |
| 10 | `#/results/{id}` | `pages/ResultsPage.tsx` → `workspace/*`, `useSort.ts`, `topicGroups.ts`, `schemeStructure.ts` | `WorkspacePanes`, `MarkGlyph`, `ScoreBlock`, `LedgerStrip`, `SectionTabs` | 3 panes ≥ 1280; 2 panes 1024–1279; tabs < 1024 | Per-question marks, subquestions, remark, versions, diffs, export, prev/next, delivery checklist |
| 11 | `#/results/reports` | `pages/ReportPage.tsx`, `report.css` | `BuilderPanel`, `ReportPreview`, `Menu`, `DataTable` | Two columns ≥ 1280; builder → preview below | All report types, selection, preview, PDF/CSV |
| 12 | `#/results/reports/{c}/{w}` | `pages/ReportPage.tsx` | `Breadcrumb` | Same | Deep link renders the same preview |
| 13 | `#/results/usage` | `pages/UsagePage.tsx` | `Metric`, `ChartFrame`, `Field` | Single column; limit fields stack < 768 | Hour/day totals, categories, limits |
| 14 | `#/classes` | `pages/ClassesPage.tsx` | `Card`, `EmptyState`, `InlineBar` | 1 → 2 → 3 columns | Class stats, last sync |
| 15 | `#/classes/classroom` | `pages/ClassroomPage.tsx` | `Stepper`, `Field`, `Select`, `Button` | Stacked; selects full width < 768 | Connect, disconnect, sync, permission check |
| 16 | `#/classes/classroom/{c}/{w}` | `pages/ClassroomPage.tsx:600+` | `DataTable`, `FilterBar`, `Pagination`, `ConfirmDialog` | Table → cards < 768 | 78-row tracking, correct-all, publish, reload, export |
| 17 | `#/classes/students` | `pages/StudentsPage.tsx` | `DataTable`, `SearchField`, `InlineBar` | Table → cards < 768 | Roster, nickname, counts, selection |
| 18 | `#/classes/students/{id}` | `pages/StudentsPage.tsx:970+` | `SectionTabs`, `Metric`, `DataTable`, `LineChart` | 2 columns ≥ 1280; tabs below | Metadata edit, assignment history, grades, Classroom ids |
| 19 | `#/classes/assignments` | `pages/AssignmentsPage.tsx` | `DataTable`, `FilterBar`, `Modal` form | Table → cards < 768 | Filters, create, publish, sync |
| 20 | `#/classes/assignments/{id}` | `pages/AssignmentsPage.tsx:930+` | summary strip, `DataTable`, `ConfirmDialog` | Table → cards < 768 | Submissions, correct-all, update, publish |
| 21 | `#/system/settings` | `pages/SettingsPage.tsx` | settings frame, `Field`, `Switch`, `DangerZone`, `ConfirmDialog` | Index ≥ 1024; select < 1024 | Every setting and its immediate-apply behaviour |
| 22 | `#/system/ai` | `pages/AiPage.tsx`, `pages/KeysPage.tsx`, `pages/NetworkPanel.tsx`, `workers/panels.tsx` | `SectionTabs`, `DataTable`, `Menu`, `Modal` | Table → cards < 768 | Key add/test/disable/remove, rotation, worker config, network/public address |
| 23 | `#/system/storage` | `pages/StoragePage.tsx` | `Metric`, `FileDrop`, `DataTable`, `ConfirmDialog` | Stacked; diff table h-scroll | Export, import preview/apply, stored corrections |
| 24 | `#/system/privacy` | `pages/PrivacyPage.tsx` | `Card`, `DangerZone`, `ConfirmDialog` | Stacked | Diagnostics export, factory reset |
| 25 | `#/system/diagnostics` | `pages/DiagnosticsPage.tsx` | `GatedPage`, `SectionTabs`, `LogViewer` | Tabs; log fills width | Developer-only access, system/Gemini/Classroom/storage/log data |
| 26 | `#/system/prompts` | `pages/PromptsPage.tsx` | `GatedPage`, editor, `ConfirmDialog` | Stacked; editor full width | Prompt editing and save |
| 27 | `#/system/audit` | `pages/AuditPage.tsx` | `EmptyState`, `CopyBlock` | Stacked | Audit record display and commands |
| 28 | `#/system/updates` | `distributed/UpdatesPage.tsx` | `GatedPage` | Stacked | Release list/publish (gated) |
| 29 | `#/system/distributed` | `distributed/DistributedPage.tsx`, `panels.tsx`, `SyncMonitor.tsx` | `GatedPage`, `SectionTabs` | Stacked | WIP notice, developer console, sync status |
| 30 | sign-in / setup | `SignIn.tsx`, `pages/SetupWizard.tsx` | `Field`, error summary, `Stepper`, `Button` | Centred card ≤ 400 px; full width < 480 | Account setup, sign-in, remember-me, self-check, skip |

**Shared files that change in almost every row:** `App.tsx` (routes), `components.tsx` (until Phase 2
replaces it with `components/`), `styles.css` (until Phase 10), `api.ts` (error mapping only — no
endpoint or payload changes).
