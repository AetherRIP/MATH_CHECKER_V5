-- Math Checker V5 database schema (SQLite)

CREATE TABLE IF NOT EXISTS app_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS courses (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    section TEXT,
    enrollment_code TEXT,
    state TEXT,
    update_time TEXT,
    fetched_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS coursework (
    id TEXT PRIMARY KEY,
    course_id TEXT NOT NULL REFERENCES courses(id) ON DELETE CASCADE,
    title TEXT NOT NULL,
    max_points REAL,
    state TEXT,
    due_date TEXT,
    allowlisted INTEGER NOT NULL DEFAULT 0,
    alternate_link TEXT,
    description TEXT,
    update_time TEXT,
    fetched_at REAL NOT NULL,
    -- Google's own title vs the teacher's Math Correct display name. The
    -- custom title always wins, so a sync never overwrites teacher config.
    classroom_title TEXT,
    custom_title TEXT,
    -- ACTIVE | ARCHIVED | DELETED_IN_CLASSROOM. A deleted Classroom
    -- assignment is archived (history preserved), never hard-deleted.
    sync_state TEXT NOT NULL DEFAULT 'ACTIVE',
    deleted_detected_at REAL,
    archived_at REAL
);
CREATE INDEX IF NOT EXISTS idx_coursework_course ON coursework(course_id);

CREATE TABLE IF NOT EXISTS students (
    id TEXT PRIMARY KEY,
    name TEXT,                   -- official Google Classroom name (source of truth)
    email TEXT,
    nickname TEXT,               -- LOCAL display name; never overwrites `name`
    fetched_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS submissions (
    id TEXT PRIMARY KEY,
    coursework_id TEXT NOT NULL REFERENCES coursework(id) ON DELETE CASCADE,
    course_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    state TEXT,
    late INTEGER,
    update_time TEXT,
    attachment_ids TEXT,          -- JSON array of Drive file ids
    fetched_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sub_coursework ON submissions(coursework_id);

CREATE TABLE IF NOT EXISTS correction_jobs (
    id TEXT PRIMARY KEY,
    submission_id TEXT,            -- NULL for manual (file upload) jobs
    coursework_id TEXT,
    course_id TEXT,
    student_id TEXT,
    student_name TEXT,
    status TEXT NOT NULL,          -- QUEUED STARTING PROCESSING WAITING_FOR_API
                                   -- RETRYING COMPLETED FAILED CANCELLED
                                   -- (legacy stage names still accepted on read)
    stage_detail TEXT,
    progress REAL NOT NULL DEFAULT 0,
    attempts INTEGER NOT NULL DEFAULT 0,
    max_attempts INTEGER NOT NULL DEFAULT 3,
    -- Retry ledger (persisted so a restart cannot silently hand a stuck job a
    -- fresh budget). ``waited_seconds`` is capacity waiting actually slept,
    -- ``park_count`` the number of separate parks, and ``manual_retries`` the
    -- explicit teacher re-queues already granted.
    waited_seconds REAL NOT NULL DEFAULT 0,
    park_count INTEGER NOT NULL DEFAULT 0,
    manual_retries INTEGER NOT NULL DEFAULT 0,
    last_failure_class TEXT,
    retry_due_at REAL,
    worker_id TEXT,                -- which pool worker ran/is running this job
    -- Remark (re-mark) jobs: their attempt is recorded as PENDING and never
    -- auto-applied, so a re-mark can never destroy a valid correction.
    is_remark INTEGER NOT NULL DEFAULT 0,
    error TEXT,
    error_code TEXT,
    created_at REAL NOT NULL,
    started_at REAL,
    finished_at REAL,
    updated_at REAL NOT NULL,
    result_id TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_jobs_one_active_per_submission
    ON correction_jobs(submission_id)
    WHERE submission_id IS NOT NULL
      AND status IN ('QUEUED','PREPARING','DOWNLOADING','UPLOADING','MARKING',
                     'VERIFYING','SAVING','WAITING_FOR_API','RETRYING');

CREATE TABLE IF NOT EXISTS results (
    id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL REFERENCES correction_jobs(id) ON DELETE CASCADE,
    coursework_id TEXT,
    submission_id TEXT,
    student_id TEXT,
    student_name TEXT,
    earned INTEGER NOT NULL DEFAULT 0,
    available INTEGER NOT NULL DEFAULT 0,
    percentage REAL NOT NULL DEFAULT 0,
    provisional_grade TEXT,
    marking_model TEXT,
    key_fingerprint TEXT,        -- safe id (SHA-256 prefix) of the key used
    audit_model TEXT,
    audit_warning TEXT,
    verification_json TEXT,        -- deterministic post-verification report
    result_json TEXT NOT NULL,     -- normalised legacy result dict
    processing_seconds REAL,
    confidence TEXT,               -- HIGH MEDIUM LOW VALIDATION_FAILED LARGE_SCORE_CHANGE
    confidence_reasons TEXT,       -- JSON array of human-readable reasons
    review_required INTEGER NOT NULL DEFAULT 0,  -- human review queue flag (§23)
    manual_override INTEGER NOT NULL DEFAULT 0,  -- teacher override wins (§23)
    -- High-score double-check: when the FIRST correction reached the
    -- threshold (default 95%), an INDEPENDENT second correction ran. The row
    -- holds the FINAL result in earned/available/percentage; the first and
    -- second raw results are preserved separately so neither is ever lost.
    double_check_triggered INTEGER NOT NULL DEFAULT 0,
    double_check_status TEXT,       -- NOT_TRIGGERED | COMPLETE |
                                    -- DOUBLE_CHECK_FAILED | REVIEW_REQUIRED
    first_earned INTEGER,
    first_available INTEGER,
    first_percentage REAL,
    second_earned INTEGER,
    second_available INTEGER,
    second_percentage REAL,
    final_earned INTEGER,
    final_available INTEGER,
    final_percentage REAL,
    disagreement_status TEXT,       -- NONE | MATCHING | SECOND_LOWER |
                                    -- MAJOR_DISAGREEMENT | UNKNOWN
    first_correction_id TEXT,       -- result_versions row id (first snapshot)
    second_correction_id TEXT,      -- result_versions row id (second snapshot)
    double_check_reason TEXT,
    double_check_differences TEXT,  -- JSON: question-level differences (§9)
    created_at REAL NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_results_submission
    ON results(coursework_id, submission_id)
    WHERE coursework_id IS NOT NULL AND submission_id IS NOT NULL;

-- Append-only audit trail of teacher review decisions (§23). A result's
-- approval/override is preserved here even if the result row is later
-- reprocessed, so review history is never lost.
CREATE TABLE IF NOT EXISTS result_reviews (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    result_id TEXT NOT NULL,
    action TEXT NOT NULL,           -- approved | overridden | rejected
    previous_earned INTEGER,
    new_earned INTEGER,
    previous_confidence TEXT,
    note TEXT,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_result_reviews_result ON result_reviews(result_id);

-- Correction VERSIONING (§D): append-only history of accepted correction
-- states for a result. ``results`` always holds the CURRENT state; every
-- accepted snapshot is kept here so a recheck never destroys the previous
-- version and the teacher can choose which one is final.
CREATE TABLE IF NOT EXISTS result_versions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    result_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    is_current INTEGER NOT NULL DEFAULT 0,
    -- Remark attempts land here: recorded and visible, but NOT applied to the
    -- correction until the teacher chooses (Keep previous / Use new result).
    pending INTEGER NOT NULL DEFAULT 0,
    earned INTEGER,
    available INTEGER,
    percentage REAL,
    provisional_grade TEXT,
    marking_model TEXT,
    key_fingerprint TEXT,
    confidence TEXT,
    confidence_reasons TEXT,
    verification_json TEXT,
    result_json TEXT,
    note TEXT,
    created_at REAL NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_result_versions_unique
    ON result_versions(result_id, version);
CREATE INDEX IF NOT EXISTS idx_result_versions_result ON result_versions(result_id);

-- Gemini key lifecycle: identity rows persist even after a key is removed
-- from the secret store, so history stays meaningful.
CREATE TABLE IF NOT EXISTS gemini_keys (
    fingerprint TEXT PRIMARY KEY,
    label TEXT,
    added_at REAL,
    -- removed = no longer in the configured key list (see KeyManager.mark_removed)
    removed INTEGER NOT NULL DEFAULT 0,
    disabled INTEGER NOT NULL DEFAULT 0,
    -- invalid = the provider REJECTED this credential (never "it once failed")
    invalid INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS key_stats (
    fingerprint TEXT PRIMARY KEY REFERENCES gemini_keys(fingerprint) ON DELETE CASCADE,
    requests INTEGER NOT NULL DEFAULT 0,
    successes INTEGER NOT NULL DEFAULT 0,
    failures INTEGER NOT NULL DEFAULT 0,
    rate_limit_events INTEGER NOT NULL DEFAULT 0,
    quota_errors INTEGER NOT NULL DEFAULT 0,
    auth_errors INTEGER NOT NULL DEFAULT 0,
    timeout_errors INTEGER NOT NULL DEFAULT 0,
    cooldown_until REAL NOT NULL DEFAULT 0,
    cooldown_kind TEXT,
    last_used REAL,
    last_failure REAL,
    last_error TEXT
);

-- Staging rows for manual (file upload) corrections and per-assignment
-- mark schemes, so jobs can find their inputs after enqueue.
CREATE TABLE IF NOT EXISTS pending_manual_files (
    job_id TEXT NOT NULL,
    kind TEXT NOT NULL,             -- 'submission' | 'mark_scheme'
    path TEXT NOT NULL,
    filename TEXT,
    PRIMARY KEY (job_id, kind)
);

CREATE TABLE IF NOT EXISTS pending_mark_schemes (
    coursework_id TEXT PRIMARY KEY,
    path TEXT NOT NULL,
    uploaded_at REAL NOT NULL
);

-- Classroom submission TRACKING (data-integrity layer §3-§13).
-- Keyed by the stable Classroom StudentSubmission id; current and previous
-- states are stored separately so a RECLAIMED submission can never be shown
-- as currently submitted. Sync failures never delete rows.
CREATE TABLE IF NOT EXISTS classroom_submissions (
    submission_id TEXT PRIMARY KEY,   -- stable Classroom id (idempotent sync)
    course_id TEXT NOT NULL,
    coursework_id TEXT NOT NULL,
    student_id TEXT NOT NULL,
    student_name TEXT,
    student_email TEXT,
    current_state TEXT NOT NULL,      -- raw Classroom state (source of truth)
    previous_state TEXT,
    state_changed_at REAL,            -- when V5 first observed the change
    late INTEGER NOT NULL DEFAULT 0,
    creation_time TEXT,
    update_time TEXT,
    submission_history TEXT,          -- JSON from Classroom submissionHistory
    attachments_json TEXT,            -- JSON: [{kind,id,title}]
    assigned_grade REAL,
    draft_grade REAL,
    max_points REAL,
    data_quality TEXT NOT NULL DEFAULT 'UNKNOWN',  -- VERIFIED PARTIAL ERROR UNKNOWN
    sync_error TEXT,
    last_synced_at REAL,
    first_seen_at REAL NOT NULL,
    sync_version INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_cs_coursework ON classroom_submissions(coursework_id);
CREATE INDEX IF NOT EXISTS idx_cs_course ON classroom_submissions(course_id);

-- Managed assignments: record the CourseWork THIS integration created via
-- the Google Classroom API. Google only permits an OAuth project to modify
-- (grade, return, attach to) coursework it created itself, so this table is
-- the persisted source of truth for the Managed vs External distinction.
-- Membership is proven by creation through this app — never guessed from a
-- title or a creator field.
CREATE TABLE IF NOT EXISTS classroom_managed_assignments (
    coursework_id TEXT PRIMARY KEY,
    course_id TEXT NOT NULL,
    title TEXT,
    google_account TEXT,          -- signed-in account email at creation time
    integration_source TEXT NOT NULL DEFAULT 'math_checker',
    project_id TEXT,              -- OAuth client_id (safe id, not a secret)
    classroom_state TEXT,         -- PUBLISHED / DRAFT as returned by Google
    alternate_link TEXT,
    max_points REAL,
    due_date TEXT,
    created_at REAL NOT NULL,
    last_verified_at REAL,
    verify_error TEXT
);

-- Publication queue: one row per (coursework, submission, operation) the
-- teacher has asked to publish to Classroom. The row stores the REAL result
-- of the Google call — never a fabricated success. status values:
-- pending | published | failed | not_supported | requires_review | skipped.
CREATE TABLE IF NOT EXISTS classroom_publish_ops (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    coursework_id TEXT NOT NULL,
    course_id TEXT NOT NULL,
    submission_id TEXT,
    student_id TEXT,
    student_name TEXT,
    op_type TEXT NOT NULL,        -- draft_grade | final_grade | return | attach_report
    payload_json TEXT,            -- e.g. {"grade": 12.5} or report selection
    status TEXT NOT NULL DEFAULT 'pending',
    http_status INTEGER,
    error_code TEXT,
    message TEXT,
    created_at REAL NOT NULL,
    attempted_at REAL,
    finished_at REAL
);
CREATE INDEX IF NOT EXISTS idx_publish_coursework
    ON classroom_publish_ops(coursework_id);

-- Locally-detected state transitions (audit trail; complements the API's
-- submissionHistory) so the UI can show e.g. turned-in then reclaimed.
CREATE TABLE IF NOT EXISTS classroom_state_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    submission_id TEXT NOT NULL,
    from_state TEXT,
    to_state TEXT NOT NULL,
    detected_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_cse_submission ON classroom_state_events(submission_id);

-- Internet usage accounting (V5 network budget): byte counters bucketed by
-- UTC hour so rolling hourly/daily limits can be enforced without a daemon.
-- Writes come from the API process AND worker child processes (WAL mode).
CREATE TABLE IF NOT EXISTS net_usage (
    bucket INTEGER NOT NULL,        -- epoch seconds at UTC hour start
    category TEXT NOT NULL,         -- gemini_upload | gemini_generate |
                                    -- gemini_health | classroom_api |
                                    -- classroom_download | other
    bytes_up INTEGER NOT NULL DEFAULT 0,
    bytes_down INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (bucket, category)
);

-- Gemini Files API registry (bandwidth §6/§9): maps a local input file
-- (content SHA-256) on a given API key to the remote file it was uploaded
-- as. Mark schemes are shared across every student job of an assignment, so
-- they are uploaded ONCE and reused until the provider expires them.
CREATE TABLE IF NOT EXISTS gemini_remote_files (
    fingerprint TEXT NOT NULL,      -- safe key id (never the key itself)
    sha256 TEXT NOT NULL,           -- content hash of the local file
    purpose TEXT NOT NULL,          -- 'mark_scheme' | 'submission' | 'question'
    remote_name TEXT NOT NULL,
    size_bytes INTEGER NOT NULL DEFAULT 0,
    uploaded_at REAL NOT NULL,
    expires_at REAL NOT NULL,       -- provider-side expiry (files expire ~48h)
    last_used_at REAL NOT NULL,
    PRIMARY KEY (fingerprint, sha256, purpose)
);

-- Provider-side files owned by ONE correction job (submissions/questions).
-- They are deliberately NOT deleted when a worker attempt ends, so a retry
-- reuses the same remote instead of re-uploading the student's work; they
-- are deleted when the job reaches a terminal state.
CREATE TABLE IF NOT EXISTS job_remote_files (
    job_id TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    remote_name TEXT NOT NULL,
    purpose TEXT NOT NULL,
    created_at REAL NOT NULL,
    PRIMARY KEY (job_id, remote_name)
);

-- Teacher-configured retention (§19): original submission files kept past
-- the terminal state. ``retain_until`` is 0 for 'always' (never expires),
-- or an epoch time for 'days'. A startup sweep removes expired rows and
-- their files, so the setting corresponds to real runtime behaviour.
CREATE TABLE IF NOT EXISTS retained_files (
    job_id TEXT NOT NULL,
    path TEXT NOT NULL,
    kind TEXT NOT NULL,
    filename TEXT,
    retain_until REAL NOT NULL DEFAULT 0,
    created_at REAL NOT NULL,
    PRIMARY KEY (job_id, path)
);

-- Per-job bandwidth diagnostics (§17/§18): proves retries reuse prepared
-- input instead of re-downloading / re-uploading. One row per correction job.
CREATE TABLE IF NOT EXISTS job_bandwidth (
    job_id TEXT PRIMARY KEY,
    download_count INTEGER NOT NULL DEFAULT 0,
    download_bytes INTEGER NOT NULL DEFAULT 0,
    prepare_count INTEGER NOT NULL DEFAULT 0,
    upload_count INTEGER NOT NULL DEFAULT 0,
    upload_bytes INTEGER NOT NULL DEFAULT 0,
    reused_uploads INTEGER NOT NULL DEFAULT 0,
    ai_attempts INTEGER NOT NULL DEFAULT 0,
    avoided_downloads INTEGER NOT NULL DEFAULT 0,
    avoided_reprepare INTEGER NOT NULL DEFAULT 0,
    -- Last attempt's provider-request ledger (gemini.call_audit): totals by
    -- stage, kind and model, plus the budget verdict. JSON, never queried.
    ai_calls_json TEXT,
    updated_at REAL NOT NULL
);

-- Per-student correction REPORT attachment to the Google Classroom
-- StudentSubmission. One row per RESULT (not per attempt), so a failed
-- attachment can be retried WITHOUT re-running the AI marking.
CREATE TABLE IF NOT EXISTS report_attachments (
    result_id TEXT PRIMARY KEY REFERENCES results(id) ON DELETE CASCADE,
    course_id TEXT,
    coursework_id TEXT,
    submission_id TEXT,          -- Classroom StudentSubmission id (stable)
    student_id TEXT,             -- Classroom user id (stable)
    report_filename TEXT,
    drive_file_id TEXT,
    status TEXT NOT NULL DEFAULT 'PENDING',
        -- PENDING | UPLOADED | ATTACHED | VERIFIED | FAILED
        -- | BLOCKED_NOT_MODIFIABLE | SKIPPED
    error TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    updated_at REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_report_attachments_status
    ON report_attachments(status);
CREATE INDEX IF NOT EXISTS idx_report_attachments_coursework
    ON report_attachments(coursework_id);

-- Cross-process report-upload lease. One holder per Classroom submission;
-- expired leases recover automatically after a process crash.
CREATE TABLE IF NOT EXISTS report_upload_locks (
    coursework_id TEXT NOT NULL,
    submission_id TEXT NOT NULL,
    owner TEXT NOT NULL,
    lease_until REAL NOT NULL,
    updated_at REAL NOT NULL,
    PRIMARY KEY(coursework_id, submission_id)
);

-- Canonical report mapping (spec §1/§2/§6): one active correction report per
-- student submission. ``canonical_reports`` records which result's report is
-- the current canonical attachment for a given submission, so a re-correction
-- or remark updates the canonical report IN PLACE instead of leaving the old
-- Math Correct report attached alongside a new one (which would produce
-- "Report.pdf", "Report (1).pdf", "Report (2).pdf"...). The Classroom API only
-- supports addAttachments (there is no removeAttachments), so the dedup happens
-- by replacing the existing Drive file and by refusing to add a second Math
-- Correct file. A file Classroom references is NEVER deleted by cleanup.
CREATE TABLE IF NOT EXISTS canonical_reports (
    course_id TEXT NOT NULL,
    coursework_id TEXT NOT NULL,
    submission_id TEXT NOT NULL,
    canonical_result_id TEXT NOT NULL REFERENCES results(id) ON DELETE CASCADE,
    drive_file_id TEXT,
    updated_at REAL NOT NULL,
    PRIMARY KEY (course_id, coursework_id, submission_id)
);
CREATE INDEX IF NOT EXISTS idx_canonical_reports_result
    ON canonical_reports(canonical_result_id);

-- =====================================================================
-- Automated correction workflow (orchestration around the EXISTING engine)
-- =====================================================================

-- Per-assignment automation selection (§3, §46). Only coursework explicitly
-- recorded here as enabled is ever monitored; every other assignment is
-- ignored. Membership is expressed by an explicit teacher choice, never by a
-- default.
CREATE TABLE IF NOT EXISTS automation_assignments (
    coursework_id TEXT PRIMARY KEY,
    course_id TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    last_checked_at REAL,
    last_error TEXT
);
CREATE INDEX IF NOT EXISTS idx_automation_assignments_enabled
    ON automation_assignments(enabled);

-- Persistent workflow job (spec §12, §13, §42). One row per (assignment,
-- student, submission, submission VERSION), so a resubmission creates a NEW
-- job while the previous version's history is preserved. ``status`` is an
-- explicit state-machine value; nothing infers success from an API 200.
CREATE TABLE IF NOT EXISTS automation_jobs (
    id TEXT PRIMARY KEY,
    course_id TEXT NOT NULL,
    coursework_id TEXT NOT NULL,
    assignment_title TEXT,
    student_id TEXT,
    student_name TEXT,
    submission_id TEXT NOT NULL,
    submission_version TEXT NOT NULL,
    status TEXT NOT NULL,
    step_detail TEXT,
    correction_job_id TEXT,
    result_id TEXT,
    marks_obtained INTEGER,
    maximum_marks INTEGER,
    percentage REAL,
    grade TEXT,
    report_filename TEXT,
    drive_file_id TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    max_attempts INTEGER NOT NULL DEFAULT 3,
    next_attempt_at REAL NOT NULL DEFAULT 0,
    error TEXT,
    error_code TEXT,
    claimed_by TEXT,
    claimed_at REAL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    finished_at REAL,
    completed_at REAL,
    hidden_at REAL
);
-- One job per submission VERSION; the same submission version can never be
-- corrected twice, but a new version legitimately creates a new job (§10/§11).
CREATE UNIQUE INDEX IF NOT EXISTS idx_automation_jobs_version
    ON automation_jobs(coursework_id, submission_id, submission_version);
CREATE INDEX IF NOT EXISTS idx_automation_jobs_status
    ON automation_jobs(status);
CREATE INDEX IF NOT EXISTS idx_automation_jobs_coursework
    ON automation_jobs(coursework_id);

-- Durable component outcomes for teacher-triggered grade + report upload.
-- A row is keyed by corrected result, not automation attempt, so retries can
-- independently resume the component that did not succeed.
CREATE TABLE IF NOT EXISTS bulk_upload_items (
    result_id TEXT PRIMARY KEY REFERENCES results(id) ON DELETE CASCADE,
    coursework_id TEXT NOT NULL,
    submission_id TEXT NOT NULL,
    report_hash TEXT,
    grade REAL,
    grade_status TEXT NOT NULL DEFAULT 'PENDING',
    grade_message TEXT,
    report_status TEXT NOT NULL DEFAULT 'PENDING',
    report_message TEXT,
    updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_bulk_upload_items_submission
    ON bulk_upload_items(coursework_id, submission_id);

-- Per-submission bulk run claims prevent repeated/overlapping requests.
CREATE TABLE IF NOT EXISTS bulk_upload_claims (
    result_id TEXT PRIMARY KEY REFERENCES results(id) ON DELETE CASCADE,
    run_id TEXT NOT NULL,
    lease_until REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_bulk_upload_claims_run ON bulk_upload_claims(run_id);

-- Durable report identity/outcome. Ambiguous writes stay parked for remote
-- reconciliation rather than blindly creating another Classroom attachment.
CREATE TABLE IF NOT EXISTS report_upload_records (
    result_id TEXT PRIMARY KEY REFERENCES results(id) ON DELETE CASCADE,
    coursework_id TEXT NOT NULL,
    submission_id TEXT NOT NULL,
    student_id TEXT,
    report_hash TEXT NOT NULL,
    status TEXT NOT NULL,
    remote_file_id TEXT,
    lease_until REAL,
    error TEXT,
    updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_report_upload_records_identity
    ON report_upload_records(coursework_id, submission_id, student_id, report_hash);

CREATE TABLE IF NOT EXISTS automation_bulk_runs (
    id TEXT PRIMARY KEY,
    status TEXT NOT NULL,
    total INTEGER NOT NULL,
    completed INTEGER NOT NULL DEFAULT 0,
    failed INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS automation_bulk_run_items (
    run_id TEXT NOT NULL REFERENCES automation_bulk_runs(id) ON DELETE CASCADE,
    result_id TEXT NOT NULL,
    status TEXT NOT NULL,
    grade_status TEXT,
    report_status TEXT,
    message TEXT,
    updated_at REAL NOT NULL,
    PRIMARY KEY(run_id, result_id)
);
CREATE INDEX IF NOT EXISTS idx_automation_bulk_run_items_run
    ON automation_bulk_run_items(run_id, status);

-- Append-only activity log for the automation view (§44). Never contains a
-- token, key or credential - only classroom ids, counts and marks.
CREATE TABLE IF NOT EXISTS automation_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at REAL NOT NULL,
    level TEXT NOT NULL DEFAULT 'info',   -- info | success | warning | error
    event TEXT NOT NULL,
    message TEXT,
    automation_job_id TEXT,
    course_id TEXT,
    coursework_id TEXT,
    submission_id TEXT
);
CREATE INDEX IF NOT EXISTS idx_automation_log_created
    ON automation_log(created_at);

-- Assignment-specific IGCSE 1-9 grade boundaries (§20/§21). A grade is always
-- a deterministic lookup of (raw marks -> percentage -> boundary table); when
-- an assignment has no table here the configured global table is used.
CREATE TABLE IF NOT EXISTS grade_boundaries (
    coursework_id TEXT PRIMARY KEY,
    boundaries_json TEXT NOT NULL,
    session_label TEXT,
    updated_at REAL NOT NULL
);

-- ============================================================ repair session
-- TESTING-ONLY records of an assignment repair run ("TESTING ONLY · REPAIRED
-- RESULTS NOT PUBLISHED"). These are DEDICATED TEST RECORDS: they never feed
-- the correction, grade or publication flows, and the original rows in
-- ``results`` are never modified by a repair.
CREATE TABLE IF NOT EXISTS repair_runs (
    id TEXT PRIMARY KEY,
    coursework_id TEXT NOT NULL,
    course_id TEXT,
    assignment_title TEXT,
    class_name TEXT,
    banner TEXT NOT NULL DEFAULT 'TESTING ONLY · REPAIRED RESULTS NOT PUBLISHED',
    status TEXT NOT NULL DEFAULT 'RUNNING',   -- RUNNING COMPLETE PARTIAL
    started_at REAL NOT NULL,
    finished_at REAL,
    summary_json TEXT,                        -- assignment-wide dashboard counts
    notes TEXT
);
CREATE TABLE IF NOT EXISTS repair_items (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES repair_runs(id) ON DELETE CASCADE,
    coursework_id TEXT NOT NULL,
    submission_id TEXT,
    student_id TEXT,
    student_name TEXT,
    -- SECOND_PASS (existing correction re-validated), NEW (never corrected),
    -- DETERMINISTIC (accounting fix only), BLOCKED (inaccessible source)
    mode TEXT NOT NULL,
    -- REPAIRED VERIFIED MATCH NEEDS_REVIEW UNRESOLVED BLOCKED FAILED
    status TEXT NOT NULL,
    original_result_id TEXT,
    original_earned REAL,
    original_available REAL,
    original_percentage REAL,
    repaired_earned REAL,
    repaired_available REAL,
    repaired_percentage REAL,
    repaired_grade TEXT,
    score_basis TEXT,
    questions_total INTEGER NOT NULL DEFAULT 0,
    questions_added INTEGER NOT NULL DEFAULT 0,
    questions_changed INTEGER NOT NULL DEFAULT 0,
    questions_flagged INTEGER NOT NULL DEFAULT 0,
    missing_fixed INTEGER NOT NULL DEFAULT 0,
    zeros_fixed INTEGER NOT NULL DEFAULT 0,
    unresolved_count INTEGER NOT NULL DEFAULT 0,
    diff_json TEXT,               -- per-question: original vs repaired + reason
    audit_findings_json TEXT,     -- deterministic audit of the ORIGINAL
    validation_json TEXT,         -- deterministic validation of the REPAIR
    artifact_path TEXT,           -- data/repairs/... full JSON (both versions)
    report_filename TEXT,
    error TEXT,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_repair_items_run ON repair_items(run_id);
CREATE INDEX IF NOT EXISTS idx_repair_items_cw ON repair_items(coursework_id);

-- NOTE: column migrations (e.g. correction_jobs.worker_id) are applied
-- conditionally by app.db._migrate() at startup, NOT here — ALTER TABLE has
-- no IF NOT EXISTS in SQLite and would fail on fresh databases.

-- ============================================================================
-- Mark-scheme discrepancy detection + bulk correction approval (V5)
--
-- The mark scheme is an important reference, NOT an infallible source of
-- mathematical truth. When the marking engine has a defensible mathematical
-- reason to believe a supplied mark scheme is wrong or incomplete, the
-- disagreement is recorded here. The ORIGINAL mark scheme is never modified,
-- and no previously graded submission is changed without explicit human
-- approval of a staged bulk-correction proposal.
-- ============================================================================

-- One row per detected disagreement (class + assignment + question + type).
-- Re-detecting the same disagreement UPDATES the existing row instead of
-- creating a duplicate notification. ``state`` is one of:
--   CONFIRMED_DISCREPANCY | POSSIBLE_DISCREPANCY | VALID_ALTERNATIVE |
--   UNCERTAIN | HUMAN_REVIEWED
CREATE TABLE IF NOT EXISTS scheme_discrepancies (
    id TEXT PRIMARY KEY,
    coursework_id TEXT,
    course_id TEXT,
    assignment_title TEXT,
    question_number TEXT NOT NULL,
    discrepancy_type TEXT NOT NULL,
    state TEXT NOT NULL,
    scheme_answer TEXT,
    verified_answer TEXT,
    verified_reasoning TEXT,
    evidence TEXT,
    suspicion TEXT,
    confidence REAL,
    max_marks INTEGER,
    student_result_id TEXT,
    submission_id TEXT,
    student_id TEXT,
    student_name TEXT,
    awarded_marks INTEGER,
    justified_marks INTEGER,
    review_recommended INTEGER NOT NULL DEFAULT 1,
    review_required INTEGER NOT NULL DEFAULT 1,
    review_status TEXT NOT NULL DEFAULT 'AWAITING_REVIEW',
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    UNIQUE(coursework_id, question_number, discrepancy_type)
);
CREATE INDEX IF NOT EXISTS idx_scheme_disc_coursework
    ON scheme_discrepancies(coursework_id);
CREATE INDEX IF NOT EXISTS idx_scheme_disc_result
    ON scheme_discrepancies(student_result_id);
CREATE INDEX IF NOT EXISTS idx_scheme_disc_status
    ON scheme_discrepancies(review_status);

-- Questions the teacher has taken OUT of marking for one assignment. A row
-- here means the question is not asked for, not marked, not counted in the
-- total available marks and never raises a scheme discrepancy. The mark
-- scheme FILE is untouched: the exclusion is a marking decision, so re-running
-- with the row removed restores the question exactly.
CREATE TABLE IF NOT EXISTS scheme_ignored_questions (
    coursework_id TEXT NOT NULL,
    question_number TEXT NOT NULL,   -- as the blueprint labels it ("12", "4(a)")
    note TEXT,
    created_at REAL NOT NULL,
    PRIMARY KEY (coursework_id, question_number)
);
CREATE INDEX IF NOT EXISTS idx_scheme_ignored_coursework
    ON scheme_ignored_questions(coursework_id);

-- The teacher's handling decision for one question or sub-question of an
-- assignment's mark scheme: AUTO (mark it), MANUAL (a human checks it) or SKIP
-- (never mark it). One row per question, keyed by the scheme's own identifier
-- rather than by position, so a re-extraction that reorders questions cannot
-- move a decision onto a different question.
--
-- ``answer_text`` is the teacher's edited expected answer. It is kept HERE, not
-- written back into the blueprint or the PDF: the uploaded mark scheme is
-- evidence and must stay exactly as it was uploaded.
CREATE TABLE IF NOT EXISTS scheme_question_decisions (
    coursework_id TEXT NOT NULL,
    question_key TEXT NOT NULL,      -- canonical id: "1", "1(a)", "1(b)(ii)"
    question_label TEXT,             -- the id as the scheme prints it
    decision TEXT NOT NULL,          -- AUTO | MANUAL | SKIP
    -- 0 when the row exists only because the teacher edited the ANSWER: an
    -- edited answer is not a handling decision, and must not be reported as
    -- one. Set to 1 by any real decision.
    decided INTEGER NOT NULL DEFAULT 1,
    answer_text TEXT,                -- teacher's edited answer, NULL = as extracted
    note TEXT,
    source TEXT NOT NULL DEFAULT 'HUMAN',   -- HUMAN | PROPOSED
    updated_by TEXT,
    updated_at REAL NOT NULL,
    PRIMARY KEY (coursework_id, question_key)
);
CREATE INDEX IF NOT EXISTS idx_scheme_decisions_coursework
    ON scheme_question_decisions(coursework_id);

-- Append-only history of every decision and answer edit. A decision is never
-- overwritten without a row here first, so the teacher can see what they
-- changed and when — including a change back to a value used before.
CREATE TABLE IF NOT EXISTS scheme_decision_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    coursework_id TEXT NOT NULL,
    question_key TEXT NOT NULL,
    question_label TEXT,
    action TEXT NOT NULL,            -- DECISION | ANSWER | NOTE | CLEARED
    previous_decision TEXT,
    new_decision TEXT,
    previous_answer TEXT,
    new_answer TEXT,
    reason TEXT,
    source TEXT NOT NULL DEFAULT 'HUMAN',
    actor TEXT,
    at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_scheme_decision_history
    ON scheme_decision_history(coursework_id, at);

-- Append-only audit of human decisions on a discrepancy. Distinguishes the
-- AI's recommendation (``ai_recommendation``) from the human's final decision.
CREATE TABLE IF NOT EXISTS scheme_discrepancy_reviews (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    discrepancy_id TEXT NOT NULL,
    decision TEXT NOT NULL,
    decided_by TEXT,
    note TEXT,
    ai_recommendation TEXT,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_scheme_disc_reviews_disc
    ON scheme_discrepancy_reviews(discrepancy_id);

-- Staged BULK correction proposals. Creating a proposal NEVER changes a grade.
-- ``status``: PROPOSED | APPROVED | APPLIED | PARTIAL | REJECTED.
CREATE TABLE IF NOT EXISTS bulk_correction_proposals (
    id TEXT PRIMARY KEY,
    coursework_id TEXT,
    course_id TEXT,
    discrepancy_id TEXT,
    question_number TEXT NOT NULL,
    scheme_answer TEXT,
    verified_answer TEXT,
    reason TEXT,
    status TEXT NOT NULL DEFAULT 'PROPOSED',
    created_by TEXT,
    created_at REAL NOT NULL,
    decided_by TEXT,
    decided_at REAL,
    applied_at REAL,
    affected_count INTEGER NOT NULL DEFAULT 0,
    changed_count INTEGER NOT NULL DEFAULT 0,
    applied_count INTEGER NOT NULL DEFAULT 0,
    failed_count INTEGER NOT NULL DEFAULT 0,
    classroom_publish_state TEXT,
    note TEXT
);
CREATE INDEX IF NOT EXISTS idx_bulk_prop_coursework
    ON bulk_correction_proposals(coursework_id);
CREATE INDEX IF NOT EXISTS idx_bulk_prop_status
    ON bulk_correction_proposals(status);

-- One row per affected submission in a proposal. Original marks are preserved
-- here for auditing even after the results row is updated.
CREATE TABLE IF NOT EXISTS bulk_correction_items (
    id TEXT PRIMARY KEY,
    proposal_id TEXT NOT NULL,
    result_id TEXT,
    submission_id TEXT,
    student_id TEXT,
    student_name TEXT,
    original_earned INTEGER,
    proposed_earned INTEGER,
    delta_points INTEGER,
    max_marks INTEGER,
    question_awarded_before INTEGER,
    question_awarded_after INTEGER,
    status TEXT NOT NULL DEFAULT 'PENDING',
    error TEXT,
    applied_at REAL,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_bulk_items_proposal
    ON bulk_correction_items(proposal_id);

-- Per-OPERATION state for one automation job (report upload / grade / return).
--
-- The job's own ``status`` column is the state-machine position, and
-- ``error``/``error_code`` hold only the LAST failure. That is not enough to
-- tell a genuinely failed operation from one that already SUCCEEDED and whose
-- error is history: a report upload that timed out (and was in fact accepted
-- by Google) parked a job as REPORT_UPLOAD_FAILED while the report and the
-- returned work were already on the submission, and the stale error then
-- stayed on screen through every retry.
--
-- One row per logical operation keeps the two apart:
--   * ``state``        — the CURRENT outcome: PENDING | IN_PROGRESS |
--                        SUCCEEDED | FAILED | NEEDS_VERIFICATION | CANCELLED.
--   * ``error``/...    — HISTORY: what failed, when, on which attempt, and
--                        whether it was later resolved (``resolved``).
-- Rows are never deleted by error clearing: the row is what prevents a
-- duplicate report attachment or a second grade write, so clearing removes
-- only the resolved MESSAGE.
CREATE TABLE IF NOT EXISTS automation_operations (
    job_id TEXT NOT NULL,
    op_key TEXT NOT NULL,            -- report | grade | return
    kind TEXT NOT NULL,              -- report_upload | grade | return
    state TEXT NOT NULL,
    attempt INTEGER NOT NULL DEFAULT 0,
    error TEXT,
    error_code TEXT,
    error_at REAL,
    error_attempt INTEGER,
    resolved INTEGER NOT NULL DEFAULT 0,
    resolved_at REAL,
    recovered INTEGER NOT NULL DEFAULT 0,
    verified INTEGER NOT NULL DEFAULT 0,
    note TEXT,
    dismissed_at REAL,
    first_attempt_at REAL,
    last_attempt_at REAL,
    succeeded_at REAL,
    updated_at REAL NOT NULL,
    PRIMARY KEY (job_id, op_key)
);
CREATE INDEX IF NOT EXISTS idx_automation_operations_state
    ON automation_operations(state);
CREATE INDEX IF NOT EXISTS idx_automation_operations_errors
    ON automation_operations(resolved);
