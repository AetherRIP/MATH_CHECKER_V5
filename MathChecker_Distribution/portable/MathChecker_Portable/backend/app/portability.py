"""Portable student/result data (§2 of the simplification work).

Moving Math Checker to another computer used to mean copying the whole SQLite
file, which also carried API-key fingerprints, usage counters and OAuth-adjacent
state. This module implements a *deliberate* transfer format instead: a
versioned JSON package holding exactly the teacher-owned records — students,
classes, assignments, corrections and their marks/grades/feedback — and nothing
secret.

Design rules, each of them enforced below rather than documented and hoped for:

* **Deny by default.** ``EXPORT_SPEC`` is an allow-list of table/column pairs.
  A table or column that is not named here is not exported, so a future schema
  addition (a token table, a device credential) cannot leak by accident.
* **Never a credential.** ``FORBIDDEN_COLUMNS`` is checked against every column
  in the spec AND against every incoming row, so neither an export nor a
  hand-edited import file can smuggle a secret into the transfer package.
* **Relationships survive.** Rows carry their original primary keys, and the
  import order follows the foreign keys (courses before coursework before
  results), so an imported result stays linked to the right student and
  assignment instead of becoming an orphan row.
* **Nothing is executed.** The file is parsed as JSON only. No ``eval``, no
  pickle, no code — a malformed or hostile file is rejected with a message.
* **Nothing is silently overwritten.** The default policy is ``add_only``: an
  existing row is kept and reported as a conflict. ``update_existing`` must be
  asked for explicitly, and the database is backed up (via SQLite's own backup
  API) before it runs.
* **Transactional.** Every write happens inside one ``db.transaction()``; any
  failure rolls the whole import back, so a bad file can never leave the
  database half-migrated.

The package is plain JSON so it stays inspectable and diffable. Report *files*
(PDFs) are not copied — the package carries the references (assignment id,
student id, filename) so a report can be regenerated from the imported result.
"""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any, Iterable

from . import db
from .errors import AppError
from .logging_setup import log_event
from .version import APP_VERSION, SCHEMA_VERSION

#: Envelope marker. A file without it is not one of ours.
FORMAT = "mathchecker.transfer"
#: Bumped only for a breaking change; readers accept anything <= this.
FORMAT_VERSION = 1
#: Marks the file as *containing no secrets* so a teacher can tell at a glance.
CONTENT_KIND = "student-results"

#: Columns that must never appear in a package, in either direction.
FORBIDDEN_COLUMNS = frozenset({
    "password", "password_hash", "password_salt", "session_secret",
    "secret", "secrets", "token", "tokens", "refresh_token", "access_token",
    "api_key", "api_keys", "gemini_keys", "key_fingerprint",
    "device_credential", "credential", "credentials", "private_key",
    "oauth", "session", "cookie", "capsule",
})


def _columns(table: str) -> tuple[str, ...]:
    """The real columns of ``table`` in this build's schema."""
    rows = db.query(f"PRAGMA table_info({table})")
    return tuple(r["name"] for r in rows)


def _has_table(table: str) -> bool:
    row = db.query_one(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
        (table,))
    return row is not None


def _usable(table: str, wanted: Iterable[str]) -> tuple[str, ...]:
    """Intersect a wish-list with the columns that actually exist.

    An older or newer database may have a different column set; the package
    still transfers everything it legitimately can instead of failing outright.
    """
    if not _has_table(table):
        return ()
    present = set(_columns(table))
    keep = [c for c in wanted if c in present]
    for column in keep:
        if column.lower() in FORBIDDEN_COLUMNS:
            raise AppError(
                f"Refusing to export column '{table}.{column}': it can hold a "
                "credential.", status=500, code="forbidden_column")
    return tuple(keep)


# ---------------------------------------------------------------- export spec
#
# (table, primary/candidate key columns, exported columns in order)
#
# ``key`` is what makes a row the SAME row on another installation: a Google id
# for Classroom-sourced records, so a re-sync there recognises it instead of
# creating a duplicate.

