import { useEffect, useState } from "react";
import { api, type UsageState } from "../api";
import { DataTable, PageHeader, Stat, useToast } from "../components";
import { BarChart } from "../charts/Charts";

const ERROR_CLASS_LABELS: Record<string, string> = {
  RATE_LIMIT: "Rate limit (429)",
  HIGH_DEMAND: "High demand / 503",
  TIMEOUT: "Timeout",
  NETWORK_ERROR: "Network error",
  AUTHENTICATION_ERROR: "Authentication / key",
  INVALID_REQUEST: "Invalid request",
  CONTEXT_TOO_LARGE: "Context too large",
  TRANSIENT_PROVIDER_ERROR: "Provider error",
  MODEL_ERROR: "Model error",
  PARSING_ERROR: "Bad model response",
  VALIDATION_ERROR: "Validation error",
  API_UNAVAILABLE: "No key available",
  WORKER_CRASH: "Worker crash",
  UNKNOWN_ERROR: "Unknown",
};

const CATEGORY_LABELS: Record<string, string> = {
  gemini_upload: "Gemini — file uploads (PDFs to Files API)",
  gemini_generate: "Gemini — marking requests & responses",
  gemini_health: "Gemini — key health probes",
  classroom_api: "Classroom — API calls (sync, metadata)",
  classroom_download: "Classroom — Drive downloads & exports",
  other: "Other",
};

function mb(bytes: number): string {
  return (bytes / (1024 * 1024)).toFixed(2);
}

function LimitBar({ used, cap }: { used: number; cap: number }) {
  const pct = cap > 0 ? Math.min(100, (used / cap) * 100) : 0;
  return (
    <div className="progress" style={{ height: 8, marginTop: 6 }}
      role="progressbar" aria-valuenow={Math.round(pct)} aria-valuemin={0} aria-valuemax={100}>
      <div className="bar" style={{
        width: `${pct}%`,
        background: pct >= 100 ? "var(--err)" : pct >= 80 ? "var(--warn)"
          : "linear-gradient(90deg, var(--accent), var(--accent-2))",
      }} />
    </div>
  );
}

