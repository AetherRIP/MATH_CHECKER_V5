"""Load and validate the versioned marking reference set.

The reference set is a plain JSON document so it can be reviewed, diffed and
held under version control. This module is the only place that decides what a
reference item *is* and whether it may contribute to a teacher-certified score.

Ground-truth classes are explicit
---------------------------------
``teacher_verified``   a qualified teacher has confirmed the mark
``official_reference`` an official scheme/answer exists, but the specific
                       candidate mark has not been independently verified
``ai_generated``       the mark came from an AI correction (never eligible)
``unverified``         provenance is unknown

A missing class defaults to ``official_reference`` for the existing manifest:
its marks were derived clause-by-clause from a printed scheme. That is useful
for measurement, but the loader never promotes it because the file contains a
signature, a previous run, or a passing test.

Partitions are per PAPER, never per item. Splitting a paper would let the same
script appear in both tuning and holdout data, which is precisely the
contamination this field exists to prevent.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

DEFAULT_DATASET = (Path(__file__).resolve().parents[3] / "docs"
                   / "reference-marking" / "manifest.json")

GROUND_TRUTH_CLASSES = (
    "teacher_verified",
    "official_reference",
    "ai_generated",
    "unverified",
)
PARTITIONS = ("development", "holdout")
PROVENANCE_ELIGIBLE_WITH_TEACHER = ("official_reference", "teacher_verified")

_REFERENCE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_SUBQUESTION = re.compile(r"^\s*\d+\s*[a-z]+\s*$", re.IGNORECASE)


class DatasetError(ValueError):
    """The reference set is structurally unusable; no run may start."""

    def __init__(self, errors: list[str], path: Path | None = None):
        self.errors = errors
        where = f" in {path}" if path else ""
        super().__init__(f"reference dataset is invalid{where}: " + "; ".join(errors))


@dataclass(frozen=True)
class ReferenceItem:
    reference_id: str
    paper_id: str
    question: str
    subquestion: str | None
    max_marks: int
    scheme_mark: int
    prompt: str
    scheme: tuple[str, ...]
    student_answer: tuple[str, ...]
    categories: tuple[str, ...]
    ruling: str
    basis: str
    provenance: str
    partition: str
    board: str
    qualification: str
    topic: str
    paper_title: str
    student_pdf: str | None
    scheme_pdf: str | None

    @property
    def is_subquestion(self) -> bool:
        return self.subquestion is not None

    @property
    def level(self) -> str:
        return "subquestion" if self.is_subquestion else "question"

    def as_public(self) -> dict:
        """Everything a reviewer needs, with no student identity fields."""
        return {
            "reference_id": self.reference_id,
            "paper_id": self.paper_id,
            "paper_title": self.paper_title,
            "board": self.board,
            "qualification": self.qualification,
            "topic": self.topic,
            "partition": self.partition,
            "question": self.question,
            "subquestion": self.subquestion,
            "level": self.level,
            "max_marks": self.max_marks,
            "scheme_mark": self.scheme_mark,
            "provenance": self.provenance,
            "prompt": self.prompt,
            "scheme": list(self.scheme),
            "student_answer": list(self.student_answer),
            "categories": list(self.categories),
            "ruling": self.ruling,
            "basis": self.basis,
            "student_pdf": self.student_pdf,
            "scheme_pdf": self.scheme_pdf,
        }


@dataclass
class Paper:
    id: str
    title: str
    board: str
    qualification: str
    topic: str
    partition: str
    provenance: str
    lesson_id: str | None
    marks_available: int
    expected_total: int
    scheme_stated_total: int | None
    scheme_header_disagrees: bool
    teacher_grade: str | None
    student_pdf: str | None
    scheme_pdf: str | None
    items: tuple[ReferenceItem, ...]

    def as_public(self) -> dict:
        return {
            "id": self.id,
            "title": self.title,
            "board": self.board,
            "qualification": self.qualification,
            "topic": self.topic,
            "partition": self.partition,
            "provenance": self.provenance,
            "lesson_id": self.lesson_id,
            "marks_available": self.marks_available,
            "expected_total": self.expected_total,
            "scheme_stated_total": self.scheme_stated_total,
            "scheme_header_disagrees": self.scheme_header_disagrees,
            "teacher_grade": self.teacher_grade,
            "items": len(self.items),
        }


@dataclass
class ReferenceDataset:
    path: Path
    schema_version: int
    version: str
    sha256: str
    papers: tuple[Paper, ...]
    warnings: tuple[str, ...] = ()
    review: dict = field(default_factory=dict)
    metadata: dict = field(default_factory=dict)

    @property
    def items(self) -> tuple[ReferenceItem, ...]:
        return tuple(item for paper in self.papers for item in paper.items)

    def paper(self, paper_id: str) -> Paper | None:
        return next((p for p in self.papers if p.id == paper_id), None)

    def items_for(self, partition: str = "all") -> tuple[ReferenceItem, ...]:
        if partition == "all":
            return self.items
        return tuple(i for i in self.items if i.partition == partition)

    def partitions(self) -> dict[str, int]:
        counts = {name: 0 for name in PARTITIONS}
        for item in self.items:
            counts[item.partition] = counts.get(item.partition, 0) + 1
        return counts

    def provenance_counts(self) -> dict[str, int]:
        counts = {name: 0 for name in GROUND_TRUTH_CLASSES}
        for item in self.items:
            counts[item.provenance] = counts.get(item.provenance, 0) + 1
        return counts

    def descriptor(self) -> dict:
        categories: dict[str, int] = {}
        for item in self.items:
            for category in item.categories:
                categories[category] = categories.get(category, 0) + 1
        return {
            "path": str(self.path),
            "schema_version": self.schema_version,
            "version": self.version,
            "sha256": self.sha256,
            "papers": [paper.as_public() for paper in self.papers],
            "paper_count": len(self.papers),
            "item_count": len(self.items),
            "question_count": sum(1 for i in self.items if not i.is_subquestion),
            "subquestion_count": sum(1 for i in self.items if i.is_subquestion),
            "marks_available": sum(i.max_marks for i in self.items),
            "scheme_marks": sum(i.scheme_mark for i in self.items),
            "partitions": self.partitions(),
            "provenance": self.provenance_counts(),
            "categories": dict(sorted(categories.items())),
            "boards": sorted({i.board for i in self.items}),
            "qualifications": sorted({i.qualification for i in self.items}),
            "topics": sorted({i.topic for i in self.items}),
            "warnings": list(self.warnings),
            "review": dict(self.review),
        }


def _int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return None


def _text(value: Any) -> str:
    return str(value or "").strip()


def _strings(value: Any) -> tuple[str, ...]:
    if isinstance(value, str):
        return (value,) if value.strip() else ()
    if isinstance(value, (list, tuple)):
        return tuple(str(v).strip() for v in value if str(v).strip())
    return ()


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True)


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _subquestion_of(question: str) -> str | None:
    if not _SUBQUESTION.match(question):
        return None
    return re.sub(r"^\s*\d+\s*", "", question).strip().lower() or None


def load_dataset(path: str | Path | None = None) -> ReferenceDataset:
    """Read, validate and return the reference set.

    Structural problems raise :class:`DatasetError` and no evaluation may run.
    Methodological problems (no holdout, unsigned review, a scheme header whose
    total disagrees with its parts) are warnings: they do not make the marks
    unusable, but they must appear in every report that uses the data.
    """
    target = Path(path) if path is not None else DEFAULT_DATASET
    if not target.is_file():
        raise DatasetError([f"file not found: {target}"], target)
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise DatasetError([f"cannot read JSON: {exc}"], target) from exc
    if not isinstance(payload, dict):
        raise DatasetError(["top level must be a JSON object"], target)

    errors: list[str] = []
    warnings: list[str] = []

    schema_version = _int(payload.get("schema_version")) or 1
    version = _text(payload.get("dataset_version") or payload.get("version"))
    if not version:
        errors.append("dataset_version (or version) is required")
        version = "unversioned"

    papers_raw = payload.get("papers")
    if not isinstance(papers_raw, list) or not papers_raw:
        errors.append("papers must be a non-empty list")
        papers_raw = []

    review = payload.get("review") if isinstance(payload.get("review"), dict) else {}
    if not _text(review.get("verified_by")):
        warnings.append(
            "review_unverified: the dataset-level review is unsigned; "
            "only per-item teacher assessments can certify a score")

    papers: list[Paper] = []
    seen_papers: set[str] = set()
    seen_ids: set[str] = set()
    seen_questions: set[tuple[str, str]] = set()

    for paper_index, raw in enumerate(papers_raw):
        if not isinstance(raw, dict):
            errors.append(f"paper #{paper_index} must be an object")
            continue
        paper_id = _text(raw.get("id"))
        if not paper_id:
            errors.append(f"paper #{paper_index} has no id")
            continue
        if paper_id in seen_papers:
            errors.append(f"duplicate paper id: {paper_id}")
            continue
        seen_papers.add(paper_id)

        partition = _text(raw.get("partition") or "development").lower()
        if partition not in PARTITIONS:
            errors.append(f"{paper_id}: partition must be one of {PARTITIONS}")
        provenance = _text(raw.get("ground_truth") or raw.get("provenance")
                           or "official_reference").lower()
        if provenance not in GROUND_TRUTH_CLASSES:
            errors.append(f"{paper_id}: unknown ground_truth class {provenance!r}")

        board = _text(raw.get("board") or raw.get("examination_board")) or "Not recorded"
        qualification = _text(raw.get("qualification")) or "Not recorded"
        topic = _text(raw.get("topic")) or "Not recorded"
        title = _text(raw.get("title")) or paper_id

        items_raw = raw.get("items")
        if not isinstance(items_raw, list) or not items_raw:
            errors.append(f"{paper_id}: items must be a non-empty list")
            items_raw = []

        paper_items: list[ReferenceItem] = []
        for item_index, item in enumerate(items_raw):
            where = f"{paper_id} item #{item_index + 1}"
            if not isinstance(item, dict):
                errors.append(f"{where}: must be an object")
                continue
            question = _text(item.get("question") or item.get("question_id"))
            if not question:
                errors.append(f"{where}: question is required")
                continue
            key = (paper_id, question)
            if key in seen_questions:
                errors.append(f"{where}: duplicate question label {question!r} in paper")
                continue
            seen_questions.add(key)

            maximum = _int(item.get("max_marks", item.get("marks_available")))
            scheme_mark = _int(item.get("teacher_awarded",
                                        item.get("expected", item.get("awarded"))))
            if maximum is None or maximum < 0:
                errors.append(f"{where}: max_marks must be a non-negative integer")
                continue
            if scheme_mark is None or scheme_mark < 0 or scheme_mark > maximum:
                errors.append(
                    f"{where}: expected mark must be an integer between 0 and "
                    f"{maximum}")
                continue

            item_provenance = _text(item.get("ground_truth")
                                    or item.get("provenance") or provenance).lower()
            if item_provenance not in GROUND_TRUTH_CLASSES:
                errors.append(f"{where}: unknown ground_truth class "
                              f"{item_provenance!r}")
            item_partition = _text(item.get("partition") or partition).lower()
            if item_partition != partition:
                errors.append(
                    f"{where}: partition {item_partition!r} differs from its paper "
                    f"({partition!r}); a paper cannot be split across partitions")

            reference_id = _text(item.get("reference_id")) or f"{paper_id}.q{question}"
            if not _REFERENCE_ID.match(reference_id):
                errors.append(
                    f"{where}: reference_id {reference_id!r} must be 1-128 chars "
                    "of letters, digits, '.', '_', ':' or '-'")
            if reference_id in seen_ids:
                errors.append(f"{where}: duplicate reference_id {reference_id!r}")
            seen_ids.add(reference_id)

            sub = _text(item.get("subquestion")) or _subquestion_of(question)
            paper_items.append(ReferenceItem(
                reference_id=reference_id,
                paper_id=paper_id,
                question=question,
                subquestion=sub,
                max_marks=maximum,
                scheme_mark=scheme_mark,
                prompt=_text(item.get("prompt")),
                scheme=_strings(item.get("scheme")),
                student_answer=_strings(item.get("student_answer")),
                categories=_strings(item.get("categories")),
                ruling=_text(item.get("ruling")),
                basis=_text(item.get("basis")),
                provenance=item_provenance,
                partition=partition,
                board=board,
                qualification=qualification,
                topic=topic,
                paper_title=title,
                student_pdf=_text(raw.get("student_pdf")) or None,
                scheme_pdf=_text(raw.get("scheme_pdf")) or None,
            ))

        marks_available = _int(raw.get("marks_available",
                                      sum(i.max_marks for i in paper_items)))
        expected_total = _int(raw.get("expected_total",
                                     sum(i.scheme_mark for i in paper_items)))
        if marks_available is None or expected_total is None:
            errors.append(f"{paper_id}: marks_available and expected_total must "
                          "be integers")
            marks_available = sum(i.max_marks for i in paper_items)
            expected_total = sum(i.scheme_mark for i in paper_items)
        elif paper_items:
            item_max = sum(i.max_marks for i in paper_items)
            item_expected = sum(i.scheme_mark for i in paper_items)
            if marks_available != item_max:
                errors.append(
                    f"{paper_id}: marks_available={marks_available} but its items "
                    f"sum to {item_max}")
            if expected_total != item_expected:
                errors.append(
                    f"{paper_id}: expected_total={expected_total} but its items "
                    f"sum to {item_expected}")
            if expected_total > marks_available:
                errors.append(f"{paper_id}: expected_total exceeds marks_available")

        stated = _int(raw.get("scheme_stated_total"))
        header_disagrees = bool(raw.get("scheme_header_disagrees"))
        if stated is not None and stated != marks_available:
            header_disagrees = True
            warnings.append(
                f"{paper_id}: the printed scheme header states {stated} marks but "
                f"its parts sum to {marks_available}")
        if header_disagrees:
            warnings.append(
                f"{paper_id}: scheme_header_disagrees — per-question marks are used, "
                "and the discrepancy is reported rather than silently resolved")

        papers.append(Paper(
            id=paper_id,
            title=title,
            board=board,
            qualification=qualification,
            topic=topic,
            partition=partition,
            provenance=provenance,
            lesson_id=_text(raw.get("lesson_id")) or None,
            marks_available=marks_available,
            expected_total=expected_total,
            scheme_stated_total=stated,
            scheme_header_disagrees=header_disagrees,
            teacher_grade=_text(raw.get("teacher_grade")) or None,
            student_pdf=_text(raw.get("student_pdf")) or None,
            scheme_pdf=_text(raw.get("scheme_pdf")) or None,
            items=tuple(paper_items),
        ))

    if papers and not any(p.partition == "holdout" for p in papers):
        warnings.append(
            "no_holdout_partition: every paper is in the development partition; "
            "the dataset cannot provide an independent holdout estimate")
    for item in (i for p in papers for i in p.items):
        if "nested-subquestion" in item.categories and not item.is_subquestion:
            warnings.append(
                f"{item.reference_id}: marked as a nested subquestion but stored "
                "under a whole-question label; it contributes to question-level "
                "metrics only")

    if errors:
        raise DatasetError(sorted(set(errors)), target)

    return ReferenceDataset(
        path=target,
        schema_version=schema_version,
        version=version,
        sha256=file_sha256(target),
        papers=tuple(papers),
        warnings=tuple(dict.fromkeys(warnings)),
        review=review,
        metadata={k: v for k, v in payload.items()
                  if k not in {"papers", "review"}},
    )


def items_by_id(dataset: ReferenceDataset) -> dict[str, ReferenceItem]:
    return {item.reference_id: item for item in dataset.items}


def papers_with_items(dataset: ReferenceDataset,
                      items: Iterable[ReferenceItem]) -> dict[str, list[ReferenceItem]]:
    grouped: dict[str, list[ReferenceItem]] = {}
    for item in items:
        grouped.setdefault(item.paper_id, []).append(item)
    for rows in grouped.values():
        rows.sort(key=lambda row: row.question)
    return grouped
