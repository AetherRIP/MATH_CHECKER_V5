# Product and Repository Analysis

What Math Checker actually is, how the frontend is built, and which behaviours the redesign
must preserve. This document is the functional contract for every later phase.

---

## 1. What the application does

Math Checker marks handwritten/printed IGCSE maths homework. A teacher supplies:

1. a **student submission** (PDF or image: `.pdf .png .jpg .jpeg .webp`), and
2. the **official mark scheme** (PDF),

and the backend routes the document to one of **two existing pipelines** based on real page
count (`frontend/src/pages/PdfCorrectionPage.tsx:20-38`, `backend` `/api/corrections/unified`):

| Pipeline | When | Behaviour |
|---|---|---|
| **Single-pass** (`standard`) | ≤ `threshold_pages` (30 by default) | Whole paper marked, verified, saved in one job |
| **Batched** (`large`) | Above the threshold, or forced | inspect → plan question-aware batches → mark each → validate → merge |

The result carries per-question marks, a confidence level, reasons, a percentage, a 1–9 grade
looked up from configured boundaries, an optional report PDF, and a provenance state
(`ai` / `verified` / `teacher` / `overridden`). Marking is **never** silently overwritten: a
teacher override is permanent (`ResultsPage` remark/versions APIs, `ReviewPage` override).

Around that core:

- **Google Classroom** sync of courses, coursework and submissions; publishing grades and
  attaching reports; a "Correct entire assignment" bulk action.
- **Automatic Correction** — unattended pipeline with review mode and per-student delivery.
- **Review Queue** — results flagged low-confidence / validation-failed / large-score-change.
- **Mark-Scheme Review** — the engine records where a supplied mark scheme appears wrong.
- **Reports** — class/assignment report builder with A4 PDF export.
- **Storage / Privacy** — export/import of a `mathchecker.transfer v1` file, retention
  settings, factory reset, diagnostics bundle.
- **System** — API key pool with rotation and health, internet-usage limits, background worker
  settings, server lifecycle, Multi-School (WIP, developer-gated), application updates.

Observed live state during the audit: **199 completed corrections, 4 failed, 1 processing,
169 results, 12/12 API keys healthy, 5 items awaiting review, a 78-student class**, Classroom
connected. This is a production-sized data set, not a demo — which is why density and table
behaviour matter.

---

## 2. Technical architecture (frontend)

| Concern | Implementation | Evidence |
|---|---|---|
| Framework | React 18.3 + TypeScript 5.6, Vite 5.4 | `frontend/package.json` |
| Dependencies | **only** `react`, `react-dom` — no router, no UI kit, no icon package, no chart library, no CSS framework | `frontend/package.json` |
| Entry | `frontend/index.html` → `src/main.tsx` → `App` | `index.html:1-30` |
| Routing | **Hash routing** parsed by `parseHash()`; one `<Shell>` switch renders the page | `App.tsx:200-230`, `App.tsx:520-560` |
| Code splitting | every page is `lazyRoute(() => import(...))`, shell eager; stale-chunk reload guarded by `sessionStorage` | `App.tsx:30-63` |
| State | local `useState` per page + polling + one SSE stream; no store library | `App.tsx:270-320`, `DashboardPage.tsx:57-95` |
| Styling | two global stylesheets (`styles.css` 1,640 lines, `report.css` 282 lines) + **936 inline style objects** | measured |
| Theming | `data-theme` on `<html>`, pre-paint script in `index.html`, `ThemeProvider` (`light/dark/system`) | `theme.tsx`, `index.html:8-22` |
| Icons | 58 hand-written inline SVGs (`Icons.tsx`) | measured |
| Charts | 9 hand-rolled SVG components (`charts/Charts.tsx`) with text summaries and hidden tables | `Charts.tsx:29-345` |
| Overlays | `Modal`, `Drawer` with focus trap, Escape, scroll lock, focus restore; one `TooltipLayer` portal | `components.tsx:400-600`, `TooltipLayer.tsx` |
| Feedback | toast stack (`aria-live=polite`), inline notices, skeletons, `Empty`, `ErrorState` | `components.tsx` |
| Search | top-bar command palette (`Ctrl K`) over students/assignments/courses | `SearchBox.tsx` |
| Tests | `node --test tests/*.test.ts`: 89 tests (86 pass, 3 skip, 0 fail) | measured |
| Lint/format | **none configured** | no eslint/prettier/biome config |

### Route model

`VALID_ROUTES` lists **24 canonical routes**; `ALIASES` maps 4 legacy hashes
(`report→reports`, `keys→ai`, `workers→ai`, `resultsPage→results`). Six parameterised detail
routes are handled by the page component from `nav.params`:

