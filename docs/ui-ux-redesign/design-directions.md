# Design Directions

Three genuinely different interface philosophies were developed **before** choosing one. They
differ in navigation model, density, type, colour logic and the composition of the marking
workspace — not in accent hue.

Each is evaluated against the product's real constraints (10-hour marking sessions, 78-student
classes, 169 results, an offline laptop, WCAG 2.2 AA, and the preservation list in
`product-and-repository-analysis.md §4`).

---

## Direction A — **The Marking Desk** (chosen, product name: *Chalk & Ink*)

**Philosophy:** the interface is a teacher's desk, not a dashboard. Paper, margin, ink, and a
running tally. The product's centre of gravity is one paper at a time; everything else is a
ledger you consult between papers.

**Visual identity:** warm paper canvas (`#FBF9F6`), near-black ink (`#16181D`), a single
**verdigris** accent (`#0F6B5C`) for interaction, and a restrained **mark red** (`#B42318`)
reserved for awarded/lost marks and destructive actions. No gradients on actions, no glass.
Typography carries the identity: **IBM Plex Sans** for UI, **IBM Plex Serif** for student work,
feedback and report prose, **IBM Plex Mono** for identifiers and log lines — one superfamily,
self-hosted, offline (constraint C8).

**Navigation:** six destinations in a 72 px rail + labels: *Home · Correct · Review · Results ·
Classes · System*. Each destination owns a horizontal **section tab** row for its pages (e.g.
Results → Results / Reports). Detail views keep the parent tab lit and use a breadcrumb.

**Layout:** a 12-column grid with a **56 px margin column** on reading surfaces that holds
question numbers and marks — the visual signature of the system, and functionally the place a
teacher's eye already goes.

**Marking workspace:** three zones — margin/question index · answer · mark panel — plus a fixed
**ledger strip** at the bottom with `44 / 49 · 89.8% · band 8 · 12 of 28 checked`.

**Signature details:** index-tab section navigation; marks set as typographic glyphs (`3/3`,
`0/3`, `½`) rather than coloured pills; a "stamp" provenance label (bordered, letter-spaced);
the running ledger strip.

**ASCII sketch — Home**

```
┌──────┬──────────────────────────────────────────────────────────────┐
│ Home │  Today                                    [Search  ⌘K] [◐]  │
│ Corr │ ──────────────────────────────────────────────────────────── │
│ Rev  │  Good afternoon. 5 results need you.                         │
│ Res  │                                                              │
│ Cls  │  ┌─ Needs you ──────────────┐ ┌─ Today ──────────────────┐   │
│ Sys  │  │ 5 review · 2 failed      │ │ 0 marked   0 queued      │   │
│      │  │ › Ch2L1 (3)  12m         │ │ ▁▂▅▃▁▂  last 30 days     │   │
│      │  │ › Q11 scheme (1)         │ └──────────────────────────┘   │
│      │  └──────────────────────────┘ ┌─ Classes ────────────────┐   │
│      │  ┌─ Next up ────────────────┐ │ 10A 12/18  10B 15/18     │   │
│      │  │ Ch2L3 · 15 to mark  →    │ └──────────────────────────┘   │
│      │  └──────────────────────────┘                                │
└──────┴──────────────────────────────────────────────────────────────┘
```

**Strengths:** distinct from every generic grading SaaS; serif feedback is genuinely easier to
read for long text; the margin model matches the domain; calm surfaces suit 3-hour sessions;
mobile collapses naturally (margin becomes a top summary).

**Risks:** skeuomorphism if overdone (paper textures, faux shadows) — explicitly forbidden;
serif must not be used for UI labels (readability at 12–13 px).

---

## Direction B — **Control Room**

**Philosophy:** an operations console for someone who runs 50 papers a day. Maximum information
per pixel, keyboard-first, dark-first, status everywhere.

**Visual identity:** graphite (`#0B0F14`), one electric cyan accent, monospaced numerals and
identifiers, 1 px rules instead of cards, 28 px table rows, dense charts.

