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


def test_decisions_says_the_scheme_is_attached_but_unread(fresh_state):
    """A scheme that exists but has never been read is not a missing scheme.

    Reading a scheme happens while correcting the first paper, so opening the
    review page before that is normal. The endpoint used to answer "No mark
    scheme is attached to this assignment yet" — a 404 about a file that is
    sitting on disk — which sends the teacher hunting for an upload they already
    did. It now answers 409 and says what to do next.
    """
    from pathlib import Path

    from app.config import data_dir

    _seed_assignment()
    scheme = Path(data_dir()) / "mark_schemes" / "cw1.pdf"
    scheme.parent.mkdir(parents=True, exist_ok=True)
    scheme.write_bytes(b"%PDF-1.4\n% an uploaded scheme that was never read\n")

    with TestClient(app) as client:
        answer = client.get("/api/scheme/decisions", params={"coursework_id": "cw1"})

    assert answer.status_code == 409
    assert answer.json()["code"] == "scheme_not_read"
    assert "attached" in answer.json()["message"].lower()
    assert "read" in answer.json()["message"].lower()


def test_decisions_still_says_missing_when_nothing_was_uploaded(fresh_state):
    """The honest 404 has to survive: with no scheme at all the message is right."""
    _seed_assignment()

    with TestClient(app) as client:
        answer = client.get("/api/scheme/decisions", params={"coursework_id": "cw1"})

    assert answer.status_code == 404
    assert answer.json()["code"] == "not_found"
    assert "no mark scheme" in answer.json()["message"].lower()


def _scheme_pdf() -> "object":
    """An attached scheme on disk, with no structure read from it yet."""
    from pathlib import Path

    from app.config import data_dir

    scheme = Path(data_dir()) / "mark_schemes" / "cw1.pdf"
    scheme.parent.mkdir(parents=True, exist_ok=True)
    scheme.write_bytes(b"%PDF-1.4\n% a scheme nobody has read yet\n")
    return scheme


def test_reading_an_attached_scheme_makes_it_reviewable_before_any_correction(
        fresh_state, monkeypatch):
    """The review page exists to check questions BEFORE a paper is corrected.

    Reading used to happen only as a side effect of a correction, so a brand-new
    assignment could not be reviewed at all. Reading on demand fills that gap:
    the questions come back, the structure is stored, and the very next
    /decisions call can list them.
    """
    from app.engine import blueprint as bp

    _seed_assignment()
    _scheme_pdf()
    calls: list[str] = []

    def stub(client, path, **kwargs):
        calls.append(str(path))
        blueprint = {
            "questions": [
                {"question_number": "1", "max_marks": 3, "page": 1, "criteria": "M1 A1",
                 "parts": [{"number": "a", "max_marks": 1, "answers": ["24 - 18y"]},
                           {"number": "b", "max_marks": 2, "answers": ["y^2 + 8y - 20"]}]},
                {"question_number": "2", "max_marks": 1, "answers": ["10p - 15"]},
            ],
            "total_marks": 4,
        }
        bp.save_cached(bp.file_hash(path), blueprint, model="test")
        return blueprint, {"source": "built"}

    monkeypatch.setattr(bp, "get_blueprint", stub)

    with TestClient(app) as client:
        read = client.post("/api/scheme/read", params={"coursework_id": "cw1"})

        assert read.status_code == 200
        assert read.json()["read"] is True
        assert read.json()["questions"] == 2
        assert read.json()["total_marks"] == 4
        assert read.json()["cached"] is False

        # The point of reading: the review page can now list the questions.
        decisions = client.get("/api/scheme/decisions", params={"coursework_id": "cw1"})
        assert decisions.status_code == 200
        labels = [row["question_label"] for row in decisions.json()["questions"]]
        assert labels == ["1", "1(a)", "1(b)", "2"]

        # Reading again must not pay for a second extraction.
        again = client.post("/api/scheme/read", params={"coursework_id": "cw1"})
        assert again.json()["cached"] is True
        assert len(calls) == 1


