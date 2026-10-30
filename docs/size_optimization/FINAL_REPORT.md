# FINAL_REPORT — measured result of the size pass

Date: **2026-10-20**. Repository: `E:\MATH_CHECKER_V5`, branch `main`,
`HEAD e0b3e33` (unchanged). Nothing was committed or pushed; the working tree
carries the deletions described in `CHANGES.md` for review.

---

## 1. What actually caused the 3.7 GB

The application is **10 MB of source**. The reported 3.70 GB was the whole
working directory plus `.git`, and it decomposed into four independent things:

| Cause | Size | Share |
|---|---:|---:|
| Committed release-pipeline output (`MathChecker_Distribution/`) | 1.16 GiB | 33 % |
| Git history storing every one of those builds | 968 MiB | 27 % |
| Live user data (`data/`) | 820 MiB | 23 % |
| Dev environments, npm deps, deliverable, dev scratch, caches | ~550 MiB | 17 % |

The single mistake that produced most of it: **the release pipeline's working
copies are committed to git**. 15,201 of 15,695 tracked files were build output,
including four complete portable packages, an unpacked duplicate of the current
package, the staging copy, and a bundled Python runtime. Each commit then added
another ~190 MB tree plus a ~130 MB ZIP/installer pair to `.git`.

Nothing was mysterious: no 3 GB model weights, no database blobs outside `data/`,
no logs hiding in the tree.

## 2. Before / after

Measured with `python scripts/size_audit.py` (raw output in
`_measurements/size_before.{json,txt}` and `_measurements/size_final.{json,txt}`).

| Measurement | Before | After | Change |
|---|---:|---:|---:|
| **Total working directory** | **3,780,889,620 B (3.52 GiB)** | **2,475,039,905 B (2.30 GiB)** | **−1,305,849,715 B (−34.5 %)** |
| Git metadata and history (`.git/`) | 1,015,205,455 B | 659,741,880 B | −355,463,575 B (−35.0 %) |
| Runtime dependencies (`.venv/` + `.venv.foreign/` + `node_modules/`) | 411,116,884 B | 334,015,208 B | −77,101,676 B (−18.8 %) |
| Caches and disposable artifacts | 13,074,364 B | 268,125 B | −12,806,239 B (−97.9 %) |
| Release-pipeline working copies | 1,240,680,466 B | 380,134,539 B | −860,545,927 B (−69.4 %) |
| **Portable distribution** (`release/MathChecker_Portable.zip`) | 76,365,101 B | 76,365,101 B | **0 (unchanged by design)** |
| Production package (`release/` = ZIP + installer + manifest) | 129,780,182 B | 129,780,182 B | 0 |
| File count | 31,377 | 15,593 | −15,784 |

Reduction: `(3,780,889,620 − 2,475,039,905) / 3,780,889,620 = 34.54 %`.

Other measured package sizes (uncompressed `tar` unless stated):

| Package | Size |
|---|---:|
| Minimal source package (`git archive` of source, tests, docs — no builds/data) | 7,639,040 B (7.3 MiB); **2,008,552 B gzipped** |
| Full clean-checkout payload (`git archive HEAD`, i.e. what a clone materialises) | 1,350,686,720 B (1.26 GiB) |
| User-data directory (`data/`) | 859,453,825 B (820 MiB) — untouched |

### Which change produced which saving

| Change | Bytes | Share of total |
|---|---:|---:|
| Removed `portable/` + `release_staging/` + two superseded `previous_builds/` | 860,545,927 | 66 % |
| `git gc` repack of the loose objects (no history change) | 355,463,575 | 27 % |
| Removed stale Linux `.venv.foreign/` | 77,101,676 | 6 % |
| Removed `__pycache__` / `.pytest_cache` | 12,806,239 | 1 % |

**Disk reclaimed vs. package size are different things.** Reclaimed: 1.31 GB of
checkout plus history. The *distributable* did not get smaller — nothing about
what a teacher receives changed, which is the point.

## 3. Integrity of the deliverable

The kept package was re-verified end-to-end after the deletions:

```console
$ python MathChecker_Distribution/scripts/verify_portable.py --zip release/MathChecker_Portable.zip
...
38/38 checks passed
EXIT=0
```