**Navigation:** a command bar plus a 48 px icon rail; everything is a command (`⌘K` → "review
queue", "retry job 1234"). No section tabs; the palette is the map.

**Marking workspace:** a full-bleed split — 8 px document gutter, question rail as a virtualised
list, marks as an editable numeric grid; nothing is "pretty", everything is one keystroke away.

**Strengths:** superb for bulk operations, job triage and keyboard users; excellent perceived
performance; the density matches 78-row tables.

**Weaknesses against this product:** the app's users are teachers marking homework, not operators;
dark-first is hostile to long reading of student prose; the aesthetic is the visual cliché of
developer tooling (Linear/Vercel/Datadog) and would be *less* distinctive than the current UI;
onboarding and first-run setup would feel cold; and it duplicates the current product's mistake
of treating everything as an operational surface.

**Verdict:** rejected as the product direction — **but its data discipline is adopted** for the
*Ledger* register (compact tables, tabular numerals, keyboard shortcuts, command palette).

---

## Direction C — **Studio**

**Philosophy:** editorial calm. One idea per screen, generous whitespace, very large type, a
guided wizard for every multi-step task, cards floating in a wide canvas.

**Visual identity:** off-white canvas, one deep ink, a warm ochre accent, 20–24 px body type,
28 px radii, softly elevated cards, illustrations in empty states.

**Navigation:** a top bar with 4 words + a "more" menu; pages are long-form documents with a
sticky in-page table of contents.

**Marking workspace:** a vertical stack — question index as a chapter list, one question per
screen, mark controls as large segmented buttons, "Next question" as the primary action.

**Strengths:** the best onboarding and mobile experience of the three; extremely low cognitive
load; excellent for a teacher's first week.

**Weaknesses:** it hides the ledger the product depends on (169 results, 78 students, 24k px of
automation). One-question-per-screen adds ~28 navigations per paper, a direct violation of
constraint C10 (no layout that requires more interaction per paper than today). Whitespace at
the cost of comparison is the wrong trade for a marking tool.

**Verdict:** rejected as the product direction — **but its onboarding stance is adopted** for
first-run setup and empty states (one decision per step, plain language, no dead ends).

---

## 5. Comparison

Weights reflect the product, not taste: *task fit* and *long-session comfort* are weighted
highest; *distinctiveness* matters because the redesign must not land on another generic SaaS
look; *implementation risk* matters because this is a solo-maintained app with no UI framework.

| Criterion (weight) | A · Marking Desk | B · Control Room | C · Studio |
|---|---:|---:|---:|
| Task fit: marking & review (×3) | **9** | 7 | 5 |
| Data fit: tables, lists, jobs (×2) | 8 | **9** | 4 |
| Long-session comfort (×2) | **9** | 5 | 8 |
| Readability of feedback text (×2) | **9** | 6 | 8 |
| Distinctiveness (×2) | **9** | 4 | 7 |
| Accessibility headroom (×2) | **9** | 7 | 7 |
| Mobile usability (×1) | 8 | 6 | **9** |
| Implementation complexity (×1, higher = simpler) | 7 | 6 | **8** |
| Performance risk (×1, higher = safer) | **8** | 7 | 6 |
| Scales to all 30 screens (×2) | **9** | 9 | 5 |
| **Weighted total** | **8.8** | 6.7 | 6.2 |

Scores are design judgement, not measurement; they are shown so the choice can be challenged.

---

## 6. The chosen direction — *Chalk & Ink* (Direction A, formalised)

### 6.1 Identity in one paragraph

Math Checker should feel like **a well-made marking desk**: warm paper, precise ink, a margin for
marks, a running tally at the bottom, and nothing on the surface that is not either the work or a
fact about the work. Interaction colour is a single verdigris; red is reserved for marks and
destruction; the type is a scholarly superfamily (Plex Sans/Serif/Mono) with real tabular
numerals. There are no gradients on actions, no blurred cards, and no decorative illustration
competing with data.

### 6.2 The two registers

One system, two densities — this is what makes it scale from 30 screens without forcing a
dashboard look onto a marking page:

| | **Desk** (reading & deciding) | **Ledger** (scanning & operating) |
|---|---|---|
| Surfaces | Marking workspace, correction detail, report preview, feedback, setup | Home, Results, Students, Assignments, Tasks, Automation, Settings |
| Body type | Plex Serif 15–16 px / 1.6 for prose, feedback, student answers | Plex Sans 13–14 px / 1.5 for labels and dense rows |
| Density | Comfortable: 40 px rows, 24 px section gaps | Compact: 32 px rows, 16 px section gaps, sticky headers |
| Colour | Ink + one accent + mark red only on marks | Ink + accent; status = text + icon + tone |
| Motion | 120–160 ms fades for panel changes; none on scroll | 80–120 ms row hover; ledger numbers count up once, never repeatedly |
| Signature | 56 px margin column, mark glyphs, ledger strip | Column rules, frozen identifier column, inline micro-bars |

Both registers share the same tokens, the same focus ring, the same 4 px spacing ramp and the
same six-destination navigation. A page never mixes registers within one card.

### 6.3 Why this beats the alternatives *for this product*

1. **It follows the unit of work.** The product's core loop is one paper → one question → one mark.
   Direction A gives that loop a physical shape (margin, ledger strip) instead of burying it in
   tabs and cards.
2. **It fixes the two worst measured problems structurally.** The empty centre pane exists because
   the layout assumes a document that often isn't there; the Desk's three-zone grid makes "no
   document" a *state of one zone* with a useful fallback (extracted answer + link to the
   original), not a void. The 24,493 px automation page becomes a ledger with a pinned summary.
3. **It is defensible on accessibility.** Warm paper + near-black ink is the easiest palette in
   which to hit 4.5:1 everywhere; it removes the failing translucent surfaces and gradient labels.
4. **It is implementable without a UI framework.** No new dependency: one stylesheet, the existing
   React components, the existing SVG icon set (redrawn on a 20 px grid), and two font files.
5. **It is distinctive without being cute.** The margin column, the ledger strip and the
   index-tab navigation are recognisable, and none of them is a gimmick — each does a job.

### 6.4 Guardrails (what *Chalk & Ink* must never become)

- No paper texture, faux tape, torn edges, handwriting fonts or drop-cap decoration.
- No serif in UI chrome, buttons, table headers or labels.
- No more than one accent hue on screen at a time; mark red is never a button colour.
- No blur or gradient on any surface that carries text.
- No animation on scroll, on data refresh, or on numbers that change frequently.
- No "hero" KPI that repeats a number already visible in the ledger below it.

### 6.5 What is deliberately kept from today's product

The honest states ("No papers corrected in the last 30 days"), the reconnecting banner, the
keyboard-trapped overlays, the reduced-motion rule, colour-never-alone status, the zero-dependency
charts, the command palette, the developer role gates, and the report stylesheet's serif voice —
which is where the idea for this direction actually came from (`report.css:156` already sets
Georgia for printed reports).
