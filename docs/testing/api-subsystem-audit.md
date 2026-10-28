# API subsystem audit and repair — Math Checker V5

Date: 2026-10-27 · Branch: `main` · Scope: the whole API sub-system (key
testing, key lifecycle, provider error classification, key persistence,
rotation, backend endpoints, and the desktop "AI & keys" page), with the
**"Test all" button** investigated first because it was the reported failure.

Every number below comes from a command that was actually run. Checks that
could not be run are reported as **NOT VERIFIED / BLOCKED** with the reason —
never as a pass.

---

## A. Summary

**The API sub-system is healthy; the "Test all" button was not.** The backend
was already doing the right thing and saying so clearly over its SSE stream —
the desktop page simply misread it.

The button's four real defects, all reproduced with executing tests and all
fixed:

| # | Sev | Defect | User-visible effect |
|---|---|---|---|
| **D1** | **P1** | "Test all" treated **every stream event as a key result** — `start`, every `key_stage` progress event, and the final `done` all became result rows — and reported the **event count as the number of keys**. The backend's summary was never read. | Two keys produced ten rows (most reading "— · auth refused · generation failed" for keys that were fine) and the page announced **"Finished testing 10 keys against the live API."** No pass/fail/skipped breakdown, no timings, no reasons. |
| **D2** | **P2** | A cancelled or timed-out run left **"Testing each key against the live API…"** on screen forever. The budget was a flat 2 minutes, although one probe may legitimately take ~60 s. | The page looked stuck. Slow runs were killed mid-way with no explanation. |
| **D3** | **P2** | **The key list was never refreshed after any action on the page.** `LoadAsync()` returns immediately while a page is busy (`if (IsBusy) return;`), so every `await LoadAsync()` inside a command was a no-op. | After a test, `Add`, `Enable`, `Disable` or `Remove`, the health counters and key statuses still showed the state from *before* the change until Refresh was pressed. |
| **D4** | **P2** | Result rows were **silently dropped** when no WPF dispatcher was attached, and queued to `Application.Current`'s dispatcher when the page ran on any other thread (a dead queue). The page then said **"No keys were tested: add a key first."** | Results vanish and the page tells the user nothing was tested, for a run that tested everything. Observed as a real failure in the full test run. |
| **D5** | **P3** | Every command property built a **new command instance on each read**. | A re-bound page could be handed a second "Test all" command while the first stream was still running — two runs competing for one page's state. The rest of the app asserts stable instances (CorrectionPageTests); this page did not. |

The backend's own contract has **no** defect: 9 new contract tests for
`GET /api/keys/test/stream` and `POST /api/keys/{fp}/test` pass unchanged
against the shipped server code.

---

## B. Defects discovered

### D1 — "Test all" counted events, not keys (P1)

**Reproduction (live).** With two keys configured, the real server emitted
**10 events** — verified by streaming it:

```
data: {"type": "start", "total": 2}
data: {"type": "key_stage", ... "stage": "auth" ...}          x3 for key 1
data: {"type": "key_result", ... "status": "HEALTHY", "summary": {"tested": 1, ...}}
data: {"type": "key_stage", ...}                              x3 for key 2
data: {"type": "key_result", ... "summary": {"tested": 2, ...}}
data: {"type": "done", "summary": {"total": 2, "tested": 2, "passed": 2, "failed": 0, "skipped": 0, "elapsed_s": 0.01}}
```

sequence: `start, key_stage×6, key_result×2, done` — **10 events for 2 keys**.

**Root cause.** `AiKeysViewModel.TestAllCommand` passed each SSE element
straight into a row without ever looking at its `type`, and then used
`ResultFacts.Count` as the number of keys tested:

```csharp
var status = PayloadFacts.StatusLabel(Text(element, "status"));   // absent on key_stage → "—"
var auth = element.TryGetProperty("auth_ok", out var a) && a.ValueKind == True;   // absent → false
...
ShowResult($"Finished testing {ResultFacts.Count} keys against the live API.");   // events, not keys
```

Stage events carry no `status`/`auth_ok`/`generate_ok`, so `StatusLabel(null)`
rendered "—" and the missing flags rendered "refused"/"failed": four to five
misleading rows per key, and a key count that was actually an event count. The
`done` event's `summary` (total/tested/passed/failed/skipped/elapsed_s) was
never read.

