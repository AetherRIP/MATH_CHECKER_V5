# docs/ — developer guides, reports, evidence and logs

Start with [WINDOWS_SETUP.md](WINDOWS_SETUP.md) to install, restore or repair a
Windows checkout (prerequisites, dependency inventory, troubleshooting),
[ARCHITECTURE.md](ARCHITECTURE.md) for the current component map,
[PROJECT_STRUCTURE.md](PROJECT_STRUCTURE.md) for the repository inventory,
[development-environments.md](development-environments.md) for which backend the
desktop, the browser dev server and a test snapshot use,
[MARK_SCHEME_SUBQUESTIONS.md](MARK_SCHEME_SUBQUESTIONS.md) for how mark-scheme
subquestions are detected and mapped, [SERVER_LIFECYCLE.md](SERVER_LIFECYCLE.md)
for startup/supervision details, and
[API_QUOTA_AND_RETRY.md](API_QUOTA_AND_RETRY.md) for how Gemini quota is spent,
who retries, and what the request ledger records.
Historical reports and evidence below are retained for context; they are not a
substitute for the current README and architecture guide. Student and account
identifiers in those reports have been replaced with stable labels such as
`Student A`; do not add real names, email addresses or raw student reports to
this folder.

Everything else in this folder is **documentation produced while building the app**.
Nothing here is imported, served or packaged: the release allowlist
(`MathChecker_Distribution/scripts/release_allowlist.py`) never copies `docs/`,
so keeping reports here also guarantees they cannot leak into a portable build.

The project root holds the app entry points plus documents referenced by the
application (`README.md`, `DISTRIBUTED_DEPLOYMENT.md`). See
[PROJECT_STRUCTURE.md](PROJECT_STRUCTURE.md) for the whole layout and cleanup
policy.

## reports/ — audit and delivery reports (markdown)

| File | What it records |
|---|---|
| [AUDIT_REPORT.md](reports/AUDIT_REPORT.md) | Live-accuracy safety fixes + real-student audit (2026-10-08) |
| [DISCOVERY_REPORT.md](reports/DISCOVERY_REPORT.md) | Account discovery, device registration and tiny-agent startup |
| [DISTRIBUTED_REPORT.md](reports/DISTRIBUTED_REPORT.md) | Distributed multi-school architecture implementation |
| [MULTI_ACCOUNT_AND_PUBLIC_ADDRESS_REPORT.md](reports/MULTI_ACCOUNT_AND_PUBLIC_ADDRESS_REPORT.md) | Multi-account switch + public-address switch delivery |
| [PORTABLE_RELEASE_REPORT.md](reports/PORTABLE_RELEASE_REPORT.md) | Portable Windows release build and verification |
| [PURGE_REPORT_C1L13.md](reports/PURGE_REPORT_C1L13.md) | Targeted data purge of one assignment |
| [PURGE_REPORT_TESTING_CLASS.md](reports/PURGE_REPORT_TESTING_CLASS.md) | Targeted data purge of the TESTING class |
| [REPORT_GENERATION_AUDIT.md](reports/REPORT_GENERATION_AUDIT.md) | Report generation/attachment audit: log forensics, root causes, fixes |
| [FINAL_RELEASE_CHECKLIST.md](reports/FINAL_RELEASE_CHECKLIST.md) | Release checklist and final gate results |
| [FINAL_RELEASE_HANDOVER.md](reports/FINAL_RELEASE_HANDOVER.md) | Release handover notes and validation evidence |
| [LIVE_CLASSROOM_TESTING_REPORT.md](reports/LIVE_CLASSROOM_TESTING_REPORT.md) | Live Classroom integration testing report |
| [MARKING_DETERMINISM_INVESTIGATION.md](reports/MARKING_DETERMINISM_INVESTIGATION.md) | Marking determinism investigation and reproduction |
| [OPTIMIZATION_REPORT_2026-10-18.md](reports/OPTIMIZATION_REPORT_2026-10-18.md) | Performance/size optimization report |
| [SYSTEM_AUDIT_REPORT.md](reports/SYSTEM_AUDIT_REPORT.md) | Full system safety and correctness audit |
| [VERIFICATION_GAPS_CLOSURE.md](reports/VERIFICATION_GAPS_CLOSURE.md) | Verification gaps found and how each was closed |
| [live-classroom-readonly-audit-2026-10-22.md](reports/live-classroom-readonly-audit-2026-10-22.md) | Read-only live Classroom audit |
| [RETRY_APPROVE_FIX_REPORT.md](reports/RETRY_APPROVE_FIX_REPORT.md) | Retry / Approve reliability fix on the Automatic Correction page |
| [SIMPLIFICATION_REPORT.md](reports/SIMPLIFICATION_REPORT.md) | Simplification, data portability and UI integration pass |
| [SYNC_AGENT_REPORT.md](reports/SYNC_AGENT_REPORT.md) | Background sync agent + role-based report distribution |
| [TEAMS_ACCOUNT_AUDIT_REPORT.md](reports/TEAMS_ACCOUNT_AUDIT_REPORT.md) | Assistant teams, account management and developer audit logs |
| [TWO_WINDOWS_E2E_REPORT.md](reports/TWO_WINDOWS_E2E_REPORT.md) | Two-environment end-to-end integration test evidence |
| [UI_ARCHITECTURE_REPORT.md](reports/UI_ARCHITECTURE_REPORT.md) | UI architecture correction: separate pages, Task Manager, AI workers |
| [V5_CLEANUP_AND_PACKAGING_REPORT.md](reports/V5_CLEANUP_AND_PACKAGING_REPORT.md) | Workspace cleanup, `V5.exe` launcher build and release verification |

