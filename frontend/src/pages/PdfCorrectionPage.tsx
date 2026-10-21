import { useCallback, useEffect, useId, useMemo, useRef, useState } from "react";
import {
  correctionPipelines, largePdfCancel, largePdfJob, largePdfJobs,
  largePdfRetryBatch, largePdfRetryJob, unifiedCorrection,
  type LargePdfJob, type PipelineInfo, type UnifiedCorrectionResult,
} from "../api";
import {
  Badge, Button, Field, Icon, PageHeader, Section, Stamp, useToast,
} from "../components";
import { friendlyError } from "../errors";
import { useAdaptivePoll } from "../useAdaptivePoll";
import { startLargeJobPoll } from "../largePdfPolling";
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
 * One file slot: a real <input type="file">, a keyboard-reachable button and a
 * drag target. The input stays in the tab order — a div with onClick alone
 * would make uploading impossible without a mouse.
 */
function FileSlot({ id, label, hint, accept, file, onPick, icon }: {
  id: string; label: string; hint: string; accept: string;
  file: File | null; onPick: (f: File | null) => void; icon: "file" | "review";
}) {
  const input = useRef<HTMLInputElement>(null);
  const [dragging, setDragging] = useState(false);

  function take(list: FileList | null) {
    onPick(list?.[0] ?? null);
  }

  return (
    <div
      className={`dropzone ${dragging ? "dragging" : ""}`}
      onDragOver={(e) => { e.preventDefault(); setDragging(true); }}
      onDragLeave={() => setDragging(false)}
      onDrop={(e) => {
        e.preventDefault();
        setDragging(false);
        if (e.dataTransfer.files?.length) take(e.dataTransfer.files);
      }}
    >
      <input
        ref={input}
        id={id}
        type="file"
        accept={accept}
        onChange={(e) => take(e.target.files)}
      />
      <div className="row" style={{ gap: 8 }}>
        <Icon name={file ? "check-circle" : icon} size={16}
          style={{ color: file ? "var(--ok)" : "var(--text-faint)" }} />
        <span className="dz-title">{label}</span>
      </div>
      {file ? (
        <>
          <div className="filecard" style={{ width: "100%" }}>
            <Icon name="file" size={16} />
            <div className="fc-body">
              <div className="fc-name">{file.name}</div>
              <div className="fc-meta">{humanBytes(file.size)}</div>
            </div>
            <div className="fc-actions">
              <Button size="sm" variant="ghost" type="button"
                onClick={() => input.current?.click()}>Replace</Button>
            </div>
          </div>
          <div className="dz-hint">{hint}</div>
        </>
      ) : (
        <>
          <div className="dz-hint">{hint}</div>
          <div className="dz-actions">
            <Button size="sm" variant="secondary" type="button" icon="upload"
              onClick={() => input.current?.click()}>
              Choose file
            </Button>
            <span className="dz-hint">or drop it here</span>
          </div>
        </>
      )}
    </div>
  );
}

