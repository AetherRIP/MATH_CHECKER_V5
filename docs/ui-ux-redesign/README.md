# Math Checker — Full UI/UX Redesign Research (Phase 0)

**Status:** research and planning only. No application code, asset, route, dependency or
configuration was modified. Date: **2026-10-20**. App version observed: **v5.2.1 · b20261013-rc1**.

This package is a complete, evidence-based blueprint for a **ground-up redesign** of the
Math Checker web interface. It is written for the coding agent that will implement the
redesign, not for a design showcase. Every page is accounted for, every shared component is
inventoried, every recommendation is tied to observed evidence and a `RD-xxx` finding ID.

---

## 1. Scope and stance

Math Checker is a **local, single-teacher desktop-style web application** that marks Edexcel
IGCSE maths homework with the teacher's own Gemini API keys and optionally pulls submissions
from Google Classroom. It is not a SaaS product and has no marketing site, no sign-up funnel,
no billing, and no multi-tenant onboarding.

Two layers were separated deliberately:

| Layer | What it means here | Status |
|---|---|---|
| **A. Functional foundation** | 24 base routes, 6 deep-link detail routes, 4 legacy aliases, 4 non-route flow surfaces; 199 completed corrections; 169 results; 78-student classes; live SSE job feed; PDF pipelines; Classroom sync; report PDF export | Preserved. Every behaviour named in this package must keep working. |
| **B. Visual and interaction design** | Layout, navigation architecture, typography, colour, density, components, motion, responsive behaviour | **Free to be replaced completely.** |

The current UI is treated as a *functional specification*, not as a design reference. Where
this package says "replace", it means replace — not restyle.

## 2. How the evidence was gathered

1. **Static inspection** of `frontend/src` (58 files — 45 `.tsx`, 11 `.ts`, 2 `.css` — 27,912 lines), `index.html`,
   `vite.config.ts`, `tsconfig.json`, both stylesheets, `frontend/tests`, `frontend/scripts`.
2. **Live inspection** of the running application at `http://127.0.0.1:8642` (backend healthy,
   real data: 169 results, 199 completed jobs, 78-student class) in a real Chromium tab at
   **1440×900**, **390×844** and during a genuine backend restart.
3. **DOM measurement** — for every rendered route: heading outline, component counts, scroll
   height, horizontal overflow, accessible names, labelled controls, target sizes, computed
   text/background contrast.
4. **Token arithmetic** — WCAG contrast ratios computed from the actual hex values in
   `frontend/src/styles.css`.
5. **External research** — 41 primary-source pages read (W3C/WAI, NN/g, Material 3, Fluent 2,
   GOV.UK, Apple HIG, web.dev, Gradescope, Turnitin, Google Classroom, PDF.js, Adobe, KaTeX,
   MathJax).
   Sources, what was actually readable, and what could not be verified are listed in
   [external-research-and-competitors.md](external-research-and-competitors.md).
6. **Tooling baseline** — `node --test "tests/*.test.ts"` from `frontend/`: **89 tests, 86 pass,
   3 skipped** (they need a live app at `:8720`), 0 fail. `ui_smoke.mjs` **cannot run**:
   `jsdom` is not a declared dependency (RD-046).

Screenshots were captured during the audit and kept **local only** in
`screenshots/`: the app was inspected against the teacher's live data set, so the
images show real student names and are git-ignored. They are evidence for this
review, not repository content.

## 3. Document index

| # | Document | Contents |
|---|---|---|
| 0 | **README.md** (this file) | Scope, method, index, headline conclusions, counts |
| 1 | [product-and-repository-analysis.md](product-and-repository-analysis.md) | Architecture, data flow, real capabilities, hard constraints, functional preservation list |
| 2 | [complete-page-inventory.md](complete-page-inventory.md) | All 30 screens + 4 aliases + 4 flow surfaces, with purpose, components, inspection status |
| 3 | [external-research-and-competitors.md](external-research-and-competitors.md) | 28 sources with URLs, findings, applicability, and what could not be verified |
| 4 | [design-directions.md](design-directions.md) | Three distinct directions, scored comparison, the chosen one and why |
| 5 | [complete-redesign-blueprint.md](complete-redesign-blueprint.md) | Per-page redesign specification for all 30 screens |
| 6 | [component-and-interaction-system.md](component-and-interaction-system.md) | New component set, variants, states, keyboard and micro-interactions |
| 7 | [design-system-specification.md](design-system-specification.md) | Tokens, type, colour, spacing, grid, breakpoints, elevation, motion |
| 8 | [redesign-findings-register.md](redesign-findings-register.md) | 49 findings with IDs, evidence, priority, risk, validation |
| 9 | [implementation-roadmap.md](implementation-roadmap.md) | 10 phases, dependency-aware tasks, per-phase acceptance criteria |
| 10 | [regression-accessibility-performance-checklist.md](regression-accessibility-performance-checklist.md) | Functional preservation, a11y, responsive, performance and acceptance checklists |

