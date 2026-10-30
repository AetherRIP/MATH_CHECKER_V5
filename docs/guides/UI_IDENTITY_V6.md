# MATH_CHECKER_V5 — "Instrument" design direction

A redesign plan for the desktop client. Stage A record: what changes, why, and how it is verified.

## The problem with the current look

The shipped interface is competently built but visually generic: indigo on near-white, one
radial-gradient hero, 18 DIP cards with soft shadows everywhere, pill-shaped status chips, and a
single typeface doing every job. It reads as a well-made dashboard template rather than a tool
for examiners. Nothing in it belongs to *marking*: no ruled tables, no margin marks, no
instrument-like readouts of the numbers teachers actually watch.

## Direction: the examiner's instrument panel

Five decisions carry the identity:

1. **Type as structure.** Space Grotesk (already bundled, previously unused) for display,
   page titles, navigation, buttons and figures; Plus Jakarta Sans for body copy that teachers
   read for long stretches; Cascadia Mono for ids. Figures are tabular, so columns of scores
   line up and a number reads as a reading rather than as text.
2. **Ink and signal.** A cool ink-graphite base (never pure black, never cream) with one signal
   accent: cyan-teal, the colour of a measurement, not of a brand. Vermilion is reserved for
   errors, amber for review, green for confirmed. Light theme is cool paper (not warm cream)
   with a deep teal ink accent.
3. **Hairlines over haze.** Surfaces are separated by 1 DIP hairlines and layering; shadows drop
   to a whisper in light and stay restrained in dark. Geometry tightens: cards 12, controls 8,
   dialogs 14, chips a 5-radius stamp so status reads as a stamp on a script, not a lozenge.
4. **Ledger tables.** Horizontal rules, no zebra striping, uppercase micro-headers, denser rows.
   A results table should look like a mark sheet, not a card list.
5. **One memorable moment.** The hero panel is an ink block with a cyan measurement rule at its
   edge, instrument-style figures and a single white action. Everything around it stays quiet.

Structural devices that recur: 3 DIP marker bars for the active navigation item and the page
eyebrow rule, hairline section dividers, monospace ids beside human-readable labels.

## Stages

- **B — foundation (shared):** `Themes/Tokens.xaml` (font roles, radii, padding),
  `LightTheme.xaml` / `DarkTheme.xaml` (palette, hero ink, elevation), `Themes/Controls.xaml`
  (type scale and figures, buttons, fields, chips, ledger DataGrid, nav marker, focus ring,
  progress, page frame header).
- **C — screens:** dashboard hero and metric strip recomposed on the new system; every other
  screen inherits the foundation through the shared templates, then gets a consistency pass for
  the parts a template cannot reach.
- **D/E — integration and verification:** Release build, desktop test suite (including the
  page-width regression test), offscreen renders of the main screens in both themes at 628 and
  1008 DIP, and a real application launch against the demo sandbox.

## Constraints honoured

No behaviour changes: bindings, commands, view models, API contracts, grading logic, Classroom
safeguards and packaging are untouched. `StudentsView.xaml` and `AssignmentsView.xaml` carry
in-flight user edits and are preserved. The React/Vite folder is a separate untracked experiment
and is not integrated.
