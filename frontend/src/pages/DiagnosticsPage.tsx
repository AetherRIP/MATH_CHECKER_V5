import { useCallback, useEffect, useState } from "react";
import { api, timeAgo, type LogCategory, type LogFeed } from "../api";
import { Badge, Empty, KV, Loading, PageHeader, Segmented, Stat, useToast } from "../components";

interface Diagnostics {
  system: {
    app_version: string; python_version: string; platform: string;
    database: string; jobs_total: number; mock_mode: boolean;
  };
  gemini: {
    total: number; healthy: number; cooldown: number; invalid: number;
    has_keys: boolean; aggregate: Record<string, number>;
    keys: { fingerprint: string; status: string; requests: number; last_used: number | null }[];
  };
  classroom: {
    connected: boolean; client_configured: boolean; problem: string | null;
    last_sync: number | null; courses: number; coursework: number; submissions: number;
  };
  jobs: Record<string, number>;
  storage: { db_bytes: number; tmp_bytes: number; mark_scheme_bytes: number; logs_bytes: number };
}

function kb(bytes: number) {
  if (bytes > 1024 * 1024) return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
  return `${Math.max(1, Math.round(bytes / 1024))} KB`;
}

type Tab = "system" | "gemini" | "classroom" | "database" | "jobs" | "logs" | "storage";

const TABS: { value: Tab; label: string }[] = [
  { value: "system", label: "System" },
  { value: "gemini", label: "Gemini" },
  { value: "classroom", label: "Classroom" },
  { value: "database", label: "Database" },
  { value: "jobs", label: "Jobs" },
  { value: "logs", label: "Logs" },
  { value: "storage", label: "Storage" },
];

/* ---------------------------------------------------------- log view ---- */

const LOG_FILTERS: { key: LogCategory | "all"; label: string }[] = [
  { key: "all", label: "All" },
  { key: "running", label: "Running" },
  { key: "completed", label: "Completed" },
  { key: "failed", label: "Failed" },
  { key: "cancelled", label: "Cancelled" },
  { key: "warning", label: "Warnings" },
  { key: "system", label: "System" },
];

const LEVEL_TONE: Record<string, "ok" | "err" | "warn" | "info" | "mute"> = {
  ERROR: "err", CRITICAL: "err", WARNING: "warn", INFO: "info", DEBUG: "mute",
};

