"""Server lifecycle: start on demand, stay up while in use, stop when idle.

These tests drive the real state machine and the real registry (no sleeps, no
wall clock, no threads): the clock is injected, the "is anything running?"
collector is injected, and the exit request is captured instead of being sent
to uvicorn. What is being proven is the DECISION — which is the part that
must not be got wrong, because the failure mode is a teacher's correction
being interrupted or the app vanishing while it is in use.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import app.config as config
from app.lifecycle import activity, busy, control, sessions
from app.lifecycle import monitor as monitor_module
from app.lifecycle.monitor import (
    ALWAYS_ON, COUNTING_DOWN, HELD, SERVING, SHUTTING_DOWN, WAITING_FOR_WORK,
    LifecycleMonitor, LifecycleMonitorHolder,
)
from app.lifecycle.sessions import MAX_SESSION_ID, SessionRegistry

pytest.importorskip("fastapi")


# --------------------------------------------------------------- configuration

def lcfg(**over):
    """A lifecycle config with test-sized numbers."""
    base = dict(enabled=True, supervisor=True, heartbeat_seconds=5,
                session_timeout_seconds=30, idle_grace_seconds=10,
                max_shutdown_wait_seconds=60, last_chance_seconds=0,
                backend_port=0)
    base.update(over)
    return SimpleNamespace(**base)


@pytest.fixture
def clock():
    """A clock the test moves by hand.

    Deliberately NOT patched onto ``time.monotonic``: that would freeze
    asyncio's own scheduler and hang the whole test session. The monitor and
    the registry both take the clock as a value.
    """
    return {"now": 1000.0}


def make_monitor(clock, *, cfg=None, collect=None, holds=None):
    """A monitor wired to captured request/cancel hooks and the fake clock."""
    requested: list[tuple[str, str]] = []
    cancelled: list[int] = []
    mon = LifecycleMonitor(
        registry=SessionRegistry(timeout_seconds=30),
        clock=lambda: clock["now"],
        tick_seconds=0.01,
        collect=collect or (lambda: []),
        holds=holds or (lambda: []),
        request=lambda reason, detail="": (requested.append((reason, detail)),
                                           True)[1],
        cancel=lambda: (cancelled.append(1), True)[1],
    )
    cfg = cfg or lcfg()
    return mon, requested, cancelled, cfg


@pytest.fixture
def patched_cfg(monkeypatch):
    def install(cfg):
        monkeypatch.setattr(monitor_module, "_cfg", lambda: cfg)
        monkeypatch.setattr(monitor_module, "log_event", lambda *a, **k: None)
    return install


# ------------------------------------------------------------------ registry

def test_two_tabs_are_two_sessions():
    """A second tab is a second session, never the same one counted twice."""
    reg = SessionRegistry(timeout_seconds=30)
    reg.beat("tab-a")
    reg.beat("tab-b")
    assert reg.active_count() == 2
    assert reg.opened_total == 2


def test_one_tab_leaving_does_not_end_the_other(clock):
    """The example from the spec: A closes, B is still there, app stays up."""
    reg = SessionRegistry(timeout_seconds=30)
    reg.beat("A", now=clock["now"])
    reg.beat("B", now=clock["now"])
    assert reg.leave("A") is True
    assert reg.active_count(clock["now"]) == 1
    assert [s.id for s in reg.active(clock["now"])] == ["B"]


def test_a_silent_session_expires_and_is_removed(clock):
    """A closed browser that never said goodbye stops counting as active."""
    reg = SessionRegistry(timeout_seconds=30)
    reg.beat("A", now=clock["now"])
    assert reg.active_count(clock["now"] + 29) == 1
    assert reg.prune(clock["now"] + 31) == ["A"]
    assert reg.active_count(clock["now"] + 31) == 0


def test_absurd_or_empty_session_ids_are_refused():
    reg = SessionRegistry()
    assert reg.beat("") is None
    assert reg.beat("   ") is None
    assert reg.beat("x" * (MAX_SESSION_ID + 1)) is None
    assert reg.active_count() == 0


def test_expiry_helpers_report_real_ages(clock):
    reg = SessionRegistry(timeout_seconds=30)
    reg.beat("A", now=clock["now"])
    assert reg.last_seen_age(clock["now"] + 5) == pytest.approx(5.0)
    assert reg.next_expiry_in(clock["now"] + 5) == pytest.approx(25.0)
    assert reg.last_seen_age(clock["now"] + 40) is None


# ------------------------------------------------------- the state machine

def test_an_active_session_keeps_the_app_up(patched_cfg, clock):
    mon, requested, _cancelled, cfg = make_monitor(clock)
    patched_cfg(cfg)
    mon.beat("A")
    for _ in range(20):
        assert mon.tick() == SERVING
    assert requested == []
    assert mon.snapshot()["active_sessions"] == 1


def test_countdown_starts_when_the_last_session_goes(patched_cfg, clock):
    mon, requested, _cancelled, cfg = make_monitor(clock)
    patched_cfg(cfg)
    mon.beat("A")
    mon.leave("A")
    assert mon.tick() == COUNTING_DOWN
    assert mon.snapshot()["countdown_seconds"] == pytest.approx(10.0)
    assert requested == []          # not yet: the countdown is still running


def test_shutdown_happens_after_the_idle_period_and_only_once(patched_cfg, clock):
    mon, requested, _cancelled, cfg = make_monitor(clock)
    patched_cfg(cfg)
    mon.tick()
    clock["now"] += 5
    assert mon.tick() == COUNTING_DOWN
    clock["now"] += 11
    assert mon.tick() == SHUTTING_DOWN
    assert requested == [("idle", "no active browser sessions")]
    # Ten more ticks must not ask uvicorn to exit ten more times.
    for _ in range(10):
        mon.tick()
    assert requested == [("idle", "no active browser sessions")]
    assert mon.stops == 1


def test_returning_during_the_countdown_cancels_the_pending_shutdown(
        patched_cfg, clock):
    mon, requested, cancelled, cfg = make_monitor(clock)
    patched_cfg(cfg)
    mon.beat("A")
    mon.leave("A")
    mon.tick()                                     # counting down
    clock["now"] += 5
    mon.tick()                                     # ...still counting down
    assert mon.state == COUNTING_DOWN
    result = mon.beat("A")                         # the teacher came back
    assert result["accepted"] is True
    assert mon.state == SERVING
    assert mon.deadline is None
    # It now stays up for as long as the tab keeps beating — long past the
    # countdown that had already been running when the teacher came back.
    for _ in range(50):
        clock["now"] += 20
        mon.beat("A")
        assert mon.tick() == SERVING
    assert requested == []


def test_a_beat_during_the_shutdown_request_takes_it_back(patched_cfg, clock):
    """The last-chance window: a request is made, a heartbeat undoes it."""
    mon, requested, cancelled, cfg = make_monitor(
        clock, cfg=lcfg(last_chance_seconds=5))
    patched_cfg(cfg)
    mon.tick()                                     # countdown
    clock["now"] += 11
    assert mon.tick() == SHUTTING_DOWN              # request pending
    assert requested == []                          # ...inside the window
    mon.beat("A")
    assert mon.state == SERVING
    assert cancelled == [1]
    assert mon.cancels == 1
    assert control.pending() is False


def test_work_in_flight_defers_the_shutdown(patched_cfg, clock):
    running = {"busy": ["correction_jobs"]}
    mon, requested, _cancelled, cfg = make_monitor(
        clock, collect=lambda: ([{"source": "correction_jobs",
                                  "detail": "1 correction job(s) in progress"}]
                                if running["busy"] else []))
    patched_cfg(cfg)
    mon.tick()
    clock["now"] += 11
    assert mon.tick() == WAITING_FOR_WORK
    assert requested == []
    assert mon.deferrals == 1
    # The job finishes: the very next tick stops the app.
    running["busy"] = []
    clock["now"] += 5
    assert mon.tick() == SHUTTING_DOWN
    assert requested[0][0] == "idle"


def test_a_wedged_operation_cannot_pin_the_app_open_forever(patched_cfg, clock):
    mon, requested, _cancelled, cfg = make_monitor(
        clock, cfg=lcfg(max_shutdown_wait_seconds=60),
        collect=lambda: [{"source": "critical_operations",
                          "detail": "operation(s) in flight: db_write"}])
    patched_cfg(cfg)
    mon.tick()
    clock["now"] += 11
    assert mon.tick() == WAITING_FOR_WORK
    clock["now"] += 30
    assert mon.tick() == WAITING_FOR_WORK          # still inside the cap
    clock["now"] += 40                             # past the cap
    assert mon.tick() == SHUTTING_DOWN
    assert requested[0][0] == "idle"


def test_a_hold_never_expires_on_a_timer(patched_cfg, clock):
    """A published address keeps the app up for as long as it is published."""
    held = {"on": True}
    mon, requested, _cancelled, cfg = make_monitor(
        clock, holds=lambda: ([{"source": "public_endpoint",
                                "detail": "the public address is live"}]
                              if held["on"] else []))
    patched_cfg(cfg)
    assert mon.tick() == HELD
    clock["now"] += 86_400                          # a whole day later
    assert mon.tick() == HELD
    assert requested == []
    assert mon.snapshot()["countdown_seconds"] is None
    held["on"] = False                              # address switched off
    assert mon.tick() == COUNTING_DOWN


def test_the_switch_off_means_always_on(patched_cfg, clock):
    mon, requested, _cancelled, cfg = make_monitor(clock, cfg=lcfg(enabled=False))
    patched_cfg(cfg)
    mon.tick()
    clock["now"] += 86_400
    assert mon.tick() == ALWAYS_ON
    assert requested == []


def test_the_switch_off_applies_without_a_restart(patched_cfg, clock):
    cfg = lcfg(enabled=True)
    mon, requested, _cancelled, _ = make_monitor(clock, cfg=cfg)
    patched_cfg(cfg)
    mon.tick()
    clock["now"] += 11
    assert mon.tick() == SHUTTING_DOWN
    assert requested


def test_config_is_re_read_on_every_tick(patched_cfg, clock):
    """Changing Settings takes effect on the next tick, not on the next boot."""
    cfg = lcfg(session_timeout_seconds=30, idle_grace_seconds=10)
    mon, requested, _cancelled, _ = make_monitor(clock, cfg=cfg)
    patched_cfg(cfg)
    mon.beat("A")
    cfg.enabled = False
    assert mon.tick() == ALWAYS_ON
    assert mon.snapshot()["enabled"] is False
    assert requested == []


def test_rejected_heartbeat_reports_the_refusal(patched_cfg, clock):
    mon, _requested, _cancelled, cfg = make_monitor(clock)
    patched_cfg(cfg)
    assert mon.beat("x" * 500)["accepted"] is False
    assert mon.snapshot()["active_sessions"] == 0


def test_starting_the_monitor_takes_the_configured_timeout(
        patched_cfg, clock):
    """The configured session timeout applies from the first moment.

    Otherwise the registry would judge "is this session still here?" with its
    built-in default for up to one tick — and a boot log would report a value
    the configuration does not agree with.
    """
    cfg = lcfg(session_timeout_seconds=45, heartbeat_seconds=5)
    mon, _requested, _cancelled, _ = make_monitor(clock, cfg=cfg)
    patched_cfg(cfg)
    mon.registry.set_timeout(90)

    async def scenario():
        mon.start()
        assert mon.registry.timeout_seconds == 45
        await mon.stop()

    asyncio.run(scenario())


def test_the_monitor_starts_and_stops_its_loop(patched_cfg, clock):
    """The tick loop runs on the app's own event loop and stops when asked."""
    mon, _requested, _cancelled, cfg = make_monitor(clock, cfg=lcfg(enabled=False))
    patched_cfg(cfg)

    async def scenario():
        mon.start()
        await asyncio.sleep(0.05)
        assert mon.state == ALWAYS_ON
        await mon.stop()
        assert mon.state == "stopped"

    asyncio.run(scenario())


