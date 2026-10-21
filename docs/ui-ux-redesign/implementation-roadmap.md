# Implementation Roadmap

A dependency-aware plan for building **Chalk & Ink**. Ten phases; each is independently
verifiable and leaves the application working. Do not start a phase before its dependencies are
green — several phases assume primitives that do not exist yet, and page work started early will
be redone.

**Global rule:** the app must remain runnable and shippable at the end of every phase. No phase
may leave two half-styled shells in production for long: the shell switch (Phase 3) is the only
"big bang", and it is protected by route-by-route page work afterwards.

---

## Phase 0 — Baseline and safety net (½ session)

**Objective:** make regressions visible before changing anything.

| Task | Detail |
|---|---|
| T0.1 | Record the current baseline: build output sizes, `node --test` result, and a screenshot set of all 24 base routes at 1440/768/390. |
| T0.2 | Fix the test harness (RD-046): add `jsdom` as a dev dependency **or** port `ui_smoke.mjs` to Playwright; regenerate its expectations from `complete-page-inventory.md` (RD-048); add `npm test` / `npm run test:ui`. |
| T0.3 | Add lint/format (RD-047): ESLint (typescript-eslint, react-hooks), Prettier, and three custom rules/checks: no inline `style={{}}` in `src/**/*.tsx`, no raw `<button>` outside `components/`, no `aria-label`-less icon button. Land as warnings first, errors at Phase 2. |
| T0.4 | Create a branch `redesign/chalk-and-ink`; keep `main` shippable. |

**Done when:** `npm run build`, `npm test` and the UI smoke pass on the unchanged app, and the
baseline screenshots are stored.

---

## Phase 1 — Foundations: tokens, type, colour (2 sessions)

**Objective:** replace the visual *vocabulary* without changing a single component's markup.

**Findings:** RD-004, RD-005, RD-033, RD-034, RD-035, RD-036, RD-037, RD-039, RD-045.

| Task | Detail | Files |
|---|---|---|
| T1.1 | Add the three-tier token layer from `design-system-specification.md §2–§8` in a new `styles/tokens.css`, imported before `styles.css`. Keep the old variables as aliases temporarily so nothing breaks. | `src/styles/tokens.css` (new) |
| T1.2 | Self-host IBM Plex Sans/Serif/Mono subsets (6 files ≤ 180 KB total), `@font-face` with `swap` + metric overrides, preload the Sans 500. Remove the fake `Inter` first position from `--font`. | `public/fonts/`, `index.html` |
| T1.3 | Replace the type scale with rem tokens; convert `--fs-*` usages. Verify 200 % zoom and 320 px reflow. | `styles.css` |
| T1.4 | Map the four undefined properties to real tokens (`--line`→`--rule`, `--bg`→`--canvas`, `--surface-2`→`--surface-sunken`, `--info`→`--info`). | `PromptsPage.tsx:36-37`, `SchemeSourceViewer.tsx:179-180`, `SetupWizard.tsx:257` |
| T1.5 | Replace both z-index systems with the one scale; delete the `:has()` stacking hack and portal menus in Phase 2. | `styles.css`, `report.css` |
| T1.6 | Delete duplicate keyframes; add the name constant for "Math Checker"; fix `size-sm` (RD-037, RD-038, RD-039). | `styles.css`, `SignIn.tsx`, new `src/brand.ts` |
| T1.7 | Recompute and record contrast for every text/surface pair in the new palette; fail the build if a token pair drops below 4.5 (small script). | `scripts/check_contrast.mjs` (new) |

**Risks:** font swap causing CLS on long pages (mitigated by metric overrides + preload); dark
theme regressions (verify both themes on every page in Phase 10).

**Acceptance**
- `document.fonts` contains Plex Sans/Serif at 500 after load; no request to any external host.
- Every text pair ≥ 4.5:1; `--control-border` ≥ 3:1 in both themes.
- Grep: zero uses of `--line`, `--bg`, `--surface-2`; zero duplicate keyframes.
- Build output stays ≤ 25 KB CSS gzip (baseline 12 KB).

**Done when:** the app looks different and nothing has moved. No layout or component change yet.

---

## Phase 2 — Primitives (4–5 sessions)

**Objective:** the component set every page will use.

**Findings:** RD-001, RD-006, RD-009, RD-019 (component), RD-022, RD-030, RD-031, RD-032,
RD-038, RD-041, RD-044.

