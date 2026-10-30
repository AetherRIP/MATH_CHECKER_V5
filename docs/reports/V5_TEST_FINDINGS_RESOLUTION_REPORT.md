# V5 — End-to-end test findings: resolution and regression report

| | |
|---|---|
| Scope | The three confirmed application/tooling findings from `V5_END_TO_END_TEST_REPORT.md`, plus the provider-outage resilience finding |
| Fixture | `paper-b` (synthetic, 1 page, 4 items: 1a/1b/1c + Q2, 7 marks) |
| Independent reference | `docs/reference-marking/manifest.json#paper-b`, derived from the printed scheme (6/7) |
| Branch / commit | `main`, working tree only — **nothing committed or pushed** |
| Overall result | **PASS** — all three findings reproduced, fixed, regression-tested, and one of them re-verified end-to-end with a real model |

---

## 1. Findings, causes and outcomes

| # | Finding | Reproduced | Root cause | Status |
|---|---|---|---|---|
| 1 | Accuracy harness ignores nested `subquestions` | Yes (exact strings) | Both comparators read only each question's roll-up row, which the app fills *in addition to* the per-marking-point rows | **FIXED** |
| 2 | Corrupt PDF accepted with HTTP 200 and queued | Yes (HTTP 200, `page_count: 0`, `QUEUED`) | Upload validated the filename suffix only; `pdf_inspect.page_count` returns `0` instead of raising | **FIXED** |
| 3a | `NEEDS_REPAIR` never reaches the result's warnings | Yes (warning list unchanged) | The state was stored as `verification.blueprint.verification_state` and read by no consumer | **FIXED** |
| 3b | Verifier can print `earned 8 / available 7` in REVIEW | Yes (`8 / 7`, `114.29 %`) | `earned` is the raw awarded sum (correct — a mark is never clamped) but the excess was only reported as a per-question anomaly, never as a totals-level statement | **FIXED** (reported, not clamped) |
| 4 | Preferred providers returned real `504 DEADLINE_EXCEEDED` | Observed live in the previous run | External provider behaviour; the app's cooldown/park/failover path handled it | **NO CHANGE to the outage path**; the 504's *classification* was fixed afterwards (§6a) |

All four were reproduced **before** any edit, against the then-current code, using the app's own functions
(`apply_blueprint`, `verify_result`, `pdf_inspect`, the real FastAPI upload endpoint) and no mocked engine.

---

## 2. Finding 1 — the accuracy harness could not see the marks it was asked to check

### Reproduction (before)

Fed the *shape the app actually stores* (question 1 roll-up 3/4 **plus** `subquestions`
1a 1/1, 1b 1/1, 1c 1/2):

```
single-paper verdict : NOT CERTIFIED (4 mismatches)
   - Q1  : the model marked a question that is not in the answer key (awarded 3)
   - Q1a : expected 1 but the model returned no mark for this question
   - Q1b : expected 1 but the model returned no mark for this question
   - Q1c : expected 1 but the model returned no mark for this question
aggregate harness    : compared 2/4 items, exact 2
   matching_errors   : {'unmatched_model': ['1'], 'unmatched_manifest': []}
```

These are the same strings the live run produced (`docs/logs/paper_b_live_report.txt`), so the
reproduction and the field failure agree.

### What changed

* `MathChecker_Distribution/scripts/verify_live_marking.py`
  * `Outcome` gained `subquestions` (`id → {awarded, max, question}`), and a new
    `load_outcome(stored)` builds it from a stored result — extracted out of `mark_once` so the
    accuracy machinery is testable without an app, a model or quota.
  * `compare_marks()` now routes each answer-key label: a marking point (`1c`) is compared against
    that marking point; a whole question (`2`) against the question. The question's roll-up row is
    recognised as the same marks counted once more and is **not** reported as an invented question.
  * A marking point with no stored row is reported as **“could NOT be compared”** with the
    question-level figure it was rolled up into — a harness/attribution limit — instead of the false
    accusation “the model returned no mark”.
  * The report and outcome JSON now carry a `parts :` line and a `subquestions` block, so the
    per-marking-point evidence survives into the artifact.
