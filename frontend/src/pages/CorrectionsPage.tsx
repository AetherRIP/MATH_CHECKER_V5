import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api, displayName, elapsed, postForm, ACTIVE_STATUSES, type Job } from "../api";
import { Empty, PageHeader, Progress, StatusBadge, useToast } from "../components";
import { useAdaptivePoll } from "../useAdaptivePoll";

/** Compact MB helper for the per-job bandwidth line (§17). */
function mb(bytes: number | undefined): string {
  const value = bytes ?? 0;
  if (value < 1024) return `${value} B`;
  if (value < 1024 * 1024) return `${(value / 1024).toFixed(0)} KB`;
  return `${(value / (1024 * 1024)).toFixed(2)} MB`;
}

type JobFilter = "all" | "running" | "completed" | "failed" | "cancelled";

const JOB_FILTERS: { key: JobFilter; label: string; match: (j: Job) => boolean }[] = [
  { key: "all", label: "All", match: () => true },
  { key: "running", label: "Running", match: (j) => ACTIVE_STATUSES.includes(j.status) },
  { key: "completed", label: "Completed", match: (j) => j.status === "COMPLETED" },
  { key: "failed", label: "Failed", match: (j) => j.status === "FAILED" },
  { key: "cancelled", label: "Cancelled", match: (j) => j.status === "CANCELLED" },
];

/** Cadence while a job is moving, or while the live stream is not delivering. */
const POLL_MS = 4000;
/** Cadence when the stream is healthy and every job has stopped. */
const POLL_IDLE_MS = 20_000;

export function useJobQueue(onChange?: (jobs: Job[]) => void) {
  const [jobs, setJobs] = useState<Job[]>([]);
  const [streamAlive, setStreamAlive] = useState(false);
  const ref = useRef(onChange);
  ref.current = onChange;

  const load = useCallback(() => api<{ jobs: Job[] }>("/jobs?limit=50")
    .then((r) => { setJobs(r.jobs); ref.current?.(r.jobs); })
    .catch(() => {}), []);

  // Live updates over SSE: one connection, pushed by the backend.
  useEffect(() => {
    let source: EventSource | null = null;
    try {
      source = new EventSource("/api/jobs/stream");
      source.onmessage = (event) => {
        const payload = JSON.parse(event.data);
        setStreamAlive(true);
        if (payload.type === "hello") { load(); return; }
        if (payload.type === "job" && payload.job) {
          const job = payload.job as Job;
          setJobs((list) => {
            const next = list.filter((j) => j.id !== job.id);
            next.unshift(job);
            return next.slice(0, 50);
          });
        }
      };
      // EventSource reconnects on its own; until it is back we poll instead, so
      // the list is never dependent on the stream (proxies do buffer it).
      source.onerror = () => setStreamAlive(false);
    } catch { source = null; }
    return () => source?.close();
  }, [load]);

  // The poll is a safety net, not the primary channel: it runs fast only while a
  // job is actually moving or the stream is quiet, and drops to a slow repair
  // cadence (and stops while the window is hidden) when everything has stopped.
  const busy = jobs.some((j) => ACTIVE_STATUSES.includes(j.status));
  useAdaptivePoll(load, {
    active: busy || !streamAlive, fast: POLL_MS, idle: POLL_IDLE_MS,
  });

  return { jobs, load };
}

/**
 * The live correction log — one implementation, used by the unified correction
 * page and by the legacy Corrections page so they can never disagree.
 */
