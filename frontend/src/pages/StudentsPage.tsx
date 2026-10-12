import { useCallback, useEffect, useMemo, useState } from "react";
import {
  api, classLabel, timeAgo, type BulkDetailsResult, type ClassVocabulary,
  type StudentListItem, type StudentProfile,
} from "../api";
import {
  Badge, Button, DataTable, Empty, Icon, Loading, PageHeader, rowActivate,
  Segmented, Stat, useToast,
} from "../components";

/* ================================================================ status */

const STATUS_TONE: Record<string, "ok" | "err" | "warn" | "info" | "mute"> = {
  NOT_STARTED: "mute", CREATED: "info", SUBMITTED: "ok", SUBMITTED_LATE: "warn",
  RETURNED: "ok", RECLAIMED: "err", EDITED_AFTER_SUBMISSION: "warn",
  MISSING: "mute", PROCESSING: "info", CORRECTED: "ok", GRADED: "ok",
  FAILED: "err", INVALID: "err", UNKNOWN: "mute",
};

const STATUS_ICON: Record<string, string> = {
  NOT_STARTED: "○", CREATED: "◇", SUBMITTED: "✓", SUBMITTED_LATE: "⏱",
  RETURNED: "↩", RECLAIMED: "↺", EDITED_AFTER_SUBMISSION: "✎", MISSING: "∅",
  PROCESSING: "⚙", CORRECTED: "★", GRADED: "🏅", FAILED: "✗", INVALID: "⛔",
  UNKNOWN: "?",
};

function StatusPill({ status, label }: { status: string; label: string }) {
  return (
    <span className={`badge ${STATUS_TONE[status] ?? "mute"}`}>
      <span className="dot" />
      {STATUS_ICON[status] ?? "•"} {label}
    </span>
  );
}

function SourceTag({ text }: { text: string }) {
  return (
    <span className="faint small" style={{ display: "block", marginTop: 2 }}>
      SOURCE: {text}
    </span>
  );
}

function initials(name: string): string {
  const parts = name.trim().split(/\s+/).filter(Boolean);
  if (!parts.length) return "?";
  return (parts[0][0] + (parts[1]?.[0] ?? "")).toUpperCase();
}

/* ==================================================== class / grade input */

/**
 * A free-text class input with the teacher's own classes suggested.
 *
 * Deliberately NOT a locked <select>: a school that says "11 Computer
 * Science" or "Year 12" must be able to type it, so the list is a datalist and
 * the value is always the teacher's to choose.
 */
function ClassInput({
  value, onChange, options, placeholder = "10A", id, ariaLabel,
}: {
  value: string;
  onChange: (next: string) => void;
  options: string[];
  placeholder?: string;
  id?: string;
  /** Accessible name for call sites that do not wrap this in a <label>. */
  ariaLabel?: string;
}) {
  const listId = id ? `${id}-classes` : undefined;
  return (
    <>
      <input
        id={id}
        className="input"
        list={listId}
        value={value}
        placeholder={placeholder}
        autoComplete="off"
        aria-label={ariaLabel}
        onChange={(e) => onChange(e.target.value)}
      />
      <datalist id={listId}>
        {options.map((c) => <option key={c} value={c} />)}
      </datalist>
    </>
  );
}

/* ================================================================ page */

interface StudentsPageProps {
  /** Routed student (#/students/{id}) — the URL is the single source of
   *  truth so profiles are bookmarkable and openable from anywhere. */
  studentId?: string | null;
  onNavigate?: (hash: string) => void;
}

