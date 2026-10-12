# Automatic page — Retry / Approve reliability fix

Audit of every interactive control on **Automatic Correction**
(`frontend/src/pages/AutomationPage.tsx`) from the button down to the job row,
the fixes that were applied, and what was actually tested.

Buttons audited: `Check now`, `Review mode`, `Enable/Disable automation`,
`Turn ON/OFF` (background worker), report-detail `Summary/Standard/Full`,
polling-interval buttons, per-assignment checkboxes, and the per-job
`Approve & Send`, `Retry`, `Retry report` (list and drawer).

---

## Root causes found and fixed

### R1 — Retry reported success for work it never did (backend + frontend)

`runner.retry()` returned the job UNCHANGED and the route answered **HTTP 200**
whenever the status was not retryable — a job still running, an already-finished
job, or a review-mode hold. The frontend never looked at the response and always
toasted *"Retrying from the failed step"*. So the button "worked" while nothing
happened.

* `app/automation/runner.py::WorkflowRunner.retry` now refuses with
  `StepFailure(code="not_retryable")` and a message that names the real action
  (`retry_refusal`). The route maps it to **409** and keeps the runner's code.
* `app/automation/routes.py::retry_job` catches `StepFailure` (it previously had
  no handler at all, so a raise would have been a 500).

### R2 — Retry restarted the whole pipeline instead of the failed step

The resume point was `RESUME_FROM.get(status, states.CORRECTING)`: **every**
`ATTENTION_REQUIRED` and `BLOCKED_NOT_MODIFIABLE` job silently defaulted to
`CORRECTING`, i.e. re-validate the result → **re-render the report** →
re-upload/re-attach → re-publish the grade → re-return. A retry after a blocked
step therefore created a second report round-trip, and for a review hold it
re-drove the entire pipeline *underneath* a pending teacher decision.

* `app/automation/states.py` gains `retry_step(job)` — the single source of truth
  for the resume point — plus `BLOCKED_STEP_FROM_CODE` (the step each recorded
  attention code blocked: `attach_disabled`, `no_submission_target`,
  `grade_disabled`, `return_disabled`, `grade_unavailable`, `requires_review`,
  `result_missing`, `correction_job_missing`), `DECISION_CODES`
  (`review_mode_await_teacher`) and `_evidenced_step()` for the fallback case
  (e.g. expired Google authorization) that can only resume from the furthest
  step the job has *evidence* for. No resume point can re-run the AI correction
  or re-render the report.
* `routes._job_public` now advertises `resumable` from that same function (plus
  `retry_from`, the step a retry will run), so the button and the backend can no
  longer disagree. On this machine's live data that alone removed the Retry
  button from all **18** review-mode holds, where the correct action is Approve.

### R3 — Retry appeared dead for up to two minutes (stale claim)

After a crash the job kept the dead runner's claim; `store.claim()` refuses a
claim younger than the 120 s lease, so the re-armed job sat still and the button
looked ignored.

* `retry()` now makes the job due immediately and drops the claim **only when it
  is stale** (`store.CLAIM_LEASE_SECONDS`, now shared instead of a magic 120 in
  three places). A *fresh* claim means a live worker owns the job, so it is left
  alone and the retry runs the moment that worker releases it — the step can
  never run twice.

### R4 — Approve marked a job COMPLETE without publishing anything

`approve()` ran its report + grade stages only for `ATTENTION_REQUIRED` and its
return stage only for `GRADE_VERIFIED / GRADE_DRAFTED / RETURNING`. A job parked
in `REPORT_UPLOAD_FAILED` or `ATTACHMENT_FAILED` (both in `APPROVABLE_STATES`)
therefore fell straight through to **`COMPLETE`** — a recorded success with no
report sent, no grade and no return. A crash-left `GRADE_DRAFTED` was returned to
the student with only a **draft** grade.

* The two failed-report stages now run the report stage, then the grade stage,
  then the return, exactly as the method's own docstring promises ("resumes at
  the FAILED stage"); `GRADE_DRAFTED` runs the grade stage so an approved job is
  never finished on a draft. `GRADE_VERIFIED` is deliberately untouched: it
  means the grade is already published, and the existing contract test
  (`test_approve_resumes_at_the_failed_stage_without_redoing_earlier_ones`) still
  pins that.

### R5 — Approve lost the reason for a refusal

`routes.approve_job` hard-coded `code="not_approvable"` for every `StepFailure`,
so a "no result to approve yet" came back as "not approvable". It now returns the
runner's own code (`no_result`, `grade_unavailable`, `not_approvable`).

### R6 — Frontend: duplicate clicks, no feedback, success claimed too early

* `approveJob` used one global `busy` flag and the row spinner was gated on
  `busy && detail?.id === j.id` — so clicking Approve in the list gave **no**
  feedback and the button was **not** disabled (a real double-approval window).
* `retryJob` / `retryReport` had **no** in-flight guard at all: N clicks = N
  concurrent POSTs.
* `approveJob` derived its success toast from `job.grade`, and both actions
  dropped the response's missing fields into the drawer (`_job_public` carries no
  `publication`/`result`/`attachment`), blanking the evidence panels.

