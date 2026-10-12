"""Worker bridge (§51/§54): OWNERSHIP + result sync hooks for the EXISTING
correction engine — with zero changes to the engine itself.

``record_ownership(result_id, job)`` is called from Result-saved paths where a
job row is available. It records who/what corrected the submission and, when
the job belongs to the distributed layer, publishes the §19 lightweight result
event. The existing engine remains the source of truth; this only adds the
distributed bookkeeping around it.
"""
from __future__ import annotations

import json
import os

from .. import db


def record_ownership(result_id: str, job: dict | None,
                     *, worker_id: str | None = None,
                     device_id: str | None = None,
                     account_id: str | None = None) -> dict | None:
    """Record who corrected a submission, for ANY save path, best-effort."""
    if not result_id:
        return None
    try:
        row = db.query_one(
            "SELECT earned, available, percentage, provisional_grade, "
            "double_check_status FROM results WHERE id = ?", (result_id,))
        if not row:
            return None
        from ..distributed import audit
        audit.record(
            action="correction.completed", target_type="result",
            target_id=result_id, actor_account=account_id,
            device_id=device_id,
            details={
                "worker_id": worker_id,
                "earned": row["earned"], "available": row["available"],
                "grade": row["provisional_grade"],
                "double_check_status": row["double_check_status"],
            })
        # Distributed event (§19) only when job has distributed metadata.
        if not job:
            return None
        coursework = job.get("coursework_id")
        mapping = None
        if coursework:
            from ..distributed import org
            mapping = org.get_assignment_mapping(coursework)
        payload = {
            "student_id": job.get("student_id"),
            "student_name": job.get("student_name"),
            "school_id": (mapping or {}).get("school_id"),
            "class_id": (mapping or {}).get("class_id"),
            "assignment_id": coursework,
            "submission_id": job.get("submission_id"),
            "submission_version": job.get("submission_version") or "v1",
            "raw_marks": row["earned"], "max_marks": row["available"],
            "percentage": row["percentage"],
            "final_grade": row["provisional_grade"],
            "correction_status": "COMPLETE",
            "double_check_status": row["double_check_status"],
            "worker_id": worker_id, "device_id": device_id,
            "account_id": account_id,
            "result_id": result_id,
        }
        from ..distributed import sync
        return sync.record_correction_result(
            payload, account_id=account_id, device_id=device_id,
            worker_id=worker_id)
    except Exception:  # noqa: BLE001 — bookkeeping must never break saving
        return None