export function JobQueue({ activeJobId, compact }: {
  activeJobId?: string | null;
  compact?: boolean;
}) {
  const toast = useToast();
  const { jobs, load } = useJobQueue();
  const [filter, setFilter] = useState<JobFilter>("all");
  // Visible-log marker only: hides finished entries from THIS view.
  // Never deletes jobs, results or any database record (§16).
  const [hideBefore, setHideBefore] = useState<number>(0);

  async function retry(id: string) {
    try {
      await api(`/jobs/${id}/retry`, { method: "POST" });
      toast("info", "Job re-queued");
      load();
    } catch (e) { toast("err", (e as Error).message); }
  }

  async function cancel(id: string) {
    try {
      await api(`/jobs/${id}/cancel`, { method: "POST" });
      toast("info", "Cancel requested");
      load();
    } catch (e) { toast("err", (e as Error).message); }
  }

  const activeCount = jobs.filter((j) => ACTIVE_STATUSES.includes(j.status)).length;

  const visibleJobs = useMemo(() => {
    const f = JOB_FILTERS.find((x) => x.key === filter) ?? JOB_FILTERS[0];
    return jobs.filter((j) =>
      !((hideBefore > 0) && j.created_at < hideBefore
        && !ACTIVE_STATUSES.includes(j.status)) && f.match(j));
  }, [jobs, filter, hideBefore]);

  const filterCount = (key: JobFilter) => {
    const f = JOB_FILTERS.find((x) => x.key === key)!;
    return jobs.filter((j) =>
      !((hideBefore > 0) && j.created_at < hideBefore
        && !ACTIVE_STATUSES.includes(j.status)) && f.match(j)).length;
  };

  function clearLog() {
    // Display-only clear: mark all currently-finished jobs as hidden.
    // Jobs, results and audit rows stay in the database untouched.
    setHideBefore(Date.now() / 1000);
    toast("info", "Log view cleared — finished jobs hidden. No data was deleted.");
  }

  return (
    <div className="card">
      <div className="row between wrap" style={{ marginBottom: 10, gap: 8 }}>
        <h3 style={{ margin: 0 }}>
          Job log <span className="hint">live via SSE</span>
        </h3>
        <div className="row wrap" style={{ gap: 8, alignItems: "center" }}>
          <span className="badge info">{activeCount} active</span>
          <button className="btn sm ghost" onClick={clearLog}>Clear Log</button>
        </div>
      </div>
      <div className="chip-row" style={{ marginBottom: 12 }}>
        {JOB_FILTERS.map((f) => {
          const count = filterCount(f.key);
          if (count === 0 && f.key !== "all") return null;
          return (
            <button key={f.key}
              className={`chip ${filter === f.key ? "active" : ""}`}
              onClick={() => setFilter(f.key)}>
              {f.label}<span className="chip-count">{count}</span>
            </button>
          );
        })}
      </div>
      {visibleJobs.length === 0 ? (
        <Empty icon="⏱" title={
            jobs.length === 0 ? "Your correction queue is empty"
            : filter !== "all" ? "Nothing matches this filter"
            : "Log cleared — running jobs will still appear here"}
          hint={jobs.length === 0
            ? "Queue a correction above or allow a Classroom assignment."
            : "Switch the filter to see other entries."} />
      ) : (
        <div style={{ maxHeight: compact ? "46vh" : "68vh", overflowY: "auto" }}>
          {visibleJobs.map((job) => (
            <div key={job.id} className="stack"
              style={{ gap: 5, padding: "10px 0",
                       borderBottom: "1px solid rgba(255,255,255,0.05)",
                       ...(job.id === activeJobId
                           ? { outline: "1px solid var(--accent)", outlineOffset: 2,
                               borderRadius: 6, paddingInline: 6 } : {}) }}>
              <div className="row between">
                <span style={{ fontWeight: 600 }}>{displayName(job)}</span>
                <StatusBadge status={job.status} />
              </div>
              <Progress value={job.progress} thin
                label={`Correcting ${displayName(job)}`} />
              <div className="row between small muted">
                <span>{job.stage_detail ?? job.status}{job.error ? ` — ${job.error}` : ""}</span>
                <span className="faint mono">{elapsed(job.created_at, job.finished_at)}</span>
              </div>
              {/* Why this job is waiting, and what it already spent doing so.
                  The ledger is persisted on the backend, so this survives a
                  restart and never reads as "still waiting" with no history. */}
              {(job.status === "WAITING_FOR_API" || job.status === "RETRYING"
                || (job.park_count ?? 0) > 0 || (job.manual_retries ?? 0) > 0) && (
                <div className="faint small">
                  {[
                    job.status === "WAITING_FOR_API" ? "Waiting for API capacity" : null,
                    job.park_count ? `${job.park_count} capacity wait(s)` : null,
                    job.waited_seconds ? `${Math.round(job.waited_seconds)}s spent waiting` : null,
                    job.last_failure_class ?? null,
                    job.retry_due_at ? `next try ${new Date(job.retry_due_at * 1000).toLocaleTimeString()}` : null,
                    `attempt ${job.attempts}/${job.max_attempts}`,
                    (job.manual_retries ?? 0) > 0
                      ? `manual retry ${job.manual_retries}/${job.max_manual_retries ?? 3}`
                      : null,
                  ].filter(Boolean).join(" · ")}
                </div>
              )}
              {job.bandwidth && (
                <div className="faint small" title="Per-job network operations — proves retries reuse prepared input">
                  ↓{mb(job.bandwidth.download_bytes)} Drive ({job.bandwidth.download_count} dl)
                  {' · '}↑{mb(job.bandwidth.upload_bytes)} to AI ({job.bandwidth.upload_count} up)
                  {' · '}{job.bandwidth.ai_attempts} AI attempt(s)
                  {job.bandwidth.prepare_count > 0 && ` · ${job.bandwidth.prepare_count} prepare`}
                  {(job.bandwidth.avoided_downloads > 0 || job.bandwidth.reused_uploads > 0
                    || job.bandwidth.avoided_reprepare > 0) && (
                    <span style={{ color: "var(--ok, #6ee7a8)" }}>
                      {" · reused: "}
                      {job.bandwidth.avoided_downloads > 0 && `${job.bandwidth.avoided_downloads} download(s) avoided`}
                      {job.bandwidth.reused_uploads > 0 && `${job.bandwidth.avoided_downloads > 0 ? ", " : ""}${job.bandwidth.reused_uploads} upload(s) reused`}
                      {job.bandwidth.avoided_reprepare > 0 && `${(job.bandwidth.avoided_downloads > 0 || job.bandwidth.reused_uploads > 0) ? ", " : ""}${job.bandwidth.avoided_reprepare} re-prepare(s) avoided`}
                    </span>
                  )}
                </div>
              )}
              {(job.status === "FAILED" || job.status === "CANCELLED") && (
                <div><button className="btn sm ghost" onClick={() => retry(job.id)}>Retry</button></div>
              )}
              {ACTIVE_STATUSES.includes(job.status) && (
                <div><button className="btn sm ghost" onClick={() => cancel(job.id)}>Cancel</button></div>
              )}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

/**
 * Legacy Corrections page — kept reachable by its #/corrections deep link.
 *
 * The unified correction page (AI → Correct) is now the single everyday entry
 * point; this page preserves the plain upload form for anyone who has it
 * bookmarked and still posts to the same /corrections/manual engine.
 */
export default function CorrectionsPage() {
  const toast = useToast();
  const [studentName, setStudentName] = useState("");
  const [uploading, setUploading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const subRef = useRef<HTMLInputElement>(null);
  const msRef = useRef<HTMLInputElement>(null);
  const [refresh, setRefresh] = useState(0);

  async function submit() {
    const sub = subRef.current?.files?.[0];
    const ms = msRef.current?.files?.[0];
    if (!sub || !ms) {
      setError("Choose both a student submission and the mark scheme PDF.");
      return;
    }
    setUploading(true); setError(null);
    const body = new FormData();
    body.append("submission", sub);
    body.append("mark_scheme", ms);
    body.append("student_name", studentName || sub.name.replace(/\.[^.]+$/, ""));
    try {
      await postForm("/corrections/manual", body);
      toast("ok", "Correction queued — progress appears below.");
      setStudentName("");
      if (subRef.current) subRef.current.value = "";
      if (msRef.current) msRef.current.value = "";
      setRefresh((n) => n + 1);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setUploading(false);
    }
  }

  return (
    <>
      <PageHeader kicker="Correct · Job log" title="Queue a correction"
        sub="The everyday correction flow is AI → Correct; this older form is kept for existing links." />

      <div className="results-layout">
        <div className="card">
          <h3>New correction <span className="hint">PDF or image</span></h3>
          <label className="field">
            <span>Student name</span>
            <input className="input" placeholder="e.g. Alice Wong" value={studentName}
              onChange={(e) => setStudentName(e.target.value)} />
          </label>
          <label className="field">
            <span>Student submission</span>
            <input className="input" type="file" accept=".pdf,.png,.jpg,.jpeg,.webp" ref={subRef} />
          </label>
          <label className="field">
            <span>Official mark scheme (PDF)</span>
            <input className="input" type="file" accept=".pdf" ref={msRef} />
          </label>
          {error && <div className="notice err" style={{ marginBottom: 10 }}>{error}</div>}
          <button className="btn primary" style={{ width: "100%" }} onClick={submit} disabled={uploading}>
            {uploading ? "Uploading…" : "Queue correction"}
          </button>
          <p className="faint small" style={{ marginBottom: 0, marginTop: 8 }}>
            This queue uses the same standard single-pass correction engine as
            the New correction page.
          </p>
        </div>

        <JobQueue key={refresh} />
      </div>
    </>
  );
}