Fixes in `AutomationPage.tsx`: a per-job `pending` map backed by a ref (so the
guard is decided synchronously at click time), `loading=` on every Retry /
Approve / Retry-report button (the shared `Button` disables while loading),
success toasts only when the returned row confirms it (`status === "COMPLETE"`
for Approve, `report_uploaded` for the report repair, "still retryable" for a
retry that did not start), errors shown with the backend's message, and a
`refreshDetail()` re-read of the job so the drawer shows verified per-stage
evidence instead of blanks.

---

## Files changed

| File | Change |
| --- | --- |
| `backend/app/automation/states.py` | `retry_step()`, `_evidenced_step()`, `BLOCKED_STEP_FROM_CODE`, `DECISION_CODES`, docstring |
| `backend/app/automation/runner.py` | `retry()` policy + refusal + stale-claim handling, `retry_refusal()`, `approve()` stage fix for failed-report stages and `GRADE_DRAFTED` |
| `backend/app/automation/routes.py` | `_job_public` uses `retry_step` (`resumable`, `retry_from`), `retry_job` 409 mapping, `approve_job` keeps the runner's code |
| `backend/app/automation/store.py` | `CLAIM_LEASE_SECONDS` shared constant |
| `frontend/src/api.ts` | `AutomationJob.retry_from` |
| `frontend/src/pages/AutomationPage.tsx` | per-job pending guard, real feedback, evidence-based success, drawer detail refresh, button states |
| `backend/tests/test_retry_approve_buttons.py` | 22 new regression tests |
| `frontend/dist/*` | rebuilt (the release builder ships this directory) |

Removed: `backend/tests/test_repro_retry_approve.py` (the temporary probe file).

---

## Tests performed

**New suite** — `tests/test_retry_approve_buttons.py` (22 tests, all passing):

retry refused while running / after completion / on a review hold (job row
untouched, no re-render, no Classroom call); retry resumes at the blocked step
without re-rendering or re-uploading the report; retry clears a stale claim and
takes effect at once; retry never pulls a job out from under a fresh claim; a
second retry is refused instead of duplicating; approve after a failed report
stage really sends the report, publishes the grade and returns; approve of a
crash-left draft publishes the final grade; approve without a result is refused;
approval persists across the boot path; HTTP-level 409/200 contracts, the
`resumable`/`retry_from`/`approvable` payload the page renders, the two-click
case, a full approve over HTTP with publication evidence, and the state table
shared by both sides.

**Whole backend suite**: `python -m pytest tests/ -q` → **1409 passed** (22 new,
1387 pre-existing, none modified or weakened).

**Frontend**: `npx tsc -b` clean; `npx vite build` clean.

**Real running server, real data (on a COPY of `data/`, scheduler disabled so
nothing could reach Google)** — `/api/automation/jobs` over the live rows:

* review hold (`ATTENTION_REQUIRED`, grade 8, `review_mode_await_teacher`) →
  `resumable=false`, `approvable=true`, `retry_from=null` → the page offers
  **Approve only**;
* `REPORT_FAILED` job → `resumable=true`, `retry_from="Report generated"`;
* `POST /retry` on the hold → **409 `not_retryable`**, message names Approve, and
  all eight columns of the row are byte-identical before/after;
* `POST /retry` on a `COMPLETE` job → **409** "already complete";
* `POST /retry` on the real `CORRECTION_FAILED` job → **200**, status
  `CORRECTING`, `attempts` 1 → 0, due immediately, activity log shows
  "Retry requested from Correcting" / "Retrying from Correcting";
* the served bundle (`GET /assets/index-DA46Hemq.js`) contains the new messages
  and no longer contains the old optimistic "Retrying from the failed step".

Collateral check: the live database was never opened by the verification run
(still 39 jobs, 152 results); the temporary copy was deleted.

---

## Still untested / remaining notes

* **No real Google Classroom call was executed.** Every Classroom/Drive
  interaction is faked in the suite and the live-data run deliberately kept the
  scheduler off, so "approve really publishes to Google" is verified only as far
  as `classroom.managed.publish` / `report_attachment.attach_report` being called
  with the right arguments and the outcome being re-verified from the API
  responses. A real end-to-end approval against a test Classroom account is
  still outstanding.
* **No frontend unit tests exist in this repo** (no test runner in
  `frontend/package.json`), so the double-click guard and the toast wording are
  covered by the type check, the production build and code inspection only.
* **Bounded race**: a retry issued in the same instant the scheduler retries the
  same job automatically can have both threads attempt the same step; the
  operations involved are idempotent (same grade value, same report file), and
  the same exposure already existed via `recover_in_flight()` at boot.
* `frontend/src/pages/CorrectionsPage.tsx::retry` (a *different* endpoint,
  `/api/jobs/{id}/retry`) still shows an optimistic "Job re-queued" toast
  without checking the response. Left alone as out of scope — say the word and
  it can get the same treatment.
* The shipped distribution copies under `MathChecker_Distribution/` are build
  output; re-run `build_release.bat` to pick up this fix (the allowlist already
  covers `backend/app/**.py` and `frontend/dist`).
* Old `data/app.db.bak-*` backups from earlier cleanup runs are still in the data
  directory; they are not referenced by anything.
