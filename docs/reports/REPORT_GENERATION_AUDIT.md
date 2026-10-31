# Report-generation audit — log forensics, root causes and fixes

Date: 2026-10-12 · Scope: generating, saving, attaching, exporting and opening
correction reports, plus the report-related failures recorded in the app log.

Nothing in the user's data was deleted or rewritten. Every experiment ran
against a **copy** of `data/app.db` in a temporary directory; the real database,
the four parked jobs and the log file are untouched.

---

## 1. Logs inspected

| Log | Location | Size / range | What it holds |
|---|---|---|---|
| Application runtime log (JSON lines) | `data/logs/app.log` | 16.3 MB, 70 878 records, 2026-10-05 → 2026-10-12 | every component: `automation`, `app`, `classroom.*`, `gemini.*`, `security`, `testing_guard`, `pypdf.*` |
| Automation job log (per job) | `automation_log` table in `data/app.db` | 23 automation jobs, 30 results | state transitions, per-step failures, retry decisions |
| Report attachment state | `report_attachments` table | per result | honest per-stage status (`VERIFIED`, `UPLOAD_FAILED`, …) |
| Release build logs | `MathChecker_Distribution/logs/build_release_rebuild.log` (git-ignored local transcript), `build_release_final.log` | portable/installer build | release-time validation |

Aggregated by event and level, the report-relevant failures were:

| Count | Component | Event | Meaning |
|---|---|---|---|
| 1 246 | `automation` | `workflow step failed permanently` (`REPORT_FAILED`) | 4 distinct jobs parked on report validation |
| 50 973 | `automation` | `workflow step failed permanently` (`CORRECTION_FAILED`) | **one** job re-failing for 32 h |
| 208 | `automation` | `workflow step failed permanently` (`step_error`) | one job, correction side |
| 11 | `testing_guard` | `testing-only: blocked upload report to Drive` | guard doing its job, misreported downstream |
| 8 | `app` | `unhandled error` (dead Google grant) | 500s for an actionable condition |
| 1 | `app` | `unhandled error` (`'status'`) | one unreproducible 500 |
| 1 | `app` | `unhandled error` (testing-only block) | 500 for the report/grade write gate |
| 1 230 | `gemini.proxy` | `key rotation skipped (re-upload too large)` | correction side, out of scope here |

---

## 2. Errors discovered, root causes and fixes

### A. `REPORT_FAILED / report_invalid` — "the report is missing expected content"

* **Impact:** the correction succeeded, the marks were correct, but the report
  was never generated, validated or delivered. Four jobs were parked
  (`89f100a0` Student E, `6111b206` Student G, `71566a94` Student D, `d4dc86eb`
  Student A), and 1 246 log lines were produced.
* **Root cause (reproduced):** the automation worker rendered the **raw** result
  row while every other surface renders the **decorated** identity.
  `engine.service.get_result()` returns `student_name = 'Student F'`; the API
  export path calls `student_identity.decorate()` first, so the teacher's copy
  printed `'Student E'`. The automated PDF therefore **disagreed
  with the downloaded PDF about which student the report was about**, and the
  pre-upload validation compared it against the Classroom roster name on the
  job → mismatch → `StepFailure(transient=False, REPORT_FAILED)`.
  Reproduced byte-for-byte: raw render printed `Student F` → validation
  `False`; decorated render printed `Student E` → validation
  `True`.
* **Fix:**
  * [runner.py](../../backend/app/automation/runner.py) — new `_decorate()` /
    `_report_result()`; the report render, the report validation, the attachment
    and the report-only retry now all use the canonical identity
    (`display_name`, `classroom_name`, `nickname`, `class_name`), the same row
    the results page, the exports and the Drive filename use.
  * [pdf_validation.py](../../backend/app/automation/pdf_validation.py) — validation
    now takes `expected_any` (any one of a student's names is enough, instead of
    demanding one specific spelling) and compares **whitespace-normalised**
    text, so a name that wrapped onto two lines in the PDF is no longer
    reported as missing.
  * [runner.py](../../backend/app/automation/runner.py) `_expected_text()` — returns
    `(must_contain, any_of)`: the assignment title the renderer actually prints
    (read from the same coursework row) must appear, and any of the student's
    names must appear.
* **Verification:** all five jobs that previously failed
  (`REPORT_FAILED`, incl. the one in `REPORT_UPLOAD_FAILED`) now render real
  10–11-page reports that pass the validator, with the correct canonical name
  and marks.

### B. `REPORT_UPLOAD_FAILED` — "Google Drive upload failed: TESTING ONLY: …"

* **Impact:** the local stop-the-line guard (intentional) was recorded as a
  Google Drive failure, the job burned all 3 attempts and parked as a Drive
  problem (job `35f84682`), and the direct API path answered the same condition
  with `HTTP 500 internal_error` (log 2026-10-12 09:12:50).