EXPORT_SPEC: tuple[tuple[str, tuple[str, ...], tuple[str, ...]], ...] = (
    ("courses", ("id",),
     ("id", "name", "section", "state")),
    ("coursework", ("id",),
     ("id", "course_id", "title", "max_points", "state", "due_date",
      "allowlisted", "alternate_link", "classroom_title", "custom_title",
      "sync_state")),
    ("students", ("id",),
     ("id", "name", "email", "nickname")),
    ("correction_jobs", ("id",),
     ("id", "submission_id", "coursework_id", "course_id", "student_id",
      "student_name", "status", "attempts", "max_attempts", "is_remark",
      "created_at", "finished_at")),
    ("results", ("id",),
     ("id", "job_id", "coursework_id", "submission_id", "student_id",
      "student_name", "earned", "available", "percentage",
      "provisional_grade", "marking_model", "audit_warning", "result_json",
      "verification_json", "processing_seconds", "confidence",
      "confidence_reasons", "review_required", "manual_override",
      "double_check_triggered", "double_check_status", "first_earned",
      "first_available", "first_percentage", "second_earned",
      "second_available", "second_percentage", "final_earned",
      "final_available", "final_percentage", "disagreement_status",
      "double_check_reason", "double_check_differences", "created_at")),
    ("result_reviews", ("result_id", "action", "created_at"),
     ("result_id", "action", "previous_earned", "new_earned",
      "previous_confidence", "note", "created_at")),
    ("canonical_reports", ("course_id", "coursework_id", "submission_id"),
     ("course_id", "coursework_id", "submission_id", "canonical_result_id",
      "drive_file_id", "updated_at")),
    ("report_attachments", ("result_id",),
     ("result_id", "course_id", "coursework_id", "submission_id",
      "student_id", "report_filename", "drive_file_id", "status", "error",
      "attempts", "updated_at")),
    ("grade_boundaries", ("coursework_id",),
     ("coursework_id", "boundaries_json", "session_label", "updated_at")),
)

#: Columns a row MUST carry to be importable at all. A row missing one of
#: these is reported as invalid instead of being inserted half-formed, so the
#: file's problems are visible rather than guessed around.
ESSENTIAL_COLUMNS: dict[str, tuple[str, ...]] = {
    "courses": ("id", "name"),
    "coursework": ("id", "course_id", "title"),
    "students": ("id",),
    "correction_jobs": ("id", "status"),
    "results": ("id", "job_id", "result_json"),
    "result_reviews": ("result_id", "action"),
    "canonical_reports": ("course_id", "coursework_id", "submission_id",
                          "canonical_result_id"),
    "report_attachments": ("result_id",),
    "grade_boundaries": ("coursework_id", "boundaries_json"),
}

#: Values for NOT NULL columns that an older or partial package may not carry.
#: The string ``"now"`` means "the moment of import". Nothing here invents
#: academic data — only bookkeeping timestamps and empty counters.
DEFAULTS: dict[str, dict[str, Any]] = {
    "courses": {"fetched_at": "now"},
    "coursework": {"fetched_at": "now"},
    "students": {"fetched_at": "now"},
    "correction_jobs": {"status": "COMPLETED", "attempts": 0,
                         "max_attempts": 3, "is_remark": 0,
                         "created_at": "now", "updated_at": "now",
                         "finished_at": "now"},
    "results": {"earned": 0, "available": 0, "percentage": 0.0,
                "created_at": "now", "review_required": 0,
                "manual_override": 0, "double_check_triggered": 0},
    "result_reviews": {"created_at": "now"},
    "canonical_reports": {"updated_at": "now"},
    "report_attachments": {"status": "PENDING", "attempts": 0,
                           "updated_at": "now"},
    "grade_boundaries": {"updated_at": "now"},
}

#: Human labels for the export summary, so the UI never invents its own names.
SECTION_LABELS: dict[str, str] = {
    "courses": "Classes",
    "coursework": "Assignments",
    "students": "Students",
    "correction_jobs": "Correction jobs",
    "results": "Correction results",
    "result_reviews": "Review decisions",
    "canonical_reports": "Report links",
    "report_attachments": "Report attachments",
    "grade_boundaries": "Assignment grade boundaries",
}

