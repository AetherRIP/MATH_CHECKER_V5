# Repository audit — 2026-10-30

Scope: the whole MathChecker V5 checkout, before any cleanup. This document
records what was found, what was changed and what was deliberately left alone.
The work itself is summarised in
[REPOSITORY_CLEANUP_REPORT.md](REPOSITORY_CLEANUP_REPORT.md).

Method: `git ls-files`, `git status`, `git ls-files -ci --exclude-standard`
(tracked-but-ignored files), `du`, direct inspection of every suspicious file and
directory, one secret scan over tracked content and over every commit, and the
repository's own cleanup policy in
[docs/architecture/PROJECT_STRUCTURE.md](../architecture/PROJECT_STRUCTURE.md).

---

## 1. What the repository looked like

4 470 tracked files, 60 commits (2026-10-12 → 2026-10-30), one branch plus three
merged/feature branches.

| Top-level path | Tracked files | Notes |
|---|---|---|
| `MathChecker_Distribution/` | 3 713 | The portable Windows packaging workspace, including a bundled Python runtime in `build/` |
| `backend/` | 323 | Application, schema, tests, scripts |
| `desktop/` | 200 | WPF client, tests, installer, build script |
| `docs/` | 109 | Architecture, guides, reports, results, evidence |
| `frontend/` | 91 | React + TypeScript UI |
| `scripts/`, `autostart/`, `agent/`, root files | 34 | Launchers, packaging tooling, sync agent |

Largest local directories (working tree, not all tracked): `data/` 1.9 GB
(student data + archives, git-ignored), `desktop/` 655 MB (build output),
`.git/` 644 MB, `MathChecker_Distribution/` 471 MB, a stray
`47743487-…/` directory 471 MB, `dev_local/` 399 MB, `.venv.foreign/` 285 MB,
`release/` 186 MB, `frontend/` 109 MB, `graphify-out/` 71 MB.

---

## 2. Problems found

### P1 — Machine-local runtime state was tracked in git (fixed)

| File | Content | Why it does not belong |
|---|---|---|
| `agent/device_id` | 32-byte device UUID | Per-machine identity. Cloning the repo cloned somebody's device identity; the release allowlist already excludes it from shipped builds. |
| `agent/agent.lock` | A process ID | Runtime lock file, meaningless (and stale) in source control. |
| `agent/agent_state.json` | Agent enabled/paused state plus an account id | Runtime state; test runs rewrote it, leaving the checkout permanently dirty. |
| `autostart/service_install.json` | Windows autostart receipt with absolute paths (`E:\MATH_CHECKER_V5\…`) | Machine-specific; also disclosed a local filesystem layout. |

All four are recreated on demand (`tiny_agent.device_identity` writes
`device_id` when it is missing, `agent_control` treats a missing state file as
the default). They are now untracked and ignored; the files remain on the
machine that produced them.

### P2 — Superseded build output was tracked (fixed)

`MathChecker_Distribution/previous_builds/20261016-193501/` held a tracked copy
of an older portable package: a 73 MB ZIP, a 51 MB installer and three metadata
files. `.gitignore` explicitly excludes `previous_builds/` and the file itself
says keeping such copies is what once grew `.git` past 1 GB. Tracking it meant
every future build would add another 124 MB diff. Untracked (files kept locally).

### P3 — `.gitignore` rules were unanchored (fixed)

`release/`, `dist/`, `logs/` and `tmp/` had no leading slash, so they matched
*nested* directories as well. `MathChecker_Distribution/release/` — the finished
package that is meant to be committed — was being ignored by a rule aimed at the
root `release/` folder, and `MathChecker_Distribution/build/runtime/…/.agents/`
matched the new agent-tooling rule. Rules are now anchored to the root.

### P4 — Twenty stale log files at the repository root (removed)

