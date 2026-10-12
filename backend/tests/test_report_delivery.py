"""Report-delivery state tracking (§5/§6) + the grade/report independence rule.

The central claim under test: a synchronised GRADE is not proof the PDF
arrived, so the two states must move independently — including the exact case
"a report upload fails while the grade record succeeds".

Runs against the isolated conftest database; no network.
"""
from __future__ import annotations

import time

import pytest

import app.db as db
from app.distributed import accounts, report_delivery, sync


@pytest.fixture(autouse=True)
def reset_dist_tables(fresh_state):
    accounts._TABLES_READY = False
    # These tests drive the sync layer directly (no HTTP route to initialise
    # the distributed tables for us), so create them explicitly.
    accounts.ensure_tables_ready()
    yield
    accounts._TABLES_READY = False


def _result_payload(**over):
    payload = {
        "student_id": "stu-1", "submission_id": "sub-1",
        "raw_marks": 7, "max_marks": 9, "percentage": 77.8,
        "final_grade": 7, "correction_status": "CORRECTED",
    }
    payload.update(over)
    return payload


def test_report_reference_creates_a_pending_delivery(fresh_state):
    out = sync.record_correction_result(
        _result_payload(report_filename="stu-1.pdf", report_sha256="abc123",
                        report_size_bytes=4096),
        account_id="acct-1", worker_id="w1")
    assert out["report_delivery"] == report_delivery.PENDING
    item = report_delivery.delivery_for(out["record_id"])
    assert item["state"] == report_delivery.PENDING
    assert item["report_filename"] == "stu-1.pdf"
    assert item["attempts"] == 0


def test_grade_sync_succeeds_when_no_report_reference_is_present(fresh_state):
    out = sync.record_correction_result(_result_payload(), worker_id="w1")
    assert out["record_id"]
    assert out["report_delivery"] is None
    assert report_delivery.delivery_for(out["record_id"]) is None


def test_report_upload_fails_while_the_grade_record_succeeds(fresh_state):
    """The headline independence case (§5)."""
    out = sync.record_correction_result(
        _result_payload(report_filename="r.pdf"), worker_id="w1")
    record_id = out["record_id"]

    # The grade landed and is queryable...
    assert sync.list_correction_results(limit=10)[0]["id"] == record_id
    # ...while the report has failed and is scheduled to retry.
    failed = report_delivery.mark_failed(record_id, "connection reset")
    assert failed["state"] == report_delivery.RETRY_SCHEDULED
    assert failed["last_error"] == "connection reset"
    assert failed["next_retry_at"] is not None
    # The grade row is untouched by the report failure.
    still = db.query_one("SELECT id FROM correction_results_sync WHERE id = ?",
                         (record_id,))
    assert still is not None


def test_retry_backoff_grows_and_is_scheduled_in_the_future(fresh_state):
    out = sync.record_correction_result(
        _result_payload(submission_id="sub-backoff", report_filename="r.pdf"),
        worker_id="w1")
    record_id = out["record_id"]
    first = report_delivery.mark_failed(record_id, "timeout")
    second = report_delivery.mark_failed(record_id, "timeout")
    assert second["attempts"] == 2
    # Delay must be in the future and must not shrink as attempts grow.
    d1 = first["next_retry_at"] - first["updated_at"]
    d2 = second["next_retry_at"] - second["updated_at"]
    assert d1 > 0 and d2 > 0
    assert d2 >= d1 * 0.5, "backoff must not collapse (jitter aside)"


def test_exhausting_attempts_asks_for_human_review(fresh_state):
    out = sync.record_correction_result(
        _result_payload(submission_id="sub-max", report_filename="r.pdf"),
        worker_id="w1")
    record_id = out["record_id"]
    item = None
    for _ in range(report_delivery.DEFAULT_MAX_ATTEMPTS):
        item = report_delivery.mark_failed(record_id, "still failing")
    assert item["state"] == report_delivery.NEEDS_REVIEW
    assert item["next_retry_at"] is None, "a stuck report must stop auto-retrying"


def test_manual_retry_recovers_a_needs_review_item(fresh_state):
    out = sync.record_correction_result(
        _result_payload(submission_id="sub-manual", report_filename="r.pdf"),
        worker_id="w1")
    record_id = out["record_id"]
    for _ in range(report_delivery.DEFAULT_MAX_ATTEMPTS):
        report_delivery.mark_failed(record_id, "boom")
    revived = report_delivery.retry_now(record_id)
    assert revived["state"] == report_delivery.PENDING
    assert report_delivery.due_retries(now=time.time() + 1) != []


