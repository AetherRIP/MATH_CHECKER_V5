import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  automationApprove, automationAssignments, automationJob, automationJobs,
  automationLog, automationRetryJob, automationRetryReport, automationRunNow,
  automationSetAssignment, automationSettings, automationStatus, automationWorker,
  timeAgo,
  type AutomationAssignment, type AutomationJob, type AutomationLogEntry,
  type AutomationStatus,
} from "../api";;
import {
  Badge, Button, DataTable, Drawer, Empty, Icon, PageHeader, Stat, useToast,
} from "../components";
import {
  AutomationFlow, LiveJobs, SystemPanel, computeNodes, failureCategory,
} from "../AutomationFlow";

/** Tone for a workflow state, so a failure never reads like a success. */
function stateTone(status: string): "ok" | "err" | "warn" | "info" | "mute" {
  if (status === "COMPLETE") return "ok";
  if (["CORRECTION_FAILED", "REPORT_FAILED", "REPORT_UPLOAD_FAILED",
       "ATTACHMENT_FAILED", "GRADE_FAILED", "GRADE_VERIFICATION_FAILED",
       "RETURN_FAILED"].includes(status)) return "err";
  if (status === "BLOCKED_NOT_MODIFIABLE") return "warn";
  if (status === "ATTENTION_REQUIRED") return "warn";
  if (status === "CLEANUP_PENDING") return "warn";
  return "info";
}

function clock(ts: number | null | undefined): string {
  if (!ts) return "—";
  return new Date(ts * 1000).toLocaleTimeString([], {
    hour: "2-digit", minute: "2-digit",
  });
}