* **Root cause:** `PublicationBlocked` raised inside the HTTP client was caught
  by the generic `except Exception` in `attach_report` → `STATUS_UPLOAD_FAILED`;
  the runner mapped that to a **transient** `REPORT_UPLOAD_FAILED`; the API's
  global handler had no case for it and fell through to 500.
* **Fix:**
  * [report_attachment.py](../../backend/app/classroom/report_attachment.py) — new
    honest status `STATUS_TESTING_BLOCKED` (+ `ERROR_TESTING_ONLY`), checked
    **before** any write and also caught if the flag races; the message says
    nothing left the machine and points at the setting. Deliberately *not* in
    `FAILED_STATUSES`, so it cannot be mistaken for a Drive failure.
  * [runner.py](../../backend/app/automation/runner.py) → `ATTENTION_REQUIRED /
    testing_only_blocked`, `transient=False` (no wasted retries), parked for a
    human, and retryable at the report stage once the flag is off.
  * [main.py](../../backend/app/main.py) — dedicated `PublicationBlocked` handler →
    **403 `testing_only_blocked`**; the generic handler also recognises it.
  * [routes_all.py](../../backend/app/api/routes_all.py) — the report upload action
    and `_classroom_error()` map the guard to 403 + `TESTING_ONLY_BLOCKED`
    instead of 502 "could not reach Google".
* **Verification:** end-to-end in a sandbox (real attach path, guard ON): final
  state `ATTENTION_REQUIRED / testing_only_blocked`, `attempts=1` (was 3),
  parked 10 years, and **zero** upload/attach/share calls attempted.

### C. "workflow step failed permanently" ×50 973 on ONE job

* **Impact:** 32 hours of the scheduler re-running the same permanently failed
  job (median gap 0.17 s), a 16 MB log and wasted API/quota calls.
* **Root cause:** the failure was an expired Google grant classified down the
  transient path, and the job kept being picked up by the scheduler tick.
* **Status:** **not reproducible in the current source.** The parking guard
  (`PARK_SECONDS` + `store.RECOVER_HORIZON`, which exists specifically to keep a
  parked job parked across boot recovery) is present, and the real database now
  shows every failed job parked ≈3 647 days into the future. The classification
  of an expired grant was also already corrected
  (`classify_exception` → `ATTENTION_REQUIRED / requires_auth`).
* **Fix:** no source change; added a regression test that a permanent failure
  parks, survives `recover_in_flight()`, is never returned by `next_runnable()`,
  and is re-armed only by a manual Retry.

### D. `KeyError: 'status'` — one unhandled 500 (2026-10-08 11:37:24)

* **Status: UNRESOLVED (evidence insufficient).** The log recorded only
  `{"event": "unhandled error", "error": "'status'"}` — no request path, no
  stack trace — and the surrounding 20 s hold nothing but the automation
  retries and token refreshes. Reading 16.3 MB / 70 878 records found no second
  occurrence, and every in-scope `["status"]` read on a possibly-sparse dict
  (`_job_public`, `_report_rows`, `_batch_public`, `_publication`,
  `attach-report`) was inspected and is fed by a `SELECT *`/explicit column
  list that includes `status`.
* **Fix (diagnosability, not suppression):**
  [main.py](../../backend/app/main.py) now records `method`, `path`, `exc_type` and a
  **traceback** with every unhandled error, and
  [errors.py](../../backend/app/errors.py) `brief_traceback()` formats from the
  exception's own chain so it still works outside an `except` block. The next
  occurrence is therefore traceable; nothing was hidden or swallowed.

### E. Known remaining (out of scope for report delivery)

* `gemini.proxy: key rotation skipped (re-upload too large)` (1 230) — a large
  submission whose Gemini file belongs to a rotated key cannot be re-uploaded,
  so the correction fails permanently (job `7f787b2b`, 208 lines). This is the
  correction engine's file-ownership path, not the report chain; it is recorded
  here because it is the remaining reason a report is never produced for those
  submissions.
* 1 589 `generate_content failed` / 1 596 `key entered cooldown` /
  1 034 provider cooldowns — Gemini capacity, handled by the existing retry
  policy (`job parked for API capacity`, `attempt budget untouched`).

---

## 3. Fixes implemented (files changed)

| File | Change |
|---|---|
| [backend/app/automation/runner.py](../../backend/app/automation/runner.py) | render/validate/attach now use the canonical student identity; `_expected_text()` returns must-contain + any-of; `STATUS_TESTING_BLOCKED` → non-retryable `ATTENTION_REQUIRED/testing_only_blocked` |
| [backend/app/automation/pdf_validation.py](../../backend/app/automation/pdf_validation.py) | `expected_any` (alias) support; whitespace/case-normalised comparison; `missing` reported in the result |
| [backend/app/classroom/report_attachment.py](../../backend/app/classroom/report_attachment.py) | `STATUS_TESTING_BLOCKED` / `ERROR_TESTING_ONLY`; guard checked before the first write and caught if it races |
| [backend/app/api/routes_all.py](../../backend/app/api/routes_all.py) | testing-only gate → 403 `TESTING_ONLY_BLOCKED` (upload action + `_classroom_error`) |
| [backend/app/main.py](../../backend/app/main.py) | `PublicationBlocked` handler → 403; unhandled errors log method/path/type/traceback |
| [backend/app/errors.py](../../backend/app/errors.py) | `brief_traceback()` works from the exception's own chain |

