# Teacher-certified marking accuracy — implementation and evidence report

Date: 2026-10-29
Dataset: `docs/reference-marking/manifest.json` (version 1, SHA-256 `f0a32cc9…`,
recomputed after a documentation correction)
Application: Math Checker 5.2.1, build 20261013-rc1, schema 5

## 1. Bottom line

The benchmark infrastructure is implemented end to end: dataset validation,
append-only teacher verification, a reproducible runner, explicit metrics,
disagreement analysis, a dashboard, and 38 regression tests.

**Certified accuracy is unavailable.** At the end of this work, **0 of 16
reference items** carry an explicit teacher confirmation. No live inference was
performed, no provider quota was spent, and no teacher-certified score was
manufactured. The correct statement is:

> MathChecker V5 has not yet been measured against teacher-confirmed marks.
> The certified figure is unavailable, not 0%.

A deterministic stub self-check (which awards the reference mark by
construction) produced 16/16. That number is **plumbing evidence only**; it is
stored under `data/scratch/benchmark-infra-runs/`, outside the benchmark run
directory, and is not an accuracy measurement.

## 2. Existing evaluation infrastructure discovered

| Artefact | What it is | Why it was not a certified benchmark |
|---|---|---|
| `docs/reference-marking/manifest.json` | Two synthetic papers, 16 marking items, 36 available marks, scheme clauses and scheme-derived expectations | The dataset-level `review.verified_by` is null; there is no per-item teacher record, no holdout partition, and no provenance class on items |
| `docs/reference-marking/REVIEW_SHEET.md` | Printable human review sheet | A checklist, not a verifiable record; no assessor identity, timestamp, revision history, or machine-readable status |
| `backend/scripts/verify_marking_reference.py` | Drives the real live harness per paper and aggregates paper-level agreement | Monolithic script; no per-item eligibility, no teacher overlay, no run artifact with dataset/verification hashes, no coverage/safety metric set |
| `MathChecker_Distribution/scripts/verify_live_marking.py` | Real app run in an isolated data directory; stores question and subquestion marks, model, timings, verdict | Produces one paper's outcome, not a certified dataset or a comparable benchmark run |
| `docs/logs/paper_b_live_outcome.json`, `paper_b_fixed_outcome.json` | Archived provider outputs | Previous AI output is evidence of behaviour, never ground truth |
| `docs/results/MARKING_REFERENCE_RESULTS.json` | Archived reference run (paper A 12/12 against scheme-derived marks) | Scheme-derived, unsigned; not teacher-confirmed |
| Review queue (`/api/review`, `result_reviews`) | Human review of **student results** | A different object; using it as benchmark ground truth would conflate a production result with a reference label |
| `AuditPage` | Read-only historical accuracy audit artefacts | Not a reproducible benchmark; no dataset versioning or certification |

The existing manifest, papers, live harness and mark-scheme alignment ideas were
reused. The new package replaces the script's ad-hoc aggregation for benchmark
purposes; the old script remains the raw paper-level live driver.

## 3. Reference dataset format and verification rules

The dataset remains the existing JSON manifest. `backend/app/benchmark/dataset.py`
validates it and derives stable reference IDs (`paper-a.q1`, `paper-b.q1a`).

Rules enforced as errors (no run may start):

- non-empty papers, unique paper IDs, unique question labels within a paper;
- unique, URL-safe `reference_id`;
- non-negative integer maximum marks and expected mark, expected ≤ maximum;
- `marks_available` and `expected_total` equal the sums of the paper's items;
- a valid ground-truth class and partition;
- an item cannot disagree with its paper's partition.

Reported as warnings, not silently resolved:

- unsigned dataset-level review;
- a scheme header whose total disagrees with its items (paper B: 8 vs 7);
- no holdout partition;
- a nested-subquestion item stored under a whole-question label (paper A Q3,
  paper B Q2), which cannot contribute to subquestion metrics.

