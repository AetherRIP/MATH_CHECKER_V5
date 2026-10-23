import { useEffect, useState } from "react";
import {
  accessState, api, authStatus, lifecycleStatus, recheckPublicAddress,
  setHostAccount, setMultiAccount, setPublicAddress, timeAgo,
  type AccessState, type AuthStatus, type LifecycleConfig, type LifecycleStatus,
  type MultiAccountCapabilities, type PublicAddressState,
  type RemoteAccessStatus, type Settings,
} from "../api";
import { Badge, Button, Icon, PageHeader, Section, useToast } from "../components";
import { SyncClassroomButton } from "../SyncClassroom";

function Row({ ok, label, detail }: {
  ok: boolean | null; label: string; detail?: React.ReactNode;
}) {
  return (
    <div className="row between" style={{ padding: "5px 0", gap: 10 }}>
      <span className="row" style={{ gap: 7, alignItems: "center" }}>
        <Icon name={ok === null ? "clock" : ok ? "check-circle" : "x"}
          size={15} />
        <span>{label}</span>
      </span>
      <span className="faint small" style={{ textAlign: "right" }}>{detail}</span>
    </div>
  );
}

/**
 * One address with its REAL preview link.
 *
 * "Open …Preview" is an ordinary anchor to the actual address, so the
 * browser makes the same request another user's device would make. There is
 * no screenshot and no simulated route: if the address is broken the tab
 * fails to load, which is exactly the signal we want.
 */
function AddressRow({ label, url, ok, openLabel, copyLabel, copied, onCopy }: {
  label: string; url: string | null; ok: boolean;
  openLabel: string; copyLabel: string; copied: string | null;
  onCopy: (label: string, url: string) => void;
}) {
  return (
    <div style={{ padding: "7px 0" }}>
      <div className="row between" style={{ gap: 10 }}>
        <span className="row" style={{ gap: 7, alignItems: "center" }}>
          <Icon name={ok ? "check-circle" : "x"} size={15} />
          <span>{label}</span>
        </span>
        <span className="faint small">{ok ? "Online" : "Offline"}</span>
      </div>
      {url ? (
        <div className="row wrap" style={{ gap: 8, alignItems: "center", marginTop: 4 }}>
          <code style={{ wordBreak: "break-all" }}>{url}</code>
        </div>
      ) : (
        <div className="faint small" style={{ marginTop: 2 }}>
          No address — not enabled.
        </div>
      )}
      {url && (
        <div className="row wrap" style={{ gap: 8, marginTop: 6 }}>
          <a className="btn secondary sm" href={url} target="_blank"
            rel="noreferrer" style={{ textDecoration: "none" }}>
            <Icon name="external" size={14} /> {openLabel}
          </a>
          <Button variant="secondary" size="sm"
            icon={copied === label ? "check" : "copy"}
            onClick={() => onCopy(label, url)}>
            {copied === label ? "Copied" : copyLabel}
          </Button>
        </div>
      )}
    </div>
  );
}

/**
 * Remote Access panel (§23-§25): measured reachability and real previews.
 *
 * Every ✓ on this card is a measurement - local and LAN come from an actual
 * ``/api/health`` request made by the server, and Public only reads online
 * when the launcher verified the public address end to end. A running tunnel
 * process is never reported as success. The card polls a tiny status payload
 * every 15s, and only while Settings is open.
 */
function RemoteAccessCard() {
  const [state, setState] = useState<RemoteAccessStatus | null>(null);
  const [copied, setCopied] = useState<string | null>(null);
  const [tick, setTick] = useState(0);

  useEffect(() => {
    const load = () => api<RemoteAccessStatus>("/remote/status")
      .then(setState).catch(() => {});
    load();
    const id = setInterval(load, 15_000);
    return () => clearInterval(id);
  }, [tick]);

  const online = Boolean(state && state.status === "online" && state.public_url);
  const r = state?.reachable;

  function copy(label: string, url: string) {
    void navigator.clipboard?.writeText(url).then(() => {
      setCopied(label);
      window.setTimeout(() => setCopied(null), 2000);
    });
  }

  return (
    <Section title="Remote Access" icon="wifi"
      hint="measured, never assumed"
      actions={
        <Button variant="secondary" size="sm" icon="refresh"
          onClick={() => setTick((n) => n + 1)}>Re-check</Button>
      }>
      {!state ? (
        <div className="skeleton" style={{ height: 120 }} />
      ) : (
        <div className="grid grid-2" style={{ alignItems: "start" }}>
          <div>
            <AddressRow label="Local" url={state.local_url}
              ok={Boolean(r?.local)} openLabel="Open Local"
              copyLabel="Copy Local Address" copied={copied} onCopy={copy} />
            <AddressRow label="LAN" url={state.lan_url}
              ok={Boolean(r?.lan)} openLabel="Open LAN Preview"
              copyLabel="Copy LAN Address" copied={copied} onCopy={copy} />
            <p className="faint small" style={{ marginTop: 4, marginBottom: 0 }}>
              The public address and its ON/OFF switch live in the Public Address
              card above; this panel only measures reachability.
            </p>
          </div>

          <div>
            <div className="faint small" style={{ marginBottom: 4 }}>Diagnostic</div>
            <Row ok={Boolean(r?.local)} label="Local" detail="Reachable" />
            <Row ok={state.lan_enabled ? Boolean(r?.lan) : null} label="LAN"
              detail={state.lan_enabled
                ? (r?.lan ? "Reachable" : "Not reachable")
                : "Not enabled"} />
            <Row ok={online} label="Public"
              detail={online ? "Reachable" : (state.status === "starting"
                ? "Starting…" : "Failed")} />
            <Row ok={Boolean(r?.api)} label="API"
              detail={r?.api ? "Working" : "Not responding"} />
            <Row ok={r?.websocket === "not_required" ? null : true}
              label="WebSocket"
              detail={r?.websocket === "not_required"
                ? "Not required" : "Working"} />
            <Row ok={null} label="Live updates"
              detail={r?.events === "polling"
                ? "Polling (tunnel buffers streams)"
                : "Streaming"} />
            <div className="faint small" style={{ marginTop: 6 }}>
              Last verified {state.last_verified ?? "—"}
            </div>
          </div>
        </div>
      )}

      {state && !online && (
        <div className="notice warn small" style={{ marginTop: 8 }}>
          <Icon name="alert" size={15} />
          <span><strong>Reason:</strong> {state.reason ?? "Public access is off."}</span>
        </div>
      )}

      {state && (
        <p className="faint small" style={{ marginBottom: 0, marginTop: 8 }}>
          {online
            ? `Verification ran ${Object.entries(state.verified_checks ?? {})
                .map(([k, v]) => `${k} ${v ? "ok" : "failed"}`).join(", ") || "—"}.`
            : "Either the Public Address switch above is off, or the address "
              + "has not been verified yet. Switching it on starts one tunnel, "
              + "verifies the address end to end, and reports the real reason "
              + "here if anything fails."}
        </p>
      )}
    </Section>
  );
}

