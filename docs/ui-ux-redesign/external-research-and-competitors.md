# External Research and Competitor Analysis

**41 primary sources were read for this phase.** Every row below records what the page actually
said, how it applies to Math Checker, and — where relevant — what could **not** be verified.
Fabricated citations are worse than none, so unreadable pages are marked as such and their claims
are limited accordingly.

Research was collected with live page reads on 2026-10-20. Material 3, Apple HIG, PDF.js and
several product pages expose only metadata or shells to automated reading; those rows say so.

---

## A. Usability and error handling (Nielsen Norman Group)

| # | Source | URL | What it says (as read) | Applies to |
|---|---|---|---|---|
| 1 | *10 Usability Heuristics Applied to Complex Applications* — NN/g | https://www.nngroup.com/articles/usability-heuristics-complex-applications/ | Prevent errors, offer undo/backtracking, keep options visible instead of relying on memory. For waits over 10 s, "please wait" and a loop animation are inadequate — say what is happening and how far along it is. | RD-016 (upload with no progress), RD-018 (raw errors), RD-020 (empty workspace), RD-023 |
| 2 | *Error-Message Guidelines* — NN/g | https://www.nngroup.com/articles/error-message-guidelines/ | Place the error next to the field, state the problem in plain language, suggest a fix, preserve input, use text not just colour, and do not interrupt before the user has had a chance to finish. | RD-018, RD-019; the Correct form (RD-016) |
| 3 | *Data Tables: Four Major User Tasks* — NN/g | https://www.nngroup.com/articles/data-tables/ | Design tables around finding, comparing, editing and acting on records; use human-readable identifiers, place related columns together, make filters discoverable, keep structure visible. | RD-025 (redundant/unreadable columns), RD-026, RD-027 |
| 4 | *Dashboards: Making Charts and Graphs Easier to Understand* — NN/g | https://www.nngroup.com/articles/dashboards-preattentive/ | Dashboards are for at-a-glance decisions, not exploration. Position and length (bars, lines) are judged far more accurately than area or angle. | Dashboard redesign: keep bar/line as primary; the donut for job state becomes a stacked bar |
| 5 | *Memory Recognition and Recall in User Interfaces* — NN/g | https://www.nngroup.com/articles/recognition-and-recall/ | Recognition beats recall: keep functions and relevant information visible; use menus, histories and contextual cues. | RD-011 (24 hidden nav items), RD-024 (13 settings sections), command palette as the recall aid |

**Synthesis for Math Checker:** the current UI already *tries* to be honest (real states, no fake
data), but it fails NN/g's error guidance in the place it matters most — the attention feed prints
provider payloads and a failed request prints `Failed to fetch`. The redesign's error model in
`component-and-interaction-system.md §7` is derived directly from sources 1–2.

---

## B. Accessibility — WCAG 2.2

| # | Criterion | URL | Requirement as read | Applies to |
|---|---|---|---|---|
| 6 | 1.4.3 Contrast (Minimum), AA | https://www.w3.org/WAI/WCAG22/Understanding/contrast-minimum.html | **4.5:1** for normal text, **3:1** for large text. | **Fails today:** `--text-faint` 3.09:1 light / 3.66:1 dark; white on `#7c6af2` 4.04:1 (RD-004, RD-005) |
| 7 | 1.4.11 Non-text Contrast, AA | https://www.w3.org/WAI/WCAG22/Understanding/non-text-contrast.html | UI components and meaningful graphics need **3:1** against adjacent colours. *(Page read only partially — extraction returned a late example.)* | Chart series, focus rings, input borders, toggle tracks |
| 8 | 2.5.8 Target Size (Minimum), AA | https://www.w3.org/WAI/WCAG22/Understanding/target-size-minimum.html | **24×24 CSS px** minimum, with spacing/size exceptions. | Passes today (smallest 30×30); the redesign targets 32–44 px (RD-009) |
| 9 | 2.4.11 Focus Not Obscured (Minimum), AA | https://www.w3.org/WAI/WCAG22/Understanding/focus-not-obscured-minimum.html | A focused control must not be **entirely** hidden by author content — sticky headers/footers and overlays are the usual culprits. | New sticky filter bars and the sticky table header must not cover a focused row action |
| 10 | 2.4.7 Focus Visible, AA | https://www.w3.org/WAI/WCAG22/Understanding/focus-visible.html | Keyboard focus must be visibly indicated, not time-limited. | Already present (`:focus-visible` 2 px + offset); must survive the token rewrite |
| 11 | 3.3.1 Error Identification, **A** | https://www.w3.org/WAI/WCAG22/Understanding/error-identification.html | Identify the item and describe the error **in text**. | RD-018 (a raw payload is not an identification of what the teacher must do) |
| 12 | 3.3.3 Error Suggestion, AA | https://www.w3.org/WAI/WCAG22/Understanding/error-suggestion.html | If a correction is known, suggest it. | Upload validation: say which file is wrong and why, not just "failed" |
| 13 | 1.4.10 Reflow, AA | https://www.w3.org/WAI/WCAG22/Understanding/reflow.html | Usable at **320 CSS px** without 2-D scrolling, except content that genuinely needs 2-D (data tables are the named example) — cells must still reflow. | The workspace tab-collapse at 390 px is the right model; tables must stack or scroll deliberately (RD-040) |
| 14 | 1.4.4 Resize Text, AA | https://www.w3.org/WAI/WCAG22/Understanding/resize-text.html | Text must resize to **200%** without loss of content or function. | All type is `px`/`vw` clamp with no `rem` (RD-034): the redesign moves to rem with a 16 px base |
| 15 | *(method note)* | — | WCAG 3.3.1 is **Level A**, not AA; Reflow exempts genuine 2-D content such as tables. | Corrected in the roadmap's acceptance criteria |

