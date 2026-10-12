"""Schools, classes and Classroom mappings (§4, §5, §11, §12, §53).

Class names are NOT globally unique: uniqueness is (school_id, name) and every
entity carries a stable internal ID. Classroom IDs remain the primary keys in
the mapping tables so renames/deletions in Classroom never lose history (§58).
"""
from __future__ import annotations

import time
import uuid

from .. import db
from . import accounts


# -- schools --------------------------------------------------------------------

def _ready() -> None:
    import app.distributed.accounts as _acc
    _acc.ensure_tables_ready()


def create_school(name: str, *, code: str | None = None,
                  created_by: str | None = None) -> dict:
    _ready()
    name = (name or "").strip()[:120]
    if not name:
        raise ValueError("school name is required")
    school_id = uuid.uuid4().hex
    now = time.time()
    db.execute(
        """INSERT INTO schools(id, name, code, active, created_by, created_at,
           updated_at) VALUES(?,?,?,1,?,?,?)""",
        (school_id, name, (code or "").strip()[:32] or None, created_by, now, now))
    from . import audit
    audit.record(action="school.created", target_type="school", target_id=school_id,
                 details={"name": name})
    return get_school(school_id)


def get_school(school_id: str) -> dict | None:
    _ready()
    row = db.query_one("SELECT * FROM schools WHERE id = ?", (school_id,))
    return dict(row) if row else None


def list_schools(*, include_inactive: bool = False) -> list[dict]:
    sql = "SELECT * FROM schools"
    if not include_inactive:
        sql += " WHERE active = 1"
    rows = db.query(sql + " ORDER BY name")
    return [dict(r) for r in rows]


def rename_school(school_id: str, name: str) -> None:
    name = (name or "").strip()[:120]
    if not name:
        raise ValueError("school name is required")
    db.execute("UPDATE schools SET name = ?, updated_at = ? WHERE id = ?",
               (name, time.time(), school_id))
    from . import audit
    audit.record(action="school.renamed", target_type="school", target_id=school_id,
                 details={"name": name})


def set_school_active(school_id: str, active: bool) -> None:
    db.execute("UPDATE schools SET active = ?, updated_at = ? WHERE id = ?",
               (1 if active else 0, time.time(), school_id))


def assign_account(account_id: str, school_id: str,
                   class_id: str | None = None) -> None:
    """Attach an Assistant/Co-Teacher to a school (optionally one class)."""
    if not get_school(school_id):
        raise ValueError("school not found")
    account = accounts.get_account(account_id)
    if not account:
        raise ValueError("account not found")
    existing = db.query(
        "SELECT class_id FROM account_scope WHERE account_id = ? AND school_id = ?",
        (account_id, school_id))
    if class_id is None and existing:
        # Whole-school scope wins; drop the per-class rows for that school.
        db.execute("DELETE FROM account_scope WHERE account_id = ? AND school_id = ?",
                   (account_id, school_id))
    db.execute(
        "INSERT OR IGNORE INTO account_scope(account_id, school_id, class_id)"
        " VALUES(?,?,?)", (account_id, school_id, class_id))


def unassign_account(account_id: str, school_id: str,
                     class_id: str | None = None) -> None:
    if class_id:
        db.execute("DELETE FROM account_scope WHERE account_id = ? AND school_id = ?"
                   " AND class_id = ?", (account_id, school_id, class_id))
    else:
        db.execute("DELETE FROM account_scope WHERE account_id = ? AND school_id = ?",
                   (account_id, school_id))


def school_assignments(school_id: str) -> list[dict]:
    rows = db.query(
        """SELECT s.account_id, a.username, a.display_name, a.role,
                  s.class_id, c.name AS class_name
           FROM account_scope s JOIN accounts a ON a.id = s.account_id
           LEFT JOIN classes c ON c.id = s.class_id
           WHERE s.school_id = ?""", (school_id,))
    return [dict(r) for r in rows]


# -- classes --------------------------------------------------------------------

