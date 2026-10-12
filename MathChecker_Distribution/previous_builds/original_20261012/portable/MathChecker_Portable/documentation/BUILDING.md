# Building a Math Checker release

The release system lives entirely in `MathChecker_Distribution/`. It **reads**
from the private master project and never modifies it. Everything it writes
lives inside this folder.

## Prerequisites (build machine only)

| Tool | Why | Notes |
|---|---|---|
| Python 3.12+ | runs the build scripts | any recent CPython |
| Node.js + npm | compiles the UI (`frontend/dist`) | only if you don't pass `--skip-frontend-build` |
| Inno Setup 6 | compiles `MathChecker_Setup.exe` | `winget install JRSoftware.InnoSetup` works |

Recipients need **none** of these — the release bundles its own Python runtime.

## One command

```bat
build_release.bat
```

On first run it also builds `build\runtime\` (a self-contained Python runtime
with the backend dependencies), then produces `release\`:

```
release/
├── MathChecker_Setup.exe      themed Windows installer
├── MathChecker_Portable.zip   runs with no installation
├── SHA256SUMS.txt
└── release_manifest.json
```

## Steps, if you prefer them individually

```bat
python scripts\build_runtime.py            :: create build\runtime (once)
cd frontend && npm run build               :: compile the UI
cd ..
python scripts\build_release.py --with-runtime build\runtime
```

Useful flags: `--skip-frontend-build`, `--no-clean`, `--with-runtime <folder>`.

## What the pipeline does (10 steps)

1. clean `release_staging/`, `portable/`, `release/`
2. production frontend build test
3. copy **only allowlisted** files (`scripts/release_allowlist.py`)
4. secret + privacy scan — **blocks the build** on any finding
5. dependency validation
6. portable package (folder + zip)
7. Windows installer (Inno Setup, themed like the app)
8. SHA-256 hashes
9. release manifest (version, build, dependency versions, artifact hashes)
10. final validation

If the scan finds a secret or private file, the build stops with

```
RELEASE BLOCKED
Potential sensitive file detected:
  .../release_staging/config/token.json
      - forbidden file pattern '*token.json'  (example)
The installer was NOT created.
```

Nothing is silently deleted: fix the source (or the allowlist) and rebuild.

## Testing a build

- Portable: run `runtime\python.exe main.py 8644 --demo` from
  `portable\MathChecker_Portable\` — it uses only the bundled runtime.
- Installer (non-elevated, into a scratch folder):

  ```bat
  release\MathChecker_Setup.exe /VERYSILENT /CURRENTUSER ^
      /DIR=%TEMP%\mc_test /NOICONS
  %TEMP%\mc_test\unins000.exe /VERYSILENT
  ```

  Your data under `%LOCALAPPDATA%\MathChecker` is preserved across uninstall —
  that separation is the point.