/* ================================================== public address switch */

const PUBLIC_BADGE: Record<PublicAddressState["status"],
  { tone: "ok" | "warn" | "err" | "mute"; label: string }> = {
  online: { tone: "ok", label: "Connected" },
  starting: { tone: "warn", label: "Starting" },
  failed: { tone: "err", label: "Failed" },
  disabled: { tone: "mute", label: "Disabled" },
};

/**
 * PUBLIC ADDRESS — the ON/OFF switch for the app-managed public endpoint.
 *
 * Two different things are shown on purpose and never merged:
 *   * the SWITCH position — the saved preference, which survives a restart;
 *   * the STATE — what the backend measured (Disabled / Starting / Connected /
 *     Failed), because a tunnel that did not start must never look switched on.
 *
 * The address appears only while a verification stands, and the button is
 * disabled while a change is in flight so a double click cannot start two
 * tunnels. Turning it off stops the real tunnel process tree; it does not touch
 * the separately configured Backend URL, and it can be turned back on here.
 */
function PublicAddressCard({ state, onChange, unavailable }: {
  state: AccessState | null;
  onChange: (next: AccessState) => void;
  unavailable: boolean;
}) {
  const toast = useToast();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [copied, setCopied] = useState(false);
  // What the user just asked for, while the request is in flight. It moves the
  // SWITCH (so a click never looks ignored during the ~1 minute a real tunnel
  // needs) while the badge stays honest: "Working…", never "Connected".
  const [pending, setPending] = useState<boolean | null>(null);

  const pa = state?.public_address ?? null;
  const isOwner = Boolean(state?.owner);
  const status = pa?.status ?? "disabled";
  const badge = PUBLIC_BADGE[status];
  // Only a LIVE, verified address is displayed as an address.
  const address = status === "online" ? (pa?.public_url ?? null) : null;
  const externallyRun = pa?.managed_by === "launcher" || pa?.managed_by === "external";

  async function toggle(enabled: boolean) {
    if (!state || busy) return;
    setBusy(true);
    setPending(enabled);
    setError(null);
    try {
      const next = await setPublicAddress(enabled);
      onChange({ ...state, public_address: next.public_address,
                 multi_account: next.multi_account });
      if (!enabled) {
        toast("ok", "Public address switched off — the tunnel was stopped and "
          + "the address is no longer advertised.");
      } else if (next.public_address.status === "online") {
        toast("ok", "Public address is live — the endpoint answered every check.");
      } else {
        toast("err", next.public_address.reason
          ?? "The public address could not be started.");
      }
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
      setPending(null);
    }
  }

  async function recheck() {
    if (!state || busy) return;
    setBusy(true);
    setError(null);
    try {
      const next = await recheckPublicAddress();
      onChange({ ...state, public_address: next.public_address });
      toast(next.public_address.status === "online" ? "ok" : "err",
        next.public_address.status === "online"
          ? "The public address answered every check."
          : (next.public_address.reason ?? "The public address did not answer."));
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <Section title="Public Address" icon="wifi"
      hint="the app-managed endpoint"
      actions={<Badge tone={badge.tone}>{busy ? "Working…" : badge.label}</Badge>}>
      {error && (
        <div className="notice err small" style={{ marginBottom: 10 }} role="alert">
          {error}
        </div>
      )}

      {unavailable && !state ? (
        <div className="notice warn small" style={{ marginBottom: 0 }}>
          <Icon name="info" size={15} />
          <span>
            This application process was started before the Public Address
            control existed, so it cannot answer for it. Close and reopen Math
            Checker (no reinstall and no re-configuration) and the switch will
            appear here.
          </span>
        </div>
      ) : !state ? (
        <div className="skeleton" style={{ height: 96 }} />
      ) : !isOwner ? (
        <p className="faint small" style={{ marginBottom: 0 }}>
          Only this installation's own computer, or a signed-in Developer, can
          change public access — so no switch is shown here. Remote clients keep
          using whatever the owner has set.
        </p>
      ) : (
        <>
          <label className="switch">
            <input type="checkbox" checked={pending ?? Boolean(pa?.requested)}
              disabled={busy} aria-label="Public Address"
              onChange={(e) => toggle(e.target.checked)} />
            <span className="track" />
            <span className="muted">
              {busy
                ? (pending ? "Switching the public address ON…"
                           : "Switching the public address OFF…")
                : pa?.requested
                  ? "Public address is switched ON"
                  : "Public address is switched OFF — nothing is exposed"}
            </span>
          </label>

          {busy && (
            <p className="faint small" style={{ marginTop: 8, marginBottom: 0 }}>
              Creating the tunnel and proving it serves this app takes up to a
              minute. You can leave this page — the state shown here is always the
              endpoint's real state, never an assumption.
            </p>
          )}

          {externallyRun && (
            <div className="notice warn small" style={{ marginTop: 10 }}>
              <Icon name="alert" size={15} />
              <span>
                Public access in this session was started outside the
                application (<code>start_public.bat</code> / <code>remote.py</code>).
                The application cannot stop that tunnel — close that window
                (Ctrl+C) to end public access, then use this switch.
              </span>
            </div>
          )}

          {status === "online" && address && (
            <div style={{ marginTop: 10 }}>
              <code style={{ wordBreak: "break-all" }}>{address}</code>
              <div className="row wrap" style={{ gap: 8, marginTop: 6 }}>
                <a className="btn secondary sm" href={address} target="_blank"
                  rel="noreferrer" style={{ textDecoration: "none" }}>
                  <Icon name="external" size={14} /> Open Public Preview
                </a>
                <Button variant="secondary" size="sm"
                  icon={copied ? "check" : "copy"}
                  onClick={() => void navigator.clipboard?.writeText(address)
                    .then(() => {
                      setCopied(true);
                      window.setTimeout(() => setCopied(false), 2000);
                    })}>
                  {copied ? "Copied" : "Copy Public Address"}
                </Button>
                <Button variant="secondary" size="sm" icon="refresh"
                  loading={busy} onClick={recheck}>Re-check</Button>
              </div>
              <p className="faint small" style={{ marginTop: 6, marginBottom: 0 }}>
                Verified {pa?.last_verified
                  ? timeAgo(Number(pa.last_verified)) : "now"}
                {Object.entries(pa?.checks ?? {}).length > 0 && (
                  <> · {" "}
                    {Object.entries(pa?.checks ?? {})
                      .map(([k, v]) => `${k} ${v ? "ok" : "failed"}`).join(", ")}</>
                )}
                {" "}· the login page is served here and sign-in plus permissions
                apply as they do locally.
              </p>
            </div>
          )}

          {status === "starting" && (
            <div className="notice info small" style={{ marginTop: 10 }}>
              <Icon name="clock" size={15} />
              <span>Creating the public tunnel and verifying the address before
                it is shown. This takes a few seconds.</span>
            </div>
          )}

          {status === "failed" && (
            <div className="notice err small" style={{ marginTop: 10 }} role="alert">
              <Icon name="alert" size={15} />
              <span>
                <strong>Not active.</strong> {pa?.reason ?? "The public address is not reachable."}
              </span>
            </div>
          )}

          {status === "disabled" && pa?.last_address && (
            <p className="faint small" style={{ marginTop: 8, marginBottom: 0 }}>
              Last verified address (inactive — not being served):{" "}
              <code>{pa.last_address}</code>. The preference is saved, so
              switching ON publishes an address again without re-entering anything.
            </p>
          )}

          {!pa?.can_enable && pa?.blocked_reason && !externallyRun && (
            <div className="notice warn small" style={{ marginTop: 10 }}>
              <Icon name="info" size={15} />
              <span>{pa.blocked_reason}</span>
            </div>
          )}

          <p className="faint small" style={{ marginTop: 10, marginBottom: 0 }}>
            This is the endpoint remote clients reach. It is <strong>not</strong> the
            shared backend address on the Multi-School page: switching it off
            never affects this computer's own access or a backend that runs on
            another machine. While it is on, HTTPS is used, the sign-in gate and
            permissions stay enforced, and no keys, tokens or passwords are ever
            part of the address.
          </p>
          <p className="faint small" style={{ marginBottom: 0 }}>
            <strong>What this switch cannot control:</strong> a reverse proxy,
            router port-forward or tunnel that you configured yourself, outside
            this application, keeps working whatever this switch says. Turning
            it off stops the address <em>this application</em> manages and stops
            advertising it; it cannot reach into a separate service you run.
          </p>
        </>
      )}
    </Section>
  );
}

/* =========================================================== host account */

/**
 * HOST ACCOUNT (§5).
 *
 * The installation's own sign-in. The password is only ever sent to the
 * server, which stores a salted hash; nothing is displayed back, no default
 * account exists, and changing an existing account requires the current
 * password even on this computer. The card never renders a password field
 * with a saved value, because the server never returns one.
 */
function HostAccountCard({ auth, onChanged }: {
  auth: AuthStatus | null;
  onChanged?: () => void;
}) {
  const toast = useToast();
  const configured = Boolean(auth?.configured);
  const [username, setUsername] = useState(auth?.username ?? "");
  const [current, setCurrent] = useState("");
  const [next, setNext] = useState("");
  const [confirm, setConfirm] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [done, setDone] = useState<string | null>(null);

  useEffect(() => { setUsername(auth?.username ?? ""); }, [auth?.username]);

  async function save() {
    setError(null);
    setDone(null);
    if (next.length < 10) {
      setError("Choose a password of at least 10 characters.");
      return;
    }
    if (next !== confirm) {
      setError("The two password entries do not match.");
      return;
    }
    if (configured && !current) {
      setError("Enter your current password to change the account.");
      return;
    }
    setBusy(true);
    try {
      const result = await setHostAccount({
        username: username.trim() || undefined,
        password: next,
        current_password: configured ? current : undefined,
      });
      setCurrent(""); setNext(""); setConfirm("");
      setDone(result.username ?? "account updated");
      toast("ok", configured
        ? "Host account updated — use the new credentials at the next sign-in."
        : "Host account created — sign-in is now required on the public address.");
      onChanged?.();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <Section title="Host account" icon="lock"
      hint="this installation's own sign-in"
      actions={<Badge tone={configured ? "ok" : "warn"}>
        {configured ? "Configured" : "Not set up"}
      </Badge>}>
      <p className="small" style={{ marginTop: 0 }}>
        {configured
          ? "Change the username or password used to sign in. Your current "
            + "password is required, the new password is stored only as a "
            + "salted hash, and it is never shown or logged again."
          : "No account exists yet. Create the host account before switching "
            + "public access on — the public address then serves the login "
            + "page and refuses everything else until someone signs in."}
      </p>

      <div className="grid grid-2">
        <label className="field">
          <span>Username
            {configured && (
              <span className="hint"> — leave blank to keep the current one</span>
            )}
          </span>
          <input className="input" value={username} autoComplete="username"
            placeholder={configured
              ? (auth?.username ?? "unchanged — sign in to see it")
              : "teacher"}
            onChange={(e) => setUsername(e.target.value)} />
        </label>
        {configured && (
          <label className="field">
            <span>Current password</span>
            <input className="input" type="password" value={current}
              autoComplete="current-password"
              onChange={(e) => setCurrent(e.target.value)} />
          </label>
        )}
        <label className="field">
          <span>{configured ? "New password" : "Password"}</span>
          <input className="input" type="password" value={next}
            autoComplete="new-password"
            onChange={(e) => setNext(e.target.value)} />
        </label>
        <label className="field">
          <span>Repeat password</span>
          <input className="input" type="password" value={confirm}
            autoComplete="new-password"
            onChange={(e) => setConfirm(e.target.value)} />
        </label>
      </div>

      {error && (
        <div className="notice err small" style={{ marginBottom: 10 }} role="alert">
          {error}
        </div>
      )}
      {done && (
        <div className="notice ok small" style={{ marginBottom: 10 }}>
          Account updated for <strong>{done}</strong>. Nothing was displayed or
          stored in plain text.
        </div>
      )}

      <div className="row wrap" style={{ gap: 8, alignItems: "center" }}>
        <Button variant="primary" icon="lock" loading={busy} onClick={save}
          disabled={!next}>
          {configured ? "Change host account" : "Create host account"}
        </Button>
        <span className="faint small">
          Stored as a salted hash · never returned by any API response ·
          rate-limited like sign-in
        </span>
      </div>

      <p className="faint small" style={{ marginBottom: 0, marginTop: 10 }}>
        If the password is lost, a genuinely local request from this computer
        can set a new one the same way (this page, opened on the machine running
        Math Checker). Remote callers always need the current password, so a
        public address can never be used to take the account over.
      </p>
    </Section>
  );
}

/* ================================================= multi-account switch */

/**
 * MULTI-ACCOUNT MODE — availability of the accounts/roles/teams layer.
 *
 * Disabling it hides and BLOCKS multi-account functionality (the backend
 * refuses the operations itself) without deleting a single row, table or
 * module, so switching it back on restores the system as it was. The switch is
 * owner-only: the endpoint enforces that server-side, and the card refuses to
 * offer a control that would only fail.
 */
function MultiAccountCard({ caps, isOwner, onChange, unavailable }: {
  caps: MultiAccountCapabilities | null;
  isOwner: boolean;
  onChange: (caps: MultiAccountCapabilities) => void;
  unavailable: boolean;
}) {
  const toast = useToast();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function toggle(enabled: boolean) {
    if (busy) return;
    setBusy(true);
    setError(null);
    try {
      const next = await setMultiAccount(enabled);
      onChange(next.multi_account);
      toast("ok", enabled
        ? "Multi-account mode is ON — account, role and team features are available again."
        : "Multi-account mode is OFF — the interface and its operations are hidden and refused. Nothing was deleted.");
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  if (!caps) {
    return (
      <Section title="Multi-Account Mode" icon="user" hint="temporary switch">
        {unavailable ? (
          <p className="faint small" style={{ marginBottom: 0 }}>
            Not available in this running process — it was started before this
            setting existed. Reopen Math Checker to see it; multi-account data
            and settings are untouched either way.
          </p>
        ) : <div className="skeleton" style={{ height: 120 }} />}
      </Section>
    );
  }
  const on = caps.multi_account;

  return (
    <Section title="Multi-Account Mode" icon="user" hint="temporary switch"
      actions={<Badge tone={on ? "ok" : "mute"}>{on ? "ON" : "OFF"}</Badge>}>
      {error && (
        <div className="notice err small" style={{ marginBottom: 10 }} role="alert">
          {error}
        </div>
      )}

      {isOwner ? (
        <label className="switch">
          <input type="checkbox" checked={on} disabled={busy}
            aria-label="Multi-Account Mode"
            onChange={(e) => toggle(e.target.checked)} />
          <span className="track" />
          <span className="muted">
            {on
              ? "Accounts, roles, teams and shared-device discovery are available"
              : "Single-account mode — multi-account interface and operations are off"}
          </span>
        </label>
      ) : (
        <p className="small" style={{ marginTop: 0 }}>
          Currently <strong>{on ? "ON" : "OFF"}</strong> for this installation.
          Only the installation's own computer or a Developer can change it.
        </p>
      )}

      <p className="faint small" style={{ marginTop: 10, marginBottom: 0 }}>
        Turning this off temporarily hides account administration, role
        assignment, team management and team-based account discovery, and the
        backend refuses those operations too. <strong>Nothing is deleted:</strong>{" "}
        accounts, roles, permissions, teams, scopes, device links and audit
        records are kept exactly as they are, so switching it back on restores
        them. Sign-in, Google Classroom, corrections, grade storage, reports,
        retry/approve/sync, device registration and the shared-backend
        connection all keep working either way. No restart is needed
        {caps.restart_required ? " (this build asks for a restart)" : ""}.
        {caps.source === "env" && (
          <> The value is currently forced by the environment variable{" "}
            <code>{caps.env_var}</code>, which wins over this switch.</>
        )}
      </p>
      <p className="faint small" style={{ marginBottom: 0 }}>
        Stored in this installation's config as <code>{caps.setting}</code>
        {" "}(read on every request, so the change applies immediately).
      </p>
    </Section>
  );
}

/**
 * What the backend is REALLY doing right now, in one sentence.
 *
 * ``state`` comes from the running process's own state machine (see
 * app/lifecycle/monitor.py). Nothing here is inferred from how long the page
 * has been open, and a sleeping app says so plainly: the relay answers that
 * payload, and it reports ``backend: "stopped"``.
 */
const LIFECYCLE_LABEL: Record<string, string> = {
  serving: "In use — staying up",
  counting_down: "Nobody is using the app — countdown running",
  waiting_for_work: "Idle, finishing work before stopping",
  held: "Staying up — a reason forbids stopping",
  shutting_down: "Stopping now",
  always_on: "Always on (idle stop is switched off)",
  sleeping: "Stopped (starts again when the site is opened)",
  stopped: "Stopped",
};

function NumberField({ label, value, min, max, hint, suffix, onCommit }: {
  label: string; value: number; min: number; max: number;
  hint: string; suffix: string; onCommit: (next: number) => void;
}) {
  const [draft, setDraft] = useState(String(value));
  useEffect(() => setDraft(String(value)), [value]);
  return (
    <label className="field">
      <span>{label}</span>
      <div className="row" style={{ gap: 8, alignItems: "center" }}>
        <input type="number" min={min} max={max} value={draft}
          onChange={(e) => setDraft(e.target.value)}
          onBlur={() => {
            const parsed = Number(draft);
            if (!Number.isFinite(parsed)) { setDraft(String(value)); return; }
            const clamped = Math.max(min, Math.min(max, Math.round(parsed)));
            setDraft(String(clamped));
            if (clamped !== value) onCommit(clamped);
          }}
          style={{ maxWidth: 120 }} />
        <span className="faint small">{suffix}</span>
      </div>
      <span className="faint small">{hint}</span>
    </label>
  );
}

/**
 * Server lifecycle card.
 *
 * Two switches and three numbers, plus the state the app is in at this moment.
 * The state block polls ``/lifecycle/status`` every 5s and only while Settings
 * is open, so it costs nothing when the page is closed — and a status request
 * never counts as a user, so watching this card cannot keep an idle app awake.
 */
function ServerLifecycleCard({ config, missing, onUpdate }: {
  config: LifecycleConfig | null;
  missing: boolean;
  onUpdate: (payload: Partial<Settings>) => Promise<void>;
}) {
  const [live, setLive] = useState<LifecycleStatus | null>(null);
  const [unavailable, setUnavailable] = useState(false);

  useEffect(() => {
    const load = () => lifecycleStatus()
      .then((s) => { setLive(s); setUnavailable(false); })
      .catch(() => setUnavailable(true));
    load();
    const id = setInterval(load, 5000);
    return () => clearInterval(id);
  }, []);

  if (missing || !config) {
    return (
      <Section title="Server lifecycle" icon="activity"
        hint="unavailable in this running copy">
        <div className="notice warn small" style={{ marginBottom: 0 }}>
          <Icon name="info" size={15} />
          <span>
            This Math Checker process was started before on-demand start and
            idle stop existed, so it cannot report or change them. Restart
            Math Checker (Stop.bat, or Ctrl+C in its window, then start.bat);
            nothing here was lost and no reconfiguration is needed.
          </span>
        </div>
      </Section>
    );
  }

  const patch = (next: Partial<LifecycleConfig>) =>
    onUpdate({ lifecycle: { ...config, ...next } });

  const state = live?.state ?? "connecting";
  const blockers = live?.blockers ?? [];

  return (
    <Section title="Server lifecycle" icon="activity"
      hint={config.enabled
        ? (config.supervisor ? "on demand + idle stop" : "idle stop only")
        : "always on"}>
      <div className="row between" style={{ padding: "2px 0 8px" }}>
        <span className="row" style={{ gap: 7, alignItems: "center" }}>
          <Icon name={unavailable ? "x" : state === "serving" ? "check-circle" : "clock"}
            size={15} />
          <span>
            {unavailable
              ? "This process cannot report its lifecycle state."
              : LIFECYCLE_LABEL[state] ?? state}
          </span>
        </span>
        <span className="faint small">
          {live
            ? `${live.active_sessions} browser session${live.active_sessions === 1 ? "" : "s"} active`
            : "checking…"}
        </span>
      </div>
      {live?.countdown_seconds != null && (
        <p className="small" style={{ marginTop: 0 }}>
          Stopping in <strong>{Math.round(live.countdown_seconds)}s</strong>
          {" "}unless somebody opens the app again — a reload cancels it and
          keeps the app running.
        </p>
      )}
      {blockers.length > 0 && (
        <ul className="small faint" style={{ marginTop: 0 }}>
          {blockers.map((b) => (
            <li key={b.source}>{b.detail}</li>
          ))}
        </ul>
      )}

      <label className="switch">
        <input type="checkbox" checked={config.enabled}
          onChange={(e) => void patch({ enabled: e.target.checked })} />
        <span className="track" />
        <span className="muted">
          Stop the app when nobody is using it (after the countdown below)
        </span>
      </label>
      <p className="faint small">
        A correction, a report being written, a long document still being
        marked or an open database write always finishes first — the stop waits
        for real work, and it never survives beyond the maximum wait you set.
        Queued jobs, results and settings are untouched: the next start resumes
        them.
      </p>

      <label className="switch">
        <input type="checkbox" checked={config.supervisor}
          onChange={(e) => void patch({ supervisor: e.target.checked })} />
        <span className="track" />
        <span className="muted">
          Start it again automatically when the site is opened
        </span>
      </label>
      <p className="faint small">
        A small always-on relay holds the app's address and starts the backend
        the moment a page asks for it. It is tiny (no engine, no database), and
        it is what makes reopening the site after a stop work. <strong>Changing
        this switch needs a restart of Math Checker.</strong> With it off, a
        stopped app stays stopped until somebody starts it — and a published
        Public Address keeps the app up either way, because a visitor from the
        internet cannot start a program on this computer.
      </p>

      <div className="grid grid-2" style={{ gap: 12, marginTop: 4 }}>
        <NumberField label="Heartbeat interval" value={config.heartbeat_seconds}
          min={5} max={3600} suffix="seconds"
          hint="How often each open tab says it is still there."
          onCommit={(n) => void patch({ heartbeat_seconds: n })} />
        <NumberField label="Session timeout" value={config.session_timeout_seconds}
          min={15} max={7200} suffix="seconds"
          hint="A tab that misses this many seconds of heartbeats counts as gone (kept at least twice the heartbeat)."
          onCommit={(n) => void patch({ session_timeout_seconds: n })} />
        <NumberField label="Idle countdown" value={config.idle_grace_seconds}
          min={0} max={86400} suffix="seconds"
          hint="How long the app keeps running after the last browser session ends."
          onCommit={(n) => void patch({ idle_grace_seconds: n })} />
        <NumberField label="Maximum wait for running work"
          value={config.max_shutdown_wait_seconds} min={0} max={604800}
          suffix="seconds"
          hint="The limit on how long a stop may wait for jobs to reach a safe point."
          onCommit={(n) => void patch({ max_shutdown_wait_seconds: n })} />
      </div>
      <p className="faint small" style={{ marginBottom: 0 }}>
        Stored in this installation's config as <code>[lifecycle]</code>. The
        first three apply within a few seconds; the automatic start needs a
        restart. Measured now: {live?.stops ?? 0} idle stop(s) and{" "}
        {live?.cancels ?? 0} cancelled by somebody coming back.
      </p>
    </Section>
  );
}

/* ================================================================== page */

export default function SettingsPage() {
  const toast = useToast();
  const [settings, setSettings] = useState<Settings | null>(null);
  const [access, setAccess] = useState<AccessState | null>(null);
  const [auth, setAuth] = useState<AuthStatus | null>(null);
  const [accessFailed, setAccessFailed] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [reload, setReload] = useState(0);

  useEffect(() => {
    api<Settings>("/settings").then(setSettings).catch((e) => setError(e.message));
    authStatus().then(setAuth).catch(() => {});
    // The two switches are polled only while Settings is open, so the state on
    // screen follows the real tunnel rather than a one-off reading. A failure is
    // remembered: an application process started before this feature cannot
    // answer at all, and two silent skeletons would hide that from the user.
    const load = () => accessState()
      .then((value) => { setAccess(value); setAccessFailed(false); })
      .catch(() => setAccessFailed(true));
    load();
    const id = setInterval(load, 10_000);
    return () => clearInterval(id);
  }, [reload]);
  const accessUnavailable = accessFailed && access === null;

  async function update(payload: Partial<Settings> & { classroom?: { write_draft_grades: boolean } }) {
    try {
      const next = await api<Settings>("/settings", { method: "POST", body: JSON.stringify(payload) });
      setSettings(next);
      toast("ok", "Settings saved");
    } catch (e) {
      setError((e as Error).message);
    }
  }

  /**
   * The stop-the-line publication gate lives here and nowhere else.
   *
   * Turning it OFF is the one switch that lets a grade, a report upload or a
   * returned submission reach Google again, so the confirmation is explicit
   * about what it means instead of reporting a bland "Settings saved".
   */
  async function setTestingOnly(only: boolean) {
    if (!settings?.testing) return;
    await update({ testing: { ...settings.testing, only } });
    toast(only ? "warn" : "info", only
      ? "Testing only mode is ON — nothing will be sent to Google Classroom or Drive"
      : "Testing only mode is OFF — approvals can now publish grades and upload reports");
  }

  if (!settings) {
    return (
      <>
        <PageHeader kicker="System · Settings" title="Settings" icon="settings"
          sub="Local configuration — changes apply immediately." />
        <div className="skeleton" style={{ height: 220 }} />
      </>
    );
  }

  /**
   * The publication gate's state must never be GUESSED, and it must never be
   * read blind.
   *
   * A running copy that predates this field cannot answer for it. Reading
   * ``settings.testing.only`` off such a payload threw during render and took
   * the WHOLE Settings page down with it — every card, not just this one — so
   * the teacher saw a blank page instead of a switch. An absent block is now a
   * named state, exactly like the Public Address and Multi-Account cards
   * below: the card reports that this process cannot answer, and says how to
   * get the control back.
   */
  const testing = settings.testing ?? null;
  // Same rule as ``testing`` above: an older running copy cannot answer for
  // this block, and an absent one is a named state rather than a crash.
  const lifecycle = settings.lifecycle ?? null;

  return (
    <>
      <PageHeader kicker="System · Settings" title="Settings" icon="settings"
        sub="Everything this installation needs, grouped by what it affects.">
        {/* §7: connection + synchronization are configured on this page, so the
            shared sync action belongs here too — and it is the same button. */}
        <SyncClassroomButton onSynced={() => setReload((n) => n + 1)} />
      </PageHeader>

      {error && <div className="notice err" style={{ marginBottom: 12 }}>{error}</div>}

      <nav className="settings-jump" aria-label="Jump to a settings section">
        {[
          ["access", "Access & accounts"], ["classroom", "Google Classroom"],
          ["correction", "Correction"], ["ai", "AI provider"],
          ["data", "Storage & data"], ["reports", "Reports & identity"],
        ].map(([id, label]) => (
          <a key={id} className="chip" href={`#${id}`}>{label}</a>
        ))}
      </nav>

      {/* ------------------------------------------------ access & accounts */}
      <h2 className="nav-section" id="access" style={{ marginTop: 0 }}>Access &amp; accounts</h2>

      <PublicAddressCard state={access} onChange={setAccess}
        unavailable={accessUnavailable} />

      <HostAccountCard auth={auth} onChanged={() => setReload((n) => n + 1)} />

      <MultiAccountCard caps={access?.multi_account ?? null}
        isOwner={Boolean(access?.owner)} unavailable={accessUnavailable}
        onChange={(caps) => access && setAccess({ ...access, multi_account: caps })} />

      <RemoteAccessCard />

      {/* -------------------------------------------------- google classroom */}
      <h2 className="nav-section" id="classroom">Google Classroom</h2>

      <Section title="Connection" icon="classroom">
        <Row ok={Boolean(auth?.authenticated) || !auth?.required}
          label="Application sign-in"
          detail={auth?.required
            ? (auth.authenticated ? "Signed in" : "Required")
            : "Not required on this computer"} />
        <Row ok={Boolean(settings.classroom.write_draft_grades)}
          label="Draft grade write-back"
          detail={settings.classroom.write_draft_grades ? "Enabled" : "Disabled"} />
        <label className="switch" style={{ marginTop: 8 }}>
          <input type="checkbox" checked={settings.classroom.write_draft_grades}
            onChange={(e) => update({ classroom: { write_draft_grades: e.target.checked } })} />
          <span className="track" />
          <span className="muted">Attempt to write draft grades back to Classroom</span>
        </label>
        <p className="faint small" style={{ marginBottom: 0 }}>
          Google only allows an app to grade coursework it created itself. When
          refused, the app shows suggested grades for one-click manual entry
          instead. Connect, choose assignments and publish from
          <a href="#/ai"> AI → Classroom</a>.
        </p>
      </Section>

      {/* ------------------------------------------------- correction engine */}
      <h2 className="nav-section" id="correction">Correction preferences</h2>

      <div className="grid grid-2" style={{ alignItems: "start" }}>
        <Section title="Background workers" icon="cpu">
          <label className="field">
            <span>Concurrent corrections ({settings.worker.concurrency})</span>
            <input type="range" min={1} max={4} value={settings.worker.concurrency}
              onChange={(e) => update({ worker: { ...settings.worker, concurrency: Number(e.target.value) } })} />
          </label>
          <p className="faint small" style={{ marginBottom: 0 }}>
            More workers mark submissions in parallel but consume Gemini quota faster.
          </p>
        </Section>

        <Section title="Student file retention" icon="file">
          <label className="field">
            <span>Keep original submissions</span>
            <select value={settings.retention.keep_submissions}
              onChange={(e) => update({ retention: {
                ...settings.retention,
                keep_submissions: e.target.value as typeof settings.retention.keep_submissions,
              } })}>
              <option value="never">Never keep (delete after marking)</option>
              <option value="until_complete">Only while the correction runs (deleted when it finishes)</option>
              <option value="days">Keep for a number of days</option>
              <option value="always">Always keep</option>
            </select>
          </label>
          <p className="faint small" style={{ marginBottom: 0 }}>
            Applies to every paper this device marks — ones downloaded from
            Classroom and ones you upload — for both correction engines. A
            kept paper stays viewable from its result.
          </p>
          {settings.retention.keep_submissions === "days" && (
            <label className="field">
              <span>Days to keep</span>
              <input type="number" min={0} defaultValue={settings.retention.keep_days}
                onBlur={(e) => update({ retention: {
                  ...settings.retention, keep_days: Number(e.target.value) } })} />
            </label>
          )}
          <p className="faint small" style={{ marginBottom: 0 }}>
            Temporary processing copies are always cleaned up automatically; a
            kept paper is stored separately from them.
          </p>
        </Section>

        <Section title="Grade boundaries" icon="chart"
          hint="1–9 scale, looked up — never invented">
          <label className="field">
            <span>Session label (optional)</span>
            <input defaultValue={settings.grades.session_label}
              placeholder="e.g. June 2026"
              onBlur={(e) => update({ grades: {
                ...settings.grades, session_label: e.target.value } })} />
          </label>
          <div className="row wrap" style={{ gap: 6 }}>
            {settings.grades.boundaries.map(([g, m], i) => (
              <span key={`${g}-${i}`} className="badge mute">{g}: {m}%</span>
            ))}
          </div>
        </Section>

        <Section title="Offline demo (mock mode)" icon="activity">
          <label className="switch">
            <input type="checkbox" checked={settings.mock_mode}
              onChange={(e) => update({ mock_mode: e.target.checked })} />
            <span className="track" />
            <span className="muted">Simulate AI responses without network access</span>
          </label>
          <p className="faint small" style={{ marginBottom: 0 }}>
            Useful for exploring the app and for smoke tests. Results are clearly
            labelled mock data.
          </p>
        </Section>
      </div>

      <Section title="Testing only mode" icon="lock"
        hint={testing
          ? (testing.only ? "ON — nothing is published" : "off")
          : "unavailable in this running copy"}>
        {!testing ? (
          <div className="notice warn small" style={{ marginBottom: 0 }}>
            <Icon name="info" size={15} />
            <span>
              This Math Checker process was started before this control existed,
              so it cannot report or change Testing only mode — an older copy is
              still running. Close it (Stop.bat, or Ctrl+C in its window) and
              start Math Checker again; no reinstall and no re-configuration is
              needed, and nothing here was lost.
            </span>
          </div>
        ) : (
          <>
            <label className="switch">
              <input type="checkbox" checked={testing.only}
                onChange={(e) => void setTestingOnly(e.target.checked)} />
              <span className="track" />
              <span className="muted">Block every grade write, report upload and return</span>
            </label>
            {testing.only ? (
              <div className="notice warn" style={{ marginTop: 8 }}>
                <span>
                  <strong>TESTING ONLY · results stay on this machine.</strong>{" "}
                  No grade is written, no report is uploaded or attached and no work
                  is returned to Google Classroom or Drive — an approval that needs
                  to publish fails and says so, and nothing is sent. Reading courses,
                  submissions and mark schemes still works, so marking can still be
                  validated against real work. Switch this off to publish again.
                </span>
              </div>
            ) : (
              <p className="faint small" style={{ marginBottom: 0 }}>
                Off — approving a report can write the draft grade, upload the report
                and return the work to Google Classroom.
              </p>
            )}
            <p className="faint small" style={{ marginBottom: 0, marginTop: 8 }}>
              This is the same gate the corrector, the automation worker and every
              Google Classroom/Drive call consult, so a retry or a scheduled job
              cannot bypass it.
            </p>
          </>
        )}
      </Section>

      <ServerLifecycleCard config={lifecycle} missing={!lifecycle}
        onUpdate={update} />

      {/* ------------------------------------------------------- ai provider */}
      <h2 className="nav-section" id="ai">AI provider</h2>

      <Section title="Gemini model chain" icon="ai"
        hint="preserved from the original engine">
        <div className="row wrap">
          {settings.gemini.primary_models.map((m, i) => (
            <span key={m} className="badge info">{i + 1}. {m}</span>
          ))}
        </div>
        <p className="faint small" style={{ marginBottom: 0 }}>
          The engine falls through this chain automatically when a model is busy,
          benched, or unavailable — identical to the proven legacy behavior.
          Keys are added and tested on <a href="#/ai">AI → Models &amp; Keys</a>;
          they are never part of an export.
        </p>
      </Section>

      {/* ------------------------------------------------------- storage/data */}
      <h2 className="nav-section" id="data">Storage &amp; data portability</h2>

      <Section title="Data portability" icon="database"
        hint="move records between computers">
        <p className="small" style={{ marginTop: 0 }}>
          Exporting your students and correction results — and importing them on
          another computer — lives on the Storage page, next to the counts and
          the stored correction list it acts on.
        </p>
        <div className="row wrap" style={{ gap: 8 }}>
          <a className="btn primary sm" href="#/storage">
            <Icon name="database" size={14} /> Open Storage &amp; export/import
          </a>
          <a className="btn secondary sm" href="/api/backup/database" download>
            Download database backup
          </a>
          <a className="btn secondary sm" href="/api/backup/config" download>
            Export configuration
          </a>
        </div>
        <p className="faint small" style={{ marginBottom: 0, marginTop: 8 }}>
          The database backup is the whole SQLite file (complete but
          machine-specific). The Storage page's export is the versioned,
          secrets-free package meant for moving to another version or computer.
        </p>
      </Section>

      <Section title="Network access" icon="wifi">
        <label className="field">
          <span>Who can reach the app on this network?</span>
          <select value={settings.network.access_mode}
            onChange={(e) => update({ network: {
              ...settings.network,
              access_mode: e.target.value as typeof settings.network.access_mode,
            } })}>
            <option value="local">This computer only (127.0.0.1)</option>
            <option value="lan">Other devices on my network</option>
          </select>
        </label>
        <p className="faint small" style={{ marginBottom: 0 }}>
          Restart the app for a change to take effect. Reaching the app from
          outside this network is the <strong>Public Address</strong> switch at
          the top of this page, which starts a verified HTTPS endpoint — the
          only supported way to expose it.
        </p>
      </Section>

      {/* ------------------------------------------------ identity & reports */}
      <h2 className="nav-section" id="reports">Reports &amp; identity</h2>

      <div className="grid grid-2" style={{ alignItems: "start" }}>
        <Section title="Teacher &amp; school" icon="user">
          <label className="field">
            <span>Teacher name</span>
            <input defaultValue={settings.teacher.teacher_name}
              onBlur={(e) => update({ teacher: { ...settings.teacher, teacher_name: e.target.value } })} />
          </label>
          <label className="field">
            <span>School / institution</span>
            <input defaultValue={settings.teacher.school_name}
              onBlur={(e) => update({ teacher: { ...settings.teacher, school_name: e.target.value } })} />
          </label>
          <p className="faint small" style={{ marginBottom: 0 }}>
            Appears on reports. Never uploaded anywhere.
          </p>
        </Section>

        <Section title="Report branding" icon="reports"
          hint="permanent letterhead">
          <label className="field">
            <span>Title under the name</span>
            <input defaultValue={settings.reports.teacher_title}
              onBlur={(e) => update({ reports: { ...settings.reports, teacher_title: e.target.value } })} />
          </label>
          <div className="grid grid-2" style={{ gap: "var(--s3)", marginBottom: "var(--s3)" }}>
            <label className="field" style={{ marginBottom: 0 }}>
              <span>Centre</span>
              <input defaultValue={settings.reports.center_name}
                onBlur={(e) => update({ reports: { ...settings.reports, center_name: e.target.value } })} />
            </label>
            <label className="field" style={{ marginBottom: 0 }}>
              <span>Academic year</span>
              <input defaultValue={settings.reports.academic_year}
                onBlur={(e) => update({ reports: { ...settings.reports, academic_year: e.target.value } })} />
            </label>
          </div>
          <label className="field">
            <span>Tagline</span>
            <input defaultValue={settings.reports.quote}
              onBlur={(e) => update({ reports: { ...settings.reports, quote: e.target.value } })} />
          </label>
          <label className="field">
            <span>Monogram (logo letter)</span>
            <input defaultValue={settings.reports.monogram} maxLength={2}
              onBlur={(e) => update({ reports: { ...settings.reports, monogram: e.target.value } })} />
          </label>
          <p className="faint small" style={{ marginBottom: 0 }}>
            This is the identity printed on every exported PDF. The teacher
            name above and Google Classroom sign-in never change it.
          </p>
        </Section>
      </div>

      <Section title="Role" icon="shield" hint="read-only">
        <p className="small" style={{ marginTop: 0 }}>
          You are signed in as{" "}
          <strong>{settings.role === "developer" ? "Developer"
            : settings.role === "coteacher" ? "Co-teacher" : "Teacher"}</strong>.
        </p>
        <p className="faint small" style={{ marginBottom: 0 }}>
          Roles are assigned by the account and permission system — they are
          deliberately not changeable here, so a role can never be granted to
          yourself from Settings. The role you sign in with decides which pages,
          dashboards and actions you can open; Developer unlocks Diagnostics.
          Secrets are never displayed at any role. While single-account mode is
          active the installation runs as the host account you set up above.
        </p>
      </Section>

      <p className="faint small" style={{ textAlign: "center" }}>
        Math Checker v{settings.version}
      </p>
    </>
  );
}
