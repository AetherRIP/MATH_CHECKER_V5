// Task Manager — what the application is actually doing, and what it did.
//
// One page, one list, built ONLY from the backend's own records:
//
//   * /jobs                → single-pass correction jobs (with their SSE stage
//                            events, so the list updates as work progresses)
//   * /large-pdf/jobs      → batched long-document jobs (real batch + page
//                            progress, per-job retry/stop)
//   * /automation/jobs     → the Google Classroom correction workflow
//   * /classroom/sync/progress → the live synchronization run
//   * /workers             → the pool that is executing all of it
//
// A row appears only when a real record exists: nothing is invented, no
// progress is simulated, and when an operation cannot report a percentage the
// page shows its actual stage instead of a made-up bar. The same submission is
// never listed twice — a Classroom workflow row covers the correction job it
// drives, so repeated synchronization cannot duplicate a task here.
//
// Worker CONFIGURATION lives on the AI page; this page executes and monitors.

import { useCallback, useMemo, useState } from "react";
import {
  ACTIVE_STATUSES, api, automationApprove, automationJobs, automationRetryJob,
  automationRetryReport, automationStatus, cancelJob, displayName, elapsed,
  largePdfCancel, largePdfJobs, largePdfRetryJob, retryJob, syncClassroomProgress,
  timeAgo,
  type AutomationJob, type AutomationStatus, type Job, type LargePdfJob,
  type LargePdfListing, type SyncSnapshot,
} from "../api";
import { Badge, Button, Empty, PageHeader, Progress, Stat, useToast } from "../components";
import { useAdaptivePoll } from "../useAdaptivePoll";
import { useJobQueue } from "./CorrectionsPage";
import type { PoolState } from "../workers/panels";

type Kind = "correction" | "large" | "automation" | "sync";
type Bucket = "running" | "queued" | "needsYou" | "completed" | "failed" | "cancelled";

const KIND_LABEL: Record<Kind, string> = {
  correction: "Correction",
  large: "Large PDF",
  automation: "Classroom",
  sync: "Sync",
};

const BUCKET_LABEL: Record<Bucket, string> = {
  running: "Running",
  queued: "Queued",
  needsYou: "Needs you",
  completed: "Completed",
  failed: "Failed",
  cancelled: "Cancelled",
};

const COMPLETED_STATES = new Set([
  "COMPLETED", "COMPLETE", "RELEASED", "PUBLISHED", "APPROVED", "DONE", "SUBMITTED",
]);
const CANCELLED_STATES = new Set(["CANCELLED", "CANCELED", "STOPPED", "SKIPPED"]);
const NEEDS_YOU_STATES = new Set([
  "AWAITING_REVIEW", "AWAITING_APPROVAL", "NEEDS_ATTENTION", "REVIEW_REQUIRED", "PAUSED",
]);

/**
 * Which of the five real states a backend status string belongs to.
 *
 * Explicit sets first (the backend's own vocabulary: JobStatus, the large-PDF
 * states and the automation state machine), then a conservative pattern match.
 * An unknown status is never shown as completed — the worst case is "Queued".
 */
function bucketOf(status: string | null | undefined): Bucket {
  const s = (status ?? "").toUpperCase();
  if (!s) return "queued";
  if (NEEDS_YOU_STATES.has(s)) return "needsYou";
  if (COMPLETED_STATES.has(s)) return "completed";
  if (CANCELLED_STATES.has(s)) return "cancelled";
  if (/FAIL|ERROR|REJECT/.test(s)) return "failed";
  if ((ACTIVE_STATUSES as string[]).includes(s)) return "running";
  if (/PROCESS|RUNNING|INSPECT|PLANNING|VALIDAT|MERG|UPLOAD|DOWNLOAD|MARK|SYNC|CORRECT|VERIFY/.test(s)) {
    return "running";
  }
  return "queued";
}

function human(status: string | null | undefined): string {
  const s = (status ?? "").replace(/_/g, " ").trim().toLowerCase();
  return s ? s.charAt(0).toUpperCase() + s.slice(1) : "Unknown";
}