`audit-baseline-backend.log`, `backend-suite.log`, `desktop-suite*.log`,
`e2e.log`, `final-*.log`, `mission-baseline.*.log`, `probe.log`,
`pytest-*.log`, `ui-audit.log`. All git-ignored, none referenced by code or
documentation; verification evidence lives in `docs/results/` and
`docs/reports/`.

### P5 — Regenerable caches and a temporary audit workspace (removed)

`__pycache__/`, `.pytest_cache/` and `.skill-audit-tmp/` (8.2 MB: an earlier
skill-audit scratch area with demo copies and a link checker). Inspected before
deletion; nothing in them is referenced by the build, the tests or the docs.

### P6 — Nine broken documentation links and one dead log link (fixed)

`docs/testing/api-subsystem-audit.md` and
`docs/testing/full-stack-stress-and-memory-audit.md` linked to `../backend/…` and
`../desktop/…`, one level short after an earlier move, and
`docs/reports/REPORT_GENERATION_AUDIT.md` linked to a git-ignored build log. All
fixed; a link checker now exists so the next move cannot break them silently.

### P7 — Documentation had no hierarchy (fixed)

Eighteen Markdown files sat flat in `docs/`, next to `reports/`, `results/`,
`testing/`, `evidence/`, `size_optimization/`, `desktop-migration/` and
`ui-ux-redesign/`. Architecture, setup, feature guides, performance notes and
release instructions were indistinguishable. They are now grouped into
`architecture/`, `development/`, `guides/`, `performance/`, `release/`,
`reports/` and `results/`, with `docs/README.md` as the map.

### P8 — Root README was a development log (fixed)

