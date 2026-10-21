# Regression, Accessibility and Performance Checklist

The gate for the redesign. Run the relevant block after **every** task, and the whole document
before sign-off. A build that compiles is not a pass: each item names how it is verified.

---

## 0. Baseline to capture before Phase 1

| Item | Command / method | Record |
|---|---|---|
| Build | `cd frontend && npm run build` | exit code, `dist/assets` sizes, entry gzip |
| Unit tests | `cd frontend && node --test "tests/*.test.ts"` | **89 tests / 86 pass / 3 skip** (2026-10-20) |
| UI smoke | `node scripts/ui_smoke.mjs` | currently **fails**: `jsdom` missing (RD-046) |
| Backend suite | `.venv/Scripts/python.exe -m pytest backend/tests -q` | pass/fail counts |
| Screens | 24 base routes × 1440/768/390 | local `docs/ui-ux-redesign/screenshots/` (git-ignored — real class data) |
| Route metrics | DOM probe: `h1`, headings, tables, overflow, scrollHeight | table in `complete-page-inventory.md` |
| Marking fixture | one result id with a known score (e.g. `44/49`) | id + score preserved for T6 comparison |

---

## 1. Functional preservation (must all pass)

### 1.1 Routing

- [ ] All **24 base routes** resolve to a page with the expected `<h1>`.
- [ ] All **6 detail routes** resolve with real ids: `#/assignments/{id}`, `#/students/{id}`,
      `#/results/{id}`, `#/scheme-review/{id}`, `#/reports/{c}/{w}`, `#/classroom/{c}/{w}`.
- [ ] All **4 aliases** redirect: `report`, `keys`, `workers`, `resultsPage`.
- [ ] Legacy `#/corrections` redirects to the canonical upload page; `#/dashboard` → `#/home`.
- [ ] An unknown hash does not show a blank page (falls back to the landing destination).
- [ ] Browser Back returns to the previous page, not to a redirect loop.
- [ ] A deep link pasted into a fresh tab lands on the right screen after load.

### 1.2 Upload and correction

- [ ] Both files required; submission accepts `.pdf .png .jpg .jpeg .webp`, mark scheme `.pdf`.
- [ ] Wrong type / oversize is rejected **before** upload with a specific message.
- [ ] Student name falls back to the filename when empty.
- [ ] Advanced pipeline choice still offers Automatic / Single-pass / Batched and respects
      `large_pdf_enabled = false`.
- [ ] The response names the applied pipeline, page count and batch count.
- [ ] Upload progress is determinate and cancel works before the server accepts the job.
- [ ] Cancelling or failing preserves the chosen files and typed name.
- [ ] A queued job appears in the job log/queue within one poll interval.

### 1.3 Marking (the numbers must not change)

- [ ] For a fixture result, the total `earned / available`, percentage and band are identical to
      the pre-redesign values (`44/49 · 89.80% · band 8`).
- [ ] Setting a question mark calls the same endpoint with the same body and persists after reload.
- [ ] Subquestions, unmarked subquestions and partial marks render and remain editable.
- [ ] Provenance states (AI / verified / teacher / overridden) are unchanged in meaning.
- [ ] Confidence level and reasons are present where they were present before.
- [ ] Prev/Next moves between results in the same order as before.
- [ ] Export produces the same data (CSV/PDF row counts and totals).
- [ ] With retention `never`, the workspace still explains the missing document and links to the
      original where known.
- [ ] With retention `always`, the document renders, pages navigate and zoom works.

### 1.4 Review and decisions

- [ ] Accept marks the result approved and removes it from the queue.
- [ ] Override saves the mark **and** the note; the result is permanently protected.
- [ ] A failed accept restores the row and shows the error with a retry.
- [ ] Bulk accept affects exactly the selected rows (count in the confirmation).
- [ ] Nothing is published to Classroom without explicit confirmation.

### 1.5 Jobs, automation, Classroom, reports, storage

- [ ] Cancel and retry work on single and batched jobs; per-batch retry still works.
- [ ] Automation run state, review mode, polling interval, monitored assignments and holds persist.
- [ ] Classroom sync, permission check, tracking table and "correct entire assignment" all work.
- [ ] Report generation, preview and export (PDF/CSV) produce the same content.
- [ ] Storage export downloads a `mathchecker.transfer v1` file; import previews before applying.
- [ ] Factory reset and disconnect still require confirmation.
- [ ] Diagnostics export still produces a bundle.

### 1.6 Session and gating

- [ ] Sign-in: wrong password shows an error summary and an inline message; correct password enters the app.
- [ ] First-run: creating the account signs in immediately; `Skip setup` works.
- [ ] Public mode still requires a session; the remote-no-setup explanation still appears.
- [ ] Developer pages remain gated for a teacher and available to a developer.
- [ ] Sign out clears the session and returns to sign-in.