/**
 * ONE correction entry point (§3).
 *
 * The backend reads the student PDF's real page count and routes to the
 * EXISTING single-pass engine or the EXISTING large-PDF worker. Advanced
 * options exposes the same choice manually, and the response always names the
 * pipeline that actually ran. Nothing here marks anything itself.
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
  const [error, setError] = useState<ReturnType<typeof friendlyError> | null>(null);
  const [outcome, setOutcome] = useState<UnifiedCorrectionResult | null>(null);
  const [largeJob, setLargeJob] = useState<LargePdfJob | null>(null);
  const [queueKey, setQueueKey] = useState(0);
  const subId = useId();
  const msId = useId();

  useEffect(() => {
    correctionPipelines().then(setPipelines).catch(() => {});
  }, []);

  // Follow a batched job with the real per-batch rows from the backend.
  const largeJobId = outcome?.large_pdf_job_id ?? null;
  const [largePollToken, setLargePollToken] = useState(0);

  useEffect(() => {
    if (!largeJobId) { setLargeJob(null); return; }
    return startLargeJobPoll({
      fetchJob: () => largePdfJob(largeJobId),
      onJob: setLargeJob,
    });
  }, [largeJobId, largePollToken]);

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

  const ready = Boolean(submission && markScheme);

  async function submit() {
    if (!submission || !markScheme) {
      setError({
        summary: "Both documents are needed.",
        action: "Choose the student's work and the official mark scheme.",
        raw: "missing file",
      });
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
      setQueueKey((n) => n + 1);
    } catch (e) {
      setError(friendlyError(e));
    } finally {
      setBusy(false);
    }
  }

  async function act(label: string, fn: () => Promise<LargePdfJob>) {
    setBusy(true);
    try {
      setLargeJob(await fn());
      setLargePollToken((t) => t + 1); // resume polling if the job moved
      toast("ok", label);
    } catch (e) {
      toast("err", friendlyError(e).summary);
    } finally {
      setBusy(false);
    }
  }

  const limit = pipelines?.threshold_pages;

  return (
    <>
      <PageHeader
        title="Correct a PDF"
        sub="Upload the student's work and the official mark scheme. Page counts, question matching and marking all happen on your machine."
        meta={
          <>
            <span>{limit ? `up to ${limit} pages in one pass` : "page count checked on upload"}</span>
            <span aria-hidden="true">·</span>
            <span>PDF, PNG or JPG</span>
            {largeActive > 0 && (
              <>
                <span aria-hidden="true">·</span>
                <span>{largeActive} batched document{largeActive === 1 ? "" : "s"} in progress</span>
              </>
            )}
          </>
        }
      >
        {largeActive > 0 && (
          <Button variant="ghost" icon="cpu" onClick={() => { window.location.hash = "#/largepdf"; }}>
            View batched jobs
          </Button>
        )}
      </PageHeader>

      <div className="upload-layout">
        <section className="card" aria-labelledby="upload-heading">
          <div className="row between wrap" style={{ marginBottom: "var(--s4)" }}>
            <h2 id="upload-heading" style={{ margin: 0, fontSize: "var(--fs-md)" }}>
              The two documents
            </h2>
            <Stamp>{ready ? "Both chosen" : "Two files needed"}</Stamp>
          </div>

          <Field label="Student name (optional)" hint="Used to name the result; falls back to the file name.">
            <input
              className="input"
              placeholder="e.g. Alice Wong"
              value={studentName}
              onChange={(e) => setStudentName(e.target.value)}
            />
          </Field>

          <div className="grid grid-2" style={{ gap: "var(--s3)" }}>
            <FileSlot
              id={subId}
              label="Student's work"
              hint="The completed paper, scanned or exported."
              icon="file"
              accept={ACCEPT}
              file={submission}
              onPick={(f) => { setSubmission(f); setError(null); }}
            />
            <FileSlot
              id={msId}
              label="Official mark scheme"
              hint="PDF only. Marks and method come from here."
              icon="review"
              accept=".pdf"
              file={markScheme}
              onPick={(f) => { setMarkScheme(f); setError(null); }}
            />
          </div>

          <div style={{ marginTop: "var(--s4)" }}>
            <button className="btn ghost sm" type="button"
              aria-expanded={advanced}
              onClick={() => setAdvanced((v) => !v)}>
              <Icon name={advanced ? "chevron-down" : "chevron-right"} size={14} />
              Advanced: choose the pipeline
              {pipeline !== "auto" && (
                <Badge tone="warn">{pipeline === "large" ? "batched" : "single pass"} forced</Badge>
              )}
            </button>

            {advanced && (
              <div style={{ marginTop: "var(--s2)" }}>
                <div className="stack" style={{ gap: 6 }}>
                  {(pipelines?.options ?? []).map((o) => (
                    <label key={o.value} className={`choice ${pipeline === o.value ? "checked" : ""}`}>
                      <input type="radio" name="pipeline" value={o.value}
                        checked={pipeline === o.value}
                        disabled={o.value === "large" && pipelines?.large_pdf_enabled === false}
                        onChange={() => setPipeline(o.value as Pipeline)} />
                      <span className="choice-body">
                        <span className="choice-title">{o.label}</span>
                        <span className="choice-detail">
                          {o.detail}
                          {o.value === "large" && pipelines?.large_pdf_enabled === false
                            ? " — currently switched off in Settings." : ""}
                        </span>
                      </span>
                    </label>
                  ))}
                </div>
                <p className="faint small" style={{ margin: "var(--s2) 0 0" }}>
                  Automatic is the same rule the whole application uses, so every
                  entry point agrees. Forcing a method changes only which existing
                  pipeline runs for this upload.
                </p>
              </div>
            )}
          </div>

          {error && (
            <div className="notice err" role="alert" style={{ marginTop: "var(--s4)" }}>
              <Icon name="alert" size={16} />
              <span>
                <strong>{error.summary}</strong>
                {error.action && <> {error.action}</>}
              </span>
            </div>
          )}

          {pipelines?.large_pdf_enabled === false && pipeline === "auto" && (
            <div className="notice warn small" style={{ marginTop: "var(--s4)" }}>
              <Icon name="info" size={15} />
              <span>
                Batched correction is switched off in Settings, so a document over
                {" "}{pipelines.threshold_pages} pages will be refused until it is
                switched back on.
              </span>
            </div>
          )}

          <div className="row wrap between" style={{ marginTop: "var(--s5)", gap: "var(--s3)" }}>
            <span className="faint small">
              {ready
                ? "Nothing is marked until you press the button — the server reads the real page count first."
                : "Add both documents to continue."}
            </span>
            <Button variant="primary" icon="wand" loading={busy}
              disabled={!ready} onClick={submit}>
              {busy ? "Inspecting…" : "Inspect and correct"}
            </Button>
          </div>
        </section>

        <aside className="stack" style={{ minWidth: 0 }}>
          <section className="card" aria-labelledby="how-heading">
            <h2 id="how-heading" style={{ margin: "0 0 var(--s3)", fontSize: "var(--fs-md)" }}>
              What happens next
            </h2>
            <ol className="steps">
              <li>
                <span className="step-n">1</span>
                <span>
                  <strong>Inspect</strong>
                  <span className="faint small">The page count and layout are read; the pipeline is chosen.</span>
                </span>
              </li>
              <li>
                <span className="step-n">2</span>
                <span>
                  <strong>Match</strong>
                  <span className="faint small">Questions are tied to the mark scheme, not guessed from the paper alone.</span>
                </span>
              </li>
              <li>
                <span className="step-n">3</span>
                <span>
                  <strong>Mark and verify</strong>
                  <span className="faint small">Every mark is checked once more before it is saved.</span>
                </span>
              </li>
              <li>
                <span className="step-n">4</span>
                <span>
                  <strong>Decide</strong>
                  <span className="faint small">Anything uncertain goes to Review with the evidence attached.</span>
                </span>
              </li>
            </ol>
            <p className="faint small" style={{ margin: "var(--s3) 0 0" }}>
              The grade scale 1–9 comes from your configured boundaries. The AI
              never invents a grade.
            </p>
          </section>

          {outcome && (
            <section className="card" aria-labelledby="outcome-heading" aria-live="polite">
              <div className="row between" style={{ marginBottom: "var(--s3)" }}>
                <h2 id="outcome-heading" style={{ margin: 0, fontSize: "var(--fs-md)" }}>
                  Queued
                </h2>
                <Badge tone={outcome.applied_pipeline === "large" ? "info" : "ok"}>
                  {outcome.applied_pipeline === "large" ? "Batched" : "Single pass"}
                </Badge>
              </div>
              <div className="kv">
                <span className="k">Pages</span>
                <span className="v mono">{outcome.page_count}</span>
              </div>
              {outcome.job.batch_count != null && (
                <div className="kv">
                  <span className="k">Batches</span>
                  <span className="v mono">{outcome.job.batch_count}</span>
                </div>
              )}
              {outcome.files?.submission && (
                <div className="kv">
                  <span className="k">File</span>
                  <span className="v truncate" style={{ maxWidth: 200 }}>
                    {outcome.files.submission.name}
                  </span>
                </div>
              )}
              <p className="small muted" style={{ margin: "var(--s3) 0 0" }}>
                {outcome.reason}
              </p>
              {outcome.job_id && onOpenResult && (
                <Button size="sm" variant="secondary" iconRight="arrow-right"
                  style={{ marginTop: "var(--s3)" }}
                  onClick={() => onOpenResult(outcome.job_id!)}>
                  Open the job
                </Button>
              )}
            </section>
          )}
        </aside>
      </div>

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

      <div style={{ marginTop: "var(--s4)" }}>
        <JobQueue key={queueKey} compact activeJobId={outcome?.job_id ?? null} />
      </div>
    </>
  );
}
