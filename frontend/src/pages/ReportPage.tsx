import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api, timeAgo } from "../api";
import {
  Badge, Button, Empty, Icon, Loading, PageHeader, Stat, useToast,
} from "../components";

/* ============================================================== types */

interface AssignmentOption {
  id: string; course_id: string; course_name: string | null;
  title: string; max_points: number | null; due_date: string | null;
  code: string; chapter: string; lesson: string; topic: string;
  detail: string; label: string;
}

interface ClassOption { label: string; students: number }

interface ReportTypeOption { value: string; label: string }

interface Branding {
  teacher_name: string; teacher_title: string; center: string;
  year: string; quote: string; monogram: string; logo: boolean;
}

interface ReportOptions {
  report_types: ReportTypeOption[];
  classes: ClassOption[];
  assignments: AssignmentOption[];
  threshold_default: number;
  threshold_options: number[];
  grade_scale: string;
  boundaries: [string, number][];
  branding: Branding;
  connected: boolean;
  statuses: { value: string; label: string }[];
  modes?: { value: string; label: string; hint?: string }[];
  mode_default?: string;
}

/**
 * One selected assignment as the PREVIEW MODEL reports it.
 *
 * The model is built from `AssignmentCode.as_dict()`, which names the
 * assignment's identity `coursework_id` - the options endpoint above calls the
 * same thing `id`. Reading `id` off a model assignment silently yielded
 * `undefined`, which is how every grade-matrix cell can end up blank, so the
 * two shapes are kept apart here.
 *
 * The identity is what the grade matrix pairs on: `code` ("CH1L1") is the
 * lesson's full identifier and two assignments may legitimately share one,
 * while `coursework_id` is unique per assignment.
 */
interface ModelAssignment {
  coursework_id: string;
  code: string; key?: string; label: string; detail: string;
  title: string; topic: string; chapter: string; lesson: string;
}

interface GradeEntry {
  code: string; key?: string; coursework_id?: string;
  label: string; detail: string; title: string;
  chapter: string; lesson: string; grade: string; source: string;
  grade_label: string; percentage: number | null; status: string;
  submitted: boolean; is_draft: boolean; is_missing: boolean;
  is_outstanding?: boolean; tracking?: string; tracking_label?: string;
}

interface StudentEntry {
  student_id: string; display_name: string; class_label: string;
  grades: GradeEntry[];
}

interface FlatEntry {
  student_id: string; display_name: string; class_label: string;
  assignments: {
    code: string; key?: string; coursework_id?: string;
    label: string; detail: string; title: string;
    chapter: string; lesson: string; grade?: string; grade_label?: string;
    status?: string; status_label?: string; submitted?: boolean;
    source?: string; percentage?: number | null;
    tracking?: string; original_grade?: number | null;
  }[];
}

interface ReportGroup {
  class_label: string; student_count: number;
  students: StudentEntry[];
  missing: FlatEntry[]; draft: FlatEntry[]; low_grade: FlatEntry[];
  grade_one: FlatEntry[];
}

interface ClassroomNote {
  student_id: string; display_name: string; class_label: string;
}

interface ReportModel {
  title: string; report_type: string; sections: string[];
  detailed?: boolean; mode?: string;
  class_names: string; threshold: number | null;
  generated_at: number | string | null;
  summary: {
    students: number; assignments: number; submitted: number; cells: number;
    missing: number; outstanding?: number; draft: number; low_grade: number;
    grade_one: number;
  };
  assignments: ModelAssignment[];
  classroom_note?: ClassroomNote[];
  groups: ReportGroup[];
  branding: Branding;
  statuses?: string[];
}

interface StudentOption {
  student_id: string; display_name: string; class_label: string;
}

/* ============================================================== helpers */

const STATUS_FILTERS = [
  { value: "missing", label: "Outstanding (missing / draft / 0)", tone: "err" as const },
  { value: "draft", label: "Draft / incomplete", tone: "warn" as const },
  { value: "low_grade", label: "Grade below threshold", tone: "warn" as const },
  { value: "submitted", label: "Handed in (status table)", tone: "ok" as const },
];

// Fallback when an older backend does not advertise its report modes yet.
const DEFAULT_MODES = [
  { value: "summary", label: "Missing only - one line per student" },
  { value: "detailed", label: "Detailed - per-assignment breakdown" },
];

function classNames(...parts: (string | false | null | undefined)[]) {
  return parts.filter(Boolean).join(" ");
}

/* ============================================================ checkboxes */