**Impact.** The one button whose job is to tell a teacher whether their keys
work produced a pile of false failures and a wrong count. A user with two good
keys was told ten keys were tested, most of them apparently broken.

### D2 — a cancelled or slow run left the page "testing" (P2)

`TestingAll` was reset in a `finally`, but the note stayed
`"Testing each key against the live API…"` and the cancellation returned through
`AsyncCommand` untouched. The budget was `TimeSpan.FromMinutes(2)` regardless of
how many keys were configured, while a single probe is `models.list` **plus** a
generation, each clamped to 30 s in `gemini/tester.py` — so a three-key list
could be cut off part-way with no message. Reproduced by the
`Test_all_reports_a_cancelled_run_instead_of_staying_in_progress` test on the
shipped code (it asserted the note mentions the cancellation and failed).

### D3 — the page never reloaded after an action (P2)

`PageViewModel.RunAsync` owns the busy window and every command runs inside it:

```csharp
if (IsBusy) { return; }      // LoadAsync, verbatim
```

So `await LoadAsync()` at the end of `AddKeysCommand`, `TestSelectedCommand`,
`TestAllCommand`, `EnableCommand`, `DisableCommand` and `RemoveCommand` did
nothing at all. Reproduced: the refresh test timed out after 19 s waiting for a
second `GET /api/keys` that never came.

### D4 — rows dropped without a dispatcher (P2)

Rows were only written inside `if (dispatcher is null) { return; }` where
`dispatcher = Application.Current?.Dispatcher`. Two failure modes:

1. **No application attached** (headless, or a host that has not created one):
   every row is dropped, and the completion line falls back to
   `"No keys were tested: add a key first."` — a statement that is simply false.
2. **An application exists on another thread** — exactly what happened when the
   whole desktop suite ran: rows were queued with `BeginInvoke` into a dispatcher
   nothing was pumping, so they never appeared. That is how this was found: the
   test passed when run alone and failed inside the full suite.

### D5 — commands were rebuilt on every read (P3)

```csharp
public AsyncCommand TestAllCommand => Command(async () => { ... });   // a new instance per read
```

`AsyncCommand` does guard re-entrancy (`_running` + `CanExecute`), so a single
bound instance is safe — but the instance is not stable, so a re-bind mid-run
yields a *fresh* command that will happily start a second stream against the same
`_testingAll`/`_testCts` state. The project's own convention disagrees:
`CorrectionPageTests.The_submit_command_is_one_stable_instance`. Reproduced by
`The_commands_are_stable_instances` on the shipped code (`Assert.Same` failed).

---

## C. Fixes implemented

| File | Change | Regression test |
|---|---|---|
| [AiKeysViewModel.cs](../desktop/src/MathChecker.Desktop/ViewModels/AiKeysViewModel.cs) | **D1** Event-type dispatch (`start`/`key_stage`/`key_result`/`done`); one row per key, updated live from stage events; the backend's summary drives the completion line ("Tested 2 keys in 3.4 s — 1 healthy · 1 failed · 0 skipped" plus each failure's reason). **D2** `OperationCanceledException` is caught and reported ("Test cancelled after N of M keys — the rows above are the results that had arrived"); budget is 90 s per key, capped at 9 minutes so the page reports the outcome *before* the client's 10-minute transport timeout. **D4** Rows are applied on the `SynchronizationContext` that started the run (direct when already there), so nothing is dropped and nothing is queued to a dead dispatcher. **D5** Commands are cached instances. **D3** Mutating commands pass `after: LoadAsync`. | [AiKeysTestAllTests.cs](../desktop/tests/MathChecker.Desktop.Tests/AiKeysTestAllTests.cs) (5 tests) |
| [PageViewModel.cs](../desktop/src/MathChecker.Desktop/ViewModels/PageViewModel.cs) | **D3** `RunAsync`/`Command` take an optional `after` hook that runs once the busy window has closed — the mechanism that makes a post-action reload possible at all. | `Test_all_refreshes_the_key_list_after_the_run` |
| [AiKeysView.xaml](../desktop/src/MathChecker.Desktop/Views/AiKeysView.xaml) | "Test all" is disabled while a run is in flight (`IsEnabled="{Binding CanTestAll}"`) and explains itself in a tooltip. | `Test_all_ignores_a_second_click_while_running` |
| [test_keys_test_all_stream.py](../backend/tests/test_keys_test_all_stream.py) | **New** (9 tests): the SSE contract the UI is written against — event order, per-event fields, summary arithmetic (`passed + failed + skipped == tested == total`), stage-before-result per key, no key material in the stream, a rate limit / local error never invalidating a key, a real rejection still sticking, empty configuration, early disconnect, and the single-key endpoint's fields + 404. | itself |

