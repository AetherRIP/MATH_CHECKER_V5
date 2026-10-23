import { useCallback, useEffect, useState } from "react";
import { api } from "../api";
import { Badge, Button, Empty, PageHeader } from "../components";

/**
 * Accuracy Audit — the internal record of how the marker performed on a real
 * Google Classroom assignment, before and after the fixes.
 *
 * Everything here is READ-ONLY and comes from artefacts the audit scripts
 * produced. It never publishes, attaches or returns anything.
 */

interface RunSummary {
  kind?: string;
  file?: string;
  earned?: number | null;
  available?: number | null;
  percentage?: number | null;
  grade?: string | null;
  review_required?: number | null;
  job_status?: string | null;
  questions?: number;
  unresolved_questions?: string[];
  pages?: number;
  route?: string;
  batch_count?: number;
  recovered?: string | null;
  run_at?: number;
}

interface QuestionRow {
  question: string;
  official_max?: number | null;
  original_awarded: number;
  original_status?: string | null;
  retest_awarded: number;
  retest_status?: string | null;
  delta: number;
  changed: boolean;
  retest_evidence?: string;
  retest_reason?: string;
  review_reason?: string | null;
}

interface Defect {
  id: string;
  title: string;
  severity?: string;
  impact?: string;
  evidence?: string[];
  root_cause?: string;
  fix?: string;
  status?: string;
}

interface Summary {
  available?: boolean;
  message?: string;
  generated_at?: number;
  testing_only?: {
    banner?: string;
    active?: boolean;
    reason?: string;
    publication_blocked_by?: string[];
    isolated_correction_env?: string;
    read_only_access_kept?: string;
  };
  assignment?: Record<string, unknown>;
  inventory?: {
    submissions_found?: number;
    with_answer_files?: number;
    state_counts?: Record<string, number>;
  };
  runs?: {
    original_production?: RunSummary;
    fresh_before_fix?: RunSummary | null;
    fresh_after_fix?: RunSummary | null;
  };
  questions?: QuestionRow[];
  consistency?: {
    per_question_max_inconsistent?: Record<string, number[]>;
    available_totals?: number[];
    available_by_student?: Record<string, number>;
  };
  defects?: Defect[];
  regression?: {
    passed?: number | null;
    failed?: number | null;
    summary?: string;
    cases?: string[];
  };
  reports?: string[];
  limitations?: string[];
}

function score(run: RunSummary | undefined | null): string {
  if (!run || run.earned === null || run.earned === undefined) return "—";
  return `${run.earned}/${run.available} · ${run.percentage ?? "?"}% · grade ${
    run.grade ?? "?"
  }`;
}

// ----------------------------------------------------------- repair dashboard

interface RepairDiffRow {
  question: string;
  max_marks: number | null;
  original_awarded: number | null;
  repaired_awarded: number | null;
  original_status?: string;
  repaired_status?: string;
  change: string;
  validation: string;
  reason: string;
  evidence?: string;
  criteria?: string;
}

interface RepairItem {
  id: string;
  run_id: string;
  student_name: string;
  mode: string;
  status: string;
  original_earned: number | null;
  original_available: number | null;
  repaired_earned: number | null;
  repaired_available: number | null;
  repaired_grade: string | null;
  score_basis: string | null;
  questions_total: number;
  questions_added: number;
  questions_changed: number;
  questions_flagged: number;
  unresolved_count: number;
  diff: RepairDiffRow[];
  validation?: { ok?: boolean; checks?: Record<string, boolean>; unresolved_questions?: string[] };
  audit_findings?: { code: string; severity: string; question: string | null; detail: string }[];
  report_filename: string | null;
  error: string | null;
}

