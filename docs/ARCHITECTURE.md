# Math Checker architecture

This describes the existing application boundaries; it does not prescribe a
replacement architecture. The supported product is a local-first FastAPI and
React application with an optional portable Windows distribution.

## Runtime and entry points

- **`start.bat` / `start.sh`** are the source-checkout launchers. They change to
the project directory, select a native/bundled/system Python, honour the
portable data override when present, then run root **`main.py`**.
- **`main.py`** handles process-level startup, port selection, instance/stop
commands and optional supervision. It starts **`backend/app/main.py`**, which
constructs the FastAPI application, registers the API routers and manages
application lifespan.
- **`backend/app/`** is the backend runtime package. `config.py` resolves
settings, secrets and the data directory; `db.py` owns SQLite access and
initialization from `schema.sql`. Runtime state belongs under the configured
`DATA_DIR`, not in source modules.
- **`frontend/src/`** is the React/TypeScript UI. Vite compiles it into
`frontend/dist/`; the backend serves that generated bundle. The source package
and lockfile are under `frontend/`, and `npm run build` performs the TypeScript
build before bundling.

## Main application flows

### Mathematical marking and scheme safeguards

`backend/app/engine/service.py` is the correction facade called by API routes
and workers. It obtains manual or Classroom submission files, routes long PDFs
when appropriate, calls the preserved marking implementation in
`engine/legacy_marking.py` through the Gemini client/key manager, verifies the
result, and persists it through SQLite. `engine/scheme_structure.py` holds the
structured question/subquestion representation (identities, accepted answers,
M/A/B marking points, per-subquestion validation); `engine/blueprint.py`,
`verify.py`, `question_inventory.py`, `result_guard.py` and `scheme_audit.py`
provide question/max-mark reconciliation at subquestion level, verification,
overwrite protection and recorded mark-scheme discrepancies. See
[MARK_SCHEME_SUBQUESTIONS.md](MARK_SCHEME_SUBQUESTIONS.md). Keep scoring-policy changes separate from
folder cleanup and preserve their regression tests.

`backend/app/jobs/` stores correction jobs and coordinates retry, recovery,
cancellation and duplicate prevention. `backend/app/workers/` runs isolated
workers. `backend/app/large_pdf/` owns the resumable large-document workflow;
it shares the correction engine and database rather than implementing a second
marking policy.

### Google Classroom, reports and automation

`backend/app/classroom/` contains OAuth/token handling, API client wrappers,
local synchronization/cache, submission handling, managed assignment actions,
and report-attachment and cleanup helpers. Classroom network calls are behind
this boundary; tests normally mock external responses. Actual OAuth, account
permissions and live Google behavior require configured credentials and are not
proven by offline tests.

`backend/app/reporting/` builds report content and documents. API endpoints are
in `backend/app/api/report_routes.py`; report attachments use the Classroom
helpers. PDF rendering, image/PDF processing and merging depend on the libraries
pinned by `backend/requirements.txt` (notably ReportLab, PyMuPDF, pypdf and
Pillow where used).

A lesson's identifier is its FULL chapter-and-lesson code, derived from the real
Classroom title by `reporting/class_report.parse_assignment` ("CH1L1",
"CH2L1"). Lesson numbers restart inside every chapter, so the chapter is never
dropped: grouping, sorting, the report tables and the grade matrix all use the
complete identifier, while anything that must pair a mark with its *own*
assignment keys on `coursework_id`, because two assignments may legitimately
share a code. A title with no chapter keeps exactly the identifier the source
data has ("L2"); a chapter is never invented, and an identifier the parser
cannot trust is left as the title text.

`backend/app/automation/` persists the automated workflow and its state. Its
scheduler calls `automation/runner.py`, which composes the existing correction,
report, attachment, grade/publish and cleanup services. Retry states and
idempotent steps are persisted; this module is orchestration, not a replacement
for those domain services.

The Automatic Correction page reads its counters from
`store.execution_counts()`, which counts LOGICAL jobs (one per submission,
`COUNT(DISTINCT aj.id)`) and only when a job is genuinely active: its worker
claim is inside `CLAIM_LEASE_SECONDS`, or its child correction job is in a
running engine state. Completed, failed, retry-backoff and stale-claim jobs are
reported as waiting or not at all, so a job moving through several pipeline
stages is never counted more than once.