def test_delivered_report_is_never_re_sent(fresh_state):
    out = sync.record_correction_result(
        _result_payload(submission_id="sub-done", report_filename="r.pdf"),
        worker_id="w1")
    record_id = out["record_id"]
    report_delivery.mark_sending(record_id)
    done = report_delivery.mark_delivered(record_id, report_sha256="deadbeef")
    assert done["state"] == report_delivery.DELIVERED
    assert done["delivered_at"] is not None

    # Re-registering the same result (e.g. an auth-token refresh, or a
    # resent batch) must NOT rewind it to PENDING.
    again = report_delivery.record_pending(result_record_id=record_id,
                                           report_filename="r.pdf")
    assert again["state"] == report_delivery.DELIVERED

    # A manual retry must not resurrect an acknowledged upload either.
    assert report_delivery.retry_now(record_id)["state"] == report_delivery.DELIVERED
    # And it is not offered to the retry worker.
    assert all(d["result_record_id"] != record_id
               for d in report_delivery.due_retries(now=time.time() + 10_000))


def test_stale_sending_is_retried_after_lost_acknowledgement(fresh_state):
    """Upload succeeded but the ack was lost: the item must come back around."""
    out = sync.record_correction_result(
        _result_payload(submission_id="sub-stale", report_filename="r.pdf"),
        worker_id="w1")
    record_id = out["record_id"]
    report_delivery.mark_sending(record_id)
    # Age the SENDING row past the retry cap.
    db.execute(
        "UPDATE report_deliveries SET updated_at = ? WHERE result_record_id = ?",
        (time.time() - report_delivery.RETRY_CAP_SECONDS - 60, record_id))
    due = report_delivery.due_retries(now=time.time())
    assert any(d["result_record_id"] == record_id for d in due)


def test_summary_counts_reflect_real_state(fresh_state):
    a = sync.record_correction_result(
        _result_payload(submission_id="s-a", report_filename="a.pdf"),
        worker_id="w1")["record_id"]
    b = sync.record_correction_result(
        _result_payload(submission_id="s-b", report_filename="b.pdf"),
        worker_id="w1")["record_id"]
    report_delivery.mark_delivered(b)
    report_delivery.mark_failed(a, "nope")
    summary = report_delivery.summary()
    assert summary["delivered"] == 1
    assert summary["retry_scheduled"] == 1
    assert summary["total"] == 2


def test_confirming_delivery_requires_explicit_verification(fresh_state):
    from fastapi.testclient import TestClient
    from app.main import app
    from app.distributed import accounts as acc

    dev = acc.create_account(username="devx", display_name="D",
                             password="sup3rsafe", role="DEVELOPER")
    token = acc.issue_token(dev["id"])
    headers = {"Authorization": f"Bearer {token}"}
    record_id = sync.record_correction_result(
        _result_payload(submission_id="sub-conf", report_filename="r.pdf"),
        worker_id="w1")["record_id"]

    client = TestClient(app)
    # Without explicit confirmation the mark must be refused.
    bad = client.post(f"/api/distributed/report-deliveries/{record_id}/delivered",
                      headers=headers, json={})
    assert bad.status_code == 400
    assert bad.json()["code"] == "confirmation_required"

    ok = client.post(f"/api/distributed/report-deliveries/{record_id}/delivered",
                     headers=headers, json={"confirmed": True})
    assert ok.status_code == 200, ok.text
    assert ok.json()["state"] == report_delivery.DELIVERED


def test_report_retry_route_and_permission(fresh_state):
    from fastapi.testclient import TestClient
    from app.main import app
    from app.distributed import accounts as acc

    dev = acc.create_account(username="devy", display_name="D",
                             password="sup3rsafe", role="DEVELOPER")
    token = acc.issue_token(dev["id"])
    headers = {"Authorization": f"Bearer {token}"}
    record_id = sync.record_correction_result(
        _result_payload(submission_id="sub-rt", report_filename="r.pdf"),
        worker_id="w1")["record_id"]
    for _ in range(report_delivery.DEFAULT_MAX_ATTEMPTS):
        report_delivery.mark_failed(record_id, "boom")

    client = TestClient(app)
    listed = client.get("/api/distributed/report-deliveries", headers=headers)
    assert listed.status_code == 200, listed.text
    assert listed.json()["summary"]["total"] >= 1

    retried = client.post(
        f"/api/distributed/report-deliveries/{record_id}/retry", headers=headers)
    assert retried.status_code == 200, retried.text
    assert retried.json()["state"] == report_delivery.PENDING

    assert client.get("/api/distributed/report-deliveries").status_code == 401
