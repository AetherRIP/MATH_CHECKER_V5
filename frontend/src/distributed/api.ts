// Typed client for the distributed multi-school layer (/api/distributed/*).
//
// Auth model (§59): the backend issues a bearer token on login; the server
// derives role/scope from the token — the client never declares its role.
// The token lives in localStorage so an Assistant's dashboard survives a page
// reload without re-typing credentials.

const TOKEN_KEY = "mc5.distributed.token";
const SESSION_KEY = "mc5.distributed.session";

/* ------------------------------------------------------------------ types */

export type Role = "DEVELOPER" | "TEACHER" | "CO_TEACHER" | "ASSISTANT";

export interface Scope {
  school_ids: string[];
  class_ids: string[];
}

export interface DistributedSession {
  token?: string; // only on login response
  account_id: string;
  username: string;
  display_name: string;
  role: Role;
  permissions: string[];
  scope: Scope;
  app_version: string;
}

export interface School {
  id: string;
  name: string;
  code: string | null;
  active: number | boolean;
  created_at: number;
  updated_at: number;
}

export interface ClassInfo {
  id: string;
  school_id: string;
  name: string;
  academic_year: string | null;
  active: number | boolean;
}

export interface SchoolsFeed {
  schools: School[];
  classes: ClassInfo[];
}

export interface AccountRow {
  id: string;
  username: string;
  display_name: string;
  role: Role;
  roles?: Role[];
  active: number | boolean;
  last_login: number | null;
  last_device_id: string | null;
  created_at: number;
}

export interface AccountList {
  accounts: AccountRow[];
  permissions_hint: string[];
}

export type Presence =
  | "ONLINE" | "ONLINE_CHECKING" | "AVAILABLE" | "BUSY" | "PAUSED"
  | "OFFLINE" | "ERROR" | "UPDATING" | "RECOVERY";

export interface WorkerRow {
  worker_id: string;
  device_id: string;
  account_id: string | null;
  account_name: string | null;
  account_role: string | null;
  device_label: string | null;
  app_version: string | null;
  agent_version: string | null;
  status: string | null;
  presence: Presence | string;
  last_heartbeat: number | null;
  current_job_id: string | null;
  label: string | null;
}

export interface ErrorReport {
  id: string;
  ts: number;
  error_type: string;
  message: string;
  failed_stage: string | null;
  account_id: string | null;
  device_id: string | null;
  worker_id: string | null;
  resolved: number | boolean;
  context: Record<string, unknown>;
  session_events: unknown[];
  stack_trace?: string | null;
}

export type UpdateState =
  | "CURRENT" | "UPDATE_AVAILABLE" | "DOWNLOADING" | "VERIFYING"
  | "WAITING_FOR_IDLE" | "INSTALLING" | "HEALTH_CHECK" | "UPDATED"
  | "FAILED" | "ROLLED_BACK" | "RECOVERY_REQUIRED" | "OFFLINE";

export interface UpdateRequestRow {
  update_id: string;
  target_version: string;
  target_component: string;
  minimum_supported_version: string | null;
  release_notes: string | null;
  priority: string;
  required: number | boolean;
  auto_install: number | boolean;
  scope: { kind?: string } & Record<string, unknown>;
  package_sha256: string | null;
  created_by: string;
  created_at: number;
}

export interface UpdateHistoryRow {
  id: number;
  device_id: string;
  update_id: string | null;
  old_version: string;
  new_version: string;
  result: string;
  failure_reason: string | null;
  rollback_result: string | null;
  started_at: number | null;
  completed_at: number | null;
}

export interface AuditRow {
  id: number;
  event_id: string;
  ts: number;
  action: string;
  actor_account: string | null;
  actor_role: string | null;
  affected_account: string | null;
  school_id: string | null;
  team_id: string | null;
  outcome: "success" | "failure" | "denied" | string;
  target_type: string | null;
  target_id: string | null;
  details: Record<string, unknown>;
}

