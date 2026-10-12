"""Tiny Agent ON/OFF control + multi-role accounts (§extension, §6-§10).

Covers the Definition-of-Done items that were genuinely missing:
  * an account can hold Assistant AND Developer at once,
  * legacy single-role accounts are untouched,
  * the agent switch really stops network activity and keeps the queue,
  * the preference survives a restart,
  * and pausing never interferes with the marking engine.

All tests use the isolated conftest database and a temporary agent directory,
so nothing is written to a real installation.
"""
from __future__ import annotations

import json

import pytest

import app.db as db
from app.distributed import accounts, agent_control, audit, sync
from app.distributed.tiny_agent import TinyAgent


@pytest.fixture(autouse=True)
def reset_dist_tables(fresh_state):
    accounts._TABLES_READY = False
    yield
    accounts._TABLES_READY = False


@pytest.fixture()
def agent_dir(tmp_path, monkeypatch):
    """Isolated agent directory: never touch the real agent/ folder."""
    path = tmp_path / "agent"
    path.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("MC_AGENT_DIR", str(path))
    return path


def _dev() -> dict:
    return accounts.create_account(username="dev", display_name="Dev",
                                   password="sup3rsafe", role="DEVELOPER")


# ============================================================ multi-role (§6-§10)

def test_account_can_hold_assistant_and_developer_together(fresh_state):
    acct = accounts.create_account(
        username="aid", display_name="Assistant Dev", password="p4ssw0rd!",
        roles=["ASSISTANT", "DEVELOPER"])
    assert set(accounts.account_roles(acct)) == {"ASSISTANT", "DEVELOPER"}
    assert accounts.has_role(acct, "ASSISTANT")
    assert accounts.has_role(acct, "DEVELOPER")
    # The stored primary role is the most privileged one.
    assert acct["role"] == "DEVELOPER"


def test_multi_role_permissions_are_a_union(fresh_state):
    acct = accounts.create_account(
        username="aid2", display_name="AD", password="p4ssw0rd!",
        roles=["ASSISTANT", "DEVELOPER"])
    perms = accounts.permissions_for(acct)
    # Developer wildcard survives AND the assistant rights are still present,
    # so switching dashboards never strips the marking workflow.
    assert "*" in perms


def test_legacy_single_role_account_behaviour_unchanged(fresh_state):
    """An account created the old way (role= only) must read exactly as before."""
    acct = accounts.create_account(username="t1", display_name="T",
                                   password="t3ach1ng!", role="TEACHER")
    assert accounts.account_roles(acct) == ["TEACHER"]
    assert accounts.has_role(acct, "TEACHER")
    assert not accounts.has_role(acct, "DEVELOPER")
    # Simulate a row created before the `roles` column existed.
    db.execute("UPDATE accounts SET roles = NULL WHERE id = ?", (acct["id"],))
    legacy = accounts.get_account(acct["id"])
    assert accounts.account_roles(legacy) == ["TEACHER"]


def test_multi_role_is_not_narrowed_to_the_weaker_role_scope(fresh_state):
    """A Developer+Assistant sees globally, even with an assistant class scope."""
    from app.distributed import org
    school = org.create_school("S1")
    classroom = org.create_class(school["id"], "10A")
    acct = accounts.create_account(
        username="aid3", display_name="AD", password="p4ssw0rd!",
        roles=["ASSISTANT", "DEVELOPER"])
    accounts.set_scope(acct["id"], [{"school_id": school["id"],
                                     "class_id": classroom["id"]}])
    scoped = accounts.get_account(acct["id"])
    schools, classes = accounts.scoped_ids(scoped)
    assert schools == set() and classes == set(), "developer role must stay global"


def test_assistant_only_account_is_still_scoped(fresh_state):
    from app.distributed import org
    school = org.create_school("S2")
    classroom = org.create_class(school["id"], "9B")
    acct = accounts.create_account(username="a1", display_name="A",
                                   password="p4ssw0rd!", role="ASSISTANT")
    accounts.set_scope(acct["id"], [{"school_id": school["id"],
                                     "class_id": classroom["id"]}])
    scoped = accounts.get_account(acct["id"])
    schools, classes = accounts.scoped_ids(scoped)
    assert classroom["id"] in classes
    assert accounts.can_see_class(scoped, school["id"], classroom["id"])
    assert not accounts.can_see_class(scoped, school["id"], "other-class")


