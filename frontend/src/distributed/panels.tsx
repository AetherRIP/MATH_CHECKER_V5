// Distributed dashboard panels: accounts, workers, updates, errors, audit,
// recent correction results. Every status shown here comes from a real
// backend event — the UI never invents presence or progress (§62).
import { useEffect, useState } from "react";
import {
  Badge, Button, DataTable, Empty, ErrorState, Loading, Modal, Section,
  useToast,
} from "../components";
import { timeAgo } from "../api";
import {
  addTeamMember, changeAccountDisplayName, changeAccountRoles,
  changeAccountUsername, createAccount, createTeam, getTeam, listAccounts,
  listAudit, listErrors, listResults, listTeams, listUpdates, listWorkers,
  removeTeamMember, resetAccountPassword, teamResults, updateHistory,
  updatesForDevice, setAccountActive, setAccountScope, storedSession,
  type AccountList, type AccountRow, type AuditRow, type CorrectionResultRow,
  type ErrorReport, type Role, type SchoolsFeed, type TeamMember, type TeamRow,
  type UpdateHistoryRow, type UpdateRequestRow, type WorkerRow,
} from "./api";

type Tone = "ok" | "err" | "warn" | "info" | "mute";

/** Presence → badge tone. Only real backend states map to a colour. */
export function presenceTone(p: string): Tone {
  switch (p) {
    case "ONLINE": case "AVAILABLE": case "UPDATED": return "ok";
    case "BUSY": case "ONLINE_CHECKING": case "UPDATING": case "DOWNLOADING":
    case "VERIFYING": case "INSTALLING": case "HEALTH_CHECK": return "info";
    case "WAITING_FOR_IDLE": case "PAUSED": case "UPDATE_AVAILABLE":
    case "OFFLINE": return "warn";
    case "ERROR": case "FAILED": case "RECOVERY": case "RECOVERY_REQUIRED": return "err";
    default: return "mute";
  }
}

/* ================================================================ workers */

export function WorkersPanel({ canView }: { canView: boolean }) {
  const [workers, setWorkers] = useState<WorkerRow[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!canView) return;
    let alive = true;
    const load = () =>
      listWorkers()
        .then((r) => { if (alive) { setWorkers(r.workers); setError(null); } })
        .catch((e) => { if (alive) setError((e as Error).message); });
    load();
    const t = setInterval(load, 30_000); // presence via heartbeat, not aggressive polling
    return () => { alive = false; clearInterval(t); };
  }, [canView]);

  if (!canView) return null;
  if (error) return <ErrorState title="Workers unavailable" detail={error} />;
  if (!workers) return <Loading label="Loading workers…" />;

  return (
    <Section title="Workers" icon="cpu"
      hint="Presence comes from real heartbeats — a worker that stopped sending one shows OFFLINE.">
      {workers.length === 0 ? (
        <Empty icon="cpu" title="No workers registered"
          hint="Workers appear here once a device runs the tiny agent and registers." />
      ) : (
        <DataTable head={<tr><th>Worker</th><th>Presence</th><th>Account</th>
          <th>Device</th><th>Versions</th><th>Current job</th><th>Last heartbeat</th></tr>}>
          {workers.map((w) => (
            <tr key={w.worker_id}>
              <td style={{ fontFamily: "var(--mono, monospace)", fontSize: 12 }}>
                {w.worker_id}
              </td>
              <td><Badge tone={presenceTone(w.presence)}>{String(w.presence)}</Badge></td>
              <td>{w.account_name ?? <span className="faint">—</span>}</td>
              <td>{w.device_label ?? w.device_id?.slice(0, 8) ?? "—"}</td>
              <td className="small faint">
                app {w.app_version ?? "?"} · agent {w.agent_version ?? "?"}
              </td>
              <td>{w.current_job_id
                ? <span style={{ fontFamily: "var(--mono, monospace)", fontSize: 12 }}>
                    {w.current_job_id.slice(0, 12)}…</span>
                : <span className="faint">idle</span>}</td>
              <td className="small">{w.last_heartbeat ? timeAgo(w.last_heartbeat) : "never"}</td>
            </tr>
          ))}
        </DataTable>
      )}
    </Section>
  );
}

/* ================================================================ accounts */

