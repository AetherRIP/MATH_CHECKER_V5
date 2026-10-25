# Desktop UI improvement pass — 2026-10-24

Scope: the WPF client in `desktop/src/MathChecker.Desktop` (26 pages, 7 spaces,
command palette, marking desk). The correction engine, backend routes, marking
rules and verification logic were not modified.

Environment: Windows 11 (10.0.26200), 125 % display scaling, .NET SDK 10.0.402
(installed per-user for this pass; the machine previously had the runtime only),
Demo Mode against the real backend with an isolated data folder.

## 1. What was inspected

* Every view, view model, theme, converter and control in the client (~20k lines).
* All 26 pages driven through UI Automation in one real window session, captured
  as full-window PNGs (1800×1020 physical at 1440×875 DIP) via `PrintWindow`, and
  re-captured at the 900×600 minimum window size (1125×750 physical).
* 518 interactive controls inventoried per page with name, type, enabled state
  and bounds (`dev_local/ui_audit/wide/inventory-main.json`).
* Backend contract exercised independently by `desktop/tools/MathChecker.Desktop.E2E`.

Harnesses added for this pass (dev-only, untracked by design — `dev_local/` is
scratch):

| File | Purpose |
|---|---|
| `dev_local/ui_audit.ps1` | walks every space and page in one session, screenshots each, dumps the control inventory, captures palette / compact rail; isolated demo profile, restores the user's settings |
| `dev_local/ui_interact.ps1` | functional probe: selection-gated actions, a real search, the interval control across a refresh, Classroom gating; 17 assertions |

A capture defect found and fixed in the harness itself: `CopyFromScreen` with a
DPI-virtualised `GetWindowRect` silently crops a 125 % window (the first audit
images were 1435 px wide for a 1800 px window, which looked like content spilling
past the layout margin). The harness now uses `PrintWindow` at the window's real
pixel size.

## 2. Defects found

Ordered by impact. Each was reproduced in the running application.

| # | Defect | Evidence |
|---|---|---|
| 1 | **Selection-gated actions were always enabled and silently did nothing.** Today's *Cancel job* / *Retry job*, Tasks / Job log *Cancel / Retry / Open result*, Results *Open paper / Export PDF / CSV / Markdown*, Scheme *Stage / Record decision / Approve / Reject*, Assignments *Recheck / Open first page image*, AI keys *Test selected / Disable / Re-enable / Remove*, Automatic *Approve / Retry selected*. Their commands return early when nothing is selected, so the button looked usable and produced no feedback. | inventory: `enabled=true` for all of them with no row selected; commands read `if (Selected is null) return;` |
| 2 | **Automation page interval combo was blank with a red validation border** while the header said “interval 10 min”. Clearing the item list made the ComboBox write `null` into a non-nullable `int` property, failing conversion; the reload never restored the choice, and saving could have posted `interval_minutes: 0`. | `Automatic` page capture; `AutomationViewModel.LoadCoreAsync` set the value before rebuilding `Intervals` |
| 3 | **Results “Created” column hard-clipped the timestamp** (`2026-10-24 02:1`) and the question table clipped loss reasons; Assignments clipped the class name. Those grids had no `ElementStyle`, so the shared `CellText` ellipsis style was not applied. | `Results`, `Assignments` captures |
| 4 | **Search opened on “Nothing found”** before any query ran — an empty state claiming a failed search. | `Search` capture |
| 5 | **Instructions rendered as monospace data fields.** Tasks' “SELECTED JOB” and Assignments' “MARK SCHEME” boxes contained instructional sentences inside the JSON-style bordered box. | `Tasks`, `Assignments` captures |
| 6 | **Reports export buttons were enabled before a report existed** (0 rows), i.e. nothing to export. | `Reports` capture |
| 7 | Result action labels were inconsistent: `Export PDF` next to `CSV` and `Markdown`. | `Results` capture |
| 8 | Classroom offered course/assignment actions and empty pickers with no connection, next to an error-styled demo notice. | `Classroom` capture |

## 3. Fixes implemented

1. Added selection-state properties and bound `IsEnabled` on every action that
   cannot act without a target, with a tooltip naming what to select first:
   `DashboardViewModel.HasSelectedJob`, `SchemeReviewViewModel.HasDiscrepancy` /
   `HasProposal`, `AssignmentsViewModel.HasSelection`,
   `AiKeysViewModel.HasSelection`, `ClassroomViewModel.HasCourse` /
   `HasAssignmentTarget` / `HasSubmissionTarget`, `ReportsViewModel.HasRows`.
   Existing `HasSelection` was reused on Jobs and Results.
