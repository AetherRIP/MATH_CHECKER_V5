// The correction worker panel.
//
// This is the AI page's Normal PDF Worker section. It reads the REAL backend
// state (``/workers``) — the pool that runs the single-pass engine — so the
// status, counts, progress and control actions here are the same facts the
// engine is acting on, never a frontend illusion.
//
// Job EXECUTION history and monitoring live on the Task Manager page; this file
// is about configuring and observing the worker itself.

import { useCallback, useEffect, useState } from "react";
import { api } from "../api";
import { useAdaptivePoll } from "../useAdaptivePoll";
import {
  Badge, Button, DataTable, Empty, KV, Section, Segmented, Stat, useToast,
} from "../components";

export interface WorkerInfo {
  worker_id: string; status: string; current_job_id: string | null;
  student_name: string | null; fingerprint: string | null;
  jobs_done: number; jobs_failed: number; errors: number;
  last_error: string | null; elapsed: number | null;
}

export interface PoolState {
  workers: WorkerInfo[]; active: number; size: number; recommended: number;
  queue: { queued: number; starting: number; processing: number; waiting_for_api: number;
           retrying: number; completed: number; failed: number; cancelled: number };
  metrics: { avg_seconds?: number; fastest_seconds?: number; slowest_seconds?: number;
             avg_queue_wait_seconds?: number; jobs_per_hour?: number; retries_total?: number;
             completed_measured?: number };
}

function WorkerBadge({ status }: { status: string }) {
  const tone = status === "PROCESSING" ? "info"
    : status === "IDLE" ? "mute"
    : status === "RETRYING" ? "warn"
    : status === "STOPPED" ? "err" : "mute";
  return <Badge tone={tone as "info" | "mute" | "warn" | "err"}>{status}</Badge>;
}

/* --------------------------------------------------------- normal PDF worker */

/**
 * The NORMAL PDF worker: the pool of isolated processes that run the trusted
 * single-pass engine on submitted short PDFs.
 *
 * Configuration that is actually applied: the worker count goes to the real
 * pool (``/workers/resize``), which the backend caps by healthy API keys; the
 * retry count is read back from the engine's own configuration
 * (``/settings``). The table below is the pool's own state, so
 * "IDLE/PROCESSING/STOPPED" is a fact.
 */
