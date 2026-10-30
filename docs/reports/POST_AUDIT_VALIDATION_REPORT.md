# Post-audit validation — MATH_CHECKER_V5

Date: 2026-10-27. Scope: establish a trustworthy baseline for the real
application after the skill/Graphify audit, investigate the unexplained UI
folder, and rank the engineering risks. **No commits, pushes, builds of
release artifacts, grade submissions or destructive operations were performed,
and no production source file was modified.**

Raw evidence (logs, scripts, generated fixtures) is in `.skill-audit-tmp/validation/`.

## 1. Working tree

- `git status --short` → 69 entries before validation, 69 after; 46 tracked
  modifications, all present before this work. Nothing was reset, cleaned,
  overwritten or staged.
- `release/` is **untracked** (0 tracked files) and its `SHA256SUMS.txt`
  matches both artifacts byte for byte:
  `f72c2e54…` (portable zip, 112,483,276 B), `5f2d35ff…` (installer, 81,801,244 B).
- `.skill-audit-tmp/backup/` was left intact.

## 2. Test matrix

| # | Suite | Exact command | Result | Notes |
|---|---|---|---|---|
| 1 | Aggregate (documented) | `.venv/Scripts/python.exe scripts/run_all_tests.py` | **FAIL** — 2/3 checks, 633 s | see rows 2–4 |
| 2 | Frontend build | `npm run build` (in `frontend/`) | PASS 13.9 s | `tsc` + vite, `index-BRP1Xanj.js`, 240.22 kB |
| 3 | Frontend suite | `node --test "tests/*.test.ts"` (in `frontend/`) | PASS 7.8 s | 84 passed, **3 skipped** (browser checks) |
| 4 | Backend suite, run 1 | `.venv/Scripts/python.exe -m pytest backend/tests -q -p no:cacheprovider` | **FAIL** — 2282 passed, **1 failed**, 1 skipped, 610 s | failed: `test_mock_correction_isolation.py::test_a_mock_correction_writes_nothing_outside_its_own_database` (F3) |
| 5 | Backend suite, run 2 (machine idle) | same as row 4 | **PASS** — 2283 passed, 1 skipped, 607 s, **EXIT=0** | failure not reproducible |
| 6 | Failing test in isolation | `pytest backend/tests/test_mock_correction_isolation.py -q` | PASS 5 passed, 11.4 s | |
| 7 | Desktop suite (PATH `dotnet`) | `dotnet test desktop/tests/MathChecker.Desktop.Tests` | **FAIL — exit 155** | `No .NET SDKs were found` — PATH `dotnet` is runtime-only, a condition the release report already documents |
| 8 | Desktop suite (SDK) | `~/.dotnet/dotnet.exe test desktop/tests/MathChecker.Desktop.Tests` | **PASS** — 277 passed, 0 failed, 0 skipped, EXIT=0, 2 m 34 s | |
| 9 | Gemini failure/quota/retry (mocks) | `pytest backend/tests/test_key_manager.py test_key_management.py test_provider_outage.py test_quort_budget.py test_gemini_overload_scoped.py test_retry_architecture.py test_model_health_store.py -q` | **PASS** — 136 passed, 20.3 s | 504 classified as timeout, one retry per 504, failover to another model, retry budget exhaustion, 503 cooldown scoped per model, half-open re-arm |
| 10 | Google Classroom (mocks) | `pytest backend/tests/test_classroom_{auth,error_reporting,tracking,pagination,deletion_sync}.py -q` | **PASS** — 71 passed, 19.4 s | no direct network call in those files |
| 11 | Browser workspace check (live app) | `run_all_tests.py --only frontend --app-url http://127.0.0.1:8750/` | **FAIL** | 27.5 s timeout waiting for the 3-pane workspace (F1) |
| 12 | Installer build | `MathChecker_Distribution/build_release.bat` | **NOT RUN** | stages/overwrites `release/` artifacts — outside the authorised scope |

Warnings seen: one deprecation warning per pytest run
(`StarletteDeprecationWarning: Using httpx with starlette.testclient is
deprecated; install httpx2`). No test-warning failures.

Environmental limitations: row 7 is a PATH/tooling issue, not a code failure;
rows 11/12 as explained; the WPF window itself was not launched (no GUI session
in this environment), so desktop *startup* is evidenced by row 8 plus the
backend's own startup, not by an interactive window.

## 3. Live workflow validation (Demo Mode, synthetic data only)

Started with:
`MC5_DEMO=1 MC5_NO_BROWSER=1 MC5_DATA_DIR=.skill-audit-tmp/demo-base python main.py 8750`