---

## 2. Accessibility (WCAG 2.2 AA target)

### 2.1 Automated checks (run on every route, both themes)

- [ ] Contrast script: every text node ≥ 4.5:1; large text ≥ 3:1; control borders ≥ 3:1.
      *(Baseline failures: `--text-faint` 3.09/3.66; white on gradient end 4.04.)*
- [ ] Zero interactive elements without an accessible name.
      *(Baseline: 3 on `#/scheme-review`.)*
- [ ] Every `input`/`select`/`textarea` has a label, `aria-label` or `aria-labelledby`.
- [ ] Exactly one `<h1>` per route; no skipped heading levels.
- [ ] Every `<table>` has a `caption` (may be visually hidden) and `scope` on headers.
- [ ] No `div`/`span` with `role="button"` that ignores Space.
      *(Baseline: `AttentionItem`.)*
- [ ] No horizontal document scroll at 320, 768, 1024, 1440, 1920.

### 2.2 Manual keyboard walk (every route)

- [ ] Skip link is the first Tab stop and moves focus to `#main-content`.
- [ ] Focus is always visible (2 px ring) on every surface including the dark rail and coloured badges.
- [ ] Tab order follows visual order; no keyboard trap except dialogs (which trap intentionally).
- [ ] Menus: ↑↓/Home/End, typeahead, Escape restores focus to the trigger.
- [ ] Tabs: ←→ moves between tabs, Home/End jump, the panel is `aria-controls`-linked.
- [ ] Dialogs: focus enters, Tab cycles, Escape closes, focus returns to the opener.
- [ ] Tables: sortable headers are operable by keyboard and expose `aria-sort`.
- [ ] Row actions reachable without a mouse; row activation works with Enter **and** Space.
- [ ] After a route change, focus lands on the new `<h1>` and the title is announced.

### 2.3 Screen-reader spot checks (NVDA or VoiceOver, 5 routes)

- [ ] Home: job progress announced politely, not on every tick.
- [ ] Results: table announces caption, column headers and sort state.
- [ ] Workspace: margin list announces question and marks; mark control announces current value.
- [ ] Upload: file selection and progress are announced; errors are announced.
- [ ] Review: accept and undo announcements are understandable.

### 2.4 Content and zoom

- [ ] 200 % zoom: no text clipped, no control unreachable, no overlap with sticky chrome.
- [ ] 320 px: every flow usable (upload, mark one question, accept a result, export).
- [ ] `prefers-reduced-motion: reduce`: no transforms, no count-up, no chart draw animation.
- [ ] Forced colours / high contrast: content remains readable (spot check).

---

## 3. Responsive matrix

Test each width for each surface; a cell passes only if the flow is completable, not merely visible.

| Surface | 320 | 390 | 768 | 1024 | 1440 | 1920 |
|---|---|---|---|---|---|---|
| Home | | | | | | |
| Correct (upload) | | | | | | |
| Tasks table | | | | | | |
| Automation | | | | | | |
| Marking workspace | | | | | | |
| Results table | | | | | | |
| Reports builder/preview | | | | | | |
| Settings | | | | | | |
| Sign-in / setup | | | | | | |

Specific rules:
- [ ] Navigation: bottom bar < 768; icon rail 768–1279; labelled rail ≥ 1280.
- [ ] Workspace: tabs < 1024; two panes 1024–1279; three panes ≥ 1280.
- [ ] Tables: stacked cards < 768 **or** deliberate horizontal scroll with a frozen first column;
      never clipped by an ancestor's `overflow: hidden`.
- [ ] Touch: every target ≥ 44×44 CSS px under `pointer: coarse`.
- [ ] Landscape phone (844×390): the workspace remains usable.

---

## 4. Performance

Budgets are relative to the measured baseline: **entry 246 KB / 77 KB gzip, total JS 719 KB,
CSS 59.6 KB / 12 KB gzip, no fonts, no images.**

| Metric | Budget | Method |
|---|---|---|
| Entry JS | ≤ 90 KB gzip | `gzip -c dist/assets/index-*.js \| wc -c` |
| Total JS | ≤ 850 KB raw | `cat dist/assets/*.js \| wc -c` |
| CSS | ≤ 25 KB gzip | `gzip -c dist/assets/*.css \| wc -c` |
| Fonts | ≤ 180 KB total, ≤ 3 files on first paint | `ls -l public/fonts` + network panel |
| Requests on Home (steady state) | ≤ 3 per 15 s cycle (down from ~8) | Network panel, 60 s capture |
| Hidden tab | pollers paused or ≥ 60 s | Network panel with tab hidden |
| CLS | ≤ 0.05 on Home, Results, Workspace | Performance panel |
| INP | ≤ 200 ms on sort/filter and question switching | Performance panel |
| Long tasks | none > 200 ms during a batch run | Performance panel |
| Memory | no growth over 10 minutes of live job updates | Performance monitor |

