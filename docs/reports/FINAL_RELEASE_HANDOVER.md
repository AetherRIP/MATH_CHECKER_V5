# Final engineering handover — Math Checker

## Verdict: NOT READY for unrestricted real-world handover

A tested **release candidate** has been built: version **5.2.1**, build
**20261013-rc1**, schema **5**. Local automated checks and portable-package
checks pass, but the required live Classroom/mathematics acceptance and complete
multi-user isolation audit have not been completed. Do not treat the build
script's `RELEASE READY` banner as production certification.

## Source and architecture

- Production root: `E:\MATH_CHECKER_V5`; entry point `main.py` (supervisor/relay
  and FastAPI backend), not any archived/reference application.
- Distribution: `MathChecker_Distribution`; allowlist-first build, privacy scan,
  bundled Windows Python, Inno Setup, manifest and hashes.
- Inventory: `FINAL_RELEASE_CHECKLIST.md` in this directory lists normal and
  developer UI modules/control counts, backend route declarations and critical
  workflow paths. It is a source inventory, NOT a manual click-by-click sign-off.
- Standard marking: job manager/pool → Gemini/key proxy → blueprint/verification
  → result guard → SQLite. Long documents use large_pdf batches and merging.
- Automation: detect/version-dedup → correct → report → canonical Drive attachment
  → grade write/readback → return/readback → cleanup. Existing retry architecture
  and lifecycle busy tracking are retained.

## Concrete changes

### Grading, review and audit safety

- `backend/app/api/scheme_routes.py`: teacher/developer role dependency on all
  global scheme-review routes; actor resolved from server-side identity, not a
  caller-supplied `actor`. Per-discrepancy proposal details and explicit failed-item
  recovery endpoint. Fixed publish callback contract; optional grade return uses
  existing **1–9 assignment/global boundaries**, final-grade/return operation and
  readback of assigned grade AND RETURNED state. No success inferred from PATCH.
- `backend/app/engine/scheme_audit.py`: confidence alone no longer confirms a
  mathematical error; a human decision can confirm it. Per-question notification
  grouping. Supported evidence required for proposed marks; ambiguous cases are
  not staged as automatic changes. Teacher overrides protected before staging
  AND application. Stale question marks/maxima/totals rejected. Approval is read
  and claimed under BEGIN IMMEDIATE; per-item savepoints roll back failures.
  Retry recovers failed local items only. Original and new current versions are
  recorded; assignment grade boundaries are retained. Rejection rechecks state
  under the write lock. Completed review classification survives re-detection.
- `backend/app/engine/verify.py`: a supported answer with a contradictory actual
  mark, missing evidence, or ambiguous decision reaches the existing human-review
  gate instead of silently retaining a confident zero. It does not rewrite marks.
- `backend/app/large_pdf/runner.py`: records discrepancy/version history on the
  long-document save path; preserves teacher overrides and stores a pending
  candidate instead of replacing the teacher's result.
- `backend/app/automation/runner.py`: an assigned grade alone no longer proves
  that work was returned; RETURNED state is required.

### UI, automation and access

- `backend/app/automation/routes.py`, `store.py`: confirmed Clear activity action
  uses a persisted visibility watermark. Stored audit rows, corrections, active
  jobs and retries are not deleted. Assignment statistics default to zero rather
  than blank cells. Automation approval now requires teacher/developer role.
- `backend/app/api/setup_routes.py`: review-mode automation holds appear in Review
  Queue. `frontend/src/pages/ReviewPage.tsx` sends these to Automation delivery
  review instead of pretending a result override is delivery approval.
- `frontend/src/App.tsx`: Review Queue badge uses the SAME review endpoint as the
  page, not the number of unrelated attention alerts (the demonstrated mismatch).
  Multi-School remains visible as WIP; normal users get a cover while implementation
  and developer diagnostics remain intact.
- `frontend/src/pages/AutomationPage.tsx`: working confirmed Clear dialog; a
  configured automatic-return policy no longer claims grades already returned.
- `frontend/src/pages/ResultsPage.tsx`: neutral discrepancy panel separates the
  mathematical recommendation from the actual recorded mark.
- `frontend/src/pages/SchemeReviewPage.tsx`: reloads the existing proposal and offers
  failed-item-only recovery; deliberate approval modal remains mandatory.
- `backend/app/distributed/accounts.py`, `routes.py`, `sync.py`: unassigned limited
  accounts no longer inherit global classes/events/correction results simply
  because their scope set is empty. This fixes specific leaks, not all tenancy.

### Packaging

- `backend/app/version.py`: build identity 20261013-rc1, schema unchanged.
- `MathChecker_Distribution/scripts/build_release.py`: documentation staging excludes
  forbidden binary documents, preserving Markdown instead of weakening the scanner.
