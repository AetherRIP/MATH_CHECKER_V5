// Multi-School dashboard (§13, §48): login, schools/classes tree, worker
// health, accounts, updates, errors, audit. One page, role-adapted — the
// backend enforces every permission, the UI only hides what cannot work.
import { useCallback, useEffect, useMemo, useState } from "react";
import {
  Badge, Button, Empty, Icon, Loading, Modal, PageHeader, Section,
  useToast,
} from "../components";
import {
  changePassword, clearSession, distributedCapabilities, distributedOrigin,
  fetchMe, listSchools, login, logout, pingBackend, setSharedBackend,
  sharedBackend, storedSession, storedToken,
  createSchool, renameSchool, setSchoolActive, createClass,
  type DistributedSession, type MultiAccountMode, type SchoolsFeed,
} from "./api";
import {
  AccountsPanel, AuditPanel, ErrorsPanel, MyUpdateStatus, ResultsPanel,
  TeamsPanel, UpdatesPanel, WorkersPanel,
} from "./panels";
import { SyncMonitorPanel } from "./SyncMonitor";

/** Stable per-browser device id for token↔device binding (§21). */
function deviceId(): string {
  const KEY = "mc5.distributed.device_id";
  let id = window.localStorage.getItem(KEY);
  if (!id) {
    id = window.crypto?.randomUUID?.() ??
      `dev-${Date.now()}-${Math.random().toString(16).slice(2, 10)}`;
    window.localStorage.setItem(KEY, id);
  }
  return id;
}

/* ==================================================== shared backend address */

/**
 * Which backend this dashboard reads (§ shared backend).
 *
 * Empty address = the backend on THIS computer (single-machine install). In a
 * multi-laptop deployment the other installations point here at the one
 * always-on backend so every dashboard shows the same accounts, teams, devices,
 * shared reports and audit events. The badge follows a REAL HTTP answer from
 * `GET /api/distributed/ping` — never the mere fact that the page loaded, and
 * never an optimistic guess (Stage 3: no green light without a real response).
 */
function SharedBackendPanel() {
  const [url, setUrl] = useState(sharedBackend());
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<{ ok: boolean; text: string } | null>(null);

  async function check() {
    setBusy(true);
    setResult(null);
    try {
      setSharedBackend(url);       // saved first, so the probe tests that address
      const info = await pingBackend();
      setResult({ ok: true,
                  text: `Reachable — ${info.service} v${info.version}` });
    } catch (err) {
      setResult({ ok: false, text: (err as Error).message });
    } finally {
      setBusy(false);
    }
  }

  const origin = distributedOrigin();
  const crossOrigin = origin !== window.location.origin;
  return (
    <div className="card" style={{ padding: 16, marginTop: 12 }}
      data-testid="shared-backend-panel">
      <div className="row"
        style={{ justifyContent: "space-between", alignItems: "center", gap: 8 }}>
        <strong className="small">Shared backend</strong>
        {result
          ? <Badge tone={result.ok ? "ok" : "err"}
              icon={result.ok ? "check-circle" : "alert"}>
              {result.ok ? "CONNECTED" : "UNREACHABLE"}</Badge>
          : <Badge tone="mute" icon="activity">not checked</Badge>}
      </div>
      <p className="small faint" style={{ margin: "6px 0" }}>
        Leave empty to use the backend on this computer. On a second laptop enter
        the always-on backend, e.g. <code>http://teacher-pc:8642</code>.
      </p>
      <div className="row" style={{ gap: 8 }}>
        <input style={{ flex: 1 }} value={url} placeholder={window.location.origin}
          aria-label="Shared backend URL"
          onChange={(e) => setUrl(e.target.value)} />
        <Button size="sm" loading={busy} onClick={check}>Test &amp; save</Button>
      </div>
      {result && (
        <div className={`notice ${result.ok ? "info" : "err"} small`}
          style={{ marginTop: 8 }} role={result.ok ? undefined : "alert"}>
          {result.text}
        </div>
      )}
      <div className="faint small" style={{ marginTop: 6 }}>
        Requests go to <code>{origin}</code> · device id{" "}
        <code>{deviceId().slice(0, 12)}…</code>
      </div>
      {crossOrigin && (
        <div className="faint small" style={{ marginTop: 4 }}>
          Another machine: its backend must list <code>{window.location.origin}</code>{" "}
          in <code>MC5_ALLOWED_ORIGINS</code>, or the browser will block the request.
        </div>
      )}
    </div>
  );
}