/** A row of `correction_results_sync` exactly as the API returns it. The
 *  primary key column is `id` — NOT `result_id`, which only exists inside the
 *  payload / ownership tables — so rows must be keyed on `id`. */
export interface CorrectionResultRow {
  id: string;
  submission_id: string;
  submission_version: string;
  worker_id: string;
  account_id: string | null;
  device_id: string | null;
  school_id: string | null;
  class_id: string | null;
  payload: Record<string, unknown>;
  received_at: number;
}

export interface CorrectionOutcome {
  duplicate?: boolean;
  result_id?: string;
  [k: string]: unknown;
}

/* ---------------------------------------------------------------- session */

export function storedToken(): string | null {
  return window.localStorage.getItem(TOKEN_KEY);
}

export function storedSession(): DistributedSession | null {
  try {
    const raw = window.localStorage.getItem(SESSION_KEY);
    return raw ? (JSON.parse(raw) as DistributedSession) : null;
  } catch {
    return null;
  }
}

export function clearSession(): void {
  window.localStorage.removeItem(TOKEN_KEY);
  window.localStorage.removeItem(SESSION_KEY);
}

/* -------------------------------------------------- shared backend address */

const SHARED_BACKEND_KEY = "mc5.distributed.backend";

/**
 * The SHARED backend this dashboard talks to.
 *
 * Empty means "the backend that served this page", i.e. a single-machine
 * install — exactly the previous behaviour. In a multi-laptop deployment every
 * other installation points here at the one always-on backend, e.g.
 * `http://teacher-pc:8642` or `https://maths.example.org`, so the Developer,
 * Teacher and Assistant dashboards all read the SAME accounts, teams, devices,
 * shared reports and audit events.
 */
export function sharedBackend(): string {
  try {
    return (window.localStorage.getItem(SHARED_BACKEND_KEY) ?? "")
      .trim().replace(/\/+$/, "");
  } catch {
    return "";
  }
}

/**
 * Point this installation at a different shared backend.
 *
 * The bearer token belongs to the backend that ISSUED it, so changing the
 * address signs the current session out rather than sending a stale token to a
 * different server.
 */
export function setSharedBackend(url: string): void {
  const next = (url ?? "").trim().replace(/\/+$/, "");
  if (next !== sharedBackend()) clearSession();
  try {
    if (next) window.localStorage.setItem(SHARED_BACKEND_KEY, next);
    else window.localStorage.removeItem(SHARED_BACKEND_KEY);
  } catch { /* private mode: fall back to same-origin for this page load */ }
}

/** The origin every distributed request is sent to. */
export function distributedOrigin(): string {
  return sharedBackend() || window.location.origin;
}

/** Tiny Agent switch + the OBSERVED reality (never just the preference). */
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

/**
 * Read the Tiny Agent's ON/OFF preference together with what really happened.
 *
 * `label` is derived server-side and refuses to say "running" without a recent
 * heartbeat, so the UI can show the agent's real state rather than the switch.
 */
export function agentState(): Promise<AgentState> {
  return distApi<AgentState>("/agent/state");
}

/** Turn the Tiny Agent ON/OFF (its own process re-reads this immediately). */
export function setAgentState(enabled: boolean): Promise<AgentState> {
  return distApi<AgentState>("/agent/state", {
    method: "POST", body: JSON.stringify({ enabled }),
  });
}

/**
 * Why a request to the configured backend failed, in the operator's language.
 *
 * Inside `fetch` a cross-origin reply the browser REFUSED is indistinguishable
 * from a server that is switched off, so "check the address" alone sends the
 * operator to the wrong place. When the address is another machine, say which
 * exact origin that machine must list in MC5_ALLOWED_ORIGINS — including the
 * reminder that hostnames are compared literally, because `http://localhost:8645`
 * and `http://127.0.0.1:8645` are two different origins (§ shared backend).
 */
