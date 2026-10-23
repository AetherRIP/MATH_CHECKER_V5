# Large PDF Corrector — historical audit, root causes and repairs

> **REMOVED — HISTORICAL RECORD.** The dedicated Large PDF correction pipeline
> (`backend/app/large_pdf/*`, its routes, scheduler and UI) has been removed
> from the application. This document is kept only as the audit record of the
> defects found and the **shared** safeguards that were retained for normal
> correction (`engine/blueprint.py`, `engine/verify.py`, the result guard and
> the partial-correction/review handling). Nothing here describes an active
> feature, and the scripts it names may no longer exist.

Date: 2026-10-21 · Scope at the time: `backend/app/large_pdf/*`, the shared
reconciliation (`engine/blueprint.py`, `engine/verify.py`), the result surfaces
that display a correction, the correction PDF, and the regression suite.

## 1. Evidence used

All numbers below come from the installation's own database
(`data/app.db`, `data/logs/app.log`) and from a deterministic benchmark
(`scripts/bench_large_pdf.py`). No AI quota was spent re-running real papers.

### Job A — the "22/22" report

| | |
|---|---|
| Job | `f030d0fa9bae4e018b0bca10ddaeec6a` (student 1) |
| Paper | 50 pages, 2 batches |
| Timeline | enqueued 20:39:14, plan 20:40:18, batch 1 stub ×3 until 20:46:18, recovery 20:50:37→20:59:01, finalized 20:59:02 — **19.8 min** |
| Stored result | `earned=22`, `available=22`, `percentage=100.0`, `review_required=1` |
| Verification | `score_basis=MARKED_ONLY`, `paper_total_marks=255`, `unresolved_count=60`, `unresolved_marks=233` |
| Question rows | 67 rows: **7 VALID, 25 REVIEW, 35 NEEDS_REVIEW** |

### Job B — 23 minutes, no usable reconciliation

`2cd15f52…` (student 2), 49 pages: batch 1 stubbed at 21:08 and was re-marked
until 21:14; finalized 21:23 (23.1 min). The stored result held **164 rows**,
`earned=4 / available=6 (66.7%)` with 162 rows unresolved. Its blueprint was not
available at finalize, so no question count could be reconciled.

### The mark scheme is complete, not the cause

The cached blueprint for Job A's scheme (sha `1428ebc6…`) is:

* 67 questions, **255 marks**, 178 declared subquestions
* completeness verdict `COMPLETE: all 67 item(s) accounted for, printed index 1-67 with no gaps`

So the paper's real maximum is 255, and every expected question was known to the
system before marking began. The failure was downstream of extraction.

## 2. Root cause of "22/22"

Two independent defects stacked:

1. **Reconciliation rejected whole-question marks.**
   `apply_blueprint` grouped a returned row `"1"` onto scheme question 1, then
   saw that the scheme declares `1(a)/1(b)(i)/1(b)(ii)`. Because the row named
   no sub-part it was recorded as "unattributed" and the question was forced to
   `REVIEW` — even though the row carried the question's own maximum
   (`1: 5/5`, `2: 2/2`, …). On this paper that happened to **60 of 67 questions**.

2. **The score was then computed over what survived.**
   `score_breakdown` excludes unresolved rows from both numerator and
   denominator ("the marker's failure must not be charged to the student"). With
   60 rows unresolved, the only rows left were 7 questions worth 22 marks, all
   fully awarded → **22 / 22 = 100%**. The review queue and the results list
   printed that fraction directly, so a catastrophic run looked like full marks.

The job was correctly held in review (`review_required=1`), but nothing in the
display, the API or the PDF said "incomplete", so the stored fraction read as a
score. The earlier fix that introduced the marked-only denominator solved a real
problem (46.6% for a 89.9% script) and created this one: it had no separate
"partial" state.

## 3. Root causes of the slow runs

Measured with `scripts/bench_large_pdf.py` on a synthetic 50-page paper + 30-page
scheme (text layer):

| Stage | Time |
|---|---|
| scheme text evidence | 0.12 s |
| student PDF inspection (50 pages) | 0.007 s |
| full `planner.prepare` | 0.006 s |
| split into batches | 0.011 s |

Local PDF/report work is **not** the bottleneck (≤0.3 s). The wall clock is
inside the provider:

1. **One 25-page batch request takes 6–9 minutes.** Job A: batch 0 finished after
   6.8 min; Job B: batch 1 first response after **8 minutes**. Two batches run
   concurrently (`concurrency=2`), so per-job latency ≈ the slowest single call.
2. **Identical stub re-marks.** Job A's batch 1 returned the same 6-mark stub on
   all three attempts (20:44:53 / 20:45:10 / 20:46:18). The third request could
   not change the answer and still cost a full extra round-trip.
