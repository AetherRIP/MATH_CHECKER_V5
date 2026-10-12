import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  correctionPipelines, largePdfCancel, largePdfJob, largePdfJobs,
  largePdfRetryBatch, largePdfRetryJob, unifiedCorrection,
  type LargePdfJob, type PipelineInfo, type UnifiedCorrectionResult,
} from "../api";
import { Badge, Button, Icon, Section, useToast } from "../components";
import { useAdaptivePoll } from "../useAdaptivePoll";
import { JobQueue } from "./CorrectionsPage";
import { LargePdfJobDetail } from "./LargePdfWorkerPage";

type Pipeline = "auto" | "standard" | "large";

function humanBytes(bytes: number): string {
  if (!bytes) return "0 B";
  let value = bytes;
  for (const unit of ["B", "KB", "MB", "GB"]) {
    if (value < 1024) return `${value.toFixed(value < 10 && unit !== "B" ? 1 : 0)} ${unit}`;
    value /= 1024;
  }
  return `${value.toFixed(1)} GB`;
}

const ACCEPT = ".pdf,.png,.jpg,.jpeg,.webp";

/**
 * ONE PDF correction page (§3).
 *
 * The two former pages ("PDF Correction" and "Large PDF Correction") are now
 * one entry point. Nothing was merged in the engine: the backend takes the
 * student PDF's real page count and routes to the EXISTING single-pass engine
 * or the EXISTING large-PDF worker, exactly as the AI-correction engine always
 * has. Advanced Options exposes the same choice manually, and the response
 * always names the pipeline that actually ran.
 */
