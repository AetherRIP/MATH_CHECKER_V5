#!/usr/bin/env python3
"""Check every local link in the repository's Markdown files.

    python scripts/check_doc_links.py            # report broken links
    python scripts/check_doc_links.py --strict   # exit 1 when any is broken

Why this exists
---------------
This documentation set has been reorganised more than once, and a moved file
silently breaks every link that pointed at it — including links inside reports
years old that nobody re-reads. The checker resolves each link that looks like a
path, relative to the file containing it, and fails loudly when the target does
not exist, so a move can be verified instead of hoped for.

It checks:
  * inline links  ``[text](target)``   (skipping http(s), mailto, anchors)
  * image links   ``![alt](target)``
  * reference definitions ``[label]: target``

Anchors (``file.md#section``) are resolved by file only: a stale ``#anchor`` is
reported separately and never fails the run, because heading text is allowed to
change without a file moving.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):  # pragma: no cover
        pass

REPO = Path(__file__).resolve().parents[1]

#: Directories that are not documentation: build output, dependencies, the
#: bundled runtime, generated graph data and ignored local scratch.
SKIP_DIRS = {
    ".git", ".venv", ".venv.foreign", "node_modules", "graphify-out",
    "MathChecker_Distribution", "dev_local", "release", ".tmp",
    "frontend/dist", "bin", "obj", "__pycache__",
    # Editor/agent tooling installed on one machine: third-party skill text,
    # not this project's documentation.
    ".agents", ".claude", ".opencode",
}

_LINK = re.compile(r"!?\[[^\]]*\]\(([^)]+)\)")
_DEFINITION = re.compile(r"^\s*\[[^\]]+\]:\s*(\S+)", re.MULTILINE)


def markdown_files() -> list[Path]:
    files = []
    for path in REPO.rglob("*.md"):
        parts = set(path.relative_to(REPO).parts[:-1])
        # A dot-directory only counts when it is a TOP-LEVEL one: the bundled
        # runtime legitimately vendors packages that contain their own .agents/
        # folder, and those must not be treated as this repository's tooling.
        if parts & (SKIP_DIRS - {".agents", ".claude", ".opencode"}):
            continue
        top = path.relative_to(REPO).parts[0]
        if top in {".agents", ".claude", ".opencode"}:
            continue
        if any(part in {"bin", "obj", "node_modules"} for part in parts):
            continue
        files.append(path)
    return sorted(files)


def _targets(text: str) -> list[str]:
    found = _LINK.findall(text) + _DEFINITION.findall(text)
    return [target.strip().strip("<>") for target in found]


def _is_external(target: str) -> bool:
    lowered = target.lower()
    return (lowered.startswith(("http://", "https://", "mailto:", "tel:"))
            or lowered.startswith("data:"))


def check(strict: bool) -> int:
    files = markdown_files()
    broken: list[tuple[Path, str]] = []
    stale_anchor: list[tuple[Path, str]] = []
    checked = 0

    for path in files:
        text = path.read_text(encoding="utf-8", errors="replace")
        for target in _targets(text):
            if not target or _is_external(target) or target.startswith("#"):
                continue
            relative, _, anchor = target.partition("#")
            if not relative:
                continue
            checked += 1
            resolved = (path.parent / relative).resolve()
            try:
                resolved.relative_to(REPO)
            except ValueError:
                # Link escapes the repository; that is worth knowing about.
                broken.append((path, target))
                continue
            if not resolved.exists():
                broken.append((path, target))
            elif anchor and resolved.suffix == ".md":
                if f"#{anchor}" in text and anchor not in resolved.read_text(
                        encoding="utf-8", errors="replace"):
                    stale_anchor.append((path, target))

    print(f"{len(files)} Markdown files, {checked} local links checked")
    for path, target in broken:
        print(f"BROKEN  {path.relative_to(REPO)}  ->  {target}")
    if stale_anchor:
        print(f"\n{len(stale_anchor)} anchor(s) not found (heading renamed?):")
        for path, target in stale_anchor:
            print(f"  STALE  {path.relative_to(REPO)}  ->  {target}")
    if broken:
        print(f"\n{len(broken)} broken link(s).")
        return 1 if strict else 0
    print("\nno broken links.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--strict", action="store_true",
                        help="exit non-zero when a link is broken")
    args = parser.parse_args()
    return check(args.strict)


if __name__ == "__main__":
    sys.exit(main())