function CheckRow({ checked, onChange, label, hint, tone }: {
  checked: boolean; onChange: (v: boolean) => void; label: string;
  hint?: string; tone?: "ok" | "warn" | "err";
}) {
  return (
    <label className={classNames("rp-check", checked && "checked")}>
      <input
        type="checkbox"
        checked={checked}
        onChange={(e) => onChange(e.target.checked)}
      />
      <span className="check-box" aria-hidden="true">
        <Icon name="check" size={11} />
      </span>
      <span className="check-text">
        <span className={classNames("check-label", tone && `tone-${tone}`)}>
          {label}
        </span>
        {hint && <span className="check-hint">{hint}</span>}
      </span>
    </label>
  );
}

/* ============================================================== page */

interface ReportDeepLink { courseId?: string; workId?: string }

export default function ReportPage({ deepLink = {} }: { deepLink?: ReportDeepLink }) {
  const toast = useToast();

  const [options, setOptions] = useState<ReportOptions | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [connecting, setConnecting] = useState(false);

  // ---- filters -------------------------------------------------------
  const [reportType, setReportType] = useState("class");
  const [classFilter, setClassFilter] = useState("all");
  const [selected, setSelected] = useState<Set<string> | null>(null);
  const [assignmentQuery, setAssignmentQuery] = useState("");
  const [statuses, setStatuses] = useState<Set<string>>(new Set());
  const [mode, setMode] = useState("summary");
  const [threshold, setThreshold] = useState<number>(2);
  const [studentId, setStudentId] = useState("");
  const [students, setStudents] = useState<StudentOption[]>([]);

  // ---- preview -------------------------------------------------------
  const [model, setModel] = useState<ReportModel | null>(null);
  const [building, setBuilding] = useState(false);
  const [stale, setStale] = useState(false);
  const [exporting, setExporting] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const autoBuilt = useRef(false);

  /* ---------------------------------------------------------- loading */

  const loadOptions = useCallback(() => {
    setLoadError(null);
    api<ReportOptions>("/reports/options")
      .then((o) => {
        setOptions(o);
        setThreshold(o.threshold_default ?? 2);
        setMode(o.mode_default ?? "summary");
        if (deepLink.workId) setSelected(new Set([deepLink.workId]));
      })
      .catch((e) => setLoadError((e as Error).message));
  }, [deepLink.workId]);

  useEffect(() => { loadOptions(); }, [loadOptions]);

  useEffect(() => {
    if (reportType !== "student" || students.length) return;
    api<{ students: StudentOption[] }>("/students")
      .then((r) => setStudents(r.students ?? []))
      .catch(() => setStudents([]));
  }, [reportType, students.length]);

  /* --------------------------------------------------------- filtering */

  const assignmentList = useMemo(() => {
    const all = options?.assignments ?? [];
    const needle = assignmentQuery.trim().toLowerCase();
    if (!needle) return all;
    return all.filter((a) =>
      a.title.toLowerCase().includes(needle)
      || a.code.toLowerCase().includes(needle)
      || (a.course_name ?? "").toLowerCase().includes(needle));
  }, [options, assignmentQuery]);

  const selectedIds = useMemo(() => {
    if (!selected || !options) return null;
    return options.assignments.filter((a) => selected.has(a.id)).map((a) => a.id);
  }, [selected, options]);

  const assignmentsParam = selectedIds ? selectedIds.join(",") : "all";

  const params = useCallback((overrides: Record<string, string | undefined> = {}) => {
    const base: Record<string, string | undefined> = {
      report_type: reportType,
      classes: classFilter,
      assignments: assignmentsParam,
      threshold: String(threshold),
      statuses: [...statuses].join(","),
      mode,
      student_id: reportType === "student" ? studentId || undefined : undefined,
      ...overrides,
    };
    const out = new URLSearchParams();
    for (const [k, v] of Object.entries(base)) {
      if (v != null && v !== "") out.set(k, v);
    }
    return out;
  }, [reportType, classFilter, assignmentsParam, threshold, statuses, mode, studentId]);

  const markStale = () => setStale(true);

  /* ------------------------------------------------------------ build */

  const build = useCallback(async (silent = false) => {
    if (!options) return;
    if (reportType === "student" && !studentId) {
      setError("Choose a student to build a student report.");
      return;
    }
    if (selectedIds && selectedIds.length === 0) {
      setError("Select at least one assignment, or choose All assignments.");
      return;
    }
    setError(null);
    setBuilding(true);
    try {
      const qs = params().toString();
      const data = await api<ReportModel>(`/reports/build?${qs}`);
      setModel(data);
      setStale(false);
      if (!silent) toast("ok", `Preview updated — ${data.summary.students} student(s), ${data.summary.assignments} assignment(s)`);
    } catch (e) {
      const message = (e as Error).message;
      setError(message);
      if (!silent) toast("err", message);
    } finally {
      setBuilding(false);
    }
  }, [options, params, reportType, studentId, selectedIds, toast]);

  // First paint: build once so the teacher never lands on an empty page.
  useEffect(() => {
    if (!options || autoBuilt.current) return;
    autoBuilt.current = true;
    void build(true);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [options]);

  /* ----------------------------------------------------------- export */

  const download = useCallback(async (
    query: URLSearchParams, label: string,
  ) => {
    setExporting(label);
    setError(null);
    try {
      const res = await fetch(`/api/reports/build/export?${query.toString()}`);
      if (!res.ok) {
        let message = `Export failed (HTTP ${res.status})`;
        try {
          const body = await res.json();
          message = body.message || message;
        } catch { /* not JSON */ }
        throw new Error(message);
      }
      const blob = await res.blob();
      const disposition = res.headers.get("content-disposition") ?? "";
      const match = /filename="([^"]+)"/.exec(disposition);
      const filename = match?.[1] ?? `${reportType}_report.pdf`;
      const url = URL.createObjectURL(blob);
      const link = document.createElement("a");
      link.href = url;
      link.download = decodeURIComponent(filename);
      document.body.appendChild(link);
      link.click();
      link.remove();
      window.setTimeout(() => URL.revokeObjectURL(url), 5000);
      toast("ok", `Report exported — ${filename}`);
    } catch (e) {
      const message = (e as Error).message;
      setError(message);
      toast("err", message);
    } finally {
      setExporting(null);
    }
  }, [reportType, toast]);

  const exportCurrent = () => { void download(params(), "current"); };

  const exportAll = () => {
    void download(params({ classes: "all", assignments: "all" }), "all");
  };

  /* --------------------------------------------------------- connect */

  async function connect() {
    setConnecting(true);
    try {
      await api("/classroom/connect", { method: "POST" });
      toast("ok", "Google Classroom connected");
      loadOptions();
    } catch (e) {
      const message = (e as Error).message;
      setError(message);
      toast("err", message);
    } finally {
      setConnecting(false);
    }
  }

  /* ----------------------------------------------------------- states */

  if (loadError) {
    return (
      <>
        <PageHeader title="Reports" sub="Build, preview and export official reports." />
        <div className="notice err">
          <Icon name="alert" size={16} />
          <span>
            <strong>Could not load the report options.</strong> {loadError}
          </span>
        </div>
        <div style={{ marginTop: 12 }}>
          <Button icon="refresh" onClick={loadOptions}>Retry</Button>
        </div>
      </>
    );
  }

  if (!options) {
    return (
      <>
        <PageHeader title="Reports" sub="Build, preview and export official reports." />
        <Loading label="Loading report options…" />
      </>
    );
  }

  const hasData = (options.assignments.length > 0);
  const needsStudent = reportType === "student" && !studentId;

  /* ------------------------------------------------------------- view */

  return (
    <>
      <PageHeader
        title="Reports"
        sub={`Choose a report type, class and assignments, preview it, then export the same data as an A4 PDF.${model ? ` Last preview ${timeAgo(typeof model.generated_at === "number" ? model.generated_at : null)}.` : ""}`}
        icon="reports"
      >
        <Button
          icon="refresh"
          onClick={() => void build()}
          loading={building}
          disabled={!hasData}
          tooltip="Rebuild the preview from the current filters"
        >
          Generate preview
        </Button>
        <Button
          icon="download"
          onClick={exportAll}
          loading={exporting === "all"}
          disabled={!hasData || building}
          tooltip="Export every class and every assignment"
        >
          Export All
        </Button>
        <Button
          variant="primary"
          icon="download"
          onClick={exportCurrent}
          loading={exporting === "current"}
          disabled={!hasData || building || needsStudent}
          tooltip="Export exactly what the preview shows"
        >
          Export PDF
        </Button>
      </PageHeader>

      {!options.connected && (
        <div className="notice info" style={{ marginBottom: 14 }}>
          <Icon name="info" size={16} />
          <span>
            Google Classroom is not connected — reports are built from the data
            already synced on this computer.
          </span>
          <Button size="sm" icon="classroom" onClick={() => void connect()}
            loading={connecting} style={{ marginLeft: "auto" }}>
            Connect
          </Button>
        </div>
      )}

      {error && (
        <div className="notice err" style={{ marginBottom: 14 }}>
          <Icon name="alert" size={16} />
          <span>{error}</span>
        </div>
      )}

      <div className="rp-layout">
        {/* ------------------------------------------------ the builder */}
        <div className="rp-builder">
          <div className="card rp-card">
            <h3><Icon name="filter" size={16} /> Report builder</h3>

            <label className="field">
              <span>Report type</span>
              <select className="input" value={reportType}
                onChange={(e) => { setReportType(e.target.value); markStale(); }}>
                {options.report_types.map((t) => (
                  <option key={t.value} value={t.value}>{t.label}</option>
                ))}
              </select>
            </label>

            <label className="field">
              <span>Class</span>
              <select className="input" value={classFilter}
                onChange={(e) => { setClassFilter(e.target.value); markStale(); }}>
                <option value="all">All classes</option>
                {options.classes.map((c) => (
                  <option key={c.label} value={c.label}>
                    {c.label} · {c.students} student{c.students === 1 ? "" : "s"}
                  </option>
                ))}
              </select>
            </label>

            {reportType === "student" && (
              <label className="field">
                <span>Student</span>
                <select className="input" value={studentId}
                  onChange={(e) => { setStudentId(e.target.value); markStale(); }}>
                  <option value="">Choose a student…</option>
                  {students.map((s) => (
                    <option key={s.student_id} value={s.student_id}>
                      {s.display_name}{s.class_label ? ` · ${s.class_label}` : ""}
                    </option>
                  ))}
                </select>
              </label>
            )}

            {/* ---- assignments (multi-select) ---- */}
            <div className="field">
              <span>
                Assignments
                <span className="hint">
                  {selected
                    ? `${selected.size} selected of ${options.assignments.length}`
                    : `All ${options.assignments.length}`}
                </span>
              </span>

              <div className="rp-assign-tools">
                <input
                  className="input" placeholder="Search assignments…"
                  value={assignmentQuery} aria-label="Search assignments"
                  onChange={(e) => setAssignmentQuery(e.target.value)}
                />
                <div className="row" style={{ gap: 6 }}>
                  <Button size="sm" variant="ghost"
                    onClick={() => {
                      setSelected(new Set(options.assignments.map((a) => a.id)));
                      markStale();
                    }}>
                    All
                  </Button>
                  <Button size="sm" variant="ghost"
                    onClick={() => { setSelected(new Set()); markStale(); }}>
                    None
                  </Button>
                </div>
              </div>

              {options.assignments.length === 0 ? (
                <Empty icon="assignments" title="No assignments yet"
                  hint="Sync Google Classroom or create an assignment first." />
              ) : (
                <div className="rp-assign-list" role="group"
                  aria-label="Assignments to include">
                  {assignmentList.map((a) => {
                    const checked = selected ? selected.has(a.id) : true;
                    return (
                      <CheckRow key={a.id} checked={checked} label={a.code}
                        hint={a.title}
                        onChange={(on) => {
                          setSelected((prev) => {
                            const base = new Set(
                              prev ?? options.assignments.map((x) => x.id));
                            if (on) base.add(a.id); else base.delete(a.id);
                            return base;
                          });
                          markStale();
                        }} />
                    );
                  })}
                </div>
              )}
            </div>

            {/* ---- status filters ---- */}
            <div className="field">
              <span>
                Status filters
                <span className="hint">
                  {statuses.size ? `${statuses.size} selected` : "all sections"}
                </span>
              </span>
              <div className="rp-checks">
                {STATUS_FILTERS.map((s) => (
                  <CheckRow key={s.value} tone={s.tone}
                    checked={statuses.has(s.value)} label={s.label}
                    onChange={(on) => {
                      setStatuses((prev) => {
                        const next = new Set(prev);
                        if (on) next.add(s.value); else next.delete(s.value);
                        return next;
                      });
                      markStale();
                    }} />
                ))}
              </div>
              <p className="faint small" style={{ margin: "6px 0 0" }}>
                Tick nothing to print every section of the selected report type.
              </p>
            </div>

            {/* ---- report detail (missing-only vs detailed) ---- */}
            <label className="field">
              <span>
                Report detail
                <span className="hint">
                  {mode === "detailed" ? "per-assignment rows" : "one line per student"}
                </span>
              </span>
              <select className="input" value={mode}
                aria-label="Report detail level"
                onChange={(e) => {
                  setMode(e.target.value);
                  markStale();
                }}>
                {(options?.modes ?? DEFAULT_MODES).map((m) => (
                  <option key={m.value} value={m.value}>{m.label}</option>
                ))}
              </select>
              <p className="faint small" style={{ margin: "6px 0 0" }}>
                Missing only lists each student once with their missing
                assignment codes. Detailed adds a separate row per assignment
                with chapter, lesson and the recorded status.
              </p>
            </label>

            {/* ---- threshold ---- */}
            <label className="field">
              <span>Highlight students scoring below</span>
              <div className="row" style={{ gap: 8 }}>
                <select className="input" value={String(threshold)}
                  aria-label="Low-grade threshold"
                  onChange={(e) => {
                    setThreshold(Number(e.target.value));
                    markStale();
                  }}>
                  {options.threshold_options.map((t) => (
                    <option key={t} value={t}>{t}</option>
                  ))}
                </select>
                <span className="faint small nowrap">grade</span>
              </div>
            </label>
            {options.grade_scale && (
              <p className="faint small" style={{ margin: "0 0 var(--s3)" }}>
                Scale: {options.grade_scale}
              </p>
            )}

            <Button variant="primary" block icon="eye"
              onClick={() => void build()} loading={building}
              disabled={!hasData || needsStudent}>
              Generate preview
            </Button>
            {stale && (
              <p className="rp-stale">
                <Icon name="info" size={13} /> Filters changed — regenerate the
                preview before exporting.
              </p>
            )}
          </div>
        </div>

        {/* ------------------------------------------------ the preview */}
        <div className="rp-preview">
          {building && !model ? (
            <Loading label="Generating report…" />
          ) : !model ? (
            <div className="card">
              <Empty icon="reports" title="No preview yet"
                hint="Choose a report type and assignments, then generate a preview."
                action={<Button variant="primary" icon="eye"
                  onClick={() => void build()}>Generate preview</Button>} />
            </div>
          ) : (
            <>
              <div className="rp-preview-bar">
                <div className="row wrap" style={{ gap: 8 }}>
                  <Badge tone="info" icon="reports">{model.title}</Badge>
                  <Badge tone="mute">{model.class_names}</Badge>
                  <Badge tone="mute">
                    {model.summary.assignments} assignment{model.summary.assignments === 1 ? "" : "s"}
                  </Badge>
                  {model.threshold != null && (
                    <Badge tone="warn">below grade {model.threshold}</Badge>
                  )}
                </div>
                <div className="row wrap" style={{ gap: 6 }}>
                  <span className="faint small">
                    Same data as the exported PDF
                  </span>
                  <Button size="sm" icon="download" onClick={exportCurrent}
                    loading={exporting === "current"} disabled={building || needsStudent}>
                    Export PDF
                  </Button>
                </div>
              </div>

              <div className={classNames("rp-paper", building && "busy")}
                aria-busy={building || undefined}>
                <ReportPaper model={model} />
              </div>
            </>
          )}
        </div>
      </div>
    </>
  );
}