**Measured contrast (this codebase, not a guess):**

| Pair | Ratio | Verdict |
|---|---:|---|
| `--text-faint` #8a93a5 on #ffffff (light) | **3.09:1** | fails AA text |
| `--text-faint` #6b7690 on #141e33 (dark) | **3.66:1** | fails AA text |
| `--text-faint` #6b7690 on #0c1220 (dark page) | 4.11:1 | fails AA text |
| white on `--accent-2` #7c6af2 (gradient end) | **4.04:1** | fails AA text |
| `--text-dim` #566178 on white | 6.22:1 | passes |
| `--warn` #a2620a on `--warn-soft` over white | 4.29:1 | marginal fail |
| `--ok` #147a52 on white | 5.34:1 | passes |
| `--err` #c22f45 on white | 5.55:1 | passes |
| dark `--text` #eceff7 on #141e33 | 14.45:1 | passes |

---

## C. Design systems (tokens, density, navigation)

| # | Source | URL | What it says | Applies to |
|---|---|---|---|---|
| 16 | *Color roles* — Material 3 | https://m3.material.io/styles/color/roles | Colour is assigned by **role** (surface, on-surface, primary-container…) rather than by hue names. *(Metadata only — body not exposed to the reader.)* | The redesign names roles (`--surface-canvas`, `--ink-primary`, `--mark-awarded`), not colours |
| 17 | *State layers* — Material 3 | https://m3.material.io/foundations/interaction/states/state-layers | Interaction states are systematic overlays, not ad-hoc colours. *(Metadata only.)* | One state model for buttons/rows/inputs (`component-and-interaction-system.md §2`) |
| 18 | *Applying elevation* — Material 3 | https://m3.material.io/styles/elevation/applying-elevation | Elevation expresses relative distance along z. *(Metadata only.)* | Two elevations only: inline surfaces (border, no shadow) and overlays (shadow) |
| 19 | *Design tokens* — Fluent 2 | https://fluent2.microsoft.design/design-tokens | Distinguishes **global** tokens from **semantic alias** tokens so themes don't hard-code values. | Directly fixes RD-035/RD-036: one token layer, one alias layer |
| 20 | *Layout* — Fluent 2 | https://fluent2.microsoft.design/layout | 4 px spacing base; spacing expresses relationships; avoid disorienting density. | Replaces the 936 hardcoded margins (RD-031) with a 4 px ramp |
| 21 | *Info label — usage* — Fluent 2 | https://fluent2.microsoft.design/components/web/react/core/infolabel/usage | The smallest label size is only for well-separated or user-chosen compact density. | 11 px uppercase table headers today; new minimum is 12 px with 4.5:1 |
| 22 | *Sidebars* — Apple HIG | https://developer.apple.com/design/human-interface-guidelines/sidebars/ | A sidebar navigates app areas or top-level collections. *(Metadata only.)* | Six destinations, not 24 items |
| 23 | *Designing for macOS* — Apple HIG | https://developer.apple.com/design/human-interface-guidelines/designing-for-macos/ | macOS is framed as a spacious environment for in-depth work. *(No density specifics exposed.)* | Long-session comfort: calmer surfaces, fewer competing accents |
| 24 | *Question pages* — GOV.UK Design System | https://raw.githubusercontent.com/alphagov/govuk-design-system/main/src/patterns/question-pages/index.md | Start with one question per page; group only when research supports it. *(Site timed out; official repo source read.)* | The Correct flow becomes 2 steps (files → details) instead of one dense 3-column form |
| 25 | *Error summary* — GOV.UK Design System | https://raw.githubusercontent.com/alphagov/govuk-design-system/main/src/components/error-summary/index.md | Show a summary at the top, move focus to it, link each item to the field, and repeat inline. | Upload/validation errors: summary + inline (RD-016) |
| 26 | *Structuring forms* — GOV.UK Service Manual | https://www.gov.uk/service-manual/design/form-structure | One thing per page helps focus, mobile use and error recovery. | Settings split into sections with their own URLs |

