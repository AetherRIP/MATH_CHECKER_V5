"""The data the worker dashboard shows must be real backend state.

Three things had to exist for a card to be honest:

  * which worker is actually running the job and what paper/page count the
    teacher queued (the filename lived only in ``pending_manual_files``, which
    terminal cleanup deletes),
  * a stage history built from the transitions the backend itself reported, so
    "previous stages" is evidence rather than a reconstruction,
  * identical fields on the polled job list and on the SSE stream, otherwise a
    live card silently loses its worker or paper name.

No provider is contacted here: jobs are created and moved by the manager.
"""

from __future__ import annotations

import time

import pytest

import app.db as db
from app.jobs.manager import (
    JobManager, record_stage_event, stage_history,
)


def _manual(manager: JobManager, name: str = "Test 2", paper: str = "Paper 2.pdf",
            pages: int = 15) -> dict:
    return manager.enqueue(
        submission_id=None, coursework_id=None, course_id=None,
        student_id=None, student_name=name, paper_name=paper, page_count=pages)


# ------------------------------------------------------- paper metadata

def test_enqueue_records_the_paper_and_page_count(fresh_state):
    manager = JobManager()
    job = _manual(manager)

    stored = db.query_one(
        "SELECT paper_name, page_count FROM correction_jobs WHERE id = ?",
        (job["id"],))

    assert stored["paper_name"] == "Paper 2.pdf"
    assert stored["page_count"] == 15


def test_paper_metadata_is_optional(fresh_state):
    manager = JobManager()
    job = manager.enqueue(submission_id=None, coursework_id=None, course_id=None,
                          student_id=None, student_name="Test 3")
    stored = db.query_one(
        "SELECT paper_name, page_count FROM correction_jobs WHERE id = ?",
        (job["id"],))
    assert stored["paper_name"] is None
    assert stored["page_count"] is None


# --------------------------------------------------------- stage history

def test_enqueue_starts_the_history_with_queued(fresh_state):
    manager = JobManager()
    job = _manual(manager)

    history = stage_history(job["id"])

    assert [row["status"] for row in history] == ["QUEUED"]
    assert history[0]["stage"] == "Queued"


def test_every_reported_stage_is_recorded_with_its_time(fresh_state):
    manager = JobManager()
    job = _manual(manager)

    manager.report_progress(job["id"], status="PREPARING", stage="Preparing the PDFs")
    manager.report_progress(job["id"], stage="Compressing PDF",
                            progress=0.2)
    manager.report_progress(job["id"], status="MARKING",
                            stage="Reading the mark scheme")

    history = stage_history(job["id"])

    assert [row["stage"] for row in history] == [
        "Queued", "Preparing the PDFs", "Compressing PDF", "Reading the mark scheme"]
    assert all(row["at"] and row["at"] <= time.time() for row in history)
    assert history[2]["progress"] == pytest.approx(0.2)


def test_a_repeated_stage_is_not_recorded_twice(fresh_state):
    manager = JobManager()
    job = _manual(manager)

    manager.report_progress(job["id"], stage="Uploading the paper")
    manager.report_progress(job["id"], stage="Uploading the paper")

    assert [row["stage"] for row in stage_history(job["id"])].count(
        "Uploading the paper") == 1


def test_heartbeats_can_be_excluded_from_the_history(fresh_state):
    """The pool's elapsed-clock heartbeat rewrites the same stage text; those
    ticks must not fill the history the teacher reads."""
    manager = JobManager()
    job = _manual(manager)

    manager.report_progress(job["id"], stage="Marking with gemini-3.6-flash")
    manager.report_progress(job["id"], stage="Marking with gemini-3.6-flash · 12s",
                            record_event=False)

    assert [row["stage"] for row in stage_history(job["id"])] == [
        "Queued", "Marking with gemini-3.6-flash"]


def test_history_is_capped(fresh_state):
    manager = JobManager()
    job = _manual(manager)

    for index in range(80):
        record_stage_event(job["id"], status="MARKING",
                           stage=f"stage {index}", progress=None)

    assert len(stage_history(job["id"])) <= 61


def test_a_terminal_job_keeps_its_history(fresh_state):
    manager = JobManager()
    job = _manual(manager)
    manager.report_progress(job["id"], status="MARKING", stage="Marking the paper")
    manager._set_status(job["id"], "COMPLETED", finish=True)

    history = stage_history(job["id"])

    assert history[-1]["stage"] == "Marking the paper"
    assert len(history) >= 2


# ------------------------------------------------------ API payload shape

def test_the_public_job_carries_worker_paper_and_pages(fresh_state):
    from app.api.routes_all import _job_public

    manager = JobManager()
    job = _manual(manager)
    db.execute(
        "UPDATE correction_jobs SET worker_id = ?, status = ? WHERE id = ?",
        ("W3", "MARKING", job["id"]))
    row = db.row_to_dict(db.query_one(
        "SELECT * FROM correction_jobs WHERE id = ?", (job["id"],)))

    payload = _job_public(row)

    assert payload["worker_id"] == "W3"
    assert payload["paper_name"] == "Paper 2.pdf"
    assert payload["page_count"] == 15
    assert payload["updated_at"] == row["updated_at"]


def test_detail_endpoint_appends_the_real_terminal_state(fresh_state, monkeypatch):
    """The detail payload ends where the job ended, not at the last stage push."""
    from app.api import routes_all

    manager = JobManager()
    job = _manual(manager)
    manager.report_progress(job["id"], status="MARKING", stage="Marking the paper")
    finished = time.time()
    db.execute(
        "UPDATE correction_jobs SET status = 'FAILED', stage_detail = "
        "'All configured Gemini models failed', finished_at = ? WHERE id = ?",
        (finished, job["id"]))

    captured: dict = {}

    class _Service:
        def get_correction_status(self, job_id):
            return db.row_to_dict(db.query_one(
                "SELECT * FROM correction_jobs WHERE id = ?", (job_id,)))

    monkeypatch.setattr(routes_all, "_correction_service", lambda: _Service())

    detail = routes_all.job_detail(job["id"])
    captured["history"] = detail["stage_history"]

    assert captured["history"][-1]["status"] == "FAILED"
    assert captured["history"][-1]["at"] == pytest.approx(finished)
    assert captured["history"][-1]["stage"] == "All configured Gemini models failed"
    assert detail["paper_name"] == "Paper 2.pdf"