**Order (dependencies matter):** `Button`/`IconButton` → `Field`/`Input` family → `Menu` →
`Modal`/`ConfirmDialog`/`Drawer` → `Toast` (undo) → `DataTable` + `Pagination` → `EmptyState`/
`ErrorState`/`Skeleton` → `Metric`/`Progress`/`MarkGlyph` → `SectionTabs`/`Breadcrumb` →
`FileDrop`/`FileCard`/`UploadProgress` → charts restyle.

| Task | Detail |
|---|---|
| T2.1 | `Button` with 4 variants and 3 sizes; `IconButton` with a required `label`; a codemod mapping `success|warning|nav` → `primary|secondary|ghost`. |
| T2.2 | `Field` + `Input`/`Textarea`/`Select`/`Checkbox`/`Switch`/`NumberStepper`; error summary component. |
| T2.3 | `Menu` (keyboard + collision positioning) replacing `Dropdown`; `ExportMenu` becomes links in a `Menu`. |
| T2.4 | Keep `useOverlayBehaviour`; add `aria-labelledby`; `ConfirmDialog` focuses Cancel for destructive actions. |
| T2.5 | `Toast` with `undo` and tone-based duration. |
| T2.6 | `DataTable` with sort, sticky header, frozen column, `caption`/`scope`/`aria-sort`, empty/error/loading slots, `Pagination`. |
| T2.7 | `EmptyState`, `ErrorState` (cause + retry + diagnostics link), `Skeleton(rows)`. |
| T2.8 | `Metric`, `Progress` (restyled), `MarkGlyph`, `ScoreBlock`, `InlineBar`, `Stamp`. |
| T2.9 | `FileDrop`, `FileCard`, `UploadProgress` (XHR transport behind the existing endpoint). |
| T2.10 | `AttentionItem` becomes a real button (RD-006). |
| T2.11 | Chart restyle: labels 12 px `--muted`, dashes/patterns, direct labels, keep hidden tables (RD-041). |

**Acceptance**
- Zero inline styles in `src/components/**`.
- All controls have accessible names (automated check).
- Every interactive target ≥ 32 px (≥ 44 px coarse pointers).
- Keyboard walk of a Storybook-style harness page (or a `/dev/components` route, dev-only).
- One `npm test` suite covering table sorting, menu keyboard, field error wiring.

**Done when:** pages still render with the old markup but through new primitives (aliases in place).

---

## Phase 3 — Shell, navigation and IA (3–4 sessions)

**Objective:** six destinations, one page frame, real redirects.

**Findings:** RD-003, RD-010, RD-011, RD-012, RD-013, RD-014, RD-040.

| Task | Detail |
|---|---|
| T3.1 | Route table: map 24 routes → 6 destinations + 12 sub-routes; add redirects (replace) for `#/dashboard`, `#/corrections`, `#/reports/{c}/{w}` canonicalisation, aliases. Keep every deep link working. |
| T3.2 | `AppRail` (72/236 px, collapse shortcut, `aria-current`), bottom bar + More sheet under 768 px. |
| T3.3 | `PageHeader`/`Page` frame with one `<h1>`, ≤ 3 actions, meta line; remove `PageHeader` from `KeysPage` and any tab panel. |
| T3.4 | `SectionTabs` with `tablist/tab/tabpanel`, roving tabindex, overflow handling. |
| T3.5 | Header: title/breadcrumb, search, jobs pill, theme, account. Remove the duplicated title and status chips from the header. |
| T3.6 | `CommandPalette` built from `SearchBox` (pages, students, assignments, courses, actions). |
| T3.7 | Route-change focus management + polite announcement (RD-010). |
| T3.8 | Breakpoint tokens; `WorkspacePanes` reads 1023 px (RD-040). |

**Risks:** the highest-risk phase. Deep links are used by search results and bookmarks; every
redirect needs a test. Overlays may regress when portalled (RD-036) — verify menus inside cards,
tables and drawers.

**Acceptance**
- All 30 screens reachable; every legacy hash lands on its canonical page (test matrix in the checklist).
- Exactly one `<h1>` per route; nav never scrolls at 900 px height; active destination and section
  both correct on every route.
- Keyboard: Tab reaches skip link → rail → header → content; focus lands on `<h1>` after navigation.

**Done when:** the shell is new, the pages inside are still old (acceptable for one phase).

---

## Phase 4 — Home (2 sessions)

**Findings:** RD-018, RD-029.