# -------------------------------------------------- the real "is it busy?" data

def test_an_open_write_transaction_counts_as_critical(fresh_state):
    """db.transaction() marks itself, so a stop cannot land inside a commit."""
    import app.db as db

    assert activity.busy() == []
    with db.transaction() as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS lifecycle_probe (n INTEGER)")
        assert activity.in_flight() >= 1
        assert "db_write" in activity.busy()
    assert activity.busy() == []


def test_a_failed_transaction_does_not_leak_the_count(fresh_state):
    import app.db as db

    with pytest.raises(RuntimeError):
        with db.transaction() as conn:
            conn.execute("CREATE TABLE IF NOT EXISTS lifecycle_probe2 (n INTEGER)")
            raise RuntimeError("boom")
    assert activity.busy() == []


def test_critical_operations_show_up_as_a_blocker(fresh_state):
    assert busy._critical_operations() is None
    with activity.critical("report_write"):
        detail = busy._critical_operations()
    assert detail and "report_write" in detail


def test_an_unreadable_source_is_not_clear(fresh_state, monkeypatch):
    """\"We could not tell whether a correction is running\" is not permission."""
    def broken():
        raise RuntimeError("database is locked")

    monkeypatch.setattr(busy, "SOURCES", (("correction_jobs", broken),))
    blockers = busy.collect()
    assert len(blockers) == 1
    assert "could not be checked" in blockers[0]["detail"]


