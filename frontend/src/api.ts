// Shared API helpers and types for Math Checker V5.

export type JobStatus =
  | "QUEUED" | "PREPARING" | "DOWNLOADING" | "UPLOADING" | "MARKING"
  | "VERIFYING" | "SAVING" | "WAITING_FOR_API" | "RETRYING"
  | "COMPLETED" | "FAILED" | "CANCELLED";

export const ACTIVE_STATUSES: JobStatus[] = [
  "QUEUED", "PREPARING", "DOWNLOADING", "UPLOADING", "MARKING",
  "VERIFYING", "SAVING", "WAITING_FOR_API", "RETRYING",
];

export interface JobBandwidth {
  job_id: string;
  download_count: number;
  download_bytes: number;
  prepare_count: number;
  upload_count: number;
  upload_bytes: number;
  reused_uploads: number;
  ai_attempts: number;
  avoided_downloads: number;
  avoided_reprepare: number;
  updated_at: number;
}

export interface Job {
  id: string;
  submission_id: string | null;
  coursework_id: string | null;
  student_name: string | null;
  /** Canonical display name: nickname -> Google Classroom name. */
  display_name?: string | null;
  student_id?: string | null;
  status: JobStatus;
  stage_detail: string | null;
  progress: number;
  attempts: number;
  max_attempts: number;
  error: string | null;
  created_at: number;
  finished_at: number | null;
  result_id: string | null;
  bandwidth?: JobBandwidth | null;
}

/** Requeue a failed or cancelled correction. The backend resets attempts,
 *  progress and error to NULL, so a retry starts from a clean slate and never
 *  duplicates a completed marking (§16: nothing is deleted, the job is reused). */
export function retryJob(jobId: string): Promise<Job> {
  return api<Job>(`/jobs/${encodeURIComponent(jobId)}/retry`, { method: "POST" });
}

export function cancelJob(jobId: string): Promise<{ cancelled: boolean }> {
  return api(`/jobs/${encodeURIComponent(jobId)}/cancel`, { method: "POST" });
}

export interface KeyInfo {
  fingerprint: string;
  label: string | null;
  status: "HEALTHY" | "COOLDOWN" | "INVALID" | "DISABLED";
  requests: number;
  successes: number;
  failures: number;
  rate_limit_events: number;
  quota_errors: number;
  auth_errors: number;
  timeout_errors: number;
  cooldown_remaining: number;
  cooldown_kind: string | null;
  last_used: number | null;
  last_error: string | null;
  disabled: boolean;
}

export interface ApiKeyStatus {
  total: number;
  healthy: number;
  cooldown: number;
  invalid: number;
  disabled: number;
  has_keys: boolean;
  all_down: boolean;
  keys: KeyInfo[];
}

export interface Dashboard {
  corrections_today: number;
  first_run?: boolean;
  pending: number;
  processing: number;
  completed: number;
  failed: number;
  jobs: Record<string, number>;
  api_health: {
    healthy: number; cooldown: number; invalid: number; total: number;
    has_keys: boolean; aggregate: Record<string, number>;
  };
  classroom: {
    configured: boolean; connected: boolean; last_sync: number | null;
    courses: number; allowlisted: number; pending_submissions: number;
    email?: string | null;
  };
  role: string;
  mock_mode: boolean;
  version: string;
  build_number?: string;
  build_date?: string;
  demo_mode?: boolean;
}

export interface PrivacyItem {
  key: string;
  label: string;
  present: boolean;
}

export interface PrivacyInfo {
  data_directory: string;
  application_files_separate: boolean;
  items: PrivacyItem[];
  note: string;
}

export type AccessMode = "local" | "lan";
export type RetentionMode = "never" | "until_complete" | "days" | "always";

/** Local tiny-agent switch state (backend AgentState), or null when unavailable.
 *
 * `enabled` is the PERSISTED preference; `label`/`seconds_since_heartbeat`
 * describe the agent actually observed on this machine, so the UI can show a
 * paused or quiet agent honestly instead of echoing the switch back. */
export interface AgentState {
  enabled: boolean;
  reason: string;
  updated_at: number | null;
  updated_by: string | null;
  corrupt: boolean;
  label: string;
  last_heartbeat: number | null;
  seconds_since_heartbeat: number | null;
}

export function agentState(): Promise<AgentState> {
  return api<AgentState>("/agent/state");
}

/** Turn the local background sync agent on or off. This affects THIS
 *  installation only and never blocks local correction work. */
export function setAgentEnabled(enabled: boolean): Promise<AgentState> {
  return api<AgentState>("/agent/state", {
    method: "POST", body: JSON.stringify({ enabled }),
  });
}

/** A Teacher/Developer account this machine has discovered.
 *
 * Deliberately carries only what discovery is allowed to reveal: the role, a
 * display name and device availability. There is no username, scope or
 * permission field, because the backend never sends them. */
export interface DiscoveredAccount {
  account_id: string;
  display_name: string;
  role: string;
  roles: string[];
  availability: "online" | "offline" | "unknown";
  last_heartbeat: number | null;
  online_devices: number;
  registered_devices: number;
  linked: boolean;
  sync_authorized: boolean;
  linking_required: boolean;
}

export interface AgentDiscovery {
  device_id: string | null;
  registered: boolean;
  accounts: DiscoveredAccount[];
  linked_accounts: string[];
  credential_expires_at?: number | null;
  server_time?: number;
}

/** Accounts + device availability this machine may synchronise with.
 *  Empty until the agent has registered and discovered. */
export function agentDiscovery(): Promise<AgentDiscovery> {
  return api<AgentDiscovery>("/agent/discovery");
}

export interface Settings {
  role: "teacher" | "coteacher" | "developer";
  mock_mode: boolean;
  debug: boolean;
  worker: { concurrency: number; max_retries: number };
  gemini: { primary_models: string[]; request_timeout_ms: number };
  classroom: { write_draft_grades: boolean };
  usage: { hourly_mb: number; daily_mb: number };
  gemini_key_count: number;
  has_oauth_client: boolean;
  teacher: { teacher_name: string; school_name: string; onboarded: boolean };
  /** Permanent report letterhead (§42) — only this screen may change it. */
  reports: {
    teacher_title: string; center_name: string; academic_year: string;
    quote: string; logo_path: string; monogram: string;
    footer_note: string; accent: string; document_label: string;
  };
  grades: { boundaries: [string, number][]; session_label: string };
  retention: {
    keep_submissions: RetentionMode; keep_days: number;
    cleanup_temp_on_start: boolean;
  };
  network: { access_mode: AccessMode; port: number };
  /** TESTING ONLY mode. While ``only`` is true every grade write, report
   *  upload, attachment change and return is refused before it is sent. */
  testing: { only: boolean; reason: string };
  /** Server lifecycle: start on demand, stay up while in use, stop when idle. */
  lifecycle: LifecycleConfig;
  version: string;
}

/** The server-lifecycle settings as the backend actually holds them.
 *  ``enabled`` is the idle stop; ``supervisor`` is the small always-on relay
 *  that starts the backend again, and it is a PROCESS — so it needs a restart,
 *  unlike ``enabled`` which the monitor re-reads on its next tick. */
export interface LifecycleConfig {
  enabled: boolean;
  supervisor: boolean;
  heartbeat_seconds: number;
  session_timeout_seconds: number;
  idle_grace_seconds: number;
  max_shutdown_wait_seconds: number;
  last_chance_seconds: number;
}

