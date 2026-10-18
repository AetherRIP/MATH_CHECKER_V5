# Why the same paper scored 47 and then 49 — and what now makes a repeat stable

**Date:** 2026-10-16
**Trigger:** live certification finding — two independent corrections of the same
paper returned different marks for one subquestion (`Q9: 0 then 2`), so the
submission totalled **47/50** in one run and **49/50** in the other.
**Fix commit:** uncommitted (working tree), see "Files changed" below.

---

## 1. What the evidence actually said

From `docs/results/LIVE_CLASSROOM_CERTIFICATION.json` (student `S-2113d5c0`,
assignment `Ch2L1 - Constructing Algebraic Expressions`, coursework `889658133898`),
both runs of the same paper:

| | run 1 | run 2 |
|---|---|---|
| result id | `89252459f6f34138b2ba44d532577e6d` | `9468eecab55146bb93d4b1bda36a9594` |
| model | `gemini-3.5-flash-lite` | `gemini-3.5-flash-lite` |
| Q9 | **0** | **2** |
| total | 47 | 49 |
| other 18 questions | identical | identical |

Two details in the stored data pointed straight at the mechanism:

* the only difference between the two runs was **Q9**, and it differed by exactly
  Q9's maximum (2) — not by a partial-credit amount;
* in run 1, Q9's `reason` was the **empty string** `""`, while every other
  question reported `"None"`.

The audit harness builds that field from `question["loss_reason"]`
(`verify_live_classroom.py:710`). `loss_reason` is `None` for a normal row and
`""` for exactly one row type in the codebase: the **technical-gap placeholder**
that `apply_blueprint` inserts when the marker returns no answer for a
question it knows exists (`backend/app/engine/blueprint.py`).

So run 1's Q9 was not the model saying "wrong". It was the pipeline saying
"the model never answered Q9".

## 2. Root cause

When the marker omits a question, `apply_blueprint` appends a placeholder:

```python
reconciled.append({
    "question_number": number,
    "max_marks": max_marks,
    "awarded_marks": 0,          # <- a numeric mark
    "loss_reason": "",           # <- nothing explains it
    "status": STATUS_REVIEW,
    "review_reason": "The marker did not return an answer for this question. "
                     "It is UNMARKED — do not treat this 0 as a confirmed score; "
                     "review it manually.",
})
```

Keeping the question in the **denominator** is deliberate and correct (a dropped
question must never inflate the percentage — pinned by
`test_missing_question_becomes_review_not_silent_zero`). Two things were not:

1. **The targeted retry that should heal the gap ran exactly once**, and it could
   only build its narrow prompt from a **cached mark-scheme file found by path**
   (`question_inventory.narrow_blueprint` → `blueprint_for_mark_scheme(path)`).
   With no such file the retry was refused outright with *"No reliable
   mark-scheme structure is available for a targeted retry"* — a message that
   claims the questions *"stay unmarked (not 0)"* while the stored row is a 0.
2. **The placeholder was indistinguishable from a score** in every human-facing
   field: `awarded_marks: 0` and a blank `loss_reason`.

Therefore: a **flaky model omission** — which the live runs prove happens, since
run 2 returned Q9 normally — turned into a permanent, persisted, numeric **0**.
The same paper then totalled 47 or 49 depending on whether the model happened to
emit the row that run. That is the whole of the 0-then-2 disagreement.

It is worth being precise: the sampling config was **not** the fault.
`DETERMINISTIC_SAMPLING = {"temperature": 0.0, "seed": 42}` is genuinely applied
on every marking call, and `seed` is a real field of the installed SDK's
`GenerateContentConfig` (verified directly against `google.genai.types`). The
app-side defect was what happened *after* the model answered.

## 3. Reproduction, against the real engine code

`docs/results/audit/q9_repro.py` drives the actual pipeline with a paper whose
model payload omits one question, then compares it with a run that returns it:

```
RUN A (Q3 omitted) -> Q3 awarded=0 status='REVIEW' loss_reason=''  TOTAL = 5/7
RUN B (Q3 returned) ->                                        TOTAL = 7/7
DISAGREEMENT: Q3 0 then 2   (totals 5 vs 7)

BEFORE THE FIX
  narrow_blueprint(None, ['3'])                    -> None
  run_recovery_round(mark_scheme_path=None)        -> ok=False still_missing=['3']
     error: No reliable mark-scheme structure is available for a targeted retry…

AFTER THE FIX
  narrow_blueprint(None, ['3'], blueprint=<in-memory>) -> built
  run_recovery_round(no path, in-memory blueprint) -> ok=True recovered=['3']
  healed TOTAL = 7/7   (run B was 7/7)
  => VERDICT: the two runs now AGREE: True
```

Same signature as the live finding (one question, differing by its full mark),
same cause, and the fix closes it.

## 4. The fixes