export function AccountsPanel({ canManage, schools }: {
  canManage: boolean; schools: SchoolsFeed | null;
}) {
  const toast = useToast();
  const [accounts, setAccounts] = useState<AccountList | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [creating, setCreating] = useState(false);
  const [scoping, setScoping] = useState<AccountRow | null>(null);
  const [editing, setEditing] = useState<AccountRow | null>(null);

  const load = () =>
    listAccounts().then(setAccounts).catch((e) => setError((e as Error).message));

  useEffect(() => { if (canManage) load(); }, [canManage]);
  if (!canManage) return null;

  async function toggleActive(a: AccountRow) {
    try {
      await setAccountActive(a.id, !a.active);
      toast("ok", `${a.username} ${a.active ? "disabled" : "enabled"}`);
      load();
    } catch (e) { toast("err", (e as Error).message); }
  }

  return (
    <Section title="Accounts" icon="user"
      hint="Developer manages the Teacher account; Teacher creates Co-Teachers and Assistants.">
      {error ? <ErrorState title="Accounts unavailable" detail={error} />
      : !accounts ? <Loading label="Loading accounts…" /> : (
        <DataTable head={<tr><th>Username</th><th>Display name</th><th>Role</th>
          <th>Status</th><th>Last login</th><th></th></tr>}>
          {accounts.accounts.map((a) => (
            <tr key={a.id}>
              <td>{a.username}</td>
              <td>{a.display_name}</td>
              <td><Badge tone={a.role === "DEVELOPER" ? "err"
                  : a.role === "TEACHER" ? "info"
                  : a.role === "CO_TEACHER" ? "warn" : "mute"}>{a.role.replace("_", "-")}</Badge></td>
              <td><Badge tone={a.active ? "ok" : "mute"}>
                {a.active ? "active" : "disabled"}</Badge></td>
              <td className="small">{a.last_login ? timeAgo(a.last_login) : "never"}</td>
              <td style={{ textAlign: "right", whiteSpace: "nowrap" }}>
                <Button size="sm" variant="ghost" icon="edit"
                  onClick={() => setEditing(a)}>Edit</Button>
                {(a.role === "CO_TEACHER" || a.role === "ASSISTANT") && (
                  <Button size="sm" variant="ghost" icon="tag"
                    onClick={() => setScoping(a)}>Scope</Button>
                )}
                {a.role !== "DEVELOPER" && (
                  <Button size="sm" variant={a.active ? "danger" : "success"}
                    onClick={() => toggleActive(a)}>
                    {a.active ? "Disable" : "Enable"}
                  </Button>
                )}
              </td>
            </tr>
          ))}
        </DataTable>
      )}
      <div style={{ marginTop: 10 }}>
        <Button icon="plus" onClick={() => setCreating(true)}>New account</Button>
      </div>

      {creating && (
        <CreateAccountModal onClose={() => setCreating(false)} onCreated={() => {
          setCreating(false); load();
        }} />
      )}
      {scoping && (
        <ScopeModal account={scoping} schools={schools} onClose={() => setScoping(null)}
          onSaved={() => { setScoping(null); toast("ok", "Scope updated"); }} />
      )}
      {editing && (
        <AccountEditModal account={editing} onClose={() => setEditing(null)}
          onSaved={() => { setEditing(null); load(); }} />
      )}
    </Section>
  );
}

/* ==================================================== account management (§2/§3) */

/** Edit an existing account: username, display name, roles, password reset.
 *
 * Password reset asks only for the NEW password — the existing one is never
 * retrieved or displayed, matching the backend, which never exposes it. */
