// Developer sync-monitoring dashboard (§8).
//
// Deliberately separate from the correction workflow: this view answers
// "is synchronisation healthy?" — which agents are alive, what is stuck in the
// delivery queue, which revisions conflict — and never touches marking.
//
// Every status rendered here is derived on the BACKEND from real heartbeats and
// real queue rows. Nothing is inferred from the server merely being up: an
// agent that stopped heartbeating shows as NOT REPORTING with its last-seen
// time, and an enabled-but-silent agent is never shown as running (§4/§62).
import { useCallback, useEffect, useState } from "react";
import {
  Badge, Button, DataTable, Empty, ErrorState, Loading, Section, Stat, useToast,
} from "../components";
import { timeAgo } from "../api";
import {
  retryReportDelivery, syncMonitor,
  type SyncMonitorSnapshot, type SyncRetry,
} from "./api";

type Tone = "ok" | "err" | "warn" | "info" | "mute";

/** Report-delivery state -> badge tone. Only real backend states get a colour. */
export function deliveryTone(state: string): Tone {
  switch (state) {
    case "DELIVERED": return "ok";
    case "SENDING": return "info";
    case "PENDING": return "mute";
    case "RETRY_SCHEDULED": return "warn";
    case "NEEDS_REVIEW": case "FAILED": return "err";
    default: return "mute";
  }
}

/** Presence tone, but staleness always wins: a silent agent is never "ok". */
function agentTone(agent: { stale: boolean; presence: string }): Tone {
  if (agent.stale) return "warn";
  if (agent.presence === "BUSY") return "info";
  if (agent.presence === "ERROR") return "err";
  return "ok";
}

function Counters({ snap }: { snap: SyncMonitorSnapshot }) {
  const a = snap.agents_summary;
  const r = snap.sync.reports;
  // Pending = everything still waiting to be sent (never attempted, in flight,
  // or backing off). Failed and Needs-review are reported SEPARATELY: an
  // exhausted transfer bound for a human is a different problem from one that
  // is momentarily stuck, and merging them would hide the first (§5/§6).
  const pending = r.pending + r.sending + r.retry_scheduled;
  return (
    <div className="grid grid-auto" style={{ marginBottom: 14 }}>
      <Stat label="Agents reporting" value={a.reporting}
        tone={a.reporting > 0 ? "ok" : undefined} />
      <Stat label="Silent agents" value={a.silent}
        tone={a.silent > 0 ? "warn" : undefined} />
      <Stat label="Reports pending" value={pending} />
      <Stat label="Reports delivered" value={r.delivered} tone="ok" />
      <Stat label="Reports failed" value={r.failed}
        tone={r.failed > 0 ? "err" : undefined} />
      <Stat label="Needs review" value={r.needs_review}
        tone={r.needs_review > 0 ? "warn" : undefined} />
      <Stat label="Conflicting revisions" value={snap.duplicates.length}
        tone={snap.duplicates.length > 0 ? "warn" : undefined} />
    </div>
  );
}

function VersionChips({ title, counts }: {
  title: string; counts: Record<string, number>;
}) {
  const entries = Object.entries(counts || {});
  if (entries.length === 0) return null;
  return (
    <div className="row wrap" style={{ gap: 6, alignItems: "center" }}>
      <span className="faint small">{title}</span>
      {entries.sort((x, y) => y[1] - x[1]).map(([v, n]) => (
        <Badge key={v} tone="mute">{v} × {n}</Badge>
      ))}
    </div>
  );
}