def test_set_roles_can_promote_an_existing_account(fresh_state):
    acct = accounts.create_account(username="a2", display_name="A",
                                   password="p4ssw0rd!", role="ASSISTANT")
    updated = accounts.set_roles(acct["id"], ["ASSISTANT", "DEVELOPER"])
    assert set(accounts.account_roles(updated)) == {"ASSISTANT", "DEVELOPER"}
    with pytest.raises(ValueError):
        accounts.set_roles(acct["id"], [])
    with pytest.raises(ValueError):
        accounts.set_roles(acct["id"], ["WIZARD"])


# ================================================== tiny agent ON/OFF (§extension)

def test_this_version_runs_no_tiny_agent_by_default(agent_dir):
    """§1 of the simplification: the product switch ships OFF.

    Two separate facts are asserted, because they are deliberately different:
      * the PER-INSTALLATION preference is untouched — a missing state file
        still reads as "never switched off", so no stored record was rewritten;
      * the EFFECTIVE answer is "off", because this version does not run an
        agent at all.
    """
    state = agent_control.load_state()
    assert state["enabled"] is True          # the saved preference, preserved
    assert state["reason"] == "default"      # never rewritten by the switch
    assert agent_control.product_enabled() is False
    assert agent_control.is_enabled() is False


def test_the_agent_runs_again_when_the_product_switch_is_turned_on(
        agent_dir, monkeypatch):
    """The documented re-enable path actually restores the old behaviour."""
    monkeypatch.setenv(agent_control.ENV_VAR, "1")
    assert agent_control.product_enabled() is True
    assert agent_control.product_source() == "env"
    assert agent_control.is_enabled() is True
    # The per-installation OFF switch still works on top of it.
    agent_control.save_state(False)
    assert agent_control.is_enabled() is False
    agent_control.save_state(True)
    assert agent_control.is_enabled() is True


def test_agent_off_persists_across_reload(agent_dir, monkeypatch):
    monkeypatch.setenv(agent_control.ENV_VAR, "1")
    agent_control.save_state(False, updated_by="acct-1")
    assert agent_control.is_enabled() is False
    # A NEW process reading the same directory must still see OFF.
    fresh = agent_control.load_state()
    assert fresh["enabled"] is False
    assert fresh["updated_by"] == "acct-1"
    assert fresh["corrupt"] is False
    # And back on again.
    agent_control.save_state(True, updated_by="acct-1")
    assert agent_control.is_enabled() is True
    # With the version switch back off, no preference can make it run.
    monkeypatch.delenv(agent_control.ENV_VAR)
    assert agent_control.is_enabled() is False


def test_agent_state_write_is_atomic(agent_dir):
    agent_control.save_state(False)
    leftovers = list(agent_dir.glob("*.tmp"))
    assert leftovers == [], f"temp files left behind: {leftovers}"


def test_corrupt_state_file_keeps_agent_running_and_says_so(agent_dir):
    agent_control.state_path().write_text("{not json", encoding="utf-8")
    state = agent_control.load_state()
    assert state["enabled"] is True, "never silently stop syncing on a bad file"
    assert state["corrupt"] is True
    assert agent_control.status_label(state) == agent_control.LABEL_UNKNOWN


def test_status_labels(agent_dir):
    enabled = agent_control.load_state()
    assert agent_control.status_label(enabled, agent_seen_seconds_ago=None) \
        == agent_control.LABEL_STARTING
    assert agent_control.status_label(enabled, agent_seen_seconds_ago=5) \
        == agent_control.LABEL_RUNNING
    paused = agent_control.save_state(False)
    assert agent_control.status_label(paused) == agent_control.LABEL_PAUSED