---

## D. Frontend performance

| # | Source | URL | What it says | Applies to |
|---|---|---|---|---|
| 27 | *Best practices for fonts* — web.dev | https://web.dev/articles/font-best-practices | `font-display: optional` avoids swap-induced layout shift; `swap` shows text sooner but can shift. | RD-033/RD-045: self-host one subset with `swap` + `size-adjust` fallback metrics |
| 28 | *Cumulative Layout Shift* — web.dev | https://web.dev/articles/cls | Target ≤ 0.1 at p75, measured separately for mobile and desktop. | Storage's timestamped button label and late-loading cards are shift risks |
| 29 | *Interaction to Next Paint* — web.dev | https://web.dev/articles/inp | Good = ≤ 200 ms at p75 across click/tap/keyboard. | 50-row tables with inline handlers and 450 ms width transitions (RD-044) |

Baseline measured for this app: **719 KB JS total, 246 KB entry (77 KB gzip), 59.6 KB CSS
(12 KB gzip), no webfonts, no images.** Performance is a strength; the redesign must not spend it.

---

## E. Assessment, grading and document products

| # | Product / page | URL | What the source documents | What is good | Limitation of what was verified |
|---|---|---|---|---|---|
| 30 | **Gradescope** — AI-assisted grading & answer groups | https://guides.gradescope.com/hc/en-us/articles/24838908062093-AI-assisted-grading-and-answer-groups | Group-grading view: left = group name, submission count, sample submission; right = progress and rubric; move to next ungraded group. | Answer and scoring controls in one workflow; progress is explicit; the next unit of work is one action away. | Documented for **group grading on fixed-template PDFs**, institutional licence; not proof of every Gradescope view. |
| 31 | **Gradescope** — student feedback view | https://guides.gradescope.com/hc/en-us/articles/21856421392525-Understand-Your-Submission-Feedback-and-Grades | Students open a question to see rubric items and comments; can download a marked-up copy. | Feedback is anchored to the question, and there is an exportable marked copy. | Student-facing guidance, not the instructor screen. |
| 32 | **Turnitin Feedback Studio** — product page | https://www.turnitin.com/products/feedback-studio/ | QuickMarks, pinned comments, rubrics, grading forms. | Reusable comments + criterion scoring + free-form feedback coexist. | Product overview, not an interface walkthrough. |
| 33 | **Turnitin** — new grading experience | https://guides.turnitin.com/hc/en-us/articles/35446790262541-Navigating-the-new-grading-and-feedback-experience | Collapsible feedback side panel (QuickMarks, summary comments), in-margin and on-document feedback, integrity layers, grading panel with rubric and "Apply to grade". | Separates *where* feedback lives (margin vs summary) from *how* the grade is set; rubric links to comments. | Textual guide; live workspace not tested. |
| 34 | **Google Classroom** — gradebook | https://support.google.com/edu/classroom/answer/9199710?hl=en | Teachers view submissions from Grades, enter draft grades, return work; grade status has colours. | Explicit separation of *entering* a grade and *returning* work. | Help article, not a rendered screen. |
| 35 | **Google Classroom** — grade with a rubric | https://support.google.com/edu/classroom/answer/9335428?hl=en&co=GENIE.Platform%3DDesktop | The rubric lives in the **right column** while the student's file is open; ratings update the score; the total can be overridden. | The single most transferable pattern for Math Checker's workspace. | Does not establish behaviour at small viewports. |
| 36 | **PDF.js** — demo | https://mozilla.github.io/pdf.js/web/viewer.html | Viewer shell with zoom/search/page controls. | Reference for a document pane that must not steal the page. | **Could not inspect the working viewer** — the demo returned an empty shell ("0%"). |
| 37 | **PDF.js** — FAQ | https://github.com/mozilla/pdf.js/wiki/frequently-asked-questions | Reading-focused viewer: keyboard navigation, zoom, search, print, download, outline sidebar; annotations rendered, subset editable. | Confirms a document pane can be keyboard-operable and light. | Documentation, not a test of this app's integration. |
| 38 | **Adobe Acrobat web** — PDF editor | https://www.adobe.com/acrobat/online/pdf-editor.html | Comments, sticky notes, text boxes, freehand, highlights, share/download. | Anchored feedback and visual marking are separate tools. | Marketing page; no live editor session inspected. |
| 39 | **KaTeX** — options | https://github.com/KaTeX/KaTeX/blob/main/docs/options.md | Default output is HTML **plus MathML** for accessibility. | If maths notation ever needs rendering, accessible output is the default. | Documentation, not a screen-reader test. |
| 40 | **KaTeX** — browser usage | https://github.com/KaTeX/KaTeX/blob/main/docs/browser.md | The `fonts/` directory must stay beside the CSS; `font-display` is configurable; fonts can be preloaded. | Concrete plan for offline math fonts (constraint C8). | — |
| 41 | **MathJax** — accessibility / fonts | https://docs.mathjax.org/en/v4.1/basic/accessibility.html · https://docs.mathjax.org/en/v4.1/output/fonts.html | Keyboard-accessible expressions, speech/exploration/Braille; 11 fonts, configurable paths, dynamically loaded ranges make typesetting async. | Shows accessible maths is a system decision, not a stylesheet detail. | Capabilities, not a test of a specific integration. |

