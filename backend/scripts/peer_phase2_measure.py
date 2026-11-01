#!/usr/bin/env python3
"""Measure the Phase 2 peer service: idle CPU and working set.

    .venv/Scripts/python.exe backend/scripts/peer_phase2_measure.py [--seconds 20]

Starts a real supervised peer process in a temporary data directory with
throw-away credentials, lets it settle, then samples its working set and CPU over
an idle window. Prints a small JSON object so the numbers in the Phase 2 record
can be reproduced instead of trusted.

Limitations, stated rather than hidden: this is a development machine with the
project's virtual environment (Python 3.13 + cryptography). A packaged runtime
bundles its own interpreter and will differ. The process is idle by construction —
Phase 2 has no timers and no networking — so this measures the floor, not a
workload.
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sys
import tempfile
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from app.peer import ca, control, identity, invite, revocation, roster, service


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--seconds", type=float, default=20.0)
    args = parser.parse_args()

    now = time.time()
    dept = ca.DepartmentCA.create("dept-measure", "Measurement Department", now=now)
    device = identity.DeviceIdentity.generate(dept.dept_id, label="measure", now=now)
    request = invite.InviteRequest.create(device, now=now)
    invitation = invite.Invitation.issue(
        request, department=dept, roster=roster.Roster(dept.dept_id),
        revocations=revocation.RevocationList(dept.dept_id), now=now)
    credentials = invitation.accept(device, request=request, now=now)

    workdir = pathlib.Path(tempfile.mkdtemp(prefix="mc5-peer-measure-"))
    credentials.save(workdir / "credentials.json")
    control.save_state(True, directory=workdir)

    supervisor = service.PeerService(directory=workdir,
                                     credentials_path=workdir / "credentials.json",
                                     max_restarts=0, start_timeout=40.0)
    started = supervisor.start()
    if started.state != service.RUNNING:
        print(json.dumps({"ok": False, "state": started.state,
                          "detail": started.detail}, indent=2))
        return 1
    try:
        time.sleep(2.0)                      # let imports and the reader settle
        first = service.process_usage(started.pid)
        t0 = time.time()
        time.sleep(args.seconds)
        second = service.process_usage(started.pid)
        elapsed = time.time() - t0
        pong = supervisor.ping()
        report = {
            "ok": bool(first.get("available") and second.get("available")),
            "state": started.state,
            "pid": started.pid,
            "encrypted_store": started.encrypted_store,
            "idle_window_seconds": round(elapsed, 2),
            "working_set_mb": round(second["working_set_bytes"] / (1024 * 1024), 1),
            "cpu_percent_idle": round(
                100.0 * (second["cpu_seconds"] - first["cpu_seconds"]) / elapsed, 3),
            "responds_to_ping": isinstance(pong.get("pong"), float),
            "targets": {"cpu_percent_max": 0.5, "working_set_mb_max": 40},
            "limits": "development venv, idle process, no networking in Phase 2",
        }
        print(json.dumps(report, indent=2))
        return 0
    finally:
        stopped = supervisor.stop()
        print(json.dumps({"stopped": stopped.state,
                          "lock_released": not (workdir / service.LOCK_FILENAME).exists()}))


if __name__ == "__main__":
    sys.exit(main())