Ground-truth classes: `teacher_verified`, `official_reference`,
`ai_generated`, `unverified`. The shipped manifest defaults to
`official_reference`. **A dataset-level signature, a previous AI result, a
passing harness, or a green dashboard does not promote an item.** Only an
explicit assessment in the verification overlay does, and `ai_generated` /
`unverified` items are never eligible even if a teacher types a number.

Partitions (`development`, `holdout`) are assigned per paper and inherited by
every item. A paper cannot be split across partitions, so one script's multiple
AI versions cannot contaminate both.

Current composition: 2 papers, **16 items** (13 question-level, 3
subquestion-level), 36 marks, all in `development`, no independent holdout.

## 4. Teacher verification workflow

UI: **Review → Accuracy benchmark** (`frontend/src/pages/AccuracyPage.tsx`).

For each reference item the teacher can:

- inspect the question, every scheme clause, the candidate's working, the
  scheme-derived mark, and the latest AI mark;
- enter or confirm the mark they would award;
- record accepted alternative methods;
- add a note (required for disputed/incomplete);
- choose `verified`, `disputed`, or `incomplete`;
- see the append-only assessment history;
- tick an explicit confirmation before the submit button enables.

The overlay is `data/benchmark/verifications.json`, separate from the production
database. Records are append-only with a sequence number, assessor, role, note,
dataset version, and timestamp. Two assessors can sign the same item:

- same mark → eligible, both assessments preserved;
- different marks → `disputed`, ineligible, **both marks preserved**; the system
  never picks the assessment closest to the AI;
- a later `incomplete`/`disputed` record blocks eligibility.

API: `GET /api/benchmark/overview`, `GET /api/benchmark/references`,
`GET /api/benchmark/references/{id}`, `POST /api/benchmark/references/{id}/verify`,
`GET /api/benchmark/runs`, `POST /api/benchmark/runs/{id}/reproduce`.
The write endpoint returns `production_db_modified: false` and
`classroom_writes: 0`; it has no code path to `results`, grades, retry state,
provider health, or Classroom.

## 5. Evaluation runner and reproducibility

`backend/scripts/benchmark_accuracy.py` and `backend/app/benchmark/runner.py`.

Modes:

- `live` — invokes the existing isolated live harness per paper; requires
  `--ack INFERENCE`; never runs without eligible references;
- `replay` — reuses archived provider outcome JSON for audit/regression;
- `stub` — deterministic plumbing check only.

Safety gates:

- no teacher-verified references and no `--allow-unverified` → exit 4, **no
  provider request**;
- `--allow-unverified` measures against scheme-derived marks and is always
  recorded as `scheme_reference_not_certified`, never certified;
- no prompt, threshold or correction change is made by the runner.

A run records:

- dataset path, version, full SHA-256, warnings, paper/item counts;
- verification-overlay snapshot hash and the full reference snapshot used;
- every raw per-paper outcome (`raw/<paper>.json`);
- model, prompt version, correction mode, redacted config and config SHA-256;
- app version, build, schema version, git commit, Python version;
- status (`complete`, `incomplete`, `not_eligible`), failures, limitations;
- computed metrics and disagreements;
- `report.md` and `run.json`.

`reproduce_run` rebuilds the dataset from the recorded reference snapshot and
recomputes metrics from the recorded raw outcomes, without the provider or the
live verification store. A later teacher assessment cannot silently change an
old report. The dashboard additionally invalidates the **current** certified
display if the verification snapshot or dataset hash has changed since the run.

## 6. Metrics and exact denominators