function LogsPanel() {
  const toast = useToast();
  const [feed, setFeed] = useState<LogFeed | null>(null);
  const [category, setCategory] = useState<LogCategory | "all">("all");
  const [confirmClear, setConfirmClear] = useState(false);
  const [clearing, setClearing] = useState(false);

  const load = useCallback(() => {
    api<LogFeed>(`/diagnostics/logs?tail=300&category=${category}`)
      .then(setFeed)
      .catch(() => {});
  }, [category]);

  useEffect(() => {
    load();
    const t = setInterval(load, 8000);
    return () => clearInterval(t);
  }, [load]);

  async function clearStoredLog() {
    setClearing(true);
    try {
      const r = await api<{ bytes_removed: number; note: string }>(
        "/diagnostics/logs/clear",
        { method: "POST", body: JSON.stringify({ confirm: true }) });
      toast("ok", `Stored log cleared (${r.bytes_removed} bytes) — database records untouched`);
      setConfirmClear(false);
      load();
    } catch (e) {
      toast("err", (e as Error).message);
    } finally {
      setClearing(false);
    }
  }

  const allCounts = feed?.counts ?? {};
  const totalCount = Object.values(allCounts).reduce((a, b) => a + (b ?? 0), 0);

  return (
    <div className="card">
      <div className="row between wrap" style={{ marginBottom: 10 }}>
        <h3 style={{ margin: 0 }}>
          Logs
          <span className="hint">{totalCount} entries · secrets auto-redacted</span>
        </h3>
        <div className="row wrap">
          <button className="btn sm ghost" onClick={load}>Refresh</button>
          <button className="btn sm" style={{ color: "var(--warn)" }}
            onClick={() => setConfirmClear(true)}>
            Clear Log
          </button>
        </div>
      </div>

      <div className="chip-row" style={{ marginBottom: 12 }}>
        {LOG_FILTERS.map((f) => {
          const count = f.key === "all" ? totalCount : (allCounts[f.key] ?? 0);
          if (count === 0 && f.key !== "all" && category !== f.key) return null;
          return (
            <button key={f.key}
              className={`chip ${category === f.key ? "active" : ""}`}
              onClick={() => setCategory(f.key)}>
              {f.label}<span className="chip-count">{count}</span>
            </button>
          );
        })}
      </div>

      {!feed ? <Loading label="Loading logs…" /> : feed.entries.length === 0 ? (
        <Empty icon="🧹" title="No log entries here"
          hint="Nothing matches this category — or the log was cleared." />
      ) : (
        <div className="log-view" style={{ maxHeight: "55vh", overflowY: "auto" }}>
          {feed.entries.map((e, i) => (
            <div key={i} className="row" style={{ gap: 8, alignItems: "baseline", padding: "1px 0" }}>
              <span className="faint mono" style={{ minWidth: 138, fontSize: 11 }}>
                {e.ts ? String(e.ts).replace("T", " ").slice(0, 19) : "—"}
              </span>
              <Badge tone={LEVEL_TONE[e.level.toUpperCase()] ?? "mute"}>{e.level}</Badge>
              <span className="faint small" style={{ minWidth: 110 }}>{e.component}</span>
              <span className="small">{e.event}</span>
            </div>
          ))}
        </div>
      )}

      {confirmClear && (
        <div className="notice warn" style={{ marginTop: 12 }}>
          <strong>Clear the stored log file?</strong>
          <div className="small" style={{ margin: "6px 0" }}>
            This discards the log file history. Student data, correction results,
            Classroom data, assignments and job history are NOT affected — they
            live in the database and are never touched by this action.
          </div>
          <div className="row wrap">
            <button className="btn sm" disabled={clearing} onClick={clearStoredLog}>
              {clearing ? "Clearing…" : "Yes, clear the log file"}
            </button>
            <button className="btn sm ghost" onClick={() => setConfirmClear(false)}>
              Cancel
            </button>
          </div>
        </div>
      )}
    </div>
  );
}

