"""Regression tests for the Automatic page's Retry and Approve buttons.

Every test here pins ONE root cause found by auditing the buttons end to end
(UI handler -> API route -> runner -> persisted job row). The harness is the
same one ``tests/test_automation.py`` uses, so only the workflow logic is under
test: Google Classroom, the AI correction engine and the report renderer are
faked, and the database is isolated per test.
"""

from __future__ import annotations

import time

import pytest

from app import db
from app.automation import states, store
from app.automation.runner import StepFailure

from tests.test_automation import (  # noqa: E402
    FakeAttach, FakePublish, _make_job, make_runner,
)


# ---------------------------------------------------------------------------
# Root cause 1 — Retry reported success for work it never did
# ---------------------------------------------------------------------------

def test_retry_while_the_job_is_still_running_is_refused():
    """A job in flight has no step to retry: the old code returned the job
    unchanged with HTTP 200, so the button said "retrying" for a no-op."""
    job = _make_job()
    r = make_runner()
    store.set_status(job["id"], states.CORRECTING)

    with pytest.raises(StepFailure) as excinfo:
        r.retry(job["id"])
    assert excinfo.value.code == "not_retryable"
    assert "still in progress" in str(excinfo.value)
    assert store.get(job["id"])["status"] == states.CORRECTING


def test_retry_on_a_finished_job_is_refused_instead_of_reporting_success():
    job = _make_job()
    r = make_runner()
    assert r.advance(job["id"])["status"] == states.COMPLETE

    with pytest.raises(StepFailure) as excinfo:
        r.retry(job["id"])
    assert excinfo.value.code == "not_retryable"
    assert store.get(job["id"])["status"] == states.COMPLETE


def test_retry_on_a_review_hold_names_approve_and_changes_nothing():
    """A review-mode hold is a teacher DECISION, not a failure. Retry used to
    resume from CORRECTING, re-rendering the report and re-driving the whole
    pipeline underneath the pending approval."""
    from app.config import AutomationConfig
    job = _make_job()
    publish, attach = FakePublish(), FakeAttach()
    r = make_runner(config=AutomationConfig(review_mode=True), publish=publish,
                    attach=attach)
    held = r.advance(job["id"])
    assert held["status"] == states.ATTENTION_REQUIRED
    assert held["error_code"] == "review_mode_await_teacher"
    renders = r._state["renders"]

    with pytest.raises(StepFailure) as excinfo:
        r.retry(job["id"])
    assert excinfo.value.code == "not_retryable"
    assert "Approve" in str(excinfo.value)

    after = store.get(job["id"])
    assert after["status"] == states.ATTENTION_REQUIRED
    assert after["error_code"] == "review_mode_await_teacher"
    # The pipeline never re-ran: no second render, no Classroom write.
    r.advance(job["id"])
    r.run_pending()
    assert r._state["renders"] == renders
    assert attach.calls == 0
    assert publish.calls == []


# ---------------------------------------------------------------------------
# Root cause 2 — Retry restarted the whole pipeline instead of failing step
# ---------------------------------------------------------------------------

def test_retry_resumes_at_the_blocked_step_and_never_re_renders_the_report():
    """A job parked because ONE step was blocked must retry that step: the
    report must not be rendered or uploaded a second time."""
    from app.config import AutomationConfig
    job = _make_job()
    attach = FakeAttach()
    publish = FakePublish()
    r = make_runner(attach=attach, publish=publish,
                    config=AutomationConfig(review_mode=False,
                                            return_work=False))
    parked = r.advance(job["id"])
    assert parked["status"] == states.ATTENTION_REQUIRED
    assert parked["error_code"] == "return_disabled"
    renders, attaches = r._state["renders"], attach.calls
    assert (renders, attaches) == (1, 1)          # the report really was sent
    assert publish.calls == ["assign_grade"]      # the grade really was posted

    # The teacher turns automatic return back on and presses Retry.
    r._config.return_work = True
    r.retry(job["id"])
    assert store.get(job["id"])["status"] == states.GRADE_VERIFIED

    final = r.advance(job["id"])
    assert final["status"] == states.COMPLETE
    assert r._state["renders"] == renders, "the report was re-rendered"
    assert attach.calls == attaches, "a second report upload was attempted"
    assert r._state["enqueues"] == 1, "the AI correction was re-run"
    assert publish.calls == ["assign_grade", "return"]


