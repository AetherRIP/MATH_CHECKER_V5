"""Phase 2: the supervised peer service, its switch and its local protocol.

The process tests here start a **real** child interpreter, because a mocked
"started successfully" would prove nothing about the failure modes this phase
exists to bound: orphaned children, duplicate supervisors, a crash that nobody
notices. The deterministic tests cover the branches that must never spawn at all.
"""
from __future__ import annotations

import json
import subprocess
import sys
import time
from dataclasses import replace

import pytest

from app.peer import (CERT_EXPIRED, ROSTER_INCOMPLETE, WRONG_DEPARTMENT,
                      PeerVerificationError)
from app.peer import ca as peer_ca
from app.peer import control as peer_control
from app.peer import identity as peer_identity
from app.peer import invite as peer_invite
from app.peer import revocation as peer_revocation
from app.peer import roster as peer_roster
from app.peer import service as peer_service

NOW = 1_800_000_000.0


@pytest.fixture()
def department():
    return peer_ca.DepartmentCA.create("dept-alpha", "Alpha Department", now=NOW)


def _credentials(department, *, now=NOW, days=peer_ca.CERT_DEFAULT_DAYS):
    device = peer_identity.DeviceIdentity.generate(department.dept_id, now=now)
    request = peer_invite.InviteRequest.create(device, now=now)
    invitation = peer_invite.Invitation.issue(
        request, department=department,
        roster=peer_roster.Roster(department.dept_id),
        revocations=peer_revocation.RevocationList(department.dept_id),
        days=days, now=now)
    return invitation.accept(device, request=request, now=now)


@pytest.fixture()
def credentials(department):
    return _credentials(department)


def _enabled(tmp_path):
    peer_control.save_state(True, directory=tmp_path)


class _NoSpawn:
    """A popen stand-in that fails the test if the service tries to start one."""

    def __call__(self, *args, **kwargs):        # pragma: no cover - asserted
        raise AssertionError("the service must not spawn a process on this path")


# ---------------------------------------------------------------------------
# the switch
# ---------------------------------------------------------------------------

def test_switch_is_off_by_default_and_durable(tmp_path):
    assert peer_control.is_enabled(tmp_path) is False
    state = peer_control.load_state(tmp_path)
    assert state["enabled"] is False and state["corrupt"] is False

    peer_control.save_state(True, updated_by="test", directory=tmp_path)
    assert peer_control.is_enabled(tmp_path) is True
    peer_control.save_state(False, directory=tmp_path)
    assert peer_control.is_enabled(tmp_path) is False


def test_corrupt_switch_file_is_reported_and_keeps_the_service_off(tmp_path):
    peer_control.state_path(tmp_path).write_bytes(b"{not json")
    state = peer_control.load_state(tmp_path)
    assert state["corrupt"] is True
    # A preference nobody can read must not start anything.
    assert peer_control.is_enabled(tmp_path) is False
    described = peer_control.describe(directory=tmp_path)
    assert described["label"] == peer_control.LABEL_UNKNOWN


def test_describe_never_calls_a_switched_on_service_running(tmp_path):
    peer_control.save_state(True, directory=tmp_path)
    described = peer_control.describe(service=None, directory=tmp_path)
    assert described["state"] == "STOPPED"
    assert described["label"] == peer_control.LABEL_STOPPED

    running = peer_service.ServiceStatus(state=peer_service.RUNNING,
                                         detail="peer service is running").as_dict()
    described = peer_control.describe(service=running, directory=tmp_path)
    assert described["label"] == peer_control.LABEL_RUNNING


# ---------------------------------------------------------------------------
# secure startup inputs
# ---------------------------------------------------------------------------

def test_store_probe_reports_writability_and_encryption(tmp_path):
    probe = peer_service.probe_secure_store(tmp_path / "probe.json")
    assert probe.writable is True
    assert isinstance(probe.encrypted, bool)
    assert probe.detail
    # The probe cleans up after itself: no file is left in the peer directory.
    assert not (tmp_path / "probe.json").exists()


def test_verify_credentials_returns_a_public_snapshot(credentials):
    snapshot = peer_service.verify_credentials(credentials, now=NOW)
    assert snapshot["device_id"] == credentials.identity.device_id
    assert snapshot["dept_id"] == credentials.dept_id
    assert snapshot["roster_epoch"] >= 1
    text = json.dumps(snapshot)
    assert "PRIVATE" not in text and "private_key" not in text
    assert snapshot["warn_renewal"] is False