/** One reason the app is not stopping yet (a real job or a real hold). */
export interface LifecycleBlocker {
  source: string;
  detail: string;
}

/**
 * The LIVE lifecycle state, measured by the backend — never a guess and never
 * a value the page invented. ``state`` is the state machine it is really in:
 *
 *   serving            a browser session is beating
 *   counting_down      nobody is active; ``countdown_seconds`` is running
 *   waiting_for_work   the countdown expired but work is still in flight
 *   held               a reason that never expires forbids stopping (a live
 *                      public address, for example)
 *   shutting_down      a clean stop has been requested (cancellable briefly)
 *   always_on          the feature is switched off
 *   sleeping           the relay is answering because the backend is stopped
 *
 * Two producers share this shape: the running backend (``source`` absent) and
 * the relay behind a sleeping app (``source: "supervisor"``, ``backend:
 * "stopped"``). Neither ever reports "running" for a backend that is not.
 */
export interface LifecycleStatus {
  enabled: boolean;
  supervisor: boolean;
  backend: "running" | "stopped";
  state: string;
  active_sessions: number;
  heartbeat_seconds: number;
  session_timeout_seconds: number;
  idle_grace_seconds: number;
  max_shutdown_wait_seconds?: number;
  last_chance_seconds?: number;
  countdown_seconds: number | null;
  last_heartbeat_age_seconds: number | null;
  session_expires_in_seconds?: number | null;
  blockers: LifecycleBlocker[];
  deferrals?: number;
  stops?: number;
  cancels?: number;
  uptime_seconds?: number;
  public_port?: number;
  backend_port?: number;
  source?: "supervisor";
  /** Present when this instance was started as the relay's own child. */
  shutdown?: { attached: boolean; pending: boolean; reason?: string };
}

/** The real lifecycle state right now. Read-only: it never counts as a user. */
export function lifecycleStatus(): Promise<LifecycleStatus> {
  return api<LifecycleStatus>("/lifecycle/status");
}

/**
 * "This tab is still here."
 *
 * A GET on purpose: it is tiny, idempotent, fires every 30s per open tab, and
 * must survive being sent from a page that is being unloaded (``keepalive``).
 * The answer carries the current state, so a tab that returns during the
 * countdown can see that it just cancelled the stop.
 */
export function lifecycleHeartbeat(sid: string, hidden: boolean):
    Promise<LifecycleStatus & { ok: boolean }> {
  const query = `sid=${encodeURIComponent(sid)}&hidden=${hidden ? 1 : 0}`;
  return api<LifecycleStatus & { ok: boolean }>(`/lifecycle/heartbeat?${query}`);
}

/**
 * "This tab is closing." Sent with ``keepalive`` so the browser does not kill
 * the request the instant the page goes away — which is what makes a closed
 * tab noticed immediately instead of after the session timeout.
 */
export function lifecycleLeave(sid: string): void {
  apiKeepalive(`/lifecycle/leave?sid=${encodeURIComponent(sid)}`);
}

export interface SetupCheck {
  name: string;
  status: "pass" | "warn" | "fail";
  detail: string;
  fix?: string | null;
}

export interface SetupStatus {
  app_name: string;
  version: string;
  first_run: boolean;
  teacher: {
    teacher_name: string; school_name: string;
    onboarded: boolean; onboarded_at: number;
  };
  checks: {
    teacher_profile: boolean;
    ai_keys: boolean;
    ai_key_count: number;
    classroom_oauth_client: boolean;
    classroom_connected: boolean;
    storage_writable: boolean;
  };
}

export interface SelfCheck {
  overall: "pass" | "warn" | "fail";
  checks: SetupCheck[];
  version: string;
  schema_version: number;
}

export interface ReviewItem {
  id: string;
  student_name: string | null;
  /** Canonical display name: nickname -> Google Classroom name. */
  display_name?: string | null;
  classroom_name?: string | null;
  nickname?: string | null;
  student_id: string | null;
  coursework_id: string | null;
  submission_id: string | null;
  earned: number;
  available: number;
  percentage: number;
  confidence: string | null;
  confidence_reasons: string[];
  review_required: number;
  manual_override: number;
  created_at: number;
  assignment_title: string | null;
}

// ---- Classroom basics (courses / coursework / legacy submissions) ----

export interface Course { id: string; name: string; section: string | null }

export interface Coursework {
  id: string; course_id: string; title: string; max_points: number | null;
  allowlisted: number; due_date: string | null;
}

export interface Submission {
  id: string; user_id: string; student_name: string; state: string;
  job: { id: string; status: string } | null;
  result: { id: string; earned: number; available: number; percentage: number } | null;
}

// ---- Classroom tracking (normalized view model) ----

export type TrackingStatus =
  | "NOT_STARTED" | "CREATED" | "SUBMITTED" | "SUBMITTED_LATE" | "RETURNED"
  | "RECLAIMED" | "EDITED_AFTER_SUBMISSION" | "MISSING" | "PROCESSING"
  | "CORRECTED" | "GRADED" | "FAILED" | "INVALID" | "UNKNOWN";

export interface TrackingAttachment { kind: string; id: string | null; title: string | null }

export interface TrackingRow {
  submission_id: string;
  course_id: string | null;
  coursework_id: string | null;
  coursework_title: string | null;
  student_id: string;
  student_name: string;
  student_email: string | null;
  raw_state: string | null;
  previous_state: string | null;
  state_changed_at: number | null;
  status: TrackingStatus;
  status_label: string;
  late: boolean;
  creation_time: string | null;
  update_time: string | null;
  attachments: TrackingAttachment[];
  assigned_grade: number | null;
  draft_grade: number | null;
  max_points: number | null;
  data_quality: "VERIFIED" | "PARTIAL" | "ERROR" | "UNKNOWN";
  stale: boolean;
  sync_error: string | null;
  last_synced_at: number | null;
  first_seen_at: number | null;
  synthetic: boolean;
  job: { id: string; status: string; error: string | null } | null;
  result: { id: string; earned: number; available: number; percentage: number } | null;
}

export interface TrackingOverview {
  counts: Partial<Record<TrackingStatus, number>>;
  total: number;
  sync_errors: number;
  stale: number;
  last_sync: number | null;
}

export interface TrackingData {
  rows: TrackingRow[];
  overview: TrackingOverview;
}

export interface StateEvent {
  from_state: string | null;
  to_state: string;
  detected_at: number;
}

export interface SubmissionDetail {
  submission: TrackingRow & {
    current_state: string;
    submission_history: { state?: string; grade?: number | null; updateTime?: string | null; actorUserId?: string | null }[];
  };
  state_events: StateEvent[];
  attachments: TrackingAttachment[];
  jobs: { id: string; status: string; created_at: number; finished_at: number | null; error: string | null }[];
  results: { id: string; earned: number; available: number; percentage: number; created_at: number }[];
}

export interface SyncSummary {
  course_id: string;
  coursework_id: string;
  submissions: number;
  roster: number;
  transitions: number;
  synced_at: number;
  pages?: number;
  elapsed_s?: number;
}

export interface TrackingDiagnostic {
  check: string;
  status: "pass" | "fail" | "skip";
  detail: string;
}

// ---- Student profiles (Classroom identity + local corrections) ----

export interface StudentListItem {
  student_id: string;
  official_name: string | null;
  email: string | null;
  nickname: string | null;
  display_name: string;
  /** Teacher-assigned class/grade. Local metadata; never sent to Classroom. */
  class_name: string | null;
  /** The same class, but never blank — reports and tables read this. */
  class_label: string;
  assignments: number;
  submitted: number;
  tracking_available: boolean;
  corrections: number;
  total_earned: number;
  total_available: number;
  pending: number;
}

