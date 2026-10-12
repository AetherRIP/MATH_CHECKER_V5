// The two correction workers, each in its own panel.
//
// These panels are the AI page's worker sections (AI → Normal PDF Worker and
// AI → Large PDF Worker). They read the REAL backend state — ``/workers`` for
// the pool that runs the single-pass engine, ``/large-pdf/jobs`` for the
// batched worker — so the status, counts, batch progress and control actions
// here are the same facts the engine is acting on, never a frontend illusion.
//
// Job EXECUTION history and monitoring live on the Task Manager page; this file
// is about configuring and observing the workers themselves.

import { useCallback, useEffect, useState } from "react";
import {
  api, correctionPipelines, largePdfCancel, largePdfJobs, largePdfRetryJob,
  type LargePdfJob, type LargePdfListing, type LargePdfProgress,
  type PipelineInfo,
} from "../api";
import { useAdaptivePoll } from "../useAdaptivePoll";

/** A large-PDF job that has not reached a terminal state yet. */
function largeJobRunning(job: LargePdfJob): boolean {
  return !["COMPLETED", "FAILED", "CANCELLED", "ERROR"].includes(job.status);
}
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

/** Tone for a large-worker job state so a held/failed job never reads green. */
export function largeTone(status: string): "info" | "mute" | "warn" | "err" | "ok" {
  if (status === "COMPLETED") return "ok";
  if (status === "AWAITING_REVIEW") return "warn";
  if (status === "FAILED") return "err";
  if (status === "PROCESSING" || status === "VALIDATING" || status === "MERGING") return "info";
  return "mute";
}

/**
 * Live progress for one long-document job, from the backend's real job +
 * batch state — never a simulated percentage.
 *
 * Shows: the student and PDF, total pages, batch X of Y, the pages being
 * processed right now, questions detected vs marked, the current stage
 * (extracting / detecting questions / marking / verifying / report), a
 * proportional progress bar and a clear error + retry state.
 */
export function LargeJobProgress({ job, progress }: {
  job: LargePdfJob;
  progress?: LargePdfProgress;
}) {
  const status = job.status;
  const pct = progress && progress.page_count > 0
    ? Math.min(100, Math.round(100 * progress.pages_done / progress.page_count))
    : (status === "COMPLETED" ? 100 : 0);
  const barTone = status === "FAILED" ? "tone-err"
    : status === "AWAITING_REVIEW" ? "tone-warn"
    : status === "COMPLETED" ? "tone-ok" : "";
  const errored = status === "FAILED"
    || (progress?.batch_summary?.failed ?? 0) > 0;
  return (
    <div className="card" style={{ padding: "12px 14px", marginBottom: 10 }}>
      <div className="row between wrap">
        <strong>{job.student_name || "Student"}</strong>
        <Badge tone={largeTone(status)}>{job.status_label || status}</Badge>
      </div>

      <div className="hint small" style={{ marginTop: 4 }}>
        {progress?.page_count ? `${progress.page_count}-page PDF · ` : ""}
        {progress?.current_batch && progress?.batch_total
          ? `Batch ${progress.current_batch} of ${progress.batch_total}
            (${progress.current_batch_pages
              ? `pages ${progress.current_batch_pages.start}–${progress.current_batch_pages.end}`
              : "pages pending"})`
          : ""}
        {progress?.questions_total
          ? ` · ${progress.questions_marked}/${progress.questions_total} questions marked`
          : ""}
      </div>

      <div className={`progress ${barTone}`} style={{ margin: "8px 0" }}>
        <div className="bar" style={{ width: `${pct}%` }} />
      </div>

      <div className="row between wrap hint small">
        <span>{progress?.stage?.label || "Waiting"}</span>
        <span>{pct}% · {progress ? `${progress.pages_done}/${progress.page_count} pages` : ""}</span>
      </div>

      {(errored || job.retryable) && job.error && (
        <div className="hint small" style={{ color: "var(--err, #c0392b)", marginTop: 6 }}>
          {job.error}
        </div>
      )}
    </div>
  );
}

/* --------------------------------------------------------- normal PDF worker */

/**
 * The NORMAL PDF worker: the pool of isolated processes that run the trusted
 * single-pass engine on submitted short PDFs.
 *
 * Configuration that is actually applied: the worker count goes to the real
 * pool (``/workers/resize``), which the backend caps by healthy API keys; the
 * retry count and the single-pass page limit are read back from the engine's
 * own configuration (``/settings`` and ``/corrections/pipelines``). The table
 * below is the pool's own state, so "IDLE/PROCESSING/STOPPED" is a fact.
 */
