"""Questions excluded from marking, and the mark-scheme context the review
screen shows for a discrepancy.

The exclusion must do three things at once, all from the same stored rows:
leave the question out of the marking prompt, out of the saved marks and total,
and out of the scheme audit. It must never touch the mark-scheme PDF, and
removing the exclusion must restore the question exactly.
"""

import json

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(fresh_state, monkeypatch):
    from app.config import get_config, save_config
    cfg = get_config()
    cfg.mock_mode = True
    save_config(cfg)
    from app.main import app
    with TestClient(app) as test_client:
        yield test_client


def _scheme(questions, total=None, *, name="cw-1"):
    """Install a mark scheme PDF + its cached blueprint for one assignment."""
    from app.config import data_dir
    from app.engine import blueprint as bp_mod

    folder = data_dir() / "mark_schemes"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{name}.pdf"
    path.write_bytes(b"%PDF-1.4 fake scheme for the test")
    sha = bp_mod.file_hash(path)
    blueprint = {
        "questions": questions,
        "total_marks": total if total is not None else
        sum(int(q.get("max_marks") or 0) for q in questions),
    }
    bp_mod.save_record(sha, blueprint, model="test")
    return blueprint


QUESTIONS = [
    {"question_number": "11", "max_marks": 2, "criteria": "B1 B1", "page": 3},
    {"question_number": "12", "max_marks": 2, "page": 4,
     "criteria": "M1 Any 3 terms correct, A1 cao 11p + 6",
     "printed_label": "Jan 17/3H/Q5 b"},
    {"question_number": "13", "max_marks": 3, "page": 5,
     "criteria": "M1 M1 A1 2x^2 + 19x + 24"},
]


# --------------------------------------------------------------- the engine

def test_an_excluded_question_leaves_the_blueprint_and_the_total():
    from app.engine import scheme_exclusions as se

    _scheme(QUESTIONS)
    se.add("cw-1", "12", "scheme answer is wrong")

    trimmed, removed = se.filter_blueprint({"questions": QUESTIONS,
                                            "total_marks": 7}, "cw-1")

    assert [q["question_number"] for q in trimmed["questions"]] == ["11", "13"]
    assert trimmed["total_marks"] == 5, "the excluded question's marks must leave the total"
    assert removed == [{"question_number": "12", "max_marks": 2,
                        "note": "scheme answer is wrong"}]


def test_an_excluded_part_only_lowers_that_question():
    from app.engine import scheme_exclusions as se

    parts = [{"question_number": "4", "max_marks": 5, "parts": [
        {"number": "4(a)", "max_marks": 2},
        {"number": "4(b)", "max_marks": 3},
    ]}]
    _scheme(parts, name="cw-parts")
    se.add("cw-parts", "4(b)")

    trimmed, removed = se.filter_blueprint({"questions": parts, "total_marks": 5},
                                           "cw-parts")

    assert [p["number"] for p in trimmed["questions"][0]["parts"]] == ["4(a)"]
    assert trimmed["questions"][0]["max_marks"] == 2
    assert trimmed["total_marks"] == 2
    assert [item["question_number"] for item in removed] == ["4(b)"]


def test_rows_for_the_excluded_question_are_dropped_from_a_result():
    from app.engine import scheme_exclusions as se

    result = {"questions": [
        {"question_number": "11", "earned": 2, "available": 2},
        {"question_number": "12", "earned": 1, "available": 2},
        {"question_number": "12(a)", "earned": 1, "available": 1},
        {"question_number": "13", "earned": 3, "available": 3},
    ]}

    kept = se.drop_questions(result, [{"question_number": "12"}])

    assert [q["question_number"] for q in kept["questions"]] == ["11", "13"]


def test_the_marking_prompt_no_longer_prints_the_question():
    from app.engine import scheme_exclusions as se
    from app.engine.blueprint import prompt_block

    _scheme(QUESTIONS)
    se.add("cw-1", "12")
    trimmed, _ = se.filter_blueprint({"questions": QUESTIONS, "total_marks": 7}, "cw-1")

    prompt = prompt_block(trimmed)

    assert "11p + 6" not in prompt, "the excluded criteria must not be sent"
    assert "19x" in prompt, "the remaining questions stay in the prompt"


def test_excluding_is_idempotent_and_reversible():
    from app.engine import scheme_exclusions as se

    _scheme(QUESTIONS)
    se.add("cw-1", "12", "first note")
    se.add("cw-1", "12", "second note")

    assert list(se.ignored("cw-1")) == ["12"]
    assert se.ignored("cw-1")["12"]["note"] == "second note"

    assert se.remove("cw-1", "12") is True
    assert se.ignored("cw-1") == {}
    assert se.remove("cw-1", "12") is False, "removing twice is not an error, it is a no-op"