/** The class vocabulary the UI offers as suggestions (free text still allowed). */
export interface ClassVocabulary {
  classes: string[];
}

export interface BulkDetailsResult {
  updated: string[];
  skipped: string[];
  class_name: string | null;
  profiles: StudentProfile[];
}

export interface StudentAssignment {
  submission_id: string;
  coursework_id: string;
  title: string | null;
  status: TrackingStatus;
  status_label: string;
  raw_state: string | null;
  late: boolean;
  attachments: number;
  assigned_grade: number | null;
  max_points: number | null;
  due_date: string | null;
  creation_time: string | null;
  update_time: string | null;
  stale: boolean;
  data_quality: string;
  last_synced_at: number | null;
  result: { id: string; earned: number; available: number; percentage: number } | null;
  job: { id: string; status: string; error: string | null; error_code: string | null } | null;
}

export interface StudentPerformance {
  classroom_average: number | null;
  classroom_highest: number | null;
  classroom_lowest: number | null;
  classroom_graded: number;
  classroom_not_graded: number;
  classroom_zeros: number;
  mc_average: number | null;
  mc_highest: number | null;
  mc_lowest: number | null;
  mc_zeros: number;
  total_earned: number;
  total_available: number;
  overall_percentage: number | null;
  missing: number;
  late: number;
  corrected: number;
}

export interface StudentBehavior {
  on_time: number;
  late: number;
  missing: number;
  draft: number;
  turned_in: number;
  returned: number;
}

export interface StudentCorrection {
  id: string;
  coursework_id: string | null;
  submission_id: string | null;
  earned: number;
  available: number;
  percentage: number;
  provisional_grade: string | null;
  marking_model: string | null;
  status: string | null;
  needs_review?: boolean;
  review_questions: string[];
  created_at: number;
}

export interface StudentProfile {
  student_id: string;
  official_name: string | null;
  email: string | null;
  nickname: string | null;
  /** Teacher-assigned class/grade (local metadata). */
  class_name: string | null;
  /** Never blank: falls back to the Classroom course, then "Not assigned". */
  class_label: string;
  display_name: string;
  course: { course_id: string; name: string | null; section: string | null } | null;
  performance: StudentPerformance;
  behavior: StudentBehavior;
  sources: Record<string, string>;
  classroom: {
    available: boolean;
    assignments_total: number;
    submitted: number;
    not_submitted: number;
    unknown: number;
    submitted_percentage: number | null;
    status_counts: Record<string, number>;
    assignments: StudentAssignment[];
  };
  corrections: {
    total: number;
    pending: number;
    failed: number;
    total_earned: number;
    total_available: number;
    overall_percentage: number | null;
    needs_review: number;
    recent: StudentCorrection[];
    jobs: { id: string; status: string; created_at: number; finished_at: number | null; error: string | null }[];
    last_corrected_at: number | null;
  };
  fetched_at: number;
}

/* ---------------------------------------------------------- reports ---- */

export interface ReportStats {
  total_students: number;
  status_counts: Record<string, number>;
  submission: {
    submitted: number; not_submitted: number; draft: number;
    turned_in: number; turned_in_late: number; returned: number;
    with_attachments: number;
  };
  classroom_grading: {
    graded: number; ungraded: number;
    average: number | null; highest: number | null; lowest: number | null;
  };
  math_checker: {
    corrected: number; processing: number; failed: number; cancelled: number;
    invalid: number; not_processed: number;
    average: number | null; highest: number | null; lowest: number | null;
    total_earned: number; total_available: number;
    overall_percentage: number | null;
  };
}

export interface ReportRow {
  submission_id: string;
  student_id: string;
  student_name: string;
  nickname: string | null;
  class_name?: string | null;
  class_label?: string;
  student_email: string | null;
  status: TrackingStatus;
  status_label: string;
  raw_state: string | null;
  late: boolean;
  creation_time: string | null;
  update_time: string | null;
  attachments: number;
  attachment_names: string[];
  assigned_grade: number | null;
  max_points: number | null;
  classroom_graded: boolean;
  classroom_percentage: number | null;
  draft_grade: number | null;
  mc_status: "CORRECTED" | "PROCESSING" | "FAILED" | "CANCELLED" | "NOT_PROCESSED";
  mc_earned: number | null;
  mc_available: number | null;
  mc_percentage: number | null;
  job_status: string | null;
  job_error: string | null;
  data_quality: string;
  stale: boolean;
  last_synced_at: number | null;
  synthetic: boolean;
}

export interface AssignmentReport {
  generated_at: number;
  class: {
    course_id: string; name: string; section: string | null;
    teacher: string | null; connected: boolean; students_total: number;
  };
  assignment: {
    id: string; title: string; description: string | null;
    created: string | null; due_date: string | null;
    max_points: number | null; state: string | null; allowlisted: boolean;
  };
  stats: ReportStats;
  rows: ReportRow[];
}

export interface ClassReport {
  generated_at: number;
  class: {
    course_id: string; name: string; section: string | null;
    teacher: string | null; connected: boolean;
    students_total: number; assignments_total: number;
  };
  stats: ReportStats;
  assignments: {
    id: string; title: string; due_date: string | null;
    max_points: number | null; stats: ReportStats;
  }[];
  students: {
    student_id: string; student_name: string; nickname: string | null;
    student_email: string | null; assignments: number; submitted: number;
    late: number; not_submitted: number; corrected: number; failed: number;
    classroom_average: number | null; mc_average: number | null;
    mc_total_earned: number; mc_total_available: number;
  }[];
}

/* ------------------------------------------------------------- log ------ */

export type LogCategory =
  | "running" | "completed" | "failed" | "cancelled" | "warning" | "system";

export interface LogEntry {
  ts: string | null;
  level: string;
  component: string;
  event: string;
  category: LogCategory;
  context: Record<string, unknown>;
}

export interface LogFeed {
  entries: LogEntry[];
  counts: Partial<Record<LogCategory | "all", number>>;
  total: number;
}

export interface UsageCategoryTotals { up: number; down: number }

export interface ApiRetryStats {
  requests: number;
  successes: number;
  failures: number;
  retries: number;
  failovers: number;
  cooldowns: number;
  avoided: { downloads: number; uploads: number; reprepare: number };
  by_class: Record<string, number>;
  updated_at?: number | null;
  provider: {
    available: boolean;
    cooldown_remaining: number;
    consecutive_high_demand: number;
    threshold?: number;
    cooldowns?: number;
    last_failure_at?: number | null;
    last_failure_kind?: string | null;
    last_success_at?: number | null;
  };
  keys: Record<string, number>;
}

export interface SearchItem {
  id: string;
  title: string;
  subtitle: string | null;
  route: string;
}

export interface SearchGroup {
  key: string;
  label: string;
  items: SearchItem[];
}

export interface SearchResponse {
  query: string;
  groups: SearchGroup[];
  total: number;
}

export interface PermissionsReport {
  checked: boolean;
  ok: boolean | null;
  connected: boolean;
  required: string[];
  granted?: string[];
  missing: string[];
  detail?: string;
  checked_at?: number;
}

export interface FullSyncSummary {
  courses: number;
  assignments: number;
  students: number;
  submissions: number;
  errors: { part: string; detail: string }[];
  partial: boolean;
  synced_at: number;
  elapsed_s: number;
}

