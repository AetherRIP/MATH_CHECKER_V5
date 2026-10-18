import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  largePdfCancel, largePdfCreate, largePdfJob, largePdfJobs,
  largePdfRetryBatch, largePdfRetryJob, timeAgo,
  type LargePdfBatch, type LargePdfJob, type LargePdfListing,
} from "../api";
import {
  Badge, Button, DataTable, Empty, PageHeader, Progress, Stat, useToast,
} from "../components";
import { useAdaptivePoll } from "../useAdaptivePoll";
import { isLargeJobTerminal } from "../largePdfPolling";

function statusTone(status: string): "ok" | "err" | "warn" | "info" | "mute" {
  if (status === "COMPLETED") return "ok";
  if (status === "FAILED" || status === "CANCELLED") return "err";
  if (status === "AWAITING_REVIEW") return "warn";
  if (status === "PROCESSING" || status === "READY") return "info";
  return "mute";
}

function batchTone(status: string): "ok" | "err" | "warn" | "info" | "mute" {
  if (status === "COMPLETED") return "ok";
  if (status === "FAILED") return "err";
  if (status === "REVIEW_REQUIRED" || status === "VERIFICATION_REQUIRED") return "warn";
  if (status === "PROCESSING") return "info";
  return "mute";
}

function runProgress(status: string): number {
  switch (status) {
    case "QUEUED": return 0.05;
    case "INSPECTING": return 0.12;
    case "PLANNING": return 0.2;
    case "READY": return 0.25;
    case "PROCESSING": return 0.6;
    case "VALIDATING": return 0.8;
    case "MERGING": return 0.9;
    case "AWAITING_REVIEW":
    case "COMPLETED": return 1;
    default: return 0;
  }
}

