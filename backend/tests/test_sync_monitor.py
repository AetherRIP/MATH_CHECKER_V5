"""Developer sync-monitoring snapshot (§8).

Verifies the things that would actually mislead a developer: claiming a silent
agent is online, hiding a retrying report, missing a conflicting revision, or
leaking a secret into the diagnostics view.

Runs against the isolated conftest database; no network.
"""
from __future__ import annotations

import json
import time

import pytest

import app.db as db
from app.distributed import accounts, agents, audit, report_delivery, sync, sync_monitor


@pytest.fixture(autouse=True)
def reset_dist_tables(fresh_state):
    accounts._TABLES_READY = False
    accounts.ensure_tables_ready()
    yield
    accounts._TABLES_READY = False


@pytest.fixture()
def dev():
    return accounts.create_account(username="devsm", display_name="Dev",
                                   password="sup3rsafe", role="DEVELOPER")


@pytest.fixture()
def assistant():
    return accounts.create_account(username="asim", display_name="A",
                                   password="p4ssw0rd!", role="ASSISTANT")


def _headers(account):
    return {"Authorization": f"Bearer {accounts.issue_token(account['id'])}"}


def test_snapshot_reports_a_fresh_agent_as_reporting(dev):
    agents.heartbeat(device_id="dev-1", worker_id="w-1", account_id=dev["id"],
                     app_version="5.2.1", agent_version="1.0.0")
    snap = sync_monitor.snapshot()
    assert snap["agents_summary"]["total"] == 1
    assert snap["agents_summary"]["reporting"] == 1
    assert snap["agents_summary"]["silent"] == 0
    agent = snap["agents"][0]
    assert agent["app_version"] == "5.2.1"
    assert agent["agent_version"] == "1.0.0"
    assert agent["stale"] is False


def test_snapshot_treats_a_silent_agent_as_not_reporting(dev):
    """The stored status still says ONLINE, but the heartbeat is ancient."""
    agents.heartbeat(device_id="dev-2", worker_id="w-2", account_id=dev["id"])
    # Age it well past the presence window.
    db.execute("UPDATE workers SET last_heartbeat = ? WHERE worker_id = ?",
               (time.time() - agents.OFFLINE_AFTER_SECONDS - 60, "w-2"))
    snap = sync_monitor.snapshot()
    agent = snap["agents"][0]
    assert agent["stale"] is True
    assert agent["presence"] == agents.PRESENCE_OFFLINE
    assert snap["agents_summary"]["reporting"] == 0
    assert snap["agents_summary"]["silent"] == 1


def test_versions_are_aggregated(dev):
    for i, (app_v, agent_v) in enumerate([("5.2.1", "1.0.0"),
                                          ("5.2.1", "1.0.1"),
                                          ("5.1.0", "1.0.0")]):
        agents.heartbeat(device_id=f"dev-{i}", worker_id=f"wv-{i}",
                         account_id=dev["id"], app_version=app_v,
                         agent_version=agent_v)
    snap = sync_monitor.snapshot()
    assert snap["agents_summary"]["app_versions"] == {"5.2.1": 2, "5.1.0": 1}
    assert snap["agents_summary"]["agent_versions"] == {"1.0.0": 2, "1.0.1": 1}


def test_retry_history_shows_undelivered_reports_only(dev):
    passing = sync.record_correction_result(
        {"student_id": "s1", "submission_id": "sub-ok", "raw_marks": 5,
         "max_marks": 9, "percentage": 55.5, "final_grade": 5,
         "correction_status": "CORRECTED", "report_filename": "ok.pdf"},
        worker_id="w1")["record_id"]
    failing = sync.record_correction_result(
        {"student_id": "s2", "submission_id": "sub-bad", "raw_marks": 3,
         "max_marks": 9, "percentage": 33.3, "final_grade": 3,
         "correction_status": "CORRECTED", "report_filename": "bad.pdf"},
        worker_id="w1")["record_id"]
    report_delivery.mark_delivered(passing)
    report_delivery.mark_failed(failing, "connection refused")

    snap = sync_monitor.snapshot()
    ids = [r["result_record_id"] for r in snap["retries"]]
    assert failing in ids
    assert passing not in ids, "a delivered report is not 'retrying'"
    assert snap["sync"]["reports"]["delivered"] == 1
    assert snap["sync"]["reports"]["retry_scheduled"] == 1