function AccountEditModal({ account, onClose, onSaved }: {
  account: AccountRow; onClose: () => void; onSaved: () => void;
}) {
  const toast = useToast();
  const [username, setUsername] = useState(account.username);
  const [displayName, setDisplayName] = useState(account.display_name);
  const [roles, setRoles] = useState<Role[]>(
    account.roles && account.roles.length ? account.roles : [account.role]);
  const [newPassword, setNewPassword] = useState("");
  const [busy, setBusy] = useState(false);

  const allRoles: Role[] = ["CO_TEACHER", "ASSISTANT", "TEACHER", "DEVELOPER"];

  async function run(label: string, fn: () => Promise<unknown>) {
    setBusy(true);
    try { await fn(); toast("ok", label); onSaved(); }
    catch (e) { toast("err", (e as Error).message); }
    finally { setBusy(false); }
  }

  return (
    <Modal title={`Edit — ${account.username}`} icon="edit" onClose={onClose}
      footer={<Button variant="ghost" onClick={onClose}>Done</Button>}>
      <div style={{ display: "grid", gap: 10 }}>
        <label className="small">Username
          <div className="row" style={{ gap: 8 }}>
            <input value={username} onChange={(e) => setUsername(e.target.value)} />
            <Button size="sm" loading={busy} disabled={!username.trim()
              || username === account.username}
              onClick={() => run("Username changed",
                () => changeAccountUsername(account.id, username.trim()))}>Save</Button>
          </div></label>
        <label className="small">Display name
          <div className="row" style={{ gap: 8 }}>
            <input value={displayName} onChange={(e) => setDisplayName(e.target.value)} />
            <Button size="sm" loading={busy} disabled={!displayName.trim()
              || displayName === account.display_name}
              onClick={() => run("Display name changed",
                () => changeAccountDisplayName(account.id, displayName.trim()))}>Save</Button>
          </div></label>
        <div className="small">Roles
          <div className="row wrap" style={{ gap: 12, marginTop: 4 }}>
            {allRoles.map((r) => (
              <label key={r} className="row small" style={{ gap: 5 }}>
                <input type="checkbox" checked={roles.includes(r)}
                  onChange={(e) => setRoles(e.target.checked
                    ? [...roles, r] : roles.filter((x) => x !== r))} />
                {r.replace("_", "-")}
              </label>
            ))}
            <Button size="sm" loading={busy} disabled={roles.length === 0}
              onClick={() => run("Roles updated",
                () => changeAccountRoles(account.id, roles))}>Save roles</Button>
          </div>
        </div>
        <label className="small">Reset password (new value only)
          <div className="row" style={{ gap: 8 }}>
            <input type="password" value={newPassword} autoComplete="new-password"
              onChange={(e) => setNewPassword(e.target.value)} />
            <Button size="sm" variant="danger" loading={busy}
              disabled={newPassword.length < 8}
              onClick={() => run("Password reset — the user's other sessions were signed out",
                () => resetAccountPassword(account.id, newPassword))}>Reset</Button>
          </div></label>
      </div>
      <p className="small faint" style={{ marginTop: 8 }}>
        The existing password is never retrieved or shown. A reset signs the
        user's other sessions out and is recorded in the audit trail.
      </p>
    </Modal>
  );
}

/* ==================================================== assistant teams (§1) */