That run extracts the real ZIP, starts it with its own bundled runtime
(`MathChecker.bat`), checks 15 required paths and all 36 `*.pyd` modules, scans
for credentials/personal data/build-machine paths, corrects a PDF through the
real API ("earned=9/12 percentage=75.0 provisional_grade=7"), renders a report
PDF with the packaged runtime, exports/imports data between two copies, and
stops cleanly. `SHA256SUMS.txt` hashes still match exactly
(`db25948c…` ZIP, `6074d1d0…` installer). Full log:
`_measurements/verify_portable_after.log`.

## 4. Correction behaviour and accuracy

No application file changed. Verified, not assumed:

```console
$ git diff --name-only -- backend frontend agent main.py remote.py scripts | wc -l
0
```

* The changed tracked files are `.gitignore`, `docs/architecture/ARCHITECTURE.md`,
  `docs/architecture/PROJECT_STRUCTURE.md` (+17/−5 lines) plus new files under
  `docs/size_optimization/`, `scripts/`, `.githooks/`.
* The 11,488 deletions are all under `MathChecker_Distribution/`; a grep of
  `backend/app`, `frontend/src`, `agent`, `main.py`, `remote.py` finds **no
  reference** to any removed path.
* Correction logic, prompts, model selection, retries, verification passes,
  question recovery and double-checking were not touched. Tests were not
  removed or weakened.

Backend suite (`pytest backend/tests -q`):

| Run | Result | Wall time |
|---|---|---:|
| Before | **2125 passed**, 2 warnings | 585.95 s |
| After | **2124 passed, 1 skipped**, 2 warnings | 534.55 s |

The one skip is `test_the_repository_runtime_is_not_missing_its_compiled_modules[portable]`,
which by design skips when no `portable/` tree exists in the checkout:

```
SKIPPED .../test_release_runtime_integrity.py:151: no bundled runtime under portable/ in this checkout
```

Its `build/` twin — the runtime that is still committed — **ran and passed**
(`test_release_runtime_integrity.py: 9 passed, 1 skipped`). So the guard that
prevents shipping a runtime without its `*.pyd` modules is still enforced.

## 5. Performance

| Measurement | Value |
|---|---|
| Suite wall time, before → after (one run each) | 585.95 s → 534.55 s (−8.8 %) |
| App import (`import app.main`), 3 runs | 4.65 s (cold FS), 1.05 s, 1.02 s |
| Cold start to `/api/health` 200, 3 runs | 3.15 s, 1.93 s, 2.96 s |
| Marking-path modules (78 tests), 3 runs | 14.58 s, 15.01 s, 11.32 s (wall 16.2/15.8/12.1 s) |
| Frontend production build | `tsc -b && vite build`, 2.16 s, exit 0 |

Honest reading: the 8.8 % suite difference is **not** claimed as a speedup. The
same code ran both times, and the marking-path spread alone is 28 % run to run.
Startup and correction timing have **no before-counterpart on purpose**: the code
is byte-identical (see `_measurements/runtime_impact.txt`), so a delta would
only measure machine noise. What the measurements do show is that the app still
starts in ~2–3 s and the marking tests still take ~11–15 s per run, i.e. no
regression introduced by removing files nothing reads.

Backend suite wall time is ~9 minutes; the timings above were taken on an idle
machine, the suite runs were not.

## 6. Validation performed

```bash
# sizes
.venv/Scripts/python.exe scripts/size_audit.py --top 20 --json > docs/size_optimization/_measurements/size_final.json
# full suite (before and after)
.venv/Scripts/python.exe -m pytest backend/tests -q -p no:cacheprovider
# the release-runtime guard, with skip reasons
.venv/Scripts/python.exe -m pytest backend/tests/test_release_runtime_integrity.py -q -rs
# deliverable, end to end
cd MathChecker_Distribution && ../.venv/Scripts/python.exe scripts/verify_portable.py --zip release/MathChecker_Portable.zip
# dependency manifests install from scratch (throwaway venv)
.venv/Scripts/python.exe -m venv "$tmp/venv" && "$tmp/venv/Scripts/python.exe" -m pip install -r backend/requirements.txt && pip check
# frontend
cd frontend && npm run build
# git integrity
git log --oneline | wc -l ; git rev-parse HEAD ; git ls-tree -r HEAD --name-only | wc -l ; git fsck --no-progress
# staged-blob guard (detection proven with a probe file, then removed)
.venv/Scripts/python.exe scripts/size_guard.py
```

Results: suite parity (2124 passed / 1 designed skip), portable 38/38 PASS,
clean-venv install exit 0 with `pip check` clean, frontend build exit 0,
`pip check` in `.venv` "No broken requirements found", git history intact
(11 commits, same `HEAD`, 15,695 files still reachable at `HEAD`, `git fsck`
reports only two pre-existing dangling commits).

