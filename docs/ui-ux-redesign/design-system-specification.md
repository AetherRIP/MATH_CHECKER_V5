# Design System Specification — *Chalk & Ink*

Concrete tokens and rules for the redesign. Every value below is either computed (contrast
ratios were calculated from the hex values) or a deliberate constraint. An implementation agent
should be able to build the entire UI from this document without inventing values.

---

## 1. Philosophy

1. **One accent, one mark colour.** Verdigris means *interactive*. Mark red means *a mark was lost*
   or *this will destroy something*. Nothing else is coloured for decoration.
2. **Borders before shadows.** Surfaces are defined by a 1 px rule. Shadows exist only for things
   that float (menus, modals, drawers, toasts).
3. **Type carries the hierarchy.** Size, weight and family — not colour, not cards, not icons.
4. **Density is a property of the surface, not the app.** Desk pages breathe; ledger pages compact.
5. **Every value is a token.** No component may contain a raw hex, px margin or duration.

---

## 2. Token architecture (three tiers)

Following Fluent 2's global → alias model (external research #19):

```
tier 1  primitives   --paper-50, --ink-900, --verdigris-600, --space-4, --text-13 …
                     (raw values; never used directly in components)
tier 2  semantic     --canvas, --surface, --ink, --muted, --rule, --accent, --mark, --focus …
                     (what a component should use; theme-aware)
tier 3  component    --button-primary-bg, --table-row-hover, --input-border …
                     (only where a component needs a semantic pair that doesn't exist)
```

Rules: components use tier 2 or 3; theme switching happens **only** at tier 2; no `!important`
except in the print stylesheet; no inline styles (RD-031).

---

## 3. Colour

### 3.1 Light — “Paper”

| Token | Value | Role |
|---|---|---|
| `--canvas` | `#FBF9F6` | App background (warm paper) |
| `--surface` | `#FFFFFF` | Cards, tables, panels |
| `--surface-sunken` | `#F3EFE9` | Inputs, code blocks, document canvas, row hover |
| `--rule` | `#E3DDD3` | Decorative separators, card borders |
| `--rule-strong` | `#C9C2B6` | Section dividers, table header underline (decorative) |
| `--control-border` | `#877E72` | Input/checkbox boundaries (≥ 3:1, WCAG 1.4.11) |
| `--ink` | `#16181D` | Primary text |
| `--muted` | `#5A6472` | Secondary text, labels |
| `--faint` | `#6B7280` | Tertiary text (timestamps, hints) |
| `--accent` | `#0F6B5C` | Primary actions, links, focus, selection |
| `--accent-ink` | `#FFFFFF` | Text on accent |
| `--accent-soft` | `#E6F1ED` | Accent tint backgrounds |
| `--mark` | `#B42318` | Lost marks, destructive, errors |
| `--mark-soft` | `#FBEAE8` | Error surfaces |
| `--ok` | `#166A43` | Success, verified, published |
| `--warn` | `#8A5300` | Warnings, low confidence |
| `--info` | `#1D4ED8` | Informational (rare — never for actions) |
| `--focus` | `#0F6B5C` | Focus ring |

### 3.2 Dark — “Night ledger”

| Token | Value | Role |
|---|---|---|
| `--canvas` | `#101317` | App background |
| `--surface` | `#171B21` | Cards, tables |
| `--surface-sunken` | `#0C0F13` | Inputs, document canvas |
| `--rule` | `#2A313B` | Separators |
| `--rule-strong` | `#3D4653` | Section dividers |
| `--control-border` | `#66727F` | Input boundaries |
| `--ink` | `#F2F4F7` | Primary text |
| `--muted` | `#A9B3C1` | Secondary text |
| `--faint` | `#8C97A6` | Tertiary text |
| `--accent` | `#4FD1B5` | Primary actions (dark text on it) |
| `--accent-ink` | `#062A24` | Text on accent |
| `--accent-soft` | `#12312C` | Accent tint |
| `--mark` | `#FF8A7A` | Lost marks, destructive |
| `--mark-soft` | `#3A1D1B` | Error surfaces |
| `--ok` | `#4ADE9B` | Success |
| `--warn` | `#F0B429` | Warning |
| `--info` | `#8AB4FF` | Informational |
| `--focus` | `#4FD1B5` | Focus ring |

