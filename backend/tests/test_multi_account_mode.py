"""MULTI-ACCOUNT MODE — the switch that disables (never deletes) the layer.

The shipped default is OFF (single-account install). These tests check, against
the real application:

* the default, the config file and the environment override;
* that every multi-account OPERATION is refused by the backend (not merely
  hidden in the UI), and that nothing outside that set is affected;
* that the single-account essentials survive a disable - sign-in, heartbeats,
  device registration, the sync/report/update endpoints, the school/class
  structure and the app's own health/dashboard/settings API;
* that switching it back ON restores the full layer with every account, team
  and audit record still in place (no migration, no data loss).
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import app.db as db
from app import multi_account
from app.distributed import accounts, audit, org, teams
from app.main import app


@pytest.fixture(autouse=True)
def _isolate_switch(monkeypatch, fresh_state):
    """Every test here controls the switch itself (conftest defaults it ON)."""
    monkeypatch.delenv("MULTI_ACCOUNT_ENABLED", raising=False)
    accounts._TABLES_READY = False
    yield
    accounts._TABLES_READY = False


@pytest.fixture
def client():
    return TestClient(app, client=("127.0.0.1", 51000))


@pytest.fixture
def dev_account(fresh_state):
    return accounts.create_account(
        username="dev", display_name="Dev", password="sup3rsafe",
        role="DEVELOPER", permissions=["*"])


def _token(account: dict, device_id: str = "switch-test-device") -> str:
    return accounts.issue_token(account["id"], device_id)


# ------------------------------------------------------------------ the flag
def test_default_is_off(fresh_state, monkeypatch):
    """A fresh install (nothing configured) is a single-account install."""
    monkeypatch.delenv("MULTI_ACCOUNT_ENABLED", raising=False)
    assert multi_account.DEFAULT_ENABLED is False
    assert multi_account.enabled() is False
    assert multi_account.source() == "default"


def test_config_file_can_enable_it(fresh_state, monkeypatch):
    from app.config import get_config, save_config
    monkeypatch.delenv("MULTI_ACCOUNT_ENABLED", raising=False)
    cfg = get_config()
    cfg.accounts.multi_account = True
    save_config(cfg)
    assert multi_account.enabled() is True
    assert multi_account.source() == "config"


def test_environment_override_wins_both_ways(fresh_state, monkeypatch):
    from app.config import get_config, save_config
    cfg = get_config()
    cfg.accounts.multi_account = True
    save_config(cfg)

    monkeypatch.setenv("MULTI_ACCOUNT_ENABLED", "0")
    assert multi_account.enabled() is False
    assert multi_account.source() == "env"

    cfg.accounts.multi_account = False
    save_config(cfg)
    monkeypatch.setenv("MULTI_ACCOUNT_ENABLED", "true")
    assert multi_account.enabled() is True


def test_set_enabled_persists_and_survives_a_reload(fresh_state, monkeypatch):
    monkeypatch.delenv("MULTI_ACCOUNT_ENABLED", raising=False)
    assert multi_account.set_enabled(True) is True
    # A restart re-reads config.toml: the preference is really on disk.
    from app.config import CONFIG_PATH, get_config
    assert "multi_account = true" in CONFIG_PATH.read_text(encoding="utf-8")
    get_config(reload=True)
    assert multi_account.enabled() is True
    assert multi_account.set_enabled(False) is False
    get_config(reload=True)
    assert multi_account.enabled() is False


def test_capabilities_payload(fresh_state, monkeypatch):
    monkeypatch.delenv("MULTI_ACCOUNT_ENABLED", raising=False)
    caps = multi_account.capabilities()
    assert caps["multi_account"] is False
    assert caps["default"] is False
    assert caps["setting"] == "[accounts] multi_account"
    assert caps["env_var"] == "MULTI_ACCOUNT_ENABLED"
    assert caps["restart_required"] is False


# ------------------------------------------------- the bootstrap accounts
def test_a_single_account_install_has_no_accounts_at_all(fresh_state, monkeypatch):
    """The shipped default must stay account-free, and the tables still exist.

    ``auth_gate._has_any_account()`` counts ANY account, so the old
    unconditional bootstrap made a brand-new copy report itself as already
    configured: the sign-in screen demanded a password that existed only in a
    file nobody was told about, and the first-run "Create the first account"
    path was unreachable. This is the regression guard for that.
    """
    from app.auth_gate import _has_any_account
    from app.distributed import bootstrap

    monkeypatch.delenv("MULTI_ACCOUNT_ENABLED", raising=False)
    result = bootstrap.bootstrap()
    assert result["accounts"] == "skipped"
    assert result["bootstrap_needed"] is False
    assert accounts.account_count() == 0
    assert _has_any_account() is False
    assert not (fresh_state / "distributed_bootstrap.txt").exists()
    # The tables themselves are still ensured: the single-account essentials
    # (device registration, presence, sync, reports, updates) use them.
    assert db.query_one("SELECT COUNT(*) AS n FROM accounts")["n"] == 0


def test_a_fresh_install_offers_account_setup_not_a_password_prompt(
        client, fresh_state, monkeypatch):
    """What the browser's sign-in screen reads on a new installation."""
    from app.distributed import bootstrap

    monkeypatch.delenv("MULTI_ACCOUNT_ENABLED", raising=False)
    bootstrap.bootstrap()
    body = client.get("/api/auth/status").json()
    # ``configured: False`` is exactly what makes SignIn.tsx render the
    # "Create the first account for this installation" form instead of a
    # sign-in form nobody can satisfy.
    assert body["configured"] is False
    assert body["authenticated"] is False


