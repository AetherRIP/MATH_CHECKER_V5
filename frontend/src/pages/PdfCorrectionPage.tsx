import { useId, useRef, useState } from "react";
import {
  unifiedCorrection, type UnifiedCorrectionResult,
} from "../api";
import {
  Badge, Button, Field, Icon, PageHeader, Stamp, useToast,
} from "../components";
import { friendlyError } from "../errors";
import { JobQueue } from "./CorrectionsPage";

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
      className={`dropzone usk-dropzone ${dragging ? "dragging" : ""}`}
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
 * ONE correction entry point.
 *
 * The student PDF and the official mark scheme go to the standard
 * single-pass correction engine. Nothing here marks anything itself.
 */
export default function PdfCorrectionPage({ onOpenResult }: {
  onOpenResult?: (resultId: string) => void;
}) {
  const toast = useToast();
  const [studentName, setStudentName] = useState("");
  const [submission, setSubmission] = useState<File | null>(null);
  const [markScheme, setMarkScheme] = useState<File | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<ReturnType<typeof friendlyError> | null>(null);
  const [outcome, setOutcome] = useState<UnifiedCorrectionResult | null>(null);
  const [queueKey, setQueueKey] = useState(0);
  const subId = useId();
  const msId = useId();

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
    setBusy(true); setError(null); setOutcome(null);
    try {
      const form = new FormData();
      form.append("submission", submission);
      form.append("mark_scheme", markScheme);
      form.append("student_name",
        studentName.trim() || submission.name.replace(/\.[^.]+$/, ""));
      const result = await unifiedCorrection(form);
      setOutcome(result);
      toast("ok", result.page_count
        ? `Queued for correction — ${result.page_count} pages. Progress appears below.`
        : "Queued for correction — progress appears below.");
      setStudentName("");
      setSubmission(null); setMarkScheme(null);
      setQueueKey((n) => n + 1);
    } catch (e) {
      setError(friendlyError(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <>
      <PageHeader
        kicker="Correct · New correction"
        title="Correct a paper"
        sub="Upload the student's work and the official mark scheme. Page counts, question matching and marking all happen on your machine."
        meta={
          <>
            <span>page count read on upload</span>
            <span aria-hidden="true">·</span>
            <span>PDF, PNG or JPG</span>
          </>
        }
      />

      <div className="upload-layout">
        <section className="card" aria-labelledby="upload-heading">
          <div className="row between wrap" style={{ marginBottom: "var(--s4)" }}>
            <h2 id="upload-heading" className="card-title">
              <span className="step-n" aria-hidden="true">1</span>
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

          {error && (
            <div className="notice err" role="alert" style={{ marginTop: "var(--s4)" }}>
              <Icon name="alert" size={16} />
              <span>
                <strong>{error.summary}</strong>
                {error.action && <> {error.action}</>}
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
              {busy ? "Uploading…" : "Inspect and correct"}
            </Button>
          </div>
        </section>

        <aside className="stack" style={{ minWidth: 0 }}>
          <section className="card" aria-labelledby="how-heading">
            <h2 id="how-heading" className="card-title" style={{ marginBottom: "var(--s3)" }}>
              What happens next
            </h2>
            <ol className="steps">
              <li>
                <span className="step-n">1</span>
                <span>
                  <strong>Inspect</strong>
                  <span className="faint small">The page count and layout are read.</span>
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
                <h2 id="outcome-heading" className="card-title">
                  Queued
                </h2>
                <Badge tone="ok">Single pass</Badge>
              </div>
              <div className="kv">
                <span className="k">Pages</span>
                <span className="v mono">{outcome.page_count}</span>
              </div>
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

      <div style={{ marginTop: "var(--s4)" }}>
        <JobQueue key={queueKey} compact activeJobId={outcome?.job_id ?? null} />
      </div>
    </>
  );
}