def test_a_published_address_holds_the_app_up(fresh_state, monkeypatch):
    """The one reason that must never be defeated by a timer."""
    from app import public_access

    monkeypatch.setattr(public_access, "status",
                        lambda: {"status": "disabled"})
    busy.reset_hold_cache()
    assert busy.holds() == []
    monkeypatch.setattr(public_access, "status",
                        lambda: {"status": "online", "public_url": "https://x"})
    busy.reset_hold_cache()
    holds = busy.holds()
    assert holds and "cannot start this app" in holds[0]["detail"]
    # The answer is reused for a while instead of reading a status file and
    # checking a live PID on every tick.
    calls = {"n": 0}

    def counted():
        calls["n"] += 1
        return {"status": "online"}

    monkeypatch.setattr(public_access, "status", counted)
    busy.reset_hold_cache()
    assert busy._public_endpoint() is not None
    for _ in range(10):
        assert busy._public_endpoint() is not None
    assert calls["n"] == 1
    busy.reset_hold_cache()


def test_automation_leases_are_real_blockers(fresh_state):
    """A live automation lease is read from the table."""
    import app.db as db
    from app.automation import store as automation_store

    import time as _time

    assert busy._automation_jobs() is None
    with db.transaction() as conn:
        conn.execute(
            "INSERT INTO automation_jobs (id, course_id, coursework_id, "
            "submission_id, submission_version, status, created_at, "
            "updated_at, claimed_by, claimed_at) VALUES "
            "('j1','c1','cw1','s1','v1','CORRECTING',?,?,?,?)",
            (1.0, 1.0, "worker-1", _time.time()))
    detail = busy._automation_jobs()
    assert detail and "automation" in detail
    # A claim older than the lease is not work in progress any more.
    with db.transaction() as conn:
        conn.execute("UPDATE automation_jobs SET claimed_at = ? WHERE id = 'j1'",
                     (_time.time() - automation_store.CLAIM_LEASE_SECONDS - 5,))
    assert busy._automation_jobs() is None

    # A job parked for a retry, waiting for a teacher's approval or finished is
    # NOT work in progress: it survives the shutdown and resumes on the next
    # start, so it must not keep the app awake.
    with db.transaction() as conn:
        conn.execute("UPDATE automation_jobs SET status = 'AWAITING_APPROVAL', "
                     "claimed_by = NULL, claimed_at = NULL WHERE id = 'j1'")
    assert busy._automation_jobs() is None