def test_reading_with_no_scheme_says_there_is_none(fresh_state):
    _seed_assignment()

    with TestClient(app) as client:
        answer = client.post("/api/scheme/read", params={"coursework_id": "cw1"})

    assert answer.status_code == 404
    assert "no mark scheme" in answer.json()["message"].lower()




def test_demo_mode_builds_a_labelled_structure_to_review(fresh_state, monkeypatch):
    """Demo Mode has no model to read a scheme with. It must still make the
    review workflow usable — and say, on every row, that the structure is
    synthetic, so nothing here can be mistaken for the real scheme."""
    from app.config import get_config

    _seed_assignment()
    _scheme_pdf()
    monkeypatch.setattr(get_config(), "mock_mode", True)

    with TestClient(app) as client:
        read = client.post("/api/scheme/read", params={"coursework_id": "cw1"})
        assert read.status_code == 200
        assert read.json()["read"] is True
        assert read.json()["demo"] is True
        assert read.json()["questions"] >= 1

        decisions = client.get("/api/scheme/decisions", params={"coursework_id": "cw1"})
        assert decisions.status_code == 200
        body = decisions.json()
        assert body["scheme"]["demo"] is True
        assert body["questions"], "the review page has rows to show"

        # Every row is labelled, none is trusted — and every row carries the
        # ordinary proposal (mark automatically), so the demo practising the
        # workflow behaves like the workflow, not like a special case.
        for row in body["questions"]:
            assert row["extraction_status"] == "DEMO"
            assert row["reliable"] is False
            assert row["decision"] == "AUTO"
            assert row["decided"] is False, "a synthetic row is not a decision"
            # The placeholder announces itself in the first word, so it reads as
            # fake at a glance in a narrow table cell.
            assert row["effective_answer"].startswith("[demo]"), row["effective_answer"]
            assert "placeholder" in row["effective_answer"].lower()

        # The shape has to be worth practising on: a real paper does not give
        # every question the same marks in the same two-part form.
        labels = [row["question_label"] for row in body["questions"]]
        top = [row for row in body["questions"] if row["depth"] == 0]
        assert len({row["max_marks"] for row in top}) > 1, [r["max_marks"] for r in top]
        assert any("(ii)" in label for label in labels), labels
        for question in top:
            parts = [row for row in body["questions"]
                     if row["parent_key"] == question["question_key"]]
            if parts:
                # The page shows "x of y marks marked": a question's parts must
                # account for the question, or those two numbers disagree.
                assert sum(part["max_marks"] for part in parts) == question["max_marks"], \
                    question
        assert body["summary"]["total_marks"] == sum(row["max_marks"] for row in top)

        # A decision still works, which is the point of having the rows at all.
        first = body["questions"][0]["question_label"]
        decided = client.post("/api/scheme/decision", json={
            "coursework_id": "cw1", "question_number": first,
            "decision": "SKIP", "reason": "exploring the review workflow",
        })
        assert decided.status_code == 200
        assert decided.json()["decision"]["decision"] == "SKIP"
        skipped_row = next(row for row in decided.json()["questions"]
                           if row["question_label"] == first)
        assert skipped_row["skipped"] is True

        # A two-level label is a marking point of its own, so a decision on
        # 1(b)(ii) has to resolve to that row and not to its parent.
        deepest = next(label for label in labels if label.endswith("(ii)"))
        nested = client.post("/api/scheme/decision", json={
            "coursework_id": "cw1", "question_number": deepest,
            "decision": "MANUAL", "reason": "checking a nested part",
        })
        assert nested.status_code == 200, nested.json()
        nested_row = next(row for row in nested.json()["questions"]
                          if row["question_label"] == deepest)
        assert nested_row["decision"] == "MANUAL"
        assert nested_row["parent_key"] != nested_row["question_key"]

        # Reading again reuses the structure rather than building a new one.
        again = client.post("/api/scheme/read", params={"coursework_id": "cw1"})
        assert again.json()["cached"] is True
