import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  api, dataImport, dataImportPreview, dataInventory, timeAgo,
  type DataInventory, type ImportPreview, type ImportReport,
} from "../api";
import {
  Badge, Button, DataTable, Empty, Icon, PageHeader, Progress, Section, Stat,
  useToast,
} from "../components";
import { SyncClassroomButton } from "../SyncClassroom";

/** A stored correction, as the results list actually returns it. */
interface StoredResult {
  id: string;
  student_name: string | null;
  display_name?: string | null;
  earned: number;
  available: number;
  percentage: number;
  provisional_grade: string | null;
  created_at: number;
  assignment_title?: string | null;
  class_title?: string | null;
  coursework_id?: string | null;
  status?: string;
  review_required?: number;
}

function humanBytes(bytes: number): string {
  if (!bytes) return "0 B";
  let value = bytes;
  for (const unit of ["B", "KB", "MB", "GB"]) {
    if (value < 1024) return `${value.toFixed(value < 10 && unit !== "B" ? 1 : 0)} ${unit}`;
    value /= 1024;
  }
  return `${value.toFixed(1)} TB`;
}

/* ==================================================================== export */

function ExportCard({ inventory, onExported }: {
  inventory: DataInventory;
  onExported?: () => void;
}) {
  const toast = useToast();
  const [busy, setBusy] = useState(false);
  const ex = inventory.export;
  const counts = inventory.counts;

  async function download() {
    setBusy(true);
    try {
      // Fetch then save, so a failure is REPORTED instead of silently
      // navigating the tab to a JSON error body.
      const response = await fetch("/api/data/export", { credentials: "same-origin" });
      if (!response.ok) {
        const body = await response.json().catch(() => ({}));
        throw new Error(body.message ?? `Export failed (HTTP ${response.status})`);
      }
      const blob = await response.blob();
      const url = URL.createObjectURL(blob);
      const anchor = document.createElement("a");
      anchor.href = url;
      anchor.download = ex.filename;
      document.body.appendChild(anchor);
      anchor.click();
      anchor.remove();
      URL.revokeObjectURL(url);
      toast("ok", `Exported ${counts.students ?? 0} student(s) and `
        + `${counts.results ?? 0} result(s) to ${ex.filename}`);
      onExported?.();
    } catch (e) {
      toast("err", (e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <Section title="Export data" icon="download"
      hint={`format ${ex.format} v${ex.format_version}`}>
      <div className="grid grid-4" style={{ marginBottom: 12 }}>
        <Stat compact label="Students" value={counts.students ?? 0} />
        <Stat compact label="Classes" value={counts.classes ?? 0} />
        <Stat compact label="Assignments" value={counts.assignments ?? 0} />
        <Stat compact label="Results" value={counts.results ?? 0} />
      </div>

      <p className="small" style={{ marginTop: 0 }}>
        This exports your <strong>students, classes, assignments, corrections
        and marks</strong> — including raw marks, maximum marks, percentages,
        the 1–9 grade, question-level marks and feedback, correction
        timestamps, review decisions and the references that link results to
        their reports.
      </p>
      <ul className="small faint" style={{ margin: "0 0 12px 18px" }}>
        {ex.sections.map((s) => <li key={s}>{s}</li>)}
      </ul>

      <div className="notice info small" style={{ marginBottom: 12 }}>
        <Icon name="shield" size={14} />
        <span>
          <strong>Never included:</strong> API keys, passwords, OAuth tokens,
          session cookies, device credentials, workflow queues and
          machine-local caches. The file is refused if any secret-looking field
          is present.
        </span>
      </div>

      <div className="row wrap" style={{ gap: 8 }}>
        <Button variant="primary" icon="download" loading={busy} onClick={download}>
          Export data ({ex.filename.split("_").slice(-1)[0]})
        </Button>
        <span className="faint small" style={{ alignSelf: "center" }}>
          Written as plain JSON so you can inspect it before moving it.
        </span>
      </div>
    </Section>
  );
}

/* ==================================================================== import */

function ImportCard({ onImported }: { onImported?: () => void }) {
  const toast = useToast();
  const fileRef = useRef<HTMLInputElement>(null);
  const [file, setFile] = useState<File | null>(null);
  const [preview, setPreview] = useState<ImportPreview | null>(null);
  const [report, setReport] = useState<ImportReport | null>(null);
  const [mode, setMode] = useState<"add_only" | "update_existing">("add_only");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  function reset() {
    setPreview(null); setReport(null); setError(null);
  }

  async function doPreview() {
    if (!file) return;
    setBusy(true); setError(null); setReport(null);
    try {
      const result = await dataImportPreview(file);
      setPreview(result);
      toast("info", `${result.totals.new} new record(s), `
        + `${result.totals.matching} already present, `
        + `${result.totals.invalid} unusable.`);
    } catch (e) {
      setPreview(null);
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  async function doImport() {
    if (!file) return;
    setBusy(true); setError(null);
    try {
      const outcome = await dataImport(file, mode);
      setReport(outcome);
      toast("ok", `${outcome.totals.added} added, ${outcome.totals.updated} `
        + `updated, ${outcome.totals.skipped} left as they were.`);
      onImported?.();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  const overflows = Boolean(preview && preview.totals.matching > 0);
  const nothingNew = Boolean(preview && preview.totals.new === 0
    && preview.totals.matching === 0);

  return (
    <Section title="Import data" icon="upload" hint="from another installation">
      <p className="small" style={{ marginTop: 0 }}>
        Choose a file produced by <strong>Export data</strong>. You will see
        exactly what would happen before anything is written, and the import
        only ever runs inside one database transaction — a failure changes
        nothing at all.
      </p>

      <div className="row wrap" style={{ gap: 10, alignItems: "flex-end" }}>
        <label className="field" style={{ marginBottom: 0, flex: "1 1 260px" }}>
          <span>Exported .json file</span>
          <input className="input" type="file" accept=".json,application/json"
            ref={fileRef}
            onChange={(e) => {
              setFile(e.target.files?.[0] ?? null);
              reset();
            }} />
        </label>
        <Button variant="secondary" icon="search" loading={busy}
          disabled={!file} onClick={doPreview}>
          Preview import
        </Button>
      </div>

      {file && (
        <div className="faint small" style={{ marginTop: 6 }}>
          {file.name} · {humanBytes(file.size)}
        </div>
      )}

      {error && (
        <div className="notice err small" style={{ marginTop: 12 }} role="alert">
          <strong>Nothing was imported.</strong> {error}
        </div>
      )}

      {preview && (
        <div style={{ marginTop: 14 }}>
          {preview.warnings.map((w) => (
            <div className="notice warn small" key={w} style={{ marginBottom: 6 }}>
              {w}
            </div>
          ))}
          {preview.contains_secrets && (
            <div className="notice err small" style={{ marginBottom: 6 }}>
              This file claims to carry secrets. Secret fields are refused, so
              they cannot reach this installation.
            </div>
          )}

          <div className="row wrap" style={{ gap: 8, marginBottom: 10 }}>
            <Badge tone="info">{preview.totals.new} new</Badge>
            <Badge tone={preview.totals.matching > 0 ? "warn" : "mute"}>
              {preview.totals.matching} already present
            </Badge>
            <Badge tone={preview.totals.invalid > 0 ? "err" : "mute"}>
              {preview.totals.invalid} unusable
            </Badge>
            {preview.exported_at_iso && (
              <span className="faint small" style={{ alignSelf: "center" }}>
                exported {preview.exported_at_iso}
                {preview.app_version ? ` · app v${preview.app_version}` : ""}
              </span>
            )}
          </div>

          <DataTable head={<tr>
            <th>Records</th><th>In file</th><th>New</th>
            <th>Already here</th><th>Unusable</th></tr>}>
            {preview.sections.map((s) => (
              <tr key={s.table}>
                <td>{s.label}</td>
                <td>{s.total}</td>
                <td>{s.new}</td>
                <td className={s.matching ? "warn" : "muted"}>{s.matching}</td>
                <td className={s.invalid ? "err" : "muted"}>{s.invalid}</td>
              </tr>
            ))}
          </DataTable>

          {nothingNew && (
            <div className="notice info small" style={{ marginTop: 10 }}>
              Nothing in this file is new to this installation.
            </div>
          )}

          {preview.invalid_rows.length > 0 && (
            <details style={{ marginTop: 10 }}>
              <summary className="small">
                {preview.invalid_rows.length} unusable record(s) — these are
                skipped and reported, never guessed at
              </summary>
              <ul className="small faint" style={{ marginTop: 6 }}>
                {preview.invalid_rows.map((row, i) => (
                  <li key={i}>{row.table}: {row.reason}</li>
                ))}
              </ul>
            </details>
          )}

          <fieldset style={{ marginTop: 14, border: 0, padding: 0 }}>
            <legend className="label small" style={{ marginBottom: 6 }}>
              How should matching records be treated?
            </legend>
            {preview.policies.options.map((o) => (
              <label key={o.value} className="row small"
                style={{ gap: 8, alignItems: "flex-start", marginBottom: 8 }}>
                <input type="radio" name="import-mode" value={o.value}
                  checked={mode === o.value}
                  onChange={() => setMode(o.value as typeof mode)}
                  style={{ marginTop: 3 }} />
                <span>
                  <strong>{o.label}</strong>
                  <span className="faint" style={{ display: "block" }}>{o.detail}</span>
                </span>
              </label>
            ))}
          </fieldset>

          {overflows && mode === "update_existing" && (
            <div className="notice warn small" style={{ marginBottom: 10 }}>
              {preview.totals.matching} existing record(s) will be replaced by
              the file's copies. A database backup is taken first and its path
              is shown below.
            </div>
          )}

          <Button variant={mode === "update_existing" ? "warning" : "primary"}
            icon="upload" loading={busy} onClick={doImport}>
            {mode === "update_existing"
              ? "Back up, then import and update"
              : "Import new records only"}
          </Button>
        </div>
      )}

      {report && (
        <div style={{ marginTop: 16 }}>
          <div className="notice ok small" style={{ marginBottom: 10 }}>
            Import finished: <strong>{report.totals.added}</strong> added,{" "}
            <strong>{report.totals.updated}</strong> updated,{" "}
            <strong>{report.totals.skipped}</strong> left untouched,{" "}
            <strong>{report.totals.invalid}</strong> unusable.
            {report.backup_path && (
              <span className="faint" style={{ display: "block" }}>
                Backup written before the import: <code>{report.backup_path}</code>
              </span>
            )}
          </div>
          <DataTable head={<tr>
            <th>Records</th><th>Added</th><th>Updated</th>
            <th>Left alone</th><th>Unusable</th></tr>}>
            {report.sections.map((s) => (
              <tr key={s.table}>
                <td>{s.label}</td>
                <td>{s.added}</td>
                <td>{s.updated}</td>
                <td className="muted">{s.skipped}</td>
                <td className={s.invalid ? "err" : "muted"}>{s.invalid}</td>
              </tr>
            ))}
          </DataTable>
          {report.invalid_rows.length > 0 && (
            <div className="faint small" style={{ marginTop: 8 }}>
              Skipped: {report.invalid_rows.map((r) => `${r.table} (${r.reason})`)
                .join(", ")}
            </div>
          )}
        </div>
      )}
    </Section>
  );
}

/* ===================================================================== page */

export default function StoragePage() {
  const [inventory, setInventory] = useState<DataInventory | null>(null);
  const [results, setResults] = useState<StoredResult[]>([]);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(() => {
    dataInventory().then(setInventory).catch((e) => setError((e as Error).message));
    api<{ results: StoredResult[] }>("/results?limit=25")
      .then((r) => setResults(r.results ?? [])).catch(() => {});
  }, []);

  useEffect(() => { load(); }, [load]);

  const counts = inventory?.counts ?? {};
  const health = useMemo(() => {
    if (!inventory) return [];
    return [
      { label: "Students", value: counts.students ?? 0 },
      { label: "Classes", value: counts.classes ?? 0 },
      { label: "Assignments", value: counts.assignments ?? 0 },
      { label: "Corrections", value: counts.results ?? 0 },
      { label: "Graded (1–9)", value: counts.graded_results ?? 0 },
      { label: "Awaiting review", value: counts.review_required ?? 0,
        tone: (counts.review_required ?? 0) > 0 ? "warn" as const : undefined },
      { label: "Failed jobs", value: counts.failed_jobs ?? 0,
        tone: (counts.failed_jobs ?? 0) > 0 ? "err" as const : undefined },
      { label: "Report links", value: counts.canonical_reports ?? 0 },
    ];
  }, [inventory, counts]);

  return (
    <>
      <PageHeader kicker="System · Storage" title="Storage"
        sub="Everything this installation has saved — and how to move it to another computer."
        icon="database">
        {/* Sync Classroom is genuinely relevant here: it is what populates the
            students, assignments and submissions this page stores. */}
        <SyncClassroomButton onSynced={() => load()} />
      </PageHeader>

      {error && (
        <div className="notice err" style={{ marginBottom: 12 }}>{error}</div>
      )}

      <div className="grid grid-4" style={{ marginBottom: 16 }}>
        {health.map((h) => (
          <Stat key={h.label} label={h.label} value={h.value} tone={h.tone} />
        ))}
      </div>

      <Section title="Storage status" icon="database"
        hint={inventory ? `app v${inventory.app_version} · schema ${inventory.schema_version}` : undefined}>
        {!inventory ? (
          <div className="skeleton" style={{ height: 90 }} />
        ) : (
          <>
            <div className="faint small" style={{ marginBottom: 10 }}>
              Records live in a local SQLite database of{" "}
              <strong>{humanBytes(inventory.database_bytes)}</strong> and
              survive restarts, updates and reinstalls. Nothing here is sent
              anywhere unless you export it.
            </div>
            <div className="row wrap" style={{ gap: 8 }}>
              <a className="btn sm secondary" href="/api/backup/database" download>
                Download full database backup (.db)
              </a>
              <a className="btn sm secondary" href="/api/backup/config" download>
                Export configuration
              </a>
            </div>
            <p className="faint small" style={{ marginBottom: 0, marginTop: 8 }}>
              A database backup is the whole SQLite file (a complete, restorable
              copy, useful for support). The versioned <em>Export data</em>{" "}
              package below is what you use to move between computers or
              versions, because it carries only your student and correction
              records and never your keys.
            </p>
          </>
        )}
      </Section>

      {inventory && <ExportCard inventory={inventory} onExported={load} />}

      <ImportCard onImported={load} />

      <Section title="Stored corrections" icon="chart"
        hint={`${counts.results ?? 0} total · showing ${results.length}`}>
        {results.length === 0 ? (
          <Empty icon="chart" art="chart" title="No corrections stored yet"
            hint="Run a correction — from Correct a PDF or a Classroom assignment — and its result appears here." />
        ) : (
          <DataTable head={<tr>
            <th>Student</th><th>Assignment</th><th>Marks</th><th>%</th>
            <th>Grade</th><th>When</th><th>Report</th></tr>}>
            {results.map((r) => (
              <tr key={r.id}>
                <td>
                  <a href={`#/results/${encodeURIComponent(r.id)}`}>
                    {r.display_name || r.student_name || "Unnamed"}
                  </a>
                  {r.review_required ? <Badge tone="warn">review</Badge> : null}
                </td>
                <td className="muted small">{r.assignment_title || "—"}</td>
                <td>{r.earned}/{r.available}</td>
                <td>
                  <span className="row" style={{ gap: 6, alignItems: "center" }}>
                    {Math.round(r.percentage)}%
                    <Progress value={r.percentage / 100} thin />
                  </span>
                </td>
                <td>{r.provisional_grade ?? "—"}</td>
                <td className="faint small">{timeAgo(r.created_at)}</td>
                <td>
                  <a className="btn sm ghost"
                    href={`/api/results/${encodeURIComponent(r.id)}/export?format=pdf`}>
                    PDF
                  </a>
                </td>
              </tr>
            ))}
          </DataTable>
        )}
        <p className="faint small" style={{ marginBottom: 0, marginTop: 8 }}>
          Students and assignments themselves are listed on the{" "}
          <a href="#/students">student records</a> and{" "}
          <a href="#/assignments">assignment</a> views.
        </p>
      </Section>
    </>
  );
}
