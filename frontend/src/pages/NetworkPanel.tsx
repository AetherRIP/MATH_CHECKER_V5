// AI → Network configuration.
//
// Two DIFFERENT things, shown side by side and never merged:
//
//   * the BACKEND the user interface is talking to — which is this
//     application's own local server unless a separate backend was configured
//     externally. It is measured (a real request, with its real latency) rather
//     than assumed, and it always works while the app is open.
//
//   * the PUBLIC ADDRESS — the optional, app-managed https:// endpoint. It has
//     a saved switch AND a measured state; the URL is shown only while a real
//     verification stands, and turning it off never disables local operation.
//
// Access mode (this computer only / other devices on my network) is saved to
// the same configuration the launcher reads, so it survives a restart.

import { useEffect, useState } from "react";
import {
  accessState, api, authStatus, recheckPublicAddress, setPublicAddress,
  type AccessMode, type AccessState, type Settings,
} from "../api";
import { Badge, Button, Icon, Section, useToast } from "../components";

type Probe = { ok: boolean; status: number | null; ms: number | null; at: number };

export default function NetworkPanel({ onNavigate }: {
  onNavigate: (hash: string) => void;
}) {
  const toast = useToast();
  const [settings, setSettings] = useState<Settings | null>(null);
  // The address this server actually answers on. It comes from the backend's
  // own local_url (built from the live port), never from the configured port
  // alone — a copy started on 8660 must not be advertised as 8642.
  const [localUrl, setLocalUrl] = useState<string | null>(null);
  const [access, setAccess] = useState<AccessState | null>(null);
  const [accessFailed, setAccessFailed] = useState(false);
  const [probe, setProbe] = useState<Probe | null>(null);
  const [checking, setChecking] = useState(false);
  const [busy, setBusy] = useState(false);

  // A real request against the backend the UI is using right now.
  async function check() {
    setChecking(true);
    const started = performance.now();
    try {
      const response = await fetch("/api/health", { cache: "no-store" });
      setProbe({ ok: response.ok, status: response.status,
                 ms: Math.round(performance.now() - started), at: Date.now() });
    } catch {
      setProbe({ ok: false, status: null,
                 ms: Math.round(performance.now() - started), at: Date.now() });
    } finally {
      setChecking(false);
    }
  }

  useEffect(() => {
    api<Settings>("/settings").then(setSettings).catch(() => {});
    authStatus().then((s) => setLocalUrl(s.local_url || null)).catch(() => {});
    check();
    const load = () => accessState()
      .then((value) => { setAccess(value); setAccessFailed(false); })
      .catch(() => setAccessFailed(true));
    load();
    const id = setInterval(load, 10_000);
    return () => clearInterval(id);
  }, []);

  async function saveMode(mode: AccessMode) {
    try {
      const next = await api<Settings>("/settings", {
        method: "POST", body: JSON.stringify({ network: { ...settings?.network, access_mode: mode } }) });
      setSettings(next);
      toast("ok", mode === "lan"
        ? "Saved — other devices on this network can reach the app after a restart."
        : "Saved — this computer only.");
    } catch (e) { toast("err", (e as Error).message); }
  }

  async function togglePublic(enabled: boolean) {
    setBusy(true);
    try {
      const next = await setPublicAddress(enabled);
      setAccess((prev) => prev && { ...prev, public_address: next.public_address,
                                    multi_account: next.multi_account ?? prev.multi_account });
      if (!enabled) toast("ok", "Public Address is off — the local app is unaffected.");
      else if (next.public_address.status === "online") {
        toast("ok", `Verified at ${next.public_address.public_url}`);
      } else {
        toast("err", next.public_address.reason ?? "The public address did not answer.");
      }
    } catch (e) { toast("err", (e as Error).message); }
    finally { setBusy(false); }
  }

  async function recheck() {
    setBusy(true);
    try {
      const next = await recheckPublicAddress();
      setAccess((prev) => prev && { ...prev, public_address: next.public_address });
      toast(next.public_address.status === "online" ? "ok" : "err",
        next.public_address.status === "online"
          ? `Verified at ${next.public_address.public_url}`
          : (next.public_address.reason ?? "The public address did not answer."));
    } catch (e) { toast("err", (e as Error).message); }
    finally { setBusy(false); }
  }

  const pa = access?.public_address ?? null;
  const address = localUrl ?? (settings ? `http://127.0.0.1:${settings.network.port}` : null);
  const port = Number(address?.split(":").pop()) || settings?.network.port || pa?.port || null;
  const mode: AccessMode = settings?.network.access_mode ?? "local";
  const accessUnavailable = accessFailed && access === null;

  return (
    <>
      <Section title="Backend connection" icon="wifi"
        hint="the server this window talks to — local to this computer unless a separate backend was configured">
        <div className="row between wrap" style={{ gap: 10, marginBottom: 10 }}>
          <div className="row wrap" style={{ gap: 8 }}>
            <Badge tone={probe?.ok ? "ok" : probe ? "err" : "mute"}>
              {probe === null ? "Checking…" : probe.ok ? "Connected" : "Not answering"}
            </Badge>
            {port !== null && <Badge tone="mute">port {port}</Badge>}
            {mode === "lan" && <Badge tone="warn">other devices allowed</Badge>}
          </div>
          <Button size="sm" variant="secondary" icon="refresh"
            loading={checking} onClick={check}>Check connection</Button>
        </div>

        <div className="hint small">
          Address: <code>{address ?? "—"}</code>
          {probe && (
            <>
              {" · "}
              {probe.ok
                ? `answered HTTP ${probe.status} in ${probe.ms} ms`
                : `no answer${probe.status ? ` (HTTP ${probe.status})` : ""}`}
              {" · "}
              checked {new Date(probe.at).toLocaleTimeString()}
            </>
          )}
        </div>

        {!probe?.ok && probe !== null && (
          <div className="notice err small" style={{ marginTop: 10 }}>
            <Icon name="alert" size={14} /> The interface cannot reach its
            backend, so nothing can be corrected until it answers.
          </div>
        )}

        <label className="field" style={{ maxWidth: 420, marginTop: 12 }}>
          <span>Who can reach the app on this network?</span>
          <select className="input" value={mode}
            onChange={(e) => saveMode(e.target.value as AccessMode)}>
            <option value="local">This computer only</option>
            <option value="lan">Other devices on my network</option>
          </select>
        </label>
        <p className="faint small" style={{ marginBottom: 0 }}>
          The access mode is applied by the launcher on the next start
          {mode === "lan"
            ? ". Anyone on your network who can reach the port still has to sign in."
            : "."}
        </p>
      </Section>

      <Section title="Public Address" icon="link"
        hint="an optional https:// address for this installation — separate from the backend above">
        {accessUnavailable ? (
          <div className="notice warn small">
            <Icon name="info" size={14} /> This running copy does not expose the
            access switches (it was started before them). Restart the app to
            manage Public Address here.
          </div>
        ) : !pa ? (
          <div className="skeleton" style={{ height: 90 }} />
        ) : (
          <>
            <div className="row between wrap" style={{ gap: 10, marginBottom: 10 }}>
              <div className="row wrap" style={{ gap: 8 }}>
                <Badge tone={pa.status === "online" ? "ok"
                  : pa.status === "starting" ? "warn"
                  : pa.status === "failed" ? "err" : "mute"}>
                  {pa.status === "online" ? "Public and verified"
                    : pa.status === "starting" ? "Starting…"
                    : pa.status === "failed" ? "Failed" : "Off"}
                </Badge>
                {pa.tunnel_running && <Badge tone="info">tunnel running</Badge>}
                {!pa.cloudflared_available && <Badge tone="mute">helper missing</Badge>}
              </div>
              <div className="row wrap" style={{ gap: 8 }}>
                <Button size="sm" variant="ghost" icon="refresh"
                  loading={busy && pa.requested} onClick={recheck}
                  disabled={busy || accessUnavailable}>Re-check</Button>
                <Button size="sm" variant={pa.requested ? "ghost" : "secondary"}
                  icon={pa.requested ? "pause" : "play"} loading={busy}
                  disabled={busy || accessUnavailable || (!pa.requested && !pa.can_enable)}
                  onClick={() => togglePublic(!pa.requested)}>
                  {pa.requested ? "Turn off" : "Turn on"}
                </Button>
              </div>
            </div>

            {pa.status === "online" && pa.public_url ? (
              <div className="hint small">
                Address: <code>{pa.public_url}</code>
                {pa.last_verified ? ` · verified ${new Date(pa.last_verified * 1000).toLocaleString()}` : ""}
              </div>
            ) : (
              <div className="hint small">
                {pa.requested
                  ? "Switched on, but no address is advertised until it is verified from outside."
                  : pa.last_address
                    ? <>Last verified address (inactive): <code>{pa.last_address}</code></>
                    : "Off. The app is reachable on this computer only."}
              </div>
            )}

            {pa.reason && pa.status !== "online" && (
              <div className={`notice ${pa.status === "failed" ? "err" : "warn"} small`} style={{ marginTop: 10 }}>
                <Icon name="info" size={14} /> {pa.reason}
              </div>
            )}
            {pa.blocked_reason && (
              <div className="notice warn small" style={{ marginTop: 10 }}>
                <Icon name="lock" size={14} /> {pa.blocked_reason}
              </div>
            )}

            <p className="faint small" style={{ marginTop: 10, marginBottom: 0 }}>
              Turning Public Address off stops only the tunnel: local correction,
              reports, Classroom sync and the backend above keep working, and the
              setting stays saved for next time.
            </p>
          </>
        )}

        <Button size="sm" variant="ghost" icon="settings"
          onClick={() => onNavigate("#/settings")}>
          Access & accounts, host password and multi-account mode
        </Button>
      </Section>
    </>
  );
}