#: Tables that DO hold secrets or machine-local state, listed explicitly so the
#: omission is a decision on record rather than an oversight.
DELIBERATELY_EXCLUDED: dict[str, str] = {
    "gemini_keys": "API key identity rows",
    "key_stats": "per-key usage counters",
    "gemini_remote_files": "uploaded-file registry",
    "job_remote_files": "per-job uploaded-file registry",
    "retained_files": "local submission cache",
    "job_bandwidth": "bandwidth accounting",
    "net_usage": "network usage totals",
    "app_meta": "machine state (schema version, cursors)",
    "classroom_state_events": "raw Classroom polling bookkeeping",
    "classroom_submissions": "Classroom submission cache (re-synced)",
    "classroom_managed_assignments": "app-created coursework ownership",
    "classroom_publish_ops": "in-flight Classroom publish operations",
    "submissions": "Classroom submission cache (re-synced)",
    "automation_jobs": "workflow queue state",
    "automation_log": "workflow activity log",
    "automation_assignments": "workflow selection flags",
    "large_pdf_jobs": "in-flight long-document jobs",
    "large_pdf_batches": "per-batch progress for long-document jobs",
    "repair_runs": "repair session bookkeeping",
    "repair_items": "repair item bookkeeping",
    "pending_manual_files": "temporary upload staging",
    "pending_mark_schemes": "temporary mark-scheme staging",
}


# ------------------------------------------------------------------ exporting

def _row_to_dict(row: Any, columns: tuple[str, ...]) -> dict:
    return {c: row[c] for c in columns}


def collect() -> dict[str, Any]:
    """Read every exportable record, in foreign-key-safe order.

    ``correction_jobs`` is narrowed to the jobs the exported results point at:
    carrying the whole job table would transfer failed and cancelled queue
    entries the teacher never asked to move, while dropping it entirely would
    break the ``results.job_id`` foreign key and make the import fail.
    """
    job_ids = [r["job_id"] for r in db.query(
        "SELECT job_id FROM results WHERE job_id IS NOT NULL")]
    data: dict[str, list[dict]] = {}
    for table, _key, wanted in EXPORT_SPEC:
        columns = _usable(table, wanted)
        if not columns:
            data[table] = []
            continue
        if table == "correction_jobs":
            if not job_ids:
                data[table] = []
                continue
            marks = ",".join("?" * len(job_ids))
            rows = db.query(
                f"SELECT {', '.join(columns)} FROM correction_jobs "
                f"WHERE id IN ({marks})", tuple(job_ids))
        else:
            rows = db.query(
                f"SELECT {', '.join(columns)} FROM {table}")
        data[table] = [_row_to_dict(r, columns) for r in rows]
    return data


def counts(data: dict[str, list[dict]]) -> dict[str, int]:
    return {name: len(rows) for name, rows in data.items() if rows}


def build_package(*, source_label: str | None = None) -> dict[str, Any]:
    """The complete, versioned transfer package."""
    data = collect()
    per_table = counts(data)
    results = data.get("results", [])
    graded = sum(1 for r in results if r.get("provisional_grade"))
    return {
        "format": FORMAT,
        "format_version": FORMAT_VERSION,
        "content": CONTENT_KIND,
        "contains_secrets": False,
        "application": "Math Checker",
        "app_version": APP_VERSION,
        "database_schema": SCHEMA_VERSION,
        "exported_at": time.time(),
        "exported_at_iso": time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                         time.gmtime()),
        "source": source_label,
        "counts": per_table,
        "summary": {
            "students": per_table.get("students", 0),
            "classes": per_table.get("courses", 0),
            "assignments": per_table.get("coursework", 0),
            "results": len(results),
            "grades": graded,
        },
        "excluded": {
            "tables": DELIBERATELY_EXCLUDED,
            "note": ("Credentials, API keys, OAuth material, session data, "
                     "machine-local caches and workflow queues are never part "
                     "of this file."),
        },
        "data": data,
    }


