"""Execute a benchmark and preserve enough evidence to reproduce it.

A run is not just a number. It records:

* the exact dataset file hash and version,
* a snapshot of every reference item and teacher mark that contributed,
* the correction configuration and model identity (secrets redacted),
* every raw per-paper outcome,
* the computed metrics and disagreements.

``reproduce_run`` recomputes the metrics from the snapshot + raw outcomes and
compares them byte-for-byte (canonical JSON). If the code changes the metric
definitions, old runs remain reproducible under their recorded version; a new
evaluation must be a new run rather than an edited report.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import subprocess
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from ..version import APP_VERSION, BUILD_NUMBER, SCHEMA_VERSION
from .dataset import (Paper, ReferenceDataset, ReferenceItem, canonical_json,
                      file_sha256)
from .metrics import compute_metrics
from .reports import render_report
from .store import ResolvedReference

RUN_SCHEMA_VERSION = 1
_RUN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_SECRET_KEY = re.compile(r"(key|token|secret|password|credential|authorization)",
                         re.IGNORECASE)

Corrector = Callable[[Paper, list[ReferenceItem]], dict]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _git_commit() -> str | None:
    try:
        completed = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=Path(__file__).resolve().parents[3],
            capture_output=True, text=True, timeout=5,
            encoding="utf-8", errors="replace",
        )
        if completed.returncode != 0:
            return None
        return completed.stdout.strip() or None
    except (OSError, subprocess.SubprocessError):
        return None


def redact_config(value: Any) -> Any:
    """Remove credential-shaped values before a config enters a report."""
    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            if _SECRET_KEY.search(str(key)):
                out[str(key)] = "<redacted>"
            else:
                out[str(key)] = redact_config(item)
        return out
    if isinstance(value, (list, tuple)):
        return [redact_config(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    temp.write_text(json.dumps(payload, indent=2, ensure_ascii=True) + "\n",
                    encoding="utf-8")
    os.replace(temp, path)


def _reference_snapshot(rows: list[ResolvedReference | tuple[ReferenceItem, int, str]],
                        dataset: ReferenceDataset | None = None) -> dict:
    items = []
    papers: dict[str, dict] = {}
    for row in rows:
        if isinstance(row, tuple):
            item, mark, basis = row
        else:
            item, mark, basis = row.item, int(row.teacher_mark or 0), "teacher_verified"
        paper = dataset.paper(item.paper_id) if dataset is not None else None
        papers[item.paper_id] = {
            "id": item.paper_id,
            "title": item.paper_title,
            "board": item.board,
            "qualification": item.qualification,
            "topic": item.topic,
            "partition": item.partition,
            "marks_available": paper.marks_available if paper else None,
            "expected_total": paper.expected_total if paper else None,
            "teacher_grade": paper.teacher_grade if paper else None,
            "scheme_stated_total": paper.scheme_stated_total if paper else None,
            "scheme_header_disagrees": (paper.scheme_header_disagrees
                                        if paper else False),
        }
        items.append({
            "reference_id": item.reference_id,
            "paper_id": item.paper_id,
            "question": item.question,
            "subquestion": item.subquestion,
            "level": item.level,
            "max_marks": item.max_marks,
            "scheme_mark": item.scheme_mark,
            "teacher_mark": int(mark),
            "mark_basis": basis,
            "categories": list(item.categories),
            "scheme": list(item.scheme),
            "prompt": item.prompt,
            "student_answer": list(item.student_answer),
            "ruling": item.ruling,
            "basis": item.basis,
            "provenance": item.provenance,
            "partition": item.partition,
            "board": item.board,
            "qualification": item.qualification,
            "topic": item.topic,
            "paper_title": item.paper_title,
        })
    return {"papers": list(papers.values()), "items": items}


def _dataset_from_snapshot(snapshot: dict) -> ReferenceDataset:
    """Rebuild the minimal dataset needed to recompute metrics."""
    papers_raw = snapshot.get("papers") or []
    items = snapshot.get("items") or []
    by_paper: dict[str, list[ReferenceItem]] = {}
    for row in items:
        paper_id = str(row["paper_id"])
        item = ReferenceItem(
            reference_id=str(row["reference_id"]),
            paper_id=paper_id,
            question=str(row["question"]),
            subquestion=row.get("subquestion"),
            max_marks=int(row["max_marks"]),
            scheme_mark=int(row.get("scheme_mark", 0)),
            prompt=str(row.get("prompt") or ""),
            scheme=tuple(row.get("scheme") or ()),
            student_answer=tuple(row.get("student_answer") or ()),
            categories=tuple(row.get("categories") or ()),
            ruling=str(row.get("ruling") or ""),
            basis=str(row.get("basis") or ""),
            provenance=str(row.get("provenance") or "official_reference"),
            partition=str(row.get("partition") or "development"),
            board=str(row.get("board") or "Not recorded"),
            qualification=str(row.get("qualification") or "Not recorded"),
            topic=str(row.get("topic") or "Not recorded"),
            paper_title=str(row.get("paper_title") or paper_id),
            student_pdf=None,
            scheme_pdf=None,
        )
        by_paper.setdefault(paper_id, []).append(item)

    papers = []
    for meta in papers_raw:
        paper_id = str(meta["id"])
        rows = by_paper.get(paper_id, [])
        papers.append(Paper(
            id=paper_id,
            title=str(meta.get("title") or paper_id),
            board=str(meta.get("board") or "Not recorded"),
            qualification=str(meta.get("qualification") or "Not recorded"),
            topic=str(meta.get("topic") or "Not recorded"),
            partition=str(meta.get("partition") or "development"),
            provenance="official_reference",
            lesson_id=meta.get("lesson_id"),
            marks_available=sum(i.max_marks for i in rows),
            expected_total=sum(i.scheme_mark for i in rows),
            scheme_stated_total=meta.get("scheme_stated_total"),
            scheme_header_disagrees=bool(meta.get("scheme_header_disagrees")),
            teacher_grade=(str(meta["teacher_grade"])
                           if meta.get("teacher_grade") else None),
            student_pdf=None,
            scheme_pdf=None,
            items=tuple(rows),
        ))
    return ReferenceDataset(
        path=Path("<recorded-run-snapshot>"),
        schema_version=RUN_SCHEMA_VERSION,
        version=str(snapshot.get("dataset_version") or "snapshot"),
        sha256=str(snapshot.get("dataset_sha256") or ""),
        papers=tuple(papers),
        warnings=tuple(snapshot.get("dataset_warnings") or ()),
        review={},
    )


def _resolved_from_snapshot(snapshot: dict,
                            dataset: ReferenceDataset
                            ) -> dict[str, ResolvedReference]:
    by_id = {item.reference_id: item for item in dataset.items}
    resolved: dict[str, ResolvedReference] = {}
    for row in snapshot.get("items") or []:
        item = by_id.get(str(row["reference_id"]))
        if item is None:
            continue
        resolved[item.reference_id] = ResolvedReference(
            item=item,
            status=("verified" if row.get("mark_basis") == "teacher_verified"
                    else "unverified"),
            teacher_mark=int(row["teacher_mark"]),
            reason=str(row.get("mark_basis") or ""),
            assessments=(),
        )
    return resolved


def reproduce_run(run: dict) -> dict:
    """Recompute a recorded run's metrics from its snapshot and raw outcomes.

    No provider request is made and no live verification store is consulted,
    so adding a later teacher assessment cannot silently change an old report.
    """
    snapshot = run.get("reference_snapshot") or {}
    if not snapshot.get("items"):
        return {"reproduced": False,
                "reason": "run has no reference snapshot",
                "differences": ["missing reference_snapshot.items"]}
    dataset = _dataset_from_snapshot(snapshot)
    resolved = _resolved_from_snapshot(snapshot, dataset)
    include_unverified = bool(run.get("certification", {}).get("basis")
                              == "scheme_reference_not_certified")
    outcomes = {row["paper_id"]: row for row in run.get("outcomes") or []}
    recomputed = compute_metrics(
        dataset, resolved, outcomes,
        partition=str(run.get("partition") or "all"),
        include_unverified=include_unverified,
    )
    recorded = run.get("metrics") or {}
    differences = []
    if canonical_json(recomputed) != canonical_json(recorded):
        # Locate the top-level section that differs; a full diff is stored in
        # the run artifact rather than spammed into the API response.
        for key in sorted(set(recomputed) | set(recorded)):
            if canonical_json(recomputed.get(key)) != canonical_json(recorded.get(key)):
                differences.append(key)
    return {
        "run_id": run.get("run_id"),
        "reproduced": not differences,
        "differences": differences,
        "checked_at": _now(),
        "metrics_sha256": hashlib.sha256(
            canonical_json(recomputed).encode("utf-8")).hexdigest(),
    }


def _limitations(run: dict, metrics: dict, *, include_unverified: bool,
                 dataset: ReferenceDataset) -> list[str]:
    notes: list[str] = []
    sample = metrics["sample"]
    if include_unverified:
        notes.append(
            "This run used scheme-derived references, not teacher-confirmed "
            "marks. It is a measurement of the marker against the printed "
            "scheme, NOT a teacher-certified accuracy figure.")
    if sample["references_expected"] < 30:
        notes.append(
            f"Small sample: {sample['references_expected']} references across "
            f"{sample['papers_expected']} scripts. Confidence intervals are wide; "
            "the result describes this dataset, not every future paper.")
    if dataset.partitions().get("holdout", 0) == 0:
        notes.append(
            "No holdout partition is declared, so no independent generalisation "
            "estimate is available.")
    if run.get("status") != "complete":
        notes.append(
            "The run is incomplete. Metrics cover only the scripts that "
            "produced a valid outcome and must not be presented as final.")
    if metrics["grades"]["compared"] == 0:
        notes.append(
            "No teacher-verified grade was recorded, so grade agreement is "
            "unavailable.")
    verified = run.get("certification", {}).get("teacher_verified_references", 0)
    total = run.get("dataset", {}).get("item_count", 0)
    if verified and total and verified < total:
        notes.append(
            f"Only {verified} of {total} reference items are teacher-verified; "
            "the certified score covers that subset and its coverage is reported.")
    if any("nested-subquestion" in i.categories and not i.is_subquestion
           for i in dataset.items):
        notes.append(
            "At least one nested-subquestion item is stored at whole-question "
            "level; subquestion metrics do not cover that item.")
    if any(p.scheme_header_disagrees for p in dataset.papers):
        notes.append(
            "A mark-scheme header disagrees with its own item totals; per-item "
            "marks were used and the discrepancy is reported.")
    return notes


def execute_benchmark(dataset: ReferenceDataset,
                      resolved: dict[str, ResolvedReference],
                      corrector: Corrector,
                      *, partition: str = "all",
                      include_unverified: bool = False,
                      model: str = "not recorded",
                      prompt_version: str = "not recorded",
                      config: dict | None = None,
                      output_dir: str | Path | None = None,
                      run_id: str | None = None,
                      mode: str = "custom",
                      started_at: str | None = None,
                      verification_sha256: str | None = None) -> dict:
    """Run every eligible paper through ``corrector`` and record the result.

    ``corrector(paper, items)`` returns the same shape the live harness stores:
    ``questions``/``subquestions`` maps, ``total`` (or top-level earned/available),
    ``review_required`` and ``grade``. Raising, returning ``status='error'`` or
    omitting the marks all become visible incompleteness — never a silent pass.
    """
    run_id = run_id or (datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
                        + "-" + uuid.uuid4().hex[:6])
    if not _RUN_ID.match(run_id):
        raise ValueError("run_id may only contain letters, digits, '.', '_' and '-'")

    if include_unverified:
        selected = [item for item in dataset.items_for(partition)
                    if item.provenance != "ai_generated"]
        rows: list[ResolvedReference | tuple[ReferenceItem, int, str]] = [
            (item, item.scheme_mark, "scheme_reference") for item in selected]
        basis = "scheme_reference_not_certified"
    else:
        selected_rows = [r for r in resolved.values()
                         if r.eligible and (partition == "all"
                                            or r.item.partition == partition)]
        selected_rows.sort(key=lambda r: (r.item.paper_id, r.item.question))
        rows = selected_rows
        basis = "teacher_verified"

    snapshot = _reference_snapshot(rows, dataset)
    snapshot.update({
        "dataset_version": dataset.version,
        "dataset_sha256": dataset.sha256,
        "dataset_warnings": list(dataset.warnings),
        "partition": partition,
        "certification_basis": basis,
    })
    by_paper: dict[str, list[ReferenceItem]] = {}
    for row in rows:
        item = row[0] if isinstance(row, tuple) else row.item
        by_paper.setdefault(item.paper_id, []).append(item)
    for values in by_paper.values():
        values.sort(key=lambda i: i.question)

    started = started_at or _now()
    outcomes: list[dict] = []
    failures: list[dict] = []

    if not by_paper:
        record = {
            "schema_version": RUN_SCHEMA_VERSION,
            "run_id": run_id,
            "status": "not_eligible",
            "certified": False,
            "certification": {
                "basis": basis,
                "eligible": False,
                "label": "Teacher-Verified Benchmark Accuracy (unavailable)",
                "reason": ("no teacher-verified references in this partition"
                           if not include_unverified
                           else "no reference items selected"),
                "teacher_verified_references": 0,
            },
            "started_at": started,
            "finished_at": _now(),
            "partition": partition,
            "mode": mode,
            "dataset": {
                "path": str(dataset.path),
                "version": dataset.version,
                "sha256": dataset.sha256,
                "item_count": len(dataset.items),
                "teacher_verified_references": 0,
                "coverage_of_dataset": 0.0,
            },
            "environment": {
                "app_version": APP_VERSION,
                "build_number": BUILD_NUMBER,
                "schema_version": SCHEMA_VERSION,
                "git_commit": _git_commit(),
                "python": platform.python_version(),
            },
            "configuration": {
                "model": model,
                "prompt_version": prompt_version,
                "mode": mode,
                "config": redact_config(config or {}),
                "config_sha256": hashlib.sha256(canonical_json(
                    redact_config(config or {})).encode("utf-8")).hexdigest(),
            },
            "verification": {
                "snapshot_sha256": verification_sha256,
                "assessments_used": 0,
            },
            "reference_snapshot": snapshot,
            "outcomes": [],
            "failures": failures,
            "metrics": compute_metrics(dataset, resolved, {},
                                       partition=partition,
                                       include_unverified=include_unverified),
            "limitations": [
                "No teacher-verified references were available; the certified "
                "score is unavailable rather than zero.",
                "No provider request was made because there was nothing "
                "eligible to measure against.",
            ],
        }
        record["report_markdown"] = render_report(record)
        if output_dir is not None:
            _save(record, Path(output_dir), outcomes)
        return record

    for paper_id in sorted(by_paper):
        paper = dataset.paper(paper_id)
        if paper is None:
            failures.append({"paper_id": paper_id, "error": "paper not in dataset"})
            continue
        try:
            outcome = corrector(paper, by_paper[paper_id])
            if not isinstance(outcome, dict):
                raise TypeError("corrector returned a non-object outcome")
            outcome = dict(outcome)
            outcome.setdefault("paper_id", paper_id)
            outcome.pop("paper_id", None)
            outcome["paper_id"] = paper_id
        except Exception as exc:  # noqa: BLE001 - preserve the failure, don't hide it
            outcome = {"paper_id": paper_id, "status": "error",
                       "error": f"{type(exc).__name__}: {exc}"}
        outcomes.append(outcome)
        if str(outcome.get("status") or "").lower() in {"error", "failed",
                                                        "timeout", "invalid"}:
            failures.append({"paper_id": paper_id,
                             "error": str(outcome.get("error") or
                                          outcome.get("status"))})

    metrics = compute_metrics(dataset, resolved, {o["paper_id"]: o for o in outcomes},
                              partition=partition,
                              include_unverified=include_unverified)
    complete = (not failures
                and metrics["coverage"]["missing_mark_count"] == 0
                and metrics["coverage"]["invalid_result_count"] == 0)
    certified = bool(not include_unverified and complete and rows)
    reason = ("all included references are teacher-verified and every item was "
              "matched" if certified else
              "the included references are scheme-derived, not teacher-verified"
              if include_unverified else
              "the run is incomplete or contains unmatched/invalid marks")
    record = {
        "schema_version": RUN_SCHEMA_VERSION,
        "run_id": run_id,
        "status": "complete" if complete else "incomplete",
        "certified": certified,
        "certification": {
            "basis": basis,
            "eligible": bool(rows),
            "label": ("Teacher-Verified Benchmark Accuracy" if certified
                      else "Benchmark result (not certified)"),
            "reason": reason,
            "teacher_verified_references": (0 if include_unverified
                                            else len(rows)),
        },
        "started_at": started,
        "finished_at": _now(),
        "partition": partition,
        "mode": mode,
        "dataset": {
            "path": str(dataset.path),
            "version": dataset.version,
            "sha256": dataset.sha256,
            "item_count": len(dataset.items),
            "teacher_verified_references": (0 if include_unverified else len(rows)),
            # Coverage here always means teacher-verified coverage, even on a
            # scheme-derived measurement run. A 100% "coverage" with zero
            # verified marks is exactly the kind of misleading number this
            # report exists to prevent.
            "coverage_of_dataset": (0.0 if include_unverified else
                                    round(len(rows) / len(dataset.items), 6)
                                    if dataset.items else 0.0),
            "warnings": list(dataset.warnings),
        },
        "environment": {
            "app_version": APP_VERSION,
            "build_number": BUILD_NUMBER,
            "schema_version": SCHEMA_VERSION,
            "git_commit": _git_commit(),
            "python": platform.python_version(),
        },
        "configuration": {
            "model": model,
            "prompt_version": prompt_version,
            "mode": mode,
            "config": redact_config(config or {}),
            "config_sha256": hashlib.sha256(canonical_json(
                redact_config(config or {})).encode("utf-8")).hexdigest(),
        },
        "verification": {
            "snapshot_sha256": verification_sha256,
            "assessments_used": (0 if include_unverified else len(rows)),
        },
        "reference_snapshot": snapshot,
        "outcomes": outcomes,
        "failures": failures,
        "metrics": metrics,
    }
    record["limitations"] = _limitations(record, metrics,
                                         include_unverified=include_unverified,
                                         dataset=dataset)
    record["reproduction"] = reproduce_run(record)
    record["report_markdown"] = render_report(record)
    if output_dir is not None:
        _save(record, Path(output_dir), outcomes)
    return record


def _save(record: dict, output_dir: Path, outcomes: list[dict]) -> None:
    run_dir = output_dir / record["run_id"]
    run_dir.mkdir(parents=True, exist_ok=True)
    raw_dir = run_dir / "raw"
    for outcome in outcomes:
        paper_id = str(outcome.get("paper_id") or "unknown")
        _write_json(raw_dir / f"{paper_id}.json", outcome)
    _write_json(run_dir / "run.json", record)
    (run_dir / "report.md").write_text(record["report_markdown"] + "\n",
                                       encoding="utf-8")
    record["artifacts"] = {
        "run_json": str(run_dir / "run.json"),
        "report_markdown": str(run_dir / "report.md"),
        "raw_outcomes": str(raw_dir),
    }
    # Persist the artifact paths too; the report body never depends on them.
    _write_json(run_dir / "run.json", record)


def load_run(path: str | Path) -> dict:
    target = Path(path)
    if target.is_dir():
        target = target / "run.json"
    return json.loads(target.read_text(encoding="utf-8"))


def list_runs(root: str | Path) -> list[dict]:
    base = Path(root)
    if not base.is_dir():
        return []
    rows = []
    for child in base.iterdir():
        run_file = child / "run.json"
        if not child.is_dir() or not run_file.is_file():
            continue
        try:
            record = load_run(run_file)
        except (OSError, ValueError):
            continue
        rows.append({
            "run_id": record.get("run_id"),
            "status": record.get("status"),
            "certified": record.get("certified"),
            "started_at": record.get("started_at"),
            "finished_at": record.get("finished_at"),
            "model": (record.get("configuration") or {}).get("model"),
            "dataset_version": (record.get("dataset") or {}).get("version"),
            "references": (record.get("metrics") or {}).get(
                "sample", {}).get("references_expected"),
            "path": str(run_file),
        })
    rows.sort(key=lambda r: str(r.get("started_at") or ""), reverse=True)
    return rows


def latest_run(root: str | Path) -> dict | None:
    rows = list_runs(root)
    return load_run(rows[0]["path"]) if rows else None


def dataset_fingerprint(path: str | Path) -> str | None:
    target = Path(path)
    return file_sha256(target) if target.is_file() else None
