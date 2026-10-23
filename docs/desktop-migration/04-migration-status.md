# Migration status and verification record

Date: 2026-10-21 (desktop shell), packaging re-verified 2026-10-22. Everything
below was executed on this Windows machine unless it is explicitly marked
unverified.

## 1. What exists now

A native WPF client (`desktop\`, .NET 10, `net10.0-windows`) that owns the shell
(navigation, backend supervision, settings, logging, dialogs) and reaches every
V5 feature through the existing FastAPI backend. The correction engine, marking
rules, prompts, database schema, Classroom integration and reporting code are
unchanged. The only backend edits for the desktop profile are additive: three
resolved submission flags per assignment row, a full `corrections.history` list
beside the existing 10-item `recent`, and question breakdowns in the demo seed.

| Area | Implementation |
|---|---|
| Shell | `MainWindow` + `MainViewModel`: 26 navigation entries, backend banner, restart, log/data-folder shortcuts, status bar |
| Pages | 20 page views behind 26 navigation entries (Dashboard, Search, New correction, Tasks, Job log, Batched jobs, Automatic, Queue, Scheme mismatches, Results, Reports, Bandwidth, Classroom, Classes, Students + Student profile, Assignments, Settings, AI & keys, Storage, Privacy & data, Diagnostics, Prompts, Accuracy audit, Multi-school, Remote access, Updates) + 3 dialog windows |
| Backend lifecycle | locate → attach or start → wait for `/api/health` → monitor → stop only what it started |
| HTTP | one typed client: uploads (multipart), SSE with polling fallback, downloads with the backend's filename, CSRF header, typed `ApiException` |
| Config | `%APPDATA%\MathCheckerDesktop\settings.json` (paths/port/window state; no secrets) |
| Logging | rolling redacted log under `%LOCALAPPDATA%\MathCheckerDesktop\logs` |
| Packaging | `build_release.ps1` → `MATH_CHECKER_V5\` (client + backend + Python runtime), portable ZIP, per-user Inno Setup installer, manifest |

## 2. Verification evidence

| Check | Command / artefact | Result |
|---|---|---|
| Client builds | `dotnet build desktop\src\MathChecker.Desktop` | 0 warnings, 0 errors |
| Client unit/integration tests | `dotnet test desktop\MathChecker.Desktop.slnx` | **137 passed, 0 failed** |
| End-to-end against the real backend | `dotnet run --project desktop\tools\MathChecker.Desktop.E2E` | **20 checks passed, 0 failed** |
| UI walkthrough (real window, UI Automation) | `evidence\ui-walkthrough.txt` | window opens, 26 navigation items, every page loads its own view, search returns 12 real matches, close exits the process |
| Window operations | `evidence\window-operations.txt` | resize to 1600×900 exactly, clamp at the 900×600 DIP minimum, maximize, minimize, restore, close |
| Backend still starts/stops through the client | E2E log + desktop log | started, `/api/health` ok, stopped, port free |
| Shutdown leaves nothing behind | UI probe + process list | client exits, backend tree stopped, no orphan `python.exe` |
| Packaged build runs without a dev environment | `desktop\test_release.ps1` on a copy of `artifacts\MATH_CHECKER_V5` in `%TEMP%` | **19/19 checks**: window, bundled `runtime\python.exe`, health/dashboard, real correction + PDF export, single instance, clean shutdown, no orphan process or port |
| Backend regression suite | `.venv\Scripts\python.exe -m pytest backend\tests -q` | see §2.1 |

### 2.1 Backend suite

```
.venv\Scripts\python.exe -m pytest backend\tests -q
2190 passed, 1 skipped, 2 warnings in 586.00s (0:09:46)
```

The suite passed on the additive profile fields and demo fixture described in
§1. The full run above completed against the current backend; targeted
`test_student_profiles.py` was also rerun after the final change (**13 passed**).
An earlier run of the same command was interrupted by a 9-minute tool timeout
at ~92 % with no failures; the numbers above come from the completed run.

### 2.2 What the E2E run proves

The E2E tool starts the real backend on an isolated temporary data folder with
`MC5_DEMO=1` and no credentials. It then exercises: health, session mode,
dashboard, classes, assignments, students, results, review queue, pipeline facts,
a **real unified correction upload** (1-page PDF + mark scheme, mock marking is
forced by Demo Mode), job completion, the stored result's marks, PDF and CSV
export, version history, a settings round-trip, then shutdown and a check that
the port is free.

### 2.3 Phase D1 — paper pane

Verified on 2026-10-21 against the real backend in Demo Mode, with an isolated
data folder (`dev_local\d1_data\demo`): one stored 3-page submission and one
result whose stored copy is absent.

| Check | Evidence | Result |
|---|---|---|
| Client build + tests | `dotnet test desktop\tests\MathChecker.Desktop.Tests` | **71 passed, 0 failed** |
| Real page in the pane | `dev_local\desk_probe.ps1` | Results → *Open paper* → `three-pages.pdf · 3 pages`, `Page 1 of 3`, chips `1 2 3` |
| Page strip + keyboard-free controls | same probe | *Next page* → `Page 2 of 3`; *Zoom out* → `125% → 100%`; closing exits the process and its backend |
| Missing original | `dev_local\desk_missing_probe.ps1` | `file_missing`: title *This paper's original file is missing*, the backend's message verbatim, *Re-check storage*, no pager |
| First paint budget | desktop log (`paper: … first page ready in 212 ms`) | 212 ms cold, under the 400 ms budget |

The probes and screenshots live in `dev_local\d1\` (untracked development
fixtures). No Python or frontend file was changed for D1.

### 2.4 Phase D2 — the decision phase

Verified on 2026-10-21 against the real backend in Demo Mode, on the same
isolated data folder, after seeding one reviewable four-question correction
(`dev_local\d2\seed_desk_result.py`).

| Check | Evidence | Result |
|---|---|---|
| Client build + tests | `dotnet test desktop\MathChecker.Desktop.slnx` | **115 passed, 0 failed** (desk commands, keyboard map, provenance, paper pane, DTOs, API) |
| Rail, panel, provenance | `dev_local\desk_decisions_probe.ps1` | question 2 selected as the first needing attention; amber rail edge; `Not marked`; review reason; mark breakdown; scheme check for question 3 |
| Keyboard decisions | same probe | `Space` accepts the attention question (rail glyph becomes ✓, `1 accepted`) and advances, `P`/`N` move without HTTP, digit `2` stages, `Enter` saves (`10 → 9` earned, `1 changed`), `Ctrl+Z` re-posts `3` and returns to question 3, `A` opens the sheet, confirm → `Approved by you` |
| Accept is local | `DeskCommandTests.Accept_writes_nothing_and_advances` | zero POSTs, rail advances, counts move to accepted, the question shows ✓ and loses its amber attention edge |
| Change is one integer | `DeskCommandTests.Committing_a_change_posts_one_integer_and_updates_totals_and_ledger` | one POST, body `"awarded":3`, no decimal, totals from the response, `AI 2 → you 3` ledger |
| Inline failures | `DeskCommandTests.A_failed_save_keeps_the_draft_and_shows_the_backend_error_inline` | mark and draft preserved, `backend_error` in the panel, no page-wide busy state |
| Partial approval | `DeskCommandTests.Approving_a_partial_correction_requires_a_whole_paper_mark`, `A_409_partial_correction_keeps_the_backend_message_and_retries_with_a_mark` | 409 message kept verbatim, mark required, retry posts `earned` |
| Approval writes one decision | `ApproveAsync` probe + test | `POST /override` with `note`, no `earned`; chip becomes *Approved by you*; approve disabled afterwards |

Screenshots: `dev_local\d2\desk_approval_sheet.png` and
`dev_local\d2\desk_decisions.png`.

### 2.5 Phase D3 — the visual system

The desktop client now takes its colour from the same UI kit the browser UI uses.
`ui-style-kit-css` is a CSS package, so it is integrated at the layer it documents:
its published custom properties.

| Piece | File | What it does |
|---|---|---|
| Token sync | `desktop/tools/sync-ui-kit-tokens.mjs` | reads `frontend/node_modules/ui-style-kit-css/dist/visual/paper-editorial.css`, extracts the `usk-*-rgb` roles for the `walnut-clay` theme in dark mode and writes WPF `Color`/`Brush` resources. Re-run it after a kit upgrade. |
| Generated tokens | `desktop/src/MathChecker.Desktop/Themes/KitTokens.xaml` | 19 roles (`bg`, `surface`, `text`, `border`, `primary`, `success`, `warning`, `danger`, `focus`, …). Generated — never edited by hand. |
| Semantic layer | `desktop/src/MathChecker.Desktop/Themes/Tokens.xaml` | merges the generated file and points the application's named brushes at those colours; only shades the kit does not name (hovers, hairlines, ink) are literal. |
| Density layer | `desktop/src/MathChecker.Desktop/Themes/Controls.xaml` | 4 px print-square corners, 30 px controls, 34 px table rows, flat nav rows with a clay rule on the active item, underline space tabs, and a dark menu-bar menu. |
| Shell | `desktop/src/MathChecker.Desktop/MainWindow.xaml` | 30 px menu strip (File / View / Backend, palette field, backend state), 220 px rail, 34 px page-header strip, 24 px status bar. |
| Dashboard | `desktop/src/MathChecker.Desktop/Views/DashboardView.xaml` | the old 196 px hero card is replaced by a four-tile metric strip; the table and attention list follow below. |

Verified on 2026-10-22 in Demo Mode against the running backend, with window
sizes set through the client's own settings file (this machine runs at 125 %):

| Check | Command / artefact | Result |
|---|---|---|
| Build | `dotnet build desktop/MathChecker.Desktop.slnx` | 0 warnings, 0 errors |
| Client tests | `dotnet test desktop/MathChecker.Desktop.slnx` | **137 passed, 0 failed** |
| Tokens regenerate from the kit | `node desktop/tools/sync-ui-kit-tokens.mjs` | 19 roles written, palette resolves at runtime |
| Dashboard at 1440×875 px | `dev_local/theme/final_1440.png` | four-tile strip, table with student/status/stage/created, attention list, no empty-state over real rows |
| Dashboard at 1280×800 px | `dev_local/theme/final_1280.png` | timestamp column drops at the 480 DIP breakpoint, no clipped text |
| Dashboard at 1125×750 px (900 DIP minimum) | `dev_local/theme/final_1125.png` | stage text ellipsises instead of cutting a word |
| Compact rail | `dev_local/theme/final_rail.png` | 68 px icon rail, backend chip moves into the menu strip |
| Command palette | `dev_local/theme/final_palette.png` | opens from the field or `Ctrl+K`, filters to `Students` for `stud`, toggles closed |
| File menu | `dev_local/theme/final_menu.png` | dark popup with data folder / log / settings / exit |

Screenshots are captured with `dev_local/sized_shot.ps1` (sizes the window through
the settings file, then captures it) and `dev_local/page_shot.ps1` (navigates by UI
Automation, then captures); both are DPI-aware so the image matches the window
in physical pixels.

### 2.6 Student profile

The backend profile endpoint already existed and was truthful, but the desktop
used it as a raw JSON payload in a `TextBox` under the roster; editing was two
sequential text prompts and there was no assignment table, correction detail or
progress view. The profile is now a first-class view inside the Students page.

Verified on 2026-10-22 in Demo Mode against the real backend, on a fresh
data folder (`dev_local\spdata_edit`):

| Check | Evidence | Result |
|---|---|---|
| Profile contract | `pytest backend/tests/test_student_profiles.py` | **13 passed**: verified submission states, never inferred from local results, resolved per-row flags, full history |
| Client profile tests | `dotnet test desktop/MathChecker.Desktop.slnx` | **137 passed**: backend figures and nulls, filters, two-student isolation, save path, topic/trend maths, empty breakdown, 404, history/trend cap |
| Open from the roster | `dev_local/profile_edit_probe.ps1` | selecting Student B replaces the roster with the profile; **Back to students** restores it |
| Student overview | `dev_local/theme/sp_b_overview.png` | 3 assignments, 2/3 submitted, 1 outstanding, 18/20 + 11/15 = 29/35 (82.9%), Math Checker average 81.7%; each figure names its source |
| Assignment history | `dev_local/theme/sp_b_assignments.png` | 3 real rows with update/due dates, submission, late, Classroom grade, Math Checker marks and correction state; status filter and sortable headers |
| Correction detail | `dev_local/theme/sp_b_corrections.png` | 2 stored corrections; Q1 7/8 (“mark lost in the final step”), Q2 6/6, Q3 5/6 — the same values as the stored result |
| Progress | `dev_local/theme/sp_b_progress.png` | score trend from stored percentages and topic bars aggregated from stored question marks (5 topics, question counts) |
| Edit details | same probe | nickname `Bee`, class `10B`; backend re-read reports `nickname='Bee' class='10B' display='Bee'` |
| Empty student | `dev_local/theme/sp_student_d_overview.png`, `..._corrections.png` | 1 assignment, 0 submitted, “Not graded”, “No corrections yet”, honest question empty state — no synthetic scores |
| Resize | `ViewSmokeTests` (900×600) + probe at 1280×820 | every tab lays out with no binding errors; DataGrid columns share width via `TableLayout` |

The backend's `history` field is additive; `recent` is unchanged for the web UI
and report builder. The desktop preloads question detail for the 10 newest
corrections, loads an older selected correction on demand, and caps the trend at
12 bars so a long history stays readable.

The feature was also exercised against the real connected Classroom account and
a read-only snapshot of the live database. That pass, its safety controls and
the three UI defects it found (Outstanding label ambiguity, clipped correction
score, and the Results page's false selected-correction empty state) are
documented in
[`docs/reports/live-classroom-readonly-audit-2026-10-22.md`](../reports/live-classroom-readonly-audit-2026-10-22.md).

## 3. Defects found during verification and fixed

| Defect | Cause | Fix | Regression evidence |
|---|---|---|---|
| Several pages rendered only a title (Correction, Tasks, Reports, Classroom, Students, Settings, AI & keys, Prompts) | display-only `TextBox.Text` bindings default to two-way; WPF throws for read-only properties and aborts the page layout | explicit `Mode=OneWay` on every read-only `TextBox`/`ProgressBar`/`Run` binding | UI walkthrough: all pages render their controls and data; desktop log has no `InvalidOperationException` |
| "Scheme mismatches" page showed no view | `SchemeReviewViewModel` had no `DataTemplate`, so no view existed | new `SchemeReviewView` (discrepancies, proposals, decision buttons, raw payload) + template | UI walkthrough: `VIEW SchemeReviewView`, 5 buttons, backend summary |
| Students table was always empty while the summary said "5 students" | rebuilding the class filter list cleared the `ComboBox`, WPF wrote `null` back to `ClassFilter`, and the filter then excluded every student | normalise `null`/empty to "All classes", restore the previous selection after rebuilding, compare with `Ordinal` | `StudentsPageTests` (3 tests): 5 rows after load, null selection does not hide rows, filters survive reload |
| Closing the window did not exit the process (backend stayed alive) | `Close()` called from inside a cancelled `Closing` handler is ignored by WPF | finish with `Application.Current.Shutdown()` after the async cleanup | UI probe: `CLOSED process-alive=False`; packaged run: exit + no orphan python |
| Approval sheet's whole-paper hint was clipped ("Leave empty to approve the e…") | the hint sat in a horizontal `StackPanel`, so it never wrapped | moved it to its own wrapping line under the mark field | `dev_local\d2\desk_approval_sheet.png`: full hint visible |
| Status bar glued the desk status onto the data-folder path ("…\demoQuestion 3 set back…") | `DockPanel.LastChildFill` made the status text fill the row after a left-docked sibling | `LastChildFill="False"` so each status item keeps its dock | `dev_local\d2\desk_decisions.png`: folder left, status right |

Additional hardening done in the same pass: `ProgressBar.Value` and inline `Run`
text bindings were made one-way as well, so a future style change cannot
reintroduce the layout abort.

## 4. Blocked (credential or environment dependent)

| Feature | Why blocked | What the app does instead |
|---|---|---|
| Google Classroom connect / courses / submissions / grade push / report attach | no OAuth client or Google account on this machine | Classroom page shows "OAuth client not configured" from the backend, all buttons wired but disabled by that state |
| Live Gemini corrections (non-demo marking) | no Gemini API key configured | corrections run in Demo Mode mock marking; the app and dashboard label that state honestly |
| Sign-in in public mode | needs a public-mode backend instance and an account | local mode needs no sign-in; the sign-in window is implemented and only appears when the backend requires it |
| Automated Classroom worker / bulk upload / grade boundaries | needs a connected Classroom | page wired to the real endpoints, honest empty/blocked states |
| Installer install/uninstall | verified 2026-10-22 with Inno Setup 6.7.3 | **11/11 checks**: silent per-user install, installed launcher opens the window, backend answers, dashboard served, clean exit, uninstall removes the program folder |
| Remote access toggle | would expose this machine through the tunnel; not appropriate to enable here | page reads `/remote/status`, `/access/state`, `/access/public-address`; both buttons call the real endpoint and were not exercised |

## 5. Known limitations

* Batch/large-PDF pipeline is wired (`Batched jobs`) but only listed; no >30-page
  real document was pushed through it in this pass.
* Reports are built from live endpoints, but only the E2E PDF/CSV exports were
  inspected; the report builder form was not exercised end-to-end.
* The profile's **Updated** column is Classroom's assignment/submission
  `update_time`. The tracking tables store no submission timestamp, so the UI
  does not invent a “submitted at” column.
* Avatars are initials only; the student schema has no profile image.
* Topic figures aggregate the question breakdowns of the 10 newest corrections
  (one bounded batch, loaded once). Older history is listed and can be opened,
  but it is not folded into the topic chart.
* Student profile editing uses the same local single-teacher access model as the
  rest of the desktop app; there is no per-role permission editor. The backend
  validates lengths and only updates students it already knows.
* Automation worker was toggled only in its off state (nothing to process without
  Classroom).
* No code signing; SmartScreen warns on unsigned builds.
* Demo Mode forces mock marking, so the E2E run does **not** prove live-AI
  equivalence. The engine code path is the same one the browser UI uses.

## 6. Files added

```
desktop\MathChecker.Desktop.slnx
desktop\src\MathChecker.Desktop\          22 view-model files, 20 page views + 3 windows, API client,
                                          backend host/locator, settings, logging, converters
desktop\tests\MathChecker.Desktop.Tests\  137 tests (API contracts, backend host, locator,
                                          settings, SSE/upload, students page, student
                                          profile, view rendering)
desktop\tools\MathChecker.Desktop.E2E\    end-to-end runner against the real backend
desktop\installer\app.ico                 neutral placeholder icon
desktop\installer\MathCheckerDesktop.iss  per-user Inno Setup 6 installer definition
desktop\build_release.ps1                 staged bundle + portable ZIP + installer + manifest
desktop\test_release.ps1                  runs the staged release like a user (window, backend, correction, shutdown)
desktop\src\MathChecker.Desktop\Core\AppInstance.cs  single-instance guard for the launcher
docs\desktop-migration\01-audit-and-architecture.md
docs\desktop-migration\02-feature-inventory.md
docs\desktop-migration\03-build-and-release.md
docs\desktop-migration\04-migration-status.md   (this file)
docs\desktop-migration\05-setup-and-development.md
docs\desktop-migration\evidence\ui-walkthrough.txt
```

No file outside these trees was modified. `backend\app`, `frontend\` and the
distribution scripts are untouched.

## 7. Handoff to the UI design phase

Presentation is deliberately plain: default WPF controls, one `App.xaml` style
sheet (`PageTitle`, `Muted`, `Card`, `ErrorBanner`, `JsonBox`), one `DataTemplate`
per page. Business logic lives in view models; views bind only to properties and
commands, so a redesign can replace any `Views\*.xaml` without touching the
engine, API client, backend host or models.

Views ready to receive the design (each has a view model with loading, error,
empty and busy state):

* `DashboardView`, `SearchView`, `CorrectionView`, `JobsView` (Tasks + Job log),
  `AutomationView`, `ReviewView`, `SchemeReviewView`;
* `ResultsView`, `ReportsView`, `UsageView`;
* `ClassroomView`, `ClassesView`, `StudentsView`, `AssignmentsView`;
* `SettingsView`, `AiKeysView`, `PromptsView`, `JsonPageView` (Storage, Privacy,
  Diagnostics, Accuracy audit, Multi-school, Remote access, Updates);
* shell pieces: `MainWindow` (navigation + status), `SetupWindow`,
  `SignInWindow`, `TextPromptWindow`.

Shared contracts a redesign must keep: `PageViewModel` (Title, IsBusy, Error,
Status, RefreshCommand, `LoadAsync`), `AppSession` (Api, Host, Dialogs, Settings,
Log, GoTo, Notify), and the `DataTemplate` mapping in `App.xaml`.