- `scripts/verify_portable.py` in the distribution: first-account setup precedes the
  anonymous-access test, restart tries the existing host credential before setup,
  and bounded HTTP polling handles socket timeouts without declaring success.
- Distribution config example documents scheme_audit settings. Recipient guide:
  `MathChecker_Distribution/documentation/RELEASE_HANDOVER.md` (included in artifacts).
- Tests added/updated: test_release_safeguards.py, test_release_automation.py,
  test_scheme_publish_path.py, test_scheme_audit.py, test_scheme_review_api.py.
  Developer-only scripts: backend/scripts/release_ui_check.py and
  backend/scripts/release_update_check.py. No production dependency was added;
  Playwright was installed in the development venv only.

## Verification evidence

| Check | Result | Evidence / precise scope |
|---|---|---|
| Full backend regression | PASS | `docs/logs/FINAL_REGRESSION.txt`: **1,739 passed**, 2 dependency deprecation warnings. Subsequent small approval changes covered by the affected run below. |
| Final affected safety/approval/automation/runtime checks | PASS | `docs/logs/FINAL_AFFECTED_TESTS.txt`: **68 passed**, after final source repairs. |
| Large-PDF/automation/grade workflow targeted run | PASS | `docs/logs/FINAL_WORKFLOW_TESTS.txt`: **135 passed**. External services patched. |
| Frontend typecheck/build | PASS | Final release build runs `tsc -b && vite build`; exit 0 in `docs/logs/FINAL_RELEASE_BUILD.txt`. |
| Actual browser page/size rendering | PASS | `docs/results/final_ui_checks.json`: 19 pages × 6 sizes = **114 checks**, no document horizontal overflow, no JS page errors. Real installed Chrome, isolated demo server. |
| Real browser review badge/Clear/WIP | PASS | Same JSON: review_count_matches=true, clear_dialog_workflow=true, wip_cover=true. Clear uses synthetic activity in isolated demo DB. |
| Portable fresh installation at unrelated path | PASS | `docs/logs/FINAL_PORTABLE_VERIFY.txt`: **38/38**. Actual ZIP extracted into Windows Temp path with spaces/accent/parentheses; own launcher and bundled runtime, account setup/sign-in, anonymous 401, duplicate-launch refusal, synthetic demo correction/report PDF, import/export, restart persistence, no orphan processes. |
| Portable update scenario | PASS | `docs/logs/FINAL_UPDATE_VERIFY.txt`: **5/5**, archived RC package → current RC application files; preserved Data account and synthetic student, quick_check=ok, new build served, clean stop. This is an RC-to-RC portable update, not every historical schema upgrade. |
| Artifact hashes/source freshness | PASS | Both artifact SHA-256 values matched manifest; critical scheme/verification/large-PDF sources byte-matched ZIP. |
| Privacy scanner and runtime imports | PASS | Build scanner clean: **3,832 staged files**; 9 compiled-module checks; actual bundled-runtime imports. |
| Production backup integrity | PASS | Online SQLite backup `PRAGMA quick_check=ok`. |
| Live Google Classroom/OAuth/provider quota | BLOCKED | No designated class/account authorization for this pass; no real grades changed. Patched transport tests are not live certification. |
| Independently measured live mathematical accuracy | BLOCKED | Synthetic precomputed model outputs do not establish live AI accuracy. No second independent mathematical verification service/proof engine was added. |
| Every button/state/long-content/accessibility | NOT TESTED | Page rendering plus specific interaction tests only; no exhaustive control/keyboard/contrast matrix. |
| Installer execution, fresh and update | NOT TESTED | Inno Setup compiled a real installer; installation itself was not executed. |
| Different physical clean machine | NOT TESTED | Separate path and bundled runtime on same Windows host; not a new OS/VM. |
| Complete assistant/tenant isolation | FAIL / unresolved release risk | Legacy main-app endpoints are not uniformly class-scoped. WIP cover does not provide server-side tenancy isolation. |

### Failures encountered and repaired

- Initial scanner blocked a documentation PDF. Repaired staging; final scanner clean.
- Initial portable verifier: 35/38. Corrected outdated first-run/restart assumptions
  and missing reference settings; final 38/38. Authentication was not weakened.
- A developer Clear test hung because its first implementation nested a write
  helper under the transaction lock. Fixed by using that transaction's connection;
  regression and real browser Clear passed.
- First update harness failed on a socket timeout during readiness. Polling now
  treats the timeout as not-ready within its deadline; final update 5/5 passed.
- All failures are recorded, not silently reclassified as passes.

## Distribution deliverables

Folder: `MathChecker_Distribution/release/`

- `MathChecker_Portable.zip`: **76,328,811 bytes**.
- `MathChecker_Setup.exe`: **53,381,749 bytes**.
- `SHA256SUMS.txt`, `release_manifest.json`: authoritative hashes/build metadata.
- `MathChecker.iss`: generated build recipe, not an installation success claim.