export default function UsagePage() {
  const toast = useToast();
  const [state, setState] = useState<UsageState | null>(null);
  const [hourly, setHourly] = useState("");
  const [daily, setDaily] = useState("");
  const [error, setError] = useState<string | null>(null);

  const load = () =>
    api<UsageState>("/usage").then((s) => {
      setState(s);
      setHourly(String(s.limits.hourly_mb));
      setDaily(String(s.limits.daily_mb));
    }).catch(() => {});

  useEffect(() => { load(); const t = setInterval(load, 5000); return () => clearInterval(t); }, []);

  async function saveLimits() {
    setError(null);
    try {
      await api("/settings", {
        method: "POST",
        body: JSON.stringify({ usage: { hourly_mb: Number(hourly), daily_mb: Number(daily) } }),
      });
      toast("ok", "Usage limits updated");
      load();
    } catch (e) {
      setError((e as Error).message);
    }
  }

  if (!state) {
    return (
      <>
        <PageHeader kicker="Results · Bandwidth" title="Internet Usage" sub="Everything the app sends and receives." />
        <div className="skeleton" style={{ height: 220 }} />
      </>
    );
  }

  const { hour, day, limits } = state;
  const apiStats = state.api;
  const blocked = state.hourly_over || state.daily_over;
  return (
    <>
      <PageHeader
        title="Internet Usage"
        sub="File uploads and downloads are exact byte counts; generation request overhead is estimated. Limits pause NEW corrections when reached."
      />

      {blocked && (
        <div className="notice err" style={{ marginBottom: 14 }}>
          {state.hourly_over ? "Hourly" : "Daily"} internet usage limit reached — new corrections are paused
          until the window resets. Running jobs finish normally.
        </div>
      )}
      {error && <div className="notice err" style={{ marginBottom: 12 }}>{error}</div>}

      <div className="grid grid-4" style={{ marginBottom: 14 }}>
        <Stat label="This hour" value={`${mb(hour.total)} MB`} />
        <Stat label="Today" value={`${mb(day.total)} MB`} />
        <Stat compact label="Uploaded (today)" value={`${mb(day.up)} MB`} />
        <Stat compact label="Downloaded (today)" value={`${mb(day.down)} MB`} />
      </div>

      <div className="card" style={{ marginBottom: 14 }}>
        <h3>Limits <span className="hint">0 = unlimited · applies to rolling hour / UTC day</span></h3>
        <div className="grid grid-2">
          <div>
            <div className="row between small">
              <span>Hourly limit — {limits.hourly_mb} MB</span>
              <span className="faint">
                {state.hour_remaining_mb != null ? `${state.hour_remaining_mb.toFixed(1)} MB left` : "unlimited"}
              </span>
            </div>
            {limits.hourly_bytes > 0 && <LimitBar used={hour.total} cap={limits.hourly_bytes} />}
          </div>
          <div>
            <div className="row between small">
              <span>Daily limit — {limits.daily_mb} MB</span>
              <span className="faint">
                {state.day_remaining_mb != null ? `${state.day_remaining_mb.toFixed(1)} MB left` : "unlimited"}
              </span>
            </div>
            {limits.daily_bytes > 0 && <LimitBar used={day.total} cap={limits.daily_bytes} />}
          </div>
        </div>
        <div className="row wrap" style={{ marginTop: 14, gap: 8 }}>
          <label className="small">Hourly MB
            <input className="input" style={{ width: 100, marginLeft: 6 }} value={hourly}
              onChange={(e) => setHourly(e.target.value)} inputMode="numeric" aria-label="Hourly limit in MB" />
          </label>
          <label className="small">Daily MB
            <input className="input" style={{ width: 100, marginLeft: 6 }} value={daily}
              onChange={(e) => setDaily(e.target.value)} inputMode="numeric" aria-label="Daily limit in MB" />
          </label>
          <button className="btn primary sm" onClick={saveLimits}>Save limits</button>
        </div>
      </div>

      <div className="card" style={{ marginBottom: 14 }}>
        <h3>API requests &amp; retries <span className="hint">counted locally from requests already being made — never an extra API call</span></h3>
        {apiStats && !apiStats.provider.available && (
          <div className="notice warn" style={{ marginBottom: 10 }}>
            Provider cooling down after repeated high-demand (503) failures — queued jobs are paused
            for ~{Math.ceil(apiStats.provider.cooldown_remaining)}s instead of resending whole submissions into the outage.
          </div>
        )}
        <div className="grid grid-4" style={{ marginBottom: 10 }}>
          <Stat label="Requests attempted" value={apiStats?.requests ?? 0} />
          <Stat compact label="Successful" value={apiStats?.successes ?? 0} />
          <Stat compact label="Failed" value={apiStats?.failures ?? 0} />
          <Stat compact label="Job retries" value={apiStats?.retries ?? 0} />
        </div>
        <div className="grid grid-4">
          <Stat compact label="Key failovers" value={apiStats?.failovers ?? 0} />
          <Stat compact label="Cooldowns" value={apiStats?.cooldowns ?? 0} />
          <Stat label="Re-downloads avoided" value={apiStats?.avoided.downloads ?? 0} />
          <Stat compact label="Re-uploads avoided" value={apiStats?.avoided.uploads ?? 0} />
        </div>
        <div className="row wrap small faint" style={{ marginTop: 10, gap: 10 }}>
          <span>Provider: {apiStats && apiStats.provider.available === false
            ? `cooling down (~${Math.ceil(apiStats.provider.cooldown_remaining)}s left)`
            : "ready"}</span>
          <span>PDF re-preparations avoided: {apiStats?.avoided.reprepare ?? 0}</span>
          {apiStats && (apiStats.keys?.requests ?? 0) > 0 && (
            <span>Keys — {apiStats.keys.requests} requests / {apiStats.keys.successes} ok / {apiStats.keys.failures} failed</span>
          )}
        </div>
        {apiStats && Object.keys(apiStats.by_class).length > 0 && (
          <div className="row wrap" style={{ marginTop: 8, gap: 6 }}>
            {Object.entries(apiStats.by_class).sort((a, b) => b[1] - a[1]).map(([cls, n]) => (
              <span key={cls} className="hint" style={{ fontSize: 12 }}>
                {ERROR_CLASS_LABELS[cls] ?? cls}: {n}
              </span>
            ))}
          </div>
        )}
      </div>

      <div className="card" style={{ marginBottom: 14 }}>
        <h3>Today by category</h3>
        <DataTable head={<tr>
          <th>Category</th><th className="num">Up</th><th className="num">Down</th><th className="num">Total</th>
        </tr>}>
          {Object.entries(day.by_category).length === 0 && (
            <tr><td colSpan={4} className="faint">No internet usage recorded yet today.</td></tr>
          )}
          {Object.entries(day.by_category)
            .sort((a, b) => (b[1].up + b[1].down) - (a[1].up + a[1].down))
            .map(([cat, v]) => (
              <tr key={cat}>
                <td data-label="Category">{CATEGORY_LABELS[cat] ?? cat}</td>
                <td data-label="Up" className="num">{mb(v.up)} MB</td>
                <td data-label="Down" className="num">{mb(v.down)} MB</td>
                <td data-label="Total" className="num">{mb(v.up + v.down)} MB</td>
              </tr>
            ))}
        </DataTable>
      </div>

      <div className="card">
        <h3>Last 24 hours <span className="hint">total MB per hour (up + down)</span></h3>
        <BarChart
          title="Internet usage per hour"
          hint="red bars are at the hourly limit"
          summary={`Internet usage in MB per hour over the last 24 hours: ${state.history
            .map((h) => `${new Date(h.bucket * 1000).getHours()}:00 ${mb(h.up + h.down)} MB`)
            .join(", ")}.`}
          height={170}
          valueFormat={(v) => `${v.toFixed(2)} MB`}
          bars={state.history.map((h) => {
            const total = h.up + h.down;
            const over = limits.hourly_bytes > 0 && total >= limits.hourly_bytes;
            return {
              label: `${new Date(h.bucket * 1000).getHours()}:00`,
              value: total / (1024 * 1024),
              tone: (over ? "err" : "accent") as "err" | "accent",
            };
          })}
        />
      </div>
    </>
  );
}