def test_a_label_is_matched_however_it_is_typed():
    from app.engine import scheme_exclusions as se

    _scheme(QUESTIONS)
    se.add("cw-1", "12")

    assert se.find({"questions": QUESTIONS}, "Q12")["question_number"] == "12"
    assert se.remove("cw-1", "Q12") is True
    assert se.ignored("cw-1") == {}


# ----------------------------------------------------------------- the routes

def test_the_route_refuses_a_question_the_scheme_does_not_have(client):
    _scheme(QUESTIONS)

    response = client.post("/api/scheme/ignore",
                           json={"coursework_id": "cw-1", "question_number": "99"})

    assert response.status_code == 400
    assert "not in this mark scheme" in response.json()["message"]


def test_the_route_excludes_and_restores_a_question(client):
    _scheme(QUESTIONS)

    response = client.post("/api/scheme/ignore",
                           json={"coursework_id": "cw-1", "question_number": "12",
                                 "note": "printed paper does not match"})
    assert response.status_code == 200
    body = response.json()
    assert body["ignored"]["question_number"] == "12"
    assert body["marked_total"] == 5

    listing = client.get("/api/scheme/questions", params={"coursework_id": "cw-1"}).json()
    by_number = {q["question_number"]: q for q in listing["questions"]}
    assert by_number["12"]["ignored"] is True
    assert by_number["12"]["page"] == 4
    assert by_number["11"]["ignored"] is False
    assert listing["total_marks"] == 7
    assert listing["marked_total"] == 5

    restored = client.delete("/api/scheme/ignored/cw-1/12")
    assert restored.status_code == 200
    assert restored.json()["marked_total"] == 7


def test_a_discrepancy_carries_the_real_scheme_page_and_text(client):
    import time

    from app import db
    from app.engine import legacy_marking as legacy
    from app.engine import scheme_audit as sa

    _scheme(QUESTIONS)
    now = time.time()
    with db.transaction() as conn:
        conn.execute("INSERT OR IGNORE INTO courses(id,name,section,fetched_at) "
                     "VALUES('c1','Maths','A',?)", (now,))
        conn.execute("INSERT INTO coursework(id,course_id,title,max_points,fetched_at) "
                     "VALUES('cw-1','c1','Ch2L3 - Simplifying Expressions',103,?)", (now,))
        conn.execute(
            "INSERT INTO correction_jobs(id,coursework_id,course_id,student_id,"
            "student_name,status,created_at,updated_at) "
            "VALUES('job-1','cw-1','c1','s1','Joudy Ehab','COMPLETED',?,?)",
            (now, now))
        raw = {"paper_info": {}, "overall_notes": {}, "uncertainties": [],
               "questions": [{
                   "question_number": "12", "max_marks": 2, "awarded_marks": 1,
                   "student_answer": "24x - 9", "loss_reason": "differs",
                   "scheme_check": {
                       "disagrees_with_scheme": True,
                       "student_answer_valid": True,
                       "scheme_answer": "11p + 6",
                       "independently_verified_answer": "24x - 9",
                       "discrepancy_type": "incorrect_answer",
                       "evidence": "The printed worksheet asks a different question.",
                       "confidence": 0.95, "justified_marks": 1,
                   }}]}
        result = legacy.normalise_result(raw)
        earned, available, pct = legacy.calculate_totals(result)
        conn.execute(
            "INSERT INTO results(id,job_id,coursework_id,student_id,student_name,"
            "earned,available,percentage,result_json,created_at) "
            "VALUES('res-1','job-1','cw-1','s1','Joudy Ehab',?,?,?,?,?)",
            (earned, available, pct, json.dumps(result), now))
        sa.record_from_result(
            conn,
            job={"coursework_id": "cw-1", "course_id": "c1",
                 "student_id": "s1", "student_name": "Joudy Ehab",
                 "submission_id": "sub-1"},
            result_id="res-1", result=result, now_ts=now)

    items = client.get("/api/scheme/discrepancies").json()["items"]
    assert items, "the discrepancy should have been recorded"
    row = items[0]
    assert row["scheme_page"] == 4, "the list says which page the question is on"
    detail = client.get(f"/api/scheme/discrepancies/{row['id']}").json()

    assert detail["scheme"]["page"] == 4
    assert detail["scheme"]["page_image"].endswith("/mark-scheme/page/4.png")
    assert detail["scheme"]["criteria"].startswith("M1 Any 3 terms")
    assert detail["discrepancy"]["scheme_answer"] == "11p + 6"
    assert detail["discrepancy"]["verified_answer"] == "24x - 9"
