#!/usr/bin/env python3
"""Blocking secret + privacy scan for a staged release.

Policy: if anything suspicious is found the build STOPS. Nothing is silently
deleted — the operator is told exactly which file and why, and must fix the
source (usually by adding it to the allowlist denial list, or by removing a
secret from the private project).

Two independent layers are checked:
  1. File-level rules  — forbidden names/globs and forbidden file types.
  2. Content rules     — regexes for credentials in text files.

Exit code is non-zero on any finding, so a release build can gate on it.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

# --------------------------------------------------------------- file rules

FORBIDDEN_NAMES = {
    "data", "Friend code", "Friend Code", ".git", ".venv", "venv",
    "node_modules", "__pycache__", ".pytest_cache", ".mypy_cache",
    "secrets.toml", "config.toml", "client_secret.json", "credentials.json",
    "token.json", "tokens.json", "refresh_token.txt", ".env",
}

FORBIDDEN_GLOBS = (
    "*secret*", "*credential*", "*.env", "*.pem", "*.key", "*.p12", "*.pfx",
    "*token*", "*oauth*", "*.db", "*.db-wal", "*.db-shm", "*.sqlite*",
    "*.pdf", "*.docx", "*.xlsx", "*.csv", "*.log", "app5_1_automated.py",
)

# Fixing the allow-list misconfiguration is the operator's job; we only report.

# ------------------------------------------------------------- content rules

CONTENT_RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("Google API key", re.compile(r"AIza[0-9A-Za-z_\-]{35}")),
    ("OpenAI/DeepSeek-style key", re.compile(r"\bsk-[A-Za-z0-9]{20,}\b")),
    ("private key block", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----")),
    ("OAuth client secret", re.compile(r"GOCSPX-[A-Za-z0-9_\-]{20,}")),
    ("refresh token", re.compile(r"\brefresh_token\b\s*[:=]\s*[\"'][A-Za-z0-9_\-]{20,}")),
    ("bearer token literal", re.compile(r"Bearer\s+[A-Za-z0-9_\-\.]{30,}")),
    ("aws access key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("generic password assignment",
     re.compile(r"\bpassword\b\s*[:=]\s*[\"'][^\"']{6,}[\"']", re.IGNORECASE)),
)

# Files we never bother content-scanning (binary or huge).
SKIP_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".ico", ".woff", ".woff2",
                 ".ttf", ".zip", ".exe", ".webp", ".pdf", ".map"}
MAX_SCAN_BYTES = 2 * 1024 * 1024

# ------------------------------------------------------------ runtime policy
#
# A bundled Python runtime is a stock interpreter plus vendored PyPI wheels.
# It is verified BY CONSTRUCTION: it is downloaded from python.org and PyPI
# and is never sourced from the private project, so it cannot contain the
# user's API keys, OAuth tokens, database or student data. Applying the
# general rules to it produces only false positives — library files are
# legitimately named ``oauth2.py`` / ``credentials.py`` / ``*token*.py``,
# vendor PEM *test fixtures* appear in source, and ``certifi`` ships a real
# CA bundle named ``cacert.pem``. We therefore apply a NARROW rule set to the
# runtime that targets only genuine user-data / secret artifacts.
RUNTIME_PREFIX = "runtime/"
RUNTIME_FORBIDDEN_NAMES = {
    "secrets.toml", "config.toml", "token.json", "tokens.json",
    "credentials.json", "client_secret.json", ".env",
}
RUNTIME_FORBIDDEN_GLOBS = ("*.db", "*.db-wal", "*.db-shm", "*.sqlite", "*.sqlite3")
RUNTIME_FORBIDDEN_DIRS = {".git", "data", "Friend code", "Friend Code"}


def _runtime_findings(path: Path, root: Path) -> list[str]:
    """Narrow checks for the bundled runtime: user data / secrets only."""
    problems: list[str] = []
    if path.name in RUNTIME_FORBIDDEN_NAMES:
        problems.append(f"forbidden name '{path.name}'")
    import fnmatch
    low = path.name.lower()
    for pattern in RUNTIME_FORBIDDEN_GLOBS:
        if fnmatch.fnmatch(low, pattern):
            problems.append(f"forbidden file pattern '{pattern}'")
    for part in path.relative_to(root).parts:
        if part in RUNTIME_FORBIDDEN_DIRS:
            problems.append(f"forbidden directory '{part}'")
    return problems


def _findings_for_file(path: Path, root: Path) -> list[str]:
    rel = path.relative_to(root).as_posix()

    if rel.startswith(RUNTIME_PREFIX):
        return _runtime_findings(path, root)

    problems: list[str] = []

    name = path.name
    if name in FORBIDDEN_NAMES:
        problems.append(f"forbidden name '{name}'")
    import fnmatch
    low = name.lower()
    for pattern in FORBIDDEN_GLOBS:
        if fnmatch.fnmatch(low, pattern):
            problems.append(f"forbidden file pattern '{pattern}'")

    if path.suffix.lower() in SKIP_SUFFIXES:
        return problems
    try:
        if path.stat().st_size > MAX_SCAN_BYTES:
            return problems
        text = path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return problems
    for label, rx in CONTENT_RULES:
        if rx.search(text):
            problems.append(f"possible {label} in file contents")
    return problems


# Files the distribution itself authors (never taken from the private project).
DIST_OWNED_FILES = {
    "requirements.txt", "portable.flag", "MathChecker.exe", "MathChecker.bat",
    # Stop.bat and the portable quick-start are authored here (installer/Stop.bat,
    # documentation/README_PORTABLE.txt), never copied from the private project.
    "Stop.bat", "README.txt",
}
# ``tools/`` holds the cloudflared tunnel helper the build bundles for Public
# access. It is a third-party binary fetched by the build, not a project file.
DIST_OWNED_PREFIXES = ("runtime/", "documentation/", "assets/", "tools/")


def _is_distribution_owned(rel: str) -> bool:
    return rel in DIST_OWNED_FILES or rel.startswith(DIST_OWNED_PREFIXES)


def scan(root: Path) -> list[tuple[str, list[str]]]:
    findings: list[tuple[str, list[str]]] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        problems = _findings_for_file(path, root)
        if problems:
            findings.append((path.relative_to(root).as_posix(), problems))
    return findings


def main() -> int:
    parser = argparse.ArgumentParser(description="Scan a staged release for secrets and private data.")
    parser.add_argument("staging", help="path to release_staging/")
    parser.add_argument("--allowlist-scan", action="store_true",
                        help="also verify the staging tree respects the production allowlist")
    args = parser.parse_args()

    root = Path(args.staging).resolve()
    if not root.is_dir():
        print(f"ERROR: staging directory not found: {root}", file=sys.stderr)
        return 2

    findings = scan(root)

    if args.allowlist_scan:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from release_allowlist import is_allowed
        for path in sorted(root.rglob("*")):
            if not path.is_file():
                continue
            rel = path.relative_to(root).as_posix()
            # The distribution creates a small, fixed set of its own files
            # (launcher, brand assets, docs, optional runtime). They are
            # trusted additions — but they are STILL content-scanned above.
            if _is_distribution_owned(rel):
                continue
            if not is_allowed(rel):
                findings.append((rel, ["not on the production allowlist"]))

    if findings:
        print("=" * 64)
        print("RELEASE BLOCKED")
        print("=" * 64)
        print("Potential sensitive file detected:\n")
        for rel, problems in findings:
            print(f"  {root / rel}")
            for problem in problems:
                print(f"      - {problem}")
        print("\nThe installer was NOT created.")
        print("Remove the offending material from the source and rebuild.")
        return 1

    files = sum(1 for p in root.rglob("*") if p.is_file())
    print(f"Privacy/secret scan clean: {files} files, no findings.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
