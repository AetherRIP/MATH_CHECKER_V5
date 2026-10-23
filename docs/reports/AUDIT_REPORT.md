# Math Correct — Live-Accuracy Safety Fixes + Real-Student Audit

Date: 2026-10-08 · Engine: V5.1 preserved legacy marking core (untouched)

---

## PART 1 — Root-cause findings

### Finding A — The "recheck" was not a genuinely independent correction (root cause of identical recheck results)

The existing high-score double-check re-ran the SAME engine, SAME model, SAME
prompt, SAME inputs at temperature 0, changing only the `seed`. At temperature 0
Gemini sampling is effectively deterministic, so the seed change produced a
near-bit-identical replay — the second correction confirmed the first by
construction, not by evidence. This is why the recheck "frequently returned the
same incorrect result".

Additional structural gaps found:

1. **Historical ≥95% results never got any recheck.** The double-check feature
   landed after the Oct-6/7 class batches ran; every 95–100% result before that
   (`Student H 99.04%`, `Student I 95.21%`, `Student J 97.13%`, five
   100% results in the Ch1L10/Ch1L9 batches) has
   `double_check_triggered = 0` in the database.
2. **Blank / unreadable / wrong-assignment submissions were trusted as scores.**
   `Student K 0/150` was stored with confidence HIGH — a near-blank or
   wrong-assignment scan marked "honestly" produces a 0 that looks like a
   verified failure, and `Student A 266/266` was accepted with HIGH
   confidence purely because the numbers were self-consistent.
3. **No submission-quality gate existed** (spec §9): nothing deterministically
   flagged a submission whose answers mostly don't match the mark scheme.

### What already existed and was kept (not rebuilt)

Deterministic totals, mark-scheme blueprint (question structure + max marks
extracted once, cached by SHA-256, reconciled into every run), result guard,
append-only version history, review queue + manual override, verified report
attachment, honest Classroom publish with re-read verification, deterministic
1–9 grades. All of these were verified working and left intact.

---

## PART 2 — Changes made (smallest safe set)

| # | Change | Where |
|---|--------|-------|
| 1 | **TEST/REVIEW MODE** — `automation.review_mode` config flag, **default ON**. While enabled, automation still corrects → saves → generates + validates + attaches reports, but the grade step saves the proposed grade as a **verified DRAFT** and the job stops at `ATTENTION_REQUIRED` (`review_mode_await_teacher`). No final grade is published, no work is returned, without explicit teacher action. The existing `requires_review` engine gate runs BEFORE the review-mode draft, so an engine-flagged correction never even receives a draft grade. | `app/config.py`, `app/automation/runner.py`, `app/automation/routes.py` |
| 2 | **Independent second correction made genuinely independent** — the second pass now runs with a distinct sampling path AND leads with a **different model** from the chain (first-pass model kept only as last resort; single-model installs keep a seed-only differentiation). A temperature-0 replay by the same model can no longer impersonate verification. | `app/engine/double_check.py` (`second_pass_models`), `app/workers/child.py`, `app/engine/service.py` |
| 3 | **Submission-quality validator** (`SUBMISSION_REVIEW_REQUIRED`) — deterministic checks: nothing readable, near-blank paper, majority of answers not in the mark scheme (wrong assignment), majority of questions returned unmarked. A flagged submission can never be a trusted score; only a teacher override clears it. | new `app/engine/submission_validation.py`, wired into **both** save paths (`workers/pool.py`, `engine/service.py`) and `result_guard.py`/`remark.py` |
| 4 | **Review-flag propagation** — automation surfaces `SUBMISSION_REVIEW_REQUIRED` with the specific reason and blocks grade submission (existing `requires_review` gate) | `app/automation/runner.py` |
| 5 | **Tests** — 10 new tests locking review-mode behaviour (defaults ON, draft-and-stop, publish/return blocked) and the submission-quality matrix (blank, unreadable, wrong assignment, unmarked gaps, untrusted-confidence rule) | `backend/tests/test_safety_gates.py` |

Full test suite: **973 passed, 0 failed** (all pre-existing behaviour preserved;
`test_automation.py` now explicitly runs with review mode off since it locks the
fully-automatic workflow, which remains available via the config flag).

---

## PART 3 — Real end-to-end test (real fixture submission, real Gemini)

`scripts/e2e_live_accuracy.py` ran the preserved engine on
`data/fixtures/real` (Student Answer + official mark scheme + question paper):

- Mark-scheme blueprint: 31 questions / 103 marks, HIGH confidence (cached)
- First correction: **78/103 = 75.73%** (model gemini-3.5-flash, 296s), status VALID
- Submission quality: VALID
- Double-check: not triggered (75.73% < 95%)
- Deterministic grade: **7** from the configured boundary table (AI never picks grades)
- Confidence: MEDIUM (1 structural warning, marks unchanged)
- Report: generated + PDF-validated (3 pages) — **not uploaded, no Classroom grade written**
- Original submission: SHA-256 identical before/after (**preserved**)

