"""Teacher-facing API for mark-scheme discrepancy review + bulk approval.

Proves the wired-in workflow, not just the engine: staging a proposal changes
nothing, approval requires an explicit confirmation, rejection is retained, and
an already-applied proposal cannot be applied twice.
"""

from __future__ import annotations

import json
import time

from fastapi.testclient import TestClient

from app import db
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


def test_discrepancy_list_and_detail(fresh_state):
    _seed_assignment()
    _seed_result("r1", "s1", "Alice")
    with TestClient(app) as client:
        listing = client.get("/api/scheme/discrepancies").json()
        assert listing["total"] == 1
        row = listing["items"][0]
        assert row["state"] == sa.STATE_POSSIBLE
        detail = client.get(f"/api/scheme/discrepancies/{row['id']}").json()
        assert detail["discrepancy"]["scheme_answer"] == "x=5"


def test_staging_proposal_via_api_changes_nothing(fresh_state):
    _seed_assignment()
    _seed_result("r1", "s1", "Alice")
    _seed_result("r2", "s2", "Bob")
    with TestClient(app) as client:
        disc = client.get("/api/scheme/discrepancies").json()["items"][0]
        proposal = client.post(
            f"/api/scheme/discrepancies/{disc['id']}/propose").json()
        assert proposal["status"] == sa.PROPOSAL_PROPOSED
        assert proposal["affected_count"] == 2
        assert db.query_one("SELECT earned FROM results WHERE id='r1'")["earned"] == 0


def test_approval_requires_explicit_confirmation(fresh_state):
    _seed_assignment()
    _seed_result("r1", "s1", "Alice")
    with TestClient(app) as client:
        disc = client.get("/api/scheme/discrepancies").json()["items"][0]
        proposal = client.post(
            f"/api/scheme/discrepancies/{disc['id']}/propose").json()
        # No confirmation -> refused, grade unchanged.
        refused = client.post(f"/api/scheme/proposals/{proposal['id']}/approve",
                              json={})
        assert refused.status_code == 400
        assert db.query_one("SELECT earned FROM results WHERE id='r1'")["earned"] == 0
        # Explicit confirmation -> applied.
        ok = client.post(f"/api/scheme/proposals/{proposal['id']}/approve",
                         json={"confirm": True, "note": "checked"})
        assert ok.status_code == 200
        assert ok.json()["status"] == sa.PROPOSAL_APPLIED
        assert db.query_one("SELECT earned FROM results WHERE id='r1'")["earned"] == 3
        # Second approval does not double-apply.
        again = client.post(f"/api/scheme/proposals/{proposal['id']}/approve",
                            json={"confirm": True})
        assert again.json()["applied_count"] == 1
        assert len(db.query("SELECT * FROM result_versions WHERE result_id='r1'")) == 2


def test_rejection_via_api_keeps_grades_and_record(fresh_state):
    _seed_assignment()
    _seed_result("r1", "s1", "Alice")
    with TestClient(app) as client:
        disc = client.get("/api/scheme/discrepancies").json()["items"][0]
        proposal = client.post(
            f"/api/scheme/discrepancies/{disc['id']}/propose").json()
        rejected = client.post(f"/api/scheme/proposals/{proposal['id']}/reject",
                               json={"note": "scheme was right"}).json()
        assert rejected["status"] == sa.PROPOSAL_REJECTED
        assert db.query_one("SELECT earned FROM results WHERE id='r1'")["earned"] == 0
        assert sa.get_discrepancy(disc["id"]) is not None
        # It can never be applied after a rejection.
        assert client.post(f"/api/scheme/proposals/{proposal['id']}/approve",
                           json={"confirm": True}).status_code == 400


def test_attention_feed_surfaces_open_discrepancy(fresh_state):
    _seed_assignment()
    _seed_result("r1", "s1", "Alice")
    with TestClient(app) as client:
        feed = client.get("/api/attention").json()
        types = [i["type"] for i in feed["items"]]
        assert "mark_scheme_discrepancy" in types
        assert feed["counts"]["mark_scheme_discrepancy"] == 1


def test_scheme_summary(fresh_state):
    _seed_assignment()
    _seed_result("r1", "s1", "Alice")
    with TestClient(app) as client:
        summary = client.get("/api/scheme/summary").json()
        assert summary["open_discrepancies"] == 1
        assert summary["confirmed"] == 0
        assert summary["awaiting_approval"] == 0


def test_decided_discrepancy_leaves_the_open_set(fresh_state):
    """A decided row must leave the OPEN list, which is what the review screen
    shows and counts.

    The unfiltered listing is the whole audit table, so a screen that asks for it
    keeps showing rows the teacher has already decided while the summary next to
    them reports nothing awaiting review. `review_status=AWAITING_REVIEW` is the
    open set, and it is the set `summary.open_discrepancies` counts.
    """
    _seed_assignment()
    _seed_result("r1", "s1", "Alice")
    with TestClient(app) as client:
        row = client.get("/api/scheme/discrepancies").json()["items"][0]
        assert client.get("/api/scheme/summary").json()["open_discrepancies"] == 1

        decided = client.post(
            f"/api/scheme/discrepancies/{row['id']}/decision",
            json={"decision": sa.DECISION_CONFIRM_SCHEME_WRONG}).json()["discrepancy"]
        assert decided["review_status"] == sa.REVIEW_COMPLETED
        assert decided["review_required"] == 0

        # The audit record survives; it is simply no longer open.
        assert client.get("/api/scheme/discrepancies").json()["total"] == 1
        open_only = client.get(
            "/api/scheme/discrepancies",
            params={"review_status": sa.REVIEW_AWAITING}).json()
        assert open_only["total"] == 0
        assert client.get("/api/scheme/summary").json()["open_discrepancies"] == 0


def test_asking_for_investigation_keeps_the_row_open(fresh_state):
    """Only a real decision closes a row. Asking for more investigation leaves it
    awaiting review, so it stays visible and stays in the open count."""
    _seed_assignment()
    _seed_result("r1", "s1", "Alice")
    with TestClient(app) as client:
        row = client.get("/api/scheme/discrepancies").json()["items"][0]
        client.post(f"/api/scheme/discrepancies/{row['id']}/decision",
                    json={"decision": sa.DECISION_REQUEST_INVESTIGATION})

        open_only = client.get("/api/scheme/discrepancies",
                               params={"review_status": sa.REVIEW_AWAITING}).json()
        assert [i["id"] for i in open_only["items"]] == [row["id"]]
        assert client.get("/api/scheme/summary").json()["open_discrepancies"] == 1
