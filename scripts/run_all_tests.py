#!/usr/bin/env python3
"""Run every check the repository documents, in one cross-platform command.

    python scripts/run_all_tests.py

Runs, in order:

    1. frontend build      npm run build            (tsc -b + vite)
    2. frontend suite      node --test "tests/*.test.ts"
    3. backend suite       <venv python> -m pytest backend/tests -q

The frontend suite includes the headless-browser checks for the marking
workspace. Those need the app running with a seeded result; when the app is not
reachable they are reported as skipped, and the summary says so instead of
counting them as passes.

Exit status is 0 only when every selected check passed (skipped browser tests
still count as a pass, but are called out), 1 when anything failed.

Usage:
    python scripts/run_all_tests.py                    # everything
    python scripts/run_all_tests.py --only frontend    # one check
    python scripts/run_all_tests.py --no-build -v      # stream output live
    python scripts/run_all_tests.py --app-url http://127.0.0.1:8720/
"""
from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FRONTEND = ROOT / "frontend"
DEFAULT_APP_URL = "http://127.0.0.1:8720/"
TAIL_LINES = 40
ALL_CHECKS = ("build", "frontend", "backend")

VENV_PYTHONS = (
    Path(".venv/Scripts/python.exe"),
    Path(".venv/bin/python"),
    Path("venv/Scripts/python.exe"),
    Path("venv/bin/python"),
)


@dataclass
class Result:
    """One check's outcome: pass/fail, one-line detail, timing, output."""

    name: str
    ok: bool
    detail: str
    seconds: float
    output: str = ""
    note: str = ""


ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def _plain(text: str) -> str:
    """Drop ANSI colour codes: vite/node colourise even when piped, which
    would otherwise defeat the summary parsers."""
    return ANSI_RE.sub("", text)


def _execute(cmd: list[str], cwd: Path, env: dict[str, str], timeout: float,
             stream: bool = False) -> tuple[int | None, str, float, str | None]:
    """Run a command, returning (exit code, combined output, seconds, error)."""
    started = time.monotonic()
    try:
        completed = subprocess.run(
            cmd,
            cwd=str(cwd),
            env=env,
            stdout=None if stream else subprocess.PIPE,
            stderr=None if stream else subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
        return (completed.returncode, _plain(completed.stdout or ""),
                time.monotonic() - started, None)
    except FileNotFoundError:
        return None, "", time.monotonic() - started, f"command not found: {cmd[0]}"
    except subprocess.TimeoutExpired as exc:
        partial = exc.stdout or ""
        if isinstance(partial, bytes):
            partial = partial.decode("utf-8", "replace")
        return None, partial, time.monotonic() - started, f"timed out after {timeout:.0f}s"
    except OSError as exc:                      # permission denied, bad interpreter…
        return None, "", time.monotonic() - started, str(exc)


def _last_count(pattern: str, output: str) -> int | None:
    """Last integer matched by ``pattern`` (the summary block comes last)."""
    matches = re.findall(pattern, output)
    return int(matches[-1]) if matches else None


def _counts(*, prefix: str, output: str, keys: tuple[str, ...]) -> dict[str, int]:
    """Collect final per-status counts from a test runner's summary block."""
    found: dict[str, int] = {}
    for key in keys:
        value = _last_count(rf"{re.escape(prefix)}{key} (\d+)", output)
        if value:
            found[key] = value
    return found


def _pytest_counts(output: str) -> dict[str, int]:
    """pytest prints "2096 passed, 2 warnings": the count precedes the word."""
    found: dict[str, int] = {}
    for key in ("passed", "failed", "error", "skipped", "xfailed"):
        value = _last_count(rf"(\d+) {key}", output)
        if value:
            found[key] = value
    return found


def _summarize(counts: dict[str, int], skipped_key: str = "skipped",
               failed_keys: tuple[str, ...] = ("fail", "failed")) -> str:
    passed = counts.get("pass", 0) + counts.get("passed", 0)
    failed = sum(counts.get(key, 0) for key in failed_keys)
    skipped = counts.get(skipped_key, 0)
    parts = [f"{passed} passed"]
    if failed:
        parts.append(f"{failed} failed")
    if skipped:
        parts.append(f"{skipped} skipped")
    return ", ".join(parts)


def pick_python(explicit: str | None) -> Path:
    """The interpreter that has the test tools: an explicit one, else the venv."""
    if explicit:
        return Path(explicit)
    for relative in VENV_PYTHONS:
        candidate = ROOT / relative
        if candidate.exists():
            return candidate
    return Path(sys.executable)


def app_reachable(url: str) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=3) as response:
            return response.status < 500
    except Exception:
        return False