function bucketTone(bucket: Bucket): "ok" | "warn" | "err" | "info" | "mute" {
  if (bucket === "completed") return "ok";
  if (bucket === "failed") return "err";
  if (bucket === "needsYou") return "warn";
  if (bucket === "running") return "info";
  return "mute";
}

interface TaskAction {
  label: string;
  variant?: "primary" | "secondary" | "ghost";
  run: () => Promise<unknown>;
}

interface TaskRow {
  key: string;
  kind: Kind;
  title: string;
  student: string | null;
  assignment: string | null;
  status: string;
  statusLabel: string;
  bucket: Bucket;
  /** Real stage text when the operation cannot report a percentage. */
  stage: string | null;
  /** Real percentage, or null — never a fabricated one. */
  progress: number | null;
  createdAt: number | null;
  finishedAt: number | null;
  error: string | null;
  attempts: string | null;
  detail: string[];
  actions: TaskAction[];
  openHash?: string;
}

const POLL_FAST = 2500;   // workers + large batches, while work is in flight
const POLL_MED = 5000;    // automation workflow, while it is moving
const POLL_SYNC = 2000;   // classroom synchronization, while a sync runs
// Every one of the three falls back to a slow idle cadence (and stops entirely
// while the window is hidden) — see useAdaptivePoll.