export default function AutomationPage() {
  const toast = useToast();
  const [status, setStatus] = useState<AutomationStatus | null>(null);
  const [assignments, setAssignments] = useState<AutomationAssignment[]>([]);
  const [jobs, setJobs] = useState<AutomationJob[]>([]);
  const [log, setLog] = useState<AutomationLogEntry[]>([]);
  const [busy, setBusy] = useState(false);
  const [workerError, setWorkerError] = useState<string | null>(null);
  const [detail, setDetail] = useState<AutomationJob | null>(null);
  // Which action is running for which job. Keyed by job id so one job's
  // in-flight action disables exactly that job's buttons: a second click on
  // Retry/Approve can never fire a duplicate request, and two different jobs
  // can still be worked on one after the other.
  const [pending, setPending] = useState<Record<string, string>>({});
  // A ref mirrors the state so the guard is decided SYNCHRONOUSLY, at click
  // time: a state update is too late to stop a second click in the same tick.
  const pendingRef = useRef<Record<string, string>>({});

  const isPending = useCallback(
    (id: string, action: string) => pending[id] === action, [pending]);

  const startAction = useCallback((id: string, action: string) => {
    if (pendingRef.current[id]) return false;   // refuse the second click
    pendingRef.current = { ...pendingRef.current, [id]: action };
    setPending(pendingRef.current);
    return true;
  }, []);

  const endAction = useCallback((id: string) => {
    const next = { ...pendingRef.current };
    delete next[id];
    pendingRef.current = next;
    setPending(next);
  }, []);

  // Click-through from the visualizer to one job's full detail. Fetched once
  // on demand (not polled) so a click never adds steady-state traffic.
  const openJob = useCallback((job: AutomationJob) => {
    setDetail(job);
    automationJob(job.id).then(setDetail).catch(() => {});
  }, []);

  // An action's response is the LIST shape, which has no per-stage attachment
  // or result detail. Re-read the job so the drawer shows verified evidence
  // instead of blanking out the fields the action response does not carry.
  const refreshDetail = useCallback((id: string) => {
    automationJob(id)
      .then((full) => setDetail((cur) => (cur && cur.id === id ? full : cur)))
      .catch(() => {});
  }, []);

  const load = useCallback(() => {
    automationStatus().then(setStatus).catch(() => {});
    automationAssignments().then((r) => setAssignments(r.assignments)).catch(() => {});
    automationJobs("?limit=50").then((r) => setJobs(r.jobs)).catch(() => {});
    automationLog(60).then((r) => setLog(r.entries)).catch(() => {});
  }, []);

  useEffect(() => {
    load();
    const t = window.setInterval(load, 5000);
    return () => window.clearInterval(t);
  }, [load]);

  async function setEnabled(enabled: boolean) {
    try {
      // The worker endpoint persists the preference AND verifies the thread —
      // the response is the backend's real state, never an echo of the click.
      const next = await automationWorker(enabled);
      setStatus(next);
      setWorkerError(null);
      toast("info", enabled
        ? (next.worker_state === "running"
            ? "Background worker started"
            : `Worker did not start (backend reports "${next.worker_state}")`)
        : (next.worker_state === "stopped"
            ? "Background worker stopped — manual correction keeps working"
            : `Worker did not stop (backend reports "${next.worker_state}")`));
      if ((enabled && next.worker_state !== "running")
          || (!enabled && next.worker_state !== "stopped")) {
        setWorkerError(`The backend reports "${next.worker_state}". Check Diagnostics → Logs.`);
      }
      load();
    } catch (e) {
      // A failed control call must never look like success.
      setWorkerError((e as Error).message);
      toast("err", `Worker control failed: ${(e as Error).message}`);
      load();   // re-read the true state so the UI never keeps a stale value
    }
  }

  async function setInterval(minutes: number) {
    try {
      const next = await automationSettings({ interval_minutes: minutes });
      setStatus(next);
      toast("info", `Polling every ${minutes} minutes`);
      load();
    } catch (e) { toast("err", (e as Error).message); }
  }

  async function toggleAssignment(a: AutomationAssignment) {
    try {
      await automationSetAssignment(a.coursework_id, !a.enabled);
      load();
    } catch (e) { toast("err", (e as Error).message); }
  }

  // The backend resumes the job at the step that failed and re-arms it for the
  // background worker, so the honest feedback is the step it will run - not a
  // claim that the work is already done. A refusal (409) is shown as an error.
  async function retryJob(id: string) {
    if (!startAction(id, "retry")) return;    // one retry per job at a time
    try {
      const job = await automationRetryJob(id);
      if (job.resumable) {
        // Still in a state a retry would act on: it did not start.
        toast("warn", `Retry did not start — the job is still "${job.status_label}"`);
      } else {
        toast("info", `Retrying from "${job.status_label}" — the background `
          + "worker continues the job from that step");
      }
      refreshDetail(id);
      load();
    } catch (e) {
      // A refused or failed retry must never read as success.
      toast("err", `Retry failed: ${(e as Error).message}`);
      refreshDetail(id);
      load();
    } finally { endAction(id); }
  }

  // Repairs the report upload/attachment ONLY - the correction and grade are
  // reused, so the student is never re-marked.
  async function retryReport(id: string) {
    if (!startAction(id, "report")) return;
    try {
      const job = await automationRetryReport(id);
      if (job.report_uploaded) {
        toast("ok", "Report uploaded, attached and verified");
      } else {
        toast("warn", job.error
          ? `Report still not uploaded — ${job.error}`
          : "Report still not uploaded — see the job detail");
      }
      refreshDetail(id);
      load();
    } catch (e) {
      toast("err", `Report upload failed: ${(e as Error).message}`);
      refreshDetail(id);
      load();
    } finally { endAction(id); }
  }

  async function runNow() {
    setBusy(true);
    try {
      await automationRunNow();
      toast("ok", "Checked enabled assignments");
      load();
    } catch (e) { toast("err", (e as Error).message); }
    finally { setBusy(false); }
  }

  // Review mode: ON holds every grade as a teacher-approved draft and never
  // returns work until you approve it; OFF publishes and returns automatically.
  async function setReviewMode(on: boolean) {
    try {
      const next = await automationSettings({ review_mode: on });
      setStatus(next);
      toast("info", on
        ? "Review mode ON — corrections wait for your approval"
        : "Review mode OFF — corrections publish and return automatically");
      load();
    } catch (e) { toast("err", (e as Error).message); }
  }

  // How much of the report is attached to the student's submission.
  // "detailed" is the WHOLE report: after the score and the question table,
  // every question gets its own block with the student's answer, what the
  // mark scheme required, the awarded mark points and the marking comment.
  async function setReportDetail(detail: AutomationStatus["report_detail"]) {
    try {
      const next = await automationSettings({ report_detail: detail });
      setStatus(next);
      toast("info", detail === "detailed"
        ? "Full report: every question is explained on its own"
        : `Report detail set to ${detail}`);
      load();
    } catch (e) { toast("err", (e as Error).message); }
  }

  // Approval is only reported as done when the backend says the job really
  // reached COMPLETE: a grade alone is not proof that the report was sent and
  // the work returned, and a refusal (409) is shown as an error instead.
  async function approveJob(id: string) {
    if (!startAction(id, "approve")) return;   // one approval per job at a time
    try {
      const job = await automationApprove(id);
      if (job.status === "COMPLETE") {
        toast("ok", job.grade
          ? `Approved — report sent, grade ${job.grade} published and work returned`
          : "Approved — report and grade sent, work returned");
      } else {
        // Never claim success the job row does not confirm.
        toast("warn", `Approval did not finish — the job is "${job.status_label}". `
          + "Press Approve again to continue from the failed stage.");
      }
      refreshDetail(id);
      load();
    } catch (e) {
      toast("err", `Approval failed: ${(e as Error).message}`);
      refreshDetail(id);
      load();
    } finally { endAction(id); }
  }

  const counts = status?.counts;
  const resumable = jobs.filter((j) => j.resumable);
  const pipeline = status?.pipeline ?? [];

  // The per-job stage checklist is derived from the SAME state machine the
  // graph uses, so the two can never disagree.
  const detailNodes = useMemo(
    () => (detail ? computeNodes(pipeline, [detail]) : []),
    [detail, pipeline]);
  const detailTimeline = useMemo(
    () => (detail
      ? log.filter((e) => e.automation_job_id === detail.id)
           .slice().sort((a, b) => a.created_at - b.created_at)
      : []),
    [detail, log]);

  return (
    <div className="stack">
      <PageHeader title="Automatic Correction" icon="wand"
        sub="Student submits → detected → corrected → report attached → grade 1–9 → returned. No teacher interaction needed.">
        <div className="row" style={{ gap: 8 }}>
          <Button variant="secondary" icon="refresh" onClick={runNow} loading={busy}>
            Check now
          </Button>
          <Button variant={status?.review_mode ? "primary" : "secondary"}
            icon="review"
            onClick={() => setReviewMode(!status?.review_mode)}>
            Review mode: {status?.review_mode ? "ON" : "OFF"}
          </Button>
          <Button variant={status?.enabled ? "danger" : "primary"}
            icon={status?.enabled ? "pause" : "play"}
            onClick={() => setEnabled(!status?.enabled)}>
            {status?.enabled ? "Disable automation" : "Enable automation"}
          </Button>
        </div>
      </PageHeader>

      <div className="card" style={{ padding: "14px 16px" }}>
        <div className="row" style={{ gap: 12, alignItems: "center", flexWrap: "wrap" }}>
          <div style={{ minWidth: 200 }}>
            <div style={{ fontWeight: 650 }}>Background Worker</div>
            <div className="faint small">
              Automatic correction only — the website and manual AI correction
              keep running when it is off.
            </div>
          </div>
          <div className="row" style={{ gap: 8, alignItems: "center" }}>
            <span className="small faint">Status</span>
            {!status ? (
              <Badge tone="mute" icon="refresh">…</Badge>
            ) : status.worker_state === "running" ? (
              <Badge tone="ok" icon="check-circle">RUNNING</Badge>
            ) : status.worker_state === "stalled" ? (
              <Badge tone="err" icon="alert">NOT RUNNING</Badge>
            ) : (
              <Badge tone="mute" icon="x">STOPPED</Badge>
            )}
          </div>
          <div className="row" style={{ gap: 18, marginLeft: "auto", flexWrap: "wrap" }}>
            <div>
              <div className="faint small">Last activity</div>
              <div style={{ fontWeight: 650 }}>{clock(status?.last_activity)}</div>
            </div>
            <div>
              <div className="faint small">Pending jobs</div>
              <div style={{ fontWeight: 650 }}>{status?.pending_jobs ?? "—"}</div>
            </div>
            <div>
              <div className="faint small">Active jobs</div>
              <div style={{ fontWeight: 650 }}>{status?.active_jobs ?? "—"}</div>
            </div>
          </div>
          <Button
            variant={status?.worker_state === "running" ? "danger" : "primary"}
            icon={status?.worker_state === "running" ? "pause" : "play"}
            loading={busy}
            disabled={!status}
            onClick={() => setEnabled(status?.worker_state !== "running")}>
            {status?.worker_state === "running" ? "Turn OFF" : "Turn ON"}
          </Button>
        </div>
        {workerError && (
          <div className="notice err" style={{ marginTop: 10 }} role="alert">
            <Icon name="alert" size={15} />
            <span>{workerError}</span>
          </div>
        )}
        {status?.worker_state === "stopped" && (status?.active_jobs ?? 0) > 0 && (
          <div className="faint small" style={{ marginTop: 8 }}>
            {status?.active_jobs} in-flight job(s) are finishing safely; no new
            automatic jobs will start until the worker is turned back on.
          </div>
        )}
      </div>

      <div className="card" style={{ padding: "10px 14px" }}>
        <div className="row" style={{ gap: 10, alignItems: "center", flexWrap: "wrap" }}>
          <span className="small" style={{ fontWeight: 650 }}>Attached report</span>
          <span className="faint small">
            {status?.report_detail === "detailed"
              ? "Full — every question explained on its own"
              : status?.report_detail === "summary"
                ? "Summary — score and overview only"
                : "Standard — score, overview and question table"}
          </span>
          <div className="row" style={{ gap: 6, marginLeft: "auto" }}>
            {([["summary", "Summary"], ["standard", "Standard"],
               ["detailed", "Full"]] as const).map(([value, label]) => (
              <Button key={value} size="sm"
                variant={status?.report_detail === value ? "primary" : "secondary"}
                onClick={() => setReportDetail(value)}>
                {label}
              </Button>
            ))}
          </div>
        </div>
        {status?.attach_report === false && (
          <div className="faint small" style={{ marginTop: 6 }}>
            Report attachment is currently disabled, so no report is uploaded.
          </div>
        )}
      </div>

      {/* Grade return is its OWN state (§19): a correction, a generated report
          and an uploaded report are NOT a returned grade. This card reports the
          real backend setting, never a local toggle. */}
      <div className="card" style={{ padding: "10px 14px" }}>
        <div className="row" style={{ gap: 10, alignItems: "center", flexWrap: "wrap" }}>
          <span className="small" style={{ fontWeight: 650 }}>Grade return</span>
          {status?.review_mode ? (
            <Badge tone="warn" icon="review">Held for your approval</Badge>
          ) : status?.submit_grade === false ? (
            <Badge tone="mute" icon="x">Off</Badge>
          ) : status?.return_work === false ? (
            <Badge tone="warn" icon="alert">Grade only — not returned</Badge>
          ) : (
            <Badge tone="ok" icon="check-circle">
              Grade posted and work returned
            </Badge>
          )}
          <span className="faint small">
            {status?.review_mode
              ? "Every grade is saved as a draft; press Approve on a correction to publish the grade and return the work."
              : status?.submit_grade === false
                ? "Corrections and reports are produced, but no grade is written to Google Classroom."
                : status?.return_work === false
                  ? "The grade is written to the correct submission, but the work is not returned automatically."
                  : "Each corrected submission has its grade written to Classroom and the work returned; both are read back from Classroom to verify."}
          </span>
        </div>
      </div>

      <div className="grid grid-4">
        <Stat label="Automatic Correction"
          value={status?.enabled
            ? <Badge tone="ok" icon="check-circle">ON</Badge>
            : <Badge tone="mute" icon="x">OFF</Badge>} />
        <Stat label="Polling" value={`Every ${status?.interval_minutes ?? "—"} minutes`} />
        <Stat label="Last check" value={clock(status?.last_check)}
          tone={status?.last_check ? undefined : "warn"} />
        <Stat label="Next check" value={status?.enabled ? clock(status?.next_check) : "paused"} />
      </div>

      {status?.review_mode && (
        <div className="notice info">
          <Icon name="review" size={15} />
          <span>
            <strong>Review mode is ON.</strong> Every corrected submission is
            kept as a draft: no grade is published and no work is returned
            until you press <strong>Approve</strong> on it. Turn review mode
            off to publish and return automatically.
          </span>
        </div>
      )}

      <div className="grid grid-4">
        <Stat label="Submitted" value={counts?.total ?? 0} />
        <Stat label="Completed" value={counts?.completed ?? 0} tone="ok" />
        <Stat label="Processing" value={counts?.processing ?? 0} />
        <Stat label="Waiting" value={counts?.waiting ?? 0} />
      </div>

      <section className="card">
        <div className="flow-layout">
          <AutomationFlow
            pipeline={pipeline}
            jobs={jobs}
            source={{
              label: "Google Classroom",
              sub: status?.last_check
                ? `last scan ${clock(status.last_check)}`
                : "awaiting first scan",
              live: Boolean(status?.enabled),
            }}
            onOpenJob={openJob}
          />
          <div className="stack">
            <LiveJobs jobs={jobs} totalStages={pipeline.length}
              onOpenJob={openJob} />
            <SystemPanel
              counts={{
                total: counts?.total ?? 0,
                completed: counts?.completed ?? 0,
                processing: counts?.processing ?? 0,
                waiting: counts?.waiting ?? 0,
                failed: counts?.failed ?? 0,
              }}
              enabled={Boolean(status?.enabled)}
              running={Boolean(status?.running)}
            />
          </div>
        </div>
        <p className="faint small" style={{ marginTop: 10 }}>
          Every node maps to a real workflow stage and is driven by live job
          state. A stage bar shows position in the pipeline, never a guessed
          percentage. Click a node or a student to open the job.
        </p>
      </section>

      <section className="card">
        <h3>Polling interval</h3>
        <div className="row wrap" style={{ gap: 8 }}>
          {(status?.intervals ?? [5, 10, 15, 30]).map((m) => (
            <Button key={m} size="sm"
              variant={status?.interval_minutes === m ? "primary" : "secondary"}
              onClick={() => setInterval(m)}>
              {m} minutes
            </Button>
          ))}
        </div>
        <p className="faint small">
          The monitor checks enabled assignments only, on this interval. It makes
          no Google requests between checks.
        </p>
      </section>

      <section className="card">
        <h3>Assignments monitored</h3>
        <p className="faint small">
          Only assignments ticked here are ever corrected automatically. Nothing
          is enabled by default.
        </p>
        {assignments.length === 0 ? (
          <Empty icon="classroom" title="No assignments cached yet"
            hint="Sync Google Classroom first, then choose which assignments run automatically." />
        ) : (
          <DataTable head={
            <tr>
              <th>Use automation</th><th>Assignment</th><th>Mark scheme</th>
              <th className="num">Submitted</th><th className="num">Completed</th>
              <th className="num">Processing</th><th className="num">Waiting</th>
              <th className="num">Failed</th>
            </tr>
          }>
            {assignments.map((a) => (
              <tr key={a.coursework_id}>
                <td>
                  <input type="checkbox" checked={a.enabled}
                    aria-label={`Automatic correction for ${a.title}`}
                    onChange={() => toggleAssignment(a)} />
                </td>
                <td>
                  <strong>{a.title}</strong>
                  {a.last_error && <div className="faint small">Last check failed: {a.last_error}</div>}
                </td>
                <td>{a.markscheme
                  ? <Badge tone="ok" icon="check">ready</Badge>
                  : <Badge tone="warn" icon="alert">missing</Badge>}</td>
                <td>{a.stats.submitted}</td>
                <td>{a.stats.completed}</td>
                <td>{a.stats.processing}</td>
                <td>{a.stats.waiting}</td>
                <td>{a.stats.failed}</td>
              </tr>
            ))}
          </DataTable>
        )}
      </section>

      <section className="card">
        <h3>Recent jobs {resumable.length > 0 && (
          <Badge tone="warn">{resumable.length} need attention</Badge>)}
        </h3>
        {jobs.length === 0 ? (
          <Empty icon="inbox" title="No automated corrections yet"
            hint="New submissions for enabled assignments appear here automatically." />
        ) : (
          <DataTable minWidth={820} head={
            <tr>
              <th>Student</th><th>Assignment</th><th>State</th><th>Result</th>
              <th>Grade</th><th>Updated</th><th></th>
            </tr>
          }>
            {jobs.map((j) => (
              <tr key={j.id} className="clickable-row" onClick={() => openJob(j)}>
                <td>{j.student_name || "Student"}</td>
                <td>{j.assignment_title || "—"}</td>
                <td>
                  <Badge tone={stateTone(j.status)}>{j.status_label}</Badge>
                  {j.error && <div className="faint small">{j.error}</div>}
                </td>
                <td>{j.marks_obtained != null
                  ? `${j.marks_obtained}/${j.maximum_marks} (${j.percentage}%)` : "—"}</td>
                <td>{j.grade ? <Badge tone="ok">Grade {j.grade}</Badge> : "—"}</td>
                <td>{timeAgo(j.updated_at)}</td>
                <td>
                  <div className="row" style={{ gap: 6, flexWrap: "nowrap" }}>
                    {j.approvable && (
                      <Button size="sm" variant="success" icon="check"
                        loading={isPending(j.id, "approve")}
                        tooltip="Send the report, publish the final grade and return the work"
                        onClick={(e) => { e.stopPropagation(); approveJob(j.id); }}>
                        Approve &amp; Send</Button>
                    )}
                    {j.resumable && (
                      <Button size="sm" variant="secondary" icon="refresh"
                        loading={isPending(j.id, "retry")}
                        tooltip={j.retry_from
                          ? `Retry only the "${j.retry_from}" step`
                          : "Retry the step that failed"}
                        onClick={(e) => { e.stopPropagation(); retryJob(j.id); }}>
                        Retry</Button>
                    )}
                    {j.has_result && !j.report_uploaded && (
                      <Button size="sm" variant="secondary" icon="upload"
                        loading={isPending(j.id, "report")}
                        onClick={(e) => { e.stopPropagation(); retryReport(j.id); }}>
                        Retry report</Button>
                    )}
                  </div>
                </td>
              </tr>
            ))}
          </DataTable>
        )}
      </section>

      <section className="card">
        <h3>Recent activity</h3>
        {log.length === 0 ? (
          <Empty icon="activity" title="Nothing logged yet" />
        ) : (
          <ul style={{ listStyle: "none", padding: 0, margin: 0 }}>
            {log.map((e) => (
              <li key={e.id} className="row small"
                style={{ gap: 10, padding: "5px 0", alignItems: "baseline" }}>
                <span className="faint" style={{ minWidth: 52 }}>
                  {clock(e.created_at)}</span>
                <span style={{ color: e.level === "error" ? "var(--err)"
                  : e.level === "warning" ? "var(--warn)"
                  : e.level === "success" ? "var(--ok)" : undefined }}>
                  {e.message || e.event}</span>
              </li>
            ))}
          </ul>
        )}
      </section>

      {detail && (
        <Drawer title={detail.student_name || "Correction job"}
          onClose={() => setDetail(null)}>
          <div className="job-detail">
            <div className="detail-cols">
              <div className="detail-block">
                <h4>Student</h4>
                <div className="detail-kv"><span>Name</span>
                  <span>{detail.student_name || "—"}</span></div>
                <div className="detail-kv"><span>Student ID</span>
                  <span>{detail.student_id || "—"}</span></div>
              </div>
              <div className="detail-block">
                <h4>Assignment</h4>
                <div className="detail-kv"><span>Name</span>
                  <span>{detail.assignment_title || "—"}</span></div>
                <div className="detail-kv"><span>Coursework ID</span>
                  <span>{detail.coursework_id || "—"}</span></div>
                <div className="detail-kv"><span>Course ID</span>
                  <span>{detail.course_id || "—"}</span></div>
              </div>
              <div className="detail-block">
                <h4>Submission</h4>
                <div className="detail-kv"><span>Submission ID</span>
                  <span>{detail.submission_id || "—"}</span></div>
                <div className="detail-kv"><span>Version</span>
                  <span>{detail.submission_version || "—"}</span></div>
                <div className="detail-kv"><span>Updated</span>
                  <span>{timeAgo(detail.updated_at)}</span></div>
              </div>
              {/* §8: which pipeline is doing the work, plus the real timing.
                  The duration is only shown once the job actually finished. */}
              <div className="detail-block">
                <h4>Pipeline &amp; timing</h4>
                <div className="detail-kv"><span>Pipeline</span>
                  <span>
                    {detail.pipeline
                      ? <Badge tone={detail.pipeline === "batched" ? "info" : "mute"}>
                          {detail.pipeline_label ?? detail.pipeline}
                        </Badge>
                      : <span className="faint">not assigned yet</span>}
                  </span></div>
                <div className="detail-kv"><span>Stage</span>
                  <span>{detail.status_label}
                    {pipeline.length > 0 && (
                      <span className="faint small">
                        {" "}({Math.min(detail.stage_index + 1, pipeline.length)}
                        /{pipeline.length})
                      </span>
                    )}</span></div>
                <div className="detail-kv"><span>Started</span>
                  <span>{detail.created_at ? clock(detail.created_at) : "—"}</span></div>
                <div className="detail-kv"><span>Finished</span>
                  <span>{detail.finished_at ? clock(detail.finished_at) : "running"}</span></div>
                <div className="detail-kv"><span>Duration</span>
                  <span>{detail.finished_at && detail.created_at
                    ? `${Math.max(0, Math.round(detail.finished_at - detail.created_at))}s`
                    : (detail.created_at
                        ? `${Math.max(0, Math.round(Date.now() / 1000 - detail.created_at))}s so far`
                        : "—")}</span></div>
              </div>
              <div className="detail-block">
                <h4>Correction</h4>
                <div className="detail-kv"><span>Raw score</span>
                  <span>{detail.marks_obtained != null
                    ? `${detail.marks_obtained}/${detail.maximum_marks}` : "—"}</span></div>
                <div className="detail-kv"><span>Percentage</span>
                  <span>{detail.percentage != null ? `${detail.percentage}%` : "—"}</span></div>
                <div className="detail-kv"><span>Final grade</span>
                  <span>{detail.grade
                    ? <Badge tone="ok">IGCSE {detail.grade}</Badge> : "—"}</span></div>
                <div className="detail-kv"><span>Double-check</span>
                  <span>
                    {!detail.result
                      ? "—"
                      : detail.result.double_check_triggered
                        ? (
                          <Badge tone={detail.result.double_check_status === "REVIEW_REQUIRED"
                            || detail.result.double_check_status === "DOUBLE_CHECK_FAILED"
                            ? "warn" : "ok"}>
                            {detail.result.double_check_status}
                          </Badge>
                        ) : <Badge tone="mute">not triggered</Badge>}
                  </span></div>
                <div className="detail-kv"><span>Review warnings</span>
                  <span className="small">
                    {!detail.result
                      ? "—"
                      : detail.result.review_required
                        ? <Badge tone="warn">flagged for review</Badge>
                        : (detail.result.confidence || "none")}
                  </span></div>
                {detail.result?.confidence_reasons && (
                  <div className="faint small" style={{ marginTop: 4 }}>
                    {String(detail.result.confidence_reasons).slice(0, 240)}
                  </div>
                )}
              </div>
              <div className="detail-block">
                <h4>Report</h4>
                <div className="detail-kv"><span>File</span>
                  <span>{detail.attachment?.report_filename
                    || detail.report_filename || "—"}</span></div>
                <div className="detail-kv"><span>Drive file</span>
                  <span>{detail.report_uploaded
                    ? (detail.attachment?.drive_file_id
                        || detail.drive_file_id || "uploaded")
                    : "not uploaded"}</span></div>
                <div className="detail-kv"><span>On submission</span>
                  <span>{detail.attachment?.attached
                    ? "verified"
                    : (detail.attachment?.status || "—")}</span></div>
              </div>
              <div className="detail-block">
                <h4>Classroom publication</h4>
                <div className="detail-kv"><span>Report</span>
                  <span>{detail.publication
                    ? (detail.publication.report_sent
                        ? <Badge tone="ok">sent</Badge>
                        : <Badge tone={detail.publication.report === "failed"
                            ? "err" : "warn"}>{detail.publication.report}</Badge>)
                    : "—"}</span></div>
                <div className="detail-kv"><span>Grade</span>
                  <span>{detail.publication
                    ? (detail.publication.grade_published
                        ? <Badge tone="ok">published</Badge>
                        : <Badge tone="warn">{detail.publication.grade}</Badge>)
                    : "—"}</span></div>
                <div className="detail-kv"><span>Work returned</span>
                  <span>{detail.publication
                    ? (detail.publication.returned
                        ? <Badge tone="ok">verified</Badge>
                        : <Badge tone="mute">not yet</Badge>)
                    : "—"}</span></div>
                <div className="faint small" style={{ marginTop: 4 }}>
                  Report, grade and return are separate Google Classroom
                  operations — each stage is shown only once Google confirmed it.
                </div>
              </div>
            </div>

            <div>
              <div className="row" style={{ justifyContent: "space-between" }}>
                <h4 style={{ margin: 0 }}>Processing state</h4>
                <Badge tone={stateTone(detail.status)}>{detail.status_label}</Badge>
              </div>
              {detail.error && (
                <div className="notice err" style={{ marginTop: 8 }}>
                  <Icon name="alert" size={15} />
                  <span><strong>{failureCategory(detail)}</strong> — {detail.error}</span>
                </div>
              )}
              {/* The honest "is the report really on the submission?" action:
                  repairs the upload/attachment only, never re-runs the AI. */}
              {detail.approvable && (
                <div className="notice info" style={{ marginTop: 8 }}>
                  <Icon name="check-circle" size={15} />
                  <span>
                    {detail.awaiting_approval
                      ? "Report generated and validated; grade calculated. Nothing has been sent to Google Classroom yet — approve to send the grade and report together."
                      : "This correction is ready: approving sends the report and grade, then returns the work."}
                  </span>
                  <Button size="sm" variant="success" icon="check"
                    loading={isPending(detail.id, "approve")}
                    onClick={() => approveJob(detail.id)}>
                    Approve and Send Grade + Report</Button>
                </div>
              )}
              {detail.has_result && !detail.report_uploaded && (
                <div className="notice warn" style={{ marginTop: 8 }}>
                  <Icon name="upload" size={15} />
                  <span>The correction report is not on the student's
                    submission yet.</span>
                  <Button size="sm" variant="secondary" icon="upload"
                    loading={isPending(detail.id, "report")}
                    onClick={() => retryReport(detail.id)}>
                    Retry Report Upload</Button>
                </div>
              )}
              <div className="checklist" style={{ marginTop: 8 }}>
                {detailNodes.map((n) => (
                  <div key={n.state} className={`check-row ${n.status}`}>
                    <span className="mk">
                      <Icon size={14} name={
                        n.status === "completed" ? "check-circle"
                          : n.status === "failed" ? "alert"
                          : n.status === "active" ? "refresh" : "clock"} />
                    </span>
                    <span>{n.label}</span>
                  </div>
                ))}
              </div>
            </div>

            <div>
              <h4 style={{ margin: "0 0 6px" }}>Activity timeline</h4>
              {detailTimeline.length === 0 ? (
                <p className="faint small">
                  No events captured for this job in the recent activity window.
                </p>
              ) : (
                <div className="timeline">
                  {detailTimeline.map((e) => (
                    <div className="tl-row" key={e.id}>
                      <span className="tl-t">{clock(e.created_at)}</span>
                      <span>{e.message || e.event}</span>
                    </div>
                  ))}
                </div>
              )}
            </div>
          </div>
        </Drawer>
      )}
    </div>
  );
}