function unreachableMessage(): string {
  const target = distributedOrigin();
  const origin = window.location.origin;
  const base = `Cannot reach the shared backend at ${target} — check the `
    + "address and that the backend computer is running.";
  if (!sharedBackend() || sharedBackend() === origin) return base;
  return `${base} If it IS running, its MC5_ALLOWED_ORIGINS must list this `
    + `page's exact origin ${origin} (localhost and 127.0.0.1 are different `
    + "origins), or the browser refuses the reply.";
}

/**
 * Ask the configured backend whether it is really there.
 *
 * Public on the backend (``GET /api/distributed/ping``) so it works before a
 * sign-in, and answers with only the service name and version. This is what a
 * "connected" badge must be based on — never on the page having loaded.
 */
export async function pingBackend(): Promise<{ ok: boolean; service: string;
  version: string }> {
  let response: Response;
  try {
    response = await fetch(`${sharedBackend()}/api/distributed/ping`,
      { method: "GET" });
  } catch {
    throw new Error(unreachableMessage());
  }
  const body = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw new Error(`The address ${distributedOrigin()} answered HTTP `
      + `${response.status} — it may not be a Math Checker backend.`);
  }
  return body as { ok: boolean; service: string; version: string };
}

/* --------------------------------------------- capabilities (§ multi-account) */

/**
 * Which optional layers THIS backend offers (public route, no token required).
 *
 * Read before sign-in as well as after, so the login screen and the dashboard
 * hide multi-account surfaces exactly when the backend refuses those
 * operations. The interface follows this value; it is never the protection —
 * the backend answers 403 ``multi_account_disabled`` by itself.
 */
export interface MultiAccountMode {
  multi_account: boolean;
  default: boolean;
  source: "env" | "config" | "default";
  setting: string;
  env_var: string;
  restart_required: boolean;
}

export async function distributedCapabilities(): Promise<MultiAccountMode> {
  return distApi<MultiAccountMode>("/capabilities");
}

/* ----------------------------------------------------------------- client */

/** fetch wrapper for /api/distributed/* with the bearer token attached. */
export async function distApi<T>(
  path: string,
  init?: RequestInit & { token?: string },
): Promise<T> {
  const token = init?.token ?? storedToken();
  const headers: Record<string, string> = {
    ...((init?.headers as Record<string, string>) ?? {}),
    "Content-Type": "application/json",
  };
  if (token) headers["Authorization"] = `Bearer ${token}`;
  let response: Response;
  try {
    response = await fetch(`${sharedBackend()}/api/distributed${path}`,
      { ...init, headers });
  } catch {
    // A network/CORS failure is ACTIONABLE, not an internal error: name the
    // backend that could not be reached AND, for another machine, the origin
    // its allow list has to contain.
    throw new Error(unreachableMessage());
  }
  const body = await response.json().catch(() => ({}));
  if (!response.ok) {
    const err = new Error(
      (body as { message?: string }).message ??
        `Request failed (HTTP ${response.status})`);
    // A 401 whose code says the TOKEN is dead (auth_required/auth_invalid)
    // must drop the stale session so the login screen shows instead of every
    // panel erroring. A 401 `bad_credentials` is different: the token is fine,
    // the user merely typed the wrong CURRENT password on /change-password —
    // signing them out there would punish a typo (§ password page).
    if (response.status === 401) {
      const code = (body as { code?: string }).code;
      if (code !== "bad_credentials") clearSession();
    }
    throw err;
  }
  return body as T;
}

/* ----------------------------------------------------- sync monitor (§8) */

/** One registered agent installation with DERIVED presence.
 *  `stale` is computed from the heartbeat, so a worker whose stored status
 *  still says ONLINE but has gone quiet is never shown as running. */
