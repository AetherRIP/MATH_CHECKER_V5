/**
 * Real-time automation visualizer.
 *
 * A node graph that mirrors the ACTUAL workflow state machine, driven by the
 * same job data the page already polls. There is no second polling loop and no
 * fabricated animation: every node's status is derived from real job states
 * (the persisted `status` + `stage_index` the backend already computes), and
 * activity only pulses along the connector when a real job is genuinely at
 * that stage.
 *
 *   idle      calm, muted
 *   completed marked with a check, preserved
 *   active    highlighted + pulsing, activity moves toward the next stage
 *   failed    boldly marked with the failure category
 */

import { useMemo } from "react";
import { Icon, type IconName } from "./Icons";
import { Badge } from "./components";
import type { AutomationJob } from "./api";

/** Failure / needs-attention states: the job stopped at its resume stage. */
const FAILED_STATES = new Set([
  "CORRECTION_FAILED", "REPORT_FAILED", "REPORT_UPLOAD_FAILED",
  "ATTACHMENT_FAILED", "GRADE_FAILED", "GRADE_VERIFICATION_FAILED",
  "RETURN_FAILED", "ATTENTION_REQUIRED", "BLOCKED_NOT_MODIFIABLE",
]);

/** States that are finished and will never advance again. */
const WAITING_STATES = new Set(["NEW"]);

type NodeStatus = "idle" | "completed" | "active" | "failed";

const NODE_ICON: Record<string, IconName> = {
  NEW: "activity",
  CORRECTING: "wand",
  RESULT_VALIDATED: "chart",
  REPORT_GENERATED: "file",
  REPORT_VALIDATED: "check",
  REPORT_UPLOADED: "cloud",
  REPORT_ATTACHED: "paperclip",
  ATTACHMENT_VERIFIED: "check-circle",
  GRADE_DRAFTED: "tag",
  GRADE_VERIFIED: "check-circle",
  RETURNING: "send",
  RETURNED: "send",
  COMPLETE: "check-circle",
};

// Which stage a FAILED job should be pinned to, when the backend has not
// already produced a resume index for it (defensive fallback only).
const RESUME_HINT: Record<string, string> = {
  CORRECTION_FAILED: "CORRECTING",
  REPORT_FAILED: "REPORT_GENERATED",
  REPORT_UPLOAD_FAILED: "REPORT_VALIDATED",
  ATTACHMENT_FAILED: "REPORT_VALIDATED",
  GRADE_FAILED: "ATTACHMENT_VERIFIED",
  GRADE_VERIFICATION_FAILED: "GRADE_DRAFTED",
  RETURN_FAILED: "GRADE_VERIFIED",
};

const STATUS_ORDER: Record<NodeStatus, number> = {
  idle: 0, completed: 1, active: 2, failed: 3,
};

export interface StageNodeInput { state: string; label: string }

interface NodeView {
  state: string;
  label: string;
  status: NodeStatus;
  jobs: AutomationJob[];
  failedJobs: AutomationJob[];
}

function statusIndex(job: AutomationJob, pipeline: StageNodeInput[]): number {
  const byState = pipeline.findIndex((p) => p.state === job.status);
  if (byState >= 0) return byState;            // failed states are already mapped
  const hint = RESUME_HINT[job.status];
  if (hint) {
    const h = pipeline.findIndex((p) => p.state === hint);
    if (h >= 0) return h;
  }
  const idx = job.stage_index;
  if (typeof idx === "number" && idx >= 0) return Math.min(idx, pipeline.length - 1);
  return 0;
}

/** Derive one visual node per pipeline stage from the REAL jobs. */
export function computeNodes(pipeline: StageNodeInput[],
                             jobs: AutomationJob[]): NodeView[] {
  const nodes: NodeView[] = pipeline.map((p) => ({
    state: p.state, label: p.label, status: "idle", jobs: [], failedJobs: [],
  }));
  if (pipeline.length === 0) return nodes;
  const last = pipeline.length - 1;

  const raise = (n: NodeView, s: NodeStatus) => {
    if (STATUS_ORDER[s] > STATUS_ORDER[n.status]) n.status = s;
  };

  for (const job of jobs) {
    if (job.status === "CANCELLED") {
      const at = statusIndex(job, pipeline);
      for (let i = 0; i < at; i++) raise(nodes[i], "completed");
      continue;
    }
    if (job.status === "COMPLETE") {
      for (let i = 0; i <= last; i++) raise(nodes[i], "completed");
      continue;
    }
    const at = Math.max(0, Math.min(statusIndex(job, pipeline), last));
    for (let i = 0; i < at; i++) raise(nodes[i], "completed");
    const here = nodes[at];
    here.jobs.push(job);
    if (FAILED_STATES.has(job.status)) {
      here.failedJobs.push(job);
      raise(here, "failed");
    } else {
      raise(here, "active");
    }
  }
  return nodes;
}

