"""PUBLIC ADDRESS — the Settings switch that really starts/stops the endpoint.

What is real here: the switch, its persistence in ``config.toml``, the state
machine, the status file every surface in the app reads, the refusal to publish
an address that was not verified, the stop of the tunnel process tree, and the
owner-only HTTP endpoints.

What is stubbed (and only this): the ``cloudflared`` child process and the
external verification requests. Creating a real Cloudflare tunnel in a unit test
would depend on the internet, on Cloudflare and on a browser-reachable hostname
— so the tunnel process is replaced by a fake that behaves like the real one
(prints an address, stays alive, dies on stop), and ``verify_public`` /
``verify_alive`` are driven explicitly. The production verification code path
itself is covered by ``test_remote_access.py`` against a real local HTTP server.
"""
from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

import app.db as db
from app import auth_gate as ag
from app import public_access as pa
from app import remote_access as ra
from app.distributed import accounts
from app.errors import AppError
from app.main import app

FAKE_URL = "https://fake-tunnel-1.trycloudflare.com"


@pytest.fixture(autouse=True)
def _fresh_switch(monkeypatch, fresh_state):
    """A new manager (and a new debugger-free state) for every test."""
    monkeypatch.setattr(pa, "_manager", None)
    accounts._TABLES_READY = False
    monkeypatch.setenv("MC5_PORT", "8791")
    yield
    mgr = pa._manager
    if mgr is not None:
        try:
            mgr.shutdown()
        except Exception:  # noqa: BLE001
            pass
    monkeypatch.setattr(pa, "_manager", None)
    accounts._TABLES_READY = False


@pytest.fixture
def owner_account(fresh_state):
    return accounts.create_account(
        username="owner", display_name="Owner", password="sup3rsafe",
        role="DEVELOPER", permissions=["*"])


class FakeTunnel:
    """A cloudflared stand-in: announces an address, lives until stopped."""

    instances: list["FakeTunnel"] = []

    def __init__(self, name, argv, *, env=None, log_file=None, cwd=None):
        self.name = name
        self.argv = list(argv)
        self.lines = [f"INF Requesting new quick Tunnel on trycloudflare.com...",
                      f"INF |  {FAKE_URL}  |"]
        self.dead = False
        self.stopped = False
        self.pid = 424242
        FakeTunnel.instances.append(self)

    def alive(self) -> bool:
        return not self.dead

    def stop(self) -> None:
        self.stopped = True
        self.dead = True

    def tail(self, n: int = 6) -> str:
        return " | ".join(self.lines[-n:])


@pytest.fixture
def fake_tunnel(monkeypatch, fresh_state):
    FakeTunnel.instances = []
    monkeypatch.setattr(pa, "find_cloudflared",
                        lambda root=None: "C:/fake/cloudflared.exe")
    monkeypatch.setattr(pa, "Managed", FakeTunnel)
    monkeypatch.setattr(pa, "verify_public",
                        lambda base, timeout=90, http_fn=None:
                        (True, {"health": True, "ui": True, "auth_status": True,
                                "api_gated": True}, ""))
    monkeypatch.setattr(pa, "verify_alive", lambda base, timeout=20, http_fn=None:
                        (True, ""))
    return FakeTunnel


@pytest.fixture
def client():
    return TestClient(app, client=("127.0.0.1", 52000))


def _state() -> dict:
    return pa.status()


# ------------------------------------------------------------------- default
def test_public_address_defaults_to_off(fresh_state, monkeypatch):
    monkeypatch.delenv("MC5_PUBLIC", raising=False)
    state = _state()
    assert state["requested"] is False
    assert state["status"] == pa.STATE_DISABLED
    assert state["public_url"] is None
    assert state["managed_by"] is None
    assert ra.public_url() is None


def test_enable_without_cloudflared_reports_a_reason(fresh_state, monkeypatch,
                                                    owner_account):
    monkeypatch.delenv("MC5_PUBLIC", raising=False)
    monkeypatch.setattr(pa, "find_cloudflared", lambda root=None: None)
    state = pa.enable()
    assert state["status"] == pa.STATE_FAILED
    assert "cloudflared" in state["reason"]
    assert state["can_enable"] is False
    assert state["public_url"] is None
    # Nothing was persisted, so the switch did not silently turn public mode on.
    from app.config import get_config
    assert get_config().security.enabled is False