def test_retry_clears_a_stale_claim_so_the_retry_takes_effect_at_once():
    """Root cause of 'the Retry button does nothing': after a crash the job kept
    the dead runner's claim, so store.claim() refused to advance it for up to
    120s and the retry looked ignored."""
    job = _make_job()
    r = make_runner()
    store.set_status(job["id"], states.REPORT_UPLOAD_FAILED, result_id="r1",
                     grade="8")
    assert store.claim(job["id"], worker="dead-runner") is not None
    db.execute("UPDATE automation_jobs SET claimed_at = ? WHERE id = ?",
               (time.time() - 1000, job["id"]))      # the worker died long ago

    r.retry(job["id"])
    assert store.get(job["id"])["claimed_by"] is None
    assert store.next_runnable()["id"] == job["id"]
    assert r.advance(job["id"])["status"] == states.COMPLETE


def test_retry_never_pulls_a_job_out_from_under_a_live_worker():
    """A FRESH claim means another worker owns the job right now: the retry is
    still queued (due immediately), but the live claim is left alone so the
    step can never run twice."""
    job = _make_job()
    r = make_runner()
    store.set_status(job["id"], states.GRADE_FAILED, result_id="r1", grade="8")
    assert store.claim(job["id"], worker="live-runner") is not None

    r.retry(job["id"])
    running = store.get(job["id"])
    assert running["claimed_by"] == "live-runner"      # untouched
    assert running["status"] == states.ATTACHMENT_VERIFIED
    # Once that worker releases the job, the retry runs on the next tick.
    store.release(job["id"])
    assert r.advance(job["id"])["status"] == states.COMPLETE


def test_a_second_retry_of_the_same_job_is_refused_not_duplicated():
    """Repeated clicks on Retry: the first re-arms the job, the second is
    refused (it is running again) instead of starting a second pass."""
    job = _make_job()
    r = make_runner()
    store.set_status(job["id"], states.GRADE_FAILED)

    r.retry(job["id"])
    assert store.get(job["id"])["status"] == states.ATTACHMENT_VERIFIED
    attach_calls = r._attach.calls
    with pytest.raises(StepFailure):
        r.retry(job["id"])
    assert store.get(job["id"])["status"] == states.ATTACHMENT_VERIFIED
    assert r._attach.calls == attach_calls


# ---------------------------------------------------------------------------
# Root cause 3 — Approve marked a job COMPLETE without publishing anything
# ---------------------------------------------------------------------------

def test_approve_after_a_failed_report_stage_really_publishes():
    """Approve used to fall straight through every stage and record COMPLETE
    on a job whose report had never been uploaded."""
    from app.classroom import report_attachment as ra
    job = _make_job()
    attach = FakeAttach(statuses=[ra.STATUS_UPLOAD_FAILED, ra.STATUS_VERIFIED])
    publish = FakePublish()
    r = make_runner(attach=attach, publish=publish)
    failed = r.advance(job["id"])
    assert failed["status"] == states.REPORT_UPLOAD_FAILED
    assert publish.calls == []                    # nothing reached Classroom

    final = r.approve(job["id"])
    assert final["status"] == states.COMPLETE
    assert final["drive_file_id"] == "drive-1"    # the report really was sent
    assert attach.calls == 2
    assert publish.calls == ["assign_grade", "return"]


def test_approve_of_a_crash_left_draft_publishes_the_final_grade():
    """A crash right after a DRAFT write must not let Approve return the work
    with only a draft grade on the submission."""
    job = _make_job()
    publish = FakePublish()
    r = make_runner(publish=publish)
    store.set_status(job["id"], states.GRADE_DRAFTED, grade="8",
                     result_id="r1", drive_file_id="drive-1")

    final = r.approve(job["id"])
    assert final["status"] == states.COMPLETE
    assert publish.calls == ["assign_grade", "return"]


def test_approve_without_a_result_is_refused_and_stays_where_it_was():
    job = _make_job()
    r = make_runner()
    with pytest.raises(StepFailure) as excinfo:
        r.approve(job["id"])
    assert excinfo.value.code == "no_result"
    assert store.get(job["id"])["status"] == states.NEW


def test_an_approved_job_persists_across_a_restart():
    """Approve -> COMPLETE on disk; the boot path (and the poller) leave it."""
    from app.config import AutomationConfig
    job = _make_job()
    r = make_runner(config=AutomationConfig(review_mode=True))
    assert r.advance(job["id"])["status"] == states.ATTENTION_REQUIRED
    assert r.approve(job["id"])["status"] == states.COMPLETE

    store.recover_in_flight()                     # what a Windows restart runs
    reloaded = store.get(job["id"])
    assert reloaded["status"] == states.COMPLETE
    assert reloaded["completed_at"] is not None
    assert reloaded["claimed_by"] is None
    assert store.next_runnable() is None


# ---------------------------------------------------------------------------
# API layer — the button and the backend must agree about every state
# ---------------------------------------------------------------------------

