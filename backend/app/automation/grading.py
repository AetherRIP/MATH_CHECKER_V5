"""Deterministic IGCSE grade 1-9 calculation + boundary validation (§18-§21).

The AI produces RAW MARKS only. This module is the single place a grade is
computed:

    marks_obtained / maximum_marks -> percentage -> boundary table -> grade 1-9

The scale is ALWAYS 1-9 (§19): never A*-G, never a percentage grade, never a
custom scale. A grade is never invented — when the inputs or the boundary
table are invalid, :class:`GradeError` is raised and the job is flagged as
requiring attention instead of submitting an incorrect Classroom grade.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..engine import grading as engine_grading
from . import store

#: The only grades the application ever produces (§19).
VALID_GRADES = tuple(str(n) for n in range(1, 10))


class GradeError(RuntimeError):
    """Marks or boundaries are invalid — never guess a grade (§21)."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class GradeResult:
    marks_obtained: int
    maximum_marks: int
    percentage: float
    grade: str
    boundary_source: str


def _valid_boundaries(boundaries: list[tuple[str, float]]) -> bool:
    if not boundaries or len(boundaries) < 2:
        return False
    seen: set[str] = set()
    for label, minimum in boundaries:
        if str(label) not in VALID_GRADES:
            return False
        if not (0.0 <= float(minimum) <= 100.0):
            return False
        if str(label) in seen:
            return False
        seen.add(str(label))
    # There must be a boundary a score of 0 can reach, or the lookup has no
    # defined answer for the bottom of the scale.
    return min(float(m) for _, m in boundaries) <= 0.0


def compute_grade(earned, available, *, coursework_id: str | None = None,
                  boundaries: list[tuple[str, float]] | None = None) -> GradeResult:
    """Validate the inputs, then compute the deterministic grade 1-9.

    Everything §21 requires is checked before any grade exists:

      * maximum marks present and > 0
      * obtained marks present and >= 0
      * obtained <= maximum
      * percentage is the exact arithmetic value
      * a boundary table exists for the assignment (or the global one)
      * the boundary structure is valid
      * the resulting grade is a whole number between 1 and 9
    """
    try:
        obtained = int(earned)
    except (TypeError, ValueError):
        raise GradeError("marks_missing", "The correction produced no marks.")
    try:
        maximum = int(available)
    except (TypeError, ValueError):
        raise GradeError("maximum_missing",
                         "This assignment has no maximum mark to grade against.")
    if maximum <= 0:
        raise GradeError("maximum_invalid",
                         "Maximum marks must be greater than zero.")
    if obtained < 0:
        raise GradeError("marks_negative", "Obtained marks cannot be negative.")
    if obtained > maximum:
        raise GradeError(
            "marks_exceed_maximum",
            f"Obtained marks ({obtained}) exceed the maximum ({maximum}).")

    percentage = obtained / maximum * 100.0

    source = "assignment" if boundaries is not None else "global"
    table = boundaries
    if table is None:
        table, source = store.grade_boundaries(coursework_id)

    if not _valid_boundaries(table):
        raise GradeError(
            "boundaries_invalid",
            "No valid IGCSE grade boundary table is available for this "
            "assignment, so no grade was calculated.")

    grade = str(engine_grading.grade_for_percentage(percentage, table))
    if grade not in VALID_GRADES:
        raise GradeError(
            "grade_out_of_range",
            f"The boundary lookup produced an invalid grade ({grade}); the "
            f"scale is always 1-9.")

    return GradeResult(marks_obtained=obtained, maximum_marks=maximum,
                       percentage=round(percentage, 2), grade=grade,
                       boundary_source=source)


def grade_from_result(result: dict, *, coursework_id: str | None = None,
                      boundaries: list[tuple[str, float]] | None = None) -> GradeResult:
    """Grade a stored correction result row (raw marks -> grade 1-9)."""
    return compute_grade(result.get("earned"), result.get("available"),
                         coursework_id=coursework_id or result.get("coursework_id"),
                         boundaries=boundaries)