export interface UsageState {
  now: number;
  hour: { up: number; down: number; total: number; by_category: Record<string, UsageCategoryTotals> };
  day: { up: number; down: number; total: number; by_category: Record<string, UsageCategoryTotals> };
  limits: { hourly_mb: number; daily_mb: number; hourly_bytes: number; daily_bytes: number };
  hourly_over: boolean;
  daily_over: boolean;
  hour_remaining_mb: number | null;
  day_remaining_mb: number | null;
  history: { bucket: number; up: number; down: number }[];
  api?: ApiRetryStats | null;
}

/* ------------------------------- remark / re-mark (§Remark) -------------- */

export interface RemarkSide {
  version: number | null;
  earned: number | null;
  available: number | null;
  percentage: number;
  provisional_grade: string | null;
  marking_model: string | null;
  confidence: string | null;
  verified: string | null;
  created_at: number | null;
  pending: boolean;
  /** False when the attempt failed validation - never the sensible default. */
  reliable: boolean;
}

export interface RemarkQuestionChange {
  question_number: string;
  previous: number | null;
  new: number | null;
  max: number | null;
  kind: "changed" | "added" | "removed";
}

export interface RemarkComparison {
  previous: RemarkSide;
  new: RemarkSide;
  delta_points: number | null;
  percentage_delta: number;
  questions: RemarkQuestionChange[];
  question_changes: number;
}

export interface RemarkStatus {
  result_id: string;
  active: RemarkSide;
  pending: RemarkSide | null;
  pending_version: number | null;
  comparison: RemarkComparison | null;
  can_use_new: boolean;
  awaiting_decision: boolean;
  attempt_count: number;
}

/** One downstream step retried on its own - never the AI marking. */
export type RetryAction = "report" | "upload" | "attach" | "grade";

/* ------------------- correction report -> Google Classroom attachment ---- */

/**
 * Lifecycle of one report PDF on one student's Classroom submission.
 *
 * The failure values are deliberately SPECIFIC (§20): a Drive upload error, a
 * Classroom refusal and a Drive-ownership problem are different things with
 * different fixes, and none of them means the marking itself failed.
 */
export type AttachmentStatus =
  | "PENDING" | "UPLOADED" | "ATTACHED" | "VERIFIED" | "CLEANUP_PENDING"
  | "DRIVE_UPLOAD_FAILED" | "CLASSROOM_ATTACHMENT_FAILED"
  | "ATTACHMENT_VERIFY_FAILED" | "REPORT_ATTACHMENT_OWNERSHIP_ERROR"
  | "FAILED" | "BLOCKED_NOT_MODIFIABLE" | "SKIPPED";

/** Drive is transport, not storage: this is where the temp file got to. */
export type DriveCleanupState =
  | "cleaned" | "pending" | "not_needed"
  | "created" | "uploaded" | "attached" | "confirmed" | "cleanup_failed";

/** The manual route when Google will not let the app attach the file. */
export interface AttachmentFallback {
  pdf_url: string;
  filename: string;
  classroom_url: string | null;
  student_name: string | null;
  submission_id: string | null;
  instructions: string;
}

/**
 * The teacher-facing state of one returned correction (§19).
 *
 * The three facts are reported SEPARATELY and are never inferred from one
 * another: a report can be attached while the Drive cleanup is still pending,
 * and neither one proves a grade was returned to Classroom.
 */
export interface ReportAttachmentState {
  result_id: string;
  status: AttachmentStatus;
  attempts: number;
  error: string | null;
  drive_file_id: string | null;
  report_filename: string | null;
  /** The report is really on the student's submission (verified by re-read). */
  attached: boolean;
  /** A grade is really in Classroom - read back, never assumed. */
  grade_returned: boolean;
  grade_return_note?: string | null;
  /** Where the temporary Drive file got to. */
  drive_cleanup: DriveCleanupState;
  /** The teacher can retry the attachment without re-marking. */
  retryable: boolean;
  course_id?: string | null;
  coursework_id?: string | null;
  submission_id?: string | null;
  student_id?: string | null;
  lifecycle?: string | null;
  owner_email?: string | null;
  cleanup_attempts?: number | null;
  cleanup_error?: string | null;
  previous_drive_file_id?: string | null;
  updated_at?: number | null;
  fallback: AttachmentFallback | null;
}

export interface AttachReportOutcome {
  status: AttachmentStatus;
  message: string;
  result_id?: string;
  drive_file_id?: string;
  report_filename?: string;
  other_attachments?: number;
  /** Present on an ownership refusal: safe diagnostics, never a token (§3). */
  ownership?: {
    ok: boolean;
    reason: string;
    owners?: string[];
    account?: string;
  };
}

export interface ReportCleanupOutcome {
  result_id: string;
  cleanup: {
    ok: boolean;
    skipped?: string;
    deleted?: string;
    reason?: string;
    attempts?: number;
  };
  state: ReportAttachmentState;
}

/* ------------------------------------- product: assignments / attention ---- */

export type AssignmentKind = "managed" | "external" | "local";

export interface ClassOverview {
  id: string;
  name: string;
  section: string | null;
  local: boolean;
  students: number;
  assignments: number;
  submissions: number;
  submitted: number;
  missing: number;
  checked: number;
  review: number;
  average: number | null;
  external_assignments: number;
  last_sync: number | null;
}

export interface ManagementInfo {
  kind: "managed" | "external";
  managed: boolean;
  google_account?: string | null;
  integration_source?: string;
  created_at?: number;
  last_verified_at?: number | null;
  classroom_state?: string | null;
}

export interface AssignmentCapabilities {
  kind: AssignmentKind;
  draft_grade: boolean;
  final_grade: boolean;
  return: boolean;
  attach_report: boolean;
  reason: string | null;
}

export interface AssignmentOverview {
  id: string;
  course_id: string;
  course_name: string | null;
  course_section: string | null;
  title: string;
  max_points: number | null;
  state: string | null;
  due_date: string | null;
  allowlisted: number;
  description: string | null;
  alternate_link: string | null;
  local: boolean;
  managed: boolean;
  management: ManagementInfo;
  students_total: number;
  submitted: number;
  draft: number;
  missing: number;
  reclaimed: number;
  checked: number;
  needs_review: number;
  failed: number;
  processing: number;
  average: number | null;
  has_mark_scheme: boolean;
  last_synced_at: number | null;
}

export interface AssignmentDetail extends AssignmentOverview {
  tracking: TrackingOverview;
  capabilities: AssignmentCapabilities;
}

export interface MarkSchemeQuestion {
  question_number: string;
  max_marks: number;
  criteria?: string;
}

export interface MarkSchemeStructure {
  question_count: number;
  total_marks: number;
  numbering: string[];
  first: number | null;
  last: number | null;
  gaps: string[];
  duplicate_numbers: string[];
  missing_max_marks: string[];
  has_gaps: boolean;
  complete: boolean;
}

/** §17: the independent local reconstruction of the PDF, cross-checked against
 *  the extracted structure. Every number here comes from a different source so
 *  the teacher can see *why* a structure is doubted, not just that it is. */
