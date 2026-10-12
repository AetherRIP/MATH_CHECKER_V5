#!/usr/bin/env python3
"""Explicit production ALLOWLIST for Math Checker releases.

The release system is allowlist-first: a file ships ONLY if this module
explicitly says it belongs in the production application. Anything not named
here — a new experiment, a stray data file, a credential, the private
``Friend code/`` folder — is silently *not copied*, and the separate privacy
scan fails the build if something suspicious was somehow staged.

This file never modifies the private project: staging only ever READS from it.

The rules:
  * ALLOWED_FILES  — exact repo-relative files that ship
  * ALLOWED_TREES  — (directory, allowed extensions) shipped recursively
  * DENIED_NAMES   — hard refusals, even if a parent tree is allowed
  * DENIED_GLOBS   — hard refusals by glob (secrets, credentials, personal data)
"""

from __future__ import annotations

# ---- exact files that are required at runtime --------------------------------

ALLOWED_FILES: tuple[str, ...] = (
    "main.py",
    "start.bat",
    "start.sh",
    "README.md",
    "backend/requirements.txt",
    # Public / LAN access. These are REQUIRED at runtime, not optional extras:
    # the Settings panel and the sign-in screen tell the teacher to run
    # start_public.bat, and that file needs remote.py beside it. Shipping the
    # app without them produces an install whose remote access cannot be
    # started at all - the exact "installer is behind the app" failure this
    # allowlist exists to prevent.
    "start_public.bat",
    "remote.py",
    # NOTE: backend/app/reporting/assets/logo.png and signature.png are
    # deliberately NOT shipped. They are the build owner's personal logo and
    # signature; bundling them put one person's identity on every report of
    # every install. A distributed build renders the drawn monogram instead,
    # and a teacher configures their own logo/signature in Settings.
)

# ---- recursive trees (only these extensions are copied) ----------------------

ALLOWED_TREES: tuple[tuple[str, tuple[str, ...]], ...] = (
    # Backend application code (the whole FastAPI app + schema + engine).
    ("backend/app", (".py", ".sql")),
    # The tiny agent's SOURCE is preserved and still shipped, but this version
    # does NOT run it: ``[agent] tiny_agent`` defaults to false, the agent's
    # auto-start is refused by scripts/install_autostart.py, and the backend
    # reports "Disabled in this version" (see app/distributed/agent_control.py).
    # It is kept on the allowlist so re-enabling the agent needs no release
    # change, and so the historical device/account records stay meaningful.
    # Only source ships - the per-device identity files (agent/device_id,
    # agent/agent.json) are excluded by the .py gate and never copied.
    ("agent", (".py",)),
    # The built frontend (served by the backend). Source/package files are NOT
    # shipped: the distribution runs the compiled bundle only.
    ("frontend/dist", (".html", ".js", ".css", ".png", ".svg", ".ico",
                       ".woff", ".woff2", ".ttf", ".json", ".map")),
)

# ---- hard denials ------------------------------------------------------------

DENIED_NAMES: frozenset[str] = frozenset({
    "data", "Friend code", "Friend Code", ".git", ".venv", "venv", "runtime",
    "node_modules", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache",
    ".freebuff", "logs", "tmp", "reports", "mark_schemes", "blueprints",
    "retained", "tests", "scripts", ".gitattributes",
})

DENIED_GLOBS: tuple[str, ...] = (
    # secrets / credentials / keys
    "*secret*", "*credential*", "*.env", "*.pem", "*.key", "*.p12", "*.pfx",
    "*.token", "*.refresh*", "api_keys*", "oauth*", "client_secret*",
    "secrets.toml", "config.toml",
    # personal / student / generated data
    "*.db", "*.db-wal", "*.db-shm", "*.db-journal", "*.sqlite", "*.sqlite3",
    "*.pdf", "*.docx", "*.xlsx", "*.csv",
    # private source of the original app
    "app5_1_automated.py",
    # dev material
    "*.log", "*.pyc", "*.pyo", "*.pytest_cache", "*.tsbuildinfo",
    "*.md.bak", "*~",
)


def is_allowed(rel_path: str) -> bool:
    """Return True only when ``rel_path`` (POSIX, repo-relative) may ship."""
    import posixpath

    rel = rel_path.replace("\\", "/").lstrip("./")
    parts = rel.split("/")
    name = parts[-1]
    lower = rel.lower()

    # never descend into a denied folder name anywhere in the path
    for part in parts[:-1]:
        if part in DENIED_NAMES:
            return False
    if name in DENIED_NAMES:
        return False

    # denied by glob against either the whole path or the file name
    import fnmatch
    for pattern in DENIED_GLOBS:
        if fnmatch.fnmatch(lower, pattern) or fnmatch.fnmatch(name.lower(), pattern):
            return False

    # exact files
    if rel in ALLOWED_FILES:
        return True

    # allowed trees, extension gated
    for tree, exts in ALLOWED_TREES:
        prefix = tree.rstrip("/") + "/"
        if rel.startswith(prefix):
            import os
            return os.path.splitext(name)[1].lower() in exts
    return False


#: When these exist in a staged release, this one must too. Used by the
#: release build as a hard consistency check (see build_release.py).
REMOTE_ACCESS_REQUIRED: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("backend/app/remote_access.py", ("remote.py", "start_public.bat")),
)


def missing_remote_access_files(staged: "list[str]") -> list[str]:
    """Names that must be staged alongside the app's remote-access support."""
    present = {p.replace("\\", "/") for p in staged}
    absent: list[str] = []
    for trigger, required in REMOTE_ACCESS_REQUIRED:
        if trigger not in present:
            continue
        absent.extend(name for name in required if name not in present)
    return absent


def describe() -> str:
    """Human-readable summary printed by the build script."""
    lines = ["Production allowlist", "=" * 21, "Exact files:"]
    lines += [f"  + {f}" for f in ALLOWED_FILES]
    lines.append("Trees (extension-gated):")
    lines += [f"  + {tree}/  {', '.join(exts)}" for tree, exts in ALLOWED_TREES]
    lines.append("Hard denials (never shipped):")
    lines += [f"  - {g}" for g in DENIED_GLOBS]
    lines.append(f"  - folders: {', '.join(sorted(DENIED_NAMES))}")
    return "\n".join(lines)


if __name__ == "__main__":
    print(describe())