export default function StudentsPage({
  studentId = null,
  onNavigate,
}: StudentsPageProps) {
  const toast = useToast();
  const selectedId = studentId;
  const [students, setStudents] = useState<StudentListItem[] | null>(null);
  const [search, setSearch] = useState("");
  const [sort, setSort] = useState<"name" | "score" | "recent">("name");
  const [error, setError] = useState<string | null>(null);

  // profile state
  const [profile, setProfile] = useState<StudentProfile | null>(null);
  const [profileLoading, setProfileLoading] = useState(false);
  const [editingInfo, setEditingInfo] = useState(false);
  const [nickDraft, setNickDraft] = useState("");
  const [classDraft, setClassDraft] = useState("");

  // bulk state
  const [classes, setClasses] = useState<string[]>([]);
  const [checked, setChecked] = useState<Set<string>>(new Set());
  const [bulkClass, setBulkClass] = useState("");
  const [bulkBusy, setBulkBusy] = useState(false);
  const [historySort, setHistorySort] = useState<"name" | "date" | "classroom" | "mc" | "status">("date");
  const [historyFilter, setHistoryFilter] = useState<"all" | "submitted" | "missing" | "late" | "corrected">("all");

  const loadList = useCallback(() => {
    api<{ students: StudentListItem[] }>("/students")
      .then((r) => setStudents(r.students))
      .catch((e) => setError(e.message));
  }, []);

  useEffect(() => { loadList(); }, [loadList]);

  useEffect(() => {
    api<ClassVocabulary>("/students/classes")
      .then((r) => setClasses(r.classes ?? []))
      .catch(() => setClasses([]));
  }, []);

  // Load the profile for the CURRENT route (deep links, search, other pages).
  useEffect(() => {
    if (!studentId) {
      setProfile(null);
      setProfileLoading(false);
      setEditingInfo(false);
      return;
    }
    let cancelled = false;
    setProfile(null);
    setProfileLoading(true);
    setEditingInfo(false);
        api<StudentProfile>(`/students/${encodeURIComponent(studentId)}`)
      .then((p) => {
        if (cancelled) return;
        setProfile(p);
        setNickDraft(p.nickname ?? "");
        setClassDraft(p.class_name ?? "");
      })
      .catch((e) => { if (!cancelled) setError((e as Error).message); })
      .finally(() => { if (!cancelled) setProfileLoading(false); });
    return () => { cancelled = true; };
  }, [studentId]);

  const openProfile = useCallback((id: string) => {
    if (onNavigate) onNavigate(`#/students/${encodeURIComponent(id)}`);
  }, [onNavigate]);

  const closeProfile = useCallback(() => {
    if (onNavigate) onNavigate("#/students");
  }, [onNavigate]);

  // NOTE: the nickname-only endpoint (PUT /students/{id}/nickname) is kept for
  // compatibility, but the UI has ONE Save for the whole Student Information
  // block, because nickname and class are the same kind of local metadata.

  /** One Save for the whole "Student Information" block (§3). */
  async function saveInfo() {
    if (!selectedId) return;
    try {
      const updated = await api<StudentProfile>(
        `/students/${encodeURIComponent(selectedId)}/details`,
        {
          method: "PUT",
          body: JSON.stringify({ nickname: nickDraft, class_name: classDraft }),
        });
      setProfile(updated);
      setNickDraft(updated.nickname ?? "");
      setClassDraft(updated.class_name ?? "");
      setEditingInfo(false);
      toast("ok", "Saved — local metadata only, Google Classroom untouched");
      loadList();
    } catch (e) {
      toast("err", (e as Error).message);
    }
  }

  const toggleChecked = useCallback((id: string) => {
    setChecked((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id); else next.add(id);
      return next;
    });
  }, []);

  const toggleAllVisible = useCallback((ids: string[]) => {
    setChecked((prev) => {
      const next = new Set(prev);
      const allIn = ids.every((id) => next.has(id));
      ids.forEach((id) => (allIn ? next.delete(id) : next.add(id)));
      return next;
    });
  }, []);

  /** Assign one class to every ticked student (§5). */
  async function applyBulkClass() {
    const ids = [...checked];
    if (!ids.length) {
      toast("warn", "Tick at least one student first.");
      return;
    }
    setBulkBusy(true);
    try {
      const res = await api<BulkDetailsResult>("/students/bulk-details", {
        method: "POST",
        body: JSON.stringify({ student_ids: ids, class_name: bulkClass }),
      });
      const done = res.updated.length;
      toast(
        done ? "ok" : "warn",
        `${done} student${done === 1 ? "" : "s"} set to ${
          res.class_name || "no class"}`
        + (res.skipped.length ? ` · ${res.skipped.length} skipped` : ""),
      );
      setChecked(new Set());
      setBulkClass("");
      loadList();
      if (selectedId && res.profiles.some((p) => p.student_id === selectedId)) {
        api<StudentProfile>(`/students/${encodeURIComponent(selectedId)}`)
          .then(setProfile)
          .catch(() => { /* the list is already refreshed */ });
      }
    } catch (e) {
      toast("err", (e as Error).message);
    } finally {
      setBulkBusy(false);
    }
  }

  const filtered = useMemo(() => {
    if (!students) return [];
    const q = search.trim().toLowerCase();
    let out = students.filter((s) =>
      !q
      || (s.display_name ?? "").toLowerCase().includes(q)
      || (s.official_name ?? "").toLowerCase().includes(q)
      || (s.nickname ?? "").toLowerCase().includes(q)
      || (s.class_name ?? "").toLowerCase().includes(q)
      || (s.email ?? "").toLowerCase().includes(q));
    if (sort === "name") {
      out = [...out].sort((a, b) => a.display_name.localeCompare(b.display_name));
    } else if (sort === "score") {
      const pct = (s: StudentListItem) =>
        s.total_available ? (s.total_earned / s.total_available) * 100 : -1;
      out = [...out].sort((a, b) => pct(b) - pct(a));
    } else {
      out = [...out].sort((a, b) => b.corrections - a.corrections);
    }
    return out;
  }, [students, search, sort]);

  /* Assignment history: filter + sort for the selected student (§10). */
  const history = useMemo(() => {
    if (!profile) return [];
    let out = [...profile.classroom.assignments];
    if (historyFilter === "submitted") {
      out = out.filter((a) => !["NOT_STARTED", "CREATED", "MISSING", "RECLAIMED"]
        .includes(a.status));
    } else if (historyFilter === "missing") {
      // Outstanding = never handed in, a draft, or a recorded grade of 0 —
      // the same centralized rule the reports use (§31, §48).
      const notHanded = ["NOT_STARTED", "CREATED", "MISSING", "RECLAIMED"];
      out = out.filter((a) => notHanded.includes(a.status)
        || (a.result != null && a.result.earned === 0)
        || a.assigned_grade === 0);
    } else if (historyFilter === "late") {
      out = out.filter((a) => a.late);
    } else if (historyFilter === "corrected") {
      out = out.filter((a) => a.result != null);
    }
    switch (historySort) {
      case "name": out.sort((a, b) => (a.title ?? "").localeCompare(b.title ?? "")); break;
      case "date": out.sort((a, b) => String(b.update_time ?? b.creation_time ?? "")
        .localeCompare(String(a.update_time ?? a.creation_time ?? ""))); break;
      case "classroom": out.sort((a, b) =>
        ((b.assigned_grade != null && b.max_points ? b.assigned_grade / b.max_points : -1)
         - (a.assigned_grade != null && a.max_points ? a.assigned_grade / a.max_points : -1))); break;
      case "mc": out.sort((a, b) =>
        ((b.result?.percentage ?? -1) - (a.result?.percentage ?? -1))); break;
      case "status": out.sort((a, b) => a.status.localeCompare(b.status)); break;
    }
    return out;
  }, [profile, historySort, historyFilter]);

  /* ---------------------------------------------------------- profile view */
  if (selectedId) {
    const p = profile;

    /* Derived current status (§12): latest submission / grade / correction,
       computed ONLY from the loaded profile — never fabricated. */
    const byRecent = p ? [...p.classroom.assignments].sort((a, b) =>
      String(b.update_time ?? b.creation_time ?? "")
        .localeCompare(String(a.update_time ?? a.creation_time ?? ""))) : [];
    const latestAssignment = byRecent[0] ?? null;
    const latestGrade = byRecent.find((a) => a.assigned_grade != null) ?? null;
    const latestCorrection = p?.corrections.recent[0] ?? null;
    const failedJobs = p?.corrections.jobs.filter((j) => j.status === "FAILED") ?? [];
    return (
      <>
        <PageHeader
          title={p?.display_name ?? "Student"}
          sub={p && p.nickname && p.official_name
            ? `${classLabel(p)} · official Classroom name: ${p.official_name}`
            : "Personal dashboard — Classroom truth and local corrections, kept apart."}>
          <button className="btn sm ghost" onClick={closeProfile}>
            ← All students
          </button>
        </PageHeader>

        {error && <div className="notice err" style={{ marginBottom: 12 }}>⚠ {error}</div>}
        {profileLoading && !p && <Loading label="Loading profile…" />}

        {p && (
          <>
            {/* ---- Student Information: nickname + class, one Save (§3) ---- */}
            <div className="card">
              <div className="row between wrap" style={{ gap: 10 }}>
                <h3 style={{ margin: 0 }}>Student Information</h3>
                <span className="hint">local metadata · never sent to Google</span>
              </div>
              {editingInfo ? (
                <div className="stack" style={{ marginTop: 12, maxWidth: 520 }}>
                  <label className="stack small" style={{ gap: 4 }}>
                    <span className="faint">Google Classroom name (read-only)</span>
                    <input className="input" value={p.official_name ?? "—"}
                      disabled readOnly />
                  </label>
                  <label className="stack small" style={{ gap: 4 }}>
                    <span className="faint">Nickname</span>
                    <input className="input" value={nickDraft} autoFocus
                      placeholder="e.g. Mo"
                      onChange={(e) => setNickDraft(e.target.value)}
                      onKeyDown={(e) => { if (e.key === "Enter") saveInfo(); }} />
                  </label>
                  <label className="stack small" style={{ gap: 4 }}>
                    <span className="faint">Class / Grade</span>
                    <ClassInput id="profile-class" value={classDraft}
                      onChange={setClassDraft} options={classes}
                      placeholder="10A — or type your own" />
                  </label>
                  <div className="row" style={{ gap: 8 }}>
                    <Button size="sm" icon="check" onClick={saveInfo}>Save</Button>
                    <button className="btn sm ghost"
                      onClick={() => {
                        setEditingInfo(false);
                        setNickDraft(p.nickname ?? "");
                        setClassDraft(p.class_name ?? "");
                      }}>
                      Cancel
                    </button>
                  </div>
                </div>
              ) : (
                <div className="stack small" style={{ marginTop: 10 }}>
                  <div>
                    <span className="faint">Google Classroom name: </span>
                    {p.official_name || "—"}
                  </div>
                  <div>
                    <span className="faint">Nickname: </span>
                    {p.nickname || <span className="faint">Not set</span>}
                  </div>
                  <div>
                    <span className="faint">Class / Grade: </span>
                    <strong>{classLabel(p)}</strong>
                    {!p.class_name && p.course && (
                      <span className="faint">
                        {" "}· taken from the Classroom course; set one to override
                      </span>
                    )}
                  </div>
                  <div>
                    <span className="faint">Display name: </span>
                    <span className="mono">{p.display_name}</span>
                    <span className="faint">
                      {" "}(nickname if set, otherwise the Classroom name)
                    </span>
                  </div>
                </div>
              )}
            </div>

            {/* ---- identity hero ---- */}
            <div className="card">
              <div className="row between wrap" style={{ gap: 16 }}>
                <div className="row" style={{ gap: 14 }}>
                  <div style={{
                    width: 64, height: 64, borderRadius: 18, display: "grid",
                    placeItems: "center", fontWeight: 800, fontSize: 22,
                    background: "linear-gradient(135deg, var(--accent), #7c5cff)",
                    color: "#fff", boxShadow: "0 8px 24px rgba(124,92,255,.35)",
                  }}>
                    {initials(p.display_name)}
                  </div>
                  <div>
                    <div style={{ fontSize: 22, fontWeight: 700 }}>{p.display_name}</div>
                    <div className="small faint">
                      Nickname: {p.nickname ? `“${p.nickname}”` : "Not set"}
                      {p.official_name ? ` · Google Classroom: ${p.official_name}` : ""}
                    </div>
                    <div className="small muted">
                      Class: <strong>{classLabel(p)}</strong>
                      {!p.class_name && p.course
                        ? <span className="faint"> (from Classroom course)</span>
                        : null}
                      {' · '}Google Classroom ID: <span className="mono">{p.student_id}</span>
                    </div>
                    {p.email && <div className="small muted">{p.email}</div>}
                    <div className="row wrap" style={{ gap: 6, marginTop: 4 }}>
                      <Badge tone={p.classroom.available ? "ok" : "warn"}>
                        {p.classroom.available ? "Classroom data connected" : "Classroom offline — sync needed"}
                      </Badge>
                      <SourceTag text={p.sources.identity} />
                    </div>
                  </div>
                </div>
                <div className="row wrap" style={{ gap: 8 }}>
                  {editingInfo ? (
                    <>
                      <button className="btn primary sm" onClick={saveInfo}>Save</button>
                      <button className="btn sm ghost"
                        onClick={() => {
                          setEditingInfo(false);
                          setNickDraft(p.nickname ?? "");
                          setClassDraft(p.class_name ?? "");
                        }}>
                        Cancel
                      </button>
                    </>
                  ) : (
                    <button className="btn sm" onClick={() => setEditingInfo(true)}>
                      {p.nickname || p.class_name
                        ? "Edit student information"
                        : "Set nickname & class"}
                    </button>
                  )}
                  <a className="btn sm ghost"
                    href={`/api/students/${encodeURIComponent(p.student_id)}/report?format=pdf`}>
                    Export report ▾
                  </a>
                </div>
              </div>
            </div>

            {/* ---- current status (§12) ---- */}
            <div className="card">
              <h3 style={{ marginTop: 0 }}>
                Current status
                <span className="hint">recent assignment, latest grades and correction activity</span>
              </h3>
              <div className="grid grid-4">
              <div className="stat">
                <div className="label">Recent assignment</div>
                <div className="value" style={{ fontSize: 14 }}>
                  {latestAssignment
                    ? (latestAssignment.title ?? latestAssignment.coursework_id)
                    : p.classroom.available ? "None yet" : "Sync needed"}
                </div>
                <SourceTag text={latestAssignment ? latestAssignment.status_label : p.sources.submission_status} />
              </div>
              <div className="stat">
                <div className="label">Latest Classroom grade</div>
                <div className="value">
                  {latestGrade
                    ? `${latestGrade.assigned_grade}/${latestGrade.max_points ?? "?"}`
                    : "Not graded"}
                </div>
                <SourceTag text={latestGrade ? `Google Classroom · ${latestGrade.title ?? "assignment"}` : "Google Classroom — no grade returned yet"} />
              </div>
              <div className="stat">
                <div className="label">Latest Math Checker grade</div>
                <div className="value">
                  {latestCorrection
                    ? `${latestCorrection.earned}/${latestCorrection.available}`
                    : "Not processed"}
                </div>
                <SourceTag text={latestCorrection ? `AI correction · ${timeAgo(latestCorrection.created_at)}` : p.sources.scores} />
              </div>
              <div className="stat">
                <div className="label">Correction activity</div>
                <div className="value">
                  {p.corrections.total > 0
                    ? `${p.corrections.total} done${p.corrections.pending ? ` · ${p.corrections.pending} queued` : ""}`
                    : "No activity"}
                </div>
                <SourceTag text={p.corrections.last_corrected_at
                  ? `Last corrected ${timeAgo(p.corrections.last_corrected_at)}`
                  : "Local job queue"} />
              </div>
              </div>
            </div>

            {/* ---- stat tiles (each labelled with its source) ---- */}
            <div className="grid grid-4">
              <div className="stat">
                <div className="label">Assignments</div>
                <div className="value">{p.classroom.available ? p.classroom.assignments_total : "?"}</div>
                <SourceTag text={p.classroom.available ? p.sources.submission_status : "Unavailable — sync needed"} />
              </div>
              <div className="stat">
                <div className="label">Submitted</div>
                <div className="value ok">
                  {p.classroom.available
                    ? `${p.classroom.submitted}/${p.classroom.assignments_total}`
                    : "—"}
                </div>
                <SourceTag text={p.classroom.available ? p.sources.submission_status : "no Classroom data"} />
              </div>
              <div className="stat">
                <div className="label">Not submitted</div>
                <div className="value">
                  {p.classroom.available ? p.classroom.not_submitted : "—"}
                </div>
                <SourceTag text={p.sources.submission_status} />
              </div>
              <div className="stat">
                <div className="label">Corrections</div>
                <div className="value">{p.corrections.total}</div>
                <SourceTag text={p.sources.scores} />
              </div>
            </div>

            <div className="grid grid-4">
              <div className="stat">
                <div className="label">Total marks</div>
                <div className="value">
                  {p.corrections.total
                    ? `${p.corrections.total_earned}/${p.corrections.total_available}`
                    : "—"}
                </div>
                <SourceTag text={p.sources.scores} />
              </div>
              <div className="stat">
                <div className="label">Overall</div>
                <div className={`value ${p.corrections.overall_percentage != null && p.corrections.overall_percentage >= 50 ? "ok" : ""}`}>
                  {p.corrections.overall_percentage != null
                    ? `${p.corrections.overall_percentage}%` : "—"}
                </div>
                <SourceTag text={p.sources.correction_detail} />
              </div>
              <div className="stat">
                <div className="label">Pending</div>
                <div className="value">{p.corrections.pending}</div>
                <SourceTag text="Local job queue" />
              </div>
              <div className="stat">
                <div className="label">Needs review</div>
                <div className={`value ${p.corrections.needs_review ? "warn" : ""}`}>
                  {p.corrections.needs_review}
                </div>
                <SourceTag text="AI correction verification" />
              </div>
            </div>

            {!p.classroom.available && (
              <div className="notice warn">
                No Google Classroom data for this student yet — submission numbers are
                hidden rather than guessed. Open Classroom, pick the class/assignment
                and press “Sync Google Classroom”.
              </div>
            )}

            {/* ---- performance (§9) ---- */}
            <div className="card">
              <h3 style={{ marginTop: 0 }}>
                Performance
                <span className="hint">Classroom grades and Math Checker marks stay separate</span>
              </h3>
              <div className="grid grid-4">
                <div className="stat">
                  <div className="label">Average Classroom grade</div>
                  <div className="value">
                    {p.performance.classroom_average != null
                      ? `${p.performance.classroom_average}%` : "Not graded"}
                  </div>
                  <SourceTag text={`Google Classroom · ${p.performance.classroom_graded} graded, ${p.performance.classroom_not_graded} not graded`} />
                </div>
                <div className="stat">
                  <div className="label">Highest / lowest (Classroom)</div>
                  <div className="value">
                    {p.performance.classroom_highest != null
                      ? `${p.performance.classroom_highest}% / ${p.performance.classroom_lowest}%`
                      : "Not available"}
                  </div>
                  <SourceTag text="Google Classroom" />
                </div>
                <div className="stat">
                  <div className="label">Average Math Checker</div>
                  <div className="value">
                    {p.performance.mc_average != null
                      ? `${p.performance.mc_average}%` : "Not processed"}
                  </div>
                  <SourceTag text={p.sources.scores} />
                </div>
                <div className="stat">
                  <div className="label">Highest / lowest (Math Checker)</div>
                  <div className="value">
                    {p.performance.mc_highest != null
                      ? `${p.performance.mc_highest}% / ${p.performance.mc_lowest}%`
                      : "Not available"}
                  </div>
                  <SourceTag text="AI correction result" />
                </div>
              </div>
              <div className="grid grid-4">
                <div className="stat compact">
                  <div className="label">Total marks (Math Checker)</div>
                  <div className="value">
                    {p.performance.total_available > 0
                      ? `${p.performance.total_earned}/${p.performance.total_available}`
                      : "Not available"}
                  </div>
                  <SourceTag text={p.sources.scores} />
                </div>
                <div className="stat compact">
                  <div className="label">Overall percentage</div>
                  <div className="value">
                    {p.performance.overall_percentage != null
                      ? `${p.performance.overall_percentage}%` : "Not available"}
                  </div>
                  <SourceTag text="Math Checker totals" />
                </div>
                <div className="stat compact">
                  <div className="label">Zeros</div>
                  <div className="value">
                    {p.performance.classroom_zeros} Classroom · {p.performance.mc_zeros} Math Checker
                  </div>
                  <SourceTag text="Actual 0 marks only — missing work is never counted as zero" />
                </div>
                <div className="stat compact">
                  <div className="label">Outstanding assignments</div>
                  <div className="value warn">{p.performance.missing}</div>
                  <SourceTag text="Google Classroom — missing, draft or grade 0" />
                </div>
              </div>
            </div>

            {/* ---- submission behavior (§9): secondary detail, collapsed ---- */}
            <div className="card">
              <details className="expander">
                <summary>
                  <Icon name="students" size={15} />
                  Submission behavior
                  <span className="hint">on-time vs late vs outstanding</span>
                </summary>
                <div className="expander-body">
                <div className="grid grid-4">
                <div className="stat">
                  <div className="label">On-time submissions</div>
                  <div className="value ok">{p.behavior.on_time}</div>
                </div>
                <div className="stat">
                  <div className="label">Late submissions</div>
                  <div className={`value ${p.behavior.late > 0 ? "warn" : ""}`}>{p.behavior.late}</div>
                </div>
                <div className="stat">
                  <div className="label">Outstanding (incl. draft)</div>
                  <div className={`value ${p.behavior.missing > 0 ? "warn" : ""}`}>{p.behavior.missing}</div>
                </div>
                <div className="stat">
                  <div className="label">Draft (opened, not submitted)</div>
                  <div className="value">{p.behavior.draft}</div>
                </div>
              </div>
              <div className="grid grid-4">
                <Stat compact label="Turned in" value={p.behavior.turned_in} />
                <Stat compact label="Returned / graded" value={p.behavior.returned} />
                <Stat compact label="Corrected by Math Checker"
                  value={p.performance.corrected} tone="ok" />
                <Stat compact label="Correction failures"
                  value={p.corrections.failed}
                  tone={p.corrections.failed > 0 ? "err" : undefined} />
              </div>
                </div>
              </details>
            </div>

            {/* ---- assignments: full history (§10): second disclosure level ---- */}
            <div className="card">
              <details className="expander">
                <summary>
                  <Icon name="assignments" size={15} />
                  Assignment history
                  <span className="hint">submission state from Google Classroom</span>
                  <Badge tone="mute">{p.classroom.assignments.length}</Badge>
                  <span className="faint small" style={{ marginLeft: "auto" }}>View all</span>
                </summary>
                <div className="expander-body">
                <div className="row between wrap" style={{ marginBottom: 10, gap: 8 }}>
                <span className="faint small">Filter &amp; sort</span>
                <div className="row wrap">
                  <Segmented<"all" | "submitted" | "missing" | "late" | "corrected">
                    value={historyFilter} onChange={setHistoryFilter}
                    ariaLabel="Filter assignment history"
                    options={[
                      { value: "all", label: "All" },
                      { value: "submitted", label: "Submitted" },
                      { value: "missing", label: "Outstanding" },
                      { value: "late", label: "Late" },
                      { value: "corrected", label: "Corrected" },
                    ]} />
                  <Segmented<"date" | "name" | "classroom" | "mc" | "status">
                    value={historySort} onChange={setHistorySort}
                    ariaLabel="Sort assignment history"
                    options={[
                      { value: "date", label: "Date" },
                      { value: "name", label: "Name" },
                      { value: "classroom", label: "Classroom" },
                      { value: "mc", label: "Math Checker" },
                      { value: "status", label: "Status" },
                    ]} />
                </div>
              </div>
              {p.classroom.assignments.length === 0 ? (
                <Empty icon="📭" title="No assignment data"
                  hint="Sync an assignment in the Classroom page to see real submission states here." />
              ) : history.length === 0 ? (
                <Empty icon="🔍" title="Nothing matches this filter"
                  hint="Try a different filter above." />
              ) : (
                <DataTable head={
                  <tr>
                    <th>Assignment</th><th>Date</th><th>Submission</th>
                    <th>Late?</th><th>Returned?</th>
                    <th className="num">Classroom grade</th>
                    <th className="num">Math Checker</th>
                    <th className="num">%</th><th>Correction</th><th /></tr>
                }>
                  {history.map((a) => {
                    const jobState = a.job?.status ?? null;
                    const correctionLabel = a.result
                      ? "Corrected"
                      : jobState === "FAILED" ? "Correction failed"
                      : jobState && ["QUEUED", "PREPARING", "DOWNLOADING", "UPLOADING",
                          "MARKING", "VERIFYING", "SAVING", "WAITING_FOR_API",
                          "RETRYING", "STARTING", "PROCESSING"].includes(jobState)
                        ? "Processing"
                        : "Not processed";
                    const returned = a.status === "RETURNED" || a.status === "GRADED";
                    return (
                      <tr key={a.submission_id}>
                        <td data-label="Assignment">
                          {a.title ?? a.coursework_id}
                          {a.due_date && <div className="faint small">Due: {a.due_date}</div>}
                        </td>
                        <td data-label="Date" className="small">
                          {a.update_time
                            ? String(a.update_time).replace("T", " ").slice(0, 16)
                            : <span className="faint">Not available</span>}
                        </td>
                        <td data-label="Submission">
                          <StatusPill status={a.status} label={a.status_label} />
                          <div className="faint small mono">{a.raw_state ?? "—"}</div>
                        </td>
                        <td data-label="Late?">{a.late ? "⚠ Yes" : "No"}</td>
                        <td data-label="Returned?">{returned ? "✓ Yes" : "No"}</td>
                        <td data-label="Classroom grade" className="num">
                          {a.assigned_grade != null
                            ? `${a.assigned_grade}/${a.max_points ?? "?"}`
                            : <span className="faint">Not graded</span>}
                        </td>
                        <td data-label="Math Checker" className="num">
                          {a.result
                            ? `${a.result.earned}/${a.result.available}`
                            : <span className="faint">Not available</span>}
                        </td>
                        <td data-label="%" className="num">
                          {a.result
                            ? `${a.result.percentage.toFixed(1)}%`
                            : <span className="faint">—</span>}
                        </td>
                        <td data-label="Correction">
                          <Badge tone={correctionLabel === "Corrected" ? "ok"
                            : correctionLabel === "Correction failed" ? "err"
                            : correctionLabel === "Processing" ? "info" : "mute"}>
                            {correctionLabel === "Corrected" ? "✓ "
                              : correctionLabel === "Correction failed" ? "! "
                              : correctionLabel === "Processing" ? "⚙ " : "○ "}
                            {correctionLabel}
                          </Badge>
                          {a.job?.error && (
                            <div className="notice err small" style={{ marginTop: 4 }}>{a.job.error}</div>
                          )}
                        </td>
                        <td data-label="Report">
                          {a.result ? (
                            <a className="btn secondary sm"
                              href={`/api/students/${encodeURIComponent(p.student_id)}`
                                + `/assignments/${encodeURIComponent(a.coursework_id)}`
                                + `/report?detail=detailed`}
                              data-tooltip="Detailed correction report for this assignment">
                              <Icon name="download" size={13} /> PDF
                            </a>
                          ) : (
                            <Button size="sm" variant="ghost" icon="chart"
                              data-tooltip="Open the results page"
                              onClick={() => { if (onNavigate) onNavigate("#/results"); }}>
                              View
                            </Button>
                          )}
                        </td>
                      </tr>
                    );
                  })}
                </DataTable>
              )}
                </div>
              </details>
            </div>

            {/* ---- recent activity (§12): latest submissions/grades/failures ---- */}
            <div className="card">
              <h3 style={{ marginTop: 0 }}>
                Recent activity
                <span className="hint">latest submissions, grades, corrections and any warnings</span>
              </h3>
              {p.classroom.assignments.length === 0 && p.corrections.recent.length === 0
                && failedJobs.length === 0 ? (
                <Empty icon="🕒" title="No activity yet"
                  hint="Sync Google Classroom and run a correction — real events appear here." />
              ) : (
                <div className="activity-feed">
                  {latestAssignment && (
                    <div className="activity-item">
                      <span className="activity-dot" />
                      <div>
                        <strong>{latestAssignment.title ?? latestAssignment.coursework_id}</strong>
                        {' — '}{latestAssignment.status_label}
                        {latestAssignment.assigned_grade != null
                          ? ` · graded ${latestAssignment.assigned_grade}/${latestAssignment.max_points ?? "?"}`
                          : ""}
                        <div className="faint small">
                          Submission updated {latestAssignment.update_time
                            ? String(latestAssignment.update_time).replace("T", " ").slice(0, 16)
                            : "at an unknown time"}
                        </div>
                      </div>
                    </div>
                  )}
                  {latestCorrection && (
                    <div className="activity-item">
                      <span className="activity-dot" />
                      <div>
                        Corrected: {latestCorrection.earned}/{latestCorrection.available}
                        {' '}({latestCorrection.percentage.toFixed(1)}%)
                        <div className="faint small">{timeAgo(latestCorrection.created_at)}
                          {latestCorrection.marking_model ? ` · ${latestCorrection.marking_model}` : ""}</div>
                      </div>
                    </div>
                  )}
                  {failedJobs.slice(0, 3).map((j) => (
                    <div className="activity-item warn" key={j.id}>
                      <span className="activity-dot" />
                      <div>
                        Correction job failed
                        <div className="faint small">
                          {j.error ?? "unknown error"} · {timeAgo(j.finished_at ?? j.created_at)}
                        </div>
                      </div>
                    </div>
                  ))}
                </div>
              )}
            </div>

            {/* ---- corrections (local truth) ---- */}
            <div className="card">
              <h3 style={{ marginTop: 0 }}>
                Recent corrections
                <span className="hint">marks from this app's AI correction</span>
              </h3>
              {p.corrections.recent.length === 0 ? (
                <Empty icon="📐" title="No corrections yet"
                  hint="Corrections appear here once this student's work has been marked." />
              ) : (
                <DataTable head={
                  <tr>
                    <th>When</th><th className="num">Score</th><th className="num">%</th>
                    <th>Grade band</th><th>Status</th><th>Model</th>
                  </tr>
                }>
                  {p.corrections.recent.map((c) => (
                    <tr key={c.id}>
                      <td data-label="When" className="small">{timeAgo(c.created_at)}</td>
                      <td data-label="Score" className="num mono">
                        <a className="student-link"
                          href={`#/results/${encodeURIComponent(c.id)}`}
                          data-tooltip="Open the full correction"
                          onClick={(e) => {
                            if (onNavigate) {
                              e.preventDefault();
                              onNavigate(`#/results/${encodeURIComponent(c.id)}`);
                            }
                          }}>
                          {c.earned}/{c.available}
                        </a>
                      </td>
                      <td data-label="%" className="num">{c.percentage.toFixed(1)}%</td>
                      <td data-label="Grade band">{c.provisional_grade ?? "—"}</td>
                      <td data-label="Status">
                        <Badge tone={c.status === "REVIEW" ? "warn" : "ok"}>
                          {c.status === "REVIEW"
                            ? `REVIEW (${c.review_questions.length} question(s))`
                            : (c.status ?? "VALID")}
                        </Badge>
                      </td>
                      <td data-label="Model" className="small faint mono">
                        {c.marking_model ?? "—"}
                      </td>
                    </tr>
                  ))}
                </DataTable>
              )}
              {p.corrections.failed > 0 && (
                <div className="notice err small" style={{ marginTop: 10 }}>
                  {p.corrections.failed} correction job(s) failed — technical failures,
                  not student scores. Retry them from the Corrections page.
                </div>
              )}
            </div>
          </>
        )}
      </>
    );
  }

  /* ------------------------------------------------------------- list view */
  return (
    <>
      <PageHeader title="Students"
        sub="Personal dashboards built from Google Classroom truth plus this app's corrections — nothing inferred." />

      {error && <div className="notice err" style={{ marginBottom: 12 }}>⚠ {error}</div>}

      <div className="card">
        <div className="row wrap" style={{ marginBottom: 12 }}>
          <input className="input" style={{ maxWidth: 300 }}
            placeholder="Search name, nickname, class, email…"
            value={search} onChange={(e) => setSearch(e.target.value)}
            aria-label="Search students" />
          <Segmented<"name" | "score" | "recent"> value={sort} onChange={setSort}
            ariaLabel="Sort students"
            options={[
              { value: "name", label: "Name" },
              { value: "score", label: "Score" },
              { value: "recent", label: "Corrections" },
            ]} />
        </div>

        {!students && <Loading label="Loading students…" />}
        {students && filtered.length === 0 && (
          <Empty icon="🎓"
            title={students.length === 0 ? "No students yet" : "Nothing matches"}
            hint={students.length === 0
              ? "Connect Google Classroom and sync an assignment — real students appear here."
              : "Try a different search."} />
        )}

        {filtered.length > 0 && (
          <>
            {/* ---- bulk bar: assign a class to a whole cohort at once ---- */}
            <div className="row wrap between"
              style={{ gap: 8, marginBottom: 10, alignItems: "center" }}>
              <label className="row small" style={{ gap: 6, cursor: "pointer" }}>
                <input
                  type="checkbox"
                  checked={checked.size > 0
                    && filtered.every((s) => checked.has(s.student_id))}
                  onChange={() => toggleAllVisible(filtered.map((s) => s.student_id))}
                  aria-label="Select all visible students"
                />
                <span className="faint">
                  {checked.size
                    ? `${checked.size} selected`
                    : "Select students"}
                </span>
              </label>
              {checked.size > 0 && (
                <div className="row wrap" style={{ gap: 8, alignItems: "center" }}>
                  <span className="small faint">Set class to</span>
                  <div style={{ width: 150 }}>
                    <ClassInput id="bulk-class" value={bulkClass}
                      onChange={setBulkClass} options={classes}
                      ariaLabel="Class to apply to selected students"
                      placeholder="Class / grade" />
                  </div>
                  <Button size="sm" icon="check" loading={bulkBusy}
                    onClick={applyBulkClass}>
                    Apply to {checked.size}
                  </Button>
                  <button className="btn sm ghost"
                    onClick={() => { setChecked(new Set()); setBulkClass(""); }}>
                    Clear
                  </button>
                </div>
              )}
            </div>
            <DataTable head={
              <tr>
                <th aria-label="Select" />
                <th>Student</th><th>Nickname</th><th>Class</th>
                <th>Submissions</th>
                <th className="num">Corrections</th><th className="num">Score</th><th>Pending</th>
              </tr>
            }>
              {filtered.map((s) => (
                <tr key={s.student_id} className="clickable"
                  onClick={() => openProfile(s.student_id)}
                  {...rowActivate(() => openProfile(s.student_id))}>
                  <td data-label="Select" onClick={(e) => e.stopPropagation()}>
                    <input
                      type="checkbox"
                      checked={checked.has(s.student_id)}
                      onChange={() => toggleChecked(s.student_id)}
                      aria-label={`Select ${s.display_name}`}
                    />
                  </td>
                  <td data-label="Student">
                    <div style={{ fontWeight: 600 }}>{s.display_name}</div>
                    {s.nickname && s.official_name && (
                      <div className="faint small">Classroom: {s.official_name}</div>
                    )}
                    {s.email && <div className="faint small">{s.email}</div>}
                  </td>
                  <td data-label="Nickname" className="small">
                    {s.nickname || <span className="faint">—</span>}
                  </td>
                  <td data-label="Class" className="small">
                    {s.class_name
                      ? <Badge tone="info">{s.class_name}</Badge>
                      : <span className="faint small">{s.class_label}</span>}
                  </td>
                  <td data-label="Submissions" className="small">
                    {s.tracking_available ? (
                      <Badge tone={s.submitted === s.assignments ? "ok" : s.submitted > 0 ? "warn" : "mute"}>
                        {s.submitted}/{s.assignments} submitted
                      </Badge>
                    ) : (
                      <span className="faint small">no Classroom data</span>
                    )}
                  </td>
                  <td data-label="Corrections" className="num">{s.corrections}</td>
                  <td data-label="Score" className="num mono">
                    {s.total_available ? `${s.total_earned}/${s.total_available}` : "—"}
                  </td>
                  <td data-label="Pending">
                    {s.pending > 0
                      ? <Badge tone="info">{s.pending} queued</Badge>
                      : <span className="faint">—</span>}
                  </td>
                </tr>
              ))}
            </DataTable>
          </>
        )}
      </div>
    </>
  );
}