### 3.3 Verified contrast

All values computed with the WCAG relative-luminance formula.

| Pair | Ratio | Requirement | Result |
|---|---:|---|---|
| `--ink` on `--canvas` | **16.90** | 4.5 | pass |
| `--muted` on `--surface` | **6.00** | 4.5 | pass |
| `--faint` on `--canvas` | **4.60** | 4.5 | pass |
| `--faint` on `--surface` | **4.83** | 4.5 | pass |
| `--accent` on `--surface` | 6.41 | 4.5 | pass |
| white on `--accent` | 6.41 | 4.5 | pass |
| `--mark` on `--surface` | 6.57 | 4.5 | pass |
| `--mark` on `--mark-soft` | 5.65 | 4.5 | pass |
| `--ok` on `--surface` | 6.61 | 4.5 | pass |
| `--warn` on `--surface` | 6.33 | 4.5 | pass |
| `--control-border` on `--surface` | **3.83** | 3.0 (1.4.11) | pass |
| `--ink` on `--surface` (dark) | 15.68 | 4.5 | pass |
| `--muted` on `--surface` (dark) | 8.15 | 4.5 | pass |
| `--faint` on `--surface` (dark) | 5.84 | 4.5 | pass |
| `--accent` on `--surface` (dark) | 9.15 | 4.5 | pass |
| `--accent-ink` on `--accent` (dark) | 8.15 | 4.5 | pass |
| `--mark` on `--surface` (dark) | 7.54 | 4.5 | pass |
| `--control-border` on `--surface` (dark) | **3.52** | 3.0 | pass |
| `--faint` on `--surface-sunken` (light) | **4.22** | 4.5 | **fail — forbidden pair** |

**Rules derived from the table**

* `--faint` is **not allowed** on `--surface-sunken` in light; use `--muted` there (5.24).
* `--rule` (1.35:1) is decorative only — never the sole boundary of a control.
* No gradients behind text; no translucent text surfaces (the current `rgba` card surfaces are what
  made contrast unpredictable).
* Status colour is always paired with text and an icon; the mark colour never encodes an action
  ("danger" buttons are the only exception, and only for destruction).
* Known current failures this fixes: `--text-faint` 3.09:1 (light) / 3.66:1 (dark), white on the
  gradient end 4.04:1 (RD-004, RD-005).

---

## 4. Typography

### 4.1 Families

| Role | Family | Weights | Loading |
|---|---|---|---|
| UI, labels, tables | **IBM Plex Sans** | 400, 500, 600 | Self-hosted `woff2`, Latin subset, `font-display: swap`, preload the 500 weight only |
| Prose (feedback, answers, reports) | **IBM Plex Serif** | 400, 600 | Self-hosted, subset, loaded on first Desk page (not the shell) |
| Numerals, identifiers, logs | **IBM Plex Mono** | 400, 500 | Self-hosted, subset; used for job ids, logs, API keys, marks in log context |

**Why Plex:** one superfamily across three roles (coherent identity), open licence (SIL OFL), real
tabular figures, excellent maths-adjacent glyph coverage, and it is *not* the current stack —
which declares `Inter` but never loads it, so every screen today renders in Segoe UI (RD-033).

**Offline requirement (constraint C8):** fonts are bundled with the app, never fetched from a CDN.
Total budget: ≤ 180 KB for all six files (subset to Latin + punctuation + `£€%‰×÷−–—`).

