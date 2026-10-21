#!/usr/bin/env python3
"""Report where the disk space of a Math Checker checkout actually goes.

The repository is much larger on disk than its source code, because it also
holds a release workspace, virtual environments and runtime user data. This
script measures each of those groups *separately*, so "the project is 3 GB" can
be answered with facts instead of guesses.

Usage
-----
    python scripts/size_audit.py                 # human-readable report
    python scripts/size_audit.py --json          # machine-readable report
    python scripts/size_audit.py --top 30        # longer largest-file list
    python scripts/size_audit.py --fail-over-bytes 2500000000
                                                 # exit 1 when over a budget

Categories (first matching path prefix wins)
--------------------------------------------
``git``               ``.git/`` — history and object store
``python_env``        ``.venv/``, ``.venv.foreign/``, ``venv/``, ``runtime/``
``node_deps``         ``frontend/node_modules/`` — from package-lock.json
``caches``            ``__pycache__``, ``.pytest_cache``, ``*.pyc`` ...
``release_build``     ``MathChecker_Distribution/{build,portable,
                      release_staging,previous_builds}/`` — pipeline copies
``release_artifacts`` ``MathChecker_Distribution/release/`` — hand-off files
``user_data``         ``data/`` — submissions, mark schemes, database, reports
``dev_scratch``       ``dev_local/``, ``.freebuff/``, ``autostart/``, ``state/``
``source``            everything else (app code, tests, docs, packaging)

Nothing is deleted by this script; it only reads file metadata.
"""

from __future__ import annotations

import argparse
import heapq
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

CATEGORY_PATHS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("git", (".git",)),
    ("python_env", (".venv", ".venv.foreign", "venv", "runtime")),
    ("node_deps", ("frontend/node_modules",)),
    ("release_build", (
        "MathChecker_Distribution/build",
        "MathChecker_Distribution/portable",
        "MathChecker_Distribution/release_staging",
        "MathChecker_Distribution/previous_builds",
    )),
    ("release_artifacts", ("MathChecker_Distribution/release",)),
    ("user_data", ("data",)),
    ("dev_scratch", ("dev_local", ".freebuff", "autostart", "state")),
)

CACHE_DIR_NAMES = frozenset({
    "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache",
    ".hypothesis", ".tox",
})
CACHE_SUFFIXES = (".pyc", ".pyo")


def category_for(rel_posix: str) -> str:
    """Return the reporting category of a repo-relative POSIX path."""
    for category, prefixes in CATEGORY_PATHS:
        for prefix in prefixes:
            if rel_posix == prefix or rel_posix.startswith(prefix + "/"):
                return category
    return "source"


def _is_cache(rel_parts: tuple[str, ...]) -> bool:
    if any(part in CACHE_DIR_NAMES for part in rel_parts[:-1]):
        return True
    return bool(rel_parts) and rel_parts[-1].endswith(CACHE_SUFFIXES)


def scan(root: Path = ROOT, top: int = 20) -> dict:
    """Walk ``root`` once and return sizes by category, top-level and file."""
    totals: dict[str, int] = {}
    counts: dict[str, int] = {}
    top_level: dict[str, int] = {}
    largest: list[tuple[int, str]] = []
    total_bytes = 0
    total_files = 0

    for dirpath, _dirnames, filenames in os.walk(root):
        rel_dir = Path(dirpath).relative_to(root)
        dir_parts = () if rel_dir.as_posix() == "." else rel_dir.parts
        for name in filenames:
            try:
                size = (Path(dirpath) / name).stat(follow_symlinks=False).st_size
            except OSError:
                continue
            parts = dir_parts + (name,)
            rel_file = "/".join(parts)
            category = category_for(rel_file)
            if category == "source" and _is_cache(parts):
                category = "caches"
            totals[category] = totals.get(category, 0) + size
            counts[category] = counts.get(category, 0) + 1
            total_bytes += size
            total_files += 1
            key = parts[0]
            top_level[key] = top_level.get(key, 0) + size
            if len(largest) < top:
                heapq.heappush(largest, (size, rel_file))
            elif size > largest[0][0]:
                heapq.heapreplace(largest, (size, rel_file))

    return {
        "root": str(root),
        "total_bytes": total_bytes,
        "total_files": total_files,
        "categories": {k: {"bytes": v, "files": counts[k]}
                       for k, v in sorted(totals.items(), key=lambda kv: -kv[1])},
        "top_level": dict(sorted(top_level.items(), key=lambda kv: -kv[1])),
        "largest_files": [
            {"path": path, "bytes": size}
            for size, path in sorted(largest, reverse=True)
        ],
    }


def human(num: int) -> str:
    value = float(num)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if value < 1024 or unit == "TiB":
            return f"{int(value):,} B" if unit == "B" else f"{value:,.1f} {unit}"
        value /= 1024
    return f"{num:,} B"


def render(report: dict) -> str:
    out: list[str] = []
    add = out.append
    add("Math Checker - repository size audit")
    add("=" * 38)
    add(f"root:  {report['root']}")
    add(f"total: {human(report['total_bytes'])} in {report['total_files']:,} files")
    add("")
    add("By category")
    add("-" * 38)
    for name, data in report["categories"].items():
        share = (data["bytes"] / report["total_bytes"] * 100) if report["total_bytes"] else 0
        add(f"  {name:<18}{human(data['bytes']):>14}  {share:5.1f}%  "
            f"{data['files']:,} files")
    add("")
    add("By top-level path")
    add("-" * 38)
    for name, size in report["top_level"].items():
        add(f"  {name:<34}{human(size):>14}")
    add("")
    add(f"Largest files (top {len(report['largest_files'])})")
    add("-" * 38)
    for item in report["largest_files"]:
        add(f"  {human(item['bytes']):>14}  {item['path']}")
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", default=str(ROOT), help="checkout to measure")
    parser.add_argument("--top", type=int, default=20,
                        help="how many of the largest files to list")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    parser.add_argument("--fail-over-bytes", type=int, default=0,
                        help="exit 1 when the measured total exceeds this budget")
    args = parser.parse_args(argv)

    report = scan(Path(args.root).resolve(), top=max(1, args.top))
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print(render(report))

    if args.fail_over_bytes and report["total_bytes"] > args.fail_over_bytes:
        print(f"\nsize budget exceeded: {human(report['total_bytes'])} > "
              f"{human(args.fail_over_bytes)}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