2. `AutomationViewModel`: `IntervalMinutes` is now `int?` so the ComboBox's
   transient `null` write is legal; the item list is rebuilt before the value is
   applied and the value is re-notified afterwards. Saving refuses `<= 0` with a
   sentence instead of posting zero. Guarded by a probe that refreshes the page
   and re-checks the selection.
3. Applied the existing `CellText` (character ellipsis) style to the clipped
   grids: Results (corrections, questions, attempts), Assignments, Jobs, Search.
   Widened Results' `Created` to 132 px so the timestamp fits at 100 % and 125 %.
4. Search has two honest states: *Search your local data* before the first query,
   *Nothing found* only after a query returns no rows.
5. Tasks and Assignments show the instruction as muted text and only render the
   monospace box once a real selection exists.
6. Reports' export buttons follow `HasRows`.
7. Results labels are now `Export PDF` / `Export CSV` / `Export Markdown`.
8. Classroom disables the course/assignment pickers and all five actions until
   they have a target, resets stale selections when disconnected, and explains
   with “Connect Google Classroom first: courses and assignments are read from
   the account, never invented.”

## 4. Verification

| Check | Command | Result |
|---|---|---|
| Build | `dotnet build desktop/MathChecker.Desktop.slnx -c Debug` | **0 warnings, 0 errors** |
| Client unit/integration tests | `dotnet test desktop/MathChecker.Desktop.slnx` | **160 passed, 0 failed** |
| Functional UI probe (real window, UI Automation) | `dev_local/ui_interact.ps1` | **17/17 passed** |
| Correction workflow, real backend | `dotnet run --project desktop/tools/MathChecker.Desktop.E2E` | **19/19 passed** — upload → job `COMPLETED` → `9/12 (75%)` → PDF `%PDF` 4536 B → CSV → versions → settings round-trip → clean stop, port free |
| All pages render | `dev_local/ui_audit.ps1` at 1440×875 | 26 pages, 515 controls, **no error banner on any page**, clean exit, no orphan backend |
| Minimum window size | same harness at 900×600 | 26 pages captured; priority columns drop, text ellipsises, pages scroll — no clipped or overlapping controls found |
| Shell states | same harness | palette opens from `Ctrl+K` with 26 pages; compact rail (68 px) keeps navigation and moves the backend chip into the menu strip |

Screenshots: `dev_local/ui_audit/wide/shots/` (1440×875) and
`dev_local/ui_audit/shots/` (900×600); functional states in
`dev_local/ui_audit/interaction/`.

Before/after: the pre-fix captures were inspected during the audit and are
described above, but the harness reuses file names, so later runs replaced those
PNGs. The current images show the fixed state; the before-state descriptions
quote the observed text (e.g. `2026-10-24 02:1`).

## 5. Remaining issues and limits

1. **Classroom and live marking are unverifiable here.** No OAuth client and no
   Gemini key on this machine, so Classroom stays “OAuth client not configured”
   and corrections run in Demo Mode mock marking. The UI is honest about both;
   live-AI equivalence is not proven by this pass.
2. **Demo/role notices are error-styled and leak an internal code.** The red
   banner reads “…cannot connect to a real Google Classroom account … [demo_mode]”
   and Prompts shows “This page is Developer-role only. [developer_only]”. Both
   are expected states, not failures, and the bracketed token is backend
   vocabulary. Fixing this properly needs a structured severity/notice field from
   the backend rather than string matching in the client, so it was left alone.
3. **Remote access offers *Enable public address* even when the backend reports
   `can_enable: false`** (cloudflared missing). The reason is visible in the raw
   payload on that page, but not as a sentence next to the button. `PageAction`
   already accepts a `canRun` predicate; wiring it needs the remote status to be
   parsed into the page view model.
4. **Not exercised in this pass:** drag-and-drop upload through the UI (the API
   upload path is covered by E2E), *Build report* click, *Save settings* click,
   AI key submission, Classroom connect, prompt overrides, factory reset
   (destructive), and Marking desk decisions (last verified 2026-10-21 on a
   seeded reviewable result).
5. **Marking-desk header detail is dense:** the selected-correction header packs
   score, grade, class, confidence, origin and date into one bold block, and the
   Questions grid has no empty-state line when a correction stores no breakdown.
6. Both new harnesses live in `dev_local/` (untracked scratch) by design; promote
   `ui_interact.ps1` into `desktop/` if it should run in CI.

## 6. Files changed

Modified (client only):