/* ====================================================== the preview paper */

function ReportPaper({ model }: { model: ReportModel }) {
  const b = model.branding;
  const has = (key: string) => model.sections.includes(key);
  const s = model.summary;

  return (
    <article className="rp-doc">
      {/* ---------------------------------------------------- masthead */}
      <header className="rp-masthead">
        <div className="rp-mark" aria-hidden="true">
          {b.monogram || "A"}
        </div>
        {b.quote && <p className="rp-tagline">{b.quote}</p>}
        <p className="rp-center">
          {[b.center, b.year ? `Academic Year ${b.year}` : ""]
            .filter(Boolean).join(" · ")}
        </p>
        {b.teacher_name && (
          <p className="rp-teacher">
            {b.teacher_name}
            {b.teacher_title && <span>{b.teacher_title}</span>}
          </p>
        )}
        <div className="rp-rule" />
      </header>

      <div className="rp-title-block">
        <span className="rp-eyebrow">Math Checker</span>
        <h2 className="rp-title">{model.title}</h2>
        <p className="rp-context">
          {model.class_names} · {model.summary.assignments} assignment(s)
          selected · {model.summary.students} student(s)
        </p>
        <span className="rp-label">For signature</span>
      </div>

      {/* ---------------------------------- administrative note (§19) */}
      {(model.classroom_note?.length ?? 0) > 0 && (
        <p className="rp-note">
          Google Classroom note — no Classroom record found for:{" "}
          {(model.classroom_note ?? [])
            .map((n) => `${n.class_label} — ${n.display_name}`)
            .join("; ")}
          .
        </p>
      )}

      {/* ----------------------------------------------------- summary */}
      {has("summary") && (
        <Section title="Summary"
          hint="Scope of this report and the figures it is built from.">
          <div className="rp-stats">
            <Stat label="Students" value={s.students} />
            <Stat label="Assignments" value={s.assignments} />
            <Stat label="Handed in" value={`${s.submitted}/${s.cells}`} tone="ok" />
            <Stat label="Outstanding" value={s.outstanding ?? s.missing}
              tone={s.outstanding ?? s.missing ? "err" : undefined} />
            <Stat label="Grade 1" value={s.grade_one}
              tone={s.grade_one ? "warn" : undefined} />
            <Stat label="Below threshold" value={s.low_grade}
              tone={s.low_grade ? "warn" : undefined} />
          </div>
        </Section>
      )}

      {/* -------------------------------------------------- assignments */}
      {has("assignments") && (
        <Section title="Assignments included"
          hint="Only the assignments selected for this report are counted.">
          {model.assignments.length === 0 ? (
            <p className="rp-empty">No assignments were selected.</p>
          ) : (
            <Table
              head={<tr><th>Code</th><th>Chapter / lesson</th><th>Topic</th></tr>}
              rows={model.assignments.map((a) => (
                <tr key={a.coursework_id}>
                  <td className="rp-code">{a.code}</td>
                  <td>{a.detail || a.title}</td>
                  <td>{a.topic || a.title}</td>
                </tr>
              ))}
            />
          )}
        </Section>
      )}

      {/* ------------------------------------------------- grade matrix */}
      {has("grades") && model.groups.length > 0 && (
        <Section title="Grade overview"
          hint="Every selected assignment, by class and student.">
          {model.groups.map((g) => (
            <ClassBlock key={g.class_label} label={g.class_label}
              meta={`${g.student_count} student(s)`}>
              <Table
                head={<tr>
                  <th>Student</th>
                  {model.assignments.map((a) => (
                    <th key={a.coursework_id} className="center">{a.code}</th>
                  ))}
                </tr>}
                rows={g.students.map((st) => {
                  // Keyed by the assignment's own row (coursework id), never by
                  // the code alone: CH1L1 and CH2L1 are different lessons and
                  // two "Quiz 3"s are different assignments.
                  const byAssignment = new Map(
                    st.grades.map((x) => [x.coursework_id ?? x.code, x]));
                  return (
                    <tr key={st.student_id}>
                      <td className="rp-name">{st.display_name}</td>
                      {model.assignments.map((a) => {
                        const entry = byAssignment.get(
                          a.coursework_id || a.code);
                        const [cell, tone] = matrixCell(entry, model.threshold);
                        return (
                          <td key={a.coursework_id} className={classNames("center", tone && `tone-${tone}`)}>
                            {cell}
                          </td>
                        );
                      })}
                    </tr>
                  );
                })}
              />
            </ClassBlock>
          ))}
          <p className="rp-note">
            NS = no submission · DR = draft / incomplete · S = submitted, not
            yet marked · – = no data
            {model.threshold != null
              ? ` · marks below grade ${model.threshold} are highlighted.`
              : " · low-grade highlighting is off."}
          </p>
        </Section>
      )}

      {/* ---------------------------------------------------- low grade */}
      {has("low_grade") && (
        <Section title="Low-grade students"
          hint={model.threshold == null
            ? "The low-grade threshold is disabled."
            : `Actual grades strictly below grade ${model.threshold} on the assignment's own grading scale.`}>
          {flat(model.groups, "low_grade").length === 0 ? (
            <p className="rp-empty">
              {model.threshold == null
                ? "No threshold is set, so nothing is highlighted as a low grade."
                : `No student scored below grade ${model.threshold} for the selected assignments.`}
            </p>
          ) : (
            <Table
              head={<tr>
                <th>Student name</th><th>Class</th><th className="center">Grade</th>
                <th className="center">Result</th><th>Assignment</th>
                <th className="center">Chapter</th><th className="center">Lesson</th>
              </tr>}
              rows={flat(model.groups, "low_grade").map((row, i) =>
                row.assignments.map((a) => (
                  <tr key={`${row.student_id}-${a.coursework_id ?? a.code}-${i}`} className="tone-warn">
                    <td className="rp-name">{row.display_name}</td>
                    <td>{row.class_label}</td>
                    <td className="center strong">{a.grade_label || "—"}</td>
                    <td className="center">{a.grade || "—"}</td>
                    <td>{a.label}</td>
                    <td className="center">{a.chapter || "—"}</td>
                    <td className="center">{a.lesson || "—"}</td>
                  </tr>
                )))}
            />
          )}
        </Section>
      )}

      {/* ------------------------------------------------------ grade 1 */}
      {has("grade_one") && (
        <Section title="Grade 1"
          hint="Students whose recorded result is exactly 1 — a scored outcome, kept strictly separate from outstanding work.">
          {flat(model.groups, "grade_one").length === 0 ? (
            <p className="rp-empty">No students received a grade of 1 for the selected assignments.</p>
          ) : (
            <Table
              head={<tr>
                <th>Student name</th><th>Class</th><th>Grade 1 assignments</th>
              </tr>}
              rows={flat(model.groups, "grade_one").map((row) => (
                <tr key={row.student_id} className="tone-warn">
                  <td className="rp-name">{row.display_name}</td>
                  <td>{row.class_label}</td>
                  <td className="strong">
                    {row.assignments.map((a) => a.code).join(", ")}
                  </td>
                </tr>
              ))}
            />
          )}
        </Section>
      )}

      {/* ------------------------------------------------------ missing */}
      {has("missing") && (
        <Section title="Outstanding / missing homework"
          hint="Still outstanding: never handed in, left as a draft, or recorded with a grade of 0. Each student appears once.">
          {flat(model.groups, "missing").length === 0 ? (
            <p className="rp-empty">No outstanding homework for the selected assignments.</p>
          ) : (
            model.groups.filter((g) => g.missing.length > 0).map((g) => (
              <ClassBlock key={g.class_label} label={g.class_label}
                meta={`${g.missing.length} student(s)`}>
                <Table
                  head={<tr><th>Student name</th><th className="center">Missing homework</th></tr>}
                  rows={g.missing.map((m) => (
                    <tr key={m.student_id} className="tone-err">
                      <td className="rp-name">{m.display_name}</td>
                      <td className="center strong">
                        {m.assignments.map((a) => a.code).join(" / ")}
                      </td>
                    </tr>
                  ))}
                />
                {model.detailed !== false && (
                  <Table
                    head={<tr>
                      <th>Student name</th><th>Class</th><th className="center">Assignment</th>
                      <th className="center">Chapter</th><th className="center">Lesson</th>
                      <th>Status</th>
                    </tr>}
                    rows={g.missing.flatMap((m) =>
                      m.assignments.map((a) => (
                        <tr key={`${m.student_id}-${a.coursework_id ?? a.code}`}>
                          <td className="rp-name">{m.display_name}</td>
                          <td>{m.class_label}</td>
                          <td className="center">{a.code}</td>
                          <td className="center">{a.chapter || "—"}</td>
                          <td className="center">{a.lesson || "—"}</td>
                          <td><Badge tone={a.status_label === "Draft" || a.status_label === "Grade 0" ? "warn" : "err"}>
                            {a.status_label || "Not submitted"}
                          </Badge></td>
                        </tr>
                      )))}
                  />
                )}
                <p className="rp-note">
                  {[...new Set(g.missing.flatMap((m) =>
                    m.assignments.map((a) =>
                      a.detail ? `${a.code} = ${a.detail}` : "")))]
                    .filter(Boolean).join("; ")}
                </p>
              </ClassBlock>
            ))
          )}
        </Section>
      )}

      {/* -------------------------------------------------------- draft */}
      {has("draft") && (
        <Section title="Draft / incomplete submissions"
          hint="Work Google Classroom says was opened but never handed in — drafts are counted as outstanding above, and listed here with their original state.">
          {flat(model.groups, "draft").length === 0 ? (
            <p className="rp-empty">No draft submissions for the selected assignments.</p>
          ) : (
            <Table
              head={<tr>
                <th>Student</th><th>Class</th><th className="center">Assignment</th>
                <th>Lesson</th><th>Status</th>
              </tr>}
              rows={flat(model.groups, "draft").flatMap((row) =>
                row.assignments.map((a) => (
                  <tr key={`${row.student_id}-${a.coursework_id ?? a.code}`} className="tone-warn">
                    <td className="rp-name">{row.display_name}</td>
                    <td>{row.class_label}</td>
                    <td className="center">{a.code}</td>
                    <td>{a.label}</td>
                    <td><Badge tone="warn">{a.status_label || "Draft / incomplete"}</Badge></td>
                  </tr>
                )))}
            />
          )}
        </Section>
      )}

      {/* ------------------------------------------------------- status */}
      {has("status") && model.groups.length > 0 && (
        <Section title="Student-by-student status"
          hint="Every student, every selected assignment, with the recorded status and mark.">
          {model.groups.map((g) => (
            <ClassBlock key={g.class_label} label={g.class_label}
              meta={`${g.student_count} student(s)`}>
              <Table
                head={<tr>
                  <th>Student name</th><th>Class</th><th className="center">Assignment</th>
                  <th className="center">Chapter</th><th className="center">Lesson</th>
                  <th>Status</th><th className="center">Grade</th>
                </tr>}                  rows={g.students.flatMap((st) =>
                    st.grades.map((gr) => {
                      const outstanding = gr.tracking === "OUTSTANDING";
                      const info: [string, "ok" | "warn" | "err"] = outstanding
                        ? [gr.tracking_label || "Outstanding",
                           gr.tracking_label === "Draft" || gr.tracking_label === "Grade 0"
                             ? "warn" : "err"]
                        : gr.tracking === "GRADE_ONE"
                        ? [gr.tracking_label || "Grade 1", "warn"]
                        : gr.grade_label ? ["Graded", "ok"] : ["Submitted", "ok"];
                      const [state, tone] = info;
                      return (
                      <tr key={`${st.student_id}-${gr.coursework_id ?? gr.code}`}>
                        <td className="rp-name">{st.display_name}</td>
                        <td>{g.class_label}</td>
                        <td className="center">{gr.code}</td>
                        <td className="center">{gr.chapter || "—"}</td>
                        <td className="center">{gr.lesson || "—"}</td>
                        <td><Badge tone={tone}>{state}</Badge></td>
                        <td className="center">
                          {gr.submitted ? gr.grade : "—"}
                        </td>
                      </tr>
                    );
                  }))}
              />
            </ClassBlock>
          ))}
        </Section>
      )}

      {/* ------------------------------------------------------ sign-off */}
      {has("signoff") && (
        <Section title="Verification / sign-off">
          <p className="rp-note">
            Homework status recorded based on the latest homework check.
            Grades reflect the marks actually recorded for each selected
            assignment.
          </p>
          <div className="rp-sign">
            <div className="rp-sign-left">
              <strong>{b.teacher_name || "Teacher"}</strong>
              <span>{b.teacher_title}</span>
              <span>{b.center}{b.year ? ` · Academic Year ${b.year}` : ""}</span>
            </div>
            <div className="rp-sign-right">
              <span className="rp-sign-cap">For signature</span>
              <span className="rp-sign-line" />
            </div>
          </div>
        </Section>
      )}
    </article>
  );
}