Demo Mode's guarantees are structural (own data dir, Classroom hard-disabled,
mock engine, synthetic students), so the real `data/` was never opened.

| Workflow | Evidence | Result |
|---|---|---|
| Startup + connectivity | banner `Math Checker v5.2.1 (build 20261013-rc1)`; relay on 8750 + supervised backend; `GET /api/health` → 200 | PASS |
| Data isolation | new data dir `.skill-audit-tmp/demo-base/demo`; 8 seeded synthetic results (`Student A`…`Student E`) | PASS |
| Route surface | `/openapi.json` → 238 paths (135 GET); 69 read-only parameter-free GETs called: **42 × 200**, 12 × 401, 8 × 403, 4 × 422, 2 × 400 | PASS |
| Auth/authz on live routes | `/api/distributed/*` → 401/403 (their own bearer auth), `/api/diagnostics` → 403 (developer-only), `/api/auth/me` → 401 (demo needs no sign-in) | PASS — refusals are correct, not defects |
| PDF upload/parsing — valid | 1-page synthetic → 200, `page_count: 1` | PASS |
| PDF — empty | 0 bytes → 400 `corrupt_pdf` "the file is empty" | PASS |
| PDF — not a PDF | text bytes named `.pdf` → 400 `corrupt_pdf` "does not contain a PDF header" | PASS |
| PDF — truncated | header-only PDF → 400 `corrupt_pdf` "not a readable PDF" | PASS |
| PDF — wrong type | `.txt` → 400 `bad_file_type` | PASS |
| PDF — large (size) | 51 MB → 413 `file_too_large` on `/api/corrections/manual` (50 MB guard); 51 MB → 400 `corrupt_pdf` on `/api/corrections/unified` (200 MB guard) — both correct for their own limit (F2) | PASS (inconsistency noted) |
| PDF — large (pages) | valid 150-page PDF (79,657 B) → 200, `page_count: 150` | PASS |
| Correction request e2e | `POST /api/corrections/unified` → 200 `job_id`; result created | PASS |
| Progress reporting | observed `QUEUED 0.0` → `COMPLETED 1.0`, `result_id` set, `error: null`, 1.7 s; `/api/jobs` is the polling surface | PASS |
| Cancellation | `POST /api/jobs/{id}/cancel` → 409 "not active or unknown" for a finished job and for an unknown id; a successful cancel could not be observed (mock job finishes in 1.7 s) | PARTIAL |
| Mark calculations / reports | `/api/reports/build` → 200, `CLASS REPORT`, 9 sections; result pages endpoint honestly reports `available:false`, `reason:"retention_disabled"` | PASS |
| Classroom safety in demo | `/api/classroom/courses` → 400 `demo_mode` refusal; status `connected:false`, `write_draft_grades:false` | PASS |
| Gemini/quota in demo | engine mocked, 0 keys configured — no real call possible | not applicable; covered by row 9 |

## 4. Findings, ranked

### F1 — the three browser checks cannot pass as documented, and a reachable app turns them into a FAIL (Important, confirmed)
- `frontend/tests/workspaceLayout.test.ts` and `workspaceDrag.test.ts` open
  `/#/results/${WORKSPACE_RESULT_ID}`, default `r-ui-test`.
- `git grep "r-ui-test"` finds the id **only in those two files and their header
  comments** — no seed script, fixture or documented step creates it.
- Repointing at a real result (`WORKSPACE_RESULT_ID=demo-result-1`) still timed
  out after 26.6 s waiting for the 3-pane workspace.
- The skip condition (`workspaceLayout.test.ts:222-228`) covers only "no browser
  binary" and "app not reachable", although the header promises a skip when
  "the app running with a seeded result" is missing. So a reachable app without
  the fixture produces a hard FAIL rather than an honest "not run".
- Because the default run never starts the app, the suite reports
  *PASS 84 passed, 3 skipped* and the gap stays invisible until someone follows
  the documented instruction to start the app.
- Root cause: the fixture that produced `r-ui-test` (a result with retained
  submission pages) is not in the repository, and "fixture result absent" is not
  a recognised not-run state.
- Smallest safe fix (test-side, ~10 lines): probe the result id first and extend
  `skipReason` with "fixture result not present" so the check reports *not run*;
  restoring the seed script separately is what regains real coverage. Neither
  touches product code or the 46 modified files.