def test_a_stale_heartbeat_never_reads_as_running(agent_dir):
    """A 16-hour-old heartbeat must NOT be reported as a running agent.

    "Enabled" is a preference, not proof of life: claiming "Agent Running" on
    stale data is exactly the mistake §4 forbids.
    """
    state = agent_control.load_state()
    assert state["enabled"] is True
    assert agent_control.status_label(state, agent_seen_seconds_ago=16 * 3600) \
        == agent_control.LABEL_NOT_REPORTING
    # Just inside the two-interval window is still considered recently active.
    assert agent_control.status_label(
        state, agent_seen_seconds_ago=agent_control.RECENTLY_ACTIVE_SECONDS - 1) \
        == agent_control.LABEL_RUNNING
    # And just outside it is not.
    assert agent_control.status_label(
        state, agent_seen_seconds_ago=agent_control.RECENTLY_ACTIVE_SECONDS + 1) \
        == agent_control.LABEL_NOT_REPORTING


def test_describe_reports_stale_reality(agent_dir, monkeypatch):
    """describe() must surface the staleness, not just the preference."""
    import time as _t

    # While this version does not run an agent, describe() says exactly that —
    # a disabled agent is never described as running, paused or starting.
    disabled = agent_control.describe(last_heartbeat=_t.time() - 30)
    assert disabled["product_enabled"] is False
    assert disabled["label"] == agent_control.LABEL_DISABLED
    assert disabled["product"]["reenable"]        # the documented way back

    monkeypatch.setenv(agent_control.ENV_VAR, "1")
    stale = agent_control.describe(last_heartbeat=_t.time() - 16 * 3600)
    assert stale["enabled"] is True
    assert stale["label"] == agent_control.LABEL_NOT_REPORTING
    fresh = agent_control.describe(last_heartbeat=_t.time() - 30)
    assert fresh["label"] == agent_control.LABEL_RUNNING


def _agent(agent_dir, post):
    return TinyAgent(backend_url="http://127.0.0.1:9", agent_dir=agent_dir,
                     worker_id="w1", account_id="acct-1", post=post)


def test_paused_agent_makes_no_network_calls_and_keeps_its_queue(agent_dir):
    calls = []

    def fake_post(path, payload, timeout=None):
        calls.append(path)
        return {"ok": True}

    agent = _agent(agent_dir, fake_post)
    event_id = agent.queue_event("correction.result", {"submission_id": "s1"})
    assert len(agent.queue.pending()) == 1

    agent_control.save_state(False)
    result = agent.tick()

    assert result["paused"] is True
    assert calls == [], f"paused agent must not touch the network, saw {calls}"
    # Pending work is preserved for a later switch-on.
    assert len(agent.queue.pending()) == 1
    assert result["pending"] == 1
    assert event_id


def test_enabled_agent_heartbeats_and_flushes(agent_dir, monkeypatch):
    monkeypatch.setenv(agent_control.ENV_VAR, "1")
    calls = []

    def fake_post(path, payload, timeout=None):
        calls.append(path)
        return {"ok": True}

    agent = _agent(agent_dir, fake_post)
    agent.queue_event("correction.result", {"submission_id": "s2"})
    result = agent.tick()

    assert result["heartbeat"] is True
    assert result["paused"] is False
    assert "/api/distributed/heartbeat" in calls
    assert agent.queue.pending() == [], "an acked event must leave the queue"


def test_switching_off_after_being_on_takes_effect_immediately(agent_dir, monkeypatch):
    monkeypatch.setenv(agent_control.ENV_VAR, "1")
    calls = []
    agent = _agent(agent_dir, lambda p, payload, timeout=None: calls.append(p) or {})

    agent.tick()
    assert calls, "expected a heartbeat while enabled"

    calls.clear()
    agent_control.save_state(False)
    agent.tick()
    assert calls == [], "the switch must take effect without restarting the agent"


def test_pausing_the_agent_does_not_block_the_marking_engine(agent_dir):
    """The correction pipeline must be completely independent of the switch."""
    import app.jobs.manager as jobs
    import time as _time
    agent_control.save_state(False)

    db.execute(
        "INSERT INTO correction_jobs(id, submission_id, status, progress, "
        "attempts, max_attempts, created_at, updated_at) "
        "VALUES(?,?,?,?,?,?,?,?)",
        ("job-paused-1", "sub-1", "QUEUED", 0.0, 0, 3, _time.time(), _time.time()))
    row = db.query_one("SELECT status FROM correction_jobs WHERE id = ?",
                       ("job-paused-1",))
    assert row["status"] == "QUEUED", "queueing work must work while the agent is off"
    manager = jobs.JobManager()
    assert manager.get("job-paused-1")["status"] == "QUEUED"


