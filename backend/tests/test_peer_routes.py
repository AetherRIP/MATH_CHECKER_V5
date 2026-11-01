"""The desktop's peer surface, tested through the backend it actually calls.

The supervisor is faked here on purpose: ``test_peer_service.py`` already starts
real children. What these tests pin down is the contract the UI depends on —
that a preference is never reported as an observation, that a failed start is
reported as a failure, and that reading the page cannot start anything.
"""
from __future__ import annotations

import json

import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from app.errors import AppError

from app.api import peer_routes
from app.peer import control as peer_control
from app.peer import service as peer_service


class FakeService:
    """A supervisor stand-in that records calls and returns chosen states."""

    def __init__(self, *, on_start: str = peer_service.RUNNING,
                 start_detail: str = "peer service is running",
                 on_stop: str = peer_service.STOPPED,
                 stop_detail: str = "peer service stopped") -> None:
        self.starts = 0
        self.stops = 0
        self._state = peer_service.STOPPED
        self._detail = "not started"
        self._on_start = on_start
        self._start_detail = start_detail
        self._on_stop = on_stop
        self._stop_detail = stop_detail
        self._pid = None

    def status_dict(self) -> dict:
        return {"state": self._state, "detail": self._detail, "pid": self._pid,
                "restarts": 0, "encrypted_store": True,
                "store_detail": "stored with DPAPI", "peer": None}

    def start(self):
        self.starts += 1
        self._state, self._detail = self._on_start, self._start_detail
        self._pid = 4242 if self._state == peer_service.RUNNING else None
        return peer_service.ServiceStatus(state=self._state, detail=self._detail,
                                          pid=self._pid)

    def stop(self):
        self.stops += 1
        self._state, self._detail = self._on_stop, self._stop_detail
        self._pid = None
        return peer_service.ServiceStatus(state=self._state, detail=self._detail)


@pytest.fixture()
def peer_home(tmp_path, monkeypatch):
    """A private peer directory: the switch file never touches real data."""
    monkeypatch.setattr(peer_control, "state_path",
                        lambda directory=None: tmp_path / "peer" / "peer_state.json")
    peer_routes.reset_supervisor()
    yield tmp_path / "peer"
    peer_routes.reset_supervisor()


@pytest.fixture()
def client(peer_home):
    app = FastAPI()
    app.add_exception_handler(
        AppError,
        lambda request, exc: JSONResponse(exc.to_dict(include_detail=True),
                                          status_code=exc.status))
    peer_routes.register(app)
    with TestClient(app) as test_client:
        yield test_client


def _use(monkeypatch, service) -> None:
    """Install the fake where the router actually reads it.

    The switch path calls the ``_service()`` factory but the status snapshot
    reads the memoised ``_SERVICE``; stubbing only the factory made the route
    report "no supervisor" while it was talking to one. Production sets both by
    construction, so the stub replaces the same name.
    """
    monkeypatch.setattr(peer_routes, "_SERVICE", service)


# --------------------------------------------------------------- reading state

def test_status_is_off_by_default_and_starts_nothing(client, monkeypatch):
    # Deliberately no supervisor at all: opening the page must not construct one.
    body = client.get("/api/peer/status").json()

    assert body["enabled"] is False
    assert body["state"] == "DISABLED"
    assert body["label"] == peer_control.LABEL_OFF
    assert body["supervised"] is False
    assert peer_routes._SERVICE is None


def test_a_missing_switch_file_is_reported_as_off(client, peer_home):
    assert not (peer_home / "peer_state.json").exists()
    body = client.get("/api/peer/status").json()
    assert body["enabled"] is False and body["corrupt"] is False


def test_a_corrupt_switch_file_is_unknown_and_off(client, peer_home):
    peer_home.mkdir(parents=True, exist_ok=True)
    (peer_home / "peer_state.json").write_bytes(b"{not json")
    body = client.get("/api/peer/status").json()
    assert body["enabled"] is False
    assert body["corrupt"] is True
    assert body["label"] == peer_control.LABEL_UNKNOWN


def test_status_says_credentials_are_absent_before_an_invitation(client, peer_home):
    body = client.get("/api/peer/status").json()
    assert body["credentials_present"] is False
    # ...and a supervisor has still not been constructed.
    assert body["supervised"] is False


def test_status_never_exposes_key_material(client, peer_home, monkeypatch):
    service = FakeService()
    _use(monkeypatch, service)
    client.post("/api/peer/preference", json={"enabled": True})

    text = json.dumps(client.get("/api/peer/status").json()).lower()
    for forbidden in ("private_key", "privatekey", "secret", "password", "token"):
        assert forbidden not in text


# ------------------------------------------------------------------ the switch

