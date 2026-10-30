# Repository cleanup report — 2026-10-30

What was found is in [REPOSITORY_AUDIT.md](REPOSITORY_AUDIT.md). This document
records what changed, what was verified afterwards, and what is still open.

No application feature, marking prompt, grading rule, model setting or
performance policy was changed. The only production-code change in the tree is
the performance-hardening work from the preceding pass, which was reviewed and
committed separately.

---

## 1. Before and after

| | Before | After |
|---|---|---|
| Tracked files | 4 470 | **4 475** (9 untracked, 14 added by this and the hardening pass) |
| Machine-local state tracked | 4 files (`agent/device_id`, `agent/agent.lock`, `agent/agent_state.json`, `autostart/service_install.json`) | none |
| Superseded build output tracked | 5 files, 124 MB (`previous_builds/20261016-193501/`) | none |
| Stale `*.log` files at the repository root | 20 | 0 |
| Regenerable caches in the checkout | `__pycache__/`, `.pytest_cache/`, `.skill-audit-tmp/` (8.2 MB) | none |
| Markdown files at the `docs/` root | 18 flat files | 0 (all grouped, `README.md` remains as the index) |
| Broken local documentation links | 16 across the repo (10 after excluding agent tooling) | **0 of 253** |
| Root README | 545 lines organised by delivery phase | rewritten for a first-time reader, 11 sections |
| Changelog / contributing guide | none | `CHANGELOG.md`, `CONTRIBUTING.md` |
| Documentation link check | none | `scripts/check_doc_links.py` |

---

## 2. Structure changes

### Documentation hierarchy

```
docs/
├── README.md            the map: where to look and why
├── architecture/        ARCHITECTURE · PROJECT_STRUCTURE · SERVER_LIFECYCLE
├── development/         WINDOWS_SETUP · DEVELOPMENT_ENVIRONMENTS ·
│                        SKILL_AUDIT_REPORT · DISTRIBUTED_DEPLOYMENT (was at the root)
├── guides/              CLASSROOM_SIGNIN · MARK_SCHEME_SUBQUESTIONS ·
│                        SUBMISSION_RETENTION · UI_IDENTITY_V6
├── performance/         API_QUOTA_AND_RETRY · PDF_COMPRESSION
├── testing/             subsystem audits (existing)
├── release/             RELEASE_DEPENDENCIES · RELEASE_INSTALL · RELEASE_REPORT
├── reports/             dated audit and delivery reports (existing + 2 moved in)
└── results/             machine-readable verification output (existing)
```

Files were moved with `git mv`, so their history follows them, and every
reference was rewritten in the same pass. Nine paths also gained consistent
names (`development-environments.md` → `DEVELOPMENT_ENVIRONMENTS.md`,
`pdf-compression.md` → `PDF_COMPRESSION.md`, and so on).

Moves were verified, not assumed: `scripts/check_doc_links.py` resolves every
local link relative to the file that contains it and fails on a missing target.
It found 16 pre-existing broken links before the reorganisation and 0 after.

### Root README

The old README was an accurate but chronological document for the people who
built the project. The new one answers, in order: what this is and who it is for,
what works today, how it is put together (including a Mermaid architecture
diagram), what it is built with, where everything lives, how to run it, how it is
tested, what the performance work found, how student data is handled, what is
**not** finished, and which screenshots still need capturing.

Every number in it is traceable to a dated run or a stored artefact, and the two
claims that cannot be substantiated (teacher-certified accuracy, grade write-back)
are stated as limitations rather than features.

### `.gitignore`

- Added the four machine-local state files and `agent/events_queue.jsonl`.
- Added local scratch and agent tooling: `/.tmp/`, `/.agents/`, `/.claude/`,
  `/.opencode/`.
- Added `graphify-out/` (regenerable, ~20 MB rewritten whole per rebuild).
- **Anchored** `release/`, `dist/`, `logs/` and `tmp/` to the repository root.
  They were unanchored, so `MathChecker_Distribution/release/` — the finished
  package that is *meant* to be committed — was ignored by a rule aimed at the
  root folder, and the bundled runtime's vendored `.agents/` folders matched the
  new tooling rule.

---

## 3. Git changes

Three commits, each independently coherent:

1. **Repository hygiene** — untrack the machine-local state and the superseded
   build copy; anchor the ignore rules.
2. **Performance hardening** — the backend retry/timeout/watchdog work, its tests,
   its scripts and its reports (reviewed in full before committing; the diff is
   the one described in `docs/results/PERFORMANCE_HARDENING_REPORT.md`).
3. **Documentation and presentation** — README, documentation hierarchy, link
   fixes, changelog, contributing guide, the audit and this report, and the link
   checker.

Deliberately **not** committed, and left in the working tree:

- **Desktop UI and theme refactor in progress** (30+ XAML files, a new
  `ClickMenu.cs`, a removed `KitTokens.xaml` and a removed token-sync script).
  These edits appeared *during* this session from another editor; they are not
  this cleanup's work, they are not described by any report here, and committing
  somebody's half-finished refactor is not safe.