def test_turning_the_switch_on_creates_the_administration_accounts(
        fresh_state, monkeypatch):
    """Multi-Account Mode must be usable the moment it is switched on.

    The distributed layer authenticates with its OWN accounts (bearer tokens),
    so with none present the feature would be enabled and unreachable.
    """
    import json

    monkeypatch.delenv("MULTI_ACCOUNT_ENABLED", raising=False)
    from app.distributed import bootstrap
    assert bootstrap.bootstrap()["accounts"] == "skipped"

    assert multi_account.set_enabled(True) is True
    creds_path = fresh_state / "distributed_bootstrap.txt"
    assert creds_path.exists()
    creds = json.loads(creds_path.read_text(encoding="utf-8"))
    developer = accounts.authenticate(creds["developer"]["username"],
                                      creds["developer"]["password"])
    assert developer is not None
    assert developer["role"] == "DEVELOPER"
    assert accounts.authenticate(creds["teacher"]["username"],
                                 creds["teacher"]["password"]) is not None

    # Switching off and on again never creates a second set of accounts.
    created = accounts.account_count()
    multi_account.set_enabled(False)
    multi_account.set_enabled(True)
    assert accounts.account_count() == created


def test_an_already_provisioned_install_is_never_re_provisioned(
        fresh_state, monkeypatch):
    """An install that already has its administration account keeps it.

    This is the upgrade path: an installation provisioned by an earlier build
    (or by the switch itself) must not gain a second developer account, must
    not overwrite the stored one, and must not drop a new credentials file over
    the one that still describes the live account.
    """
    from app.distributed import bootstrap

    monkeypatch.delenv("MULTI_ACCOUNT_ENABLED", raising=False)
    existing = accounts.create_account(
        username="head-teacher", display_name="Head Teacher",
        password="sup3rsafe", role="DEVELOPER", permissions=["*"])
    result = bootstrap.bootstrap(create_accounts=True)
    assert result["accounts"] == "present"
    assert accounts.account_count() == 1
    assert not (fresh_state / "distributed_bootstrap.txt").exists()
    assert accounts.authenticate("head-teacher", "sup3rsafe")["id"] == existing["id"]


# ------------------------------------------------------- the disabled rules
BLOCKED = [
    ("GET", "/api/distributed/accounts"),
    ("POST", "/api/distributed/accounts"),
    ("POST", "/api/distributed/accounts/abc/scope"),
    ("POST", "/api/distributed/accounts/abc/disable"),
    ("POST", "/api/distributed/accounts/abc/username"),
    ("POST", "/api/distributed/accounts/abc/display-name"),
    ("POST", "/api/distributed/accounts/abc/password-reset"),
    ("POST", "/api/distributed/accounts/abc/roles"),
    ("GET", "/api/distributed/teams"),
    ("GET", "/api/distributed/me/teams"),
    ("POST", "/api/distributed/teams"),
    ("GET", "/api/distributed/teams/xyz"),
    ("POST", "/api/distributed/teams/xyz/members"),
    ("POST", "/api/distributed/teams/xyz/members/remove"),
    ("POST", "/api/distributed/teams/xyz/active"),
    ("GET", "/api/distributed/teams/xyz/results"),
    ("GET", "/api/distributed/device/discovery"),
    ("GET", "/api/distributed/devices"),
    ("POST", "/api/distributed/devices/d1/link"),
    ("POST", "/api/distributed/devices/d1/revoke"),
]

ALLOWED = [
    ("GET", "/api/distributed/ping"),
    ("GET", "/api/distributed/capabilities"),
    ("POST", "/api/distributed/login"),
    ("POST", "/api/distributed/logout"),
    ("GET", "/api/distributed/me"),
    ("POST", "/api/distributed/change-password"),
    ("GET", "/api/distributed/agent/state"),
    ("POST", "/api/distributed/agent/state"),
    ("POST", "/api/distributed/heartbeat"),
    ("POST", "/api/distributed/register-worker"),
    ("POST", "/api/distributed/device/register"),
    ("GET", "/api/distributed/schools"),
    ("POST", "/api/distributed/schools"),
    ("POST", "/api/distributed/schools/s1/classes"),
    ("GET", "/api/distributed/workers"),
    ("GET", "/api/distributed/events"),
    ("POST", "/api/distributed/results"),
    ("GET", "/api/distributed/report-deliveries"),
    ("GET", "/api/distributed/updates"),
    ("GET", "/api/distributed/releases"),
    ("GET", "/api/distributed/errors"),
    ("GET", "/api/distributed/audit"),
    ("GET", "/api/distributed/sync-monitor"),
]