Tasks: needs-you grouping by cause with plain-language mapping (T4.1, requires an error-mapping
function in `api.ts`); today metrics with scope labels + sparkline (T4.2); next-up card (T4.3);
classes strip (T4.4); first-run and no-work states (T4.5); delete the hero band and the duplicate
counts (T4.6).

**Acceptance:** a failed job shows one sentence and one action, never a payload; every number has a
scope; 320 px stacks without overflow; the page fits in ≤ 1,200 px at 1440×900 for a normal day.

---

## Phase 5 — Correct, Tasks, Large PDF, Automation (4–5 sessions)

**Findings:** RD-002, RD-016, RD-017, RD-023.

| Task | Detail |
|---|---|
| T5.1 | Two-step Correct flow with `FileDrop`/`FileCard`, inline validation, `UploadProgress` with cancel, applied-pipeline disclosure, queue rail. |
| T5.2 | Tasks as a `DataTable` with filters, sort, row actions, bulk cancel/retry, SSE updates. |
| T5.3 | Batched documents table + detail (reuse Tasks table). |
| T5.4 | Automation split into Overview / Students / Jobs / Bulk tabs with a sticky run strip; cap the visualiser at 8 events in a 420 px frame. |
| T5.5 | Replace `#/corrections` with a redirect (T3.1 already does this; remove the page component). |

**Acceptance:** a 40 MB upload shows determinate progress and can be cancelled before the server
accepts it; a failed upload preserves both files and the name; every page in the family uses the
same table; the automation page's scroll height drops from 24,493 px to ≤ 3,000 px for the same
content.

---

## Phase 6 — Marking workspace (5–6 sessions; the centrepiece)

**Findings:** RD-007, RD-020, RD-025 (margin), RD-027 (workspace).

| Task | Detail |
|---|---|
| T6.1 | Extract `ResultsPage` into `workspace/` components (margin, answer, mark, ledger strip, detail sections). No behaviour change in this task — a pure extraction, verified by tests. |
| T6.2 | Margin list: topic groups, marks as glyphs, triage rule + icon, keyboard `J/K`, scroll-into-view. |
| T6.3 | Answer pane: document mode (toolbar, zoom, page x/y, keyboard) and the **no-document state** (extracted answer, Classroom link, retention link). |
| T6.4 | Mark pane: scheme excerpt, mark control (segmented + number), note, `Save & next`, "Why this mark". |
| T6.5 | Ledger strip: total, %, band, `12 of 28 checked`, flagged count, Prev/Next; sticky. |
| T6.6 | Detail sections below (remark, double-check, versions, recovery) as `h2` sections with `h3` panels. |
| T6.7 | Responsive: tabs under 1024 px; marks set on the Mark tab return to Questions with the row updated. |
| T6.8 | Shortcut sheet (`?`) and undo (`Z`). |

**Risks:** this is where marks are set. Any regression changes grades. Mitigation: T6.1 before any
visual work; every mark action keeps its existing endpoint and payload; the regression checklist's
marking block is run after every task.

**Acceptance:** every mark action produces the same API call and the same stored value as today;
`44/49 · 89.8%` matches the old header for the same result; keyboard-only marking of a 5-question
paper is possible; with retention `never` the workspace still shows the extracted answer and a
route to the original; with retention `always` the document renders and zooms.

---

## Phase 7 — Results, reports, usage (4 sessions)

**Findings:** RD-015, RD-025, RD-026, RD-027, RD-028.

Tasks: Results table + column chooser + range pagination (T7.1); Students percentage column (T7.2,
shared table); Reports builder/preview split with one action set (T7.3); report deep-link
breadcrumb (T7.4); Usage limits + summary strip (T7.5).

**Acceptance:** 169 results navigable by page, sortable by score and date, with the full assignment
name reachable; the report preview renders the same content at ≤ 2,500 px per section with anchors;
export produces byte-identical PDFs to today's for the same inputs (compare hashes for one report).

---

## Phase 8 — Review, Classes, Classroom (3–4 sessions)

**Findings:** RD-021, RD-027 (tracking), RD-049.

Tasks: Review table with filters/sort, row expansion, bulk accept + undo (T8.1); scheme-review
detail route + single primary decision (T8.2); Classroom connection stepper + tracking table with
filters and pagination (T8.3); Classes cards with real empty states (T8.4); Assignments table +
detail, bulk correct confirmation with count (T8.5); Students profile tabs (T8.6).

