# Keeping MATH_CHECKER_V5 small

Four different things get called "the project size". They are not
interchangeable, and only one of them is the number to optimise:

| # | Artifact | What it contains | How to measure |
|---|---|---|---|
| 1 | **Development checkout** | source + `.git` + `.venv` + `data/` + release workspace | `python scripts/size_audit.py` |
| 2 | **Minimal source package** | tracked source only, no venv/venv caches/data/builds | `git archive HEAD \| wc -c` |
| 3 | **Production package** | the portable build handed to a teacher | size of `MathChecker_Distribution/release/MathChecker_Portable.zip` + `MathChecker_Setup.exe` |
| 4 | **User-data directory** | database, mark schemes, reports, logs | `du -sh data/` |

Removing caches makes (1) smaller and does nothing for (2)–(4). Deleting
`data/tmp` shrinks (4) only. Nothing in this repository can shrink (3) without
changing what ships.

## Everyday commands

```bash
# Where is the disk going? (categories, top-level paths, largest files)
python scripts/size_audit.py --top 20
python scripts/size_audit.py --json > /tmp/size.json
python scripts/size_audit.py --fail-over-bytes 2500000000   # optional CI gate

# Warn before committing generated output or a very large file
python scripts/size_guard.py
git config core.hooksPath .githooks        # opt in to the pre-commit warning
```

`scripts/size_audit.py` never deletes anything; it only reads metadata.

## What is expected to be local-only

These are git-ignored and are **not** part of a clean checkout:

| Path | Rebuilt by |
|---|---|
| `.venv/` | `setup.bat` (Windows) — installs `backend/requirements-dev.txt` |
| `.venv.foreign/` | created by `setup.bat` when it moves an old environment aside |
| `frontend/node_modules/` | `cd frontend && npm ci` |
| `frontend/dist/` | `cd frontend && npm run build` |
| `data/` | created by the app on first start; **contains real user data — back it up, never delete it** |
| `dev_local/` | development scratch; safe to delete, nothing imports it |
| `__pycache__/`, `.pytest_cache/`, `*.pyc` | regenerated automatically |
| `MathChecker_Distribution/portable/`, `.../release_staging/` | `build_release.bat` (wiped and rewritten every build) |
| `MathChecker_Distribution/previous_builds/` | nothing — it is rollback material written when a build is superseded. Keep at most the newest package, and do not commit new ones |

`MathChecker_Distribution/release/` is the one release directory a user is meant
to keep: it holds the current portable ZIP, the installer and the manifest that
proves what they contain. `MathChecker_Distribution/build/` holds the bundled
runtime the pipeline needs; keep it if you want to rebuild without downloading a
Python runtime.

## Rebuilding from a clean checkout

```bat
setup.bat                        :: venv + dev dependencies + data dir
cd frontend && npm ci && npm run build && cd ..
python main.py                   :: run the app from source
.venv\Scripts\python -m pytest backend\tests -q
```

```bat
cd MathChecker_Distribution
build_release.bat                :: portable ZIP + installer (Inno Setup 6 needed)
```

## Restoring anything removed by `docs/size_optimization/`

Every deleted item was tracked at `HEAD` and is restored with one command:

```bash
git checkout -- MathChecker_Distribution/portable
git checkout -- MathChecker_Distribution/previous_builds/original_20261012
```

Superseded builds from older commits:

```bash
git log --oneline -- MathChecker_Distribution/previous_builds
git checkout <commit> -- MathChecker_Distribution/previous_builds/<label>
```

## History is the expensive part

Committing a release package makes the repository permanently larger: `.git`
keeps every version of every binary, so deleting `portable/` from the working
tree does not give the bytes back. The pre-commit guard exists to stop that from
happening again. Shrinking the *existing* history requires a rewrite
(`git filter-repo`) and is a separate, approved decision — see
`FINAL_REPORT.md` §7.

## Reports

- `BASELINE.md` — what the size was and why
- `OPTIMIZATION_PLAN.md` — what was safe to change, and what was not
- `CHANGES.md` — what was actually done
- `FINAL_REPORT.md` — measured before/after, validation, remaining options
- `_measurements/` — raw command output the numbers come from. Paths are kept,
  but classroom and file identifiers of the real user data they were measured on
  are replaced with zeroes so the evidence carries no user data.