def _env_for(args: argparse.Namespace) -> dict[str, str]:
    env = os.environ.copy()
    env.setdefault("PYTHONUTF8", "1")           # keep child output decodable
    if args.app_url:
        # The browser checks read this; it also keeps the printed target honest.
        env["WORKSPACE_LAYOUT_URL"] = args.app_url
    return env


def check_build(args: argparse.Namespace, env: dict[str, str]) -> Result:
    npm = shutil.which("npm")
    if not npm:
        return Result("frontend build", False, "npm not found on PATH", 0.0)
    code, output, seconds, error = _execute(
        [npm, "run", "build"], FRONTEND, env, args.timeout, args.verbose)
    if error:
        return Result("frontend build", False, error, seconds, output)
    if code != 0:
        return Result("frontend build", False, f"exit {code}", seconds, output)
    bundle = re.search(r"(index-[\w-]+\.js)\s+([\d.]+ kB)", output)
    detail = f"tsc + vite ok ({bundle.group(1)}, {bundle.group(2)})" if bundle else "tsc + vite ok"
    return Result("frontend build", True, detail, seconds, output)


def check_frontend(args: argparse.Namespace, env: dict[str, str]) -> Result:
    node = shutil.which("node")
    if not node:
        return Result("frontend tests", False, "node not found on PATH", 0.0)
    # The pattern is passed through unexpanded: Node's own glob handles it on
    # every OS (cmd.exe would not expand it either).
    code, output, seconds, error = _execute(
        [node, "--test", "tests/*.test.ts"], FRONTEND, env, args.timeout, args.verbose)
    if error:
        return Result("frontend tests", False, error, seconds, output)

    counts = _counts(prefix="\u2139 ", output=output,
                     keys=("tests", "pass", "fail", "skipped", "cancelled"))
    detail = _summarize(counts) if counts else f"exit {code}"
    if counts.get("cancelled"):
        detail += f", {counts['cancelled']} cancelled"

    ok = code == 0
    note = ""
    if counts.get("skipped"):
        note = (f"{counts['skipped']} browser check(s) skipped - the app was not reachable at "
                f"{args.app_url}; start it (start.bat) and rerun for the live UI checks.")
    return Result("frontend tests", ok, detail, seconds, output, note)


def check_backend(args: argparse.Namespace, env: dict[str, str]) -> Result:
    python = pick_python(args.python)
    if not python.exists():
        return Result("backend tests", False, f"interpreter not found: {python}", 0.0)

    probe_code, probe_out, probe_secs, probe_error = _execute(
        [str(python), "-c", "import pytest, httpx"], ROOT, env, 120.0)
    if probe_error or probe_code != 0:
        reason = probe_error or (probe_out.strip().splitlines() or ["import failed"])[-1]
        return Result("backend tests", False,
                      f"missing test tools ({reason})", probe_secs,
                      note="install them with: "
                           f"{python} -m pip install -r backend/requirements-dev.txt")

    code, output, seconds, error = _execute(
        [str(python), "-m", "pytest", "backend/tests", "-q", "-p", "no:cacheprovider"],
        ROOT, env, args.timeout, args.verbose)
    if error:
        return Result("backend tests", False, error, seconds, output)

    counts = _pytest_counts(output)
    detail = _summarize(counts) if counts else f"exit {code}"
    return Result("backend tests", code == 0, detail, seconds, output)


