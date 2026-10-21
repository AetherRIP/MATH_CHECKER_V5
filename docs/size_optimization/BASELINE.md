# BASELINE — where the 3.7 GB actually is

Measured on **2026-10-20** in `E:\MATH_CHECKER_V5` (Windows 11, NTFS, Git Bash)
with [`scripts/size_audit.py`](../../scripts/size_audit.py).

```bash
python scripts/size_audit.py --top 20 --json > docs/size_optimization/_measurements/size_before.json
python scripts/size_audit.py --top 20      > docs/size_optimization/_measurements/size_before.txt
```

The raw output of both commands is kept in
`docs/size_optimization/_measurements/` so every number below can be re-derived.

---

## 1. Is this the right project?

| Check | Value |
|---|---|
| Path | `E:\MATH_CHECKER_V5` |
| Remote | `https://github.com/AetherRIP/MATH_CHECKER_V5` |
| Branch | `main` → `origin/main` |
| HEAD | `e0b3e33 Protect app data and expand dashboard coverage` |
| Commits in history | 11 |
| Working tree at start | **clean** (no staged, unstaged or untracked changes) |
| Stash / extra refs | none (only `main`, `origin/main`, `origin/HEAD`) |

Confirmed: the FastAPI + React "Math Checker V5" application, its release
workspace in `MathChecker_Distribution/`, its test suite in `backend/tests/`
(111 test modules) and its runtime data directory in `data/`.

## 2. Reported vs. measured

| Measurement | Bytes | Human |
|---|---:|---|
| Reported in the task | 3,976,392,704 | 3.70 GiB |
| **Measured now (whole working directory)** | **3,780,889,620** | **3.52 GiB** |
| Difference | −195,503,084 | −4.9 % |

The reported figure is ~186 MiB larger than today's measurement. The gap is
consistent with measurements taken at different moments: `.git` was repacked,
and `data/` is live (the SQLite WAL, logs and `data/tmp/` change on every run).
Both numbers describe **the same thing — the full working directory plus
`.git`** — not a ZIP, not a packaged build, not a fresh clone. Nothing in the
repository is anywhere near 3.7 GB as a single artifact; the size is the *sum*
of a checkout, its history and its runtime data.

File count: **31,377 files**, of which 15,695 are tracked by git. The
application source itself is 613 files / 10.0 MiB.

## 3. Size by category

Categories are the ones produced by `scripts/size_audit.py`; the "class" column
maps them onto the audit brief (A–F).

| Category | Class | Bytes | Size | Share | Files |
|---|---|---:|---:|---:|---:|
| `release_build` — `MathChecker_Distribution/{build,portable,release_staging,previous_builds}` | C + D | 1,240,680,466 | 1.16 GiB | 32.8 % | 18,196 |
| `git` — `.git/` (history + object store) | F | 1,015,205,455 | 968.2 MiB | 26.9 % | 356 |
| `user_data` — `data/` | E | 859,453,825 | 819.6 MiB | 22.7 % | 127 |
| `python_env` — `.venv/` + `.venv.foreign/` | D | 342,379,317 | 326.5 MiB | 9.1 % | 9,063 |
| `release_artifacts` — `MathChecker_Distribution/release/` | C (deliverable) | 129,780,182 | 123.8 MiB | 3.4 % | 5 |
| `dev_scratch` — `dev_local/`, `autostart/`, `state/`, `.freebuff/` | D + E | 101,118,631 | 96.4 MiB | 2.7 % | 127 |
| `node_deps` — `frontend/node_modules/` | B | 68,737,567 | 65.6 MiB | 1.8 % | 2,447 |
| `caches` — `__pycache__`, `.pytest_cache`, `*.pyc` | C | 13,074,364 | 12.5 MiB | 0.3 % | 443 |
| `source` — application, tests, docs, packaging scripts | A | 10,459,813 | 10.0 MiB | 0.3 % | 613 |

**The headline: 0.3 % of the project is source code.** 97 % is build output,
history, runtime data and development environments.

## 4. Top 20 contributors

Directory level (MiB), from `_measurements/dirs_before.txt`:

| # | Path | Size | What it is |
|---:|---|---:|---|
| 1 | `.git/` | 968.2 MiB | Object store; two packs (473.6 + 122.8 MiB) and 317 loose objects |
| 2 | `MathChecker_Distribution/previous_builds/` | 561.8 MiB | Four superseded release builds, each with a full portable tree |
| 3 | `MathChecker_Distribution/build/` | 238.8 MiB | Pipeline input: bundled Python runtime + `cloudflared.exe` |
| 4 | `data/purge_backups/` | 195.2 MiB | Backups of purged student data (user data) |
| 5 | `MathChecker_Distribution/release_staging/` | 191.5 MiB | Staged application copy, wiped on every build |
| 6 | `MathChecker_Distribution/portable/` | 191.2 MiB | Unpacked copy of the portable package |
| 7 | `data/app.db` (+`-wal`) | 152.2 MiB | Live SQLite database (user data) |
| 8 | `MathChecker_Distribution/release/` | 123.8 MiB | Current hand-off artifacts (zip + setup exe) |
| 9 | `data/tmp/` | 119.0 MiB | 13 extracted/derived PDFs from past jobs |
| 10 | `data/audit_downloads/` | 102.6 MiB | Downloads kept for audit |
| 11 | `data/mark_schemes/` | 93.2 MiB | Mark schemes (user data) |
| 12 | `.venv.foreign/lib/` | 73.5 MiB | Stale **Linux** virtual environment (`/usr/bin/python3.14`) |
| 13 | `frontend/node_modules/` | 65.6 MiB | npm dependencies, regenerable from `package-lock.json` |
| 14 | `dev_local/release_audit_backup/` | 49.0 MiB | Dev scratch + a copy of `app.db` |
| 15 | `dev_local/runs/` | 24.7 MiB | Dev run logs |
| 16 | `data/fixtures/` | 18.5 MiB | Test fixtures / synthetic submissions |
| 17 | `data/logs/` | 16.3 MiB | Application logs |
| 18 | `backend/tests/` | 9.6 MiB | 111 test modules (class A — keep) |
| 19 | `dev_local/archive/` + `reports/` + `ui_audit/` | 17.5 MiB | Dev scratch |
| 20 | `autostart/` | 4.3 MiB | Machine-local autostart state + logs |