def test_enabling_persists_the_preference_and_reports_the_observation(
        client, peer_home, monkeypatch):
    service = FakeService()
    _use(monkeypatch, service)

    body = client.post("/api/peer/preference", json={"enabled": True}).json()

    assert peer_control.is_enabled(peer_home) is True
    assert service.starts == 1
    assert body["requested"] is True
    assert body["confirmed"] is True
    assert body["enabled"] is True            # stored preference
    assert body["state"] == peer_service.RUNNING
    assert body["supervised"] is True


def test_disabling_stops_the_service_and_keeps_the_preference_off(
        client, peer_home, monkeypatch):
    service = FakeService()
    _use(monkeypatch, service)
    client.post("/api/peer/preference", json={"enabled": True})

    body = client.post("/api/peer/preference", json={"enabled": False}).json()

    assert peer_control.is_enabled(peer_home) is False
    assert service.stops == 1
    assert body["confirmed"] is True
    # describe() names the combination: switch off is DISABLED, not the
    # supervisor's own STOPPED. The preference is the truth at this point.
    assert body["state"] == "DISABLED"
    assert body["label"] == peer_control.LABEL_OFF


def test_an_enable_that_cannot_start_is_not_reported_as_started(
        client, peer_home, monkeypatch):
    service = FakeService(on_start=peer_service.CREDENTIALS_UNAVAILABLE,
                          start_detail="this device has not been invited yet")
    _use(monkeypatch, service)

    body = client.post("/api/peer/preference", json={"enabled": True}).json()

    assert body["confirmed"] is False
    assert body["state"] == peer_service.CREDENTIALS_UNAVAILABLE
    assert "invited" in body["detail"]
    # The preference stays on, so a later launch retries after the invitation.
    assert peer_control.is_enabled(peer_home) is True


def test_repeated_enables_do_not_start_a_second_supervisor(
        client, peer_home, monkeypatch):
    service = FakeService()
    _use(monkeypatch, service)

    first = client.post("/api/peer/preference", json={"enabled": True}).json()
    second = client.post("/api/peer/preference", json={"enabled": True}).json()

    assert service.starts == 1
    assert first["state"] == second["state"] == peer_service.RUNNING


def test_repeated_disables_are_harmless(client, peer_home, monkeypatch):
    service = FakeService()
    _use(monkeypatch, service)
    client.post("/api/peer/preference", json={"enabled": True})

    client.post("/api/peer/preference", json={"enabled": False})
    body = client.post("/api/peer/preference", json={"enabled": False}).json()

    assert service.stops == 1
    assert body["state"] in (peer_service.STOPPED, peer_service.DISABLED)


def test_an_unhealthy_service_is_reported_as_unhealthy(client, peer_home, monkeypatch):
    service = FakeService(on_start=peer_service.UNHEALTHY,
                          start_detail="the peer process exited unexpectedly (code 3)")
    _use(monkeypatch, service)

    body = client.post("/api/peer/preference", json={"enabled": True}).json()

    assert body["confirmed"] is False
    assert body["state"] == peer_service.UNHEALTHY
    assert body["label"] == peer_control.LABEL_UNHEALTHY


# ------------------------------------------------------------- refused requests

@pytest.mark.parametrize("payload", [
    {}, {"enabled": "yes"}, {"enabled": 1}, {"enabled": None}, {"on": True},
])
def test_malformed_requests_are_refused_without_touching_the_switch(
        client, peer_home, monkeypatch, payload):
    service = FakeService()
    _use(monkeypatch, service)

    response = client.post("/api/peer/preference", json=payload)

    assert response.status_code == 400
    assert service.starts == 0 and service.stops == 0
    assert not (peer_home / "peer_state.json").exists()


def test_a_non_loopback_caller_cannot_touch_the_switch(client, peer_home,
                                                      monkeypatch):
    service = FakeService()
    _use(monkeypatch, service)
    monkeypatch.setattr(peer_routes, "LOCAL_HOSTS", frozenset({"127.0.0.1"}))

    response = client.post("/api/peer/preference", json={"enabled": True})

    assert response.status_code == 403
    assert service.starts == 0
    assert peer_control.is_enabled(peer_home) is False


def test_a_failed_switch_write_starts_nothing(client, peer_home, monkeypatch):
    service = FakeService()
    _use(monkeypatch, service)

    def boom(*args, **kwargs):
        raise OSError("the disk is read-only")

    monkeypatch.setattr(peer_routes.peer_control, "save_state", boom)

    response = client.post("/api/peer/preference", json={"enabled": True})

    assert response.status_code == 500
    assert "read-only" in json.dumps(response.json())
    assert service.starts == 0
