# Live Classroom testing report — real student work, read-only

**Run:** 2026-10-16, 18:14–18:30 (local)
**Harness:** `MathChecker_Distribution/scripts/verify_live_classroom.py`
**Evidence:** [`docs/results/LIVE_CLASSROOM_CERTIFICATION.json`](../results/LIVE_CLASSROOM_CERTIFICATION.json) · console log `docs/logs/verify_live_classroom.txt` · app log `docs/logs/live_classroom_app.log`
**Outcome:** 26 of 28 checks passed. **Exit code 4 — cross-checked, NOT certified.** No grade, report or file of any kind was published.

Two things must not be blurred together in this report:

1. **The safety result is clean.** Every check about *what could leave the machine* passed, and two of them are proofs rather than assertions.
2. **The accuracy result is not a pass.** There were no human-verified marks to compare against, so the marks below are *cross-checked*, not *certified*. One genuine marking instability was found and is in the review queue.

---

## 1. What was verified before a single student byte was fetched

The order matters: the barrier was proven **first**, and the run would have stopped at exit 3 without fetching anything if it had failed.

| Check | Evidence |
| --- | --- |
| Static audit — every mutating Classroom/Drive call is behind the guard | Only 3 raw HTTP writes exist in `classroom/client.py` (`upload_report_to_drive`, `replace_drive_file_content`, `delete_drive_file`); all three are `@_guarded_write`. Every other verb (draft grade, final grade, `:return`, share, `modifyAttachments`) passes through `classroom_request`, which refuses non-GET. Identical counts in `portable/` and `release_staging/`, so the packaged build is not a hole. |
| A new bypass now breaks the build | `backend/tests/test_testing_only_guard.py::TestNoWritePathCanBypassTheGuard` parses the source with `ast` and fails if any raw write is not decorated, if a write appears in a *new* module, or if a `requests.Session` is introduced. The detector was itself tested against a known-unguarded sample file. |
| The guard tests actually bite | Neutering `assert_publication_allowed`/`blocked_reason` turns **17 tests red**, including every new runtime test. They are not vacuous. |
| The app reads the barrier as ON | Guard probe (`testing_only: True`) *and* the running app's own `GET /api/settings` (`testing: {only: true}`), `mock_mode: false`. |
| **Zero writes can leave, even by accident** | A child process loaded the app's own guard against this run's config, rigged the HTTP transport, and drove 8 publishing helpers — draft grade, final grade, `:return`, report upload, report replace, share, `modifyAttachments`, report delete. **All 8 refused; recorded transport writes: 0.** |
| The gate is a switch, not a wall | With testing-only off, `assert_publication_allowed` does not raise; a real read (`GET /api/classroom/sync/start`) still succeeded during the run. |

**Defence in depth** in the throwaway config: `[testing] only = true`, plus `automation.enabled = false`, `attach_report = false`, `return_work = false`, `draft_only = true`.

### The publish path, through the app's own API, aimed at a real submission

The harness asked the app to publish a draft grade onto a **real turned-in submission in your real O-Level class**, with `attempt_unmanaged: true` so that "this assignment isn't app-managed anyway" could not be the thing that stopped it:

```
POST /api/classroom/assignments/891651209745/publish
  -> HTTP 200, 1 operation, 0 published
  -> "TESTING ONLY: 'PATCH https://classroom.googleapis.com/v1/courses/877926720424/
      courseWork/891651209745/studentSubmissions/Cg4IweGmoIYFEJH0ttT5GQ' was blocked
      and nothing was sent to Google Classroom or Drive."
```

The app's own log records the same event: `WARNING testing_guard: testing-only: blocked PATCH https://classroom.googleapis.com/...`.

---

## 2. What was marked (real papers, live model)

Read-only inventory: **2 courses, 19 assignments, 13 with both turned-in work and a stored mark scheme** (78 submissions each, 1–57 turned in). Three submissions were selected (bounded by `--max-total 3`); two produced a usable paper.

| Student (opaque) | Assignment | Model | Marks | Questions | Repeatability |
| --- | --- | --- | --- | --- | --- |
| `S-2113d5c0` | cw `889658133898` | `gemini-3.5-flash-lite` | **47/50 (94%)**, grade 9 | 19 | ✗ **1 of 19 differed — Q9: 0 then 2** |
| `S-fadedaa9` | cw `826520400348` | `gemini-3.5-flash-lite` | **137/139 (98.6%)**, grade 9 | 41 | ✓ 0 of 41 differed |

Partial credit was awarded where a student lost a mark (Q5 2 marks; Q6, Q7 2 marks each). Both results carry a deterministic verification report; **both were returned with `confidence: LOW` and flagged `review_required` by the app itself.**

### Findings that need your judgement