def test_enable_without_a_sign_in_account_is_refused(fresh_state, monkeypatch,
                                                    fake_tunnel):
    monkeypatch.delenv("MC5_PUBLIC", raising=False)
    monkeypatch.setattr(ag, "credentials_configured", lambda: False)
    monkeypatch.setattr(ag, "_has_any_account", lambda: False)
    state = pa.enable()
    assert state["status"] == pa.STATE_FAILED
    assert "sign-in account" in state["reason"]
    assert state["can_enable"] is False
    assert FakeTunnel.instances == []      # no tunnel was even created


# ------------------------------------------------------- switching it ON
def test_enable_creates_verifies_and_publishes(fresh_state, monkeypatch,
                                              fake_tunnel, owner_account):
    monkeypatch.delenv("MC5_PUBLIC", raising=False)
    state = pa.enable()
    assert state["status"] == pa.STATE_ONLINE
    assert state["public_url"] == FAKE_URL
    assert state["requested"] is True
    assert state["managed_by"] == "application"
    assert state["checks"]["api_gated"] is True
    assert len(FakeTunnel.instances) == 1

    # The tunnel was aimed at THIS server, on its own port.
    argv = FakeTunnel.instances[0].argv
    assert argv[argv.index("--url") + 1] == "http://127.0.0.1:8791"

    # Published exactly once, and only after verification.
    written = ra.read_status()
    assert written["status"] == "online" and written["public_url"] == FAKE_URL
    assert ra.public_url() == FAKE_URL
    assert ra.summary()["public_url"] == FAKE_URL
    assert ra.summary()["reachable"]["public"] is True

    # The preference is on disk, so a restart brings the endpoint back.
    from app.config import CONFIG_PATH, get_config
    assert "enabled = true" in CONFIG_PATH.read_text(encoding="utf-8")
    get_config(reload=True)
    assert pa.requested() is True

    pa.disable()


def test_enable_twice_creates_one_tunnel(fresh_state, monkeypatch, fake_tunnel,
                                        owner_account):
    monkeypatch.delenv("MC5_PUBLIC", raising=False)
    assert pa.enable()["status"] == pa.STATE_ONLINE
    again = pa.enable()
    assert again["status"] == pa.STATE_ONLINE
    assert len(FakeTunnel.instances) == 1     # no second tunnel, no conflict
    pa.disable()


def test_failed_verification_switches_back_to_off(fresh_state, monkeypatch,
                                                 fake_tunnel, owner_account):
    """A tunnel that exists but does not serve the app is NOT published."""
    monkeypatch.delenv("MC5_PUBLIC", raising=False)
    monkeypatch.setattr(pa, "verify_public",
                        lambda base, timeout=90, http_fn=None:
                        (False, {"health": True, "ui": False},
                         "Public endpoint answered but did not serve the Math Checker UI."))
    state = pa.enable()
    assert state["status"] == pa.STATE_FAILED
    assert "did not serve" in state["reason"]
    assert state["public_url"] is None
    assert FakeTunnel.instances[0].stopped is True      # the tunnel was stopped
    assert ra.public_url() is None
    from app.config import get_config
    assert get_config(reload=True).security.enabled is False   # rolled back
    assert ra.read_status()["status"] == "offline"


# ------------------------------------------------------- switching it OFF
def test_disable_stops_the_tunnel_and_stops_advertising(fresh_state, monkeypatch,
                                                       fake_tunnel,
                                                       owner_account):
    monkeypatch.delenv("MC5_PUBLIC", raising=False)
    online = pa.enable()
    assert online["public_url"] == FAKE_URL

    off = pa.disable()
    assert off["status"] == pa.STATE_DISABLED
    assert off["requested"] is False
    assert off["public_url"] is None
    assert FakeTunnel.instances[0].stopped is True

    # Not advertised anywhere any more...
    assert ra.public_url() is None
    assert ra.summary()["public_url"] is None
    assert ra.summary()["reachable"]["public"] is False
    assert ra.read_status()["status"] == "offline"
    # ...but the address itself is remembered for the next time it is switched on.
    assert off["last_address"] == FAKE_URL
    from app.config import get_config
    assert get_config().security.last_public_url == FAKE_URL
    assert get_config().security.enabled is False


