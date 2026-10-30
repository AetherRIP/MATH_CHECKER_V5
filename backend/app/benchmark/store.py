"""Append-only teacher verification records for the reference set.

Why a separate store rather than columns on ``results``
-------------------------------------------------------
A benchmark assessment is not a student result. Writing it into ``results``
would change a grade, wake the Classroom automation, or be overwritten by a
later correction. The overlay therefore lives under the application data
directory and references the synthetic ``reference_id`` only. It is:

* append-only — a revision adds a row, no previous judgement is destroyed;
* multi-assessor — two teachers can both sign the same reference, and a
  disagreement is preserved rather than averaged or resolved toward the AI;
* explicit — a record cannot be written without ``confirmed=True``.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .dataset import (GROUND_TRUTH_CLASSES, ReferenceDataset, ReferenceItem,
                      canonical_json, items_by_id)

ASSESSMENT_STATUSES = ("verified", "disputed", "incomplete")
STORE_SCHEMA_VERSION = 1
_WRITE_LOCK = threading.Lock()


class VerificationError(ValueError):
    """The verification overlay is unreadable or a record is invalid."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def default_store_path() -> Path:
    # Imported lazily so tests and MC5_DATA_DIR overrides bind to the current
    # data directory rather than the one that existed at import time.
    from ..config import data_dir

    return data_dir() / "benchmark" / "verifications.json"


@dataclass(frozen=True)
class Assessment:
    seq: int
    reference_id: str
    awarded: int | None
    status: str
    assessor: str
    role: str
    note: str
    alternative_methods: tuple[str, ...]
    created_at: str
    dataset_version: str

    def as_public(self) -> dict:
        return {
            "seq": self.seq,
            "reference_id": self.reference_id,
            "awarded": self.awarded,
            "status": self.status,
            "assessor": self.assessor,
            "role": self.role,
            "note": self.note,
            "alternative_methods": list(self.alternative_methods),
            "created_at": self.created_at,
            "dataset_version": self.dataset_version,
        }


@dataclass(frozen=True)
class ResolvedReference:
    item: ReferenceItem
    status: str
    teacher_mark: int | None
    reason: str
    assessments: tuple[Assessment, ...]

    @property
    def eligible(self) -> bool:
        return self.status == "verified" and self.teacher_mark is not None

    def as_public(self) -> dict:
        return {
            "reference_id": self.item.reference_id,
            "status": self.status,
            "teacher_mark": self.teacher_mark,
            "reason": self.reason,
            "eligible": self.eligible,
            "assessments": [a.as_public() for a in self.assessments],
        }


