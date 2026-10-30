"""API for the teacher-verified marking benchmark.

Read endpoints serve the reference set and the latest recorded run. The only
write endpoint appends a teacher assessment to the benchmark overlay. There is
deliberately no endpoint that edits a result, a grade, a job or a retry budget.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import PlainTextResponse

from ..benchmark.dataset import (DEFAULT_DATASET, DatasetError, load_dataset)
from ..benchmark.reports import render_report
from ..benchmark.runner import (latest_run, list_runs, load_run, reproduce_run)
from ..benchmark.store import (ASSESSMENT_STATUSES, VerificationError,
                              VerificationStore, resolution_summary,
                              resolve_references)
from ..config import data_dir

router = APIRouter(prefix="/api/benchmark", tags=["benchmark"])
_RUN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_SECRET_KEY = re.compile(r"(key|token|secret|password|credential|authorization)",
                         re.IGNORECASE)


def register(app) -> None:
    app.include_router(router)


def _dataset_path() -> Path:
    override = os.environ.get("MC5_BENCHMARK_DATASET")
    return Path(override) if override else DEFAULT_DATASET


def _runs_root() -> Path:
    return data_dir() / "benchmark" / "runs"


def _load_dataset_or_409():
    try:
        return load_dataset(_dataset_path())
    except DatasetError as exc:
        raise HTTPException(status_code=409, detail={
            "code": "benchmark_dataset_invalid",
            "message": str(exc),
            "errors": exc.errors,
        }) from exc


def _store() -> VerificationStore:
    store = VerificationStore()
    try:
        # Force a read now so a corrupt overlay is a clear 409, not a 500 in
        # the middle of a resolution pass.
        store.assessments()
        return store
    except VerificationError as exc:
        raise HTTPException(status_code=409, detail={
            "code": "benchmark_verification_unreadable",
            "message": str(exc),
        }) from exc


def _run_path(run_id: str) -> Path:
    if not _RUN_ID.match(str(run_id or "")):
        raise HTTPException(status_code=400, detail="Invalid run id")
    root = _runs_root().resolve()
    candidate = (root / run_id / "run.json").resolve()
    if root not in candidate.parents or not candidate.is_file():
        raise HTTPException(status_code=404, detail="Benchmark run not found")
    return candidate


def _ai_index(run: dict | None) -> tuple[dict[str, dict], dict[str, str]]:
    if not run:
        return {}, {}
    metrics = run.get("metrics") or {}
    marks = {row["reference_id"]: row for row in metrics.get("rows") or []}
    missing = {
        row["reference_id"]: row.get("reason") or "missing"
        for row in (metrics.get("matching") or {}).get(
            "unmatched_reference_items") or []
    }
    return marks, missing


def _ai_public(row: dict | None, missing_reason: str | None,
               run: dict | None) -> dict | None:
    if not row:
        if missing_reason and run:
            return {
                "run_id": run.get("run_id"),
                "mark": None,
                "max": None,
                "difference": None,
                "direction": None,
                "message": missing_reason,
            }
        return None
    return {
        "run_id": run.get("run_id") if run else None,
        "mark": row.get("ai_mark"),
        "max": row.get("max_marks"),
        "teacher_mark_at_run": row.get("teacher_mark"),
        "difference": row.get("difference"),
        "direction": row.get("direction"),
        "matched_by": row.get("matched_by"),
        "review_required": row.get("review_required", False),
    }


def _current_dataset(run: dict | None, dataset) -> dict:
    sha = (run.get("dataset") or {}).get("sha256") if run else None
    version = (run.get("dataset") or {}).get("version") if run else None
    return {
        "matches": bool(run) and sha == dataset.sha256,
        "run_sha256": sha,
        "run_version": version,
        "current_sha256": dataset.sha256,
        "current_version": dataset.version,
    }


@router.get("/overview")
def overview():
    dataset = _load_dataset_or_409()
    store = _store()
    resolved = resolve_references(dataset, store)
    summary = resolution_summary(dataset, resolved)
    run = latest_run(_runs_root())
    dataset_match = _current_dataset(run, dataset)
    current_verification_sha = store.snapshot_sha256(dataset)
    run_verification_sha = ((run.get("verification") or {}).get("snapshot_sha256")
                            if run else None)
    verification_matches = bool(run and run_verification_sha
                                and run_verification_sha
                                == current_verification_sha)

    certified_available = bool(
        summary["eligible"] > 0 and run and run.get("certified")
        and run.get("status") == "complete" and dataset_match["matches"]
        and verification_matches)
    certified_accuracy = None
    if certified_available:
        certified_accuracy = {
            **(run.get("metrics") or {}).get("agreement", {}),
            "sample": (run.get("metrics") or {}).get("sample"),
            "run_id": run.get("run_id"),
            "finished_at": run.get("finished_at"),
            "verification_snapshot_sha256": run_verification_sha,
        }

    latest = None
    if run:
        metrics = run.get("metrics") or {}
        latest = {
            "run_id": run.get("run_id"),
            "status": run.get("status"),
            "certified": bool(run.get("certified")),
            "certification": run.get("certification"),
            "started_at": run.get("started_at"),
            "finished_at": run.get("finished_at"),
            "mode": run.get("mode"),
            "model": (run.get("configuration") or {}).get("model"),
            "dataset_version": dataset_match["run_version"],
            "dataset_matches": dataset_match["matches"],
            "verification_matches": verification_matches,
            "sample": metrics.get("sample"),
            "agreement": metrics.get("agreement"),
            "error": metrics.get("error"),
            "coverage": metrics.get("coverage"),
            "review": metrics.get("review"),
            "grades": metrics.get("grades"),
            "disagreements": metrics.get("disagreements") or [],
            "disagreement_groups": metrics.get("disagreement_groups") or {},
            "excluded_reasons": metrics.get("excluded_reasons") or {},
            "limitations": run.get("limitations") or [],
            "reproduction": run.get("reproduction"),
            "artifacts": run.get("artifacts"),
        }

    if not run:
        reason = ("No benchmark has been run. Certified accuracy is unavailable "
                  "until teacher-verified references exist and an evaluation "
                  "has completed.")
    elif not summary["eligible"]:
        reason = ("Certified accuracy is unavailable: no reference item has an "
                  "explicit teacher confirmation.")
    elif not dataset_match["matches"]:
        reason = ("The latest run used a different dataset version. Run a new "
                  "evaluation before treating its score as current.")
    elif run.get("certified") and not verification_matches:
        reason = ("Teacher assessments have changed since this run. Run a new "
                  "evaluation before treating its score as current.")
    elif not run.get("certified"):
        reason = str((run.get("certification") or {}).get("reason")
                     or "The latest run is not certified.")
    else:
        reason = None

    return {
        "dataset": dataset.descriptor(),
        "verification": summary,
        "certified_available": certified_available,
        "certified_accuracy": certified_accuracy,
        "certification_reason": reason,
        "latest_run": latest,
        "runs_root": str(_runs_root()),
        "definitions": (run.get("metrics") or {}).get("definitions")
        if run else None,
    }


@router.get("/references")
def references(status: str | None = None, partition: str | None = None,
               limit: int = 500):
    dataset = _load_dataset_or_409()
    store = _store()
    resolved = resolve_references(dataset, store)
    run = latest_run(_runs_root())
    ai_marks, missing = _ai_index(run)
    wanted = str(status or "").strip().lower()
    items = []
    for item in dataset.items:
        if partition and item.partition != partition:
            continue
        row = resolved[item.reference_id]
        if wanted and row.status != wanted:
            continue
        items.append({
            **item.as_public(),
            "reference": row.as_public(),
            "ai": _ai_public(ai_marks.get(item.reference_id),
                             missing.get(item.reference_id), run),
        })
        if len(items) >= max(1, min(1000, limit)):
            break
    return {
        "items": items,
        "total": len(items),
        "dataset_version": dataset.version,
        "dataset_sha256": dataset.sha256,
        "run_id": run.get("run_id") if run else None,
        "statuses": list(ASSESSMENT_STATUSES),
        "certified_available": bool(
            run and run.get("certified")
            and (run.get("dataset") or {}).get("sha256") == dataset.sha256
            and (run.get("verification") or {}).get("snapshot_sha256")
            == store.snapshot_sha256(dataset)),
    }


@router.get("/references/{reference_id}")
def reference_detail(reference_id: str):
    dataset = _load_dataset_or_409()
    store = _store()
    item = next((i for i in dataset.items if i.reference_id == reference_id), None)
    if item is None:
        raise HTTPException(status_code=404, detail="Reference item not found")
    resolved = resolve_references(dataset, store)
    run = latest_run(_runs_root())
    ai_marks, missing = _ai_index(run)
    return {
        **item.as_public(),
        "reference": resolved[reference_id].as_public(),
        "history": [a.as_public() for a in store.history(reference_id)],
        "ai": _ai_public(ai_marks.get(reference_id), missing.get(reference_id), run),
        "run_id": run.get("run_id") if run else None,
    }


@router.post("/references/{reference_id}/verify")
def verify_reference(reference_id: str, payload: dict):
    dataset = _load_dataset_or_409()
    item = next((i for i in dataset.items if i.reference_id == reference_id), None)
    if item is None:
        raise HTTPException(status_code=404, detail="Reference item not found")
    if payload.get("confirm") is not True:
        raise HTTPException(status_code=400, detail={
            "code": "confirmation_required",
            "message": ("A benchmark mark must be explicitly confirmed before it "
                        "can be recorded."),
        })
    status = str(payload.get("status") or "verified").strip().lower()
    if status not in ASSESSMENT_STATUSES:
        raise HTTPException(status_code=400, detail={
            "code": "bad_status",
            "message": f"status must be one of {list(ASSESSMENT_STATUSES)}",
        })
    awarded = payload.get("awarded")
    if awarded is not None:
        if isinstance(awarded, bool) or not isinstance(awarded, int):
            raise HTTPException(status_code=400, detail={
                "code": "invalid_mark",
                "message": "awarded must be an integer or null",
            })
        if awarded < 0 or awarded > item.max_marks:
            raise HTTPException(status_code=400, detail={
                "code": "mark_out_of_range",
                "message": f"awarded must be between 0 and {item.max_marks}",
            })
    if status == "verified" and awarded is None:
        raise HTTPException(status_code=400, detail={
            "code": "mark_required",
            "message": "a verified assessment requires an awarded mark",
        })
    if status in {"disputed", "incomplete"} and not str(payload.get("note") or "").strip():
        raise HTTPException(status_code=400, detail={
            "code": "note_required",
            "message": "a disputed or incomplete assessment needs a brief explanation",
        })
    alternatives = payload.get("alternative_methods") or []
    if isinstance(alternatives, str):
        alternatives = [alternatives]
    if not isinstance(alternatives, list):
        raise HTTPException(status_code=400, detail={
            "code": "invalid_alternatives",
            "message": "alternative_methods must be a list of strings",
        })
    try:
        store = _store()
        store.record(
            reference_id=reference_id,
            awarded=awarded,
            status=status,
            assessor=str(payload.get("assessor") or ""),
            role=str(payload.get("role") or ""),
            note=str(payload.get("note") or ""),
            alternative_methods=[str(v) for v in alternatives],
            dataset_version=dataset.version,
            confirmed=True,
        )
        resolved = resolve_references(dataset, store)
    except VerificationError as exc:
        raise HTTPException(status_code=400, detail={
            "code": "verification_rejected",
            "message": str(exc),
        }) from exc
    return {
        "reference_id": reference_id,
        "reference": resolved[reference_id].as_public(),
        "verification": resolution_summary(dataset, resolved),
        "production_db_modified": False,
        "classroom_writes": 0,
    }


@router.get("/runs")
def runs():
    dataset = _load_dataset_or_409()
    rows = list_runs(_runs_root())
    for row in rows:
        run = load_run(row["path"])
        row["current_dataset"] = bool(
            (run.get("dataset") or {}).get("sha256") == dataset.sha256)
    return {"runs": rows, "total": len(rows)}


@router.get("/runs/{run_id}")
def run_detail(run_id: str):
    run = load_run(_run_path(run_id))
    return run


@router.get("/runs/{run_id}/report", response_class=PlainTextResponse)
def run_report(run_id: str):
    run = load_run(_run_path(run_id))
    return PlainTextResponse(run.get("report_markdown")
                             or render_report(run))


@router.post("/runs/{run_id}/reproduce")
def reproduce(run_id: str):
    run = load_run(_run_path(run_id))
    result = reproduce_run(run)
    return result


@router.get("/report", response_class=PlainTextResponse)
def latest_report():
    run = latest_run(_runs_root())
    if not run:
        raise HTTPException(status_code=404, detail="No benchmark run recorded")
    return PlainTextResponse(run.get("report_markdown") or render_report(run))
