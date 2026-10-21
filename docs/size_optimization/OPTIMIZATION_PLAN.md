# OPTIMIZATION_PLAN — what can be removed, what must not be

Derived from `BASELINE.md`. Ordered by value ÷ risk. Every row states the
classification (audit brief A–F), the expected saving and how the item is
recovered if the decision later needs reversing.

---

## 0. Principles applied

1. **Size is never bought with correctness.** No correction logic, verification
   pass, retry, question-recovery step, test or fixture is touched.
2. **Only generated or duplicated data is removed.** Anything a human uploaded
   (`data/`) stays exactly where it is.
3. **Everything removed is recoverable from git.** The working tree was clean and
   every candidate is tracked at `HEAD` (`e0b3e33`), so `git checkout -- <path>`
   restores it byte-for-byte.
4. **No history rewrite, no commit, no push.** History reduction is *proposed*,
   with the exact commands, but not executed.
5. **Offline behaviour is preserved.** The current shippable package
   (`release/MathChecker_Portable.zip`) and the bundled runtime that makes a
   rebuild possible without a network remain in place.

## 1. What is DEFINITELY safe to remove (this pass)

| # | Item | Bytes | Class | Why it is safe | Reversal |
|---:|---|---:|---|---|---|
| 1 | `MathChecker_Distribution/portable/` | 200,435,305 | C | Byte-identical to the contents of `release/MathChecker_Portable.zip` — verified by CRC32 + size over all 3,836 entries (`BASELINE.md` §6). It is a pipeline working copy that `build_release.py` wipes and rewrites. | `git checkout -- MathChecker_Distribution/portable` |
| 2 | `MathChecker_Distribution/release_staging/` | 200,781,895 | C | The staged allowlisted copy the packager consumes; "cleaned each build" per `MathChecker_Distribution/README.md`. Contains no source that is not also in the app. | `git checkout -- …/release_staging` |
| 3 | `MathChecker_Distribution/previous_builds/original_20261012/` | 329,607,594 | C | Superseded build. The documented retention policy is "keep at most the two most recent previous builds"; this is the *oldest* of three and its two successors exist. | `git checkout -- …/previous_builds/original_20261012` |
| 4 | `MathChecker_Distribution/previous_builds/20261016-120509/` | 129,721,133 | C | Superseded build (older of the two dated ones). The newer `20261016-193501` is **kept**, so `backend/scripts/release_update_check.py` (which needs one previous package) still works. | `git checkout -- …/previous_builds/20261016-120509` |
| 5 | `.venv.foreign/` | 77,104,578 | D | A **Linux** virtualenv (`pyvenv.cfg`: `home = /usr/bin`, `version 3.14.7`, created at `/home/rudransha/…`) that `setup.bat` moved aside on Windows. Its shebangs are already dead after the move; nothing in the repo references it; it cannot run on this host. Regenerable with `python3 -m venv` under WSL. | recreate with `python3 -m venv .venv.foreign` |
| 6 | `__pycache__/`, `.pytest_cache/`, `backend/.pytest_cache/`, `MathChecker_Distribution/scripts/__pycache__/`, `*.pyc` | ~13,074,000 | C | Bytecode and test caches; regenerated on first run. Never shipped (allowlist denies `__pycache__`, `.pytest_cache`, `*.pyc`). | automatic |
| 7 | `git gc` (pack the 317 loose objects, 368.8 MiB) | ~150–350 MB | F | Repacks existing objects; **does not rewrite history**, drops no reachable commit (no stash, no extra refs). | n/a (no content change) |

**Expected total: ~950 MB before git repacking, ~1.1–1.3 GB after.**

## 2. Deliberately KEPT (and why)

