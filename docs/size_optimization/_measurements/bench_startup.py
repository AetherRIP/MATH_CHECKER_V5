#!/usr/bin/env python3
"""Measure import cost and cold server start for the *source* app.

Used by docs/size_optimization/FINAL_REPORT.md. It starts the real launcher
(`main.py`) in Demo mode against a throwaway data directory, waits for
`/api/health` to answer, then stops the instance the same way `Stop.bat` does.

    python docs/size_optimization/_measurements/bench_startup.py [runs]

Nothing in the project's own `data/` directory is read or written.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
PY = ROOT / ".venv" / "Scripts" / "python.exe"
if not PY.is_file():
    PY = Path(sys.executable)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def health(port: int, timeout: float = 0.5) -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health",
                                    timeout=timeout) as response:
            return response.status == 200
    except Exception:
        return False


def import_seconds() -> float:
    code = ("import sys, time; sys.path.insert(0, 'backend');"
            "t=time.perf_counter(); import app.main;"
            "print(f'{time.perf_counter()-t:.3f}')")
    out = subprocess.run([str(PY), "-c", code], cwd=ROOT,
                         capture_output=True, text=True, check=True)
    return float(out.stdout.strip())


def start_seconds() -> float:
    port = free_port()
    data_dir = tempfile.mkdtemp(prefix="mc5-bench-")
    env = dict(os.environ, MC5_DEMO="1", MC5_DATA_DIR=data_dir)
    started = time.perf_counter()
    proc = subprocess.Popen([str(PY), "main.py", str(port)], cwd=ROOT, env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        deadline = started + 90
        while time.perf_counter() < deadline:
            if health(port):
                return time.perf_counter() - started
            if proc.poll() is not None:
                raise RuntimeError(f"launcher exited with {proc.returncode}")
            time.sleep(0.1)
        raise RuntimeError("server did not answer /api/health in 90 s")
    finally:
        subprocess.run([str(PY), "main.py", "--stop", str(port)], cwd=ROOT,
                       env=env, capture_output=True)
        for _ in range(100):
            if not health(port) and proc.poll() is not None:
                break
            time.sleep(0.1)
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()


def main() -> int:
    runs = int(sys.argv[1]) if len(sys.argv) > 1 else 3
    results = {"import_s": [], "start_s": []}
    for index in range(runs):
        results["import_s"].append(round(import_seconds(), 3))
        results["start_s"].append(round(start_seconds(), 3))
        print(f"run {index + 1}/{runs}: import {results['import_s'][-1]} s, "
              f"start {results['start_s'][-1]} s", flush=True)
    results["python"] = sys.version.split()[0]
    results["platform"] = sys.platform
    print(json.dumps(results, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
