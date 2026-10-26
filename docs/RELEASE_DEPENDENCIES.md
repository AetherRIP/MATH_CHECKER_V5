# Release dependency manifest (Windows x64 installer)

What the shipped package contains, what it needs from the machine, and what it
needs from the internet. Produced alongside `release/release_manifest.json`,
which records the version, sizes and SHA-256 of each built artefact.

## Bundled — the recipient installs nothing

| Component | Source | Approx. size |
|---|---|---|
| WPF desktop client (`desktop\V5.exe`) | `dotnet publish --self-contained win-x64` from `desktop\src\MathChecker.Desktop` | 130 MB (includes the .NET 10 runtime, WPF natives, app assets/fonts) |
| CPython 3.13 runtime (`runtime\python.exe`) | `MathChecker_Distribution\build\runtime` (embedded CPython + site-packages) | 140 MB before pruning; `pip`, `pytest`, `pygments` and caches are stripped for the release |
| Backend (`main.py`, `backend\app`, `backend\scripts`) | the repository, tests and `__pycache__` excluded | ~8 MB |
| Native/extension modules inside the runtime | `_socket`, `_sqlite3`, `_ssl`, `_ctypes`, `_hashlib`, `_decimal`, `_multiprocessing`, `pyexpat`, `unicodedata` | — |
| Python dependencies | `backend\requirements.txt`, pinned in `backend\requirements-lock.txt`: fastapi, uvicorn, python-multipart, google-genai, google-auth(-oauthlib), requests, openpyxl, reportlab, PyMuPDF (`pymupdf<2`), pypdf, Pillow, cryptography | 100 MB of the runtime |
| PDF tooling | PyMuPDF + pypdf + Pillow (compiled into the runtime; no external executable) | — |
| UI assets | compiled XAML, fonts (`Assets\Fonts`), icons | inside the client |
| Launcher, guide, version marker | `start_desktop.bat`, `README.txt`, `VERSION` | < 20 KB |
| Installer | Inno Setup 6.7 (`PrivilegesRequired=lowest`, lzma2/solid) | — |

Verified before packaging (`desktop\build_release.ps1` fails the build otherwise):

* every required `.pyd` extension is present in the runtime;
* the runtime imports fastapi, uvicorn, fitz, PIL, pydantic_core, cryptography,
  google.genai, pypdf, openpyxl, reportlab, multipart, sqlite3 and ssl from a
  sanitised environment;
* the bundled backend starts from the staged tree and answers `/api/health`,
  creating its database;
* no `data\`, `config.toml`, `secrets.toml`, `.env`, `.git`, `*.db`, `*.pdf`,
  `__pycache__`, `*.pyc` or `*.pdb` reaches the package, and no absolute
  developer path is baked into the shipped binaries.

## Provided by the machine (no installation needed)

* Windows 10/11 x64. Nothing else: no .NET runtime, no Python, no Visual C++
  redistributable beyond what Windows 10/11 already ships, no Git, no browser.
* Write access to the current user's profile (`%LOCALAPPDATA%`,
  `%APPDATA%`) for the database, settings and logs.
* A free TCP port on loopback (default 8642; the client moves to the next free
  port if another program owns it).
* ~600 MB of disk for the installed program plus room for the data folder.

## Required from the internet / accounts

These are **not** bundled and are not misrepresented as part of the installer:

* **Internet access and an AI provider key (Gemini)** for correcting papers.
  Entered in Settings → AI keys by the user; stored under
  `%LOCALAPPDATA%\MathChecker` (never in the package or a log).
* **A Google Classroom account** (optional) for classroom/assignment sync. The
  app installs and runs without it; manual PDF upload and all local features
  work with no account.
* No network access is needed to install or to open the app.

## Build-only tools (never needed by the recipient)

* .NET 10 SDK (desktop publish)
* Inno Setup 6 (installer compiler)
* PowerShell 7+ recommended (`-Environment` on `Start-Process`; a fallback path
  keeps the smoke test working on Windows PowerShell 5.1)
* The bundled runtime already exists; rebuilding it is a separate concern of
  `MathChecker_Distribution\build_release.bat` and needs its own Python/pip.

## Secrets and privacy

The package is built from an allowlist of paths (`main.py`, `backend`, the
published client, the runtime, the launcher, the guide). The repository's
`data\` folder — which holds the developer's `config.toml`, `secrets.toml`,
`app.db`, student PDFs and mark schemes — is never copied, and the build fails
if any of those artefacts or a developer absolute path appears in the staged
tree.
