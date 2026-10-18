"""The REAL Classroom write-back path after a bulk approval.

These tests exercise the function the app actually uses (not a fake), with the
Classroom transport patched, so a signature mismatch between the engine and the
publish callback can never silently turn a successful return into a failure (or
vice versa).
"""

from __future__ import annotations

import json
import time

from fastapi.testclient import TestClient

from app import db
from app.classroom import managed as managed_mod
from app.engine import legacy_marking as legacy
from app.engine import scheme_audit as sa
from app.main import app


def _seed_assignment() -> None:
    now = time.time()
    with db.transaction() as conn:
        conn.execute("INSERT OR IGNORE INTO courses(id,name,section,fetched_at) "
                     "VALUES('c1','Maths','A',?)", (now,))
        conn.execute("INSERT INTO coursework(id,course_id,title,max_points,"
                     "fetched_at) VALUES('cw1','c1','Algebra HW',10,?)", (now,))


def _seed_result(result_id: str, submission_id: str, student: str) -> None:
    now = time.time()
    check = {"disagrees_with_scheme": True, "student_answer_valid": True,
             "scheme_answer": "x=5", "independently_verified_answer": "x=4",
             "discrepancy_type": "incorrect_answer",
             "evidence": "2x+3=11 => x=4", "confidence": 0.95,
             "justified_marks": 3}
    with db.transaction() as conn:
        conn.execute("INSERT INTO correction_jobs(id,submission_id,coursework_id,"
                     "course_id,student_id,student_name,status,created_at,updated_at)"
                     " VALUES(?,?,?,?,?,?,?,?,?)",
                     (f"job-{result_id}", submission_id, "cw1", "c1", submission_id,
                      student, "COMPLETED", now, now))
        raw = {"paper_info": {}, "overall_notes": {}, "uncertainties": [],
               "questions": [{"question_number": "12", "max_marks": 3,
                              "awarded_marks": 0, "student_answer": "x=4",
                              "loss_reason": "differs", "scheme_check": check}]}
        result = legacy.normalise_result(raw)
        earned, available, pct = legacy.calculate_totals(result)
        conn.execute(
            "INSERT INTO results(id,job_id,coursework_id,submission_id,student_id,"
            "student_name,earned,available,percentage,provisional_grade,result_json,"
            "created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (result_id, f"job-{result_id}", "cw1", submission_id, submission_id,
             student, earned, available, pct, legacy.percentage_band_grade(pct),
             json.dumps(result), now))
        sa.record_from_result(conn, job={"coursework_id": "cw1", "course_id": "c1",
                                         "submission_id": submission_id,
                                         "student_id": submission_id,
                                         "student_name": student},
                              result_id=result_id, result=result)


def _stage_and_approve(published: int = 1, failed: int = 0, verified: bool = True):
    """Approve a staged proposal with the Classroom transport patched.

    Returns (recorded_call, response_json).
    """
    _seed_assignment()
    _seed_result("r1", "s1", "Alice")
    _seed_result("r2", "s2", "Bob")
    recorded: dict = {}

    def fake_publish(coursework_id, course_id, operations):
        recorded["coursework_id"] = coursework_id
        recorded["course_id"] = course_id
        recorded["operations"] = operations
        return {"coursework_id": coursework_id,
                "results": [{"submission_id": op["submission_id"], "grade": op["grade"],
                             "status": "published" if i < published else "failed"}
                            for i, op in enumerate(operations)],
                "summary": {"published": published, "failed": failed,
                            "not_supported": 0, "requires_auth": 0,
                            "skipped": 0, "total": len(operations)}}

    from app.automation.runner import WorkflowRunner
    original_reader = WorkflowRunner._read_submission
    WorkflowRunner._read_submission = lambda self, job: {
        "assignedGrade": 9 if verified else 4, "state": "RETURNED" if verified else "TURNED_IN"}
    original = managed_mod.publish
    managed_mod.publish = fake_publish
    try:
        with TestClient(app) as client:
            disc = client.get("/api/scheme/discrepancies").json()["items"][0]
            proposal = client.post(
                f"/api/scheme/discrepancies/{disc['id']}/propose").json()
            resp = client.post(f"/api/scheme/proposals/{proposal['id']}/approve",
                               json={"confirm": True, "publish": True,
                                     "note": "verified"})
            assert resp.status_code == 200, resp.text
            body = resp.json()
    finally:
        managed_mod.publish = original
        WorkflowRunner._read_submission = original_reader
    return recorded, body


def test_real_publish_path_is_invoked_with_classroom_ids(fresh_state):
    recorded, body = _stage_and_approve(published=2)
    # The publish callback received the REAL Classroom identity and operations,
    # not a malformed argument that silently fails.
    assert recorded["coursework_id"] == "cw1"
    assert recorded["course_id"] == "c1"
    assert len(recorded["operations"]) == 2
    assert all(op["type"] == "final_grade" for op in recorded["operations"])
    assert all(op["submission_id"] for op in recorded["operations"])
    # Final Classroom grade follows the existing 1–9 boundary conversion.
    assert all(op["grade"] == 9 for op in recorded["operations"])
    # And the outcome is reported honestly as published.
    assert body["status"] == sa.PROPOSAL_APPLIED
    assert body["classroom_publish_state"] == "published"


def test_real_publish_partial_failure_is_not_reported_as_success(fresh_state):
    recorded, body = _stage_and_approve(published=1, failed=1)
    assert len(recorded["operations"]) == 2
    assert body["classroom_publish_state"] == "partial"
    # The local correction still applied (honest partial behaviour).
    assert db.query_one("SELECT earned FROM results WHERE id='r1'")["earned"] == 3


def test_classroom_readback_mismatch_is_reported_as_failure(fresh_state):
    recorded, body = _stage_and_approve(published=2, verified=False)
    assert body["classroom_publish_state"] == "failed"
    assert body["classroom_publish"]["failed"] == 2


def test_publish_state_mapping_never_invents_success():
    assert sa.classroom_publish_state(
        {"published": 2, "failed": 0, "total": 2}) == "published"
    assert sa.classroom_publish_state(
        {"published": 1, "failed": 1, "total": 2}) == "partial"
    assert sa.classroom_publish_state({"error": "boom"}) == "failed"
    assert sa.classroom_publish_state(None) == "failed"
    assert sa.classroom_publish_state(
        {"summary": {"not_supported": 1, "total": 1}}) == "not_supported"
    assert sa.classroom_publish_state(
        {"summary": {"requires_auth": 1, "total": 1}}) == "requires_auth"
    assert sa.classroom_publish_state(
        {"published": 0, "total": 0}) == "nothing_to_publish"
    # A raw provider report with a nested summary is understood too.
    assert sa.classroom_publish_state(
        {"summary": {"published": 3, "failed": 0, "total": 3}}) == "published"
