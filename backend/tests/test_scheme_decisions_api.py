"""The Mark scheme review API: what the desktop page reads and writes.

These are contract tests. The desktop page has no other way to learn a
question's answer, state or decision, so the shape and the numbers here are
what the teacher ends up seeing. They run against the real routes with a real
(if synthetic) cached blueprint.
"""

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(fresh_state):
    from app.config import get_config, save_config
    cfg = get_config()
    cfg.mock_mode = True
    save_config(cfg)
    from app.main import app
    with TestClient(app) as test_client:
        yield test_client


SCHEME = [
    {"question_number": "1", "max_marks": 3, "page": 2,
     "parts": [
         {"number": "a", "max_marks": 1, "answers": ["24 - 18y"], "page": 2},
         {"number": "b", "max_marks": 2, "answers": ["y^2 + 8y - 20"], "page": 2},
     ]},
    {"question_number": "2", "max_marks": 1, "answers": ["10p - 15"], "page": 2},
    {"question_number": "3", "max_marks": 4, "criteria": "M1 A1 cao", "page": 3},
]


def _install(coursework_id="cw-api", questions=None):
    from app.config import data_dir
    from app.engine import blueprint as bp_mod

    questions = questions if questions is not None else SCHEME
    folder = data_dir() / "mark_schemes"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{coursework_id}.pdf"
    path.write_bytes(b"%PDF-1.4 fake scheme for the test")
    bp_mod.save_record(
        bp_mod.file_hash(path),
        {"questions": questions,
         "total_marks": sum(int(q.get("max_marks") or 0) for q in questions)},
        model="test")
    return coursework_id


def _decisions(client, cw):
    response = client.get("/api/scheme/decisions", params={"coursework_id": cw})
    assert response.status_code == 200, response.text
    return response.json()


def test_the_page_lists_every_question_with_answer_marks_and_state(client):
    cw = _install()
    body = _decisions(client, cw)

    labels = [(q["question_label"], q["depth"]) for q in body["questions"]]
    assert labels == [("1", 0), ("1(a)", 1), ("1(b)", 1), ("2", 0), ("3", 0)]

    part = next(q for q in body["questions"] if q["question_label"] == "1(a)")
    assert part["answers"] == ["24 - 18y"]
    assert part["max_marks"] == 1
    assert part["page"] == 2
    assert part["extraction_status"] == "RELIABLE"
    assert part["decision"] == "AUTO"
    assert part["decided"] is False, "a proposal is shown as not yet decided"

    missing = next(q for q in body["questions"] if q["question_label"] == "3")
    assert missing["extraction_status"] == "CRITERIA_ONLY"
    # Guidance without an answer is still proposed for automatic marking: the
    # independent check is the teacher's exception, and the row says what the
    # extraction could not do so they can choose it.
    assert missing["decision"] == "AUTO"
    assert missing["decided"] is False

    assert body["labels"]["SKIP"] == "Skip question"


def test_the_summary_counts_states_and_marks(client):
    cw = _install()
    stats = _decisions(client, cw)["summary"]

    assert stats["questions"] == 3
    assert stats["subquestions"] == 2
    assert stats["total"] == 5
    assert stats["unresolved"] == 5
    assert stats["total_marks"] == 8
    assert stats["marked_marks"] == 8
    assert stats["skipped_marks"] == 0


def test_deciding_a_question_updates_the_summary(client):
    cw = _install()
    response = client.post("/api/scheme/decision", json={
        "coursework_id": cw, "question_number": "1(a)", "decision": "MANUAL",
        "reason": "scheme answer looks wrong"})
    assert response.status_code == 200, response.text

    body = response.json()
    assert body["decision"]["decision"] == "MANUAL"
    assert body["summary"]["manual"] == 1          # 1(a); no other row was changed
    assert body["summary"]["automatic"] == 4
    assert body["summary"]["unresolved"] == 4

    stored = next(q for q in _decisions(client, cw)["questions"]
                  if q["question_label"] == "1(a)")
    assert stored["decided"] is True
    assert stored["decision"] == "MANUAL"