The app also started from scratch several times during these checks (three
benchmark launches plus the portable verifier's launches) against throwaway
data directories — none of them touched the removed trees.

## 7. Remaining size, and what it would take

| Item | Size | Status |
|---|---:|---|
| `.git` pack (history of committed builds) | 626 MiB | **Needs approval**: only a history rewrite shrinks it |
| `data/` (user data) | 820 MiB | **Needs approval**: live DB 152 MiB, `purge_backups/` 195 MiB, `app.db.bak-*` ~215 MiB, `tmp/` 119 MiB, `audit_downloads/` 103 MiB, `mark_schemes/` 93 MiB |
| `build/` (bundled runtime + cloudflared) | 239 MiB | **Kept on purpose** — offline rebuild input, and it is the runtime the release guard asserts on |
| `.venv/` | 253 MiB | Kept — the active dev environment |
| `release/` (deliverable) | 124 MiB | Kept — the artifact a teacher receives |
| `previous_builds/20261016-193501/` | 124 MiB | Kept — one rollback package + `release_update_check.py` baseline |
| `dev_local/` | 92 MiB | Local-only audit evidence; safe to delete if unwanted |
| `frontend/node_modules/` | 66 MiB | Kept — regenerable from `package-lock.json` |

### History rewrite (NOT performed — requires explicit approval)

Removing the build directories from *history* is the only way to get another
~0.4–0.5 GB. It rewrites every commit SHA after the first affected commit, so it
needs a coordinated decision and a backup mirror:

```bash
git clone --mirror . ../MATH_CHECKER_V5-backup.git      # backup first
git filter-repo --force \
    --path MathChecker_Distribution/portable --invert-paths \
    --path MathChecker_Distribution/release_staging --invert-paths \
    --path MathChecker_Distribution/previous_builds --invert-paths
git reflog expire --expire=now --all && git gc --prune=now --aggressive
```

Expected after that: `.git` in the ~120–200 MB range, total checkout ~1.9 GB
(dominated by `data/`), and every existing clone must be re-cloned.

### User-data cleanup (NOT performed — requires approval)

`data/` is real teaching data; nothing was deleted or moved. If the owner wants
it smaller, in increasing order of caution:

```bash
# 119 MB of derived/extracted PDFs from finished jobs (stop the app first)
rm -rf data/tmp/*

# 103 MB of audit downloads
ls data/audit_downloads        # review first
```

`purge_backups/`, `app.db.bak-*` and `mark_schemes/` are *records*, not caches —
they should be archived to external storage by the owner, never swept.

## 8. Limitations

1. **History is still large** (626 MiB pack). Everything committed before this
   pass is still there, by design.
2. **The deletions are uncommitted.** `git status` shows 11,488 deletions plus
   `.gitignore` and two docs edited. Until they are committed, a `git checkout .`
   would restore the build trees (and the space) — that is the safety net, not a
   completed change.
3. **`git gc` is not a shrink for everyone.** It reclaimed 355 MB here because
   four near-identical ZIPs were stored uncompressed-as-loose-objects; a clone
   that already ran `gc` will show the new baseline, not the delta.
4. **The Windows installer was not rebuilt** (Inno Setup 6 not exercised); the
   existing signed-off `MathChecker_Setup.exe` hash still matches its manifest.
5. **Live Gemini / Google Classroom were not exercised** — no credentials; that
   is unchanged from the previous audit and unrelated to this pass.
6. **Startup and marking timings have no before-counterpart** (identical code);
   only variance is reported.
7. `release/SHA256SUMS.txt` uses CRLF line endings, so `sha256sum -c` fails on
   Linux (pre-existing; hashes were compared manually and match).

## 9. Verdict

* Working directory: **3.52 GiB → 2.30 GiB (−34.5 %, −1.31 GB measured)**.
* Application source: unchanged (0 files), correction behaviour verified by the
  full suite and a 38/38 end-to-end portable verification that corrects a real
  PDF.
* Deliverable: **byte-identical** (76.4 MB ZIP, 53.4 MB installer, hashes match).
* Offline operation: intact — the bundled runtime and `cloudflared.exe` are still
  in the repository, so a rebuild needs no download.
* User data: untouched.
* Remaining reductions (history rewrite ≈ 0.45 GB, `data/` cleanup ≈ 0.4 GB)
  need the owner's explicit approval and are documented above with commands.