export function TeamsPanel({ canManage, schools }: {
  canManage: boolean; schools: SchoolsFeed | null;
}) {
  const toast = useToast();
  const [teams, setTeams] = useState<TeamRow[] | null>(null);
  const [accounts, setAccounts] = useState<AccountRow[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [schoolId, setSchoolId] = useState("");
  const [name, setName] = useState("");
  const [open, setOpen] = useState<TeamRow | null>(null);
  const [members, setMembers] = useState<TeamMember[]>([]);

  const load = () => {
    listTeams().then((r) => setTeams(r.teams))
      .catch((e) => setError((e as Error).message));
    if (canManage) listAccounts().then((r) => setAccounts(r.accounts)).catch(() => {});
  };
  useEffect(() => { load(); }, [canManage]);

  async function openTeam(t: TeamRow) {
    setOpen(t);
    try { setMembers((await getTeam(t.id)).members ?? []); }
    catch (e) { toast("err", (e as Error).message); }
  }

  if (error) return <ErrorState title="Teams unavailable" detail={error} />;
  if (!teams) return <Loading label="Loading teams…" />;

  return (
    <Section title="Assistant teams" icon="user"
      hint="Correction records are shared only inside a team (and its school). Members always keep their own identity on every record.">
      <DataTable head={<tr><th>Team</th><th>School</th><th>Members</th><th></th></tr>}>
        {teams.map((t) => (
          <tr key={t.id}>
            <td>{t.name}</td>
            <td className="small">{schools?.schools.find((s) => s.id === t.school_id)?.name ?? t.school_id.slice(0, 8)}</td>
            <td className="small">{t.member_count}</td>
            <td style={{ textAlign: "right" }}>
              <Button size="sm" variant="ghost" icon="user"
                onClick={() => openTeam(t)}>Members</Button>
            </td>
          </tr>
        ))}
        {teams.length === 0 && (
          <tr><td colSpan={4} className="faint small">No teams yet.</td></tr>
        )}
      </DataTable>

      {canManage && (
        <div className="row wrap" style={{ gap: 8, marginTop: 10 }}>
          <input placeholder="New team name" value={name}
            onChange={(e) => setName(e.target.value)} />
          <select value={schoolId} onChange={(e) => setSchoolId(e.target.value)}>
            <option value="">Choose school…</option>
            {(schools?.schools ?? []).map((s) => (
              <option key={s.id} value={s.id}>{s.name}</option>
            ))}
          </select>
          <Button icon="plus" disabled={!name.trim() || !schoolId} onClick={async () => {
            try {
              await createTeam(name.trim(), schoolId);
              toast("ok", "Team created"); setName(""); setSchoolId(""); load();
            } catch (e) { toast("err", (e as Error).message); }
          }}>Create team</Button>
        </div>
      )}

      {open && (
        <Modal title={`Team — ${open.name}`} icon="user" onClose={() => setOpen(null)}
          footer={<Button variant="ghost" onClick={() => setOpen(null)}>Close</Button>}>
          <ul className="small" style={{ paddingLeft: 18 }}>
            {members.map((m) => (
              <li key={m.account_id} style={{ marginBottom: 4 }}>
                {m.display_name} <span className="faint">({m.role.replace("_", "-")})</span>
                {canManage && (
                  <Button size="sm" variant="ghost" icon="trash"
                    onClick={async () => {
                      try {
                        await removeTeamMember(open.id, m.account_id);
                        toast("ok", `${m.username} removed`);
                        setMembers((await getTeam(open.id)).members ?? []); load();
                      } catch (e) { toast("err", (e as Error).message); }
                    }}>Remove</Button>
                )}
              </li>
            ))}
            {members.length === 0 && <li className="faint">No members yet.</li>}
          </ul>
          {canManage && (
            <div className="row wrap" style={{ gap: 8, marginTop: 8 }}>
              <select id={`add-member-${open.id}`} defaultValue="">
                <option value="">Add member…</option>
                {accounts
                  .filter((a) => (a.role === "CO_TEACHER" || a.role === "ASSISTANT")
                    && !members.some((m) => m.account_id === a.id))
                  .map((a) => (
                    <option key={a.id} value={a.id}>{a.display_name} ({a.username})</option>
                  ))}
              </select>
              <Button size="sm" icon="plus" onClick={async () => {
                const sel = document.getElementById(`add-member-${open.id}`) as HTMLSelectElement | null;
                const accountId = sel?.value;
                if (!accountId) return;
                try {
                  await addTeamMember(open.id, accountId);
                  toast("ok", "Member added");
                  setMembers((await getTeam(open.id)).members ?? []); load();
                } catch (e) { toast("err", (e as Error).message); }
              }}>Add</Button>
            </div>
          )}
          <TeamSharedResults teamId={open.id} />
        </Modal>
      )}
    </Section>
  );
}

/** Records shared by the team — each still names the assistant who made it. */
function TeamSharedResults({ teamId }: { teamId: string }) {
  const [rows, setRows] = useState<CorrectionResultRow[] | null>(null);
  useEffect(() => {
    teamResults(teamId, 25).then((r) => setRows(r.results)).catch(() => setRows([]));
  }, [teamId]);
  if (!rows) return <Loading label="Loading shared records…" />;
  return (
    <div style={{ marginTop: 12 }}>
      <div className="small faint" style={{ marginBottom: 4 }}>Shared correction records</div>
      <DataTable head={<tr><th>Submission</th><th>Grade</th><th>Corrected by</th><th>When</th></tr>}>
        {rows.map((r) => (
          <tr key={r.id}>
            <td className="small">{String(r.payload?.submission_id ?? r.submission_id)}</td>
            <td className="small">{String(r.payload?.final_grade ?? "—")}</td>
            <td className="small faint">{(r.account_id ?? "—").slice(0, 10)}</td>
            <td className="small">{timeAgo(r.received_at)}</td>
          </tr>
        ))}
        {rows.length === 0 && (
          <tr><td colSpan={4} className="faint small">No shared records yet.</td></tr>
        )}
      </DataTable>
    </div>
  );
}

function CreateAccountModal({ onClose, onCreated }: {
  onClose: () => void; onCreated: () => void;
}) {
  const toast = useToast();
  const [username, setUsername] = useState("");
  const [displayName, setDisplayName] = useState("");
  const [password, setPassword] = useState("");
  const [role, setRole] = useState<Role>("ASSISTANT");
  const [busy, setBusy] = useState(false);

  async function submit() {
    setBusy(true);
    try {
      await createAccount(username.trim(), displayName.trim(), password, role);
      toast("ok", `Account ${username.trim()} created`);
      onCreated();
    } catch (e) {
      toast("err", (e as Error).message);
    } finally { setBusy(false); }
  }

  return (
    <Modal title="Create account" icon="plus" onClose={onClose}
      footer={<>
        <Button variant="ghost" onClick={onClose}>Cancel</Button>
        <Button variant="primary" loading={busy} disabled={
          !username.trim() || !displayName.trim() || password.length < 8
        } onClick={submit}>Create</Button>
      </>}>
      <div className="form-grid" style={{ display: "grid", gap: 10 }}>
        <label className="small">Username
          <input value={username} onChange={(e) => setUsername(e.target.value)}
            autoComplete="off" /></label>
        <label className="small">Display name
          <input value={displayName} onChange={(e) => setDisplayName(e.target.value)} /></label>
        <label className="small">Password (min 8 chars)
          <input type="password" value={password} autoComplete="new-password"
            onChange={(e) => setPassword(e.target.value)} /></label>
        <label className="small">Role
          <select value={role} onChange={(e) => setRole(e.target.value as Role)}>
            <option value="ASSISTANT">Assistant</option>
            <option value="CO_TEACHER">Co-Teacher</option>
          </select></label>
      </div>
      <p className="small faint" style={{ marginTop: 8 }}>
        Passwords are hashed server-side and never displayed again. Only the
        Developer can create Teacher or Developer accounts.
      </p>
    </Modal>
  );
}

function ScopeModal({ account, schools, onClose, onSaved }: {
  account: AccountRow; schools: SchoolsFeed | null;
  onClose: () => void; onSaved: () => void;
}) {
  const toast = useToast();
  const [scope, setScope] = useState<{ school_id: string; class_id: string | null }[]>([]);
  const [schoolId, setSchoolId] = useState("");
  const [classId, setClassId] = useState("");
  const [busy, setBusy] = useState(false);

  const classes = (schools?.classes ?? []).filter((c) => c.school_id === schoolId);

  async function submit() {
    setBusy(true);
    try {
      await setAccountScope(account.id, scope);
      onSaved();
    } catch (e) {
      toast("err", (e as Error).message);
    } finally { setBusy(false); }
  }

  return (
    <Modal title={`Scope — ${account.username}`} icon="tag" onClose={onClose}
      footer={<>
        <Button variant="ghost" onClick={onClose}>Cancel</Button>
        <Button variant="primary" loading={busy} onClick={submit}>Save scope</Button>
      </>}>
      <p className="small faint" style={{ marginTop: 0 }}>
        Empty scope = unrestricted (Developer/Teacher). Co-Teachers and
        Assistants only see assigned schools/classes.
      </p>
      {scope.length > 0 && (
        <ul className="small" style={{ paddingLeft: 18, marginBottom: 10 }}>
          {scope.map((s, i) => {
            const school = schools?.schools.find((x) => x.id === s.school_id);
            const cls = schools?.classes.find((x) => x.id === s.class_id);
            return (
              <li key={i}>
                {school?.name ?? s.school_id}
                {cls ? ` / ${cls.name}` : " / all classes"}
                <Button size="sm" variant="ghost" icon="trash"
                  onClick={() => setScope(scope.filter((_, j) => j !== i))} />
              </li>
            );
          })}
        </ul>
      )}
      <div style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap" }}>
        <select value={schoolId} onChange={(e) => { setSchoolId(e.target.value); setClassId(""); }}>
          <option value="">Choose school…</option>
          {(schools?.schools ?? []).map((s) => (
            <option key={s.id} value={s.id}>{s.name}</option>
          ))}
        </select>
        <select value={classId} onChange={(e) => setClassId(e.target.value)}
          disabled={!schoolId}>
          <option value="">All classes</option>
          {classes.map((c) => (
            <option key={c.id} value={c.id}>{c.name}</option>
          ))}
        </select>
        <Button size="sm" icon="plus" disabled={!schoolId}
          onClick={() => {
            setScope([...scope, { school_id: schoolId, class_id: classId || null }]);
            setSchoolId(""); setClassId("");
          }}>Add</Button>
      </div>
    </Modal>
  );
}

/* ================================================================ updates */

export function UpdatesPanel({ isDeveloper }: { isDeveloper: boolean }) {
  const [updates, setUpdates] = useState<UpdateRequestRow[] | null>(null);
  const [history, setHistory] = useState<UpdateHistoryRow[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let alive = true;
    Promise.all([
      storedSession()?.role === "DEVELOPER" || canDiagnostics()
        ? listUpdates() : Promise.resolve({ updates: [] }),
      updateHistory(),
    ]).then(([u, h]) => {
      if (!alive) return;
      setUpdates(u.updates); setHistory(h.history);
    }).catch((e) => { if (alive) setError((e as Error).message); });
    return () => { alive = false; };
  }, []);

  if (error) return <ErrorState title="Updates unavailable" detail={error} />;
  if (!updates || !history) return <Loading label="Loading updates…" />;

  return (
    <Section title="Updates" icon="download"
      hint="Every install is backed up first and rolled back automatically on a failed health check.">
      <DataTable head={<tr><th>Target</th><th>Component</th><th>Priority</th>
        <th>Mode</th><th>Created</th></tr>}>
        {updates.map((u) => (
          <tr key={u.update_id}>
            <td style={{ fontFamily: "var(--mono, monospace)", fontSize: 12 }}>
              v{u.target_version} · {u.scope?.kind ?? "ALL"}</td>
            <td>{u.target_component}</td>
            <td>{u.priority}</td>
            <td>
              {u.required ? <Badge tone="err">required</Badge>
                : u.auto_install ? <Badge tone="info">auto</Badge>
                : <Badge tone="mute">optional</Badge>}
            </td>
            <td className="small">{timeAgo(u.created_at)}</td>
          </tr>
        ))}
        {updates.length === 0 && (
          <tr><td colSpan={5} className="faint small">No update requests yet.</td></tr>
        )}
      </DataTable>

      <h4 style={{ margin: "16px 0 6px" }}>Update history</h4>
      <DataTable head={<tr><th>Device</th><th>From → To</th><th>Result</th>
        <th>Rollback</th><th>When</th></tr>}>
        {history.map((h) => (
          <tr key={h.id}>
            <td style={{ fontFamily: "var(--mono, monospace)", fontSize: 12 }}>
              {h.device_id.slice(0, 12)}…</td>
            <td>{h.old_version || "?"} → {h.new_version || "?"}</td>
            <td><Badge tone={h.result === "SUCCESS" ? "ok"
              : h.result === "ROLLED_BACK" ? "warn" : "err"}>{h.result}</Badge></td>
            <td>{h.rollback_result ?? "—"}</td>
            <td className="small">{h.completed_at ? timeAgo(h.completed_at) : "running…"}</td>
          </tr>
        ))}
        {history.length === 0 && (
          <tr><td colSpan={5} className="faint small">No updates performed yet.</td></tr>
        )}
      </DataTable>

      {isDeveloper && (
        <div style={{ marginTop: 10 }}>
          {/* Creating a release now happens on the dedicated Application
              Updates page, which validates and explicitly publishes — this
              panel must not offer a second, unvalidated publish path. */}
          <Button icon="download"
            onClick={() => { window.location.hash = "#/updates"; }}>
            Manage releases
          </Button>
        </div>
      )}
    </Section>
  );
}