/* ================================================= this installation's state */

/**
 * What THIS installation can prove about itself (Stage 3 checklist).
 *
 * Everything shown is a real answer: the backend badge follows `GET /ping` and
 * the device id is this browser's own stable id. Nothing here is green merely
 * because the page rendered.
 *
 * The Tiny Agent switch that used to sit here was removed with the agent itself
 * (§1): this version runs none, so there is no switch to offer. The device
 * identity, the registration and the sync paths are all still here.
 */
function ThisInstallation() {
  const [ping, setPing] = useState<{ ok: boolean; text: string } | null>(null);

  const refresh = useCallback(async () => {
    try {
      const info = await pingBackend();
      setPing({ ok: true, text: `Math Checker backend v${info.version}` });
    } catch (err) {
      setPing({ ok: false, text: (err as Error).message });
    }
  }, []);

  useEffect(() => { refresh(); }, [refresh]);

  return (
    <div className="card" style={{ padding: "12px 16px" }}
      data-testid="this-installation">
      <div className="row wrap" style={{ gap: 18, alignItems: "center" }}>
        <div>
          <div className="faint small">Shared backend</div>
          <div className="row" style={{ gap: 8, alignItems: "center" }}>
            <Badge tone={ping ? (ping.ok ? "ok" : "err") : "mute"}
              icon={ping ? (ping.ok ? "check-circle" : "alert") : "activity"}>
              {ping ? (ping.ok ? "CONNECTED" : "UNREACHABLE") : "checking…"}
            </Badge>
            <span className="small">{ping ? ping.text : distributedOrigin()}</span>
          </div>
        </div>
        <div>
          <div className="faint small">Device</div>
          <div className="small"><code>{deviceId().slice(0, 12)}…</code></div>
        </div>
        <div>
          <div className="faint small">Tiny Agent</div>
          <div className="row" style={{ gap: 8, alignItems: "center" }}>
            <Badge tone="mute">not used in this version</Badge>
            <span className="small faint">
              no background agent runs here; corrections, Classroom and sync are
              unaffected
            </span>
          </div>
        </div>
      </div>
      {ping && !ping.ok && (
        <div className="notice err small" style={{ marginTop: 8 }} role="alert">
          {ping.text}
        </div>
      )}
    </div>
  );
}

/* =================================================================== login */

function LoginScreen({ onSignedIn }: { onSignedIn: (s: DistributedSession) => void }) {
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const session = await login(username.trim(), password, deviceId());
      onSignedIn(session);
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div style={{ maxWidth: 460, margin: "8vh auto 0" }}>
      <div className="card" style={{ padding: 24 }}>
        <h2 style={{ marginTop: 0 }}>Multi-School sign in</h2>
        <p className="small faint">
          Shared-backend account — Teacher, Co-Teacher, Assistant or Developer.
        </p>
        <form onSubmit={submit} style={{ display: "grid", gap: 12 }}>
          <label className="small">Username
            <input value={username} autoFocus autoComplete="username"
              onChange={(e) => setUsername(e.target.value)} /></label>
          <label className="small">Password
            <input type="password" value={password} autoComplete="current-password"
              onChange={(e) => setPassword(e.target.value)} /></label>
          {error && <div className="notice err small">{error}</div>}
          <Button variant="primary" block loading={busy}
            disabled={!username.trim() || !password} type="submit">
            Sign in
          </Button>
        </form>
      </div>
      <SharedBackendPanel />
    </div>
  );
}

/* =========================================================== schools tree */

