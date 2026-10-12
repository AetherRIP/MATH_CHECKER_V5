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

def clean_staging() -> None:
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
        Path(r"C:\Program Files (x86)\cloudflared\cloudflared.exe"),
        Path(r"C:\Program Files\cloudflared\cloudflared.exe"),
    ]

    source = next((c for c in candidates if c.is_file()), None)
    if source is None:
        log("3b/10", "WARNING: cloudflared not found on this build machine; "
                     "Public access will need it installed on the target PC.")
        return None

    dest_dir = STAGING / "tools"
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / exe
    shutil.copy2(source, dest)
    log("3b/10", f"bundled tunnel helper: tools/{exe} "
                  f"({dest.stat().st_size // 1024} KB, from {source})")
    return f"tools/{exe}"


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


def verify_bundled_runtime(runtime_dir: Path | None) -> None:
    """Fail the build when the bundled Python cannot import what it ships."""
    if not runtime_dir:
        return
    python = _runtime_python(Path(runtime_dir).resolve())
    if not python:
        fail(f"--with-runtime {runtime_dir} contains no python interpreter.")
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
    result = subprocess.run([str(python), "-c", code],
                            capture_output=True, text=True)
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
        python = _runtime_python(Path(runtime_dir).resolve())
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
            result = subprocess.run([str(python), "-c", code],
                                    capture_output=True, text=True)
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
