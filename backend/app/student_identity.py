"""The ONE place that decides what a student is called.

This module exists because the name was previously chosen independently in
several places, each with a different fallback: one showed the raw denormalised
snapshot, one showed the internal student id, one showed an em-dash, one showed
"Student". The teacher saw a different identity on different pages, and on the
correction result page sometimes nothing at all.

The rules, once, for everyone:

    display name = nickname  or  Google Classroom name  or  a safe fallback
    class       = the class/grade the teacher assigned, or "Not assigned"

Three properties this module guarantees:

1. **Classroom identity is the source of truth.** ``students.name`` is the
   official Google Classroom name. A nickname is only a local display
   preference and NEVER overwrites it.
2. **Identity is never a name.** Resolution goes through stable Google
   Classroom ids (``student_id`` / ``userId``). Two students can share a
   classroom name, a nickname or even a class; they can never be confused,
   because no lookup here is ever keyed on a display name.
3. **A nickname is display-only.** Every Google Classroom API call keeps using
   the real student/submission/course ids, so a nickname - or a class - can
   never break student matching, sync, grade return or report attachment.

A class is teacher-defined metadata with exactly the same status as a
nickname: local, display-only, and never inferred from a Google Classroom
course name when the teacher has assigned one. The course name is only a
fallback for students who have not been given a class, and only because this
app already shows the Classroom course as the student's "class".

The pipeline is deliberately allowed to carry a *snapshot* name on rows such as
``results.student_name`` (a correction is a historical record), but the
snapshot is only ever a last-resort fallback. The live Classroom identity is
re-resolved whenever a stable id is available.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from . import db

log = logging.getLogger(__name__)

#: Shown only when a student has no Classroom record at all (a manual upload
#: with no Classroom identity). Never an id, never a file name.
FALLBACK_DISPLAY_NAME = "Student"

#: Shown in a report when the teacher has not assigned a class. A blank cell
#: in a printed report reads as a bug; this reads as a decision.
UNASSIGNED_CLASS = "Not assigned"

MAX_NICKNAME_LENGTH = 60
MAX_CLASS_LENGTH = 40

#: Offered as suggestions in the UI. DELIBERATELY NOT a whitelist: a school
#: with "11 Computer Science" or "Year 12" must be able to type it.
SUGGESTED_CLASSES = (
    "9A", "9B", "9C", "10A", "10B", "10C",
    "11A", "11B", "11C", "12A", "12B", "12C",
)

_WS = re.compile(r"\s+")

__all__ = [
    "FALLBACK_DISPLAY_NAME",
    "UNASSIGNED_CLASS",
    "MAX_NICKNAME_LENGTH",
    "MAX_CLASS_LENGTH",
    "SUGGESTED_CLASSES",
    "display_name",
    "normalize_nickname",
    "normalize_class_name",
    "class_label",
    "class_name_for",
    "resolve",
    "decorate",
    "student_id_for_submission",
    "known_classes",
    "backfill_missing_identities",
]


# ------------------------------------------------------------- validation

def _clean(value: Any) -> str:
    """Trim and collapse internal whitespace runs to single spaces.

    A name may legitimately contain spaces, apostrophes, hyphens and
    non-English characters, so nothing is stripped - only pointless
    whitespace and control characters are.
    """
    if value is None:
        return ""
    text = str(value).replace("\x00", "")
    text = "".join(ch for ch in text if ch == "\n" or ch == "\t" or ch >= " ")
    return _WS.sub(" ", text).strip()


def normalize_nickname(value: Any) -> str | None:
    """Clean a nickname for storage, or ``None`` when it is effectively empty.

    Whitespace-only input clears the nickname rather than storing "", which is
    what makes "the teacher typed a space by accident" a no-op instead of a
    student mysteriously displaying as a blank name.
    """
    text = _clean(value)
    return text[:MAX_NICKNAME_LENGTH] or None


def normalize_class_name(value: Any) -> str | None:
    """Clean a class/grade for storage, or ``None`` when it is empty.

    There is no list of "valid" classes: the teacher may use "10A", "11
    Computer Science" or "Year 12", and this accepts all of them.
    """
    text = _clean(value)
    return text[:MAX_CLASS_LENGTH] or None


def class_label(class_name: Any, *, fallback: str = UNASSIGNED_CLASS) -> str:
    """The class as it should READ in a report - never blank."""
    text = _clean(class_name)
    return text or fallback


def display_name(nickname: str | None = None,
                 classroom_name: str | None = None,
                 *, fallback: str | None = None) -> str:
    """The canonical naming rule, as a pure function.

    >>> display_name(None, "Mohamed Ahmed Hassan")
    'Mohamed Ahmed Hassan'
    >>> display_name("Mo", "Mohamed Ahmed Hassan")
    'Mo'
    >>> display_name("  ", "Mo", fallback="x")
    'Mo'
    """
    for candidate in (nickname, classroom_name):
        text = (candidate or "").strip() if isinstance(candidate, str) else ""
        if text:
            return text
    return (fallback or "").strip() or FALLBACK_DISPLAY_NAME


def student_id_for_submission(submission_id: str | None) -> str | None:
    """Recover a student id from a submission id (stable Classroom ids only).

    This is what rescues older rows written before the student id was threaded
    through to the result: a result can still find its student by submission.
    """
    if not submission_id:
        return None
    try:
        row = db.query_one(
            "SELECT student_id FROM classroom_submissions WHERE submission_id = ? "
            "LIMIT 1", (submission_id,))
    except Exception:  # noqa: BLE001 - naming must never break a page
        log.warning("student_identity: submission lookup failed", exc_info=True)
        return None
    if row and row["student_id"]:
        return str(row["student_id"])
    return None


def _student_row(student_id: str) -> Any:
    try:
        return db.query_one(
            "SELECT id, name, email, nickname, class_name FROM students "
            "WHERE id = ?", (student_id,))
    except Exception:  # noqa: BLE001
        log.warning("student_identity: student lookup failed", exc_info=True)
        return None


def _classroom_name_for(student_id: str) -> str | None:
    """The Classroom name from synced submissions, when `students` lacks it."""
    try:
        row = db.query_one(
            "SELECT student_name FROM classroom_submissions "
            "WHERE student_id = ? AND student_name IS NOT NULL "
            "AND student_name != '' LIMIT 1", (student_id,))
    except Exception:  # noqa: BLE001
        return None
    return row["student_name"] if row else None


def class_name_for(student_id: str | None) -> str | None:
    """The teacher-assigned class for one student, or ``None``.

    Deliberately a plain lookup on the stable student id: never a scan for a
    matching name, because two students can be in the same class AND share a
    name.
    """
    if not student_id:
        return None
    row = _student_row(str(student_id))
    if row is None:
        return None
    try:
        return (row["class_name"] or "").strip() or None
    except (IndexError, KeyError, TypeError):
        return None


def known_classes() -> list[str]:
    """Every class the teacher has actually used, for the UI's suggestions.

    The teacher's own vocabulary comes first, because it is the one they
    understand; the standard 9A-12C set is only appended when it has not been
    used yet, and the UI still allows free text either way.
    """
    used: list[str] = []
    try:
        rows = db.query(
            "SELECT DISTINCT class_name FROM students "
            "WHERE class_name IS NOT NULL AND TRIM(class_name) != '' "
            "ORDER BY class_name COLLATE NOCASE")
    except Exception:  # noqa: BLE001 - suggestions must never break a page
        log.warning("student_identity: class list failed", exc_info=True)
        rows = []
    for row in rows:
        name = _clean(row["class_name"])
        if name and name not in used:
            used.append(name)
    for name in SUGGESTED_CLASSES:
        if name not in used:
            used.append(name)
    return used


def resolve(student_id: str | None = None, *,
            submission_id: str | None = None,
            snapshot_name: str | None = None) -> dict:
    """Resolve one student's identity from stable Classroom ids.

    Returns ``{student_id, classroom_name, nickname, class_name, display_name,
    source}``.

    ``source`` says where the name came from, which makes it obvious in a
    response whether a page is showing live Classroom data or a historical
    snapshot. Resolution order:

    1. the student id, either given or recovered from the submission id;
    2. the live Classroom name (``students.name``, then synced submissions);
    3. the nickname stored locally for that student;
    4. the snapshot name carried on the row (historical records);
    5. ``"Student"``.

    The class is resolved from the student id alone and is NEVER derived from
    a nickname or a display name.
    """
    sid = (student_id or "").strip() or None
    if not sid:
        sid = student_id_for_submission(submission_id)

    classroom_name: str | None = None
    nickname: str | None = None
    class_name: str | None = None
    source = "fallback"

    if sid:
        row = _student_row(sid)
        if row:
            classroom_name = (row["name"] or "").strip() or None
            nickname = (row["nickname"] or "").strip() or None
            class_name = (row["class_name"] or "").strip() or None
        if not classroom_name:
            classroom_name = _classroom_name_for(sid)
        if classroom_name or nickname:
            source = "classroom"
        else:
            source = "classroom_id_only"

    snapshot = (snapshot_name or "").strip() if isinstance(snapshot_name, str) else ""
    # The Classroom name always wins over a denormalised snapshot.
    return {
        "student_id": sid,
        "classroom_name": classroom_name,
        "nickname": nickname,
        "class_name": class_name,
        "class_label": class_label(class_name),
        "display_name": display_name(nickname, classroom_name,
                                     fallback=snapshot or None),
        "source": source,
    }


def decorate(row: dict, *, student_key: str = "student_id",
             name_key: str = "student_name",
             submission_key: str = "submission_id") -> dict:
    """Add ``display_name``/``classroom_name``/``nickname`` to an API row.

    The row is returned with ``display_name`` **replacing** nothing: the raw
    ``student_name`` is preserved for callers that need the stored snapshot,
    but every UI should read ``display_name``.
    """
    if row is None:
        return row
    identity = resolve(row.get(student_key),
                       submission_id=row.get(submission_key),
                       snapshot_name=row.get(name_key))
    out = dict(row)
    out["display_name"] = identity["display_name"]
    out["classroom_name"] = identity["classroom_name"]
    out["nickname"] = identity["nickname"]
    out["class_name"] = identity["class_name"]
    out["class_label"] = identity["class_label"]
    out["student_id"] = identity["student_id"]
    out["name_source"] = identity["source"]
    return out


def backfill_missing_identities(*, log_changes: bool = True) -> dict:
    """Repair rows that lost the student id (§15 migration).

    Fills ``results.student_id`` from the synced ``classroom_submissions``
    table via ``submission_id``, and fills a missing ``students.name`` from
    the same source. **No student is ever created or renamed** — only missing
    links are added, keyed on the stable Classroom id, so history, grades and
    reports stay attached to the same student.
    """
    stats = {"results_linked": 0, "results_named": 0, "students_named": 0}

    try:
        # 1. results whose student id was lost, but whose submission is known.
        rows = db.query(
            "SELECT id, submission_id, student_name FROM results "
            "WHERE (student_id IS NULL OR student_id = '') "
            "AND submission_id IS NOT NULL AND submission_id != ''")
        for row in rows:
            sid = student_id_for_submission(row["submission_id"])
            if not sid:
                continue
            db.execute("UPDATE results SET student_id = ? WHERE id = ?",
                       (sid, row["id"]))
            stats["results_linked"] += 1
            if not (row["student_name"] or "").strip():
                live = resolve(sid)["classroom_name"]
                if live:
                    db.execute("UPDATE results SET student_name = ? WHERE id = ?",
                               (live, row["id"]))
                    stats["results_named"] += 1

        # 2. students synced from Classroom submissions with no official name.
        rows = db.query(
            "SELECT DISTINCT cs.student_id, cs.student_name FROM classroom_submissions cs "
            "JOIN students s ON s.id = cs.student_id "
            "WHERE (s.name IS NULL OR s.name = '') "
            "AND cs.student_name IS NOT NULL AND cs.student_name != ''")
        for row in rows:
            db.execute("UPDATE students SET name = ? WHERE id = ?",
                       (row["student_name"], row["student_id"]))
            stats["students_named"] += 1
    except Exception:  # noqa: BLE001 - a migration must never break startup
        log.warning("student_identity: backfill failed", exc_info=True)
        return stats

    if log_changes and any(stats.values()):
        log.info("student_identity: backfilled %s", stats)
    return stats