* `backend/scripts/verify_marking_reference.py`
  * `compare_paper(paper, questions, subquestions=None)` merges the marking points as comparable
    labels; a roll-up whose children were all compared is recorded as `rollup_labels`, not as a
    matching error.
  * Two *different* marking points are never stem-matched to each other (`1b` returned where the key
    says `1a` is now **two** reported errors, not one agreement).
  * Group fallback (a question-level row against a key that itemises parts) is preserved, and its
    label is no longer double-counted as an unmatched model label.

### Before / after counts

| Measurement | Before | After |
|---|---|---|
| Reference items compared (paper-b) | 2 / 4 | **4 / 4** |
| Exact agreements | 2 | **4** |
| False “no mark returned” errors | 3 | **0** |
| Spurious matching errors | 1 (`1`) | **0** |
| Single-paper verdict | NOT CERTIFIED (4 mismatches) | **CERTIFIED (0)** |
| MAE per item (real outcome, offline) | n/a (items invisible) | **0** |

After — measured on the **real** stored outcome of the fresh live run (`docs/logs/paper_b_fixed_outcome.json`):

```
compared 4 of 4 | exact 4 | MAE 0
over 0 under 0 | matching_errors {'unmatched_model': [], 'unmatched_manifest': []}
rollups {'1': ['1a', '1b', '1c']} | subquestions compared 3
   2  expected 3 awarded 3 by exact
   1a expected 1 awarded 1 by exact
   1b expected 1 awarded 1 by exact
   1c expected 1 awarded 1 by exact
```

### Regression tests added

`backend/tests/test_live_marking_verify.py` (7 new) and `backend/tests/test_marking_reference_harness.py` (6 new):

* multiple top-level questions with nested subquestions (paper-b shape);
* a mix of questions with and without subquestions;
* a missing/wrong mark on a genuine nested item;
* an unreachable marking point (older result, no breakdown) → “could NOT be compared”;
* a top-level-only key (the bundled fixture quiz) → unchanged behaviour;
* a marking point outside the key → reported;
* a mislabelled marking point (`1b` vs `1a`) → reported, never aligned;
* aggregation without double-counting (children + roll-up add up once).

No marking logic was changed to make any of these pass.

---

## 3. Finding 2 — corrupt PDFs were queued

### Reproduction (before)

```
corrupt   -> HTTP 200 page_count=0 job_id=yes  queued job status='QUEUED'
truncated -> HTTP 200 page_count=0 job_id=yes  queued job status='QUEUED'
empty     -> HTTP 200 page_count=0 job_id=yes  queued job status='QUEUED'
valid     -> HTTP 200 page_count=1
```

### What changed

* `backend/app/engine/pdf_inspect.py` — new `integrity_problem(path)`:
  empty file; no `%PDF-` header within the first 1024 bytes (the spec tolerates leading junk);
  a document that opens but reports no pages; a first page that cannot be read; and the case where
  neither PyMuPDF nor pypdf can open it. It returns a short fixed sentence (never document contents,
  never a parser traceback). PyMuPDF is tried first and pypdf is the fallback, so a file the existing
  parser accepts is never rejected.
* `backend/app/api/routes_all.py` — both upload endpoints (`/api/corrections/unified` and
  `/api/corrections/manual`) validate each **PDF** after writing it and **before** any
  `pending_manual_files` row or job is created; on failure the staged files for that upload are
  deleted and the request is refused with `400 {"code": "corrupt_pdf"}`.

Image uploads (`.png/.jpg/.webp`) are unchanged — see §8.

### Before / after