3. **Sequential targeted recovery.** Job A ran 7 chunk requests between 20:50:37
   and 20:59:01 (**8.4 min**) and recovered 5 of 42 missing questions.

## 4. Repairs

### 4.1 A partial correction is a first-class state (correctness)

* `large_pdf/validate.py`
  * `score_breakdown` now reports `partial`, `marked_percentage` (of the marked
    subset) and `paper_percentage` (of the paper), and clamps every row before
    summing (non-numeric → 0, negative → 0, above-max → max).
  * new `coverage_inventory()` counts every expected question exactly once:
    `marked`, `unanswered` (confirmed blank), `illegible`, `requires_review`,
    `unaccounted`, and a single `verified` verdict.
* `large_pdf/runner.py`
  * a partial stores `percentage = earned / paper_marks`, **no provisional
    grade**, `verification.partial = true`, coverage counts, and is always
    persisted with `review_required = 1` (`_persist_result` enforces this even
    if a caller passes `review=0`);
  * `needs_review` also fires when coverage is not verified, so "completed" can
    never be reached with an unaccounted question.
* `engine/blueprint.py`
  * a single row for a question that claims the scheme's **own maximum** is
    accepted as a question-level mark (`subquestion_attribution =
    "question_level"`, reported in `question_level_marks`). A fragment claiming
    a smaller maximum, a partial sub-part set, a duplicate sub-part or a foreign
    label still go to review — the relaxation is one narrow, provable case.
* `engine/verify.py`
  * `partial_summary()` gives every list surface the same compact descriptor,
    and derives it for rows written before the flag existed.

### 4.2 The gap is visible everywhere (no more 22/22 on screen)

* `GET /api/review` and `GET /api/results` attach `partial {marked, expected,
  unresolved, paper_marks, …}`; the raw verification blob is not leaked.
* Review queue: an incomplete row shows `Incomplete · 7 of 67 questions marked ·
  60 unmarked · 233 of 255 marks`, and the **Accept** button is gone — a partial
  can only be settled by entering a mark for the whole paper (backend refuses
  with `409 partial_correction`; the mark is validated against the paper total
  and the result stops being partial afterwards).
* Results list and detail: `Incomplete` badge with coverage instead of a score.
* Correction PDF: headline reads `22 / 22 MARKED ONLY`, percentage `—`, plus an
  "Incomplete correction … DRAFT for review" callout. No 100% is printed.

### 4.3a The incomplete state reaches the aggregate surfaces too

The classroom tracking projection, the report rows, the report statistics, the
class report page and the printed reports all read the same result record, so
they all used to read a partial's stored fraction as a mark:

* `classroom/submissions.py` loads each result's `verification_json` and attaches
  `result.partial` (the compact coverage descriptor); the raw blob is not
  projected.
* `api/report_routes.py` marks such a row `mc_status=INCOMPLETE`, withholds
  `mc_available`/`mc_percentage`, exposes `mc_partial`, and excludes it from
  every average and total. `stats.math_checker` gains an `incomplete` count and
  both printed reports gain a "Needs attention" line explaining that those
  marks are excluded.
* `reporting/class_report.py` renders the cell as
  `Incomplete (7 of 67 questions marked)` with no percentage, no grade label and
  no contribution to class averages.
* `frontend/src/api.ts` types the new status and flag; the Classroom tracking
  table and the submission rows show `incomplete` instead of `7/7 (100.0%)`.

### 4.3b Malformed model output is reported, never fatal

`validate_batch` documents "never raises", but it (and the merge, the retry
fingerprint, and the shared reconciliation) called `int(...)` on raw model
fields. A provider returning `"awarded_marks": "n/a"`, `2.5`, `null`, a list,
or a bare string row would have thrown out of the job — or worse, been coerced
into a mark. Now:

* `large_pdf/validate.py` has `as_mark()` (whole numbers only; `"7"` and `7.0`
  accepted, everything else `None`) and `_rows_of()` (object rows only).
  `validate_batch` reports a non-object response, a non-list `questions`, a row
  with no number, and an unreadable mark as `problems`, and an unreadable mark
  is **never** classified as a confirmed blank.
* `merge_batches` holds such a row at `NEEDS_REVIEW` with an explicit reason
  instead of crediting 0 (which would have been a free zero) or full marks.
* the identical-stub fingerprint in `runner.py` reads marks through `as_mark`,
  so the retry decision itself cannot crash.
* `engine/blueprint.py` (`_award_of`) and `engine/question_inventory.py`
  (confirmed-zero check) tolerate an unreadable mark on the shared path, so a
  malformed batch cannot take down reconciliation or the Normal corrector.