- **`47743487-…/`** — a stray 471 MB starter-template project, unrelated to
  MathChecker (details in the audit).
- **`.freebuff/project-id`** and **`AGENTS.md` / `.graphifyignore`** — tool
  markers and agent instructions for the editor used here, not project source.

No pre-existing history was rewritten, nothing was force-pushed, no branch was
deleted, and nothing was pushed to the remote. The three commits above were
written in this session and regrouped once before finishing, while still
unpushed, so that each one contains exactly the change its message describes;
the project's earlier commits were not touched.

---

## 4. Validation after cleanup

All commands were run from the repository root on the cleaned tree.

| Check | Command | Result |
|---|---|---|
| Backend suite | `.venv/Scripts/python.exe -m pytest backend/tests -q -p no:randomly` | **2 555 passed, 1 skipped**, 12 m 11 s, exit 0 |
| Desktop build + tests | `dotnet test desktop/MathChecker.Desktop.slnx -c Release` | **335 passed**, 0 failed |
| Frontend production build | `cd frontend && npm run build` | **exit 0**, `dist/` emitted in 2.4 s |
| App imports and starts | `uvicorn app.main:app` on a scratch data dir | started, **`GET /api/health` → 200**, stopped by PID afterwards |
| API routes registered | same process | 28 routes, 6 under `/api` (plus the mounted routers) |
| Documentation links | `python scripts/check_doc_links.py --strict` | 103 files, **253 links, 0 broken** |
| Secret scan | tracked content + all 60 commits | no real credential; see §5 |

### Things worth knowing about those results

- **The backend suite result is from this tree**, after the `.gitignore`,
  documentation and hygiene changes. It includes the fix that made the
  request-ledger leak between tests disappear.
- **The desktop suite was run against a tree that also contains the concurrent
  UI refactor mentioned in §3.** It passes (335/335) *with* those edits present.
  This cleanup changed no desktop code, so that result is a statement about the
  working tree, not about the files in the commits above.
- **A stale test assertion was found and diagnosed, not silenced.** One
  `WorkerDashboardTests` case compared the rendered page against ALL-CAPS
  headings that exist only in pre-rewrite binaries (verified by searching the
  built assemblies: `Release` contains `Name it and start`, the stale `Debug` and
  `artifacts` copies contain `NAME IT AND START`). It passed or failed depending
  on which binary was loaded. The corresponding assertion updates were already
  present in the working tree when this was diagnosed, and they belong to the
  concurrent editor; this pass neither authored nor reverted them.
- **A leftover `testhost.exe` process** was holding the test assembly and
  breaking `dotnet test` copies. It was killed by PID; no build output was
  deleted to work around it.
- The desktop `Debug` configuration cannot run tests on this machine at all:
  Windows Application Control blocks the freshly built `V5.dll`
  (`0x800711C7`). `Release` works. Reported as an environment limitation in
  `docs/results/RELIABILITY_TEST_REPORT.md`.

---

## 5. Security observations

- No real credential exists in tracked content or in any of the 60 commits: the
  only Google-key-shaped strings are synthetic test fixtures with repeated
  characters, `data/`, `.env`, `NEW_API_KEYS.txt` and `client_secret_*.json` were
  never committed, and the private-key "hits" are string constants inside
  vendored libraries.
- `agent/device_id` (a random device UUID, not a credential) was public in
  history. It is now untracked; nothing needs rotating.
- No history rewrite was performed, so the two oversized package commits remain
  in `.git` (~644 MB). Trimming them would require a force-push; that is the
  owner's decision.

---

## 6. Remaining issues

1. **Repository size.** `.git` is ~644 MB and the tracked tree includes a ~300 MB
   bundled Python runtime. This is a deliberate product decision (the repository
   ships a runnable Windows package), but it is heavy for a portfolio. The
   cheapest improvement is to move `MathChecker_Distribution/release/*.zip|*.exe`
   to GitHub Releases and untrack them; that is not done here because it changes
   how releases are distributed.
2. **No screenshots.** Deliberate: live screens contain student names. The README
   lists the six screenshots to capture from offline demo mode.
3. **No CI.** Every result above is a local run. A single GitHub Actions workflow
   running `pytest`, `dotnet test -c Release`, `npm run build` and
   `check_doc_links.py --strict` would make the badges and the claims
   self-verifying.
4. **Concurrent desktop refactor** is uncommitted and undescribed by these
   reports; it needs its own review and commit when it is finished.
5. **Desktop `Debug` tests remain blocked** by the machine's application-control
   policy.
6. **`docs/reports/` is a historical archive** with mixed naming (`ALL_CAPS.md`
   next to `kebab-case-dated.md`). It is labelled as historical in
   `docs/README.md`; a future pass could rename for consistency, but renaming 28
   reports for cosmetics was not worth the link churn today.