| Case | Before | After |
|---|---|---|
| Corrupt body with a `%PDF-` header | 200, queued, `page_count: 0` | **400** `corrupt_pdf` — “it is not a readable PDF.” |
| Truncated real PDF | 200, queued | **400** |
| Empty file | 200, queued | **400** — “the file is empty.” |
| Text file named `.pdf` | 200, queued | **400** — “it does not contain a PDF header.” |
| Corrupt **mark scheme** | 200, queued | **400**, names the mark scheme |
| Valid 2-page PDF | 200, queued, marks | 200, `page_count: 2`, job **COMPLETED**, result stored |
| 31-page PDF | 200, queued, marks | unchanged (covered by `test_unified_correction.py`) |

Verified on the real endpoint via `TestClient`: after each refusal the `correction_jobs` table is
empty, `pending_manual_files` is empty, and the staging folder has no file.

### Regression tests added

`backend/tests/test_upload_pdf_validation.py` (7): corrupt / truncated / empty / non-PDF / corrupt
scheme / manual endpoint, each asserting HTTP 400 **and** the absence of a queued job, pending row and
staged file; plus a valid PDF that still follows the normal workflow to `COMPLETED`.

**One pre-existing test needed its input corrected:**
`test_worker_control.py::test_off_does_not_disable_manual_correction_or_the_site` uploaded
`b"%PDF-1.4 mock student"` — not a PDF at all, accepted only because nothing checked content. Its
assertions are unchanged; the fixture now uploads a real one-page PDF (and still asserts HTTP 200 +
a job id). This is the intended consequence of validating content, not a weakened assertion.

---

## 4. Finding 3a — the scheme audit's verdict now reaches the result

### Reproduction (before)

`verify_result(result, {"verification_state": "NEEDS_REPAIR", ...})` returned:

```
status   : VALID
warnings : ['2 question(s) have no M/A/B breakdown metadata …']
blueprint.verification_state : NEEDS_REPAIR      ← present, read by nobody
```

### What changed

`backend/app/engine/verify.py` appends one warning when the structure this paper was marked against
was not confirmed by the app's own audit:

> The mark scheme structure this paper was marked against is NEEDS_REPAIR: the app's own check did
> not confirm the scheme. The marks are unchanged, but verify the scheme before the grade is used.

* Source of truth: `blueprint_report["verification_state"]` — the persisted audit state written by
  `blueprint._build_blueprint` and forwarded by the worker child — with the completeness verdict
  appended when one exists.
* Emitted once (`_add_warning` de-duplicates), for `NEEDS_REPAIR`, `NEEDS_MANUAL_REVIEW`,
  `REPAIRING`, `REVERIFYING` only; a `VERIFIED` scheme adds nothing.
* Appended after the per-question reasons so the existing warning order is untouched, and no mark,
  status or grade is altered by it.

### Before / after

