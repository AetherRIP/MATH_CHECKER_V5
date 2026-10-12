"""The dedicated CLASS REPORT: an administrative homework/grades document.

Three things separate it from the general class report:

  * the teacher CHOOSES which assignments it covers - it never silently
    includes the whole course;
  * it keeps "did not submit" and "scored 1" apart, because those are
    different problems with different follow-ups;
  * every row names the actual assignment it is about - code, chapter and
    lesson - so a teacher reading "L4" can see what L4 is without a key.

Everything here is a pure function of the rows handed in by the API layer,
so the data rules can be tested without a database and the PDF renderer can
never quietly invent a mark.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Iterable, Sequence

from ..submission_status import (
    OUTSTANDING,
    GRADE_ONE as GRADE_ONE_STATUS,
    DRAFT_STATUSES as _SHARED_DRAFT_STATUSES,
    resolve as resolve_tracking,
)

# Classroom statuses that mean "the student did not hand this in". A student
# who reclaimed their work, or whose submission row never arrived, has not
# submitted it either - none of these carry a mark.
MISSING_STATUSES = frozenset({
    "MISSING", "CREATED", "NOT_STARTED", "RECLAIMED",
})

#: Google Classroom says the student opened the work but never handed it in.
#: A draft is OUTSTANDING (§31) but keeps its own label and its own section,
#: so it is never silently relabelled as a non-submission (§32).
DRAFT_STATUSES = frozenset(_SHARED_DRAFT_STATUSES)

#: Google Classroom explicitly says the work was never handed in (§21).
NOT_TURNED_IN_STATUSES = frozenset({"MISSING", "NOT_STARTED", "RECLAIMED"})

#: A recorded grade of exactly 1. This is a RESULT, not an absence: the work
#: exists and was scored. It must never be inferred from a missing status.
GRADE_ONE = 1

#: Default low-grade threshold: a grade STRICTLY below this is highlighted.
#: The teacher changes it per report (§24) - nothing else may assume a value.
DEFAULT_LOW_THRESHOLD = 2.0

_FILE_SUFFIX = re.compile(r"\.(pdf|docx?|pptx?|xlsx?|png|jpe?g|txt|zip)$", re.I)
_CH_L = re.compile(r"\bch\s*(\d{1,2})\s*[.\- ]?\s*l\s*(\d{1,2})\b", re.I)
_LONG = re.compile(r"\bchapter\s*(\d{1,2})\b[^.]*?\blesson\s*(\d{1,2})\b", re.I)
_LESSON = re.compile(r"\blesson\s*(\d{1,2})\b", re.I)
_QUIZ = re.compile(r"\bquiz\s*(\d{1,2})\b", re.I)
_CODE = re.compile(r"^([A-Za-z]{1,3}\s?\d{1,2})\b")


@dataclass(frozen=True)
class AssignmentCode:
    """What a teacher calls an assignment, and what it actually is (§21).

    ``code`` is the compact notation used in tables ("L4"); ``detail`` spells
    it out ("Chapter 1, Lesson 4 - Percentages"). Both come from the real
    Classroom title - nothing is assigned by position or guessed.
    """

    code: str
    chapter: str = ""
    lesson: str = ""
    topic: str = ""
    title: str = ""
    coursework_id: str = ""
    max_points: Any = None

    @property
    def detail(self) -> str:
        """The long form: 'Chapter 1, Lesson 4 - Percentages'."""
        bits: list[str] = []
        if self.chapter and self.lesson:
            bits.append(f"Chapter {self.chapter}, Lesson {self.lesson}")
        elif self.lesson:
            bits.append(f"Lesson {self.lesson}")
        elif self.chapter:
            bits.append(f"Chapter {self.chapter}")
        if self.topic:
            bits.append(self.topic)
        return " - ".join(bits) or self.title

    @property
    def label(self) -> str:
        """'L4 - Chapter 1, Lesson 4 - Percentages', or the title alone."""
        return f"{self.code} - {self.detail}" if self.detail else self.code

    @property
    def sort_key(self) -> tuple:
        """Order lessons by chapter then lesson; everything else keeps title."""
        def num(value: str) -> int:
            try:
                return int(value)
            except (TypeError, ValueError):
                return 0
        return (0, num(self.chapter), num(self.lesson), self.title) \
            if (self.chapter or self.lesson) else (1, 0, 0, self.title)

    def as_dict(self) -> dict:
        return {
            "coursework_id": self.coursework_id,
            "title": self.title,
            "code": self.code,
            "chapter": self.chapter,
            "lesson": self.lesson,
            "topic": self.topic,
            "detail": self.detail,
            "label": self.label,
            "max_points": self.max_points,
        }


def parse_assignment(title: str, *, coursework_id: str = "",
                     max_points: Any = None) -> AssignmentCode:
    """Derive the compact code and the chapter/lesson detail from a title.

    Handles the shapes real Classroom titles come in: "Ch1L4 - Percentages",
    "Chapter 1 Lesson 7", "Math Chapter 2 lesson 3", "Quiz 2 and Answers".
    A title it does not recognise falls back to its own first token rather
    than to a number, because an invented code is worse than a long one.
    """
    raw = str(title or "").strip()
    clean = _FILE_SUFFIX.sub("", raw).strip()

    chapter = lesson = topic = ""

    match = _CH_L.search(clean)
    if match:
        chapter, lesson = match.group(1), match.group(2)
    else:
        match = _LONG.search(clean)
        if match:
            chapter, lesson = match.group(1), match.group(2)
        else:
            lesson_match = _LESSON.search(clean)
            if lesson_match:
                lesson = lesson_match.group(1)

    # Whatever follows the chapter/lesson marker is the topic.
    marker = re.search(r"\bl(?:esson)?\s*\.?\s*\d{1,2}\b", clean, re.I)
    if marker:
        topic = clean[marker.end():].strip(" -–—:·,.")
    else:
        quiz = _QUIZ.search(clean)
        topic = clean[quiz.end():].strip(" -–—:·,.") if quiz else ""

    if lesson:
        code = f"L{lesson}"
    elif chapter:
        code = f"Ch{chapter}"
    else:
        quiz = _QUIZ.search(clean)
        if quiz:
            code = f"Q{quiz.group(1)}"
        else:
            first = _CODE.match(clean)
            code = (first.group(1).replace(" ", "") if first
                    else clean[:24] or "HW")

    return AssignmentCode(code=code, chapter=chapter, lesson=lesson,
                          topic=topic, title=clean,
                          coursework_id=str(coursework_id or ""),
                          max_points=max_points)


def _codes_for(courses: Iterable[dict]) -> list[AssignmentCode]:
    codes = [parse_assignment(c.get("title"), coursework_id=c.get("id"),
                              max_points=c.get("max_points"))
             for c in courses]
    codes.sort(key=lambda c: c.sort_key)
    return codes


def grade_label(percentage: float | None, boundaries=None) -> str:
    """The grade on the assignment's OWN scale, or "" when there is none.

    A grade is a deterministic lookup of (percentage -> configured boundary
    table), the same function the correction engine uses, so a report can
    never show a grade the app would not show anywhere else. A missing
    percentage yields "" - never a fabricated 0.
    """
    if percentage is None:
        return ""
    try:
        from ..engine.grading import active_boundaries, grade_for_percentage
        table = boundaries if boundaries is not None else active_boundaries()
        return str(grade_for_percentage(float(percentage), table))
    except Exception:  # noqa: BLE001 - a grade must never break a report
        return ""


def numeric_grade(label: Any) -> float | None:
    """Parse a grade label to a number, or None when it is not on a scale."""
    try:
        return float(str(label).strip())
    except (TypeError, ValueError):
        return None


#: Plain-language words for the statuses a printed report shows. Every one
#: carries a WORD, so a grayscale printout is still unambiguous (§51).
_STATUS_WORDS = {
    "MISSING": "Missing",
    "NOT_STARTED": "Not turned in",
    "RECLAIMED": "Unsubmitted by student",
    "CREATED": "Draft / incomplete",
}


def _status_word(status: str) -> str:
    return _STATUS_WORDS.get(str(status or ""), "Not submitted")


# ---------------------------------------------------------------------------
# Row classification
# ---------------------------------------------------------------------------


@dataclass
class Cell:
    """One student x one assignment, already resolved to a plain outcome."""

    student_id: str
    assignment: AssignmentCode
    status: str = ""
    submitted: bool = False
    grade: str = ""
    grade_source: str = ""
    raw_earned: float | None = None
    percentage: float | None = None
    #: Percentage regardless of source, so the grade scale can be applied.
    pct: float | None = None
    #: Grade on the assignment's own configured scale ("1".."9").
    grade_label: str = ""
    #: Centralized tracking status (§31-§34): OUTSTANDING / GRADE_ONE /
    #: SUBMITTED / PENDING, resolved by app.submission_status.resolve.
    tracking: str = ""
    #: The state/grade the tracking status was derived from (§32).
    original_status: str = ""
    original_grade: float | None = None
    #: Human word for the tracking status ("Missing", "Draft", "Grade 0").
    tracking_label: str = ""

    @property
    def is_draft(self) -> bool:
        """Opened but not handed in - outstanding, but labelled a draft."""
        return self.status in DRAFT_STATUSES

    @property
    def is_not_turned_in(self) -> bool:
        """Google Classroom explicitly says the work was never handed in."""
        return self.status in NOT_TURNED_IN_STATUSES

    @property
    def is_missing(self) -> bool:
        """Never handed in (excludes drafts) - the raw Classroom meaning."""
        return not self.submitted and not self.is_draft

    @property
    def is_outstanding(self) -> bool:
        """Counts as outstanding homework: MISSING, draft, or grade 0 (§31).

        Falls back to the raw meaning only if the resolver never ran, so a
        cell can never silently read as "fine".
        """
        if self.tracking:
            return self.tracking == OUTSTANDING
        return not self.submitted

    @property
    def is_grade_one(self) -> bool:
        return self.raw_earned is not None and float(self.raw_earned) == GRADE_ONE

    def below(self, threshold: float | None) -> bool:
        """Grade strictly below the teacher's threshold (§24).

        Requires an actual recorded grade: a missing or draft submission has
        no grade and can therefore never land in the low-grade section.
        """
        if threshold is None or not self.submitted:
            return False
        value = numeric_grade(self.grade_label)
        return value is not None and value < float(threshold)


def _num(value: Any) -> float | None:
    try:
        if value is None or isinstance(value, bool):
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def build_cells(rows: Sequence[dict], codes: Sequence[AssignmentCode],
                ) -> dict[str, dict[str, Cell]]:
    """``{student_id: {coursework_id: Cell}}`` for the selected assignments.

    Rows come from the API layer already keyed by real Classroom ids, and the
    grade written into a cell is only ever the grade recorded on that same
    row - there is no path by which one student's mark can land on another's
    line.
    """
    by_id = {c.coursework_id: c for c in codes if c.coursework_id}
    out: dict[str, dict[str, Cell]] = {}
    for row in rows:
        coursework_id = str(row.get("coursework_id") or "")
        assignment = by_id.get(coursework_id)
        if assignment is None:
            continue
        student_id = str(row.get("student_id") or "")
        if not student_id:
            continue

        status = str(row.get("status") or "")
        submitted = status not in MISSING_STATUSES
        cell = Cell(student_id=student_id, assignment=assignment,
                    status=status, submitted=submitted)

        # Math Correct's own mark is the recorded result; Classroom's assigned
        # grade is used only when Math Correct has none.
        earned = _num(row.get("mc_earned"))
        available = _num(row.get("mc_available"))
        pct: float | None = None
        if earned is not None:
            cell.raw_earned = earned
            cell.grade = (f"{int(earned) if earned == int(earned) else earned}"
                          f"/{int(available) if available and available == int(available) else available}")
            cell.grade_source = "Math Correct"
            pct = _num(row.get("mc_percentage"))
            cell.percentage = pct
        else:
            assigned = _num(row.get("assigned_grade"))
            if assigned is not None:
                cell.raw_earned = assigned
                maximum = _num(row.get("max_points"))
                cell.grade = (f"{assigned:g}"
                              + (f"/{maximum:g}" if maximum else ""))
                cell.grade_source = "Classroom"
                if maximum:
                    pct = round(assigned / maximum * 100.0, 2)
        # The percentage is only ever derived from THIS row's own marks, so a
        # grade can never be computed from another student's work.
        cell.pct = pct
        cell.grade_label = grade_label(pct)
        if not cell.grade and submitted:
            cell.grade = "No grade recorded"
        # One resolver decides what this cell MEANS everywhere (§35).
        track = resolve_tracking(status, cell.raw_earned)
        cell.tracking = track.status
        cell.original_status = track.original_status
        cell.original_grade = track.original_grade
        cell.tracking_label = track.label
        out.setdefault(student_id, {})[coursework_id] = cell
    return out


@dataclass
class Group:
    """One teacher-assigned class and everything the report says about it."""

    class_label: str
    students: list[dict] = field(default_factory=list)
    missing: list[dict] = field(default_factory=list)
    grade_one: list[dict] = field(default_factory=list)
    draft: list[dict] = field(default_factory=list)
    low_grade: list[dict] = field(default_factory=list)

    @property
    def student_count(self) -> int:
        return len(self.students)


@dataclass
class ClassReportData:
    """Everything the Class Report renders, already grouped and resolved."""

    course: dict
    groups: list[Group]
    codes: list[AssignmentCode]
    generated_at: str = ""
    total_students: int = 0
    threshold: float | None = DEFAULT_LOW_THRESHOLD
    #: Students assigned to a selected class with NO Google Classroom record
    #: (§19/§31): shown as an administrative note, never mixed into tables.
    classroom_note: list[dict] = field(default_factory=list)

    @property
    def all_missing(self) -> int:
        """Students with at least one OUTSTANDING item (missing/draft/0)."""
        return sum(len(g.missing) for g in self.groups)

    @property
    def all_outstanding(self) -> int:
        """Alias of :attr:`all_missing` - the §31 word for the same truth."""
        return self.all_missing

    @property
    def all_grade_one(self) -> int:
        return sum(len(g.grade_one) for g in self.groups)

    @property
    def all_draft(self) -> int:
        return sum(len(g.draft) for g in self.groups)

    @property
    def all_low_grade(self) -> int:
        return sum(len(g.low_grade) for g in self.groups)

    @property
    def all_submitted(self) -> int:
        """(student x assignment) cells that were actually handed in."""
        return sum(1 for g in self.groups for s in g.students
                   for grade in s.get("grades", []) if grade.get("submitted"))

    @property
    def all_cells(self) -> int:
        return sum(len(s.get("grades", [])) for g in self.groups
                   for s in g.students)

    @property
    def assignment_count(self) -> int:
        return len(self.codes)

    @property
    def class_labels(self) -> list[str]:
        return [g.class_label for g in self.groups]

    def as_dict(self) -> dict:
        return {
            "course": self.course,
            "assignments": [c.as_dict() for c in self.codes],
            "assignment_count": self.assignment_count,
            "total_students": self.total_students,
            "missing_total": self.all_missing,
            "outstanding_total": self.all_outstanding,
            "grade_one_total": self.all_grade_one,
            "draft_total": self.all_draft,
            "low_grade_total": self.all_low_grade,
            "submitted_total": self.all_submitted,
            "cell_total": self.all_cells,
            "threshold": self.threshold,
            "class_labels": self.class_labels,
            "classroom_note": self.classroom_note,
            "groups": [{
                "class_label": g.class_label,
                "student_count": g.student_count,
                "students": g.students,
                "missing": g.missing,
                "grade_one": g.grade_one,
                "draft": g.draft,
                "low_grade": g.low_grade,
            } for g in self.groups],
            "generated_at": self.generated_at,
        }

# ---------------------------------------------------------------------------
# Assembly
# ---------------------------------------------------------------------------


def assemble(course: dict, rows: Sequence[dict],
             coursework_ids: Sequence[str], *,
             class_labels: Sequence[str] | None = None,
             generated_at: str = "",
             threshold: float | None = DEFAULT_LOW_THRESHOLD,
             boundaries=None,
             classroom_note: Sequence[dict] | None = None) -> ClassReportData:
    """Group the selected assignments' rows by the teacher's own classes.

    ``rows`` are the shared report rows (one per student x assignment) and
    must already be limited to ``coursework_ids``. A student appears in a
    class group by the class the TEACHER assigned, which is the same value
    the Students page and every other report uses.

    ``threshold`` is the teacher's low-grade cut-off on the assignment's own
    grading scale (§24); ``None`` turns the low-grade section off entirely.
    """
    wanted = [str(c) for c in coursework_ids if str(c).strip()]
    chosen = {c["id"]: c for c in (course.get("courses") or [])}
    codes = _codes_for(chosen[cid] for cid in wanted if cid in chosen)
    cells = build_cells(rows, codes)

    # One row per student, in the order the API produced them.
    order: list[str] = []
    info: dict[str, dict] = {}
    for row in rows:
        sid = str(row.get("student_id") or "")
        if not sid or sid in info:
            continue
        order.append(sid)
        info[sid] = {
            "student_id": sid,
            "display_name": row.get("display_name")
            or row.get("student_name") or "Student",
            "classroom_name": row.get("student_name"),
            "class_label": row.get("class_label") or "Not assigned",
            "cells": cells.get(sid, {}),
        }

    only = {c.strip() for c in (class_labels or []) if str(c).strip()}
    grouped: dict[str, list[str]] = {}
    for sid in order:
        label = info[sid]["class_label"]
        if only and label not in only:
            continue
        grouped.setdefault(label, []).append(sid)

    groups: list[Group] = []
    for label in sorted(grouped):
        group = Group(class_label=label)
        for sid in grouped[label]:
            entry = info[sid]
            group.students.append({
                "student_id": sid,
                "display_name": entry["display_name"],
                "class_label": label,
                "grades": [
                    {"code": c.code, "label": c.label, "detail": c.detail,
                     "title": c.title, "chapter": c.chapter,
                     "lesson": c.lesson,
                     "grade": cell.grade or "—", "source": cell.grade_source,
                     "grade_label": cell.grade_label,
                     "percentage": cell.pct,
                     "status": cell.status,
                     "submitted": cell.submitted,
                     "is_draft": cell.is_draft,
                     "is_missing": cell.is_missing,
                     "is_outstanding": cell.is_outstanding,
                     "tracking": cell.tracking,
                     "tracking_label": cell.tracking_label}
                    for c in codes
                    if (cell := entry["cells"].get(c.coursework_id)) is not None
                ],
            })

            missing = []
            draft = []
            low_grade = []
            grade_one = []
            for c in codes:
                cell = entry["cells"].get(c.coursework_id)
                if cell is None:
                    continue
                base = {"code": c.code, "label": c.label,
                        "detail": c.detail, "title": c.title,
                        "chapter": c.chapter, "lesson": c.lesson}
                if cell.is_draft:
                    # A draft is its own state (§22) - kept separately below.
                    draft.append({**base, "status": cell.status,
                                  "status_label": "Draft / incomplete",
                                  "tracking": cell.tracking})
                if cell.is_outstanding:
                    # §31/§32: MISSING, draft and grade-0 are ALL outstanding,
                    # each entry keeping the original Classroom state.
                    missing.append({**base,
                                    "status": cell.original_status or cell.status,
                                    "status_label": (cell.tracking_label
                                                     or _status_word(cell.status)),
                                    "tracking": cell.tracking,
                                    "original_grade": cell.original_grade})
                if cell.is_grade_one and cell.tracking in ("", GRADE_ONE_STATUS):
                    grade_one.append({**base, "grade": cell.grade or "1",
                                      "source": cell.grade_source})
                if cell.below(threshold):
                    # §25: the ACTUAL grade, never the word "low".
                    low_grade.append({**base, "grade": cell.grade or "—",
                                      "grade_label": cell.grade_label,
                                      "percentage": cell.pct,
                                      "source": cell.grade_source})
            if draft:
                group.draft.append({"student_id": sid,
                                    "display_name": entry["display_name"],
                                    "class_label": label,
                                    "assignments": draft})
            if missing:
                group.missing.append({"student_id": sid,
                                      "display_name": entry["display_name"],
                                      "class_label": label,
                                      "assignments": missing})
            if low_grade:
                group.low_grade.append({"student_id": sid,
                                        "display_name": entry["display_name"],
                                        "class_label": label,
                                        "assignments": low_grade})
            if grade_one:
                group.grade_one.append({"student_id": sid,
                                        "display_name": entry["display_name"],
                                        "class_label": label,
                                        "assignments": grade_one})
        groups.append(group)

    return ClassReportData(
        course=course, groups=groups, codes=codes, generated_at=generated_at,
        total_students=sum(g.student_count for g in groups),
        threshold=threshold,
        classroom_note=list(classroom_note or []))


# ---------------------------------------------------------------------------
# PDF
# ---------------------------------------------------------------------------


#: The report type controls the title and which sections are printed. Every
#: type is rendered from the SAME model by the SAME renderer (§47).
REPORT_TITLES = {
    "class": "CLASS REPORT",
    "missing": "MISSING HOMEWORK REPORT",
    "assignment": "ASSIGNMENT REPORT",
    "student": "STUDENT REPORT",
    "performance": "PERFORMANCE / GRADE REPORT",
}

#: Every section this renderer can print, in report order (§28).
ALL_SECTIONS = (
    "summary", "assignments", "grades", "low_grade", "grade_one",
    "missing", "draft", "status", "signoff",
)

#: Which sections each report type prints. Types not listed print everything.
REPORT_SECTIONS = {
    "class": ALL_SECTIONS,
    "assignment": ALL_SECTIONS,
    "student": ALL_SECTIONS,
    "performance": ("summary", "assignments", "grades", "low_grade",
                    "grade_one", "status", "signoff"),
    # The missing report pairs OUTSTANDING work with its natural opposite:
    # students who actually scored 1 (§24-§26).
    "missing": ("summary", "assignments", "grade_one", "missing", "draft",
                "signoff"),
}

#: Always present: what was reported on, over which assignments, and who
#: signed it off.
CORE_SECTIONS = ("summary", "assignments", "signoff")

#: The CONSOLIDATED ("Missing only") layout: one row per student, one column
#: of missing assignment codes, and nothing else. The detailed layout above
#: repeats every assignment underneath the same student, which is exactly the
#: duplication a teacher asked to stop seeing. Both come from the same model;
#: only the number of tables the renderer prints changes.
SUMMARY_SECTIONS = {
    "missing": ("summary", "assignments", "missing", "signoff"),
}

#: The teacher's status filters (§17) map onto report sections (§28).
STATUS_SECTIONS = {
    "missing": "missing",
    "not_turned_in": "missing",
    "draft": "draft",
    "low_grade": "low_grade",
    "submitted": "status",
}


def resolve_sections(report_type: str | None,
                     statuses: Sequence[str] | None = None,
                     detailed: bool = True) -> set[str]:
    """Sections to print for this report (§17, §57).

    With no status filter the report type decides. When the teacher ticks
    status filters, those win: the report then contains the core sections
    plus exactly the statuses asked for, so "Missing only" really is missing
    only.

    ``detailed=False`` selects the consolidated layout: the missing-work
    report shrinks to the student-centric list, with no per-assignment detail
    table, no draft block and no grade-1 block bolted onto it. Selecting a
    status explicitly still wins, so ticking "Draft" always shows drafts.
    """
    rtype = (report_type or "class").lower()
    picked: set[str] = set()
    for raw in statuses or []:
        key = str(raw or "").strip().lower()
        if key in STATUS_SECTIONS:
            picked.add(STATUS_SECTIONS[key])
    if not picked:
        if not detailed and rtype in SUMMARY_SECTIONS:
            return set(SUMMARY_SECTIONS[rtype])
        return set(REPORT_SECTIONS.get(rtype, ALL_SECTIONS))
    return set(CORE_SECTIONS) | picked


def scale_note() -> str:
    """The grade scale actually in force, spelled out for the reader.

    A report must never leave a reader guessing what a grade means, and the
    boundaries are configurable, so this reads them from the live grading
    configuration rather than assuming a scale.
    """
    try:
        from ..engine.grading import describe_boundaries
        return describe_boundaries()
    except Exception:  # noqa: BLE001 - a legend must never break a report
        return ""


def _chapters_for(assignments: list[dict]) -> str:
    """'L1 = Chapter 1, Lesson 1; L2 = ...' - a code is never the only clue."""
    seen: set[str] = set()
    parts: list[str] = []
    for a in assignments:
        key = a.get("detail") or a.get("title") or a.get("code", "")
        if key and key not in seen:
            seen.add(key)
            parts.append(f"{a['code']} = {a['detail']}"
                         if a.get("detail") else a.get("label", a["code"]))
    return "; ".join(parts)


def _generated(value: Any) -> str:
    """A human timestamp - never a raw epoch float on an official document."""
    if not value:
        return "-"
    try:
        return datetime.fromtimestamp(float(value)).strftime("%Y-%m-%d %H:%M")
    except (TypeError, ValueError, OverflowError, OSError):
        return safe_generated(value)


def safe_generated(value: Any) -> str:
    return str(value)


def _matrix_cell(entry: dict | None, threshold: float | None) -> tuple[str, str | None]:
    """One grade-matrix cell as a SHORT token plus an optional tone.

    A 20-assignment matrix gives each column ~6mm; a full sentence there
    wraps to five lines and turns an 80-row table into 35 pages. The legend
    under the table spells every token out, so nothing is communicated by
    colour alone (§51) and the report stays readable (§44).
    """
    if entry is None:
        return "-", None
    if entry.get("is_draft"):
        return "DR", "warn"
    if not entry.get("submitted"):
        return "NS", "err"
    grade = str(entry.get("grade") or "")
    if grade in ("", "No grade recorded", "-"):
        return "S", "ok"
    label = entry.get("grade_label")
    if threshold is not None and label:
        try:
            if float(label) < float(threshold):
                return grade, "warn"
        except (TypeError, ValueError):
            pass
    return grade, None


def _flatten_groups(data: "ClassReportData", key: str) -> list[dict]:
    """Concatenate one section across every class, keeping the class label."""
    return [g for group in data.groups for g in (getattr(group, key) or [])]


def render_pdf(data: ClassReportData, brand, *,
               title: str = "CLASS REPORT", label: str = "FOR SIGNATURE",
               focus_missing: bool = False,
               report_type: str = "class",
               statuses: Sequence[str] | None = None,
               detailed: bool = True,
               masthead: bool = True) -> Any:
    """Render the assembled report with the shared design system (§27).

    The document is built from ONE model (``data``) through ONE renderer, so
    the on-screen preview and the exported PDF can never disagree: they are
    two views of exactly these groups, sections and numbers.

    Sections are printed in report order and carry their own empty state when
    there is nothing to show, so a clean class never produces a broken blank
    table (§28, §59).
    """
    from . import design as D

    theme = D.make_theme(accent=brand.accent or None, label="class report")
    styles = D.build_styles(theme)
    story: list = []

    course = data.course or {}
    rtype = (report_type or "class").lower()
    if focus_missing:
        rtype, statuses = "missing", None
    sections = resolve_sections(rtype, statuses, detailed)
    if data.threshold is None:
        # No threshold -> there is no low-grade question to answer (§24).
        sections.discard("low_grade")
    title = title or REPORT_TITLES.get(rtype, "CLASS REPORT")

    class_names = ", ".join(data.class_labels) or "All classes"
    context = (f"{class_names}  -  {data.assignment_count} assignment(s) "
               f"selected  -  {data.total_students} student(s)")
    threshold = data.threshold

    # ---- headline numbers ------------------------------------------------
    D.add(story, D.stat_strip(theme, styles, [
        ("Students", str(data.total_students)),
        ("Assignments", str(data.assignment_count)),
        ("Handed in", f"{data.all_submitted}/{data.all_cells}"),
        ("Outstanding", str(data.all_outstanding)),
        ("Grade 1", str(data.all_grade_one)),
        ("Below threshold", str(data.all_low_grade)),
    ], tone="accent"))

    # ---- administrative note: students with no Classroom record (§19) ----
    if data.classroom_note:
        note = "; ".join(f"{n.get('class_label', '-')} \u2014 "
                         f"{n.get('display_name', '-')}"
                         for n in data.classroom_note)
        D.add(story, D.note(
            theme, styles,
            f"Google Classroom note \u2013 no Classroom record found for: "
            f"{note}."))

    # ---- summary ---------------------------------------------------------
    if "summary" in sections:
        D.add(story, D.section(theme, styles, "SUMMARY",
                               subtitle="Scope of this report and the "
                                        "figures it is built from."))
        D.add(story, D.key_values(theme, styles, [
            ("Classes covered", class_names),
            ("Students", str(data.total_students)),
            ("Assignments selected", str(data.assignment_count)),
            ("Handed in (submissions)", f"{data.all_submitted} of {data.all_cells}"),
            ("Outstanding (missing / draft / 0)", str(data.all_outstanding)),
            ("Students with drafts", str(data.all_draft)),
            ("Students with grade 1", str(data.all_grade_one)),
            ("Students below threshold",
             (f"{data.all_low_grade} (grade < {threshold:g})"
              if threshold is not None else "Threshold disabled")),
            ("Generated", _generated(data.generated_at)),
        ]))

    # ---- the selected assignments key ------------------------------------
    if "assignments" in sections:
        D.add(story, D.section(theme, styles, "ASSIGNMENTS INCLUDED",
                               subtitle="Only the assignments selected for "
                                        "this report are counted."))
        if data.codes:
            D.add(story, D.data_table(
                theme, styles, ["Code", "Chapter / lesson", "Topic"],
                [[c.code, (f"Chapter {c.chapter}, Lesson {c.lesson}"
                           if c.chapter and c.lesson else (c.detail or "-")),
                  c.topic or c.title or "-"] for c in data.codes],
                widths=D.fit_widths(theme, [12, 34, 54]),
                aligns=["left", "left", "left"]))
        else:
            D.add(story, D.empty_state(theme, styles,
                                       "No assignments were selected."))

    # ---- grade overview: one matrix per class ----------------------------
    if "grades" in sections and data.groups:
        D.add(story, D.section(theme, styles, "GRADE OVERVIEW",
                               subtitle="Every selected assignment, by "
                                        "class and student."))
        for group in data.groups:
            D.add(story, D.group_divider(
                theme, styles, f"CLASS {group.class_label}",
                meta=f"{group.student_count} student(s)", tone="gold"))
            header = ["Student"] + [c.code for c in data.codes]
            weights = [30] + [70 / max(1, len(data.codes))] * len(data.codes)
            grade_rows, tones = [], []
            for student in group.students:
                by_code = {g["code"]: g for g in student["grades"]}
                row = [student["display_name"]]
                tone = None
                for code in data.codes:
                    token, cell_tone = _matrix_cell(by_code.get(code.code),
                                                    threshold)
                    row.append(token)
                    tone = tone or cell_tone
                grade_rows.append(row)
                tones.append(tone)
            D.add(story, D.data_table(
                theme, styles, header, grade_rows,
                widths=D.fit_widths(theme, weights),
                row_tones=tones,
                aligns=["left"] + ["center"] * len(data.codes)))
            legend = ("NS = no submission   DR = draft / incomplete   "
                      "S = submitted, not yet marked   - = no data   "
                      + (f"marks below grade {threshold:g} are highlighted."
                         if threshold is not None else
                         "low-grade highlighting is off."))
            scale = scale_note()
            D.add(story, D.note(theme, styles, legend))
            if scale:
                D.add(story, D.note(
                    theme, styles,
                    f"Grade scale used in this report: {scale}."))

    # ---- low-grade students ---------------------------------------------
    if "low_grade" in sections:
        D.add(story, D.section(
            theme, styles, "LOW-GRADE STUDENTS",
            subtitle=(f"Actual grades strictly below grade {threshold:g} "
                      f"on the assignment's own grading scale."
                      if threshold is not None
                      else "The low-grade threshold is disabled.")))
        # §25: the ACTUAL grade and the actual marks - never the word "low".
        rows = [[g["display_name"], g["class_label"],
                 a.get("grade_label") or "-", a.get("grade") or "-",
                 a["label"], a.get("chapter") or "-", a.get("lesson") or "-"]
                for g in _flatten_groups(data, "low_grade")
                for a in g["assignments"]]
        if rows:
            D.add(story, D.data_table(
                theme, styles,
                ["Student Name", "Class", "Grade", "Result",
                 "Assignment", "Chapter", "Lesson"],
                rows,
                widths=D.fit_widths(theme, [21, 8, 8, 11, 24, 14, 14]),
                row_tones=["warn"] * len(rows),
                aligns=["left", "left", "center", "center", "left",
                        "center", "center"]))
        elif threshold is None:
            D.add(story, D.empty_state(
                theme, styles, "No threshold is set, so nothing is "
                               "highlighted as a low grade."))
        else:
            D.add(story, D.empty_state(
                theme, styles,
                f"No student scored below grade {threshold:g} for the "
                "selected assignments."))

    # ---- grade 1 (§24) ---------------------------------------------------
    if "grade_one" in sections:
        D.add(story, D.section(
            theme, styles, "GRADE 1",
            subtitle="Students whose recorded result is exactly 1 - a scored "
                     "outcome, kept strictly separate from outstanding work."))
        grouped = _flatten_groups(data, "grade_one")
        rows = [[g["display_name"], g["class_label"],
                 ", ".join(a["code"] for a in g["assignments"])]
                for g in grouped]
        if rows:
            D.add(story, D.data_table(
                theme, styles,
                ["Student Name", "Class", "Grade 1 Assignments"],
                rows,
                widths=D.fit_widths(theme, [30, 14, 56]),
                row_tones=["warn"] * len(rows),
                aligns=["left", "left", "left"]))
            legend = _chapters_for(
                [a for g in grouped for a in g["assignments"]])
            if legend:
                D.add(story, D.note(theme, styles, legend))
        else:
            D.add(story, D.empty_state(
                theme, styles, "No students received a grade of 1 for the "
                               "selected assignments."))

    # ---- outstanding / missing homework ---------------------------------
    if "missing" in sections:
        D.add(story, D.section(
            theme, styles, "OUTSTANDING / MISSING HOMEWORK",
            subtitle="Work that is still outstanding: never handed in, left "
                     "as a draft, or recorded with a grade of 0. Each "
                     "student appears once."))
        any_missing = False
        for group in data.groups:
            if not group.missing:
                continue
            any_missing = True
            D.add(story, D.group_divider(
                theme, styles, f"CLASS {group.class_label}",
                meta=f"{len(group.missing)} student(s)", tone="gold"))
            # The at-a-glance table (§39): names left, codes centred.
            D.add(story, D.data_table(
                theme, styles, ["Student Name", "Missing Homework"],
                [[m["display_name"],
                  " / ".join(a["code"] for a in m["assignments"])]
                 for m in group.missing],
                widths=D.fit_widths(theme, [38, 62]),
                row_tones=["err"] * len(group.missing),
                aligns=["left", "center"]))
            # The detail table (§26): where each item sits in the syllabus.
            # Consolidated mode stops here - the same student was already
            # named in the table above, and naming them again per assignment
            # is the duplication this mode exists to remove.
            if detailed:
                detail = [[m["display_name"], m["class_label"], a["code"],
                           a.get("chapter") or "-", a.get("lesson") or "-",
                           a.get("status_label") or "Not submitted"]
                          for m in group.missing for a in m["assignments"]]
                D.add(story, D.data_table(
                    theme, styles,
                    ["Student Name", "Class", "Assignment", "Chapter",
                     "Lesson", "Status"],
                    detail,
                    widths=D.fit_widths(theme, [22, 9, 14, 14, 14, 27]),
                    aligns=["left", "left", "center", "center", "center",
                            "left"]))
            legend = _chapters_for(
                [a for m in group.missing for a in m["assignments"]])
            if legend:
                D.add(story, D.note(theme, styles, legend))
        if not any_missing:
            D.add(story, D.empty_state(
                theme, styles,
                "No outstanding homework for the selected assignments."))

    # ---- draft / incomplete ---------------------------------------------
    if "draft" in sections:
        D.add(story, D.section(
            theme, styles, "DRAFT / INCOMPLETE SUBMISSIONS",
            subtitle="Work Google Classroom says was opened but never "
                     "handed in. Drafts are counted as outstanding above, "
                     "and are listed here with their original state."))
        rows = [[g["display_name"], g["class_label"], a["code"], a["label"],
                 a.get("status_label") or "Draft / incomplete"]
                for g in _flatten_groups(data, "draft")
                for a in g["assignments"]]
        if rows:
            D.add(story, D.data_table(
                theme, styles,
                ["Student", "Class", "Assignment", "Lesson", "Status"],
                rows,
                widths=D.fit_widths(theme, [22, 10, 12, 36, 20]),
                row_tones=["warn"] * len(rows),
                aligns=["left", "left", "center", "left", "left"]))
        else:
            D.add(story, D.empty_state(
                theme, styles,
                "No draft submissions for the selected assignments."))

    # ---- student-by-student status --------------------------------------
    if "status" in sections and data.groups:
        D.add(story, D.section(
            theme, styles, "STUDENT-BY-STUDENT STATUS",
            subtitle="Every student, every selected assignment, with the "
                     "recorded status and mark."))
        for group in data.groups:
            D.add(story, D.group_divider(
                theme, styles, f"CLASS {group.class_label}",
                meta=f"{group.student_count} student(s)", tone="gold"))
            rows, tones = [], []
            for student in group.students:
                for grade in student["grades"]:
                    if grade.get("is_draft"):
                        state, tone = "Draft / incomplete", "warn"
                    elif not grade.get("submitted"):
                        state, tone = "Not submitted", "err"
                    elif grade.get("grade_label"):
                        state, tone = "Graded", "ok"
                    else:
                        state, tone = "Submitted", "ok"
                    rows.append([
                        student["display_name"], group.class_label,
                        grade["code"], grade.get("chapter") or "-",
                        grade.get("lesson") or "-", state,
                        grade["grade"] if grade.get("submitted") else "-",
                    ])
                    tones.append(tone)
            if not rows:
                rows = [["No submission rows for this class", "", "", "",
                         "", "", ""]]
                tones = [None]
            D.add(story, D.data_table(
                theme, styles,
                ["Student Name", "Class", "Assignment", "Chapter", "Lesson",
                 "Status", "Grade"],
                rows,
                widths=D.fit_widths(theme, [22, 9, 12, 13, 11, 20, 13]),
                row_tones=tones,
                aligns=["left", "left", "center", "center", "center",
                        "left", "center"]))

    # ---- verification / sign-off ----------------------------------------
    if "signoff" in sections:
        D.add(story, D.section(theme, styles, "VERIFICATION / SIGN-OFF"))
        D.add(story, D.signature_block(
            theme, styles, brand,
            note=("Homework status recorded based on the latest homework "
                  "check. Grades reflect the marks actually recorded for "
                  "each selected assignment."),
            extra=[
                brand.teacher_title or "Mathematics Teacher",
                brand.center or "School",
                f"Academic Year {brand.year}" if brand.year
                else "Academic Year 2026-2027",
            ]))

    # -- hand the finished story to the shared design system (§27) --------
    report = D.Report(
        theme=theme, branding=brand,
        title=title, story=story, context=context,
        label=label, eyebrow="Math Correct",
        masthead=masthead,
        doc_id=(f"MC-CLASS-{course.get('id', '')[:8]}" if course.get('id')
                else ""))
    return report.build()


def preview_model(data: ClassReportData, *, report_type: str = "class",
                  statuses: Sequence[str] | None = None,
                  detailed: bool = True,
                  title: str = "") -> dict:
    """The SAME model the PDF renders, shaped for the on-screen preview.

    The preview is not a second implementation: it reads the identical
    ``ClassReportData`` the PDF consumes and exposes the same sections as
    data, so a teacher can never see one thing on screen and another on
    paper (§45).
    """
    rtype = (report_type or "class").lower()
    sections = [s for s in ALL_SECTIONS
                if s in resolve_sections(rtype, statuses, detailed)]
    if data.threshold is None:
        sections = [s for s in sections if s != "low_grade"]
    groups = [{
        "class_label": g.class_label,
        "student_count": g.student_count,
        "students": g.students,
        "missing": g.missing,
        "draft": g.draft,
        "low_grade": g.low_grade,
        "grade_one": g.grade_one,
    } for g in data.groups]

    return {
        "title": title or REPORT_TITLES.get(rtype, "CLASS REPORT"),
        "report_type": rtype,
        "detailed": bool(detailed),
        "sections": sections,
        "class_names": ", ".join(data.class_labels) or "All classes",
        "threshold": data.threshold,
        "generated_at": data.generated_at,
        "summary": {
            "students": data.total_students,
            "assignments": data.assignment_count,
            "submitted": data.all_submitted,
            "cells": data.all_cells,
            "missing": data.all_missing,
            "outstanding": data.all_outstanding,
            "draft": data.all_draft,
            "low_grade": data.all_low_grade,
            "grade_one": data.all_grade_one,
        },
        "assignments": [c.as_dict() for c in data.codes],
        "classroom_note": data.classroom_note,
        "groups": groups,
    }