def create_class(school_id: str, name: str, *,
                 academic_year: str | None = None) -> dict:
    name = (name or "").strip()[:80]
    if not name:
        raise ValueError("class name is required")
    if not get_school(school_id):
        raise ValueError("school not found")
    if db.query_one("SELECT 1 FROM classes WHERE school_id = ? AND name = ?",
                    (school_id, name)):
        raise ValueError("class name already exists in this school")
    class_id = uuid.uuid4().hex
    now = time.time()
    db.execute(
        """INSERT INTO classes(id, school_id, name, academic_year, active,
           created_at, updated_at) VALUES(?,?,?,?,1,?,?)""",
        (class_id, school_id, name, (academic_year or "").strip()[:32] or None,
         now, now))
    from . import audit
    audit.record(action="class.created", target_type="class", target_id=class_id,
                 details={"school_id": school_id, "name": name})
    return get_class(class_id)


def get_class(class_id: str) -> dict | None:
    _ready()
    row = db.query_one("SELECT * FROM classes WHERE id = ?", (class_id,))
    return dict(row) if row else None


def list_classes(school_id: str | None = None, *,
                 include_inactive: bool = False) -> list[dict]:
    sql = "SELECT * FROM classes"
    clauses, params = [], []
    if school_id:
        clauses.append("school_id = ?")
        params.append(school_id)
    if not include_inactive:
        clauses.append("active = 1")
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    rows = db.query(sql + " ORDER BY school_id, name", tuple(params))
    return [dict(r) for r in rows]


def rename_class(class_id: str, name: str) -> None:
    name = (name or "").strip()[:80]
    cls = get_class(class_id)
    if not cls:
        raise ValueError("class not found")
    if db.query_one("SELECT 1 FROM classes WHERE school_id = ? AND name = ? AND id != ?",
                    (cls["school_id"], name, class_id)):
        raise ValueError("class name already exists in this school")
    db.execute("UPDATE classes SET name = ?, updated_at = ? WHERE id = ?",
               (name, time.time(), class_id))


def set_class_active(class_id: str, active: bool) -> None:
    db.execute("UPDATE classes SET active = ?, updated_at = ? WHERE id = ?",
               (1 if active else 0, time.time(), class_id))


# -- Classroom mapping (§12/§58) ------------------------------------------------

def map_course(course_id: str, school_id: str, class_id: str) -> dict:
    cls = get_class(class_id)
    if not cls or cls["school_id"] != school_id:
        raise ValueError("class does not belong to that school")
    db.execute(
        """INSERT INTO classroom_mappings(course_id, school_id, class_id, updated_at)
           VALUES(?,?,?,?)
           ON CONFLICT(course_id) DO UPDATE SET school_id = excluded.school_id,
             class_id = excluded.class_id, updated_at = excluded.updated_at""",
        (course_id, school_id, class_id, time.time()))
    from . import audit
    audit.record(action="classroom.course_mapped", target_type="course",
                 target_id=course_id,
                 details={"school_id": school_id, "class_id": class_id})
    return get_course_mapping(course_id)


def get_course_mapping(course_id: str) -> dict | None:
    row = db.query_one("SELECT * FROM classroom_mappings WHERE course_id = ?",
                       (course_id,))
    return dict(row) if row else None


def map_assignment(coursework_id: str, school_id: str, class_id: str, *,
                   assigned_worker_id: str | None = None,
                   correction_config: str | None = None) -> dict:
    """Map a Classroom coursework to School/Class (+ assigned worker).

    Classroom ownership (§11) is unaffected: this only records which
    correction worker is responsible for processing it.
    """
    cls = get_class(class_id)
    if not cls or cls["school_id"] != school_id:
        raise ValueError("class does not belong to that school")
    db.execute(
        """INSERT INTO assignment_mappings(coursework_id, class_id, school_id,
           assigned_worker_id, correction_config, updated_at)
           VALUES(?,?,?,?,?,?)
           ON CONFLICT(coursework_id) DO UPDATE SET class_id = excluded.class_id,
             school_id = excluded.school_id,
             assigned_worker_id = excluded.assigned_worker_id,
             correction_config = excluded.correction_config,
             updated_at = excluded.updated_at""",
        (coursework_id, class_id, school_id, assigned_worker_id,
         correction_config, time.time()))
    from . import audit
    audit.record(action="classroom.assignment_mapped", target_type="coursework",
                 target_id=coursework_id,
                 details={"school_id": school_id, "class_id": class_id})
    row = db.query_one("SELECT * FROM assignment_mappings WHERE coursework_id = ?",
                       (coursework_id,))
    return dict(row) if row else None


def get_assignment_mapping(coursework_id: str) -> dict | None:
    row = db.query_one("SELECT * FROM assignment_mappings WHERE coursework_id = ?",
                       (coursework_id,))
    return dict(row) if row else None