### F2 — two sibling correction endpoints accept different maximum sizes (Important, confirmed behaviour; intent unconfirmed)
- `/api/corrections/manual` → 50 MB (`routes_all.py:781`), observed `413 file_too_large`.
- `/api/corrections/unified` → 200 MB (`routes_all.py:844`), 4× larger, observed accepted.
- The unified docstring says "Upload, persistence and queueing are unchanged",
  which suggests the two were meant to match.
- Neither 413 path is covered by any test (`grep` for `file_too_large`/`413` in
  `backend/tests` finds nothing).
- Related robustness note: both routes do `content = await upload.read()` and
  only then check the size, so a request peaks at the full body size in memory
  (submission + mark scheme; contracturally up to ~400 MB on the unified route).
  Requiring a session in public mode limits who can do it, but a streaming cap
  would be cheaper than a post-hoc check.
- Smallest safe fix: one shared module-level constant used by both routes, plus
  one test per route asserting 413. Which number is "right" is your call.

### F3 — one non-reproducible backend failure: the isolation test asserts global table emptiness (Medium, confirmed symptom)
- Run 1 failed `test_a_mock_correction_writes_nothing_outside_its_own_database`
  while the frontend build and my inspection ran concurrently; run 2 on an idle
  machine passed 2283/2283 (EXIT=0); in isolation the file passes 5/5.
- The test asserts five tables are *globally* empty after `run_correction`
  (lines 128-136), so any row another test or the app's own background
  automation writes during that window fails it — a timing/order dependency,
  not necessarily a product bug. **Not reproducible, so the root cause is a
  hypothesis**, stated as such.
- Smallest safe fix (3 lines): snapshot the tables before `run_correction` and
  assert no *new* rows, which is what "a mock correction writes nothing" means
  and is immune to other tests' leftovers.

### F4 — the shipped artifacts do not match the current working tree (Medium, confirmed)
- `release/release_manifest.json`: `builtAt 2026-10-26T21:42:42Z`, version
  `1.0.0`, staged 258.5 MB, built on `ARTHUR_RIP_002`.
- `docs/release/RELEASE_REPORT.md` states the desktop suite as "265 passed"; the tree
  now has **277**.
- The tree carries 46 modified files plus new untracked features/tests
  (`CreateAssignmentView`, review reasons, scheme exclusions, the D1 budget fix).
- Artifacts are intact (hashes verified) but predate that work, so installing
  `MathChecker-Setup-x64-1.0.0.exe` today gives the pre-audit build.
- Fix requires rebuilding (publishes/overwrites artifacts) → needs your go-ahead.

### F5 — cancellation cannot distinguish "too late" from "wrong id" (Low, confirmed)
- Both a completed job and an unknown id return the same 409
  "Job cannot be cancelled (not active or unknown)".
- A 404 for unknown ids (or a distinct "already finished" body) would make the
  client's message truthful. Successful cancellation is covered by the suite,
  not by this run, because a demo mock correction finishes in 1.7 s.

### F6 — carried over from the previous audit (Low)
- No CI workflow, so no automated dependency-vulnerability scan.
- `data/secrets.toml` is mode `0644`; on Windows NTFS ACLs govern, so this is
  only worth tightening for a POSIX deployment.

## 5. The unexplained folder `47743487-1851-4fbd-a2dc-46f3a3127a6b/`

**What it is:** a complete, unrelated Freebuff Sites application — README "Study
app" / "A flashcard study app in the spirit of Quizlet" (`BRAND` in
`src/shared/types.ts`, browser title "Recall"). Bun + Vite + React 19 + Hono on
Cloudflare Workers, with D1 (`DB`) and R2 (`FILES`) bindings and
`@freebuff/auth`; pages `Library`, `Deck`, `Flashcards`, `Learn`, `Match`,
`Stats`, `Settings`, `SignIn`; its own `bun.lock`, `wrangler.jsonc`,
`deploy.json`, `.dev.vars.example`, `scripts/postbuild.mjs`, 466 MB
`node_modules`.

**Evidence it is not part of MATH_CHECKER_V5:**

| Check | Result |
|---|---|
| `git log --all -- <path>` | empty — never committed |
| `git grep` for the UUID / `study-files` / `wrangler` in tracked files | nothing |
| root `package.json` / workspace file | none exists |
| its `.freebuff/project-id` | `83b30012-bd85-470e-a554-7756de17f231` — a **different** Freebuff project from this repo's `79baf723-38d0-4c20-abd6-ea03a794125e` |
| comparison with `frontend/` | `math-checker-v5-frontend` v5.0.0, React **18.3**, purpose = math-checker dashboard; the orphan is React **19**, purpose = flashcards. Different product, stack and framework major → duplicates or replaces nothing |
| local run state | `.wrangler/state/v3/d1/miniflare-*.sqlite` + cache → it was **run** locally (Miniflare), so it is live work, not an export or debris |