---

## PART 4 — REAL-STUDENT AUDIT (5 submissions, Student V excluded)

Method: originals re-downloaded read-only from Classroom Drive, official
mark-scheme PDFs used, mark-scheme blueprint validated, **fresh independent
correction** (distinct seed, no previous score/reasoning shown to the model),
question-by-question comparison against the automatic result. The audited
results were then applied deterministically (engine totals + boundary-table
grade), stored with append-only history, and changed results flagged for teacher
review. Original scans verified byte-stable.

### STUDENT 1 — Student H (Ch1L9 – Ratio, 209 marks)
- Automatic: **207/209** (99.04%, grade 9)
- Audited: **207/209** (99.04%, grade 9) — **CONFIRMED**
- Question differences: none
- Report replaced: N/A (no report was attached; not needed)
- Classroom: no grade posted (nothing to correct)

### STUDENT 2 — Student I (Ch1L9 – Ratio, 209 marks, 3 files)
- Automatic: **199/209** (95.22%, grade 9)
- Audited: **199/209** (95.22%, grade 9) — total CONFIRMED
- Question differences: **Q34: 2 → 3/4, Q45: 5 → 4/5** (offsetting; same total,
  different distribution — recorded in the audit trail for the teacher)
- Report replaced: N/A · Classroom: no grade posted

### STUDENT 3 — Student J (Ch1L9 – Ratio, 209 marks)
- Automatic: **203/209** (97.13%, grade 9)
- Audited: **207/209** (99.04%, grade 9) — **UNDER-AWARD FOUND: Q54 0 → 4/4**
  (the automatic checker awarded 0 where the audited correction found full
  marks earned)
- Applied: stored result updated to 207/209, review flag set, new report
  generated + validated (11,157 bytes, 1 report, no duplicates)
- Classroom: no grade posted (nothing to correct); original scan untouched

### STUDENT 4 — Student A (Ch1L13 – Sets, 266 marks) — was 100%
- Automatic: **266/266** (100%, grade 9)
- Audited: **266/266** (100%, grade 9) — **CONFIRMED GENUINE 100%**
- Every audited question mark matched; the Classroom grade 9 already returned
  to the student is correct as-is
- Report replaced: NO (existing attached report is correct) · Classroom grade
  updated: NO (already correct)

### STUDENT 5 — Student L (Ch1L10 – Map Scale, 46 marks) — was 100%
- Automatic: **46/46** (100%, grade 9)
- Audited: **39/46** (84.78%, grade **8**) — **FALSE 100% FOUND**
- Question differences: **Q5: 6 → 3/6, Q7: 4 → 2/4, Q9: 2 → 0/2** (−7 marks,
  each with mark-scheme-based loss reasons recorded in the audit file)
- Applied: stored result updated to 39/46, grade recalculated 9 → 8 by the
  deterministic boundary system, review flag set (teacher must confirm before
  any grade is published), new report generated + validated (7,307 bytes)
- Classroom: no grade had been posted, so no incorrect grade reached the
  student; original scan untouched

### AUDIT TOTALS
- Automatic 100% results audited: **2/5** (Student A, Student L)
- 100% results found incorrect: **1** (Student L)
- Total marks corrected: **−7 (Student L), +4 (Student J) = 11 marks**
- Grades changed: **1** (Student L 9 → 8)
- Reports replaced: **2** (new validated PDFs; no Classroom attachment existed to replace)
- Classroom results updated: **0 needed** (4 of 5 had no posted grade; the one
  posted grade — Student A — was audited correct)

### Is the suspected problem real?
**Yes — partially.** Findings:

1. The automatic checker is **not systematically over-awarding**: of five
   high-score submissions, two were exactly confirmed, one was *under*-awarded.
2. But **false 100% results do occur** (1 of 2 audited 100%s was 7 marks
   inflated, grade 9→8). The prior "recheck" could never catch this because it
   was a deterministic replay of the first pass (Finding A).
3. Errors concentrate in **individual questions, not question types**: Q5/Q7/Q9
   (map-scale construction working) and Q54 (ratio) — the marker accepted
   stated answers without verifying the student's working against the
   mark-scheme marking points. The blueprint already pins question structure
   and max marks, so mark-scheme mapping is NOT the main problem.
4. The failure is in the **marking judgement + missing verification layers**,
   now addressed by: genuinely independent second correction (different
   sampling path/model), submission-quality gating, and the review-mode hold
   so no unverified score reaches a student before a teacher looks at it.

---

## PART 5 — How to operate now