def _node_version() -> str:
    node = shutil.which("node")
    if not node:
        return "not found"
    completed = subprocess.run([node, "--version"], capture_output=True, text=True,
                               encoding="utf-8", errors="replace")
    return completed.stdout.strip() or "unknown"


def _print_header(python: Path, args: argparse.Namespace) -> None:
    version = _node_version()
    print("=" * 68)
    print(" Math Checker - full verification")
    print("=" * 68)
    print(f" python    {python}")
    print(f" node      {version}")
    print(f" app       {args.app_url}")
    print()


def _print_output(name: str, output: str) -> None:
    lines = [line for line in output.splitlines() if line.strip()]
    if not lines:
        return
    print()
    print(f"--- {name}: last {min(len(lines), TAIL_LINES)} lines of output ---")
    for line in lines[-TAIL_LINES:]:
        print(f"  {line}")


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):      # never die on a stray glyph
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass

    parser = argparse.ArgumentParser(
        description="Run the frontend build, frontend suite and backend suite; "
                    "print one pass/fail summary.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Exit status: 0 = every selected check passed, 1 = something failed.",
    )
    parser.add_argument("--only", action="append", choices=ALL_CHECKS, metavar="CHECK",
                        help="run only this check (repeatable): build, frontend, backend")
    parser.add_argument("--no-build", action="store_true",
                        help="skip the frontend production build")
    parser.add_argument("--app-url", default=os.environ.get("WORKSPACE_LAYOUT_URL",
                                                           DEFAULT_APP_URL),
                        help=f"running app for the browser checks (default {DEFAULT_APP_URL})")
    parser.add_argument("--python", help="interpreter for pytest (default: the repo venv)")
    parser.add_argument("--timeout", type=float, default=3600.0,
                        help="per-check timeout in seconds (default 3600)")
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="stream each check's output instead of capturing it")
    args = parser.parse_args(argv)

    selected = list(dict.fromkeys(args.only)) if args.only else list(ALL_CHECKS)
    if args.no_build:
        selected = [name for name in selected if name != "build"]
    if not selected:
        parser.error("nothing to run: --no-build removed the only selected check")

    python = pick_python(args.python)
    env = _env_for(args)
    _print_header(python, args)

    if "frontend" in selected:
        if app_reachable(args.app_url):
            print(f" app reachable - live browser checks will run against {args.app_url}")
        else:
            print(f" app NOT reachable at {args.app_url} - browser checks will skip")
            print("   (start the app with start.bat to exercise them)")
        print()

    runners = {"build": check_build, "frontend": check_frontend, "backend": check_backend}
    results: list[Result] = []
    total = len(selected)
    overall_start = time.monotonic()

    try:
        for index, name in enumerate(selected, start=1):
            print(f" [{index}/{total}] {name} ...", end="", flush=True)
            result = runners[name](args, env)
            results.append(result)
            # \r rewrites the same line; trailing spaces clear the placeholder.
            print(f"\r [{index}/{total}] {result.name:<16} "
                  f"{'PASS' if result.ok else 'FAIL':<4} {result.seconds:7.1f}s  "
                  f"{result.detail}   ")
            if not result.ok and result.note:
                print(f"      {result.note}")
    except KeyboardInterrupt:
        print("\n\nINTERRUPTED - stopping the run.")
        return 130

    elapsed = time.monotonic() - overall_start
    failed = [result for result in results if not result.ok]
    notes = [result for result in results if result.note and result.ok]

    for result in failed:
        _print_output(result.name, result.output)
    for result in notes:
        print(f"\n note: {result.note}")

    passed = len(results) - len(failed)
    print()
    print("-" * 68)
    if failed:
        names = ", ".join(result.name for result in failed)
        print(f" RESULT: FAIL - {passed}/{len(results)} checks passed ({elapsed:.1f}s)")
        print(f"         failed: {names}")
        return 1
    print(f" RESULT: PASS - {passed}/{len(results)} checks passed ({elapsed:.1f}s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