export function NormalWorkerPanel({ onNavigate }: {
  onNavigate: (hash: string) => void;
}) {
  const toast = useToast();
  const [state, setState] = useState<PoolState | null>(null);
  const [pipelines, setPipelines] = useState<PipelineInfo | null>(null);
  const [limits, setLimits] = useState<{ concurrency: number; max_retries: number } | null>(null);
  const [resizing, setResizing] = useState(false);

  // The worker pool is the only thing here that changes on its own; the pool's
  // configured limits and the pipeline description are read once.
  useEffect(() => {
    api<{ worker: { concurrency: number; max_retries: number } }>("/settings")
      .then((s) => setLimits(s.worker)).catch(() => {});
    correctionPipelines().then(setPipelines).catch(() => {});
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
      hint={`single-pass engine, up to ${pipelines?.threshold_pages ?? 30} pages per submission`}>
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

      {pipelines?.large_pdf_enabled === false && (
        <div className="notice warn small" style={{ marginTop: 10 }}>
          The batched pipeline is switched off, so a document above{" "}
          {pipelines.threshold_pages} pages is refused instead of being marked
          single-pass.
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

/* ---------------------------------------------------------- large PDF worker */

/**
 * The LARGE PDF worker: the batched pipeline that splits a long document into
 * real batches, marks each one and merges the result.
 *
 * It reads the REAL backend state from ``/large-pdf/jobs`` — the same worker
 * the routing rule sends every submission over the threshold to — so the
 * counts, batch progress and retry controls here are never a frontend
 * illusion. Its configuration (enabled, threshold, batch size, concurrency)
 * comes from the engine's own config in the same response.
 */
export function LargeWorkerPanel({ onOpen }: { onOpen: () => void }) {
  const toast = useToast();
  const [listing, setListing] = useState<LargePdfListing | null>(null);
  const [busy, setBusy] = useState<string | null>(null);

  const load = useCallback(() => largePdfJobs().then(setListing).catch(() => {}), []);
  const largeBusy = Boolean(listing && (
    listing.jobs.some(largeJobRunning)
    || (listing.counts.queued ?? 0) + (listing.counts.inspecting ?? 0)
      + (listing.counts.planning ?? 0) + (listing.counts.ready ?? 0)
      + (listing.counts.processing ?? 0) > 0));
  useAdaptivePoll(load, { active: largeBusy, fast: 2500, idle: 20_000 });

  async function act(label: string, id: string, fn: () => Promise<unknown>) {
    setBusy(id);
    try {
      await fn();
      toast("ok", label);
      largePdfJobs().then(setListing).catch(() => {});
    } catch (e) {
      toast("err", (e as Error).message);
    } finally {
      setBusy(null);
    }
  }

  if (!listing) return <div className="skeleton" style={{ height: 220 }} />;
  const { config, counts, jobs } = listing;
  const active = jobs.filter((j) =>
    ["QUEUED", "INSPECTING", "PLANNING", "READY", "PROCESSING", "VALIDATING", "MERGING", "AWAITING_REVIEW", "FAILED"]
      .includes(j.status));
  const progressOf = (j: LargePdfJob) => listing.progress?.[j.id];
  const recent = jobs.slice(0, 8);

  return (
    <Section title="Large PDF worker" icon="grid"
      hint="batched pipeline: split a long document, mark each batch, merge the result">
      <div className="row between wrap" style={{ gap: 10, marginBottom: 12 }}>
        <div className="row wrap" style={{ gap: 8 }}>
          <Badge tone={config.enabled ? "info" : "mute"}>
            {config.enabled ? "ENABLED" : "DISABLED"}
          </Badge>
          <Badge tone="mute">{config.concurrency} batch(es) at a time</Badge>
          {counts.failed > 0 && <Badge tone="err">{counts.failed} failed</Badge>}
          {counts.awaiting_review > 0 && <Badge tone="warn">{counts.awaiting_review} awaiting review</Badge>}
        </div>
        <Button size="sm" variant="secondary" icon="external" onClick={onOpen}>
          Open the full Large AI Worker page
        </Button>
      </div>

      <div className="grid grid-4" style={{ margin: "0 0 12px" }}>
        <Stat compact label="Queued" value={counts.queued + counts.ready}
          tone={counts.queued + counts.ready > 0 ? "warn" : undefined} />
        <Stat compact label="Processing" value={counts.processing} />
        <Stat compact label="Awaiting review" value={counts.awaiting_review}
          tone={counts.awaiting_review > 0 ? "warn" : undefined} />
        <Stat compact label="Failed" value={counts.failed}
          tone={counts.failed > 0 ? "err" : undefined} />
      </div>

      <div className="hint small" style={{ marginBottom: 10 }}>
        Route: the student PDF decides — <strong>{config.threshold_pages} pages or fewer</strong> uses
        the Normal PDF worker; more uses this worker, split into {config.target_min}–{config.target_max}
        -page batches. Currently {active.length} job(s) in flight.
        {config.enabled ? "" : " It is switched off, so long documents are refused."}
      </div>

      {active.length > 0 && (
        <div style={{ marginBottom: 12 }}>
          {active.map((job) => (
            <LargeJobProgress key={job.id} job={job}
              progress={progressOf(job)} />
          ))}
        </div>
      )}

      {recent.length === 0 ? (
        <Empty icon="📄" title="No long documents yet"
          hint={`Submissions over ${config.threshold_pages} pages appear here automatically.`} />
      ) : (
        <DataTable head={<tr>
          <th>Student</th><th className="num">Pages</th><th className="num">Batches</th>
          <th>Status</th><th>Actions</th>
        </tr>}>
          {recent.map((job) => (
            <tr key={job.id}>
              <td data-label="Student"><strong>{job.student_name || "Student"}</strong></td>
              <td data-label="Pages" className="num">{job.page_count || "—"}</td>
              <td data-label="Batches" className="num">{job.batch_count || "—"}</td>
              <td data-label="Status">
                <Badge tone={largeTone(job.status)}>{job.status_label || job.status}</Badge>
                {job.step_detail && <div className="faint small">{job.step_detail}</div>}
              </td>
              <td data-label="Actions">
                <div className="row">
                  {job.retryable && (
                    <Button size="sm" variant="secondary" loading={busy === job.id}
                      onClick={() => act("Retry started", job.id, () => largePdfRetryJob(job.id))}>Retry</Button>
                  )}
                  {active.some((a) => a.id === job.id) && (
                    <Button size="sm" variant="ghost" loading={busy === job.id}
                      onClick={() => act("Stopped", job.id, () => largePdfCancel(job.id))}>Stop</Button>
                  )}
                </div>
              </td>
            </tr>
          ))}
        </DataTable>
      )}
    </Section>
  );
}