function SchoolsTree({ schools, canManage, onChanged }: {
  schools: SchoolsFeed | null; canManage: boolean; onChanged: () => void;
}) {
  const toast = useToast();
  const [creating, setCreating] = useState<string | null>(null);
  const [renaming, setRenaming] = useState<string | null>(null);

  if (!schools) return <Loading label="Loading schools…" />;
  const { schools: list, classes } = schools;

  async function withToast(p: Promise<unknown>, ok: string) {
    try { await p; toast("ok", ok); onChanged(); }
    catch (e) { toast("err", (e as Error).message); }
  }

  return (
    <Section title="Schools & classes" icon="class"
      hint="Class names repeat across schools (10A in School A ≠ 10A in School B) — every node has a stable internal ID.">
      {list.length === 0 ? (
        <Empty icon="class" title="No schools yet"
          hint={canManage ? "Create the first school to get started."
            : "No schools are assigned to your account."} />
      ) : (
        <div style={{ display: "grid", gap: 12 }}>
          {list.map((s) => {
            const schoolClasses = classes.filter((c) => c.school_id === s.id);
            return (
              <div key={s.id} className="card" style={{ padding: "12px 14px" }}>
                <div style={{ display: "flex", alignItems: "center", gap: 10,
                  flexWrap: "wrap" }}>
                  <strong style={{ fontSize: 15 }}>{s.name}</strong>
                  {s.code && <Badge tone="mute">{s.code}</Badge>}
                  <Badge tone={s.active ? "ok" : "mute"}>
                    {s.active ? "active" : "inactive"}</Badge>
                  <span style={{ flex: 1 }} />
                  {canManage && (
                    <>
                      <Button size="sm" variant="ghost" icon="plus"
                        onClick={() => setCreating(`class:${s.id}`)}>Class</Button>
                      <Button size="sm" variant="ghost" icon="edit"
                        onClick={() => setRenaming(s.id)}>Rename</Button>
                      <Button size="sm" variant="ghost"
                        icon={s.active ? "pause" : "play"}
                        onClick={() => withToast(
                          setSchoolActive(s.id, !s.active),
                          s.active ? "School deactivated" : "School activated")}>
                        {s.active ? "Deactivate" : "Activate"}
                      </Button>
                    </>
                  )}
                </div>
                <div style={{ display: "flex", gap: 6, flexWrap: "wrap",
                  marginTop: 8 }}>
                  {schoolClasses.length === 0 && (
                    <span className="small faint">No classes</span>
                  )}
                  {schoolClasses.map((c) => (
                    <span key={c.id} className="chip" style={{
                      display: "inline-flex", gap: 6, alignItems: "center",
                      padding: "3px 10px", borderRadius: 999,
                      background: "var(--surface-2, rgba(128,128,128,.12))",
                      fontSize: 13,
                    }}>
                      {c.name}
                      {c.academic_year && <span className="faint">{c.academic_year}</span>}
                      {!c.active && <span className="faint">(inactive)</span>}
                    </span>
                  ))}
                </div>
              </div>
            );
          })}
        </div>
      )}
      {canManage && (
        <div style={{ marginTop: 12 }}>
          <Button icon="plus" onClick={() => setCreating("school")}>New school</Button>
        </div>
      )}

      {creating && (
        <CreateModal kind={creating} onClose={() => setCreating(null)}
          onDone={() => { setCreating(null); onChanged(); }} />
      )}
      {renaming && (
        <RenameModal school={list.find((s) => s.id === renaming)!}
          onClose={() => setRenaming(null)}
          onDone={() => { setRenaming(null); onChanged(); }} />
      )}
    </Section>
  );
}

function CreateModal({ kind, onClose, onDone }: {
  kind: string; onClose: () => void; onDone: () => void;
}) {
  const toast = useToast();
  const schoolId = kind.startsWith("class:") ? kind.slice(6) : null;
  const [name, setName] = useState("");
  const [extra, setExtra] = useState("");
  const [busy, setBusy] = useState(false);

  async function submit() {
    setBusy(true);
    try {
      if (schoolId) {
        await createClass(schoolId, name.trim(), extra.trim() || undefined);
        toast("ok", `Class ${name.trim()} created`);
      } else {
        await createSchool(name.trim(), extra.trim() || undefined);
        toast("ok", `School ${name.trim()} created`);
      }
      onDone();
    } catch (e) {
      toast("err", (e as Error).message);
    } finally { setBusy(false); }
  }

  return (
    <Modal title={schoolId ? "New class" : "New school"} icon="plus" onClose={onClose}
      footer={<>
        <Button variant="ghost" onClick={onClose}>Cancel</Button>
        <Button variant="primary" loading={busy} disabled={!name.trim()} onClick={submit}>
          Create
        </Button>
      </>}>
      <div style={{ display: "grid", gap: 10 }}>
        <label className="small">{schoolId ? "Class name" : "School name"}
          <input value={name} autoFocus placeholder={schoolId ? "e.g. 10A" : "e.g. School A"}
            onChange={(e) => setName(e.target.value)} /></label>
        <label className="small">{schoolId ? "Academic year (optional)" : "Short code (optional)"}
          <input value={extra} placeholder={schoolId ? "e.g. 2026/27" : "e.g. SA"}
            onChange={(e) => setExtra(e.target.value)} /></label>
      </div>
      {!schoolId && (
        <p className="small faint" style={{ marginTop: 8 }}>
          Class names are NOT globally unique — School A/10A and School B/10A
          stay separate classes with separate IDs.
        </p>
      )}
    </Modal>
  );
}