/* -------------------------------------------------------- small pieces */

function isLow(label: string | undefined, threshold: number | null): boolean {
  if (!label || threshold == null) return false;
  const value = Number(label);
  return Number.isFinite(value) && value < threshold;
}

/** One grade-matrix cell — the same short token the exported PDF prints. */
function matrixCell(
  entry: GradeEntry | undefined,
  threshold: number | null,
): [string, "ok" | "warn" | "err" | undefined] {
  if (!entry) return ["–", undefined];
  if (entry.is_draft) return ["DR", "warn"];
  if (!entry.submitted) return ["NS", "err"];
  const grade = entry.grade || "";
  if (!grade || grade === "No grade recorded" || grade === "—") {
    return ["S", "ok"];
  }
  if (isLow(entry.grade_label, threshold)) return [grade, "warn"];
  return [grade, undefined];
}

function flat(groups: ReportGroup[], key: "missing" | "draft" | "low_grade" | "grade_one") {
  return groups.flatMap((g) => g[key]);
}

function Section({ title, hint, children }: {
  title: string; hint?: string; children: React.ReactNode;
}) {
  return (
    <section className="rp-section">
      <div className="rp-section-head">
        <h4>{title}</h4>
        {hint && <p>{hint}</p>}
      </div>
      {children}
    </section>
  );
}

function ClassBlock({ label, meta, children }: {
  label: string; meta?: string; children: React.ReactNode;
}) {
  return (
    <div className="rp-class">
      <div className="rp-class-banner">
        <span>Class {label}</span>
        {meta && <span className="rp-class-meta">{meta}</span>}
      </div>
      {children}
    </div>
  );
}

function Table({ head, rows }: { head: React.ReactNode; rows: React.ReactNode[] }) {
  if (!rows.length) return <p className="rp-empty">Nothing to show.</p>;
  return (
    <div className="rp-table-wrap">
      <table className="rp-table">
        <thead>{head}</thead>
        <tbody>{rows}</tbody>
      </table>
    </div>
  );
}