@pytest.fixture
def client(fresh_state):
    from app.main import app
    from fastapi.testclient import TestClient
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def fake_runner(monkeypatch):
    """Point the API routes at one fake-external WorkflowRunner."""
    import app.automation.runner as runner_mod
    created = {}

    def factory(*, attach=None, publish=None, config=None, **kw):
        r = make_runner(attach=attach, publish=publish, config=config, **kw)
        created["runner"] = r
        monkeypatch.setattr(runner_mod, "runner", lambda: r)
        return r

    return factory


def test_retry_endpoint_refuses_a_review_hold_with_409(client, fake_runner):
    from app.config import AutomationConfig
    job = _make_job()
    r = fake_runner(config=AutomationConfig(review_mode=True))
    assert r.advance(job["id"])["status"] == states.ATTENTION_REQUIRED

    resp = client.post(f"/api/automation/jobs/{job['id']}/retry")
    assert resp.status_code == 409, resp.text
    body = resp.json()
    assert body["code"] == "not_retryable"
    assert "Approve" in body["message"]
    assert store.get(job["id"])["status"] == states.ATTENTION_REQUIRED


def test_retry_endpoint_reports_the_real_step_for_a_failed_job(client,
                                                              fake_runner):
    job = _make_job()
    r = fake_runner()
    store.set_status(job["id"], states.ATTACHMENT_FAILED)

    # What the page advertises BEFORE the click: the step the retry will run.
    row = next(j for j in client.get("/api/automation/jobs").json()["jobs"]
               if j["id"] == job["id"])
    assert row["resumable"] is True
    assert row["retry_from"] == states.label(states.REPORT_VALIDATED)

    resp = client.post(f"/api/automation/jobs/{job['id']}/retry")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == states.REPORT_VALIDATED
    assert "Retrying from" in body["step_detail"]
    assert body["resumable"] is False          # it is running again now
    assert store.get(job["id"])["claimed_by"] is None


def test_jobs_payload_hides_retry_on_a_hold_and_offers_approve(client,
                                                              fake_runner):
    from app.config import AutomationConfig
    job = _make_job()
    r = fake_runner(config=AutomationConfig(review_mode=True))
    r.advance(job["id"])
    row = next(j for j in client.get("/api/automation/jobs").json()["jobs"]
               if j["id"] == job["id"])
    assert row["resumable"] is False        # no Retry button on a hold
    assert row["approvable"] is True        # Approve is the real action
    assert row["awaiting_approval"] is True


def test_jobs_payload_offers_retry_for_a_blocked_step_only(client, fake_runner):
    """A job parked by a blocked step keeps its Retry escape hatch, and says
    which step the retry will run."""
    job = _make_job()
    r = fake_runner()
    store.set_status(job["id"], states.ATTENTION_REQUIRED,
                     detail="Automatic return is disabled", result_id="r1",
                     grade="8", error_code="return_disabled")
    row = next(j for j in client.get("/api/automation/jobs").json()["jobs"]
               if j["id"] == job["id"])
    assert row["resumable"] is True
    assert row["retry_from"] == states.label(states.GRADE_VERIFIED)
    assert row["approvable"] is True


def test_two_retry_clicks_do_not_duplicate_work(client, fake_runner):
    """The double-click case: the first retry re-arms the job, the second is
    refused, and neither of them runs the step twice."""
    job = _make_job()
    r = fake_runner()
    store.set_status(job["id"], states.RETURN_FAILED)

    first = client.post(f"/api/automation/jobs/{job['id']}/retry")
    second = client.post(f"/api/automation/jobs/{job['id']}/retry")
    assert first.status_code == 200, first.text
    assert second.status_code == 409, second.text
    assert store.get(job["id"])["status"] == states.GRADE_VERIFIED
    assert r._publish.calls == []


def test_approve_endpoint_publishes_and_persists(client, fake_runner):
    from app.config import AutomationConfig
    job = _make_job()
    publish = FakePublish()
    attach = FakeAttach()
    r = fake_runner(config=AutomationConfig(review_mode=True),
                    publish=publish, attach=attach)
    assert r.advance(job["id"])["status"] == states.ATTENTION_REQUIRED

    resp = client.post(f"/api/automation/jobs/{job['id']}/approve")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == states.COMPLETE
    assert body["report_uploaded"] is True
    assert attach.calls == 1
    assert publish.calls == ["assign_grade", "return"]

    # The evidence the drawer renders comes from the detail endpoint: only
    # Google's own confirmed operations may read as published/returned.
    detail = client.get(f"/api/automation/jobs/{job['id']}").json()
    assert detail["publication"]["grade_published"] is True
    assert detail["publication"]["returned"] is True
    assert detail["publication"]["complete"] is True
    assert detail["report_uploaded"] is True

    # Persisted: a fresh read of the row and a second approval attempt agree.
    assert store.get(job["id"])["status"] == states.COMPLETE
    again = client.post(f"/api/automation/jobs/{job['id']}/approve")
    assert again.status_code == 409
    assert again.json()["code"] == "not_approvable"
    assert attach.calls == 1 and publish.calls == ["assign_grade", "return"]


