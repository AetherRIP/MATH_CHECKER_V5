import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  api, displayName, timeAgo, type AttachmentStatus, type Course, type Coursework,
  type ReportCleanupOutcome,
  type RemarkSide, type RemarkStatus,
  type ReportAttachmentState, type RetryAction,
} from "../api";
import {
  Badge, Button, Dropdown, Empty, ErrorState, ExportMenu, Icon, Loading,
  Modal, PageHeader, ProvenanceBadge, Segmented, Stat, WorkspacePanes,
  useToast, type IconName, type Tone,
} from "../components";
import { prefersReducedMotion } from "../motion";
import {
  MASTERY_LABEL, TRIAGE_LABEL, TRIAGE_RANK, buildTopicGroups, questionTriage,
  type MasteryBand,
} from "../topicGroups";
import { useSort, type SortColumns } from "../useSort";

interface ResultVersion {
  version: number; is_current: boolean; earned: number; available: number;
  percentage: number; provisional_grade: string | null;
  marking_model: string | null; confidence: string | null;
  note: string | null; created_at: number;
}

function VersionsPanel({ resultId, onRestored }: {
  resultId: string; onRestored: () => void;
}) {
  const toast = useToast();
  const [versions, setVersions] = useState<ResultVersion[] | null>(null);
  const [busy, setBusy] = useState<number | null>(null);

  const load = useCallback(() => {
    api<{ versions: ResultVersion[] }>(`/results/${resultId}/versions`)
      .then((r) => setVersions(r.versions)).catch(() => setVersions([]));
  }, [resultId]);
  useEffect(() => { load(); }, [load]);

  async function useVersion(v: ResultVersion) {
    setBusy(v.version);
    try {
      await api(`/results/${resultId}/versions/${v.version}/restore`, { method: "POST" });
      toast("ok", `Correction v${v.version} is now the final result`);
      load();
      onRestored();
    } catch (e) { toast("err", (e as Error).message); } finally { setBusy(null); }
  }

  if (versions === null) return null;
  if (versions.length <= 1) return null;

  return (
    <div className="card">
      <h3>Correction history <span className="hint">a recheck never overwrites a previous version</span></h3>
      <div className="table-wrap">
        <table className="table">
          <thead><tr><th>Version</th><th className="num">Score</th><th>Model</th>
            <th>Confidence</th><th>When</th><th>Note</th><th /></tr></thead>
          <tbody>
            {versions.map((v) => (
              <tr key={v.version}>
                <td data-label="Version">v{v.version}{" "}
                  {v.is_current && <Badge tone="ok">Current</Badge>}
                </td>
                <td data-label="Score" className="num mono">
                  {v.earned}/{v.available}{" "}
                  <span className="faint small">({Math.round(v.percentage)}%)</span>
                </td>
                <td data-label="Model" className="small faint">{v.marking_model ?? "—"}</td>
                <td data-label="Confidence" className="small faint">
                  {v.confidence ? v.confidence.toLowerCase().replace(/_/g, " ") : "—"}
                </td>
                <td data-label="When" className="small faint">{timeAgo(v.created_at)}</td>
                <td data-label="Note" className="small muted wrap-cell">{v.note ?? "—"}</td>
                <td>
                  {!v.is_current && (
                    <Button size="sm" variant="secondary" icon="check"
                      loading={busy === v.version}
                      onClick={() => useVersion(v)}>Use as final</Button>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

type ResultStatus = "corrected" | "needs_review" | "overridden";

/** High-score double-check block (§19): shows the independent verification
 *  result clearly — never presenting the first score as the final one. */
function DoubleCheckPanel({ result }: { result: ResultDetail }) {
  if (!result.double_check_triggered) return null;
  const status = result.double_check_status ?? "";
  const first = result.first_earned;
  const firstMax = result.first_available;
  const second = result.second_earned;
  const secondMax = result.second_available;
  // The API may deliver the persisted differences column as a JSON string.
  const rawDiffs = result.double_check_differences;
  const diffs: DoubleCheckDifference[] = typeof rawDiffs === "string"
    ? (() => { try { return JSON.parse(rawDiffs) as DoubleCheckDifference[]; }
      catch { return []; } })()
    : (rawDiffs ?? []);
  const confirmed = status === "COMPLETE" && (second == null || second >= (first ?? 0));
  const review = status === "REVIEW_REQUIRED" || status === "DOUBLE_CHECK_FAILED";
  const tone = review ? "warn" : "ok";
  return (
    <div className="card" data-testid="double-check-panel">
      <div className="row" style={{ justifyContent: "space-between", alignItems: "center" }}>
        <h3 style={{ margin: 0 }}>Independent double-check</h3>
        <Badge tone={tone}>
          {status === "COMPLETE"
            ? (confirmed ? "✓ Independently double-checked — confirmed"
              : "✓ Independently double-checked")
            : status === "REVIEW_REQUIRED"
              ? "⚠ REVIEW REQUIRED — large disagreement"
              : "⚠ Double-check failed — first result kept"}
        </Badge>
      </div>
      <div className="grid grid-3" style={{ marginTop: 10 }}>
        <Stat compact label={`First correction${result.first_percentage != null
          ? ` (${result.first_percentage.toFixed(2)}%)` : ""}`}
          value={first != null ? `${first} / ${firstMax ?? "—"}` : "—"} />
        <Stat compact label={`Second correction (independent)${result.second_percentage != null
          ? ` (${result.second_percentage.toFixed(2)}%)` : ""}`}
          value={second != null ? `${second} / ${secondMax ?? "—"}` : "not completed"} />
        <Stat compact label={`Difference${result.disagreement_status
          ? ` (${result.disagreement_status.replace(/_/g, " ").toLowerCase()})` : ""}`}
          value={first != null && second != null ? `${Math.abs(first - second)} mark(s)` : "—"} />
      </div>
      <p className="small muted" style={{ marginBottom: 0 }}>
        Final result: <strong className="mono">{result.earned}/{result.available}</strong>{" "}
        ({result.percentage.toFixed(2)}%) — the final score above is the result
        of the independent verification, not the first correction.
      </p>
      {result.double_check_reason && (
        <p className="small muted" style={{ marginBottom: 0 }}>{result.double_check_reason}</p>
      )}
      {diffs.length > 0 && (
        <details className="expander" style={{ marginTop: 8 }}>
          <summary>Where the two corrections differed ({diffs.length} question(s))</summary>
          <div className="expander-body">
            <div className="table-wrap">
              <table className="table table-stack">
                <thead>
                  <tr><th>Q</th><th className="num">First</th><th className="num">Second</th>
                    <th>Second pass reason</th></tr>
                </thead>
                <tbody>
                  {diffs.map((d) => (
                    <tr key={d.question_number}>
                      <td data-label="Q" className="num">{d.question_number}</td>
                      <td data-label="First" className="num mono">{d.first_awarded}/{d.max_marks}</td>
                      <td data-label="Second" className="num mono">{d.second_awarded}/{d.max_marks}</td>
                      <td data-label="Reason" className="muted small wrap-cell">
                        {d.second_loss_reason || d.first_loss_reason || "—"}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>
        </details>
      )}
    </div>
  );
}

interface ResultSummary {
  id: string; student_name: string; display_name?: string | null;
  classroom_name?: string | null; nickname?: string | null;
  earned: number; available: number;
  percentage: number; provisional_grade: string; marking_model: string; created_at: number;
  // Enriched by GET /api/results (assignment/class context + derived status).
  status?: ResultStatus;
  assignment_title?: string | null;
  course_id?: string | null;
  course_name?: string | null;
  coursework_id?: string | null;
  publish_status?: string | null;
  version_count?: number;
  confidence?: string | null;
  review_required?: number;
  manual_override?: number;
  // High-score double-check (list payload + detail).
  double_check_triggered?: number;
  double_check_status?: string | null;
}

interface DoubleCheckDifference {
  question_number: string; first_awarded: number; second_awarded: number;
  max_marks: number; first_loss_reason: string; second_loss_reason: string;
}
// A per-question mark-scheme verification result. Present ONLY when the engine
// had a defensible mathematical reason to disagree with the supplied mark
// scheme; ordinary questions carry none. Teacher-facing only, and deliberately
// factual: it never states a provider, model or prompt detail.
interface SchemeCheck {
  disagrees_with_scheme: boolean;
  student_answer_valid: boolean;
  scheme_answer: string;
  independently_verified_answer: string;
  discrepancy_type: string;
  evidence: string;
  confidence: number;
  justified_marks: number | null;
}
interface Question {
  question_number: string; max_marks: number; awarded_marks: number; topic: string;
  student_answer: string; working_summary: string; loss_reason: string;
  mark_breakdown: { code: string; awarded: boolean; reason: string }[];
  correct_method: string; full_mark_solution: string;
  scheme_check?: SchemeCheck;
  status?: "VALID" | "REVIEW";
  review_reason?: string;
  teacher_mark?: number;
  /** §4: the per-subquestion breakdown. Present only when the mark scheme
   *  declares sub-parts, and each entry carries ITS OWN marks, answers and
   *  M/A/B codes — never another part's. */
  subquestions?: {
    id: string; label: string; parent_id?: string;
    max_marks: number; awarded_marks: number;
    /** MARKED, or UNMARKED when the marker returned nothing for this part. */
    status: string;
    answers?: string[]; mark_codes?: string[];
  }[];
  subquestion_marks?: number;
  /** Labels of the declared marking points the marker did not return. */
  unmarked_subquestions?: string[];
}
interface ResultDetail extends ResultSummary {
  student_id: string | null; submission_id: string | null; processing_seconds: number | null;
  key_fingerprint: string | null;
  // High-score double-check detail fields.
  first_earned?: number | null; first_available?: number | null; first_percentage?: number | null;
  second_earned?: number | null; second_available?: number | null; second_percentage?: number | null;
  final_earned?: number | null; final_available?: number | null; final_percentage?: number | null;
  disagreement_status?: string | null; first_correction_id?: string | null;
  second_correction_id?: string | null; double_check_reason?: string | null;
  double_check_differences?: DoubleCheckDifference[] | null;
  /** Recorded by the verification guard; may arrive as a JSON string. */
  confidence_reasons?: string[] | string | null;
  result_json: {
    paper_info: Record<string, string>;
    questions: Question[];
    overall_notes: { summary: string; strengths: string[]; revision_areas: string[] };
    uncertainties: string[];
  };
  verification_json: {
    verified: boolean;
    status?: "VALID" | "REVIEW";
    anomalies: string[]; warnings: string[];
    review_questions?: string[];
    blueprint?: { source?: string; missing_questions?: string[] } | null;
  } | null;
}

/* ---- triage + confidence: derived from RECORDED fields, never invented ---- */

const CONFIDENCE_META: Record<string, { tone: Tone; label: string }> = {
  HIGH: { tone: "ok", label: "Confidence: high" },
  MEDIUM: { tone: "info", label: "Confidence: medium" },
  LOW: { tone: "warn", label: "Confidence: low — check the flagged questions" },
  VALIDATION_FAILED: { tone: "err", label: "Validation failed — treat as provisional" },
  LARGE_SCORE_CHANGE: { tone: "warn", label: "Large change vs the previous correction" },
};

function parseReasons(raw: ResultDetail["confidence_reasons"]): string[] {
  if (!raw) return [];
  if (typeof raw === "string") {
    try {
      const parsed = JSON.parse(raw);
      return Array.isArray(parsed) ? parsed.map(String) : [];
    } catch { return []; }
  }
  return Array.isArray(raw) ? raw.map(String) : [];
}

/**
 * Per-question triage pill. The tooltip states the derivation out loud:
 * this is a reading of recorded flags (unmarked parts, mark-scheme
 * disagreement, unreadable work) — NOT a model confidence score. The engine
 * records confidence at result level only.
 */
function TriagePill({ q }: { q: Question }) {
  const t = questionTriage(q);
  return (
    <span className={`triage-pill ${t}`}
      title="Derived from recorded flags (unmarked parts, mark-scheme disagreement, unreadable work) — not a model confidence score.">
      {TRIAGE_LABEL[t]}
    </span>
  );
}

function masteryTone(band: MasteryBand): Tone {
  return band === "secure" ? "ok" : band === "developing" ? "warn" : "err";
}

function QuestionTone({ awarded, max }: { awarded: number; max: number }) {
  if (awarded === max) return <Badge tone="ok">✓ Full marks</Badge>;
  if (awarded === 0) return <Badge tone="err">✗ 0 marks</Badge>;
  return <Badge tone="warn">◐ Partial</Badge>;
}

function QuestionCard({ q, resultId, onChanged }: {
  q: Question; resultId: string; onChanged: () => void;
}) {
  const toast = useToast();
  const needsReview = q.status === "REVIEW";
  const flag = q.scheme_check;
  const [draft, setDraft] = useState(String(q.awarded_marks));
  const [busy, setBusy] = useState(false);

  async function saveTeacherMark() {
    setBusy(true);
    try {
      await api(`/results/${resultId}/questions/${encodeURIComponent(q.question_number)}/mark`,
        { method: "POST", body: JSON.stringify({ awarded: Number(draft), note: "Set in the correction viewer" }) });
      toast("ok", `Question ${q.question_number} set to ${draft}/${q.max_marks}`);
      onChanged();
    } catch (e) { toast("err", (e as Error).message); } finally { setBusy(false); }
  }

  return (
    <details className="expander" id={`question-${q.question_number}`} style={{ marginBottom: 8 }}>
      <summary>
        <span style={{ minWidth: 90 }}>Question {q.question_number}</span>
        <span className="mono" style={{ fontWeight: 700 }}>
          {q.awarded_marks}/{q.max_marks}
        </span>
        <QuestionTone awarded={q.awarded_marks} max={q.max_marks} />
        <TriagePill q={q} />
        {needsReview && <Badge tone="warn">⚠ REVIEW — unmarked</Badge>}
        {flag && <Badge tone={flag.student_answer_valid ? "warn" : "info"}>
          ⚠ Mark-scheme check</Badge>}
        <span className="faint small" style={{ marginLeft: "auto", paddingLeft: 8 }}>
          {q.topic}
        </span>
      </summary>
      <div className="expander-body">
        {needsReview && (
          <div className="notice warn small" style={{ marginBottom: 10 }}>
            {q.review_reason || "This question could not be marked — treat the 0 as "
              + "unknown, not as a confirmed score."}
          </div>
        )}
        {/* §4/§9: which marking point earned what — including the ones the
            marker never returned, which are UNMARKED (not a confirmed 0). */}
        {(q.subquestions?.length ?? 0) > 0 && (
          <div className="table-wrap" style={{ marginBottom: 12 }}>
            <table className="table small">
              <thead>
                <tr><th>Subquestion</th><th className="num">Marks</th>
                  <th>Expected answer(s)</th><th>Marking points</th><th /></tr>
              </thead>
              <tbody>
                {q.subquestions!.map((s) => {
                  const unmarked = (s.status ?? "").toUpperCase() !== "MARKED";
                  return (
                    <tr key={s.id} style={unmarked ? { opacity: 0.75 } : undefined}>
                      <td><strong>{s.label || s.id}</strong></td>
                      <td className="num">
                        {unmarked ? <span className="muted">—</span>
                          : `${s.awarded_marks}/${s.max_marks}`}
                      </td>
                      <td>{(s.answers ?? []).length
                        ? s.answers!.join(" or ")
                        : <span className="muted">not stated</span>}</td>
                      <td>{(s.mark_codes ?? []).length
                        ? s.mark_codes!.join(", ")
                        : <span className="muted">none stated</span>}</td>
                      <td>{unmarked
                        ? <Badge tone="warn">UNMARKED</Badge>
                        : <Icon name="check" size={13} style={{ color: "var(--ok)" }} />}</td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
        {/* mark checklist first — the headline result */}
        {q.mark_breakdown.length > 0 && (
          <ul className="stack" style={{ listStyle: "none", padding: 0, margin: "0 0 12px", gap: 4 }}>
            {q.mark_breakdown.map((b, i) => (
              <li key={i} className="small">
                <span style={{ color: b.awarded ? "var(--ok)" : "var(--err)", fontWeight: 700 }}>
                  {b.awarded ? "✓" : "✗"}
                </span>{" "}
                <span className="mono faint">{b.code}</span> — {b.reason}
              </li>
            ))}
          </ul>
        )}
        {q.loss_reason && (
          <div className="notice warn small" style={{ marginBottom: 10 }}>
            <strong>Marks lost:</strong> {q.loss_reason}
          </div>
        )}
        {flag && (
          <div className="notice warn small" style={{ marginBottom: 10 }}>
            <strong>Mark-scheme check.</strong> The supplied mark scheme gives{" "}
            <span className="mono">{flag.scheme_answer || "no reference answer"}</span>;{" "}
            independent verification gives{" "}
            <span className="mono">{flag.independently_verified_answer || "—"}</span>.{" "}
            <em>{flag.discrepancy_type.replace(/_/g, " ")}</em>
            {flag.confidence != null &&
              <> · confidence {Math.round(flag.confidence * 100)}%</>}.
            {flag.evidence ? ` ${flag.evidence}` : ""}
            {flag.justified_marks != null && flag.student_answer_valid && (
              <> The mathematical recommendation is
                {" "}{flag.justified_marks}/{q.max_marks}; the recorded award is
                {" "}{q.awarded_marks}/{q.max_marks}.</>
            )}{" "}
            A suspected error is reviewed before it can affect other students&apos;
            grades.
          </div>
        )}
        <div className="grid grid-2">
          <div className="stack small" style={{ gap: 6 }}>
            <div>
              <strong className="small">Student answer / working</strong>
              <p className="muted small" style={{ margin: "2px 0 0" }}>
                {q.student_answer || q.working_summary || "No readable answer extracted."}
              </p>
            </div>
            <div>
              <strong className="small">Correct method</strong>
              <p className="muted small" style={{ margin: "2px 0 0" }}>
                {q.correct_method || "Not provided."}
              </p>
            </div>
          </div>
          <div className="stack small" style={{ gap: 6 }}>
            <div>
              <strong className="small">Full-mark solution</strong>
              <p className="muted small" style={{ margin: "2px 0 0" }}>
                {q.full_mark_solution || "Not provided."}
              </p>
            </div>
          </div>
        </div>

        {/* ---- teacher mark (Part N/O): AI mark is preserved in history ---- */}
        <div className="row wrap" style={{ gap: 10, alignItems: "center", marginTop: 12,
          borderTop: "1px solid var(--border)", paddingTop: 10 }}>
          <span className="small">
            AI mark: <strong>{q.awarded_marks}/{q.max_marks}</strong>
          </span>
          {q.teacher_mark != null && (
            <Badge tone="info">Teacher: {q.teacher_mark}/{q.max_marks}</Badge>
          )}
          <label className="row" style={{ gap: 6, marginLeft: "auto", alignItems: "center" }}>
            <span className="small">Teacher mark</span>
            <input className="input" type="number" min={0} max={q.max_marks}
              style={{ width: 76 }} value={draft}
              onChange={(e) => setDraft(e.target.value)} />
          </label>
          <Button size="sm" variant="secondary" icon="check" loading={busy}
            onClick={saveTeacherMark}>Set final mark</Button>
        </div>
      </div>
    </details>
  );
}

/* ============ correction report -> the student's Classroom submission === */

/**
 * The three states a returned correction actually has (§19).
 *
 * They are separate facts with separate evidence, so the panel never merges
 * them: Drive is transport, and a pending Drive cleanup is NOT a failed
 * correction - the grade is back and the report is attached either way.
 */
const ATTACHMENT_STEPS = [
  "Correction complete",
  "Grade returned to Classroom",
  "Report attached to this student's submission",
  "Temporary Drive file cleaned up",
] as const;

type StepState = "done" | "failed" | "blocked" | "warn" | "todo";

const STEP_MARK: Record<StepState, { glyph: string; tone: Tone }> = {
  done: { glyph: "✓", tone: "ok" },
  failed: { glyph: "✕", tone: "err" },
  blocked: { glyph: "⊘", tone: "warn" },
  // Housekeeping still outstanding - an amber tick, never a red one (§12).
  warn: { glyph: "⚠", tone: "warn" },
  todo: { glyph: "○", tone: "mute" },
};

/**
 * Build the checklist from what the backend RECORDED, never from optimism.
 *
 * `attached` is only true after the StudentSubmission was re-read and our
 * Drive file id found on it; `grade_returned` only when a grade was read back
 * out of Classroom; `drive_cleanup` only says "cleaned" once the temporary
 * file is actually gone. Anything else is shown as outstanding, with the
 * reason the backend recorded.
 */
function attachmentSteps(state: ReportAttachmentState): StepState[] {
  const blocked = state.status === "BLOCKED_NOT_MODIFIABLE";
  const skipped = state.status === "SKIPPED";
  // Correction itself is done - we are looking at a finished result.
  const correction: StepState = "done";
  const grade: StepState = skipped ? "todo"
    : state.grade_returned ? "done"
      : state.status === "PENDING" ? "todo" : "warn";
  const report: StepState = skipped ? "todo"
    : blocked ? "blocked"
      : state.attached ? "done"
        : state.status === "PENDING" ? "todo" : "failed";
  const cleanup: StepState = skipped || blocked ? "todo"
    : state.drive_cleanup === "cleaned" ? "done"
      : state.drive_cleanup === "not_needed" ? "todo"
        : state.drive_cleanup === "pending" ? "warn"
          : state.status === "PENDING" ? "todo" : "failed";
  return [correction, grade, report, cleanup];
}

const ATTACHMENT_HEADLINE: Record<AttachmentStatus, { tone: Tone; label: string }> = {
  VERIFIED: { tone: "ok", label: "Report attached and verified" },
  // The correction SUCCEEDED; only the Drive housekeeping is outstanding.
  CLEANUP_PENDING: { tone: "warn", label: "Attached — Drive cleanup pending" },
  ATTACHED: { tone: "warn", label: "Attached — not verified yet" },
  UPLOADED: { tone: "warn", label: "Uploaded — not attached yet" },
  PENDING: { tone: "info", label: "Report not attached yet" },
  DRIVE_UPLOAD_FAILED: { tone: "err", label: "Could not upload the report to Drive" },
  CLASSROOM_ATTACHMENT_FAILED: { tone: "err", label: "Classroom refused the attachment" },
  ATTACHMENT_VERIFY_FAILED: { tone: "err", label: "Attachment not confirmed on the submission" },
  REPORT_ATTACHMENT_OWNERSHIP_ERROR: {
    tone: "err", label: "This app does not own the report file",
  },
  FAILED: { tone: "err", label: "Report attachment failed" },
  BLOCKED_NOT_MODIFIABLE: {
    tone: "warn", label: "Google will not allow automatic attachment",
  },
  SKIPPED: { tone: "mute", label: "Not attached — no Classroom submission" },
};

function AttachmentPanel({ result, onChanged }: {
  result: ResultDetail; onChanged: () => void;
}) {
  const toast = useToast();
  const [state, setState] = useState<ReportAttachmentState | null>(null);
  const [busy, setBusy] = useState(false);

  const load = useCallback(() => {
    api<ReportAttachmentState>(`/results/${result.id}/classroom-attachment`)
      .then(setState)
      .catch(() => setState(null));
  }, [result.id]);

  useEffect(() => { load(); }, [load]);

  async function retry() {
    setBusy(true);
    try {
      const outcome = await api<{ status: AttachmentStatus; message: string }>(
        `/results/${result.id}/attach-report`, { method: "POST" });
      toast(outcome.status === "VERIFIED" ? "ok" : "warn", outcome.message);
      load();
      onChanged();
    } catch (e) {
      toast("err", (e as Error).message);
      // The backend already persisted the real failure - re-read so the panel
      // shows its reason instead of a stale green tick.
      load();
    } finally {
      setBusy(false);
    }
  }

  /** Retry ONLY the Drive delete - never re-renders or re-uploads (§13). */
  async function retryCleanup() {
    setBusy(true);
    try {
      const outcome = await api<ReportCleanupOutcome>(
        `/results/${result.id}/report-cleanup`, { method: "POST" });
      const done = outcome.cleanup.ok;
      toast(done ? "ok" : "warn",
        done ? "Temporary Drive file deleted."
             : `Drive cleanup still pending: ${outcome.cleanup.reason ?? "Google refused the delete"}`);
      load();
    } catch (e) {
      toast("err", (e as Error).message);
      load();
    } finally {
      setBusy(false);
    }
  }

  // Nothing to say when this correction has no Classroom submission at all.
  if (!state || state.status === "SKIPPED") return null;

  const head = ATTACHMENT_HEADLINE[state.status];
  const steps = attachmentSteps(state);
  const cleanupPending = state.drive_cleanup === "pending";
  const failed = !state.attached && state.status !== "PENDING"
    && state.status !== "BLOCKED_NOT_MODIFIABLE";

  return (
    <div className="card">
      <div className="row between wrap" style={{ gap: 10 }}>
        <h3 style={{ margin: 0 }}>
          Correction report in Google Classroom
          <span className="hint">attached to this student&rsquo;s own submission</span>
        </h3>
        <div className="row wrap" style={{ gap: 8 }}>
          <Badge tone={head.tone}>{head.label}</Badge>
          {cleanupPending && (
            <Button size="sm" variant="secondary" icon="refresh" loading={busy}
              onClick={retryCleanup}>
              Retry Drive cleanup
            </Button>
          )}
          {state.status !== "CLEANUP_PENDING" && (
            <Button size="sm" variant="secondary" icon="refresh" loading={busy}
              onClick={retry}>
              {state.retryable ? "Retry attachment" : "Attach report"}
            </Button>
          )}
        </div>
      </div>

      <ul style={{ listStyle: "none", padding: 0, margin: "12px 0 0", gap: 6 }}
        className="stack small">        {ATTACHMENT_STEPS.map((label, i) => {
          const { glyph, tone } = STEP_MARK[steps[i]];
          const color = tone === "ok" ? "var(--ok)"
            : tone === "err" ? "var(--err)"
              : tone === "warn" ? "var(--warn)" : "var(--text-faint)";
          return (
            <li key={label} className="row" style={{ gap: 8, alignItems: "baseline" }}>
              <span aria-hidden style={{ width: 14, fontWeight: 700, color }}>
                {glyph}
              </span>
              <span className={steps[i] === "done" ? "" : "faint"}>{label}</span>
            </li>
          );
        })}
        {steps[1] !== "done" && (
          <li className="row" style={{ gap: 8, alignItems: "baseline" }}>
            <span aria-hidden style={{ width: 14, fontWeight: 700, color: "var(--text-faint)" }}>◐</span>
            <span className="faint">
              Grade is still a{" "}
              {(result.publish_status === "published") ? "published" : "draft"}{" "}
              — the teacher reviews and returns it
            </span>
          </li>
        )}
      </ul>

      {cleanupPending && (
        <div className="notice warn" style={{ marginTop: 12 }}>
          <strong>Correction, grade and report all succeeded.</strong>{" "}
          Only the temporary Drive file is left behind
          {state.cleanup_attempts ? ` after ${state.cleanup_attempts} attempt(s)` : ""}
          {state.cleanup_error ? `: ${state.cleanup_error}` : "."}{" "}
          It is retried automatically; Math Correct never stores reports in
          Drive.
        </div>
      )}

      {state.error && (
        <div className={`notice ${failed ? "err" : "warn"}`} style={{ marginTop: 12 }}>
          <strong>Reason:</strong> {state.error}
        </div>
      )}

      {state.report_filename && (
        <div className="faint small mono" style={{ marginTop: 10 }}>
          File: {state.report_filename}
          {state.attempts > 1 && ` · ${state.attempts} attachment attempt(s)`}
          {state.drive_cleanup === "cleaned" && " · deleted from Drive after attaching"}
        </div>
      )}

      {state.fallback && (
        <div className="notice info" style={{ marginTop: 12 }}>
          <strong>Attach it manually:</strong> {state.fallback.instructions}
          <div className="row wrap" style={{ gap: 8, marginTop: 8 }}>
            <a className="btn secondary sm" href={`/api${state.fallback.pdf_url}`}
              download={state.fallback.filename}>
              <Icon name="download" size={14} />
              Download {state.fallback.filename}
            </a>
            {state.fallback.classroom_url && (
              <a className="btn secondary sm" href={state.fallback.classroom_url}
                target="_blank" rel="noreferrer noopener">
                <Icon name="external" size={14} />
                Open in Classroom
              </a>
            )}
          </div>
        </div>
      )}
    </div>
  );
}

/* ============================================ remark / re-mark (§Remark) === */

/** "Attempt 2" style label; "—" for a side that has no attempt yet. */
function attemptLabel(side: RemarkSide): string {
  return side.version != null ? `Attempt ${side.version}` : "—";
}

function SideCell({ side }: { side: RemarkSide }) {
  return (
    <div>
      <div className="mono" style={{ fontWeight: 700, fontSize: "var(--fs-lg)" }}>
        {side.earned ?? "—"}/{side.available ?? "—"}
      </div>
      <div className="faint small">
        {attemptLabel(side)} · {side.percentage.toFixed(1)}%
        {side.marking_model ? ` · ${side.marking_model}` : ""}
      </div>
      {!side.reliable && (
        <Badge tone="warn">Needs review — {side.confidence ?? "unverified"}</Badge>
      )}
    </div>
  );
}

function RemarkPanel({ result, onChanged }: {
  result: ResultDetail; onChanged: () => void;
}) {
  const toast = useToast();
  const [status, setStatus] = useState<RemarkStatus | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [confirming, setConfirming] = useState(false);

  const load = useCallback(() => {
    api<RemarkStatus>(`/results/${result.id}/remark`).then(setStatus).catch(() => {});
  }, [result.id]);
  useEffect(() => { load(); }, [load]);

  async function startRemark() {
    setBusy("remark");
    try {
      const out = await api<{ job_id: string; duplicate: boolean }>(
        `/results/${result.id}/remark`, { method: "POST" });
      setConfirming(false);
      toast(out.duplicate ? "warn" : "ok",
        out.duplicate
          ? "A re-mark is already running for this submission — no second run started."
          : "Re-mark started. The current correction stays exactly as it is until you choose.");
      // The attempt lands as pending when the marking finishes.
      setTimeout(load, 400);
    } catch (e) { toast("err", (e as Error).message); } finally { setBusy(null); }
  }

  async function decide(decision: "USE_NEW" | "KEEP_PREVIOUS") {
    setBusy("decision");
    try {
      const out = await api<{ active_version: number; forced_review: boolean }>(
        `/results/${result.id}/remark/decision`, {
          method: "POST", body: JSON.stringify({ decision }),
        });
      toast(out.forced_review ? "warn" : "ok",
        out.forced_review
          ? `Attempt ${out.active_version} is now the correction and is flagged for review.`
          : decision === "USE_NEW"
            ? `Attempt ${out.active_version} is now the active correction.`
            : "Kept the previous correction. The re-mark stays in the history.");
      load();
      onChanged();
    } catch (e) { toast("err", (e as Error).message); } finally { setBusy(null); }
  }

  const cmp = status?.comparison;

  return (
    <div className="card">
      <div className="row between wrap" style={{ gap: 10 }}>
        <h3 style={{ margin: 0 }}>
          Remark
          <span className="hint">
            re-runs the AI marking — the current correction is never lost
          </span>
        </h3>
        <Button size="sm" variant="secondary" icon="refresh"
          loading={busy === "remark"} onClick={() => setConfirming(true)}>
          Re-mark this submission
        </Button>
      </div>

      {status && status.attempt_count > 1 && (
        <div className="faint small" style={{ marginTop: 8 }}>
          {status.attempt_count} marking attempt{status.attempt_count === 1 ? "" : "s"} recorded
        </div>
      )}

      {cmp && (
        <>
          <div className="notice warn" style={{ marginTop: 10 }}>
            <strong>Re-mark waiting for your decision.</strong>{" "}
            The correction below is still the original — nothing has been applied yet.
          </div>
          <div className="table-wrap" style={{ marginTop: 10 }}>
            <table className="table">
              <thead>
                <tr><th /><th>Previous</th><th>New</th></tr>
              </thead>
              <tbody>
                <tr>
                  <td data-label=""><strong>Total</strong></td>
                  <td data-label="Previous" className="mono">
                    {cmp.previous.earned}/{cmp.previous.available}
                  </td>
                  <td data-label="New" className="mono">
                    {cmp.new.earned}/{cmp.new.available}{" "}
                    <span className={(cmp.delta_points ?? 0) >= 0 ? "" : "faint"}>
                      ({(cmp.delta_points ?? 0) >= 0 ? "+" : ""}{cmp.delta_points})
                    </span>
                  </td>
                </tr>
                <tr>
                  <td data-label=""><strong>Percentage</strong></td>
                  <td data-label="Previous">{cmp.previous.percentage.toFixed(1)}%</td>
                  <td data-label="New">{cmp.new.percentage.toFixed(1)}%</td>
                </tr>
              </tbody>
            </table>
          </div>

          <div className="grid grid-2" style={{ marginTop: 12 }}>
            <SideCell side={cmp.previous} />
            <SideCell side={cmp.new} />
          </div>

          {cmp.questions.length > 0 && (
            <details className="expander" style={{ marginTop: 10 }} open>
              <summary>{cmp.question_changes} question mark(s) changed</summary>
              <div className="expander-body">
                <ul style={{ listStyle: "none", padding: 0, margin: 0, gap: 4 }}
                  className="stack small">
                  {cmp.questions.map((q) => (
                    <li key={q.question_number}>
                      <strong>Q{q.question_number}</strong>:{" "}
                      <span className="faint mono">{q.previous ?? "—"}</span>
                      {" → "}
                      <span className="mono" style={{ fontWeight: 700 }}>
                        {q.new ?? "—"}
                      </span>
                      {q.max != null && <span className="faint">/{q.max}</span>}
                      {q.kind === "added" && <Badge tone="info">new question</Badge>}
                      {q.kind === "removed" && <Badge tone="warn">dropped</Badge>}
                    </li>
                  ))}
                </ul>
              </div>
            </details>
          )}

          {!cmp.new.reliable && (
            <div className="notice err small" style={{ marginTop: 10 }}>
              This re-mark did not pass validation. Using it is allowed, but it
              will be flagged for review rather than trusted silently.
            </div>
          )}

          <div className="row wrap" style={{ gap: 8, marginTop: 12 }}>
            <Button variant="primary" icon="check" loading={busy === "decision"}
              onClick={() => decide("USE_NEW")}>Use new result</Button>
            <Button variant="secondary" icon="arrow-left" loading={busy === "decision"}
              onClick={() => decide("KEEP_PREVIOUS")}>Keep previous</Button>
          </div>
        </>
      )}

      {confirming && (
        <Modal title="Re-mark submission?" icon="refresh"
          onClose={() => setConfirming(false)}
          footer={
            <>
              <Button variant="ghost" onClick={() => setConfirming(false)}>Cancel</Button>
              <Button variant="primary" icon="refresh" loading={busy === "remark"}
                onClick={startRemark}>Re-mark</Button>
            </>
          }>
          <div className="stack small" style={{ gap: 6 }}>
            <div><span className="faint">Student:</span>{" "}
              <strong>{displayName(result)}</strong></div>
            <div><span className="faint">Assignment:</span>{" "}
              <strong>{result.assignment_title ?? "—"}</strong></div>
            <div><span className="faint">Current mark:</span>{" "}
              <strong className="mono">{result.earned}/{result.available}</strong></div>
          </div>
          <div className="notice info small" style={{ marginTop: 12 }}>
            Re-marking runs the AI marking process again, which uses a Gemini key.
            The previous correction, its question marks, its report and its
            Classroom attachment are all preserved — the new attempt is recorded
            alongside and only becomes active if you choose it.
          </div>
        </Modal>
      )}
    </div>
  );
}

/** Independent retries (§10) so a downstream failure never re-runs marking. */
function RetryActions({ result }: { result: ResultDetail }) {
  const toast = useToast();
  const [busy, setBusy] = useState<RetryAction | null>(null);

  async function run(action: RetryAction) {
    setBusy(action);
    try {
      await api(`/results/${result.id}/retry`, {
        method: "POST", body: JSON.stringify({ action }),
      });
      toast("ok", {
        report: "Report regenerated from the current marks.",
        upload: "Report uploaded to Drive.",
        attach: "Report attached to the student's submission.",
        grade: "Classroom draft grade re-synced.",
      }[action]);
    } catch (e) { toast("err", (e as Error).message); } finally { setBusy(null); }
  }

  return (
    <details className="expander">
      <summary>Retry a single step — without re-marking</summary>
      <div className="expander-body">
        <p className="muted small" style={{ marginTop: 0 }}>
          Each of these retries only its own step. If a Google call failed, fix
          it here instead of paying for another Gemini marking run.
        </p>
        <div className="row wrap" style={{ gap: 8 }}>
          <Button size="sm" variant="secondary" icon="file" loading={busy === "report"}
            onClick={() => run("report")}>Regenerate report</Button>
          <Button size="sm" variant="secondary" icon="upload" loading={busy === "upload"}
            onClick={() => run("upload")}>Retry report upload</Button>
          <Button size="sm" variant="secondary" icon="paperclip" loading={busy === "attach"}
            onClick={() => run("attach")}>Retry Classroom attachment</Button>
          <Button size="sm" variant="secondary" icon="send" loading={busy === "grade"}
            onClick={() => run("grade")}>Retry grade sync</Button>
        </div>
      </div>
    </details>
  );
}

/* ============================================== marking workspace (§11) */

interface ResultPages {
  available: boolean; reason: string | null; message: string | null;
  page_count: number; file_kind: string | null; file_name: string | null;
  source: string | null; dpi: number; page_url_template: string;
}

/**
 * The student's ACTUAL work (centre pane). The backend serves the stored
 * submission page by page (``/api/results/{id}/pages``); when the original is
 * not stored (retention deletes it at job end) this says so honestly instead
 * of pretending. Zoom: buttons or +/- keys. Pages load lazily.
 */
function DocumentPane({ resultId }: { resultId: string }) {
  const [info, setInfo] = useState<ResultPages | null>(null);
  const [zoom, setZoom] = useState(100);
  const [failed, setFailed] = useState<Record<number, boolean>>({});
  const pagesRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    setInfo(null); setZoom(100); setFailed({});
    api<ResultPages>(`/results/${resultId}/pages`)
      .then(setInfo).catch(() => setInfo(null));
  }, [resultId]);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const t = e.target as HTMLElement | null;
      if (t && ["INPUT", "TEXTAREA", "SELECT"].includes(t.tagName)) return;
      if (e.key === "+" || e.key === "=") setZoom((z) => Math.min(300, z + 25));
      if (e.key === "-") setZoom((z) => Math.max(50, z - 25));
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  if (!info) {
    return (
      <div className="skeleton-lines" aria-busy="true">
        <div className="skeleton" style={{ height: 240 }} />
      </div>
    );
  }
  if (!info.available) {
    return (
      <Empty icon="file" art="paper" title="The original work is not stored on this device"
        hint={info.message ?? "It is deleted when a job finishes unless Settings → Privacy & Data is set to keep submissions."} />
    );
  }

  const urlFor = (p: number) => info.page_url_template.replace("{page}", String(p));

  // "Fit width" must also undo any sideways scroll left over from a zoomed
  // view; shrinking the image alone left the pages scrolled off-screen, which
  // read as "the button does not fix it".
  function fitWidth() {
    setZoom(100);
    pagesRef.current?.scrollTo({ left: 0 });
  }

  return (
    <div className="stack">
      {/* Header stays put; only the pages scroll (see .doc-pages). */}
      <div className="doc-head">
        <div className="doc-meta small muted" title={info.file_name ?? undefined}>
          {info.file_name} · {info.page_count} page(s)
          {info.source === "retained" ? " · retained copy" : ""}
        </div>
        <div className="row doc-zoom" style={{ gap: 6 }}>
          <Button size="sm" variant="ghost" aria-label="Zoom out"
            onClick={() => setZoom((z) => Math.max(50, z - 25))}>−</Button>
          <span className="small mono" aria-live="polite">{zoom}%</span>
          <Button size="sm" variant="ghost" aria-label="Zoom in"
            onClick={() => setZoom((z) => Math.min(300, z + 25))}>+</Button>
          <Button size="sm" variant="ghost" onClick={fitWidth}>Fit width</Button>
        </div>
      </div>
      <div className="doc-pages" ref={pagesRef}>
        {Array.from({ length: info.page_count }, (_, i) => i + 1).map((p) => (
          failed[p] ? (
            <div className="notice err small" key={p}>Page {p} could not be shown.</div>
          ) : (
            <figure key={p} style={{ margin: 0 }}>
              <img src={urlFor(p)}
                alt={`Page ${p} of ${info.file_name ?? "the student's submission"}`}
                loading="lazy"
                onError={() => setFailed((f) => ({ ...f, [p]: true }))}
                style={{ width: `${zoom}%`, height: "auto", display: "block",
                  border: "1px solid var(--border)", borderRadius: "var(--radius-xs)" }} />
              <figcaption className="faint small" style={{ marginTop: 4 }}>Page {p}</figcaption>
            </figure>
          )
        ))}
      </div>
    </div>
  );
}

/** Left pane: who/what is being marked + the question tree with real marks. */
function QuestionNavigator({ result, activeQ, onSelect }: {
  result: ResultDetail; activeQ: string | null; onSelect: (q: string) => void;
}) {
  const qs = result.result_json.questions;
  const groups = buildTopicGroups(qs);
  const provenance: "ai" | "verified" | "teacher" | "overridden" =
    result.manual_override ? "overridden"
      : result.verification_json?.verified ? "verified" : "ai";
  return (
    <div className="stack" style={{ gap: 2 }}>
      <div>
        <div style={{ fontWeight: 700 }}>{displayName(result)}</div>
        <div className="faint small">
          {result.assignment_title ?? result.result_json.paper_info.paper}
        </div>
      </div>
      <div className="provenance-row" style={{ margin: "6px 0 10px" }}>
        <ProvenanceBadge kind={provenance} />
        {result.review_required ? <Badge tone="review" icon="review">Needs review</Badge> : null}
      </div>
      <div className="faint small" style={{ textTransform: "uppercase", letterSpacing: "0.06em", fontWeight: 700 }}>
        Questions <span style={{ textTransform: "none", letterSpacing: 0 }}>by topic</span>
      </div>
      {groups.map((g) => (
        <div key={g.topic}>
          <div className="qtree-topic"
            title={`${g.topic}: ${g.earned}/${g.available} marks (${g.percentage.toFixed(1)}%)`}>
            <span className="t">{g.topic}</span>
            <span className="m">{g.earned}/{g.available}</span>
          </div>
          {g.questions.map((q) => (
            <div key={q.question_number}>
              <button type="button" className={`qtree-item ${activeQ === q.question_number ? "active" : ""}`}
                onClick={() => onSelect(q.question_number)}>
                <span style={{ minWidth: 32, fontWeight: 700 }}>Q{q.question_number}</span>
                <span className="mono" style={{ fontWeight: 700 }}>
                  {q.awarded_marks}/{q.max_marks}
                </span>
                {q.status === "REVIEW" && <Badge tone="warn">unmarked</Badge>}
                {q.scheme_check && <Badge tone="info">scheme</Badge>}
              </button>
              {(q.subquestions ?? []).map((s) => (
                <div key={s.id} className="qtree-sub">
                  <span style={{ minWidth: 24 }}>{s.label}</span>
                  <span className="mono">{s.awarded_marks}/{s.max_marks}</span>
                  {s.status !== "MARKED" && <Badge tone="warn">unmarked</Badge>}
                </div>
              ))}
            </div>
          ))}
        </div>
      ))}
    </div>
  );
}

function ResultView({ result, onReload, onPrev, onNext }: {
  result: ResultDetail; onReload: () => void;
  onPrev?: (() => void) | null; onNext?: (() => void) | null;
}) {
  const verification = result.verification_json;
  const qs = result.result_json.questions;
  const fullCount = qs.filter((q) => q.awarded_marks === q.max_marks).length;
  const zeroCount = qs.filter((q) => q.awarded_marks === 0).length;
  const partialCount = qs.length - fullCount - zeroCount;
  const marksLost = qs.reduce((sum, q) => sum + (q.max_marks - q.awarded_marks), 0);
  const [activeQ, setActiveQ] = useState<string | null>(null);

  // Topic groups + confidence triage (§ question groups): pure math from
  // ../topicGroups, the exact arithmetic its Node tests cover.
  const groups = useMemo(() => buildTopicGroups(qs), [qs]);
  const checkFirst = useMemo(
    () => qs.filter((q) => questionTriage(q) !== "ok")
      .sort((a, b) => TRIAGE_RANK[questionTriage(a)] - TRIAGE_RANK[questionTriage(b)]),
    [qs]);
  const confidenceReasons = parseReasons(result.confidence_reasons);

  // Sortable question breakdown (useSort). Question navigation (n/p, tree)
  // deliberately keeps paper order — only this table sorts.
  const breakdownCols = useMemo<SortColumns<Question>>(() => ({
    q: { value: (x) => x.question_number },
    marks: { value: (x) => (x.max_marks > 0 ? x.awarded_marks / x.max_marks : 0), numeric: true },
    topic: { value: (x) => x.topic },
    lost: { value: (x) => x.loss_reason },
  }), []);
  const { sorted: sortedQs, onSort: onSortCol, ariaSortFor } = useSort(qs, breakdownCols);
  const sortArrow = (key: string) =>
    ariaSortFor(key) === "ascending" ? "▲" : ariaSortFor(key) === "descending" ? "▼" : "";

  function select(num: string) {
    setActiveQ(num);
    const el = document.getElementById(`question-${num}`);
    el?.scrollIntoView({
      behavior: prefersReducedMotion() ? "auto" : "smooth", block: "start",
    });
    if (el instanceof HTMLDetailsElement) el.open = true;
  }

  // n/p step through the question tree (keyboard shortcuts §11).
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const t = e.target as HTMLElement | null;
      if (t && ["INPUT", "TEXTAREA", "SELECT"].includes(t.tagName)) return;
      const keys = qs.map((q) => q.question_number);
      const idx = activeQ ? keys.indexOf(activeQ) : -1;
      if (e.key === "n" && idx < keys.length - 1) select(keys[idx + 1]);
      if (e.key === "p" && idx > 0) select(keys[idx - 1]);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  });

  const resultPane = (
    <div className="stack">
      {/* ---- header ---- */}
      <div className="card">
        <div className="row between wrap">
          <div>
            <h2 style={{ margin: "0 0 2px", fontSize: "var(--fs-xl)" }}>
              {displayName(result)}
            </h2>
            {/* The official Classroom identity stays available here for
                reference; the workflow above always leads with the display name. */}
            {result.nickname && result.classroom_name && (
              <div className="muted small">Google Classroom: {result.classroom_name}</div>
            )}
            <span className="muted small">
              {result.result_json.paper_info.paper} · {result.result_json.paper_info.session} ·
              {" "}marked by {result.marking_model}
              {result.key_fingerprint && ` · key ${result.key_fingerprint}`}
              {result.processing_seconds != null && ` · ${result.processing_seconds}s`}
            </span>
            <div className="row wrap" style={{ gap: 6, marginTop: 6 }}>
              <Badge tone={verification?.status === "REVIEW" ? "warn" : "ok"}>
                {verification?.status === "REVIEW"
                  ? `⚠ REVIEW REQUIRED${(verification.review_questions?.length ?? 0) > 0
                    ? ` (${verification.review_questions?.length} question(s))` : ""}`
                  : (verification?.status ?? "VALID")}
              </Badge>
              {verification?.blueprint?.source && (
                <Badge tone="info">
                  mark scheme: {verification.blueprint.source === "cache"
                    ? "cached blueprint" : verification.blueprint.source === "built"
                      ? "blueprint extracted" : "free-form (no blueprint)"}
                </Badge>
              )}
              {result.confidence && (
                <Badge tone={CONFIDENCE_META[result.confidence]?.tone ?? "mute"} icon="shield">
                  {CONFIDENCE_META[result.confidence]?.label
                    ?? `Confidence: ${result.confidence.toLowerCase().replace(/_/g, " ")}`}
                </Badge>
              )}
            </div>
          </div>
          <div className="row wrap">
            <div style={{ textAlign: "right" }}>
              <div style={{ fontSize: "var(--fs-2xl)", fontWeight: 700, fontVariantNumeric: "tabular-nums", lineHeight: 1.1 }}>
                {result.earned}<span className="muted">/{result.available}</span>
              </div>
              <div className="muted small">
                {result.percentage.toFixed(2)}% · provisional band {result.provisional_grade}
              </div>
            </div>
            <div className="row" style={{ gap: 6 }}>
              <Button variant="ghost" size="sm" icon="arrow-left" disabled={!onPrev}
                tooltip="Previous submission (k)" onClick={() => onPrev?.()}>
                Prev
              </Button>
              <Button variant="ghost" size="sm" iconRight="arrow-right" disabled={!onNext}
                tooltip="Next submission (j)" onClick={() => onNext?.()}>
                Next
              </Button>
            </div>
            <ExportMenu base={`/results/${result.id}/export`}
              formats={["pdf", "md", "csv"]} params={{ detail: "detailed" }} />
          </div>
        </div>

        {verification && !verification.verified && (
          <div className="notice warn" style={{ marginTop: 12 }}>
            <strong>Verification flags:</strong>
            <ul style={{ margin: "6px 0 0", paddingLeft: 18 }}>
              {verification.anomalies.map((a, i) => <li key={i}>{a}</li>)}
            </ul>
          </div>
        )}
        {confidenceReasons.length > 0 && (
          <details className="expander" style={{ marginTop: 10 }}>
            <summary>
              {confidenceReasons.length} recorded reason(s) for this confidence level
            </summary>
            <div className="expander-body">
              <ul style={{ margin: "6px 0 0", paddingLeft: 18 }}>
                {confidenceReasons.map((r, i) => <li key={i} className="muted">{r}</li>)}
              </ul>
              <p className="faint small" style={{ marginBottom: 0 }}>
                Recorded by the verification guard when this correction was stored.
                Confidence flags where to look — it never changes a mark.
              </p>
            </div>
          </details>
        )}
        {verification && verification.warnings.length > 0 && (
          <details className="expander" style={{ marginTop: 10 }}>
            <summary>{verification.warnings.length} warning(s) from verification</summary>
            <div className="expander-body">
              <ul style={{ margin: "6px 0 0", paddingLeft: 18 }}>
                {verification.warnings.map((w, i) => <li key={i} className="muted">{w}</li>)}
              </ul>
            </div>
          </details>
        )}
      </div>

      <DoubleCheckPanel result={result} />

      <AttachmentPanel result={result} onChanged={onReload} />

      <RemarkPanel result={result} onChanged={onReload} />

      {/* ---- summary ---- */}
      <div className="grid grid-4">
        <Stat compact label="Correct" value={fullCount} tone="ok" />
        <Stat compact label="Partially correct" value={partialCount} tone="warn" />
        <Stat compact label="Incorrect" value={zeroCount} tone="err" />
        <Stat compact label="Marks lost" value={marksLost} />
      </div>

      {/* ---- question groups + confidence triage (§ question groups) ---- */}
      <div className="card">
        <h3>Question groups <span className="hint">by topic — a reading of the recorded marks</span></h3>
        {checkFirst.length > 0 && (
          <div className="triage-strip small" style={{ marginBottom: 10 }} data-testid="triage-strip">
            <div><strong>Start here:</strong> {checkFirst.length} question(s) carry recorded flags.</div>
            <div className="triage-chips">
              {checkFirst.map((q) => {
                const t = questionTriage(q);
                return (
                  <button key={q.question_number} type="button"
                    className={`triage-pill ${t}`}
                    style={{ cursor: "pointer", font: "inherit" }}
                    onClick={() => select(q.question_number)}>
                    Q{q.question_number} — {TRIAGE_LABEL[t]}
                  </button>
                );
              })}
            </div>
            <div className="faint" style={{ marginTop: 6 }}>
              Flags come from recorded evidence (unmarked parts, mark-scheme
              disagreement, unreadable work) — not from a model confidence score.
            </div>
          </div>
        )}
        {groups.map((g) => (
          <div key={g.topic} className="qgroup">
            <div className="qgroup-head">
              <span className="title">{g.topic}</span>
              <Badge tone={masteryTone(g.mastery)}>{MASTERY_LABEL[g.mastery]}</Badge>
              {g.checkCount > 0 && <Badge tone="warn" icon="alert">{g.checkCount} to check</Badge>}
              {g.watchCount > 0 && <Badge tone="info">{g.watchCount} to look at</Badge>}
              <div className="mastery-bar" role="img"
                aria-label={`${g.topic}: ${g.percentage.toFixed(0)}% of marks awarded`}>
                <div className={`fill ${g.mastery}`}
                  style={{ width: `${Math.min(100, Math.max(0, g.percentage))}%` }} />
              </div>
              <span className="marks mono">
                {g.earned}/{g.available} · {g.percentage.toFixed(1)}%
              </span>
            </div>
            <div className="qgroup-body">
              {g.questions.map((q) => (
                <button key={q.question_number} type="button"
                  className="qtree-item" style={{ width: "auto" }}
                  onClick={() => select(q.question_number)}>
                  <span style={{ fontWeight: 700 }}>Q{q.question_number}</span>
                  <span className="mono">{q.awarded_marks}/{q.max_marks}</span>
                  {questionTriage(q) !== "ok" && <TriagePill q={q} />}
                </button>
              ))}
            </div>
          </div>
        ))}
        <p className="faint small" style={{ margin: "10px 0 0" }}>
          Topic grouping and the bands are derived from the awarded marks —
          they never change a stored score.
        </p>
      </div>

      {/* ---- overview notes ---- */}
      <div className="card">
        <h3>Overview</h3>
        {result.result_json.overall_notes.summary && (
          <p className="muted small" style={{ marginTop: 0 }}>{result.result_json.overall_notes.summary}</p>
        )}
        <div className="grid grid-2">
          <div>
            <strong className="small">✅ Strengths</strong>
            <ul className="small muted" style={{ margin: "4px 0 0", paddingLeft: 18 }}>
              {result.result_json.overall_notes.strengths.map((s, i) => <li key={i}>{s}</li>)}
            </ul>
          </div>
          <div>
            <strong className="small">📚 Revision areas</strong>
            <ul className="small muted" style={{ margin: "4px 0 0", paddingLeft: 18 }}>
              {result.result_json.overall_notes.revision_areas.map((s, i) => <li key={i}>{s}</li>)}
            </ul>
          </div>
        </div>
      </div>

      {/* ---- question breakdown (sortable via useSort) ---- */}
      <div className="card">
        <h3>Question breakdown <span className="hint">click a column heading to sort</span></h3>
        <div className="table-wrap">
          <table className="table table-stack">
            <thead>
              <tr>
                <th aria-sort={ariaSortFor("q")}>
                  <button className="th-sort" type="button" onClick={() => onSortCol("q")}>
                    Q <span className="arrow">{sortArrow("q")}</span>
                  </button>
                </th>
                <th className="num" aria-sort={ariaSortFor("marks")}>
                  <button className="th-sort" type="button" onClick={() => onSortCol("marks")}>
                    Marks <span className="arrow">{sortArrow("marks")}</span>
                  </button>
                </th>
                <th>Status</th>
                <th aria-sort={ariaSortFor("topic")}>
                  <button className="th-sort" type="button" onClick={() => onSortCol("topic")}>
                    Topic <span className="arrow">{sortArrow("topic")}</span>
                  </button>
                </th>
                <th aria-sort={ariaSortFor("lost")}>
                  <button className="th-sort" type="button" onClick={() => onSortCol("lost")}>
                    Why marks were lost <span className="arrow">{sortArrow("lost")}</span>
                  </button>
                </th>
              </tr>
            </thead>
            <tbody>
              {sortedQs.map((q) => (
                <tr key={q.question_number}>
                  <td data-label="Question" className="num">{q.question_number}</td>
                  <td data-label="Marks" className="num">
                    <span className="mono" style={{ fontWeight: 700 }}>
                      {q.awarded_marks}/{q.max_marks}
                    </span>
                  </td>
                  <td data-label="Status"><QuestionTone awarded={q.awarded_marks} max={q.max_marks} /></td>
                  <td data-label="Topic" className="muted">{q.topic}</td>
                  <td data-label="Lost" className="muted small wrap-cell">{q.loss_reason || "—"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>

      {/* ---- detailed correction (expandable per question) ---- */}
      <div className="card">
        <h3>Detailed correction <span className="hint">expand a question for method, working and mark code</span></h3>
        {qs.map((q) => <QuestionCard key={q.question_number} q={q}
          resultId={result.id} onChanged={onReload} />)}
      </div>

      <VersionsPanel resultId={result.id} onRestored={onReload} />

      <div className="card">
        <h3>Recovery</h3>
        <RetryActions result={result} />
      </div>

      {result.result_json.uncertainties.length > 0 && (
        <div className="card">
          <h3>⚠️ Marking uncertainties</h3>
          <ul className="small muted" style={{ margin: 0, paddingLeft: 18 }}>
            {result.result_json.uncertainties.map((u, i) => <li key={i}>{u}</li>)}
          </ul>
        </div>
      )}
    </div>
  );

  // Three panes (§11): the question tree, the student's ACTUAL work, and the
  // marking result. Resizable; below 900px they become tabs.
  return (
    <WorkspacePanes storageKey="mc5.workspace.results" min={230} panes={[
      { id: "nav", label: "Questions",
        el: <QuestionNavigator result={result} activeQ={activeQ} onSelect={select} /> },
      { id: "doc", label: "Student work", el: <DocumentPane resultId={result.id} /> },
      { id: "mark", label: "Marking result", el: resultPane },
    ]} />
  );
}

/* ============================================ list / search / filters */

const RESULT_STATUS: Record<ResultStatus, { tone: Tone; label: string; icon: IconName }> = {
  corrected: { tone: "ok", label: "Corrected", icon: "check-circle" },
  needs_review: { tone: "warn", label: "Needs review", icon: "alert" },
  overridden: { tone: "info", label: "Overridden", icon: "edit" },
};

const PUBLISH_STATUS: Record<string, { tone: Tone; label: string }> = {
  published: { tone: "ok", label: "Published" },
  pending: { tone: "info", label: "Pending" },
  requires_review: { tone: "warn", label: "Blocked (review)" },
  failed: { tone: "err", label: "Failed" },
  not_supported: { tone: "mute", label: "Not supported" },
  skipped: { tone: "mute", label: "Skipped" },
};

const PAGE = 50;

export default function ResultsPage({ resultId, onNavigate }: {
  resultId?: string | null; onNavigate?: (hash: string) => void;
} = {}) {
  const [rows, setRows] = useState<ResultSummary[]>([]);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [limit, setLimit] = useState(PAGE);

  const [q, setQ] = useState("");
  const [debouncedQ, setDebouncedQ] = useState("");
  const [status, setStatus] = useState<"all" | ResultStatus>("all");
  const [courseId, setCourseId] = useState("");
  const [courseworkId, setCourseworkId] = useState("");

  const [classes, setClasses] = useState<Course[]>([]);
  const [assignments, setAssignments] = useState<Coursework[]>([]);

  const [selected, setSelected] = useState<ResultDetail | null>(null);
  const [detailLoading, setDetailLoading] = useState(false);

  // Debounce the search box so typing does not fire a request per keystroke.
  useEffect(() => {
    const t = window.setTimeout(() => setDebouncedQ(q.trim()), 250);
    return () => window.clearTimeout(t);
  }, [q]);

  // Filter option sources (classes + assignments), loaded once.
  useEffect(() => {
    api<{ classes: Course[] }>("/classes")
      .then((r) => setClasses(r.classes ?? [])).catch(() => {});
    api<{ assignments: Coursework[] }>("/assignments")
      .then((r) => setAssignments(r.assignments ?? [])).catch(() => {});
  }, []);

  const load = useCallback(() => {
    const params = new URLSearchParams();
    if (debouncedQ) params.set("q", debouncedQ);
    if (status !== "all") params.set("status", status);
    if (courseId) params.set("course_id", courseId);
    if (courseworkId) params.set("coursework_id", courseworkId);
    params.set("limit", String(limit));
    setLoading(true); setError(null);
    api<{ results: ResultSummary[]; total: number }>(`/results?${params.toString()}`)
      .then((r) => { setRows(r.results ?? []); setTotal(r.total ?? 0); })
      .catch((e) => setError((e as Error).message))
      .finally(() => setLoading(false));
  }, [debouncedQ, status, courseId, courseworkId, limit]);

  useEffect(() => { load(); }, [load]);

  const open = useCallback((id: string) => {
    setDetailLoading(true);
    api<ResultDetail>(`/results/${id}`)
      .then(setSelected).catch(() => setSelected(null))
      .finally(() => setDetailLoading(false));
  }, []);

  useEffect(() => { if (resultId) open(resultId); }, [resultId, open]);

  const assignmentsForCourse = useMemo(
    () => (courseId ? assignments.filter((a) => a.course_id === courseId) : assignments),
    [assignments, courseId]);

  function choose(id: string) {
    if (onNavigate) onNavigate(`#/results/${id}`);
    else open(id);
  }

  const filtersActive = Boolean(debouncedQ || status !== "all" || courseId || courseworkId);

  // Submission navigation in the workspace (§11): j/k and the header buttons
  // move through the CURRENT result list, in the order the table shows.
  const goRelative = (delta: number) => {
    const i = rows.findIndex((r) => r.id === resultId);
    const target = rows[i + delta];
    if (target) choose(target.id);
  };

  useEffect(() => {
    if (!resultId) return;
    const onKey = (e: KeyboardEvent) => {
      const t = e.target as HTMLElement | null;
      if (t && ["INPUT", "TEXTAREA", "SELECT"].includes(t.tagName)) return;
      if (e.key === "j") goRelative(1);
      if (e.key === "k") goRelative(-1);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  });

  // ---- correction detail (a deep link keeps every result reachable) ----
  if (resultId) {
    const i = rows.findIndex((r) => r.id === resultId);
    return (
      <>
        <PageHeader title="Correction detail"
          sub="The marking workspace — the student's work, the question tree and the result side by side.">
          <Button variant="secondary" size="sm" icon="arrow-left"
            onClick={() => onNavigate?.("#/results")}>All results</Button>
        </PageHeader>
        {detailLoading && !selected ? <Loading label="Loading correction…" />
          : selected ? <ResultView result={selected} onReload={() => open(selected.id)}
              onPrev={i > 0 ? () => goRelative(-1) : null}
              onNext={i >= 0 && i < rows.length - 1 ? () => goRelative(1) : null} />
            : <ErrorState title="Correction not found"
                detail="This result could not be loaded. It may have been removed."
                onRetry={() => open(resultId)} />}
      </>
    );
  }

  return (
    <>
      <PageHeader title="Results"
        sub="Every completed correction, searchable and filterable." />

      <div className="card" style={{ marginBottom: 14 }}>
        <div className="row wrap" style={{ gap: 10, alignItems: "center" }}>
          <label className="row" style={{ gap: 8, alignItems: "center", flex: "1 1 260px", minWidth: 220 }}>
            <Icon name="search" size={16} style={{ color: "var(--text-faint)" }} />
            <input className="input" type="search" value={q}
              placeholder="Search corrections by student, assignment or ID…"
              aria-label="Search corrections"
              onChange={(e) => setQ(e.target.value)} style={{ flex: 1 }} />
          </label>
          <Segmented value={status} ariaLabel="Filter by status"
            onChange={(v) => setStatus(v)}
            options={[
              { value: "all", label: "All" },
              { value: "corrected", label: "Corrected" },
              { value: "needs_review", label: "Needs review" },
              { value: "overridden", label: "Overridden" },
            ]} />
          {classes.length > 0 && (
            <Dropdown label={courseId ? (classes.find((c) => c.id === courseId)?.name ?? "Class") : "All classes"}
              icon="class" align="right">
              {(close) => (
                <>
                  <button role="menuitem" onClick={() => { setCourseId(""); setCourseworkId(""); close(); }}>All classes</button>
                  {classes.map((c) => (
                    <button key={c.id} role="menuitem"
                      onClick={() => { setCourseId(c.id); setCourseworkId(""); close(); }}>{c.name}</button>
                  ))}
                </>
              )}
            </Dropdown>
          )}
          {assignmentsForCourse.length > 0 && (
            <Dropdown
              label={courseworkId ? (assignmentsForCourse.find((a) => a.id === courseworkId)?.title ?? "Assignment") : "All assignments"}
              icon="assignments" align="right">
              {(close) => (
                <>
                  <button role="menuitem" onClick={() => { setCourseworkId(""); close(); }}>All assignments</button>
                  {assignmentsForCourse.map((a) => (
                    <button key={a.id} role="menuitem"
                      onClick={() => { setCourseworkId(a.id); close(); }}>{a.title}</button>
                  ))}
                </>
              )}
            </Dropdown>
          )}
          {filtersActive && (
            <Button size="sm" variant="ghost" icon="x"
              onClick={() => { setQ(""); setStatus("all"); setCourseId(""); setCourseworkId(""); }}>
              Clear
            </Button>
          )}
        </div>
        <div className="faint small" style={{ marginTop: 8 }}>
          Showing <strong>{rows.length}</strong> of <strong>{total}</strong> correction(s)
          {filtersActive ? " (filtered)" : ""}
        </div>
      </div>

      {error ? (
        <ErrorState title="Could not load corrections" detail={error} onRetry={load} />
      ) : loading ? (
        <Loading label="Loading corrections…" />
      ) : rows.length === 0 ? (
        <div className="card">
          <Empty icon="search" art={filtersActive ? "search" : "paper"}
            title={filtersActive ? "No corrections match your filters" : "No corrections yet"}
            hint={filtersActive
              ? "Try a different search term or clear the filters."
              : "Corrections appear here as soon as marking finishes."} />
        </div>
      ) : (
        <>
          <div className="table-wrap">
            <table className="table table-stack">
              <thead>
                <tr>
                  <th>Student</th><th>Assignment</th><th>Class</th>
                  <th className="num">Score</th><th className="num">%</th>
                  <th>Status</th><th>Classroom</th><th>Updated</th><th />
                </tr>
              </thead>
              <tbody>
                {rows.map((r) => {
                  const meta = RESULT_STATUS[r.status ?? "corrected"];
                  const pub = r.publish_status ? PUBLISH_STATUS[r.publish_status] : null;
                  return (
                    <tr key={r.id} onClick={() => choose(r.id)} style={{ cursor: "pointer" }}>
                      <td data-label="Student"><strong>{displayName(r)}</strong></td>
                      <td data-label="Assignment" className="muted small trunc-cell"
                        title={r.assignment_title ?? undefined}>
                        {r.assignment_title ?? "—"}
                      </td>
                      <td data-label="Class" className="muted small trunc-cell"
                        title={r.course_name ?? undefined}>{r.course_name ?? "—"}</td>
                      <td data-label="Score" className="num mono" style={{ fontWeight: 700 }}>
                        {r.earned}/{r.available}
                        {r.double_check_triggered ? (
                          <span className="faint small" style={{ marginLeft: 6 }}
                            title={`Independently double-checked (${r.double_check_status})`}>
                            ✓ 2×
                          </span>
                        ) : null}
                      </td>
                      <td data-label="%" className="num">{r.percentage.toFixed(1)}%</td>
                      <td data-label="Status">
                        <Badge tone={meta.tone} icon={meta.icon}>{meta.label}</Badge>
                        {(r.version_count ?? 0) > 1 && (
                          <span className="faint small" style={{ marginLeft: 6 }}>
                            {r.version_count} versions
                          </span>
                        )}
                      </td>
                      <td data-label="Classroom">
                        {pub ? <Badge tone={pub.tone}>{pub.label}</Badge>
                          : <span className="faint small">—</span>}
                      </td>
                      <td data-label="Updated" className="faint small">{timeAgo(r.created_at)}</td>
                      <td className="num">
                        <Button size="sm" variant="ghost" iconRight="chevron-right"
                          onClick={(e) => { e.stopPropagation(); choose(r.id); }}>
                          Open
                        </Button>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
          {rows.length < total && (
            <div className="row" style={{ justifyContent: "center", marginTop: 14 }}>
              <Button variant="secondary" icon="chevron-down"
                onClick={() => setLimit((n) => n + PAGE)}>
                Load {Math.min(PAGE, total - rows.length)} more
              </Button>
            </div>
          )}
        </>
      )}
    </>
  );
}
