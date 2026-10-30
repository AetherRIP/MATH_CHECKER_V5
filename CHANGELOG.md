# Changelog

All notable changes to Math Checker V5. The project does not use semantic
version tags yet; entries are grouped by date and ordered newest first. Dates are
commit dates, so they describe when the work landed rather than when it was
planned.

## Unreleased — repository cleanup and hardening pass (2026-10-30)

### Added
- Worker stall watchdog: the correction process emits a heartbeat, and the pool
  terminates and reaps a child that has produced no output for 300 s. A wedged
  process can no longer hold a worker slot, a leased key and a `PROCESSING` row.
- `MAX_OVERLOAD_KEY_ATTEMPTS = 2`: a provider-wide `503` is no longer re-sent
  through every configured key.
- Growing capacity parks (60 s → 120 → 240 → 480, capped at 10 min, jittered)
  instead of a fixed one-minute wait.
- `backend/scripts/job_timing_breakdown.py` — attributes every second of a job to
  queue, preparation, upload, marking or post-processing, and predicts queue wait
  for a given pool width.
- `backend/scripts/benchmark_hardening.py` — multi-paper benchmark with
  randomised arm order and recorded failures.
- `scripts/check_doc_links.py` — verifies every local Markdown link; wired into
  the development workflow.
- `.github/workflows/ci.yml` — runs the backend suite and the desktop Release
  tests on Windows, and the frontend build and the documentation link check on
  Linux, on every push and pull request.
- 16 new tests: retry amplification (9) and worker liveness (7).
- Reports: `docs/results/PERFORMANCE_HARDENING_REPORT.md`,
  `PERFORMANCE_BENCHMARK_REPORT.md`, `RELIABILITY_TEST_REPORT.md`, plus raw
  benchmark JSON including the preserved outage run.

### Changed
- Documentation reorganised into `architecture/`, `development/`, `guides/`,
  `performance/`, `release/`, `reports/`, `results/`; every internal link
  rewritten and verified (253 links, 0 broken).
- Root `README.md` rewritten for a first-time reader: features, architecture
  diagram, stack, setup, testing, performance and honest limitations.
- `.gitignore`: anchored root-only rules that were accidentally matching nested
  folders (`release/`, `dist/`, `logs/`, `tmp/`), and added the per-machine
  runtime state files.

### Fixed
- The per-correction request ledger leaked across pytest tests, so a later test
  inherited a spent 12-request budget and failed with `ProviderBudgetExceeded`.
  The ledger is now reset per test, like the other process-global singletons.
- Machine-local state was tracked in git: `agent/device_id`,
  `agent/agent.lock`, `agent/agent_state.json` and
  `autostart/service_install.json` are now untracked (they are recreated at
  runtime).
- A superseded 124 MB release copy under
  `MathChecker_Distribution/previous_builds/20261016-193501/` was tracked, which
  contradicted the documented policy that those folders are build output. It is
  now untracked; the files stay on the machine.
- Nine broken documentation links in `docs/testing/` (wrong relative depth) and
  one link to a git-ignored log.

## 2026-10-30 — correction throughput recovered

### Fixed
- A transient `503` escalated a paper to a much slower fallback model, which
  could occupy a worker for hours. The default chain now names only the model the
  installation actually marks with, and any explicitly configured fallback gets a
  four-minute, single-attempt rescue budget.
- A "high demand" park retried after two seconds, burning the attempt budget
  during an outage. It now waits at least a minute.

### Added
- `backend/scripts/benchmark_marking_config.py` — compares marking
  configurations on the same papers; thinking-disabled stays opt-in because it
  moved marks on a real paper.
- Measurement of the cause: `docs/results/CORRECTION_PERFORMANCE_RECOVERY.md`.

## 2026-10-29 — quota and retry policy

### Added
- 13-class failure taxonomy with explicit retry/park/fail decisions and jittered
  exponential backoff honouring provider `Retry-After` hints.
- Per-model circuit breaker persisted in `app_meta`, shared between processes.
- Per-correction request budget, content-addressed upload reuse, and the
  diagnostic that records why a job stopped.

## 2026-10-28 — mark scheme review

### Added
- A review page where a teacher decides how each scheme question is handled
  before marking, with the decision recorded on the result.
- Log and diagnostic redaction so bundles cannot ship live API keys.

## 2026-10-26 — packaging and navigation

### Added
- Self-contained Windows installer and portable package, verified end to end.
- People hub combining Classroom, classes and students.

## 2026-10-25 — desktop marking desk

### Added
- Real per-stage progress for a running correction rather than "starting".
- Assignment-first correction flow, per-assignment pages with rosters, and a
  full-size correction dashboard.
- Subquestion marks shown wherever marks are read.

### Fixed
- A large paper timing out, and a weak model being selected for it.
- Marks the answer itself says were not earned.

## 2026-10-22 — 2026-10-24 — native desktop client

### Added
- WPF desktop client with its own marking workspace and theme.
- PDF compression before upload, with an independent page/identity check and a
  bytes-saved report.
- Deprecated Gemini model aliases replaced with the models that actually serve
  them.

## 2026-10-12 — 2026-10-21 — foundation

### Added
- Preserved marking engine, database schema, job queue and worker pool.
- Deterministic sampling, blueprint reconciliation and the "missing question →
  review" guard.
- Google Classroom read-only integration, report generation and CSV/XLSX/PDF
  export.
- The initial pytest suite and the repository structure documented in
  `docs/architecture/PROJECT_STRUCTURE.md`.