**F1 — the narrow retry no longer requires a cached scheme file.**
`question_inventory.narrow_blueprint(..., blueprint=None)`,
`recover_missing.build_narrow_blueprint(..., blueprint=None)` and
`run_recovery_round(..., blueprint=None)` now accept the reconciled structure the
caller already holds, and `workers/child.py` passes it. A scheme that arrived
from Classroom/Drive (no local cached path) can now be retried at all. The cached
lookup is untouched when no blueprint is supplied.

**F2 — the retry repeats while the budget allows.**
`workers/child.py` ran exactly one round (`already_attempted=0`, one call). It
now loops, bounded by the existing `recover_missing.need_recovery` budget
(`worker.max_retries`, default 2), re-deriving what is still missing after each
round. Since the live evidence shows the omission itself is flaky, the extra
bounded attempt is exactly the cheap chance that closes the gap. Successful rows
are still never re-marked and the round count stays inside the configured budget.

**F3 — the placeholder states what it is.**
The placeholder's `loss_reason` — the field the report, the CSV and the audit
harness actually display — is now populated with the explicit
`blueprint.UNMARKED_REASON` instead of being blank, matching `review_reason`
(one constant, so the two can never drift apart). A `0` next to a blank reason
read as "the student scored nothing, nothing to say" in every human-facing view;
it now reads as an unmarked gap.

Deliberately **not** changed: the placeholder still occupies a `0` in `earned`
and stays in the denominator. That behaviour is a documented design decision
pinned by an existing test, and changing it would alter grades and reports
upstream. The gap is now *healed* (F1+F2) or *labelled* (F3) instead.

## 5. Verification

| Check | Result |
|---|---|
| New regression tests `backend/tests/test_marking_determinism.py` | **13 passed, exit 0** |
| Same tests with the three fixes temporarily reverted | **5 failed, exit 1** — they bite |
| Reproduction before/after (`q9_repro.py`) | runs disagree → **agree**, exit 0 |
| Full backend suite after the fix | **1907 passed, 0 failed, exit 0** (was 1894; +13) |
| Affected suites (missing-questions, determinism, large-PDF, subpart coverage, worker child, repair) | 178 passed, exit 0 |

The tests cover: the failure stated as a fact about the data; the narrow prompt
built from memory with and without a cached file; recovery healing the gap;
refusal when there is genuinely no structure; the bounded repeat closing a gap
the first retry missed; the budget being respected when a gap never heals; the
placeholder's explicit reason; and a genuine student zero **not** being
relabelled as a gap.

## 6. What is still not deterministic — and why I am not claiming otherwise

These are honest residuals, not oversights:

1. **The model itself.** `temperature: 0` plus a fixed `seed` reduces variance
   but is not a hard guarantee from any hosted LLM: the same request can still
   return different text. Nothing in the application layer can force a remote
   model to be bit-reproducible. What the app now guarantees is that the
   *app-side* contributor to a mark swing — an omission silently becoming a 0 —
   no longer exists.
2. **Which model answered.** `MODEL_HEALTH` benches a model after repeated
   failures and persists that across worker processes, so a later run may
   legitimately be answered by a different model in the chain. The stored result
   records the model actually used (`model`), so such a difference is at least
   explainable rather than mysterious. This is the mechanism behind the *other*
   paper's `DOUBLE_CHECK_FAILED` (Gemini capacity).
3. **The double-check pass deliberately uses a different seed**
   (`SECOND_PASS_SAMPLING = {"temperature": 0.0, "seed": 20260101}`) so that a
   second pass is an independent opinion rather than a replay. A disagreement
   there is surfaced as review, never silently applied.

**Not verified:** how often the live model omits a question in practice, or how
often the repeated retry heals it, because that needs a live Gemini key and a
real paper. The mechanism is proven against the real engine code with a scripted
model; the *frequency* is not measured. A live re-run of the certification
harness (with `--repeat`) is the natural next step, now that the failure mode is
understood.

## 7. Distribution note

The packaged copies under `MathChecker_Distribution/` (`portable/`,
`release_staging/`, the built `MathChecker_Portable.zip` and
`MathChecker_Setup.exe`) are build outputs regenerated by
`scripts/build_release.py`; they were **not rebuilt** here, so the packaged
artifacts do not yet contain these fixes. Rebuilding and re-scanning is a release
action and was left for you to authorise.

## 8. Files changed

| File | Change |
|---|---|
| `backend/app/engine/question_inventory.py` | `narrow_blueprint(..., blueprint=None)` |
| `backend/app/engine/recover_missing.py` | pass the in-memory blueprint through |
| `backend/app/engine/blueprint.py` | `UNMARKED_REASON`; placeholder populates `loss_reason` |
| `backend/app/workers/child.py` | bounded repeat recovery; passes the live blueprint |
| `backend/tests/test_marking_determinism.py` | **new** — 13 regression tests |
| `docs/results/audit/q9_repro.py` | **new** — the reproduction / evidence |