> **Note on maths content:** Math Checker currently ships **no** TeX/KaTeX/MathJax dependency, and
> question/feedback strings are plain text. The blueprint therefore does **not** add a math
> renderer; it specifies a serif face with real maths-friendly figures for feedback text, and
> reserves KaTeX as an optional, offline-vendored addition **only if** future content contains
> TeX. Adding it now would be a dependency with no content to render.

---

## F. Competitor patterns — what to adopt, adapt, or reject

| Pattern observed | Adopt | Adapt for Math Checker | Reject |
|---|---|---|---|
| **Answer + rubric side by side** (Gradescope, Classroom) | Yes — the core of the new workspace | Left = question index, centre = answer, right = mark + rationale; bottom = running total | Fixed 3-column at all widths (Math Checker collapses to tabs under 1024 px) |
| **Progress as "12 of 28 graded"** (Gradescope) | Yes | Pin "12 of 28 checked" in the workspace header, not just a bar | Percentage-only progress with no unit |
| **Reusable comments / QuickMarks** (Turnitin) | Partly | Reuse the existing `confidence_reasons` text as structured chips; do not invent a comment library | A full annotation engine inside the marking workspace |
| **Draft grade vs returned grade** (Classroom) | Yes | The existing "provisional band" and delivery checklist become a two-state model: *marked* → *returned* | Automatic publishing (constraint C4) |
| **Document viewer chrome** (PDF.js) | Yes | A slim document toolbar: page x/y, zoom, fit, open original | Heavy annotation toolbars |
| **Marketing-style hero metrics** (generic SaaS) | No | — | Large gradient KPI hero that repeats numbers already in the table below |
| **Glassmorphism / blurred cards** | No | — | Blur on every surface; it costs paint and reduces text contrast |

## G. Design principles extracted (used in `design-directions.md`)

1. **Separate the two registers.** Operational/overview surfaces (dashboard, tasks, automation)
   and editorial/reading surfaces (marking, feedback, reports) need different density and type —
   not one card style everywhere.
2. **The unit of work must be visible.** A teacher's unit is *one paper* or *one question*, so
   progress, position and "next" must always be on screen.
3. **Truth over decoration.** No invented numbers, no decorative gradients behind data, no
   animation that implies progress the system does not have.
4. **Errors are content.** Every failure gets a plain-language sentence, a cause where known, and
   one action — never a payload.
5. **Recognition over recall.** Six destinations, visible section tabs, a command palette, and
   recent items — instead of 24 sidebar entries and memorised URLs.
6. **Accessibility is a constraint, not a phase.** Contrast, focus, target size and reflow are
   acceptance criteria in every phase (see the checklist).

## H. Sources consulted and found unusable

| Attempted | Result |
|---|---|
| GOV.UK Design System live pages (question pages, error summary) | Timed out; official repository markdown used instead (rows 24–25) |
| Gradescope rubric guide | **403 Forbidden** — not cited |
| PDF.js live demo | Returned an empty viewer shell; only the FAQ supports viewer claims |
| Material 3 / Apple HIG bodies | HTTP 200 but metadata only; claims kept deliberately narrow |
| Turnitin / Adobe / Classroom live products | No account access; only public documentation and marketing pages |