export interface MarkSchemeVerification {
  /** NONE | IMAGE_ONLY | TEXT - how the PDF could be read locally. */
  extraction_mode?: "NONE" | "IMAGE_ONLY" | "TEXT" | null;
  pdf_pages?: number | null;
  extracted_blocks?: number | null;
  two_column_pages?: number | null;
  image_count?: number | null;
  /** Question numbers the document itself supports (high-confidence only). */
  local_question_candidates?: number;
  local_candidates?: number[];
  page_number_candidates?: number | null;
  /** Sum of the maxima in the stored structure. */
  parser_total?: number;
  /** Sum recomputed from the canonical question structure - never on trust. */
  calculated_total?: number | null;
  /** The total the mark scheme itself prints. */
  printed_total?: number | null;
  totals_agree?: boolean;
  /** 0-1, capped at 0.5 whenever an anomaly survives. */
  structural_confidence?: number;
  /** True only when every independent source agrees. */
  verified?: boolean;
  /** null when there was no local evidence to compare against. */
  evidence_agrees?: boolean | null;
  missing_questions?: (string | number)[];
  anomalies?: string[];
  /** One actionable sentence: what went wrong and what to do about it. */
  likely_cause?: string | null;
}

export interface MarkSchemeValidation {
  coursework_id: string;
  assignment_title: string | null;
  has_mark_scheme: boolean;
  processed: boolean;
  validation: MarkSchemeStructure;
  questions?: MarkSchemeQuestion[];
  total_marks?: number;
  /** Sum of the per-question maxima actually detected. */
  detected_total?: number;
  /** The total the mark scheme itself states. */
  declared_total?: number | null;
  /** §20 cross-check: detected vs stated total agree. */
  totals_agree?: boolean;
  /** §19: HIGH, or NEEDS_REVIEW when the structure cannot be trusted. */
  confidence?: "HIGH" | "NEEDS_REVIEW";
  /** §26: the parser looks suspicious - offer "Verify Mark Scheme". */
  requires_verification?: boolean;
  confidence_reasons?: string[];
  /** Independent PDF reconstruction + cross-validation evidence. */
  verification?: MarkSchemeVerification;
  paper?: string;
  manual_override?: boolean;
  warnings: string[];
  recheck?: {
    previous: MarkSchemeStructure;
    passes: MarkSchemeStructure[];
    disagreement: boolean;
    warnings: string[];
  };
}

export interface AttentionItemData {
  type: string;
  tone: "ok" | "err" | "warn" | "info";
  icon: string;
  title: string;
  detail?: string;
  route: string;
  count: number;
}

export interface AttentionFeed {
  items: AttentionItemData[];
  total: number;
  counts: Record<string, number>;
  generated_at: number;
}

export interface PublishQueueItem {
  submission_id: string;
  student_id: string | null;
  student_name: string | null;
  display_name?: string | null;
  earned: number;
  available: number;
  percentage: number;
  provisional_grade: string | null;
  confidence: string | null;
  review_required: boolean;
  manual_override: boolean;
  suggested_grade: number | null;
  max_points: number | null;
  assigned_grade: number | null;
  draft_grade: number | null;
  current_state: string | null;
}

export interface PublishQueue {
  coursework_id: string;
  course_id: string;
  management: ManagementInfo;
  max_points: number | null;
  items: PublishQueueItem[];
  summary: {
    grades_ready: number; needs_review: number; already_graded: number; total: number;
  };
  capabilities: AssignmentCapabilities;
  recent: {
    id: number; submission_id: string | null; student_name: string | null;
    op_type: string; status: string; http_status: number | null;
    error_code: string | null; message: string | null; created_at: number;
  }[];
}

export type PublishOpStatus =
  | "pending" | "published" | "failed" | "not_supported" | "requires_review"
  | "skipped" | "requires_auth";

export interface PublishOpResult {
  id: number | null;
  status: PublishOpStatus;
  http_status: number | null;
  error_code: string | null;
  message: string | null;
  submission_id?: string;
}

export interface PublishResult {
  coursework_id: string;
  management: ManagementInfo;
  results: PublishOpResult[];
  summary: {
    published: number; failed: number; not_supported: number;
    requires_auth: number; skipped: number; total: number;
    requires_review?: number;
  };
  blocked?: { submission_id: string; reason: string }[];
}

/**
 * Headers every request needs (public mode, §28): a mutating request MUST
 * carry the session's CSRF token or the auth gate answers 403.
 */
function requestHeaders(method: string, extra?: HeadersInit): Record<string, string> {
  const headers: Record<string, string> =
    { ...((extra as Record<string, string>) ?? {}) };
  if (csrfToken && !["GET", "HEAD", "OPTIONS"].includes(method.toUpperCase())) {
    headers["x-mc5-csrf"] = csrfToken;
  }
  return headers;
}

/** One place that turns a failed response into the Error the UI displays. */
function failureError(response: Response, body: unknown, fallback: string): Error {
  const parsed = (body ?? {}) as { code?: string; message?: string };
  // The session expired or public mode needs an account: tell the shell to
  // show the sign-in screen instead of leaving every page broken (§13).
  // A DEAD GOOGLE GRANT is not a dead local session — popping the sign-in
  // screen sends the teacher to log in again when the fix is reconnecting
  // Google Classroom (code produced by routes_all._classroom_error and the
  // global handler), so that code must not fire the event.
  if ((response.status === 401 || parsed.code === "auth_not_configured")
      && parsed.code !== "classroom_reconnect_required") {
    window.dispatchEvent(new CustomEvent("mc5:auth-required"));
  }
  return new Error(parsed.message ?? `${fallback} (HTTP ${response.status})`);
}

export async function api<T>(path: string, init?: RequestInit): Promise<T> {
  const method = (init?.method ?? "GET").toUpperCase();
  const headers = requestHeaders(method, init?.headers);
  headers["Content-Type"] = "application/json";
  const response = await fetch(`/api${path}`, { ...init, headers });
  const body = await response.json().catch(() => ({}));
  if (!response.ok) throw failureError(response, body, "Request failed");
  return body as T;
}

/**
 * Upload a file (multipart/form-data) with the SAME session and CSRF rules as
 * ``api``.
 *
 * Hand-rolling `fetch()` for an upload is what silently broke every mark
 * scheme upload in public mode: the request reached the server WITHOUT the
 * session's CSRF token, the auth gate answered 403 "This request could not be
 * verified.", and nothing was ever logged server-side because the request
 * never reached the route. The browser must set the multipart boundary itself,
 * which is why this cannot reuse ``api`` (it always sends JSON).
 */
/**
 * Fire-and-forget request that must outlive the page that sent it.
 *
 * ``fetch(..., {keepalive: true})`` is the only way to send something while the
 * document is being torn down (``pagehide``/``unload``), so this deliberately
 * avoids ``api()`` — the request needs no parsed answer and must not throw into
 * a page that is going away.
 */
export function apiKeepalive(path: string): void {
  try {
    void fetch(`/api${path}`, {
      method: "GET", keepalive: true, headers: requestHeaders("GET"),
      cache: "no-store",
    }).catch(() => { /* the page is going away; nothing to report */ });
  } catch {
    /* keepalive unavailable: the session simply times out instead */
  }
}

export async function postForm<T>(path: string, form: FormData): Promise<T> {
  const response = await fetch(`/api${path}`, {
    method: "POST", body: form, headers: requestHeaders("POST"),
  });
  const body = await response.json().catch(() => ({}));
  if (!response.ok) throw failureError(response, body, "Upload failed");
  return body as T;
}

/* ------------------------------------------------- session / public access */

export interface AuthStatus {
  public_mode: boolean;
  required: boolean;
  login_required?: boolean;
  configured: boolean;
  authenticated: boolean;
  username: string | null;
  display_name?: string | null;
  role?: "developer" | "teacher" | "coteacher" | "assistant" | null;
  account_id?: string | null;
  csrf: string | null;
  local_request: boolean;
  public_url: string | null;
  local_url: string;
}

