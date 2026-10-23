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


def register(app: FastAPI) -> None:
    @app.get("/api/audit/summary")
    def audit_summary() -> dict:
        findings = _read_findings()
        current = testing_state()
        findings.setdefault("testing_only", {})
        findings["testing_only"]["active"] = current.only
        findings["testing_only"]["reason"] = current.reason
        findings["testing_only"]["available"] = bool(findings.get("assignment"))
        if not findings.get("assignment"):
            findings["message"] = (
                "No accuracy audit has been recorded yet. Run "
                "scripts/audit_inventory.py to produce one.")
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