def test_approve_endpoint_refuses_a_job_with_no_result(client, fake_runner):
    job = _make_job()
    r = fake_runner()
    store.set_status(job["id"], states.ATTENTION_REQUIRED,
                     detail="waiting", error_code="requires_review")
    resp = client.post(f"/api/automation/jobs/{job['id']}/approve")
    assert resp.status_code == 409
    assert resp.json()["code"] == "no_result"
    assert store.get(job["id"])["status"] == states.ATTENTION_REQUIRED


def test_retry_and_approve_agree_on_every_state_the_page_can_show():
    """One table, both sides: whatever the API advertises as resumable is what
    the runner actually accepts, and never a decision state."""
    sample = {
        states.NEW: False,
        states.CORRECTING: False,
        states.RETURNING: False,
        states.COMPLETE: False,
        states.CANCELLED: False,
        states.CORRECTION_FAILED: True,
        states.REPORT_FAILED: True,
        states.REPORT_UPLOAD_FAILED: True,
        states.ATTACHMENT_FAILED: True,
        states.GRADE_FAILED: True,
        states.GRADE_VERIFICATION_FAILED: True,
        states.RETURN_FAILED: True,
        states.CLEANUP_PENDING: True,
        states.BLOCKED_NOT_MODIFIABLE: True,
    }
    for status, expected in sample.items():
        job = {"id": "j", "status": status, "error_code": None,
               "result_id": "r1", "drive_file_id": "d1",
               "correction_job_id": "cj1"}
        assert (states.retry_step(job) is not None) is expected, status
    # Decision states are never retryable, whatever else they carry.
    for code in ("review_mode_await_teacher",):
        job = {"id": "j", "status": states.ATTENTION_REQUIRED, "error_code": code,
               "result_id": "r1", "drive_file_id": "d1",
               "correction_job_id": "cj1"}
        assert states.retry_step(job) is None


def test_a_retry_never_resumes_at_a_step_that_re_marks_the_student():
    """The safety rule behind the whole resume table: no attention code may
    send a job back to the AI correction or the report render."""
    reruns_ai = {states.NEW}
    rerenders_report = {states.RESULT_VALIDATED, states.REPORT_GENERATED}
    for code in states.BLOCKED_STEP_FROM_CODE:
        job = {"id": "j", "status": states.ATTENTION_REQUIRED, "error_code": code,
               "result_id": "r1", "drive_file_id": "d1",
               "correction_job_id": "cj1"}
        step = states.retry_step(job)
        assert step is not None, code
        assert step not in rerenders_report, code
        if step in reruns_ai:
            # Only the case where the AI job itself is gone may re-enqueue.
            assert code == "correction_job_missing", code

    # A job with NO result yet must never be sent past the correction step.
    bare = {"id": "j", "status": states.ATTENTION_REQUIRED,
            "error_code": "requires_auth", "result_id": None,
            "drive_file_id": None, "correction_job_id": None}
    assert states.retry_step(bare) == states.NEW
    half = dict(bare, correction_job_id="cj1")
    assert states.retry_step(half) == states.CORRECTING
    delivered = dict(bare, result_id="r1", drive_file_id="d1")
    assert states.retry_step(delivered) == states.ATTACHMENT_VERIFIED


def test_the_runner_refusal_message_is_written_for_the_teacher():
    from app.automation.runner import WorkflowRunner
    assert "Approve" in WorkflowRunner.retry_refusal(
        {"status": states.ATTENTION_REQUIRED, "error_code": None})
    assert "already complete" in WorkflowRunner.retry_refusal(
        {"status": states.COMPLETE, "error_code": None})
    assert "in progress" in WorkflowRunner.retry_refusal(
        {"status": states.CORRECTING, "error_code": None})
    assert "decision" in WorkflowRunner.retry_refusal(
        {"status": states.ATTENTION_REQUIRED,
         "error_code": "review_mode_await_teacher"})


def test_a_job_with_no_retry_step_is_never_left_claimable_by_a_retry():
    """A refused retry must not touch the row: attempts, claim and due time
    stay exactly as the failed run left them."""
    job = _make_job()
    r = make_runner()
    store.set_status(job["id"], states.ATTENTION_REQUIRED,
                     detail="REVIEW MODE hold", error_code="review_mode_await_teacher",
                     result_id="r1", grade="8")
    before = db.query_one("SELECT * FROM automation_jobs WHERE id = ?", (job["id"],))
    with pytest.raises(StepFailure):
        r.retry(job["id"])
    after = db.query_one("SELECT * FROM automation_jobs WHERE id = ?", (job["id"],))
    assert (after["status"], after["attempts"], after["error_code"]) == (
        before["status"], before["attempts"], before["error_code"])