| | Before | After |
|---|---|---|
| `NEEDS_REPAIR` in `verification.warnings` | no | **yes** |
| `VERIFIED` scheme | no warning | **no warning** |
| Other warnings preserved / order | — | preserved (test asserts the marker's own note still comes first) |
| Duplicate emission | — | impossible (de-duplicated) |

`backend/tests/test_jobs_and_verify.py` — new `TestSchemeAuditPropagation` (4 tests): propagation,
absence for a verified scheme, single emission, existing warnings and their order.

---

## 5. Finding 3b — `earned 8 / available 7` is now explicit, never clamped

### Reproduction (before)

```
status: REVIEW | earned/available: 8 / 7 | percentage: 114.29
anomalies: ['Question 1 awarded 5 marks but its maximum is 4 …']
warnings : ['2 question(s) have no M/A/B breakdown metadata …']
```

The anomaly named the offending question, but the score block itself read `8/7` with nothing saying
where the extra mark came from — and no field distinguished “awarded total” from “marks available”.

### What changed

`verify_result` now returns `over_available = max(0, earned - available)` and, when it is non-zero,
one totals-level warning:

> The awarded total (8) exceeds the 7 marks available by 1 — question 1 (5 of 4). No mark has been
> changed: this correction needs a human decision before the score is used.

* `earned` and `available` keep their exact meanings (awarded sum, maximum sum) and are **never**
  clamped: silently lowering a mark is the one thing this module must not do.
* `status` stays `REVIEW`, `verified` stays `False`.
* The row counts and the printed totals are the same numbers (`8 = 5 + 3`, `7 = 4 + 3`), so the
  display is arithmetically consistent and the excess is named rather than concealed.
* No special case for `8/7`: the rule is “awarded exceeds available”, whatever the numbers.

### Before / after

| Case | Before | After |
|---|---|---|
| Normal 6/7 | `8`/`10`-style clean block, no excess field | `over_available: 0`, `VALID`, no excess warning |
| Over-awarded 8/7 | `8 / 7`, REVIEW, per-question anomaly only | `8 / 7`, REVIEW, `over_available: 1`, explicit totals warning naming Q1 |
| Awarded marks after verification | unchanged | **unchanged** (asserted in the test) |

`backend/tests/test_jobs_and_verify.py` — new `TestScoreAboveAvailable` (3 tests): excess reported and
not clamped, normal score clean, warning never duplicated.

---

## 6. Finding 4 — provider outage: verified, not changed

Deterministic tests only; **no new real outage is claimed**. `backend/tests/test_provider_outage.py` (7):

| Property | Result |
|---|---|
| A 504 is retryable but bounded (`max_retries ≤ 2`, not parked) | PASS |
| The retry budget is exhausted and reported as a failure with an error code | PASS |
| The worker-level `error_kind: timeout` classifies as `TIMEOUT` (1 retry) | PASS |
| A 504 does not disable the key and does not put it in cooldown | PASS |
| One 504'd request is not resent on the same key (1 request) | PASS |
| Failover: preferred model 504s, the next model marks (bounded attempts) | PASS |
| All models 504 → `RuntimeError`, **no result returned**, both models attempted once | PASS |

Measured behaviour worth recording (not changed):

* a server-side deadline does **not** bench the model (only 404/`limit: 0`, timeout and
  temporary-error texts do), so the next job still tries it. Documented, not changed.

### 6a. The 504's classification (fixed after §6)

`errors.classify_gemini_error` read the recorded message `504 DEADLINE_EXCEEDED. {…'Deadline
expired…'}` as **`unknown`**, because the check looked for the space-separated `"deadline
exceeded"` while the provider writes `DEADLINE_EXCEEDED` (and its nested message says "Deadline
**expired**", not "exceeded"). The job policy then mapped it to `TRANSIENT_PROVIDER_ERROR` with
**2** retries — three full marking passes for a request the provider had already answered with
"this will not finish".

It now also matches `deadline_exceeded`, so the kind is `timeout`:

| | Before | After |
|---|---|---|
| `classify_gemini_error` kind | `unknown` | **`timeout`** |
| Retry class | `TRANSIENT_PROVIDER_ERROR` | **`TIMEOUT`** |
| Retries after the failed attempt | 2 | **1** |
| Key handling | failure counted | failure + `timeout_errors`, no cooldown (a timeout never cools a key) |
| Provider gate | only high-demand is tracked | unchanged |

Nothing else moved: the model loop already stopped after one attempt per model on this message, the
key is still neither disabled nor parked, and `client_proxy` still sends exactly one request. Only
the job-level budget changed, from two retries to one.

Tests: `backend/tests/test_provider_outage.py` (`test_the_recorded_504_is_classified_as_a_timeout`,
`test_a_504_costs_one_retry_and_is_not_parked` — including exception → `error_kind` → job policy) and
`backend/tests/test_key_manager.py` (`test_a_504_deadline_is_a_timeout_in_every_spelling`).

---

## 7. End-to-end re-run on the same submission

Command (opt-in, real key, real PDFs, real model, isolated throwaway data directory):

```
python MathChecker_Distribution/scripts/verify_live_marking.py --live \
  --data-dir <isolated copy of config/secrets> \
  --student-pdf docs/reference-marking/papers/paper-b-student.pdf \
  --scheme-pdf  docs/reference-marking/papers/paper-b-mark-scheme.pdf \
  --expected    docs/logs/paper-b-expected.json
```

Result — **15/15 checks passed**, evidence in `docs/logs/paper_b_fixed_report.txt` and
`docs/logs/paper_b_fixed_outcome.json`:

```
the app accepts a real PDF and mark scheme — pages=1
the live model completes the correction — COMPLETED in 15s
marking: gemini-3.5-flash-lite | result 70158da4939e4f24bc3f395b8d5ca144
marks  : Q1 3/4, Q2 3/3
parts  : 1a 1/1, 1b 1/1, 1c 1/2
total  : 6/7 = 85.7143% | provisional grade 8
verdict: CERTIFIED                      (before the fix: NOT CERTIFIED, 4 mismatches)
the real installation's config and secrets are untouched — byte-identical
the throwaway data (and its copy of the key) is deleted
```

Against the independent reference:

| Item | Available | Expected | V5 awarded | After verification | Agreement |
|---|---|---|---|---|---|
| 1a | 1 | 1 | 1 | 1 | exact |
| 1b | 1 | 1 | 1 | 1 | exact |
| 1c | 2 | 1 | 1 | 1 | exact (M1 kept, A1 not earned — the 42 vs 41 error) |
| 2 | 3 | 3 | 3 | 3 | exact |
| **Total** | **7** | **6** | **6** | **6** | **exact** |

* **No mark was altered by verification:** the stored marks equal the marks awarded (6/7), and
  `verify_result`'s only mutations are to its own report — asserted by
  `test_awarded_above_max_is_review` and `test_eight_of_seven_is_reported_and_not_clamped`.
* **Model substitution (limitation):** the configured chain was saturated — the first live attempt
  used the normal chain and hit real `504 DEADLINE_EXCEEDED` and `429 RESOURCE_EXHAUSTED` until the
  job's 15-minute capacity budget ran out, so it was stopped and its throwaway data deleted. The
  successful run pins the chain to `gemini-3.5-flash-lite` (then `3.6`, `3.5`). The pipeline,
  verification, persistence and harness are unchanged; only the marking model differs from the
  normal preferred order.
* The previous full-chain run (unfixed harness) is kept: `docs/logs/paper_b_live_report.txt`,
  `paper_b_live_outcome.json`. Original evidence was not deleted.

---

## 8. Remaining issues and untested paths

| Item | Status | Note |
|---|---|---|
| `verify_marking_reference.py` full live aggregate (`--only paper-b`) | **NOT RUN** | The single-paper live run covers the same comparison; the aggregate run would have spent another paid correction while the preferred models were rate-limited. The fixed aggregate comparison was exercised offline on the real outcome (4/4 exact). |
| Normal (preferred-model) chain on the fixed code | **BLOCKED** (external quota) | Real 504/429 saturation; the lite-model run above is the substitute. |
| Image uploads (`.png/.jpg/.webp`) content validation | **NOT IMPLEMENTED** | Out of the confirmed finding; a corrupt image is still accepted and failed later. |
| A legacy stored result with no subquestion breakdown | **PARTIAL** | Now reported honestly as “could NOT be compared” and, in the aggregate harness, as a group total — but an itemised reference cannot be matched against it. |
| A 504 classified as `unknown` (message spelling) | **FIXED** (§6a) | Now `timeout`: one retry instead of two. |
| `NEEDS_REPAIR` also changing the result's status to REVIEW | **NOT DONE** | The finding asked for the warning to be surfaced; changing status would alter existing verification semantics. |
| Frontend build / UI checks | **NOT RUN** | No UI files were touched this round. |
| The reference set is still unsigned (`review.verified_by` empty) | **OPEN** | Unchanged by design: the aggregate harness reports `MEASURED BUT NOT CERTIFIED` (exit 4) until a teacher signs `REVIEW_SHEET.md`. |

---

## 9. Commands executed and actual results

```
# reproduction (before any edit)
python /tmp/repro_findings.py                 -> NOT CERTIFIED (4), compared 2/4, 8 / 7, NEEDS_REPAIR unwarned
python /tmp/repro_pdf.py                      -> corrupt/truncated/empty: HTTP 200 + QUEUED

# targeted regression (after each fix)
pytest backend/tests/test_provider_outage.py \
       backend/tests/test_marking_reference_harness.py \
       backend/tests/test_upload_pdf_validation.py \
       backend/tests/test_live_marking_verify.py \
       backend/tests/test_jobs_and_verify.py     -> 74 passed
pytest backend/tests/test_provider_outage.py \
       backend/tests/test_key_manager.py \
       backend/tests/test_retry_architecture.py  -> 80 passed (after the §6a classifier change)

pytest backend/tests/test_worker_control.py \
       backend/tests/test_upload_pdf_validation.py -> 14 passed

# full suite (all edits in place)
pytest backend/tests -q                          -> 2120 passed, 1 skipped, 0 failed (607 s)
# baseline before this work: 2086 passed, 1 skipped (2087 collected); +34 new tests = 2121 collected

# live end-to-end, real model
verify_live_marking.py --live …                  -> 15/15 checks passed, CERTIFIED, 6/7, 4 parts exact
```

The first full-suite run (before the `test_worker_control` fixture was corrected) reported
`2119 passed, 1 skipped, 1 failed`; that single failure was the stale non-PDF fixture and is fixed —
both numbers are recorded here rather than presenting only the green run.

## 10. Files changed

| File | Change |
|---|---|
| `MathChecker_Distribution/scripts/verify_live_marking.py` | subquestion-aware outcome, comparison, report and outcome JSON |
| `backend/scripts/verify_marking_reference.py` | subquestion merging, roll-up recognition, no cross-marking-point stem matching, group label de-duplication |
| `backend/app/engine/pdf_inspect.py` | `integrity_problem()` content validation |
| `backend/app/api/routes_all.py` | refuse unreadable PDFs before queueing (both upload endpoints); staged-file cleanup |
| `backend/app/engine/verify.py` | scheme-state warning; `over_available` + totals warning; `_add_warning`, `_over` |
| `backend/app/errors.py` | `deadline_exceeded` classified as a timeout (§6a) |
| `backend/tests/test_live_marking_verify.py` | +7 harness tests |
| `backend/tests/test_marking_reference_harness.py` | new (6 tests) |
| `backend/tests/test_upload_pdf_validation.py` | new (7 tests) |
| `backend/tests/test_provider_outage.py` | new (8 tests) |
| `backend/tests/test_key_manager.py` | +1 classifier test |
| `backend/tests/test_jobs_and_verify.py` | +7 verification tests |
| `backend/tests/test_worker_control.py` | fixture uploads a real PDF (assertions unchanged) |

No marking rule, verification rule, retry policy or UI was redesigned; no verification/validation was
weakened; no student data, result or evidence was deleted.

## 11. Git status and attestation

```
 M MathChecker_Distribution/scripts/verify_live_marking.py
 M backend/app/api/routes_all.py
 M backend/app/engine/pdf_inspect.py
 M backend/app/engine/verify.py
 M backend/scripts/verify_marking_reference.py
 M backend/tests/test_jobs_and_verify.py
 M backend/tests/test_live_marking_verify.py
 M backend/tests/test_worker_control.py
?? backend/tests/test_marking_reference_harness.py
?? backend/tests/test_provider_outage.py
?? backend/tests/test_upload_pdf_validation.py
```

Pre-existing working-tree changes from earlier work (`README.md`, `backend/requirements.txt`,
`docs/architecture/PROJECT_STRUCTURE.md`, `docs/README.md`, `setup.bat`, `start.bat`, `.env.example`,
`backend/requirements-lock.txt`, `docs/development/WINDOWS_SETUP.md`, `docs/reports/V5_END_TO_END_TEST_REPORT.md`,
`scripts/setup_windows.ps1`, `scripts/verify_windows.ps1`) are untouched and preserved.

**No commit and no push was made.** Run artifacts are local evidence in the git-ignored
`docs/logs/`, and the isolated credential copy used for the live run was deleted.