let csrfToken: string | null = null;

/** Store the CSRF token of the current session (public mode only). */
export function setCsrfToken(token: string | null): void {
  csrfToken = token;
}

/** The current auth state — /api/auth/status is public (no session needed). */
export function authStatus(): Promise<AuthStatus> {
  return api<AuthStatus>("/auth/status");
}

/**
 * Create or change the HOST account (§5).
 *
 * The server stores only a salted hash, requires the current password for a
 * change, refuses a password shorter than 10 characters, and is rate-limited
 * exactly like sign-in. No value sent here is ever echoed back.
 */
export function setHostAccount(payload: {
  username?: string; password: string; current_password?: string;
}): Promise<{ configured: boolean; username: string | null }> {
  return api("/auth/setup", {
    method: "POST", body: JSON.stringify(payload),
  });
}

export interface RemoteAccessReachability {
  local: boolean;
  lan: boolean;
  public: boolean;
  api: boolean;
  websocket: "not_required" | "working";
  events: "sse" | "polling";
}

export interface RemoteAccessStatus {
  public_mode: boolean;
  status: "online" | "starting" | "offline";
  public_url: string | null;
  reason: string | null;
  last_verified: string | null;
  verified_checks: Record<string, boolean>;
  port: number;
  local_url: string;
  lan_url: string | null;
  lan_enabled: boolean;
  updated_at: number | null;
  reachable?: RemoteAccessReachability;
}

/* ------------------------------------------ access switches (Settings page) */

/**
 * The PUBLIC ADDRESS state: the saved preference and the measured reality.
 *
 * ``requested`` is what the user asked for (persisted, survives restarts);
 * ``status`` is what the backend measured right now. They are deliberately
 * separate so a tunnel that failed to start is never displayed as "on", and
 * ``public_url`` is only ever present while a verification stands — a dead
 * address is never published, only remembered (``last_address``).
 */
export type PublicAccessStatus = "disabled" | "starting" | "online" | "failed";

export interface PublicAddressState {
  requested: boolean;
  status: PublicAccessStatus;
  /** Present ONLY while status === "online" (verified end to end). */
  public_url: string | null;
  reason: string | null;
  /** The last verified address, shown as INACTIVE when switched off. */
  last_address: string | null;
  managed_by: "application" | "launcher" | "external" | null;
  checks: Record<string, boolean>;
  last_verified: number | null;
  port: number;
  tunnel_running: boolean;
  cloudflared_available: boolean;
  can_enable: boolean;
  blocked_reason: string | null;
  mode: "public" | "local";
}

/** Multi-account mode: whether the layer is available on THIS installation. */
export interface MultiAccountCapabilities {
  multi_account: boolean;
  default: boolean;
  source: "env" | "config" | "default";
  setting: string;
  env_var: string;
  restart_required: boolean;
}

export interface AccessState {
  /** "local" (this computer) or a Developer's username; null = not the owner. */
  owner: string | null;
  /** null when the caller is not the installation's owner (never a guess). */
  public_address: PublicAddressState | null;
  multi_account: MultiAccountCapabilities;
}

/** Both switches of the access panel (owner-only detail, never a secret). */
export function accessState(): Promise<AccessState> {
  return api<AccessState>("/access/state");
}

/**
 * Turn the PUBLIC ADDRESS on/off — this starts or stops the real tunnel.
 *
 * ON  -> the backend creates the tunnel, verifies the address end to end and
 *        only then reports it; a failure returns the switch to OFF with a
 *        reason instead of pretending it worked.
 * OFF -> the tunnel process is stopped and the address stops being advertised
 *        anywhere in the app (the setting stays saved for next time).
 */
export function setPublicAddress(enabled: boolean): Promise<{
  public_address: PublicAddressState; multi_account: MultiAccountCapabilities }> {
  return api("/access/public-address", {
    method: "POST", body: JSON.stringify({ enabled }) });
}

/** Re-run the full external verification now (the "Re-check" action). */
export function recheckPublicAddress(): Promise<{
  public_address: PublicAddressState }> {
  return api("/access/public-address/recheck", { method: "POST" });
}

/** Turn MULTI-ACCOUNT MODE on/off (nothing is ever deleted either way). */
export function setMultiAccount(enabled: boolean): Promise<{
  multi_account: MultiAccountCapabilities }> {
  return api("/access/multi-account", {
    method: "POST", body: JSON.stringify({ enabled }) });
}

