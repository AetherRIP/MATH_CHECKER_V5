"""Single source of truth for homework tracking status (spec 31-35).

Every surface - reports, dashboard, class pages, student profiles, the
assignment list - resolves a submission through :func:`resolve`, so the same
Classroom row can never read "submitted" in one place and "missing" in
another (spec 48).

The rule, in priority order (spec 34)::

    1. no submission row                    -> OUTSTANDING
    2. Classroom state MISSING               -> OUTSTANDING
    3. Classroom state DRAFT (CREATED/NEW)   -> OUTSTANDING
    4. recorded grade == 0                   -> OUTSTANDING
    5. recorded grade == 1                   -> GRADE_ONE
    6. handed in, awaiting a grade           -> PENDING
    7. handed in with a grade >= 2           -> SUBMITTED

An OUTSTANDING result always preserves where it came from:
``original_status`` holds the state it was derived from (MISSING, CREATED,
GRADED, ...) and ``original_grade`` the grade when a grade is what made it
outstanding (spec 32).  A draft is outstanding WITH its draft origin intact -
never silently relabelled - and a submitted grade of 2 or more is never
missing (spec 33).

The SQL twin :func:`sql_outstanding` applies the same rule to raw
``classroom_submissions`` rows so aggregate counters (classes page,
assignment list) agree with the Python resolver row-for-row.
"""

from __future__ import annotations

from dataclasses import dataclass

#: The four tracking statuses a surface may show.
OUTSTANDING = "OUTSTANDING"
GRADE_ONE = "GRADE_ONE"
SUBMITTED = "SUBMITTED"
PENDING = "PENDING"

TRACKING_STATUSES = (OUTSTANDING, GRADE_ONE, SUBMITTED, PENDING)

#: Normalized statuses meaning "the student never handed this in".
NOT_HANDED_IN = frozenset({"MISSING", "NOT_STARTED", "RECLAIMED"})

#: Normalized status meaning "opened, work started, not handed in" (a draft).
DRAFT_STATUSES = frozenset({"CREATED"})

#: Normalized statuses meaning "the work is in the teacher's hands".
HANDED_IN = frozenset({
    "SUBMITTED", "SUBMITTED_LATE", "RETURNED", "EDITED_AFTER_SUBMISSION",
    "GRADED", "CORRECTED", "PROCESSING", "FAILED", "INVALID",
})

#: Raw classroom_submissions.current_state values that mean "never handed in".
NOT_HANDED_IN_RAW = frozenset({
    "MISSING", "NOT_STARTED", "RECLAIMED_BY_STUDENT", "CREATED", "NEW", "",
})

#: A grade that makes the work outstanding / a grade-one result (spec 33).
OUTSTANDING_GRADE = 0.0
GRADE_ONE_VALUE = 1.0


@dataclass(frozen=True)
class Tracking:
    """The resolved status of ONE student x assignment submission."""

    status: str
    original_status: str
    original_grade: float | None = None

    @property
    def outstanding(self) -> bool:
        return self.status == OUTSTANDING

    @property
    def label(self) -> str:
        """A human word for the status - never colour alone (spec 51)."""
        if self.status == OUTSTANDING:
            reason = {
                "MISSING": "Missing",
                "NOT_STARTED": "Not turned in",
                "RECLAIMED": "Unsubmitted by student",
                "CREATED": "Draft",
                "NO_SUBMISSION": "No submission record",
            }.get(self.original_status)
            if reason:
                return reason
            if self.original_grade is not None:
                return "Grade 0"
            return "Outstanding"
        if self.status == GRADE_ONE:
            return "Grade 1"
        word = {
            "GRADED": "Graded",
            "CORRECTED": "Corrected",
            "FAILED": "Correction failed",
            "INVALID": "Invalid submission",
            "PROCESSING": "Being corrected",
            "UNKNOWN": "Status unknown — resync",
        }.get(self.original_status)
        if word:
            return word
        if self.status == PENDING:
            return "Submitted, not yet graded"
        return "Submitted"


def _number(value) -> float | None:
    try:
        if value is None or isinstance(value, bool):
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def resolve(status: str | None, grade=None) -> Tracking:
    """Resolve one submission's tracking status (spec 34).

    ``status`` is the application's normalized Classroom status; ``grade`` is
    the recorded numeric mark for that same submission (Math Correct's own
    mark when it exists, otherwise Classroom's assigned grade) - or None when
    nothing is recorded.
    """
    state = str(status or "").strip().upper()
    value = _number(grade)

    # 1-3: absence of work, whatever shape the absence takes.
    if not state:
        return Tracking(OUTSTANDING, "NO_SUBMISSION", None)
    if state == "MISSING":
        return Tracking(OUTSTANDING, "MISSING", value)
    if state in NOT_HANDED_IN:
        return Tracking(OUTSTANDING, state, value)
    if state in DRAFT_STATUSES:
        return Tracking(OUTSTANDING, "CREATED", value)

    # 4-5: the recorded grade decides between outstanding / grade 1.
    if value is not None and value == OUTSTANDING_GRADE:
        return Tracking(OUTSTANDING, state, value)
    if value is not None and value == GRADE_ONE_VALUE:
        return Tracking(GRADE_ONE, state, value)

    # 6-7: the work is in the teacher's hands.
    if state in HANDED_IN:
        return Tracking(PENDING if value is None else SUBMITTED, state, value)

    # Unknown/unverifiable data: never accuse, never clear (spec 35 - do not
    # invent a missing status from an absence of data).
    return Tracking(PENDING, state, value)


def is_outstanding(status: str | None, grade=None) -> bool:
    """Convenience wrapper: does this submission count as outstanding?"""
    return resolve(status, grade).outstanding


def grade_of(row: dict) -> float | None:
    """The recorded grade for a report row, Math Correct mark first.

    Mirrors the precedence the report model uses: Math Correct's own result
    outranks Classroom's assigned grade, and None means "nothing recorded".
    """
    for key in ("mc_earned", "earned"):
        value = _number(row.get(key))
        if value is not None:
            return value
    result = row.get("result")
    if isinstance(result, dict):
        value = _number(result.get("earned"))
        if value is not None:
            return value
    return _number(row.get("assigned_grade"))


def sql_outstanding(alias: str = "cs") -> str:
    """A SQL expression (1/0) applying the SAME rule to raw rows.

    Uses raw classroom_submissions states (the storage layer) instead of
    normalized ones (the view layer); the mapping lives in NOT_HANDED_IN_RAW.
    The grade consulted is the latest Math Correct result's earned mark when
    one exists, otherwise the assigned Classroom grade - the same precedence
    as :func:`grade_of`.  Rows whose state could not be verified
    (data_quality ERROR) are never counted outstanding.
    """
    grade = (
        f"COALESCE((SELECT r.earned FROM results r "
        f"WHERE r.submission_id = {alias}.submission_id "
        f"ORDER BY r.created_at DESC LIMIT 1), {alias}.assigned_grade)"
    )
    states = ",".join(f"'{s}'" for s in sorted(NOT_HANDED_IN_RAW))
    return (
        "(CASE "
        f"WHEN COALESCE({alias}.data_quality, '') = 'ERROR' THEN 0 "
        f"WHEN COALESCE({alias}.current_state, '') IN ({states}) THEN 1 "
        f"WHEN {grade} = 0 THEN 1 "
        "ELSE 0 END)"
    )