function RenameModal({ school, onClose, onDone }: {
  school: { id: string; name: string }; onClose: () => void; onDone: () => void;
}) {
  const toast = useToast();
  const [name, setName] = useState(school.name);
  const [busy, setBusy] = useState(false);

  async function submit() {
    setBusy(true);
    try {
      await renameSchool(school.id, name.trim());
      toast("ok", "School renamed");
      onDone();
    } catch (e) {
      toast("err", (e as Error).message);
    } finally { setBusy(false); }
  }

  return (
    <Modal title="Rename school" icon="edit" onClose={onClose}
      footer={<>
        <Button variant="ghost" onClick={onClose}>Cancel</Button>
        <Button variant="primary" loading={busy} disabled={!name.trim()} onClick={submit}>
          Rename
        </Button>
      </>}>
      <label className="small">Name
        <input value={name} autoFocus onChange={(e) => setName(e.target.value)} /></label>
    </Modal>
  );
}

/* ========================================================== change password */

function ChangePasswordModal({ onClose }: { onClose: () => void }) {
  const toast = useToast();
  const [current, setCurrent] = useState("");
  const [next, setNext] = useState("");
  const [confirm, setConfirm] = useState("");
  const [busy, setBusy] = useState(false);
  const mismatch = next.length > 0 && confirm.length > 0 && next !== confirm;

  async function submit() {
    setBusy(true);
    try {
      await changePassword(current, next);
      toast("ok", "Password changed — every other session was signed out");
      onClose();
    } catch (e) {
      toast("err", (e as Error).message);
    } finally { setBusy(false); }
  }

  return (
    <Modal title="Change password" icon="user" onClose={onClose}
      footer={<>
        <Button variant="ghost" onClick={onClose}>Cancel</Button>
        <Button variant="primary" loading={busy} disabled={
          !current || next.length < 8 || mismatch
        } onClick={submit}>Change password</Button>
      </>}>
      <div style={{ display: "grid", gap: 10 }}>
        <label className="small">Current password
          <input type="password" value={current} autoComplete="current-password"
            onChange={(e) => setCurrent(e.target.value)} /></label>
        <label className="small">New password (min 8 characters)
          <input type="password" value={next} autoComplete="new-password"
            onChange={(e) => setNext(e.target.value)} /></label>
        <label className="small">Repeat new password
          <input type="password" value={confirm} autoComplete="new-password"
            onChange={(e) => setConfirm(e.target.value)} /></label>
        {mismatch && <div className="notice err small">The two passwords differ.</div>}
      </div>
      <p className="small faint" style={{ marginTop: 8 }}>
        Every other signed-in device for THIS account is signed out
        immediately (a stolen token stops working). You stay signed in here.
      </p>
    </Modal>
  );
}

/* ==================================================================== page */

