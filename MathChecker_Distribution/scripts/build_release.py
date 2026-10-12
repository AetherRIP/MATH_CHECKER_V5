#!/usr/bin/env python3
"""Math Checker release pipeline.

    PRIVATE MASTER ──build──► CLEAN RELEASE STAGING ──scan──► RELEASE
                                                              ├─ MathChecker_Setup.exe
                                                              ├─ MathChecker_Portable.zip
                                                              ├─ SHA256SUMS.txt
                                                              └─ release_manifest.json

The pipeline only ever READS from the private project and WRITES into this
``MathChecker_Distribution`` folder. It never moves, renames or edits the
master project, its data, its secrets or ``Friend code/``.

Steps
  1. clean staging
  2. production build test (frontend dist must exist and be fresh)
  3. copy ONLY allowlisted files (allowlist-first)
  4. secret + privacy scan  (BLOCKS on any finding)
  5. dependency validation
  6. build portable package (folder + zip)
  7. build installer (Inno Setup, themed)
  8. generate SHA-256 hashes
  9. generate release manifest
 10. final validation

Usage
    python build_release.py                 # standard release
    python build_release.py --with-runtime C:\\PythonRuntime
    python build_release.py --skip-frontend-build
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
DIST = HERE.parent                       # MathChecker_Distribution/
ROOT = DIST.parent                       # private master project
STAGING = DIST / "release_staging"
PORTABLE_DIR = DIST / "portable"
INSTALLER_DIR = DIST / "installer"
RELEASE_DIR = DIST / "release"
DOCS_DIR = DIST / "documentation"
PREVIOUS_DIR = DIST / "previous_builds"

sys.path.insert(0, str(HERE))
from release_allowlist import (  # noqa: E402
    ALLOWED_FILES, ALLOWED_TREES, is_allowed, missing_remote_access_files,
)


# ------------------------------------------------------------------ helpers

def log(step: str, message: str) -> None:
    print(f"[{step}] {message}", flush=True)


def fail(message: str) -> "NoReturn":  # type: ignore[name-defined]
    print("\n" + "=" * 64, flush=True)
    print(f"BUILD FAILED: {message}", flush=True)
    print("=" * 64, flush=True)
    raise SystemExit(1)


def read_app_version() -> tuple[str, str, str]:
    """Read APP_NAME/APP_VERSION from the master without importing the app."""
    version_py = ROOT / "backend" / "app" / "version.py"
    name, version, build = "Math Checker", "0.0.0", ""
    if version_py.is_file():
        for line in version_py.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith("APP_NAME"):
                name = line.split("=", 1)[1].strip().strip('"\'')
            elif line.startswith("APP_VERSION"):
                version = line.split("=", 1)[1].strip().strip('"\'')
            elif line.startswith("BUILD_NUMBER"):
                build = line.split("=", 1)[1].strip().strip('"\'')
    return name, version, build


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


# ------------------------------------------------------------- 1. clean

#: Folders this pipeline generates automatically, as opposed to the
#: hand-labelled history folders (e.g. ``original_20261012``) which are a
#: deliberate record and are never pruned.
_GENERATED_ARCHIVE = re.compile(r"^\d{8}-\d{6}$")

#: How many superseded packages to keep (the retention policy in this folder's
#: README), on top of any hand-labelled history folder.
_KEEP_ARCHIVES = 2


def archive_previous_release() -> None:
    """Keep the package this rebuild is about to replace.

    ``clean_staging`` wipes ``release/``. Without this step the previous package
    was simply destroyed, so the documented retention policy ("a build replaced
    by a newer one moves to previous_builds/<label>/") did not actually happen
    and an operator who wanted to roll back found nothing to roll back to.
    """
    if not RELEASE_DIR.is_dir():
        return
    artifacts = [p for p in RELEASE_DIR.iterdir() if p.is_file()]
    if not artifacts:
        return

    label = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
    target = PREVIOUS_DIR / label
    if target.exists():
        # Two builds inside the same second: do not silently overwrite the
        # archive that is already there.
        log("1/10", f"previous release already archived as {label}")
        return
    target.mkdir(parents=True, exist_ok=True)
    for item in artifacts:
        shutil.copy2(item, target / item.name)
    log("1/10", f"archived the previous release to previous_builds/{label} "
                f"({len(artifacts)} file(s))")

    archives = sorted((d for d in PREVIOUS_DIR.iterdir()
                       if d.is_dir() and _GENERATED_ARCHIVE.match(d.name)),
                      reverse=True)
    for stale in archives[_KEEP_ARCHIVES:]:
        shutil.rmtree(stale, ignore_errors=True)
        log("1/10", f"pruned superseded archive {stale.name} "
                    f"(policy: keep {_KEEP_ARCHIVES})")


def clean_staging() -> None:
    archive_previous_release()
    for target in (STAGING, PORTABLE_DIR, RELEASE_DIR):
        if target.exists():
            shutil.rmtree(target)
        target.mkdir(parents=True, exist_ok=True)
    log("1/10", f"cleaned {STAGING.name}, {PORTABLE_DIR.name}, {RELEASE_DIR.name}")


# -------------------------------------------------- 2. production build test

def verify_frontend(skip_build: bool) -> None:
    dist = ROOT / "frontend" / "dist"
    index = dist / "index.html"
    if not skip_build:
        npm = shutil.which("npm") or shutil.which("npm.cmd")
        if npm and (ROOT / "frontend" / "package.json").is_file():
            log("2/10", "building frontend (npm run build)…")
            try:
                subprocess.run([npm, "run", "build"], cwd=ROOT / "frontend",
                               check=True, shell=(os.name == "nt"))
            except subprocess.CalledProcessError as exc:
                fail(f"frontend production build failed ({exc}).")
        else:
            log("2/10", "npm not found — using the existing frontend/dist")
    if not index.is_file():
        fail("frontend/dist/index.html is missing. Build the UI first "
             "(cd frontend && npm run build).")
    log("2/10", "production frontend verified")


# ------------------------------------------------------- 3. stage (allowlist)

def stage_sources(with_runtime: str | None) -> list[str]:
    copied: list[str] = []
    for rel in ALLOWED_FILES:
        src = ROOT / rel
        if not src.is_file():
            continue
        # exact files still pass through the allowlist's denials
        if not is_allowed(rel):
            continue
        dest = STAGING / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dest)
        copied.append(rel)

    for tree, _exts in ALLOWED_TREES:
        base = ROOT / tree
        if not base.is_dir():
            continue
        for src in base.rglob("*"):
            if not src.is_file():
                continue
            rel = src.relative_to(ROOT).as_posix()
            if not is_allowed(rel):
                continue
            dest = STAGING / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dest)
            copied.append(rel)

    # Recipient documentation, launcher, brand assets and the portable flag
    # are distribution-owned files (never taken from the private project).
    if DOCS_DIR.is_dir():
        shutil.copytree(DOCS_DIR, STAGING / "documentation")
        copied.append("documentation/")
    launcher = INSTALLER_DIR / "launcher.bat"
    if launcher.is_file():
        shutil.copy2(launcher, STAGING / "MathChecker.bat")
        copied.append("MathChecker.bat")
    # Stopping the app must not require Task Manager: Stop.bat asks the app
    # itself (main.py --stop) to shut down, which also takes the correction
    # workers and any tunnel this copy started with it.
    stopper = INSTALLER_DIR / "Stop.bat"
    if stopper.is_file():
        shutil.copy2(stopper, STAGING / "Stop.bat")
        copied.append("Stop.bat")
    assets = INSTALLER_DIR / "assets"
    if assets.is_dir():
        shutil.copytree(assets, STAGING / "assets")
        copied.append("assets/")
    (STAGING / "portable.flag").write_text(
        "Portable install: data is kept in ./Data next to the application.\n",
        encoding="utf-8")
    copied.append("portable.flag")

    # Shell launchers must be executable IN THE ARTIFACT. A checkout (or a zip
    # round-trip) that lost the mode bit produced a package whose documented
    # Linux/macOS start command (./start.sh) failed with "Permission denied" -
    # the mode is part of the product, so it is set here rather than trusted.
    for shell_script in STAGING.rglob("*.sh"):
        try:
            shell_script.chmod(shell_script.stat().st_mode | 0o755)
        except OSError as exc:
            log("3/10", f"WARNING: could not make {shell_script.name} "
                        f"executable: {exc}")

    if with_runtime:
        runtime_src = Path(with_runtime).resolve()
        if not runtime_src.is_dir():
            fail(f"--with-runtime path not found: {runtime_src}")
        shutil.copytree(runtime_src, STAGING / "runtime",
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        copied.append("runtime/")
    # Safety net: the UI bundle is required at runtime and has been lost once
    # by an over-eager denial, so fail loudly rather than ship a headless app.
    ui_index = STAGING / "frontend" / "dist" / "index.html"
    if not ui_index.is_file():
        fail("frontend/dist/index.html was not staged — the release would have "
             "no user interface. Check the allowlist rules for frontend/dist.")
    # Consistency guard: the app ships remote-access support (Settings shows a
    # Remote Access panel and tells the teacher to run start_public.bat). If
    # the launcher files were not staged, the install would be missing the
    # mechanism it advertises - fail the build instead of shipping that.
    absent = missing_remote_access_files(copied)
    if absent:
        fail("the staged app includes remote-access support but "
             f"{', '.join(absent)} did not ship. Add them to "
             "release_allowlist.ALLOWED_FILES.")

    log("3/10", f"staged {sum(1 for p in STAGING.rglob('*') if p.is_file())} files "
                f"(allowlist only)")
    return copied


# ------------------------------------------ 3b. bundle the tunnel helper

def bundle_cloudflared() -> str | None:
    """Copy cloudflared into the release so Public access works when installed.

    Public/LAN access is a shipped feature, so its external helper must ship
    too: a teacher should not have to install a command-line tool they have
    never heard of. ``remote.py`` looks in ``tools/`` before anywhere else, so
    dropping it here is all that is needed. A build machine without
    cloudflared is not fatal - the app reports "cloudflared is not installed"
    honestly - so the release continues with a clear warning.
    """
    exe = "cloudflared.exe" if sys.platform.startswith("win") else "cloudflared"
    candidates: list[Path] = []
    override = os.environ.get("MC5_CLOUDFLARED")
    if override:
        candidates.append(Path(override))
    found = shutil.which(exe) or shutil.which("cloudflared")
    if found:
        candidates.append(Path(found))
    candidates += [
        # ``build/`` is the documented pipeline input ("bundled runtime +
        # cloudflared"), but it was never searched: a build machine that kept
        # the helper there produced a package WITHOUT Public access support
        # while the file sat in the folder the README points at. Both names are
        # checked so a Windows-built helper is found on any build machine.
        DIST / "build" / exe,
        DIST / "build" / "cloudflared.exe",
        DIST / "build" / "cloudflared",
        Path(r"C:\Program Files (x86)\cloudflared\cloudflared.exe"),
        Path(r"C:\Program Files\cloudflared\cloudflared.exe"),
    ]

    source = next((c for c in candidates if c.is_file()), None)
    if source is None:
        log("3b/10", "WARNING: cloudflared not found on this build machine; "
                     "Public access will need it installed on the target PC.")
        return None

    # The packaged application is a WINDOWS package: it ships a Windows Python
    # runtime and ``public_access.find_cloudflared`` looks for exactly
    # ``tools/cloudflared.exe``. Naming the bundle after the BUILD machine's
    # platform (``cloudflared``, no extension, on Linux) produced a package
    # containing a 54 MB helper the app could never find - Public access stayed
    # offline while the release log claimed the helper was bundled. A
    # non-Windows binary is refused rather than copied: it cannot run on the
    # target PC at all.
    if not source.name.lower().endswith(".exe"):
        log("3b/10", f"WARNING: {source} is not a Windows executable — not "
                     f"bundling it. Public access will need cloudflared "
                     f"installed on the target PC.")
        return None

    dest_dir = STAGING / "tools"
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / "cloudflared.exe"
    shutil.copy2(source, dest)
    log("3b/10", f"bundled tunnel helper: tools/cloudflared.exe "
                  f"({dest.stat().st_size // 1024} KB, from {source})")
    return "tools/cloudflared.exe"


# ---------------------------------------------------- 4/5. scan + dependencies

def run_scan() -> None:
    log("4/10", "running secret + privacy scan…")
    result = subprocess.run(
        [sys.executable, str(HERE / "scan_release.py"), str(STAGING), "--allowlist-scan"],
        capture_output=True, text=True)
    print(result.stdout.rstrip())
    if result.returncode != 0:
        print(result.stderr.rstrip())
        fail("privacy/secret scan blocked the release (see above).")
    log("4/10", "scan clean")


#: Module names that must import inside the BUNDLED runtime. A package that is
#: installed but not importable (or missing entirely) makes the release broken
#: on the recipient's machine, which is precisely what this check is for:
#: PyMuPDF was missing from a shipped runtime once, so PDF text extraction
#: degraded silently while the manifest still listed pymupdf as bundled.
RUNTIME_IMPORTS = (
    "fastapi", "uvicorn", "multipart", "pydantic", "starlette",
    "google.genai", "google.auth", "google_auth_oauthlib", "requests",
    "httpcore", "openpyxl", "reportlab", "pymupdf", "pypdf", "PIL",
    "cryptography", "sqlite3", "ssl",
)


def requirement_packages() -> list[str]:
    req = ROOT / "backend" / "requirements.txt"
    if not req.is_file():
        fail("backend/requirements.txt missing.")
    packages: list[str] = []
    for raw in req.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        packages.append(line.split(">=")[0].split("==")[0].split("[")[0].strip())
    return packages


def _runtime_python(runtime_dir: Path) -> Path | None:
    for name in ("python.exe", "bin/python3", "bin/python"):
        candidate = runtime_dir / name
        if candidate.is_file():
            return candidate
    return None


#: Import name -> the distribution that provides it, for the packages whose
#: import name is not the name in their metadata (used by the on-disk check
#: below, which is how a bundled runtime is verified on a build machine that
#: cannot execute it).
MODULE_DISTRIBUTIONS = {
    "multipart": "python-multipart",
    "PIL": "pillow",
    "google.genai": "google-genai",
    "google.auth": "google-auth",
    "google_auth_oauthlib": "google-auth-oauthlib",
}

#: Modules CPython itself provides. They live in the runtime's stdlib, never in
#: site-packages, so a site-packages-only check must not report them missing.
STDLIB_IMPORTS = frozenset({"sqlite3", "ssl"})


def _normalise(name: str) -> str:
    """PEP 503 normalisation: ``PyMuPDF`` and ``pymupdf`` are one project."""
    return re.sub(r"[-_.]+", "-", name).strip().lower()


def _runtime_site_packages(runtime_dir: Path) -> Path | None:
    """Locate site-packages in a bundled runtime (Windows or POSIX layout)."""
    for rel in ("Lib/site-packages", "site-packages"):
        candidate = runtime_dir / rel
        if candidate.is_dir():
            return candidate
    for candidate in sorted(runtime_dir.glob("lib/python3*/site-packages")):
        if candidate.is_dir():
            return candidate
    return None


def _runtime_metadata(runtime_dir: Path) -> dict[str, str]:
    """``{normalised distribution: version}`` from the runtime's dist-info.

    Reads the runtime's own metadata instead of importing it, so the version
    recorded in the release manifest is the one the RECIPIENT will run even when
    the build machine is not the runtime's platform.
    """
    site = _runtime_site_packages(runtime_dir)
    versions: dict[str, str] = {}
    if not site:
        return versions
    for info in site.glob("*.dist-info"):
        stem = info.name[: -len(".dist-info")]
        name, _, version = stem.rpartition("-")
        if name and version:
            versions[_normalise(name)] = version
    return versions


def _runtime_zip_names(runtime_dir: Path) -> set[str]:
    """Member names inside the runtime's stdlib zip, lowercased."""
    names: set[str] = set()
    for archive in sorted(runtime_dir.glob("python3*.zip")):
        try:
            with zipfile.ZipFile(archive) as zf:
                names |= {member.lower() for member in zf.namelist()}
        except (OSError, zipfile.BadZipFile):
            continue
    return names


#: Compiled extension modules the shipped application cannot run without, as
#: ``(folder inside site-packages, file-name stem)`` - an empty folder means the
#: interpreter's own binaries (runtime root / DLLs / lib-dynload).
#:
#: These are the modules whose absence is invisible until a recipient launches
#: the app: without ``_socket`` the server cannot listen, without ``_sqlite3``
#: there is no database, without ``_ssl`` no Google/Gemini call, and PyMuPDF and
#: Pillow cannot extract or draw anything. They are ``*.pyd`` on Windows, which
#: a ``*.py[cod]`` .gitignore rule silently excludes - so the runtime in this
#: repository spent a release with none of them (see .gitignore).
RUNTIME_EXTENSION_MODULES: tuple[tuple[str, str], ...] = (
    ("", "_socket"),
    ("", "_ssl"),
    ("", "_sqlite3"),
    ("", "_ctypes"),
    ("", "_hashlib"),
    ("PIL", "_imaging"),
    ("pymupdf", "_mupdf"),
    ("pydantic_core", "_pydantic_core"),
    ("cryptography/hazmat/bindings", "_rust"),
)

#: Extensions a compiled Python module can have on each platform.
_BINARY_SUFFIXES = (".pyd", ".so", ".dll")


def _runtime_binary_dirs(runtime_dir: Path) -> list[Path]:
    """Where a runtime keeps its own compiled modules."""
    dirs = [runtime_dir, runtime_dir / "DLLs"]
    dirs += sorted(runtime_dir.glob("lib/python3*/lib-dynload"))
    return [d for d in dirs if d.is_dir()]


def _package_dirs(runtime_dir: Path, folder: str) -> list[Path]:
    """``Lib/site-packages/<folder>`` (or the POSIX equivalent)."""
    site = _runtime_site_packages(runtime_dir)
    if not site:
        return []
    candidate = site / folder
    return [candidate] if candidate.is_dir() else []


def check_runtime_extensions(runtime_dir: Path) -> list[str]:
    """Compiled modules missing from a bundled runtime (``[]`` when complete).

    Structural, not import-based: it inspects the files on disk, so it works on
    a build machine that cannot execute the runtime it packages.
    """
    missing: list[str] = []
    for folder, stem in RUNTIME_EXTENSION_MODULES:
        dirs = (_runtime_binary_dirs(runtime_dir) if not folder
                else _package_dirs(runtime_dir, folder))
        found = any(entry.is_file()
                    and entry.stem.lower().startswith(stem.lower())
                    and entry.suffix.lower() in _BINARY_SUFFIXES
                    for directory in dirs for entry in directory.iterdir())
        if not found:
            where = f"Lib/site-packages/{folder}" if folder else "the runtime itself"
            missing.append(f"{stem}* ({where})")
    return missing


def _present_in_runtime(runtime_dir: Path, module: str) -> bool:
    """Is ``module`` on disk inside this runtime? (no interpreter needed)"""
    site = _runtime_site_packages(runtime_dir)
    entries = {entry.name.lower() for entry in site.iterdir()} if site else set()
    stdlib = {entry.stem.lower() for entry in runtime_dir.glob("Lib/*")}
    dlls = {entry.name.lower() for entry in runtime_dir.glob("DLLs/*")}

    if module in STDLIB_IMPORTS:
        lower = module.lower()
        if f"{lower}.py" in stdlib or lower in stdlib:
            return True
        if any(name.startswith(f"_{lower}") and name.endswith(".pyd")
               for name in dlls):
            return True
        # A Windows embeddable runtime keeps its stdlib inside python3*.zip, as
        # bytecode only (ssl.pyc, sqlite3/__init__.pyc). The compiled backends
        # those modules need are verified separately, by
        # check_runtime_extensions().
        archived = _runtime_zip_names(runtime_dir)
        return f"{lower}.pyc" in archived or f"{lower}/__init__.pyc" in archived

    if "." in module:
        # A dotted name must resolve to its OWN package: a distribution can ship
        # the top-level namespace without the submodule the application imports.
        if site is not None and (site / module.replace(".", "/")).is_dir():
            return True
    else:
        top = module.lower()
        if top in entries or f"{top}.py" in entries:
            return True

    # Fall back to metadata, which names the project rather than the import.
    distribution = MODULE_DISTRIBUTIONS.get(module)
    if distribution and site is not None:
        prefix = _normalise(distribution)
        for entry in site.iterdir():
            name = entry.name
            if name.lower().endswith(".dist-info"):
                stem = name[: -len(".dist-info")].rpartition("-")[0]
                if _normalise(stem) == prefix:
                    return True
    return False


def _static_runtime_report(runtime_dir: Path) -> tuple[list[str], dict[str, str]]:
    """Verify a bundled runtime WITHOUT executing it.

    Used when the bundled interpreter is for another platform. Returns the
    imports that are not on disk plus the metadata-read versions.
    """
    missing = [name for name in RUNTIME_IMPORTS
               if not _present_in_runtime(runtime_dir, name)]
    return missing, _runtime_metadata(runtime_dir)


def _run_in_runtime(python: Path, code: str) -> "subprocess.CompletedProcess | None":
    """Run ``code`` with the bundled interpreter; None when it cannot run here.

    This pipeline normally runs on Windows, where the bundled runtime is a
    Windows build that executes directly. A build machine on another platform
    cannot execute a foreign ``python.exe`` at all - the OS answers
    ``OSError: Exec format error``. That is a fact about the build machine, not
    a defect in the runtime, so callers fall back to checking the runtime's own
    files instead of failing the build with a traceback.
    """
    try:
        return subprocess.run([str(python), "-c", code],
                              capture_output=True, text=True)
    except OSError as exc:
        log("5/10", f"NOTE: the bundled interpreter cannot be executed on this "
                    f"build machine ({type(exc).__name__}) - verifying the "
                    f"bundled packages on disk instead.")
        return None



def verify_bundled_runtime(runtime_dir: Path | None) -> None:
    """Fail the build when the bundled Python cannot import what it ships."""
    if not runtime_dir:
        return
    runtime_path = Path(runtime_dir).resolve()
    python = _runtime_python(runtime_path)
    if not python:
        fail(f"--with-runtime {runtime_dir} contains no python interpreter.")

    # Checked FIRST and structurally: a runtime that has lost its compiled
    # modules still contains the whole stdlib zip, so it looks complete to every
    # version/size check - it only fails when a recipient launches the app.
    missing_extensions = check_runtime_extensions(runtime_path)
    if missing_extensions:
        fail("the bundled runtime is missing compiled modules the application "
             "cannot run without: " + ", ".join(missing_extensions) + ". "
             "Rebuild it with scripts/build_runtime.py --force, and check that "
             "nothing filters *.pyd out of the runtime (see .gitignore).")
    log("5/10", f"bundled runtime compiled modules present "
                f"({len(RUNTIME_EXTENSION_MODULES)} checked)")

    code = (
        "import importlib, sys\n"
        f"names = {list(RUNTIME_IMPORTS)!r}\n"
        "bad = []\n"
        "for name in names:\n"
        "    try:\n"
        "        importlib.import_module(name)\n"
        "    except Exception as exc:\n"
        "        bad.append(f'{name}: {exc}')\n"
        "if bad:\n"
        "    print('\\n'.join(bad))\n"
        "    sys.exit(1)\n"
        "print('bundled runtime imports OK')\n"
    )
    result = _run_in_runtime(python, code)
    if result is None:
        # Foreign-platform runtime (see _run_in_runtime): verify it on disk.
        missing, _versions = _static_runtime_report(runtime_path)
        if missing:
            fail("the bundled runtime is missing packages the application "
                 "needs: " + ", ".join(missing) + ". Rebuild it with "
                 "scripts/build_runtime.py --force, or ship without "
                 "--with-runtime and tell recipients to install Python.")
        log("5/10", f"bundled runtime verified on disk - all "
                    f"{len(RUNTIME_IMPORTS)} required imports present")
        return
    if result.returncode != 0:
        print(result.stdout)
        fail("the bundled runtime cannot import a package the application needs "
             "(listed above). Rebuild it with scripts/build_runtime.py --force, "
             "or ship without --with-runtime and tell recipients to install Python.")
    log("5/10", "bundled runtime imports verified")


def validate_dependencies(runtime_dir: Path | None = None) -> list[dict]:
    log("5/10", "validating dependencies…")
    packages = requirement_packages()
    verify_bundled_runtime(runtime_dir)

    # When a runtime ships, ITS versions are what the recipient runs - report
    # those. Otherwise fall back to this build machine's environment.
    if runtime_dir:
        runtime_path = Path(runtime_dir).resolve()
        python = _runtime_python(runtime_path)
        if python:
            code = (
                "import importlib.metadata as md\n"
                f"names = {packages!r}\n"
                "for name in names:\n"
                "    try:\n"
                "        print(f'{name}\\t{md.version(name)}')\n"
                "    except Exception:\n"
                "        print(f'{name}\\tnone')\n"
            )
            result = _run_in_runtime(python, code)
            if result is None:
                # Foreign-platform runtime: read the versions it ships from its
                # own metadata, so the manifest still records what a recipient
                # runs rather than this build machine's environment.
                versions = _runtime_metadata(runtime_path)
                deps = [{"package": pkg,
                         "installed": versions.get(_normalise(pkg))}
                        for pkg in packages]
                absent = [d["package"] for d in deps if d["installed"] is None]
                if absent:
                    fail("bundled runtime does not report these requirements: "
                         + ", ".join(absent))
                log("5/10", "dependency versions read from the bundled runtime's "
                            "metadata (interpreter not executable here)")
                return deps
            deps = []
            for line in result.stdout.splitlines():
                if "\t" in line:
                    pkg, version = line.split("\t", 1)
                    deps.append({"package": pkg, "installed": None
                                 if version == "none" else version})
            absent = [d["package"] for d in deps if d["installed"] is None]
            if absent:
                fail("bundled runtime does not report these requirements: "
                     + ", ".join(absent))
            return deps

    import importlib.metadata as md
    deps = []
    missing: list[str] = []
    for pkg in packages:
        try:
            deps.append({"package": pkg, "installed": md.version(pkg)})
        except md.PackageNotFoundError:
            missing.append(pkg)
            deps.append({"package": pkg, "installed": None})
    if missing:
        log("5/10", f"WARNING: not importable in this environment: {', '.join(missing)}")
    return deps


# ------------------------------------------------------ 6. portable package

def build_portable() -> Path:
    log("6/10", "assembling portable package…")
    folder = PORTABLE_DIR / "MathChecker_Portable"
    shutil.copytree(STAGING, folder)
    # Isolated data directory the app creates on first run.
    (folder / "Data").mkdir(exist_ok=True)
    (folder / "Data" / "README.txt").write_text(
        "Your local Math Checker data lives here: configuration, database,\n"
        "credentials, cache, logs and reports. It is created fresh on each\n"
        "machine and is never shared.\n\n"
        "config.toml.example next to this file is a reference copy of every\n"
        "setting the app keeps in config.toml. It holds no credentials and is\n"
        "never read by the app - editing it does nothing.\n", encoding="utf-8")
    # Reference copy of the settings file, for a teacher who prefers to read (or
    # restore) the documented keys instead of clicking through Settings. It is
    # deliberately named .example: the app only ever reads Data\config.toml, so
    # this file can never change the behaviour of a running install.
    template = DOCS_DIR / "config.toml.example"
    if template.is_file():
        shutil.copy2(template, folder / "Data" / "config.toml.example")
    else:
        log("6/10", "WARNING: documentation/config.toml.example is missing — "
                    "the portable package ships without a settings reference")
    # Teacher-facing quick start AT THE ROOT of the extracted folder, where a
    # recipient looks first: extraction, first launch, configuration, how to
    # stop the app again, and what to do when something goes wrong.
    quick_start = DOCS_DIR / "README_PORTABLE.txt"
    if quick_start.is_file():
        shutil.copy2(quick_start, folder / "README.txt")
    else:
        log("6/10", "WARNING: documentation/README_PORTABLE.txt is missing — "
                    "the portable package ships without a quick-start file")

    zipped = RELEASE_DIR / "MathChecker_Portable.zip"
    with zipfile.ZipFile(zipped, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in sorted(folder.rglob("*")):
            if path.is_file():
                zf.write(path, path.relative_to(PORTABLE_DIR))
    log("6/10", f"portable zip: {zipped.name} "
                f"({zipped.stat().st_size // 1024} KB)")
    return zipped


# ------------------------------------------------------------- 7. installer

def prepare_installer_assets() -> None:
    """Generate the branded wizard images + icon (idempotent, Pillow-based)."""
    maker = INSTALLER_DIR / "make_installer_assets.py"
    if not maker.is_file():
        return
    result = subprocess.run([sys.executable, str(maker)], capture_output=True, text=True)
    if result.returncode != 0:
        log("assets", f"WARNING: installer assets not generated: {result.stderr.strip()}")


def build_installer(name: str, version: str, build_no: str,
                    has_runtime: bool = False) -> Path | None:
    log("7/10", "preparing Windows installer…")
    iss_template = INSTALLER_DIR / "MathChecker.iss"
    if not iss_template.is_file():
        log("7/10", "installer/MathChecker.iss not found — skipping installer")
        return None

    iss = RELEASE_DIR / "MathChecker.iss"
    iss.write_text(iss_template.read_text(encoding="utf-8")
                   .replace("@VERSION@", version)
                   .replace("@BUILD@", build_no)
                   .replace("@NAME@", name), encoding="utf-8")

    iscc = _find_iscc()
    if not iscc:
        log("7/10", "Inno Setup (ISCC) not found — wrote installer/MathChecker.iss; "
                    "compile it on a Windows build machine or install Inno Setup 6.")
        return None

    args = [iscc, f"/DSourceDir={STAGING}",
            f"/DAssetsDir={INSTALLER_DIR / 'assets'}",
            f"/DHasRuntime={'1' if has_runtime else '0'}",
            f"/O{RELEASE_DIR}", str(iss)]
    log("7/10", "compiling installer with Inno Setup…")
    result = subprocess.run(args, capture_output=True, text=True)
    if result.returncode != 0:
        print(result.stdout[-2000:])
        fail("installer compilation failed.")
    exe = RELEASE_DIR / "MathChecker_Setup.exe"
    if not exe.is_file():
        fail("installer compiled but MathChecker_Setup.exe was not produced.")
    log("7/10", f"installer: {exe.name} ({exe.stat().st_size // 1024} KB)")
    return exe


def _find_iscc() -> str | None:
    found = shutil.which("iscc") or shutil.which("ISCC")
    if found:
        return found
    candidates = [
        r"C:\Program Files (x86)\Inno Setup 6\ISCC.exe",
        r"C:\Program Files\Inno Setup 6\ISCC.exe",
    ]
    # Per-user installs (e.g. `winget install JRSoftware.InnoSetup` without
    # elevation) land under %LOCALAPPDATA%\Programs.
    for env in ("LOCALAPPDATA", "PROGRAMFILES", "PROGRAMFILES(X86)", "PROGRAMFILES(X86)"):
        base = os.environ.get(env)
        if base:
            candidates.append(os.path.join(base, "Programs", "Inno Setup 6", "ISCC.exe"))
            candidates.append(os.path.join(base, "Inno Setup 6", "ISCC.exe"))
    for candidate in candidates:
        if Path(candidate).is_file():
            return candidate
    return None


# ------------------------------------------------ 8/9. hashes + manifest + 10

def generate_hashes() -> Path:
    log("8/10", "generating SHA-256 hashes…")
    lines: list[str] = []
    for name in ("MathChecker_Setup.exe", "MathChecker_Portable.zip"):
        art = RELEASE_DIR / name
        if art.is_file():
            lines.append(f"{sha256_of(art)}  {name}")
    sums = RELEASE_DIR / "SHA256SUMS.txt"
    sums.write_text("\n".join(lines) + "\n", encoding="utf-8")
    log("8/10", f"hashes written for {len(lines)} artifact(s)")
    return sums


def generate_manifest(name: str, version: str, build_no: str,
                      deps: list[dict], staged: list[str],
                      artifacts: list[str], self_contained: bool) -> Path:
    log("9/10", "writing release manifest…")
    manifest = {
        "application": name,
        "version": version,
        "build_number": build_no,
        "build_timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "python_version": sys.version.split()[0],
        "platform": sys.platform,
        "self_contained": self_contained,
        "dependencies": deps,
        "file_count": sum(1 for p in STAGING.rglob("*") if p.is_file()),
        "staged_allowlist_entries": len(staged),
        "artifacts": [],
    }
    for art_name in artifacts:
        art = RELEASE_DIR / art_name
        if art.is_file():
            manifest["artifacts"].append({
                "name": art_name,
                "bytes": art.stat().st_size,
                "sha256": sha256_of(art),
            })
    out = RELEASE_DIR / "release_manifest.json"
    out.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    log("9/10", "release_manifest.json written")
    return out


def final_validation() -> None:
    log("10/10", "final validation…")
    result = subprocess.run(
        [sys.executable, str(HERE / "scan_release.py"), str(STAGING), "--allowlist-scan"],
        capture_output=True, text=True)
    if result.returncode != 0:
        print(result.stdout)
        fail("final validation failed: staging no longer clean.")
    for required in ("SHA256SUMS.txt", "release_manifest.json"):
        if not (RELEASE_DIR / required).is_file():
            fail(f"final validation failed: {required} missing.")
    log("10/10", "release validated")


# ------------------------------------------------------------------- main

def main() -> int:
    parser = argparse.ArgumentParser(description="Build a Math Checker release.")
    parser.add_argument("--with-runtime", default=None,
                        help="folder with a bundled Python runtime to ship")
    parser.add_argument("--skip-frontend-build", action="store_true",
                        help="reuse the existing frontend/dist (do not run npm)")
    parser.add_argument("--no-clean", action="store_true", help="keep staging")
    parser.add_argument("--no-cloudflared", action="store_true",
                        help="do not bundle the cloudflared tunnel helper")
    args = parser.parse_args()

    name, version, build_no = read_app_version()
    build_no = build_no or time.strftime("%Y%m%d", time.gmtime())

    print("=" * 64)
    print(f"Math Checker release build — {name} v{version} build {build_no}")
    print(f"  master : {ROOT}")
    print(f"  staging: {STAGING}")
    print("=" * 64, flush=True)

    if ROOT == DIST:
        fail("could not locate the private master project (parent of "
             "MathChecker_Distribution).")
    if not args.no_clean:
        clean_staging()

    prepare_installer_assets()
    verify_frontend(args.skip_frontend_build)
    staged = stage_sources(args.with_runtime)
    if not args.no_cloudflared:
        bundled_tunnel = bundle_cloudflared()
        if bundled_tunnel:
            staged.append(bundled_tunnel)
    run_scan()
    deps = validate_dependencies(args.with_runtime)

    portable_zip = build_portable()
    exe = build_installer(name, version, build_no, has_runtime=bool(args.with_runtime))

    artifacts = [portable_zip.name] + ([exe.name] if exe else [])
    generate_hashes()
    generate_manifest(name, version, build_no, deps, staged, artifacts,
                      self_contained=bool(args.with_runtime))
    final_validation()

    print("\n" + "=" * 64)
    print(f"RELEASE READY — {name} v{version} (build {build_no})")
    print(f"  {RELEASE_DIR}")
    for art in artifacts:
        print(f"    - {art}")
    print("    - SHA256SUMS.txt")
    print("    - release_manifest.json")
    if not exe:
        print("\n  NOTE: installer not compiled (Inno Setup 6 not found).")
    if not args.with_runtime:
        print("  NOTE: no Python runtime bundled — recipient machines need "
              "Python unless you rebuild with --with-runtime <folder>.")
    print("=" * 64)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
