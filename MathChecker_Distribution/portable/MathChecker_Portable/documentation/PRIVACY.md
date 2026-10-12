# Privacy & Security — how the shareable release is kept clean

> Privacy is solved by **not distributing** private material — never by hiding,
> renaming or obfuscating it.

## What is NEVER shipped

The release is built from an explicit **allowlist** (`scripts/release_allowlist.py`).
A file ships only if that list names it. In practice the release contains only:

- `main.py`, `start.bat`, `start.sh`, `README.md`, `backend/requirements.txt`
- `backend/app/**` (the application code, `.py` + `.sql` only)
- `frontend/dist/**` (the built UI bundle)
- the distribution's own launcher, brand assets and documentation

Everything else — including these — is refused:

| Category | Examples |
|---|---|
| Private source | `Friend code/`, original `app5_1_automated.py` |
| Credentials | `secrets.toml`, `client_secret_*.json`, `*.env`, tokens, `*.pem`, `*.key` |
| Personal / student data | `data/`, `*.db`, `*.pdf`, `*.docx`, `*.xlsx`, `*.csv`, real reports |
| Development material | `.git/`, `__pycache__/`, `node_modules/`, `.venv/`, logs, `*.tsbuildinfo` |

## Two independent gates

1. **Allowlist-first staging** — the staging step copies *only* allowlisted
   files, so an unlisted file cannot enter even by accident.
2. **Blocking scan** (`scripts/scan_release.py`) — an independent pass over the
   staged tree that fails the build on forbidden filenames/globs *and* content
   patterns (Google/OpenAI-style keys, private-key blocks, refresh tokens,
   bearer tokens, `GOCONSPX-…`, password assignments). If anything is found:

   ```
   RELEASE BLOCKED
   Potential sensitive file detected:
     .../release_staging/config/token.json
         - forbidden file pattern '*token*'
   The installer was NOT created.
   ```

### Bundled runtime

A zero-prerequisite release ships a **stock CPython embeddable interpreter
plus vendored PyPI wheels** under `runtime/`. It is *verified by construction*:
it is downloaded from python.org and PyPI (see `scripts/build_runtime.py`) and
is never sourced from the private project, so it cannot contain your keys,
tokens, database or student data.

Because library files are legitimately named `oauth2.py`, `credentials.py` and
`*token*.py`, and because vendor test fixtures and `certifi`'s CA bundle
(`cacert.pem`) would trip the general rules, the scanner applies a **narrow**
runtime rule set that targets only genuine user-data artifacts — `secrets.toml`,
`config.toml`, `token.json`, `credentials.json`, `.env`, `*.db`/`*.sqlite*`,
and stray `data/` or `.git/` folders. A real secret inadvertently placed in
`runtime/` still fails the build.

Nothing is silently deleted — the operator must fix the source.

## Runtime separation (privacy by architecture)

- Installed app files live under `Program Files`; user data lives under
  `%LOCALAPPDATA%\MathChecker\` and is marked `uninsneveruninstall` in the
  installer, so updates and uninstalls never destroy it.
- **Demo Mode** runs in its own isolated data directory and hard-disables
  Google Classroom, so it can never inherit a real account.
- **Factory Reset** removes local data and credentials but leaves the app
  installed, with an explicit confirmation step.
- **Diagnostic export** is sanitized by construction: keys, tokens, student
  names, submissions, emails and reports are not included.

## Verifying a build yourself

```
python scripts/build_runtime.py --force                 # (optional) refresh the runtime
python scripts/scan_release.py release_staging --allowlist-scan
python scripts/build_release.py --with-runtime build/runtime
```

On Windows the whole thing is one command: `build_release.bat` (it builds the
runtime on first run, then the installer, portable zip, hashes and manifest).

`SHA256SUMS.txt` lets a recipient confirm the artifacts they received match the
ones built here.