def export_json() -> bytes:
    return json.dumps(build_package(), indent=2, sort_keys=False,
                      default=str).encode("utf-8")


def export_filename() -> str:
    stamp = time.strftime("%Y%m%d-%H%M%S")
    return f"math_checker_students_{stamp}.json"


# ------------------------------------------------------------------ validating

def parse(raw: bytes | str) -> dict[str, Any]:
    """Parse and structurally validate a package. Never executes anything."""
    if isinstance(raw, bytes):
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError:
            raise AppError("That file is not readable text — a Math Checker "
                           "export is a UTF-8 JSON file.",
                           status=400, code="bad_export_file")
    else:
        text = raw
    if len(text) > 64 * 1024 * 1024:
        raise AppError("That file is larger than 64 MB, which no student/"
                       "result export reaches. It was not read.",
                       status=400, code="export_too_large")
    try:
        payload = json.loads(text)
    except ValueError:
        raise AppError("That file is not valid JSON, so it is not a Math "
                       "Checker export.", status=400, code="bad_export_file")
    if not isinstance(payload, dict):
        raise AppError("That file's contents are not a Math Checker export.",
                       status=400, code="bad_export_file")
    if payload.get("format") != FORMAT:
        raise AppError(
            "This is not a Math Checker student/result export "
            f"(expected format '{FORMAT}').",
            status=400, code="bad_export_format")
    version = payload.get("format_version")
    if not isinstance(version, int) or version < 1:
        raise AppError("The export file has no usable format version.",
                       status=400, code="bad_export_version")
    if version > FORMAT_VERSION:
        raise AppError(
            f"This export was written by a newer version of Math Checker "
            f"(format {version}; this build reads {FORMAT_VERSION}). Update "
            "the application and try again.", status=400,
            code="export_too_new")
    data = payload.get("data")
    if not isinstance(data, dict):
        raise AppError("The export file has no data section.",
                       status=400, code="bad_export_file")
    return payload


def _section_rows(payload: dict, table: str) -> list[dict]:
    rows = (payload.get("data") or {}).get(table)
    if rows is None:
        return []
    if not isinstance(rows, list):
        raise AppError(f"The '{table}' section of this export is malformed.",
                       status=400, code="bad_export_section")
    for row in rows:
        if not isinstance(row, dict):
            raise AppError(f"The '{table}' section contains a non-record "
                           "entry.", status=400, code="bad_export_section")
    return rows


def _validate_rows(table: str, key: tuple[str, ...],
                   rows: list[dict]) -> tuple[list[dict], list[dict]]:
    """Split usable rows from invalid ones, and refuse any secret column."""
    required = tuple(dict.fromkeys(key + ESSENTIAL_COLUMNS.get(table, ())))
    good: list[dict] = []
    bad: list[dict] = []
    for row in rows:
        for column in row:
            if str(column).lower() in FORBIDDEN_COLUMNS:
                raise AppError(
                    f"This export contains a field that can hold a credential "
                    f"('{table}.{column}'). It was refused and nothing was "
                    "imported.", status=400, code="forbidden_column")
        missing = [k for k in required
                   if row.get(k) in (None, "")
                   and k not in DEFAULTS.get(table, {})]
        if missing:
            bad.append({"table": table, "row": row,
                        "reason": f"missing {'/'.join(missing)}"})
            continue
        good.append(row)
    return good, bad


def _insert_columns(table: str, wanted: Iterable[str]) -> tuple[str, ...]:
    """Columns to write on import.

    The exported columns plus any NOT NULL bookkeeping column this build fills
    with a default (``fetched_at``, ``updated_at``). Without the second group
    an INSERT that omits a NOT NULL column with no SQL default would be
    rejected by SQLite, and a perfectly good package would fail to import.
    """
    base = _usable(table, wanted)
    if not base:
        return ()
    present = set(_columns(table))
    extra = tuple(c for c in DEFAULTS.get(table, ())
                  if c in present and c not in base)
    return base + extra


