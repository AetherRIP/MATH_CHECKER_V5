# Contributing

This is a single-teacher application first and an open codebase second: changes
that make marking less predictable, less explainable, or less private are not
welcome, however convenient they are. Everything below is about keeping those
properties while the code grows.

## Before you start

- Read [docs/architecture/ARCHITECTURE.md](docs/architecture/ARCHITECTURE.md) —
  the correction pipeline and the worker isolation model explain most of the
  design decisions you will meet.
- Read the cleanup policy in
  [docs/architecture/PROJECT_STRUCTURE.md](docs/architecture/PROJECT_STRUCTURE.md)
  before deleting or moving files. Some directories look disposable and are not.
- Never commit student data, API keys, OAuth client files or a database. `data/`
  is git-ignored and must stay that way; see [Privacy rules](#privacy-rules).

## Set up a working copy

```bash
# Windows
setup.bat                 # creates .venv, installs runtime + dev dependencies

# Linux / macOS
python -m pip install -r backend/requirements-dev.txt

# Web UI (only when changing it)
cd frontend && npm ci
```

Run the app:

```bash
python main.py            # http://127.0.0.1:8642
```

`Settings → Offline demo (mock mode)` exercises the whole UI with no API key and
no network.

## Run the checks

These are the checks that define "green" for this repository. There is no CI
service connected, so they are run locally and their results are reported in the
pull request.

```bash
# Backend — full suite (~12 minutes)
.venv/Scripts/python.exe -m pytest backend/tests -q

# Backend — the area you touched (seconds)
.venv/Scripts/python.exe -m pytest backend/tests/test_retry_architecture.py -q

# Desktop
dotnet test desktop/MathChecker.Desktop.slnx -c Release

# Web UI build (TypeScript + Vite)
cd frontend && npm run build

# Documentation links
python scripts/check_doc_links.py --strict
```

Notes:

- Provider calls are mocked in the suite. A test that needs a real key is a bug,
  not a test.
- If the desktop suite fails with
  `Application Control policy has blocked this file`, the operating system is
  refusing the freshly built assembly. Rebuild in `Release` into a different
  output path, or allow-list the build folder. It is not a code failure.
- Never "fix" a failing test by weakening an assertion, skipping it, or
  suppressing the error. If the behaviour genuinely changed, explain why in the
  pull request and add a test for the new behaviour.

## What belongs where

| Change | Where it goes |
|---|---|
| HTTP surface | `backend/app/api/` |
| Marking logic, guards, scheme analysis | `backend/app/engine/` |
| Provider client, keys, retries, telemetry | `backend/app/gemini/`, `backend/app/retry_policy.py` |
| Queue, scheduling, worker processes | `backend/app/jobs/`, `backend/app/workers/` |
| Desktop UI | `desktop/src/MathChecker.Desktop/` |
| Web UI | `frontend/src/` |
| Verification and benchmark tooling | `backend/scripts/`, `scripts/` |
| Documentation | `docs/` (see the map in [docs/README.md](docs/README.md)) |

Tests live next to the surface they cover, under `backend/tests/` (pytest) and
`desktop/tests/` (xUnit). Name a test file after the module or behaviour, not
after the bug report.

## Rules that are not negotiable

1. **Do not change marking semantics casually.** Prompts, response schema,
   question detection, clamping and the double-check pass are the accuracy
   surface. A change there needs its own evidence (a benchmark on identical
   papers, or a teacher-verified comparison) and a note in the pull request.
2. **A faster configuration is not automatically better.** The 2026-10-30
   benchmark found that disabling model reasoning was twice as fast on one paper
   and five marks lower on the same paper. Speed claims need accuracy evidence
   next to them.
3. **Never silently switch models.** A fallback that marks differently from the
   primary model produces results nobody can compare. Fallbacks are explicit,
   bounded, and visible in the job's stage history.
4. **Never delete failed evidence.** A benchmark run that failed is data. It is
   preserved and labelled, not overwritten.
5. **One retry owner per request.** The application, not the SDK, decides whether
   a request is sent again. Adding a retry loop above or below the existing one
   multiplies requests against the provider's quota.
6. **Errors are classified, not swallowed.** A new failure mode gets a class and
   a test, not a bare `except`.

## Privacy rules

- `data/` (database, `secrets.toml`, OAuth client, uploaded PDFs, logs) is
  git-ignored. Do not add exceptions to that for convenience.
- Test fixtures must be synthetic or anonymised. Real student scans stay on the
  machine that captured them; the reference marking fixtures in
  `docs/reference-marking/` are synthetic on purpose.
- Log lines may carry a key *fingerprint*, never the key. Same for tokens,
  student names in benchmark artefacts, and anything written to `docs/results/`.
- Screenshots must come from the offline demo mode. Live screens contain names.

## Commit and pull-request style

The history is written for a reader who was not there. Subjects are imperative
and say what changed and why, for example:

```
Stop a transient 503 from turning into a two-hour correction
Keep a job's retry budget across restarts, and ask the provider before a batch
```

- One logical change per commit. Repository hygiene, code changes and
  documentation are separate commits when they are separable.
- Explain *why* in the body when the reason is not obvious from the diff. Include
  the measurement that justified a performance change.
- Do not rewrite history, force-push, or amend commits that already exist on a
  shared branch.
- Do not push to the remote without the owner's explicit approval.
- Pull requests state what was verified and how, and name any check that could
  not be run.

## Adding a dependency

Add a runtime dependency to `backend/requirements.txt` only if the application
needs it to run; test and build tooling belongs in
`backend/requirements-dev.txt`. Prefer the standard library, and pin upper bounds
where a major version changes the API (PyMuPDF is the worked example: 2.0 pulls
in a different dependency set and grows the portable release).

For the desktop client, no third-party NuGet packages are used today. That is a
deliberate choice for a build that must work on a school machine — please discuss
before adding one.

## Documentation

Every document should be true of the code as it is, not as it is planned to be.
When a change alters behaviour that a document describes, update that document in
the same pull request. Run `python scripts/check_doc_links.py --strict` before
pushing; a moved file with stale links is the most common way this set rots.