## results/ — machine-readable run results

Verification output written by the scripts in `backend/scripts/`. Some files
cover live Classroom runs; account names, course titles and student identifiers
are redacted or pseudonymised, and the raw student reports are deliberately not
kept in the repository. Regenerate a result with its script rather than editing
it by hand.

* `LIVE_CLASSROOM_CERTIFICATION.json` — live marking + write-refusal checks
  (`backend/scripts/verify_classroom_readonly.py` and the live harness).
* `CLASSROOM_READONLY_CERTIFICATION.json` — read-only Classroom surface checks.
* `LIVE_ACCESS_SWITCHES_RESULTS.json` — access-switch checks
  (`backend/scripts/live_verify_access_switches.py`).
* `MARKING_REFERENCE_RESULTS.json` and `MARKING_REFERENCE_PAPER_B.json` — the
  reference-marking harness (`backend/scripts/verify_marking_reference.py`).
* `UI_WORKFLOW_CHECKS.json` and `final_ui_checks.json` — browser workflow/layout
  checks (`backend/scripts/verify_ui_workflows.py`, `release_ui_check.py`).
* `API_OPTIMIZATION_BASELINE.md` — what the application measured (and could not
  measure) before the API-efficiency work; the definitions every later number
  uses.
* `API_OPTIMIZATION_FINAL_REPORT.md` — what was implemented, what the live
  measurements showed, and what is still unavailable or deferred.
* `MODEL_SWEEP_RESULTS.json` — candidate-model sweep output
  (`backend/scripts/sweep_models.py`): access, latency, provider-reported
  tokens and schema validity per model. Carries no accuracy claim.
* `CORRECTION_PERFORMANCE_RECOVERY.md` — why corrections went from ~130 s to
  ~600 s per paper, the measured cause, and what was changed.
* `MARKING_CONFIG_BENCHMARK.json` — marking configurations compared on the same
  reference papers (`backend/scripts/benchmark_marking_config.py`).
* `two_env_e2e_results.json` — two-installation end-to-end harness output.
* `audit/` — re-runnable investigation helpers (`guard_proof.py`, `q9_repro.py`)
  and their recorded outputs.
* `reference_paper_b/` — expected results and harness transcript for paper B.

## logs/ — local development logs (not version-controlled)

`docs/logs/` is git-ignored. Runtime logs of the live app live next to the data
they describe, in `data/logs/`; packaging logs live in
`MathChecker_Distribution/logs/`. Console transcripts captured during a
verification run stay on the machine that produced them and should only be
shared after checking them for student data.

## Evidence that was removed

One-off packaged-runtime evidence for the report audit (`docs/evidence/report_audit/`)
contained a real student's report and is intentionally no longer tracked. The
findings remain in [reports/REPORT_GENERATION_AUDIT.md](reports/REPORT_GENERATION_AUDIT.md).
The obsolete `docs/archive/probe/` refactor scratch was also removed; it was not
referenced by any test, build or runtime path, and version control still holds
its history.

## Also here

* `ARCHITECTURE.md` — current source/runtime boundaries and subsystem dependencies.
* `MARK_SCHEME_SUBQUESTIONS.md` — how a mark scheme's subquestions are detected,
  how answers and marking criteria are mapped to their identifier, and what is
  still left to review.
* `SERVER_LIFECYCLE.md` — lifecycle guide and a historical verification record; its
  test counts and checkout-specific findings may age and should be rechecked.