def test_expired_certificate_is_refused_with_its_reason(credentials):
    with pytest.raises(PeerVerificationError) as exc:
        peer_service.verify_credentials(
            credentials,
            now=credentials.certificate.not_after + 1)
    assert exc.value.reason == CERT_EXPIRED


def test_a_pinned_department_key_must_match(credentials, department):
    other = peer_ca.DepartmentCA.create("dept-beta", "Beta", now=NOW)
    with pytest.raises(PeerVerificationError) as exc:
        peer_service.verify_credentials(credentials, now=NOW,
                                        pinned_department_key=other.public_key_pem)
    assert exc.value.reason == WRONG_DEPARTMENT
    # The real key pins fine.
    peer_service.verify_credentials(credentials, now=NOW,
                                    pinned_department_key=department.public_key_pem)


def test_a_roster_missing_this_device_is_refused(department, credentials):
    trimmed = replace(credentials, roster=peer_roster.Roster(department.dept_id))
    with pytest.raises(PeerVerificationError) as exc:
        peer_service.verify_credentials(trimmed, now=NOW)
    assert exc.value.reason == ROSTER_INCOMPLETE


# ---------------------------------------------------------------------------
# refusals that must not spawn a process
# ---------------------------------------------------------------------------

def test_switched_off_never_starts_a_process(tmp_path, credentials):
    (tmp_path / "credentials.json").write_text("{}", encoding="utf-8")
    service = peer_service.PeerService(directory=tmp_path,
                                       credentials_path=tmp_path / "credentials.json",
                                       popen=_NoSpawn(), clock=lambda: NOW)
    status = service.start()
    assert status.state == peer_service.DISABLED
    assert status.pid is None


def test_missing_credentials_are_reported_not_invented(tmp_path):
    _enabled(tmp_path)
    service = peer_service.PeerService(
        directory=tmp_path, credentials_path=tmp_path / "absent.json",
        popen=_NoSpawn(), clock=lambda: NOW)
    status = service.start()
    assert status.state == peer_service.CREDENTIALS_UNAVAILABLE
    assert "invited" in status.detail
    assert not (tmp_path / "absent.json").exists()


def test_expired_credentials_stop_the_service_before_it_starts(tmp_path, department):
    stale = _credentials(department)
    path = tmp_path / "credentials.json"
    stale.save(path)
    _enabled(tmp_path)
    service = peer_service.PeerService(
        directory=tmp_path, credentials_path=path, popen=_NoSpawn(),
        clock=lambda: stale.certificate.not_after + 1)
    status = service.start()
    assert status.state == peer_service.AUTH_FAILED
    assert status.reason == CERT_EXPIRED


def test_a_second_supervisor_is_refused_while_the_first_is_alive(tmp_path, credentials):
    path = tmp_path / "credentials.json"
    credentials.save(path)
    _enabled(tmp_path)
    (tmp_path / peer_service.LOCK_FILENAME).write_text(str(__import__("os").getpid()),
                                                       encoding="utf-8")
    service = peer_service.PeerService(
        directory=tmp_path, credentials_path=path, popen=_NoSpawn(),
        clock=lambda: NOW)
    status = service.start()
    assert status.state == peer_service.UNHEALTHY
    assert "already running" in status.detail


def test_a_stale_lock_from_a_dead_process_is_replaced(tmp_path):
    service = peer_service.PeerService(directory=tmp_path, clock=lambda: NOW)
    # A pid that cannot be alive: pid 0 is never a running process id here.
    (tmp_path / peer_service.LOCK_FILENAME).write_text("0", encoding="utf-8")
    acquired, other = service._acquire_lock()
    assert acquired is True and other is None
    assert (tmp_path / peer_service.LOCK_FILENAME).read_text().strip() == str(
        __import__("os").getpid())
    service._release_lock()
    assert not (tmp_path / peer_service.LOCK_FILENAME).exists()


# ---------------------------------------------------------------------------
# the local protocol
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("raw,code", [
    ("", "empty_request"),
    ("not json", "malformed_request"),
    ("[]", "malformed_request"),
    (json.dumps({"protocol": "other", "request_id": "a", "operation": "ping"}),
     "unsupported_protocol"),
    (json.dumps({"protocol": peer_service.IPC_PROTOCOL, "request_id": "a",
                 "operation": "exec"}), "unsupported_operation"),
    (json.dumps({"protocol": peer_service.IPC_PROTOCOL, "operation": "ping"}),
     "malformed_request"),
    (json.dumps({"protocol": peer_service.IPC_PROTOCOL, "request_id": "x" * 65,
                 "operation": "ping"}), "malformed_request"),
    pytest.param(" " * (peer_service.MAX_REQUEST_CHARS + 1), "request_too_large",
                 id="too_large"),
])
def test_invalid_requests_are_refused_with_a_stable_code(raw, code):
    with pytest.raises(peer_service.IpcError) as exc:
        peer_service.validate_request(raw)
    assert exc.value.code == code