| Metric | Numerator | Denominator |
|---|---|---|
| Exact item agreement | matched items with AI mark = teacher mark | matched items with a valid AI mark (question + subquestion) |
| Exact question agreement | exact question-level references | matched question-level references with a valid AI mark |
| Exact subquestion agreement | exact subquestion-level references | matched subquestion-level references with a valid AI mark |
| Exact total agreement | scripts with AI total = teacher total | scripts where every reference item is eligible, matched, valid, and both totals exist |
| Mean absolute error | Σ \|AI − teacher\| | matched items with a valid AI mark |
| Normalised MAE | Σ \|AI − teacher\| / maximum | matched items with maximum > 0 |
| Overmarking rate | items with AI > teacher | matched items with a valid AI mark |
| Undermarking rate | items with AI < teacher | matched items with a valid AI mark |
| Average over/under magnitude | Σ positive/negative difference | overmarked/undermarked items only |
| Question coverage | matched question-level refs | eligible question-level refs |
| Subquestion coverage | matched subquestion-level refs | eligible subquestion-level refs |
| Missing marks | eligible items with no returned mark | reported as a count, never dropped |
| Invalid results | marks outside 0..max or not integral | reported separately from missing |
| Schema/validation failure | failed scripts | scripts attempted |
| Review-flag rate | scripts the AI flagged | scripts processed with a usable outcome |
| Human-review rate | disagreeing matched items | matched items |
| Grade agreement | exact grades | scripts with a teacher-verified grade |

Also reported: flagged-with-disagreement vs flagged-without-disagreement (a
review flag is not proof of a wrong mark), unresolved/disputed references,
excluded examples with reasons, exact-assignment counts by question type,
distinct papers/topics, total marks represented, and 95% Wilson intervals for
the agreement rates.

Two deliberate rules prevent inflated numbers:

1. a missing or unmatched question is reported and counted in coverage, never
   silently removed from a denominator;
2. a script total is compared only when **every** item on that paper is eligible
   and matched. A half-verified teacher total is never compared with a
   full-paper AI total.

## 7. Actual eligible teacher-verified examples

| Status | Items |
|---|---:|
| `verified` | **0** |
| `unverified` | 16 |
| `disputed` | 0 |
| `incomplete` | 0 |
| **Eligible for the certified score** | **0** |

Eligible question-level: 0. Eligible subquestion-level: 0. Teacher-confirmed
marks represented: 0 of 36. Dataset-level review: unsigned.

This is the measured state of the reference set, not an implementation failure:
the workflow exists, but no qualified teacher has used it yet.

## 8. Certified accuracy results

**Unavailable.** No certified score can be reported from zero verified
references. The dashboard displays “Certified accuracy unavailable — Not 0% —
not yet measured,” not a zero percentage.

No valid model comparison was performed. The runner records model, prompt and
config hashes so comparable runs can be made, but comparing a model against its
own prior output, or across different datasets/prompts, is not an accuracy
comparison. The production model was not changed.

No certified disagreement analysis exists. The disagreement report is implemented
and exercised by hand-calculated tests, but with no certified run there are no
real mismatches to interpret. Archived provider output was not used as a label.

## 9. Tests executed

- Baseline before implementation: **2,450 passed, 1 skipped**, exit 0
  (`data/scratch/baseline-benchmark-suite.log`, 688s). The previously reported
  2,449 was one test stale.
- New benchmark suite: **38 passed** (`backend/tests/test_benchmark_accuracy.py`).
- Frontend production build: `tsc -b && vite build` succeeded; the accuracy page
  chunk was emitted.
- Browser check: the page rendered at `#/accuracy`; the verification modal showed
  the prompt, scheme, working, AI mark and disabled submit until confirmation.
  No assessment was recorded during the check.
- Final full suite on the frozen code state: **2,488 passed, 1 skipped**,
  exit 0 (`data/scratch/final-benchmark-suite.log`, 713s). This is the
  baseline 2,450 plus the 38 new tests; the one skip is the pre-existing skip.

New tests cover: hand-calculated metrics; missing/duplicate questions; missing
subquestions; invalid marks; maximum constraints; total arithmetic; missing
ground truth; unverified examples; disputed two-teacher references; dataset
versioning; holdout separation; duplicate paper IDs; question matching; over/
undermarking; Wilson intervals; empty datasets; incomplete runs; failed provider
requests; reproducibility; tampered raw outcomes; global signature not promoting
items; scheme-derived runs never certified; API confirmation/mark-range/note
rules; no production DB writes.

## 10. Files changed

New:

- `backend/app/benchmark/__init__.py`
- `backend/app/benchmark/dataset.py`
- `backend/app/benchmark/store.py`
- `backend/app/benchmark/metrics.py`
- `backend/app/benchmark/runner.py`
- `backend/app/benchmark/reports.py`
- `backend/app/api/benchmark_routes.py`
- `backend/scripts/benchmark_accuracy.py`
- `backend/tests/test_benchmark_accuracy.py`
- `frontend/src/pages/AccuracyPage.tsx`
- `docs/reference-marking/README.md`
- this report

Modified:

- `backend/app/main.py` (register the benchmark router)
- `frontend/src/App.tsx` (route, nav tab, lazy import)
- `docs/reference-marking/manifest.json` (corrected the paper-a Q6 ruling typo
  “3 of 2” → “3 of 3”; no expected mark changed)

No schema migration was needed. No production database file was edited by this
work.

## 11. Production data-integrity checks

Read-only checks against `data/app.db`:

| Check | Result |
|---|---|
| Completed correction jobs | 35 |
| Results | 35 |
| Duplicate result groups per job | 0 |
| Orphan results | 0 |
| Completed jobs without a result | 0 |
| Duplicate submission jobs | 0 |
| Pre-existing results changed vs `app.db.bak-mission-20261029-215454` | **0 of 34** |
| New result since that backup | 1 (lara, 74/91, grade 8, from the preceding recovery mission) |
| `classroom_publish_ops` | 0 (backup 0) |
| `result_reviews` | 14 (backup 13; see note) |
| Benchmark verification store created in production | No |
| Benchmark runs in `data/benchmark/runs` | none |
| Provider requests made by this work | **0** |
| Google Classroom writes | **0** |

One `result_reviews` row (id 22) appeared during the session from a concurrent
interactive app session: `POST /api/results/0c6487…/override` at
`data/scratch/backend_live.log:995`, note “Accepted by teacher”. It changed the
earned mark from **0 to 0** and set the manual-override flag; it did not change a
score. The benchmark code has no route that can write this table, and no
benchmark endpoint was used for it. It is disclosed because the review count
changed while the benchmark was being verified.

## 12. Limitations

- **No teacher has verified any mark.** The central limitation; nothing else can
  produce a certified score.
- 16 items across two synthetic papers; confidence intervals are wide.
- Only 3 subquestion-level items; subquestion accuracy is barely measurable.
- No independent holdout; all papers are `development` and have been used in
  earlier harness work, so no generalisation claim is possible.
- One assessor per item is the current norm; two-teacher resolution is
  implemented and tested but not exercised by real reviewers.
- No teacher-verified grade exists, so grade agreement is unavailable.
- Paper B's header total (8) disagrees with its items (7); per-item marks are
  used and the defect is reported.
- Paper A Q3 and paper B Q2 are nested but stored at whole-question level.
- Live provider availability/quota was not tested in this work; no real
  inference was attempted, so there is no new provider-error evidence.
- Marking accuracy against teachers remains unknown. Unit-test fixtures and the
  deterministic stub are not evidence of real accuracy.

## 13. Precise next steps

1. A qualified maths teacher reviews the 16 items in **Review → Accuracy
   benchmark**, using `REVIEW_SHEET.md` as the paper checklist, and records each
   mark with the explicit confirmation. Disputed items remain ineligible until
   resolved; a second assessor can be added without destroying the first.
2. Confirm at least the 13 question-level items (and preferably all 16) before
   running a live evaluation. With fewer, the dashboard reports coverage and the
   script total remains unavailable.
3. Run:
   `python backend/scripts/benchmark_accuracy.py --mode live --ack INFERENCE`
   only when provider quota/route is available. Do not change prompts or
   thresholds during the run.
4. Verify the generated `report.md` and `reproduction` result. Only then may the
   dashboard display **Teacher-Verified Benchmark Accuracy**.
5. To generalise: add independent papers/topics, assign whole papers to a
   `holdout` partition before examining results, add a second assessor, and grow
   the verified sample well beyond 16 items before quoting a system-wide rate.
6. Re-run after any prompt, model, threshold or correction-logic change; keep
   old runs as historical evidence rather than editing them.

Until step 1–4 are complete, the honest answer to “how often does MathChecker
agree with a qualified teacher?” remains: **not yet measured**.
