# MathChecker V5 — Performance Benchmark Report

Companion documents:
[PERFORMANCE_HARDENING_REPORT.md](PERFORMANCE_HARDENING_REPORT.md) (audit and
changes), [RELIABILITY_TEST_REPORT.md](RELIABILITY_TEST_REPORT.md) (test
evidence), [PERFORMANCE_BENCHMARK.json](PERFORMANCE_BENCHMARK.json) (raw data).

**Headline:** the recovered correction path is fast when the provider answers
(48–157 s for 26–45 page requests, against 668 s median in the archive), the
timeout and retry bounds hold, and **thinking-disabled is not recommended**: it
was faster on the one paper where both configurations completed, but it moved
marks on that same paper, and no run in this repository has shown it to be
accuracy-neutral on real scripts.

---

## 1. Method

* **Inputs.** Five real submissions already in this installation's data folder
  (5, 11, 15, 24 and 29 pages of handwritten work) marked against the 21-page
  scheme the application used for that assignment. Papers are identified as
  `paper-1…5` plus a SHA-256 of their bytes; no file name, student name, mark or
  scan is published.
* **Configurations.** `production` = exactly what the application sends today
  (`gemini-3.6-flash`, provider-default thinking). `no-thinking` = the identical
  request with `thinking_config.thinking_budget = 0`. Same prompt version, same
  `OUTPUT_SCHEMA`, same `temperature 0 / seed 42`.
* **Order.** Randomised per paper (`--seed 20261030`), so a provider that speeds
  up or slows down during the run does not systematically favour one arm.
* **Capacity waits.** While a model is known to be cooling down, no request is
  sent; the wait is recorded in `capacity_wait_s` and **excluded** from
  `call_s`. A refusal that survives the wait is recorded as an error, never
  dropped.
* **Two runs, both preserved.** The first run (`PERFORMANCE_BENCHMARK_run1_outage.json`)
  was dominated by 503s and is kept on purpose: deleting an inconvenient run is
  how a benchmark starts lying.

Reproduce:

```bash
python backend/scripts/benchmark_hardening.py --ack INFERENCE --papers 4 \
    --seed 20261030 --capacity-wait-s 900 --pause-s 30
```

---

## 2. What happened in the runs

### Run 1 — 2026-10-30T19:45Z, 5 papers (outage run, preserved)

| Arm | Completed |
|---|---|
| production | 2 / 5 |
| no-thinking | 0 / 5 |

### Run 2 — 2026-10-30T19:58Z, 4 papers (primary evidence)

| Paper | Pages (submission + scheme) | production | no-thinking |
|---|---|---|---|
| paper-1 | 5 + 21 = 26 | **ok**, 59.4 s, 9 questions, 33/33 marks | 503 UNAVAILABLE |
| paper-2 | 11 + 21 = 32 | **ok**, 67.5 s, 13 questions, 40/44 marks | **ok**, 32.7 s, 17 questions, 35/44 marks |
| paper-3 | 15 + 21 = 36 | **ok**, 157.2 s, 31 questions, 74/102 marks | 503 UNAVAILABLE |
| paper-4 | 24 + 21 = 45 | 503 UNAVAILABLE | **ok**, 121.9 s, 63 questions, 169/173 marks |

| Arm | Completed | Calls | Median call | Input tok | Output tok | Thinking tok |
|---|---|---|---|---|---|---|
| production | 3 / 4 | 59.4, 67.5, 157.2 s | **67.5 s** | 52 851 | 28 922 | 30 447 |
| no-thinking | 2 / 4 | 32.7, 121.9 s | 77.3 s | 42 914 | 34 352 | 0 |

**Do not read the medians as a speed comparison**: each is computed from 2–3
samples and the arms completed on *different papers*. The only honest paired
comparison is paper-2, where both arms finished:

| paper-2 | production | no-thinking | difference |
|---|---|---|---|
| call time | 67.5 s | 32.7 s | **2.1× faster without thinking** |
| questions detected | 13 | 17 | different granularity |
| marks awarded | 40 / 44 | 35 / 44 | **−5 marks** |
| shared questions compared | — | — | 9 |
| exact agreement | — | — | 7 / 9 (MAE 0.56) |
| disagreements | — | — | Q13: 3 vs 1, Q2: 3 vs 0 |