/** Human label for a failure category (error_code preferred). */
export function failureCategory(job: AutomationJob): string {
  const code = (job.error_code || job.status || "").replace(/_/g, " ").trim();
  return code || "Unknown error";
}

/* ------------------------------------------------------------------ nodes */

function FlowNodeRow({ node, onOpenJob, last }: {
  node: NodeView;
  onOpenJob: (job: AutomationJob) => void;
  last: boolean;
}) {
  const icon = NODE_ICON[node.state] ?? "activity";
  const interactive = node.jobs.length + node.failedJobs.length > 0;
  return (
    <div className={`flow-stage ${node.status} ${interactive ? "interactive" : ""}`}>
      <button
        type="button"
        className="flow-node"
        disabled={!interactive}
        onClick={() => {
          const job = node.failedJobs[0] ?? node.jobs[0];
          if (job) onOpenJob(job);
        }}
        aria-label={`${node.label} — ${node.status}${
          node.jobs.length
            ? `, ${node.jobs.length} ${node.jobs.length === 1 ? "job" : "jobs"}`
            : ""}`}
      >
        <span className="ring" aria-hidden="true" />
        <Icon name={icon} size={17} />
      </button>
      <div className="flow-meta">
        <span className="flow-label">{node.label}</span>
        {node.status === "failed" && node.failedJobs[0] && (
          <span className="flow-fail" title={node.failedJobs[0].error || undefined}>
            {failureCategory(node.failedJobs[0])}
          </span>
        )}
        {node.jobs.length > 0 && (
          <span className="flow-count">{node.jobs.length} active</span>
        )}
      </div>
      {!last && (
        <div className={`flow-link ${node.status === "active" ? "flowing" : ""}`}
          aria-hidden="true">
          <span className="spark" />
        </div>
      )}
    </div>
  );
}

/* -------------------------------------------------------------- the graph */

export function AutomationFlow({ pipeline, jobs, source, onOpenJob }: {
  pipeline: StageNodeInput[];
  jobs: AutomationJob[];
  source: { label: string; sub?: string; live?: boolean };
  onOpenJob: (job: AutomationJob) => void;
}) {
  const nodes = useMemo(() => computeNodes(pipeline, jobs), [pipeline, jobs]);
  const activeTotal = nodes.reduce((n, x) => n + x.jobs.length, 0);

  return (
    <div className="flow-card">
      <div className="flow-card-head">
        <h3 style={{ margin: 0 }}>Live pipeline</h3>
        <span className={`flow-live ${activeTotal > 0 ? "on" : ""}`}>
          <span className="pulse" aria-hidden="true" />
          {activeTotal > 0 ? `${activeTotal} in flight` : "idle"}
        </span>
      </div>

      <div className="flow-source">
        <div className="flow-node source">
          <Icon name="classroom" size={17} />
        </div>
        <div className="flow-meta">
          <span className="flow-label">{source.label}</span>
          {source.sub && <span className="faint small">{source.sub}</span>}
        </div>
      </div>
      <div className="flow-link source-link" aria-hidden="true">
        <span className={`spark ${source.live ? "on" : ""}`} />
      </div>

      <div className="flow-stages">
        {nodes.map((n, i) => (
          <FlowNodeRow key={n.state} node={n} last={i === nodes.length - 1}
            onOpenJob={onOpenJob} />
        ))}
      </div>
    </div>
  );
}

/* ------------------------------------------------------- live job cards */