Individual files ≥ 10 MiB (top of the list; full list in
`_measurements/size_before.json`):

| Bytes | File | Note |
|---:|---|---|
| 496,555,539 | `.git/objects/pack/pack-2cace…pack` | Loose→packed history of the committed builds |
| 128,748,484 | `.git/objects/pack/pack-a33d…pack` | Earlier pack |
| 93,580,104 | `.venv/Lib/site-packages/playwright/driver/node.exe` | Playwright (dev-only) |
| 76,365,101 | `MathChecker_Distribution/release/MathChecker_Portable.zip` | **Current deliverable** |
| 76,361,483 | `…/previous_builds/20261016-193501/MathChecker_Portable.zip` | Superseded build |
| 76,328,811 | `…/previous_builds/20261016-120509/MathChecker_Portable.zip` | Superseded build |
| 76,244,957 | `…/previous_builds/original_20261012/release/MathChecker_Portable.zip` | Superseded build |
| 75,457,326 ×3 | `.git/objects/{ff,04,40}/…` (loose) | Unpacked blobs of the same zips |
| 55,366,080 ×4 | `cloudflared.exe` | **Byte-identical** in 4 places (sha256 `f096265e…`) |
| 53,404,277 | `MathChecker_Distribution/release/MathChecker_Setup.exe` | **Current deliverable** |
| 53,396,790 | `…/previous_builds/20261016-193501/MathChecker_Setup.exe` | Superseded build |
| 53,381,749 | `…/previous_builds/20261016-120509/MathChecker_Setup.exe` | Superseded build |
| 53,314,265 | `…/previous_builds/original_20261012/release/MathChecker_Setup.exe` | Superseded build |
| 51,339,264 ×4 | `data/app.db.bak-*` | Pre-cleanup DB snapshots (**user data — untouched**) |
| 26,295,808 ×4 | `…/runtime/Lib/site-packages/pymupdf/mupdfcpp64.dll` | PyMuPDF, duplicated per build tree |

## 5. Git object store

```console
$ git count-objects -vH
count: 317                 size: 368.83 MiB      # loose, recent commits
in-pack: 4626              packs: 2              size-pack: 596.46 MiB
prune-packable: 60         garbage: 0
```

`.git` is ~1.0 GB because **every release build has been committed**: 15,201 of
the 15,695 tracked files live under `MathChecker_Distribution/`. Each build adds
a fresh ~190 MiB portable tree plus a ~130 MiB zip/installer pair, so history
accumulates full copies of the same packages. This is history, not clutter — no
history rewrite is performed in this task (see `OPTIMIZATION_PLAN.md` §6).

## 6. Duplicate analysis (hash-verified)

| Comparison | Method | Result |
|---|---|---|
| `portable/MathChecker_Portable/**` vs `release/MathChecker_Portable.zip` | ZIP CRC32 + size of all 3,836 entries | **0 mismatches, 0 extra, 0 missing** — byte-identical content |
| 4 × `cloudflared.exe` (build/, portable/, release_staging/, previous_builds/original…) | SHA-256 | **all `f096265e…2ea7e2`** — same binary 4× |
| 4 × `MathChecker_Portable.zip` | SHA-256 | **all different** — genuinely different builds (different versions/manifests), *not* duplicates |
| 4 × `MathChecker_Setup.exe` | SHA-256 | all different (same reason) |
| `data/app.db.bak-*` (4 × 51.3 MiB) | same size, different mtimes | DB snapshots taken before separate cleanups — **user data, kept** |

So the duplicated bulk is: the unpacked copy of the current package (191 MiB),
the staging copy of it (191 MiB), and the cloudflared binary (3 redundant
52.8 MiB copies). The old zips/installers are distinct builds; they are
*surplus*, not duplicates.

## 7. Verdict

The 3.7 GB is **not** the application. It decomposes as:

1. **1.24 GiB (33 %)** — `MathChecker_Distribution/` build-workspace output that
   is committed to git: superseded builds, the staging copy, the unpacked
   package, and the bundled runtime that the pipeline consumes.
2. **0.95 GiB (27 %)** — `.git/`, dominated by those same build outputs
   committed over 11 commits.
3. **0.80 GiB (23 %)** — `data/`: live database, mark schemes, purge backups,
   extracted PDFs, audit downloads, logs. **Real user data; not deleted.**
4. **0.33 GiB (9 %)** — `.venv/` (active, needed) and `.venv.foreign/` (stale
   Linux environment).
5. **~0.25 GiB (7 %)** — `release/` deliverable, npm deps, dev scratch, caches.

Nothing here is a mysterious 3 GB of "junk"; it is a development checkout of an
app that also stores its own release binaries, its history and its live data in
the same tree. That is what `OPTIMIZATION_PLAN.md` targets.
