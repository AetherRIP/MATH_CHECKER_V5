"""Deterministic grade calculation from teacher-configured boundaries (§13).

The AI never invents a grade. A grade is a pure lookup:

    raw marks -> percentage -> configured boundary table -> grade 1-9

This module is deliberately separate from ``legacy_marking`` (which is
preserved verbatim, including its hard-coded percentage bands used as the
default). The engine still computes a *provisional* band; this module lets a
teacher configure the real boundaries for their exam session and recomputes
the grade deterministically from the same totals.

Everything here is a pure function of (marks, boundaries): no randomness, no
network, no model. That is what makes a grade reproducible and auditable.
"""

from __future__ import annotations

from . import legacy_marking as legacy


def grade_for_percentage(percentage: float, boundaries: list[tuple[str, float]] | None = None) -> str:
    """Return the grade whose minimum percentage the score reaches.

    ``boundaries`` is a list of ``(grade_label, minimum_percentage)`` pairs.
    When it is empty/None the preserved legacy bands are used, so behaviour
    is unchanged for an unconfigured install.
    """
    if not boundaries:
        return legacy.percentage_band_grade(percentage)

    # Highest boundary the score meets wins; ties resolved by later entry.
    ordered = sorted(boundaries, key=lambda item: item[1])

    # A score BELOW the lowest configured boundary has no configured grade. It
    # must not silently inherit the lowest label in the table: a teacher who
    # lists only the top grades (e.g. 7/8/9) would then hand a barely-attempted
    # paper a 7. Fall back to the preserved legacy percentage bands, which are
    # defined across the whole 0-100 range, so the direction of the error can
    # only ever be "too low", never a gifted high grade.
    if percentage < ordered[0][1]:
        return legacy.percentage_band_grade(percentage)

    grade = ordered[0][0]
    for label, minimum in ordered:
        if percentage >= minimum:
            grade = label
        else:
            break
    return grade


def grade_for_totals(earned: int, available: int,
                     boundaries: list[tuple[str, float]] | None = None) -> tuple[str, float]:
    """Return ``(grade, percentage)`` for a raw score, deterministically."""
    percentage = (earned / available * 100.0) if available else 0.0
    return grade_for_percentage(percentage, boundaries), percentage


def active_boundaries() -> list[tuple[str, float]]:
    """Boundaries from the live configuration (falls back to legacy bands)."""
    from ..config import get_config
    return get_config().grades.boundaries


def describe_boundaries(boundaries: list[tuple[str, float]] | None = None) -> str:
    """Human-readable boundary table for reports/logs (never empty)."""
    table = boundaries if boundaries is not None else active_boundaries()
    if not table:
        return "Default percentage bands (legacy)"
    parts = [f"{label}>={minimum:g}%" for label, minimum in
             sorted(table, key=lambda item: item[1])]
    return ", ".join(parts)
