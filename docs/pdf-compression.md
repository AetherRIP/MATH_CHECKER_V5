# PDF compression — student papers and mark schemes

Date: **2026-10-21**. Applies to the standard correction pipeline (worker
pool), for **both** the student submission and the mark scheme
(`app/engine/pdf_optimize.py`).

Nothing in this pass was committed or pushed.

---

## 1. What the implementation was before

| Question | Finding |
|---|---|
| Was the student PDF compressed? | No. The downloaded/normalized PDF was uploaded to the model exactly as received. |
| Was the mark scheme compressed? | No. Each job re-uploaded the full shared scheme. |
| Same file compressed repeatedly? | Not compressed at all — but the *same* scheme was re-uploaded for every student. |
| Before or after upload? | n/a |
| Original preserved when retention is on? | Yes, `retain_job_submissions` copied it into `retained_files`; compression was not a factor. |
| One shared utility? | The pipeline shared staging and validation, but had no compression utility. |
| Could compression damage maths? | Unproven — there was no compression. |
| Sizes/times measured? | No. |

So the change is one shared optimizer, wired into the pipeline's single
preparation step, rather than a second compression system.

## 2. What runs now

`optimize_pdf(path, role="submission" | "mark_scheme", job_id=...)` returns the
path that should be used plus an `OptimizationReport`. The **source file is never
modified, moved or replaced**.

1. **Plan** (cache miss only): read the page count, image DPI relative to the
   on-page placement rectangle, and the stored bytes those images hold.
2. **Lossless pass** (always eligible): `garbage=4, deflate, clean, objstms`.
   This is what shrinks digitally generated papers that ship uncompressed
   streams — measured 65.5 % on a 6-page text paper with an identical text layer
   and 0.0 pixel difference.
3. **Lossy image pass** only when oversized images are *both* real and dominant:
   images above 250 dpi whose stored bytes are ≥ 35 % of the file, on files
   ≤ 120 MiB and ≤ 250 pages. Each such image is resized (LANCZOS) to the
   resolution of its **largest placement** at 200 dpi and re-encoded as JPEG
   (quality 85 submission / 88 mark scheme). Masks, images with soft masks,
   images under 64 px and absurd pixel counts are left alone.
4. **Candidate gate**: accept only when the result is ≥ 3 % smaller **and**
   passes `verify_candidate`.
5. **Verify** (independent re-open of both files): identical page count,
   identical page size/rotation, identical text layer when the source had one,
   every sampled page renders, mean pixel difference within tolerance, and —
   new in this pass — **faint ink retained**: a page's mid-tone ink count must
   stay above 75 % of the original's, because a nearly-white page of light
   pencil differs from a faded one by too few grey levels for the mean-difference
   check to see.
6. **Fallback**: a failed, rejected, oversized, encrypted, unreadable or
   unhelpful result returns the original path with the reason recorded. The
   optimizer never raises into a correction.

### Cache and repeated work

Artifacts live in `data/tmp/pdf_cache/<2-hex>/<key>.pdf` (+ `.json` sidecar) with

```
key = sha256(source bytes)[:20] + sha256(role, lossy, dpi, quality, algorithm version)[:10]
```

So a retry, a re-claimed job after restart, a second student on the same job, or
the *next student on the same mark scheme* reuses one validated artifact. The
key includes the role and every compression setting, so a submission can never
reuse a scheme's artifact (or another student's). A cache hit is answered from
the key alone and costs **2–5 ms**; the planning walk is not repeated.

The cache is bounded (2 GiB, 30 days) and swept at startup. Submission artifacts
are deleted at the job's terminal cleanup (`release_job`), because a compressed
copy of a student's paper is processing data, not retained data. Mark-scheme
artifacts are shared reference documents and stay until the age/size sweep.

### Integration points

| Pipeline | Where | Documents |
|---|---|---|
| Standard | `WorkerPool._stage_inputs` — the once-per-job preparation step, used on the first attempt only; retries reuse `prepared.json` | submission PDFs, mark scheme, question paper |

## 3. Original vs processing copy