def test_disable_refuses_when_the_launcher_owns_the_session(fresh_state,
                                                          monkeypatch):
    """start_public.bat's session cannot be switched off from the app."""
    monkeypatch.setenv("MC5_PUBLIC", "1")
    ra.write_status({"status": "online", "public_url": "https://launcher.example",
                     "verified_at": time.time(), "port": 8791})
    state = _state()
    assert state["status"] == pa.STATE_ONLINE
    assert state["managed_by"] == "launcher"
    assert state["public_url"] == "https://launcher.example"
    with pytest.raises(AppError) as excinfo:
        pa.disable()
    assert excinfo.value.code == "public_access_external_session"
    assert "start_public.bat" in str(excinfo.value)


def test_a_live_external_tunnel_is_adopted_not_duplicated(fresh_state,
                                                         monkeypatch,
                                                         fake_tunnel):
    """Another session's verified address is reported, never doubled."""
    monkeypatch.delenv("MC5_PUBLIC", raising=False)
    ra.write_status({"status": "online", "public_url": "https://other.example",
                     "verified_at": time.time(), "port": 9999,
                     "tunnel_pid": __import__("os").getpid()})
    state = _state()
    assert state["status"] == pa.STATE_ONLINE
    assert state["managed_by"] == "external"
    assert state["can_enable"] is True
    assert "already running" in state["blocked_reason"]
    assert FakeTunnel.instances == []          # nothing new was started


# ----------------------------------------------------- restarts + staleness
def test_switch_survives_a_restart_without_claiming_an_address(fresh_state,
                                                              monkeypatch,
                                                              fake_tunnel,
                                                              owner_account):
    monkeypatch.delenv("MC5_PUBLIC", raising=False)
    assert pa.enable()["status"] == pa.STATE_ONLINE
    pa.shutdown()                      # the app stops (tunnel dies with it)

    monkeypatch.setattr(pa, "_manager", None)          # ...and starts again
    from app.config import get_config
    get_config(reload=True)
    after_restart = _state()
    assert after_restart["requested"] is True          # preference kept
    assert after_restart["status"] == pa.STATE_STARTING  # honest: not online yet
    assert after_restart["public_url"] is None
    assert ra.public_url() is None
    assert ra.read_status()["status"] == "offline"

    # The startup hook brings the endpoint back on its own.
    FakeTunnel.instances = []
    assert pa.autostart_if_requested() is True
    assert len(FakeTunnel.instances) == 1
    assert _state()["status"] == pa.STATE_ONLINE
    pa.disable()


def test_monitor_reports_failure_and_never_keeps_a_dead_address(
        fresh_state, monkeypatch, fake_tunnel, owner_account):
    """A tunnel that dies silently must lose its address immediately."""
    monkeypatch.delenv("MC5_PUBLIC", raising=False)
    monkeypatch.setattr(pa, "HEARTBEAT_SECONDS", 0.05)
    monkeypatch.setattr(pa, "RE_ESTABLISH_PAUSE", 0.05)
    monkeypatch.setattr(pa, "verify_alive",
                        lambda base, timeout=20, http_fn=None:
                        (False, "The public endpoint did not answer /api/health."))
    assert pa.enable()["status"] == pa.STATE_ONLINE
    first_url = ra.read_status()["public_url"]
    assert first_url == FAKE_URL

    # The tunnel dies AND no replacement can be created, so the only correct
    # outcome is: offline, no advertised address, an actionable reason.
    FakeTunnel.instances[0].dead = True
    monkeypatch.setattr(pa, "find_cloudflared", lambda root=None: None)

    deadline = time.time() + 6
    while time.time() < deadline and _state()["status"] != pa.STATE_FAILED:
        time.sleep(0.05)
    state = _state()
    assert state["status"] == pa.STATE_FAILED
    assert state["public_url"] is None
    assert ra.read_status()["status"] == "offline"
    assert ra.public_url() is None
    assert ra.summary()["status"] == "offline"


