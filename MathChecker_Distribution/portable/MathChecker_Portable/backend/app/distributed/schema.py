"""Distributed schema: created with CREATE TABLE IF NOT EXISTS at startup
(kept out of schema.sql so the existing single-machine database is upgraded
in place, additively, with no destructive migration)."""
from __future__ import annotations

TABLES = (
    # -- organisations (§4/§5) -------------------------------------------------
    """
    CREATE TABLE IF NOT EXISTS schools (
        id TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        code TEXT,
        active INTEGER NOT NULL DEFAULT 1,
        created_by TEXT,
        created_at REAL NOT NULL,
        updated_at REAL NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS classes (
        id TEXT PRIMARY KEY,
        school_id TEXT NOT NULL REFERENCES schools(id) ON DELETE CASCADE,
        name TEXT NOT NULL,
        academic_year TEXT,
        active INTEGER NOT NULL DEFAULT 1,
        created_at REAL NOT NULL,
        updated_at REAL NOT NULL,
        UNIQUE(school_id, name)
    )
    """,
    # -- accounts (§6-§10) ------------------------------------------------------
    """
    CREATE TABLE IF NOT EXISTS accounts (
        id TEXT PRIMARY KEY,
        username TEXT NOT NULL UNIQUE,
        display_name TEXT NOT NULL,
        password_salt TEXT NOT NULL,
        password_hash TEXT NOT NULL,
        role TEXT NOT NULL CHECK(role IN
            ('DEVELOPER','TEACHER','CO_TEACHER','ASSISTANT')),
        active INTEGER NOT NULL DEFAULT 1,
        permissions TEXT,
        created_by TEXT,
        created_at REAL NOT NULL,
        last_login REAL,
        last_device_id TEXT,
        -- MULTI-ROLE (§6-§10): a JSON array of every role the account holds,
        -- e.g. ["ASSISTANT","DEVELOPER"]. `role` stays as the PRIMARY role so
        -- existing queries, ordering and CHECK constraints keep working; an
        -- account created before this column simply has roles = NULL and is
        -- read as [role]. One person can therefore assist AND be a developer
        -- without a second login.
        roles TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS account_scope (
        account_id TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
        school_id TEXT NOT NULL REFERENCES schools(id) ON DELETE CASCADE,
        class_id TEXT REFERENCES classes(id) ON DELETE CASCADE,
        PRIMARY KEY (account_id, school_id, class_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS auth_tokens (
        token_hash TEXT PRIMARY KEY,
        account_id TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
        device_id TEXT,
        created_at REAL NOT NULL,
        expires_at REAL NOT NULL
    )
    """,
    # -- Classroom mapping (§12): stable Classroom IDs stay primary ----------
    """
    CREATE TABLE IF NOT EXISTS classroom_mappings (
        course_id TEXT PRIMARY KEY,
        school_id TEXT NOT NULL REFERENCES schools(id) ON DELETE CASCADE,
        class_id TEXT NOT NULL REFERENCES classes(id) ON DELETE CASCADE,
        updated_at REAL NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS assignment_mappings (
        coursework_id TEXT PRIMARY KEY,
        class_id TEXT NOT NULL REFERENCES classes(id) ON DELETE CASCADE,
        school_id TEXT NOT NULL REFERENCES schools(id) ON DELETE CASCADE,
        assigned_worker_id TEXT,
        correction_config TEXT,
        updated_at REAL NOT NULL
    )
    """,
    # -- devices + workers (§21/§22) -------------------------------------------
    """
    CREATE TABLE IF NOT EXISTS devices (
        device_id TEXT PRIMARY KEY,
        label TEXT,
        account_id TEXT REFERENCES accounts(id) ON DELETE SET NULL,
        first_seen REAL NOT NULL,
        last_heartbeat REAL,
        last_ip TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS workers (
        worker_id TEXT PRIMARY KEY,
        device_id TEXT NOT NULL REFERENCES devices(device_id) ON DELETE CASCADE,
        account_id TEXT REFERENCES accounts(id) ON DELETE SET NULL,
        app_version TEXT,
        agent_version TEXT,
        status TEXT,
        current_job_id TEXT,
        last_heartbeat REAL,
        registered_at REAL NOT NULL
    )
    """,
    # -- device credentials (§extension 3/8): short-lived, revocable ----------
    # A newly installed machine registers itself with NO user credentials and
    # receives one of these. It is a DEVICE credential, never an account one:
    # it can only discover and heartbeat, and can never name an account, read
    # reports or run corrections. Only the SHA-256 of the secret is stored, so
    # a database leak cannot be replayed (§8).
    """
    CREATE TABLE IF NOT EXISTS device_credentials (
        credential_hash TEXT PRIMARY KEY,
        device_id TEXT NOT NULL REFERENCES devices(device_id) ON DELETE CASCADE,
        label TEXT,
        issued_at REAL NOT NULL,
        expires_at REAL NOT NULL,
        revoked_at REAL,
        last_used_at REAL,
        use_count INTEGER NOT NULL DEFAULT 0
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS device_links (
        device_id TEXT NOT NULL REFERENCES devices(device_id) ON DELETE CASCADE,
        account_id TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
        linked_at REAL NOT NULL,
        linked_by TEXT,
        revoked_at REAL,
        PRIMARY KEY (device_id, account_id)
    )
    """,
    # -- assistant teams (§1 teams): shared correction records ---------------
    # A team belongs to ONE school. Membership decides which assistants' and
    # co-teachers' correction records are shared; it NEVER grants access by
    # itself unless the account is an active member (server-side check).
    """
    CREATE TABLE IF NOT EXISTS teams (
        id TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        school_id TEXT NOT NULL REFERENCES schools(id) ON DELETE CASCADE,
        active INTEGER NOT NULL DEFAULT 1,
        created_by TEXT,
        created_at REAL NOT NULL,
        updated_at REAL NOT NULL,
        UNIQUE(school_id, name)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS team_members (
        team_id TEXT NOT NULL REFERENCES teams(id) ON DELETE CASCADE,
        account_id TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
        role_in_team TEXT,
        added_at REAL NOT NULL,
        added_by TEXT,
        revoked_at REAL,
        PRIMARY KEY (team_id, account_id)
    )
    """,
    # -- event-based synchronisation (§18-§20) ---------------------------------
    """
    CREATE TABLE IF NOT EXISTS sync_events (
        seq INTEGER PRIMARY KEY AUTOINCREMENT,
        client_event_id TEXT UNIQUE,
        ts REAL NOT NULL,
        type TEXT NOT NULL,
        school_id TEXT,
        class_id TEXT,
        account_id TEXT,
        device_id TEXT,
        worker_id TEXT,
        payload TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS correction_results_sync (
        id TEXT PRIMARY KEY,
        submission_id TEXT NOT NULL,
        submission_version TEXT NOT NULL,
        worker_id TEXT NOT NULL,
        device_id TEXT,
        account_id TEXT,
        school_id TEXT,
        class_id TEXT,
        payload TEXT NOT NULL,
        received_at REAL NOT NULL,
        UNIQUE(submission_id, submission_version, worker_id)
    )
    """,
    # -- error reporting (§24/§25) + audit (§52) --------------------------------
    """
    CREATE TABLE IF NOT EXISTS error_reports (
        id TEXT PRIMARY KEY,
        ts REAL NOT NULL,
        account_id TEXT,
        device_id TEXT,
        worker_id TEXT,
        error_type TEXT,
        failed_stage TEXT,
        message TEXT,
        stack_trace TEXT,
        context TEXT,
        session_events TEXT,
        resolved INTEGER NOT NULL DEFAULT 0
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS audit_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts REAL NOT NULL,
        actor_account TEXT,
        device_id TEXT,
        action TEXT NOT NULL,
        target_type TEXT,
        target_id TEXT,
        details TEXT
    )
    """,
    # -- update management (§27-§45) --------------------------------------------
    """
    CREATE TABLE IF NOT EXISTS update_requests (
        update_id TEXT PRIMARY KEY,
        target_version TEXT NOT NULL,
        target_component TEXT NOT NULL DEFAULT 'app',
        minimum_supported_version TEXT,
        release_notes TEXT,
        priority TEXT NOT NULL DEFAULT 'normal',
        required INTEGER NOT NULL DEFAULT 0,
        auto_install INTEGER NOT NULL DEFAULT 0,
        scope TEXT NOT NULL,
        created_by TEXT NOT NULL,
        created_at REAL NOT NULL,
        package_sha256 TEXT,
        package_url TEXT,
        package_signature TEXT,
        signing_key_id TEXT,
        signing_public_key TEXT,
        -- Release lifecycle (§release management). `status='published'` is the
        -- default so pre-existing requests (and any request created through the
        -- plain /updates path) keep being delivered exactly as before.
        channel TEXT NOT NULL DEFAULT 'stable',
        status TEXT NOT NULL DEFAULT 'published',
        platform TEXT,
        arch TEXT,
        published_at REAL,
        withdrawn_at REAL,
        updated_at REAL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS update_device_status (
        update_id TEXT NOT NULL REFERENCES update_requests(update_id),
        device_id TEXT NOT NULL REFERENCES devices(device_id) ON DELETE CASCADE,
        state TEXT NOT NULL,
        detail TEXT,
        updated_at REAL NOT NULL,
        PRIMARY KEY (update_id, device_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS update_history (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        device_id TEXT NOT NULL,
        update_id TEXT,
        old_version TEXT,
        new_version TEXT,
        started_at REAL,
        completed_at REAL,
        result TEXT NOT NULL,
        failure_reason TEXT,
        rollback_result TEXT
    )
    """,
    # -- update delivery (§19): the signed package body itself ------------------
    """
    CREATE TABLE IF NOT EXISTS update_packages (
        update_id TEXT PRIMARY KEY REFERENCES update_requests(update_id),
        size INTEGER NOT NULL,
        sha256 TEXT NOT NULL,
        content BLOB NOT NULL,
        created_at REAL NOT NULL
    )
    """,
    # -- correction ownership (§51): who corrected what -------------------------
    """
    CREATE TABLE IF NOT EXISTS correction_ownership (
        result_id TEXT PRIMARY KEY,
        account_id TEXT,
        worker_id TEXT,
        device_id TEXT,
        recorded_at REAL NOT NULL
    )
    """,
    # -- report delivery (§5/§19): tracked SEPARATELY from the grade record ----
    # A successful grade sync is NOT proof the PDF arrived (§5). One row per
    # correction result; the grade can be Acknowledged while the report is
    # still Pending/Retry Scheduled/Failed.
    """
    CREATE TABLE IF NOT EXISTS report_deliveries (
        id TEXT PRIMARY KEY,
        result_record_id TEXT,
        submission_id TEXT,
        report_filename TEXT,
        report_sha256 TEXT,
        size_bytes INTEGER,
        -- PENDING | SENDING | DELIVERED | RETRY_SCHEDULED | FAILED | NEEDS_REVIEW
        state TEXT NOT NULL,
        attempts INTEGER NOT NULL DEFAULT 0,
        max_attempts INTEGER NOT NULL DEFAULT 8,
        next_retry_at REAL,
        last_error TEXT,
        created_at REAL NOT NULL,
        updated_at REAL NOT NULL,
        delivered_at REAL,
        UNIQUE(result_record_id)
    )
    """,
)

