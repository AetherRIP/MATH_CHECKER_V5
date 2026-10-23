# docs/ — developer guides, reports, evidence, logs and archived notes

Start with [ARCHITECTURE.md](ARCHITECTURE.md) for the current component map,
[PROJECT_STRUCTURE.md](PROJECT_STRUCTURE.md) for the repository inventory,
[development-environments.md](development-environments.md) for which backend the
desktop, the browser dev server and a test snapshot use,
[MARK_SCHEME_SUBQUESTIONS.md](MARK_SCHEME_SUBQUESTIONS.md) for how mark-scheme
subquestions are detected and mapped, and [SERVER_LIFECYCLE.md](SERVER_LIFECYCLE.md)
for startup/supervision details.
Historical reports and evidence below are retained for context; they are not a
substitute for the current README and architecture guide.

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
| [RETRY_APPROVE_FIX_REPORT.md](reports/RETRY_APPROVE_FIX_REPORT.md) | Retry / Approve reliability fix on the Automatic Correction page |
| [SIMPLIFICATION_REPORT.md](reports/SIMPLIFICATION_REPORT.md) | Simplification, data portability and UI integration pass |
| [SYNC_AGENT_REPORT.md](reports/SYNC_AGENT_REPORT.md) | Background sync agent + role-based report distribution |
| [TEAMS_ACCOUNT_AUDIT_REPORT.md](reports/TEAMS_ACCOUNT_AUDIT_REPORT.md) | Assistant teams, account management and developer audit logs |
| [TWO_WINDOWS_E2E_REPORT.md](reports/TWO_WINDOWS_E2E_REPORT.md) | Two-environment end-to-end integration test evidence |
| [UI_ARCHITECTURE_REPORT.md](reports/UI_ARCHITECTURE_REPORT.md) | UI architecture correction: separate pages, Task Manager, AI workers |

## results/ — machine-readable run results

* [LIVE_ACCESS_SWITCHES_RESULTS.json](results/LIVE_ACCESS_SWITCHES_RESULTS.json) —
  57 live checks from `backend/scripts/live_verify_access_switches.py` (the script writes here).
* [two_env_e2e_results.json](results/two_env_e2e_results.json) — raw results from
  `scripts/two_env_e2e.py` (the script writes here).

Both files are outputs: delete them and re-run the script to regenerate.

## logs/ — development and verification logs

Console/log output captured while running the app, the suites and the packaging
pipeline (`server.log`, `backend_run.log`, `backend_preview.log`,
`e2e_backend.log`, `full_suite_after_branding_fix.log`,
`PORTABLE_TEST_SUITE_20261012.log`, `PORTABLE_VERIFY_REPORT_20261012.txt`).

Runtime logs of the **live** app are not here — they live next to the data they
describe, in `data/logs/`.

## evidence/report_audit/ — artefacts behind REPORT_GENERATION_AUDIT.md

The packaged-runtime report check (`packaged_report_check.py`), its launch log,
and the PDF/CSV/Markdown a packaged install actually produced for the job that
failed in production.

## archive/probe/ — retained scratch (kept, not deleted)

Old scratch from a `tracking_routes.py` refactor: `new_tracking.py`,
`refactor_tracking.py`, `tracking_routes.bak.py` (a **backup of a source file**),
plus captured API responses (`asg.json`, `courses.json`), a probe mark scheme
(`ms.pdf`) and raw response dumps (`pubfull.*`, `r1.txt`, `r2.txt`, `loc.txt`).

Nothing references these files and no test or build uses them, but
`tracking_routes.bak.py` is the only copy of a pre-refactor source file, so they
were **archived rather than deleted**. Delete the folder if you are sure you no
longer need that comparison.

## Also here

* `_moves.log` — the exact `mv` output of the 2026-10-12 workspace
  reorganisation (a receipt of what moved where; safe to delete).
* `ARCHITECTURE.md` — current source/runtime boundaries and subsystem dependencies.
* `MARK_SCHEME_SUBQUESTIONS.md` — how a mark scheme's subquestions are detected,
  how answers and marking criteria are mapped to their identifier, and what is
  still left to review.
* `SERVER_LIFECYCLE.md` — lifecycle guide and a historical verification record; its
  test counts and checkout-specific findings may age and should be rechecked.