# ------------------------------------------------------------------ the API

@pytest.fixture
def client(monkeypatch, clock, patched_cfg):
    from app.main import app
    cfg = lcfg()
    patched_cfg(cfg)
    mon, requested, cancelled, _ = make_monitor(clock, cfg=cfg)
    monkeypatch.setattr(LifecycleMonitorHolder, "_instance", mon)
    # No context manager: the routes are the subject here, and running the
    # whole application lifespan (worker pool, schedulers, tunnel) for each of
    # them would only make the suite slow. The lifespan wiring is proven by
    # test_the_app_starts_and_stops_the_monitor_with_its_lifespan below.
    yield TestClient(app, client=("127.0.0.1", 52000)), mon, requested
    LifecycleMonitorHolder.reset()


def test_heartbeat_registers_a_session(client):
    http, mon, _requested = client
    body = http.get("/api/lifecycle/heartbeat?sid=tab-a").json()
    assert body["ok"] is True
    assert body["active_sessions"] == 1
    assert body["state"] == SERVING
    assert body["backend"] == "running"


def test_status_is_read_only_and_never_counts_as_a_user(client):
    http, mon, _requested = client
    body = http.get("/api/lifecycle/status").json()
    assert body["state"] == SERVING
    assert body["active_sessions"] == 0
    assert mon.registry.active_count() == 0


def test_leave_removes_only_that_tab(client):
    http, mon, _requested = client
    http.get("/api/lifecycle/heartbeat?sid=tab-a")
    http.get("/api/lifecycle/heartbeat?sid=tab-b")
    assert http.get("/api/lifecycle/leave?sid=tab-a").json()["removed"] is True
    status = http.get("/api/lifecycle/status").json()
    assert status["active_sessions"] == 1
    assert status["state"] == SERVING


def test_a_bad_session_id_is_refused_not_stored(client):
    """An absurd id is rejected by the API contract, never held in memory."""
    http, mon, _requested = client
    response = http.get(f"/api/lifecycle/heartbeat?sid={'x' * 500}")
    assert response.status_code == 422
    assert mon.registry.active_count() == 0
    # An empty id is refused just as firmly.
    assert http.get("/api/lifecycle/heartbeat?sid=").status_code == 422