def test_skipping_a_subquestion_lowers_the_paper_total(client):
    cw = _install()
    body = client.post("/api/scheme/decision", json={
        "coursework_id": cw, "question_number": "1(b)", "decision": "SKIP"}).json()

    assert body["summary"]["skipped"] == 1
    assert body["summary"]["skipped_marks"] == 2
    assert body["summary"]["marked_marks"] == 6
    skipped = next(q for q in body["questions"] if q["question_label"] == "1(b)")
    assert skipped["skipped"] is True

    # The marking path already honours the exclusion table.
    from app.engine import scheme_exclusions as se
    trimmed, removed = se.filter_blueprint(
        {"questions": SCHEME, "total_marks": 8}, cw)
    assert trimmed["total_marks"] == 6
    assert [r["question_number"] for r in removed] == ["1(b)"]


def test_editing_an_answer_persists_and_is_shown_back(client):
    cw = _install()
    response = client.post("/api/scheme/answer", json={
        "coursework_id": cw, "question_number": "1(a)", "answer": "18y - 24"})
    assert response.status_code == 200, response.text

    row = next(q for q in _decisions(client, cw)["questions"]
               if q["question_label"] == "1(a)")
    assert row["answer_text"] == "18y - 24"
    assert row["effective_answer"] == "18y - 24"
    assert row["answers"] == ["24 - 18y"], "the extraction must stay intact"
    assert row["decided"] is False, "editing an answer is not a decision"


def test_clearing_the_edit_restores_the_extracted_answer(client):
    cw = _install()
    client.post("/api/scheme/answer", json={
        "coursework_id": cw, "question_number": "1(a)", "answer": "wrong"})
    client.post("/api/scheme/answer", json={
        "coursework_id": cw, "question_number": "1(a)", "answer": None})

    row = next(q for q in _decisions(client, cw)["questions"]
               if q["question_label"] == "1(a)")
    assert row["answer_text"] is None
    assert row["effective_answer"] == "24 - 18y"


def test_the_history_lists_each_change_with_both_sides(client):
    cw = _install()
    client.post("/api/scheme/decision", json={
        "coursework_id": cw, "question_number": "2", "decision": "MANUAL"})
    client.post("/api/scheme/decision", json={
        "coursework_id": cw, "question_number": "2", "decision": "SKIP",
        "reason": "not on the printed paper"})

    response = client.get("/api/scheme/decision-history",
                          params={"coursework_id": cw, "question_number": "2"})
    assert response.status_code == 200, response.text
    items = response.json()["items"]

    assert [(i["previous_decision"], i["new_decision"]) for i in items] == [
        ("MANUAL", "SKIP"), (None, "MANUAL")]
    assert items[0]["reason"] == "not on the printed paper"
    assert items[0]["question_label"] == "2"
    assert items[0]["source"] == "HUMAN"


def test_an_unknown_decision_is_a_400_not_a_silent_success(client):
    cw = _install()
    response = client.post("/api/scheme/decision", json={
        "coursework_id": cw, "question_number": "1", "decision": "MAYBE"})
    assert response.status_code == 400
    assert "Unknown decision" in response.text


def test_a_question_the_scheme_does_not_have_is_refused(client):
    cw = _install()
    response = client.post("/api/scheme/decision", json={
        "coursework_id": cw, "question_number": "77", "decision": "SKIP"})
    assert response.status_code == 400
    assert "not in this mark scheme" in response.text


def test_finalising_refuses_undecided_questions_until_acknowledged(client):
    cw = _install()
    refused = client.post("/api/scheme/finalize", json={"coursework_id": cw})
    assert refused.status_code == 400
    assert "no decision" in refused.text

    accepted = client.post("/api/scheme/finalize", json={
        "coursework_id": cw, "acknowledge_unresolved": True,
        "note": "checked the scheme by hand"})
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["acknowledged_unresolved"] == 5


def test_an_assignment_with_no_scheme_says_so_instead_of_inventing_one(client):
    response = client.get("/api/scheme/decisions", params={"coursework_id": "nope"})
    assert response.status_code == 404
    assert "No mark scheme" in response.text


def test_a_reparsed_scheme_reports_its_orphaned_decision(client):
    cw = _install()
    client.post("/api/scheme/decision", json={
        "coursework_id": cw, "question_number": "1(b)", "decision": "SKIP"})

    # Re-upload the same assignment against a scheme whose part b is gone.
    _install(cw, questions=[
        {"question_number": "1", "max_marks": 1, "page": 2,
         "parts": [{"number": "a", "max_marks": 1, "answers": ["24 - 18y"]}]},
    ])
    body = _decisions(client, cw)

    assert [o["question_key"] for o in body["orphaned"]] == ["1b"]
    assert all(q["question_key"] != "1b" for q in body["questions"])