The view model was rewritten in place, not redesigned: `AddKeys`,
`TestSelected`, `Enable`, `Disable`, `Remove`, `ResizeWorkers`, `LoadCoreAsync`
and the DTO handling keep their behaviour (verified by the 280 pre-existing
desktop tests, of which the AI-page ones still pass).

---

## D. Test results

| Suite / command | Result | Exit |
|---|---|---|
| `desktop: dotnet test …` **baseline, before any change** | **280 passed, 0 failed, 0 skipped** (2m22s) | 0 |
| `desktop: dotnet test …` **after the fix** | **285 passed, 0 failed, 0 skipped** (2m43s) | 0 |
| `desktop: dotnet test --filter AiKeysTestAllTests` (targeted) | 5 passed (20 s) | 0 |
| Red-green: same 5 tests against the **committed** view model | **4 failed, 1 passed** — see below | 1 |
| `backend: pytest tests/test_keys_test_all_stream.py` | **9 passed** | 0 |
| `backend: pytest tests -q -p no:randomly` (full suite) | **2338 passed, 1 skipped** (11m06s) at the 7-test revision | 0 |
| `backend: pytest tests -q -p no:randomly` (final revision, 9-test file) | **2340 passed, 1 skipped** (10m40s) | 0 |
| `frontend: npm run build` (re-run at the final revision) | built in 2.67 s | 0 |
| `frontend: node --test "tests/*.test.ts"` (untouched by this mission) | 84 passed, 0 failed, 3 skipped | 0 |
| Live server: `GET /api/keys/test/stream` (2 keys, demo mode) | 10 events, 0 occurrences of key material | — |
| Live provider probe: one deliberately invalid credential, non-mock | `INVALID` / `invalid_key`, stages `connecting, auth, done` | — |

**Exact red-green evidence** (the same 5 tests, committed view model restored):

```
Failed  Test_all_shows_one_row_per_key_and_the_backend_summary
        Assert.Equal() Failure: Expected: 2, Actual: 0
Failed  Test_all_reports_a_cancelled_run_instead_of_staying_in_progress
        Assert.Single() Failure: The collection was empty
Failed  Test_all_refreshes_the_key_list_after_the_run
        timed out waiting for the page to re-read the key list after the run   (19 s)
Failed  The_commands_are_stable_instances
        Assert.Same() Failure: Values are not the same instance
Passed  Test_all_ignores_a_second_click_while_running          <- a guard: true before and after
```

Then, with the fix restored, all five pass and the full desktop suite is green.

**BLOCKED / NOT VERIFIED (stated plainly):**

- **No valid credential was tested against the provider.** No real Gemini key
  is configured, and inventing one is not a test. `models.list` + generation
  with a *working* key, real 429/quota responses, and real model-availability
  answers are therefore **NOT VERIFIED**; classification for those cases is
  covered by the mocked suites listed in section F.
- **The button was not clicked in a running window.** The desktop UI is verified
  through the real `AiKeysViewModel`, the real streaming client (`SseParser` +
  `MathCheckerApiClient`) and a **real WPF dispatcher pumping the continuations**
  on an isolated STA thread, with the SSE body taken byte-for-byte from the live
  server. That is a view-model-level verification, not a mouse-click in a rendered
  window; the interaction between the click, `IsEnabled` and the command is
  compile-verified and asserted at the property level only.
- The `SKIPPED` status is unreachable through the batch in practice: the stream's
  key list already filters blank entries, so `skipped` is always 0. The
  arithmetic that reports it is nevertheless pinned by tests.