export default function TaskManagerPage({ onNavigate }: {
  onNavigate: (hash: string) => void;
}) {
  const toast = useToast();
  // Correction jobs stream their stage updates over SSE; the poll inside the
  // shared hook keeps the list right when a proxy buffers the stream.
  const { jobs, load: reloadJobs } = useJobQueue();
  const [large, setLarge] = useState<LargePdfListing | null>(null);
  const [automation, setAutomation] = useState<AutomationJob[]>([]);
  const [automationState, setAutomationState] = useState<AutomationStatus | null>(null);
  const [sync, setSync] = useState<SyncSnapshot | null>(null);
  const [pool, setPool] = useState<PoolState | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [kindFilter, setKindFilter] = useState<Kind | "all">("all");
  const [bucketFilter, setBucketFilter] = useState<Bucket | "all">("all");
  const [query, setQuery] = useState("");

  const loadLarge = useCallback(() => {
    largePdfJobs().then(setLarge).catch(() => {});
  }, []);
  const loadAutomation = useCallback(() => {
    automationJobs().then((r) => setAutomation(r.jobs)).catch(() => {});
    automationStatus().then(setAutomationState).catch(() => {});
  }, []);
  const loadSync = useCallback(() => {
    syncClassroomProgress().then(setSync).catch(() => {});
  }, []);
  const loadPool = useCallback(() => {
    api<PoolState>("/workers").then(setPool).catch(() => {});
  }, []);

  // One adaptive cadence per source instead of three fixed timers that ran
  // whether or not anything was happening. Correction jobs also stream their
  // own stage updates, so the poll is only there to keep the list truthful.
  const largeBusy = Boolean(large && large.jobs.some((j) =>
    !["COMPLETED", "FAILED", "CANCELLED", "ERROR"].includes(j.status)));
  const poolBusy = Boolean(pool && (pool.active > 0
    || pool.queue.queued > 0 || pool.queue.starting > 0
    || pool.queue.processing > 0 || pool.queue.waiting_for_api > 0
    || pool.queue.retrying > 0));
  const automationBusy = Boolean(
    (automationState && (automationState.counts.processing > 0
      || automationState.counts.waiting > 0))
    || automation.some((j) => !["COMPLETED", "FAILED", "CANCELLED"].includes(j.status)));

  useAdaptivePoll(() => { loadLarge(); loadPool(); },
    { active: largeBusy || poolBusy, fast: POLL_FAST, idle: 20_000 });
  useAdaptivePoll(loadAutomation,
    { active: automationBusy, fast: POLL_MED, idle: 30_000, immediate: false });
  useAdaptivePoll(loadSync,
    { active: Boolean(sync?.running), fast: POLL_SYNC, idle: 30_000, immediate: false });

  async function act(row: TaskRow, action: TaskAction) {
    setBusy(row.key);
    try {
      await action.run();
      toast("ok", `${action.label} — requested`);
      reloadJobs(); loadLarge(); loadAutomation(); loadSync();
    } catch (e) {
      // The backend's refusal is the truth (e.g. "only failed jobs can be
      // retried"), so it is shown verbatim instead of a generic failure.
      toast("err", (e as Error).message);
    } finally {
      setBusy(null);
    }
  }

  /* --------------------------------------------------------------- the rows */

  const rows = useMemo<TaskRow[]>(() => {
    const out: TaskRow[] = [];

    // ONE row per piece of work. Two different backends can describe the same
    // job, and listing both would show the teacher the same correction twice:
    //   * a Classroom workflow job owns the correction job it drives (they
    //     share the submission), and
    //   * a batched long-document job is mirrored in the correction table under
    //     the SAME job id (the large-PDF record is the richer one, so it wins).
    const covered = new Set<string>();
    const batchedIds = new Set<string>();
    for (const a of automation) if (a.submission_id) covered.add(a.submission_id);
    for (const l of large?.jobs ?? []) {
      batchedIds.add(l.id);
      if (l.submission_id) covered.add(l.submission_id);
    }

    for (const job of jobs as Job[]) {
      if (batchedIds.has(job.id)) continue;
      if (job.submission_id && covered.has(job.submission_id)) continue;
      const bucket = bucketOf(job.status);
      const actions: TaskAction[] = [];
      if (bucket === "failed" || bucket === "cancelled") {
        actions.push({ label: "Retry", variant: "secondary", run: () => retryJob(job.id) });
      }
      if (bucket === "running" || bucket === "queued") {
        actions.push({ label: "Cancel", variant: "ghost", run: () => cancelJob(job.id) });
      }
      out.push({
        key: `job:${job.id}`,
        kind: "correction",
        title: job.coursework_id ? "Classroom correction (single-pass)" : "PDF correction (single-pass)",
        student: displayName(job, "") || null,
        assignment: null,
        status: job.status,
        statusLabel: human(job.status),
        bucket,
        stage: job.stage_detail,
        progress: job.progress > 0 ? job.progress : null,
        createdAt: job.created_at,
        finishedAt: job.finished_at,
        error: job.error,
        attempts: job.max_attempts ? `${job.attempts ?? 0}/${job.max_attempts}` : null,
        detail: [
          job.submission_id ? `submission ${job.submission_id.slice(0, 8)}…` : "",
          job.coursework_id ? `assignment ${job.coursework_id.slice(0, 8)}…` : "",
        ].filter(Boolean),
        actions,
        openHash: job.result_id ? `#/results/${job.result_id}` : undefined,
      });
    }

    for (const job of (large?.jobs ?? []) as LargePdfJob[]) {        const bucket = bucketOf(job.status);
      const progress = large?.progress?.[job.id];

      const pct = progress && progress.page_count > 0
        ? Math.min(100, Math.round(100 * progress.pages_done / progress.page_count))
        : (job.status === "COMPLETED" ? 100 : null);
      // Never print the same sentence twice: the large job's step detail is
      // often just its status label ("Correction completed").
      const rawStage = progress
        ? [
            progress.stage?.label,
            progress.current_batch && progress.batch_total
              ? `batch ${progress.current_batch}/${progress.batch_total}` : "",
            progress.current_batch_pages
              ? `pages ${progress.current_batch_pages.start}–${progress.current_batch_pages.end}` : "",
          ].filter(Boolean).join(" · ")
        : job.step_detail;
      const stage = rawStage && rawStage !== (job.status_label || "") ? rawStage : null;
      const actions: TaskAction[] = [];
      if (job.retryable) {
        actions.push({ label: "Retry", variant: "secondary", run: () => largePdfRetryJob(job.id) });
      }
      if (bucket === "running" || bucket === "queued") {
        actions.push({ label: "Cancel", variant: "ghost", run: () => largePdfCancel(job.id) });
      }
      out.push({
        key: `large:${job.id}`,
        kind: "large",
        title: "Large PDF correction (batched)",
        student: displayName(job, "") || job.student_name || null,
        assignment: null,
        status: job.status,
        statusLabel: job.status_label || human(job.status),
        bucket,
        stage,
        progress: pct,
        createdAt: job.created_at,
        finishedAt: job.finished_at,
        error: job.error,
        attempts: job.attempts ? `${job.attempts}` : null,
        detail: [
          job.page_count ? `${job.page_count} pages` : "",
          job.batch_count ? `${job.batch_count} batches` : "",
          progress?.questions_total
            ? `${progress.questions_marked}/${progress.questions_total} questions`
            : "",
        ].filter(Boolean),
        actions,
        openHash: job.result_id ? `#/results/${job.result_id}` : "#/largepdf",
      });
    }

    for (const job of automation) {
      const bucket = bucketOf(job.status);
      const actions: TaskAction[] = [];
      if (job.approvable) {
        actions.push({ label: "Approve grade", variant: "primary",
                       run: () => automationApprove(job.id) });
      }
      if (job.resumable || bucket === "failed") {
        actions.push({ label: job.retry_from ? `Retry from ${human(job.retry_from)}` : "Retry",
                       variant: "secondary", run: () => automationRetryJob(job.id) });
      }
      if (job.attachment?.retryable) {
        actions.push({ label: "Retry report upload", variant: "ghost",
                       run: () => automationRetryReport(job.id) });
      }
      const marks = job.marks_obtained != null && job.maximum_marks != null
        ? `${job.marks_obtained}/${job.maximum_marks}${job.percentage != null ? ` (${job.percentage}%)` : ""}`
        : null;
      out.push({
        key: `auto:${job.id}`,
        kind: "automation",
        title: "Classroom correction workflow",
        student: displayName(job, "") || job.student_name || null,
        assignment: job.assignment_title,
        status: job.status,
        statusLabel: job.status_label || human(job.status),
        bucket,
        stage: job.step_detail,
        // The workflow reports a stage, not a percentage — so it shows a stage.
        progress: null,
        createdAt: job.created_at,
        finishedAt: job.finished_at ?? job.completed_at,
        error: job.error,
        attempts: job.max_attempts ? `${job.attempts ?? 0}/${job.max_attempts}` : null,
        detail: [
          job.pipeline_label,
          marks,
          job.grade ? `grade ${job.grade}` : "",
          job.attachment ? `report: ${job.attachment.status}` : "",
        ].filter(Boolean) as string[],
        actions,
        openHash: job.result?.id ? `#/results/${job.result.id}`
          : job.coursework_id ? `#/assignments/${job.coursework_id}` : undefined,
      });
    }

    if (sync?.id) {
      const status = sync.running ? "RUNNING" : sync.error ? "FAILED" : "COMPLETED";
      out.push({
        key: "sync:current",
        kind: "sync",
        title: sync.kind === "assignment" ? "Classroom assignment sync" : "Google Classroom sync",
        student: null,
        assignment: sync.target?.coursework_id
          ? `${sync.target.course_name ?? ""} ${sync.target.coursework_title ?? ""}`.trim() || null
          : sync.target?.course_name ?? null,
        status,
        statusLabel: sync.running ? "Syncing" : sync.error ? "Failed" : "Completed",
        bucket: bucketOf(status),
        stage: sync.message ?? sync.stage,
        progress: sync.running && sync.progress > 0 ? sync.progress : null,
        createdAt: sync.started_at,
        finishedAt: sync.finished_at,
        error: sync.error,
        attempts: sync.event_count ? `${sync.event_count} events` : null,
        detail: [],
        actions: [],
        openHash: "#/classroom",
      });
    }

    // Newest first: the running work and the most recent history together.
    return out.sort((a, b) => (b.createdAt ?? 0) - (a.createdAt ?? 0));
  }, [jobs, large, automation, sync]);

  const kindCounts = useMemo(() => {
    const counts: Record<Kind, number> = { correction: 0, large: 0, automation: 0, sync: 0 };
    for (const row of rows) counts[row.kind] += 1;
    return counts;
  }, [rows]);

  const bucketCounts = useMemo(() => {
    const counts: Record<Bucket, number> = {
      running: 0, queued: 0, needsYou: 0, completed: 0, failed: 0, cancelled: 0 };
    for (const row of rows) counts[row.bucket] += 1;
    return counts;
  }, [rows]);

  const visible = useMemo(() => {
    const q = query.trim().toLowerCase();
    return rows.filter((row) => {
      if (kindFilter !== "all" && row.kind !== kindFilter) return false;
      if (bucketFilter !== "all" && row.bucket !== bucketFilter) return false;
      if (!q) return true;
      return [row.title, row.student, row.assignment, row.status, row.stage, row.error]
        .some((value) => (value ?? "").toLowerCase().includes(q));
    });
  }, [rows, kindFilter, bucketFilter, query]);

  const active = bucketCounts.running + bucketCounts.queued;
  const q = pool?.queue;

  return (
    <>
      <PageHeader title="Task Manager" icon="cpu"
        sub="Every correction, batched document, Classroom workflow and sync the application is running — from the backend's own records.">
        <div className="row wrap" style={{ gap: 8 }}>
          <Badge tone={active > 0 ? "info" : "mute"}>{active} active</Badge>
          {bucketCounts.needsYou > 0 && <Badge tone="warn">{bucketCounts.needsYou} need you</Badge>}
          {bucketCounts.failed > 0 && <Badge tone="err">{bucketCounts.failed} failed</Badge>}
        </div>
      </PageHeader>

      {/* ------------------------------------------------- running machinery */}
      <div className="grid grid-4" style={{ marginBottom: 14 }}>
        <Stat label={pool ? `Correction workers (cap ${pool.recommended})` : "Correction workers"}
          value={pool ? `${pool.active}/${pool.size}` : "—"} />
        <Stat label="Queue" value={q ? q.queued + q.starting : 0}
          tone={q && q.queued + q.starting > 0 ? "warn" : undefined} />
        <Stat label="Batched docs in flight"
          value={large ? large.counts.processing + large.counts.ready + large.counts.queued : 0} />
        <Stat label={automationState && !automationState.enabled
            ? "Tiny Agent (disabled in Settings)" : "Tiny Agent"}
          value={automationState ? (automationState.enabled ? human(automationState.worker_state) : "Off") : "—"}
          tone={automationState?.enabled ? "ok" : undefined} />
      </div>

      {/* ------------------------------------------------------- the task list */}
      <div className="card">
        <div className="row between wrap" style={{ gap: 10, marginBottom: 10 }}>
          <h3 style={{ margin: 0 }}>
            Tasks <span className="hint">{rows.length} record(s)</span>
          </h3>
          <div className="row wrap" style={{ gap: 8, alignItems: "center" }}>
            <input className="input" style={{ maxWidth: 240 }} type="search"
              placeholder="Student, assignment, stage…"
              aria-label="Search tasks"
              value={query} onChange={(e) => setQuery(e.target.value)} />
            <Button size="sm" variant="ghost" icon="refresh"
              onClick={() => { reloadJobs(); loadLarge(); loadAutomation(); loadSync(); loadPool(); }}>
              Refresh
            </Button>
          </div>
        </div>

        <div className="chip-row" style={{ marginBottom: 6 }}>
          <button className={`chip ${kindFilter === "all" ? "active" : ""}`}
            onClick={() => setKindFilter("all")}>
            All types<span className="chip-count">{rows.length}</span>
          </button>
          {(Object.keys(KIND_LABEL) as Kind[]).map((kind) => {
            const count = kindCounts[kind];
            if (count === 0) return null;
            return (
              <button key={kind} className={`chip ${kindFilter === kind ? "active" : ""}`}
                onClick={() => setKindFilter(kind)}>
                {KIND_LABEL[kind]}<span className="chip-count">{count}</span>
              </button>
            );
          })}
        </div>

        <div className="chip-row" style={{ marginBottom: 12 }}>
          <button className={`chip ${bucketFilter === "all" ? "active" : ""}`}
            onClick={() => setBucketFilter("all")}>Any state</button>
          {(["running", "queued", "needsYou", "completed", "failed", "cancelled"] as Bucket[])
            .map((bucket) => {
              const count = bucketCounts[bucket];
              if (count === 0) return null;
              return (
                <button key={bucket}
                  className={`chip ${bucketFilter === bucket ? "active" : ""}`}
                  onClick={() => setBucketFilter(bucket)}>
                  {BUCKET_LABEL[bucket]}<span className="chip-count">{count}</span>
                </button>
              );
            })}
        </div>

        {visible.length === 0 ? (
          <Empty icon="🗂" title={rows.length === 0 ? "No tasks yet" : "Nothing matches this filter"}
            hint={rows.length === 0
              ? "Corrections, batched documents, Classroom workflows and syncs appear here as they run."
              : "Clear the search or pick another state."} />
        ) : (
          <div style={{ maxHeight: "62vh", overflowY: "auto" }}>
            {visible.map((row) => (
              <div key={row.key} className="stack"
                style={{ gap: 6, padding: "12px 0",
                         borderBottom: "1px solid rgba(255,255,255,0.05)" }}>
                <div className="row between wrap" style={{ gap: 8 }}>
                  <div className="row wrap" style={{ gap: 8, alignItems: "center" }}>
                    <Badge tone="mute">{KIND_LABEL[row.kind]}</Badge>
                    <strong>{row.title}</strong>
                    {row.student && <span className="hint">{row.student}</span>}
                    {row.assignment && <span className="hint">· {row.assignment}</span>}
                  </div>
                  <Badge tone={bucketTone(row.bucket)}>{row.statusLabel}</Badge>
                </div>

                {/* A progress bar only when the backend reported a real number. */}
                {row.progress != null ? (
                  <Progress value={row.progress} thin
                    label={`${row.title}${row.student ? ` — ${row.student}` : ""}`} />
                ) : (
                  row.stage != null && (
                    <div className="hint small">{row.stage}</div>
                  )
                )}

                <div className="row between wrap small muted" style={{ gap: 8 }}>
                  <span>
                    {row.stage && row.progress != null ? `${row.stage} · ` : ""}
                    {row.error ? `— ${row.error}` : ""}
                  </span>
                  <span className="faint mono">
                    {row.attempts ? `${row.attempts} · ` : ""}
                    {row.createdAt ? `started ${timeAgo(row.createdAt)}` : ""}
                    {row.createdAt ? ` · ${elapsed(row.createdAt, row.finishedAt)}` : ""}
                  </span>
                </div>

                {row.detail.length > 0 && (
                  <div className="faint small">{row.detail.join(" · ")}</div>
                )}

                {(row.actions.length > 0 || row.openHash) && (
                  <div className="row wrap" style={{ gap: 8 }}>
                    {row.actions.map((action) => (
                      <Button key={action.label} size="sm"
                        variant={action.variant ?? "secondary"}
                        loading={busy === row.key}
                        onClick={() => act(row, action)}>
                        {action.label}
                      </Button>
                    ))}
                    {row.openHash && (
                      <Button size="sm" variant="ghost" icon="arrow-right"
                        onClick={() => onNavigate(row.openHash as string)}>
                        {row.kind === "automation" ? "Open assignment" : "Open result"}
                      </Button>
                    )}
                  </div>
                )}
              </div>
            ))}
          </div>
        )}
      </div>

      <p className="faint small" style={{ textAlign: "center" }}>
        Stages and percentages come from the workers and the job records themselves;
        an operation that reports only a stage shows its stage. Worker settings live
        on the AI page.
      </p>
    </>
  );
}