interface RepairSummary {
  banner: string;
  assignment: { coursework_id: string; title?: string };
  scheme: { questions: number; total_marks: number } | null;
  inventory: {
    counts: Record<string, number>;
    submissions: {
      student_name: string;
      state: string;
      submitted: boolean;
      has_correction: boolean;
      accessible: boolean;
      note: string;
      stored_earned: number | null;
      stored_available: number | null;
    }[];
  };
  audits: {
    result_id: string;
    student_name: string;
    earned: number;
    available: number;
    percentage: number;
    review_required: number;
    findings: { code: string; severity: string; question: string | null; detail: string }[];
  }[];
  repair: {
    runs: { id: string; status: string; started_at: number }[];
    items: RepairItem[];
    counts: Record<string, number>;
  };
}

const CHANGE_TONE: Record<string, "ok" | "warn" | "err" | "info"> = {
  MATCH: "ok",
  ADDED: "warn",
  CHANGED: "err",
  UNRESOLVED: "err",
  DETERMINISTIC_FIX: "info",
  REMOVED: "info",
};

function RepairDashboard({
  data,
  onPreview,
}: {
  data: RepairSummary;
  onPreview: (url: string, title: string) => void;
}) {
  const c = data.inventory.counts ?? {};
  const rc = data.repair.counts ?? {};
  return (
    <section className="card">
      <h3>Assignment-wide repair — every submission, every correction</h3>
      <div className="testing-banner" role="status">
        <strong>{data.banner}</strong>
        <span>
          {" "}Repaired results live ONLY in dedicated test records — the
          original corrections are preserved untouched, and nothing here is
          published, attached or returned.
        </span>
      </div>
      <div className="stat-row">
        <div className="stat"><span className="value">{c.total ?? 0}</span><span className="label">submissions found</span></div>
        <div className="stat"><span className="value">{c.submitted ?? 0}</span><span className="label">submitted</span></div>
        <div className="stat"><span className="value">{c.corrected ?? 0}</span><span className="label">already corrected</span></div>
        <div className="stat"><span className="value">{data.repair.runs.length ? (rc.items ?? 0) : 0}</span><span className="label">audited in repair run</span></div>
        <div className="stat"><span className="value">{rc.match ?? 0}</span><span className="label">confirmed (2nd pass agrees)</span></div>
        <div className="stat"><span className="value">{rc.repaired ?? 0}</span><span className="label">repaired</span></div>
        <div className="stat"><span className="value">{rc.needs_review ?? 0}</span><span className="label">need teacher review</span></div>
        <div className="stat"><span className="value">{rc.unresolved ?? 0}</span><span className="label">unresolved</span></div>
        <div className="stat"><span className="value">{c.inaccessible ?? 0}</span><span className="label">could not be accessed</span></div>
      </div>

      {data.audits.length > 0 && (
        <>
          <h4>Stored corrections — deterministic audit</h4>
          <table className="table">
            <thead>
              <tr><th>Student</th><th>Stored score</th><th>Defects found in the stored correction</th></tr>
            </thead>
            <tbody>
              {data.audits.map((a) => (
                <tr key={a.result_id}>
                  <td>{a.student_name}</td>
                  <td>{a.earned}/{a.available} · {Number(a.percentage).toFixed(2)}%</td>
                  <td>
                    {a.findings.length === 0 ? (
                      <Badge tone="ok">no structural defects</Badge>
                    ) : (
                      a.findings.map((f, i) => (
                        <Badge key={i} tone={f.severity === "high" ? "err" : f.severity === "medium" ? "warn" : "info"}>
                          {f.code}{f.question ? ` Q${f.question}` : ""}
                        </Badge>
                      ))
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </>
      )}

      {data.repair.items.length > 0 && (
        <>
          <h4>Repaired results — original vs repaired, per question</h4>
          {data.repair.items.map((item) => {
            const changedRows = (item.diff ?? []).filter(
              (d) => d.change !== "MATCH",
            );
            return (
              <article className="defect" key={item.id}>
                <h4>
                  <Badge tone={item.status === "MATCH" ? "ok" : item.status === "REPAIRED" ? "info" : item.status === "BLOCKED" || item.status === "FAILED" ? "err" : "warn"}>
                    {item.status}
                  </Badge>{" "}
                  {item.student_name} · {item.mode}
                </h4>
                <p>
                  Original: <strong>{item.original_earned ?? "—"}/{item.original_available ?? "—"}</strong>{" "}
                  → Repaired: <strong>{item.repaired_earned ?? "—"}/{item.repaired_available ?? "—"}</strong>
                  {item.repaired_grade ? ` · grade ${item.repaired_grade}` : ""}
                  {item.score_basis ? ` · score basis: ${item.score_basis}` : ""}
                </p>
                <p className="muted">
                  {item.questions_total} mark-scheme questions · {item.questions_added} added ·{" "}
                  {item.questions_changed} changed · {item.questions_flagged} flagged for teacher
                  confirmation · {item.unresolved_count} unresolved
                  {item.error ? ` · error: ${item.error}` : ""}
                </p>
                {(item.audit_findings ?? []).length > 0 && (
                  <details>
                    <summary>Defects found in the ORIGINAL correction ({item.audit_findings!.length})</summary>
                    <ul>
                      {item.audit_findings!.map((f, i) => (
                        <li key={i}><strong>{f.code}</strong>{f.question ? ` (Q${f.question})` : ""} — {f.detail}</li>
                      ))}
                    </ul>
                  </details>
                )}
                {changedRows.length > 0 ? (
                  <details open>
                    <summary>Question-level differences ({changedRows.length})</summary>
                    <table className="table">
                      <thead>
                        <tr>
                          <th>Q</th><th>Max</th><th>Original</th><th>Repaired</th>
                          <th>Change</th><th>Validation</th><th>Reason / evidence</th>
                        </tr>
                      </thead>
                      <tbody>
                        {changedRows.map((d) => (
                          <tr key={d.question}>
                            <td>{d.question}</td>
                            <td>{d.max_marks ?? "—"}</td>
                            <td>{d.original_awarded ?? "—"} <span className="muted">({d.original_status})</span></td>
                            <td>{d.repaired_awarded ?? "—"} <span className="muted">({d.repaired_status})</span></td>
                            <td><Badge tone={CHANGE_TONE[d.change] ?? "info"}>{d.change}</Badge></td>
                            <td className="muted">{d.validation}</td>
                            <td>{d.reason}{d.evidence ? <><br /><span className="muted">evidence: {d.evidence}</span></> : null}</td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </details>
                ) : (
                  <p className="muted">
                    Every question: the corrected second pass agrees with the
                    stored marks ({item.diff?.length ?? 0} questions). Agreement
                    is consistency evidence; final marking judgement stays with
                    the teacher.
                  </p>
                )}
                {item.report_filename && (
                  <Button
                    variant="secondary"
                    onClick={() =>
                      onPreview(
                        `/api/repair/report/${item.run_id}/${item.report_filename}`,
                        `${item.student_name} — repaired report`,
                      )
                    }
                  >
                    Preview repaired report
                  </Button>
                )}
              </article>
            );
          })}
        </>
      )}

      <h4>Complete submission inventory</h4>
      <table className="table">
        <thead>
          <tr>
            <th>Student</th><th>Submission state</th><th>Correction</th>
            <th>Accessible</th><th>Stored score</th><th>Note</th>
          </tr>
        </thead>
        <tbody>
          {data.inventory.submissions.map((s) => (
            <tr key={`${s.student_name}-${s.state}`}>
              <td>{s.student_name}</td>
              <td>{s.state}{s.submitted ? "" : " (not submitted)"}</td>
              <td>{s.has_correction ? <Badge tone="ok">corrected</Badge> : <Badge tone="info">none yet</Badge>}</td>
              <td>{s.accessible ? <Badge tone="ok">yes</Badge> : <Badge tone="err">no</Badge>}</td>
              <td>{s.stored_earned !== null ? `${s.stored_earned}/${s.stored_available}` : "—"}</td>
              <td className="muted">{s.note}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </section>
  );
}

export default function AuditPage() {
  const [data, setData] = useState<Summary | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [preview, setPreview] = useState<string | null>(null);
  const [repair, setRepair] = useState<RepairSummary | null>(null);
  const [repairPreview, setRepairPreview] = useState<{ url: string; title: string } | null>(null);

  const load = useCallback(() => {
    api<Summary>("/audit/summary")
      .then(setData)
      .catch((e: Error) => setError(e.message));
  }, []);
  useEffect(() => load(), [load]);

  const cwId = data?.assignment?.coursework_id;
  useEffect(() => {
    if (!cwId) return;
    api<RepairSummary>(`/repair/summary?coursework_id=${encodeURIComponent(String(cwId))}`)
      .then(setRepair)
      .catch(() => setRepair(null));
  }, [cwId]);

  if (error) {
    return (
      <>
        <PageHeader kicker="System · Accuracy audit" title="Accuracy Audit" sub="Testing-only record" icon="shield" />
        <Empty icon="alert" title="Could not load the audit" hint={error} />
      </>
    );
  }
  if (!data) {
    return (
      <>
        <PageHeader kicker="System · Accuracy audit" title="Accuracy Audit" sub="Testing-only record" icon="shield" />
        <Empty icon="clock" title="Loading audit…" hint="Reading the recorded evidence." />
      </>
    );
  }
  if (data.available === false) {
    return (
      <>
        <PageHeader kicker="System · Accuracy audit" title="Accuracy Audit" sub="Testing-only record" icon="shield" />
        <Empty icon="info" title="No audit recorded yet" hint={data.message} />
      </>
    );
  }

  const t = data.testing_only ?? {};
  const runs = data.runs ?? {};
  const questions = data.questions ?? [];
  const changed = questions.filter((q) => q.changed);
  const unreliable = questions.filter(
    (q) =>
      (q.original_status ?? "").toUpperCase() === "REVIEW" ||
      (q.retest_status ?? "").toUpperCase() === "REVIEW" ||
      (q.retest_status ?? "").toUpperCase() === "NEEDS_REVIEW",
  );
  const maxTotals = data.consistency?.available_totals ?? [];

  return (
    <>
      <PageHeader
        title="Accuracy Audit"
        sub={String(data.assignment?.name ?? "Assignment") + " · " +
          String(data.assignment?.classroom ?? "")}
        icon="shield"
      >
        <Button variant="ghost" icon="refresh" onClick={load}>
          Refresh
        </Button>
      </PageHeader>

      <div className="testing-banner" role="status">
        <strong>{t.banner ?? "TESTING ONLY · RESULTS NOT PUBLISHED"}</strong>
        <span>
          {t.active
            ? " Backend guard ACTIVE — every grade write, report upload, attachment change and work return is refused before it is sent."
            : " Backend guard installed. Publication is additionally blocked because Classroom writes and background automation are off."}
        </span>
      </div>

      {repair && (
        <RepairDashboard
          data={repair}
          onPreview={(url, title) => setRepairPreview({ url, title })}
        />
      )}
      {repairPreview && (
        <section className="card">
          <h3>{repairPreview.title}</h3>
          <div className="button-row">
            <Button variant="ghost" onClick={() => setRepairPreview(null)}>
              Close preview
            </Button>
          </div>
          <iframe
            title={repairPreview.title}
            className="report-preview"
            src={repairPreview.url}
          />
        </section>
      )}

      <section className="card">
        <h3>What is being tested</h3>
        <dl className="kv">
          <div><dt>Assignment</dt><dd>{String(data.assignment?.name ?? "—")}</dd></div>
          <div><dt>Classroom</dt><dd>{String(data.assignment?.classroom ?? "—")}</dd></div>
          <div><dt>Coursework id</dt><dd>{String(data.assignment?.coursework_id ?? "—")}</dd></div>
          <div>
            <dt>Official mark total</dt>
            <dd>
              {String(data.assignment?.official_total_marks ?? "—")} marks over{" "}
              {String(data.assignment?.official_question_count ?? "—")} questions
            </dd>
          </div>
          <div>
            <dt>Mark scheme</dt>
            <dd>
              {data.assignment?.mark_scheme_present
                ? `present locally (${Math.round(
                    Number(data.assignment?.mark_scheme_bytes ?? 0) / 1048576,
                  )} MB, scanned images)`
                : "not available"}
            </dd>
          </div>
        </dl>
        <p className="muted">
          {t.isolated_correction_env} {t.read_only_access_kept}
        </p>
        <ul className="muted">
          {(t.publication_blocked_by ?? []).map((line) => (
            <li key={line}>{line}</li>
          ))}
        </ul>
      </section>

      <section className="card">
        <h3>Test inventory</h3>
        <div className="stat-row">
          <div className="stat">
            <span className="value">{data.inventory?.submissions_found ?? "—"}</span>
            <span className="label">submissions discovered</span>
          </div>
          <div className="stat">
            <span className="value">{data.inventory?.with_answer_files ?? "—"}</span>
            <span className="label">with accessible answer files</span>
          </div>
          {Object.entries(data.inventory?.state_counts ?? {}).map(([k, v]) => (
            <div className="stat" key={k}>
              <span className="value">{v}</span>
              <span className="label">{k.toLowerCase()}</span>
            </div>
          ))}
        </div>
      </section>

      <section className="card">
        <h3>Original run vs post-fix retest</h3>
        <table className="table">
          <thead>
            <tr>
              <th>Result</th>
              <th>Score</th>
              <th>Review</th>
              <th>Status</th>
              <th>Questions</th>
              <th>Unresolved</th>
            </tr>
          </thead>
          <tbody>
            <tr>
              <td>Cached correction (pre-audit)</td>
              <td>{score(runs.original_production)}</td>
              <td>{runs.original_production?.review_required ?? "—"}</td>
              <td>stored production result</td>
              <td>—</td>
              <td>—</td>
            </tr>
            <tr>
              <td>Fresh run — before fixes</td>
              <td>{score(runs.fresh_before_fix)}</td>
              <td>{runs.fresh_before_fix?.review_required ?? "—"}</td>
              <td>{runs.fresh_before_fix?.job_status ?? "—"}</td>
              <td>{runs.fresh_before_fix?.questions ?? "—"}</td>
              <td>{runs.fresh_before_fix?.unresolved_questions?.length ?? "—"}</td>
            </tr>
            <tr>
              <td>Fresh run — after fixes</td>
              <td>{score(runs.fresh_after_fix)}</td>
              <td>{runs.fresh_after_fix?.review_required ?? "—"}</td>
              <td>
                <Badge tone={runs.fresh_after_fix?.job_status === "COMPLETED" ? "ok" : "warn"}>
                  {runs.fresh_after_fix?.job_status ?? "—"}
                </Badge>
              </td>
              <td>{runs.fresh_after_fix?.questions ?? "—"}</td>
              <td>
                {runs.fresh_after_fix?.unresolved_questions?.length
                  ? runs.fresh_after_fix.unresolved_questions.join(", ")
                  : "none"}
              </td>
            </tr>
          </tbody>
        </table>        <p className="muted">
          Both fresh runs corrected the same real 49-page submission with the
          same real mark scheme, in isolated test databases. The score differs
          slightly (234 vs 232) because model marking is stochastic; the
          structural outcome — every mark-scheme question accounted for and the
          job completing instead of being parked for review — is what changed.
        </p>
      </section>

      <section className="card">
        <h3>Question-by-question audit</h3>
        <p className="muted">
          {questions.length} questions · {changed.length} changed between the
          stored correction and the verified retest · {unreliable.length}{" "}
          flagged for review at some point.
        </p>
        <table className="table">
          <thead>
            <tr>
              <th>Q</th>
              <th>Official max</th>
              <th>Original</th>
              <th>Status</th>
              <th>Retest</th>
              <th>Δ</th>
              <th>Retest evidence</th>
            </tr>
          </thead>
          <tbody>
            {questions.map((q) => (
              <tr key={q.question} className={q.changed ? "row-changed" : ""}>
                <td>{q.question}</td>
                <td>{q.official_max ?? "—"}</td>
                <td>{q.original_awarded}</td>
                <td>
                  {q.original_status ? (
                    <Badge tone={(q.original_status ?? "").toUpperCase() === "VALID" ? "ok" : "warn"}>
                      {q.original_status}
                    </Badge>
                  ) : (
                    "—"
                  )}
                </td>
                <td>{q.retest_awarded}</td>
                <td>{q.changed ? (q.delta > 0 ? `+${q.delta}` : q.delta) : ""}</td>
                <td className="muted">
                  {q.review_reason ?? q.retest_reason ?? q.retest_evidence ?? ""}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </section>

      {maxTotals.length > 0 && (
        <section className="card">
          <h3>Denominator consistency</h3>
          <p>
            Across the stored results for this assignment the maximum total took{" "}
            <strong>{maxTotals.join(" and ")}</strong> different values — the same
            paper, the same class, different denominators for different students.
          </p>
          <table className="table">
            <thead>
              <tr><th>Question</th><th>Max marks seen</th></tr>
            </thead>
            <tbody>
              {Object.entries(
                data.consistency?.per_question_max_inconsistent ?? {},
              ).map(([q, values]) => (
                <tr key={q}>
                  <td>Q{q}</td>
                  <td>{values.join(" / ")}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </section>
      )}

      <section className="card">
        <h3>Confirmed defects, root causes and fixes</h3>
        {(data.defects ?? []).map((d) => (
          <article className="defect" key={d.id}>
            <h4>
              <Badge tone={d.severity === "high" ? "err" : "info"}>
                {d.id} · {d.severity ?? "defect"}
              </Badge>{" "}
              {d.title}
            </h4>
            {d.impact && <p><strong>Impact:</strong> {d.impact}</p>}
            {!!d.evidence?.length && (
              <>
                <p><strong>Evidence from the real data:</strong></p>
                <ul>{d.evidence.map((line) => <li key={line}>{line}</li>)}</ul>
              </>
            )}
            {d.root_cause && <p><strong>Root cause:</strong> {d.root_cause}</p>}
            {d.fix && <p><strong>Fix:</strong> {d.fix}</p>}
            {d.status && <p className="muted"><strong>Status:</strong> {d.status}</p>}
          </article>
        ))}
      </section>

      <section className="card">
        <h3>Regression tests</h3>
        <p>
          {data.regression?.summary ?? "not recorded"}{" "}
          {data.regression?.failed ? (
            <Badge tone="warn">{data.regression.failed} failing</Badge>
          ) : (
            <Badge tone="ok">all passing</Badge>
          )}
        </p>
        <ul>
          {(data.regression?.cases ?? []).map((c) => <li key={c}>{c}</li>)}
        </ul>
      </section>

      {(data.reports ?? []).length > 0 && (
        <section className="card">
          <h3>Generated reports (internal preview)</h3>
          <p className="muted">
            Test artefacts only. They are not uploaded to Drive, not attached to
            any submission and are not reachable without signing in.
          </p>
          <div className="button-row">
            {(data.reports ?? []).map((name) => (
              <Button key={name} variant="secondary" onClick={() => setPreview(name)}>
                Preview {name}
              </Button>
            ))}
          </div>
          {preview && (
            <iframe
              title={preview}
              className="report-preview"
              src={`/api/audit/reports/${encodeURIComponent(preview)}`}
            />
          )}
        </section>
      )}

      {(data.limitations ?? []).length > 0 && (
        <section className="card">
          <h3>Remaining problems and limitations</h3>
          <ul>{(data.limitations ?? []).map((l) => <li key={l}>{l}</li>)}</ul>
        </section>
      )}
    </>
  );
}