export default function PdfCorrectionPage({ onOpenResult }: {
  onOpenResult?: (resultId: string) => void;
}) {
  const toast = useToast();
  const [pipelines, setPipelines] = useState<PipelineInfo | null>(null);
  const [studentName, setStudentName] = useState("");
  const [submission, setSubmission] = useState<File | null>(null);
  const [markScheme, setMarkScheme] = useState<File | null>(null);
  const [pipeline, setPipeline] = useState<Pipeline>("auto");
  const [advanced, setAdvanced] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [outcome, setOutcome] = useState<UnifiedCorrectionResult | null>(null);
  const [largeJob, setLargeJob] = useState<LargePdfJob | null>(null);
  const [queueKey, setQueueKey] = useState(0);
  const subRef = useRef<HTMLInputElement>(null);
  const msRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    correctionPipelines().then(setPipelines).catch(() => {});
  }, []);

  // Follow a batched job with the real per-batch rows from the backend.
  const largeJobId = outcome?.large_pdf_job_id ?? null;
  const refreshLarge = useCallback(() => {
    if (!largeJobId) return;
    largePdfJob(largeJobId).then(setLargeJob).catch(() => {});
  }, [largeJobId]);

  useEffect(() => {
    if (!largeJobId) { setLargeJob(null); return; }
    refreshLarge();
    const id = setInterval(refreshLarge, 2500);
    return () => clearInterval(id);
  }, [largeJobId, refreshLarge]);

  // The queued summary cards come from the real listing, so the page can say
  // how many batched documents are still running.
  const [largeListing, setLargeListing] = useState<{ counts: Record<string, number> } | null>(null);
  const loadLargeListing = useCallback(() => largePdfJobs()
    .then((l) => setLargeListing({ counts: l.counts as unknown as Record<string, number> }))
    .catch(() => {}), []);
  useEffect(() => { void loadLargeListing(); }, [loadLargeListing]);

  const largeActive = useMemo(() => {
    const c = largeListing?.counts ?? {};
    return (c.queued ?? 0) + (c.ready ?? 0) + (c.processing ?? 0)
      + (c.inspecting ?? 0) + (c.planning ?? 0);
  }, [largeListing]);

  // The queue summary only needs to move while the batched worker is busy.
  useAdaptivePoll(loadLargeListing,
    { active: largeActive > 0, fast: 5000, idle: 30_000, immediate: false });

  async function submit() {
    if (!submission || !markScheme) {
      setError("Choose both the student submission and the official mark scheme.");
      return;
    }
    setBusy(true); setError(null); setOutcome(null); setLargeJob(null);
    try {
      const form = new FormData();
      form.append("submission", submission);
      form.append("mark_scheme", markScheme);
      form.append("student_name",
        studentName.trim() || submission.name.replace(/\.[^.]+$/, ""));
      form.append("pipeline", pipeline);
      const result = await unifiedCorrection(form);
      setOutcome(result);
      toast("ok", result.applied_pipeline === "large"
        ? `Queued for batched correction — ${result.page_count} pages in ${result.job.batch_count ?? "?"} batch(es).`
        : "Queued for the single-pass pipeline — progress appears below.");
      setStudentName("");
      setSubmission(null); setMarkScheme(null);
      if (subRef.current) subRef.current.value = "";
      if (msRef.current) msRef.current.value = "";
      setQueueKey((n) => n + 1);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  async function act(label: string, fn: () => Promise<LargePdfJob>) {
    setBusy(true);
    try {
      setLargeJob(await fn());
      toast("ok", label);
    } catch (e) {
      toast("err", (e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <>
      <Section title="Correct a PDF" icon="wand"
        hint={pipelines
          ? `${pipelines.threshold_pages}-page single-pass limit · larger documents are batched`
          : undefined}>
        {largeActive > 0 && (
          <div className="row wrap small" style={{ gap: 8, marginBottom: 10 }}>
            <Badge tone="info" icon="cpu">
              {largeActive} batched document(s) in progress
            </Badge>
          </div>
        )}

        <div className="grid grid-3" style={{ alignItems: "end" }}>
          <label className="field">
            <span>Student name</span>
            <input className="input" placeholder="e.g. Alice Wong"
              value={studentName}
              onChange={(e) => setStudentName(e.target.value)} />
          </label>
          <label className="field">
            <span>Student submission</span>
            <input className="input" type="file" accept={ACCEPT} ref={subRef}
              onChange={(e) => setSubmission(e.target.files?.[0] ?? null)} />
          </label>
          <label className="field">
            <span>Official mark scheme (PDF)</span>
            <input className="input" type="file" accept=".pdf" ref={msRef}
              onChange={(e) => setMarkScheme(e.target.files?.[0] ?? null)} />
          </label>
        </div>

        {(submission || markScheme) && (
          <div className="row wrap faint small" style={{ gap: 14, marginTop: 2 }}>
            {submission && (
              <span><Icon name="file" size={13} /> {submission.name} ·{" "}
                {humanBytes(submission.size)}</span>
            )}
            {markScheme && (
              <span><Icon name="file" size={13} /> {markScheme.name} ·{" "}
                {humanBytes(markScheme.size)}</span>
            )}
            <span>Page count is read by the server before anything is marked.</span>
          </div>
        )}

        {/* ---------- Advanced options: one control, not a second page -------- */}
        <div style={{ marginTop: 12 }}>
          <button className="btn ghost sm" type="button"
            aria-expanded={advanced}
            onClick={() => setAdvanced((v) => !v)}>
            <Icon name={advanced ? "chevron-down" : "chevron-right"} size={14} />
            {" "}Advanced options
            {pipeline !== "auto" && (
              <Badge tone="warn">
                {pipeline === "large" ? "batched" : "single-pass"} forced
              </Badge>
            )}
          </button>

          {advanced && (
            <div style={{ marginTop: 10 }}>
              <div className="row wrap" style={{ gap: 12 }}>
                {(pipelines?.options ?? []).map((o) => (
                  <label key={o.value} className="row small"
                    style={{ gap: 7, alignItems: "flex-start", maxWidth: 320 }}>
                    <input type="radio" name="pipeline" value={o.value}
                      checked={pipeline === o.value}
                      disabled={o.value === "large" && pipelines?.large_pdf_enabled === false}
                      onChange={() => setPipeline(o.value as Pipeline)}
                      style={{ marginTop: 3 }} />
                    <span>
                      <strong>{o.label}</strong>
                      <span className="faint" style={{ display: "block" }}>
                        {o.detail}
                        {o.value === "large" && pipelines?.large_pdf_enabled === false
                          ? " — currently switched off in Settings." : ""}
                      </span>
                    </span>
                  </label>
                ))}
              </div>
              <p className="faint small" style={{ marginBottom: 0, marginTop: 8 }}>
                Automatic is the same rule the whole application uses, so every
                entry point agrees. Forcing a method changes only which existing
                pipeline runs for this upload.
              </p>
            </div>
          )}
        </div>

        {error && (
          <div className="notice err" style={{ marginTop: 12 }} role="alert">
            {error}
          </div>
        )}

        {pipelines?.large_pdf_enabled === false && pipeline === "auto" && (
          <div className="notice warn small" style={{ marginTop: 12 }}>
            <Icon name="info" size={14} /> The batched pipeline is switched off
            in Settings, so a document above {pipelines.threshold_pages} pages
            will be refused until it is switched back on.
          </div>
        )}

        <div style={{ marginTop: 12 }}>
          <Button variant="primary" icon="upload" loading={busy}
            disabled={!submission || !markScheme} onClick={submit}>
            {busy ? "Inspecting & queueing…" : "Inspect and correct"}
          </Button>
        </div>

        {outcome && (
          <div className={`notice ${outcome.applied_pipeline === "large" ? "info" : "ok"} small`}
            style={{ marginTop: 12 }}>
            <div className="row wrap" style={{ gap: 8, alignItems: "center" }}>
              <Badge tone={outcome.applied_pipeline === "large" ? "info" : "ok"}>
                {outcome.applied_pipeline === "large"
                  ? "Batched large-PDF pipeline" : "Single-pass pipeline"}
              </Badge>
              <span>{outcome.page_count} page(s)</span>
              {outcome.applied_pipeline === "large" && outcome.job.batch_count && (
                <span>· {outcome.job.batch_count} batch(es)</span>
              )}
              {outcome.files?.submission && (
                <span className="faint">
                  · {outcome.files.submission.name}{" "}
                  {humanBytes(outcome.files.submission.bytes)}
                </span>
              )}
            </div>
            <div style={{ marginTop: 4 }}>{outcome.reason}</div>
          </div>
        )}
      </Section>

      {largeJob && (
        <Section title="Batched correction detail" icon="cpu"
          hint="inspect → plan → mark batches → validate → merge">
          <LargePdfJobDetail
            job={largeJob}
            busy={busy}
            onClose={() => setLargeJob(null)}
            onRetryJob={() => act("Job retry started",
              () => largePdfRetryJob(largeJob.id))}
            onCancel={() => act("Job cancelled", () => largePdfCancel(largeJob.id))}
            onRetryBatch={(index) => act(`Retrying batch ${index + 1}…`,
              () => largePdfRetryBatch(largeJob.id, index))}
            onOpenResult={(resultId) => onOpenResult?.(resultId)}
            onRefresh={() => act("Refreshed", () => largePdfJob(largeJob.id))}
          />
        </Section>
      )}

      <JobQueue key={queueKey} compact
        activeJobId={outcome?.job_id ?? null} />

      <p className="faint small" style={{ textAlign: "center" }}>
        Grade scale 1–9 is looked up from the configured boundaries; the AI
        never invents a grade.
      </p>
    </>
  );
}