Previous artifacts were archived under `MathChecker_Distribution/previous_builds/`
before clean builds; the builder retains two generated archives plus the labelled
original history. Staging/portable trees have been regenerated intentionally.
No commit, push or deployment was performed.

## Data / migration / recovery notes

- Private backup: `dev_local/release_audit_backup/` includes online SQLite backup,
  configuration and machine state snapshot. Never ship it. No production DB
  migration or test reset was performed. Schema remains 5; no new columns needed.
- Keep the user's Data directory (portable) or %LOCALAPPDATA%\MathChecker
  (installed) on update. Stop before copying; back up first. Recovery: restore
  the backed-up data to an isolated copy first and verify before replacing live
  data. Do not downgrade into a newer incompatible schema.
- Runtime `agent/agent_state.json` was already dirty at the start of this pass;
  not reset or staged. Tests use temporary MC5_DATA_DIR values, not the user's DB.
- Batch savepoints ensure failure cannot leave one item half-updated. Original
  results and approved current values are versioned. A stale item fails rather
  than overwriting a later teacher decision.

## Remaining limitations the receiving assistant must know

1. **Do not approve production deployment/handover based on these local tests.**
   Obtain a designated test class, own OAuth client, quota and teacher consent.
2. Bulk local grade recovery is implemented, but failed external bulk publication
   has no dedicated per-item publish retry UI. Inspect remote state before a
   write retry. Classroom operations and local correction are separate outcomes.
3. Bulk grade changes do not automatically regenerate existing report attachments;
   deliberate report refresh/attachment verification remains necessary. Do not
   share a stale report after approving new marks.
4. Core marking still relies on the existing AI reasoning pass. A meaningful
   discrepancy is recorded and conflicts gated; no independent proof guarantees
   every claimed answer. Required method/format correctness needs live fixture
   acceptance, not just prompt-string assertions.
5. All-role/main-API scoping is incomplete. Keep Multi-School WIP and do not use
   a shared multi-school server with real student data until that audit finishes.
6. Student-answer/evidence history can be inspected through stored result versions,
   but discrepancy rows still aggregate/update per question/type; they are not a
   separate immutable per-student evidence archive.
7. Browser checks establish rendering, not exhaustive usability or accessibility.

## Exact operator handover

1. Verify SHA256SUMS against the selected ZIP/EXE and read the included
   documentation/RELEASE_HANDOVER.md. Use the RC only in an acceptance environment.
2. Extract the full ZIP to a writable folder; run MathChecker.bat. Follow first-time
   setup, create the host account/password, then sign in. Never expose bootstrap
   setup publicly. Stop.bat stops this copy; Data stays beside it.
3. AI → API & providers: enter own keys. Settings/setup: provide own Classroom
   OAuth client configuration. Google Classroom → Connect → sync a designated
   synthetic test class only. Do not reuse developer credentials or raw Data.
4. Assignments: upload original mark scheme; Correct a PDF for manual work or
   correct eligible assignment submissions. Watch Task Manager and Results.
5. Keep Automation review mode ON initially. Review Queue handles marking review;
   review-mode holds link to Automation for delivery approval. Review badge now
   counts actual review records, not every attention notification.
6. Mark-Scheme Review: inspect expected/student/independent reasoning, prepare
   proposal, inspect affected count and original/proposed marks. Teacher/developer
   deliberate confirmation is required for this batch. Reject does not change grades.
7. Retry failed local items only if the stored proposal is still valid. For external
   failures, inspect real Classroom grade/return and attachment state first. Retry
   the failed report/grade stage, not the entire correction.
8. Verify actual assigned grade, returned state, app-owned current report, absence
   of duplicate reports and untouched unrelated attachments in the test class.
   Record failures separately. Only then seek a production release decision.

## Final acceptance checklist

- PASS: non-destructive backup, existing data/configuration preservation.
- PASS: explicit authorized scheme-batch approval, rejection, duplicate prevention,
  stale/override guards, per-item rollback/recovery, original/current audit versions.
- PASS (synthetic/local scope): ordinary marking regression, mark ceilings and
  grade 1–9 conversion; provider retry/cooldown, lifecycle and report regressions.
- PASS: review-count cause fixed; Clear hides history without deleting records.
- PASS: Multi-School visible WIP; code/models preserved, selected scope leaks fixed.
- PASS: 114 page/viewport rendering checks and selected real interactions.
- PASS: final build/privacy scan/runtime, portable fresh + RC update, hashes.
- FAIL: complete multi-school/main-API authorization not established.
- BLOCKED: live mathematical marking and authorized Classroom end-to-end test.
- NOT TESTED: exhaustive controls/accessibility, clean-machine VM, installer execution.
- NOT READY: complete real-world release acceptance; receiving assistant must close
  the blockers above rather than infer success from launch/build/test counts.
