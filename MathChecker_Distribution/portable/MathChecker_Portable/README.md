# Math Checker V5

Production Edexcel IGCSE 4MA1 homework correction system. **Built around the
proven correction engine** from `Friend code/edexcel-maths-correctorv2-main`
(preserved untouched as the reference implementation), wrapped in a reliable
multi-key, job-based, local-first application.

> **New in 5.1 (productisation pass):** first-run setup wizard, configurable
grade boundaries, deterministic result guard (a valid mark can never be
silently replaced by a suspicious one), a human review queue with teacher
overrides, teacher-facing health checks, config/database backup, enforced file
retention, and a portable distribution builder. The correction engine itself
is unchanged. See [Productisation](#productisation-51) below.

## What V5 adds around the preserved engine

| Area | Implementation |
|---|---|
| Correction engine | **Verbatim** legacy marking core (`backend/app/engine/legacy_marking.py`) — same prompt, schema, model fallback chain, normalization, totals |
| Determinism | `temperature: 0` + fixed seed on every marking call; **mark-scheme blueprint** (`engine/blueprint.py`) extracts the official question list/max marks ONCE per mark-scheme file and fixes them for every run; model-chain health + pinned first model persist across worker processes (`engine/model_health_store.py`) |
| Mark integrity | Post-marking reconciliation against the blueprint (`engine/blueprint.py` + `verify.py`): missing questions become `REVIEW` (never a silent 0), max marks are forced back to the official value, verification reports only — it never rewrites a mark |
| Gemini keys | Multi-key manager (`gemini/key_manager.py`): fingerprints, round-robin rotation, cooldowns (5s→10m), invalid-key benching, persisted stats. In-job rotation too: a mid-job 429/503/401 switches KEY (not model), re-uploading inputs under the new key because Gemini Files are key-scoped |
| Students | `api/student_routes.py` + Students page: personal profile with LOCAL nickname, Classroom-verified submission counts, local correction history, every stat source-labelled |
| Verification | Deterministic post-verification (`engine/verify.py`) replaces the 2×-cost AI audit — question marks stay pure legacy output |
| Jobs | Persistent SQLite job system (`jobs/manager.py`): staged progress, duplicate prevention, cancellation, retry, crash recovery |
| Retry / failover | Classified-failure retry architecture (`retry_policy.py`): every failure maps to a policy — rate-limit/high-demand → **park** on cooldown (attempts untouched), timeout/network → 1 bounded retry, auth/invalid/context-too-large → **never resend**. Exponential backoff with jitter + Retry-After, strict per-job retry budget (15 min park budget → clean FAILED, never a loop), key failover vs provider failure distinguished, provider circuit breaker (`gemini/provider_health.py`) pauses whole batches on sustained 503s |
| Prepared input | Per-job cache (`data/tmp/job-<id>/`, `prepared.json`): Drive download, validation/normalization and mark-scheme resolution happen ONCE; every retry/failover reuses the prepared files (counted as avoided downloads/uploads on the Usage page). Cache deleted at terminal cleanup |
| Idempotency | Before any retry (dispatch, failure handling, boot recovery) an existing successful result is attached instead of re-running — duplicate AI corrections are never created |
| Classroom | Existing OAuth client (moved to `data/secrets/`), REST wrappers preserved, DB-cached sync, allow-list, draft-grade attempt with 403 fallback |
| UI | React + Vite glassmorphism UI (`frontend/`): Dashboard, Classroom, Students, Corrections, Results, **Report**, API Keys, Settings, Developer Diagnostics |
| Local-first | Courses/assignments/submissions/students cached in SQLite with TTL; no background polling; event-driven SSE updates |

## Run

```bash
# Linux / macOS
./start.sh              # serves http://127.0.0.1:8642
./start.sh 9000         # custom port

# Windows — first time only (creates a native Windows venv + dependencies)
setup.bat
# Windows — start the app
start.bat

# or directly (Python 3.12+)
python main.py

# Frontend (only needed when changing UI code; a build ships in frontend/dist)
cd frontend
npm install             # must be run on the OS that runs the app
npm run build
```

On first launch the app opens a **setup wizard** (teacher name, AI keys, Google
Classroom, network + retention behaviour, health check). A teacher never edits
a source file to configure it.

> **Windows note:** a virtual environment is platform-specific — one created
> on Linux lives in `.venv/bin` and cannot run on Windows. `setup.bat`
> detects that, moves it aside to `.venv.foreign` (nothing is deleted) and
> builds a native `.venv\Scripts` environment with the dependencies.

> **Linux note:** `node_modules` is platform-specific. If the project was
> prepared on Windows, run `npm install` once on Linux so Rollup/esbuild
> native binaries match the OS (`npm install @rollup/rollup-linux-x64-gnu
> --no-save` fixes a rollup error without touching the lockfile).

First run creates `data/` (git-ignored). Then:

1. **Settings → Role → Developer** (for diagnostics; Teacher otherwise)
2. **API Keys → Add keys** — paste Gemini keys, one per line
3. (Optional) **Classroom → Connect** — Google sign-in via the existing OAuth client
4. **Report** — class/assignment reports with PDF export (Assignment + Class report)
5. **Corrections → Clear Log** — hides finished jobs from the view (never deletes data);
   **Diagnostics → Logs** — filtered log view (Running/Completed/Failed/Cancelled/Warnings/System)
   with a confirmed "Clear Log" that truncates only the log file

Demo without a key: **Settings → Offline demo (mock mode)**.

## Where things live

```
backend/app/
  config.py  db.py  schema.sql  logging_setup.py  errors.py  main.py
  engine/    legacy_marking.py (preserved) · blueprint.py (mark-scheme control)
             model_health_store.py (stable model choice) · service.py · verify.py
  gemini/    key_manager.py · client_proxy.py (in-job key rotation) · tester.py · mock.py
  jobs/      manager.py (queue, workers, recovery)
  workers/   pool.py · adapter.py · child.py (isolated correction process)
  classroom/ auth.py · client.py · sync.py · submissions.py · tracking_routes.py
  api/       routes_all.py (REST + SSE) · student_routes.py (profiles/nicknames)
backend/tests/          271 tests (engine, blueprint, keys, jobs, tracking, reports, API,
                               retry/failover — all provider failures mocked, no quota)
backend/scripts/        compare_legacy.py · repeatability.py (same PDF, N runs)
frontend/               React glass UI (built dist served by the backend)
agent/                  tiny-agent source (shipped, disabled in this version)
scripts/                build_portable.py · install_autostart.py · two_env_e2e.py
Friend code/            original Streamlit app — untouched reference
data/                   git-ignored: app.db, secrets.toml, secrets/, blueprints/, logs/
docs/                   reports, results, logs and evidence from the build work
                        (never shipped) — start at docs/README.md
dev_local/              dev-only scratch: report previews, harness data dirs
                        (never shipped, delete freely) — see dev_local/README.md
autostart/              machine-local autostart state written by the app
MathChecker_Distribution/  the packaging workspace (see below)
```

The full map — what is shipped, what is disposable, what must never be tidied
away, and the cleanup policy — is in
[docs/PROJECT_STRUCTURE.md](docs/PROJECT_STRUCTURE.md).

## Correction flow (real stages, never faked)

`QUEUED → PREPARING → DOWNLOADING → UPLOADING → MARKING → VERIFYING → SAVING → COMPLETED`

- Duplicate prevention: one active job per Classroom submission (partial UNIQUE index)
- Restart safety: in-flight jobs requeued on boot; completed work never redone;
  an existing result is reused instead of re-marking (no duplicate corrections)
- All-keys-cooldown: job parks in `WAITING_FOR_API` — parks do NOT consume the
  retry budget and are bounded by a 15-min wait budget (clean FAILED, no loop)
- FAILED API REQUEST ≠ RESEND EVERYTHING: failures are classified first; a
  retry reuses the prepared input (no re-download, no re-normalization, no
  mark-scheme refetch) and only when the class allows it
- Usage page shows local API stats: requests/successes/failures, retries,
  key failovers, cooldowns, and how many downloads/uploads were avoided
- Storage: student PDFs deleted immediately after Gemini upload; only marks/feedback/metadata persist

## Regression protection

- `backend/tests/test_legacy_engine.py` locks normalization, clamping, totals,
  grade bands, JSON extraction, and model-fallback semantics.
- `backend/tests/test_determinism_blueprint.py` locks the determinism layer:
  temperature/seed, blueprint caching/reconciliation, "missing question →
  REVIEW, never a silent 0", and mid-job key rotation with file re-upload.
- Real-submission comparison:
  ```bash
  cd backend
  python scripts/compare_legacy.py --student work.pdf --scheme scheme.pdf
  ```
  Run the same PDFs through the original Streamlit app and diff the printed
  question marks — they come from the identical preserved code path.
- Repeatability (the property V5 exists to guarantee):
  ```bash
  cd backend
  python scripts/repeatability.py --runs 3
  # runs the SAME student PDF + SAME mark scheme N times and prints a
  # per-question matrix, totals, models, key fingerprints and a VERDICT.
  ```
  Measured on `data/fixtures/real/` (3 configured keys): 5 runs produced
  4×75/103 and 1×74/103 with an identical question set, identical maximum
  marks (blueprint), and all three keys used.

## Honest limitations

- **Grade write-back**: Google only lets an OAuth app grade coursework it
  created itself (the legacy app hit 403 here). V5 attempts draft grades when
  enabled and otherwise shows suggested grades for manual entry.
- **Provisional grades** are percentage bands, not official Pearson boundaries.
- **Roles** are local UX gating (single-teacher desktop tool), not multi-user auth.
- Real-PDF regression requires your sample submissions; the harness above makes
  it one command.
- **Residual model variance**: question set, numbering, maximum marks and totals
  are now stable across runs; an individual question mark can still move by ~1
  when a run lands on a different model because the pinned one is temporarily
  quota-blocked. Every result records which model and which key (fingerprint)
  produced it, so this is visible — and `verification.status = REVIEW` flags
  structural gaps instead of hiding them.
- **Classroom OAuth** completes in your browser (loopback flow, up to 5 min);
  the connected account email appears on the Classroom page after sign-in.
  Submission statuses come from the Classroom API only — with no sync yet the
  UI says "unavailable" rather than guessing.

## Productisation (5.1)

Everything below is additive; the correction pipeline is untouched.

| Area | What was added |
|---|---|
| First-run setup | `GET/POST /api/setup/status`, `/api/setup/complete`; React `SetupWizard` shown until complete. Stored in `config.toml [teacher]` |
| Configurable grades | `engine/grading.py` + `[grades]` config. Grade = deterministic lookup of (marks → % → boundary table). The AI never invents a grade; the legacy percentage bands remain the default until the teacher sets real boundaries |
| Result guard | `engine/result_guard.py`, applied on BOTH save paths (worker pool + legacy service). Never replaces a valid stored result with a suspicious one (e.g. a valid 67/103 is not overwritten by 0/103); flags for review instead. Confidence: HIGH / MEDIUM / LOW / VALIDATION_FAILED / LARGE_SCORE_CHANGE |
| Review queue | `GET /api/review`, `POST /api/results/{id}/override` (teacher override is preserved and cannot be silently overwritten), `POST /api/jobs/{id}/reprocess` (the only way to force a replace) |
| Teacher diagnostics | `GET /api/diagnostics/selfcheck` — plain-language Application / Storage / AI / Classroom / Network checks, each with a status and a fix |
| Backup / restore | `GET/POST /api/backup/config` (secrets excluded unless explicitly requested), `GET /api/backup/database` (consistent SQLite copy) |
| Retention (§19) | `[retention]` config actually enforced: `never` / `until_complete` / `days` / `always`; retained originals expire and are reclaimed at startup. Mark schemes are never retained |
| Network access (§26) | `[network] access_mode = local \| lan`. Local (127.0.0.1) is the default; LAN is an explicit opt-in. Public exposure is deliberately not offered |
| Versioning (§44/§45) | `app/version.py` is the single source of truth (app + schema version). `db._migrate` records the schema version and refuses to migrate a newer database backwards |
| Portable build | `scripts/build_portable.py` assembles a shareable folder and refuses to ship credential files. `start.bat`/`start.sh` support portable mode (`portable.flag`) and a bundled `runtime/` |

## Teacher experience & Classroom honesty (5.2)

The correction engine is untouched. 5.2 is a UI/UX + workflow pass built
around it, plus the managed-vs-external Classroom architecture.

| Area | What changed |
|---|---|
| Design system | Dual light/dark palettes (`frontend/src/theme.tsx`), layered glass surfaces, a real button system (primary/secondary/success/warning/danger/ghost/nav + icon-only, loading, disabled), tooltips, status cards, attention rows, tabs, progress rings, and a dependency-free inline SVG icon set (`frontend/src/Icons.tsx`) |
| Navigation | Collapsible sidebar (persisted) with grouped IA — Teaching / Insights / Operations / Integrations / System — and a contextual top bar (page title, sync status, global search, notification bell, theme switch, profile menu) |
| Workflow | Dashboard → Classes → Assignments → Submissions → Correction → Review → Reports → Google Classroom, with new **Classes**, **Assignments** and **Review Queue** pages |
| Dashboard | Clickable status cards (Not submitted, Submitted/unchecked, Checked, Class average, Needs review, AI keys) + a global **What needs my attention?** panel fed by `/api/attention` |
| Managed vs External | Assignments created through the app are persisted as **Managed** (`classroom_managed_assignments`); everything else is **External**. The kind is proven from creation metadata, never guessed from a title |
| Honest publishing | `/api/classroom/assignments/{id}/publish` executes per-student operations (draft grade, final grade + return) and records Google's **real** response in `classroom_publish_ops` — published / failed / not_supported / requires_auth / requires_review. A 403 becomes a plain-language limitation, never a success. Attaching files to a submission is reported as not supported (the Classroom API does not offer it) |
| Safety gate | Grades for results still flagged for review are blocked server-side and surfaced as `requires_review` — the UI cannot publish what the engine has not accepted |
| New endpoints | `GET /api/classes`, `GET /api/assignments`, `GET /api/assignments/{id}`, `POST /api/assignments/local`, `POST /api/classroom/courses/{c}/assignments/create`, `POST /api/classroom/assignments/{id}/import`, `GET /api/classroom/assignments/{id}/publish/queue`, `POST .../publish`, `GET /api/attention` |
| Tests | `backend/tests/test_managed_publishing.py` locks managed detection, capability gating, the publish queue, honest 403/401 mapping, the review safety gate, and the attention feed |

> Principle: **never sacrifice correctness for appearance, and never report a
> Classroom operation as successful unless Google confirmed it.**

## Shareable release system (5.2)

The private master project is **never modified** by the release process. A
separate workspace, `MathChecker_Distribution/`, builds a clean production
package from it:

```
MathChecker_Distribution/
  build_release.bat          one command: runtime -> staging -> scan -> packages
  scripts/
    release_allowlist.py     explicit production allowlist (allowlist-first)
    scan_release.py          blocking secret + privacy scan
    build_runtime.py         reproducible bundled Python runtime (no prerequisites)
    build_release.py         pipeline orchestrator
  installer/                 themed Inno Setup script + brand assets + launcher
  documentation/             recipient guide + privacy notes + build guide
  release/                   MathChecker_Setup.exe · MathChecker_Portable.zip
                             · SHA256SUMS.txt · release_manifest.json
  logs/                      output of past pipeline runs
  previous_builds/           superseded packages (retention policy in README)
```

Neither the master project nor the release system ever writes private material
into a package. See `MathChecker_Distribution/README.md` for the folder layout
and the previous-build retention policy.

Pipeline: clean staging → production frontend build → **copy allowlisted
files only** → secret + privacy scan (BLOCKS the build on any finding) →
dependency validation → portable package → themed Windows installer → SHA-256
hashes → release manifest → final validation.

**Zero prerequisites for the recipient.** `build_release.bat` also builds
`build/runtime/` — a stock CPython embeddable interpreter plus the backend's
PyPI dependencies — so the installer and portable package run with no Python,
Node or developer tools on the target machine. It is a distribution-owned
artifact, verified by construction; the scanner applies a narrow,
user-data-focused rule set to `runtime/` (library files are legitimately named
`oauth2.py`/`credentials.py`, and `certifi` ships a real `cacert.pem`).

Building the installer needs Inno Setup 6 on the *build* machine only
(`winget install JRSoftware.InnoSetup`); the pipeline finds it on `PATH`, in
`Program Files`, or under `%LOCALAPPDATA%\Programs`.

What is safe to share: `release/MathChecker_Portable.zip` (or the compiled
`release/MathChecker_Setup.exe`), plus `SHA256SUMS.txt` and
`release_manifest.json` for the recipient to verify. The pipeline directories
themselves (`build/`, `release_staging/`, `portable/`) and
`previous_builds/original_*/` are working copies of one machine, not packages.
The build refuses to ship a bundled runtime that is missing its compiled
modules (`*.pyd`) — a runtime like that looks complete but cannot import
`socket`/`sqlite3`/`ssl`, so it would not start at all on a clean Windows PC.
See `MathChecker_Distribution/README.md` for the details.

Privacy is solved by **not distributing** private material, never by hiding it:
`Friend code/`, `data/`, credentials, tokens, databases, PDFs, logs, `.git/`
and development caches are refused by both the allowlist and the independent
scan. See `MathChecker_Distribution/documentation/PRIVACY.md`.

Data separation: installed app files live under `Program Files`; user data
(config, keys, database, cache, logs, reports) lives under
`%LOCALAPPDATA%\MathChecker\` and is marked `uninsneveruninstall`, so updates
and reinstalls never destroy it.

| Feature | Endpoint / entry point |
|---|---|
| Factory reset (removes local data, keeps the app) | `POST /api/privacy/factory-reset` (needs `{"confirm":"RESET"}`), UI: **Privacy & Data** |
| Sanitized diagnostic export | `GET /api/privacy/diagnostic-export` |
| Data locations | `GET /api/privacy/info` |
| Demo Mode (synthetic data, Classroom disabled) | `python main.py --demo` / Start-menu shortcut / `MC5_DEMO=1` |
| Build identity | `app/version.py` `BUILD_NUMBER` / `BUILD_DATE`, surfaced in the app and in the manifest |

## Security

- Keys/secrets only in `data/secrets.toml` + `data/secrets/` (git-ignored, chmod 600)
- API never returns key material; logs pass a redaction filter
- Keys are referenced everywhere by SHA-256 fingerprint prefix
