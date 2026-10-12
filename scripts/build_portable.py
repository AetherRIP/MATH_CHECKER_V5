#!/usr/bin/env python3
"""Build a portable Math Checker distribution (§2/§29/§30).

Assembles a self-contained folder a teacher can copy to another computer:

    MathCorrector/
        MathCorrector.exe     (Windows only, optional — needs a launcher)
        start.bat / start.sh
        portable.flag         (keeps data beside the app)
        main.py
        backend/              (application code + tests)
        frontend/dist/        (built UI — served by the backend)
        requirements.txt
        README.md
        data/                 (empty: config, database, reports, logs created here)

The script does NOT bundle a Python interpreter by default (that is a large,
platform-specific step); it verifies the frontend build exists and copies
everything else. Point ``--with-runtime`` at an existing virtual environment
to copy it into ``runtime/`` so the target machine needs no Python install.

Usage:
    python scripts/build_portable.py                 # -> dist/MathCorrector/
    python scripts/build_portable.py --with-runtime .venv
"""
from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Files/folders that must never end up in a shared distribution (§36/§27):
# the developer's own data, secrets, credentials and caches.
EXCLUDE_NAMES = {
    "data", ".git", ".venv", "venv", "__pycache__", ".pytest_cache",
    ".mypy_cache", "node_modules", ".freebuff", "runtime", "dist",
    "secrets.toml", "config.toml",
    # Per-DEVICE identity of the tiny agent (§16/§21): copying it would make
    # every install claim to be the same worker. Each machine mints its own.
    "device_id", "agent.json",
}
EXCLUDE_SUFFIXES = {".pyc", ".pyo", ".log", ".out"}
SECRET_GLOBS = ("client_secret_*.json", "google_oauth_client*.json", "*.env")


def _ignore(directory: str, names: list[str]):
    ignored = set()
    for name in names:
        path = Path(directory) / name
        if name in EXCLUDE_NAMES:
            ignored.add(name)
            continue
        if path.suffix in EXCLUDE_SUFFIXES:
            ignored.add(name)
            continue
        for pattern in SECRET_GLOBS:
            if path.match(pattern):
                ignored.add(name)
                break
    return ignored


def main() -> int:
    parser = argparse.ArgumentParser(description="Build a portable distribution.")
    parser.add_argument("--out", default="dist/MathCorrector",
                        help="output folder (default: dist/MathCorrector)")
    parser.add_argument("--with-runtime", default=None,
                        help="path to a virtual environment to bundle as runtime/")
    args = parser.parse_args()

    out = (ROOT / args.out).resolve()
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)

    # Frontend must be built first — the backend serves frontend/dist.
    dist_dir = ROOT / "frontend" / "dist"
    if not (dist_dir / "index.html").is_file():
        print("ERROR: frontend/dist is missing. Run: cd frontend && npm run build",
              file=sys.stderr)
        return 1

    for name in ("main.py", "README.md", "start.bat", "start.sh"):
        src = ROOT / name
        if src.is_file():
            shutil.copy2(src, out / name)
    if (out / "start.sh").is_file():
        (out / "start.sh").chmod(0o755)

    shutil.copytree(ROOT / "backend", out / "backend", ignore=_ignore)
    # The tiny agent must ship: it is the only thing a worker device runs to
    # heartbeat/register (DISTRIBUTED_DEPLOYMENT.md -> agent/agent.py).
    # Without it a portable install can never appear in Multi-School > Workers.
    agent_src = ROOT / "agent"
    if (agent_src / "agent.py").is_file():
        shutil.copytree(agent_src, out / "agent", ignore=_ignore)
    else:
        print("WARNING: agent/agent.py is missing - workers will not be able "
              "to register from this distribution.", file=sys.stderr)
    shutil.copytree(ROOT / "frontend" / "dist", out / "frontend" / "dist")
    shutil.copy2(ROOT / "backend" / "requirements.txt", out / "requirements.txt")

    # Portable flag: keeps data beside the application on the target machine.
    (out / "portable.flag").write_text(
        "Portable install: data is kept in ./data next to the application.\n",
        encoding="utf-8")
    (out / "data").mkdir(exist_ok=True)
    # Mirror the folders the app actually creates (config.toml + secrets.toml
    # live directly in data/; the rest are subfolders).
    for sub in ("secrets", "logs", "tmp", "reports", "mark_schemes",
                "blueprints", "retained"):
        (out / "data" / sub).mkdir(exist_ok=True)
    (out / "data" / "README.txt").write_text(
        "This folder holds your local data: configuration, database, reports\n"
        "and logs. It is created fresh on each machine and never shared.\n",
        encoding="utf-8")

    if args.with_runtime:
        runtime_src = Path(args.with_runtime).resolve()
        if not runtime_src.is_dir():
            print(f"ERROR: runtime path not found: {runtime_src}", file=sys.stderr)
            return 1
        shutil.copytree(runtime_src, out / "runtime", ignore=_ignore)

    # Safety check: no secret material may have slipped through (§27/§36).
    leaked = []
    for path in out.rglob("*"):
        if path.is_file():
            for pattern in SECRET_GLOBS:
                if path.match(pattern):
                    leaked.append(path)
    if leaked:
        print("ERROR: credential files found in the distribution:", file=sys.stderr)
        for path in leaked:
            print(f"  {path}", file=sys.stderr)
        return 1

    print(f"Portable distribution ready: {out}")
    print("Copy the whole folder to another computer and run start.bat/start.sh.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