A job row has ONE status and ONE error, which is the pipeline's position and not
a description of what Google Classroom actually holds. `automation_operations`
records the three independent operations a job performs — report attachment,
grade publication and return — one row per `(job_id, op_key)` with a CURRENT
state (`PENDING`, `IN_PROGRESS`, `SUCCEEDED`, `FAILED`,
`NEEDS_VERIFICATION`, `CANCELLED`) plus the ERROR HISTORY of the attempt that
failed (when, which attempt, and whether it was later resolved or recovered).
A retry re-uses that row, so it resolves the same logical operation instead of
creating a parallel one, and the row is what stops a second report attachment
or a second grade write. Success that was only established by READING the
remote state is recorded as `recovered`, and the earlier error becomes
history the moment the operation succeeds. An attempt cut off by a crash or a
timeout is recorded as `NEEDS_VERIFICATION`, never as a plain failure.
`POST /api/automation/jobs/{id}/reconcile` (and the bounded
`POST /api/automation/jobs/reconcile` behind the page's *Refresh Status*
button) re-reads Classroom, read-only, and settles those states;
`POST /api/automation/errors/clear` removes only resolved error messages and
`POST /api/automation/jobs/{id}/errors/{op}/dismiss` acknowledges one error
without changing its operation's state — a genuine failure stays visible.

Grade and report publication carries its own durable idempotency records, all
keyed by the corrected result rather than the attempt: `report_upload_records`
(Pending/Uploading/Uploaded/Ambiguous/permanent failure, plus the verified
remote file id), `bulk_upload_items` (the per-component outcome of a teacher-run
bulk upload), `bulk_upload_claims` (one live run per submission),
`report_upload_locks` (a cross-process lease on one submission's report) and
`automation_bulk_runs` (+`_items`, the recorded run and its per-submission
results). `POST /api/automation/bulk-upload` sends only the components still
missing, so it never re-runs the AI correction, never re-generates a report and
never adds a second attachment for an unchanged report. `POST
/api/automation/jobs/clear` only sets `automation_jobs.hidden_at` on terminal
jobs: no job, result, grade, report or upload record is deleted.

### HTTP and UI boundaries

Backend route modules live in `backend/app/api/`, with dedicated packages for
scheme review, lifecycle, large-PDF processing and Classroom tracking. The
application registers these routers in `backend/app/main.py`. The frontend
navigation and pages under `frontend/src/` call the API through the shared
frontend API module. Preserve route paths, database schema and frontend asset
paths when making changes.

## Persistence, configuration and privacy

The source checkout defaults to `data/`; `MC5_DATA_DIR` selects an alternate
root for isolated runs. Portable launches set data beside the package. The
installed distribution uses its configured per-user data root. SQLite,
settings/secrets TOML, OAuth client material, blueprints, mark schemes, logs,
retained submissions and temporary job files are application data: do not treat
them as generated build output or move/delete them during source cleanup.

Configuration is managed by the setup wizard and Settings and serialized by
`backend/app/config.py`. `MathChecker_Distribution/portable/MathChecker_Portable/Data/config.toml.example`
is the sanitized, packaged configuration reference. It is not an environment
file and does not contain credential values. Keep live API and OAuth secrets
outside source and generated public packages.

## Tests and release boundary

`backend/tests/` contains pytest tests for engine, API, persistence, retries,
Classroom behavior, reporting, automation, lifecycle and release safeguards.
External services are mocked in tests unless a test explicitly says otherwise.
`frontend/tests/` contains dependency-light UI logic tests; `npm run build`
provides the TypeScript and production-bundle check.

The supported release path is
`MathChecker_Distribution/build_release.bat` → `scripts/build_release.py`.
It stages only files allowed by `scripts/release_allowlist.py`, runs the
independent privacy scan, validates dependencies/runtime extensions and emits a
portable archive and (on Windows with Inno Setup) an installer. `build/`,
`release_staging/`, `portable/` and `release/` are generated pipeline locations;
`previous_builds/` holds rollback material. Never hand-edit generated release
copies or run a rebuild over them without understanding the pipeline's cleanup
and retention behavior. The separate root `scripts/build_portable.py` is a
legacy assembler, not the canonical release path.

## Start reading here

1. This document for component relationships.
2. [Project structure and cleanup policy](PROJECT_STRUCTURE.md) for source,
   data, test and generated-artifact locations.
3. [Main README](../README.md) for Windows setup, configuration, tests and
   developer commands.
4. [Server lifecycle runbook](SERVER_LIFECYCLE.md) for process supervision and
   lifecycle-specific behavior.
