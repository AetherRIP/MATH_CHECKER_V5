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