class VerificationStore:
    """A tiny JSON document with atomic, append-only updates."""

    def __init__(self, path: str | Path | None = None):
        self.path = Path(path) if path is not None else default_store_path()

    # -- io -----------------------------------------------------------------
    def _read(self) -> dict:
        if not self.path.is_file():
            return {"schema_version": STORE_SCHEMA_VERSION, "assessments": []}
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            # Never start from an empty overlay: that would silently discard
            # every teacher decision and make the certified score look
            # unavailable when it is actually corrupt.
            raise VerificationError(
                f"cannot read teacher verification store {self.path}: {exc}"
            ) from exc
        if not isinstance(payload, dict):
            raise VerificationError("verification store must be a JSON object")
        rows = payload.get("assessments")
        if not isinstance(rows, list):
            raise VerificationError("verification store has no assessments list")
        return payload

    def _write(self, payload: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_name(self.path.name + ".tmp")
        temp.write_text(json.dumps(payload, indent=2, ensure_ascii=True) + "\n",
                        encoding="utf-8")
        os.replace(temp, self.path)

    def assessments(self) -> list[Assessment]:
        rows = self._read()["assessments"]
        out: list[Assessment] = []
        for index, row in enumerate(rows):
            if not isinstance(row, dict):
                raise VerificationError(f"assessment #{index} is not an object")
            try:
                status = str(row["status"]).lower()
                if status not in ASSESSMENT_STATUSES:
                    raise VerificationError(
                        f"assessment #{index} has unknown status {status!r}")
                awarded = row.get("awarded")
                if awarded is not None:
                    if isinstance(awarded, bool) or not isinstance(awarded, int):
                        raise VerificationError(
                            f"assessment #{index} awarded must be an integer or null")
                out.append(Assessment(
                    seq=int(row.get("seq") or index + 1),
                    reference_id=str(row["reference_id"]),
                    awarded=awarded,
                    status=status,
                    assessor=str(row.get("assessor") or "unknown"),
                    role=str(row.get("role") or ""),
                    note=str(row.get("note") or ""),
                    alternative_methods=tuple(
                        str(v) for v in (row.get("alternative_methods") or [])),
                    created_at=str(row.get("created_at") or ""),
                    dataset_version=str(row.get("dataset_version") or ""),
                ))
            except (KeyError, TypeError, ValueError) as exc:
                if isinstance(exc, VerificationError):
                    raise
                raise VerificationError(
                    f"assessment #{index} is malformed: {exc}") from exc
        out.sort(key=lambda a: a.seq)
        return out

    def history(self, reference_id: str) -> list[Assessment]:
        return [a for a in self.assessments() if a.reference_id == reference_id]

    # -- write --------------------------------------------------------------
    def record(self, *, reference_id: str, awarded: int | None, status: str,
               assessor: str, role: str = "", note: str = "",
               alternative_methods: list[str] | tuple[str, ...] = (),
               dataset_version: str = "", confirmed: bool,
               now: str | None = None) -> Assessment:
        """Append one assessment.

        ``confirmed`` is a hard requirement, not a UI convention: a benchmark
        label is a professional judgement and must be explicitly confirmed.
        """
        if not confirmed:
            raise VerificationError(
                "a teacher assessment requires explicit confirmation")
        status = str(status or "").strip().lower()
        if status not in ASSESSMENT_STATUSES:
            raise VerificationError(
                f"status must be one of {ASSESSMENT_STATUSES}")
        if not str(reference_id or "").strip():
            raise VerificationError("reference_id is required")
        if not str(assessor or "").strip():
            raise VerificationError(
                "assessor is required (name, initials or role identifier)")
        if status == "verified" and awarded is None:
            raise VerificationError("a verified assessment needs an awarded mark")
        if awarded is not None and awarded < 0:
            raise VerificationError("awarded mark cannot be negative")

        # Two verification requests arriving together must not read the same
        # seq and lose one assessment. The API is one process, so a lock is
        # sufficient; the atomic replace below protects a reader from seeing a
        # half-written file.
        with _WRITE_LOCK:
            payload = self._read()
            existing = self.assessments()
            seq = (max((a.seq for a in existing), default=0) + 1)
            row = {
                "seq": seq,
                "reference_id": str(reference_id).strip(),
                "awarded": awarded,
                "status": status,
                "assessor": str(assessor).strip()[:120],
                "role": str(role or "").strip()[:120],
                "note": str(note or "").strip()[:1000],
                "alternative_methods": [str(v).strip()[:200]
                                        for v in alternative_methods
                                        if str(v).strip()],
                "created_at": now or _now(),
                "dataset_version": str(dataset_version or "")[:80],
            }
            payload.setdefault("schema_version", STORE_SCHEMA_VERSION)
            payload.setdefault("assessments", []).append(row)
            self._write(payload)
        return Assessment(
            seq=seq,
            reference_id=row["reference_id"],
            awarded=awarded,
            status=status,
            assessor=row["assessor"],
            role=row["role"],
            note=row["note"],
            alternative_methods=tuple(row["alternative_methods"]),
            created_at=row["created_at"],
            dataset_version=row["dataset_version"],
        )

    def snapshot_sha256(self, dataset: ReferenceDataset) -> str:
        relevant = [a.as_public() for a in self.assessments()
                    if a.reference_id in {i.reference_id for i in dataset.items}]
        return hashlib.sha256(canonical_json({
            "dataset": dataset.sha256,
            "assessments": relevant,
        }).encode("utf-8")).hexdigest()


def resolve_references(dataset: ReferenceDataset,
                       store: VerificationStore) -> dict[str, ResolvedReference]:
    """Combine reference items with the latest assessment of each teacher.

    Rules, in order:

    1. no assessment -> ``unverified``;
    2. a latest ``incomplete`` or ``disputed`` assessment -> not eligible;
    3. two verified teachers with different marks -> ``disputed`` (both kept);
    4. one agreed verified mark on a scheme-derived reference -> eligible;
    5. AI-generated or unknown provenance is never eligible, even if a teacher
       typed a number against it. The source itself is not trustworthy enough
       to contribute to a teacher-certified score.
    """
    by_id = items_by_id(dataset)
    latest: dict[str, dict[str, Assessment]] = {}
    for assessment in store.assessments():
        if assessment.reference_id not in by_id:
            continue
        latest.setdefault(assessment.reference_id, {})[assessment.assessor] = (
            assessment)

    resolved: dict[str, ResolvedReference] = {}
    for reference_id, item in by_id.items():
        rows = latest.get(reference_id)
        if not rows:
            resolved[reference_id] = ResolvedReference(
                item=item, status="unverified", teacher_mark=None,
                reason="no_teacher_assessment", assessments=())
            continue

        assessments = tuple(sorted(rows.values(), key=lambda a: a.seq))
        hard_stop = next((a for a in assessments
                          if a.status in {"incomplete", "disputed"}), None)
        if hard_stop is not None:
            resolved[reference_id] = ResolvedReference(
                item=item,
                status=hard_stop.status,
                teacher_mark=hard_stop.awarded,
                reason=("assessment_incomplete"
                        if hard_stop.status == "incomplete"
                        else "teacher_dispute"),
                assessments=assessments)
            continue

        verified = [a for a in assessments if a.status == "verified"]
        marks = {a.awarded for a in verified if a.awarded is not None}
        if not verified:
            resolved[reference_id] = ResolvedReference(
                item=item, status="unverified", teacher_mark=None,
                reason="no_verified_assessment", assessments=assessments)
            continue
        if len(marks) > 1:
            resolved[reference_id] = ResolvedReference(
                item=item, status="disputed", teacher_mark=None,
                reason="teachers_disagree", assessments=assessments)
            continue

        mark = next(iter(marks))
        if mark < 0 or mark > item.max_marks:
            resolved[reference_id] = ResolvedReference(
                item=item, status="incomplete", teacher_mark=mark,
                reason="teacher_mark_outside_maximum", assessments=assessments)
            continue
        if item.provenance not in ("official_reference", "teacher_verified"):
            resolved[reference_id] = ResolvedReference(
                item=item, status="incomplete", teacher_mark=mark,
                reason="provenance_not_teacher_verifiable",
                assessments=assessments)
            continue

        resolved[reference_id] = ResolvedReference(
            item=item, status="verified", teacher_mark=mark,
            reason="teacher_confirmed", assessments=assessments)

    return resolved


def resolution_summary(dataset: ReferenceDataset,
                       resolved: dict[str, ResolvedReference]) -> dict:
    counts: dict[str, int] = {}
    excluded: list[dict] = []
    for reference_id, row in resolved.items():
        counts[row.status] = counts.get(row.status, 0) + 1
        if not row.eligible:
            excluded.append({
                "reference_id": reference_id,
                "status": row.status,
                "reason": row.reason,
            })
    verified = [row for row in resolved.values() if row.eligible]
    return {
        "total_references": len(resolved),
        "counts": counts,
        "eligible": len(verified),
        "eligible_questions": sum(1 for r in verified if not r.item.is_subquestion),
        "eligible_subquestions": sum(1 for r in verified if r.item.is_subquestion),
        "eligible_marks": sum(r.item.max_marks for r in verified),
        "teacher_marks": sum(int(r.teacher_mark or 0) for r in verified),
        "unresolved": [e for e in excluded if e["status"] in
                       {"disputed", "incomplete"}],            "excluded": excluded,
            "provenance": {
                name: sum(1 for row in resolved.values()
                          if row.item.provenance == name)
                for name in GROUND_TRUTH_CLASSES
            },
        }
