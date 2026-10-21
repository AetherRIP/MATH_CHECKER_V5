# Redesign Findings Register

**49 findings**: **3 × P0**, **17 × P1**, **21 × P2**, **8 × P3**. Every finding names its evidence
and the change that resolves it. Priority reflects user impact, not visual preference.

**Priority definitions**
* **P0** — blocks or seriously misleads a core workflow, or fails WCAG Level A.
* **P1** — major usability, hierarchy, consistency or AA accessibility problem on a common screen.
* **P2** — meaningful clarity, density or maintainability problem.
* **P3** — polish, drift or hygiene.

**Validation**: `DOM` = measurable in the browser · `SRC` = source assertion · `MAN` = manual
keyboard/AT walk · `VIS` = visual review against the blueprint · `CMD` = a command's exit status.

---

## P0 — Critical (3)

| ID | Screen / component | Finding and evidence | Proposed change | Validation | Risk |
|---|---|---|---|---|---|
| **RD-001** | Mark-Scheme Review table; any icon-only control | **3 icon-only buttons have no accessible name.** DOM probe on `#/scheme-review`: two `.btn.ghost.sm` row buttons and one `.btn.secondary.sm` return `aria-label = null`, no text, no title. A screen reader announces only "button". Fails WCAG 4.1.2 (A). | `IconButton` requires `label`; row actions become labelled `Open` buttons; add a check for unnamed controls. | DOM + MAN | Low — additive |
| **RD-002** | `#/correct` | **The core workflow page has no `<h1>`.** It is framed by `Section`, whose `<h3>` reads "Correct a PDF"; the DOM query returns `h1: []`. Fails 1.3.1/2.4.6 — the page has no navigable title. | Every page renders `PageHeader` with the single `<h1>`; `Section` is never a page frame. | DOM + SRC | Low |
| **RD-018** | Home attention panel; Classes, Students, Prompts, Classroom | **Raw errors are primary content.** The dashboard shows `PERMISSION_DENIED, {'error': {'code': 403…}}` and `DEADLINE_EXCEEDED … 504` for 9 items, truncated mid-sentence; a brief backend restart replaced page content with the string `Failed to fetch` (Prompts, Classes, Students). | One error taxonomy: plain cause + one action; provider payloads only under "Technical details" in Diagnostics; `ErrorState` everywhere. | VIS + DOM | Medium — needs error mapping in `api.ts` |

## P1 — High (17)

