"""The peer process itself: verify once, then answer on stdin/stdout.

Run by :class:`app.peer.service.PeerService` as
``python -m app.peer.service_main --credentials <path>``. It is deliberately
small and single-purpose:

1. **Verify before announcing anything.** The child re-runs the Phase 1 checks on
   the credential file it is given. If they fail it says so and exits non-zero,
   so a supervisor bug cannot produce a process that serves without a valid
   chain — and it never reports ``ready`` and then discovers a problem.
2. **Speak only the local protocol.** One JSON object per line in, one out, with
   :func:`app.peer.service.validate_request` as the single validator. There is no
   operation that takes a path, a command or a host.
3. **Die with its parent.** When stdin reaches end-of-file the supervisor is gone
   (crash, kill, logout), so the process exits rather than lingering as an
   orphan nobody can see or stop.

Nothing here opens a socket, writes to the database, or imports the marking
engine. That is the whole isolation argument for Phase 2 (plan §10.4).
"""
from __future__ import annotations

import argparse
import json
import sys
import time

from . import PeerError, PeerVerificationError
from .identity import canonical_json
from .invite import PeerCredentials
from .service import (IPC_PROTOCOL, IpcError, probe_secure_store,
                      validate_request, verify_credentials)


def _emit(payload: dict) -> None:
    sys.stdout.write(json.dumps(payload, ensure_ascii=True) + "\n")
    sys.stdout.flush()


def _public_state(credentials: PeerCredentials | None) -> dict:
    """What this process may say about itself. Public fields only."""
    if credentials is None:
        return {}
    return {
        "device_id": credentials.identity.device_id,
        "dept_id": credentials.dept_id,
        "pid": None,
    }


def _load(credentials_path: str) -> tuple[PeerCredentials | None, str, str, dict]:
    """Load and verify. Returns (credentials, failure_reason, detail, snapshot)."""
    try:
        credentials = PeerCredentials.load(credentials_path)
    except PeerError as exc:
        return None, "CREDENTIALS_UNAVAILABLE", str(exc), {}
    if credentials is None:
        return None, "CREDENTIALS_UNAVAILABLE", "no credential file at that path", {}
    try:
        snapshot = verify_credentials(credentials)
    except PeerVerificationError as exc:
        return None, exc.reason, exc.detail, {}
    except (PeerError, ValueError, OSError) as exc:
        return None, "MALFORMED", f"credentials could not be verified: {exc}", {}
    return credentials, "", "", snapshot


def serve(credentials_path: str) -> int:
    probe = probe_secure_store()
    credentials, reason, detail, snapshot = _load(credentials_path)
    if credentials is None:
        _emit({"event": "failed", "reason": reason, "detail": detail,
               "store": {"writable": probe.writable, "encrypted": probe.encrypted,
                         "detail": probe.detail}})
        return 2

    _emit({"event": "ready", "protocol": IPC_PROTOCOL,
           "pid": None, "peer": snapshot,
           "store": {"writable": probe.writable, "encrypted": probe.encrypted,
                     "detail": probe.detail}})

    started = time.time()
    for line in sys.stdin:                      # EOF == supervisor gone
        raw = line.strip()
        if not raw:
            continue
        try:
            request = validate_request(raw)
        except IpcError as exc:
            _emit({"request_id": None, "ok": False,
                   "error": {"code": exc.code, "detail": exc.detail}})
            continue

        request_id = request["request_id"]
        operation = request["operation"]
        if operation == "ping":
            result = {"pong": time.time(), "uptime_seconds": round(time.time() - started, 3)}
        elif operation == "status":
            result = {"peer": snapshot, "uptime_seconds": round(time.time() - started, 3),
                      "cwd_safe": True}
        elif operation == "shutdown":
            _emit({"request_id": request_id, "ok": True, "result": {"stopping": True}})
            return 0
        else:                                   # pragma: no cover - validated above
            _emit({"request_id": request_id, "ok": False,
                   "error": {"code": "unsupported_operation", "detail": operation}})
            continue
        _emit({"request_id": request_id, "ok": True, "result": result,
               "state": _public_state(credentials)})
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m app.peer.service_main",
        description="The supervised peer process (no networking in Phase 2).")
    parser.add_argument("--credentials", required=True,
                        help="path to the credentials file written by the invitation")
    args = parser.parse_args(argv)
    try:
        return serve(args.credentials)
    except KeyboardInterrupt:                    # pragma: no cover - interactive
        return 0


if __name__ == "__main__":                       # pragma: no cover - module entry
    sys.exit(main())