## 4. Headline conclusions

**The product is stronger than its interface suggests.** The engineering underneath is
unusually careful: keyboard-trapped overlays, `aria-live` job progress, reduced-motion rules,
zero-dependency SVG charts with text summaries, honest "no data" chart states, a reconnecting
banner instead of a wall of failed requests, route-level code splitting with a stale-chunk
recovery path, and a privacy-first retention default. The redesign should *keep* that
discipline and replace the layer above it.

**The interface has drifted, and the drift is structural, not cosmetic.** Evidence:

- **24 navigation items in 5 groups**; at 900 px viewport height the last two groups sit below
  the sidebar fold with no scroll affordance (RD-011).
- **Three different page-frame patterns** (`DashboardPage` hand-builds its header, `PdfCorrectionPage`
  uses a `Section` card, 24 other files use `PageHeader`) produce different heading levels for
  the same job. `Correct a PDF` has **no `<h1>` at all** (RD-002, RD-013).
- **936 inline `style={{…}}` objects** across 36 files bypass the existing 8-step spacing scale
  with hardcoded `18/12/10/8/6` values (RD-031).
- **8 button variants** and **109 raw `<button>`** elements against 234 `<Button>` (RD-032).
- **`Inter` is declared but never loaded** — no `@font-face`, no webfont link, no Inter on the
  test machine, so every measurement in the "Slate & Indigo" system renders in Segoe UI (RD-033).
- **`--text-faint` measures 3.09:1 in light and 3.66:1 on dark surfaces** — used for timestamps,
  table headers, hints and empty-state copy (RD-004). White text on the primary gradient's
  `#7c6af2` end measures **4.04:1** (RD-005).
- **Three icon-only buttons have no accessible name** at all (RD-001).
- **Raw provider JSON is printed in the teacher's attention feed** — `PERMISSION_DENIED
  {'error': {'code': 403…}}`, `DEADLINE_EXCEEDED … 504`, truncated mid-sentence — and a brief
  backend restart replaces page content with the bare string `Failed to fetch` (RD-018, RD-019).
- **The marking workspace's centre pane is empty by default** ("The original work is not stored
  on this device") while occupying ~380 px of a 1,128 px workspace (RD-020).
- **`Automatic Correction` is 24,493 px tall**; `Settings` is 4,357 px of 17 unrelated sections
  (RD-023, RD-024).

**The redesign is therefore a re-architecture of navigation, page composition and the marking
workspace — not a theme change.** The visual language proposed here ("**Chalk & Ink**") is
paper-derived, typographically led, and deliberately unlike the current glass/indigo SaaS look,
which the contrast data shows is also the least accessible part of the system.

## 5. Completeness at a glance (measured, not estimated)

| Metric | Count |
|---|---:|
| Base routes in `VALID_ROUTES`/`App.tsx` | **24** |
| Deep-link detail routes | **6** |
| Legacy aliases (`report`, `keys`, `workers`, `resultsPage`) | **4** |
| Non-route flow surfaces (sign-in, setup wizard, attention drawer, reconnect banner) | **4** |
| Screens rendered and DOM-measured | **29 / 30** |
| Screens visually inspected via screenshots | **5** (+1 at 390 px) |
| Screens source-only | **3** (sign-in, setup wizard, `scheme-review/{id}`) |
| Shared UI components inventoried | **26** (+2 helpers, 58 icons, 9 charts, 12 domain panels) |
| Findings recorded | **49** (3 × P0, 17 × P1, 21 × P2, 8 × P3) |
| External sources actually read | **41** |

## 6. What is explicitly *not* in scope

- No code, CSS, asset, route, dependency or configuration changes. `git diff` on
  `frontend/`, `backend/`, `agent/`, `main.py`, `remote.py` is empty for this work.
- No new product features are proposed **as existing functionality**. Where the research
  suggests a capability the product lacks (e.g. bulk marking actions, document retention
  choice at upload time), it is labelled **"optional product proposal"** and kept out of the
  required phases.
- No change to marking algorithms, verification passes, retries, model selection, PDF
  processing, compression, storage or retention semantics.

## 7. How to use this package

1. Read **§2–§3 of the product analysis** to understand what must not break.
2. Read **design-directions.md §5** for the chosen direction, then **design-system-specification.md**
   for tokens — implementation starts there (Phase 1 of the roadmap).
3. Build the shell and components (Phases 2–3) before touching pages; the current pages assume
   the old primitives, so page work started earlier will be re-done.
4. Use the **findings register** as the definition of "done" for each phase; the roadmap maps
   every finding to a phase.
5. Use the **checklist** as the gate. It includes the commands that already exist, and the ones
   that must be created (RD-046) before the redesign can be considered verified.
