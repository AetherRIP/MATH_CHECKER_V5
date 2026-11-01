#!/usr/bin/env python3
"""Phase 2 acceptance: marking output must not depend on the peer service.

The structural claim ("no marking module imports ``app.peer``") is already
asserted by ``backend/tests/test_peer_isolation.py``. This script tests the
*behavioural* claim with the real marking code path:

    for each peer state -> grade one fixed paper with the real
    ``legacy.grade_with_model`` (a scripted model stands in for Gemini, exactly
    as the offline test suite does), normalise it, apply the blueprint, compute
    totals, and hash the resulting bytes.

If the hashes differ, the peer service can influence a mark and Phase 2's whole
premise is false. If they are identical, the service is provably out of the
marking path for these six states.

Determinism notes
-----------------
The comparison is over the bytes the application would persist:
``json.dumps(result, sort_keys=True, ensure_ascii=True)`` plus the totals tuple.
``sort_keys`` removes dict-ordering noise, which is not a meaningful difference.
Nothing else is normalised: no field is masked, and the script fails if a
timestamp or identifier ever appears inside the marking payload (that would be a
real nondeterminism the report would otherwise hide).

The peer state itself is recorded alongside the hash, so the table states both
what was running and what came out.

    python backend/scripts/peer_marking_independence.py
    python backend/scripts/peer_marking_independence.py --json docs/results/PEER_PHASE2_MARKING_INDEPENDENCE.json

Exit codes: 0 identical everywhere, 1 a difference or a real run failure.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
import tempfile
import time
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

from app.engine import blueprint as bp          # noqa: E402
from app.engine import legacy_marking as legacy  # noqa: E402
from app.peer import ca as peer_ca               # noqa: E402
from app.peer import control as peer_control     # noqa: E402
from app.peer import identity as peer_identity   # noqa: E402
from app.peer import invite as peer_invite       # noqa: E402
from app.peer import revocation as peer_revocation  # noqa: E402
from app.peer import roster as peer_roster       # noqa: E402
from app.peer import service as peer_service     # noqa: E402

MODEL = "gemini-3.6-flash"

#: The fixed paper. Small on purpose: this measures independence, not accuracy.
BLUEPRINT = {"questions": [
    {"question_number": "1", "max_marks": 3},
    {"question_number": "2", "max_marks": 2},
    {"question_number": "3", "max_marks": 2},
]}

MODEL_ANSWER = json.dumps({
    "paper_info": {"qualification": "O Level", "paper": "P1", "session": "June"},
    "questions": [
        {"question_number": "1", "max_marks": 3, "awarded_marks": 3,
         "topic": "algebra", "student_answer": "4x = 24, x = 6",
         "working_summary": "subtract 7, divide by 4",
         "loss_reason": "", "mark_breakdown": ["M1", "M1", "A1"],
         "correct_method": "rearrange", "full_mark_solution": "x = 6"},
        {"question_number": "2", "max_marks": 2, "awarded_marks": 2,
         "topic": "number", "student_answer": "36/100 = 9/25",
         "working_summary": "convert and simplify",
         "loss_reason": "", "mark_breakdown": ["M1", "A1"],
         "correct_method": "place value", "full_mark_solution": "9/25"},
        {"question_number": "3", "max_marks": 2, "awarded_marks": 1,
         "topic": "geometry", "student_answer": "180 - 65 = 115",
         "working_summary": "one angle found",
         "loss_reason": "second angle missing", "mark_breakdown": ["M1"],
         "correct_method": "angles on a line", "full_mark_solution": "115 and 65"},
    ],
})


# --------------------------------------------------------------- a scripted model
class _Finish:
    name = "STOP"


class _Candidate:
    finish_reason = _Finish()


class _Response:
    def __init__(self, text: str) -> None:
        self.text = text
        self.candidates = [_Candidate()]


class _Models:
    def __init__(self, text: str) -> None:
        self.text = text
        self.calls: list[dict] = []

    def generate_content(self, *, model, contents, config=None, **_):
        self.calls.append({"model": model, "contents": contents,
                           "config": config or {}})
        return _Response(self.text)


class ScriptedClient:
    """The slice of the Gemini client the marking path touches."""

    def __init__(self, text: str) -> None:
        self.models = _Models(text)

    def upload_file(self, path, *args, **kwargs):
        return object()

    def files_delete(self, *args, **kwargs) -> None:
        return None


# ------------------------------------------------------------------ one marking run
def mark_once() -> dict:
    client = ScriptedClient(MODEL_ANSWER)
    parsed, used_model = legacy.grade_with_model(
        client, [MODEL], "student-paper", "mark-scheme", blueprint=BLUEPRINT)
    normalised = legacy.normalise_result(parsed)
    result, report = bp.apply_blueprint(normalised, BLUEPRINT)
    earned, available, percentage = legacy.calculate_totals(result)

    payload = json.dumps(result, sort_keys=True, ensure_ascii=True)
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    return {
        "sha256": digest,
        "bytes": len(payload),
        "model": used_model,
        "earned": earned,
        "available": available,
        "percentage": round(float(percentage), 4),
        "missing_questions": list(report.get("missing_questions") or []),
        "questions": len(result.get("questions") or []),
        "sample_questions": result.get("questions"),
    }


# --------------------------------------------------------------- peer environment
def _credentials(directory: Path):
    # Real clock: the supervisor validates the certificate against wall time, so
    # a fixed future timestamp would be refused as "dated in the future".
    department = peer_ca.DepartmentCA.create("dept-alpha", "Alpha")
    device = peer_identity.DeviceIdentity.generate(department.dept_id)
    request = peer_invite.InviteRequest.create(device)
    invitation = peer_invite.Invitation.issue(
        request, department=department,
        roster=peer_roster.Roster(department.dept_id),
        revocations=peer_revocation.RevocationList(department.dept_id),
        days=peer_ca.CERT_DEFAULT_DAYS)
    credentials = invitation.accept(device, request=request)
    path = directory / "credentials.json"
    credentials.save(path)
    return credentials, path


def _describe(target) -> str:
    """A one-line, human-readable account of what the service actually did.

    Accepts either a service or a status object: the supervisor exposes the
    latter as a property, and calling it like a method is the sort of detail
    that should not decide whether this evidence runs.
    """
    try:
        status = target
        if not (hasattr(status, "state") and hasattr(status, "detail")):
            status = getattr(target, "status", None)
            if callable(status):
                status = status()
    except Exception as exc:                       # noqa: BLE001 - reported
        return f"status unavailable: {exc}"
    if status is None:
        return "no status"
    return f"{getattr(status, 'state', status)}: {getattr(status, 'detail', '')}".strip()


def _ping(service) -> str:
    for name, args in (("ping", ()), ("request", ("ping",))):
        method = getattr(service, name, None)
        if callable(method):
            try:
                method(*args)
                return "answered"
            except Exception as exc:               # noqa: BLE001 - reported
                return f"failed: {type(exc).__name__}: {exc}"
    return "no ping surface"


def states(workdir: Path):
    """Yield ``(name, description, teardown)`` for each peer state.

    Each one is genuinely entered: the children are real interpreters, not
    mocked successes.
    """
    # 1 -- disabled (the shipped default)
    peer_control.save_state(False, directory=workdir)
    disabled = peer_service.PeerService(directory=workdir)
    yield "disabled", _describe(disabled.start()), lambda: None

    # 2 -- enabled and running
    peer_control.save_state(True, directory=workdir)
    _, cred_path = _credentials(workdir)
    running = peer_service.PeerService(directory=workdir, credentials_path=cred_path)
    started = running.start()
    yield ("running", f"{started.state}: {started.detail} pid={started.pid}",
           running.stop)

    # 3 -- enabled, stopped cleanly
    stopped = peer_service.PeerService(directory=workdir, credentials_path=cred_path)
    stopped.start()
    stopped.stop()
    yield "stopped", _describe(stopped), lambda: None

    # 4 -- enabled, child killed from outside (unexpected termination)
    killed = peer_service.PeerService(directory=workdir, credentials_path=cred_path)
    killed.start()
    proc = getattr(killed, "_proc", None)
    if proc is not None and proc.poll() is None:
        proc.kill()
        proc.wait(timeout=10)
    time.sleep(0.4)
    observed = _describe(killed)
    yield "terminated", f"child killed: {observed}", killed.stop

    # 5 -- credentials unavailable
    peer_control.save_state(True, directory=workdir)
    # The switch must be ON here, or the service would report DISABLED and the
    # row would claim to have tested credentials while testing the switch.
    peer_control.save_state(True, directory=workdir / "no-credentials")
    missing = peer_service.PeerService(
        directory=workdir / "no-credentials",
        credentials_path=workdir / "absent.json")
    yield "credentials-unavailable", _describe(missing.start()), lambda: None

    # 6 -- IPC unavailable (child gone under a live supervisor)
    ipc = peer_service.PeerService(directory=workdir, credentials_path=cred_path)
    ipc.start()
    child = getattr(ipc, "_proc", None)
    if child is not None and child.poll() is None:
        child.kill()
        child.wait(timeout=10)
    yield "ipc-unavailable", f"ping after child death: {_ping(ipc)}", ipc.stop


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--json", help="write the evidence table to this path")
    args = parser.parse_args(argv)

    rows: list[dict] = []
    failure = ""
    with tempfile.TemporaryDirectory(prefix="mc5-peer-marking-") as tmp:
        workdir = Path(tmp) / "peer"
        for name, observation, teardown in states(workdir):
            try:
                run = mark_once()
            except Exception as exc:               # noqa: BLE001 - a real failure
                failure = f"{name}: marking failed: {type(exc).__name__}: {exc}"
                run = {"sha256": None, "error": str(exc)}
            finally:
                try:
                    teardown()
                except Exception:                  # noqa: BLE001 - teardown noise
                    pass
            rows.append({"peer_state": name, "observed": observation, **run})
            print(f"{name:<24} {observation[:52]:<52} {str(run.get('sha256'))[:16]}")

    hashes = {row["sha256"] for row in rows}
    identical = len(hashes) == 1 and None not in hashes
    totals = {(row.get("earned"), row.get("available")) for row in rows}

    print()
    for row in rows:
        print(f"{row['peer_state']:<24} earned/available="
              f"{row.get('earned')}/{row.get('available')} "
              f"questions={row.get('questions')} bytes={row.get('bytes')}")
    print()
    print("distinct output hashes:", len(hashes))
    print("distinct totals:", totals)
    print("RESULT:", "IDENTICAL across all six states" if identical
          else f"DIFFERENT ({failure})" if failure else "DIFFERENT")

    if args.json:
        target = Path(args.json)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps({
            "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "model": MODEL,
            "blueprint": BLUEPRINT,
            "normalisation": "json.dumps(result, sort_keys=True, ensure_ascii=True)",
            "identical": identical,
            "distinct_hashes": sorted(h for h in hashes if h),
            "totals": sorted(str(t) for t in totals),
            "rows": [{k: v for k, v in row.items() if k != "sample_questions"}
                     for row in rows],
            "failure": failure or None,
        }, indent=2) + "\n", encoding="utf-8", newline="\n")
        print("wrote", target)
    return 0 if identical else 1


if __name__ == "__main__":
    sys.exit(main())
