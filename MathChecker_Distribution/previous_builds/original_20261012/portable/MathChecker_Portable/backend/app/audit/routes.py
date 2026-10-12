"""Accuracy-audit API — read-only, testing-only.

Endpoints
    GET /api/audit/summary            the recorded audit (assignment, runs,
                                      per-question comparison, defects,
                                      limitations, regression outcome)
    GET /api/audit/reports            the generated report artefacts available
                                      for internal preview
    GET /api/audit/reports/{name}     one report artefact (PDF), internal only

The report endpoint deliberately serves ONLY files that already live in the
audit/report directories, by exact name; it never proxies Drive, never mints a
public URL, and never touches a student's Classroom submission.
"""

from __future__ import annotations

import json
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse

from ..config import DATA_DIR
from ..testing_guard import state as testing_state


def _findings_path() -> Path:
    return Path(DATA_DIR) / "audit_findings.json"


def _read_findings() -> dict:
    path = _findings_path()
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _preview_dirs() -> list[Path]:
    """Directories whose files may be previewed by name."""
    return [Path(DATA_DIR) / "audit_reports", Path(DATA_DIR) / "audit_runs"]


def _large_pdf_cases() -> dict:
    """Replayed evidence from REAL long-document runs (offline artefacts).

    ``scripts/audit_large_pdf_replay.py`` re-runs the merge pipeline over a
    job's STORED batch results and writes one JSON artefact per job, so this
    section is a reading of real recorded evidence — never a live re-mark and
    never a published grade. No student name is part of the artefact.
    """
    runs = Path(DATA_DIR) / "audit_runs"
    cases: list[dict] = []
    if runs.is_dir():
        for path in sorted(runs.glob("large_pdf_replay_*.json")):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if not isinstance(data, dict):
                continue
            cases.append(_shape_large_pdf_case(data, path.name))
    # The runs with unmarked or incomplete pages come first: they are what the
    # teacher has to act on.
    cases.sort(key=lambda case: (
        -(case["corrected"].get("unresolved_marks") or 0),
        str(case.get("job_id") or ""),
    ))
    return {
        "available": bool(cases),
        "cases": cases,
        "note": ("Each case replays the REAL stored batch results through the "
                 "current merge pipeline exactly as finalization does. "
                 "Reported = what the run stored at the time; Corrected = the "
                 "marked-only accounting, with unmarked questions named "
                 "instead of scored as 0."),
        "message": ("" if cases else
                    "No long-document replay has been recorded yet. Run "
                    "scripts/audit_large_pdf_replay.py <job_id>."),
    }


def _shape_large_pdf_case(data: dict, artifact: str) -> dict:
    """Compact, privacy-conscious view of one replayed long-document job."""
    batches = []
    for entry in data.get("batches") or []:
        coverage = entry.get("coverage") or {}
        batches.append({
            "batch": entry.get("batch"),
            "pages": entry.get("pages"),
            "status": entry.get("status"),
            "attempts": entry.get("attempts"),
            "returned_questions": entry.get("returned_questions"),
            "returned_marks": entry.get("returned_marks"),
            "expected_marks": coverage.get("expected_marks"),
            "coverage_ratio": coverage.get("ratio"),
            "incomplete": bool(coverage.get("stub")),
        })
    missing = (data.get("blueprint_report") or {}).get("missing_questions") or []
    corrected = data.get("score_marked_only") or {}
    return {
        "job_id": data.get("job_id"),
        "artifact": artifact,
        "coursework_title": data.get("coursework_title"),
        "page_count": data.get("page_count"),
        "batch_count": data.get("batch_count"),
        "blueprint_questions": data.get("blueprint_questions"),
        "batches": batches,
        "thin_batches": [entry.get("batch")
                         for entry in (data.get("merge") or {}).get(
                             "thin_batches") or []],
        "reported": data.get("score_old_asis") or {},
        "corrected": {
            "earned": corrected.get("earned"),
            "available": corrected.get("available"),
            "percentage": corrected.get("percentage"),
            "paper_available": corrected.get("paper_available"),
            "unresolved_count": corrected.get("unresolved_count"),
            "unresolved_marks": corrected.get("unresolved_marks"),
            "unresolved_questions": corrected.get("unresolved_questions") or [],
            "score_basis": corrected.get("score_basis"),
        },
        "missing_after_reconciliation": missing,
        "retry_plan": _shape_retry_plan(data),
        "stored_result": data.get("stored_result"),
    }


def _shape_retry_plan(data: dict) -> dict:
    """What the chunked targeted retry does (and did) for this job's gap."""
    recovery = data.get("recovery") or {}
    return {
        "missing_count": len(recovery.get("missing_questions") or []),
        "chunk_size": recovery.get("chunk_size"),
        "max_chunks": recovery.get("max_chunks"),
        "chunks": recovery.get("chunks") or [],
        "chunks_planned": recovery.get("chunks_planned") or 0,
        "deferred_count": len(recovery.get("deferred") or []),
        "blocked_reason": recovery.get("blocked_reason"),
        "previous_gate_allowed": recovery.get("previous_single_round_allowed"),
        "previous_gate_reason": recovery.get("previous_single_round_reason"),
        "note": recovery.get("note"),
    }


def register(app: FastAPI) -> None:
    @app.get("/api/audit/summary")
    def audit_summary() -> dict:
        findings = _read_findings()
        current = testing_state()
        findings.setdefault("testing_only", {})
        findings["testing_only"]["active"] = current.only
        findings["testing_only"]["reason"] = current.reason
        findings["testing_only"]["available"] = bool(findings.get("assignment"))
        # Real long-document replay evidence (the Large PDF accuracy audit).
        findings["large_pdf"] = _large_pdf_cases()
        if not findings.get("available"):
            findings["available"] = findings["large_pdf"]["available"]
        if not findings.get("assignment") and not findings["large_pdf"]["available"]:
            findings["message"] = (
                "No accuracy audit has been recorded yet. Run "
                "scripts/audit_inventory.py, scripts/audit_run.py and "
                "scripts/audit_findings.py to produce one.")
        return findings

    @app.get("/api/audit/reports")
    def audit_reports() -> dict:
        items: list[dict] = []
        for directory in _preview_dirs():
            if not directory.exists():
                continue
            for path in sorted(directory.glob("*.pdf")):
                items.append({
                    "name": path.name,
                    "bytes": path.stat().st_size,
                    "kind": directory.name,
                })
        return {"reports": items, "count": len(items)}

    @app.get("/api/audit/reports/{name}")
    def audit_report(name: str) -> FileResponse:
        # Exact-name lookup inside the allowed directories only: no path
        # traversal, no globbing, no files outside the audit artefacts.
        if "/" in name or "\\" in name or name.startswith("."):
            raise HTTPException(status_code=400, detail="Invalid report name.")
        for directory in _preview_dirs():
            candidate = directory / name
            if candidate.exists() and candidate.is_file() and \
                    candidate.suffix.lower() == ".pdf":
                return FileResponse(str(candidate), media_type="application/pdf",
                                    filename=name)
        raise HTTPException(status_code=404, detail="Report artefact not found.")
