# Component and Interaction System

The new component layer: what exists today, what survives, what is rebuilt, what is retired, and
exactly how each control behaves. Tokens referenced here are defined in
[design-system-specification.md](design-system-specification.md).

**Current state, measured:** `frontend/src/components.tsx` exports **26 visual components** plus 2
helpers (`rowActivate`, `useToast`); 58 inline SVG icons (`Icons.tsx`); 9 chart components
(`charts/Charts.tsx`); 8 empty-state line-art scenes (`EmptyArt.tsx`); 12 domain panels (`attention.tsx`, `workers/panels.tsx`,
`CorrectionPage` `JobQueue`, `distributed/panels.tsx`, `SyncMonitor`, `LargePdfJobDetail`,
`NetworkPanel`, `SearchBox`, `TooltipLayer`).

---

## 1. Disposition of the existing inventory

| # | Component | File | Verdict | Reason |
|---|---|---|---|---|
| 1 | `Badge` (6 tones) | `components.tsx:12` | **Rebuild** as `StatusBadge` only | A badge for everything is why the UI has 4 competing label styles; status is the only justified use |
| 2 | `StatusBadge` | `components.tsx:27` | **Keep, restyle** | Correct model: tone + text (+ icon). Add `icon` to every tone |
| 3 | `Button` (8 variants) | `components.tsx:44` | **Rebuild** with 4 variants | 8 variants + 109 raw `<button>` = no hierarchy (RD-032) |
| 4 | `IconButton` | `components.tsx:72` | **Keep, contract change** | `label` becomes required and is used for `aria-label` **and** tooltip; today 3 exist with none (RD-001) |
| 5 | `Stat` | `components.tsx:88` | **Replace** by `Metric` | One of three stat presentations (Stat, StatusCard, hero) |
| 6 | `StatusCard` | `components.tsx:96` | **Replace** by `Metric` + `Card` | Used in 1 file; duplicates Stat |
| 7 | `AttentionItem` | `components.tsx:120` | **Rebuild** | `div role="button"` handles Enter only, not Space (RD-006); becomes a real `<button>` or `<a>` |
| 8 | `Progress` | `components.tsx:150` | **Keep, restyle** | Already labelled and accessible; only the track/bar styling changes |
| 9 | `rowActivate` | `components.tsx:180` | **Keep** | Correct Enter/Space semantics for `<tr>`; must be used by every clickable row |
| 10 | `Empty` | `components.tsx:198` | **Keep as `EmptyState`** | Good API (title/hint/action); restyle and make mandatory on every list |
| 11 | `Loading` | `components.tsx:213` | **Keep as `Skeleton`** | Skeletons match final layout; add `rows` prop |
| 12 | `ErrorState` | `components.tsx:221` | **Keep, mandate** | Used in 5 of 24 pages; make it the only error surface (RD-019) |
| 13 | `PageHeader` | `components.tsx:242` | **Rebuild** | One `<h1>`, max 3 actions, optional meta; no icon+title+sub triple |
| 14 | `Section` | `components.tsx:262` | **Retire** | It is a card with an `h3`; two of them are page frames today (RD-002) |
| 15 | `KV` | `components.tsx:282` | **Keep as `DefinitionList`** | Semantic `<dl>`; current markup is two spans |
| 16 | `DataTable` | `components.tsx:292` | **Rebuild** | Needs sorting, sticky header, frozen column, pagination, empty/error slots, caption |
| 17 | `Segmented` | `components.tsx:305` | **Keep, restyle** | Right model for 2–4 exclusive filters; keyboard arrow support to add |
| 18 | `Tabs` | `components.tsx:327` | **Rebuild as `SectionTabs`** | Missing `aria-controls`/`tabpanel`; no roving tabindex; no overflow handling beyond scroll |
| 19 | `Dropdown` | `components.tsx:347` | **Replace by `Menu`** | No arrow-key navigation, no roving focus, no `aria-activedescendant`; positioning is `right:0` guesswork |
| 20 | `Modal` | `components.tsx:433` | **Keep, restyle** | Focus trap, Escape, scroll lock and restore are correct — preserve verbatim |
| 21 | `Drawer` | `components.tsx:466` | **Keep, restyle** | Same behaviour; add `aria-labelledby` and a 400/480 px width scale |
| 22 | `ToastProvider` | `components.tsx:500` | **Keep, extend** | Add `undo` action and a duration per tone; cap 3 visible |
| 23 | `ExportMenu` | `components.tsx:540` | **Rebuild** | Anchor inside a `<button>` is invalid; becomes `Menu` + real links |
| 24 | `ConfirmDialog` | `components.tsx:580` | **Keep, mandate** | Used once; must be used by every destructive action (RD-022) |
| 25 | `ProvenanceBadge` | `components.tsx:600` | **Keep, restyle** | The four-state model (AI / verified / teacher / edited) is excellent and must survive |
| 26 | `FilterBar` | `components.tsx:620` | **Promote & adopt** | Used in 1 file while 4 pages hand-build filters (RD-032) |
| 27 | `WorkspacePanes` | `components.tsx:650` | **Keep logic, restyle** | Divider keyboard support, width fitting and stacking are good; keep the 899 px media query but align it to the 1024 token (RD-040) |
| 28 | `Icon` (58) | `Icons.tsx` | **Keep, redraw on 20 px grid** | Consistent stroke today; align to 1.5 px at 16/20 px and add missing glyphs (see §9) |
| 29 | `EmptyArt` (8) | `EmptyArt.tsx` | **Keep, use sparingly** | Decorative, `aria-hidden`, no animation — correct |
| 30 | Charts (9) | `charts/Charts.tsx` | **Keep** | Zero-dependency, text summaries, hidden tables. Restyle axes/typography only |

