"""SQLite persistence layer (WAL mode, schema migrations, safe helpers)."""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from .config import DATA_DIR
from .lifecycle import activity

DB_PATH = DATA_DIR / "app.db"
_SCHEMA = Path(__file__).parent / "schema.sql"

_local = threading.local()
_conn_lock = threading.Lock()
# Every connection this process has opened, so a compile-time operation such as
# a factory reset can release file locks on EVERY thread (Windows refuses to
# delete a database file another connection still holds open).
_connections: set[sqlite3.Connection] = set()
_connections_lock = threading.Lock()
# Bumped by close_all(). Each thread's cached connection remembers the
# generation it was opened in, so after a reset a later request on ANY thread
# silently reopens a fresh handle instead of reusing one that was just closed
# (that reuse used to raise "Cannot operate on a closed database").
_generation = 0
# Guards a factory reset against every other DB operation. A reset holds this
# for its whole delete-and-recreate window, so no thread can re-open app.db
# while it is being removed (on Windows a handle re-opened mid-delete kept the
# old database — and its data — in place). Reentrant: transaction()/execute()
# take it, then call get_connection(), which takes it again on the same thread.
_gate = threading.RLock()


def _new_connection() -> sqlite3.Connection:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(
        DB_PATH,
        timeout=30,
        check_same_thread=False,
        isolation_level=None,  # autocommit; explicit transactions via helper
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def get_connection() -> sqlite3.Connection:
    with _gate:
        conn = getattr(_local, "conn", None)
        if conn is not None and getattr(_local, "gen", None) != _generation:
            # close_all() ran on another thread: this thread's cached handle
            # belongs to the database generation that was just replaced. Drop
            # it and reconnect to the fresh database on next use.
            conn = None
            _local.conn = None
        if conn is None:
            conn = _new_connection()
            _local.conn = conn
            _local.gen = _generation
            with _connections_lock:
                _connections.add(conn)
        return conn


def close_all() -> None:
    """Close every connection this process opened (all threads).

    Used before a factory reset deletes the database; SQLite file locks must
    be released first or Windows will refuse the delete (and the running app
    must not keep writing into a database it just deleted).

    Only the calling thread's thread-local can be cleared directly, so the
    generation counter is bumped: every other thread notices on its next
    ``get_connection()`` that its cached handle is stale and transparently
    opens a fresh one. Without this, worker/threadpool threads kept using the
    closed handle and every later request 500'd.
    """
    global _generation
    with _connections_lock:
        conns = list(_connections)
        _connections.clear()
        _generation += 1
    for conn in conns:
        try:
            conn.close()
        except Exception:
            pass
    if getattr(_local, "conn", None) is not None:
        _local.conn = None


@contextmanager
def transaction() -> Iterator[sqlite3.Connection]:
    """Serialized write transaction (SQLite = one writer at a time).

    Registered as a critical operation for the server's lifecycle: a write
    transaction is the one database state that must not be interrupted, so an
    idle shutdown waits while one is open (see ``app.lifecycle.activity``).
    Single-statement ``execute`` calls are deliberately NOT counted: SQLite
    applies one statement atomically, so it can never be left half-done, and
    counting them would mean a shutdown could always find "something" running.
    """
    with _gate:
        conn = get_connection()
        with _conn_lock:
            conn.execute("BEGIN IMMEDIATE")
            with activity.critical("db_write"):
                try:
                    yield conn
                    conn.execute("COMMIT")
                except Exception:
                    conn.execute("ROLLBACK")
                    raise


def query(sql: str, params: tuple | list = ()) -> list[sqlite3.Row]:
    with _gate:
        return get_connection().execute(sql, params).fetchall()


def query_one(sql: str, params: tuple | list = ()) -> sqlite3.Row | None:
    with _gate:
        return get_connection().execute(sql, params).fetchone()


def execute(sql: str, params: tuple | list = ()) -> sqlite3.Cursor:
    with _gate:
        with _conn_lock:
            return get_connection().execute(sql, params)


@contextmanager
def reset_exclusive() -> Iterator[None]:
    """Hold exclusive DB access for a factory reset.

    Closes every connection and blocks every other thread's query / execute /
    transaction until the reset has deleted and recreated the database, so a
    concurrent request can neither read the half-deleted database nor re-open
    its file handle mid-delete (Windows would then refuse the delete and the
    old data would survive the "reset"). The caller does the file removal and
    ``init_db()`` inside this block; the generation counter makes every thread
    reconnect to the fresh database on its next use.
    """
    with _gate:
        close_all()
        yield


def init_db() -> None:
    with _gate:
        conn = get_connection()
        conn.executescript(_SCHEMA.read_text(encoding="utf-8"))
        _migrate(conn)
        log_ready()


def _migrate(conn: sqlite3.Connection) -> None:
    """Column migrations (idempotent) — CREATE TABLE IF NOT EXISTS cannot add
    columns to databases created by earlier V5 builds."""
    existing = {row[1] for row in conn.execute("PRAGMA table_info(correction_jobs)")}
    if "worker_id" not in existing:
        conn.execute("ALTER TABLE correction_jobs ADD COLUMN worker_id TEXT")

    # Classroom assignment lifecycle (§ deletion sync). When an assignment is
    # deleted in Google Classroom it is ARCHIVED here, never hard-deleted, so
    # historical corrections/grades/reports survive. ``classroom_title`` is
    # Google's own name; ``custom_title`` is the teacher's Math Correct display
    # name and always wins, so a resync can never wipe it.
    cw_cols = {row[1] for row in conn.execute("PRAGMA table_info(coursework)")}
    if cw_cols:
        for col, ddl in (
            ("classroom_title", "TEXT"),
            ("custom_title", "TEXT"),
            ("sync_state", "TEXT NOT NULL DEFAULT 'ACTIVE'"),
            ("deleted_detected_at", "REAL"),
            ("archived_at", "REAL"),
        ):
            if col not in cw_cols:
                conn.execute(f"ALTER TABLE coursework ADD COLUMN {col} {ddl}")
        # Backfill Google's title for rows created before this column existed.
        conn.execute("UPDATE coursework SET classroom_title = title "
                     "WHERE classroom_title IS NULL")

    result_cols = {row[1] for row in conn.execute("PRAGMA table_info(results)")}
    if result_cols and "key_fingerprint" not in result_cols:
        conn.execute("ALTER TABLE results ADD COLUMN key_fingerprint TEXT")
    if result_cols and "confidence" not in result_cols:
        conn.execute("ALTER TABLE results ADD COLUMN confidence TEXT")
    if result_cols and "confidence_reasons" not in result_cols:
        conn.execute("ALTER TABLE results ADD COLUMN confidence_reasons TEXT")
    if result_cols and "review_required" not in result_cols:
        conn.execute("ALTER TABLE results ADD COLUMN review_required INTEGER NOT NULL DEFAULT 0")
    if result_cols and "manual_override" not in result_cols:
        conn.execute("ALTER TABLE results ADD COLUMN manual_override INTEGER NOT NULL DEFAULT 0")

    # High-score double-check columns (independent verification of
    # suspiciously high corrections). earned/available/percentage keep their
    # meaning (the FINAL result); the first/second/final raw breakdowns are
    # stored separately so both corrections are always preserved.
    for col, ddl in (
        ("double_check_triggered", "INTEGER NOT NULL DEFAULT 0"),
        ("double_check_status", "TEXT"),
        ("first_earned", "INTEGER"),
        ("first_available", "INTEGER"),
        ("first_percentage", "REAL"),
        ("second_earned", "INTEGER"),
        ("second_available", "INTEGER"),
        ("second_percentage", "REAL"),
        ("final_earned", "INTEGER"),
        ("final_available", "INTEGER"),
        ("final_percentage", "REAL"),
        ("disagreement_status", "TEXT"),
        ("first_correction_id", "TEXT"),
        ("second_correction_id", "TEXT"),
        ("double_check_reason", "TEXT"),
        ("double_check_differences", "TEXT"),
    ):
        if result_cols and col not in result_cols:
            conn.execute(f"ALTER TABLE results ADD COLUMN {col} {ddl}")

    # Teacher-assigned class / grade (§1 of the nickname+class work).
    #
    # This is LOCAL METADATA on the student profile, exactly like the nickname:
    # it is never written to Google, never part of any Classroom request, and
    # never used to match a student. A Classroom resync touches students.name
    # and nothing else, so the teacher's class survives every sync.
    student_cols = {row[1] for row in conn.execute("PRAGMA table_info(students)")}
    if student_cols and "class_name" not in student_cols:
        conn.execute("ALTER TABLE students ADD COLUMN class_name TEXT")

    # Temporary-Drive-file lifecycle (§§5-§17). ``status`` keeps the
    # teacher-facing attachment outcome; these columns track the *file*
    # lifecycle so cleanup can be retried without re-uploading or re-marking,
    # and so a file can be proven to be ours before we ever delete it.
    ra_cols = {row[1] for row in conn.execute("PRAGMA table_info(report_attachments)")}
    if ra_cols:
        for col, ddl in (
            ("lifecycle", "TEXT"),
            ("owner_email", "TEXT"),
            ("is_temp", "INTEGER NOT NULL DEFAULT 1"),
            ("temp_token", "TEXT"),
            ("cleanup_attempts", "INTEGER NOT NULL DEFAULT 0"),
            ("cleanup_error", "TEXT"),
            ("created_at", "REAL"),
            ("content_bytes", "BLOB"),
            ("previous_drive_file_id", "TEXT"),
            # Identifies WHICH correction was already attached, so a repeated
            # attach of the same report is a no-op instead of a second Drive
            # file (§14). A filename can never do this job.
            ("report_hash", "TEXT"),
        ):
            if col not in ra_cols:
                conn.execute(f"ALTER TABLE report_attachments ADD COLUMN {col} {ddl}")

    student_cols = {row[1] for row in conn.execute("PRAGMA table_info(students)")}
    if student_cols and "nickname" not in student_cols:
        # Local-only nickname: never overwrites the official Classroom name.
        conn.execute("ALTER TABLE students ADD COLUMN nickname TEXT")

    # Remark support (§Remark): a re-mark is recorded as a PENDING attempt that
    # the teacher must accept, instead of silently replacing a valid result.
    job_cols = {row[1] for row in conn.execute("PRAGMA table_info(correction_jobs)")}
    if job_cols and "is_remark" not in job_cols:
        conn.execute(
            "ALTER TABLE correction_jobs ADD COLUMN is_remark INTEGER NOT NULL DEFAULT 0")
    # Large-PDF targeted recovery rounds (bounded, restart-safe): the missing-
    # question retry progress lives here, so a restart or repeated finalize
    # never re-runs a round that already ran (idempotency for the AI budget).
    lp_cols = {row[1] for row in conn.execute("PRAGMA table_info(large_pdf_jobs)")}
    if lp_cols and "recovery_json" not in lp_cols:
        conn.execute("ALTER TABLE large_pdf_jobs ADD COLUMN recovery_json TEXT")
    version_cols = {row[1] for row in conn.execute("PRAGMA table_info(result_versions)")}
    if version_cols and "pending" not in version_cols:
        conn.execute(
            "ALTER TABLE result_versions ADD COLUMN pending INTEGER NOT NULL DEFAULT 0")

    # Durable automation clear/bulk upload structures for existing databases.
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS bulk_upload_claims (
            result_id TEXT PRIMARY KEY REFERENCES results(id) ON DELETE CASCADE,
            run_id TEXT NOT NULL, lease_until REAL NOT NULL, updated_at REAL NOT NULL);
        CREATE INDEX IF NOT EXISTS idx_bulk_upload_claims_run ON bulk_upload_claims(run_id);
        CREATE TABLE IF NOT EXISTS report_upload_records (
            result_id TEXT PRIMARY KEY REFERENCES results(id) ON DELETE CASCADE,
            coursework_id TEXT NOT NULL, submission_id TEXT NOT NULL, student_id TEXT,
            report_hash TEXT NOT NULL, status TEXT NOT NULL, remote_file_id TEXT,
            lease_until REAL, error TEXT, updated_at REAL NOT NULL);
        CREATE INDEX IF NOT EXISTS idx_report_upload_records_identity
            ON report_upload_records(coursework_id, submission_id, student_id, report_hash);
        CREATE TABLE IF NOT EXISTS automation_bulk_runs (
            id TEXT PRIMARY KEY, status TEXT NOT NULL, total INTEGER NOT NULL,
            completed INTEGER NOT NULL DEFAULT 0, failed INTEGER NOT NULL DEFAULT 0,
            created_at REAL NOT NULL, updated_at REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS automation_bulk_run_items (
            run_id TEXT NOT NULL REFERENCES automation_bulk_runs(id) ON DELETE CASCADE,
            result_id TEXT NOT NULL, status TEXT NOT NULL, grade_status TEXT,
            report_status TEXT, message TEXT, updated_at REAL NOT NULL,
            PRIMARY KEY(run_id, result_id));
        CREATE INDEX IF NOT EXISTS idx_automation_bulk_run_items_run
            ON automation_bulk_run_items(run_id, status);
    """)

    upload_lock_cols = {row[1] for row in conn.execute(
        "PRAGMA table_info(report_upload_locks)")}
    if not upload_lock_cols:
        conn.execute("""CREATE TABLE IF NOT EXISTS report_upload_locks (
            coursework_id TEXT NOT NULL, submission_id TEXT NOT NULL,
            owner TEXT NOT NULL, lease_until REAL NOT NULL, updated_at REAL NOT NULL,
            PRIMARY KEY(coursework_id, submission_id))""")

    automation_cols = {row[1] for row in conn.execute(
        "PRAGMA table_info(automation_jobs)")}
    if automation_cols and "hidden_at" not in automation_cols:
        conn.execute("ALTER TABLE automation_jobs ADD COLUMN hidden_at REAL")

    # One-active-per-submission: the pool leaves jobs in 'STARTING' (and the
    # recovery set includes PREPARING..RETRYING) while work runs; the older
    # index definition did not cover 'STARTING', so two enqueues of the same
    # submission could race during a run. Recreate the index so the UNIQUE
    # constraint covers every in-flight status (idempotent: only the stored
    # index definition changes).
    existing = {
        r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='correction_jobs'").fetchall()
    }
    if 'idx_jobs_one_active_per_submission' in existing:
        conn.execute("DROP INDEX IF EXISTS idx_jobs_one_active_per_submission")
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_jobs_one_active_per_submission "
        "ON correction_jobs(submission_id) "
        "WHERE submission_id IS NOT NULL AND status IN ('QUEUED','STARTING','PREPARING',"
        " 'DOWNLOADING','UPLOADING','MARKING','VERIFYING','SAVING',"
        " 'WAITING_FOR_API','RETRYING')",
    )

    _check_schema_version(conn)

    # Student identity (§15): repair rows that lost the student id so the
    # canonical display name can be resolved everywhere. Runs on every start;
    # it is idempotent and only ever ADDS links, never renames or duplicates a
    # student. Imported lazily to keep this module import-cheap.
    try:
        from .student_identity import backfill_missing_identities
        backfill_missing_identities()
    except Exception:  # noqa: BLE001 - a naming repair must never block startup
        logging.getLogger(__name__).warning(
            "student identity backfill skipped", exc_info=True)


def _check_schema_version(conn: sqlite3.Connection) -> None:
    """Record the schema version, and refuse to silently downgrade (§45).

    The data on disk must never be destroyed by running an older build on a
    newer database. If the stored version is NEWER than this build knows
    about, we log loudly and leave the data alone rather than migrating
    backwards; the app still runs, but the mismatch is visible in logs and
    on the diagnostics surface. If it is older, the column migrations above
    have already brought it forward, so we just update the recorded version.
    """
    from .version import SCHEMA_VERSION
    from .logging_setup import log_event

    row = conn.execute(
        "SELECT value FROM app_meta WHERE key = 'schema_version'").fetchone()
    stored = None
    if row is not None:
        try:
            stored = int(row[0])
        except (TypeError, ValueError):
            stored = None

    if stored is not None and stored > SCHEMA_VERSION:
        log_event(
            "db", "database schema is NEWER than this build — not migrating backwards",
            level=logging.WARNING, stored_schema=stored, build_schema=SCHEMA_VERSION,
        )
        return

    if stored != SCHEMA_VERSION:
        conn.execute(
            "INSERT INTO app_meta(key, value) VALUES('schema_version', ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (str(SCHEMA_VERSION),),
        )
        log_event("db", "schema version recorded",
                  previous=stored, schema=SCHEMA_VERSION)




def log_ready() -> None:
    from .logging_setup import log_event
    log_event("db", "database ready", db=str(DB_PATH.name))


def get_meta(key: str, default: str | None = None) -> str | None:
    row = query_one("SELECT value FROM app_meta WHERE key = ?", (key,))
    return row["value"] if row else default


def set_meta(key: str, value: str) -> None:
    execute(
        "INSERT INTO app_meta(key, value) VALUES(?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


def now() -> float:
    return time.time()


def row_to_dict(row: sqlite3.Row | None) -> dict | None:
    return dict(row) if row is not None else None


def loads(raw: str | None, default: Any = None) -> Any:
    if not raw:
        return default
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return default