This is the finding that decides the configuration question: on the single
paper where the comparison is paired, disabling thinking was **twice as fast and
five marks lower**. The application's stated priority is accuracy, so the
default stays as it is.

---

## 3. Latency breakdown

### Archive (35 jobs, before this work)

From `backend/scripts/job_timing_breakdown.py` (full table in the hardening
report):

| Phase | Median | p95 | Total |
|---|---|---|---|
| queue | 1 205 s | 13 585 s | 43.74 h |
| preparing | 11 s | 32 s | 0.77 h |
| uploading | 10 s | 28 s | 0.14 h |
| marking | **668 s** | 8 235 s | 18.89 h |
| post | 0 s | 481 s | 0.83 h |

### Today, healthy path

| Paper | Pages | Upload | Call | Total wall |
|---|---|---|---|---|
| paper-1 | 26 | 0.0 s (cached) | 59.4 s | 59.4 s |
| paper-2 | 32 | 0.8 s | 67.5 s | 68.3 s |
| paper-3 | 36 | 0.4 s | 157.2 s | 157.6 s |
| paper-4 | 45 | 13.5 s | 121.9 s | 135.4 s |

Uploads are largely free after the first time because `file_registry` reuses
files by content hash; the 13.5 s case is a first upload of a 39 MB scan.

### Where the time is not

* **Queue:** not measurable in a single-user benchmark; measured from the
  archive instead (section 5).
* **Preparation:** 0.4–0.8 s on cached inputs here; 11 s median in production.
* **Thinking tokens:** 30 447 across three production calls (10 000 per call
  average) — real cost, but see section 2 for why removing it is not free.

---

## 4. Accuracy

### Against an independent reference

The repository's only reference set is two short papers with **scheme-derived**
marks (`docs/reference-marking/manifest.json`, 16 items, 13 questions). It has
never been teacher-verified — `review.verified_by` is null — so agreement here
means "agrees with the printed scheme", nothing stronger.

Measured 2026-10-30T18:48Z with `backend/scripts/benchmark_marking_config.py`
(same tree, same model, same prompt; retained unchanged in
[MARKING_CONFIG_BENCHMARK.json](MARKING_CONFIG_BENCHMARK.json)):

| Arm | Items exact | MAE | Missing marks | Median call | Thinking tokens |
|---|---|---|---|---|---|
| default (production) | 16 / 16 | 0 | 0 | 34.0 s | 6 674 |
| no-thinking | 16 / 16 | 0 | 0 | 54.4 s | 0 |
| low-thinking (1024) | 15 / 16 | 0.0625 | 0 | 45.1 s | 0 |

On two tiny papers no-thinking changed nothing; on 16 items it was also
**slower**, because on a short paper the provider's queueing dominates whatever
thinking saves. Combined with paper-2 above, the evidence is: thinking-off is
sometimes faster, never provably neutral, and sometimes slower. That is not a
case for changing a production default.

### Against a teacher

**Unavailable.** 0 of 16 reference items are teacher-certified. No accuracy
figure in this repository is a certified accuracy figure, and this report does
not create one.

### Question-granularity instability (a finding, not a footnote)

The same paper produced **13 and 17 questions** in two runs of two
configurations, and the earlier 41-page probe produced 31 and 35 entries. The
model sometimes merges or splits sub-questions. Consequences:

* mark *totals* are not comparable across runs without normalising question
  numbering, which is why the comparison above counts only shared question
  numbers;
* a teacher reviewing a paper may see a different number of rows than last time,
  even at `temperature 0` with a fixed seed;
* it makes any "one mark different" claim weak evidence in either direction.

This behaviour predates this work (unchanged prompt, schema and sampling). It is
recorded here because it constrains how much any benchmark of this application
can prove about accuracy.

---

## 5. Queue and worker performance

Phase 5 asked what causes the archive's 8 008 s queue waits. The answer is
arithmetic, not a scheduler defect:

| Model | Predicted queue wait | Observed |
|---|---|---|
| FIFO, measured service times, 3 workers | 72.03 h | — |
| FIFO, measured service times, 4 workers | **44.06 h** | **43.74 h** |
| FIFO, measured service times, 6 workers | 27.57 h | — |
| FIFO, 160 s service time (recovered path), 4 workers | **2.58 h** | — |

A four-worker FIFO server reproduces the observed queue wait to within 0.7 %. No
worker sat idle while a job was queued; the queue was `depth × service time /
width`. Raising concurrency would have shortened it and made the provider
problem worse — the measured failures in section 2 are mostly 503s, which more
parallel requests do not fix.

What changed instead: per-job service time. The archive's marking phase was
668 s median, p95 8 235 s (the fallback-model escalation). The healthy path
today is 59–157 s for 26–45 pages. Applying that service time to the same
workload predicts a 17× reduction in aggregate queue wait.

Residual queue costs that remain, and why:

1. **Preparation runs in the scheduling thread** (`_stage_inputs`): 11 s median,
   32 s p95, 1 846 s worst case. While it runs, no other result is collected.
2. **Legitimate capacity waits** (parks) still exist: 60 s minimum, growing,
   bounded by 4 parks / 15 minutes, and now visible in the job's stage text.

---

## 6. Batching

`engine/batching.py` is a planner with no execution path: nothing in the marking
chain calls `plan_batches`, and no merge layer exists. It was therefore not
enabled, and there is no measured speed or accuracy comparison to publish.

What can be said without inventing results is the cost model, from the page
counts measured above:

| Paper | Pages | Single request | Batches at ≤ 20 pages | Extra scheme reads |
|---|---|---|---|---|
| paper-1 | 26 | 1 call | 2 calls | +1 |
| paper-2 | 32 | 1 call | 2 calls | +1 |
| paper-3 | 36 | 1 call | 2 calls | +1 |
| paper-4 | 45 | 1 call | 3 calls | +2 |

Batching trades one large request for 2–3 smaller ones plus a merge step. It
would only pay off if the provider systematically refuses large requests — which
is exactly what section 2 shows intermittently (3 of 8 arms returned 503, and
the two largest requests each failed once). That is a strong argument for
building it *later*, behind a flag, with a write-up of question-boundary
handling — not for switching it on now.

---

## 7. Answers to the deployment questions

| Question | Answer |
|---|---|
| 1. Does the timeout reliably stop or isolate abandoned requests? | Client-side yes: the process owning the request is killed and reaped. Provider-side completion of an abandoned request is unobservable and stated as a limitation. |
| 2. Can duplicate attempts run concurrently? | No in this parent: the claim is transactional, a retry is only scheduled after the child exits, and an existing result short-circuits the retry. |
| 3. Does the retry policy prevent overload amplification? | Yes for the paths measured: SDK retries pinned to 1, ≤2 key sends for a provider-wide 503 (was 8), parks grow 60→600 s, budget 15 min / 4 parks. |
| 4. Is thinking-disabled genuinely faster across multiple papers? | **Not established.** Faster on 1 paired paper (2.1×), slower on the reference set, and the arms completed on different papers otherwise. |
| 5. Does it preserve marking accuracy? | **No evidence that it does.** On the one paired paper it lost 5 marks across 2 of 9 shared questions. Not recommended. |
| 6. Does batching improve performance or reliability? | Unknown and unmeasured — no execution path exists. Cost model only. |
| 7. What causes excessive queue waiting? | Queue depth × per-job service time ÷ workers, reproduced to 0.7 %. The lever is service time, not worker count. |
| 8. What is safe to deploy immediately? | Everything in the hardening report: retry/key-send bounds, park backoff, stall watchdog, timeout diagnostics. None changes marks. |
| 9. What needs teacher review first? | Any change to thinking budget, batching, or question granularity. All affect marks or coverage. |
| 10. What remains unresolved? | Provider shedding of large requests; provider-side cost of abandoned requests; staging in the scheduling loop; teacher-certified accuracy (0 of 16). |