**Net:** 14 kept and restyled, 8 rebuilt, 4 replaced or retired, **+16 new** components required by
the new layouts: `FileDrop`, `FileCard`, `UploadProgress`, `Field`, `Menu`, `Pagination`, `Metric`,
`MarkGlyph`, `ScoreBlock`, `InlineBar`, `Stamp`, `LedgerStrip`, `Breadcrumb`, `Stepper`,
`GatedPage`, `CommandPalette`.

---

## 2. Buttons

### 2.1 Hierarchy — four variants, no more

| Variant | Use | Appearance |
|---|---|---|
| `primary` | The one action a page is for ("Inspect and correct", "Save & next", "Accept") | Solid `--accent`, white text, no gradient |
| `secondary` | Supporting action ("Refresh", "Export", "Open") | `--surface`, 1 px `--rule-strong`, ink text |
| `ghost` | Tertiary, in-row and toolbar actions | Transparent, ink text, hover `--surface-sunken` |
| `danger` | Destructive only ("Factory reset", "Remove key") | `--mark` text on `--mark-soft`, 1 px `--mark` border |

There is **no** `success`, `warning` or `nav` button variant. "Success" is a status, not an action;
colouring a button green to mean "accept" duplicates the status vocabulary and was measured as
part of the 8-variant problem (RD-032).

**One primary per view.** In a card with several actions, at most one is primary; the rest are
secondary/ghost. In a table row, actions are ghost icon+label or a `Menu`.

### 2.2 Sizes

| Size | Height | Padding | Type | Use |
|---|---:|---|---|---|
| `sm` | 28 px | 10 px | 12.5 px/600 | Table rows, dense toolbars |
| `md` (default) | 36 px | 14 px | 13.5 px/600 | Forms, page actions |
| `lg` | 44 px | 20 px | 15 px/600 | Primary action of a flow step (upload, sign-in) |

All sizes meet WCAG 2.2 AA target size (24 px); `lg` meets the 44 px touch recommendation. Icon
buttons are 32 px (`md`) / 28 px (`sm`) with a 44 px hit area via padding on touch pointers
(`@media (pointer: coarse)`).

### 2.3 States

| State | Visual | Notes |
|---|---|---|
| Rest | as variant | — |
| Hover | surface shifts one step (`--surface` → `--surface-sunken`); no transform | 80 ms |
| Active | `translateY(1px)` + one step darker | 60 ms |
| Focus-visible | 2 px `--accent` ring, 2 px offset, 3 px radius | Must be visible on every surface; never removed |
| Disabled | 45 % opacity, `cursor: not-allowed`, no hover | Must be paired with a visible reason in the surrounding UI |
| Loading | label hidden (`visibility:hidden`), 16 px spinner centred, `aria-busy="true"`, width locked to prevent layout shift | The button never changes size |
| Selected (toggle) | `--accent-soft` background + 1 px `--accent` border + `aria-pressed` | Used for segmented/filter |

### 2.4 Loading and long actions

Any action that can exceed 2 s shows progress **in place**: the button becomes a bar
(`UploadProgress`) or splits into `Cancel` + status. A disabled spinner button for a 20-minute job
is not acceptable (RD-016).

---

## 3. Forms and inputs

### 3.1 `Field` — the only way to label a control

```
<Field label="Student name" hint="Used on the report." error={…} required>
  <Input … />
</Field>
```

