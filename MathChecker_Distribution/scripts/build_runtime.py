#!/usr/bin/env python3
"""Build the bundled, self-contained Python runtime for a Math Checker release.

Produces ``build/runtime/`` — a stock CPython *embeddable* distribution with the
backend's dependencies installed — so the installer and portable package run on
a machine with no Python, Node or developer tools.

This is a DISTRIBUTION-OWNED artifact: it is downloaded from python.org and
PyPI and is never sourced from the private project, so it can never contain
your API keys, OAuth tokens, database or student data. That is also why the
release scanner applies a narrow, user-data-focused rule set to ``runtime/``
(see ``scan_release.py``).

    python scripts/build_runtime.py                 # build into build/runtime
    python scripts/build_runtime.py --force         # rebuild from scratch
    python scripts/build_runtime.py --version 3.13.7

The runtime is written only inside ``MathChecker_Distribution``; the private
master project is never touched.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import urllib.request
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
DIST = HERE.parent
ROOT = DIST.parent
RUNTIME = DIST / "build" / "runtime"
REQUIREMENTS = ROOT / "backend" / "requirements.txt"

DEFAULT_VERSION = "3.13.7"
PYPI_GET_PIP = "https://bootstrap.pypa.io/get-pip.py"

# Import-names that must resolve inside the finished runtime.
#
# EVERY runtime-critical dependency belongs here. PyMuPDF was missing from this
# list once while backend/requirements.txt called it required: the runtime build
# passed, the release shipped, and PDF text extraction (mark-scheme analysis)
# silently degraded on the recipient's machine with "no PDF text extractor
# available". A dependency that ships must be proved importable in the runtime
# that ships it, not in the build machine's own Python.
SMOKE_IMPORTS = (
    "fastapi", "uvicorn", "multipart", "pydantic", "starlette",
    "google.genai", "google.auth", "google_auth_oauthlib", "requests",
    "openpyxl", "reportlab", "pymupdf", "pypdf", "PIL", "cryptography",
    "ssl", "sqlite3",
)

# Distribution name in requirements.txt -> module that must import.
REQUIREMENT_MODULES = {
    "fastapi": "fastapi",
    "uvicorn": "uvicorn",
    "python-multipart": "multipart",
    "google-genai": "google.genai",
    "google-auth": "google.auth",
    "google-auth-oauthlib": "google_auth_oauthlib",
    "requests": "requests",
    "httpx": "httpx",
    "openpyxl": "openpyxl",
    "reportlab": "reportlab",
    "pymupdf": "pymupdf",
    "pypdf": "pypdf",
    "pillow": "PIL",
    "cryptography": "cryptography",
}


def log(message: str) -> None:
    print(f"[runtime] {message}", flush=True)


def fail(message: str) -> "NoReturn":  # type: ignore[name-defined]
    print(f"\nRUNTIME BUILD FAILED: {message}", flush=True)
    raise SystemExit(1)


def download(url: str, dest: Path) -> None:
    log(f"downloading {url}")
    with urllib.request.urlopen(url, timeout=180) as response, dest.open("wb") as fh:
        shutil.copyfileobj(response, fh)
    log(f"  -> {dest.name} ({dest.stat().st_size // 1024} KB)")


def build(version: str, force: bool) -> Path:
    if RUNTIME.exists():
        if not force:
            log(f"{RUNTIME} already exists — reuse it (pass --force to rebuild).")
            return RUNTIME
        shutil.rmtree(RUNTIME)
    RUNTIME.mkdir(parents=True)

    build_dir = RUNTIME.parent
    embed_url = (f"https://www.python.org/ftp/python/{version}/"
                 f"python-{version}-embed-amd64.zip")
    embed_zip = build_dir / f"python-{version}-embed-amd64.zip"
    download(embed_url, embed_zip)

    log("extracting embeddable interpreter")
    with zipfile.ZipFile(embed_zip) as zf:
        zf.extractall(RUNTIME)

    # Enable site-packages + site.main() in the embeddable's path file.
    pth = next(RUNTIME.glob("python3*._pth"))
    tag = pth.stem.replace("python", "")            # e.g. "313"
    pth.write_text(
        f"python{tag}.zip\r\n.\r\nLib\\site-packages\r\nimport site\r\n",
        encoding="utf-8")
    log(f"configured {pth.name}")

    python = RUNTIME / "python.exe"
    if not python.is_file():
        fail("embeddable python.exe not found after extraction")

    get_pip = build_dir / "get-pip.py"
    download(PYPI_GET_PIP, get_pip)
    log("bootstrapping pip")
    run([str(python), str(get_pip), "--no-warn-script-location"])
    get_pip.unlink(missing_ok=True)

    if not REQUIREMENTS.is_file():
        fail(f"requirements file missing: {REQUIREMENTS}")
    log("installing backend dependencies")
    run([str(python), "-m", "pip", "install", "--no-warn-script-location",
         "-r", str(REQUIREMENTS)])

    log("running import smoke test")
    code = "; ".join(f"import {name}" for name in SMOKE_IMPORTS)
    run([str(python), "-c", f"{code}; print('all runtime imports OK')"])

    # Second, independent check: every requirement must resolve in THIS runtime
    # (a package can install and still not import).
    names = sorted({REQUIREMENT_MODULES.get(pkg, pkg.replace("-", "_"))
                    for pkg in required_packages()})
    log(f"verifying {len(names)} requirement modules in the bundled runtime")
    check = (
        "import importlib, sys\n"
        f"names = {names!r}\n"
        "bad = []\n"
        "for name in names:\n"
        "    try:\n"
        "        importlib.import_module(name)\n"
        "    except Exception as exc:\n"
        "        bad.append(f'{name}: {exc}')\n"
        "print('MISSING:', bad) if bad else print('all requirements import OK')\n"
        "sys.exit(1 if bad else 0)\n"
    )
    result = subprocess.run([str(python), "-c", check])
    if result.returncode != 0:
        fail("the bundled runtime cannot import one or more required packages "
             "(see the list above). The release would be broken on a machine "
             "without Python.")

    files = sum(1 for _ in RUNTIME.rglob("*") if _.is_file())
    log(f"runtime ready: {files} files, "
        f"{sum(p.stat().st_size for p in RUNTIME.rglob('*') if p.is_file()) // (1024 * 1024)} MB")
    return RUNTIME


def required_packages() -> list[str]:
    """Distribution names from backend/requirements.txt (comments stripped)."""
    if not REQUIREMENTS.is_file():
        return []
    packages: list[str] = []
    for raw in REQUIREMENTS.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        name = line.split(">=")[0].split("==")[0].split("[")[0].strip()
        if name:
            packages.append(name.lower())
    return packages


def run(cmd: list[str]) -> None:
    result = subprocess.run(cmd)
    if result.returncode != 0:
        fail(f"command failed ({result.returncode}): {' '.join(cmd)}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Build the bundled Python runtime.")
    parser.add_argument("--version", default=DEFAULT_VERSION,
                        help=f"CPython version to bundle (default {DEFAULT_VERSION})")
    parser.add_argument("--force", action="store_true",
                        help="rebuild even if a runtime already exists")
    args = parser.parse_args()

    if sys.version_info < (3, 8):
        fail("this build helper needs Python 3.8+")

    build(args.version, args.force)
    log(f"done — pass it to the release build: "
        f"python scripts/build_release.py --with-runtime {RUNTIME}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
