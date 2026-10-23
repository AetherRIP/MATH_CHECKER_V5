import { useEffect, useMemo, useRef, useState } from "react";
import {
  api, displayName, elapsed, timeAgo, ACTIVE_STATUSES,
  type AssignmentOverview, type Dashboard, type Job, type UsageState,
} from "../api";
import {
  Badge, Button, Empty, Icon, Metric, PageHeader, Progress, StatusBadge,
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
 * The Desk (blueprint §9): the day's work, read as a ledger.
 *
 * Row 1  the day in numbers — corrected, waiting, to decide, failed.
 * Row 2  waiting on you (left) | what needs attention (right).
 * Row 3  throughput over time | pipeline right now.
 * Row 4  live work | system health, as a compact strip rather than a wall.
 * Row 5  class averages, from real assignment aggregates.
 *
 * Every number comes from the API field named beside it. Nothing is
 * estimated, and an empty chart says so instead of drawing a lie.
 */
export default function DashboardPage({ onNavigate, data }: {
  onNavigate: (r: Route) => void;
  data: Dashboard | null;
}) {
  const [assignments, setAssignments] = useState<AssignmentOverview[] | null>(null);
  const [liveJobs, setLiveJobs] = useState<Job[]>([]);
  const [activity, setActivity] = useState<ActivityFeed | null>(null);
  const [usage, setUsage] = useState<UsageState | null>(null);
  // The REVIEW QUEUE total, which is a different question from "assignments
  // flagged needs_review": results waiting for a decision can exist without
  // an assignment-level flag. The nav badge and this metric must agree.
  const [reviewTotal, setReviewTotal] = useState<number | null>(null);
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

    const loadReview = () => api<{ total: number }>("/review?limit=1")
      .then((r) => setReviewTotal(r.total)).catch(() => {});
    loadReview();
    const reviewTimer = setInterval(loadReview, 30_000);

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
      clearInterval(usageTimer); clearInterval(reviewTimer); clearInterval(poll); source.close();
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

  // Waiting on you: assignments that still have work, most urgent first.
  // Everything shown here is a real count from /assignments.
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

  // Pipeline distribution: the CURRENT job state, operational only.
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
        <div className="skeleton" style={{ height: 40, maxWidth: 280 }} />
        <div className="skeleton" style={{ height: 92 }} />
        <div className="skeleton" style={{ height: 260 }} />
      </div>
    );
  }

  const keyOk = data.api_health.has_keys && data.api_health.healthy > 0;
  const today = new Date();
  const dayLabel = today.toLocaleDateString(undefined, {
    weekday: "long", day: "numeric", month: "long",
  });
  const openWork = agg.submittedNotChecked;

  return (
    <>
      <PageHeader
        kicker="Desk · Today"
        title="Today's marking desk"
        sub={data.mock_mode
          ? "Mock mode is active: AI calls are simulated, so these numbers describe a rehearsal, not real marking."
          : "Everything you have to mark, in one place. Start with what is waiting."}
        meta={
          <>
            <span>{dayLabel}</span>
            <span aria-hidden="true">·</span>
            <span>{agg.count} assignments tracked</span>
            <span aria-hidden="true">·</span>
            <span>Classroom {data.classroom.connected ? `synced ${timeAgo(data.classroom.last_sync)}` : "not connected"}</span>
          </>
        }
      >
        <Button variant="secondary" icon="review" onClick={() => onNavigate("review")}>
          Review queue{reviewTotal ? ` · ${reviewTotal}` : ""}
        </Button>
        <Button variant="primary" icon="wand" size="lg" onClick={() => onNavigate("correct")}>
          Start a correction
        </Button>
      </PageHeader>

      <div className="metric-row enter" style={{ marginBottom: "var(--s4)" }}>
        <Metric value={Math.round(correctionsToday)} label="Corrected today"
          scope="finished correction jobs"
          tone={correctionsToday > 0 ? "ok" : undefined} />
        <Metric value={openWork} label="Submitted, not marked" scope="from Classroom" />
        <Metric value={reviewTotal ?? "—"} label="Waiting on a decision"
          scope="review queue" tone={reviewTotal ? "warn" : undefined} />
        <Metric value={data.failed} label="Failed" scope="needs a retry"
          tone={data.failed > 0 ? "mark" : undefined} />
        <Metric value={agg.average != null ? `${Math.round(agg.average)}%` : "—"}
          label="Class average" scope={agg.checked > 0 ? `${agg.checked} marked` : "nothing marked yet"} />
      </div>

      <div className="desk-grid">
        <div style={{ minWidth: 0 }} className="stack">
          <section className="card" aria-labelledby="waiting-heading">
            <div className="row between wrap" style={{ marginBottom: "var(--s3)" }}>
              <h2 id="waiting-heading" className="card-title">
                Waiting on you
              </h2>
              <Button size="sm" variant="ghost" iconRight="arrow-right"
                onClick={() => onNavigate("assignments")}>
                {queue.length > 0 ? `All ${queue.length} assignments` : "All assignments"}
              </Button>
            </div>

            {assignments === null ? (
              <div className="skeleton-lines" aria-busy="true">
                <div className="skeleton" style={{ height: 40 }} />
                <div className="skeleton" style={{ height: 40 }} />
                <div className="skeleton" style={{ height: 40 }} />
              </div>
            ) : queue.length === 0 ? (
              <Empty icon="check-circle" art="check" title="Nothing waiting"
                hint="Everything tracked in Assignments is marked. New work appears here as it syncs from Google Classroom." />
            ) : (
              <div className="table-wrap" style={{ border: "none" }}>
                <table className="table table-stack">
                  <thead>
                    <tr>
                      <th scope="col">Assignment</th>
                      <th scope="col">Progress</th>
                      <th scope="col" className="num">Outstanding</th>
                      <th scope="col"><span className="visually-hidden">Open</span></th>
                    </tr>
                  </thead>
                  <tbody>
                    {queue.map(({ a, todo, done }) => (
                      <tr key={a.id} className="clickable"
                        onClick={() => { window.location.hash = `#/assignments/${a.id}`; }}
                        {...{
                          tabIndex: 0,
                          onKeyDown: (e: React.KeyboardEvent) => {
                            if (e.key === "Enter" || e.key === " ") {
                              e.preventDefault();
                              window.location.hash = `#/assignments/${a.id}`;
                            }
                          },
                        }}>
                        <td data-label="Assignment" className="wrap-cell">
                          <span className="cell-strong">{a.title}</span>
                          <span className="cell-sub">
                            {a.course_name ?? "No class"}
                            {a.due_date ? ` · due ${a.due_date}` : ""}
                          </span>
                        </td>
                        <td data-label="Progress" style={{ minWidth: 120 }}>
                          <div className="row" style={{ gap: 8 }}>
                            <Progress value={done} thin label={`${a.title} marked`} />
                            <span className="small mono nowrap">
                              {a.checked}/{a.students_total}
                            </span>
                          </div>
                        </td>
                        <td data-label="Outstanding" className="num">
                          <div className="row end wrap" style={{ gap: 4 }}>
                            {a.processing > 0 && <Badge tone="info">{a.processing} running</Badge>}
                            {a.needs_review > 0 && <Badge tone="review" icon="review">{a.needs_review} to decide</Badge>}
                            {a.failed > 0 && <Badge tone="err" icon="alert">{a.failed} failed</Badge>}
                            {todo > 0 && (
                              <span className="small mono" style={{ color: "var(--text-dim)" }}>
                                {todo} to mark
                              </span>
                            )}
                          </div>
                        </td>
                        <td style={{ width: 24 }}>
                          <Icon name="chevron-right" size={15} />
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </section>

          <section className="card" aria-labelledby="throughput-heading">
            <div className="row between wrap" style={{ marginBottom: "var(--s2)" }}>
              <div className="row wrap" style={{ gap: 8 }}>
                <h2 id="throughput-heading" className="card-title">
                  Marking throughput
                </h2>
                <span className="faint small">papers per day, last 30 days</span>
              </div>
              {activity && (
                <span className="faint small">{activity.totals.results} papers corrected</span>
              )}
            </div>
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
              <div className="notice review small" style={{ marginTop: "var(--s3)" }}>
                <Icon name="review" size={15} />
                <span>{activity.totals.needs_review} of those results are waiting for your decision.</span>
              </div>
            )}
          </section>

          <section className="card" aria-labelledby="averages-heading">
            <div className="row wrap" style={{ gap: 8, marginBottom: "var(--s2)" }}>
              <h2 id="averages-heading" className="card-title">
                Class averages
              </h2>
              <span className="faint small">per assignment, marked work only</span>
            </div>
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
          </section>
        </div>

        <aside className="stack" style={{ minWidth: 0 }} aria-label="Attention and system state">
          <section className="card" aria-labelledby="attention-heading">
            <div className="row between" style={{ marginBottom: "var(--s3)" }}>
              <h2 id="attention-heading" className="card-title">
                Needs your attention
              </h2>
              <Button size="sm" variant="ghost" onClick={() => onNavigate("review")}>
                Review queue
              </Button>
            </div>
            <AttentionPanel compact onNavigate={(hash) => { window.location.hash = hash; }} />
          </section>

          <section className="card" aria-labelledby="live-heading">
            <h2 id="live-heading" className="card-title" style={{ marginBottom: "var(--s3)" }}>
              Running now
            </h2>
            {active.length === 0 ? (
              <Empty icon="wand" art="inbox" title="No corrections running"
                hint="Started jobs appear here in real time." />
            ) : (
              <div className="stack" style={{ gap: "var(--s3)" }}>
                {active.slice(0, 4).map((job) => (
                  <div key={job.id} className="stack" style={{ gap: 6 }}>
                    <div className="row between" style={{ gap: 8 }}>
                      <span className="truncate" style={{ fontWeight: 500 }}>{displayName(job)}</span>
                      <StatusBadge status={job.status} />
                    </div>
                    <Progress value={job.progress} thin label={`Correcting ${displayName(job)}`} />
                    <div className="row between small faint">
                      <span className="truncate">{job.stage_detail ?? job.status}</span>
                      <span className="mono nowrap">
                        {elapsed(job.created_at, job.finished_at)}
                      </span>
                    </div>
                  </div>
                ))}
              </div>
            )}
          </section>

          <section className="card" aria-labelledby="system-heading">
            <div className="row between" style={{ marginBottom: "var(--s2)" }}>
              <h2 id="system-heading" className="card-title">System</h2>
              <Button size="sm" variant="ghost" icon="key" onClick={() => onNavigate("ai")}>
                Keys
              </Button>
            </div>
            <div className="kv">
              <span className="k">Google Classroom</span>
              <span className="v">
                <Badge tone={data.classroom.connected ? "ok" : data.classroom.configured ? "warn" : "err"}>
                  {data.classroom.connected ? "Connected"
                    : data.classroom.configured ? "Offline" : "Not set up"}
                </Badge>
              </span>
            </div>
            <div className="kv">
              <span className="k">AI keys</span>
              <span className="v">
                {data.api_health.has_keys ? `${data.api_health.healthy}/${data.api_health.total} healthy` : "not added"}
                {data.api_health.has_keys && !keyOk && (
                  <Icon name="alert" size={13} style={{ color: "var(--err)", marginLeft: 5 }} />
                )}
              </span>
            </div>
            <div className="kv">
              <span className="k">Pipeline</span>
              <span className="v">{data.pending} queued · {data.processing} running</span>
            </div>
            {pipeline.length > 0 && (
              <div style={{ marginTop: "var(--s3)" }}>
                <Donut
                  title="Job states"
                  summary={`Current job states: ${pipeline.map((p) => `${p.label} ${p.value}`).join(", ")}.`}
                  slices={pipeline}
                  centerLabel={String(pipeline.reduce((n, p) => n + p.value, 0))}
                />
              </div>
            )}
            {usage && (
              <div className="stack" style={{ marginTop: "var(--s3)" }}>
                <Gauge label="Bandwidth — this hour" used={usage.hour.total}
                  cap={usage.limits.hourly_bytes}
                  detail={usage.limits.hourly_mb > 0
                    ? `${(usage.hour.total / 1_048_576).toFixed(1)} / ${usage.limits.hourly_mb} MB`
                    : "no limit"} />
                <Gauge label="Bandwidth — today" used={usage.day.total}
                  cap={usage.limits.daily_bytes}
                  detail={usage.limits.daily_mb > 0
                    ? `${(usage.day.total / 1_048_576).toFixed(1)} / ${usage.limits.daily_mb} MB`
                    : "no limit"} />
              </div>
            )}
          </section>
        </aside>
      </div>
    </>
  );
}
