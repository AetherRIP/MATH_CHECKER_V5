import {
  useCallback, useEffect, useId, useMemo, useRef, useState, type ReactNode,
} from "react";
import {
  ACTIVE_STATUSES, api, cancelJob, displayName, elapsed, postForm, retryJob, timeAgo,
  type AssignmentDetail, type AssignmentOverview, type ClassOverview, type Job,
  type MarkSchemeQuestion, type MarkSchemeSubquestion, type MarkSchemeValidation,
  type PublishQueue, type PublishResult, type TrackingData, type TrackingRow,
} from "../api";
import {
  Badge, Button, DataTable, Empty, Icon, IconButton, Modal, PageHeader, Progress,
  rowActivate, Segmented, StatusBadge, Tabs, useToast,
} from "../components";
import { flaggedParts, flaggedSummary, structureStartsOpen } from "../schemeStructure";
import { SyncClassroomButton } from "../SyncClassroom";
import SchemeSourceViewer from "./SchemeSourceViewer";

type KindFilter = "all" | "managed" | "external" | "local";

interface CorrectAllPreview {
  assignment_title: string;
  will_correct: number;
  can_start: boolean;
  blockers: string[];
  counts: Record<string, number>;
  eligible: { submission_id: string; student_name: string; display_name?: string | null }[];
  already_corrected: { submission_id: string; student_name: string; display_name?: string | null }[];
  needs_review: { submission_id: string; student_name: string; display_name?: string | null }[];
  sync_warning: string | null;
}

/* ------------------------------------------------------------ managed badge */

function ManagedBadge({ assignment }: { assignment: { managed: boolean; local: boolean } }) {
  if (assignment.local) return <Badge tone="mute" icon="database">Local</Badge>;
  if (assignment.managed) return <Badge tone="ok" icon="check-circle">Managed</Badge>;
  return <Badge tone="warn" icon="external">External</Badge>;
}

/* ================================================================== page */

export default function AssignmentsPage({ assignmentId, onNavigate }: {
  assignmentId: string | null; onNavigate: (hash: string) => void;
}) {
  return assignmentId
    ? <AssignmentDetailView id={assignmentId} onNavigate={onNavigate} />
    : <AssignmentsList onNavigate={onNavigate} />;
}

/* --------------------------------------------------------------- list view */