export function SyncMonitorPanel({ canView }: { canView: boolean }) {
  const toast = useToast();
  const [snap, setSnap] = useState<SyncMonitorSnapshot | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);

  const load = useCallback(() => {
    syncMonitor()
      .then((s) => { setSnap(s); setError(null); })
      .catch((e) => setError((e as Error).message));
  }, []);

  useEffect(() => {
    if (!canView) return;
    let alive = true;
    const tick = () => syncMonitor()
      .then((s) => { if (alive) { setSnap(s); setError(null); } })
      .catch((e) => { if (alive) setError((e as Error).message); });
    tick();
    // Presence is heartbeat-driven, so a 30s refresh is plenty — this view must
    // not become its own source of background chatter (§9).
    const t = setInterval(tick, 30_000);
    return () => { alive = false; clearInterval(t); };
  }, [canView]);

  async function retry(item: SyncRetry) {
    setBusy(item.result_record_id);
    try {
      await retryReportDelivery(item.result_record_id);
      toast("ok", `Report for ${item.submission_id ?? "submission"} requeued.`);
      load();
    } catch (e) {
      toast("err", (e as Error).message);
    } finally {
      setBusy(null);
    }
  }

  if (!canView) return null;
  if (error) return <ErrorState title="Sync monitor unavailable" detail={error} />;
  if (!snap) return <Loading label="Reading synchronisation state…" />;

  const { agents, retries, duplicates, recent_errors, sync_events } = snap;

  return (
    <Section title="Synchronisation monitor" icon="cloud"
      hint="developer view — delivery health, agents and diagnostics"
      actions={<Button size="sm" variant="ghost" icon="refresh" onClick={load}>
        Refresh
      </Button>}>

      <Counters snap={snap} />

      <div className="row wrap" style={{ gap: 16, marginBottom: 14 }}>
        <VersionChips title="App versions" counts={snap.agents_summary.app_versions} />
        <VersionChips title="Agent versions" counts={snap.agents_summary.agent_versions} />
        <Badge tone="mute">results synced {snap.sync.results_synced}</Badge>
        <Badge tone="mute">event seq {snap.sync.latest_event_seq}</Badge>
      </div>

      {/* ---- registered agent installations ---- */}
      <h4 style={{ marginBottom: 8 }}>Registered agent installations</h4>
      {agents.length === 0 ? (
        <Empty icon="cpu" title="No agents registered"
          hint="An installation appears here after its agent sends a heartbeat." />
      ) : (
        <DataTable minWidth={900} head={
          <tr>
            <th>Worker</th><th>Presence</th><th>Account</th>
            <th>App / Agent</th><th>Current job</th><th>Last heartbeat</th>
          </tr>
        }>
          {agents.map((a) => (
            <tr key={a.worker_id}>
              <td style={{ fontFamily: "var(--mono, monospace)", fontSize: 12 }}>
                {a.worker_id}
                {a.device_label && <div className="faint small">{a.device_label}</div>}
              </td>
              <td>
                <Badge tone={agentTone(a)}>
                  {a.stale ? "NOT REPORTING" : a.presence}
                </Badge>
                {a.stale && a.last_heartbeat && (
                  <div className="faint small">stale — no recent heartbeat</div>
                )}
              </td>
              <td>{a.account_name ?? <span className="faint">—</span>}
                {a.account_role && <div className="faint small">{a.account_role}</div>}
              </td>
              <td className="small faint">
                app {a.app_version} · agent {a.agent_version}
              </td>
              <td>{a.current_job_id
                ? <span style={{ fontFamily: "var(--mono, monospace)", fontSize: 12 }}>
                    {a.current_job_id.slice(0, 12)}…</span>
                : <span className="faint">idle</span>}</td>
              <td className="small">
                {a.last_heartbeat ? timeAgo(a.last_heartbeat) : "never"}
              </td>
            </tr>
          ))}
        </DataTable>
      )}

      {/* ---- report retry history ---- */}
      <h4 style={{ margin: "16px 0 8px" }}>Report retries</h4>
      {retries.length === 0 ? (
        <Empty icon="check-circle" title="No report transfers need attention"
          hint="Every queued report has been delivered." />
      ) : (
        <DataTable minWidth={820} head={
          <tr>
            <th>Submission</th><th>Report</th><th>State</th>
            <th className="num">Attempts</th><th>Last error</th>
            <th>Next retry</th><th style={{ width: 90 }} />
          </tr>
        }>
          {retries.map((r) => (
            <tr key={r.result_record_id}>
              <td style={{ fontFamily: "var(--mono, monospace)", fontSize: 12 }}>
                {r.submission_id ?? "—"}
              </td>
              <td className="small">{r.report_filename ?? <span className="faint">—</span>}</td>
              <td><Badge tone={deliveryTone(r.state)}>{r.state.replace(/_/g, " ")}</Badge></td>
              <td className="num">{r.attempts}/{r.max_attempts}</td>
              <td className="small faint" style={{ maxWidth: 260 }}>
                {r.last_error ? r.last_error.slice(0, 120) : "—"}
              </td>
              <td className="small">
                {r.next_retry_at ? timeAgo(r.next_retry_at) : <span className="faint">—</span>}
              </td>
              <td>
                <Button size="sm" variant="ghost" icon="sync"
                  loading={busy === r.result_record_id}
                  onClick={() => retry(r)}>Retry</Button>
              </td>
            </tr>
          ))}
        </DataTable>
      )}

      {/* ---- conflicting revisions ---- */}
      {duplicates.length > 0 && (
        <>
          <h4 style={{ margin: "16px 0 8px" }}>Conflicting revisions</h4>
          <div className="notice warn small" style={{ marginBottom: 8 }}>
            The same submission was recorded more than once. Nothing was merged
            automatically — review before trusting either revision.
          </div>
          <DataTable head={
            <tr><th>Submission</th><th className="num">Versions</th>
              <th className="num">Workers</th><th className="num">Records</th>
              <th>Last seen</th></tr>
          }>
            {duplicates.map((d) => (
              <tr key={d.submission_id}>
                <td style={{ fontFamily: "var(--mono, monospace)", fontSize: 12 }}>
                  {d.submission_id}
                </td>
                <td className="num">{d.versions}</td>
                <td className="num">{d.workers}</td>
                <td className="num">{d.records}</td>
                <td className="small">{timeAgo(d.last_seen)}</td>
              </tr>
            ))}
          </DataTable>
        </>
      )}

      {/* ---- recent delivery errors (safe diagnostics) ---- */}
      <h4 style={{ margin: "16px 0 4px" }}>Recent delivery errors</h4>
      <p className="faint small" style={{ marginTop: 0, marginBottom: 8 }}>
        Safe diagnostics — secrets and tokens are redacted before storage, so
        what appears here can be read without exposing credentials.
      </p>
      {recent_errors.length === 0 ? (
        <p className="faint small">No errors reported.</p>
      ) : (
        <DataTable minWidth={720} head={
          <tr><th>When</th><th>Type</th><th>Stage</th><th>Message</th><th>Worker</th></tr>
        }>
          {recent_errors.map((e) => (
            <tr key={e.id}>
              <td className="small">{timeAgo(e.ts)}</td>
              <td><Badge tone="err">{e.error_type ?? "unknown"}</Badge></td>
              <td className="small faint">{e.failed_stage ?? "—"}</td>
              <td className="small" style={{ maxWidth: 380 }}>{e.message ?? "—"}</td>
              <td className="small faint">
                {e.worker_id ? e.worker_id.slice(0, 12) : "—"}
              </td>
            </tr>
          ))}
        </DataTable>
      )}

      {/* ---- sync control history (agent on/off, retries) ---- */}
      <h4 style={{ margin: "16px 0 8px" }}>Synchronisation control history</h4>
      {sync_events.length === 0 ? (
        <p className="faint small">No agent or retry activity recorded yet.</p>
      ) : (
        <DataTable head={
          <tr><th>When</th><th>Action</th><th>Target</th><th>Details</th></tr>
        }>
          {sync_events.map((e, i) => (
            <tr key={`${e.ts}-${i}`}>
              <td className="small">{timeAgo(e.ts)}</td>
              <td><Badge tone="info">{e.action}</Badge></td>
              <td className="small faint">{e.target_id ?? e.target_type ?? "—"}</td>
              <td className="small faint">
                {Object.entries(e.details ?? {})
                  .map(([k, v]) => `${k}=${String(v)}`).join(", ") || "—"}
              </td>
            </tr>
          ))}
        </DataTable>
      )}

      <p className="faint small" style={{ marginTop: 12, marginBottom: 0 }}>
        Read-only monitoring of the delivery pipeline, kept separate from the
        correction workflow — nothing here changes a mark or a grade. Detailed
        control activity is on the audit trail below; fine-grained diagnostics
        stay on the accuracy-audit and diagnostics pages.
      </p>
    </Section>
  );
}