* `pending_manual_files` / `data/tmp/job-<id>` hold the **original** upload. That
  is what `retain_job_submissions` copies into `retained_files`, and what the
  viewer (`/api/results/{id}/pages`) serves.
* `data/tmp/pdf_cache` holds the **optimized processing copy**. It is never
  written over the original, never retained, and is deleted with the job.
* When retention is off, the job's originals are deleted as before and the
  cached submission copy is released in the same cleanup.
* If retention itself fails (disk full, locked file), cleanup stops before
  deleting anything — including the cache copy — so the only copy of a paper the
  teacher asked to keep is never lost.
* Page renders and OCR/vision input use the optimized copy; nothing writes
  back into it.

## 4. Report recorded per document

`OptimizationReport.as_dict()` and one `engine.pdf_optimize` log line per
document (role, sizes, ratio, duration, pages, integrity, which copy was used,
lossy flag, skip reason, pixel/ink metrics). No file names, paths or document
contents are logged.

| Field | Meaning |
|---|---|
| `original_bytes` / `optimized_bytes` / `ratio` | measured sizes and ratio |
| `duration_ms` | cold compression time, or cache lookup time |
| `pages_before` / `pages_after` | must be equal for an accepted copy |
| `integrity_ok` | result of `verify_candidate` |
| `used` | `original` \| `optimized` \| `cached` |
| `skipped` | why compression did not run / was rejected |
| `lossy` | whether images were re-encoded |
| `detail` | oversized-image share, images rewritten, build ms, pixel diff, minimum ink ratio |

## 5. Tests

`backend/tests/test_pdf_optimize.py` (21 tests) covers:

* a scanned **student paper** and a scanned **mark scheme** both shrink, with
  page count, geometry, rotation, text layer and ink preserved;
* mathematical notation stays extractable after optimization;
* both roles get separate artifacts from identical bytes;
* an already-lean document keeps its original (0.3 % is not worth an artifact);
* the same bytes are compressed once (second call is a cache hit, zero build
  calls);
* a candidate larger than the original is discarded;
* a rewrite failure (`TimeoutError`) returns the original, never raises;
* corrupt, encrypted and missing files are refused safely;
* an integrity failure falls back and leaves no artifact;
* a faded page fails the ink gate — and would have passed the pixel-difference
  check alone; a blanked page makes the job use the original;
* the **Normal pipeline's staging step** produces optimized copies for the
  student *and* the mark scheme in one job, and a retry reuses the prepared
  manifest;
* retention keeps the **original bytes** after optimization, and the compressed
  submission copy is released at cleanup;
* cache release drops submission copies but keeps shared mark-scheme copies;
* the size cap evicts, and `cache_stats` reports real savings.

The shared mark-scheme coverage and verification tests are unchanged.

At the time of this measurement the whole backend suite reported **2188
passed, 1 skipped** (9 m 33 s). The Large PDF pipeline has since been removed;
see the repository's final removal report for current results.

## 6. Measured results

`scripts/bench_pdf_optimize.py` (reproducible; raw JSON in
`docs/evidence/pdf-compression.json`). Synthetic but representative: 300 dpi
photo-like scans with faint short strokes, dots and tiny mark codes, plus
digitally generated papers with a real text layer. Windows, Python 3.14.

### Student submissions

| document | pages | original | optimized | saved | used | cold | warm |
|---|---:|---:|---:|---:|---|---:|---:|
| scanned 300 dpi | 6 | 1498 KiB | 492 KiB | **67.2 %** | optimized | 3100 ms | 5 ms |
| digital text | 6 | 6 KiB | 2 KiB | **65.5 %** | optimized | 145 ms | 2 ms |
| scanned 150 dpi, already lean | 2 | 189 KiB | 188 KiB | 0.3 % | **original** | 11 ms | 3 ms |

### Mark schemes

| document | pages | original | optimized | saved | used | cold | warm |
|---|---:|---:|---:|---:|---|---:|---:|
| scanned 300 dpi | 3 | 707 KiB | 249 KiB | **64.8 %** | optimized | 1736 ms | 3 ms |
| digital text | 3 | 3 KiB | 1 KiB | **57.5 %** | optimized | 142 ms | 2 ms |
| scanned 150 dpi, already lean | 2 | 170 KiB | 169 KiB | 0.3 % | **original** | 10 ms | 4 ms |