No exception was silenced, no grading rule was touched, no report was
regenerated into fake success, and no log line was deleted.

## 4. Regression tests added

| Test | Guards |
|---|---|
| [test_report_delivery_fixes.py](../../backend/tests/test_report_delivery_fixes.py) (9 tests) | wrapped-name matching, any-of aliases, full must-contain, case/spacing; the runner renders the canonical identity; the automated report and the teacher download agree; testing-only is its own non-retryable state; a testing-only hold is retryable at the report stage; a permanent failure stays parked and survives boot recovery |
| [test_report_attachment.py](../../backend/tests/test_report_attachment.py) | `test_testing_only_refuses_before_any_google_write` — no upload/attach call, honest status/message/state |
| [test_classroom_error_reporting.py](../../backend/tests/test_classroom_error_reporting.py) | the guard is registered as its own 403 gate; the handler answers 403 not 500; an unhandled error records route + type + traceback |

Two of the identity tests fail with the fix reverted (verified by temporarily
restoring the old render call), i.e. they reproduce the production defect.

Full backend suite: **1 578 passed** (`pytest -q`, 5 min), plus a compile check
of every edited file.

---

## 5. Required result table

| Item | Result | Evidence |
|---|---|---|
| Logs inspected | **DONE** | `data/logs/app.log` (16.3 MB / 70 878 records, 10-05→10-12), `automation_log`, `report_attachments`, release build logs |
| Errors discovered | **DONE** | §2 A–E above, with counts |
| Root causes | **DONE** | identity drift (A), guard misclassification (B), park-loop already guarded (C), missing diagnostics (D) |
| Fixes implemented | **DONE** | §3 — 6 source files |
| Regression tests | **DONE** | §4 — 13 new tests, 2 proven to fail without the fix |
| Report generation | **PASS** | 5/5 previously failing real jobs now render valid reports; runner end-to-end in a sandbox reaches the report/attachment stages |
| PDF integrity and content | **PASS** | 11-page PDF, `%PDF-`…`%%EOF`, canonical name, `42 / 47`, `89.4%`, grade 8, assignment title; CSV/MD agree; the unsupported format returns a clean `400 bad_report_format` |
| Storage and export | **PASS** | PDF/CSV/MD exported through the packaged app's HTTP interface with correct filenames/content types; export after a full restart returns the identical 29 693-byte report; no result or report files were deleted |
| Google Classroom attachment | **BLOCKED (no credentials)** | no Google account in this environment; the local no-write path is verified end-to-end (guard ON → 0 upload/attach/share calls, honest state). Uploads cannot be confirmed remotely, so this is not claimed as a pass |
| Portable distribution | **PASS** | rebuilt `RELEASE READY` (3 819 files); the packaged copies of all six fixed files are byte-identical to source; the packaged app launched from `E:\MC Report Verify Ünit (fix)\Math Checker (v5.2.1)` rendered a valid report for the previously failing job with its own bundled runtime + dependencies |

The packaged-runtime evidence (a real student's PDF/CSV/Markdown report and the
one-off launch check) has been removed from the repository because it contained
student data. The findings and measurements above remain; re-run the report
workflow in a throwaway data directory if the evidence needs to be reproduced.

## 6. What is still open

1. **`KeyError('status')`** — one 500 that cannot be reproduced from the log
   alone; it now records path + traceback so the next occurrence is locatable.
2. **Large-PDF Gemini 403 after key rotation** — corrections that cannot upload
   their file never reach the report stage (§2 E).
3. **The 4 parked `REPORT_FAILED` jobs and 1 `REPORT_UPLOAD_FAILED` job** are
   left exactly as they are: with the fixes they repair through the existing
   Retry button (proven in a sandbox copy), and re-running them against the real
   database would send real Classroom writes, which this audit must not do.
4. **Live Classroom upload/attachment** was not exercised for lack of
   credentials.
5. Minor, unrelated to reports: `release/SHA256SUMS.txt` is written with CRLF
   line endings, so `sha256sum -c` on a Linux/Git-Bash shell reports
   `FAILED open or read` even though both artifact hashes are correct (verified
   independently). Cosmetic for a Windows-only distribution, but it makes the
   shipped checksum file unverifiable on a Unix-like machine.

### Artifacts re-verified after the rebuild

`MathChecker_Portable.zip` (76 249 318 B) and `MathChecker_Setup.exe`
(53 333 706 B) — both SHA-256 hashes in `SHA256SUMS.txt` verified OK.