| ID | Screen / component | Finding and evidence | Proposed change | Validation | Risk |
|---|---|---|---|---|---|
| **RD-003** | AI page; Dashboard | **Two `<h1>`s and duplicated titles.** `#/ai` renders `h1 "AI"` and `h1 "API Keys"` (nested `KeysPage`); the topbar repeats the page title on every screen. | One `<h1>` per page; tab panels start at `<h2>`; the topbar shows a breadcrumb only on detail routes. | DOM + SRC | Low |
| **RD-004** | Global (`--text-faint`) | **Fails AA in both themes.** `#8a93a5` on white = **3.09:1**; `#6b7690` on `#141e33` = **3.66:1** (4.11:1 on the dark page). Used for timestamps, table headers, hints, empty-state copy. | New `--faint`: 4.60 / 4.83 light, 5.84 dark; forbid faint-on-sunken (4.22). | Computed + DOM | Low |
| **RD-005** | Primary buttons | **Gradient end fails contrast.** White on `--accent-2 #7c6af2` = **4.04:1**, so the right half of every primary button label is below AA. | Solid `--accent` (#0F6B5C, 6.41:1) with `--accent-ink`; no gradients on actions. | Computed + VIS | Low |
| **RD-011** | Sidebar (`NAV_GROUPS`) | **24 items in 5 groups; the last two groups sit below the fold at 900 px** and the sidebar scrolls with no cue. Measured: 20 items visible at 1440×900. | Six destinations + section tabs; the rail never scrolls at ≥ 600 px height. | DOM + VIS | Medium — IA change, needs redirects |
| **RD-012** | AI page | **A page renders another page.** `AiPage` mounts `KeysPage`, which renders its own `PageHeader("API Keys")`; the tabs duplicate the navigation model. | Tab panels are content, never pages; `KeysPage` becomes `ApiKeysPanel` with an `h2`. | SRC + DOM | Low |
| **RD-013** | Dashboard, Correct, all others | **Three page-frame patterns**: custom header (`DashboardPage`), `Section` (`PdfCorrectionPage`), `PageHeader` (24 files) → three title sizes, three spacings, three action placements. | One `Page`/`PageHeader` frame with at most 3 actions and one meta line. | VIS + SRC | Medium — touches every page |
| **RD-016** | `#/correct` | **No upload progress, no cancel, no per-file state.** A single `fetch` POST with a boolean `busy`; a 40 MB paper shows a disabled button labelled "Inspecting & queueing…" for the whole upload; validation only after submit. | Two-step flow, `FileDrop`/`FileCard`, determinate `UploadProgress` (XHR), cancel until acknowledged, inline validation, fields preserved on error. | VIS + DOM | Medium — transport change, endpoint contract unchanged |
| **RD-017** | `#/correct`, `#/largepdf`, `#/corrections` | **Native file inputs.** `<input type="file" className="input">` renders the OS "Choose file / No file chosen" widget — a light control inside the dark UI — with no drop zone and no remove/replace. | `FileDrop` + `FileCard`; accept filters enforced before upload. | VIS | Low |
| **RD-019** | All fetching pages | **Error/loading coverage is inconsistent.** `ErrorState` is used in **5 of 24** pages; Reports stayed on "Loading report options…" with no timeout; Students showed an error banner *and* "Loading students…" at once; Classes rendered a bare "Failed to fetch". | One state machine per page: `loading → ready / empty / error(retry)`; timeouts; never loading + error. | SRC + DOM | Medium — per-page work |
| **RD-020** | `#/results/{id}` | **The marking workspace's centre pane is empty by default** ("The original work is not stored on this device") while occupying 380 of 1,128 px. Retention defaults to `until_complete`, so this is the common case, and no action is offered. | The answer pane becomes a real state: extracted answer text, link to Drive/Classroom, retention link; three panes only when a document exists. | VIS + DOM | Medium |
| **RD-021** | `#/review` | **No sort, filter or bulk action** on the review queue; accept is optimistic with a toast and no undo; at 50 items the only path is one click per row. | Filter/sort bar, row expansion, bulk accept with confirm, 8 s Undo, row restore on failure. | VIS + SRC | Medium |
| **RD-025** | `#/results` table | **Three columns carry one fact.** Assignment, Class and Classroom all render the same truncated strings ("Ch2L1 - Constructing Algebra…", "WBIS - O Level Mathematics …"); `%` duplicates `Score`. 8 columns, 3 redundant, 2 unreadable. | Student (frozen) · Assignment · Score (fraction + micro-bar + %) · Status · Classroom · Updated; column chooser; full text on hover. | VIS + DOM | Low |
| **RD-026** | `#/students` | **Score column is not comparable.** Raw fractions with different denominators (`428/508`, `169/255`, `—`) in one column; no percentage. | Percentage primary, fraction secondary on hover; sort by percentage. | VIS + DOM | Low |
| **RD-027** | Results, Students, Tasks, Tracking | **No sorting; pagination is "Load 50 more".** 50 of 169 with no position; 80 students in one 4,299 px scroll; 78-row tracking with no in-table filter; Task Manager is not a table at all. | Shared `DataTable`: sortable headers, range pagination, frozen identifier, sticky header; virtualise > 200 rows. | DOM | Medium |
| **RD-031** | 36 files with inline styles | **936 inline style objects** (Assignments 106, Results 102, Settings 83). Values are magic numbers (18/12/10/8/6) that bypass the `--s1…--s8` scale. | No inline styles; tokens only; grep check in CI. | SRC | High effort, low risk per change |
| **RD-032** | Buttons, filters, cards | **No component discipline.** 8 button variants; 109 raw `<button>` vs 234 `<Button>`; `FilterBar` used in 1 file while 4 pages hand-build filters; `ConfirmDialog` in 1; `ErrorState` in 5. | 4 variants; shared `FilterBar`, `ConfirmDialog`, `ErrorState`; lint rule against raw `<button>` in pages. | SRC | Medium |
| **RD-033** | Global typography | **The declared font is never loaded.** `--font` starts with `Inter`; there is no `@font-face`, no webfont link, and no Inter on the test machine, so `document.fonts` is empty and every screen renders in Segoe UI. | Self-host Plex Sans/Serif/Mono subsets, preload 500, `font-display: swap`, metric overrides. | DOM + VIS | Low |

## P2 — Medium (21)

| ID | Screen / component | Finding and evidence | Proposed change | Validation | Risk |
|---|---|---|---|---|---|
| **RD-006** | `AttentionItem` (`components.tsx:120`) | **Clickable `div role="button"` activates on Enter only.** `onKeyDown` checks `e.key === "Enter"` and ignores Space, while the project's own `rowActivate` handles both. Fails 2.1.1 (A) for that control. | Render a real `<button>`/`<a>`, or use `rowActivate`. | MAN + SRC | Low |
| **RD-007** | `#/results/{id}` | **Heading order contradicts the layout.** `<h1>Correction detail</h1>` → `<h2>` only in the right pane → seven `<h3>`s in a flat run (right-pane cards before the page's own sections); left-pane titles are `div`s. | Workspace `h2`; sections `h2`; panel titles `h3` within their section. | DOM | Low |
| **RD-008** | Diagnostics, Prompts, Updates, Distributed | **Role-denied screens are bare text.** `#/prompts` renders the single string "This page is Developer-role only." with no heading, card or way back; Diagnostics renders one sentence; Updates links to a different sign-in. | One `GatedPage`: title, one-sentence reason, what the page contains, Back, optional Sign in. | VIS + SRC | Low |
| **RD-009** | Global | **Small targets for touch.** Icon buttons are 30×30 (`sm`) and 36×36. The AA minimum (24) passes, but 30 px is below the 44 px recommendation on the tablets teachers use. | 32 px default, 44 px hit area under `pointer: coarse`, `lg` 44 px for flow actions. | DOM | Low |
| **RD-010** | Shell (hash routing) | **No focus management on route change.** Navigation updates the hash and swaps the page, but focus stays on the clicked nav item and nothing announces the new page. | Move focus to `<h1 tabindex="-1">` and announce the title via a polite live region. | MAN | Medium — nested routes need care |
| **RD-014** | `#/corrections`, `#/scheme-review/{id}` | **Duplicate addresses for one task, and a route nobody generates.** The legacy upload page duplicates `#/correct` with different copy; `#/scheme-review/{id}` is supported but the UI opens a drawer and never exposes an id. | Redirect legacy hashes (replace) to canonical routes; make the discrepancy detail a real route used by the table. | DOM + SRC | Low |
| **RD-015** | `#/reports/{c}/{w}` | **Missing breadcrumb** while every other drill-down has one; the topbar sub-line shows "synced 19m ago" instead. | Breadcrumb `Results / Reports / <assignment>`. | VIS | Low |
| **RD-022** | Scheme review, Assignments, Settings, Connections | **Consequential actions are unprotected.** `ConfirmDialog` is used in **1** file while bulk correct-all, mark-scheme decisions, disconnect and reset exist; five equal-weight buttons in the discrepancy drawer; "Correct all submitted" starts a long job with no count. | `ConfirmDialog` mandatory for destructive/irreversible/bulk actions, naming object and count; one primary + secondaries. | SRC + MAN | Low |
| **RD-023** | `#/automation` | **24,493 px page** mixing configuration, live visualiser, live students, system activity, recent jobs and bulk upload; "Recent jobs" is ~20,000 px down. | Section tabs (Overview / Students / Jobs / Bulk) + sticky run strip. | DOM | Medium — preserve all controls |
| **RD-024** | `#/settings` | **4,357 px, 17 unrelated sections, no index.** High-risk controls (host account, multi-account, testing mode) sit inline with routine ones. | Two-column settings with a sticky section index, a danger zone and confirmations. | DOM + VIS | Medium |
| **RD-028** | `#/reports`, `#/reports/{c}/{w}` | **11 tables / 199 rows / 11,514 px** of preview interleaved with builder controls; two "Generate preview" and two "Export PDF" sets. | Builder + preview two-column; one action set in the preview toolbar; section anchors. | VIS + DOM | Medium |
| **RD-029** | `#/home` hero | **KPI scope is ambiguous.** "Corrections completed today 0" beside "199 COMPLETED" with no qualifier; "0/78 checked" beside "1 to mark" (roster vs submissions). | Every number states its scope; the hero becomes three labelled daily metrics. | VIS + SRC | Low |
| **RD-034** | Global typography | **No rem, no user font scaling.** The scale is `clamp(px, vw, px)`, so text ignores the browser's font-size setting. | rem-based scale, 16 px root, clamp only for display sizes. | SRC | Low |
| **RD-035** | Prompts, SchemeSourceViewer, SetupWizard | **4 undefined custom properties**: `--line`, `--bg`, `--info`, `--surface-2`. Declarations using them are dropped — Prompts has no container background/border; the source viewer's scroll box has neither. | Map to real tokens (`--rule`, `--canvas`, `--info`, `--surface-sunken`). | SRC + VIS | Low |
| **RD-036** | Overlays | **Two stacking systems.** `report.css` defines `--z-sticky…--z-toast`; `styles.css` hardcodes `20/50/55/60/120/130/140/200/3000` and adds `.card:has(.dropdown-menu){z-index:20}`. | One z-scale; portalled menus (no `:has` hack). | SRC | Medium — overlay regressions are easy |
| **RD-040** | Responsive | **No breakpoint scale; JS/CSS mismatch.** CSS uses 620/760/860/900/1100/1200; `WorkspacePanes` switches at `(max-width: 899px)` while CSS uses 900. | 480/768/1024/1280/1536 tokens; workspace collapses at 1023; one source of truth. | SRC + DOM | Low |
| **RD-041** | Charts | **Chart accessibility is partly unverified.** `ChartFrame` supplies a summary and hidden table, but series colours and donut slices rely on colour, and 11 px labels sit at `--faint` (3.09:1). | Pattern/dash differentiation + direct labels; 12 px labels at ≥ 4.5:1; keep the hidden table. | MAN + DOM | Low |
| **RD-042** | Data layer | **16 pollers, no shared cache.** Shell: dashboard 15 s, attention 30 s, review 30 s; Home adds assignments 15 s, activity 120 s, usage 60 s, SSE + 6 s fallback; pages add their own. One screen can issue ~8 requests per cycle. | One status store/context + SSE; per-resource TTL; pause when `document.hidden`. | SRC + Network | Medium |
| **RD-043** | Bundle | **No vendor/route preloading strategy.** 719 KB JS total, 246 KB entry (77 KB gzip), 59.6 KB CSS. Fine today; the redesign adds components and could regress it. | Vendor chunk, preload on nav hover/focus, budget check (entry ≤ 90 KB gzip). | CMD (build output) | Low |
| **RD-046** | Tooling | **The only route-walking UI test cannot run.** `frontend/scripts/ui_smoke.mjs` imports `jsdom`, which is not declared and not installed (`ERR_MODULE_NOT_FOUND`). | Add `jsdom` as a dev dependency **or** rewrite against Playwright; wire to `npm test`. | CMD | Low |
| **RD-047** | Tooling | **No lint/format configuration.** No ESLint, Prettier or Biome; 936 inline styles and 109 raw buttons are unenforced. | ESLint (typed) + Prettier; rules for named icon buttons, no inline styles, no raw hex. | CMD | Low |

## P3 — Low (8)

| ID | Screen / component | Finding and evidence | Proposed change | Validation | Risk |
|---|---|---|---|---|---|
| **RD-030** | Tables | **Truncation hides identity.** Assignment/class cells ellipsise mid-word with no `title`, no wrap and no expandable row. | `title` + two-line wrap for identity columns; never truncate a row's name. | VIS + DOM | Low |
| **RD-037** | `styles.css` | **Duplicate and unused animation.** `@keyframes spin` and `spinSlow` are identical; `scaleIn` and `fadeIn` have no remaining users. | Keep `spin`, `fadeIn`, `pop`, `toastIn`; delete the rest. | SRC | Low |
| **RD-038** | Sign-in | **Non-existent class.** The show/hide password button uses `btn ghost size-sm`; the real class is `sm`, so it renders at 36 px inside a 36 px field. | `size="sm"`. | VIS + SRC | Low |
| **RD-039** | Sign-in | **Product name differs.** Sign-in says "Math Correct" three times; the shell, wizard and reports say "Math Checker". | One name and tagline in a single constant. | SRC | Low |
| **RD-044** | Progress UI | **450 ms width transitions on many bars** (automation student cards, job lists) cause layout/paint work during batches. | 120 ms or none for frequently updating bars. | DOM/perf | Low |
| **RD-045** | Fonts | **No font strategy.** Adding a webfont naively causes swap shift on a 3,000 px page. | Preload 500, `size-adjust`/metric overrides, subset, self-host (source #27). | CLS measurement | Low |
| **RD-048** | Tooling | **Smoke expectations are stale.** `ui_smoke.mjs` expects "Corrections today", "API health", "Workers", "Clear Log" — strings that moved or were removed. | Regenerate expectations from the page inventory; assert headings, not marketing copy. | CMD | Low |
| **RD-049** | `#/classes` | **Empty state is a string.** Cards render the literal text "no results" in the metric area instead of an `EmptyState`. | `EmptyState variant="no-results"` inside the card body. | VIS + SRC | Low |

---

## Finding → phase mapping

| Phase | Findings resolved |
|---|---|
| 1 · Foundations | RD-004, RD-005, RD-033, RD-034, RD-035, RD-036, RD-037, RD-039, RD-045 |
| 2 · Primitives | RD-001, RD-006, RD-009, RD-019 (component), RD-022, RD-030, RD-031, RD-032, RD-038, RD-041, RD-044 |
| 3 · Shell & IA | RD-003, RD-010, RD-011, RD-012, RD-013, RD-014, RD-040 |
| 4 · Home | RD-018, RD-029 |
| 5 · Correct & jobs | RD-002, RD-016, RD-017, RD-023 |
| 6 · Marking workspace | RD-007, RD-020, RD-025 (margin), RD-027 (workspace) |
| 7 · Results & reports | RD-015, RD-025, RD-026, RD-027, RD-028 |
| 8 · Classes & Classroom | RD-021, RD-027 (tracking), RD-049 |
| 9 · System screens | RD-008, RD-019 (pages), RD-024 |
| 10 · Quality gates | RD-042, RD-043, RD-046, RD-047, RD-048 + re-verification of all P0/P1 |

## Deliberately not findings

| Observation | Why it is not a finding |
|---|---|
| Two `KEY_8` rows | Data-level duplicate label, not a design defect; the redesign makes labels editable and unique as part of the AI-table work. |
| Sidebar scrolls | Symptom of RD-011 (too many items), not a separate defect. |
| `#/prompts` "Failed to fetch" | Symptom of RD-018/RD-019; recorded there rather than duplicated. |
| Dark mode on the test machine | `system` is the default and the OS is dark. Not a defect — but AA must hold in both themes. |
| No password reset | Product gap, not a UI defect; documented as an absence in the inventory. |
