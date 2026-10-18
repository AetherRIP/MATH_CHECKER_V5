import { useEffect, useMemo, useRef, useState } from "react";
import {
  api, displayName, elapsed, timeAgo, ACTIVE_STATUSES,
  type AssignmentOverview, type Dashboard, type Job, type UsageState,
} from "../api";
import {
  Badge, Button, Empty, Icon, Progress, StatusBadge,
} from "../components";
import { AttentionPanel } from "../attention";
import { BarChart, ChartEmpty, Donut, Gauge, LineChart, type ChartTone } from "../charts/Charts";
import { formatDayLabel } from "../charts/format";
import { useCountUp } from "../motion";
import type { Route } from "../App";

/** Real-data aggregates behind the dashboard charts (§10, chart #1). */
interface ActivityDay {
  day: string; results: number; needs_review: number; manual_overrides: number;
  jobs_started: number; jobs_failed: number;
  avg_percentage: number | null; avg_processing_seconds: number | null;
}
interface ActivityFeed {
  days: ActivityDay[]; window_days: number;
  totals: { results: number; needs_review: number; manual_overrides: number;
    jobs_started: number; jobs_failed: number };
}

const JOB_TONES: Record<string, ChartTone> = {
  QUEUED: "mute", PREPARING: "accent", DOWNLOADING: "accent",
  UPLOADING: "accent", MARKING: "accent", VERIFYING: "accent",
  SAVING: "accent", WAITING_FOR_API: "warn", RETRYING: "warn",
  COMPLETED: "ok", FAILED: "err", CANCELLED: "mute",
};

/**
 * Dashboard (§9): an editorial layout, not a card wall.
 *
 * Row 1  hero band — today's throughput, count-up on first load.
 * Row 2  marking queue (left) | what needs attention (right).
 * Row 3  charts — throughput over time (real /analytics/activity data) and
 *        the current pipeline distribution. Operational stats are visually
 *        separate from academic performance and never mixed.
 * Row 4  live job activity (SSE + poll fallback) | system health.
 * Row 5  class/assignment comparison from real assignment aggregates.
 *
 * Nothing is estimated or mocked: every number comes from the API fields
 * named beside it, and charts with no data say so instead of drawing a lie.
 */