---

## E. "Test all" verification

The stubbed stream in the desktop test is the real wire format, with a **mixed
outcome** — key 1 healthy, key 2 rate-limited — which is exactly the case the old
page got wrong. After the fix the page shows:

```
ResultNote:  Tested 2 keys in 3.4 s — 1 healthy · 1 failed · 0 skipped.
             KEY_2: Gemini rate/quota limit hit. The key was temporarily removed
             from rotation; work continues on another available key.
Status:      Key test finished — 1 healthy, 1 failed.

Fact rows:
  KEY_1   Healthy · auth accepted · generation working · 123 ms
  KEY_2   Cooldown · auth accepted · generation failed · Rate limited ·
          Gemini rate/quota limit hit. …
```

and, while a run is in flight:
`Testing 2 key(s) against the live API…` with the in-progress key's row showing
the current stage message (`Connecting…`, then the auth result, then the probe).

Against the live server the same run produces a faithful summary because the
backend summary is now the source of truth:
`{"total":2,"tested":2,"passed":2,"failed":0,"skipped":0,"elapsed_s":0.01}` →
*"Tested 2 keys in 0 s — 2 healthy · 0 failed · 0 skipped."*

---

## F. API key reliability

**Was a working key being reported invalid? In the code paths exercised here: no.**
The classification rules are correct and — more importantly — *tested*:

- Only an explicit credential rejection yields `invalid_key`. Rate limits,
  quota exhaustion, timeouts, network faults, malformed responses, unsupported
  models and unfamiliar provider messages each get their own category and can
  never be persisted as "this credential is dead"
  (`test_key_validation_regressions.py`: `test_provider_rejecting_the_credential_is_invalid`,
  `test_a_leaked_key_names_invalid`, `test_quota_exhausted`, `test_rate_limited`,
  `test_temporary_provider_failure`, `test_timeout_in_every_spelling`,
  `test_network_failure_is_a_connection_error`,
  `test_malformed_response_is_a_validation_error`,
  `test_an_unfamiliar_provider_message_is_unknown_never_invalid`).
- A failure that is *local* (the SDK client could not even be built) is stored as
  `local_client_error` and leaves the stored status untouched — the historical
  cause of the "working key struck off" complaint.
- Stored status survives restarts and key edits:
  `test_restart_does_not_invalidate_previously_valid_keys`,
  `test_a_removed_and_restored_working_key_stays_healthy`,
  `test_a_rejected_key_still_comes_back_invalid`,
  `test_a_stored_key_is_used_without_modification`,
  `test_stats_persist_across_manager_restart`.
- Rotation, cooldowns and isolation are covered:
  `test_round_robin_over_healthy_keys`,
  `test_rate_limited_key_enters_cooldown_then_recovers`,
  `test_one_invalid_key_leaves_the_other_usable`,
  `test_one_failing_key_does_not_block_the_others`,
  `test_an_unsupported_model_does_not_corrupt_the_key_status`,
  `test_concurrent_workers_never_share`.
- New in this audit: a **rate limit or local error recorded by a Test All run
  persists nothing as invalid** (`test_a_rate_limited_or_local_error_does_not_invalidate_the_key`),
  while a genuine rejection *does* stick
  (`test_an_invalid_credential_is_still_recorded_as_invalid`).

The one thing that *did* make a working key look broken was D1: the desktop
fabricated "auth refused · generation failed" rows for keys that were fine.
That was a reporting defect, not a credential-classification defect — but it is
indistinguishable to a teacher, which is why it is filed as P1.

**Remaining uncertainty:** no live call with a valid key was made (no key is
configured), so real-world quota/429/5xx behaviour against Google is unverified;
only cryptographic/HTTP verification of a *rejected* credential was performed
live.

---

## G. Security and performance

**Credentials**

- The stream carries key **index + fingerprint only**; the live capture contains
  **0** occurrences of `AIza`. Pinned by
  `test_the_stream_never_carries_key_material` and the pre-existing
  `test_status_never_contains_key_material` / `test_status_never_contains_secret`.