`#/assignments/{id}`, `#/students/{id}`, `#/results/{id}`, `#/scheme-review/{id}`,
`#/reports/{courseId}/{workId}`, `#/classroom/{courseId}/{workId}`.

Unknown hashes fall back to `#/ai` (`App.tsx:224-236`) — the landing page. There is **no
marketing/landing page, no registration, no password recovery**; sign-in exists only when the
backend requires a session, and first-run account creation happens on that same screen.

### Data flow that shapes the UI

```
App shell ── /dashboard (15s) ─┐
          ── /attention (30s) ─┤   shell-wide pollers
          ── /review?limit=500 ┘
Dashboard ── /assignments (15s), /analytics/activity (120s), /usage (60s),
          ── EventSource /api/jobs/stream, poll fallback (6s)
Page      ── its own fetches (results, students, settings, …)
```

There is **no cache or shared store**: switching pages refetches; the same `/dashboard` payload
is fetched by the shell and again by `DashboardPage` (the shell passes it as a prop, but the
page still mounts its own three pollers). Measured: ~16 `setInterval` call sites
(5 × 30 s, 4 × 15 s, 3 × 5 s, 2 × 10 s, plus 8 s / 60 s / 120 s) — see RD-042.

---

## 3. Hard constraints the redesign must respect

These are not style opinions; breaking them breaks the product.

| # | Constraint | Where it lives |
|---|---|---|
| C1 | Hash routing and existing deep links must keep resolving, including the 4 aliases and 6 detail routes | `App.tsx:200-236` |
| C2 | Both pipelines and the automatic routing rule must remain selectable; the applied pipeline must always be named in the UI | `PdfCorrectionPage.tsx:100-150` |
| C3 | A teacher override is permanent; no automated run may replace it | `ReviewPage.tsx:60-80`, `ResultsPage` remark API |
| C4 | Publishing to Classroom requires explicit confirmation; nothing auto-publishes from Review | `ReviewPage.tsx:9-11` |
| C5 | Retention is a privacy setting (`never` / `until_complete` / `days` / `always`); when it is `never`, the original file is gone | `SetupWizard.tsx:236-248`, Settings |
| C6 | Progress, stage detail, attempt count and ETA of jobs must remain truthful; no invented percentages | `api.ts` `Job` type, `Progress` component |
| C7 | Grade is looked up from configured boundaries, never invented by the model | `PdfCorrectionPage.tsx:290-295` |
| C8 | Works fully offline (local model calls aside); no CDN fonts, no remote assets — the app must render on a school laptop with no internet | product constraint, `index.html` has no external links |
| C9 | Multi-School / Updates are developer- and sign-in-gated; gating must remain | `App.tsx:352-360`, `App.tsx:600-615` |
| C10 | The app is used for hours at a time in 50-paper batches: no layout that requires more scroll per paper than today's, and no animation that blocks input | `AutomationPage`, `ResultsPage` |
| C11 | Print/PDF output is a first-class surface (report PDFs); on-screen report preview must match it | `report.css`, `ReportPage.tsx` |

## 4. Functional inventory that must survive (page-level)

| Capability | Current route(s) | Must keep |
|---|---|---|
| Upload + inspect + queue a correction | `#/correct`, `#/corrections` | Both files, student-name fallback from filename, advanced pipeline override, applied-pipeline disclosure, queue |
| Live job monitoring | `#/tasks`, `#/correct` job log, dashboard SSE | Status, stage detail, attempts, cancel/retry, filters |
| Batched large-PDF recovery | `#/largepdf` | Per-batch retry/cancel, merge result, open result |
| Unattended pipeline | `#/automation` | Enable/disable, review mode, polling interval, monitored assignments, delivery state per student, holds |
| Review decisions | `#/review` | Accept, override with note, provenance, "review delivery in automation" |
| Mark-scheme disputes | `#/scheme-review` | Evidence drawer, 5 decision actions, bulk-correction preparation |
| Results + question drill-down | `#/results`, `#/results/{id}` | Search/filter, marks per question, subquestions, remark, versions, double-check diffs, export, prev/next |
| Reports | `#/reports`, `#/reports/{c}/{w}` | Type/class/assignment selection, preview, PDF/CSV export, "missing homework" report |
| Classroom | `#/classroom`, `#/classroom/{c}/{w}` | Sync, permission check, per-student tracking, correct entire assignment, publish |
| Classes / Students | `#/classes`, `#/students`, `#/students/{id}` | Roster, nickname, submission counts, per-student assignment history and grades |
| Assignments | `#/assignments`, `#/assignments/{id}` | Managed/external/local filters, create, publish, submissions table, correct all |
| AI system | `#/ai` (4 tabs) | Key pool add/test/disable/remove, worker config, network/public address |
| Storage / Privacy | `#/storage`, `#/privacy` | Export/import, inventory, retention, factory reset with confirmation, diagnostics bundle |
| Usage | `#/usage` | Hour/day totals, per-category breakdown, limits |
| Settings | `#/settings` | Public address, host account, multi-account, remote access, workers, retention, grade boundaries, mock/testing, lifecycle, model chain, portability |
| Developer | `#/diagnostics`, `#/prompts`, `#/audit`, `#/updates`, `#/distributed` | Role-gated technical surfaces |
| Session | sign-in screen, setup wizard | First-run account creation, public-mode notice, remember-me, password reveal |

