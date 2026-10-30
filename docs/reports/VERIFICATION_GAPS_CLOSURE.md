# Closing the remaining verification gaps

Date: 2026-10-16. Scope: the three explicitly unverified areas (live Google
Classroom read-only integration, independently verifiable marking accuracy,
installer/UI/deployment testing), plus safety and cleanup.

Every result below names the artefact that backs it. Anything that was **not**
tested says so plainly — nothing here is inferred from a passing build.

| Evidence file | What it proves |
| --- | --- |
| `docs/results/CLASSROOM_READONLY_CERTIFICATION.json` | Live read-only Classroom surface, 37 checks, exit 0 (2026-10-16T22:09:09) |
| `docs/results/LIVE_CLASSROOM_CERTIFICATION.json` | Live marking of real submissions + write refusal, 26/28 (18:30, earlier phase) |
| `docs/results/MARKING_REFERENCE_RESULTS.json` | Reference paper A: 12/12 exact, MAE 0 |
| `docs/results/MARKING_REFERENCE_PAPER_B.json` | Reference paper B: attempted twice, no usable outcome (recorded honestly) |
| `docs/results/UI_WORKFLOW_CHECKS.json` | Browser automation, 66 checks: **65 PASS, 1 SKIP**, 0 page errors |
| `backend/tests/test_defective_scheme.py` | Engine-layer proof that a defective/contradictory scheme is never markable (25 tests) |
| `docs/reference-marking/` | The reference set itself + `REVIEW_SHEET.md` for a human |
| `docs/guides/CLASSROOM_SIGNIN.md` | The exact interactive sign-in procedure |
| Full backend suite | Run this session, re-run after the engine fix: **1936 passed, exit 0** (`docs/results/audit/final_suite.log` holds the earlier phase's 1911-pass run) |

---

## 1. Live Google Classroom integration (read-only)

**The checkout did have a usable credential**: the installation reports
`connected: True` for `<redacted-email>` (`client_configured: True`),
so live reads were verified rather than assumed. No credential value was
searched for, printed, or invented; `docs/guides/CLASSROOM_SIGNIN.md` records the
procedure (client JSON location, Desktop-app client type, consent-screen test
users, 300-second loopback window, the two OAuth flags that a refresh token
depends on) without any secret in it.

Harness: `backend/scripts/verify_classroom_readonly.py` (new). It builds a
throwaway data directory from the real config with `[testing] only = true`
forced on, drives the app's own publication guard **before** fetching a single
student byte, then performs GETs only, then re-reads every submission it touched.

Three live runs were made — the first exposed the missing-evidence defect
(§1 discrepancies), the second proved the fix, the third proved the corrected
label. All three reported the same result: **PASS=37, exit 0, no write escaped**.
The final run (22:09:09) is the recorded one.

| Verified live | Evidence in the report |
| --- | --- |
| Course listing **follows pagination** | 2 courses over 1 page (B2b) |
| Assignment listing follows pagination | 18 assignments, 1 page (B4) |
| Roster listing follows pagination **to the end** | 78 students over **3 pages**; the note records that this is the listing where a real `nextPageToken` appears (B6) |
| Submissions in every state read to the last page | 78 submissions, 1 page; turned-in 24 (D2, E3) |
| Student matching | 5 sampled profiles resolved, 0 errors; 78/78 roster entries carry a stable `userId` (E4, E5) |
| Student emails | **Not granted** by this token (only `classroom.rosters.readonly`): reported as a privacy property, not a pass dressed up as "name + email" (E4) |
| Attachment discovery | 21 submissions carry Drive files; **2 carry only a form/link/YouTube payload** (F1) |
| Attachment metadata + real download | mimeType `application/pdf`; 3,764,654 bytes downloaded with a `%PDF` magic (F2, F3) |
| Permissions are real | two endpoints outside the granted scopes → **HTTP 403**; an invalid token → **HTTP 401** (G1, G2) |
| No mutation | 0 of 5 tracked submissions changed; the comparison provably covers `state`, `assignmentSubmission`, `submissionHistory`, `updateTime` (H1, H3) |

### Writes stayed blocked

With testing-only ON, all eight publishing helpers were driven and each raised
`PublicationBlocked`, with **zero** writes reaching the HTTP transport and a
negative control confirming the gate is a switch and not a wall:

```
testing-only: blocked write a draft grade
testing-only: blocked PATCH  .../studentSubmissions/s
testing-only: blocked POST   .../studentSubmissions/s:return
testing-only: blocked upload report to Drive
testing-only: blocked replace a Drive file
testing-only: blocked share a Drive file
testing-only: blocked modify submission attachments
testing-only: blocked delete a Drive file
```

The earlier live marking run additionally proved it against the **real API
path**: a report-upload probe returned an HTTP refusal with no `drive_file_id`.

### Live (real API) versus mock-only

- **Verified against the live API:** course/assignment/roster/submission reads
  with pagination, student profile resolution, attachment metadata and download,
  scope enforcement (403/401), and the double-check marking path on real
  submissions (earlier phase: 2 real submissions, each marked twice by the live
  model `gemini-3.5-flash-lite`, results stored and read back, and 3 records
  left in the review queue).
- **Refuse-only, never exercised as working:** grade push, report attachment,
  submission return, Drive upload/replace/share/delete. These are verified as
  *refused on the live path* — which is the only claim that matters this phase —
  and their success paths remain mock-only (unit tests).
- **Mock-only:** everything about writing marks back; partial failures, retries
  and duplicate prevention are covered by the unit suite
  (`test_report_dedup_grade_return.py`, `test_automation.py`,
  `test_report_attachment*.py`), not by live traffic.

### Live discrepancies found, and what happened to them

1. **A link-only attachment was scored as a marking failure** (earlier run,
   `S-6b7fe3d1: attachment 1: not a Drive file`). The filter in
   `MathChecker_Distribution/scripts/verify_live_classroom.py` accepted any
   attachment and then demanded a downloadable Drive file. **Fixed**: it now
   selects submissions with a Drive file and *counts and notes* link-only ones
   as unmarkable data instead of failing. The read-only run independently
   confirms the condition is real and common enough to matter (2 of 23).
2. **The read-only harness printed an evidence path it never wrote.** The child
   wrote the snapshot into the throwaway directory, the parent deleted that
   directory, and the parent's own document was never created — yet the run said
   `evidence: docs\results\...` and exited 0. **Fixed**: the parent now reads the
   child's snapshot before cleanup, writes its own record, asserts the write, and
   returns exit 1 rather than reporting a file that does not exist. Proven by
   three consecutive runs: 13734 bytes written, then 13828 bytes after the label
   fix below.
3. **A check label over-claimed.** `E4` said "real profiles (name + email)" while
   the token grants no student emails. **Fixed** to "display name", with the
   absent emails reported explicitly as expected and privacy-positive.
4. **Repeatability finding, reported not "fixed"** (earlier live run):
   `S-2113d5c0: two independent marks agree` failed with `1 of 19 questions
   differed: ['Q9: 0 then 2']`. This is a live model disagreeing with itself on
   one 2-mark question across two passes. The application behaved correctly and
   conservatively: the result was stored as **confidence LOW,
   `review_required: true`** (`result_guard`: verification not `VALID` → LOW →
   review required), so it was never presented as settled, and the second
   student's run (98.6%, i.e. above the trigger) recorded
   `double_check_status: DOUBLE_CHECK_FAILED` with review required as well.
   Nothing was published. The residual risk is inherent to LLM marking and is a
   reason to keep the teacher review gate, not a defect to patch away.

---

## 2. A human-verifiable marking reference set

Created (`backend/scripts/make_marking_reference.py`,
`backend/scripts/verify_marking_reference.py`):

- `docs/reference-marking/manifest.json` — two O-Level-style papers, each item
  carrying the ruling taken clause by clause from the **printed** scheme and its
  basis; the model's own output is never used as ground truth.
- `docs/reference-marking/REVIEW_SHEET.md` — the sheet a teacher signs off.
- `docs/reference-marking/papers/` — the PDFs (questions, student answers).

### Measured result — paper A (`docs/results/MARKING_REFERENCE_RESULTS.json`)

The set holds **16 items (paper A: 12, paper B: 4)**, each mapped in
`manifest.covers` to the case it exists to test. Measured on paper A
(`aggregate`): **12/12 items compared, 12 exact, MAE 0**, 0 over-marked, 0
under-marked, 0 rejected valid alternatives, 0 matching errors, 0
mark-allocation disagreements. App scored 22/29 = 75.86%, provisional grade 7.
Model: a real live model (`gemini-3.8-flash`), not mock.

Categories **measured** via paper A — fully correct (Q1,2,3,5,6,8), incorrect
(Q7), partially correct working (Q9, Q12), correct alternative method (Q5),
equivalent expressions (Q2, Q6), method-and-accuracy (Q1, Q8, Q9, Q12),
follow-through (Q10), missing answer (Q4), ambiguous (Q11), nested subquestion
(Q3). Categories **prepared but not yet measured** all live in paper B:
overlapping/repeated lesson identifier, and `scheme-total-disagrees` (a defective
scheme header), plus a further partially-correct item (Q1c).

### Paper B — attempted twice, **not measured**

`docs/results/MARKING_REFERENCE_PAPER_B.json` records it honestly: 0 items
compared. First attempt (21:00) waited 905 s and left the job in
`WAITING_FOR_API` (the app's "no API capacity, retry with backoff" state).
Second attempt (22:07) failed fast with the app's own reason:

```
FAILED (All configured Gemini models failed. gemini-3.8-flash: No usable Gemini
keys remain (all invalid or disabled). ...)
```

The key records corroborate it (`429 RESOURCE_EXHAUSTED: key quota exhausted`,
`503 UNAVAILABLE ... high demand`). The app's behaviour under this condition is
correct: the job failed with an explicit reason and **no marks were invented**.

The first attempt's own report was preserved before cleanup:
`docs/results/reference_paper_b/paper-b-harness.txt` (app started live, not in
mock mode, accepted the PDF — then the correction never completed) and
`paper-b-expected.json`, the key it would have been graded against: `1a=1,
1b=1, 1c=1, 2=3` — 6 against a declared 7, which *is* the deliberate defective
scheme header this paper exists to test.

Therefore these cases remain **untested**: repeated lesson identifier,
mark-scheme-extraction error (defective scheme header), and any question count
beyond paper A's 12.

### Not certified by a human

`manifest.review.verified_by` is **null**, so the harness refuses to say
CERTIFIED and exits 4 (MEASURED BUT NOT CERTIFIED). Distinguish:

- **Measured** (against a manifest derived clause-by-clause from the printed
  schemes): exact-mark agreement 12/12, MAE 0 on paper A.
- **Untested assumption**: that a competent maths teacher agrees with every
  expected mark. Nothing in this run substitutes for that signature.

Regression cover added for the read path under it: `backend/tests/test_classroom_pagination.py`.

---

## 3. Installer and interactive UI

### The installer

- Located: `MathChecker_Distribution/release/MathChecker_Setup.exe`,
  **53,404,277 bytes; SHA256 `6074d1d0…61ee`** — rebuilt at 22:37 on
  2026-10-16 (see the rebuild note below; the pre-fix build was
  `53,396,790` bytes / `f1ded1ea…75c7`, now archived under
  `previous_builds/20261016-193501/`).
- **Integrity and provenance verified this session**: the hash matches
  `release/SHA256SUMS.txt` **and** `release/release_manifest.json` exactly
  (portable zip likewise: 76,365,101 bytes, `db25948c…8a33`). The manifest pins
  version 5.2.1, build `20261013-rc1`, built 2026-10-16T19:37:29Z, Python 3.14.7,
  `self_contained: true`, 3833 files staged from a 151-entry allowlist. Each
  artifact's declared byte count and SHA-256 were re-derived from the files on
  disk and matched.
- How it is built: `MathChecker_Distribution/scripts/build_release.py` →
  Inno Setup compile of `release/MathChecker.iss` (installs per-user,
  `PrivilegesRequiredOverridesAllowed`, `/CURRENTUSER`).
### The rebuild that put the engine fix into the installer

The engine fix initially reached the source tree only; the staged copies and the
installer were pre-fix. `build_release.py --with-runtime build/runtime` was then
run end to end (exit 0, `RELEASE READY`): production frontend rebuild, 3832 files
staged from the allowlist, privacy/secret scan clean over 3833 files, bundled
runtime imports verified, portable zip assembled, Inno Setup 6.7.3 compiled the
installer, hashes and manifest regenerated. Verified afterwards:

- **Staged copies match the source again.** `diff -rq backend/app
  release_staging/backend/app` now reports only `reporting/assets` (deliberately
  not shipped — they are the build owner's personal logo and signature, so a
  distributed build renders the drawn monogram instead; see the note in
  `release_allowlist.py`) and `__pycache__` (never shipped — the allowlist copies
  only `.py`/`.sql`). The four other files that had drifted
  (`classroom/client.py`, `engine/question_inventory.py`,
  `engine/recover_missing.py`, `workers/child.py`) are byte-identical again.
- **The fix is really inside the shipped artifacts.** The zip contains
  `MathChecker_Portable/backend/app/engine/blueprint.py` dated 2026-10-16 22:18
  with the helper present; the installer compiled from that staging
  (`iscc /DSourceDir=<release_staging>`, the `SourceDir` the generated
  `MathChecker.iss` defaults to).
- **The shipped code was executed, not just compared.** The assertions were run
  against the *staged* module (`sys.path` pointing at
  `release_staging/backend`), so the shipped copy — not the source — was shown to
  keep a clean scheme HIGH, flag the contradictory total, count and flag
  unnumbered questions, reject a `None` number in `validate()`, and refuse the
  same through the override path. All passed.

### Running the installer

- **NOT run.** Installing on this machine would rewrite the **production**
  uninstall registry entry and Start Menu group, because the test install uses
  the same AppId — that is exactly the "never modify the production
  installation" constraint. No VM or second machine was available this session.
  The reusable harness exists and is the next concrete step:
  `python MathChecker_Distribution/scripts/verify_installer.py --setup release/MathChecker_Setup.exe`
  (installs into a clean folder, launches the *installed* copy, marks a PDF,
  checks data lands in `%LOCALAPPDATA%\MathChecker`, then uninstalls and
  re-checks the machine).

### The UI

Browser automation was available (Playwright + system Chrome) and ran against an
**isolated data directory**, never the real one:
`docs/results/UI_WORKFLOW_CHECKS.json` — **66 checks, 65 PASS, 1 SKIP, 0
uncaught page errors**.

- 57 navigation checks: 19 routes × 3 viewports (800/1366/1920), each asserting
  a heading, real content, and **no horizontal overflow**.
- Displayed counts match persisted state: Tasks says 9 where `GET /api/jobs`
  holds 9; Review queue 0 with the empty state; Scheme review 0.
- Workflow checks passed: an automation run; a settings control that really
  moves the backend (toggled the draft-grade classroom setting → `GET
  /api/settings` changed). A pass deliberately does **not** flip the
  public-address/network-exposure switch.
- Unknown route survives; 0 uncaught errors across the whole walk.

One skip left, with its reason — not a silent pass:

- `workflow/clear-activity`: the control is present but **disabled** because the
  visible log was empty, so there was nothing to clear.

The broken-scheme case is no longer skipped. It used to report SKIP, because in
mock mode the engine never opens the file, so a COMPLETED job proves only that
the stub ran. The harness now asks the app's own **deterministic** scheme
validator (`GET /api/assignments/<id>/mark-scheme` — no AI, no network) with the
same bytes it uploads. Run with `--data-dir <the MC5_DATA_DIR the app is using>`:

```
python backend/scripts/verify_ui_workflows.py --url http://127.0.0.1:18748 \\
    --data-dir <data-dir> --out docs/results/UI_WORKFLOW_CHECKS.json

ok   errors/broken-scheme
       expected a broken scheme yields NO markable structure and is never
                reported as confident
       actual   has_mark_scheme=True, processed=False, questions=0,
                question_count=0, confidence=None
```

When a live key **is** available the same run also presses the buttons and adds
`errors/broken-scheme-live-press` on top; in demo mode that press would be
evidence of nothing, so it is not asserted.

---

## 4. Two-machine deployment verification

**Not tested.** Everything above ran on one machine (`E:\MATH_CHECKER_V5`) with
the development tree present. No clean second Windows environment and no VM was
available in this session, so:

- installer install/launch/first-run/uninstall on a clean box: **not verified**;
- "does not depend on the original development directory": **not verified**
  (the harness for it exists — `verify_installer.py`, `verify_portable.py`);
- `docs/results/two_env_e2e_results.json` is dated 2026-10-12 and is **not**
  evidence from this session.

I am not claiming two-machine compatibility.

---

## 5. Safety and cleanup

- No grade, report or submission return was sent to a real student. Across every
  live run the transport write count was 0 and all eight publishing helpers were
  refused.
- No original Classroom record was modified: the tracked submissions were
  hash-compared before and after (0 of 5 changed) and the earlier certification
  re-read everything it touched.
- Cleanup of test-only artefacts, after checking each is not production data:
  the two isolated UI data directories (`mc5ui-*`, `mc5ui2-*`) were deleted; every
  harness throwaway directory was deleted by its own run; the reference harness's
  working copy of the key was deleted with its directory.
- A final sweep removed the six leftover harness directories under `%TEMP%`
  (`mc5-*`), **after** confirming that none held a real (non-demo) `config.toml`
  or `app.db`. The paper-B harness report was copied into
  `docs/results/reference_paper_b/` and byte-compared before its directory was
  removed, so the evidence survives the cleanup.
- No harness process was left running (checked after the last live run). The real
  `%LOCALAPPDATA%\MathChecker` (settings, database, blueprints, cache) is intact.
- Test records created for the reference set live under `docs/reference-marking/`
  in the repo, isolated from any live student record.

---

## 6. Defects found and fixed

| # | Defect | File | Status |
| --- | --- | --- | --- |
| 1 | Link-only attachment reported as a marking failure | `MathChecker_Distribution/scripts/verify_live_classroom.py` | Fixed; the new selector was executed directly against 8 attachment shapes (drive, link, form, youTube, mixed, empty, missing key, driveFile without id) — all 8 as intended — and live data confirms the condition is real (2 of 23) |
| 2 | Read-only harness reported an evidence path it never wrote | `backend/scripts/verify_classroom_readonly.py` | Fixed; verified by 3 live runs, evidence now 13.8 KB on disk |
| 3 | Check label claimed student emails were resolved | `backend/scripts/verify_classroom_readonly.py` | Fixed; the run now reports emails as not granted |
| 4 | Single-page truncation of Classroom listings (fixed in the earlier phase) | `backend/app/classroom/client.py` (`_paginate`) | Fixed; regression test `test_classroom_pagination.py`; live roster read needs 3 pages |
| 5 | `str(None)` treated as a question number, so a defective scheme could read as **verified** | `backend/app/engine/blueprint.py` (`_question_number`, used by `validate`, `structure_report`, `mark_scheme_confidence`, `validation_warnings`, `save_manual_blueprint`) | Fixed; 25 engine-layer tests in `test_defective_scheme.py` |

Defect 5 in detail, because it is the one that would have misled a teacher. Two
gates judged the same blueprint and disagreed, with the reassuring one winning:

- `validate()` accepts `{"question_number": None}` — `str(None)` is the perfectly
  good string `"None"` — so an unnumbered structure passed the gate that decides
  whether it may be used at all;
- `structure_report()` fed the same `"None"` into the numbering sequence, where
  the digit parser correctly dropped it, so a scheme whose questions were **all**
  unnumbered produced no gaps and no duplicates, reported `complete: True`, and
  returned **confidence HIGH / `requires_verification: false`** — telling the
  teacher "no need to verify" about exactly the incomplete extraction the module
  exists to expose;
- the teacher's own repair path (`save_manual_blueprint`) accepted a `None`
  number too, saving it as question "None", through the gate whose docstring
  promises "an unusable structure is rejected".

Now one helper answers "what number does this question have", so both gates
agree; an unnumbered question is counted (`unnumbered_questions`), described in
the warnings, and never reported as complete or HIGH. The false-positive guard is
pinned too: a clean, consistent paper still returns HIGH with no reasons.

**Test state after all edits:** the **full backend suite passes: 1936 passed,
exit 0** (1911 before this work, plus the 25 new engine-layer tests), re-run after
the `blueprint.py` change because that module is core. The code touched in this
phase was also re-checked on its own: **68 passed** (`test_classroom_pagination`,
`test_classroom_auth`, `test_report_dedup_grade_return`,
`test_testing_only_guard`).
The mocked upload path the brief asks about was run directly rather than assumed:
**161 passed** across `test_report_attachment.py`,
`test_report_attachment_api.py`, `test_report_dedup_grade_return.py`,
`test_retry_architecture.py`, `test_automation_reliability.py` (partial failures,
retries, duplicate prevention). Defect 1's repaired selector was exercised
directly (8/8 attachment shapes), because its end-to-end run is the one the API
quota is currently blocking — the fix is verified at the unit level, and the full
path re-runs with paper B.

The engine change (defect 5) was checked against the suites that own that code —
**219 passed** across `test_mark_scheme_confidence`, `test_mark_scheme_validation`,
`test_determinism_blueprint`, `test_submission_validation`,
`test_missing_questions_and_continuation`, `test_subquestion_scheme`,
`test_scheme_audit`, `test_scheme_review_api` — plus **25 passed** in the new
`test_defective_scheme.py`, and then the whole suite again (below), because
`blueprint.py` is a core engine module.

---

## 7. Remaining blockers and risks

1. **Reference paper B is unmeasured** — the live marking path is currently
   unusable: every configured Gemini key is reported quota-exhausted (429) or the
   model is over capacity (503). Re-run
   `python backend/scripts/verify_marking_reference.py --live --ack REFERENCE --only paper-b --timeout 900 --out docs/results/MARKING_REFERENCE_PAPER_B.json`
   once keys have quota.
2. **No human has signed the reference set** (`verified_by: null`). Until a
   competent teacher confirms `REVIEW_SHEET.md` against the schemes, the accuracy
   numbers are measured but not certified.
3. **Installer and second-machine deployment are untested** (no VM/second machine;
   running it here would modify the production install).
4. **Live marking accuracy rests on one 12-item paper** — a small sample. Paper B
   (nested subquestions, repeated lesson id, defective scheme header) is designed
   to widen it. Treat "MAE 0 on 12 items" as encouraging, not as a general
   accuracy claim.
5. **Repeatability**: 1 of 19 questions differed between two independent passes on
   real work. The app contained it (LOW confidence, review required, nothing
   published), but a teacher review gate before release stays mandatory.
6. **The live button-press path for a broken scheme** is still unverified in demo
   mode (no engine reads the file), and its end-to-end run needs the same API
   quota paper B is waiting for. The decidable half — that a broken or
   contradictory scheme can never become a *markable* or *confident* one — is now
   asserted deterministically in `test_defective_scheme.py` and in the UI
   harness's `errors/broken-scheme` check.
7. ~~The release artifacts predate the engine fix.~~ **Resolved** by the rebuild
   described in §3: the installer and portable zip are regenerated from staging
   that matches the source (only `reporting/assets` and `__pycache__` excepted,
   both by design), the new hashes are consistent across `SHA256SUMS.txt`,
   `release_manifest.json` and the files on disk, and the shipped copy was
   executed to confirm it carries the fix. What remains untested is unchanged and
   separate: nobody has **installed** the new artifact on a clean Windows machine
   (see blockers 1–4 and §4).