export default function LargePdfWorkerPage({
  onNavigate,
}: { onNavigate?: (hash: string) => void }) {
  const toast = useToast();
  const [listing, setListing] = useState<LargePdfListing | null>(null);
  const [selected, setSelected] = useState<LargePdfJob | null>(null);
  const [busy, setBusy] = useState(false);
  const [studentName, setStudentName] = useState("");
  const submissionRef = useRef<HTMLInputElement>(null);
  const schemeRef = useRef<HTMLInputElement>(null);
  const selectedRef = useRef<string | null>(null);

  useEffect(() => { selectedRef.current = selected?.id ?? null; }, [selected]);

  const load = useCallback(() => {
    largePdfJobs().then(setListing).catch(() => {});
    const id = selectedRef.current;
    if (id) largePdfJob(id).then(setSelected).catch(() => {});
  }, []);

  useEffect(() => {
    // The listing is watched adaptively: fast while a batch is actually moving,
    // slow (and silent while the tab is hidden) when the worker is idle.
    load();
  }, [load]);

  const busyJob = Boolean(listing && listing.jobs.some((j) => !isLargeJobTerminal(j.status)));
  useAdaptivePoll(load, { active: busyJob, fast: 2500, idle: 20_000, immediate: false });

  async function submit() {
    const submission = submissionRef.current?.files?.[0];
    const scheme = schemeRef.current?.files?.[0];
    if (!submission || !scheme) {
      toast("err", "Choose both the student answer PDF and the official mark scheme.");
      return;
    }
    setBusy(true);
    try {
      const form = new FormData();
      form.append("submission", submission);
      form.append("mark_scheme", scheme);
      form.append("student_name", studentName || "Student");
      const job = await largePdfCreate(form);
      toast("ok", `Inspected ${job.page_count}-page PDF — ${job.batch_count} batch(es) planned.`);
      setSelected(job);
      if (submissionRef.current) submissionRef.current.value = "";
      if (schemeRef.current) schemeRef.current.value = "";
      setStudentName("");
      load();
    } catch (e) {
      toast("err", (e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  async function act(label: string, fn: () => Promise<LargePdfJob>) {
    setBusy(true);
    try {
      const job = await fn();
      setSelected(job);
      toast("ok", label);
      load();
    } catch (e) {
      toast("err", (e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  const counts = listing?.counts;
  const cfg = listing?.config;
  const jobs = listing?.jobs ?? [];

  const liveSelected = useMemo(() => {
    if (!selected) return null;
    const fresh = jobs.find((j) => j.id === selected.id);
    return fresh ? { ...selected, ...fresh } : selected;
  }, [selected, jobs]);

  return (
    <>
      <PageHeader title="Large PDF Worker"
        sub="Long student answer PDFs are inspected, split into balanced question-aware batches, corrected by the same engine, then validated and merged.">
        {cfg && (
          <span className="badge info">
            {cfg.target_min}–{cfg.target_max} pages/batch · over {cfg.threshold_pages} pages batched
          </span>
        )}
      </PageHeader>

      <div className="grid grid-4" style={{ marginBottom: 14 }}>
        <Stat label="Queued" value={counts?.queued ?? 0} />
        <Stat label="Processing" value={(counts?.ready ?? 0) + (counts?.processing ?? 0)
          + (counts?.inspecting ?? 0) + (counts?.planning ?? 0)} />
        <Stat label="Awaiting review" value={counts?.awaiting_review ?? 0}
          tone={(counts?.awaiting_review ?? 0) > 0 ? "warn" : undefined} />
        <Stat label="Completed" value={counts?.completed ?? 0} tone="ok" />
      </div>

      <div className="card">
        <h3>Add a long document</h3>
        <div className="muted small" style={{ marginBottom: 10 }}>
          Upload the student answer PDF and the official mark scheme. The document
          is inspected and planned before any marking starts.
        </div>
        <div className="grid grid-3" style={{ alignItems: "end" }}>
          <label className="field">
            <span>Student answer PDF</span>
            <input className="input" type="file" accept=".pdf,application/pdf"
              ref={submissionRef} />
          </label>
          <label className="field">
            <span>Official mark scheme (PDF)</span>
            <input className="input" type="file" accept=".pdf,application/pdf"
              ref={schemeRef} />
          </label>
          <label className="field">
            <span>Student name</span>
            <input className="input" type="text" value={studentName}
              placeholder="Student" onChange={(e) => setStudentName(e.target.value)} />
          </label>
        </div>
        <div style={{ marginTop: 10 }}>
          <Button variant="primary" icon="upload" loading={busy} onClick={submit}>
            Inspect &amp; plan
          </Button>
        </div>
      </div>

      <div className="card">
        <h3>Submission queue</h3>
        {jobs.length === 0 ? (
          <Empty icon="file" title="No large-PDF jobs yet"
            hint="Upload a long answer booklet above to inspect and batch it." />
        ) : (
          <DataTable head={<tr>
            <th>Student</th><th>Pages</th><th>Batches</th>
            <th>Stage</th><th>Status</th><th /></tr>}>
            {jobs.map((job) => (
              <tr key={job.id}>
                <td>{job.student_name || "—"}</td>
                <td>{job.page_count || "—"}</td>
                <td>{job.batch_count || "—"}</td>
                <td className="muted small">{job.step_detail || job.status_label}</td>
                <td><Badge tone={statusTone(job.status)}>{job.status_label}</Badge></td>
                <td>
                  <Button size="sm" variant="ghost"
                    onClick={() => act("Opened", () => largePdfJob(job.id))}>
                    Open
                  </Button>
                </td>
              </tr>
            ))}
          </DataTable>
        )}
      </div>

      {liveSelected && (
        <LargePdfJobDetail
          job={liveSelected}
          busy={busy}
          onClose={() => setSelected(null)}
          onRetryJob={() => act("Job retry started", () => largePdfRetryJob(liveSelected.id))}
          onCancel={() => act("Job cancelled", () => largePdfCancel(liveSelected.id))}
          onRetryBatch={(index) => act(`Retrying batch ${index + 1}…`,
            () => largePdfRetryBatch(liveSelected.id, index))}
          onOpenResult={(resultId) => onNavigate?.(`#/results/${resultId}`)}
          onRefresh={() => act("Refreshed", () => largePdfJob(liveSelected.id))}
        />
      )}
    </>
  );
}

/**
 * Live detail for one batched job — exported so the unified correction page
 * renders the SAME batch view instead of growing a second one (§3).
 */
export function LargePdfJobDetail({
  job, busy, onClose, onRetryJob, onCancel, onRetryBatch, onOpenResult, onRefresh,
}: {
  job: LargePdfJob;
  busy: boolean;
  onClose: () => void;
  onRetryJob: () => void;
  onCancel: () => void;
  onRetryBatch: (index: number) => void;
  onOpenResult: (resultId: string) => void;
  onRefresh: () => void;
}) {
  const batches = job.batches ?? [];
  return (
    <div className="card">
      <h3 style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
        <span>{job.student_name || "Student"} — {job.page_count}-page document</span>
        <span style={{ display: "flex", gap: 8 }}>
          <Badge tone={statusTone(job.status)}>{job.status_label}</Badge>
          <Button size="sm" variant="ghost" onClick={onClose}>Close</Button>
        </span>
      </h3>

      <Progress value={runProgress(job.status)} />
      <div className="muted small" style={{ margin: "6px 0 10px" }}>
        {job.step_detail || job.status_label}
        {job.updated_at ? ` · updated ${timeAgo(job.updated_at)}` : ""}
      </div>

      {job.error && (
        <div className="notice err" style={{ marginBottom: 10 }}>
          <span>{job.error}</span>
        </div>
      )}

      {job.summary && (
        <div className="grid grid-4" style={{ marginBottom: 12 }}>
          <Stat compact label="Batches" value={job.summary.total} />
          <Stat compact label="Completed" value={job.summary.completed} tone="ok" />
          <Stat compact label="Failed" value={job.summary.failed}
            tone={job.summary.failed > 0 ? "err" : undefined} />
          <Stat compact label="Review" value={job.summary.review}
            tone={job.summary.review > 0 ? "warn" : undefined} />
        </div>
      )}

      {batches.length > 0 && (
        <DataTable head={<tr>
          <th>Batch</th><th>Pages</th><th>Questions</th>
          <th>Missing</th><th>Status</th><th /></tr>}>
          {batches.map((b: LargePdfBatch) => (
            <tr key={b.batch_index}>
              <td>{b.batch_number} / {batches.length}</td>
              <td>{b.page_start}–{b.page_end}</td>
              <td>{b.validated_questions ?? "—"}</td>
              <td className={b.missing_questions.length ? "warn" : "muted"}>
                {b.missing_questions.length ? b.missing_questions.join(", ") : "—"}
              </td>
              <td><Badge tone={batchTone(b.status)}>{b.status_label}</Badge></td>
              <td>
                {b.status === "FAILED" && (
                  <Button size="sm" variant="warning" disabled={busy}
                    onClick={() => onRetryBatch(b.batch_index)}>
                    Retry batch
                  </Button>
                )}
              </td>
            </tr>
          ))}
        </DataTable>
      )}

      {job.plan_notes && job.plan_notes.length > 0 && (
        <div className="muted small" style={{ marginTop: 10 }}>
          {job.plan_notes.map((note, i) => <div key={i}>• {note}</div>)}
        </div>
      )}

      <div style={{ display: "flex", gap: 8, marginTop: 12, flexWrap: "wrap" }}>
        <Button size="sm" onClick={onRefresh} disabled={busy}>Refresh</Button>
        {job.retryable && (
          <Button size="sm" variant="warning" onClick={onRetryJob} disabled={busy}>
            Retry failed batches
          </Button>
        )}
        {job.result_id && (
          <Button size="sm" variant="primary" onClick={() => onOpenResult(job.result_id!)}>
            Open correction result
          </Button>
        )}
        {job.status !== "COMPLETED" && job.status !== "CANCELLED" && (
          <Button size="sm" variant="danger" onClick={onCancel} disabled={busy}>
            Cancel
          </Button>
        )}
      </div>
    </div>
  );
}