Aggregate over the six documents: submissions 1692 → 682 KiB (**59.7 %**),
mark schemes 880 → 419 KiB (**52.4 %**). Every accepted copy had an identical
text layer and identical page count; worst sampled-page mean difference was
**0.24/255**; measured minimum ink ratio **0.91–1.00** against a 0.75 gate
(0.95 for the student scan, 0.912 for the scheme scan).

### Cost, rendering and "OCR"

| document | text extract (orig → opt) | render 6/3 pages at 150 dpi | ink count |
|---|---|---:|---|
| student scanned 300 dpi | 3.7 → 2.2 ms | 588 → 304 ms | 378 → 413 (+9.3 %) |
| student digital | 2.8 → 2.3 ms | 6.4 → 7.3 ms | 748 → 748 |
| mark scheme scanned | 1.7 → 1.3 ms | 290 → 157 ms | 281 → 295 (+5.0 %) |

The +5–9 % ink count on downsized scans is JPEG/resampling spreading dark strokes
into neighbouring mid-tones; no ink was lost (mean difference 0.24/255, and the
gate would reject a real fade).

Compression cost is paid **once per document per job**: ~516 ms/page for a
300 dpi scan, ~50 ms/page for a text PDF, and 2–5 ms on any retry. Rendering is
roughly **2× faster** on the optimized scan because there is less image data to
decode; text extraction is unchanged (±1 ms).

### Bandwidth and storage

* AI-side upload per student paper: 1.5 MiB → 0.5 MiB (**≈1 MiB less per
  student**, ≈30 MiB for a class of 30); one shared scheme upload of
  0.7 MiB → 0.25 MiB (≈0.45 MiB per assignment, not per student).
* Transient cache/storage per job: ≈1.0 MiB smaller for the scanned pair above.
* Retained originals keep their **original** size by design — retention is a
  legal/teacher promise, so its bytes are not reduced.
* Browser → backend upload is unchanged: the manual upload is stored as
  received, and compression happens at preparation. Reducing that leg would need
  client-side compression (see §8).

## 7. Acceptance criteria

| Criterion | Result |
|---|---|
| Both document types compressed | Yes — one utility, both roles |
| Originals preserved per retention policy | Yes — tested byte-for-byte |
| No repeated compression | Yes — content+settings cache key, 2–5 ms warm |
| Maths readability protected | 200 dpi floor, q85/88, identical text layer, pixel + ink gates |
| Shared reconciliation tests unchanged | Yes |
| Normal corrector intact | 2188 passed, 1 skipped, 0 failed at measurement time |
| Regression tests and real benchmarks | 22 optimizer tests + script/JSON above |

Neither "PDFs got smaller" nor a passing suite alone is the success claim: the
claim rests on measured savings **with** identical page/text evidence.

## 8. Known limits / honest gaps

* Benchmarks use synthetic representative documents, not the school's real
  papers (no student files were read or copied into evidence). Re-run
  `scripts/bench_pdf_optimize.py` on a real pair before quoting numbers.
* There is no local OCR engine: the model reads the page image. "OCR quality" is
  therefore protected indirectly (resolution floor, JPEG quality, render and ink
  verification), not by comparing OCR text before/after.
* No real-AI A/B accuracy run was possible offline (no keys); marking accuracy is
  covered by the scripted-engine marking/coverage suites, which pass.
* A single faint minus sign or decimal point lost inside an otherwise unchanged
  page is below what a global pixel/ink metric can detect. The protection is the
  conservative 200 dpi/quality choice plus the whole-page gates, not per-glyph
  detection.
* Compression adds ~0.5 s/page on the first attempt for a 300 dpi scan. It does
  not make the AI faster; it reduces transfer size and cache/storage use.
* Manual browser uploads still travel at original size; only the downstream
  (model/files) transfer is reduced.