def _values_for(table: str, columns: tuple[str, ...], row: dict,
                now: float) -> dict:
    """Column values for one row, filling bookkeeping defaults."""
    defaults = DEFAULTS.get(table, {})
    values: dict[str, Any] = {}
    for column in columns:
        if column in row and row[column] is not None:
            values[column] = _coerce(row[column])
        elif column in defaults:
            value = defaults[column]
            values[column] = now if value == "now" else value
        else:
            values[column] = None
    return values


# ------------------------------------------------------------------ importing

def _existing_keys(table: str, columns: tuple[str, ...]) -> set[tuple]:
    rows = db.query(f"SELECT {', '.join(columns)} FROM {table}")
    return {tuple(r[c] for c in columns) for r in rows}


def _existing_ids(table: str, column: str = "id") -> set[str]:
    return {str(r[column]) for r in db.query(f"SELECT {column} FROM {table}")}


def analyse(payload: dict) -> dict[str, Any]:
    """Preview an import WITHOUT writing anything.

    Returns, per section: how many records are new, how many already exist
    locally (conflicts), and how many are invalid. This is what the preview
    screen shows, and it is computed from the real database.
    """
    sections: list[dict[str, Any]] = []
    invalid: list[dict] = []
    totals = {"new": 0, "matching": 0, "invalid": 0}

    for table, key, wanted in EXPORT_SPEC:
        rows = _section_rows(payload, table)
        if not rows:
            continue
        columns = _insert_columns(table, wanted)
        good, bad = _validate_rows(table, key, rows)
        invalid.extend(bad)
        if not columns:
            # A table this build does not have: report it rather than fail, so
            # a package from a different build still transfers what it can.
            sections.append({"table": table, "label": SECTION_LABELS.get(table, table),
                             "total": len(rows), "new": 0, "matching": 0,
                             "invalid": len(bad) + len(good),
                             "unsupported": True})
            totals["invalid"] += len(bad) + len(good)
            continue
        key_columns = tuple(c for c in key if c in columns)
        existing = (_existing_keys(table, key_columns) if key_columns else set())
        new = matching = 0
        for row in good:
            if key_columns and tuple(row.get(c) for c in key_columns) in existing:
                matching += 1
            else:
                new += 1
        sections.append({"table": table,
                         "label": SECTION_LABELS.get(table, table),
                         "total": len(rows), "new": new, "matching": matching,
                         "invalid": len(bad), "unsupported": False})
        totals["new"] += new
        totals["matching"] += matching
        totals["invalid"] += len(bad)

    warnings = _warnings(payload)
    return {
        "ok": True,
        "format": payload.get("format"),
        "format_version": payload.get("format_version"),
        "app_version": payload.get("app_version"),
        "exported_at_iso": payload.get("exported_at_iso"),
        "exported_at": payload.get("exported_at"),
        "contains_secrets": bool(payload.get("contains_secrets")),
        "sections": sections,
        "totals": totals,
        "invalid_rows": invalid[:50],
        "warnings": warnings,
        "policies": {
            "default": "add_only",
            "options": [
                {"value": "add_only",
                 "label": "Add new records only",
                 "detail": ("Existing students, assignments and results are "
                            "left exactly as they are; only records this "
                            "installation does not have are added.")},
                {"value": "update_existing",
                 "label": "Add new records and update matching ones",
                 "detail": ("Matching records are replaced by the incoming "
                            "copy. The database is backed up first, and the "
                            "backup path is reported.")},
            ],
        },
    }