def test_a_valid_request_is_accepted():
    payload = peer_service.validate_request(json.dumps({
        "protocol": peer_service.IPC_PROTOCOL, "request_id": "abc",
        "operation": "ping"}))
    assert payload["operation"] == "ping"


# ---------------------------------------------------------------------------
# a real child process
# ---------------------------------------------------------------------------

def _live_credentials(department, path):
    """Credentials valid against the *wall clock*, for tests that spawn."""
    credentials = _credentials(department, now=time.time())
    credentials.save(path)
    return credentials


@pytest.fixture()
def live_service(tmp_path, department):
    path = tmp_path / "credentials.json"
    _live_credentials(department, path)
    _enabled(tmp_path)
    service = peer_service.PeerService(
        directory=tmp_path, credentials_path=path, start_timeout=40.0,
        request_timeout=10.0, max_restarts=0, restart_delay=0.1)
    yield service
    service.stop()


def test_real_process_starts_answers_and_stops(live_service):
    status = live_service.start()
    assert status.state == peer_service.RUNNING, status.detail
    assert status.pid and status.pid > 0
    assert status.encrypted_store is not None

    pong = live_service.ping()
    assert isinstance(pong.get("pong"), float)

    peer = live_service.peer_status()["peer"]
    assert peer["device_id"] == status.peer["device_id"]
    assert "PRIVATE" not in json.dumps(peer)

    stopped = live_service.stop()
    assert stopped.state == peer_service.STOPPED
    assert not (live_service._dir / peer_service.LOCK_FILENAME).exists()


def test_a_killed_child_is_reported_as_unhealthy(live_service):
    assert live_service.start().state == peer_service.RUNNING
    child = live_service._proc
    child.kill()
    child.wait(timeout=10)

    deadline = time.time() + 10
    while time.time() < deadline:
        if live_service.status.state == peer_service.UNHEALTHY:
            break
        time.sleep(0.05)
    assert live_service.status.state == peer_service.UNHEALTHY
    assert "exited unexpectedly" in live_service.status.detail


def test_the_child_exits_when_its_supervisor_disappears(tmp_path, department):
    """Closing stdin is the orphan guard: no child may outlive the supervisor."""
    path = tmp_path / "credentials.json"
    credentials = _live_credentials(department, path)
    root = str(peer_service._backend_root())
    import os

    env = {**os.environ, "PYTHONPATH": root}
    process = subprocess.Popen(
        [sys.executable, "-m", "app.peer.service_main", "--credentials", str(path)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, encoding="utf-8", cwd=root, env=env)
    try:
        ready = json.loads(process.stdout.readline())
        assert ready["event"] == "ready", ready
        assert ready["peer"]["device_id"] == credentials.identity.device_id
        process.stdin.close()
        assert process.wait(timeout=15) == 0
    finally:
        if process.poll() is None:               # pragma: no cover - cleanup
            process.kill()


def test_the_child_refuses_credentials_it_cannot_verify(tmp_path):
    """A wrong-department credential is refused by the child itself."""
    path = tmp_path / "credentials.json"
    alpha = peer_ca.DepartmentCA.create("dept-alpha", "Alpha", now=time.time())
    beta = peer_ca.DepartmentCA.create("dept-beta", "Beta", now=time.time())
    stolen = _credentials(alpha, now=time.time())
    forged = replace(stolen, department_public_key=beta.public_key_pem)
    forged.save(path)

    root = str(peer_service._backend_root())
    import os

    env = {**os.environ, "PYTHONPATH": root}
    process = subprocess.Popen(
        [sys.executable, "-m", "app.peer.service_main", "--credentials", str(path)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, encoding="utf-8", cwd=root, env=env)
    out, _ = process.communicate(timeout=30)
    first = json.loads(out.splitlines()[0])
    assert first["event"] == "failed"
    assert first["reason"] == "AUTH_FAILED"
    assert process.returncode == 2


def test_resource_usage_of_a_running_child_is_measurable(live_service):
    assert live_service.start().state == peer_service.RUNNING
    usage = peer_service.process_usage(live_service.status.pid)
    if not usage.get("available"):
        pytest.skip("process measurement is only implemented for Windows")
    assert usage["working_set_bytes"] > 0
    assert usage["cpu_seconds"] >= 0
