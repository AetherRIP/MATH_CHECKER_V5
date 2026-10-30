import { useCallback, useEffect, useMemo, useState } from "react";
import { api } from "../api";
import {
  Badge, Button, DataTable, Empty, Field, Modal, PageHeader, Stat, useToast,
  type Tone,
} from "../components";
import { friendlyError } from "../errors";

/**
 * Teacher-verified marking accuracy.
 *
 * Two rules drive this page:
 *
 *  1. An unverified dataset has NO accuracy figure. We show "unavailable",
 *     never 0%, because 0% reads as a measured failure.
 *  2. The AI mark and the teacher mark are visually separate. Confirming a
 *     reference is a deliberate act, with an explicit checkbox, and it writes
 *     only to the benchmark overlay — never to a student's result.
 */

interface Counts {
  exact: number;
  compared: number;
  rate: number | null;
  ci95: number[] | null;
  denominator?: string;
}

interface Disagreement {
  reference_id: string;
  question: string;
  ai_mark: number;
  teacher_mark: number;
  max_marks: number;
  difference: number;
  direction: string;
  error_category: string;
  hypothesis: string;
  basis?: string;
  scheme?: string[];
  paper_title?: string;
}

interface RunSummary {
  run_id: string;
  status: string;
  certified: boolean;
  certification?: { label?: string; reason?: string };
  started_at?: string;
  finished_at?: string;
  mode?: string;
  model?: string;
  dataset_version?: string;
  dataset_matches: boolean;
  sample?: {
    references_expected?: number;
    papers_processed?: number;
    marks_available?: number;
    papers_expected?: number;
    certification_basis?: string;
  };
  agreement?: {
    items?: Counts; questions?: Counts; subquestions?: Counts; total?: Counts;
  };
  error?: {
    mean_absolute_error?: number | null;
    normalized_mae?: number | null;
    overmarking?: { count: number; rate: number | null; mean_magnitude: number | null };
    undermarking?: { count: number; rate: number | null; mean_magnitude: number | null };
  };
  coverage?: {
    missing_mark_count?: number;
    invalid_result_count?: number;
    scripts_failed?: number;
    question_coverage?: number | null;
    subquestion_coverage?: number | null;
    unresolved_reference_count?: number;
  };
  review?: {
    review_flag_rate?: number | null;
    flagged_with_disagreement?: number;
    flagged_without_disagreement?: number;
    human_review_rate?: number | null;
    items_requiring_investigation?: number;
  };
  grades?: { available: boolean; rate: number | null; compared: number };
  disagreements?: Disagreement[];
  disagreement_groups?: Record<string, number>;
  limitations?: string[];
  reproduction?: { reproduced: boolean; differences?: string[] };
  artifacts?: Record<string, string>;
}

interface Overview {
  dataset: {
    version: string;
    sha256: string;
    paper_count: number;
    item_count: number;
    question_count: number;
    subquestion_count: number;
    marks_available: number;
    partitions: Record<string, number>;
    provenance: Record<string, number>;
    warnings: string[];
    papers: { id: string; title: string; partition: string; items: number }[];
  };
  verification: {
    total_references: number;
    eligible: number;
    eligible_questions: number;
    eligible_subquestions: number;
    eligible_marks: number;
    counts: Record<string, number>;
    unresolved: { reference_id: string; status: string; reason: string }[];
  };
  certified_available: boolean;
  certified_accuracy: Record<string, unknown> | null;
  certification_reason: string | null;
  latest_run: RunSummary | null;
}

interface Assessment {
  seq: number;
  awarded: number | null;
  status: string;
  assessor: string;
  role: string;
  note: string;
  alternative_methods: string[];
  created_at: string;
}

interface ReferenceRow {
  reference_id: string;
  paper_id: string;
  paper_title: string;
  question: string;
  subquestion: string | null;
  level: string;
  max_marks: number;
  scheme_mark: number;
  prompt: string;
  scheme: string[];
  student_answer: string[];
  categories: string[];
  ruling: string;
  basis: string;
  partition: string;
  reference: {
    status: string;
    teacher_mark: number | null;
    reason: string;
    eligible: boolean;
    assessments: Assessment[];
  };
  ai: {
    run_id?: string;
    mark: number | null;
    max: number | null;
    difference: number | null;
    direction: string | null;
    message?: string;
    review_required?: boolean;
  } | null;
}