export interface SyncAgent {
  worker_id: string;
  device_id: string;
  device_label: string | null;
  account_name: string | null;
  account_role: string | null;
  app_version: string;
  agent_version: string;
  status: string | null;
  presence: string;
  last_heartbeat: number | null;
  seconds_since_heartbeat: number | null;
  stale: boolean;
  current_job_id: string | null;
  registered_at: number | null;
}

export interface ReportDeliverySummary {
  pending: number; sending: number; delivered: number;
  retry_scheduled: number; failed: number; needs_review: number; total: number;
}

export interface SyncRetry {
  result_record_id: string;
  submission_id: string | null;
  report_filename: string | null;
  state: string;
  attempts: number;
  max_attempts: number;
  last_error: string | null;
  next_retry_at: number | null;
  updated_at: number;
  delivered_at: number | null;
}

export interface SyncDuplicate {
  submission_id: string;
  versions: number;
  workers: number;
  records: number;
  last_seen: number;
}

export interface SyncErrorRow {
  id: string; ts: number; error_type: string | null;
  failed_stage: string | null; message: string | null;
  worker_id: string | null; device_id: string | null;
  account_id: string | null; resolved: 0 | 1 | null;
}

export interface SyncEventRow {
  ts: number; action: string; target_type: string | null;
  target_id: string | null; actor_account: string | null;
  details: Record<string, unknown>;
}

export interface SyncMonitorSnapshot {
  generated_at: number;
  app_version: string;
  agents: SyncAgent[];
  agents_summary: {
    total: number; reporting: number; silent: number; busy: number;
    app_versions: Record<string, number>;
    agent_versions: Record<string, number>;
  };
  sync: {
    reports: ReportDeliverySummary;
    results_synced: number;
    latest_event_seq: number;
  };
  retries: SyncRetry[];
  duplicates: SyncDuplicate[];
  recent_errors: SyncErrorRow[];
  sync_events: SyncEventRow[];
}

export function syncMonitor(): Promise<SyncMonitorSnapshot> {
  return distApi<SyncMonitorSnapshot>("/sync-monitor");
}

export interface ReportDeliveryList {
  deliveries: SyncRetry[];
  summary: ReportDeliverySummary;
}

export function listReportDeliveries(limit = 200): Promise<ReportDeliveryList> {
  return distApi<ReportDeliveryList>(
    `/report-deliveries?limit=${encodeURIComponent(String(limit))}`);
}

/** Manual retry for a failed/stuck report transfer. The backend returns an
 *  already-delivered report unchanged rather than re-sending it. */
export function retryReportDelivery(resultRecordId: string): Promise<SyncRetry> {
  return distApi<SyncRetry>(
    `/report-deliveries/${encodeURIComponent(resultRecordId)}/retry`,
    { method: "POST" });
}


/* ------------------------------------------- release management (§13, §25) */

/** A release is an update request with a lifecycle: draft → published →
 *  withdrawn. Only a PUBLISHED release is ever offered to a client. */
export interface ReleaseRow {
  update_id: string;
  target_version: string;
  target_component: string;
  channel: string;
  status: "draft" | "published" | "withdrawn" | string;
  platform: string | null;
  arch: string | null;
  minimum_supported_version: string | null;
  release_notes: string | null;
  priority: string;
  required: boolean;
  auto_install: boolean;
  scope: { kind?: string; [k: string]: unknown };
  created_by: string;
  created_at: number;
  published_at: number | null;
  withdrawn_at: number | null;
  package_sha256: string | null;
  signing_key_id: string | null;
  has_package: boolean;
  package_size?: number | null;
}

export interface ReleaseValidation {
  ok: boolean;
  problems: string[];
  update_id: string;
  target_version?: string | null;
  channel?: string | null;
  status?: string | null;
  package: { files: number; size: number; target_version: string | null };
  sha256?: string | null;
}

/** One installation's update standing, derived on the backend. OFFLINE is a
 *  distinct state — a silent installation is NEVER reported as up to date. */