| Item | Bytes | Class | Reason |
|---|---:|---|---|
| `MathChecker_Distribution/release/` (`MathChecker_Portable.zip`, `MathChecker_Setup.exe`, `release_manifest.json`, `SHA256SUMS.txt`, `MathChecker.iss`) | 129,780,182 | C (deliverable) | This is the artifact a teacher actually receives. Deleting it would mean "no distributable until a rebuild", and a rebuild needs internet + Inno Setup 6. Offline delivery capability is a product feature, not clutter. |
| `MathChecker_Distribution/build/` (bundled Python runtime + `cloudflared.exe` + embeddable-Python zip) | 250,365,462 | B | Two reasons. (a) It is the pipeline's `--with-runtime` input, so a release can be rebuilt without downloading a runtime. (b) `backend/tests/test_release_runtime_integrity.py` asserts that **the runtime committed in git contains its `*.pyd` modules** — the regression that once shipped packages which could not `import socket`. Deleting the only committed runtime would make that guard vacuous. Size is not worth losing that guard. |
| `MathChecker_Distribution/previous_builds/20261016-193501/` | 129,769,077 | C | One rollback package + the baseline that `backend/scripts/release_update_check.py` update-tests against. |
| `data/` (database, mark schemes, purge backups, extracted PDFs, audit downloads, logs, fixtures) | 859,453,825 | E | Real student/teaching data. **Never** deleted by an automated sweep. See §4 for the opt-in review. |
| `.venv/` | 265,274,739 | B/D | The active development environment (Python 3.14.7 + Playwright). Recreatable, but it is *in use*. |
| `frontend/node_modules/` | 68,737,567 | B | Needed for `npm run build` / `npm test`; git-ignored; regenerable from `package-lock.json`. |
| `backend/`, `frontend/`, `agent/`, `scripts/`, `docs/`, `main.py`, launchers | 10,459,813 | A | The application. Not touched. |
| `dev_local/` | 96,608,000 | D | Dev scratch and preserved audit evidence (including a 49 MiB DB copy). Local-only and git-ignored; offered as an opt-in cleanup, not removed automatically. |
| `autostart/`, `state/` | 4,509,202 | E | Machine-local runtime state the code reads at start-up. |

## 3. Permanent protections (Phase 5)

1. `.gitignore` additions:
   - `.venv.foreign/` (moved-aside environment recreated by `setup.bat`);
   - `MathChecker_Distribution/portable/`, `.../release_staging/`, `.../previous_builds/`
     — rebuildable/hand-off artifacts that must not grow history again.
   - Keep the existing `!MathChecker_Distribution/**/*.pyd` negation: it is
     load-bearing for the committed runtime and a test asserts its position.
2. `scripts/size_audit.py` — one command that reports total size by
   category, top-level path and largest file, with `--json` for CI and
   `--fail-over-bytes` as an optional budget gate.
3. `scripts/size_guard.py` — warn-only pre-commit guard for *staged* blobs
   (flags a single staged file > 50 MiB, or > 20 staged files > 5 MiB). Prevents
   the exact mistake that produced 1 GB of history, without blocking legit
   assets.
4. Documentation: `docs/size_optimization/README.md` explains the four distinct
   sizes (dev checkout / minimal source package / production package /
   user-data directory), how to rebuild from a clean checkout, and how to
   restore anything this pass removed.

## 4. Requires explicit approval (NOT done)

| Item | Bytes | Why it needs approval |
|---|---:|---|
| `git filter-repo` / `git gc --prune=now` after dropping the build artifacts from history | up to ~1.0 GB | Rewrites every commit SHA and breaks existing clones. Explicitly forbidden by the task. Procedure drafted in `FINAL_REPORT.md` §7. |
| `data/tmp/` (13 extracted/derived PDFs) | 119,000,000 | Temporary by construction, but produced from real submissions. Recommend: stop the app, then remove files older than N days. |
| `data/purge_backups/` + `data/app.db.bak-*` | ~414 MB | Snapshots of *purged/older* databases. Possibly no longer needed, but only the owner can decide. |
| `data/audit_downloads/` | 102,600,000 | Audit evidence downloads. |
| `dev_local/` | 96,600,000 | Dev scratch; contains a 49 MiB `app.db` copy kept as audit evidence. |

## 5. Implementation order

1. Record the baseline suite result and `scripts/size_audit.py` output
   (done — see `_measurements/`).
2. Delete items 1–4 (build-workspace artifacts) and item 5–6 (stale env, caches).
3. `git gc` and re-measure `.git`.
4. Validate: full pytest suite, frontend `tsc -b` + `npm run build`, portable
   package integrity check (`verify_portable.py` against the kept zip), and a
   start-up + marking-path timing comparison.
5. `CHANGES.md` + `FINAL_REPORT.md` with measured before/after.

## 6. Acceptance criteria for this pass

- No change to correction logic, prompts, model selection, retries, verification
  passes or question-recovery behaviour — **no file under `backend/app/` is
  modified at all**.
- Full backend suite: same pass count as the recorded baseline.
- Frontend typecheck + build succeed.
- `release/MathChecker_Portable.zip` still verifies (hashes, runtime `*.pyd`,
  launch) — the deliverable is untouched.
- Measured working-directory reduction ≥ 0.9 GB, with the distribution package
  size reported separately and honestly.