const STATUS_TONE: Record<string, Tone> = {
  verified: "ok",
  disputed: "err",
  incomplete: "warn",
  unverified: "mute",
};

function pct(value: number | null | undefined): string {
  return value === null || value === undefined ? "—" : `${(value * 100).toFixed(1)}%`;
}

function ci(interval: number[] | null | undefined): string {
  return interval
    ? `${(interval[0] * 100).toFixed(1)}–${(interval[1] * 100).toFixed(1)}%`
    : "—";
}

function exact(part: Counts | undefined): string {
  if (!part) return "—";
  return `${part.exact}/${part.compared}`;
}

function Metric({ label, value, sub }: {
  label: string; value: string; sub?: string;
}) {
  return <Stat label={label} value={<>{value}{sub && (
    <span className="faint small" style={{ display: "block", fontWeight: 400 }}>
      {sub}
    </span>
  )}</>} />;
}

export default function AccuracyPage() {
  const toast = useToast();
  const [overview, setOverview] = useState<Overview | null>(null);
  const [references, setReferences] = useState<ReferenceRow[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [filter, setFilter] = useState("all");
  const [selected, setSelected] = useState<ReferenceRow | null>(null);
  const [history, setHistory] = useState<Assessment[]>([]);
  const [awarded, setAwarded] = useState("");
  const [status, setStatus] = useState("verified");
  const [assessor, setAssessor] = useState("");
  const [role, setRole] = useState("maths teacher");
  const [note, setNote] = useState("");
  const [alternatives, setAlternatives] = useState("");
  const [confirmed, setConfirmed] = useState(false);
  const [busy, setBusy] = useState(false);
  const [reproducing, setReproducing] = useState(false);

  const load = useCallback(() => {
    setError(null);
    Promise.all([
      api<Overview>("/benchmark/overview"),
      api<{ items: ReferenceRow[] }>("/benchmark/references?limit=1000"),
    ])
      .then(([nextOverview, nextRefs]) => {
        setOverview(nextOverview);
        setReferences(nextRefs.items);
      })
      .catch((e) => setError(friendlyError(e).summary));
  }, []);

  useEffect(() => { load(); }, [load]);

  const run = overview?.latest_run ?? null;
  const visible = useMemo(() => {
    if (filter === "all") return references;
    return references.filter((row) => row.reference.status === filter);
  }, [references, filter]);

  const coverage = overview
    ? (overview.verification.eligible / Math.max(1, overview.verification.total_references))
    : 0;

  function openReview(row: ReferenceRow) {
    setSelected(row);
    setAwarded(row.reference.teacher_mark != null
      ? String(row.reference.teacher_mark)
      : String(row.scheme_mark));
    setStatus(row.reference.status === "unverified" ? "verified" : row.reference.status);
    setAssessor("");
    setRole("maths teacher");
    setNote("");
    setAlternatives((row.reference.assessments[0]?.alternative_methods ?? []).join("; "));
    setConfirmed(false);
    setHistory([]);
    api<{ history: Assessment[] }>(
      `/benchmark/references/${encodeURIComponent(row.reference_id)}`)
      .then((r) => setHistory(r.history ?? []))
      .catch(() => {});
  }

  async function submitAssessment() {
    if (!selected) return;
    setBusy(true);
    try {
      const result = await api<{
        reference: ReferenceRow["reference"];
        verification: Overview["verification"];
      }>(`/benchmark/references/${encodeURIComponent(selected.reference_id)}/verify`, {
        method: "POST",
        body: JSON.stringify({
          awarded: awarded.trim() === "" ? null : Number(awarded),
          status,
          assessor,
          role,
          note,
          alternative_methods: alternatives.split(";").map((s) => s.trim()).filter(Boolean),
          confirm: confirmed,
        }),
      });
      toast("ok", `Recorded ${result.reference.status} assessment for ${selected.reference_id}`);
      setSelected(null);
      load();
    } catch (e) {
      toast("err", friendlyError(e).summary);
    } finally {
      setBusy(false);
    }
  }

  async function checkReproduction() {
    if (!run) return;
    setReproducing(true);
    try {
      const result = await api<{ reproduced: boolean; differences: string[] }>(
        `/benchmark/runs/${encodeURIComponent(run.run_id)}/reproduce`,
        { method: "POST" });
      if (result.reproduced) toast("ok", "Metrics reproduce exactly from the recorded inputs");
      else toast("warn", `Reproduction differs in: ${result.differences.join(", ")}`);
      load();
    } catch (e) {
      toast("err", friendlyError(e).summary);
    } finally {
      setReproducing(false);
    }
  }

  if (!overview) {
    return (
      <>
        <PageHeader kicker="Review · Accuracy benchmark"
          title="Teacher-verified marking accuracy"
          sub="Loading the reference set and recorded runs…" />
        {error ? <Empty icon="alert" title="Could not load the benchmark" hint={error} /> :
          <div className="skeleton" style={{ height: 240 }} />}
      </>
    );
  }

  const verified = overview.verification.eligible;
  const total = overview.verification.total_references;

  return (
    <>
      <PageHeader
        kicker="Review · Accuracy benchmark"
        title="Teacher-verified marking accuracy"
        sub="How often the marker agrees with a qualified teacher, how large its mistakes are, and where they occur. Only explicitly confirmed teacher marks can produce a certified score."
        meta={
          <>
            <span>{total} reference items</span>
            <span aria-hidden="true">·</span>
            <span>{verified} teacher-verified</span>
            <span aria-hidden="true">·</span>
            <span>dataset v{overview.dataset.version}</span>
          </>
        }
      >
        <Button variant="secondary" icon="refresh" onClick={load}>Refresh</Button>
      </PageHeader>

      {error && <div className="card" style={{ borderColor: "var(--err-line)" }}>
        <strong>Could not refresh:</strong> {error}
      </div>}

      <section className="card">
        {overview.certified_available ? (
          <>
            <div className="row between wrap">
              <div>
                <Badge tone="ok" icon="check-circle">Teacher-Verified Benchmark Accuracy</Badge>
                <div style={{ fontSize: 42, fontWeight: 700, marginTop: 10 }}>
                  {pct(run?.agreement?.items?.rate)}
                </div>
                <div className="muted">
                  exact item agreement · {exact(run?.agreement?.items)} matched items ·
                  95% CI {ci(run?.agreement?.items?.ci95)}
                </div>
              </div>
              <div className="muted small" style={{ maxWidth: 360 }}>
                <div>{run?.sample?.papers_processed} script(s), {run?.sample?.references_expected} references</div>
                <div>{run?.sample?.marks_available} marks represented</div>
                <div>Model: {run?.model || "not recorded"}</div>
                <div>Run: {run?.finished_at || run?.started_at}</div>
              </div>
            </div>
            <p className="small muted" style={{ marginBottom: 0 }}>
              This result describes the verified dataset above, not every future
              student paper. Confidence intervals widen quickly at small sample sizes.
            </p>
          </>
        ) : (
          <>
            <div className="row between wrap">
              <div>
                <Badge tone="warn" icon="alert">Certified accuracy unavailable</Badge>
                <div style={{ fontSize: 30, fontWeight: 700, marginTop: 10 }}>
                  Not 0% — not yet measured
                </div>
                <p className="muted" style={{ maxWidth: 680, marginBottom: 0 }}>
                  {overview.certification_reason}
                </p>
              </div>
              <div style={{ minWidth: 220 }}>
                <div className="label">Teacher verification</div>
                <div style={{ fontSize: 28, fontWeight: 700 }}>{verified}/{total}</div>
                <div className="progress" style={{ height: 8, marginTop: 6 }}
                  role="progressbar" aria-valuenow={Math.round(coverage * 100)}
                  aria-valuemin={0} aria-valuemax={100}>
                  <div className="bar" style={{ width: `${coverage * 100}%` }} />
                </div>
              </div>
            </div>
          </>
        )}
      </section>

      {run && (
        <section className="card">
          <div className="row between wrap" style={{ marginBottom: 10 }}>
            <div>
              <h3 style={{ margin: 0 }}>
                {run.certified ? "Certified run" : "Latest measurement — NOT CERTIFIED"}
              </h3>
              <div className="muted small">
                {run.run_id} · status {run.status} · {run.mode}
                {run.dataset_matches ? "" : " · dataset has changed since this run"}
              </div>
            </div>
            <div className="row wrap">
              <Button size="sm" variant="secondary" icon="refresh"
                loading={reproducing} onClick={checkReproduction}>
                Recheck reproducibility
              </Button>
              <Button size="sm" variant="ghost" icon="reports"
                onClick={() => window.open(`/api/benchmark/runs/${encodeURIComponent(run.run_id)}/report`, "_blank")}>
                Open report
              </Button>
            </div>
          </div>
          {run.sample?.certification_basis === "scheme_reference_not_certified" && (
            <div className="testing-banner" style={{ marginBottom: 12 }}>
              These marks were derived from the printed scheme but are not
              teacher-confirmed. Read them as a measurement against the scheme,
              not as the certified score.
            </div>
          )}
          <div className="stat-row">
            <Metric label="Exact item agreement" value={pct(run.agreement?.items?.rate)}
              sub={`${exact(run.agreement?.items)} matched`} />
            <Metric label="Question level" value={pct(run.agreement?.questions?.rate)}
              sub={`${exact(run.agreement?.questions)} matched`} />
            <Metric label="Subquestion level" value={pct(run.agreement?.subquestions?.rate)}
              sub={`${exact(run.agreement?.subquestions)} matched`} />
            <Metric label="Exact total" value={pct(run.agreement?.total?.rate)}
              sub={`${exact(run.agreement?.total)} scripts`} />
            <Metric label="Mean absolute error" value={
              run.error?.mean_absolute_error == null ? "—" : String(run.error.mean_absolute_error)}
              sub="marks per matched item" />
            <Metric label="Normalised MAE" value={pct(run.error?.normalized_mae)}
              sub="fraction of available marks" />
            <Metric label="Overmarked" value={pct(run.error?.overmarking?.rate)}
              sub={`${run.error?.overmarking?.count ?? 0} items`} />
            <Metric label="Undermarked" value={pct(run.error?.undermarking?.rate)}
              sub={`${run.error?.undermarking?.count ?? 0} items`} />
            <Metric label="Missing marks" value={String(run.coverage?.missing_mark_count ?? 0)}
              sub="reported, not dropped" />
            <Metric label="Review flags" value={pct(run.review?.review_flag_rate)}
              sub={`${run.review?.flagged_with_disagreement ?? 0} flagged + wrong`} />
            <Metric label="Items to investigate"
              value={String(run.review?.items_requiring_investigation ?? 0)}
              sub={pct(run.review?.human_review_rate)} />
            <Metric label="Grade agreement" value={
              run.grades?.available ? pct(run.grades.rate) : "unavailable"}
              sub={`${run.grades?.compared ?? 0} verified grades`} />
          </div>
          <div className="row wrap small muted" style={{ marginTop: 10 }}>
            <span>Reproduction: {run.reproduction?.reproduced ? "PASS" : "NOT VERIFIED"}</span>
            <span>·</span>
            <span>Dataset v{run.dataset_version}</span>
            <span>·</span>
            <span>{run.model || "model not recorded"}</span>
          </div>
        </section>
      )}

      <section className="card">
        <div className="row between wrap" style={{ marginBottom: 10 }}>
          <div>
            <h3 style={{ margin: 0 }}>Reference verification</h3>
            <div className="muted small">
              Inspect the scheme and the candidate's working, then enter the mark
              you would award. Nothing here changes a student's result.
            </div>
          </div>
          <div className="row wrap">
            {["all", "unverified", "verified", "disputed", "incomplete"].map((key) => (
              <Button key={key} size="sm" variant={filter === key ? "primary" : "ghost"}
                onClick={() => setFilter(key)}>
                {key === "all" ? "All" : key}
                <span className="faint small">
                  {key === "all" ? ` ${references.length}`
                    : ` ${references.filter((r) => r.reference.status === key).length}`}
                </span>
              </Button>
            ))}
          </div>
        </div>
        {visible.length === 0 ? (
          <Empty icon="review" title="No reference items in this filter"
            hint="Choose another status to see the rest of the dataset." />
        ) : (
          <DataTable minWidth={760} head={
            <tr>
              <th>Reference</th><th>Q</th><th>Max</th><th>Scheme mark</th>
              <th>AI mark</th><th>Teacher mark</th><th>Status</th><th />
            </tr>
          }>
            {visible.map((row) => (
              <tr key={row.reference_id}>
                <td><span className="mono small">{row.reference_id}</span></td>
                <td>{row.question}</td>
                <td>{row.max_marks}</td>
                <td>{row.scheme_mark}</td>
                <td>{row.ai?.mark == null ? <span className="faint">—</span> :
                  <span title={row.ai.message ?? undefined}>
                    {row.ai.mark}/{row.ai.max ?? row.max_marks}
                    {row.ai.difference ? ` (${row.ai.difference > 0 ? "+" : ""}${row.ai.difference})` : ""}
                  </span>}</td>
                <td>{row.reference.teacher_mark == null ? <span className="faint">—</span> :
                  row.reference.teacher_mark}</td>
                <td><Badge tone={STATUS_TONE[row.reference.status] ?? "mute"}>
                  {row.reference.status}
                </Badge></td>
                <td style={{ textAlign: "right" }}>
                  <Button size="sm" variant="secondary" icon="review"
                    onClick={() => openReview(row)}>
                    {row.reference.status === "unverified" ? "Verify" : "Review"}
                  </Button>
                </td>
              </tr>
            ))}
          </DataTable>
        )}
      </section>

      {run && (run.disagreements?.length ?? 0) > 0 && (
        <section className="card">
          <h3 style={{ marginTop: 0 }}>Disagreements behind the numbers</h3>
          <p className="muted small">
            Error categories are hypotheses from the reference tags. A numerical
            mismatch alone does not establish a root cause; every row needs human
            investigation.
          </p>
          <DataTable minWidth={820} head={
            <tr>
              <th>Reference</th><th>Q</th><th>AI</th><th>Teacher</th>
              <th>Diff</th><th>Direction</th><th>Hypothesis</th>
            </tr>
          }>
            {(run.disagreements ?? []).map((row) => (
              <tr key={row.reference_id}>
                <td><span className="mono small">{row.reference_id}</span></td>
                <td>{row.question}</td>
                <td>{row.ai_mark}/{row.max_marks}</td>
                <td>{row.teacher_mark}</td>
                <td>{row.difference > 0 ? "+" : ""}{row.difference}</td>
                <td>{row.direction}</td>
                <td className="small">{row.error_category}</td>
              </tr>
            ))}
          </DataTable>
        </section>
      )}

      {(run?.limitations?.length ?? 0) > 0 && (
        <section className="card">
          <h3 style={{ marginTop: 0 }}>Limitations of this result</h3>
          <ul className="small" style={{ marginBottom: 0 }}>
            {(run?.limitations ?? []).map((line) => <li key={line}>{line}</li>)}
          </ul>
        </section>
      )}

      <section className="card">
        <h3 style={{ marginTop: 0 }}>Dataset composition</h3>
        <div className="kv small">
          <span className="k">Papers</span>
          <span className="v">{overview.dataset.paper_count} · {overview.dataset.partitions.development ?? 0} development,
            {" "}{overview.dataset.partitions.holdout ?? 0} holdout items</span>
          <span className="k">References</span>
          <span className="v">{overview.dataset.question_count} question-level,
            {" "}{overview.dataset.subquestion_count} subquestion-level</span>
          <span className="k">Marks represented</span>
          <span className="v">{overview.dataset.marks_available}</span>
          <span className="k">Provenance</span>
          <span className="v">{Object.entries(overview.dataset.provenance)
            .map(([k, v]) => `${k}: ${v}`).join(", ")}</span>
          <span className="k">Dataset SHA-256</span>
          <span className="v mono small">{overview.dataset.sha256}</span>
        </div>
        {overview.dataset.warnings.length > 0 && (
          <details style={{ marginTop: 12 }}>
            <summary className="small muted">Dataset warnings ({overview.dataset.warnings.length})</summary>
            <ul className="small">
              {overview.dataset.warnings.map((line) => <li key={line}>{line}</li>)}
            </ul>
          </details>
        )}
      </section>

      {selected && (
        <Modal wide icon="review" title={`Review ${selected.reference_id}`}
          onClose={() => setSelected(null)}
          footer={
            <>
              <Button variant="ghost" onClick={() => setSelected(null)}>Cancel</Button>
              <Button variant="primary" loading={busy}
                disabled={!confirmed || !assessor.trim()
                  || (status !== "verified" && !note.trim())}
                onClick={submitAssessment}>
                Record teacher assessment
              </Button>
            </>
          }>
          <div className="stack">
            <div className="row between wrap">
              <Badge tone={STATUS_TONE[selected.reference.status] ?? "mute"}>
                {selected.reference.status}
              </Badge>
              <span className="muted small">{selected.paper_title} · {selected.partition}</span>
            </div>
            <div>
              <div className="label">Question</div>
              <div>{selected.prompt}</div>
            </div>
            <div>
              <div className="label">Mark scheme</div>
              <ul className="small" style={{ margin: 0 }}>
                {selected.scheme.map((line) => <li key={line}>{line}</li>)}
              </ul>
            </div>
            <div>
              <div className="label">Candidate's working</div>
              <pre className="report-preview small" style={{ margin: 0 }}>
                {selected.student_answer.join("\n") || "(blank)"}
              </pre>
            </div>
            <div className="row wrap" style={{ gap: 16 }}>
              <div>
                <div className="label">AI mark</div>
                <div>{selected.ai?.mark == null ? "not returned"
                  : `${selected.ai.mark}/${selected.ai.max ?? selected.max_marks}`}</div>
              </div>
              <div>
                <div className="label">Scheme-derived mark</div>
                <div>{selected.scheme_mark}/{selected.max_marks}</div>
              </div>
              <div>
                <div className="label">Your mark</div>
                <div>{awarded === "" ? "—" : `${awarded}/${selected.max_marks}`}</div>
              </div>
            </div>
            <p className="muted small" style={{ margin: 0 }}>{selected.ruling}</p>
            <div className="row wrap" style={{ gap: 10 }}>
              <Field label="Teacher-awarded mark" hint={`0 to ${selected.max_marks}`}>
                <input className="input" type="number" min={0}
                  max={selected.max_marks} value={awarded}
                  onChange={(e) => setAwarded(e.target.value)}
                  style={{ width: 110 }} />
              </Field>
              <Field label="Assessment">
                <select className="input" value={status}
                  onChange={(e) => setStatus(e.target.value)}>
                  <option value="verified">Verified</option>
                  <option value="disputed">Disputed</option>
                  <option value="incomplete">Incomplete</option>
                </select>
              </Field>
              <Field label="Your name or initials">
                <input className="input" value={assessor}
                  onChange={(e) => setAssessor(e.target.value)}
                  placeholder="e.g. R. A." />
              </Field>
              <Field label="Role">
                <input className="input" value={role}
                  onChange={(e) => setRole(e.target.value)} />
              </Field>
            </div>
            <Field label="Accepted alternative methods"
              hint="Optional. Separate several with a semicolon.">
              <input className="input" value={alternatives}
                onChange={(e) => setAlternatives(e.target.value)} />
            </Field>
            <Field label="Reason / note"
              hint="Required context for a disputed or incomplete assessment.">
              <textarea className="input" rows={3} value={note}
                onChange={(e) => setNote(e.target.value)} />
            </Field>
            <label className="row small" style={{ gap: 8, alignItems: "flex-start" }}>
              <input type="checkbox" checked={confirmed}
                onChange={(e) => setConfirmed(e.target.checked)} />
              <span>
                I confirm this mark against the scheme and the candidate's working.
                I understand this records a benchmark assessment only and does not
                change any student's result.
              </span>
            </label>
            {history.length > 0 && (
              <div>
                <div className="label">Assessment history (append-only)</div>
                <ul className="small">
                  {history.map((row) => (
                    <li key={row.seq}>
                      {row.created_at} · {row.assessor} ({row.role || "role not given"}) ·
                      {" "}{row.status} · {row.awarded == null ? "no mark" : `${row.awarded}`}
                      {row.note ? ` · ${row.note}` : ""}
                    </li>
                  ))}
                </ul>
              </div>
            )}
          </div>
        </Modal>
      )}
    </>
  );
}