export interface InstallationRow {
  worker_id: string;
  device_id: string;
  device_label: string | null;
  account_name: string | null;
  app_version: string | null;
  presence: string;
  last_heartbeat: number | null;
}

export interface InstallStatus {
  latest_version: string | null;
  counts: Record<string, number>;
  total: number;
  installations: Record<string, InstallationRow[]>;
}

export interface ReleaseOverview {
  latest: {
    update_id: string; version: string; channel: string;
    published_at: number | null; release_notes: string | null;
    required: boolean; package_size: number | null;
  } | null;
  stable_version: string | null;
  beta_version: string | null;
  channels: string[];
  counts: {
    registered_installations: number; reporting: number; up_to_date: number;
    outdated: number; installing: number; offline: number; unknown: number;
    failed: number; pending: number; draft_releases: number;
    withdrawn_releases: number; total_releases: number;
  };
  install_status: InstallStatus;
  generated_at: number;
}

export function listReleases(status?: string): Promise<{ releases: ReleaseRow[] }> {
  const q = status ? `?status=${encodeURIComponent(status)}` : "";
  return distApi<{ releases: ReleaseRow[] }>(`/releases${q}`);
}

export function releasesOverview(): Promise<ReleaseOverview> {
  return distApi<ReleaseOverview>("/releases/overview");
}

export function createRelease(payload: Record<string, unknown>): Promise<ReleaseRow> {
  return distApi<ReleaseRow>("/releases", {
    method: "POST", body: JSON.stringify(payload),
  });
}

export function validateRelease(updateId: string): Promise<ReleaseValidation> {
  return distApi<ReleaseValidation>(
    `/releases/${encodeURIComponent(updateId)}/validate`, { method: "POST" });
}

/** Publishing needs an explicit confirmation — the draft never goes live by
 *  accident. */
export function publishRelease(updateId: string): Promise<ReleaseRow> {
  return distApi<ReleaseRow>(
    `/releases/${encodeURIComponent(updateId)}/publish`,
    { method: "POST", body: JSON.stringify({ confirm: true }) });
}

export function withdrawRelease(updateId: string,
                                reason?: string): Promise<ReleaseRow> {
  return distApi<ReleaseRow>(
    `/releases/${encodeURIComponent(updateId)}/withdraw`,
    { method: "POST", body: JSON.stringify({ confirm: true, reason }) });
}

export function editRelease(updateId: string,
                            payload: Record<string, unknown>): Promise<ReleaseRow> {
  return distApi<ReleaseRow>(`/releases/${encodeURIComponent(updateId)}`, {
    method: "PATCH", body: JSON.stringify(payload),
  });
}

export function rollbackRelease(updateId: string): Promise<{
  ok: boolean; recommended: string; withdrew: string[]; release: ReleaseRow;
}> {
  return distApi(`/releases/${encodeURIComponent(updateId)}/rollback`,
                 { method: "POST" });
}

/** Upload the signed package body for a release (base64 of the canonical
 *  bytes). The stored digest is what a device verifies on download. */
export function uploadReleasePackage(updateId: string,
                                     base64: string): Promise<{ sha256: string; size: number }> {
  return distApi(`/updates/${encodeURIComponent(updateId)}/package`, {
    method: "POST", body: JSON.stringify({ data: base64 }),
  });
}


/* --------------------------------------------------------------- endpoint */

export async function login(username: string, password: string,
                            deviceId?: string): Promise<DistributedSession> {
  const session = await distApi<DistributedSession>("/login", {
    method: "POST",
    body: JSON.stringify({ username, password, device_id: deviceId }),
  });
  window.localStorage.setItem(TOKEN_KEY, session.token ?? "");
  window.localStorage.setItem(SESSION_KEY, JSON.stringify(session));
  return session;
}

export async function fetchMe(): Promise<DistributedSession> {
  const me = await distApi<DistributedSession>("/me");
  // Refresh the cached profile (role/scope may have been edited by the Teacher).
  window.localStorage.setItem(SESSION_KEY, JSON.stringify(me));
  return me;
}