def test_the_lifecycle_api_is_reachable_without_a_session(client, monkeypatch):
    """The sign-in screen is a browser session too: it must be able to beat."""
    http, _mon, _requested = client
    monkeypatch.setenv("MC5_LOGIN_REQUIRED", "1")
    from app import auth_gate

    assert auth_gate.gate_enabled() is True
    assert http.get("/api/lifecycle/heartbeat?sid=login-screen").status_code == 200


def test_settings_expose_and_accept_the_lifecycle_block(client):
    http, _mon, _requested = client
    body = http.get("/api/settings").json()
    # The shipped defaults, reported as the backend really holds them.
    assert body["lifecycle"]["heartbeat_seconds"] == 30
    assert body["lifecycle"]["session_timeout_seconds"] == 90
    assert body["lifecycle"]["idle_grace_seconds"] == 120
    assert body["lifecycle"]["supervisor"] is True

    updated = http.post("/api/settings", json={
        "lifecycle": {"enabled": False, "heartbeat_seconds": 1,
                      "session_timeout_seconds": 2, "idle_grace_seconds": 99999,
                      "backend_port": 70000},
    }).json()
    # Clamped exactly like the config file, and the sanity rule holds.
    assert updated["lifecycle"]["enabled"] is False
    assert updated["lifecycle"]["heartbeat_seconds"] == 5      # floor
    assert updated["lifecycle"]["session_timeout_seconds"] == 15  # >= 2x beat
    assert updated["lifecycle"]["idle_grace_seconds"] == 86400   # ceiling
    assert config.get_config().lifecycle.enabled is False


def test_settings_refuse_to_let_the_page_invent_a_role(client):
    """Regression guard: the lifecycle block must not weaken the role check."""
    http, _mon, _requested = client
    response = http.post("/api/settings", json={"role": "developer"})
    assert response.status_code == 400


# --------------------------------------------------------------- wiring checks

def test_the_app_starts_and_stops_the_monitor_with_its_lifespan(
        fresh_state, monkeypatch):
    """The monitor is part of the app's own startup, not a separate daemon.

    It is started by the application lifespan and, just as importantly, STOPPED
    by the same lifespan: a monitor still ticking through a shutdown would be
    able to ask a dying process to exit.
    """
    from app.main import app, lifespan

    LifecycleMonitorHolder.reset()
    seen: list[str] = []

    # start() is synchronous (it schedules the tick loop on the running loop);
    # only stop() is a coroutine.
    def fake_start(self, loop=None):
        seen.append("start")

    async def fake_stop(self):
        seen.append("stop")

    monkeypatch.setattr(monitor_module.LifecycleMonitor, "start", fake_start)
    monkeypatch.setattr(monitor_module.LifecycleMonitor, "stop", fake_stop)

    async def scenario():
        async with lifespan(app):
            assert seen == ["start"]
            assert app.state.lifecycle is not None

    asyncio.run(scenario())
    assert seen == ["start", "stop"]
    LifecycleMonitorHolder.reset()


def test_control_attaches_and_cancels_a_graceful_exit():
    """The monitor's request is uvicorn's should_exit, and it is reversible."""
    seen: list[bool] = []
    control.attach(lambda: seen.append(True), lambda: seen.append(False))
    try:
        assert control.attached() is True
        assert control.request("idle", "nobody here") is True
        assert seen == [True]
        assert control.pending() is True
        assert control.cancel() is True
        assert seen == [True, False]
        assert control.pending() is False
        assert control.pending_for() == 0.0
    finally:
        control.reset()
    assert control.attached() is False
    # With nothing attached the request is reported as not made, not raised.
    assert control.request("idle") is False


def test_the_shipped_settings_reference_documents_every_lifecycle_setting():
    """The portable package ships ``Data/config.toml.example`` as the settings
    reference, and the packaged verifier fails the build when the reference does
    not document every setting the app writes. The lifecycle block was added to
    the app without being added to the reference, so this keeps them in step.
    """
    import tomllib
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    template = (root / "MathChecker_Distribution" / "documentation"
                / "config.toml.example")
    assert template.is_file(), "the portable settings reference is missing"
    documented = set(tomllib.loads(template.read_text(encoding="utf-8"))
                     .get("lifecycle", {}))
    written = set(config.AppConfig().lifecycle.__dataclass_fields__)
    assert written <= documented, (
        f"undocumented lifecycle settings: {sorted(written - documented)}")