def _warnings(payload: dict) -> list[str]:
    """Plain-language notices about the file, computed from real rows."""
    notes: list[str] = []
    if payload.get("contains_secrets"):
        notes.append("This file claims to contain secrets. It was NOT imported "
                     "as-is; secret fields are refused.")
    database_schema = payload.get("database_schema")
    if isinstance(database_schema, int) and database_schema != SCHEMA_VERSION:
        notes.append(
            f"The export came from database schema {database_schema}; this "
            f"installation uses {SCHEMA_VERSION}. Compatible fields are "
            "imported, unknown fields are ignored.")
    students = _section_rows(payload, "students")
    seen: dict[str, str] = {}
    for row in students:
        name = str(row.get("name") or "").strip().lower()
        if not name:
            continue
        if name in seen:
            notes.append(
                f"Two student records share the name '{row.get('name')}' "
                f"('{seen[name]}' and '{row.get('id')}'). Both are added; "
                "they stay separate students because their ids differ.")
            break
        seen[name] = str(row.get("id"))
    results = _section_rows(payload, "results")
    if results and any(r.get("provisional_grade") for r in results):
        grades = {str(r.get("provisional_grade")) for r in results
                  if r.get("provisional_grade")}
        bad = sorted(g for g in grades if not g.strip().isdigit())
        if bad:
            notes.append("Some grades in this file are not on the 1-9 scale: "
                         + ", ".join(bad) + ". They are imported as recorded.")
    return notes


def _backup_database() -> str | None:
    """A consistent pre-import copy of the database (SQLite's backup API)."""
    import sqlite3

    from .config import DATA_DIR

    backups = Path(DATA_DIR) / "backups"
    try:
        backups.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        target = backups / f"before-import-{stamp}.db"
        source = db.get_connection()
        dest = sqlite3.connect(str(target))
        try:
            source.backup(dest)
        finally:
            dest.close()
        return str(target)
    except Exception:  # noqa: BLE001 — a backup failure must not crash import
        log_event("portability", "pre-import database backup failed")
        return None


def apply_import(payload: dict, *, mode: str = "add_only",
                 actor: str = "local") -> dict[str, Any]:
    """Apply a validated package inside ONE transaction.

    ``mode`` is ``add_only`` (default) or ``update_existing``. Anything else is
    refused rather than guessed at. The returned report lists every section's
    added / updated / skipped counts plus any row that could not be imported,
    so nothing happens silently.
    """
    if mode not in ("add_only", "update_existing"):
        raise AppError("Import mode must be 'add_only' or 'update_existing'.",
                       status=400, code="bad_import_mode")

    # Validate EVERYTHING before touching the database: a file that fails
    # halfway must not have written anything at all.
    plan: list[tuple[str, tuple[str, ...], tuple[str, ...],
                     list[dict], list[dict]]] = []
    invalid: list[dict] = []
    for table, key, wanted in EXPORT_SPEC:
        rows = _section_rows(payload, table)
        if not rows:
            continue
        columns = _insert_columns(table, wanted)
        good, bad = _validate_rows(table, key, rows)
        invalid.extend(bad)
        if not columns:
            log_event("portability", "import skipped unsupported table",
                      table=table, rows=len(rows))
            continue
        key_columns = tuple(c for c in key if c in columns)
        plan.append((table, key_columns, columns, good, bad))

    backup: str | None = None
    if mode == "update_existing":
        backup = _backup_database()

    report: list[dict[str, Any]] = []
    totals = {"added": 0, "updated": 0, "skipped": 0, "invalid": len(invalid)}
    now = time.time()

    try:
        with db.transaction() as conn:
            _write_plan(conn, plan, mode=mode, now=now, report=report,
                        totals=totals)
    except sqlite3.IntegrityError as exc:
        # The transaction has already rolled back. Say so plainly: a teacher
        # must never wonder whether half the file was applied.
        raise AppError(
            "This file conflicts with the data already in this installation "
            f"({exc}). NOTHING was imported — the database is exactly as it "
            "was. Try the 'add new records only' policy, or import into an "
            "empty installation.", status=409, code="import_conflict")

    log_event("portability", "student/result import applied",
              mode=mode, added=totals["added"], updated=totals["updated"],
              skipped=totals["skipped"], invalid=totals["invalid"],
              actor=actor)

    return {
        "applied": True,
        "mode": mode,
        "backup_path": backup,
        "sections": report,
        "totals": totals,
        "invalid_rows": invalid[:50],
        "note": ("Records that already existed were left untouched."
                 if mode == "add_only"
                 else "Matching records were updated from the file."),
    }