@pytest.mark.parametrize("method,path", BLOCKED)
def test_rule_table_blocks_every_multi_account_operation(method, path):
    assert multi_account.blocked_operation(method, path)


@pytest.mark.parametrize("method,path", ALLOWED)
def test_rule_table_keeps_single_account_essentials(method, path):
    assert multi_account.blocked_operation(method, path) is None


def test_rules_never_touch_other_api_paths():
    for path in ("/api/health", "/api/dashboard", "/api/settings",
                 "/api/access/state", "/api/distributedx/accounts"):
        assert multi_account.blocked_operation("GET", path) is None
        assert multi_account.blocked_operation("POST", path) is None


# --------------------------------------------------- enforcement over HTTP
def test_capabilities_endpoint_is_public_and_reports_off(client, monkeypatch):
    monkeypatch.delenv("MULTI_ACCOUNT_ENABLED", raising=False)
    response = client.get("/api/distributed/capabilities")
    assert response.status_code == 200
    assert response.json()["multi_account"] is False


def test_account_creation_is_refused_by_the_backend(client, dev_account,
                                                   monkeypatch):
    """A direct API call cannot create an account while the mode is off."""
    monkeypatch.delenv("MULTI_ACCOUNT_ENABLED", raising=False)
    token = _token(dev_account)
    response = client.post(
        "/api/distributed/accounts",
        json={"username": "sneaky", "display_name": "Sneaky",
              "password": "Br3akingIn1", "role": "TEACHER"},
        headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 403
    body = response.json()
    assert body["code"] == "multi_account_disabled"
    assert "Multi-account mode is off" in body["message"]
    # ...and the account really was not created.
    assert accounts.get_account_by_username("sneaky") is None


def test_role_and_team_operations_are_refused(client, dev_account, monkeypatch):
    monkeypatch.delenv("MULTI_ACCOUNT_ENABLED", raising=False)
    token = _token(dev_account)
    headers = {"Authorization": f"Bearer {token}"}
    other = accounts.create_account(username="t2", display_name="T2",
                                    password="sup3rsafe2", role="TEACHER")
    for path, payload in (
        (f"/api/distributed/accounts/{other['id']}/roles", {"roles": ["DEVELOPER"]}),
        (f"/api/distributed/accounts/{other['id']}/scope",
         {"school_ids": [], "class_ids": []}),
        (f"/api/distributed/accounts/{other['id']}/disable", {"active": False}),
        ("/api/distributed/teams", {"name": "Team"}),
        ("/api/distributed/teams/xyz/members", {"account_id": other["id"]}),
    ):
        response = client.post(path, json=payload, headers=headers)
        assert response.status_code == 403, path
        assert response.json()["code"] == "multi_account_disabled"
    # The role was NOT changed by the refused call.
    assert accounts.get_account(other["id"])["role"] == "TEACHER"


def test_single_account_essentials_keep_working_while_disabled(
        client, dev_account, monkeypatch):
    """The connection tests a real install needs must not depend on the layer."""
    monkeypatch.delenv("MULTI_ACCOUNT_ENABLED", raising=False)
    token = _token(dev_account)
    headers = {"Authorization": f"Bearer {token}"}

    assert client.get("/api/distributed/ping").status_code == 200
    login = client.post("/api/distributed/login",
                        json={"username": "dev", "password": "sup3rsafe",
                              "device_id": "switch-test-device"})
    assert login.status_code == 200
    assert login.json()["role"] == "DEVELOPER"

    assert client.get("/api/distributed/me", headers=headers).status_code == 200
    heartbeat = client.post("/api/distributed/heartbeat", json={
        "worker_id": "switch-test-device", "status": "ONLINE",
        "app_version": "5.2.1", "agent_version": "1.0.0"})
    assert heartbeat.status_code == 200
    result = client.post("/api/distributed/results", json={
        "student_id": "stu1", "submission_id": "s1",
        "submission_version": "v1", "worker_id": "switch-test-device",
        "raw_marks": 7, "max_marks": 9, "percentage": 78.0,
        "final_grade": 7, "correction_status": "done"}, headers=headers)
    assert result.status_code == 200, result.text
    for path in ("/api/distributed/schools", "/api/distributed/workers",
                 "/api/distributed/events", "/api/distributed/updates",
                 "/api/distributed/report-deliveries"):
        assert client.get(path, headers=headers).status_code == 200, path
    # The app's own (non-distributed) API is untouched by the switch.
    for path in ("/api/health", "/api/settings", "/api/dashboard"):
        assert client.get(path).status_code == 200, path


def test_schools_and_classes_survive_and_stay_usable(client, dev_account,
                                                     monkeypatch):
    """The academic structure is not multi-account functionality."""
    monkeypatch.delenv("MULTI_ACCOUNT_ENABLED", raising=False)
    token = _token(dev_account)
    headers = {"Authorization": f"Bearer {token}"}
    created = client.post("/api/distributed/schools", json={"name": "School A"},
                          headers=headers)
    assert created.status_code == 200
    school_id = created.json()["id"]
    assert client.post(f"/api/distributed/schools/{school_id}/classes",
                       json={"name": "10A"}, headers=headers).status_code == 200
    feed = client.get("/api/distributed/schools", headers=headers).json()
    assert [s["name"] for s in feed["schools"]] == ["School A"]
    assert [c["name"] for c in feed["classes"]] == ["10A"]


# ----------------------------------------- disabled -> re-enabled, no loss
def test_disable_then_reenable_keeps_accounts_teams_and_audit(
        client, dev_account, monkeypatch):
    """The whole point: the layer comes back exactly as it was."""
    token = _token(dev_account)
    headers = {"Authorization": f"Bearer {token}"}
    teacher = accounts.create_account(username="teacher1",
                                      display_name="Teacher One",
                                      password="T3ach!ng1", role="TEACHER")
    school = org.create_school("School A")
    teams.create_team("Team 10A", school_id=school["id"])
    audit.record(action="account.created", target_type="account",
                 target_id=teacher["id"], details={"username": "teacher1"})
    before_accounts = len(accounts.list_accounts())
    before_teams = db.query_one("SELECT COUNT(*) AS n FROM teams")["n"]
    before_audit = db.query_one("SELECT COUNT(*) AS n FROM audit_log")["n"]

    # --- OFF ---------------------------------------------------------------
    monkeypatch.delenv("MULTI_ACCOUNT_ENABLED", raising=False)
    assert multi_account.enabled() is False
    assert client.get("/api/distributed/accounts",
                      headers=headers).status_code == 403
    assert client.get("/api/distributed/teams", headers=headers).status_code == 403
    # Nothing was deleted or rewritten by being disabled.
    assert len(accounts.list_accounts()) == before_accounts
    assert db.query_one("SELECT COUNT(*) AS n FROM teams")["n"] == before_teams
    assert db.query_one("SELECT COUNT(*) AS n FROM audit_log")["n"] >= before_audit
    assert accounts.authenticate("teacher1", "T3ach!ng1")["role"] == "TEACHER"

    # --- ON again ----------------------------------------------------------
    monkeypatch.setenv("MULTI_ACCOUNT_ENABLED", "1")
    assert multi_account.enabled() is True
    listed = client.get("/api/distributed/accounts", headers=headers)
    assert listed.status_code == 200
    assert "teacher1" in [a["username"] for a in listed.json()["accounts"]]
    team_feed = client.get("/api/distributed/teams", headers=headers)
    assert team_feed.status_code == 200
    assert "Team 10A" in [t["name"] for t in team_feed.json()["teams"]]
    assert db.query_one("SELECT COUNT(*) AS n FROM audit_log")["n"] >= before_audit


def test_switch_toggle_endpoint_requires_the_owner(client, monkeypatch):
    """A non-local, unsigned caller can never change the mode."""
    from app import auth_gate as ag
    monkeypatch.setattr(ag, "is_genuinely_local", lambda request: False)
    monkeypatch.delenv("MULTI_ACCOUNT_ENABLED", raising=False)
    response = client.post("/api/access/multi-account", json={"enabled": True})
    assert response.status_code == 403
    assert response.json()["code"] == "owner_required"
    assert multi_account.enabled() is False


def test_switch_toggle_endpoint_works_for_the_local_owner(client, monkeypatch):
    from app import auth_gate as ag
    monkeypatch.setattr(ag, "is_genuinely_local", lambda request: True)
    monkeypatch.delenv("MULTI_ACCOUNT_ENABLED", raising=False)
    state = client.get("/api/access/state").json()
    assert state["owner"] == "local"
    assert state["multi_account"]["multi_account"] is False

    turned_on = client.post("/api/access/multi-account", json={"enabled": True})
    assert turned_on.status_code == 200
    assert turned_on.json()["multi_account"]["multi_account"] is True
    assert multi_account.enabled() is True

    turned_off = client.post("/api/access/multi-account", json={"enabled": False})
    assert turned_off.json()["multi_account"]["multi_account"] is False
    assert multi_account.enabled() is False
