"""Regression tests for the teacher-certified marking benchmark.

The metrics are checked against hand-calculated fixtures, not against whatever
the implementation happens to return. The eligibility tests are the important
ones: a previous AI correction, a signed dataset header, or a large test count
must never be able to promote an unverified mark into the certified score.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import db
from app.api import benchmark_routes
from app.benchmark.dataset import DatasetError, load_dataset
from app.benchmark.metrics import compute_metrics, wilson_interval
from app.benchmark.runner import execute_benchmark, reproduce_run
from app.benchmark.store import (VerificationError, VerificationStore,
                                 resolve_references, resolution_summary)


def _item(question: str, maximum: int, mark: int, *, categories=None,
          subquestion=None) -> dict:
    return {
        "question": question,
        "subquestion": subquestion,
        "prompt": f"Question {question}",
        "max_marks": maximum,
        "expected": mark,
        "categories": categories or ["fully-correct"],
        "scheme": [f"M1: method for {question}", f"A1: answer for {question}"],
        "student_answer": [f"working for {question}"],
        "ruling": f"{mark} of {maximum}",
        "basis": "test fixture",
    }


def _paper(paper_id: str, items: list[dict], *, partition: str = "development",
           provenance: str = "official_reference") -> dict:
    return {
        "id": paper_id,
        "title": f"Paper {paper_id}",
        "board": "Test Board",
        "qualification": "Test Qualification",
        "topic": "Algebra",
        "partition": partition,
        "ground_truth": provenance,
        "marks_available": sum(i["max_marks"] for i in items),
        "expected_total": sum(i["expected"] for i in items),
        "items": items,
    }


def _write(tmp_path: Path, papers: list[dict], *, version: str = "test-1") -> Path:
    path = tmp_path / "dataset.json"
    path.write_text(json.dumps({
        "schema_version": 2,
        "dataset_version": version,
        "review": {"required": True, "verified_by": None},
        "papers": papers,
    }, indent=2), encoding="utf-8")
    return path


def _verify(store: VerificationStore, reference_id: str, mark: int, *,
            assessor: str = "Teacher A", status: str = "verified",
            version: str = "test-1") -> None:
    store.record(reference_id=reference_id, awarded=mark, status=status,
                 assessor=assessor, role="maths teacher", note="confirmed",
                 dataset_version=version, confirmed=True,
                 now="2026-01-01T00:00:00+00:00")


@pytest.fixture
def simple_dataset(tmp_path):
    path = _write(tmp_path, [_paper("p1", [
        _item("1", 4, 2, categories=["partial-credit"]),
        _item("2", 2, 1),
    ])])
    return load_dataset(path)


# --------------------------------------------------------------- dataset rules

def test_dataset_rejects_duplicate_reference_ids(tmp_path):
    first = _item("1", 2, 1)
    second = _item("2", 2, 1)
    first["reference_id"] = "same"
    second["reference_id"] = "same"
    path = _write(tmp_path, [_paper("p1", [first, second])])
    with pytest.raises(DatasetError, match="duplicate reference_id"):
        load_dataset(path)


def test_dataset_rejects_partition_split_within_a_paper(tmp_path):
    item = _item("1", 2, 1)
    item["partition"] = "holdout"
    path = _write(tmp_path, [_paper("p1", [item], partition="development")])
    with pytest.raises(DatasetError, match="cannot be split"):
        load_dataset(path)


def test_dataset_rejects_expected_mark_above_maximum(tmp_path):
    path = _write(tmp_path, [_paper("p1", [_item("1", 2, 3)])])
    with pytest.raises(DatasetError, match="between 0 and 2"):
        load_dataset(path)


def test_dataset_rejects_totals_that_do_not_match_items(tmp_path):
    paper = _paper("p1", [_item("1", 2, 1)])
    paper["expected_total"] = 9
    path = _write(tmp_path, [paper])
    with pytest.raises(DatasetError, match="expected_total"):
        load_dataset(path)


def test_empty_dataset_is_invalid(tmp_path):
    path = tmp_path / "empty.json"
    path.write_text(json.dumps({"dataset_version": "x", "papers": []}),
                    encoding="utf-8")
    with pytest.raises(DatasetError, match="papers must be a non-empty list"):
        load_dataset(path)


def test_dataset_warns_when_no_holdout_exists(simple_dataset):
    assert any(w.startswith("no_holdout_partition")
               for w in simple_dataset.warnings)


def test_global_signature_does_not_upgrade_individual_items(tmp_path):
    path = _write(tmp_path, [_paper("p1", [_item("1", 2, 1)])])
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["review"]["verified_by"] = "A. Teacher"
    payload["review"]["verified_on"] = "2026-10-29"
    path.write_text(json.dumps(payload), encoding="utf-8")
    dataset = load_dataset(path)
    resolved = resolve_references(
        dataset, VerificationStore(tmp_path / "verifications.json"))
    assert resolved["p1.q1"].status == "unverified"
    assert all(not row.eligible for row in resolved.values())


def test_shipped_reference_set_is_loaded_but_uncertified():
    dataset = load_dataset()
    assert len(dataset.items) == 16
    assert dataset.provenance_counts()["teacher_verified"] == 0
    assert any(w.startswith("review_unverified") for w in dataset.warnings)


# -------------------------------------------------------------- eligibility

def test_unverified_items_are_not_eligible(simple_dataset, tmp_path):
    store = VerificationStore(tmp_path / "verifications.json")
    resolved = resolve_references(simple_dataset, store)
    assert all(not row.eligible for row in resolved.values())
    summary = resolution_summary(simple_dataset, resolved)
    assert summary["eligible"] == 0
    assert summary["counts"] == {"unverified": 2}


def test_verification_requires_explicit_confirmation(tmp_path):
    store = VerificationStore(tmp_path / "verifications.json")
    with pytest.raises(VerificationError, match="explicit confirmation"):
        store.record(reference_id="p1.q1", awarded=2, status="verified",
                     assessor="Teacher A", confirmed=False)


def test_verified_item_becomes_eligible(simple_dataset, tmp_path):
    store = VerificationStore(tmp_path / "verifications.json")
    _verify(store, "p1.q1", 2)
    resolved = resolve_references(simple_dataset, store)
    assert resolved["p1.q1"].eligible
    assert resolved["p1.q1"].teacher_mark == 2
    assert not resolved["p1.q2"].eligible


def test_two_teachers_agreeing_remain_eligible(simple_dataset, tmp_path):
    store = VerificationStore(tmp_path / "verifications.json")
    _verify(store, "p1.q1", 2, assessor="Teacher A")
    _verify(store, "p1.q1", 2, assessor="Teacher B")
    resolved = resolve_references(simple_dataset, store)
    assert resolved["p1.q1"].eligible
    assert len(resolved["p1.q1"].assessments) == 2


def test_two_teachers_disagreeing_are_disputed_and_both_preserved(
        simple_dataset, tmp_path):
    store = VerificationStore(tmp_path / "verifications.json")
    _verify(store, "p1.q1", 2, assessor="Teacher A")
    _verify(store, "p1.q1", 3, assessor="Teacher B")
    resolved = resolve_references(simple_dataset, store)
    row = resolved["p1.q1"]
    assert not row.eligible
    assert row.status == "disputed"
    assert row.reason == "teachers_disagree"
    assert [a.awarded for a in row.assessments] == [2, 3]


def test_ai_generated_reference_never_becomes_eligible(tmp_path):
    path = _write(tmp_path, [_paper(
        "p1", [_item("1", 2, 1)], provenance="ai_generated")])
    dataset = load_dataset(path)
    store = VerificationStore(tmp_path / "verifications.json")
    _verify(store, "p1.q1", 1)
    row = resolve_references(dataset, store)["p1.q1"]
    assert not row.eligible
    assert row.reason == "provenance_not_teacher_verifiable"


def test_incomplete_assessment_blocks_eligibility(simple_dataset, tmp_path):
    store = VerificationStore(tmp_path / "verifications.json")
    _verify(store, "p1.q1", 2, status="incomplete")
    row = resolve_references(simple_dataset, store)["p1.q1"]
    assert not row.eligible
    assert row.status == "incomplete"


def test_later_assessment_revision_does_not_erase_history(
        simple_dataset, tmp_path):
    store = VerificationStore(tmp_path / "verifications.json")
    _verify(store, "p1.q1", 1)
    _verify(store, "p1.q1", 2)
    history = store.history("p1.q1")
    assert [a.awarded for a in history] == [1, 2]
    assert resolve_references(simple_dataset, store)["p1.q1"].teacher_mark == 2


# ----------------------------------------------------------------- metrics

def _metrics_fixture(tmp_path):
    path = _write(tmp_path, [_paper("p1", [
        _item("1", 4, 2, categories=["partial-credit"]),
        _item("2", 2, 1),
        _item("3a", 2, 2, subquestion="a", categories=["alternative-method"]),
        _item("3b", 2, 1, subquestion="b", categories=["method-and-accuracy"]),
    ])])
    dataset = load_dataset(path)
    store = VerificationStore(tmp_path / "verifications.json")
    for reference_id, mark in (("p1.q1", 2), ("p1.q2", 1),
                               ("p1.q3a", 2), ("p1.q3b", 1)):
        _verify(store, reference_id, mark)
    resolved = resolve_references(dataset, store)
    outcome = {
        "paper_id": "p1",
        "status": "ok",
        "questions": {
            "1": {"awarded": 3, "max": 4},
            "2": {"awarded": 1, "max": 2},
            "3": {"awarded": 1, "max": 4},
        },
        "subquestions": {
            "3a": {"awarded": 0, "max": 2},
            "3b": {"awarded": 1, "max": 2},
        },
        "earned": 5,
        "available": 10,
        "review_required": True,
    }
    return dataset, resolved, outcome


def test_metrics_are_hand_calculated(tmp_path):
    dataset, resolved, outcome = _metrics_fixture(tmp_path)
    metrics = compute_metrics(dataset, resolved, {"p1": outcome})
    assert metrics["agreement"]["items"] == {
        "exact": 2,
        "compared": 4,
        "rate": 0.5,
        "ci95": metrics["agreement"]["items"]["ci95"],
        "denominator": "matched reference items with a valid AI mark",
    }
    assert metrics["agreement"]["questions"]["exact"] == 1
    assert metrics["agreement"]["questions"]["compared"] == 2
    assert metrics["agreement"]["subquestions"]["exact"] == 1
    assert metrics["agreement"]["subquestions"]["compared"] == 2
    assert metrics["error"]["mean_absolute_error"] == 0.75
    assert metrics["error"]["normalized_mae"] == 0.3125
    assert metrics["error"]["overmarking"] == {
        "count": 1, "rate": 0.25, "mean_magnitude": 1.0}
    assert metrics["error"]["undermarking"] == {
        "count": 1, "rate": 0.25, "mean_magnitude": 2.0}
    assert metrics["agreement"]["total"]["exact"] == 0
    assert metrics["agreement"]["total"]["compared"] == 1
    assert metrics["coverage"]["missing_mark_count"] == 0
    assert metrics["review"]["flagged_scripts"] == 1
    assert metrics["review"]["flagged_with_disagreement"] == 1


def test_missing_question_is_reported_and_not_counted_as_agreement(tmp_path):
    dataset, resolved, outcome = _metrics_fixture(tmp_path)
    del outcome["questions"]["1"]
    metrics = compute_metrics(dataset, resolved, {"p1": outcome})
    assert metrics["coverage"]["missing_mark_count"] == 1
    assert metrics["agreement"]["items"]["compared"] == 3
    assert metrics["agreement"]["items"]["exact"] == 2
    assert metrics["agreement"]["items"]["rate"] == pytest.approx(2 / 3)
    assert any(row["reference_id"] == "p1.q1"
               for row in metrics["matching"]["unmatched_reference_items"])


def test_unmatched_model_label_is_reported(tmp_path):
    dataset, resolved, outcome = _metrics_fixture(tmp_path)
    outcome["questions"]["99"] = {"awarded": 1, "max": 1}
    metrics = compute_metrics(dataset, resolved, {"p1": outcome})
    assert {"paper_id": "p1", "label": "99"} in \
        metrics["matching"]["unmatched_model_labels"]


def test_mark_above_maximum_is_invalid_not_agreeing(tmp_path):
    dataset, resolved, outcome = _metrics_fixture(tmp_path)
    outcome["questions"]["1"]["awarded"] = 9
    metrics = compute_metrics(dataset, resolved, {"p1": outcome})
    assert metrics["coverage"]["invalid_result_count"] == 1
    assert metrics["agreement"]["items"]["compared"] == 3
    # An out-of-range mark is invalid, not missing: the two are separate
    # safety metrics and must not be conflated.
    assert metrics["coverage"]["missing_mark_count"] == 0


def test_total_is_unavailable_when_only_part_of_the_paper_is_verified(tmp_path):
    path = _write(tmp_path, [_paper("p1", [
        _item("1", 4, 2), _item("2", 2, 1)])])
    dataset = load_dataset(path)
    store = VerificationStore(tmp_path / "verifications.json")
    _verify(store, "p1.q1", 2)
    resolved = resolve_references(dataset, store)
    outcome = {"status": "ok",
               "questions": {"1": {"awarded": 2, "max": 4},
                             "2": {"awarded": 1, "max": 2}},
               "earned": 3, "available": 6}
    metrics = compute_metrics(dataset, resolved, {"p1": outcome})
    assert metrics["agreement"]["total"]["compared"] == 0
    assert metrics["agreement"]["total"]["rate"] is None


def test_excluded_reference_label_is_not_reported_as_an_invented_question(tmp_path):
    path = _write(tmp_path, [_paper("p1", [
        _item("1", 2, 1), _item("2", 2, 2)])])
    dataset = load_dataset(path)
    store = VerificationStore(tmp_path / "verifications.json")
    _verify(store, "p1.q1", 1)
    resolved = resolve_references(dataset, store)
    # Q2 is not part of this partially verified run, but the marker returning
    # it is normal, not an invented label.
    metrics = compute_metrics(dataset, resolved, {"p1": {
        "status": "ok",
        "questions": {"1": {"awarded": 1, "max": 2},
                      "2": {"awarded": 2, "max": 2}},
        "earned": 3, "available": 4,
    }})
    assert metrics["matching"]["unmatched_model_labels"] == []
    assert metrics["agreement"]["items"]["compared"] == 1
    assert metrics["agreement"]["total"]["compared"] == 0


def test_subquestion_mark_is_never_split_from_a_rollup(tmp_path):
    path = _write(tmp_path, [_paper("p1", [
        _item("1a", 1, 1, subquestion="a"),
        _item("1b", 1, 1, subquestion="b"),
    ])])
    dataset = load_dataset(path)
    store = VerificationStore(tmp_path / "verifications.json")
    _verify(store, "p1.q1a", 1)
    _verify(store, "p1.q1b", 1)
    resolved = resolve_references(dataset, store)
    metrics = compute_metrics(dataset, resolved, {"p1": {
        "status": "ok",
        "questions": {"1": {"awarded": 2, "max": 2}},
        "earned": 2, "available": 2,
    }})
    assert metrics["agreement"]["subquestions"]["compared"] == 0
    assert metrics["coverage"]["missing_mark_count"] == 2


def test_wilson_interval_is_correct_for_a_known_case():
    low, high = wilson_interval(8, 10)
    assert low == pytest.approx(0.4902, abs=1e-4)
    assert high == pytest.approx(0.9433, abs=1e-4)


def test_grade_agreement_only_when_teacher_grade_exists(tmp_path):
    paper = _paper("p1", [_item("1", 2, 1)])
    paper["teacher_grade"] = "7"
    path = _write(tmp_path, [paper])
    dataset = load_dataset(path)
    store = VerificationStore(tmp_path / "verifications.json")
    _verify(store, "p1.q1", 1)
    resolved = resolve_references(dataset, store)
    metrics = compute_metrics(dataset, resolved, {"p1": {
        "status": "ok", "questions": {"1": {"awarded": 1}},
        "earned": 1, "available": 2, "grade": "7"}})
    assert metrics["grades"]["available"] is True
    assert metrics["grades"]["rate"] == 1.0


# ------------------------------------------------------------------ runner

def _corrector(calls: list[str], *, mark_delta: int = 0):
    def correct(paper, items):
        calls.append(paper.id)
        marks = {item.question: {"awarded": max(
            0, min(item.max_marks, item.scheme_mark + mark_delta)),
            "max": item.max_marks} for item in items}
        return {"status": "ok", "questions": marks,
                "earned": sum(v["awarded"] for v in marks.values()),
                "available": sum(i.max_marks for i in items)}
    return correct


def test_runner_refuses_without_eligible_references_and_makes_no_calls(
        simple_dataset, tmp_path):
    store = VerificationStore(tmp_path / "verifications.json")
    resolved = resolve_references(simple_dataset, store)
    calls: list[str] = []
    record = execute_benchmark(simple_dataset, resolved, _corrector(calls),
                               run_id="no-refs", output_dir=tmp_path / "runs")
    assert record["status"] == "not_eligible"
    assert record["certified"] is False
    assert calls == []
    assert record["metrics"]["sample"]["references_expected"] == 0


def test_certified_run_records_versions_and_redacts_credentials(tmp_path):
    path = _write(tmp_path, [_paper("p1", [_item("1", 2, 1)])])
    dataset = load_dataset(path)
    store = VerificationStore(tmp_path / "verifications.json")
    _verify(store, "p1.q1", 1)
    resolved = resolve_references(dataset, store)
    record = execute_benchmark(
        dataset, resolved, _corrector([]), run_id="certified-run",
        model="test-model", prompt_version="prompt-v1",
        config={"gemini": {"api_key": "super-secret", "temperature": 0}},
        output_dir=tmp_path / "runs")
    assert record["certified"] is True
    assert record["dataset"]["sha256"] == dataset.sha256
    assert record["dataset"]["version"] == "test-1"
    assert record["configuration"]["config"]["gemini"]["api_key"] == "<redacted>"
    assert "super-secret" not in json.dumps(record)
    assert (tmp_path / "runs" / "certified-run" / "report.md").is_file()
    assert (tmp_path / "runs" / "certified-run" / "run.json").is_file()


def test_recorded_run_reproduces_its_metrics(tmp_path):
    path = _write(tmp_path, [_paper("p1", [_item("1", 2, 1)])])
    dataset = load_dataset(path)
    store = VerificationStore(tmp_path / "verifications.json")
    _verify(store, "p1.q1", 1)
    resolved = resolve_references(dataset, store)
    record = execute_benchmark(dataset, resolved, _corrector([]),
                               run_id="repro-run")
    assert record["reproduction"]["reproduced"] is True
    assert reproduce_run(record)["reproduced"] is True


def test_tampered_raw_outcome_fails_reproduction(tmp_path):
    path = _write(tmp_path, [_paper("p1", [_item("1", 2, 1)])])
    dataset = load_dataset(path)
    store = VerificationStore(tmp_path / "verifications.json")
    _verify(store, "p1.q1", 1)
    resolved = resolve_references(dataset, store)
    record = execute_benchmark(dataset, resolved, _corrector([]),
                               run_id="tamper-run")
    assert record["reproduction"]["reproduced"] is True
    record["outcomes"][0]["questions"]["1"]["awarded"] = 0
    replay = reproduce_run(record)
    assert replay["reproduced"] is False
    assert replay["differences"]
    assert "agreement" in replay["differences"]


def test_new_teacher_assessment_does_not_change_an_old_run(tmp_path):
    path = _write(tmp_path, [_paper("p1", [_item("1", 2, 1)])])
    dataset = load_dataset(path)
    store = VerificationStore(tmp_path / "verifications.json")
    _verify(store, "p1.q1", 1)
    resolved = resolve_references(dataset, store)
    record = execute_benchmark(dataset, resolved, _corrector([]),
                               run_id="frozen-run")
    _verify(store, "p1.q1", 0, assessor="Teacher B")
    resolved_after = resolve_references(dataset, store)
    assert resolved_after["p1.q1"].status == "disputed"
    assert reproduce_run(record)["reproduced"] is True


def test_incomplete_run_cannot_be_certified(tmp_path):
    path = _write(tmp_path, [_paper("p1", [_item("1", 2, 1)])])
    dataset = load_dataset(path)
    store = VerificationStore(tmp_path / "verifications.json")
    _verify(store, "p1.q1", 1)
    resolved = resolve_references(dataset, store)

    def failing(paper, items):
        raise RuntimeError("provider unavailable")

    record = execute_benchmark(dataset, resolved, failing, run_id="failed-run")
    assert record["status"] == "incomplete"
    assert record["certified"] is False
    assert record["metrics"]["coverage"]["scripts_failed"] == 1
    assert any("incomplete" in line for line in record["limitations"])


def test_partition_isolation(tmp_path):
    path = _write(tmp_path, [
        _paper("dev", [_item("1", 2, 1)], partition="development"),
        _paper("hold", [_item("1", 2, 1)], partition="holdout"),
    ])
    dataset = load_dataset(path)
    store = VerificationStore(tmp_path / "verifications.json")
    _verify(store, "dev.q1", 1)
    _verify(store, "hold.q1", 1)
    resolved = resolve_references(dataset, store)
    calls: list[str] = []
    record = execute_benchmark(dataset, resolved, _corrector(calls),
                               partition="development", run_id="dev-run")
    assert calls == ["dev"]
    assert record["metrics"]["sample"]["papers_attempted"] == 1
    assert record["certified"] is True


def test_scheme_reference_run_is_never_certified(tmp_path):
    path = _write(tmp_path, [_paper("p1", [_item("1", 2, 1)])])
    dataset = load_dataset(path)
    resolved = resolve_references(
        dataset, VerificationStore(tmp_path / "verifications.json"))
    record = execute_benchmark(dataset, resolved, _corrector([]),
                               include_unverified=True, run_id="scheme-run")
    assert record["status"] == "complete"
    assert record["certified"] is False
    assert record["certification"]["basis"] == "scheme_reference_not_certified"


# --------------------------------------------------------------------- API

@pytest.fixture
def api_client(tmp_path, monkeypatch):
    path = _write(tmp_path, [_paper("p1", [_item("1", 2, 1)])])
    monkeypatch.setenv("MC5_BENCHMARK_DATASET", str(path))
    app = FastAPI()
    benchmark_routes.register(app)
    return TestClient(app), path


def test_overview_reports_certified_accuracy_unavailable(api_client):
    client, _ = api_client
    response = client.get("/api/benchmark/overview")
    assert response.status_code == 200
    body = response.json()
    assert body["certified_available"] is False
    assert body["certified_accuracy"] is None
    assert body["verification"]["eligible"] == 0
    assert "unavailable" in body["certification_reason"].lower()


def test_verification_requires_confirmation_and_range(api_client):
    client, _ = api_client
    rejected = client.post("/api/benchmark/references/p1.q1/verify", json={
        "awarded": 1, "status": "verified", "assessor": "Teacher A",
        "confirm": False})
    assert rejected.status_code == 400
    out_of_range = client.post("/api/benchmark/references/p1.q1/verify", json={
        "awarded": 3, "status": "verified", "assessor": "Teacher A",
        "confirm": True})
    assert out_of_range.status_code == 400
    no_reason = client.post("/api/benchmark/references/p1.q1/verify", json={
        "awarded": 1, "status": "disputed", "assessor": "Teacher A",
        "confirm": True})
    assert no_reason.status_code == 400
    assert no_reason.json()["detail"]["code"] == "note_required"


def test_verification_workflow_writes_only_benchmark_overlay(api_client):
    client, _ = api_client
    before = db.query("SELECT COUNT(*) AS n FROM results")[0]["n"]
    response = client.post("/api/benchmark/references/p1.q1/verify", json={
        "awarded": 1, "status": "verified", "assessor": "Teacher A",
        "role": "maths teacher", "note": "scheme clause met",
        "alternative_methods": [], "confirm": True})
    assert response.status_code == 200
    body = response.json()
    assert body["reference"]["eligible"] is True
    assert body["production_db_modified"] is False
    assert body["classroom_writes"] == 0
    after = db.query("SELECT COUNT(*) AS n FROM results")[0]["n"]
    assert after == before

    detail = client.get("/api/benchmark/references/p1.q1").json()
    assert detail["reference"]["status"] == "verified"
    assert detail["reference"]["teacher_mark"] == 1
    assert len(detail["history"]) == 1

    second = client.post("/api/benchmark/references/p1.q1/verify", json={
        "awarded": 0, "status": "verified", "assessor": "Teacher B",
        "confirm": True})
    assert second.status_code == 200
    assert second.json()["reference"]["status"] == "disputed"
    history = client.get("/api/benchmark/references/p1.q1").json()["history"]
    assert [row["awarded"] for row in history] == [1, 0]


def test_changed_assessment_invalidates_a_certified_run(api_client):
    client, path = api_client
    client.post("/api/benchmark/references/p1.q1/verify", json={
        "awarded": 1, "status": "verified", "assessor": "Teacher A",
        "confirm": True})
    from app.benchmark.runner import execute_benchmark
    from app.config import data_dir

    dataset = load_dataset(path)
    store = VerificationStore()
    resolved = resolve_references(dataset, store)
    execute_benchmark(
        dataset, resolved,
        lambda paper, items: {
            "status": "ok",
            "questions": {i.question: {"awarded": i.scheme_mark,
                                       "max": i.max_marks} for i in items},
            "earned": sum(i.scheme_mark for i in items),
            "available": sum(i.max_marks for i in items),
        },
        run_id="api-certified",
        output_dir=data_dir() / "benchmark" / "runs",
        verification_sha256=store.snapshot_sha256(dataset),
    )
    body = client.get("/api/benchmark/overview").json()
    assert body["certified_available"] is True

    client.post("/api/benchmark/references/p1.q1/verify", json={
        "awarded": 0, "status": "verified", "assessor": "Teacher B",
        "confirm": True})
    body = client.get("/api/benchmark/overview").json()
    assert body["certified_available"] is False
    assert body["latest_run"]["dataset_matches"] is True
    assert body["certification_reason"]


def test_unknown_reference_is_404(api_client):
    client, _ = api_client
    assert client.get("/api/benchmark/references/nope").status_code == 404
    assert client.post("/api/benchmark/references/nope/verify", json={
        "awarded": 0, "status": "verified", "assessor": "T", "confirm": True,
    }).status_code == 404