export function logout(): Promise<void> {
  const p = distApi("/logout", { method: "POST", body: "{}" })
    .catch(() => undefined);
  clearSession();
  return p.then(() => undefined);
}

/**
 * Self-service password change (§password): proves the CURRENT password,
 * revokes every other session for the account and returns a fresh token so
 * THIS caller stays signed in.
 */
export async function changePassword(currentPassword: string,
                                      newPassword: string): Promise<void> {
  const out = await distApi<{ ok: boolean; token: string }>("/change-password", {
    method: "POST",
    body: JSON.stringify({
      current_password: currentPassword, new_password: newPassword,
    }),
  });
  if (out?.token) window.localStorage.setItem(TOKEN_KEY, out.token);
}

export const listSchools = () => distApi<SchoolsFeed>("/schools");

export const createSchool = (name: string, code?: string) =>
  distApi<School>("/schools", { method: "POST", body: JSON.stringify({ name, code }) });

export const renameSchool = (schoolId: string, name: string) =>
  distApi(`/schools/${schoolId}/rename`, { method: "POST", body: JSON.stringify({ name }) });

export const setSchoolActive = (schoolId: string, active: boolean) =>
  distApi(`/schools/${schoolId}/active`, { method: "POST", body: JSON.stringify({ active }) });

export const createClass = (schoolId: string, name: string, academicYear?: string) =>
  distApi<ClassInfo>(`/schools/${schoolId}/classes`, {
    method: "POST", body: JSON.stringify({ name, academic_year: academicYear }),
  });

export const assignAccount = (schoolId: string, accountId: string, classId?: string | null) =>
  distApi(`/schools/${schoolId}/assign`, {
    method: "POST", body: JSON.stringify({ account_id: accountId, class_id: classId ?? null }),
  });

export const listAccounts = () => distApi<AccountList>("/accounts");

export const createAccount = (username: string, displayName: string,
                              password: string, role: Role) =>
  distApi<{ account_id: string; role: Role }>("/accounts", {
    method: "POST",
    body: JSON.stringify({ username, display_name: displayName, password, role }),
  });

export const setAccountScope = (accountId: string,
                                scope: { school_id: string; class_id: string | null }[]) =>
  distApi(`/accounts/${accountId}/scope`, {
    method: "POST", body: JSON.stringify({ scope }),
  });

export const setAccountActive = (accountId: string, active: boolean) =>
  distApi(`/accounts/${accountId}/disable`, {
    method: "POST", body: JSON.stringify({ active }),
  });

/* ---- account management (§2/§3) ---- */

export const changeAccountUsername = (accountId: string, username: string) =>
  distApi<{ account_id: string; username: string }>(
    `/accounts/${accountId}/username`, {
      method: "POST", body: JSON.stringify({ username }),
    });

export const changeAccountDisplayName = (accountId: string, displayName: string) =>
  distApi(`/accounts/${accountId}/display-name`, {
    method: "POST", body: JSON.stringify({ display_name: displayName }),
  });

export const resetAccountPassword = (accountId: string, newPassword: string) =>
  distApi<{ ok: boolean; sessions_revoked: number; notified: boolean }>(
    `/accounts/${accountId}/password-reset`, {
      method: "POST", body: JSON.stringify({ new_password: newPassword }),
    });

export const changeAccountRoles = (accountId: string, roles: Role[]) =>
  distApi<{ ok: boolean; roles: Role[] }>(`/accounts/${accountId}/roles`, {
    method: "POST", body: JSON.stringify({ roles }),
  });

/* ---- assistant teams (§1) ---- */

export interface TeamRow {
  id: string;
  name: string;
  school_id: string;
  active: number | boolean;
  member_count: number;
  members?: TeamMember[];
}