# ----------------------------------------------------------- HTTP endpoints
def _sign_in(client, username, password) -> str:
    """Sign in like the real UI does and return the CSRF token."""
    response = client.post("/api/auth/login",
                           json={"username": username, "password": password})
    assert response.status_code == 204, response.text
    token = response.headers.get("X-MC5-CSRF")
    assert token
    return token


def test_http_switch_refuses_a_remote_signed_in_teacher(client, fresh_state,
                                                       monkeypatch, owner_account):
    """A signed-in TEACHER on another machine cannot expose this install."""
    from app.config import get_config
    accounts.create_account(username="remote_teacher", display_name="Remote T",
                            password="T3ach!ng1", role="TEACHER")
    csrf = _sign_in(client, "remote_teacher", "T3ach!ng1")
    monkeypatch.setattr(ag, "is_genuinely_local", lambda request: False)
    monkeypatch.delenv("MC5_PUBLIC", raising=False)

    refused = client.post("/api/access/public-address", json={"enabled": True},
                          headers={"X-MC5-CSRF": csrf})
    assert refused.status_code == 403
    assert refused.json()["code"] == "owner_required"
    assert get_config().security.enabled is False
    # ...and the same caller cannot turn multi-account mode on either.
    assert client.post("/api/access/multi-account", json={"enabled": True},
                       headers={"X-MC5-CSRF": csrf}).status_code == 403


def test_http_switch_works_for_the_local_owner(client, fresh_state, monkeypatch,
                                              fake_tunnel, owner_account):
    """The real flow: signed-in owner on this machine flips it ON, then OFF."""
    monkeypatch.delenv("MC5_PUBLIC", raising=False)
    csrf = _sign_in(client, "owner", "sup3rsafe")     # public mode is off here
    headers = {"X-MC5-CSRF": csrf}

    state = client.get("/api/access/state").json()
    assert state["owner"] == "local"
    assert state["public_address"]["status"] == pa.STATE_DISABLED

    turned_on = client.post("/api/access/public-address", json={"enabled": True},
                            headers=headers)
    assert turned_on.status_code == 200, turned_on.text
    assert turned_on.json()["public_address"]["status"] == pa.STATE_ONLINE
    assert turned_on.json()["public_address"]["public_url"] == FAKE_URL
    assert ra.public_url() == FAKE_URL

    turned_off = client.post("/api/access/public-address",
                             json={"enabled": False}, headers=headers)
    assert turned_off.status_code == 200, turned_off.text
    assert turned_off.json()["public_address"]["status"] == pa.STATE_DISABLED
    assert ra.public_url() is None
    assert ra.summary()["reachable"]["public"] is False


def test_http_switch_requires_the_enabled_field(client, fresh_state, monkeypatch,
                                               owner_account):
    csrf = _sign_in(client, "owner", "sup3rsafe")
    monkeypatch.delenv("MC5_PUBLIC", raising=False)
    response = client.post("/api/access/public-address", json={},
                           headers={"X-MC5-CSRF": csrf})
    assert response.status_code == 400
    assert response.json()["code"] == "bad_request"


def test_recheck_endpoint_reverifies_the_live_address(client, fresh_state,
                                                     monkeypatch, fake_tunnel,
                                                     owner_account):
    monkeypatch.delenv("MC5_PUBLIC", raising=False)
    csrf = _sign_in(client, "owner", "sup3rsafe")
    headers = {"X-MC5-CSRF": csrf}
    turned_on = client.post("/api/access/public-address", json={"enabled": True},
                            headers=headers)
    assert turned_on.status_code == 200, turned_on.text

    monkeypatch.setattr(pa, "verify_public",
                        lambda base, timeout=90, http_fn=None:
                        (False, {"health": False}, "Public endpoint did not answer /api/health."))
    rechecked = client.post("/api/access/public-address/recheck", headers=headers)
    assert rechecked.status_code == 200, rechecked.text
    body = rechecked.json()["public_address"]
    assert body["status"] == pa.STATE_FAILED
    assert body["public_url"] is None
    assert ra.public_url() is None
