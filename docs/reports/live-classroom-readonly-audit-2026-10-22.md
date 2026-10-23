# Live Classroom read-only audit — 2026-10-22

## Scope and safety

The desktop client was run against the real MATH_CHECKER_V5 data and the
connected Google Classroom account, but **only with GET/read operations**. No
assignment was submitted, returned, graded, published, attached or edited. No
student profile or classroom membership was changed.

The live data folder contains unfinished automation work (`submit_grade`,
`return_work` and report attachment are enabled). Starting a backend against it
would have resumed that work, so the UI session used an **isolated snapshot**:

* the SQLite database was copied with SQLite's backup API from a read-only
  source connection;
* the copy's `config.toml` had `[testing] only = true` (the application's
  stop-the-line Classroom/Drive write guard), automation disabled, draft-grade
  writes off, large-PDF scheduling off, retention cleanup off, and the idle
  lifecycle off;
* in-flight local jobs in the copy were cancelled so boot recovery could not
  run them;
* Gemini API keys were removed from the copy, so no AI call was possible;
* the copied OAuth refresh token was used only for Classroom **GET** requests.

The canonical `data/app.db` and `data/secrets.toml` were never used as the
backend data directory. Live counts after the session remained unchanged
(80 students, 171 results), and a re-read of the sampled Classroom submission
returned the same state, timestamp and empty grades as before the UI session.

## Authentication and source

| Check | Result |
|---|---|
| OAuth client configured | yes |
| Classroom connected | yes |
| Account identity | verified against Google `userinfo`; email verified and identical to the account stored by the app |
| Active courses from Classroom | 2 |
| Local course rows | 2, matching the live course IDs |
| Session label | **live data** (not Demo Mode) |
| Missing-OAuth state | none |

The connected account email is intentionally omitted from this report.

## Real records loaded

| Record | Count |
|---|---|
| Students in roster | 80 |
| Classroom submission rows | 1,406 |
| Stored correction results | 171 |
| Local assignments | 20 |
| Student used for the detailed profile pass | ID suffix `8835` |
| Student used for empty-state pass | ID suffix `7465` |

## Data reconciliation

* A sampled live Classroom coursework returned the same due date stored locally.
* The same student's live `studentSubmissions` entry returned `TURNED_IN`, late,
  and the identical `updateTime` stored in the local tracking row. Live
  `assignedGrade` and `draftGrade` were both absent, exactly as the profile
  displayed (“Not graded”).
* Profile `8835`: 19 tracked assignments, 13 submitted, 6 not submitted,
  5 stored corrections, 324/554 marks (58.5%), Math Checker average 67%.
  The overview text matched every one of those values.
* The two inspected correction breakdowns summed exactly to their stored totals:
  `44 / 50` (19 questions) and `168 / 209` (55 questions).
* The zero-correction student showed 19 assignments, 3 submitted, 0
  corrections, “Not graded” and “No corrections yet”; no score or trend was
  fabricated.
* Results page read 171 stored corrections, matching the database count.

## Screens and workflows exercised

* Dashboard (live-data label, connected account, counts).
* Classroom page (connected state, read-only).
* Students roster → real student profile.
* Profile Overview, Assignments, Corrections, Progress and Details.
* Assignment status dropdown options (All, Submitted, Outstanding, Late,
  Corrected); the filter counts are covered by unit tests.
* Correction history row switching and question/topic feedback.
* “Open in marking desk” for an existing result (read-only; no decisions made).
* Student with no corrections (empty states).
* Results list and “Choose a correction” empty state.
* Assignments page.
* Window resize to 960×640.

No Edit/Save, Connect/Disconnect, Sync, re-mark, override, approve, upload or
export button was invoked in the live-data session.

## Defects found and fixed

1. **Two different “Outstanding” counts shared one label.** The overview tile
   showed `classroom.not_submitted` while the Status card showed
   `performance.missing` (which also counts a recorded zero). Both were labelled
   “Outstanding”, so a real student displayed 6 in one card and 7 in the other.
   Fixed by labelling the tile **NOT SUBMITTED** with “No work handed in, draft
   or reclaimed”, and the activity figure **Outstanding incl. 0**.
2. **Correction score column clipped** a three-digit score (`168 / 20…`).
   Widened the column and trimmed the date column in the profile history.
3. **Results page claimed a correction was selected when none was.** It showed a
   generic “Correction” header and “No per-question breakdown was stored” before
   any row was chosen. Added `HasSelection` and hid the detail pane until a row
   is selected; the “Choose a correction” state now shows instead.

Changes are in `desktop/src/MathChecker.Desktop/Views/StudentProfileView.xaml`,
`desktop/src/MathChecker.Desktop/Views/ResultsView.xaml`,
`desktop/src/MathChecker.Desktop/ViewModels/ResultsViewModel.cs`, and the
supporting profile view model/tests.

## Commands and results

| Command | Result |
|---|---|
| `dotnet build desktop/src/MathChecker.Desktop/MathChecker.Desktop.csproj` | 0 warnings, 0 errors |
| `dotnet test desktop/MathChecker.Desktop.slnx` | **137 passed, 0 failed** |
| `pytest backend/tests/test_student_profiles.py -q` | **13 passed** |
| `pytest backend/tests -q` | **2,190 passed, 1 skipped** |
| Live UI automation probe, real-data copy | all assertions passed |
| Live Classroom GET sample | state/timestamp matched local record |

## Limitations

* The inspected correction's original paper was not present in local retention
  storage, so the marking desk correctly reported *“This paper's original file
  is missing”*. Mathematical notation and handwritten answers on the page image
  therefore could not be visually verified; the stored marks, question feedback
  and breakdowns were verifiable and matched.
* No new AI correction was run against live data. Existing stored corrections
  were inspected read-only; the engine test suite passed. The isolated copy had
  no AI keys, so a live re-mark was impossible as well as prohibited.
* The profile Details tab was inspected without editing. Live profile edits were
  deliberately not attempted.
* The assignment filter dropdown was opened and its five options were verified.
  Selecting an option through UI Automation was unsupported by that control, so
  the filter result count itself was not exercised through the window; the
  filter logic is covered by `StudentProfileTests` and the backend's resolved
  submission flags.
* The temporary database, secrets copy and screenshots were deleted after the
  audit. Screenshots contained student names and were not retained.