- **Review mode is ON** (default). Automation completes corrections and reports
  but holds grades as teacher-approved-only drafts.
- Review queue: `GET /api/review` shows every flagged result with both scores
  and the dispute reasons; `POST /api/results/{id}/override` records the
  teacher's final decision (AI result + teacher result + note are both stored).
- When accuracy is proven, set `[automation] review_mode = false` in
  `data/config.toml` (or Settings → Automation) to restore the fully automatic
  workflow — the existing tests lock that workflow's correctness.
- Audit artifacts: `data/audit_results/` (raw audited corrections),
  `data/audit_reports/` (validated replacement PDFs),
  `data/audit_applied.json` (per-student application + Classroom verification).

---

## PART 7 — Assignment-wide repair: C1L13 - Sets, O Level Math (2026-10-11)

TESTING ONLY · REPAIRED RESULTS NOT PUBLISHED. Repaired results live only in
dedicated test records (`repair_runs` / `repair_items` + `data/repairs/`); the
original corrections in `results` are byte-for-byte untouched.

### Assignment identified

* Classroom: **WBIS - O Level Mathematics - Mr. Teacher (redacted)** (course
  `877926720424`) — the "O Level Math" class. (A same-named `Ch1L13 - Sets`
  exists in the separate `TESTING` course `886515702341`; it was NOT touched.)
* Assignment: **Ch1L13 - Sets**, coursework `889601795010`.
* Authoritative mark scheme: `data/mark_schemes/889601795010.pdf`
  (SHA-256 `1428ebc6…`), cached teacher-verified blueprint = **69 questions /
  266 marks**.

### Inventory (every submission accounted for)

78 enrolled · 15 submitted/returned · 10 already corrected · 5 submitted but
never corrected · 63 never submitted · 13 accessible · **2 explicitly
inaccessible** (Student T — link-only attachment; Student U — `.zip` archive,
invalid submission type).

### Root causes found in the stored corrections

1. **Unmarked questions divided into the total (Student P).** Batch 2 of 2 of
   her 49-page submission returned 8 rows / 9 marks; the 32 questions (Q38–Q69,
   128 marks) it never answered were charged to her as 0: stored **124/266 =
   46.62%, grade 4**. Real: **238/266 = 89.47%**.
2. **Incorrect per-question maxima / denominator (Student Q).** Q15/28/34
   were worth 2/2/4 in the cached scheme extraction of his run instead of the
   official 3/3/6, giving a 262-mark denominator and a stored **262/262 =
   100%**. (The scheme structure was later teacher-corrected, but his stored
   result was never re-accounted.)
3. **Over-awarding on perfect scores.** All three stored 266/266 results
   (Student A, Student B, Student C) failed independent
   re-marking: −4, −14 and −90 marks respectively.
4. **A silent zero.** Student E's Q1 was stored as a 0 with no recorded
   evidence the answer was read.
5. **Recovery 403 — the systemic repair blocker.** Every chunked-recovery
   request for an affected submission died with `403 PERMISSION_DENIED — You do
   not have permission to access the File <id>`: Gemini Files are scoped to the
   API key that uploaded them, and `_upload_for_recovery()` built its OWN
   client, so the client running the requests never knew the file's owner and
   could not re-upload on a key rotation. Fixed (see code changes); a new test
   locks it.

### Code changed

* `app/repair/` (new): `audit.py` (deterministic defect detection),
  `pipeline.py` (inventory, per-question diff, validation gates),
  `store.py` (dedicated test records + artifacts), `routes.py` (read-only API).
* `app/schema.sql`: additive `repair_runs` / `repair_items` tables.
* `scripts/repair_assignment.py` + `scripts/repair_mark_one.py`: the repair
  session driver (download → isolated re-mark → diff → test record) and its
  isolated marker.
* `app/large_pdf/runner.py`: fix 5 above (recovery uploads bind to the marking
  client).
* `frontend/src/pages/AuditPage.tsx`: assignment-wide repair dashboard with
  per-question original-vs-repaired evidence and report previews.
* `app/main.py`: register the repair routes.

### Measured results (first repair session, 2026-10-11)