Checks:
- [ ] Sorting 169 results does not re-render the whole shell.
- [ ] Switching questions in the workspace does not re-fetch the result.
- [ ] Progress bars use transform/width with ≤ 120 ms transitions; no 50-row animation storm.
- [ ] A 40 MB upload does not block the UI thread (progress updates remain smooth).
- [ ] Route chunks are fetched on navigation hover/focus if preloading is implemented.
- [ ] No dependency added except the self-hosted fonts (and `jsdom` as a dev dependency).

---

## 5. Design QA (per page, against the blueprint)

- [ ] One `<h1>`; ≤ 3 header actions; one primary action.
- [ ] Register declared (Desk/Ledger); density matches it; no mixed densities in one card.
- [ ] Spacing uses the 4 px ramp only — grep the page for `margin`/`gap` literals.
- [ ] No inline styles; no raw hex; no raw px in TSX.
- [ ] All eight states exist where applicable: loading, empty, no-results, error, partial, success,
      disabled, read-only.
- [ ] Every destructive action confirms and names its object.
- [ ] Every reversible action offers Undo.
- [ ] Every number states its scope; every truncation has a `title`.
- [ ] Charts have a summary and a data-table fallback; colour is never the only encoding.
- [ ] Icons are 20 px grid, 1.5 px stroke, aligned with text baseline.
- [ ] Both themes checked; no pure `#000`/`#fff` text on coloured surfaces.

---

## 6. Route smoke matrix

Each route must show its `<h1>` and its primary content within 2.5 s against live data.

| # | Route | Expected `<h1>` | Must be present |
|---|---|---|---|
| 1 | `#/home` | Home | needs-you, today metrics |
| 2 | `#/correct` | Correct a PDF | two file zones, primary action |
| 3 | `#/correct/tasks` | Tasks | job table |
| 4 | `#/correct/large` | Batched documents | batched table |
| 5 | `#/correct/automation` | Automation | run strip |
| 6 | `#/review` | Review queue | review table |
| 7 | `#/review/scheme` | Mark-scheme review | discrepancy list |
| 8 | `#/review/scheme/{id}` | Discrepancy | evidence + decisions |
| 9 | `#/results` | Results | results table + range |
| 10 | `#/results/{id}` | Correction detail | margin, answer state, mark pane, ledger |
| 11 | `#/results/reports` | Reports | builder + preview |
| 12 | `#/results/reports/{c}/{w}` | Reports | breadcrumb + preview |
| 13 | `#/results/usage` | Internet usage | limits + chart |
| 14 | `#/classes` | Classes | class cards |
| 15 | `#/classes/classroom` | Google Classroom | connection card |
| 16 | `#/classes/classroom/{c}/{w}` | Tracking | student table |
| 17 | `#/classes/students` | Students | student table |
| 18 | `#/classes/students/{id}` | Student name | tabs + at-a-glance |
| 19 | `#/classes/assignments` | Assignments | assignment table |
| 20 | `#/classes/assignments/{id}` | Assignment title | summary + submissions |
| 21 | `#/system/settings` | Settings | section index |
| 22 | `#/system/ai` | AI | key table |
| 23 | `#/system/storage` | Storage | export/import |
| 24 | `#/system/privacy` | Privacy & data | sections |
| 25 | `#/system/diagnostics` | Diagnostics | gated page or tabs |
| 26 | `#/system/prompts` | Gemini Prompts | gated page or editor |
| 27 | `#/system/audit` | Accuracy audit | empty state |
| 28 | `#/system/updates` | Application updates | gated page |
| 29 | `#/system/distributed` | Multi-School | WIP page |
| 30 | sign-in / setup (state-dependent) | Sign in / Welcome | form + summary |

*(Routes 3–8 and 11–13, 15–20, 22–29 are the proposed canonical paths; the implementation must keep
the old hashes redirecting to them.)*

---

## 7. Sign-off

| Area | Owner | Evidence attached | Date |
|---|---|---|---|
| Functional preservation | | test output + fixture comparison | |
| Marking numbers | | before/after screenshot + API payload | |
| Accessibility | | contrast report + keyboard walk notes | |
| Responsive | | 6-width screenshots × 9 surfaces | |
| Performance | | budgets table + traces | |
| Design QA | | route checklist | |

**The redesign is not complete while any P0 or P1 finding from
[redesign-findings-register.md](redesign-findings-register.md) remains open**, or while any of the
functional items in §1 fails.