1. **A genuine marking instability.** On `S-2113d5c0`, two independent corrections of the *same* paper returned different marks for Q9 (0 then 2) — total 47 vs 49. A 1-in-19 disagreement on a real paper is exactly the kind of thing fixture testing cannot surface. **This alone means the marker is not yet safe to run unpublished.**
2. **The high-score double-check did not complete.** On `S-fadedaa9` (98.6%, above the 95% threshold) the app correctly triggered its independent second correction — and it failed: the app log shows `api failure classified`, `job parked for API capacity`, `key entered cooldown`. The row was recorded as `DOUBLE_CHECK_FAILED` with the first result preserved and review required. The behaviour is right (it refused to claim corroboration it did not have); the cause is Gemini capacity, so **a high score can go uncorroborated when quota is tight.**
3. **One submission could not be used.** `S-6b7fe3d1`'s only attachment is not a Drive file (a form/link attachment), so there was nothing to download. Reported as a coverage gap, not a failure of the marker.

**Review queue** — 3 entries in the evidence JSON: the Q9 disagreement, and the two app-flagged `review_required` results.

---

## 3. Proof that nothing changed on Google's side

| Statement | Evidence |
| --- | --- |
| No grades were uploaded | Zero-write guard probe; the publish probe returned 0 published of 1 with the guard's own message; no other write path was exercised. |
| No reports were uploaded or attached | Report-upload probe refused before any Drive call; **no Drive file was created by this run** — a read-only listing of app-stamped temporary reports found 200 pre-existing files and **0 created during the run window**. |
| No submission was modified | All **4** touched submissions (3 candidates + the publish-probe target) were re-read from Google after the run and compared hash-for-hash (state, draft grade, assigned grade, late flag, attachments, history): **`changed: []`**. |
| No classroom record was deleted | Nothing in this run issues a delete. The harness only GETs. |
| Your real installation was not touched | `data/config.toml`, `data/secrets.toml` and `data/app.db` unchanged (size + mtime compared before/after). The whole run used an isolated throwaway data directory with a fresh database. |

---

## 4. Cleanup

**Deleted (created by this run):** the throwaway data directory — config copy, the copied Classroom refresh token and Gemini keys, the OAuth client copy, downloaded student PDFs, generated reports and the throwaway database. Verified gone; no `--serve` child process and no listener left behind.

**Left untouched and reported for your attention:** `data/audit_downloads/`, `data/audit_reports/`, `data/audit_inventory.json`, `data/app.db`, `data/config.toml`, `data/secrets.toml` — pre-existing, not provably this run's.

**For manual cleanup by you:** the read-only listing found **200 Drive files stamped as temporary correction reports** dating back to 2026-10-08, all created by earlier sessions. They are not this run's, so they were not deleted. They may be worth sweeping with the app's own report-cleanup tool.

---

## 5. Limitations — stated plainly

- **Accuracy is not certified.** Cross-checking and repeatability are not ground truth. Certifying needs human-verified per-question marks (`--expected`, the mechanism `verify_live_marking.py` already uses); this harness deliberately does not fake one.
- **The report-upload probe did not reach the guard.** `POST /api/results/{id}/retry {"action":"upload"}` answered `400 no_coursework` because a paper uploaded manually is not linked to a Classroom assignment — so it proved *refusal before any Drive call*, not the guard specifically. The guard itself is proven by the zero-write probe (which drives `upload_report_to_drive` and `replace_drive_file_content` directly) and by the publish probe. Closing this gap means marking through the app's Classroom pipeline (`/classroom/.../correct`) on an allowlisted assignment.
- **No A/B against the original app.** `compare_original_vs_v5.py` needs the friend's original implementation, which is no longer in the repository, so the engine cross-check was not run.
- **Small sample.** Two papers in one sitting. One repeat disagreement is a signal, not a rate.
- **Cost/time.** Each live correction took 100–400 s. The run is bounded by `--max-total`; the first attempt queued 24 papers because the limit was per-assignment, which is why that flag exists.
- **Scope of the claim.** The zero-write proof covers *this* app's Classroom/Drive paths at *this* commit. The app's other `urllib` egress (tunnel, update checker, distributed agent heartbeat) does not touch Classroom or a student record and is out of scope.

---

## 6. Recommended next steps

1. **Treat finding 1 as blocking.** Give the marker human-verified marks on a handful of real papers and drive down the Q9-class instability before any unpublished use — this is the difference between "plausible" and "trustworthy".
2. **Investigate the unanswered double-check.** `DOUBLE_CHECK_FAILED` on a 98.6% paper means the corroboration layer silently depends on spare Gemini capacity.
3. **Close the upload-probe gap** by running the same certification through the Classroom pipeline on an allowlisted assignment, which also exercises sync → download → mark → store end to end.
4. **Sweep the 200 stale temporary Drive reports.**