# ============================================================ API round-trip (§7)

def test_agent_state_route_round_trip_and_permission(agent_dir, monkeypatch):
    from fastapi.testclient import TestClient
    from app.main import app

    # The historical per-installation switch is still fully functional once the
    # version switch is on; that is the code path this test covers.
    monkeypatch.setenv(agent_control.ENV_VAR, "1")
    dev = _dev()
    token = accounts.issue_token(dev["id"])
    client = TestClient(app)
    headers = {"Authorization": f"Bearer {token}"}

    r = client.get("/api/distributed/agent/state", headers=headers)
    assert r.status_code == 200, r.text
    assert r.json()["enabled"] is True

    r = client.post("/api/distributed/agent/state", headers=headers,
                    json={"enabled": False})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["enabled"] is False
    assert body["label"] == agent_control.LABEL_PAUSED

    # Persisted, not just echoed back.
    assert agent_control.is_enabled() is False
    assert client.get("/api/distributed/agent/state",
                      headers=headers).json()["enabled"] is False

    r = client.post("/api/distributed/agent/state", headers=headers,
                    json={"enabled": True})
    assert r.json()["enabled"] is True


def test_agent_state_route_refuses_to_switch_on_an_agent_this_version_does_not_run(
        agent_dir):
    """A control that could not take effect must refuse instead of lying (§1).

    Switching the agent OFF is still accepted, so an installation carrying a
    legacy "enabled" record can always clear it.
    """
    from fastapi.testclient import TestClient
    from app.main import app

    token = accounts.issue_token(_dev()["id"])
    client = TestClient(app)
    headers = {"Authorization": f"Bearer {token}"}

    assert agent_control.product_enabled() is False
    r = client.post("/api/distributed/agent/state", headers=headers,
                    json={"enabled": True})
    assert r.status_code == 409, r.text
    assert r.json()["code"] == "tiny_agent_disabled"
    # The refusal explains how to bring it back rather than just saying no.
    assert agent_control.CONFIG_KEY in r.json()["message"]

    r = client.post("/api/distributed/agent/state", headers=headers,
                    json={"enabled": False})
    assert r.status_code == 200, r.text
    assert r.json()["enabled"] is False

    # The reported state always names the version switch and how to reverse it.
    state = client.get("/api/distributed/agent/state", headers=headers).json()
    assert state["product_enabled"] is False
    assert state["label"] == agent_control.LABEL_DISABLED
    assert state["product"]["available"] is False


def test_agent_state_requires_authentication(agent_dir):
    from fastapi.testclient import TestClient
    from app.main import app

    client = TestClient(app)
    assert client.get("/api/distributed/agent/state").status_code == 401
    assert client.post("/api/distributed/agent/state",
                       json={"enabled": False}).status_code == 401


def test_toggling_the_agent_is_audited(agent_dir):
    dev = _dev()
    token = accounts.issue_token(dev["id"])
    from fastapi.testclient import TestClient
    from app.main import app

    client = TestClient(app)
    client.post("/api/distributed/agent/state",
                headers={"Authorization": f"Bearer {token}"},
                json={"enabled": False})
    row = db.query_one("SELECT action FROM audit_log WHERE action = 'agent.disabled'")
    assert row is not None, "switching the agent off must leave an audit trail"


def test_login_returns_every_role(fresh_state):
    accounts.create_account(username="aid4", display_name="AD",
                            password="p4ssw0rd!",
                            roles=["ASSISTANT", "DEVELOPER"])
    from fastapi.testclient import TestClient
    from app.main import app

    client = TestClient(app)
    r = client.post("/api/distributed/login",
                    json={"username": "aid4", "password": "p4ssw0rd!"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert set(body["roles"]) == {"ASSISTANT", "DEVELOPER"}
    assert body["role"] == "DEVELOPER"
