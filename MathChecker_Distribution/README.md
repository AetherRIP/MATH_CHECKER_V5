# MathChecker_Distribution — release / packaging workspace

Builds and packages the portable Windows application from the private master
project in the parent folder. The master project is only ever **read** from here.

```
build_release.bat        one command: runtime -> staging -> scan -> packages
scripts/
  release_allowlist.py   explicit production allowlist (allowlist-first)
  scan_release.py        blocking secret + privacy scan
  build_runtime.py       reproducible bundled Python runtime (no prerequisites)
  build_release.py       pipeline orchestrator
  verify_portable.py     launch/verify the packaged portable app
installer/               themed Inno Setup script, brand assets, launcher
documentation/           recipient guide, portable README, privacy + build docs
build/                   pipeline input: bundled runtime + cloudflared
                         (cleaned and rebuilt by the pipeline)
release_staging/         the clean staged application (cleaned each build)
portable/                the packaged portable folder handed to recipients
release/                 final artefacts: MathChecker_Setup.exe,
                         MathChecker_Portable.zip, SHA256SUMS.txt,
                         release_manifest.json, MathChecker.iss
logs/                    output of past pipeline runs
previous_builds/         superseded packages, kept under the policy below
```

## Handing a build to someone else

Hand out the artefact, never a working folder:

* **`release/MathChecker_Portable.zip`** — unpack, run `MathChecker.bat` /
  `start.sh`. This is the file to send to a teacher who has no Python.
* **`release/MathChecker_Setup.exe`** — the Windows installer, *after* it has
  been compiled (see below).

Do not send `build/`, `release_staging/`, `portable/` or
`previous_builds/original_*/`: they are pipeline working copies, they carry the
build machine's own paths, and the archived `original_*` folder is a superseded
build kept only for comparison. `release/` is the single current location — its
`release_manifest.json` + `SHA256SUMS.txt` are what a recipient can check.

**The bundled runtime must contain its compiled modules (`*.pyd`).**
`build_release.py` refuses to package a runtime without `_socket`, `_sqlite3`,
`_ssl`, `_ctypes`, `_hashlib`, PyMuPDF, Pillow, `pydantic_core` and
`cryptography` extensions, and `verify_portable.py` re-checks the packaged copy.
A runtime that has lost them still contains the whole stdlib zip, so it looks
complete to every version or size check — but the app cannot import `socket`,
`sqlite3` or `ssl` on the recipient's PC and will not start at all. This is not
hypothetical: the `*.py[cod]` rule in the repository `.gitignore` also matches
`*.pyd`, so the runtime committed here had none of them. Keep the
`!MathChecker_Distribution/**/*.pyd` negation that re-includes them.

**The Windows installer can only be compiled on Windows** (Inno Setup 6). On any
other platform the pipeline writes `release/MathChecker.iss` with the current
version/build and skips the `.exe` — run `build_release.bat` on a Windows
machine to produce it. The installer is deliberately not shipped as "current"
when it is out of date: an installer built from older source would hand a
teacher the bugs that have been fixed since.

`build/`, `release_staging/`, `portable/` and `release/` are **live pipeline
directories** — `build_release.py` wipes and rewrites them, so never store
anything you want to keep inside them.

## Which build is the current one

`release/release_manifest.json` (with `release/SHA256SUMS.txt`) is the record of
the latest successful build: version, build number, file count and hashes. If a
manifest and the folder on disk disagree, the folder is a leftover — rebuild
rather than hand-edit.

## Retention policy for previous builds

Superseded packages are large (hundreds of MB), so they are kept deliberately,
not accumulated by accident:

* a build replaced by a newer one moves to
  `previous_builds/<label>/` (currently `original_20261012/`, the package the
  2026-10-12 audit started from, kept so the fix can be compared against it);
* keep at most the **two most recent** previous builds — delete older ones by
  hand once the new release has been verified on a real machine;
* never let an old build live anywhere else in the repository: one current
  location (`release/`), one history location (`previous_builds/`).

## Verifying a build

```bat
build_release.bat                  :: full pipeline (needs Inno Setup 6 for the installer)
build_release.bat --skip-frontend-build
python scripts\verify_portable.py  :: exercise the packaged portable folder
```

Pipeline output is logged to `logs/`. Full verification evidence for the
2026-10-12 build is in [docs/reports/PORTABLE_RELEASE_REPORT.md](../docs/reports/PORTABLE_RELEASE_REPORT.md)
and [docs/logs/PORTABLE_VERIFY_REPORT_20261012.txt](../docs/logs/PORTABLE_VERIFY_REPORT_20261012.txt).