INDEXES = (
    "CREATE INDEX IF NOT EXISTS idx_sync_events_ts ON sync_events(ts)",
    "CREATE INDEX IF NOT EXISTS idx_workers_device ON workers(device_id)",
    "CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit_log(ts)",
    "CREATE INDEX IF NOT EXISTS idx_errors_ts ON error_reports(ts)",
    "CREATE INDEX IF NOT EXISTS idx_report_deliveries_state "
    "ON report_deliveries(state)",
    "CREATE INDEX IF NOT EXISTS idx_device_credentials_device "
    "ON device_credentials(device_id)",
    "CREATE INDEX IF NOT EXISTS idx_device_links_account "
    "ON device_links(account_id)",
    "CREATE INDEX IF NOT EXISTS idx_teams_school ON teams(school_id)",
    "CREATE INDEX IF NOT EXISTS idx_team_members_account "
    "ON team_members(account_id)",
)


# Additive columns (§19 signing): applied to databases created before the
# signing fields existed. Existing rows keep NULL, which simply means "not
# signed" — an old request can never masquerade as a signed one.
ADDITIVE_COLUMNS: dict[str, tuple[tuple[str, str], ...]] = {
    "update_requests": (
        ("package_signature", "TEXT"),
        ("signing_key_id", "TEXT"),
        ("signing_public_key", "TEXT"),
        # Release lifecycle (§release management). Existing rows become
        # 'published' so no already-created request changes behaviour.
        ("channel", "TEXT NOT NULL DEFAULT 'stable'"),
        ("status", "TEXT NOT NULL DEFAULT 'published'"),
        ("platform", "TEXT"),
        ("arch", "TEXT"),
        ("published_at", "REAL"),
        ("withdrawn_at", "REAL"),
        ("updated_at", "REAL"),
    ),
    # Databases created before multi-role support: NULL means "just [role]".
    "accounts": (
        ("roles", "TEXT"),
    ),
    # Device platform/arch were never recorded, so a platform-specific release
    # could be offered to the wrong OS (see updates.device_context). Existing
    # rows stay NULL = "unknown", which keeps today's permissive behaviour for
    # devices that have not reported yet.
    "devices": (
        ("platform", "TEXT"),
        ("arch", "TEXT"),
    ),
    # Audit trail (§4): the original table had a row id + JSON details only.
    # These columns make each event independently identifiable and filterable
    # (actor, affected account, school/team context, outcome) WITHOUT breaking
    # existing rows — they stay NULL and are read as "unknown".
    "audit_log": (
        ("event_id", "TEXT"),
        ("actor_role", "TEXT"),
        ("affected_account", "TEXT"),
        ("school_id", "TEXT"),
        ("team_id", "TEXT"),
        ("outcome", "TEXT"),
    ),
}


def _ensure_columns(conn) -> None:
    for table, columns in ADDITIVE_COLUMNS.items():
        existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        if not existing:
            continue  # table created by this run already has the columns
        for name, ddl in columns:
            if name not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")


def ensure_tables() -> None:
    from .. import db
    with db.transaction() as conn:
        for ddl in TABLES:
            conn.execute(ddl)
        for ddl in INDEXES:
            conn.execute(ddl)
        _ensure_columns(conn)