export function timeAgo(ts: number | null | undefined): string {
  if (!ts) return "—";
  const seconds = Math.floor(Date.now() / 1000 - ts);
  if (seconds < 5) return "just now";
  if (seconds < 60) return `${seconds}s ago`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)}h ago`;
  return `${Math.floor(seconds / 86400)}d ago`;
}

export function elapsed(from: number | null, to: number | null | undefined): string {
  if (!from) return "—";
  const end = to ?? Date.now() / 1000;
  const seconds = Math.max(0, Math.floor(end - from));
  if (seconds < 60) return `${seconds}s`;
  return `${Math.floor(seconds / 60)}m ${seconds % 60}s`;
}

/* ------------------------------------------------------- canonical student name */

/** Anything that can carry a student's identity. */
export interface NamedStudent {
  display_name?: string | null;
  student_name?: string | null;
  nickname?: string | null;
  classroom_name?: string | null;
  class_name?: string | null;
  class_label?: string | null;
}

/**
 * THE student display name, in one place.
 *
 * The server already resolves this (nickname -> Google Classroom name) and
 * sends it as `display_name`; this exists so no page invents its own fallback
 * and shows the teacher a different identity on a different screen. It is
 * display only: identity everywhere else is the Google Classroom student id.
 */
export function displayName(row: NamedStudent | null | undefined,
                           fallback = "Student"): string {
  if (!row) return fallback;
  const name = row.display_name?.trim()
    || row.nickname?.trim()
    || row.classroom_name?.trim()
    || row.student_name?.trim();
  return name || fallback;
}

/** Shown where a report must never print a blank class cell. */
export const UNASSIGNED_CLASS = "Not assigned";

/**
 * The student's class, as it should READ anywhere.
 *
 * Teacher-assigned class first, then the Classroom course as a fallback, then
 * "Not assigned" — never an empty string, which reads as a bug on a page and
 * as a blank box in a printed report.
 */
export function classLabel(row: NamedStudent | null | undefined,
                           fallback = UNASSIGNED_CLASS): string {
  const assigned = row?.class_name?.trim();
  if (assigned) return assigned;
  return row?.class_label?.trim() || fallback;
}

/* ------------------------------------------------- automated correction (§1-§52) */

/** One persistent workflow job. `status` is an explicit state-machine value. */
export interface AutomationJob {
  id: string;
  course_id: string;
  coursework_id: string;
  assignment_title: string | null;
  student_id: string | null;
  student_name: string | null;
  submission_id: string;
  submission_version: string;
  status: string;
  status_label: string;
  stage_index: number;
  /**
   * Which REAL correction pipeline handled this submission (§8).
   *
   * Derived from the large-PDF jobs table, so it states a fact: a submission
   * the router sent to the batched worker has a row there. ``null`` means the
   * job has not reached correction yet and no pipeline is claimed.
   */
  pipeline?: "standard" | "batched" | null;
  pipeline_label?: string | null;
  step_detail: string | null;
  marks_obtained: number | null;
  maximum_marks: number | null;
  percentage: number | null;
  grade: string | null;
  report_filename: string | null;
  drive_file_id?: string | null;
  attempts: number;
  max_attempts: number;
  error: string | null;
  error_code: string | null;
  created_at: number;
  updated_at: number;
  /** When the job reached its final state (null while it is still running). */
  finished_at?: number | null;
  completed_at: number | null;
  resumable: boolean;
  /** The step a Retry would resume at, straight from the backend's rule. */
  retry_from?: string | null;
  /** A verified Drive file id is on the job - never inferred from status. */
  report_uploaded?: boolean;
  /** Whether a correction result exists to attach a report for. */
  has_result?: boolean;
  /** Held in review mode, waiting for a teacher to approve the grade. */
  awaiting_approval?: boolean;
  /** Has a result + grade and can be approved (publish + return) now. */
  approvable?: boolean;
  /** The REAL per-stage attachment state (status, error, lifecycle). */
  attachment?: {
    status: string; error: string | null; drive_file_id: string | null;
    report_filename: string | null; attached: boolean; retryable: boolean;
    drive_cleanup: string;
  } | null;
  result?: {
    id: string; earned: number; available: number; percentage: number;
    provisional_grade: string | null; review_required: number;
    manual_override: number;
    double_check_triggered: number; double_check_status: string | null;
    double_check_reason: string | null; disagreement_status: string | null;
    first_earned: number | null; first_available: number | null;
    second_earned: number | null; second_available: number | null;
    confidence: string | null; confidence_reasons: string | null;
  } | null;
  /** Per-stage publication status (report / grade / return), recorded by the
   *  backend — Classroom operations are separate API calls, so each stage is
   *  reported individually rather than as one "published" flag. */
  publication?: {
    report: string; report_sent: boolean;
    grade: string; grade_published: boolean;
    returned: boolean; awaiting_approval: boolean; complete: boolean;
  } | null;
}

export interface AutomationAssignment {
  coursework_id: string;
  course_id: string;
  title: string;
  max_points: number | null;
  enabled: boolean;
  last_checked_at: number | null;
  last_error: string | null;
  markscheme: boolean;
  stats: {
    submitted: number; completed: number; processing: number;
    waiting: number; failed: number; total: number;
  };
}

export interface AutomationStatus {
  enabled: boolean;
  interval_minutes: number;
  intervals: number[];
  last_check: number | null;
  next_check: number | null;
  running: boolean;
  /** Background worker (real backend state — never a local toggle). */
  worker_enabled: boolean;
  worker_state: "running" | "stopped" | "stalled";
  thread_running: boolean;
  last_activity: number | null;
  pending_jobs: number;
  active_jobs: number;
  assignments_enabled: number;
  /** Review mode: every grade is held as a teacher-approved draft. */
  review_mode: boolean;
  draft_only: boolean;
  attach_report: boolean;
  submit_grade: boolean;
  return_work: boolean;
  /** PDF profile attached to the student's submission. "detailed" is the
   *  whole report: a block per question with the student's answer, the mark
   *  scheme and the marking comment. */
  report_detail: "summary" | "standard" | "detailed";
  counts: {
    total: number; completed: number; processing: number;
    waiting: number; failed: number; needs_attention: number;
  };
  by_state: Record<string, number>;
  pipeline: { state: string; label: string }[];
}

export interface AutomationLogEntry {
  id: number;
  created_at: number;
  level: "info" | "success" | "warning" | "error";
  event: string;
  message: string | null;
  automation_job_id: string | null;
  course_id: string | null;
  coursework_id: string | null;
  submission_id: string | null;
}

export function automationStatus(): Promise<AutomationStatus> {
  return api<AutomationStatus>("/automation/status");
}

export function automationAssignments(): Promise<{
  assignments: AutomationAssignment[]; enabled_count: number;
}> {
  return api("/automation/assignments");
}

export function automationJobs(params = ""): Promise<{ jobs: AutomationJob[] }> {
  return api(`/automation/jobs${params}`);
}

/** One job with its result, for the visualizer's click-through detail. */
export function automationJob(jobId: string): Promise<AutomationJob> {
  return api<AutomationJob>(`/automation/jobs/${encodeURIComponent(jobId)}`);
}

export function automationLog(limit = 100): Promise<{ entries: AutomationLogEntry[] }> {
  return api(`/automation/log?limit=${limit}`);
}

export function automationSettings(payload: Record<string, unknown>) {
  return api<AutomationStatus>("/automation/settings", {
    method: "POST", body: JSON.stringify(payload),
  });
}

/** Background Worker ON/OFF — returns the VERIFIED backend state. */
export function automationWorker(enabled: boolean) {
  return api<AutomationStatus>("/automation/worker", {
    method: "POST", body: JSON.stringify({ enabled }),
  });
}

export function automationSetAssignment(courseworkId: string, enabled: boolean) {
  return api(`/automation/assignments/${courseworkId}`, {
    method: "POST", body: JSON.stringify({ enabled }),
  });
}

export function automationRetryJob(jobId: string) {
  return api<AutomationJob>(`/automation/jobs/${jobId}/retry`, { method: "POST" });
}

/**
 * Teacher approval of a review-mode (or reviewed-flagged) correction: publishes
 * the FINAL grade and returns the work. Reuses the verified grade; never
 * re-runs the AI or recomputes the mark.
 */
export function automationApprove(jobId: string) {
  return api<AutomationJob>(`/automation/jobs/${jobId}/approve`, { method: "POST" });
}

/**
 * Repair ONLY the report upload/attachment for a job. Reuses the existing
 * correction, score, grade and PDF, so the AI is never re-run.
 */
export function automationRetryReport(jobId: string) {
  return api<AutomationJob>(`/automation/jobs/${jobId}/retry-report`, {
    method: "POST",
  });
}

export function automationRunNow() {
  return api("/automation/run-now", { method: "POST" });
}

/* ------------------------------------------ Gemini prompt page (Developer only) */

export interface DevPrompt {
  id: string;
  when: string;
  transport: string;
  edited: boolean;
  text: string;
  builtin?: string;
  text_with_blueprint?: string;
}

export interface DevPrompts {
  role: string;
  engine_source: string;
  overrides: {
    system_prompt_override: string | null;
    user_prompt_override: string | null;
    active: boolean;
  };
  models: { chain: string[]; double_check_second_pass: string[] };
  sampling: Record<string, Record<string, number>>;
  prompts: DevPrompt[];
  response_schema?: Record<string, unknown>;
  updated?: string[];
}

/** The EXACT prompts the marking engine sends (Developer-role only). */
export function devPrompts(): Promise<DevPrompts> {
  return api<DevPrompts>("/dev/prompts");
}

/** Save a prompt override; an empty string restores the built-in prompt. */
export function saveDevPrompts(payload: {
  system_prompt_override?: string;
  user_prompt_override?: string;
}): Promise<DevPrompts> {
  return api<DevPrompts>("/dev/prompts", {
    method: "POST", body: JSON.stringify(payload),
  });
}

/* ------------------------------------------ Large PDF Worker */

export interface LargePdfBatch {
  batch_index: number;
  batch_number: number;
  page_start: number;
  page_end: number;
  pages: number;
  status: string;
  status_label: string;
  question_ids: string[];
  validated_questions: number | null;
  missing_questions: string[];
  ambiguous: boolean;
  attempts: number;
  error: string | null;
  error_code: string | null;
  finished_at: number | null;
}

export interface LargePdfSummary {
  total: number; pending: number; processing: number;
  completed: number; failed: number; review: number;
}

export interface LargePdfJob {
  id: string;
  student_name: string | null;
  student_id?: string | null;
  submission_id: string | null;
  coursework_id: string | null;
  status: string;
  status_label: string;
  step_detail: string | null;
  page_count: number;
  batch_count: number;
  result_id: string | null;
  attempts: number;
  error: string | null;
  error_code: string | null;
  retryable: boolean;
  awaiting_review: boolean;
  created_at: number;
  updated_at: number;
  finished_at: number | null;
  summary?: LargePdfSummary;
  batches?: LargePdfBatch[];
  plan?: {
    page_count: number;
    single_pass: boolean;
    batch_count: number;
    batches: { index: number; start: number; end: number; pages: number }[];
  } | null;
  plan_notes?: string[];
}

export interface LargePdfProgress {
  stage: { state: string; label: string };
  current_batch: number | null;
  batch_total: number | null;
  page_count: number;
  pages_done: number;
  current_batch_pages: { start: number; end: number } | null;
  questions_total: number | null;
  questions_marked: number;
  batch_summary: {
    total: number; pending: number; processing: number;
    completed: number; failed: number; review: number;
  };
}

export interface LargePdfListing {
  config: {
    enabled: boolean; threshold_pages: number; target_min: number;
    target_max: number; concurrency: number;
  };
  counts: {
    queued: number; inspecting: number; planning: number; ready: number;
    processing: number; awaiting_review: number; completed: number; failed: number;
  };
  jobs: LargePdfJob[];
  progress?: Record<string, LargePdfProgress>;
}

export function largePdfJobs(): Promise<LargePdfListing> {
  return api<LargePdfListing>("/large-pdf/jobs");
}

export function largePdfJob(jobId: string): Promise<LargePdfJob> {
  return api<LargePdfJob>(`/large-pdf/jobs/${encodeURIComponent(jobId)}`);
}

/** Upload a student answer PDF + mark scheme; inspects and plans batches. */
export function largePdfCreate(form: FormData): Promise<LargePdfJob> {
  return postForm<LargePdfJob>("/large-pdf/jobs", form);
}

export function largePdfProcess(jobId: string): Promise<LargePdfJob> {
  return api<LargePdfJob>(`/large-pdf/jobs/${encodeURIComponent(jobId)}/process`,
    { method: "POST" });
}

export function largePdfRetryJob(jobId: string): Promise<LargePdfJob> {
  return api<LargePdfJob>(`/large-pdf/jobs/${encodeURIComponent(jobId)}/retry`,
    { method: "POST" });
}

export function largePdfRetryBatch(jobId: string, batchIndex: number): Promise<LargePdfJob> {
  return api<LargePdfJob>(
    `/large-pdf/jobs/${encodeURIComponent(jobId)}/batches/${batchIndex}/retry`,
    { method: "POST" });
}

export function largePdfCancel(jobId: string): Promise<LargePdfJob> {
  return api<LargePdfJob>(`/large-pdf/jobs/${encodeURIComponent(jobId)}/cancel`,
    { method: "POST" });
}

/* =============================================== unified PDF correction (§3) */

/** The real routing facts (page threshold, whether batching is enabled). */
export interface PipelineInfo {
  threshold_pages: number;
  large_pdf_enabled: boolean;
  options: { value: "auto" | "standard" | "large";
             label: string; detail: string }[];
}

export function correctionPipelines(): Promise<PipelineInfo> {
  return api<PipelineInfo>("/corrections/pipelines");
}

export interface UnifiedCorrectionResult {
  /** Which EXISTING pipeline actually ran. */
  applied_pipeline: "standard" | "large";
  job_id?: string;
  large_pdf_job_id?: string;
  page_count: number;
  page_limit: number;
  reason: string;
  files: Record<string, { name: string; bytes: number }>;
  job: Job & Partial<LargePdfJob>;
}

/**
 * Upload one student PDF plus its mark scheme. The backend decides the pipeline
 * from the student PDF's real page count (or the teacher's explicit override),
 * and the response names the pipeline that ran.
 */
export function unifiedCorrection(form: FormData): Promise<UnifiedCorrectionResult> {
  return postForm<UnifiedCorrectionResult>("/corrections/unified", form);
}

/* ================================================= Google Classroom sync (§7) */

/**
 * The ONE sync implementation every page uses.
 *
 * ``/classroom/sync/start`` begins (or joins) the single in-flight sync and
 * returns at once; ``/classroom/sync/progress`` then reports the real stage,
 * message and progress from the backend's own run registry — never a client
 * timer. This is the same sync the Google Classroom page streams.
 */
export interface SyncSnapshot {
  id: string | null;
  kind: "full" | "assignment" | null;
  target: Record<string, string>;
  running: boolean;
  stage: string | null;
  message: string | null;
  progress: number;
  started_at: number | null;
  finished_at: number | null;
  elapsed_s: number | null;
  summary: Record<string, unknown> | null;
  error: string | null;
  event_count: number;
}

export function syncClassroomStart(courseId?: string, courseworkId?: string) {
  const params = new URLSearchParams();
  if (courseId && courseworkId) {
    params.set("course_id", courseId);
    params.set("coursework_id", courseworkId);
  }
  const suffix = params.toString() ? `?${params}` : "";
  return api<{ started: boolean; snapshot: SyncSnapshot }>(
    `/classroom/sync/start${suffix}`, { method: "POST" });
}

export function syncClassroomProgress(): Promise<SyncSnapshot> {
  return api<SyncSnapshot>("/classroom/sync/progress");
}

/* ======================================================= data portability (§2) */

export interface DataInventory {
  counts: Record<string, number>;
  database_bytes: number;
  export: {
    format: string;
    format_version: number;
    content: string;
    filename: string;
    sections: string[];
    excluded_tables: Record<string, string>;
  };
  schema_version: number;
  app_version: string;
}

export interface ImportSection {
  table: string;
  label: string;
  total: number;
  new: number;
  matching: number;
  invalid: number;
  unsupported?: boolean;
}

export interface ImportPreview {
  ok: boolean;
  format: string;
  format_version: number;
  app_version: string | null;
  exported_at_iso: string | null;
  contains_secrets: boolean;
  sections: ImportSection[];
  totals: { new: number; matching: number; invalid: number };
  invalid_rows: { table: string; row: Record<string, unknown>; reason: string }[];
  warnings: string[];
  policies: { default: string; options: { value: string; label: string; detail: string }[] };
  filename?: string;
  bytes?: number;
}

export interface ImportReport {
  applied: boolean;
  mode: string;
  backup_path: string | null;
  sections: { table: string; label: string; added: number; updated: number;
              skipped: number; invalid: number }[];
  totals: { added: number; updated: number; skipped: number; invalid: number };
  invalid_rows: { table: string; row: Record<string, unknown>; reason: string }[];
  note: string;
  filename?: string;
}

export function dataInventory(): Promise<DataInventory> {
  return api<DataInventory>("/data/inventory");
}

export function dataImportPreview(file: File): Promise<ImportPreview> {
  const form = new FormData();
  form.append("file", file);
  return postForm<ImportPreview>("/data/import/preview", form);
}

export function dataImport(file: File, mode: "add_only" | "update_existing") {
  const form = new FormData();
  form.append("file", file);
  form.append("mode", mode);
  if (mode === "update_existing") form.append("confirm", "replace");
  return postForm<ImportReport>("/data/import", form);
}