function canDiagnostics(): boolean {
  const perms = storedSession()?.permissions ?? [];
  return perms.includes("diagnostics.view") || perms.includes("*");
}

/* ================================================================= errors */

export function ErrorsPanel({ canViewAll }: { canViewAll: boolean }) {
  const [errors, setErrors] = useState<ErrorReport[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [detail, setDetail] = useState<ErrorReport | null>(null);

  useEffect(() => {
    let alive = true;
    // Limited roles are scoped SERVER-side to their own account's reports —
    // the UI must not invent a worker_id filter (an account id never matches
    // a worker id, which silently showed an empty list). Operators see all.
    listErrors(100)
      .then((r) => { if (alive) setErrors(r.errors); })
      .catch((e) => { if (alive) setError((e as Error).message); });
    return () => { alive = false; };
  }, [canViewAll]);

  if (error) return <ErrorState title="Errors unavailable" detail={error} />;
  if (!errors) return <Loading label="Loading error reports…" />;

  return (
    <Section title="Error reports" icon="alert"
      hint="Sanitized diagnostics from real failures. Secrets are redacted before storage.">
      {errors.length === 0 ? (
        <Empty icon="check-circle" title="No error reports"
          hint="Genuine failures reported by workers appear here." />
      ) : (
        <DataTable head={<tr><th>Type</th><th>Stage</th><th>Message</th>
          <th>Worker</th><th>When</th><th></th></tr>}>
          {errors.map((e) => (
            <tr key={e.id}>
              <td><Badge tone={e.resolved ? "mute" : "err"}>{e.error_type}</Badge></td>
              <td className="small">{e.failed_stage ?? "—"}</td>
              <td style={{ maxWidth: 340, overflow: "hidden",
                textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{e.message}</td>
              <td className="small" style={{ fontFamily: "var(--mono, monospace)" }}>
                {e.worker_id?.slice(0, 10) ?? "—"}</td>
              <td className="small">{timeAgo(e.ts)}</td>
              <td><Button size="sm" variant="ghost" icon="eye"
                onClick={() => setDetail(e)}>View</Button></td>
            </tr>
          ))}
        </DataTable>
      )}
      {detail && (
        <Modal title={`${detail.error_type} — ${detail.failed_stage ?? "unknown stage"}`}
          icon="alert" wide onClose={() => setDetail(null)}
          footer={<Button variant="secondary" onClick={() => setDetail(null)}>Close</Button>}>
          <p style={{ marginTop: 0 }}><strong>Message:</strong> {detail.message}</p>
          <p className="small faint">
            {detail.worker_id ? `Worker ${detail.worker_id} · ` : ""}
            {detail.device_id ? `Device ${detail.device_id} · ` : ""}
            {timeAgo(detail.ts)}
          </p>
          {detail.session_events?.length > 0 && (
            <>
              <h4>Session sequence</h4>
              <ol className="small" style={{ paddingLeft: 20 }}>
                {detail.session_events.map((s, i) => <li key={i}>{String(s)}</li>)}
              </ol>
            </>
          )}
          {detail.context && Object.keys(detail.context).length > 0 && (
            <>
              <h4>Context</h4>
              <pre className="small" style={{
                background: "var(--surface-2, rgba(128,128,128,.1))",
                padding: 10, borderRadius: 8, overflow: "auto",
              }}>{JSON.stringify(detail.context, null, 2)}</pre>
            </>
          )}
          {detail.stack_trace && (
            <>
              <h4>Stack (truncated)</h4>
              <pre className="small" style={{
                background: "var(--surface-2, rgba(128,128,128,.1))",
                padding: 10, borderRadius: 8, overflow: "auto",
              }}>{detail.stack_trace}</pre>
            </>
          )}
        </Modal>
      )}
    </Section>
  );
}

/* ================================================================== audit */

export function AuditPanel() {
  const [rows, setRows] = useState<AuditRow[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [q, setQ] = useState("");
  const [outcome, setOutcome] = useState("");
  const [action, setAction] = useState("");

  useEffect(() => {
    let alive = true;
    // Debounce-free: the query is small and only fires when the filters change.
    listAudit(200, action || undefined, {
      q: q || undefined, outcome: outcome || undefined,
    })
      .then((r) => { if (alive) { setRows(r.audit); setError(null); } })
      .catch((e) => { if (alive) setError((e as Error).message); });
    return () => { alive = false; };
  }, [q, outcome, action]);

  if (error) return null; // permission not granted — panel stays hidden
  if (!rows) return <Loading label="Loading audit trail…" />;

  return (
    <Section title="Account activity / audit log" icon="list"
      hint="Append-only, written server-side. Passwords, hashes and tokens are never recorded.">
      <div className="row wrap" style={{ gap: 8, marginBottom: 10 }}>
        <input placeholder="Search action or details…" value={q}
          onChange={(e) => setQ(e.target.value)} />
        <input placeholder="Action (exact)…" value={action}
          onChange={(e) => setAction(e.target.value)} />
        <select value={outcome} onChange={(e) => setOutcome(e.target.value)}>
          <option value="">Any outcome</option>
          <option value="success">Success</option>
          <option value="failure">Failure</option>
          <option value="denied">Denied</option>
        </select>
      </div>
      <DataTable head={<tr><th>Action</th><th>Outcome</th><th>Actor</th>
        <th>Affected</th><th>Details</th><th>Event</th><th>When</th></tr>}>
        {rows.map((r) => (
          <tr key={r.id}>
            <td><Badge tone="info">{r.action}</Badge></td>
            <td><Badge tone={r.outcome === "denied" ? "err"
              : r.outcome === "failure" ? "warn" : "ok"}>{r.outcome}</Badge></td>
            <td className="small">{r.actor_account?.slice(0, 10) ?? "system"}
              {r.actor_role ? <span className="faint"> {r.actor_role.replace("_", "-")}</span> : null}</td>
            <td className="small faint">{(r.affected_account ?? "—").slice(0, 10)}</td>
            <td className="small faint" style={{ maxWidth: 240, overflow: "hidden",
              textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
              {JSON.stringify(r.details)}</td>
            <td className="small faint">{r.event_id.slice(0, 8)}</td>
            <td className="small">{timeAgo(r.ts)}</td>
          </tr>
        ))}
        {rows.length === 0 && (
          <tr><td colSpan={7} className="faint small">No matching audit entries.</td></tr>
        )}
      </DataTable>
    </Section>
  );
}

/* ================================================================ results */

export function ResultsPanel({ schools }: { schools: SchoolsFeed | null }) {
  const [results, setResults] = useState<CorrectionResultRow[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let alive = true;
    listResults(100)
      .then((r) => { if (alive) setResults(r.results); })
      .catch((e) => { if (alive) setError((e as Error).message); });
    return () => { alive = false; };
  }, []);

  const nameFor = (kind: "school" | "class", id: string | null) => {
    if (!id) return null;
    if (kind === "school") return schools?.schools.find((s) => s.id === id)?.name;
    return schools?.classes.find((c) => c.id === id)?.name;
  };

  if (error) return <ErrorState title="Results unavailable" detail={error} />;
  if (!results) return <Loading label="Loading recent corrections…" />;

  return (
    <Section title="Recent corrections" icon="check-circle"
      hint="Lightweight result metadata synced from workers — reports stay in Drive/Classroom.">
      {results.length === 0 ? (
        <Empty icon="check-circle" title="No synced corrections yet"
          hint="Completed corrections from Assistant workers appear here." />
      ) : (
        <DataTable head={<tr><th>Student</th><th>Class</th><th>Marks</th>
          <th>Grade</th><th>Status</th><th>Worker</th><th>When</th></tr>}>
          {results.map((r) => {
            const p = r.payload as Record<string, string | number>;
            return (
              <tr key={r.id}>
                <td>{String(p.student_name ?? p.student_id ?? "—")}</td>
                <td className="small">
                  {nameFor("class", r.class_id)
                    ?? nameFor("school", r.school_id) ?? "—"}</td>
                <td>{p.raw_marks ?? "?"} / {p.max_marks ?? "?"}</td>
                <td>{p.final_grade ?? "—"}</td>
                <td><Badge tone={p.correction_status === "COMPLETE" ? "ok" : "warn"}>
                  {String(p.correction_status ?? "?")}</Badge></td>
                <td className="small" style={{ fontFamily: "var(--mono, monospace)" }}>
                  {String(p.worker_id ?? "—").slice(0, 10)}</td>
                <td className="small">{timeAgo(r.received_at)}</td>
              </tr>
            );
          })}
        </DataTable>
      )}
    </Section>
  );
}

/** Device-scoped update status for the Assistant/Co-Teacher view (§29). */
export function MyUpdateStatus({ deviceId }: { deviceId: string | null }) {
  const [rows, setRows] = useState<UpdateRequestRow[] | null>(null);

  useEffect(() => {
    if (!deviceId) return;
    let alive = true;
    updatesForDevice(deviceId)
      .then((r) => { if (alive) setRows(r.updates); })
      .catch(() => { if (alive) setRows([]); });
    return () => { alive = false; };
  }, [deviceId]);

  if (!deviceId || !rows?.length) return null;
  return (
    <Section title="Pending updates" icon="download">
      {rows.map((u) => (
        <div key={u.update_id} className="notice info" style={{ marginBottom: 8 }}>
          <strong>Version {u.target_version}</strong> ({u.priority}
          {u.required ? ", required" : ""}) is ready
          {u.release_notes ? ` — ${u.release_notes}` : ""}.
        </div>
      ))}
    </Section>
  );
}