## 5. Where the current implementation is hard to maintain

1. **Page-level monoliths.** `ResultsPage.tsx` 1,727 lines, `AssignmentsPage.tsx` 1,611,
   `AutomationPage.tsx` 1,463, `SettingsPage.tsx` 1,261. Each mixes data fetching, formatting,
   layout and one-off styles.
2. **Three page frames.** Custom (`DashboardPage`), `Section` (`PdfCorrectionPage`), `PageHeader`
   (24 files) → three heading levels and three spacing rhythms for the same job (RD-013).
3. **Inline styling as the default.** 936 `style={{…}}` objects; the largest offenders are
   Assignments (106), Results (102), Settings (83), Automation (57), Dashboard (48). Values are
   magic numbers, not `--s*` tokens (RD-031).
4. **Two competing stacking systems.** `report.css` defines `--z-sticky…--z-toast`; `styles.css`
   hardcodes `20/50/55/60/120/130/140/200/3000` and adds a `:has()` "lift the card that holds an
   open menu" hack (RD-036).
5. **Token drift.** Four custom properties are used but never defined: `--line`, `--bg`,
   `--surface-2`, `--info` (`PromptsPage.tsx:36-37`, `SchemeSourceViewer.tsx:179-180`,
   `SetupWizard.tsx:257`) → those declarations are dropped by the browser (RD-035).
6. **Duplicated UI patterns instead of shared ones.** `FilterBar` is used in **1** file while
   Results, Students, Assignments and Task Manager each hand-build filter rows; `ConfirmDialog`
   in **1** file while destructive actions exist in ≥6; `ErrorState` in **5** of 24 pages (RD-019,
   RD-032).
7. **Navigation as a list, not a model.** `NAV_GROUPS` renders 24 flat items; pages embed other
   pages (`AiPage` renders `KeysPage`, which renders its own `PageHeader`) (RD-011, RD-012).

## 6. What is genuinely good and must be carried forward

These are rare in codebases this size and should not be lost in a rewrite:

- **Honest states.** Charts say "No papers have been corrected in the last 30 days" rather than
  drawing an empty axis (`DashboardPage.tsx:330-340`); the reconnect banner says what is
  happening and what is *not* lost (`lifecycle.tsx:150-175`).
- **Accessible overlay behaviour.** Escape, Tab cycling, scroll lock, focus restore, `aria-modal`,
  `data-autofocus` (`components.tsx:400-470`).
- **Named progress.** Every progress bar can carry a label ("Correcting {student}") because
  an unlabelled bar is announced as "0%" (`components.tsx:150-165`).
- **Colour is never the only signal.** Status = tone + icon + text (`Badge`, `ProvenanceBadge`).
- **Keyboard rows.** `rowActivate()` gives `<tr>` Enter/Space semantics without invalid nesting
  (`components.tsx:180-196`).
- **Reduced motion and print.** Global `prefers-reduced-motion` rule and a dedicated report
  stylesheet.
- **Zero runtime UI dependencies.** 719 KB of JS total (77 KB gzip entry) with no icon font, no
  chart library, no CSS framework — a redesign must not regress this.

## 7. Assumptions that remain unverified

| Assumption | Why it matters | How to verify |
|---|---|---|
| Sign-in and first-run wizard render as expected | They only appear in auth/public or first-run states; the audit machine is signed in and configured | Enable `login_required`, or use a fresh data dir |
| `#/scheme-review/{id}` deep link renders the detail | The UI opens detail in a drawer and never exposes the id in the hash | Navigate with a real discrepancy id |
| `#/updates` authenticated content | Page is sign-in-gated | Sign in with a developer account |
| PDF document pane behaviour when files *are* retained | Retention is `never/until_complete` here, so the centre pane was empty | Set "Always keep", run one correction, reopen |
| Multi-School flows | Developer-gated WIP, explicitly not release-approved | Developer account |

None of these change the redesign direction; they change only the detail specs for those three
screens, which are marked **UNVERIFIED** in the page inventory.