export default function DiagnosticsPage() {
  const [data, setData] = useState<Diagnostics | null>(null);
  const [dbTables, setDbTables] = useState<Record<string, number> | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [tab, setTab] = useState<Tab>("system");

  useEffect(() => {
    api<Diagnostics>("/diagnostics").then(setData).catch((e) => setError(e.message));
    api<{ tables: Record<string, number> }>("/diagnostics/db").then((r) => setDbTables(r.tables)).catch(() => {});
    const timer = setInterval(() => {
      api<Diagnostics>("/diagnostics").then(setData).catch(() => {});
    }, 10_000);
    return () => clearInterval(timer);
  }, []);

  if (error) {
    return (
      <>
        <PageHeader title="Diagnostics" sub="Developer-only technical view." />
        <div className="notice err">{error}</div>
        <p className="page-sub" style={{ marginTop: "var(--s4)" }}>
          Request logs, worker timings and model health appear here once this
          installation is signed in with the Developer role.
        </p>
      </>
    );
  }
  if (!data) {
    return (
      <>
        <PageHeader title="Diagnostics" sub="Developer-only technical view." />
        <Loading label="Loading diagnostics…" />
      </>
    );
  }

  return (
    <>
      <PageHeader title="Diagnostics" sub="System, Gemini, Classroom, jobs, storage and logs.">
        <Badge tone={data.system.mock_mode ? "warn" : "ok"}>
          {data.system.mock_mode ? "Mock mode" : "Live"}
        </Badge>
      </PageHeader>

      <div style={{ marginBottom: 14 }}>
        <Segmented<Tab> value={tab} options={TABS} onChange={setTab} ariaLabel="Diagnostics sections" />
      </div>

      {tab === "system" && (
        <div className="stack">
          <div className="grid grid-4">
            <Stat label="App version" value={data.system.app_version} />
            <Stat label="Python" value={data.system.python_version} />
            <Stat label="Jobs total" value={data.system.jobs_total} />
            <Stat label="DB size" value={kb(data.storage.db_bytes)} />
          </div>
          <div className="card">
            <h3>Platform</h3>
            <KV k="OS" v={data.system.platform} />
            <KV k="Database path" v={<span className="mono">{data.system.database}</span>} />
            <KV k="Mock mode" v={data.system.mock_mode ? "on" : "off"} />
          </div>
        </div>
      )}

      {tab === "gemini" && (
        <div className="stack">
          <div className="grid grid-4">
            <Stat label="Total keys" value={data.gemini.total} />
            <Stat label="Healthy" value={data.gemini.healthy}
              tone={data.gemini.healthy > 0 ? "ok" : "err"} />
            <Stat label="Cooldown" value={data.gemini.cooldown} />
            <Stat label="Invalid" value={data.gemini.invalid}
              tone={data.gemini.invalid > 0 ? "err" : undefined} />
          </div>
          <div className="card">
            <h3>Aggregate</h3>
            {!data.gemini.has_keys ? <Empty icon="🔑" title="No keys configured" /> : (
              <>
                <KV k="Requests" v={data.gemini.aggregate.requests ?? 0} />
                <KV k="Rate-limit events" v={data.gemini.aggregate.rate_limit_events ?? 0} />
                <KV k="Timeouts" v={data.gemini.aggregate.timeout_errors ?? 0} />
              </>
            )}
          </div>
        </div>
      )}

      {tab === "classroom" && (
        <div className="card">
          <h3>Classroom</h3>
          <div className="row between" style={{ marginBottom: 8 }}>
            <span className="muted">Auth</span>
            {data.classroom.connected
              ? <Badge tone="ok">Connected</Badge>
              : <Badge tone="err">{data.classroom.client_configured ? "Not connected" : "Not configured"}</Badge>}
          </div>
          {data.classroom.problem && (
            <div className="notice warn small" style={{ marginBottom: 8 }}>{data.classroom.problem}</div>
          )}
          <KV k="Last sync" v={timeAgo(data.classroom.last_sync)} />
          <KV k="Courses cached" v={data.classroom.courses} />
          <KV k="Assignments cached" v={data.classroom.coursework} />
          <KV k="Submissions cached" v={data.classroom.submissions} />
        </div>
      )}

      {tab === "database" && (
        <div className="card">
          <h3>Database tables</h3>
          {dbTables ? (
            <div className="chip-row">
              {Object.entries(dbTables).map(([table, count]) => (
                <span key={table} className="badge mute">{table}: {count}</span>
              ))}
            </div>
          ) : (
            <Loading label="Loading table counts…" />
          )}
        </div>
      )}

      {tab === "jobs" && (
        <div className="card">
          <h3>Jobs by status</h3>
          {Object.keys(data.jobs).length === 0
            ? <Empty icon="⏱" title="No jobs recorded" />
            : Object.entries(data.jobs).map(([status, count]) => (
                <KV key={status} k={status.replace(/_/g, " ")} v={count} />
              ))}
        </div>
      )}

      {tab === "logs" && <LogsPanel />}

      {tab === "storage" && (
        <div className="card">
          <h3>Storage</h3>
          <KV k="Database" v={kb(data.storage.db_bytes)} />
          <KV k="Temp files" v={kb(data.storage.tmp_bytes)} />
          <KV k="Mark schemes" v={kb(data.storage.mark_scheme_bytes)} />
          <KV k="Logs" v={kb(data.storage.logs_bytes)} />
          <p className="faint small" style={{ marginBottom: 0, marginTop: 10 }}>
            Submission PDFs are deleted immediately after upload to Gemini — only
            marks, feedback and metadata are kept.
          </p>
        </div>
      )}
    </>
  );
}
