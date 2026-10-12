import { useEffect, useMemo, useRef, useState } from "react";
import {
  api, displayName, elapsed, timeAgo, ACTIVE_STATUSES,
  type AssignmentOverview, type Dashboard, type Job,
} from "../api";
import {
  Badge, Button, Empty, Icon, Progress, StatusBadge, StatusCard,
} from "../components";
import { AttentionPanel } from "../attention";
import type { Route } from "../App";

export default function DashboardPage({ onNavigate }: { onNavigate: (r: Route) => void }) {
  const [data, setData] = useState<Dashboard | null>(null);
  const [assignments, setAssignments] = useState<AssignmentOverview[] | null>(null);
  const [liveJobs, setLiveJobs] = useState<Job[]>([]);
  const sourceRef = useRef<EventSource | null>(null);

  useEffect(() => {
    api<Dashboard>("/dashboard").then(setData).catch(() => {});
    const loadAssignments = () => api<{ assignments: AssignmentOverview[] }>("/assignments")
      .then((r) => setAssignments(r.assignments)).catch(() => {});
    loadAssignments();
    const timer = setInterval(() => {
      api<Dashboard>("/dashboard").then(setData).catch(() => {});
      loadAssignments();
    }, 15_000);

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
    return () => { clearInterval(timer); clearInterval(poll); source.close(); };
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
  const aiTone = !data.api_health.has_keys ? "warn" : keyOk ? "ok" : "err";

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

      {/* ---- status cards ---- */}
      <div className="grid grid-auto" style={{ marginBottom: 18 }}>
        <StatusCard icon="clock" tone={agg.missing > 0 ? "warn" : "mute"}
          value={agg.missing} label="Outstanding"
          sub={agg.students ? `missing, draft or 0 · ${agg.count} assignment(s)` : "no students yet"}
          onClick={() => onNavigate("assignments")} />
        <StatusCard icon="inbox" tone={agg.submittedNotChecked > 0 ? "info" : "mute"}
          value={agg.submittedNotChecked} label="Submitted / unchecked"
          sub="waiting to be corrected"
          onClick={() => onNavigate("assignments")} />
        <StatusCard icon="check-circle" tone="ok"
          value={agg.checked} label="Checked"
          sub={agg.count ? `${agg.count} assignment(s)` : "nothing yet"}
          onClick={() => onNavigate("results")} />
        <StatusCard icon="chart" tone="info"
          value={agg.average != null ? `${agg.average.toFixed(0)}%` : "—"}
          label="Class average"
          sub={agg.checked ? `from ${agg.checked} result(s)` : "no results yet"}
          onClick={() => onNavigate("reports")} />
        <StatusCard icon="review" tone={agg.review > 0 ? "warn" : "mute"}
          value={agg.review} label="Needs review"
          sub={agg.review > 0 ? "flagged results" : "all clear"}
          onClick={() => onNavigate("review")} />
        <StatusCard icon="ai" tone={aiTone}
          value={data.api_health.has_keys ? `${data.api_health.healthy}/${data.api_health.total}` : "—"}
          label="AI keys healthy"
          sub={data.mock_mode ? "mock mode" : (keyOk ? "ready to correct" : "needs attention")}
          onClick={() => onNavigate("ai")} />
      </div>

      {/* ---- marking queue ---- */}
      <div className="card" style={{ marginBottom: 18 }}>
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
          <Empty icon="check-circle" title="Nothing waiting to be marked"
            hint="Every submission that has been turned in has been corrected." />
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
                      {a.needs_review > 0 && <Badge tone="warn" icon="review">{a.needs_review} review</Badge>}
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

      <div className="grid grid-2" style={{ alignItems: "start" }}>
        {/* ---- needs attention ---- */}
        <div className="card">
          <div className="row between" style={{ marginBottom: 12 }}>
            <h3 style={{ margin: 0 }}>
              <Icon name="alert" size={16} style={{ color: "var(--warn)" }} />
              What needs my attention?
            </h3>
            <Button size="sm" variant="ghost" onClick={() => onNavigate("assignments")}>
              View assignments
            </Button>
          </div>
          <AttentionPanel compact onNavigate={(hash) => { window.location.hash = hash; }} />
          <div className="row end" style={{ marginTop: 10 }}>
            <Button size="sm" variant="ghost" icon="arrow-right"
              onClick={() => onNavigate("assignments")}>
              Open assignments
            </Button>
          </div>
        </div>

        {/* ---- live activity ---- */}
        <div className="card">
          <h3>
            <Icon name="activity" size={16} style={{ color: "var(--accent)" }} />
            Current activity <span className="hint">live</span>
          </h3>
          {active.length === 0 ? (
            <Empty icon="wand" title="No active corrections"
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
              <button className="btn ghost sm" onClick={() => onNavigate("corrections")}>
                Review failures
              </button>
            </div>
          )}
        </div>
      </div>

      {/* ---- system health ---- */}
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
        <div className="grid grid-4" style={{ marginTop: 12 }}>
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
            <div className="label">Keys healthy</div>
            <div className={`value ${keyOk ? "ok" : data.api_health.has_keys ? "err" : "warn"}`}
              style={{ fontSize: "var(--fs-lg)" }}>
              {data.api_health.has_keys ? `${data.api_health.healthy}/${data.api_health.total}` : "—"}
            </div>
          </div>
          <div className="stat compact">
            <div className="label">Processing</div>
            <div className="value" style={{ fontSize: "var(--fs-lg)" }}>{data.processing}</div>
          </div>
          <div className="stat compact">
            <div className="label">Corrections today</div>
            <div className="value" style={{ fontSize: "var(--fs-lg)" }}>{data.corrections_today}</div>
          </div>
        </div>
      </div>
    </>
  );
}