export default function DistributedPage() {
  const [session, setSession] = useState<DistributedSession | null>(storedSession);
  const [checked, setChecked] = useState(!storedToken()); // no token → nothing to verify
  const [schools, setSchools] = useState<SchoolsFeed | null>(null);
  const [changingPassword, setChangingPassword] = useState(false);
  // Multi-account availability of THIS backend. `null` = not known yet, which
  // is treated as "off": a panel is shown only after the backend said it may be.
  const [multiAccount, setMultiAccount] = useState<MultiAccountMode | null>(null);

  useEffect(() => {
    distributedCapabilities().then(setMultiAccount).catch(() => setMultiAccount(null));
  }, []);
  const multiAccountOn = multiAccount?.multi_account ?? false;

  const hasPerm = useCallback((perm: string) => {
    const perms = session?.permissions ?? [];
    return perms.includes("*") || perms.includes(perm);
  }, [session]);

  const role = session?.role;
  const isDeveloper = role === "DEVELOPER";
  const isTeacher = role === "TEACHER";
  const isOperator = isDeveloper || isTeacher; // unrestricted roles
  const canManageSchools = hasPerm("schools.manage");
  const canManageAccounts = hasPerm("accounts.manage_non_teacher");
  // manage_own implies seeing what you manage (Assistants hold that, not
  // view_own — the backend accepts either for the same scoped list).
  const canViewWorkers = isOperator || hasPerm("workers.view_own")
    || hasPerm("workers.manage_own");

  const reloadSchools = useCallback(() => {
    listSchools().then(setSchools).catch(() => setSchools(null));
  }, []);

  // Verify the stored token is still valid on mount (§62: never trust a stale
  // session silently), and refresh the profile.
  useEffect(() => {
    if (!storedToken()) { setChecked(true); return; }
    fetchMe()
      .then((me) => { setSession(me); setChecked(true); })
      .catch(() => { clearSession(); setSession(null); setChecked(true); });
  }, []);

  useEffect(() => { if (session) reloadSchools(); }, [session, reloadSchools]);

  const scopeLine = useMemo(() => {
    if (!session) return "";
    const { school_ids, class_ids } = session.scope;
    if (isOperator) return "global view";
    const parts: string[] = [];
    if (school_ids.length) parts.push(`${school_ids.length} school(s)`);
    if (class_ids.length) parts.push(`${class_ids.length} class(es)`);
    return parts.length ? parts.join(" · ") : "no assignment yet";
  }, [session, isOperator]);

  if (!checked) return <Loading label="Checking session…" />;
  if (!session) return <LoginScreen onSignedIn={setSession} />;

  return (
    <>
      <PageHeader title="Multi-School" icon="classroom"
        sub={`Signed in as ${session.display_name} (${session.username}) · ${session.role.replace("_", "-")} · ${scopeLine} · app v${session.app_version}`}>
        <Button size="sm" variant="ghost" icon="user"
          onClick={() => setChangingPassword(true)}>Change password</Button>
        <Button size="sm" variant="ghost" icon="logout"
          onClick={async () => {
            await logout();
            setSession(null);
          }}>Sign out</Button>
      </PageHeader>

      <ThisInstallation />

      {changingPassword && (
        <ChangePasswordModal onClose={() => setChangingPassword(false)} />
      )}

      {!isOperator && session.scope.school_ids.length === 0
        && session.scope.class_ids.length === 0 && (
        <div className="notice warn" style={{ marginBottom: 14 }}>
          <strong>No assignment yet.</strong> The Teacher has not assigned you to
          any school or class, so there is nothing to correct. Ask the Teacher to
          scope your account.
        </div>
      )}

      <div style={{ display: "grid", gap: 14 }}>
        {/* Developer/Teacher sync health. Gated on the SAME permission the
            backend enforces ('diagnostics.view'), so the UI can never show a
            section whose data the caller is not allowed to read. */}
        <SyncMonitorPanel canView={hasPerm("diagnostics.view")} />

        {!isOperator && (
          <MyUpdateStatus deviceId={deviceId() || null} />
        )}

        <SchoolsTree schools={schools} canManage={canManageSchools}
          onChanged={reloadSchools} />

        <WorkersPanel canView={canViewWorkers} />

        {(isDeveloper || isTeacher) && (
          <ResultsPanel schools={schools} />
        )}

        {/* Multi-account surfaces: account administration, role assignment,
            team management and team-scoped sharing. They are hidden while the
            backend reports multi-account mode as off (the backend refuses the
            operations regardless); everything else on this page — sign-in,
            schools/classes, worker presence, results, report delivery,
            updates, errors, audit and the sync monitor — keeps working. */}
        {multiAccountOn ? (
          <>
            <AccountsPanel canManage={canManageAccounts} schools={schools} />
            <TeamsPanel canManage={canManageAccounts} schools={schools} />
          </>
        ) : (
          <Section title="Accounts, roles & teams" icon="user"
            hint="Part of multi-account mode">
            <div className="notice info" style={{ marginBottom: 0 }}>
              <Icon name="info" size={15} />
              <span>
                Multi-account mode is <strong>off</strong> on this installation, so
                account administration, role assignment, team management and
                team-based account discovery are unavailable — and the backend
                refuses them even if a request is sent directly. Nothing has been
                deleted: existing accounts, roles, teams, scopes, device links and
                audit records are kept, and an owner can switch the mode back on in
                Settings. Sign-in, corrections, reports, device registration,
                heartbeats and sync are unaffected.
              </span>
            </div>
          </Section>
        )}

        <UpdatesPanel isDeveloper={isDeveloper} />

        <ErrorsPanel canViewAll={isOperator || hasPerm("diagnostics.view")} />

        {(isDeveloper || hasPerm("diagnostics.view")) && <AuditPanel />}
      </div>
    </>
  );
}