* Renders `<label for>`, hint with `aria-describedby`, error with `aria-describedby` + `role="alert"`.
* Label position: **above** the control, 12.5 px/600, sentence case (not uppercase — the current
  uppercase labels lose legibility at 11 px).
* Required is marked with the word "required" in the hint, not only an asterisk.
* Error summary at the top of long forms (GOV.UK pattern, source #25) with links that move focus.

### 3.2 Inputs

| Control | Spec | Notes |
|---|---|---|
| `Input` | 36 px tall, 10 px padding, 1 px `--rule-strong`, 4 px radius, `--surface` background | Focus: 1 px `--accent` + 3 px `--accent-soft` ring |
| `Textarea` | min 3 rows, resizes vertically, no horizontal resize | 16 px on mobile to prevent iOS zoom |
| `Select` | Native `<select>` styled, chevron as background SVG | Never a custom listbox (accessibility cost, no benefit here) |
| `Checkbox` / `Radio` | 18 px, `accent-color: var(--accent)`, label 8 px gap | Touch area 44 px |
| `Switch` | 36×20 track, 16 px knob, label always visible | Current value echoed in text where it changes app behaviour |
| `NumberStepper` | Input + −/+ buttons, `inputmode="numeric"` | Used for marks and batch counts |
| `SearchField` | 36 px, leading icon, `⌘K` hint, clear button | Same component in header and filter bars |
| `Combobox` | Input + listbox, `aria-expanded`/`aria-activedescendant` | Class/assignment pickers with >15 options |

Inputs are **32 px on ledger pages** and **36 px on desk pages** — never smaller, never a mix
within one form.

### 3.3 File upload

The single most important form control in the product (RD-016, RD-017).

**`FileDrop`**
* 96 px tall, dashed 1 px `--rule-strong`, 6 px radius, centered icon + sentence + `Choose file`
  secondary button + format hint.
* Drag-over: `--accent-soft` fill, solid `--accent` border, "Release to add".
* Keyboard: the whole zone is a `<button>`-equivalent (`role="button"`, Enter/Space opens the file
  dialog); the visible `Choose file` is the same control, not a nested duplicate.
* Accept filter enforced **before** upload with a specific message (`PDF or JPG/PNG, up to 40 MB`).

**`FileCard`** once chosen: type icon, name (middle-truncated with `title`), size, page count when
known, `Replace`, `Remove`. `Remove` is not destructive (the file is local until submit) and needs
no confirm.

**`UploadProgress`**: determinate when `XMLHttpRequest.upload.onprogress` is available (the current
`fetch` POST cannot report upload progress — the implementation must switch to XHR or a chunked
endpoint **without changing the endpoint's contract**), stage labels `Uploading → Inspecting →
Queued`, `Cancel` until the server acknowledges. On error: keep the files, keep the fields, show
the cause and `Retry`.

### 3.4 Validation timing

Validate on **blur** for fields, on **submit** for the form; never on every keystroke. A field that
was already in error re-validates on input. Errors never clear the user's input.

---

## 4. Navigation components

| Component | Spec |
|---|---|
| **AppRail** | 6 destinations; 72/236 px; icon 20 px + label 11 px/600; active = 3 px left rule + `--surface` fill; hover = `--surface-sunken`; `aria-current="page"`; collapse toggle has a keyboard shortcut and `aria-expanded` |
| **SectionTabs** | Underlined tabs, 32 px tall, 1 px bottom rule on the container, 2 px accent underline on active; `role="tablist"` + `role="tab"` + `aria-selected` + `aria-controls`; roving tabindex (arrows, Home/End); overflow: horizontal scroll with fade edges, never a "more" menu |
| **Breadcrumb** | `Results / Correction detail`; 12 px, `--muted`, `/` separators as `aria-hidden`; parent is a link; current item is text with `aria-current="page"`; collapses to `‹ Results` on mobile |
| **Pagination** | `1–50 of 169` + Prev/Next + page size select; buttons disabled at bounds; announces the range with `aria-live="polite"` |
| **Stepper** | Labelled steps (`Step 2 of 4`), completed steps show a check, current is bold, future steps are not clickable unless already visited |
| **CommandPalette** | `⌘K`; grouped results; `aria-activedescendant`; ↑↓ move, Enter runs, Esc closes and restores focus; max 8 results per group, 40 total |

Mobile (< 768 px): the rail becomes a **bottom bar** with 5 destinations + `More` sheet (System).
The jobs pill moves into the header, which stays sticky at 48 px.

---

## 5. Data display

### 5.1 `DataTable`

| Feature | Spec |
|---|---|
| Structure | `<table>` + `<caption class="visually-hidden">`; `<th scope="col">`; `aria-sort` on sortable headers |
| Header | 32 px, sticky, 12 px/600, `--muted` text, 1 px bottom rule, no uppercase letter-spacing |
| Rows | 32 px (ledger) / 40 px (desk); hover `--surface-sunken`; clickable rows get `rowActivate` |
| Sorting | Click header cycles none → asc → desc; one sort column; `aria-sort` reflects it; stable |
| Frozen column | First column sticky with a right rule at ≥ 1024 px |
| Overflow | Wrapper scrolls horizontally; `overflow-x` is explicit, never clipped by `overflow: hidden` on an ancestor (constraint from RD-025) |
| Truncation | `max-width` + ellipsis **with `title`**; never truncate a column whose value is the row's identity |
| Empty | `<tbody>` replaced by an in-table `EmptyState` spanning `colspan` — never a blank table |
| Error | `ErrorState` in place of the table, with the failed endpoint named in Diagnostics link |
| Loading | 8 skeleton rows matching the final column widths |
| Mobile | < 768 px: card rows with `data-label` per cell (keep today's `.table-stack`) **or** deliberate horizontal scroll for numeric comparisons; never both |

### 5.2 Marks and scores

Marks are **typography**, not pills:

| Element | Spec |
|---|---|
| `MarkGlyph` | `3/3` — numerator 16 px/600 tabular, `/`, denominator 13 px/500 `--muted`; 0 marks in `--mark` |
| `ScoreBlock` | `44/49` (22 px) above `89.8%` (13 px `--muted`); used in the ledger strip and detail header |
| `InlineBar` | 3 px rule under a percentage cell, width = value, `--accent`; 0 = no bar |
| Band | `band 8` as a bordered "stamp" (11 px, letter-spacing .06em, 1 px rule) — never a coloured pill |

Colour is never the only mark signal: every 0/partial/full state carries the number itself, and a
lost-mark question carries an icon + left rule in the margin.

### 5.3 Charts

Keep the existing zero-dependency components. Changes: axis labels 12 px `--muted`; grid lines
1 px `--rule`; series colours from the semantic ramp, with **patterns/dashes** where two series
could be confused; every chart keeps its `summary` text and hidden data table. Donut is retired in
favour of a stacked bar for job states (source #4: length is judged more accurately than angle).

---

## 6. Overlays

| Component | Behaviour (preserve/define) |
|---|---|
| `Modal` | Focus trap, Escape, scrim click, scroll lock, focus restore — **keep today's `useOverlayBehaviour`**; add `aria-labelledby`; max-width 560 (default) / 720 (wide); footer actions right-aligned with primary last |
| `ConfirmDialog` | Built on Modal; names the object ("Delete 4 results from Ch2L1?"); dangerous confirm button uses `danger`; focus starts on **Cancel** for destructive actions (today it auto-focuses the confirm button via `data-autofocus`) |
| `Drawer` | Right side, 400 px (480 wide), same trap semantics; used for jobs, discrepancy detail, and job logs; closes on route change |
| `Menu` | Replaces `Dropdown`: ↑↓/Home/End, typeahead, `role="menu"`/`menuitem`, closes on Tab, Escape restores focus to trigger; positions with collision detection (flip above if < 8 px below) — no `:has()` z-index hacks (RD-036) |
| `Tooltip` | Keep the single portal layer. Show on hover **and** focus after 400 ms; hide on Escape; never the only source of a control's name (icon buttons always have `aria-label`) |
| `Toast` | Bottom-right, max 3, 5 s (info/success) / 8 s (error, with `Retry`), `role="status"` for info and `role="alert"` for errors; optional `Undo`; pauses on hover/focus |

**Stacking order** comes from one scale: `--z-sticky 10 · --z-rail 20 · --z-header 30 ·
--z-popover 40 · --z-overlay 50 · --z-modal 60 · --z-toast 70 · --z-tooltip 80`.

---

## 7. Feedback states

### 7.1 `EmptyState`

Required structure: icon or line-art (decorative) · **title** (one sentence, no "No data") · one
sentence of explanation · **one** action when an action exists. Variants: `first-run`, `no-results`,
`all-done`, `not-configured`. Every list, table and chart uses it (today Classes renders the literal
string "no results" — RD-049).

### 7.2 `ErrorState`

Three parts: **what failed** ("Couldn't load results"), **why** (the plain-language cause when
known), **what to do** (Retry, plus "Open Diagnostics" for technical causes). Never a raw
exception, never `Failed to fetch`, never a provider payload (RD-018, RD-019).

### 7.3 `Banner`

Page-level, dismissible only if non-critical. Tones: info / warn / error / success. One per page.
The reconnect banner stays a fixed banner with `role="status"`.

### 7.4 Progress

* Determinate whenever the backend provides a fraction (`Progress` with label).
* Indeterminate only for unknown-duration work, max 2 px tall, with a text label beside it.
* Job progress includes the stage sentence ("Marking Q12 of 28") — the backend already sends
  `stage_detail`; it must be shown, not just the bar.
* No progress animation that implies completion (the current 450 ms width easing on 50 rows is
  also a paint cost — RD-044).

---

## 8. Micro-interactions

| Interaction | Spec | Reduced motion |
|---|---|---|
| Row hover | background 80 ms, no movement | same (colour only) |
| Card hover (clickable only) | 1 px border darkens; **no lift/translate** | same |
| Button press | 1 px translate, 60 ms | none |
| Tab change | content cross-fades 120 ms | instant |
| Drawer / modal | 160 ms ease-out translate + fade | instant |
| Toast | 160 ms rise + fade | instant |
| Number count-up | **only** on Home's three daily metrics, once per page load, 400 ms | disabled |
| Chart draw | line path 240 ms on first render only | disabled |
| Question change (workspace) | mark pane cross-fade 120 ms; margin selection moves 80 ms | instant |
| Undo window | 8 s toast with a shrinking rule | rule hidden |
| Focus ring | appears immediately (no transition) | — |

Rules: nothing animates on scroll; nothing animates on data refresh; no animation may delay an
interactive element (the current `.enter-stagger` delays content by up to 200 ms).

---

## 9. Icons

Keep the hand-drawn 58-icon set; redraw on a 20 px grid with 1.5 px stroke at 20 px and 1.25 px at
16 px. Add: `check`, `x`, `chevron-up`, `chevron-down`, `chevrons-left/right`, `download`,
`upload`, `filter`, `sort-asc`, `sort-desc`, `more-horizontal`, `external-link`, `undo`, `alert-triangle`,
`circle-check`, `circle-x`, `loader`, `file-pdf`, `file-image`, `panel-left`, `panel-right`, `zoom-in`,
`zoom-out`, `maximize`, `printer`, `clock`, `user`, `users`, `key`, `shield`, `wifi-off`.

Icon-only controls are **only** allowed when the icon is unambiguous **and** an `aria-label` is
supplied. The 3 unnamed buttons found in the audit are the counter-example (RD-001).

---

## 10. Component API conventions

1. **No inline styles.** Every component takes `className` and, where a variant is needed, a typed
   prop. 936 inline style objects are the root cause of the spacing drift (RD-031).
2. **Variants are closed unions**, not free strings: `variant: "primary" | "secondary" | "ghost" | "danger"`.
3. **Sizes are a scale**: `size?: "sm" | "md" | "lg"`.
4. **Every interactive component accepts `data-testid`** so the existing DOM-level tests can be
   ported without reaching into class names.
5. **Accessible name is a required prop** for icon-only controls (`label: string`).
6. **States are props, not booleans scattered in pages**: `status?: "idle" | "loading" | "error" | "empty"`.
7. **No component renders its own `<h1>`** except `PageHeader`.
8. **Composition over configuration**: `Card` + `CardHeader` + `CardBody` instead of a 12-prop
   `Section`.

---

## 11. Migration map (old → new)

| Old usage | New |
|---|---|
| `<Button variant="success" icon="check">Accept</Button>` | `<Button variant="primary" icon="check">Accept</Button>` |
| `<Button variant="warning">` | `variant="secondary"` + a `StatusBadge tone="warn"` beside it |
| `<Badge tone="info">` for non-status labels | Plain text, or `Tag` (new, neutral, 1 px border) |
| `<Stat>` / `<StatusCard>` / hero band | `<Metric value label scope />` in a `MetricRow` |
| `<Section title=…>` as a page frame | `<PageHeader>` + `<Card>` |
| `<Dropdown>` | `<Menu>` with `items` |
| `<ExportMenu>` | `<Menu>` whose items are `<a download>` |
| `<PageHeader icon=…>` | No icon; the page title is enough |
| `.enter-stagger` | Removed (no delayed content) |
| `var(--line)`, `var(--bg)`, `var(--surface-2)`, `var(--info)` | Real tokens (RD-035) |
| `className="btn ghost size-sm"` | `size="sm"` (RD-038) |

Every replacement must keep the original behaviour and be verified by the page-level tests in
[regression-accessibility-performance-checklist.md](regression-accessibility-performance-checklist.md).