| Student | Status | Original | Repaired | Detail |
|---|---|---|---|---|
| Student P | NEEDS_REVIEW | 124/266 (46.62%) | **238/266 (89.47%)** | +32 added, 11 changed, 0 unresolved |
| Student Q | UNRESOLVED | 262/262 (wrong denominator) | 119/161 marked-only | 27 unresolved (recovery 403) |
| Student A | NEEDS_REVIEW | 266/266 | 262/266 | 3 changed |
| Student R | NEEDS_REVIEW | 259/266 | 243/266 | 13 changed |
| Student B | NEEDS_REVIEW | 266/266 | 252/266 | 10 changed |
| Student D | NEEDS_REVIEW | 232/266 | 232/266 | 29 changed (same total) |
| Student E | UNRESOLVED | 261/266 | 239/246 marked-only | 6 unresolved (recovery 403) |
| Student C | UNRESOLVED | 266/266 | 176/252 marked-only | 37 changed, 4 unresolved |
| Student O | UNRESOLVED | 265/266 | 132/134 marked-only | 34 unresolved (recovery 403) |
| Student M | UNRESOLVED | 263/266 | 17/17 marked-only | 64 unresolved (recovery 403) |
| Student N | REPAIRED (new) | — | 220/266 | 69 added, 0 unresolved |
| Student S | REPAIRED (new) | — | 236/266 | 49-file scan merged, 69 added |
| Student F | UNRESOLVED (new) | — | 234/263 | 68 added, 1 unresolved |
| Student T | BLOCKED | — | — | link-only attachment |
| Student U | BLOCKED | — | — | `.zip` archive |

Nothing was fabricated: unmarked questions stayed UNRESOLVED with explicit
reasons, and every disagreement preserves BOTH versions for the teacher.

### Safety verification

`results`, `report_attachments`, `classroom_publish_ops`,
`classroom_state_events`, `correction_jobs`, `result_versions` and the live
`large_pdf_jobs` counts are all identical before/after the session; the 7 assigned grades for
C1L13 are unchanged; `[testing] only = true` remains active and the backend
guard refuses every mutating Classroom/Drive call.

---

## PART 6 — Large PDF Corrector: unmarked-gap recovery (2026-10-10)

### Root cause of the "~50% deducted" result

Job `279ca8ce…` (*Ch1L13 - Sets*, 49 pages, 2 batches): batch 2 of 2 (pages
25–49) returned 8 sub-rows / 9 marks against ~136 expected — it was stored as
a COMPLETE batch, and the 32 mark-scheme questions (Q38–Q69, 128 marks) it
never answered were divided into the total: **124/266 = 46.62%** stored,
while the questions actually marked scored **124/138 = 89.86%**. The failure
was in batch response completeness + merge accounting, not in the student's
work or the mark scheme (which is sent whole and unsplit to every batch).

### Fixes landed in this round

1. **Incomplete-batch completeness gate** — a batch whose share of questions
   is far below its share of pages is flagged and re-marked; unmarked is never
   scored as 0. Stored scores are marked-only, with the unresolved remainder
   named (`score_basis: MARKED_ONLY`, `unresolved_marks`), and any unresolved
   marks keep the job in teacher review.
2. **Chunked targeted recovery (D8)** — the single-round retry used to refuse
   any gap above 12 questions, so this 32-question gap was unrecoverable.
   `plan_chunked_recovery()` now re-marks a gap of any size in bounded chunks:
   at most `large_pdf.recovery_chunk_questions` (6) questions per request and
   at most `large_pdf.recovery_max_chunks` (8) requests per job — 6 chunks
   `[6,6,6,6,6,2]` for this submission. Progress is persisted after every
   chunk (a restart never re-bills a request); questions beyond the budget are
   explicitly deferred; every question that survives the retries keeps an
   explicit `review_reason` + `retry_outcome` (which chunks ran, what they
   recovered, why it is still unresolved). A gap covering ~the whole paper is
   still refused, with the reason recorded on the job. Old gate: REFUSED (32 >
   12). New planner: 6 chunks, 0 deferred — recorded in
   `data/audit_runs/large_pdf_replay_279ca8ce4b90441ca6889a42dc62ede4.json`.
3. **Replay of all four real long-document runs** (offline, stored batch
   results, no API quota): 46.62%→89.86% and 78.2%→89.66% corrected; the two
   healthy runs unchanged at 97.37% / 87.22% — no false positives.

### Tests

- `tests/test_large_pdf_chunked_recovery.py` — 12 tests: 32-question gap in
  6 bounded chunks, config-driven chunk size/budget, partial recovery keeps
  explicit reasons, per-chunk error handling, crash-safe persistence (no
  chunk re-runs on repeated finalize), whole-paper-gap refusal, budget
  exhaustion, no-narrow-blueprint refusal.
- Full backend suite: **1261 passed** (exit 0); frontend `tsc -b && vite build`
  clean. `[testing] only = true` remains active: every mutating
  Classroom/Drive call raises `PublicationBlocked` before leaving the machine
  (17 guard tests), and no live records were modified in this round.

### Limitations

- Student P's live result was **not** re-marked by this change; her stored result
  is unchanged and still held in review. Running the 6 recovery chunks against
  her live job would spend real API quota and is left for the teacher to
  trigger. How many of the 32 questions she would actually earn is unverified.
- Chunked recovery needs the cached mark-scheme structure to build the narrow
  prompt; without it the questions stay UNMARKED with an explicit reason.