- Provider errors are converted to **canned sentences** by
  `classify_gemini_error` — the raw exception text is never forwarded, so a
  provider message echoing the credential cannot leak through
  `error_message`. Stored stats pass through `_safe_error_text` (`redact()`).
- The single-key endpoint's 404 for an unknown fingerprint does not disclose
  whether the value existed. No key material is returned by any tested endpoint.
- Nothing was added to logs; the new code logs nothing at all.

**Performance and resources**

- One test run is **sequential** by design (`await asyncio.to_thread(run_one)`
  per key) — bounded concurrency, no parallel fan-out, no retry loop.
- The page cancels the run when the user navigates away (`OnDeactivated`).
- The run budget is bounded twice over: 90 s per key, capped at 9 minutes, inside
  the client's 10-minute `HttpClient.Timeout`.
- `ResultFacts` holds at most one row per key (previously one per event), so the
  bound collection no longer grows with stage chatter.
- No new dependency; no polling added.

---

## H. Remaining issues

1. **The live job stream dies at the client's 10-minute timeout.** `/api/jobs/stream`
   is an open-ended SSE stream and `MathCheckerApiClient` sets
   `_http.Timeout = TimeSpan.FromMinutes(10)`, which applies to the whole
   response. After 10 minutes the stream is aborted; `JobsViewModel` logs it and
   sets `LiveMode = "polling (stream unavailable)"` but does **not** reconnect
   until the page is revisited. Data stays correct (the 10 s poll timer keeps the
   list current), so the user is not misled — but "live stream" is silently
   downgraded. **Not fixed**: the correct change (`HttpClient.Timeout =
   Timeout.InfiniteTimeSpan` plus per-request timeouts) affects every request in
   the app and is a larger, riskier change than this mission's scope.
2. **Valid-credential provider behaviour is unverified** (section F) — needs one
   real key and the owner's consent to spend a fraction of a request.
3. **Desktop UI verification is view-model level** (section D). A mouse-click
   test of the button in a rendered window, and the packaged installer's AI page,
   were not performed.
4. **Pre-existing, unchanged:** three web-frontend live-UI tests skip/fail
   without the `r-ui-test` fixture (documented in
   `docs/post-audit-validation-report.md` §F1); the two correction endpoints
   still accept different maximum upload sizes (50 MB vs 200 MB) with no 413 test.
5. **Version skew (informational):** the development venv has `google-genai`
   **2.33.0** while the packaged runtime in `desktop/artifacts/` bundles
   **2.29.0**. Both were exercised only for the import path here; the provider
   SDK used by a real correction is the bundled one.
6. **Environment note:** the .NET SDK was **not installed** on this machine at the
   start of this mission, so no desktop test could run at all. It was installed
   user-locally for this audit (`%USERPROFILE%\.dotnet-sdk`, SDK **10.0.403**) —
   no project file, setting or PATH was changed. Without it, none of the desktop
   evidence in this report would exist.

---

## I. Git status

No commit, push, reset or cleanup was performed; nothing was staged. The
pre-existing modifications in the working tree (46 files at the start, unrelated
to this mission) are untouched.

Changed/added by this mission:

```
 M desktop/src/MathChecker.Desktop/ViewModels/AiKeysViewModel.cs
 M desktop/src/MathChecker.Desktop/ViewModels/PageViewModel.cs
 M desktop/src/MathChecker.Desktop/Views/AiKeysView.xaml
?? desktop/tests/MathChecker.Desktop.Tests/AiKeysTestAllTests.cs   (new, 5 tests)
?? backend/tests/test_keys_test_all_stream.py                     (new, 9 tests)
?? docs/testing/api-subsystem-audit.md                            (new, this report)
```

Diffstat for the three modified files: **357 insertions, 54 deletions**
(`AiKeysViewModel.cs` 386 changed lines including comments, `PageViewModel.cs`
22, `AiKeysView.xaml` 3).

**Recommendation.** The API sub-system is fit for use: the stream, the
classification rules and the key lifecycle are correct and covered, and the
"Test all" button now reports what actually happened — one row per key, the
backend's own totals, honest cancellation, and a key list that reflects the
change. Before calling it fully verified, one real key should be exercised once
(items H2) and the button clicked once in the running window (H3).