def test_pending_failed_and_review_counts_stay_separate(dev):
    """Queued, hard-failed and out-of-attempts are three different situations.

    The dashboard must report them distinctly: folding 'failed' into a single
    bucket (or into 'pending') would let an exhausted transfer hide behind a
    healthy-looking queue.
    """
    def result(sub_id):
        return sync.record_correction_result(
            {"student_id": "s1", "submission_id": sub_id, "raw_marks": 5,
             "max_marks": 9, "percentage": 55.5, "final_grade": 5,
             "correction_status": "CORRECTED", "report_filename": f"{sub_id}.pdf"},
            worker_id="w1")["record_id"]

    result("sub-queued")                 # never attempted -> PENDING
    exhausted = result("sub-exhausted")  # burns the whole attempt budget
    hard_failed = result("sub-failed")   # FAILED, written by a give-up path
    for _ in range(report_delivery.DEFAULT_MAX_ATTEMPTS):
        report_delivery.mark_failed(exhausted, "still down")
    db.execute("UPDATE report_deliveries SET state = ? WHERE result_record_id = ?",
               (report_delivery.FAILED, hard_failed))

    reports = sync_monitor.snapshot()["sync"]["reports"]
    assert reports["pending"] == 1
    assert reports["failed"] == 1
    assert reports["needs_review"] == 1
    # An exhausted transfer is Needs-review, never Failed: a human still has to
    # decide, and the two states must not be collapsed into one another.
    assert reports["needs_review"] + reports["failed"] == 2


def test_conflicting_revisions_are_surfaced(dev):
    base = {"student_id": "s9", "raw_marks": 5, "max_marks": 9,
            "percentage": 55.5, "final_grade": 5,
            "correction_status": "CORRECTED"}
    # Same submission, same version, two different workers -> conflict.
    for worker in ("wa", "wb"):
        sync.record_correction_result(
            {**base, "submission_id": "sub-conflict"}, worker_id=worker,
            account_id=dev["id"])
    # A clean single-worker submission must NOT be reported as a conflict.
    sync.record_correction_result(
        {**base, "submission_id": "sub-clean"}, worker_id="wa",
        account_id=dev["id"])

    dupes = sync_monitor.snapshot()["duplicates"]
    assert [d["submission_id"] for d in dupes] == ["sub-conflict"]
    assert dupes[0]["workers"] == 2


def test_a_later_version_of_the_same_submission_is_a_conflict(dev):
    base = {"student_id": "s10", "raw_marks": 5, "max_marks": 9,
            "percentage": 55.5, "final_grade": 5,
            "correction_status": "CORRECTED", "submission_id": "sub-v"}
    sync.record_correction_result({**base, "submission_version": "v1"},
                                  worker_id="wq", account_id=dev["id"])
    sync.record_correction_result({**base, "submission_version": "v2"},
                                  worker_id="wq", account_id=dev["id"])
    dupes = sync_monitor.snapshot()["duplicates"]
    assert dupes and dupes[0]["versions"] == 2


def test_snapshot_never_leaks_secrets(dev):
    audit.report_error(
        error_type="sync", message="upload failed with api_key=AIzaSyTOPSECRET123",
        failed_stage="upload", account=dev, device_id="d1", worker_id="w1",
        context={"Authorization": "Bearer abc.def.ghi", "password": "hunter2",
                 "safe": "kept"})
    snap = sync_monitor.snapshot()
    blob = json.dumps(snap)
    for secret in ("AIzaSyTOPSECRET123", "hunter2", "Bearer abc.def.ghi"):
        assert secret not in blob, f"leaked {secret!r} into the monitor snapshot"
    # The error itself is still reported, just without the secret.
    assert snap["recent_errors"], "the error should still be visible"


def test_sync_events_include_agent_switch_history(dev):
    from fastapi.testclient import TestClient
    from app.main import app
    client = TestClient(app)
    client.post("/api/distributed/agent/state", headers=_headers(dev),
                json={"enabled": False})
    actions = [e["action"] for e in sync_monitor.snapshot()["sync_events"]]
    assert "agent.disabled" in actions


def test_route_is_developer_only(dev, assistant):
    from fastapi.testclient import TestClient
    from app.main import app

    client = TestClient(app)
    ok = client.get("/api/distributed/sync-monitor", headers=_headers(dev))
    assert ok.status_code == 200, ok.text
    body = ok.json()
    for key in ("agents", "agents_summary", "sync", "retries", "duplicates",
                "recent_errors", "sync_events"):
        assert key in body, f"missing {key}"

    # An Assistant holds diagnostics.limited, which must NOT open the whole
    # fleet's operational picture.
    blocked = client.get("/api/distributed/sync-monitor",
                         headers=_headers(assistant))
    assert blocked.status_code == 403, blocked.text
    assert client.get("/api/distributed/sync-monitor").status_code == 401


def test_teacher_can_see_the_monitor():
    teacher = accounts.create_account(username="tsm", display_name="T",
                                      password="t3ach1ng!", role="TEACHER")
    from fastapi.testclient import TestClient
    from app.main import app
    client = TestClient(app)
    r = client.get("/api/distributed/sync-monitor", headers=_headers(teacher))
    assert r.status_code == 200, r.text