### 4.3 Performance work that does not trade accuracy

* Identical incomplete responses are not re-requested: the batch fingerprint
  (question ids + marks) is compared between attempts. On Job A's shape this
  removes the third, pointless 25-page request (~1 min of a 19.8-min job) while
  a *different* stub is still retried.
* Per-attempt, per-chunk and inspection timings are now logged
  (`elapsed_ms` on `batch attempt finished`, `recovery chunk applied`,
  `inspection finished`), so the next slow run can be attributed from its log
  instead of guessed at.
* Recovery was deliberately **not** shortened. An early-stop rule was prototyped
  and reverted: on Job A chunks 1–2 recovered nothing but chunks 3 and 6
  recovered four real questions, so stopping early would have discarded marks.

## 5. Tests

New `backend/tests/test_large_pdf_integrity.py` (27 tests). Mandatory regression:

* 67-question paper, 22 questions marked → `AWAITING_REVIEW`, `partial=true`,
  `percentage != 100`, no grade, `coverage.marked=22`,
  `coverage.requires_review=45`, `unresolved_questions` names 23–67.
* a genuinely complete paper still completes at 100% with band 9;
* a legitimate 22/22 two-question paper is accepted.

Also covered: a malformed model response (bare strings, a row with no number,
`"n/a"`, a fractional mark, a non-object response) is reported and held for
review — never a crash, never a silent 0, never a completed correction;
marks above the maximum are clamped; negative/non-numeric awards
never enter the total; the scheme's maxima override the model's; blank answers
become `CONFIRMED_ZERO` (not review); whole-question rows accepted, fragments and
partial sub-part sets held; duplicate ids counted once; undeclared questions
flagged not counted; a permanently failing batch cannot produce a result; a
timeout retries only the failed batch; repeated submissions reuse the job; a
partial cannot be approved without a whole-paper mark; the PDF prints no
percentage for a partial; an empty recovery chunk does not stop later recovery.

Updated: `test_large_pdf_incomplete_batch.py` (partial scoring contract,
identical-stub retry count) and `test_subquestion_scheme.py` (question-level
acceptance, plus the fragment case that must still review).

Results (re-verified 2026-10-21 after the final edit):

* `test_large_pdf.py` (47) + `test_large_pdf_integrity.py` (27) +
  `test_large_pdf_incomplete_batch.py` (16) + `test_subquestion_scheme.py` (82):
  **172 passed**
* `test_reports_and_logs.py`: **18 passed**, including four new ones that hold
  the report surfaces to the same contract (an incomplete row is not a score,
  does not move an average, is flagged in the tracking projection, and the
  class PDF says so).
* all five of the above together: **190 passed**.
* full backend suite: **2156 passed, 1 skipped** (8m38s, one run)
* `tsc -b` and `npm run build` (`tsc -b && vite build`): clean
* Normal PDF Corrector: `test_api_mock.py::test_manual_correction_end_to_end`
  passes; the standard prompt and `calculate_totals` are unchanged.

## 6. Before / after on the recorded failure

| | Before | After |
|---|---|---|
| Displayed result | `22/22 · 100%` | `Incomplete · 7 of 67 questions marked · 233 of 255 marks unmarked` |
| Stored percentage | 100.0 | 8.6 (share of paper) with `marked_percentage=100` |
| Provisional grade | "9" band from 100% | `None` |
| Approvable as-is | yes (Accept) | no — requires a whole-paper mark |
| PDF | `22/22`, `100%`, "Excellent" | `22/22 MARKED ONLY`, `—`, "Incomplete… DRAFT" |
| Missing questions surfaced | no | yes, named (23–67) |
| Class/assignment reports | 22/22 folded into the class average | `Incomplete`, excluded from every average |
| Retry on identical stub | 3rd request | stopped after 2 |

## 7. Remaining limitations

* No live AI re-run was performed (quota). Phase timings are from the stored
  runs and the scripted engine; the local-stage figure was measured directly.
* Job B's historical coverage reads `expected=164` because its blueprint was not
  available at finalize; for a blueprint-less run the inventory can only count
  the rows it has. New runs always reconcile against the cached scheme.
* Stored legacy results keep their old `percentage` column value; the API, UI and
  PDF now treat them as partial, but the raw number is still 100 in the DB until
  the row is re-run or overridden.
* Recovery remains sequential. It is 8.4 of 19.8 minutes on this shape and is the
  largest remaining lever; running chunks concurrently needs per-chunk clients
  and provider-limit handling, and was not attempted here.
* Aggregate surfaces now exclude partial rows (see §4.4), but a class average
  built only from partially corrected papers reads `—` rather than "no data";
  the count of incompletes is what tells the teacher why.