**Acceptance:** accepting a result is reversible for 8 s and restores the row on failure; a
mark-scheme decision names the affected results before it applies; 78 students are reachable with
filter + pagination; no destructive action lacks `ConfirmDialog`.

---

## Phase 9 — System screens and flows (3 sessions)

**Findings:** RD-008, RD-019 (pages), RD-024.

Tasks: Settings two-column frame + danger zone (T9.1); AI page without nested header, editable
unique key labels (T9.2); Storage export/import diff (T9.3); Privacy sections (T9.4); `GatedPage`
for Diagnostics/Prompts/Updates/Multi-School (T9.5); Audit empty state as the canonical pattern
(T9.6); Sign-in redesign with error summary (T9.7); Setup wizard → 4 steps (T9.8).

**Acceptance:** every high-risk setting is confirmed and described; a signed-out developer sees a
page with a reason and a way forward; sign-in errors appear in a summary **and** inline.

---

## Phase 10 — Performance, accessibility, polish, gate (3 sessions)

**Findings:** RD-042, RD-043, RD-044, RD-046, RD-047, RD-048 + verification of all P0/P1.

| Task | Detail |
|---|---|
| T10.1 | Consolidate pollers into one status store + SSE; pause on `document.hidden` (RD-042). |
| T10.2 | Bundle: vendor chunk, route preload on hover/focus, budget check in CI (entry ≤ 90 KB gzip, CSS ≤ 60 KB). |
| T10.3 | Full accessibility pass: contrast script, focus walk, target sizes, heading outline per route, dialogs, live regions, 200 % zoom, 320 px reflow, reduced motion. |
| T10.4 | Cross-browser: Chromium, Firefox, WebKit (Playwright) on the smoke matrix. |
| T10.5 | Performance: measure INP on Results (sort/filter), workspace question switching, and upload; compare to baseline. |
| T10.6 | Delete the old stylesheet sections and aliases; remove dead components; update `docs/`. |

**Gate:** every item in
[regression-accessibility-performance-checklist.md](regression-accessibility-performance-checklist.md)
passes; all P0/P1 findings closed; no new finding above P2 opened during implementation.

---

## Dependency graph

```
P0 baseline
 └─ P1 tokens/type
     └─ P2 primitives
         └─ P3 shell & IA
             ├─ P4 Home
             ├─ P5 Correct/jobs
             │    └─ P6 workspace (needs FileDrop? no; needs DataTable & Page frame)
             ├─ P7 Results/reports (needs DataTable)
             ├─ P8 Review/Classes (needs DataTable, ConfirmDialog)
             └─ P9 System (needs Field, Menu, GatedPage)
                  └─ P10 gates (needs everything)
```

`P6` depends on `P2` (MarkGlyph, LedgerStrip primitives) and `P3` (page frame + tabs), not on `P5`.

---

## Effort, sequencing and rollback

| Phase | Sessions (est.) | Reversible by |
|---|---:|---|
| 0 | 0.5 | n/a |
| 1 | 2 | reverting `tokens.css` + font files |
| 2 | 4–5 | aliases keep old components callable until Phase 10 |
| 3 | 3–4 | route map is data; revert the shell file |
| 4–9 | 3–5 each | each page is one commit; old page component remains until its phase is green |
| 10 | 3 | n/a |

**Rollback strategy:** page-level commits, not a big-bang merge. The old stylesheet stays imported
until Phase 10 so any page not yet migrated keeps working. Feature-flag the shell only if a route
proves unreachable (`?shell=legacy`), and remove the flag at the end of Phase 3.

---

## Definition of done (whole programme)

1. All 49 findings closed or explicitly deferred with a written reason.
2. One `<h1>` per route; zero unnamed controls; zero inline styles; zero raw hex in components.
3. Contrast: every text pair ≥ 4.5:1; UI boundaries ≥ 3:1, in both themes.
4. Keyboard-only completion of: upload → queue → review → mark a question → export.
5. 320 px reflow and 200 % zoom usable on every route.
6. `npm run build`, unit tests, UI smoke and the new a11y script all green.
7. Entry JS ≤ 90 KB gzip (baseline 77 KB); CSS ≤ 25 KB gzip (baseline 12 KB); no new runtime
   dependency except fonts (and `jsdom` as a dev dependency).
8. Functional parity verified against the checklist, including marking numbers, PDF export and
   Classroom publishing.