export default function DashboardPage({ onNavigate, data }: {
  onNavigate: (r: Route) => void;
  data: Dashboard | null;
}) {
  const [assignments, setAssignments] = useState<AssignmentOverview[] | null>(null);
  const [liveJobs, setLiveJobs] = useState<Job[]>([]);
  const [activity, setActivity] = useState<ActivityFeed | null>(null);
  const [usage, setUsage] = useState<UsageState | null>(null);
  const sourceRef = useRef<EventSource | null>(null);

  const correctionsToday = useCountUp(data?.corrections_today ?? 0);

  useEffect(() => {
    const loadAssignments = () => api<{ assignments: AssignmentOverview[] }>("/assignments")
      .then((r) => setAssignments(r.assignments)).catch(() => {});
    loadAssignments();
    const timer = setInterval(loadAssignments, 15_000);

    const loadActivity = () => api<ActivityFeed>("/analytics/activity?days=30")
      .then(setActivity).catch(() => {});
    loadActivity();
    const activityTimer = setInterval(loadActivity, 120_000);

    const loadUsage = () => api<UsageState>("/usage").then(setUsage).catch(() => {});
    loadUsage();
    const usageTimer = setInterval(loadUsage, 60_000);

    const source = new EventSource("/api/jobs/stream");
    sourceRef.current = source;
    let streamAlive = false;
    source.onmessage = (event) => {
      streamAlive = true;
      const payload = JSON.parse(event.data);
      if (payload.type === "hello") {
        api<{ jobs: Job[] }>("/jobs?limit=20")
          .then((r) => setLiveJobs(r.jobs)).catch(() => {});
        return;
      }
      if (payload.type === "job" && payload.job) {
        const job = payload.job as Job;
        setLiveJobs((jobs) => {
          const next = jobs.filter((j) => j.id !== job.id);
          next.unshift(job);
          return next.slice(0, 20);
        });
      }
    };
    source.onerror = () => { streamAlive = false; };
    // Public tunnels that buffer SSE (§14) leave the stream silent forever;
    // fall back to polling so the live activity list still updates remotely.
    const poll = setInterval(() => {
      if (streamAlive) return;
      api<{ jobs: Job[] }>("/jobs?limit=20")
        .then((r) => setLiveJobs(r.jobs)).catch(() => {});
    }, 6_000);
    return () => {
      clearInterval(timer); clearInterval(activityTimer);
      clearInterval(usageTimer); clearInterval(poll); source.close();
    };
  }, []);

  const agg = useMemo(() => {
    const list = assignments ?? [];
    const sum = (f: (a: AssignmentOverview) => number) => list.reduce((n, a) => n + f(a), 0);
    const checked = sum((a) => a.checked);
    const weighted = list.reduce((n, a) => n + (a.average ?? 0) * a.checked, 0);
    return {
      count: list.length,
      missing: sum((a) => a.missing),
      submittedNotChecked: Math.max(0, sum((a) => a.submitted) - checked),
      checked,
      students: sum((a) => a.students_total),
      review: sum((a) => a.needs_review),
      failed: sum((a) => a.failed),
      processing: sum((a) => a.processing),
      average: checked > 0 ? weighted / checked : null,
    };
  }, [assignments]);

  const active = liveJobs.filter((j) => ACTIVE_STATUSES.includes(j.status));

  // Marking queue: the assignments that still have work, most urgent first.
  // Everything shown here is a real count from /assignments — nothing is
  // estimated, and an assignment with nothing outstanding is not listed.
  const queue = useMemo(() => {
    const list = assignments ?? [];
    const outstanding = (a: AssignmentOverview) =>
      Math.max(0, a.submitted - a.checked) + a.needs_review + a.failed + a.processing;
    const rank = (a: AssignmentOverview) =>
      (a.processing > 0 ? 0 : 1) * 1000 + (a.failed + a.needs_review) * 10 +
      Math.max(0, a.submitted - a.checked);
    return list
      .filter((a) => outstanding(a) > 0)
      .sort((a, b) => rank(a) - rank(b))
      .slice(0, 6)
      .map((a) => ({
        a,
        todo: Math.max(0, a.submitted - a.checked),
        done: a.students_total > 0 ? Math.min(1, a.checked / a.students_total) : 0,
      }));
  }, [assignments]);

  // Pipeline distribution for the donut: the CURRENT job state, operational
  // (never mixed into the academic charts).
  const pipeline = useMemo(() => {
    const jobs = data?.jobs ?? {};
    const entries = Object.entries(jobs).filter(([, n]) => n > 0);
    return entries.map(([status, n]) => ({
      label: status.replace(/_/g, " "),
      value: n,
      tone: JOB_TONES[status] ?? "mute",
    }));
  }, [data]);

  if (!data) {
    return (
      <div className="skeleton-lines" aria-busy="true">
        <div className="skeleton" />
        <div className="skeleton" style={{ height: 120 }} />
        <div className="skeleton" style={{ height: 180 }} />
      </div>
    );
  }

  const keyOk = data.api_health.has_keys && data.api_health.healthy > 0;

  return (
    <>
      <div className="row between wrap" style={{ marginBottom: 18, gap: 12 }}>
        <div>
          <h1 className="page-title" style={{ marginBottom: 2 }}>Dashboard</h1>
          <p className="page-sub" style={{ marginBottom: 0 }}>
            {data.mock_mode
              ? "Mock mode active — AI calls are simulated"
              : "Your classes, corrections and Classroom at a glance"}
          </p>
        </div>
        <div className="row wrap">
          <Button variant="ghost" icon="alert" onClick={() => onNavigate("review")}>
            Review queue
          </Button>
          <Button variant="secondary" icon="sync" onClick={() => onNavigate("classroom")}>
            Sync Classroom
          </Button>
          <Button variant="primary" icon="plus" onClick={() => onNavigate("assignments")}>
            New Assignment
          </Button>
        </div>
      </div>

      {/* ---- Row 1: today's throughput (hero band) ---- */}
      <div className="hero-band enter" style={{ marginBottom: 18 }}>
        <div>
          <div className="hero-label">Corrections completed today</div>
          <div className="hero-value">{Math.round(correctionsToday)}</div>
          <div className="faint small" style={{ marginTop: 2 }}>
            counted from finished correction jobs
          </div>
        </div>
        <div className="hero-side">
          <div className="hero-stat">
            <div className="n">{data.pending}</div>
            <div className="l">Pending</div>
          </div>
          <div className="hero-stat">
            <div className="n" style={{ color: data.processing > 0 ? "var(--accent)" : undefined }}>
              {data.processing}
            </div>
            <div className="l">Processing</div>
          </div>
          <div className="hero-stat">
            <div className="n" style={{ color: "var(--ok)" }}>{data.completed}</div>
            <div className="l">Completed</div>
          </div>
          <div className="hero-stat">
            <div className="n" style={{ color: data.failed > 0 ? "var(--err)" : undefined }}>
              {data.failed}
            </div>
            <div className="l">Failed</div>
          </div>
        </div>
      </div>

      {/* ---- Row 2: marking queue (7) | attention (5) ---- */}
      <div className="grid enter-stagger"
        style={{ gridTemplateColumns: "minmax(0, 7fr) minmax(0, 5fr)", marginBottom: 18 }}>
        <div className="card">
          <div className="row between wrap">
            <h3 style={{ margin: 0 }}>
              <Icon name="inbox" size={16} style={{ color: "var(--accent)" }} />
              Marking queue
              {queue.length > 0 && <span className="hint">{queue.length} assignment(s) with work outstanding</span>}
            </h3>
            <Button size="sm" variant="ghost" icon="arrow-right"
              onClick={() => onNavigate("assignments")}>
              All assignments
            </Button>
          </div>

          {assignments === null ? (
            <div className="skeleton-lines" style={{ marginTop: 12 }} aria-busy="true">
              <div className="skeleton" style={{ height: 44 }} />
              <div className="skeleton" style={{ height: 44 }} />
            </div>
          ) : queue.length === 0 ? (
            <Empty icon="check-circle" art="check" title="Nothing waiting to be marked"
              hint="Everything tracked in Assignments is marked — new work appears here as it syncs from Google Classroom." />
          ) : (
            <div className="stack" style={{ marginTop: 12 }}>
              {queue.map(({ a, todo, done }) => (
                <button key={a.id} type="button"
                  className="attention-item clickable"
                  style={{ width: "100%", textAlign: "left", font: "inherit", color: "inherit" }}
                  onClick={() => { window.location.hash = `#/assignments/${a.id}`; }}>
                  <span className="col" style={{ flex: 1, minWidth: 0, gap: 6 }}>
                    <span className="row between wrap" style={{ gap: 8 }}>
                      <span className="ai-title" style={{ minWidth: 0, overflowWrap: "anywhere" }}>
                        {a.title}
                      </span>
                      <span className="row" style={{ gap: 6 }}>
                        {a.processing > 0 && <Badge tone="info" icon="activity">{a.processing} processing</Badge>}
                        {a.failed > 0 && <Badge tone="err" icon="alert">{a.failed} failed</Badge>}
                        {a.needs_review > 0 && <Badge tone="review" icon="review">{a.needs_review} review</Badge>}
                        {todo > 0 && <Badge tone="mute" icon="inbox">{todo} to mark</Badge>}
                      </span>
                    </span>
                    <Progress value={done} thin />
                    <span className="row between wrap small faint" style={{ gap: 8 }}>
                      <span>{a.course_name ?? "No class"} · {a.checked}/{a.students_total} checked</span>
                      {a.due_date ? <span>due {a.due_date}</span> : null}
                    </span>
                  </span>
                  <Icon name="chevron-right" size={16} style={{ marginTop: 2, color: "var(--text-faint)" }} />
                </button>
              ))}
            </div>
          )}
        </div>

        <div className="card">
          <div className="row between" style={{ marginBottom: 12 }}>
            <h3 style={{ margin: 0 }}>
              <Icon name="alert" size={16} style={{ color: "var(--warn)" }} />
              What needs my attention?
            </h3>
            <Button size="sm" variant="ghost" onClick={() => onNavigate("review")}>
              Review queue{agg.review > 0 ? ` (${agg.review})` : ""}
            </Button>
          </div>
          <AttentionPanel compact onNavigate={(hash) => { window.location.hash = hash; }} />
        </div>
      </div>

      {/* ---- Row 3: throughput chart (academic) | pipeline (operational) ---- */}
      <div className="grid grid-2 enter-stagger"
        style={{ marginBottom: 18, alignItems: "start" }}>
        <div className="card">
          <h3>
            <Icon name="trending" size={16} style={{ color: "var(--accent)" }} />
            Marking throughput <span className="hint">papers corrected per day, last 30 days</span>
          </h3>
          {activity == null ? (
            <div className="skeleton" style={{ height: 170 }} aria-busy="true" />
          ) : activity.totals.results === 0 ? (
            <ChartEmpty>No papers have been corrected in the last 30 days.</ChartEmpty>
          ) : (
            <LineChart
              title="Papers corrected per day"
              hint={`${activity.totals.results} in 30 days`}
              summary={`Papers corrected per day over the last 30 days: ${activity.totals.results} total.`}
              points={activity.days.map((d) => ({
                label: formatDayLabel(d.day), value: d.results,
              }))}
            />
          )}
          {activity && activity.totals.needs_review > 0 && (
            <div className="notice review small" style={{ marginTop: 10 }}>
              <Icon name="review" size={15} />
              <span>{activity.totals.needs_review} of those results are waiting for a teacher decision.</span>
            </div>
          )}
        </div>

        <div className="card">
          <h3>
            <Icon name="cpu" size={16} style={{ color: "var(--accent)" }} />
            Pipeline right now <span className="hint">operational job state</span>
          </h3>
          {pipeline.length === 0 ? (
            <ChartEmpty>No jobs recorded yet.</ChartEmpty>
          ) : (
            <Donut
              title="Job states"
              summary={`Current job states: ${pipeline.map((p) => `${p.label} ${p.value}`).join(", ")}.`}
              slices={pipeline}
              centerLabel={String(pipeline.reduce((n, p) => n + p.value, 0))}
            />
          )}
          <div className="row wrap" style={{ marginTop: 10, gap: 8 }}>
            <Button size="sm" variant="ghost" icon="cpu" onClick={() => onNavigate("tasks")}>
              Task Manager
            </Button>
            <Button size="sm" variant="ghost" icon="wand" onClick={() => onNavigate("correct")}>
              Correct a PDF
            </Button>
          </div>
        </div>
      </div>

      {/* ---- Row 4: live activity | system health ---- */}
      <div className="grid grid-2 enter-stagger"
        style={{ marginBottom: 18, alignItems: "start" }}>
        <div className="card">
          <h3>
            <Icon name="activity" size={16} style={{ color: "var(--accent)" }} />
            Live job activity <span className="hint">updates as work progresses</span>
          </h3>
          {active.length === 0 ? (
            <Empty icon="wand" art="inbox" title="No active corrections"
              hint="Started jobs appear here in real time." />
          ) : (
            <div className="stack">
              {active.slice(0, 5).map((job) => (
                <div key={job.id} className="stack" style={{ gap: 6 }}>
                  <div className="row between">
                    <span style={{ fontWeight: 650 }}>{displayName(job)}</span>
                    <StatusBadge status={job.status} />
                  </div>
                  <Progress value={job.progress} thin
                    label={`Correcting ${displayName(job)}`} />
                  <div className="row between small muted">
                    <span>{job.stage_detail ?? job.status}</span>
                    <span className="faint mono">
                      {elapsed(job.created_at, job.finished_at)} · attempt {job.attempts}/{job.max_attempts}
                    </span>
                  </div>
                </div>
              ))}
            </div>
          )}
          {agg.failed > 0 && (
            <div className="notice err small" style={{ marginTop: 12 }}>
              <Icon name="alert" size={15} />
              <span>{agg.failed} correction(s) failed. </span>
              <button className="btn ghost sm" onClick={() => onNavigate("tasks")}>
                Review failures
              </button>
            </div>
          )}
        </div>

        <div className="card">
          <div className="row between wrap">
            <h3 style={{ margin: 0 }}>
              <Icon name="cpu" size={16} style={{ color: "var(--accent)" }} />
              System health
            </h3>
            <div className="row wrap">
              <Button size="sm" variant="ghost" icon="key" onClick={() => onNavigate("ai")}>
                Manage keys
              </Button>
              <Button size="sm" variant="ghost" icon="gear" onClick={() => onNavigate("settings")}>
                Diagnostics
              </Button>
            </div>
          </div>
          <div className="grid grid-2" style={{ marginTop: 12, gap: 10 }}>
            <div className="stat compact">
              <div className="label">Classroom</div>
              <div className="value" style={{ fontSize: "var(--fs-lg)" }}>
                <Badge tone={data.classroom.connected ? "ok" : data.classroom.configured ? "warn" : "err"}
                  icon="classroom">
                  {data.classroom.connected ? "Connected"
                    : data.classroom.configured ? "Offline" : "Not set up"}
                </Badge>
              </div>
              <div className="faint small" style={{ marginTop: 4 }}>
                Last sync {timeAgo(data.classroom.last_sync)}
              </div>
            </div>
            <div className="stat compact">
              <div className="label">AI keys healthy</div>
              <div className={`value ${keyOk ? "ok" : data.api_health.has_keys ? "err" : "warn"}`}
                style={{ fontSize: "var(--fs-lg)" }}>
                {data.api_health.has_keys ? `${data.api_health.healthy}/${data.api_health.total}` : "—"}
              </div>
              <div className="faint small" style={{ marginTop: 4 }}>
                {!data.api_health.has_keys ? "no keys added yet"
                  : keyOk ? "ready to correct" : "needs attention"}
              </div>
            </div>
          </div>
          {usage && (
            <div className="stack" style={{ marginTop: 12 }}>
              <Gauge label="Internet usage — this hour" used={usage.hour.total}
                cap={usage.limits.hourly_bytes}
                detail={usage.limits.hourly_mb > 0
                  ? `${(usage.hour.total / 1_048_576).toFixed(1)} / ${usage.limits.hourly_mb} MB`
                  : "no limit"} />
              <Gauge label="Internet usage — today" used={usage.day.total}
                cap={usage.limits.daily_bytes}
                detail={usage.limits.daily_mb > 0
                  ? `${(usage.day.total / 1_048_576).toFixed(1)} / ${usage.limits.daily_mb} MB`
                  : "no limit"} />
            </div>
          )}
        </div>
      </div>

      {/* ---- Row 5: academic comparison (real /assignments aggregates) ---- */}
      <div className="card">
        <h3>
          <Icon name="chart" size={16} style={{ color: "var(--accent)" }} />
          Class performance <span className="hint">average Math Checker percentage per assignment</span>
        </h3>
        {assignments === null ? (
          <div className="skeleton" style={{ height: 170 }} aria-busy="true" />
        ) : agg.checked === 0 ? (
          <ChartEmpty>Correct some work and assignment averages appear here.</ChartEmpty>
        ) : (
          <BarChart
            title="Average percentage per assignment"
            hint="only assignments with corrected work"
            summary={`Average Math Checker percentage per assignment: ${
              assignments.filter((a) => a.checked > 0)
                .map((a) => `${a.title} ${Math.round(a.average ?? 0)}%`).join(", ")}.`}
            valueFormat={(v) => `${Math.round(v)}%`}
            bars={assignments.filter((a) => a.checked > 0).slice(0, 8).map((a) => ({
              label: a.title.length > 14 ? `${a.title.slice(0, 13)}…` : a.title,
              value: a.average ?? 0,
            }))}
          />
        )}
      </div>
    </>
  );
}
