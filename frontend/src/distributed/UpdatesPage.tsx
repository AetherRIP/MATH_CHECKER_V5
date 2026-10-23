// Application Updates (§13, §25): the Developer-only release control plane.
//
// This page MANAGES releases; it never installs one. Publishing here only makes
// a release discoverable — each installed copy decides for itself whether to
// download, verify and install it. Nothing on this page touches a mark, a
// grade or the correction workflow.
//
// Two facts are enforced, not merely displayed:
//   * only a DEVELOPER account reaches this page AND the backend re-checks the
//     role on every request, so hiding the nav item is not the security;
//   * a release can only go live through validate -> publish, with an explicit
//     confirmation, so an incomplete release cannot reach a client.
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  Badge, Button, DataTable, Empty, ErrorState, Loading, Modal, PageHeader,
  Section, Stat, useToast,
} from "../components";
import { timeAgo } from "../api";
import {
  createRelease, editRelease, listReleases, publishRelease, releasesOverview,
  rollbackRelease, storedSession, updateHistory, uploadReleasePackage,
  validateRelease, withdrawRelease,
  type ReleaseOverview, type ReleaseRow, type ReleaseValidation,
  type UpdateHistoryRow,
} from "./api";

type Tone = "ok" | "err" | "warn" | "info" | "mute";

// The distributed session's `role` is the PRIMARY role, and DEVELOPER is the
// most privileged, so a Developer+Assistant account reports "DEVELOPER" here
// (the backend re-checks the full role set on every request regardless).
const isDeveloper = (s: ReturnType<typeof storedSession>): boolean =>
  Boolean(s && s.role === "DEVELOPER");

export function releaseStatusTone(status: string): Tone {
  switch (status) {
    case "published": return "ok";
    case "draft": return "info";
    case "withdrawn": return "warn";
    default: return "mute";
  }
}

/** Installation standing -> badge tone. OFFLINE is its own state, never "ok". */
export function installTone(state: string): Tone {
  switch (state) {
    case "UP_TO_DATE": return "ok";
    case "UPDATE_AVAILABLE": return "warn";
    case "DOWNLOADING": case "INSTALLING": return "info";
    case "FAILED": return "err";
    case "OFFLINE": return "mute";
    default: return "mute";
  }
}