```
desktop/src/MathChecker.Desktop/ViewModels/DashboardViewModel.cs
desktop/src/MathChecker.Desktop/ViewModels/SchemeReviewViewModel.cs
desktop/src/MathChecker.Desktop/ViewModels/AssignmentsViewModel.cs
desktop/src/MathChecker.Desktop/ViewModels/AiKeysViewModel.cs
desktop/src/MathChecker.Desktop/ViewModels/AutomationViewModel.cs
desktop/src/MathChecker.Desktop/ViewModels/ClassroomViewModel.cs
desktop/src/MathChecker.Desktop/ViewModels/SearchViewModel.cs
desktop/src/MathChecker.Desktop/ViewModels/ReportsViewModel.cs
desktop/src/MathChecker.Desktop/Views/DashboardView.xaml
desktop/src/MathChecker.Desktop/Views/JobsView.xaml
desktop/src/MathChecker.Desktop/Views/ResultsView.xaml
desktop/src/MathChecker.Desktop/Views/SchemeReviewView.xaml
desktop/src/MathChecker.Desktop/Views/AssignmentsView.xaml
desktop/src/MathChecker.Desktop/Views/AiKeysView.xaml
desktop/src/MathChecker.Desktop/Views/AutomationView.xaml
desktop/src/MathChecker.Desktop/Views/ClassroomView.xaml
desktop/src/MathChecker.Desktop/Views/SearchView.xaml
desktop/src/MathChecker.Desktop/Views/ReportsView.xaml
```

Added: `dev_local/ui_audit.ps1`, `dev_local/ui_interact.ps1`, this report.
No backend, frontend, engine, installer or release file was touched. Nothing was
committed or pushed.

## 7. Page inventory (state after this pass)

| Space / page | Status | Notes |
|---|---|---|
| Desk / Today | WORKING | 8 jobs, 4 metric tiles, attention list; Cancel/Retry gated on selection |
| Desk / Search | WORKING | query returned 12 real rows; pre-search hint distinct from “nothing found” |
| Correct / New correction | WORKING | drop target + pickers; upload path proven by E2E (UI drag not clicked) |
| Correct / Tasks | WORKING | polling table, worker size, gated actions; hint is no longer a fake field |
| Correct / Job log | WORKING | same table without the worker panel |
| Correct / Automatic | WORKING | settings reflect backend; interval survives refresh; Save not clicked |
| Review / Queue | WORKING | honest empty state, Accept/Override correctly disabled with nothing waiting |
| Review / Scheme mismatches | WORKING (empty) | renders with zero discrepancies/proposals; approve/reject need a staged proposal |
| Review / Marking desk | WORKING | opened from Results; per-question decisions last verified 2026-10-21 |
| Results / Results | WORKING | selection → actions, Open paper navigates; PDF/CSV verified by E2E |
| Results / Reports | PARTIAL | scope pickers and empty state render; Build/Export not clicked |
| Results / Bandwidth | WORKING | read-only provider traffic |
| People / Classroom | BLOCKED | no OAuth client on this machine; page states it and disables actions |
| People / Classes | WORKING | 2 classes with real counts; Export class report not clicked |
| People / Students | WORKING | 5 students with real figures; profile not opened this pass |
| People / Assignments | WORKING | 3 assignments, ellipsised class column; Create not submitted |
| Config / Settings | PARTIAL | form populated; Save not clicked |
| Config / AI & keys | WORKING (no keys) | empty state correct; key operations unverifiable without a key |
| Config / Prompts | PARTIAL | editors render, developer-only notice shown as an error banner (see 5.2) |
| System / Storage, Privacy & data, Diagnostics, Accuracy audit, Multi-school, Updates | WORKING (read-only) | real payloads; destructive controls (factory reset) deliberately not run |
| System / Remote access | WORKING (gated poorly) | status rendered; Enable offered while `can_enable: false` (see 5.3) |

## 8. Readiness

**READY WITH LIMITATIONS.** All 26 pages render against the real backend, the
correction workflow passes end to end, actions now either work or say why they
cannot, and the layout holds at the 900×600 minimum. The limits are the blocked
integrations (Classroom OAuth, live Gemini marking), the demo/role notice tone
(item 5.2), remote-access capability gating (5.3) and the untested controls
listed in 5.4.

Highest-value next steps, in order: give backend notices a severity instead of
error-styling expected states; wire `canRun` to the remote status; add empty
states to the marking desk's question grid; promote the interaction probe into
the test suite; then verify Classroom and a live correction with real credentials.