export interface TeamMember {
  account_id: string;
  username: string;
  display_name: string;
  role: Role;
  roles: Role[];
  active: number | boolean;
}

export const listTeams = () =>
  distApi<{ teams: TeamRow[]; mine: string[] }>("/teams");

export const myTeams = () => distApi<{ teams: TeamRow[] }>("/me/teams");

export const createTeam = (name: string, schoolId: string) =>
  distApi<TeamRow>("/teams", {
    method: "POST", body: JSON.stringify({ name, school_id: schoolId }),
  });

export const getTeam = (teamId: string) =>
  distApi<TeamRow & { members: TeamMember[] }>(`/teams/${teamId}`);

export const addTeamMember = (teamId: string, accountId: string,
                              roleInTeam?: string) =>
  distApi(`/teams/${teamId}/members`, {
    method: "POST",
    body: JSON.stringify({ account_id: accountId, role_in_team: roleInTeam }),
  });

export const removeTeamMember = (teamId: string, accountId: string) =>
  distApi(`/teams/${teamId}/members/remove`, {
    method: "POST", body: JSON.stringify({ account_id: accountId }),
  });

export const teamResults = (teamId: string, limit = 100) =>
  distApi<{ results: CorrectionResultRow[]; member_count: number }>(
    `/teams/${teamId}/results?limit=${limit}`);

export const listWorkers = () => distApi<{ workers: WorkerRow[] }>("/workers");

export const listErrors = (limit = 100, workerId?: string) =>
  distApi<{ errors: ErrorReport[] }>(
    `/errors?limit=${limit}${workerId ? `&worker_id=${encodeURIComponent(workerId)}` : ""}`);

export const listUpdates = () => distApi<{ updates: UpdateRequestRow[] }>("/updates");

export const createUpdate = (payload: {
  target_version: string;
  target_component?: string;
  minimum_supported_version?: string;
  release_notes?: string;
  priority?: string;
  required?: boolean;
  auto_install?: boolean;
  package_sha256?: string;
  scope?: Record<string, unknown>;
}) => distApi<UpdateRequestRow>("/updates", {
  method: "POST", body: JSON.stringify(payload),
});

export const updatesForDevice = (deviceId: string) =>
  distApi<{ updates: UpdateRequestRow[] }>(
    `/updates/for-device?device_id=${encodeURIComponent(deviceId)}`);

export const updateHistory = (deviceId?: string) =>
  distApi<{ history: UpdateHistoryRow[] }>(
    `/updates/history${deviceId ? `?device_id=${encodeURIComponent(deviceId)}` : ""}`);

export interface AuditFilters {
  action?: string;
  actor_account?: string;
  affected_account?: string;
  school_id?: string;
  team_id?: string;
  outcome?: string;
  q?: string;
  since?: number;
}

/** Account-activity / audit log (§4) with search + filtering. */
export const listAudit = (limit = 200, action?: string, filters: AuditFilters = {}) => {
  const params = new URLSearchParams({ limit: String(limit) });
  if (action) params.set("action", action);
  for (const [k, v] of Object.entries(filters)) {
    if (v !== undefined && v !== null && String(v) !== "") params.set(k, String(v));
  }
  return distApi<{ audit: AuditRow[] }>(`/audit?${params.toString()}`);
};

export const listResults = (limit = 100, schoolId?: string, classId?: string) => {
  const params = new URLSearchParams({ limit: String(limit) });
  if (schoolId) params.set("school_id", schoolId);
  if (classId) params.set("class_id", classId);
  return distApi<{ results: CorrectionResultRow[] }>(`/results?${params.toString()}`);
};

export const reportError = (payload: {
  error_type: string;
  message: string;
  failed_stage?: string;
  context?: Record<string, unknown>;
  session_events?: unknown[];
  stack_trace?: string;
}) => distApi<CorrectionOutcome>("/errors", {
  method: "POST", body: JSON.stringify(payload),
});