function bytes(n: number | null | undefined): string {
  if (n == null) return "—";
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / 1024 / 1024).toFixed(2)} MB`;
}

function base64Of(buffer: ArrayBuffer): string {
  const bytesArr = new Uint8Array(buffer);
  let binary = "";
  const chunk = 0x8000;
  for (let i = 0; i < bytesArr.length; i += chunk) {
    binary += String.fromCharCode(...bytesArr.subarray(i, i + chunk));
  }
  return window.btoa(binary);
}

/* ============================================================ page shell */

export default function UpdatesPage() {
  const session = storedSession();
  if (!session) {
    return (
      <div>
        <PageHeader kicker="System · Updates" title="Application Updates" icon="download"
          sub="Distribute releases to installed copies of the application." />
        <Section title="Sign in required" icon="shield">
          <p style={{ marginTop: 0 }}>
            Release management uses the Multi-School developer account. Sign in
            there first, then return to this page.
          </p>
          <Button variant="primary" icon="classroom"
            onClick={() => { window.location.hash = "#/distributed"; }}>
            Go to Multi-School sign-in
          </Button>
        </Section>
      </div>
    );
  }
  if (!isDeveloper(session)) {
    return (
      <div>
        <PageHeader kicker="System · Updates" title="Application Updates" icon="download"
          sub="Distribute releases to installed copies of the application." />
        <Section title="Developer access required" icon="shield">
          <p style={{ marginTop: 0 }}>
            You are signed in as <strong>{session.display_name}</strong>{" "}
            ({session.role.replace("_", "-")}). Publishing and withdrawing
            releases is restricted to the Developer role, and the backend
            rejects any attempt outside it.
          </p>
        </Section>
      </div>
    );
  }
  return <UpdatesConsole />;
}

/* ============================================================== console */

function UpdatesConsole() {
  const toast = useToast();
  const [overview, setOverview] = useState<ReleaseOverview | null>(null);
  const [releases, setReleases] = useState<ReleaseRow[] | null>(null);
  const [history, setHistory] = useState<UpdateHistoryRow[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [creating, setCreating] = useState(false);
  const [detail, setDetail] = useState<ReleaseRow | null>(null);
  const [editing, setEditing] = useState<ReleaseRow | null>(null);
  const [confirming, setConfirming] = useState<Confirm | null>(null);
  const [busyId, setBusyId] = useState<string | null>(null);

  const load = useCallback(() => {
    return Promise.all([
      releasesOverview(), listReleases(), updateHistory(),
    ]).then(([o, r, h]) => {
      setOverview(o); setReleases(r.releases); setHistory(h.history);
      setError(null);
    }).catch((e) => setError((e as Error).message));
  }, []);

  useEffect(() => {
    load();
    // Releases change rarely; a slow refresh keeps the fleet numbers honest
    // without turning this page into its own polling load (§9).
    const t = setInterval(load, 30_000);
    return () => clearInterval(t);
  }, [load]);

  const runAction = useCallback(async (id: string, fn: () => Promise<unknown>,
                                            ok: string) => {
    setBusyId(id);
    try {
      await fn();
      toast("ok", ok);
      await load();
    } catch (e) {
      toast("err", (e as Error).message);
    } finally {
      setBusyId(null);
    }
  }, [load, toast]);

  const failures = useMemo(
    () => history.filter((h) => h.result !== "UPDATED" && h.result !== "SUCCESS"),
    [history]);

  if (error) return <ErrorState title="Updates unavailable" detail={error}
                                onRetry={load} />;
  if (!overview || !releases) return <Loading label="Reading release state…" />;

  const c = overview.counts;
  const installs = overview.install_status;

  return (
    <div>
      <PageHeader kicker="System · Updates" title="Application Updates" icon="download"
        sub="Publish signed releases, watch distribution, and withdraw a bad build — without sending installers by hand."
        children={<Button size="sm" variant="ghost" icon="refresh"
          onClick={load}>Refresh</Button>} />

      {/* ---- current release overview ---- */}
      <div className="grid grid-auto" style={{ marginBottom: 14 }}>
        <Stat label="Latest published"
          value={overview.latest ? `v${overview.latest.version}` : "—"}
          tone={overview.latest ? "ok" : undefined} />
        <Stat label="Stable" value={overview.stable_version
          ? `v${overview.stable_version}` : "unavailable"} />
        <Stat label="Beta" value={overview.beta_version
          ? `v${overview.beta_version}` : "unavailable"} />
        <Stat label="Published"
          value={overview.latest?.published_at
            ? timeAgo(overview.latest.published_at) : "—"} />
        <Stat label="Channel"
          value={overview.latest?.channel ?? "—"} />
        <Stat label="Installations" value={c.registered_installations} />
        <Stat label="Up to date" value={c.up_to_date}
          tone={c.up_to_date > 0 ? "ok" : undefined} />
        <Stat label="Outdated" value={c.outdated}
          tone={c.outdated > 0 ? "warn" : undefined} />
        <Stat label="Failed installs" value={c.failed}
          tone={c.failed > 0 ? "err" : undefined} />
        <Stat label="Pending installs" value={c.pending} />
      </div>

      <div className="row wrap" style={{ gap: 8, marginBottom: 14 }}>
        <Badge tone="mute">{c.total_releases} release(s) recorded</Badge>
        <Badge tone={c.draft_releases > 0 ? "info" : "mute"}>
          {c.draft_releases} draft</Badge>
        <Badge tone={c.withdrawn_releases > 0 ? "warn" : "mute"}>
          {c.withdrawn_releases} withdrawn</Badge>
        <Badge tone="mute">{c.reporting}/{c.registered_installations} reporting</Badge>
      </div>

      {/* ---- installation status breakdown ---- */}
      <Section title="Installation status" icon="cpu"
        hint="Derived from real heartbeats. An installation that has stopped reporting shows OFFLINE — never “up to date”.">
        {installs.total === 0 ? (
          <Empty icon="cpu" title="No installations registered"
            hint="An installation appears once its agent registers and heartbeats." />
        ) : (
          <>
            <div className="row wrap" style={{ gap: 6, marginBottom: 10 }}>
              {Object.entries(installs.counts).sort().map(([state, n]) => (
                <Badge key={state} tone={installTone(state)}>
                  {state.replace(/_/g, " ")} × {n}
                </Badge>
              ))}
            </div>
            <DataTable minWidth={760} head={
              <tr><th>Installation</th><th>Status</th><th>Version</th>
                <th>Account</th><th>Last heartbeat</th></tr>
            }>
              {Object.entries(installs.installations).flatMap(([state, rows]) =>
                rows.map((row) => (
                  <tr key={row.worker_id}>
                    <td style={{ fontFamily: "var(--mono, monospace)", fontSize: 12 }}>
                      {row.worker_id}
                      {row.device_label && <div className="faint small">{row.device_label}</div>}
                    </td>
                    <td><Badge tone={installTone(state)}>
                      {state.replace(/_/g, " ")}</Badge></td>
                    <td className="small">{row.app_version ?? "unknown"}</td>
                    <td className="small">{row.account_name ?? "—"}</td>
                    <td className="small">{row.last_heartbeat
                      ? timeAgo(row.last_heartbeat) : "never"}</td>
                  </tr>
                )))}
            </DataTable>
          </>
        )}
      </Section>

      {/* ---- release history ---- */}
      <Section title="Release history" icon="file"
        hint="Append-only: a release is never silently overwritten. Withdrawing keeps the record and uninstalls nothing."
        actions={<Button size="sm" icon="plus" onClick={() => setCreating(true)}>
          New release</Button>}>
        {releases.length === 0 ? (
          <Empty icon="download" title="No releases yet"
            hint="Create a release, upload its signed package, validate, then publish."
            action={<Button icon="plus" onClick={() => setCreating(true)}>
              Create the first release</Button>} />
        ) : (
          <DataTable minWidth={980} head={
            <tr><th>Version</th><th>Channel</th><th>Platform</th><th>Status</th>
              <th>Published</th><th>Size</th><th>Notes</th>
              <th style={{ width: 240 }}>Actions</th></tr>
          }>
            {releases.map((r) => (
              <tr key={r.update_id}>
                <td style={{ fontFamily: "var(--mono, monospace)", fontSize: 12 }}>
                  v{r.target_version}
                  {!r.has_package && <Badge tone="warn">no package</Badge>}
                </td>
                <td><Badge tone="mute">{r.channel}</Badge></td>
                <td className="small">{r.platform ?? "any"}{r.arch ? `/${r.arch}` : ""}</td>
                <td><Badge tone={releaseStatusTone(r.status)}>
                  {r.status}</Badge>
                  {r.required && <Badge tone="err">required</Badge>}</td>
                <td className="small">{r.published_at
                  ? timeAgo(r.published_at) : <span className="faint">not published</span>}</td>
                <td className="small">{bytes(r.package_size)}</td>
                <td className="small" style={{ maxWidth: 220, overflow: "hidden",
                  textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                  {r.release_notes || <span className="faint">—</span>}</td>
                <td>
                  <div className="row wrap" style={{ gap: 4 }}>
                    <Button size="sm" variant="ghost" icon="eye"
                      onClick={() => setDetail(r)}>Details</Button>
                    {r.status === "draft" && (
                      <Button size="sm" variant="primary" icon="check-circle"
                        loading={busyId === r.update_id}
                        onClick={() => setConfirming({
                          kind: "publish", release: r,
                          title: `Publish v${r.target_version}?`,
                          body: "Clients will begin discovering this release once it is published.",
                          confirmLabel: "Publish release",
                        })}>Publish</Button>
                    )}
                    {r.status === "published" && (
                      <>
                        <Button size="sm" variant="ghost" icon="sync"
                          loading={busyId === r.update_id}
                          onClick={() => setConfirming({
                            kind: "rollback", release: r,
                            title: `Recommend v${r.target_version} again?`,
                            body: "Newer published releases on this channel are withdrawn so future offers fall back to this version. Installed copies are left untouched.",
                            confirmLabel: "Roll back",
                          })}>Rollback</Button>
                        <Button size="sm" variant="ghost" icon="trash"
                          loading={busyId === r.update_id}
                          onClick={() => setConfirming({
                            kind: "withdraw", release: r,
                            title: `Withdraw v${r.target_version}?`,
                            body: "Future update offers stop. Already-updated installations keep their version; nothing is uninstalled and no history is deleted.",
                            confirmLabel: "Withdraw release",
                          })}>Withdraw</Button>
                      </>
                    )}
                    <Button size="sm" variant="ghost" icon="edit"
                      onClick={() => setEditing(r)}>Notes</Button>
                  </div>
                </td>
              </tr>
            ))}
          </DataTable>
        )}
      </Section>

      {/* ---- installation & update error logs ---- */}
      <Section title="Installation & update errors" icon="alert"
        hint="Failures reported by the installing copies. A failed install keeps the previous working version.">
        {failures.length === 0 ? (
          <Empty icon="check-circle" title="No failed installs"
            hint="Genuine update failures reported by installations appear here." />
        ) : (
          <DataTable minWidth={820} head={
            <tr><th>Device</th><th>From → To</th><th>Result</th>
              <th>Reason</th><th>Rollback</th><th>When</th></tr>
          }>
            {failures.map((h) => (
              <tr key={h.id}>
                <td style={{ fontFamily: "var(--mono, monospace)", fontSize: 12 }}>
                  {h.device_id.slice(0, 12)}…</td>
                <td>{h.old_version || "?"} → {h.new_version || "?"}</td>
                <td><Badge tone={h.rollback_result ? "warn" : "err"}>
                  {h.result}</Badge></td>
                <td className="small faint" style={{ maxWidth: 260 }}>
                  {h.failure_reason || "—"}</td>
                <td className="small">{h.rollback_result ?? "—"}</td>
                <td className="small">{h.completed_at
                  ? timeAgo(h.completed_at) : "running…"}</td>
              </tr>
            ))}
          </DataTable>
        )}
      </Section>

      <p className="faint small" style={{ marginTop: 4 }}>
        Releases are Ed25519-signed. A device verifies the checksum and the
        publisher signature against a key pinned on that machine before anything
        is installed — an unsigned package is refused.
      </p>

      {creating && (
        <CreateReleaseModal onClose={() => setCreating(false)}
          onDone={async () => { setCreating(false); await load(); }} />
      )}
      {editing && (
        <EditNotesModal release={editing} onClose={() => setEditing(null)}
          onDone={async () => { setEditing(null); await load(); }} />
      )}
      {detail && <ReleaseDetailModal release={detail}
        onClose={() => setDetail(null)} />}
      {confirming && (
        <ConfirmModal spec={confirming} busy={busyId === confirming.release.update_id}
          onClose={() => setConfirming(null)}
          onConfirm={async () => {
            const { kind, release } = confirming;
            setConfirming(null);
            if (kind === "publish") {
              await runAction(release.update_id,
                () => publishRelease(release.update_id),
                `v${release.target_version} published`);
            } else if (kind === "withdraw") {
              await runAction(release.update_id,
                () => withdrawRelease(release.update_id, "withdrawn from console"),
                `v${release.target_version} withdrawn`);
            } else {
              await runAction(release.update_id,
                () => rollbackRelease(release.update_id),
                `v${release.target_version} is the recommended release again`);
            }
          }} />
      )}
    </div>
  );
}

/* ========================================================== confirm modal */

interface Confirm {
  kind: "publish" | "withdraw" | "rollback";
  release: ReleaseRow;
  title: string;
  body: string;
  confirmLabel: string;
}

function ConfirmModal({ spec, busy, onClose, onConfirm }: {
  spec: Confirm; busy: boolean; onClose: () => void; onConfirm: () => void;
}) {
  return (
    <Modal title={spec.title} icon="alert" onClose={onClose}
      footer={<>
        <Button variant="ghost" onClick={onClose}>Cancel</Button>
        <Button variant={spec.kind === "withdraw" ? "danger" : "primary"}
          loading={busy} onClick={onConfirm}>{spec.confirmLabel}</Button>
      </>}>
      <p style={{ marginTop: 0 }}>{spec.body}</p>
      <p className="faint small" style={{ marginBottom: 0 }}>
        v{spec.release.target_version} · {spec.release.channel} ·{" "}
        {spec.release.platform ?? "all platforms"}
      </p>
    </Modal>
  );
}

/* ==================================================== release detail modal */

function ReleaseDetailModal({ release, onClose }: {
  release: ReleaseRow; onClose: () => void;
}) {
  return (
    <Modal title={`Release v${release.target_version}`} icon="file" wide
      onClose={onClose}
      footer={<Button variant="secondary" onClick={onClose}>Close</Button>}>
      <DataTable head={<tr><th>Field</th><th>Value</th></tr>}>
        {([
          ["Update id", release.update_id],
          ["Component", release.target_component],
          ["Channel", release.channel],
          ["Status", release.status],
          ["Platform", `${release.platform ?? "any"}${release.arch ? `/${release.arch}` : ""}`],
          ["Minimum supported", release.minimum_supported_version ?? "—"],
          ["Priority", release.priority],
          ["Required", release.required ? "yes" : "no"],
          ["Auto-install", release.auto_install ? "yes" : "no"],
          ["Scope", release.scope?.kind ?? "ALL"],
          ["Package size", bytes(release.package_size)],
          ["SHA-256", release.package_sha256 ?? "—"],
          ["Signing key", release.signing_key_id ?? "unsigned"],
          ["Created", timeAgo(release.created_at)],
          ["Published", release.published_at ? timeAgo(release.published_at) : "—"],
          ["Withdrawn", release.withdrawn_at ? timeAgo(release.withdrawn_at) : "—"],
        ] as [string, string][]).map(([k, v]) => (
          <tr key={k}><td className="small faint">{k}</td>
            <td className="small" style={{ fontFamily: "var(--mono, monospace)",
              wordBreak: "break-all" }}>{v}</td></tr>
        ))}
      </DataTable>
      {release.release_notes && (
        <>
          <h4 style={{ marginBottom: 4 }}>Release notes</h4>
          <p className="small" style={{ whiteSpace: "pre-wrap" }}>
            {release.release_notes}</p>
        </>
      )}
    </Modal>
  );
}

/* ==================================================== edit notes modal */

function EditNotesModal({ release, onClose, onDone }: {
  release: ReleaseRow; onClose: () => void; onDone: () => void;
}) {
  const toast = useToast();
  const [notes, setNotes] = useState(release.release_notes ?? "");
  const [busy, setBusy] = useState(false);
  async function submit() {
    setBusy(true);
    try {
      await editRelease(release.update_id, { release_notes: notes });
      toast("ok", `Release notes updated for v${release.target_version}`);
      onDone();
    } catch (e) { toast("err", (e as Error).message); }
    finally { setBusy(false); }
  }
  return (
    <Modal title={`Edit v${release.target_version}`} icon="edit" onClose={onClose}
      footer={<>
        <Button variant="ghost" onClick={onClose}>Cancel</Button>
        <Button variant="primary" loading={busy} onClick={submit}>Save notes</Button>
      </>}>
      <p className="small faint" style={{ marginTop: 0 }}>
        The version, channel and package are immutable — editing them would
        silently change what clients receive.
      </p>
      <label className="small">Release notes
        <textarea rows={5} value={notes}
          onChange={(e) => setNotes(e.target.value)} /></label>
    </Modal>
  );
}

/* ==================================================== create release modal */

function CreateReleaseModal({ onClose, onDone }: {
  onClose: () => void; onDone: () => void;
}) {
  const toast = useToast();
  const fileRef = useRef<HTMLInputElement | null>(null);
  const [version, setVersion] = useState("");
  const [channel, setChannel] = useState("stable");
  const [platform, setPlatform] = useState("");
  const [component, setComponent] = useState("app");
  const [minVersion, setMinVersion] = useState("");
  const [notes, setNotes] = useState("");
  const [priority, setPriority] = useState("normal");
  const [required, setRequired] = useState(false);
  const [signature, setSignature] = useState("");
  const [keyId, setKeyId] = useState("");
  const [publicKey, setPublicKey] = useState("");
  const [release, setRelease] = useState<ReleaseRow | null>(null);
  const [validation, setValidation] = useState<ReleaseValidation | null>(null);
  const [busy, setBusy] = useState(false);

  const versionOk = /^\d+\.\d+(\.\d+)?$/.test(version.trim());
  const canValidate = versionOk && Boolean(fileRef.current?.files?.[0]
    || release?.has_package);

  async function validate() {
    setBusy(true);
    try {
      // 1. A release always starts as a DRAFT — it can never go live until it
      //    has been validated AND explicitly published.
      let target = release;
      if (!target) {
        target = await createRelease({
          target_version: version.trim(), channel, target_component: component,
          minimum_supported_version: minVersion.trim() || undefined,
          release_notes: notes.trim() || undefined, priority,
          required, platform: platform || undefined,
          package_signature: signature.trim() || undefined,
          signing_key_id: keyId.trim() || undefined,
          signing_public_key: publicKey.trim() || undefined,
        });
        setRelease(target);
      }
      // 2. Attach the package body (canonical JSON bytes) if one was chosen.
      const file = fileRef.current?.files?.[0];
      if (file) {
        const base64 = base64Of(await file.arrayBuffer());
        await uploadReleasePackage(target.update_id, base64);
      }
      // 3. Ask the backend to validate the WHOLE picture before publishing.
      const result = await validateRelease(target.update_id);
      setValidation(result);
      if (result.ok) toast("ok", "Package validated — ready to publish");
      else toast("warn", "Validation found problems");
    } catch (e) {
      toast("err", (e as Error).message);
    } finally { setBusy(false); }
  }

  async function publish() {
    if (!release) return;
    setBusy(true);
    try {
      const fresh = await validateRelease(release.update_id);
      setValidation(fresh);
      if (!fresh.ok) {
        toast("err", "Release is not valid yet"); return;
      }
      await publishRelease(release.update_id);
      toast("ok", `v${release.target_version} published`);
      onDone();
    } catch (e) { toast("err", (e as Error).message); }
    finally { setBusy(false); }
  }

  return (
    <Modal title="New release" icon="download" wide onClose={onClose}
      footer={<>
        <Button variant="ghost" onClick={onClose}>Cancel</Button>
        <Button variant="secondary" icon="shield" loading={busy} onClick={validate}>
          {release ? "Re-validate" : "Create draft & validate"}
        </Button>
        <Button variant="primary" icon="check-circle" loading={busy}
          disabled={!validation?.ok} onClick={publish}>Publish release</Button>
      </>}>
      <div style={{ display: "grid", gap: 10 }}>
        <div className="row wrap" style={{ gap: 10 }}>
          <label className="small" style={{ flex: "1 1 160px" }}>Version *
            <input value={version} placeholder="e.g. 1.9.0"
              onChange={(e) => setVersion(e.target.value)} /></label>
          <label className="small" style={{ flex: "1 1 140px" }}>Channel
            <select value={channel} onChange={(e) => setChannel(e.target.value)}>
              <option value="stable">Stable</option>
              <option value="beta">Beta</option>
            </select></label>
          <label className="small" style={{ flex: "1 1 140px" }}>Platform
            <select value={platform} onChange={(e) => setPlatform(e.target.value)}>
              <option value="">All platforms</option>
              <option value="windows">Windows</option>
              <option value="linux">Linux</option>
              <option value="darwin">macOS</option>
            </select></label>
        </div>
        <div className="row wrap" style={{ gap: 10 }}>
          <label className="small" style={{ flex: "1 1 160px" }}>Component
            <select value={component} onChange={(e) => setComponent(e.target.value)}>
              <option value="app">Math Correct app</option>
              <option value="agent">Tiny agent</option>
            </select></label>
          <label className="small" style={{ flex: "1 1 160px" }}>
            Minimum supported version
            <input value={minVersion} placeholder="optional, e.g. 1.5.0"
              onChange={(e) => setMinVersion(e.target.value)} /></label>
          <label className="small" style={{ flex: "1 1 140px" }}>Priority
            <select value={priority} onChange={(e) => setPriority(e.target.value)}>
              <option value="low">Low</option>
              <option value="normal">Normal</option>
              <option value="high">High</option>
              <option value="critical">Critical</option>
            </select></label>
        </div>
        <label className="small">Release notes
          <textarea rows={3} value={notes}
            onChange={(e) => setNotes(e.target.value)} /></label>
        <label className="small" style={{ display: "flex", gap: 8, alignItems: "center" }}>
          <input type="checkbox" checked={required}
            onChange={(e) => setRequired(e.target.checked)} />
          Required (devices must eventually update)
        </label>
        <div className="row wrap" style={{ gap: 10, alignItems: "center" }}>
          <label className="small" style={{ flex: "1 1 220px" }}>
            Release package (canonical JSON)
            <input ref={fileRef} type="file"
              accept=".json,.pkg,application/json" /></label>
        </div>
        <label className="small">Detached signature (base64)
          <input value={signature}
            placeholder="produced by scripts/sign_release.py"
            onChange={(e) => setSignature(e.target.value)} /></label>
        <div className="row wrap" style={{ gap: 10 }}>
          <label className="small" style={{ flex: "1 1 220px" }}>Signing key id
            <input value={keyId} onChange={(e) => setKeyId(e.target.value)} /></label>
          <label className="small" style={{ flex: "1 1 300px" }}>Signing public key (PEM, informational)
            <input value={publicKey} onChange={(e) => setPublicKey(e.target.value)} /></label>
        </div>
      </div>

      <p className="small faint" style={{ marginTop: 10, marginBottom: 6 }}>
        Signing happens offline: run <code>scripts/sign_release.py</code> with
        your private key to produce the signature and key id. The private key
        never leaves the Developer's machine, and a package without a valid
        signature is refused at publish time.
      </p>

      {validation && (
        <div className={`notice ${validation.ok ? "ok" : "warn"}`}>
          <strong>{validation.ok ? "Validation passed" : "Validation failed"}</strong>
          <div className="small">
            package: {validation.package.files} file(s), {bytes(validation.package.size)}
            {validation.package.target_version
              ? `, manifest v${validation.package.target_version}` : ""}
          </div>
          {validation.problems.length > 0 && (
            <ul className="small" style={{ marginBottom: 0, paddingLeft: 18 }}>
              {validation.problems.map((p) => <li key={p}>{p}</li>)}
            </ul>
          )}
        </div>
      )}
      {!canValidate && (
        <p className="small faint" style={{ marginBottom: 0 }}>
          Enter a valid version and choose a package file to validate.
        </p>
      )}
    </Modal>
  );
}