const PENDING_STAGE_STATES = new Set([
  "NEW", "CORRECTING", "RESULT_VALIDATED", "REPORT_GENERATED",
  "REPORT_VALIDATED", "REPORT_UPLOADED", "REPORT_ATTACHED",
  "ATTACHMENT_VERIFIED", "GRADE_DRAFTED", "GRADE_VERIFIED", "RETURNING",
  "CLEANUP_PENDING",
]);

export function isActiveJob(job: AutomationJob): boolean {
  return PENDING_STAGE_STATES.has(job.status);
}

function StageBar({ job, total }: { job: AutomationJob; total: number }) {
  // Stage POSITION, never a fabricated time-percentage.
  const pos = Math.max(0, Math.min(job.stage_index, Math.max(1, total - 1)));
  const pct = total > 1 ? Math.round((pos / (total - 1)) * 100) : 0;
  return (
    <div className="stage-bar" aria-hidden="true">
      <span style={{ width: `${pct}%` }} />
    </div>
  );
}

export function LiveJobs({ jobs, totalStages, onOpenJob }: {
  jobs: AutomationJob[];
  totalStages: number;
  onOpenJob: (job: AutomationJob) => void;
}) {
  const active = jobs.filter(isActiveJob);
  const waiting = jobs.filter((j) => WAITING_STATES.has(j.status));
  // A job can be both active and waiting (RETRYING); one row per job.
  const shown = [...new Map([...active, ...waiting].map((j) => [j.id, j])).values()];
  if (shown.length === 0) {
    return (
      <div className="flow-card live-empty">
        <h3 style={{ margin: 0 }}>Live students</h3>
        <p className="faint small" style={{ marginTop: 6 }}>
          Nothing is being corrected right now. Jobs appear here the moment a
          submission is detected.
        </p>
      </div>
    );
  }
  return (
    <div className="flow-card">
      <div className="flow-card-head">
        <h3 style={{ margin: 0 }}>Live students</h3>
        <span className="faint small">{shown.length} shown</span>
      </div>
      <div className="live-jobs">
        {shown.map((j) => (
          <button key={j.id} type="button" className="live-job"
            onClick={() => onOpenJob(j)}>
            <div className="lj-top">
              <span className="lj-name">{j.student_name || "Student"}</span>
              <span className="lj-stage">{j.status_label}</span>
            </div>
            <div className="lj-sub faint small">
              {j.assignment_title || "—"}
              {j.marks_obtained != null && (
                <> · {j.marks_obtained}/{j.maximum_marks}</>
              )}
            </div>
            {/* §8: say when the LARGE-PDF pipeline is doing the work. The value
                comes from the real batched-job rows, never a guess. */}
            {j.pipeline && (
              <div className="lj-sub small" style={{ marginTop: 2 }}>
                <Badge tone={j.pipeline === "batched" ? "info" : "mute"}>
                  {j.pipeline_label ?? (j.pipeline === "batched"
                    ? "Batched large-PDF pipeline"
                    : "Standard single-pass pipeline")}
                </Badge>
              </div>
            )}
            {!WAITING_STATES.has(j.status) && (
              <StageBar job={j} total={totalStages} />
            )}
          </button>
        ))}
      </div>
    </div>
  );
}

/* ------------------------------------------------------------ system panel */

export function SystemPanel({ counts, enabled, running }: {
  counts: { total: number; completed: number; processing: number;
            waiting: number; failed: number };
  enabled: boolean;
  running: boolean;
}) {
  const rows: [string, number, string?][] = [
    ["Active jobs", counts.processing],
    ["Waiting", counts.waiting],
    ["Completed", counts.completed, "ok"],
    ["Failed", counts.failed, counts.failed ? "err" : undefined],
  ];
  return (
    <div className="flow-card sys-panel">
      <h3 style={{ margin: 0 }}>System activity</h3>
      <div className="sys-grid">
        {rows.map(([label, value, tone]) => (
          <div className="sys-cell" key={label}>
            <span className={`sys-value ${tone ?? ""}`}>{value}</span>
            <span className="sys-label">{label}</span>
          </div>
        ))}
      </div>
      <div className="sys-status">
        <span className="dot-row">
          <span className={`led ${enabled ? (running ? "ok" : "warn") : "off"}`} />
          Automation {enabled ? (running ? "running" : "enabled, idle") : "off"}
        </span>
      </div>
    </div>
  );
}