function AssignmentsList({ onNavigate }: { onNavigate: (hash: string) => void }) {
  const toast = useToast();
  const [items, setItems] = useState<AssignmentOverview[] | null>(null);
  const [classes, setClasses] = useState<ClassOverview[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [q, setQ] = useState("");
  const [kind, setKind] = useState<KindFilter>("all");
  const [wizard, setWizard] = useState(false);

  const load = useCallback(() => {
    setError(null);
    api<{ assignments: AssignmentOverview[] }>("/assignments")
      .then((r) => setItems(r.assignments)).catch((e) => setError(e.message));
  }, []);
  useEffect(() => {
    load();
    api<{ classes: ClassOverview[] }>("/classes").then((r) => setClasses(r.classes)).catch(() => {});
  }, [load]);

  const filtered = useMemo(() => {
    let list = items ?? [];
    if (kind === "managed") list = list.filter((a) => a.managed);
    if (kind === "external") list = list.filter((a) => !a.managed && !a.local);
    if (kind === "local") list = list.filter((a) => a.local);
    const needle = q.trim().toLowerCase();
    if (needle) {
      list = list.filter((a) => a.title.toLowerCase().includes(needle)
        || (a.course_name ?? "").toLowerCase().includes(needle));
    }
    return list;
  }, [items, kind, q]);

  return (
    <>
      <PageHeader title="Assignments" icon="assignments"
        sub="Every assignment: what was submitted, what has been corrected, and what can be published to Classroom.">
        <input className="input" style={{ maxWidth: 220 }} placeholder="Search assignments…"
          value={q} onChange={(e) => setQ(e.target.value)} aria-label="Search assignments" />
        {/* The SAME shared sync every other page uses: it pulls the courses,
            assignments, students and submissions this page reads, so an
            assignment that exists in Classroom but not yet here can be found
            without leaving the page. */}
        <SyncClassroomButton onSynced={load} />
        <Button variant="ghost" icon="refresh" onClick={load}>Refresh</Button>
        <Button variant="primary" icon="plus" onClick={() => setWizard(true)}>Create Assignment</Button>
      </PageHeader>

      <div className="chip-row" style={{ marginBottom: 12 }}>
        {(["all", "managed", "external", "local"] as KindFilter[]).map((k) => (
          <button key={k} className={`chip ${kind === k ? "active" : ""}`} onClick={() => setKind(k)}>
            {k === "all" ? "All" : k === "managed" ? "Managed by Math Checker"
              : k === "external" ? "External Classroom" : "Local only"}
          </button>
        ))}
      </div>

      {error && <div className="notice err" style={{ marginBottom: 12 }}>{error}</div>}

      {!items ? (
        <div className="stack">
          {[0, 1, 2].map((i) => <div key={i} className="skeleton" style={{ height: 90 }} />)}
        </div>
      ) : filtered.length === 0 ? (
        <div className="card">
          <Empty icon="assignments" art={items.length === 0 ? "paper" : "search"}
            title={items.length === 0 ? "No assignments yet" : "Nothing matches"}
            hint={items.length === 0
              ? "Create your first assignment to begin correcting submissions."
              : "Try a different filter or search."}
            action={<Button variant="primary" icon="plus" onClick={() => setWizard(true)}>
              Create Assignment</Button>} />
        </div>
      ) : (
        <div className="card">
          <DataTable minWidth={900} head={
            <tr>
              <th>Assignment</th><th>Class</th><th>Type</th>
              <th className="num">Students</th><th className="num">Submitted</th>
              <th className="num">Checked</th><th className="num">Review</th>
              <th className="num">Average</th><th>Status</th>
            </tr>
          }>
            {filtered.map((a) => {
              const correction = a.submitted > 0 ? Math.min(1, a.checked / a.submitted) : 0;
              return (
                <tr key={a.id} className="clickable"
                  {...rowActivate(() => onNavigate(`#/assignments/${encodeURIComponent(a.id)}`))}
                  onClick={() => onNavigate(`#/assignments/${encodeURIComponent(a.id)}`)}>
                  <td data-label="Assignment">
                    <div style={{ fontWeight: 650 }}>{a.title}</div>
                    <div className="faint small">
                      {a.due_date ? `due ${a.due_date}` : "no due date"}
                      {a.max_points ? ` · ${a.max_points} marks` : ""}
                    </div>
                  </td>
                  <td data-label="Class" className="small">{a.course_name ?? "—"}</td>
                  <td data-label="Type"><ManagedBadge assignment={a} /></td>
                  <td data-label="Students" className="num">{a.students_total}</td>
                  <td data-label="Submitted" className="num">{a.submitted}</td>
                  <td data-label="Checked" className="num">{a.checked}</td>
                  <td data-label="Review" className="num">
                    {a.needs_review > 0
                      ? <Badge tone="warn">{a.needs_review}</Badge>
                      : <span className="faint">0</span>}
                  </td>
                  <td data-label="Average" className="num">
                    {a.average != null ? `${a.average}%` : "—"}
                  </td>
                  <td data-label="Status">
                    <div style={{ minWidth: 120 }}>
                      <Progress value={correction} thin tone="ok" />
                      <div className="faint small" style={{ marginTop: 3 }}>
                        {a.processing > 0 ? `${a.processing} correcting`
                          : a.failed > 0 ? `${a.failed} failed`
                          : `${Math.round(correction * 100)}% checked`}
                      </div>
                    </div>
                  </td>
                </tr>
              );
            })}
          </DataTable>
        </div>
      )}

      {wizard && (
        <CreateWizard classes={classes} onClose={() => setWizard(false)}
          onCreated={(id) => { setWizard(false); toast("ok", "Assignment created"); load();
            onNavigate(`#/assignments/${encodeURIComponent(id)}`); }} />
      )}
    </>
  );
}

/* ------------------------------------------------------------- detail view */

/* ------------------------------------------------ mark-scheme validation (F) */

/** §17: the teacher-facing verification result. Every line is a check against
 *  an independent source - never "the AI said N". */
const MODE_LABEL: Record<string, string> = {
  NONE: "Not readable locally",
  IMAGE_ONLY: "Image-only PDF — no text layer",
  TEXT: "Text layer",
};

function Verdict({ ok, children }: { ok: boolean; children: ReactNode }) {
  return (
    <div className="small" style={{ display: "flex", gap: 6, alignItems: "flex-start" }}>
      <Icon name={ok ? "check-circle" : "alert"} size={14}
        style={{ color: ok ? "var(--ok)" : "var(--warn)", flexShrink: 0 }} />
      <span>{children}</span>
    </div>
  );
}

function MarkSchemeVerificationResult({ data, onInspect, onVerifyAgain, busy }: {
  data: MarkSchemeValidation; onInspect: () => void; onVerifyAgain: () => void;
  busy: boolean;
}) {
  const ver = data.verification;
  if (!ver) return null;
  const v = data.validation;
  // DISCREPANCIES decide the verdict; evidence notes ("no text layer") explain
  // how the pages had to be read. Presenting the second as the first would flag
  // a perfectly consistent scheme as broken.
  const anomalies = ver.discrepancies ?? ver.anomalies ?? [];
  const readingNotes = ver.evidence_notes ?? [];
  const clean = ver.verified === true && anomalies.length === 0;
  const pct = Math.round((ver.structural_confidence ?? 0) * 100);
  const calc = ver.calculated_total;
  const printed = ver.printed_total;
  const totalsMatch = calc != null && printed != null && calc === printed;
  const seqComplete = (ver.missing_questions?.length ?? 0) === 0
    && v.duplicate_numbers.length === 0;
  const mode = ver.extraction_mode ?? "NONE";

  return (
    <div className={`notice ${clean ? "ok" : "warn"}`} style={{ marginBottom: 10 }}>
      {/* .notice is a flex row, so everything goes in one column child. */}
      <div style={{ flex: 1, minWidth: 0 }}>
      <div className="row between wrap" style={{ gap: 8, marginBottom: 6 }}>
        <strong style={{ fontSize: 12, letterSpacing: "0.04em" }}>
          MARK SCHEME VERIFICATION
        </strong>
        {clean
          ? <Badge tone="ok" icon="check">Verified</Badge>
          : <Badge tone="warn" icon="alert">
              {anomalies.length === 1 ? "Discrepancy detected" : `${anomalies.length} discrepancies`}
            </Badge>}
      </div>

      {clean ? (
        <>
          <Verdict ok>{v.question_count} questions detected</Verdict>
          <Verdict ok>{calc ?? v.total_marks} total marks</Verdict>
          <Verdict ok>
            Question sequence complete{" "}
            <span className="muted">
              ({v.first ?? "—"}
              {v.last != null && v.last !== v.first ? `–${v.last}` : ""})
            </span>
          </Verdict>
          <Verdict ok>Question/subquestion structure consistent</Verdict>
          <Verdict ok>
            Calculated marks match paper total
            {printed != null ? ` (${calc} = ${printed})` : ""}
          </Verdict>
          <div className="small" style={{ marginTop: 6 }}>
            <strong>Confidence: {pct}%</strong>{" "}
            <span className="muted">· structural confidence, cross-checked against
              {" "}{ver.local_question_candidates ?? 0} question candidate(s) found in
              the document itself</span>
          </div>
          <div className="small muted" style={{ marginTop: 2 }}>
            No structural problems detected.
          </div>
          {readingNotes.map((note, i) => (
            <div key={i} className="small muted" style={{ marginTop: 2 }}>
              How it was read: {note}
            </div>
          ))}
        </>
      ) : (
        <>
          <div className="small" style={{ marginBottom: 6 }}>
            <strong>Structural discrepancy detected.</strong> This structure could not
            be confirmed against the independent evidence below, so it is not safe to
            mark against until it is resolved. A worse result never replaces a
            verified structure, so nothing has been overwritten.
          </div>
          <table className="table small" style={{ marginBottom: 6 }}>
            <tbody>
              <tr><td>Questions in stored structure</td>
                <td className="num">{v.question_count}</td></tr>
              <tr><td>Question candidates found in the PDF text</td>
                <td className="num">{ver.local_question_candidates ?? 0}</td></tr>
              <tr><td>Printed paper total</td>
                <td className="num">{printed ?? "—"}</td></tr>
              <tr><td>Total calculated from the question structure</td>
                <td className="num">{calc ?? ver.parser_total ?? "—"}</td></tr>
            </tbody>
          </table>
          {ver.likely_cause && (
            <div className="small" style={{ marginBottom: 4 }}>
              <strong>Likely cause:</strong> {ver.likely_cause}
            </div>
          )}
          <ul className="small" style={{ margin: "4px 0 0", paddingLeft: 18 }}>
            {anomalies.slice(0, 6).map((a, i) => <li key={i}>{a}</li>)}
            {anomalies.length > 6 &&
              <li className="muted">+{anomalies.length - 6} more…</li>}
          </ul>
          {readingNotes.map((note, i) => (
            <div key={i} className="small muted" style={{ marginTop: 4 }}>
              How it was read: {note}
            </div>
          ))}
          {!seqComplete && (
            <div className="small" style={{ marginTop: 4 }}>
              Question sequence: missing{" "}
              <strong>{(ver.missing_questions ?? []).join(", ") || "—"}</strong>
              {v.duplicate_numbers.length > 0 &&
                <> · duplicated <strong>{v.duplicate_numbers.join(", ")}</strong></>}
            </div>
          )}
          {totalsMatch === false && (
            <div className="small" style={{ marginTop: 4 }}>
              <strong>Mark allocation mismatch:</strong> the questions add up to{" "}
              {calc} marks but the mark scheme states {printed}.
            </div>
          )}
        </>
      )}

      <div className="small muted" style={{ marginTop: 8, lineHeight: 1.6 }}>
        Evidence: {MODE_LABEL[mode] ?? mode} · {ver.pdf_pages ?? 0} page(s) ·{" "}
        {ver.extracted_blocks ?? 0} block(s) · {ver.two_column_pages ?? 0} two-column
        page(s) · {ver.image_count ?? 0} image(s) ·{" "}
        {ver.page_number_candidates ?? 0} page-number candidate(s) excluded from
        the question list
      </div>

      <div className="row wrap" style={{ gap: 8, marginTop: 8 }}>
        <Button size="sm" variant="secondary" icon="list" onClick={onInspect}>
          Inspect questions
        </Button>
        <Button size="sm" variant="primary" icon="shield" loading={busy}
          onClick={onVerifyAgain}>Verify again</Button>
      </div>
      </div>
    </div>
  );
}

/**
 * §2/§9: the declared marking points, so a doubtful extraction can be
 * reviewed at the subquestion that is actually wrong instead of invalidating
 * an otherwise-correct mark scheme.
 *
 * The block is COLLAPSIBLE: a long scheme's structure would otherwise push the
 * verdict (and the marking points the teacher actually came for) off the
 * screen. It folds away when every marking point is accounted for and opens
 * itself when something needs review; a click on the header wins over both.
 * Folded, the header still names the flagged parts, so the default hides
 * nothing — see `schemeStructure.ts`.
 */
function SubquestionStructure({ subs, anomalies, complete }: {
  subs: MarkSchemeSubquestion[]; anomalies: string[];
  complete: boolean | null | undefined;
}) {
  const flagged = flaggedParts(subs);
  const startsOpen = structureStartsOpen({
    complete, flagged: flagged.length, anomalies: anomalies.length });
  // null = "no opinion yet", so the block follows the data until the teacher
  // says otherwise (data can arrive or change after the first render, e.g. on
  // "Verify again").
  const [shown, setShown] = useState<boolean | null>(null);
  const open = shown ?? startsOpen;
  const panelId = useId();
  if (subs.length === 0) return null;
  const summary = flaggedSummary(subs);
  return (
    <div className="notice info" style={{ marginBottom: 10 }}>
      <Icon name="list" size={16} />
      <div style={{ flex: 1, minWidth: 0 }}>
        <div className="row between wrap" style={{ gap: 8, marginBottom: open ? 4 : 0 }}>
          <button type="button" className="disclosure" aria-expanded={open}
            aria-controls={panelId} onClick={() => setShown(!open)}>
            <Icon name={open ? "chevron-down" : "chevron-right"} size={14} />
            <strong style={{ fontSize: 12, letterSpacing: "0.04em" }}>
              SUBQUESTION STRUCTURE ({subs.length})
            </strong>
            <span className="small muted">{open ? "Hide" : "Show"}</span>
          </button>
          {complete === false
            ? <Badge tone="warn" icon="alert">
                {flagged.length} need review</Badge>
            : <Badge tone="ok" icon="check">Each marking point mapped</Badge>}
        </div>
        {!open && (
          <div className="small muted">
            Folded — {summary.labels.length > 0
              ? <>flagged: <strong>{summary.labels.join(", ")}</strong>
                  {summary.more > 0 && <> +{summary.more} more</>}</>
              : <>every marking point is mapped</>}
            .
          </div>
        )}
        {open && (
          <div id={panelId}>
            <table className="table small" style={{ marginBottom: 4 }}>
              <thead>
                <tr><th>Subquestion</th><th className="num">Marks</th>
                  <th>Expected answer(s)</th><th>Marking points</th><th /></tr>
              </thead>
              <tbody>
                {subs.map((s) => {
                  const ok = (s.validation ?? "OK") === "OK";
                  return (
                    <tr key={s.id}>
                      <td><strong>Q{s.question}</strong> {s.label}</td>
                      <td className="num">{s.max_marks}</td>
                      <td>{s.answers.length ? s.answers.join(" or ") : <span className="muted">not stated</span>}</td>
                      <td>{s.mark_codes.length ? s.mark_codes.join(", ") : <span className="muted">none stated</span>}</td>
                      <td>{ok
                        ? <Icon name="check" size={13} style={{ color: "var(--ok)" }} />
                        : <Icon name="alert" size={13} style={{ color: "var(--warn)" }} />}</td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
            {anomalies.length > 0 && (
              <ul className="small" style={{ margin: "4px 0 0", paddingLeft: 18 }}>
                {anomalies.slice(0, 6).map((a, i) => <li key={i}>{a}</li>)}
                {anomalies.length > 6 &&
                  <li className="muted">+{anomalies.length - 6} more…</li>}
              </ul>
            )}
            <div className="small muted" style={{ marginTop: 4 }}>
              Each marking point keeps its own answers, M/A/B codes and page. A part
              the marker does not return stays UNMARKED for review — never a silent
              zero, and never another part's marks.
            </div>
          </div>
        )}
      </div>
    </div>
  );
}

function MarkSchemePanel({ courseworkId, hasMarkScheme, onChanged }: {
  courseworkId: string; hasMarkScheme: boolean; onChanged: () => void;
}) {
  const toast = useToast();
  const [data, setData] = useState<MarkSchemeValidation | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [editing, setEditing] = useState(false);
  const [rows, setRows] = useState<MarkSchemeQuestion[]>([]);
  const [dismissed, setDismissed] = useState(false);
  // "Where did this question come from?" — the real page, on demand.
  const [viewingSource, setViewingSource] = useState(false);
  const [focusQuestion, setFocusQuestion] = useState<string | null>(null);

  const load = useCallback(() => {
    api<MarkSchemeValidation>(`/assignments/${encodeURIComponent(courseworkId)}/mark-scheme`)
      .then(setData).catch(() => {});
  }, [courseworkId]);
  useEffect(() => { load(); }, [load]);

  async function recheck() {
    setBusy("recheck");
    try {
      const res = await api<MarkSchemeValidation>(
        `/assignments/${encodeURIComponent(courseworkId)}/mark-scheme/recheck`,
        { method: "POST" });
      setData(res); setDismissed(false);
      toast(res.recheck?.disagreement ? "warn" : "ok",
        `Rechecked: ${res.validation.question_count} question(s) · `
        + `${res.validation.total_marks} marks`
        + (res.recheck?.disagreement ? " (passes disagreed — kept the most complete)" : ""));
    } catch (e) { toast("err", (e as Error).message); } finally { setBusy(null); }
  }

  function openEditor() {
    setRows((data?.questions ?? []).map((q) => ({ ...q })));
    setEditing(true);
  }

  function setRow(i: number, patch: Partial<MarkSchemeQuestion>) {
    setRows((prev) => prev.map((r, idx) => (idx === i ? { ...r, ...patch } : r)));
  }

  async function saveOverride() {
    setBusy("save");
    try {
      const res = await api<MarkSchemeValidation>(
        `/assignments/${encodeURIComponent(courseworkId)}/mark-scheme/override`,
        { method: "POST", body: JSON.stringify({ questions: rows, note: "Manual structure edit" }) });
      setData(res); setEditing(false); setDismissed(false);
      toast("ok", "Mark scheme structure saved");
      onChanged();
    } catch (e) { toast("err", (e as Error).message); } finally { setBusy(null); }
  }

  if (!hasMarkScheme) {
    return (
      <div className="notice info" style={{ marginBottom: 12 }}>
        <Icon name="alert" size={16} />
        <span>Upload the mark scheme to validate its question structure before correcting.</span>
      </div>
    );
  }
  if (!data) return <div className="skeleton" style={{ height: 80, marginBottom: 12 }} />;

  const v = data.validation;
  const needsLook = !v.complete || (data.warnings?.length ?? 0) > 0;
  // §26: the parser looks suspicious, so offer an independent Gemini check
  // rather than silently marking against a structure we do not trust.
  const suspect = data.requires_verification === true;
  const verified = data.confidence === "HIGH" && !suspect;

  return (
    <div className="card" style={{ marginBottom: 16 }}>
      <div className="row between wrap" style={{ marginBottom: 8 }}>
        <h3 style={{ margin: 0 }}>
          <Icon name="check-circle" size={16} style={{ color: "var(--accent)" }} />
          Mark scheme validation
          {data.processed
            ? (verified
              ? <Badge tone="ok" icon="check">Verified</Badge>
              : <Badge tone="warn" icon="alert">Structure requires review</Badge>)
            : <Badge tone="mute">Not processed</Badge>}
          {data.manual_override && <Badge tone="info" icon="edit">Edited by you</Badge>}
        </h3>
        <div className="row wrap">
          <Button size="sm" variant="secondary" icon="refresh"
            loading={busy === "recheck"} onClick={recheck}>Recheck mark scheme</Button>
          <Button size="sm" variant="ghost" icon="file"
            onClick={() => { setFocusQuestion(null); setViewingSource((v) => !v); }}>
            {viewingSource ? "Hide source pages" : "Source pages"}</Button>
          <Button size="sm" variant="ghost" icon="edit"
            onClick={openEditor} disabled={!data.processed}>Review structure</Button>
        </div>
      </div>

      {data.processed && (
        <div className="grid grid-auto" style={{ marginBottom: 10 }}>
          <div className="stat"><div className="label">Detected questions</div>
            <div className="value">{v.question_count}</div></div>
          <div className="stat"><div className="label">Total marks</div>
            <div className="value">{v.total_marks}</div></div>
          {v.has_subquestions && (
            <div className="stat"><div className="label">Subquestions</div>
              <div className="value">{v.subquestion_count ?? 0}</div></div>
          )}
          <div className="stat"><div className="label">Numbering</div>
            <div className="value">
              {v.first ?? "—"}{v.last != null && v.last !== v.first ? `–${v.last}` : ""}
            </div></div>
        </div>
      )}

      {/* §2/§9: the declared marking points themselves, before the verdict. */}
      {data.processed && (
        <SubquestionStructure subs={data.subquestions ?? []}
          anomalies={data.subquestion_anomalies ?? []}
          complete={v.subquestions_complete} />
      )}

      {/* §17: the verification result itself, with the evidence behind it. */}
      {data.processed && (
        <MarkSchemeVerificationResult data={data} onInspect={openEditor}
          onVerifyAgain={recheck} busy={busy === "recheck"} />
      )}

      {/* The source itself: every page of the uploaded file, the questions the
          read attributed to it, and any page that returned nothing. */}
      {data.processed && viewingSource && (
        <div style={{ marginBottom: 12 }}>
          <SchemeSourceViewer courseworkId={courseworkId}
            questions={data.questions ?? []} focusQuestion={focusQuestion}
            onClose={() => setViewingSource(false)} />
        </div>
      )}

      {suspect && (
        <div className="notice warn" style={{ marginBottom: 10 }}>
          <Icon name="alert" size={16} />
          <div>
            <strong>Mark scheme requires verification.</strong>
            <div className="small" style={{ marginTop: 4 }}>
              Math Correct could not confidently determine the paper structure.
            </div>
            <ul className="small" style={{ margin: "6px 0 0", paddingLeft: 18 }}>
              {(data.confidence_reasons ?? []).map((r, i) => <li key={i}>{r}</li>)}
            </ul>
            <div className="row wrap" style={{ gap: 8, marginTop: 8 }}>
              <Button size="sm" variant="ghost" icon="file"
                onClick={() => { setFocusQuestion(null); setViewingSource(true); }}>
                Open the source pages
              </Button>
            </div>
            {data.totals_agree === false && (
              <div className="small" style={{ marginTop: 6 }}>
                <strong>Mark scheme mismatch:</strong> detected question total{" "}
                {data.detected_total} vs stated total {data.declared_total}.
                Manual verification required.
              </div>
            )}
            <div className="row wrap" style={{ gap: 8, marginTop: 8 }}>
              <Button size="sm" variant="primary" icon="shield" loading={busy === "recheck"}
                onClick={recheck}>Verify mark scheme</Button>
              <Button size="sm" variant="secondary" icon="edit" onClick={openEditor}>
                Edit manually
              </Button>
            </div>
          </div>
        </div>
      )}

      {needsLook && !suspect && !dismissed && (
        <div className="notice warn">
          <Icon name="alert" size={16} />
          <div>
            <strong>Possible incomplete extraction</strong>
            {!v.complete && (
              <div className="small" style={{ marginTop: 4 }}>
                {v.gaps.length > 0 && <div>Missing question numbers: {v.gaps.join(", ")}</div>}
                {v.duplicate_numbers.length > 0 &&
                  <div>Duplicate numbers: {v.duplicate_numbers.join(", ")}</div>}
                {v.missing_max_marks.length > 0 &&
                  <div>No mark allocation: {v.missing_max_marks.join(", ")}</div>}
              </div>
            )}
            {data.warnings?.map((w, i) => <div key={i} className="small">{w}</div>)}
            <div className="row wrap" style={{ gap: 8, marginTop: 8 }}>
              <Button size="sm" variant="secondary" icon="refresh"
                loading={busy === "recheck"} onClick={recheck}>Recheck</Button>
              <Button size="sm" variant="secondary" icon="edit" onClick={openEditor}>Review</Button>
              <Button size="sm" variant="ghost" onClick={() => setDismissed(true)}>
                Continue anyway</Button>
            </div>
          </div>
        </div>
      )}

      {editing && (
        <Modal title="Review mark-scheme structure" icon="edit" wide
          onClose={() => setEditing(false)}
          footer={
            <>
              <Button variant="ghost" onClick={() => setEditing(false)}>Cancel</Button>
              <Button variant="ghost" icon="plus"
                onClick={() => setRows((p) => [...p, { question_number: "", max_marks: 1, criteria: "" }])}>
                Add question</Button>
              <Button variant="primary" icon="check" loading={busy === "save"}
                onClick={saveOverride}>Save structure</Button>
            </>
          }>
          <p className="muted small" style={{ marginTop: 0 }}>
            Fix a question count, numbering or mark allocation. This is saved for
            future corrections of this mark scheme — existing results are never changed.
          </p>
          <div style={{ maxHeight: 300, overflow: "auto" }}>
            <table className="table">
              <thead><tr><th>Question</th><th className="num">Max marks</th><th /></tr></thead>
              <tbody>
                {rows.map((r, i) => (
                  <tr key={i}>
                    <td><input className="input" value={r.question_number}
                      aria-label={`Question ${i + 1} number`}
                      onChange={(e) => setRow(i, { question_number: e.target.value })} /></td>
                    <td><input className="input" type="number" min={1} value={r.max_marks}
                      aria-label={`Marks available for question ${r.question_number || i + 1}`}
                      onChange={(e) => setRow(i, { max_marks: Number(e.target.value) })} /></td>
                    <td>
                      <IconButton icon="trash" label="Remove question" size="sm"
                        onClick={() => setRows((p) => p.filter((_, idx) => idx !== i))} />
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <div className="notice info small">
            Total: {rows.reduce((s, r) => s + (Number(r.max_marks) || 0), 0)} marks · {rows.length} question(s)
          </div>
        </Modal>
      )}
    </div>
  );
}

function AssignmentDetailView({ id, onNavigate }: { id: string; onNavigate: (hash: string) => void }) {
  const toast = useToast();
  const [detail, setDetail] = useState<AssignmentDetail | null>(null);
  const [tracking, setTracking] = useState<TrackingData | null>(null);
  const [queue, setQueue] = useState<PublishQueue | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [tab, setTab] = useState<"submissions" | "publish">("submissions");
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [busy, setBusy] = useState<string | null>(null);
  const [publishMode, setPublishMode] = useState<"draft" | "final">("draft");
  const [confirmPublish, setConfirmPublish] = useState(false);
  const [publishResult, setPublishResult] = useState<PublishResult | null>(null);
  const [preview, setPreview] = useState<CorrectAllPreview | null>(null);
  const [previewOpen, setPreviewOpen] = useState(false);
  const [recheck, setRecheck] = useState(false);
  const [totalDraft, setTotalDraft] = useState<string>("");
  const [liveJobs, setLiveJobs] = useState<Job[]>([]);
  const sourceRef = useRef<EventSource | null>(null);

  useEffect(() => {
    setTotalDraft(detail?.max_points != null ? String(detail.max_points) : "");
  }, [detail?.max_points]);

  /* ---- live progress for THIS assignment ------------------------------------
   * Corrections run in background workers, so the submissions table would
   * otherwise stay frozen at "0 checked" until the teacher manually reloaded.
   * We follow the SSE stream and merge job events scoped to this coursework.
   * Public tunnels that buffer SSE (§14) leave the stream silent forever, so a
   * poll fallback keeps progress moving in that case. */
  useEffect(() => {
    let streamAlive = false;
    let lastEvent = 0;
    const source = new EventSource("/api/jobs/stream");
    sourceRef.current = source;

    const merge = (job: Job) => {
      if (job.coursework_id !== id) return;
      setLiveJobs((jobs) => {
        const next = jobs.filter((j) => j.id !== job.id);
        next.unshift(job);
        return next.slice(0, 25);
      });
    };

    source.onmessage = (event) => {
      streamAlive = true;
      lastEvent = Date.now();
      const payload = JSON.parse(event.data);
      if (payload.type === "hello") {
        api<{ jobs: Job[] }>("/jobs?limit=200")
          .then((r) => setLiveJobs(r.jobs.filter((j) => j.coursework_id === id)))
          .catch(() => {});
        return;
      }
      if (payload.type === "job" && payload.job) merge(payload.job as Job);
    };
    source.onerror = () => { streamAlive = false; };

    // Fallback: only when the stream has not delivered anything recently, so a
    // healthy SSE connection is never duplicated with polling traffic.
    const poll = setInterval(() => {
      if (streamAlive && Date.now() - lastEvent < 30_000) return;
      api<{ jobs: Job[] }>("/jobs?limit=200")
        .then((r) => setLiveJobs(r.jobs.filter((j) => j.coursework_id === id)))
        .catch(() => {});
    }, 6_000);

    return () => { clearInterval(poll); source.close(); sourceRef.current = null; };
  }, [id]);

  const activeHere = useMemo(
    () => liveJobs.filter((j) => ACTIVE_STATUSES.includes(j.status)),
    [liveJobs]);
  const failedHere = useMemo(
    () => liveJobs.filter((j) => j.status === "FAILED").slice(0, 8),
    [liveJobs]);
  const activeCount = activeHere.length;

  async function retryCorrection(jobId: string) {
    setBusy(`retry:${jobId}`);
    try {
      await retryJob(jobId);
      toast("ok", "Correction requeued.");
      setLiveJobs((jobs) => jobs.filter((j) => j.id !== jobId));
      loadTracking(); loadDetail();
    } catch (e) { toast("err", (e as Error).message); } finally { setBusy(null); }
  }

  async function cancelCorrection(jobId: string) {
    setBusy(`cancel:${jobId}`);
    try {
      await cancelJob(jobId);
      toast("info", "Correction cancelled — nothing was deleted.");
      loadTracking(); loadDetail();
    } catch (e) { toast("err", (e as Error).message); } finally { setBusy(null); }
  }

  const loadDetail = useCallback(() => {
    setError(null);
    api<AssignmentDetail>(`/assignments/${encodeURIComponent(id)}`)
      .then(setDetail).catch((e) => setError(e.message));
  }, [id]);
  const loadTracking = useCallback(() => {
    api<TrackingData>(`/classroom/tracking?coursework_id=${encodeURIComponent(id)}`)
      .then(setTracking).catch(() => {});
  }, [id]);
  const loadQueue = useCallback(() => {
    api<PublishQueue>(`/classroom/assignments/${encodeURIComponent(id)}/publish/queue`)
      .then(setQueue).catch(() => {});
  }, [id]);

  useEffect(() => { loadDetail(); loadTracking(); loadQueue(); }, [loadDetail, loadTracking, loadQueue]);

  // The moment the last job for this assignment lands, refresh the rows so the
  // new marks appear without the teacher touching anything.
  const prevActive = useRef(0);
  useEffect(() => {
    if (prevActive.current > 0 && activeCount === 0) {
      loadTracking(); loadDetail(); loadQueue();
    }
    prevActive.current = activeCount;
  }, [activeCount, loadTracking, loadDetail, loadQueue]);

  async function uploadMarkScheme(file: File) {
    if (!detail) return;
    setBusy("scheme");
    const body = new FormData();
    body.append("file", file);
    try {
      await postForm(
        `/classroom/courses/${detail.course_id}/assignments/${id}/mark-scheme`, body);
      toast("ok", "Mark scheme uploaded");
      loadDetail();
    } catch (e) { toast("err", (e as Error).message); } finally { setBusy(null); }
  }

  async function correctSelected() {
    if (!detail || selected.size === 0) return;
    setBusy("correct");
    try {
      const r = await api<{ enqueued: number; duplicates: string[]; skipped_not_submitted: string[];
        skipped_no_attachment: string[]; unknown: string[] }>(
        `/classroom/courses/${detail.course_id}/assignments/${id}/correct`,
        { method: "POST", body: JSON.stringify({ submission_ids: [...selected] }) });
      toast(r.enqueued > 0 ? "ok" : "warn",
        `Queued ${r.enqueued} correction(s)` +
        (r.skipped_not_submitted.length ? ` · ${r.skipped_not_submitted.length} not submitted` : "") +
        (r.skipped_no_attachment.length ? ` · ${r.skipped_no_attachment.length} no attachment` : ""));
      setSelected(new Set());
      loadTracking(); loadDetail();
    } catch (e) { toast("err", (e as Error).message); } finally { setBusy(null); }
  }

  async function openCorrectAll() {
    if (!detail) return;
    setBusy("preview");
    try {
      const p = await api<CorrectAllPreview>(
        `/assignments/${encodeURIComponent(id)}/correct-all/preview?refresh=true`);
      setPreview(p); setRecheck(false); setPreviewOpen(true);
      if (p.sync_warning) toast("warn", p.sync_warning);
    } catch (e) { toast("err", (e as Error).message); } finally { setBusy(null); }
  }

  async function runCorrectAll() {
    if (!detail) return;
    setBusy("correct-all");
    try {
      const r = await api<{ enqueued: number; already_corrected: string[];
        needs_review: string[]; skipped_missing: string[]; invalid: string[];
        enqueue_failed: unknown[]; sync_warning: string | null }>(
        `/classroom/courses/${detail.course_id}/assignments/${id}/correct-all`,
        { method: "POST", body: JSON.stringify({ refresh: true, recheck }) });
      toast(r.enqueued > 0 ? "ok" : "warn",
        `Queued ${r.enqueued} correction(s)`
        + (r.already_corrected.length ? ` · ${r.already_corrected.length} already corrected` : "")
        + (r.skipped_missing.length ? ` · ${r.skipped_missing.length} not submitted` : "")
        + (r.invalid.length ? ` · ${r.invalid.length} invalid` : ""));
      if (r.sync_warning) toast("warn", r.sync_warning);
      setPreviewOpen(false);
      loadTracking(); loadDetail();
    } catch (e) { toast("err", (e as Error).message); } finally { setBusy(null); }
  }

  async function updateClassroomTotal() {
    if (!detail) return;
    const value = Number(totalDraft);
    if (!Number.isFinite(value) || value <= 0) {
      toast("err", "Enter a total greater than 0.");
      return;
    }
    if (value === detail.max_points) {
      toast("info", "The Classroom total is already that value.");
      return;
    }
    setBusy("total");
    try {
      const r = await api<{ max_points: number; verified: boolean }>(
        `/classroom/assignments/${encodeURIComponent(id)}/classroom-total`,
        { method: "POST", body: JSON.stringify({ max_points: value }) });
      toast("ok", `Google Classroom total is now ${r.max_points}.`);
      loadDetail(); loadQueue();
    } catch (e) { toast("err", (e as Error).message); } finally { setBusy(null); }
  }

  function toggleSelect(sid: string, on: boolean) {
    setSelected((prev) => {
      const next = new Set(prev);
      if (on) next.add(sid); else next.delete(sid);
      return next;
    });
  }

  const ops = useMemo(() => {
    if (!queue) return [] as { type: string; submission_id: string; grade: number | null }[];
    const ready = queue.items.filter((i) =>
      !i.review_required && i.suggested_grade != null && selectedIds(queue, selected).has(i.submission_id));
    return ready.map((i) => ({
      type: publishMode === "draft" ? "draft_grade" : "final_grade",
      submission_id: i.submission_id,
      grade: i.suggested_grade,
    }));
  }, [queue, publishMode, selected]);

  async function doPublish(attemptUnmanaged: boolean) {
    if (!detail) return;
    setBusy("publish");
    try {
      const r = await api<PublishResult>(
        `/classroom/assignments/${encodeURIComponent(id)}/publish`,
        { method: "POST", body: JSON.stringify({
          course_id: detail.course_id, operations: ops, attempt_unmanaged: attemptUnmanaged }) });
      setPublishResult(r);
      setConfirmPublish(false);
      const s = r.summary;
      const tone = s.failed > 0 || s.not_supported > 0 ? "warn" : "ok";
      toast(tone, `Published ${s.published} · ${s.failed} failed · ${s.not_supported} not supported`
        + (s.requires_review ? ` · ${s.requires_review} need review` : ""));
      loadQueue();
    } catch (e) { toast("err", (e as Error).message); } finally { setBusy(null); }
  }

  if (!detail) {
    return (
      <>
        <PageHeader title="Assignment" icon="assignments" />
        {error ? <div className="notice err">{error}</div>
          : <div className="skeleton" style={{ height: 200 }} />}
      </>
    );
  }

  const rows = tracking?.rows ?? [];
  const managed = detail.managed;

  return (
    <>
      <PageHeader title={detail.title} icon="assignments"
        sub={`${detail.course_name ?? "Class"}${detail.due_date ? ` · due ${detail.due_date}` : ""}`}>
        <ManagedBadge assignment={detail} />
        <Button variant="ghost" icon="arrow-left" onClick={() => onNavigate("#/assignments")}>
          All assignments
        </Button>
      </PageHeader>

      {error && <div className="notice err" style={{ marginBottom: 12 }}>{error}</div>}

      <div className="grid grid-auto" style={{ marginBottom: 16 }}>
        <div className="stat"><div className="label">Students</div><div className="value">{detail.students_total}</div></div>
        <div className="stat"><div className="label">Submitted</div><div className="value ok">{detail.submitted}</div></div>
        <div className="stat"><div className="label">Checked</div><div className="value">{detail.checked}</div></div>
        <div className="stat"><div className="label">Needs review</div>
          <div className={`value ${detail.needs_review ? "warn" : ""}`}>{detail.needs_review}</div></div>
        <div className="stat"><div className="label">Outstanding</div>
          <div className={`value ${detail.missing ? "warn" : ""}`}>{detail.missing}</div></div>
        <div className="stat"><div className="label">Average</div>
          <div className="value">{detail.average != null ? `${detail.average}%` : "—"}</div></div>
      </div>

      {/* ---- live correction progress (Stage 5) ---------------------------- */}
      {(activeHere.length > 0 || failedHere.length > 0) && (
        <div className="card" style={{ marginBottom: 16 }}>
          {activeHere.length > 0 && (
            <>
              <div className="row between wrap" style={{ marginBottom: 12 }}>
                <h3 style={{ margin: 0 }}>
                  <Icon name="wand" size={16} style={{ color: "var(--accent)" }} />
                  Correcting now
                  <span className="hint">{activeHere.length} in progress</span>
                </h3>
                <span className="small faint">Updates automatically — no refresh needed</span>
              </div>
              <div className="stack" style={{ gap: 12 }}>
                {activeHere.map((job) => (
                  <div key={job.id} className="stack" style={{ gap: 6 }}>
                    <div className="row between wrap">
                      <span style={{ fontWeight: 650 }}>
                        {displayName(job)}
                        {job.attempts > 1 && (
                          <span className="badge warn" style={{ marginLeft: 6, fontSize: 10 }}>
                            retry {job.attempts - 1}
                          </span>
                        )}
                      </span>
                      <div className="row" style={{ gap: 8, alignItems: "center" }}>
                        <StatusBadge status={job.status} />
                        <IconButton icon="x" label="Cancel this correction"
                          onClick={() => cancelCorrection(job.id)} />
                      </div>
                    </div>
                    <Progress value={job.progress} thin
                      label={`Correcting ${displayName(job)}`} />
                    <div className="row between small muted">
                      <span>{job.stage_detail ?? job.status.replace(/_/g, " ").toLowerCase()}</span>
                      <span className="faint mono">
                        {Math.round(job.progress)}% · {elapsed(job.created_at, job.finished_at)}
                        {job.max_attempts ? ` · attempt ${job.attempts}/${job.max_attempts}` : ""}
                      </span>
                    </div>
                  </div>
                ))}
              </div>
            </>
          )}

          {failedHere.length > 0 && (
            <div className="notice err small" style={{ marginTop: activeHere.length ? 14 : 0 }}>
              <div className="row between wrap" style={{ marginBottom: 8 }}>
                <strong>
                  <Icon name="alert" size={15} /> {failedHere.length} correction(s) failed
                </strong>
                <Button size="sm" variant="ghost" icon="sync"
                  onClick={() => onNavigate("#/corrections")}>
                  All failures
                </Button>
              </div>
              <div className="stack" style={{ gap: 6 }}>
                {failedHere.map((job) => (
                  <div key={job.id} className="row between wrap" style={{ gap: 8 }}>
                    <span>
                      <strong>{displayName(job)}</strong>
                      {job.error && <span className="faint"> — {job.error}</span>}
                    </span>
                    <Button size="sm" variant="secondary" icon="sync"
                      loading={busy === `retry:${job.id}`}
                      onClick={() => retryCorrection(job.id)}>
                      Retry
                    </Button>
                  </div>
                ))}
              </div>
            </div>
          )}
        </div>
      )}

      {!managed && (
        <div className="notice warn" style={{ marginBottom: 12 }}>
          <Icon name="external" size={16} />
          <span>
            <strong>External Classroom Assignment.</strong>{" "}
            {detail.capabilities.reason ?? "Not created by Math Checker."}
          </span>
        </div>
      )}

      <MarkSchemePanel courseworkId={id} hasMarkScheme={detail.has_mark_scheme}
        onChanged={loadDetail} />

      <div className="card" style={{ marginBottom: 16 }}>
        <div className="row between wrap" style={{ alignItems: "flex-end", gap: 12 }}>
          <div style={{ minWidth: 240 }}>
            <h3 style={{ margin: 0 }}>
              <Icon name="classroom" size={16} style={{ color: "var(--accent)" }} />
              Google Classroom total points
            </h3>
            <p className="small muted" style={{ margin: "4px 0 0" }}>
              This is the assignment's real total in Google Classroom — kept separate
              from Math Checker's maximum marks. Updating it changes Google Classroom itself.
            </p>
          </div>
          <div className="row" style={{ gap: 8, alignItems: "flex-end" }}>
            <div>
              <div className="small muted">Classroom total</div>
              <input className="input" type="number" min={0.5} step={0.5}
                style={{ maxWidth: 120 }} value={totalDraft}
                onChange={(e) => setTotalDraft(e.target.value)}
                aria-label="Classroom total points" />
            </div>
            <Button variant="primary" icon="send" loading={busy === "total"}
              disabled={!managed} onClick={updateClassroomTotal}>
              Update Classroom Assignment
            </Button>
          </div>
        </div>
        {!managed && (
          <div className="notice warn small" style={{ marginTop: 10 }}>
            <Icon name="external" size={14} />
            <span>{detail.capabilities.reason
              ?? "This assignment was not created by Math Checker; Google may refuse the change."}</span>
          </div>
        )}
      </div>

      <Tabs value={tab} onChange={setTab} ariaLabel="Assignment sections" options={[
        { value: "submissions", label: "Submissions", icon: "students" },
        { value: "publish", label: "Publish to Classroom", icon: "send" },
      ]} />

      {tab === "submissions" && (
        <div className="card">
          <div className="row between wrap" style={{ marginBottom: 12 }}>
            <h3 style={{ margin: 0 }}>
              <Icon name="students" size={16} style={{ color: "var(--accent)" }} />
              Submissions <span className="hint">{rows.length} students</span>
            </h3>
            <div className="row wrap">
              <label className="btn secondary sm" data-tooltip={detail.has_mark_scheme ? "Replace mark scheme" : "Required before correcting"}>
                <Icon name="upload" size={14} />
                {busy === "scheme" ? "Uploading…" : (detail.has_mark_scheme ? "Mark scheme ✓" : "Upload mark scheme")}
                <input type="file" accept="application/pdf" style={{ display: "none" }}
                  onChange={(e) => { const f = e.target.files?.[0]; if (f) uploadMarkScheme(f); }} />
              </label>
              {selected.size > 0 && (
                <Button variant="primary" size="sm" icon="wand" loading={busy === "correct"}
                  onClick={correctSelected}>Correct selected ({selected.size})</Button>
              )}
              <Button variant="secondary" size="sm" icon="wand" loading={busy === "preview"}
                onClick={openCorrectAll} disabled={!detail.allowlisted && !detail.local}>
                Correct all submitted
              </Button>
            </div>
          </div>

          {rows.length === 0 ? (
            <Empty icon="inbox" title="No submissions synced yet"
              hint="Sync Google Classroom to pull submission states for this assignment."
              action={<Button variant="secondary" icon="sync"
                onClick={() => onNavigate("#/classroom")}>Sync Classroom</Button>} />
          ) : (
            <DataTable minWidth={820} head={
              <tr>
                <th style={{ width: 32 }} />
                <th>Student</th><th>Status</th><th>Submitted</th>
                <th className="num">Mark</th><th>Classroom</th><th>Corrected</th>
              </tr>
            }>
              {rows.map((r) => (
                <SubmissionRow key={r.submission_id} row={r}
                  checked={selected.has(r.submission_id)}
                  onCheck={(on) => toggleSelect(r.submission_id, on)}
                  onOpenStudent={() => onNavigate(`#/students/${encodeURIComponent(r.student_id)}`)} />
              ))}
            </DataTable>
          )}
        </div>
      )}

      {tab === "publish" && queue && (
        <PublishPanel
          queue={queue} managed={managed} mode={publishMode} setMode={setPublishMode}
          selected={selected} setSelected={setSelected}
          onPublish={() => setConfirmPublish(true)}
          onAttemptUnmanaged={() => doPublish(true)}
          result={publishResult} />
      )}

      {confirmPublish && queue && (
        <Modal title="Ready to publish" icon="send" onClose={() => setConfirmPublish(false)}
          footer={
            <>
              <Button variant="ghost" onClick={() => setConfirmPublish(false)}>Cancel</Button>
              <Button variant="primary" icon="send" loading={busy === "publish"}
                onClick={() => doPublish(false)}>
                {managed ? `Publish ${ops.length} Grade${ops.length === 1 ? "" : "s"}`
                  : `Attempt publish (${ops.length})`}
              </Button>
            </>
          }>
          <div className="grid grid-3" style={{ gap: 8 }}>
            <div className="stat compact"><div className="label">Checked</div>
              <div className="value">{detail.checked}</div></div>
            <div className="stat compact"><div className="label">Grades ready</div>
              <div className="value ok">{ops.length}</div></div>
            <div className="stat compact"><div className="label">Require review</div>
              <div className="value warn">{queue.summary.needs_review}</div></div>
          </div>
          <div className="notice info small" style={{ marginTop: 12 }}>
            {publishMode === "draft"
              ? "Draft grades are saved to Classroom but are NOT visible to students until you return the work."
              : "Final grades are published AND the work is returned — students will see their mark immediately."}
          </div>
          {!managed && (
            <div className="notice warn small" style={{ marginTop: 8 }}>
              This assignment is external. Google will most likely refuse — we will report Google's
              real response, never a false success.
            </div>
          )}
        </Modal>
      )}

      {previewOpen && preview && (
        <Modal title="Correct all submitted homework" icon="wand"
          onClose={() => setPreviewOpen(false)}
          footer={
            <>
              <Button variant="ghost" onClick={() => setPreviewOpen(false)}>Cancel</Button>
              <Button variant="primary" icon="wand" loading={busy === "correct-all"}
                disabled={!preview.can_start || (preview.will_correct === 0 && !recheck)}
                onClick={runCorrectAll}>
                {recheck
                  ? `Recheck ${preview.will_correct + (preview.counts.already_corrected ?? 0) + (preview.counts.needs_review ?? 0)}`
                  : `Start correction (${preview.will_correct})`}
              </Button>
            </>
          }>
          <div className="grid grid-3" style={{ gap: 8 }}>
            <div className="stat compact"><div className="label">Will correct</div>
              <div className="value ok">{preview.will_correct}</div></div>
            <div className="stat compact"><div className="label">Already corrected</div>
              <div className="value">{preview.counts.already_corrected ?? 0}</div></div>
            <div className="stat compact"><div className="label">Needs review</div>
              <div className="value warn">{preview.counts.needs_review ?? 0}</div></div>
            <div className="stat compact"><div className="label">Not submitted</div>
              <div className="value">{preview.counts.missing ?? 0}</div></div>
            <div className="stat compact"><div className="label">Invalid</div>
              <div className="value">{preview.counts.invalid ?? 0}</div></div>
            <div className="stat compact"><div className="label">In progress</div>
              <div className="value">{preview.counts.processing ?? 0}</div></div>
          </div>
          {preview.blockers.length > 0 && (
            <div className="notice err small" style={{ marginTop: 10 }}>
              {preview.blockers.join(" ")}
            </div>
          )}
          {(preview.counts.already_corrected ?? 0) > 0 && (
            <label className="row" style={{ gap: 8, marginTop: 10, alignItems: "center" }}>
              <input type="checkbox" checked={recheck}
                onChange={(e) => setRecheck(e.target.checked)} />
              <span className="small">
                Also recheck the {preview.counts.already_corrected} already-corrected
                submission(s) — the previous result is kept as a version, never overwritten.
              </span>
            </label>
          )}
          <div className="notice info small" style={{ marginTop: 10 }}>
            Correcting never publishes to Google Classroom. Already-corrected students are
            skipped unless you tick recheck above.
          </div>
        </Modal>
      )}
    </>
  );
}

function selectedIds(queue: PublishQueue, selected: Set<string>): Set<string> {
  if (selected.size > 0) return selected;
  return new Set(queue.items.filter((i) => !i.review_required).map((i) => i.submission_id));
}

/* ----------------------------------------------------------- submission row */

function SubmissionRow({ row, checked, onCheck, onOpenStudent }: {
  row: TrackingRow; checked: boolean; onCheck: (on: boolean) => void; onOpenStudent: () => void;
}) {
  const canCorrect = row.attachments.length > 0
    && !["NOT_STARTED", "CREATED", "MISSING", "RECLAIMED", "INVALID", "UNKNOWN"].includes(row.status);
  const tone = row.status === "CORRECTED" || row.status === "GRADED" ? "ok"
    : row.status === "FAILED" || row.status === "INVALID" ? "err"
    : row.status === "SUBMITTED" || row.status === "SUBMITTED_LATE" || row.status === "PROCESSING" ? "info"
    : "mute";
  return (
    <tr>
      <td data-label="Select">
        <input type="checkbox" checked={checked} disabled={!canCorrect}
          aria-label={`Select ${displayName(row)}`}
          title={canCorrect ? "Include in the next correction run" : "Nothing submitted to correct"}
          onChange={(e) => onCheck(e.target.checked)} />
      </td>
      <td data-label="Student">
        <a className="student-link" href="#" onClick={(e) => { e.preventDefault(); onOpenStudent(); }}>
          {displayName(row)}
        </a>
        {row.late && <span className="badge warn" style={{ marginLeft: 6, fontSize: 10 }}>late</span>}
      </td>
      <td data-label="Status">
        <Badge tone={tone as "ok" | "err" | "info" | "mute"}>{row.status_label}</Badge>
      </td>
      <td data-label="Submitted" className="small faint">{timeAgo(row.state_changed_at ?? row.last_synced_at)}</td>
      <td data-label="Mark" className="num">
        {row.result ? `${row.result.earned}/${row.result.available}` : "—"}
        {row.assigned_grade != null && (
          <div className="faint small">Classroom {row.assigned_grade}</div>
        )}
      </td>
      <td data-label="Classroom" className="small">
        {row.assigned_grade != null ? <Badge tone="ok">graded</Badge>
          : row.draft_grade != null ? <Badge tone="info">draft</Badge>
          : <span className="faint">—</span>}
      </td>
      <td data-label="Corrected" className="small faint">
        {row.result ? "yes" : row.job ? row.job.status.toLowerCase() : "no"}
      </td>
    </tr>
  );
}

/* --------------------------------------------------------- publishing panel */

function PublishPanel({ queue, managed, mode, setMode, selected, setSelected, onPublish, onAttemptUnmanaged, result }: {
  queue: PublishQueue; managed: boolean; mode: "draft" | "final";
  setMode: (m: "draft" | "final") => void;
  selected: Set<string>; setSelected: (s: Set<string>) => void;
  onPublish: () => void; onAttemptUnmanaged: () => void; result: PublishResult | null;
}) {
  const items = queue.items;
  const readyIds = items.filter((i) => !i.review_required && i.suggested_grade != null)
    .map((i) => i.submission_id);

  function toggle(sid: string, on: boolean) {
    const next = new Set(selected.size ? selected : readyIds);
    if (on) next.add(sid); else next.delete(sid);
    setSelected(next);
  }

  return (
    <div className="card">
      <div className="row between wrap" style={{ marginBottom: 12 }}>
        <h3 style={{ margin: 0 }}>
          <Icon name="send" size={16} style={{ color: "var(--accent)" }} />
          Publishing queue
        </h3>
        <div className="row wrap">
          <Segmented value={mode} onChange={setMode} ariaLabel="Grade type"
            options={[{ value: "draft", label: "Draft grade" }, { value: "final", label: "Final grade" }]} />
          {managed
            ? <Button variant="primary" icon="send" onClick={onPublish}
                disabled={readyIds.length === 0}>Publish grades</Button>
            : <Button variant="warning" icon="send" onClick={onAttemptUnmanaged}
                disabled={readyIds.length === 0}>Attempt external publish</Button>}
        </div>
      </div>

      <div className="grid grid-3" style={{ marginBottom: 12 }}>
        <div className="stat compact"><div className="label">Grades ready</div>
          <div className="value ok">{queue.summary.grades_ready}</div></div>
        <div className="stat compact"><div className="label">Require review</div>
          <div className="value warn">{queue.summary.needs_review}</div></div>
        <div className="stat compact"><div className="label">Already graded</div>
          <div className="value">{queue.summary.already_graded}</div></div>
      </div>

      {!queue.capabilities.final_grade && (
        <div className="notice warn small" style={{ marginBottom: 12 }}>
          <Icon name="lock" size={15} />
          <span>{queue.capabilities.reason}</span>
        </div>
      )}

      {items.length === 0 ? (
        <Empty icon="inbox" title="Nothing to publish yet"
          hint="Correct submissions first; publishable grades will appear here." />
      ) : (
        <DataTable head={
          <tr>
            <th style={{ width: 32 }} />
            <th>Student</th><th className="num">Mark</th>
            <th className="num">Suggested</th><th>State</th>
          </tr>
        }>
          {items.map((i) => {
            const isSel = selected.size ? selected.has(i.submission_id) : !i.review_required;
            return (
              <tr key={i.submission_id}>
                <td data-label="Select">
                  <input type="checkbox" checked={isSel} disabled={i.review_required}
                    aria-label={`Select ${displayName(i)}`}
                    onChange={(e) => toggle(i.submission_id, e.target.checked)} />
                </td>
                <td data-label="Student">
                  {displayName(i)}
                  {i.review_required && <Badge tone="warn" icon="review">review</Badge>}
                </td>
                <td data-label="Mark" className="num">
                  {i.earned}/{i.available} <span className="faint small">({Math.round(i.percentage)}%)</span>
                </td>
                <td data-label="Suggested" className="num">
                  {i.suggested_grade != null ? `${i.suggested_grade}` : "—"}
                  {i.max_points != null && <span className="faint small"> / {i.max_points}</span>}
                </td>
                <td data-label="State" className="small">
                  {i.assigned_grade != null ? <Badge tone="ok">graded {i.assigned_grade}</Badge>
                    : i.draft_grade != null ? <Badge tone="info">draft {i.draft_grade}</Badge>
                    : <span className="faint">not published</span>}
                </td>
              </tr>
            );
          })}
        </DataTable>
      )}

      {result && (
        <div style={{ marginTop: 16 }}>
          <h3>Last publish result</h3>
          <div className="row wrap" style={{ marginBottom: 8 }}>
            <Badge tone="ok" icon="check">Published {result.summary.published}</Badge>
            {result.summary.failed > 0 && <Badge tone="err" icon="alert">Failed {result.summary.failed}</Badge>}
            {result.summary.not_supported > 0 && <Badge tone="warn" icon="lock">Not supported {result.summary.not_supported}</Badge>}
            {result.summary.requires_auth > 0 && <Badge tone="warn" icon="key">Auth required {result.summary.requires_auth}</Badge>}
            {result.summary.requires_review ? <Badge tone="warn" icon="review">Review {result.summary.requires_review}</Badge> : null}
          </div>
          <div className="stack">
            {result.results.map((r, idx) => (
              <div key={idx} className={`notice ${r.status === "published" ? "ok"
                : r.status === "not_supported" || r.status === "requires_review" ? "warn" : "err"}`}>
                <Icon name={r.status === "published" ? "check-circle"
                  : r.status === "not_supported" ? "lock" : "alert"} size={16} />
                <span>
                  <strong>{r.status.replace(/_/g, " ")}</strong>
                  {r.submission_id ? ` · ${submissionLabel(queue, r.submission_id)}` : ""}
                  {r.message ? ` — ${r.message}` : ""}
                  {r.http_status ? <span className="faint"> (HTTP {r.http_status})</span> : null}
                </span>
              </div>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}

function submissionLabel(queue: PublishQueue, sid: string): string {
  const item = queue.items.find((i) => i.submission_id === sid);
  return item ? displayName(item) : sid.slice(0, 8);
}

/* ----------------------------------------------------------- create wizard */

function CreateWizard({ classes, onClose, onCreated }: {
  classes: ClassOverview[]; onClose: () => void; onCreated: (id: string) => void;
}) {
  const toast = useToast();
  const [step, setStep] = useState(0);
  const [title, setTitle] = useState("");
  const [courseId, setCourseId] = useState(classes.find((c) => !c.local)?.id ?? "");
  const [description, setDescription] = useState("");
  const [dueDate, setDueDate] = useState("");
  const [dueTime, setDueTime] = useState("");
  const [maxPoints, setMaxPoints] = useState("20");
  const [reviewMode, setReviewMode] = useState<"always" | "flagged">("flagged");
  const [mode, setMode] = useState<"local" | "classroom">("classroom");
  const [scheme, setScheme] = useState<File | null>(null);
  const [busy, setBusy] = useState(false);

  const steps = ["Basics", "Files", "Correction", "Google Classroom"];

  async function create() {
    setBusy(true);
    try {
      let id: string;
      if (mode === "local") {
        const r = await api<{ id: string }>("/assignments/local", {
          method: "POST", body: JSON.stringify({
            title, description, max_points: maxPoints, due_date: dueDate || undefined }) });
        id = r.id;
      } else {
        if (!courseId) throw new Error("Choose a Google Classroom class first.");
        const r = await api<{ coursework: { id: string } }>(
          `/classroom/courses/${courseId}/assignments/create`, {
            method: "POST", body: JSON.stringify({
              title, description, max_points: maxPoints,
              due_date: dueDate || undefined, due_time: dueTime || undefined }) });
        id = r.coursework.id;
      }
      if (scheme && mode === "classroom") {
        const body = new FormData();
        body.append("file", scheme);
        try {
          await postForm(`/classroom/courses/${courseId}/assignments/${id}/mark-scheme`, body);
        } catch {
          toast("warn", "Assignment created, but the mark scheme upload failed.");
        }
      }
      onCreated(id);
    } catch (e) {
      toast("err", (e as Error).message);
    } finally { setBusy(false); }
  }

  const canNext = step === 0 ? title.trim().length > 0 : true;

  return (
    <Modal title="Create assignment" icon="plus" wide onClose={onClose}
      footer={
        <>
          {step > 0 && <Button variant="ghost" icon="arrow-left" onClick={() => setStep(step - 1)}>Back</Button>}
          <span className="spacer" />
          <Button variant="ghost" onClick={onClose}>Cancel</Button>
          {step < steps.length - 1
            ? <Button variant="primary" iconRight="arrow-right" disabled={!canNext}
                onClick={() => setStep(step + 1)}>Continue</Button>
            : <Button variant="primary" icon="check" loading={busy} onClick={create}>Create assignment</Button>}
        </>
      }>
      <div className="tabs" aria-label="Wizard steps">
        {steps.map((s, i) => (
          <button key={s} type="button" role="tab" aria-selected={i === step}
            className={`tab ${i === step ? "active" : ""}`}
            onClick={() => i <= step && setStep(i)}>
            <span className="badge mute" style={{ marginRight: 2 }}>{i + 1}</span>{s}
          </button>
        ))}
      </div>

      {step === 0 && (
        <>
          <label className="field"><span>Assignment name</span>
            <input className="input" value={title} autoFocus
              onChange={(e) => setTitle(e.target.value)} placeholder="e.g. Algebra — Quadratics HW" /></label>
          <label className="field"><span>Class</span>
            <select className="input" value={courseId} onChange={(e) => setCourseId(e.target.value)}>
              <option value="">Choose a class…</option>
              {classes.filter((c) => !c.local).map((c) => (
                <option key={c.id} value={c.id}>{c.name}{c.section ? ` · ${c.section}` : ""}</option>
              ))}
            </select></label>
          <label className="field"><span>Description (optional)</span>
            <textarea className="input" rows={3} value={description}
              onChange={(e) => setDescription(e.target.value)} /></label>
          <div className="grid grid-2">
            <label className="field"><span>Due date</span>
              <input className="input" type="date" value={dueDate}
                onChange={(e) => setDueDate(e.target.value)} /></label>
            <label className="field"><span>Due time (UTC)</span>
              <input className="input" type="time" value={dueTime}
                onChange={(e) => setDueTime(e.target.value)} /></label>
          </div>
        </>
      )}

      {step === 1 && (
        <>
          <div className="notice info small" style={{ marginBottom: 12 }}>
            <Icon name="file" size={15} />
            <span>The mark scheme is required before corrections can run. You can also add it later from the assignment page.</span>
          </div>
          <label className="field"><span>Mark scheme (PDF)</span>
            <input className="input" type="file" accept="application/pdf"
              onChange={(e) => setScheme(e.target.files?.[0] ?? null)} /></label>
          <p className="faint small">
            Question papers and per-student work come from Google Classroom submissions.
          </p>
        </>
      )}

      {step === 2 && (
        <>
          <label className="field"><span>Total marks</span>
            <input className="input" type="number" min={1} value={maxPoints}
              onChange={(e) => setMaxPoints(e.target.value)} /></label>
          <label className="field"><span>Review requirement</span>
            <div className="notice small" style={{ marginTop: 0 }}>
              Current behaviour: {reviewMode === "flagged"
                ? "only suspicious results are flagged for review"
                : "every result is flagged for review"} before grades can be published.
            </div>
            <div className="chip-row" style={{ marginTop: 8 }}>
              <button type="button" className={`chip ${reviewMode === "flagged" ? "active" : ""}`}
                onClick={() => setReviewMode("flagged")}>Flag suspicious only</button>
              <button type="button" className={`chip ${reviewMode === "always" ? "active" : ""}`}
                onClick={() => setReviewMode("always")}>Always review</button>
            </div></label>
          <div className="notice info small">
            <Icon name="shield" size={15} />
            <span>The deterministic result guard is always active: a valid mark is never silently
              replaced, and missing questions become “needs review”, never zero.</span>
          </div>
        </>
      )}

      {step === 3 && (
        <>
          <div className="grid grid-2">
            <button type="button"
              className={`status-card clickable ${mode === "classroom" ? "tone-info" : ""}`}
              onClick={() => setMode("classroom")}>
              <div className="sc-icon"><Icon name="classroom" size={20} /></div>
              <div>
                <div className="sc-label">Create &amp; sync to Classroom</div>
                <div className="sc-sub">Managed — Math Checker can publish grades back.</div>
              </div>
              {mode === "classroom" && <Icon name="check-circle" size={18} style={{ color: "var(--accent)" }} />}
            </button>
            <button type="button"
              className={`status-card clickable ${mode === "local" ? "tone-info" : ""}`}
              onClick={() => setMode("local")}>
              <div className="sc-icon"><Icon name="database" size={20} /></div>
              <div>
                <div className="sc-label">Create in Math Checker only</div>
                <div className="sc-sub">Local — no Classroom publishing.</div>
              </div>
              {mode === "local" && <Icon name="check-circle" size={18} style={{ color: "var(--accent)" }} />}
            </button>
          </div>

          <div className="card" style={{ marginTop: 16, background: "var(--surface)" }}>
            <h3>Preview</h3>
            <div className="kv"><span className="k">Title</span><span className="v">{title || "—"}</span></div>
            <div className="kv"><span className="k">Class</span>
              <span className="v">{mode === "local" ? "Local assignments"
                : classes.find((c) => c.id === courseId)?.name ?? "—"}</span></div>
            <div className="kv"><span className="k">Type</span>
              <span className="v">{mode === "classroom" ? "Managed by Math Checker" : "External/Local only"}</span></div>
            <div className="kv"><span className="k">Due</span>
              <span className="v">{dueDate || "—"}{dueTime ? ` ${dueTime}` : ""}</span></div>
            <div className="kv"><span className="k">Marks</span><span className="v">{maxPoints || "—"}</span></div>
            <div className="kv"><span className="k">Mark scheme</span>
              <span className="v">{scheme ? scheme.name : "upload later"}</span></div>
          </div>
        </>
      )}
    </Modal>
  );
}