**Fallback metrics** (avoid CLS, source #27):
```css
@font-face { font-family: "Plex Sans"; src: url(...) format("woff2"); font-display: swap;
             size-adjust: 100%; ascent-override: 92%; descent-override: 24%; }
--font-ui: "Plex Sans", "Segoe UI", system-ui, sans-serif;
```

### 4.2 Scale (rem, 16 px root — fixes RD-034)

| Token | rem | px | Use |
|---|---|---|---|
| `--text-2xs` | 0.6875 | 11 | Table micro-labels, badge text (only where ≥ 4.5:1) |
| `--text-xs` | 0.75 | 12 | Meta, timestamps, table headers |
| `--text-sm` | 0.8125 | 13 | Dense UI, secondary text |
| `--text-base` | 0.875 | 14 | Default UI body |
| `--text-md` | 1 | 16 | Prose in Desk register, mobile inputs |
| `--text-lg` | 1.125 | 18 | Card titles, page section headings |
| `--text-xl` | 1.375 | 22 | Page titles (ledger) |
| `--text-2xl` | 1.75 | 28 | Page titles (desk), score blocks |
| `--text-3xl` | 2.25 | 36 | Home greeting only |

Line heights: `1.3` for headings, `1.5` for UI, `1.65` for prose. Weights: 400/500/600 only — no
700+ (Plex 700 is heavier than the hierarchy needs and the current 750/780 values are synthetic).

### 4.3 Numerals

Every numeric context (marks, scores, counts, durations, bytes, ids) sets
`font-variant-numeric: tabular-nums lining-nums; font-feature-settings: "tnum" 1, "lnum" 1;`.
Right-align numeric table columns; never centre them.

### 4.4 Maths and formulae

The product currently stores question and feedback text as plain strings, with no TeX
(verified: no `katex`, `mathjax`, `\(` or `$$` in `frontend/src` or `backend/app`). Therefore:

* Render feedback/prose in Plex Serif at 16 px/1.65 with a 68-character measure.
* Inline variables (`x`, `n`) are italicised by markup where the source provides it; no automatic
  math parsing.
* If TeX ever appears in the content, vendor **KaTeX** locally (CSS + fonts, `htmlAndMathml`
  output, sources #39–40) — **do not** add it speculatively.

---

## 5. Spacing

4 px base, geometric-ish ramp. `--space-*` is the **only** permitted gap/margin source.

| Token | px | Typical use |
|---|---:|---|
| `--space-1` | 4 | Icon-to-label, badge padding |
| `--space-2` | 8 | Inline groups, table cell padding (vertical) |
| `--space-3` | 12 | Form field gaps, card inner gaps |
| `--space-4` | 16 | Card padding (compact), list gaps |
| `--space-5` | 24 | Card padding (default), section gaps |
| `--space-6` | 32 | Page gutters, block separation |
| `--space-7` | 48 | Between major page sections |
| `--space-8` | 64 | Page top/bottom on empty states |
| `--space-9` | 96 | First-run wizard vertical rhythm |

Forbidden: `18px`, `10px`, `6px`, `2px` margins (the current top inline values — RD-031). The only
sub-4 values allowed are `1px` borders and `2px` optical nudges inside a component.

---

## 6. Layout

* **Grid:** 12 columns, 24 px gutter, 32 px page margins (24 px < 1024, 16 px < 768).
* **Max widths:** app content 1,360 px; ledger tables 100 % of content; prose 68 ch (~720 px);
  settings index 240 px + content; workspace full content width.
* **Page rhythm:** title block 24 px bottom; sections 48 px apart; cards 24 px apart.
* **Rail:** 236 px expanded / 72 px collapsed. Content never sits under it (no overlay rail on
  desktop).
* **Workspace:** `margin(56) | answer(flex) | mark(400)`, gaps 0 with 1 px rules; ledger strip 56 px.
* **Z-scale:** sticky 10 · rail 20 · header 30 · popover 40 · overlay 50 · modal 60 · toast 70 ·
  tooltip 80. Replaces both the hardcoded values and the `report.css` token set (RD-036).

---

## 7. Density

| Property | Ledger | Desk |
|---|---:|---:|
| Table row height | 32 px | 40 px |
| Card padding | 16 / 20 px | 24 / 32 px |
| Section gap | 16 px | 32 px |
| Control height | 32 px | 36 px |
| Body text | 13 px sans | 16 px serif |
| Max content width | 1,360 px | 1,120 px (prose 720) |

A page picks one density; a card never mixes them.

---

## 8. Shape, border, elevation

| Token | Value | Use |
|---|---|---|
| `--r-xs` | 3 px | Badges, stamps, inline bars |
| `--r-sm` | 4 px | Buttons, inputs, cells |
| `--r-md` | 6 px | Cards, panels, menus |
| `--r-lg` | 10 px | Modals, drawers |
| `--r-pill` | 999 px | Only progress tracks and status dots |

| Token | Value | Use |
|---|---|---|
| `--elev-1` | `0 1px 2px rgb(0 0 0 / .06), 0 8px 24px rgb(0 0 0 / .08)` | Menus, popovers, tooltips |
| `--elev-2` | `0 2px 6px rgb(0 0 0 / .08), 0 24px 56px rgb(0 0 0 / .18)` | Modals, drawers |

Cards: 1 px `--rule`, no shadow. Hover on a **clickable** card changes only the border to
`--rule-strong` — no lift, no scale (the current `translateY(-2px)` on every card adds motion noise
to a screen with 20 cards).

---

## 9. Icons

20 px grid, 1.5 px stroke, round caps/joins, `currentColor`. Sizes 14/16/20/24. Icons are never
the only label for a destructive or ambiguous action. See `component-and-interaction-system.md §9`
for the required additions.

---

## 10. Motion

| Token | ms | Use |
|---|---:|---|
| `--dur-1` | 60 | Press feedback |
| `--dur-2` | 80 | Hover, row background |
| `--dur-3` | 120 | Tab/panel cross-fade, selection move |
| `--dur-4` | 160 | Menus, tooltips, toasts |
| `--dur-5` | 240 | Modal/drawer, chart first draw |

Easing: `--ease-out: cubic-bezier(.2, 0, 0, 1)` for entrances; `--ease-in: cubic-bezier(.4, 0, 1, 1)`
for exits. No springs, no bounce, no stagger that delays content.

**Forbidden:** animation on scroll; animation of numbers that change frequently; animated gradients;
parallax; anything that runs longer than 240 ms except the one-time Home count-up (400 ms).

`prefers-reduced-motion: reduce` removes all transforms and limits opacity transitions to 1 ms —
keep the existing global rule and extend it to the new components.

---

## 11. Responsive

### 11.1 Breakpoints (replace 620/760/860/900/1100/1200 — RD-040)

| Token | Width | Shell | Content |
|---|---|---|---|
| `--bp-sm` | 480 px | Bottom bar, 5 + More | Single column, 16 px margins |
| `--bp-md` | 768 px | Bottom bar | Tables stack to cards or scroll deliberately |
| `--bp-lg` | 1024 px | Rail 72 px (icons) | Two-column pages unlock; workspace becomes tabs below this |
| `--bp-xl` | 1280 px | Rail 236 px | Three-zone workspace; detail side panes |
| `--bp-2xl` | 1536 px | Rail 236 px | Content capped at 1,360 px, centred |

**One source of truth:** the JS breakpoints (`WorkspacePanes` uses `(max-width: 899px)`) must read
from the same constants as CSS. No second magic number.

### 11.2 Behaviour matrix

| Surface | 320–479 | 480–767 | 768–1023 | 1024–1279 | ≥ 1280 |
|---|---|---|---|---|---|
| Navigation | Bottom bar + More | Bottom bar + More | Rail icons | Rail icons | Rail + labels |
| Header | 48 px, title + jobs | + search icon | + search field | full | full |
| Tables | Stacked cards | Stacked cards | H-scroll with frozen col | Full | Full + column chooser |
| Workspace | Tabs + ledger strip | Tabs | Tabs | 2 panes (margin+answer, mark sheet) | 3 panes |
| Upload | Stacked zones | Stacked | 2 zones | 2 zones | 2 zones + queue rail |
| Settings | Index becomes a select | Select | Select | Index + content | Index + content |
| Reports | Builder only, preview via "Preview" | Builder → preview | Two columns | Two columns | Two columns |

### 11.3 Reflow (WCAG 1.4.10)

At 320 px every page must be usable without horizontal page scrolling. Tables may scroll inside
their own container (the criterion's data-table exception), but each cell must remain readable.
The workspace explicitly **must not** attempt a 3-pane layout below 1024 px.

---

## 12. Accessibility requirements (built in, not a phase)

| Area | Requirement | Verification |
|---|---|---|
| Contrast | All text ≥ 4.5:1 (≥ 3:1 for ≥ 24 px or ≥ 19 px bold); UI boundaries ≥ 3:1 | Token table above + computed audit on rendered pages |
| Focus | 2 px ring, 2 px offset, on every interactive element, never obscured by sticky chrome | Keyboard walk of every page |
| Targets | ≥ 24×24 (AA); 32 px default, 44 px for primary flow actions | DOM measurement |
| Names | Every icon-only control has `aria-label`; every input has a `<label>` | Automated audit (today: 3 unnamed buttons, RD-001) |
| Headings | Exactly one `<h1>`; no skipped levels | DOM audit per route |
| Forms | Error summary + inline error, `aria-describedby`, `role="alert"` | Manual |
| Dialogs | `aria-modal`, labelled, trap, Escape, focus restore | Manual (already correct today) |
| Live regions | Job progress and review counts announce politely, not on every tick | Manual |
| Tables | `caption`, `scope`, `aria-sort`, row activation via keyboard | Manual |
| Charts | Text summary + hidden data table for every chart | Manual |
| Motion | Reduced-motion honoured, including new components | Emulation |
| Zoom | 200 % zoom and 320 px reflow, no clipping | Emulation |
| Language | `<html lang="en">`, no `lang` gaps in mixed content | Manual |

---

## 13. Print and report

`report.css` becomes a proper print layer built on the same tokens:

* Serif body (Plex Serif), 10.5 pt, 1.45 leading, A4 margins 18 mm.
* Headings in `--ink`, rules in `--rule-strong` (print-safe: `#999`).
* No colour-coded marks alone: print adds `✓`/`✗`/`½` glyphs and the numeric mark.
* Page breaks: `break-inside: avoid` on question blocks, `break-after: page` between report sections.
* The on-screen preview renders the **same component tree** as the PDF, scaled — so what a teacher
  sees is what prints.

---

## 14. Implementation notes

```css
/* one entry, cascade layers, no inline styles */
@layer reset, tokens, base, components, patterns, utilities;

:root { /* tier 1 primitives */ }
:root, :root[data-theme="light"] { /* tier 2 semantic */ }
:root[data-theme="dark"] { /* tier 2 semantic */ }
```

* Replace the two hardcoded z-index sets with the single scale (§6).
* Delete `--line`, `--bg`, `--info`, `--surface-2` usages by mapping them to real tokens (RD-035).
* Keep the pre-paint theme script in `index.html`; it is correct and prevents a flash.
* No `backdrop-filter` anywhere except the modal/drawer scrim.
* `color-mix()` is allowed only for hover states, with a fallback declaration before it.

---

## 15. Governance

1. A new value requires a token; a new token requires a role name (not a colour name).
2. No component may add a raw hex or px — the review check is a grep for `#[0-9a-f]{3,8}` and
   `px` inside `src/**/*.tsx`.
3. Any change to a tier-2 token must be re-checked against §3.3 (contrast) before merge.
4. Density is chosen per page, never per card.
5. The design system is "done" when the four lint-style greps in the checklist pass:
   zero inline styles, zero raw hex in components, zero unnamed icon buttons, one `<h1>` per route.