545 lines organised around delivery phases ("Productisation (5.1)", "Teacher
experience… (5.2)"), which reads as a changelog for the people who built it
rather than an introduction for someone arriving fresh. Rewritten around what
the project does, how it is built, how to run it, what is verified and what is
not.

### P9 — Test isolation defect exposed by the suite (fixed)

A full run failed two tests that pass in isolation with
`ProviderBudgetExceeded: … budget of 12 AI requests`. The per-correction request
ledger is process-global and was never reset between tests, so a later test
inherited a spent budget. Reset per test, like the other process-global
singletons. (This is the same class of bug the repository had already fixed for
the circuit breaker and the concurrency controller.)

### P10 — Desktop suite blocked by the operating system (not a code defect)

A `Debug`-configuration run failed all 320 tests with
`Application Control policy has blocked this file (0x800711C7)` for the freshly
built `V5.dll`. The same suite in `Release` passes 335/335. Reported, not worked
around: the machine needs the build folder allow-listed for `Debug` runs.

---

## 3. Files removed

| Path | Size | Reason |
|---|---|---|
| `__pycache__/` | 60 KB | Regenerable bytecode |
| `.pytest_cache/` | 276 KB | Regenerable test cache |
| `.skill-audit-tmp/` | 8.2 MB | Temporary audit scratch, nothing references it |
| 20 `*.log` files at the root | ~60 KB | Superseded local transcripts (ignored by git) |
| `MathChecker_Distribution/previous_builds/20261016-193501/` (untracked, kept on disk) | 124 MB | Tracked build output contradicting the documented policy |
| `agent/device_id`, `agent/agent.lock`, `agent/agent_state.json`, `autostart/service_install.json` (untracked, kept on disk) | < 2 KB | Machine-local runtime state |

No source file, test, fixture, document, benchmark result or release artefact
was deleted.

---

## 4. Files and directories deliberately preserved

| Path | Why | Note |
|---|---|---|
| `MathChecker_Distribution/build/` (3 677 files) | The bundled portable Python runtime is intentional: the repository ships a runnable Windows package, and `.gitignore` has a documented negation for its `.pyd` files | ~300 MB; unusual for a portfolio repo but a deliberate product decision |
| `MathChecker_Distribution/release/` (tracked ZIP + installer) | The finished package recipients download; explicitly allowed by the ignore policy's comment | If repository size becomes a problem, move these to GitHub Releases — not done here |
| `docs/results/`, `docs/reports/`, `docs/evidence/`, `docs/size_optimization/`, `docs/desktop-migration/` | Performance recovery, benchmark, reliability and audit evidence with measured numbers | Historical, dated, and labelled as such |
| `data/` | Student data and the live database | Git-ignored; never touched |
| `dev_local/` | Development scratch that contains preserved release-audit copies | Git-ignored; contents not inspected beyond the documented purpose |
| `graphify-out/` (71 MB) | The code knowledge graph used by the development tooling | Now ignored: regenerable with `graphify update .` |
| `.agents/`, `.claude/`, `.opencode/` | Agent tooling installed on this machine | Now ignored: third-party skill text, not project source |
| `.githooks/pre-commit` | A repository hook that exists and is referenced | Kept; not installed automatically |
| `NEW_API_KEYS.txt` (untracked) | Live credential drop file | Correctly git-ignored; never committed (verified in §5) |

### Preserved but flagged for the owner

- **`47743487-1851-4fbd-a2dc-46f3a3127a6b/` (471 MB, untracked).** A stray
  starter-template project (Bun, Vite, Wrangler, `deploy.json`, `node_modules`)
  that has nothing to do with MathChecker. It looks like a template scaffold
  created inside the checkout by another tool on 2026-10-24. Left in place
  because deleting 471 MB of somebody else's files is not a cleanup decision an
  agent should make unasked. Safe to delete if it is not wanted.
- **`dev_local/` (399 MB)** and **`release/` (186 MB)**: git-ignored working
  copies. The documented policy says to keep at most two generations of build
  output; this audit did not delete any of them.
- **`.freebuff/project-id`**: tool marker for the editor that produced this
  session. Left as is.

---

## 5. Security observations

No credential was found in tracked content, and none is reproduced in this
document.

| Check | Result |
|---|---|
| Google API keys in tracked files | 5 matches, all synthetic test fixtures (repeated-character bodies such as `…AAAA`, `…000`) used by privacy and API-key tests |
| Google API keys anywhere in the 60-commit history | 10 distinct strings, all traceable to those same fixtures; no random-entropy key |
| Private-key blocks | Only string constants inside vendored libraries (`google-auth`, `cryptography`) and a marker inside `cloudflared.exe` — not key material |
| OAuth client files (`client_secret_*.json`) | Never committed (`git log --all -- …` returns nothing) |
| `.env`, `*_API_KEYS.txt`, `NEW_API_KEYS.txt` | Never committed |
| `data/` (database, `secrets.toml`, student PDFs, logs) | Never committed |
| Machine identifiers | `agent/device_id` was tracked (see P1) — a device UUID, not a credential; now untracked |

Recommended follow-up, for completeness rather than urgency: because
`agent/device_id` was public in history, treat the device identity of the machine
that produced those commits as known. It is a random UUID with no credential
value, so nothing needs rotating; rotating it is a one-line deletion of
`agent/device_id` on that machine.

`.gitignore` was reviewed against the usual categories (Python, Node, .NET, IDE,
OS, local databases, logs, secrets) and already covered them. The changes made
were the four missing machine-state files, the new local-scratch entries, and the
anchoring fix in P3.

---

## 6. What was not done, and why

- **No Git history rewrite.** The two oversized package commits and the tracked
  device id remain in history. Removing them would need a force-push and would
  invalidate every existing clone; that is the owner's decision, not a cleanup
  side effect.
- **No deletion of `MathChecker_Distribution/build` or `release`.** Both are
  deliberate product decisions documented in the repository.
- **No screenshot capture.** The UI shows real student names; screenshots must
  come from the offline demo mode, and the README lists the set to capture.
- **No feature, marking-behaviour or model-configuration changes.** Performance
  policies were left exactly as the previous pass set them.