**What each disposition would mean:** building it needs `bun install` then
`bun run build` (output `dist/`), independent of this repo; ignoring it in
git/graphify only changes indexing; removing it deletes another project's only
local copy plus 466 MB of `node_modules`.

**Recommendation: move it out of this repository** — e.g. to
`C:\Projects\study-47743487-1851-4fbd-a2dc-46f3a3127a6b\` — since its UUID name
and embedded `.freebuff` project id are its real identity. If it stays, add it
to `.gitignore` (a plain `git add -A` would otherwise commit 466 MB) and to
`.graphifyignore`. Nothing was modified or deleted; both ignore edits are held
pending your decision because they would be pointless if you move it.

## 6. Graphify status

- **Tracked graph files:** none yet (`git ls-files graphify-out` → 0).
  `graph.json` + `GRAPH_REPORT.md` are the two committable files by policy; the
  cache, manifest, `.graphify_*`, `memory/` and `*.html` are gitignored.
- **Currency:** `built_at_commit` == `HEAD` (`800129f`) and no source file
  changed after the build → current. `frontend/dist` (1.5 MB, rebuilt during
  this validation) and `desktop/**/bin|obj` are gitignored and contribute **0**
  graph nodes.
- **Misleading nodes from the orphan folder: yes.**
  - 248 nodes / 701 edges (1.4 % of the graph), 30 files, across 5 communities;
  - **Community 56** is a 42-node community named
    `47743487-1851-4fbd-a2dc-46f3a3127a6b/package.json` whose members are npm
    dependency labels (`react@^19.2.0`, `lucide-react@^1.60.0`, …);
  - `GRAPH_REPORT.md` "Surprising Connections" lists two **INFERRED** edges from
    this project's own UI/UX docs to the orphan's components:
    `docs/ui-ux-redesign/complete-redesign-blueprint.md → …/src/app/components/ui.tsx`
    (`EmptyState()`) and `component-and-interaction-system.md` → same file
    (`Menu()`). An agent asking the graph about this app's UI component system
    can be sent into a flashcard app.
  - The authoritative ranking is still clean: `god-nodes --top 20` contains only
    project hubs (`execute()`, `query_one()`, `get_config()`, `AppError`,
    `MathCheckerApiClient`, `log_event()`, …).
- **Documented refresh procedure:** first build `graphify extract . --code-only`
  (AST only, no API key); incremental after code changes `graphify update .` —
  also prunes files that became ignored or deleted (observed: "pruned 81 node(s)
  from 12 newly-ignored file(s)"); re-cluster/report `graphify cluster-only .`;
  community names via `graphify label .` (needs an LLM backend). `AGENTS.md`
  carries the `graphify update .` rule.
- **Hooks:** none are installed (no active git hooks, nothing in git config or
  `.claude/settings*.json`), and `AGENTS.md` now states that explicitly — no
  automatic update hook is falsely documented.
- No graph files were regenerated in this validation pass: the only change worth
  making (excluding the orphan) depends on the decision in §5.

## 7. Top three recommended engineering tasks

1. **Resolve the orphan folder** (move it out, or add the two ignore lines and
   run one `graphify update .`). Highest risk-to-effort ratio: a 466 MB
   accidental commit, plus two misleading INFERRED links from the project's own
   design docs into unrelated code.
2. **Make the test harness honest and the size limits consistent** (F1 + F2):
   skip the browser checks when the fixture result is absent
   (and/or restore the `r-ui-test` seed), and give the two correction routes one
   shared upload limit with a 413 test each. Entirely test-side, no product
   behaviour change.
3. **Turn the suite into a reliable gate, then refresh the release** (F3 + F4):
   make the mock-isolation assertion a delta, then rebuild the installer and
   update `docs/release/RELEASE_REPORT.md`'s counts (265 → 277 desktop tests) so shipped
   artifacts match the tree. The rebuild needs your explicit go-ahead because it
   overwrites `release/`.

Low-risk fixes that can be implemented independently without conflicting with
the existing user changes: all of item 2 and the F3 delta assertion — they touch
only `frontend/tests/*`, `backend/tests/test_mock_correction_isolation.py` and
the two ignore files, none of which appear in the 46-file modification list.
They were **not** applied here, per the instruction to deliver the report first.
