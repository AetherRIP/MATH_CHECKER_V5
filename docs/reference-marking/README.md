# Marking reference set and teacher verification

This directory holds the reference papers and the expectations derived from
their printed mark schemes. It is the input to the benchmark; it is **not** a
teacher-certified dataset until teachers confirm the marks item by item.

Current status: **0 of 16 reference items are teacher-verified.** The certified
accuracy figure is therefore unavailable, not 0%.

## Ground-truth classes

| Class | Meaning | Eligible for the certified score? |
|---|---|---|
| `teacher_verified` | A qualified teacher confirmed the mark | Yes, after an explicit assessment |
| `official_reference` | A printed scheme or official answer exists; the candidate-specific mark has not been independently confirmed | Only after a teacher assessment |
| `ai_generated` | The mark came from an AI correction | **Never** |
| `unverified` | Provenance is unknown | **Never** |

A dataset-level signature does not promote the items. Prior AI output, a
passing harness, or a green dashboard does not either. The loader defaults the
existing manifest to `official_reference`; only an explicit record in the
verification overlay makes an item eligible.

## Files

- `manifest.json` — papers, questions, maximum marks, scheme clauses, the
  printed-scheme ruling, and the expected mark.
- `papers/` — the synthetic student papers and mark schemes used by the live
  harness.
- `REVIEW_SHEET.md` — the printable human review sheet.
- Verification records and run artifacts are **not** committed here. They live
  under the application data directory (`data/benchmark/`) because they contain
  assessor identities and raw provider output.

## Dataset rules enforced by `app.benchmark.dataset`

- Every item has a stable `reference_id` (`paper-a.q1`). Explicit IDs are
  allowed but must be unique.
- `max_marks` and the expected mark are non-negative integers; the expected mark
  cannot exceed the maximum.
- A paper's `marks_available` and `expected_total` must equal the sums of its
  items. A disagreement is an error, not a warning.
- Partitions are `development` and `holdout`, assigned per **paper**. An item
  cannot disagree with its paper, because splitting one script across tuning and
  holdout would contaminate both. Multiple AI correction versions are separate
  run artifacts, not separate dataset items, so they always share the paper's
  partition.
- A scheme header that disagrees with the sum of its parts is a warning. The
  per-item marks are used and the discrepancy is reported.
- A nested-subquestion item stored under a whole-question label is a warning:
  it can contribute to question-level metrics, not subquestion metrics.

## Teacher verification

Open **Review → Accuracy benchmark** in the app. For each item:

1. read the scheme clauses and the candidate's working;
2. compare the AI mark with the scheme-derived mark (they are shown separately);
3. enter the mark you would award;
4. choose `verified`, `disputed`, or `incomplete`;
5. add accepted alternative methods and a note where relevant;
6. tick the explicit confirmation and record the assessment.

The record is append-only. A second teacher may assess the same item; if the
marks disagree, both are preserved and the item becomes `disputed` and
ineligible. The system never chooses the assessment closest to the AI mark.

API equivalents:

```
GET  /api/benchmark/overview
GET  /api/benchmark/references
GET  /api/benchmark/references/{reference_id}
POST /api/benchmark/references/{reference_id}/verify
GET  /api/benchmark/runs
POST /api/benchmark/runs/{run_id}/reproduce
```

None of these endpoints writes to `results`, a student grade, a retry budget, a
provider health record, or Google Classroom. The only write is the benchmark
overlay.

## Running an evaluation

```
# Refuses (exit 4) when nothing is teacher-verified; makes no provider request.
python backend/scripts/benchmark_accuracy.py --partition all

# Real pipeline, only after references are verified and with explicit consent.
python backend/scripts/benchmark_accuracy.py --mode live --ack INFERENCE

# Replay archived provider output for an audit or a regression comparison.
python backend/scripts/benchmark_accuracy.py --allow-unverified --mode replay \
    --replay paper-b=docs/logs/paper_b_fixed_outcome.json
```

`--allow-unverified` measures against the printed scheme and is always reported
as **NOT CERTIFIED**. It must never be presented as the teacher-certified score.

Each run writes `run.json`, `report.md`, and `raw/<paper>.json` under
`data/benchmark/runs/<run_id>/`. The run records the dataset version and hash,
the full reference snapshot, every raw outcome, the model/prompt/config hash,
the app version and git commit. `reproduce_run` recomputes the metrics from that
snapshot and the raw outcomes without contacting the provider. A later teacher
assessment cannot silently change an old report.

## Known limitations of the current reference set

- 16 items across two synthetic papers; the confidence intervals are wide.
- All items are in the `development` partition; there is no independent holdout.
- `paper-b`'s scheme header states 8 marks while its parts sum to 7.
- `paper-a` Q3 and `paper-b` Q2 are nested but stored at whole-question level.
- No teacher has confirmed any mark yet.