export function NormalWorkerPanel({ onNavigate }: {
  onNavigate: (hash: string) => void;
}) {
  const toast = useToast();
  const [state, setState] = useState<PoolState | null>(null);
  const [limits, setLimits] = useState<{ concurrency: number; max_retries: number } | null>(null);
  const [resizing, setResizing] = useState(false);

  // The worker pool is the only thing here that changes on its own; the pool's
  // configured limits are read once.
  useEffect(() => {
    api<{ worker: { concurrency: number; max_retries: number } }>("/settings")
      .then((s) => setLimits(s.worker)).catch(() => {});
  }, []);

  const loadPool = useCallback(() => api<PoolState>("/workers")
    .then(setState).catch(() => {}), []);

  const poolBusy = Boolean(state && (state.active > 0
    || state.queue.queued > 0 || state.queue.starting > 0
    || state.queue.processing > 0 || state.queue.waiting_for_api > 0
    || state.queue.retrying > 0));
  useAdaptivePoll(loadPool, { active: poolBusy, fast: 2500, idle: 20_000 });

  async function resize(n: number) {
    setResizing(true);
    try {
      const result = await api<{ requested: number; running: number; recommended: number }>(
        "/workers/resize", { method: "POST", body: JSON.stringify({ workers: n }) });
      toast("ok", `${result.running} worker(s) running (cap ${result.recommended} by healthy keys)`);
      api<PoolState>("/workers").then(setState).catch(() => {});
    } catch (e) {
      toast("err", (e as Error).message);
    } finally {
      setResizing(false);
    }
  }

  if (!state) return <div className="skeleton" style={{ height: 220 }} />;
  const q = state.queue;
  const m = state.metrics;

  return (
    <Section title="Normal PDF worker" icon="cpu"
      hint="single-pass correction engine">
      <div className="row between wrap" style={{ gap: 10, marginBottom: 12 }}>
        <div className="row wrap" style={{ gap: 8 }}>
          <Badge tone={state.active > 0 ? "info" : "mute"}>
            {state.active > 0 ? "PROCESSING" : "IDLE"}
          </Badge>
          <Badge tone="mute">{state.active}/{state.size} active</Badge>
          {state.workers.some((w) => w.status === "STOPPED") && (
            <Badge tone="err">worker stopped</Badge>
          )}
          {state.workers.some((w) => w.last_error) && (
            <Badge tone="warn">worker error</Badge>
          )}
        </div>
        <div className="row wrap" style={{ gap: 8 }}>
          <Button size="sm" variant="secondary" icon="wand"
            onClick={() => onNavigate("#/correct")}>Correct a PDF</Button>
          <Button size="sm" variant="ghost" icon="list"
            onClick={() => onNavigate("#/tasks")}>Task Manager</Button>
        </div>
      </div>

      <div className="grid grid-4" style={{ marginBottom: 12 }}>
        <Stat compact label="Queued" value={q.queued} tone={q.queued > 0 ? "warn" : undefined} />
        <Stat compact label="Processing" value={q.processing + q.starting} />
        <Stat compact label="Completed" value={q.completed} tone="ok" />
        <Stat compact label="Failed" value={q.failed} tone={q.failed > 0 ? "err" : undefined} />
      </div>

      <div className="row between wrap" style={{ marginTop: 4 }}>
        <span className="hint">
          Worker count — capped automatically by healthy API keys ({state.recommended}).
          {limits ? ` Retries per submission: ${limits.max_retries}.` : ""}
        </span>
        <Segmented<string>
          value={String(state.size)}
          onChange={(n) => resize(Number(n))}
          ariaLabel="Worker count"
          options={[1, 2, 3, 4, 5, 6].map((n) => ({ value: String(n), label: String(n) }))} />
      </div>
      {resizing && <div className="faint small" style={{ marginTop: 8 }}>Resizing…</div>}

      <div style={{ marginTop: 12 }}>
        {state.workers.length === 0 ? (
          <Empty icon="⛏" title="No workers running"
            hint="Workers start with the application; add an API key to run corrections." />
        ) : (
          <DataTable head={<tr>
            <th>Worker</th><th>Status</th><th>Job</th><th>Student</th>
            <th className="num">Done</th><th className="num">Errors</th>
          </tr>}>
            {state.workers.map((w) => (
              <tr key={w.worker_id}>
                <td data-label="Worker"><strong>{w.worker_id}</strong></td>
                <td data-label="Status"><WorkerBadge status={w.status} /></td>
                <td data-label="Job" className="mono">{w.current_job_id ? w.current_job_id.slice(0, 8) + "…" : "—"}</td>
                <td data-label="Student">{w.student_name ?? "—"}</td>
                <td data-label="Done" className="num">{w.jobs_done}</td>
                <td data-label="Errors" className="num">{w.errors}</td>
              </tr>
            ))}
          </DataTable>
        )}
      </div>

      {state.workers.some((w) => w.last_error) && (
        <div className="notice err small" style={{ marginTop: 10 }}>
          Last worker error: {state.workers.filter((w) => w.last_error)
            .map((w) => `${w.worker_id}: ${w.last_error}`).join(" · ")}
        </div>
      )}

      <div className="card" style={{ marginTop: 12 }}>
        <h3>Throughput
          <span className="hint">measured, not assumed — add workers only while throughput improves</span>
        </h3>
        {Object.keys(m).length === 0 ? (
          <Empty icon="📈" title="No completed jobs measured yet"
            hint="Metrics appear as corrections finish." />
        ) : (
          <div className="grid grid-4">
            {m.avg_seconds != null && <Stat compact label="Avg correction time" value={`${m.avg_seconds}s`} />}
            {m.fastest_seconds != null && <Stat compact label="Fastest" value={`${m.fastest_seconds}s`} />}
            {m.slowest_seconds != null && <Stat compact label="Slowest" value={`${m.slowest_seconds}s`} />}
            <Stat compact label="Jobs / hour" value={m.jobs_per_hour ?? 0} tone="ok" />
            {m.avg_queue_wait_seconds != null && <Stat compact label="Avg queue wait" value={`${m.avg_queue_wait_seconds}s`} />}
            <Stat compact label="Retries total" value={m.retries_total ?? 0} />
            {m.completed_measured != null && <Stat compact label="Measured jobs" value={m.completed_measured} />}
          </div>
        )}
      </div>

      <div className="card" style={{ marginTop: 12 }}>
        <h3>Queue</h3>
        <KV k="Queued" v={q.queued} />
        <KV k="Starting" v={q.starting} />
        <KV k="Processing" v={q.processing} />
        {q.waiting_for_api > 0 && <KV k="Waiting for API" v={q.waiting_for_api} />}
        {q.retrying > 0 && <KV k="Retrying" v={q.retrying} />}
        <KV k="Completed" v={q.completed} />
        {q.failed > 0 && <KV k="Failed" v={q.failed} />}
        {q.cancelled > 0 && <KV k="Cancelled" v={q.cancelled} />}
      </div>
    </Section>
  );
}