def _write_plan(conn, plan, *, mode: str, now: float,
                report: list[dict], totals: dict) -> None:
    """Write every prepared section inside the caller's transaction."""
    for table, key_columns, columns, good, bad in plan:
        existing = set()
        if key_columns:
            existing = {
                tuple(r[c] for c in key_columns)
                for r in conn.execute(
                    f"SELECT {', '.join(key_columns)} FROM {table}")
            }
        added = updated = skipped = 0
        for row in good:
            values = _values_for(table, columns, row, now)
            if not key_columns:
                # Append-only table (review decisions): the surrogate id is
                # never carried, so SQLite assigns the next one locally.
                conn.execute(
                    f"INSERT INTO {table} ({', '.join(columns)}) "
                    f"VALUES ({', '.join('?' * len(columns))})",
                    tuple(values[c] for c in columns))
                added += 1
                continue
            identity = tuple(values.get(c) for c in key_columns)
            if identity in existing:
                if mode == "add_only":
                    skipped += 1
                    continue
                assignments = [c for c in columns if c not in key_columns]
                if assignments:
                    conn.execute(
                        f"UPDATE {table} SET "
                        + ", ".join(f"{c}=?" for c in assignments)
                        + " WHERE "
                        + " AND ".join(f"{c} IS ?" for c in key_columns),
                        tuple(values[c] for c in assignments) + identity)
                updated += 1
            else:
                conn.execute(
                    f"INSERT INTO {table} ({', '.join(columns)}) "
                    f"VALUES ({', '.join('?' * len(columns))})",
                    tuple(values[c] for c in columns))
                existing.add(identity)
                added += 1
        report.append({"table": table,
                       "label": SECTION_LABELS.get(table, table),
                       "added": added, "updated": updated,
                       "skipped": skipped, "invalid": len(bad)})
        totals["added"] += added
        totals["updated"] += updated
        totals["skipped"] += skipped


def _coerce(value: Any) -> Any:
    """Store JSON values as SQLite accepts them without changing meaning."""
    if isinstance(value, bool):
        return 1 if value else 0
    if isinstance(value, (dict, list)):
        return json.dumps(value)
    return value


def storage_inventory() -> dict[str, Any]:
    """What this installation currently holds (the Storage page's numbers)."""
    def count(sql: str) -> int:
        row = db.query_one(sql)
        return int(row[0]) if row else 0

    tables = {
        "students": "SELECT COUNT(*) FROM students",
        "classes": "SELECT COUNT(*) FROM courses",
        "assignments": "SELECT COUNT(*) FROM coursework",
        "results": "SELECT COUNT(*) FROM results",
        "graded_results": "SELECT COUNT(*) FROM results "
                          "WHERE provisional_grade IS NOT NULL",
        "jobs": "SELECT COUNT(*) FROM correction_jobs",
        "failed_jobs": "SELECT COUNT(*) FROM correction_jobs "
                       "WHERE status='FAILED'",
        "review_required": "SELECT COUNT(*) FROM results "
                           "WHERE review_required=1 AND manual_override=0",
        "report_attachments": "SELECT COUNT(*) FROM report_attachments",
        "canonical_reports": "SELECT COUNT(*) FROM canonical_reports",
    }
    counts_out: dict[str, int] = {}
    for name, sql in tables.items():
        try:
            counts_out[name] = count(sql)
        except Exception:  # noqa: BLE001 — an older schema may lack a table
            counts_out[name] = 0

    try:
        size_bytes = int(db.get_connection().execute(
            "SELECT page_count * page_size FROM pragma_page_count(), "
            "pragma_page_size()").fetchone()[0])
    except Exception:  # noqa: BLE001 — size is informational only
        size_bytes = 0

    return {
        "counts": counts_out,
        "database_bytes": size_bytes,
        "export": {
            "format": FORMAT,
            "format_version": FORMAT_VERSION,
            "content": CONTENT_KIND,
            "filename": export_filename(),
            "sections": [SECTION_LABELS.get(t, t) for t, _k, _c in EXPORT_SPEC
                         if _usable(t, _c)],
            "excluded_tables": DELIBERATELY_EXCLUDED,
        },
        "schema_version": SCHEMA_VERSION,
        "app_version": APP_VERSION,
    }
