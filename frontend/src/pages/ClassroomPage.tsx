import { Fragment, useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  api, displayName, postForm, timeAgo,
  type Course, type Coursework, type PermissionsReport, type SubmissionDetail,
  type StateEvent, type TrackingData, type TrackingRow, type TrackingStatus,
} from "../api";
import {
  Badge, Button, DataTable, Empty, ExportMenu, Icon, PageHeader, Progress,
  rowActivate, Segmented, useToast,
} from "../components";

/* ================================================================ status */

const STATUS_META: Record<TrackingStatus, { tone: "ok" | "err" | "warn" | "info" | "mute"; icon: string }> = {
  NOT_STARTED: { tone: "mute", icon: "○" },
  CREATED: { tone: "info", icon: "◇" },
  SUBMITTED: { tone: "ok", icon: "✓" },
  SUBMITTED_LATE: { tone: "warn", icon: "⏱" },
  RETURNED: { tone: "ok", icon: "↩" },
  RECLAIMED: { tone: "err", icon: "↺" },
  EDITED_AFTER_SUBMISSION: { tone: "warn", icon: "✎" },
  MISSING: { tone: "mute", icon: "∅" },
  PROCESSING: { tone: "info", icon: "⚙" },
  CORRECTED: { tone: "ok", icon: "★" },
  GRADED: { tone: "ok", icon: "🏅" },
  FAILED: { tone: "err", icon: "✗" },
  INVALID: { tone: "err", icon: "⛔" },
  UNKNOWN: { tone: "mute", icon: "?" },
};

const FILTERS: { key: string; label: string; match: (s: TrackingStatus) => boolean }[] = [
  { key: "all", label: "All", match: () => true },
  { key: "submitted", label: "Submitted", match: (s) => s === "SUBMITTED" || s === "SUBMITTED_LATE" },
  { key: "not_submitted", label: "Not Submitted", match: (s) => s === "NOT_STARTED" || s === "CREATED" || s === "MISSING" },
  { key: "late", label: "Late", match: (s) => s === "SUBMITTED_LATE" },
  { key: "reclaimed", label: "Reclaimed", match: (s) => s === "RECLAIMED" },
  { key: "returned", label: "Returned", match: (s) => s === "RETURNED" },
  { key: "no_attachment", label: "Missing Attachment", match: (s) => s === "SUBMITTED" || s === "SUBMITTED_LATE" },
  { key: "processing", label: "Processing", match: (s) => s === "PROCESSING" },
  { key: "corrected", label: "Corrected", match: (s) => s === "CORRECTED" },
  { key: "graded", label: "Graded", match: (s) => s === "GRADED" },
  { key: "failed", label: "Failed", match: (s) => s === "FAILED" },
  { key: "invalid", label: "Invalid", match: (s) => s === "INVALID" },
  { key: "sync_error", label: "Sync Error", match: () => false },
  { key: "stale", label: "Stale", match: () => false },
];

function StatusChip({ row }: { row: TrackingRow }) {
  const meta = STATUS_META[row.status] ?? STATUS_META.UNKNOWN;
  return (
    <span className={`badge ${meta.tone}`} title={`Classroom state: ${row.raw_state ?? "—"}`}>
      <span className="dot" />
      {row.status_label}
      {row.stale && " · stale"}
    </span>
  );
}

/** Can this row actually be corrected? Real attachments + handed-in state. */
function correctable(row: TrackingRow): boolean {
  if (row.attachments.length === 0) return false;
  return !["NOT_STARTED", "CREATED", "MISSING", "RECLAIMED", "INVALID", "UNKNOWN"]
    .includes(row.status);
}

function QualityChip({ row }: { row: TrackingRow }) {
  const map = {
    VERIFIED: { tone: "ok", text: "verified" },
    PARTIAL: { tone: "warn", text: "partial" },
    ERROR: { tone: "err", text: "error" },
    UNKNOWN: { tone: "mute", text: "unknown" },
  } as const;
  const q = map[row.data_quality] ?? map.UNKNOWN;
  return <span className={`badge ${q.tone}`} style={{ fontSize: 10 }}>◆ {q.text}</span>;
}

function TransitionLine({ event }: { event: StateEvent }) {
  return (
    <li>
      <span className="mono">{event.from_state ?? "—"}</span>
      {" → "}
      <span className="mono">{event.to_state}</span>
      <span className="faint small" style={{ marginLeft: 8 }}>{timeAgo(event.detected_at)}</span>
    </li>
  );
}

function ExpandedDetail({ row, detail }: { row: TrackingRow; detail: SubmissionDetail | null }) {
  if (!detail) {
    return <tr><td colSpan={9}><Loading label="Loading detail…" /></td></tr>;
  }
  const sub = detail.submission;
  return (
    <tr className="expanded">
      <td colSpan={9} style={{ background: "var(--surface)" }}>
        <div className="grid grid-3" style={{ padding: "4px 2px" }}>
          <div>
            <h3 style={{ marginBottom: 6 }}>State</h3>
            <div className="small stack" style={{ gap: 2 }}>
              <div>Current: <span className="mono">{sub.current_state}</span></div>
              <div>Previous: <span className="mono">{sub.previous_state ?? "—"}</span></div>
              <div className="faint">Changed {timeAgo(sub.state_changed_at)}</div>
              <div className="faint">Last verified {timeAgo(sub.last_synced_at)}</div>
              <div className="faint">First seen {timeAgo(sub.first_seen_at)}</div>
              <div style={{ marginTop: 6 }}><QualityChip row={row} /></div>
              {sub.sync_error && (
                <div className="notice err small" style={{ marginTop: 8 }}>{sub.sync_error}</div>
              )}
            </div>
          </div>
          <div>
            <h3 style={{ marginBottom: 6 }}>Attachments &amp; grade</h3>
            {detail.attachments.length === 0 ? (
              <div className="faint small">No attachments</div>
            ) : (
              <ul className="small" style={{ margin: 0, paddingLeft: 18 }}>
                {detail.attachments.map((a, i) => (
                  <li key={i}>
                    <span className="badge mute" style={{ fontSize: 10 }}>{a.kind}</span>{" "}
                    {a.title ?? a.id}
                  </li>
                ))}
              </ul>
            )}
            <div className="divider" style={{ margin: "10px 0" }} />
            <div className="small stack" style={{ gap: 2 }}>
              <div>Classroom: {sub.assigned_grade ?? "—"} / {sub.max_points ?? "?"}</div>
              {sub.draft_grade != null && <div className="faint">Draft grade: {sub.draft_grade}</div>}
              {row.result && (row.result.partial ? (
                <div>
                  V5: Incomplete — {row.result.partial.marked_questions} of{" "}
                  {row.result.partial.expected_questions} questions marked
                </div>
              ) : (
                <div>V5: {row.result.earned}/{row.result.available} ({row.result.percentage.toFixed(1)}%)</div>
              ))}
            </div>
          </div>
          <div>
            <h3 style={{ marginBottom: 6 }}>History</h3>
            {detail.state_events.length === 0 ? (
              <div className="faint small">No local transitions (unchanged since first sync).</div>
            ) : (
              <ul className="small" style={{ margin: 0, paddingLeft: 18 }}>
                {detail.state_events.map((e, i) => <TransitionLine key={i} event={e} />)}
              </ul>
            )}
            {detail.jobs.length > 0 && (
              <>
                <div className="divider" style={{ margin: "10px 0" }} />
                <h3 style={{ marginBottom: 6 }}>V5 corrections</h3>
                <ul className="small" style={{ margin: 0, paddingLeft: 18 }}>
                  {detail.jobs.map((j) => (
                    <li key={j.id}>
                      <span className="mono">{j.id.slice(0, 8)}</span>{" "}
                      {j.status} · {timeAgo(j.finished_at ?? j.created_at)}
                      {j.error && <div className="notice err small">{j.error}</div>}
                    </li>
                  ))}
                </ul>
              </>
            )}
          </div>
        </div>
      </td>
    </tr>
  );
}

// tiny inline loading (avoids circular import noise)
function Loading({ label }: { label: string }) {
  return <span className="faint small">⏳ {label}</span>;
}

/* ================================================================ page */

/**
 * `/api/classroom/status` — the field names here MUST match the API exactly.
 *
 * They once did not: this page read `configured` while the endpoint sends
 * `client_configured`, so that lookup was always `undefined` and a perfectly
 * configured install was told its OAuth client was missing.
 */
interface Health {
  connected: boolean; client_configured: boolean; problem: string | null;
  last_sync: number | null; write_draft_grades: boolean;
  email?: string | null; connected_at?: number | null;
}

type SortKey = "name" | "status" | "recent";

interface ClassroomDeepLink {
  courseId?: string;
  workId?: string;
}

export default function ClassroomPage({
  deepLink = {},
}: { deepLink?: ClassroomDeepLink }) {
  const toast = useToast();

  // connection + pickers (deep links from search: #/classroom/{course}/{work})
  const [health, setHealth] = useState<Health | null>(null);
  const [courses, setCourses] = useState<Course[]>([]);
  const [courseId, setCourseId] = useState(deepLink.courseId ?? "");
  const [work, setWork] = useState<Coursework[]>([]);
  const [workId, setWorkId] = useState("");

  // tracking view
  const [tracking, setTracking] = useState<TrackingData | null>(null);
  const [filter, setFilter] = useState("all");
  const [checkedIds, setCheckedIds] = useState<Set<string>>(new Set());
  const [search, setSearch] = useState("");
  const [sort, setSort] = useState<SortKey>("name");
  const [expanded, setExpanded] = useState<string | null>(null);
  const [detail, setDetail] = useState<SubmissionDetail | null>(null);
  const [detailFor, setDetailFor] = useState<string | null>(null);

  // ui state
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [syncStage, setSyncStage] = useState<string | null>(null);
  const [syncProgress, setSyncProgress] = useState(0);
  // full-class sync (§3/§22): staged progress over ALL courses/assignments
  const [fullSyncStage, setFullSyncStage] = useState<string | null>(null);
  const [perms, setPerms] = useState<PermissionsReport | null>(null);
  const [checkingPerms, setCheckingPerms] = useState(false);
  // whole-assignment run (§14-§15): real counts, updated from tracking data
  const [batch, setBatch] = useState<{
    title: string; total: number; enqueued: number;
    skipped_missing: number; skipped_draft: number; invalid: number;
    enqueue_failed: number; sync_warning: string | null;
  } | null>(null);
  const [diag, setDiag] = useState<{
    results: { check: string; status: string; detail: string }[];
    passed: number; failed: number; skipped: number; total: number;
  } | null>(null);
  const esRef = useRef<EventSource | null>(null);
  const fullEsRef = useRef<EventSource | null>(null);
  const autoSyncedRef = useRef<Set<string>>(new Set());

  const loadHealth = useCallback(() => {
    api<Health>("/classroom/status").then(setHealth).catch(() => {});
  }, []);

  const loadCourses = useCallback((force = false) => {
    setError(null);
    api<{ courses: Course[] }>(`/classroom/courses${force ? "?force=true" : ""}`)
      .then((r) => {
        setCourses(r.courses);
        // Open on the first class instead of an empty page waiting for a
        // choice: the pickers stay available for switching.
        setCourseId((cur) => cur || r.courses[0]?.id || "");
      })
      .catch((e) => setError(e.message));
  }, []);

  useEffect(() => { loadHealth(); }, [loadHealth]);
  useEffect(() => {
    if (health?.connected) loadCourses();
  }, [health?.connected, loadCourses]);

  useEffect(() => {
    if (!courseId) return;
    setWork([]); setWorkId(""); setTracking(null);
    api<{ assignments: Coursework[] }>(`/classroom/courses/${courseId}/assignments`)
      .then((r) => {
        setWork(r.assignments);
        // Same rule as the class picker: land on real data. The deep-link
        // effect below still overrides this when the URL names an assignment.
        setWorkId((cur) => cur || r.assignments[0]?.id || "");
      })
      .catch((e) => setError(e.message));
  }, [courseId]);

  // Deep link: once assignments load, preselect the linked assignment.
  useEffect(() => {
    if (deepLink.workId && work.some((w) => w.id === deepLink.workId)) {
      setWorkId(deepLink.workId);
    }
  }, [deepLink, work]);

  const loadTracking = useCallback(() => {
    if (!workId) return;
    api<TrackingData>(`/classroom/tracking?course_id=${courseId}&coursework_id=${workId}`)
      .then(setTracking)
      .catch((e) => setError(e.message));
  }, [courseId, workId]);

  useEffect(() => { loadTracking(); }, [loadTracking]);

  /* First view of an assignment with NO local data → pull the real roster
     + submissions automatically (the reason assignments looked empty). */
  useEffect(() => {
    if (!workId || !tracking || autoSyncedRef.current.has(workId)) return;
    if (health && !health.connected) return;
    if (tracking.rows.length > 0) {
      autoSyncedRef.current.add(workId);
      return;
    }
    autoSyncedRef.current.add(workId);
    runSync();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [workId, tracking, health]);

  /* ---- sync with real SSE progress ---- */
  const runSync = useCallback(() => {
    if (!courseId || !workId) return;
    setError(null);
    setSyncStage("Starting…");
    setSyncProgress(0.05);
    esRef.current?.close();
    const es = new EventSource(`/api/classroom/courses/${courseId}/assignments/${workId}/sync`);
    esRef.current = es;
    es.onmessage = (ev) => {
      try {
        const msg = JSON.parse(ev.data);
        if (msg.type === "stage") {
          setSyncStage(msg.message);
          setSyncProgress((p) => Math.min(0.9, p + 0.25));
        } else if (msg.type === "roster") {
          setSyncStage(`Roster: ${msg.count} students`);
          setSyncProgress(0.35);
        } else if (msg.type === "page") {
          setSyncStage(`Fetched ${msg.collected} submissions (${msg.pages} page${msg.pages > 1 ? "s" : ""})`);
          setSyncProgress(0.7);
        } else if (msg.type === "done") {
          setSyncStage(null); setSyncProgress(0);
          toast("ok", `Synced ${msg.summary.submissions} submissions · ${msg.summary.transitions} change${msg.summary.transitions === 1 ? "" : "s"}`);
          loadTracking();
          es.close();
        } else if (msg.type === "error") {
          setSyncStage(null); setSyncProgress(0);
          setError(`Sync failed: ${msg.message} — your existing data has been preserved.`);
          toast("err", "Classroom sync failed — previous data kept");
          es.close();
        }
      } catch { /* ignore malformed frames */ }
    };
    es.onerror = () => {
      setSyncStage(null); setSyncProgress(0);
      es.close();
    };
  }, [courseId, workId, loadTracking, toast]);

  /* ---- full-class sync: courses → assignments → students → submissions --- */
  const runFullSync = useCallback(() => {
    if (fullSyncStage) return;
    setError(null);
    setFullSyncStage("Connecting…");
    setSyncStage("Connecting…");
    setSyncProgress(0.05);
    let finished = false;
    fullEsRef.current?.close();
    const es = new EventSource("/api/classroom/sync");
    fullEsRef.current = es;
    const stop = () => {
      setFullSyncStage(null); setSyncStage(null); setSyncProgress(0);
      es.close();
    };
    es.onmessage = (ev) => {
      try {
        const msg = JSON.parse(ev.data);
        if (msg.type === "stage") {
          setFullSyncStage(msg.message);
          setSyncStage(msg.message);
          setSyncProgress((p) => Math.min(0.9, p + 0.08));
        } else if (msg.type === "students") {
          setFullSyncStage(`Fetching students — ${msg.count} in ${msg.course ?? "class"}`);
        } else if (msg.type === "assignment") {
          setFullSyncStage(
            `Fetching submissions — ${msg.title} (${msg.index}/${msg.total})`);
          setSyncProgress(Math.min(0.9, 0.2 + 0.7 * (msg.index / Math.max(1, msg.total))));
        } else if (msg.type === "done") {
          finished = true;
          stop();
          const s = msg.summary;
          if (s.partial) {
            const parts = (s.errors || []).map((e: { part: string }) => e.part).join(", ");
            setError(`Sync completed with problems in: ${parts}. Successful data was saved.`);
            toast("warn", `Sync partial — ${s.errors.length} part(s) failed`);
          } else {
            toast("ok", `Sync complete — ${s.courses} class${s.courses === 1 ? "" : "es"} · `
              + `${s.assignments} assignments · ${s.students} students · `
              + `${s.submissions} submissions`);
          }
          loadHealth();
          loadCourses(true);
          if (courseId) {
            api<{ assignments: Coursework[] }>(`/classroom/courses/${courseId}/assignments`)
              .then((r) => setWork(r.assignments))
              .catch(() => {});
          }
          loadTracking();
        } else if (msg.type === "error") {
          finished = true;
          stop();
          setError(`Sync failed${msg.stage ? ` while ${msg.stage}` : ""}: ${msg.message}`);
          toast("err", "Sync failed — existing data kept");
        }
      } catch { /* ignore malformed frames */ }
    };
    es.onerror = () => {
      if (finished) return;
      stop();
      setError("Sync failed: lost connection to the server before completion.");
      toast("err", "Sync failed");
    };
  }, [fullSyncStage, courseId, loadHealth, loadCourses, loadTracking, toast]);

  async function checkPermissions() {
    setCheckingPerms(true);
    try {
      setPerms(await api<PermissionsReport>("/classroom/permissions"));
    } catch (e) {
      setPerms({ checked: false, ok: null, connected: true, required: [],
                 missing: [], detail: (e as Error).message });
    } finally {
      setCheckingPerms(false);
    }
  }

  useEffect(() => () => {
    esRef.current?.close();
    fullEsRef.current?.close();
  }, []);

  const loadDetail = useCallback(async (submissionId: string) => {
    setDetailFor(submissionId);
    try {
      setDetail(await api<SubmissionDetail>(
        `/classroom/submissions/${encodeURIComponent(submissionId)}/detail`));
    } catch (e) {
      setError((e as Error).message);
    }
  }, []);

  function toggleExpand(row: TrackingRow) {
    if (expanded === row.submission_id) {
      setExpanded(null); setDetail(null); setDetailFor(null);
    } else {
      setExpanded(row.submission_id);
      loadDetail(row.submission_id);
    }
  }

  /* ---- preserved flows ---- */
  async function connect() {
    setBusy("connect"); setError(null);
    try {
      await api("/classroom/connect", { method: "POST" });
      toast("ok", "Google Classroom connected");
      loadHealth();
      loadCourses(true);
      setPerms(null);
    } catch (e) { setError((e as Error).message); } finally { setBusy(null); }
  }

  async function disconnect() {
    setBusy("disconnect"); setError(null);
    try {
      await api("/classroom/disconnect", { method: "POST" });
      toast("info", "Google Classroom disconnected");
      setCourses([]); setWork([]); setTracking(null);
      setCourseId(""); setWorkId("");
      loadHealth();
    } catch (e) { setError((e as Error).message); } finally { setBusy(null); }
  }

  async function toggleAllow(cw: Coursework) {
    await api(`/classroom/courses/${courseId}/assignments/${cw.id}/allow?allowed=${cw.allowlisted ? "false" : "true"}`, { method: "POST" });
    setWork((items) => items.map((x) => x.id === cw.id
      ? { ...x, allowlisted: cw.allowlisted ? 0 : 1 } : x));
  }

  async function uploadScheme(file: File) {
    setBusy("scheme"); setError(null);
    const body = new FormData();
    body.append("file", file);
    try {
      await postForm(`/classroom/courses/${courseId}/assignments/${workId}/mark-scheme`, body);
      toast("ok", "Mark scheme uploaded");
    } catch (e) { setError((e as Error).message); } finally { setBusy(null); }
  }

  async function correctAll() {
    if (!selected) return;
    setBusy("correct"); setError(null);
    try {
      const r = await api<{
        enqueued: number; duplicates: number; total_students: number;
        skipped_missing: string[]; skipped_draft: string[]; invalid: string[];
        enqueue_failed: { student: string; error: string }[];
        sync_warning: string | null;
      }>(`/classroom/courses/${courseId}/assignments/${workId}/correct-all`,
        { method: "POST", body: JSON.stringify({ refresh: true }) });
      setBatch({
        title: selected.title,
        total: r.total_students,
        enqueued: r.enqueued + r.duplicates,
        skipped_missing: r.skipped_missing.length,
        skipped_draft: r.skipped_draft.length,
        invalid: r.invalid.length,
        enqueue_failed: r.enqueue_failed.length,
        sync_warning: r.sync_warning,
      });
      const parts = [`Queued ${r.enqueued} correction(s)`];
      if (r.duplicates) parts.push(`${r.duplicates} already queued`);
      if (r.skipped_missing.length) parts.push(`${r.skipped_missing.length} not submitted — skipped safely`);
      if (r.skipped_draft.length) parts.push(`${r.skipped_draft.length} draft — skipped safely`);
      if (r.invalid.length) parts.push(`${r.invalid.length} invalid submission(s)`);
      if (r.enqueue_failed.length) parts.push(`${r.enqueue_failed.length} failed to queue`);
      toast(r.enqueued > 0 ? "ok" : "warn", parts.join(" · "));
      if (r.sync_warning) toast("warn", r.sync_warning);
      if (r.enqueue_failed.length > 0) {
        setError(`${r.enqueue_failed.length} student(s) could not be queued: `
          + r.enqueue_failed.map((f) => `${f.student} (${f.error})`).join(", "));
      }
      loadTracking();
    } catch (e) { setError((e as Error).message); } finally { setBusy(null); }
  }

  /* Live batch progress: refresh tracking rows every 2s while jobs run. */
  useEffect(() => {
    if (!batch) return;
    let stop = false;
    let ticks = 0;
    const timer = setInterval(() => {
      ticks += 1;
      loadTracking();
      if (stop || ticks > 300) clearInterval(timer);
    }, 2000);
    return () => { stop = true; clearInterval(timer); };
  }, [batch, loadTracking]);

  interface CorrectResponse {
    enqueued: number;
    duplicates: string[];
    skipped_not_submitted: string[];
    skipped_no_attachment: string[];
    unknown: string[];
  }

  async function correctSelection(ids: string[]) {
    if (ids.length === 0) return;
    setBusy("correct-selected"); setError(null);
    try {
      const r = await api<CorrectResponse>(
        `/classroom/courses/${courseId}/assignments/${workId}/correct`,
        { method: "POST", body: JSON.stringify({ submission_ids: ids }) });
      const skipped = r.skipped_not_submitted.length + r.skipped_no_attachment.length;
      const parts = [`Enqueued ${r.enqueued} correction(s)`];
      if (r.duplicates.length) parts.push(`${r.duplicates.length} already queued`);
      if (r.skipped_not_submitted.length) parts.push(`${r.skipped_not_submitted.length} not submitted — skipped`);
      if (r.skipped_no_attachment.length) parts.push(`${r.skipped_no_attachment.length} without attachment — skipped`);
      if (r.unknown.length) parts.push(`${r.unknown.length} unknown — skipped`);
      toast(r.enqueued > 0 ? "ok" : "warn", parts.join(" · "));
      if (skipped > 0 && r.enqueued === 0) {
        setError("Nothing was queued: none of the selected students has submitted work with an attachment.");
      }
      setCheckedIds(new Set());
      loadTracking();
    } catch (e) { setError((e as Error).message); } finally { setBusy(null); }
  }

  function toggleSelect(id: string, on: boolean) {
    setCheckedIds((prev) => {
      const next = new Set(prev);
      if (on) next.add(id); else next.delete(id);
      return next;
    });
  }

  async function runDiagnostics() {
    setBusy("diag"); setError(null);
    try {
      setDiag(await api("/classroom/diagnostics/run", { method: "POST" }));
    } catch (e) { setError((e as Error).message); } finally { setBusy(null); }
  }

  /* ---- derived view ---- */
  const rows = tracking?.rows ?? [];
  const overview = tracking?.overview;

  const filtered = useMemo(() => {
    const f = FILTERS.find((x) => x.key === filter) ?? FILTERS[0];
    const q = search.trim().toLowerCase();
    let out = rows.filter((r) => {
      if (filter === "no_attachment") {
        if (!f.match(r.status)) return false;
        if (r.attachments.length > 0) return false;
        if (r.raw_state !== "TURNED_IN" && r.raw_state !== "STUDENT_EDITED_AFTER_TURN_IN") return false;
      } else if (filter === "sync_error") {
        if (!r.sync_error) return false;
      } else if (filter === "stale") {
        if (!r.stale) return false;
      } else if (!f.match(r.status)) {
        return false;
      }
      if (!q) return true;
      return (r.student_name ?? "").toLowerCase().includes(q)
        || (r.student_email ?? "").toLowerCase().includes(q)
        || (r.coursework_title ?? "").toLowerCase().includes(q)
        || (r.raw_state ?? "").toLowerCase().includes(q);
    });
    if (sort === "name") out = [...out].sort((a, b) => (a.student_name ?? "").localeCompare(b.student_name ?? ""));
    if (sort === "status") out = [...out].sort((a, b) =>
      a.status.localeCompare(b.status) || (a.student_name ?? "").localeCompare(b.student_name ?? ""));
    if (sort === "recent") out = [...out].sort((a, b) =>
      (b.state_changed_at ?? b.last_synced_at ?? 0) - (a.state_changed_at ?? a.last_synced_at ?? 0));
    return out;
  }, [rows, filter, search, sort]);

  const correctableRows = filtered.filter(correctable);
  const allSelected = correctableRows.length > 0
    && correctableRows.every((r) => checkedIds.has(r.submission_id));

  /* ---- connect gate ---- */
  if (health && !health.connected) {
    return (
      <>
        <PageHeader kicker="Classes · Classroom" title="Google Classroom"
          sub="Connect once; courses and submissions are cached locally afterwards." />
        <div className="card" style={{ maxWidth: 560 }}>
          {health.client_configured ? (
            <>
              <p className="muted" style={{ marginTop: 0 }}>
                Sign in with the Google account that teaches your classes. A window
                will open for Google sign-in; nothing is stored except a refresh
                token on this computer.
              </p>
              {health.problem && health.problem !== "Not connected. Click Connect Google Classroom." && (
                <div className="notice warn" style={{ marginBottom: 12 }}>{health.problem}</div>
              )}
              {error && <div className="notice err" style={{ marginBottom: 12 }}>{error}</div>}
              <button className="btn primary" onClick={connect} disabled={busy === "connect"}>
                {busy === "connect" ? "Waiting for Google — finish the sign-in in your browser…" : "Connect Google Classroom"}
              </button>
              {busy === "connect" && (
                <div className="faint small" style={{ marginTop: 8 }}>
                  The browser will open automatically. This page waits until you
                  finish signing in (up to 5 minutes).
                </div>
              )}
            </>
          ) : (
            <div className="notice err">
              <strong>Google OAuth client is not installed.</strong>{" "}
              {health.problem ??
                "Put your Google OAuth client JSON in data/secrets/google_oauth_client.json, then reload this page."}
            </div>
          )}
        </div>
      </>
    );
  }

  const selected = work.find((w) => w.id === workId);

  return (
    <>
      <PageHeader kicker="Classes · Classroom" title="Google Classroom"
        sub="Full submission tracking — every state, every student, with sync quality you can trust.">
        {overview && overview.sync_errors > 0 && (
          <Badge tone="err">{overview.sync_errors} sync error{overview.sync_errors === 1 ? "" : "s"}</Badge>
        )}
        {overview && overview.stale > 0 && <Badge tone="warn">{overview.stale} stale</Badge>}
      </PageHeader>

      {error && <div className="notice err" style={{ marginBottom: 12 }}>⚠ {error}</div>}

      {/* ---- health header ---- */}
      <div className="card">
        <div className="row between wrap">
          <div className="row wrap">
            <Badge tone={health?.connected ? "ok" : "err"}>
              {health?.connected ? "Connection healthy" : "Disconnected"}
            </Badge>
            {health?.email && (
              <span className="row small" style={{ gap: 5 }}
                title="Signed-in Google account (source: Google Classroom)">
                <Icon name="paperclip" size={13} /> {health.email}
              </span>
            )}
            <span className="faint small"
              title="Last successful Google Classroom synchronization (local time)">
              Last synced: {(() => {
                const ts = health?.last_sync ?? overview?.last_sync;
                return ts ? new Date(ts * 1000).toLocaleString() : "never";
              })()}
            </span>
            {syncStage && (
              <span className="small" style={{ color: "var(--accent)" }}>{syncStage}</span>
            )}
          </div>
          <div className="row wrap">
            {syncProgress > 0 && (
              <div style={{ minWidth: 160, flex: 1, maxWidth: 320 }}>
                <Progress value={syncProgress} thin />
              </div>
            )}
            <button className="btn primary sm" onClick={runFullSync}
              disabled={!!fullSyncStage || !!syncStage}>
              {fullSyncStage ? "Syncing…" : "Sync Google Classroom"}
            </button>
            <button className="btn sm ghost" onClick={checkPermissions}
              disabled={checkingPerms}>
              {checkingPerms ? "Checking…" : "Check permissions"}
            </button>
            <button className="btn sm ghost" onClick={disconnect}
              disabled={busy === "disconnect"}>
              {busy === "disconnect" ? "Disconnecting…" : "Disconnect"}
            </button>
          </div>
        </div>
        {perms && perms.ok === false && perms.missing.length > 0 && (
          <div className="notice warn small" style={{ marginTop: 10 }}>
            Missing Google permissions: {perms.missing.map((s) => s.split("/").pop()).join(", ")}.
            The app will not silently fail — reconnect to grant everything the
            features need.
            {" "}
            <button className="btn sm" onClick={connect} disabled={busy === "connect"}>
              Reconnect with full permissions
            </button>
          </div>
        )}
        {perms && perms.checked && perms.ok && (
          <div className="notice ok small" style={{ marginTop: 10 }}>
            All required Google permissions are granted ({perms.granted?.length ?? 0} scopes).
          </div>
        )}
        {perms && !perms.checked && perms.detail && (
          <div className="notice info small" style={{ marginTop: 10 }}>{perms.detail}</div>
        )}
      </div>

      {/* ---- pickers + assignment actions ---- */}
      <div className="card">
        <div className="picker-bar">
          <label className="field" style={{ marginBottom: 0 }}>
            <span>Class</span>
            <select className="input" value={courseId} onChange={(e) => setCourseId(e.target.value)}>
              <option value="">Choose a class…</option>
              {courses.map((c) => (
                <option key={c.id} value={c.id}>{c.name}{c.section ? ` · ${c.section}` : ""}</option>
              ))}
            </select>
          </label>
          <label className="field" style={{ marginBottom: 0 }}>
            <span>Assignment</span>
            <select className="input" value={workId} onChange={(e) => setWorkId(e.target.value)} disabled={!courseId}>
              <option value="">Choose an assignment…</option>
              {work.map((w) => (
                <option key={w.id} value={w.id}>{w.title}{w.max_points ? ` · ${w.max_points} pts` : ""}</option>
              ))}
            </select>
          </label>
        </div>

        {selected && (
          <>
            <hr className="divider" />
            <div className="row between wrap">
              <div className="row wrap">
                <Badge tone={selected.allowlisted ? "ok" : "mute"}>
                  {selected.allowlisted ? "Allowed" : "Not allowed"}
                </Badge>
                <button className="btn sm" onClick={() => toggleAllow(selected)}>
                  {selected.allowlisted ? "Remove from allow-list" : "Allow for correction"}
                </button>
              </div>
              <div className="row wrap">
                <label className="btn sm">
                  {busy === "scheme" ? "Uploading…" : "Upload mark scheme"}
                  <input type="file" accept="application/pdf" style={{ display: "none" }}
                    onChange={(e) => { const f = e.target.files?.[0]; if (f) uploadScheme(f); }} />
                </label>
                <button className="btn primary sm" disabled={!selected.allowlisted || busy === "correct"} onClick={correctAll}>
                  {busy === "correct" ? "Starting…" : "Correct Entire Assignment"}
                </button>
              </div>
            </div>
            {!selected.allowlisted && (
              <div className="notice warn small" style={{ marginTop: 10 }}>
                This assignment is not allow-listed — nothing will be processed until you allow it.
              </div>
            )}
          </>
        )}
      </div>

      {/* ---- whole-assignment run progress (real counts) ---- */}
      {batch && (
        <div className="card" style={{ borderColor: "rgba(109,141,255,0.4)" }}>
          <div className="row between wrap" style={{ marginBottom: 8 }}>
            <h3 style={{ margin: 0 }}>
              Correcting {batch.title}
              <span className="hint">
                {(() => {
                  const done = overview
                    ? (overview.counts.CORRECTED ?? 0) + (overview.counts.FAILED ?? 0)
                      + (overview.counts.INVALID ?? 0) + (overview.counts.GRADED ?? 0)
                    : 0;
                  const processing = overview?.counts.PROCESSING ?? 0;
                  return `${Math.min(done + processing, batch.enqueued)} / ${batch.enqueued} of ${batch.total} students`;
                })()}
              </span>
            </h3>
            <button className="btn sm ghost" onClick={() => setBatch(null)}>Dismiss</button>
          </div>
          {overview && (
            <div className="chip-row">
              <span className="badge ok"><span className="dot" />✓ Corrected: {(overview.counts.CORRECTED ?? 0) + (overview.counts.GRADED ?? 0)}</span>
              <span className="badge info"><span className="dot" />⚙ Processing: {(overview.counts.PROCESSING ?? 0)}</span>
              <span className="badge mute"><span className="dot" />○ Missing: {(overview.counts.NOT_STARTED ?? 0) + (overview.counts.MISSING ?? 0) + (overview.counts.RECLAIMED ?? 0)}</span>
              <span className="badge info"><span className="dot" />◇ Draft: {(overview.counts.CREATED ?? 0)}</span>
              <span className="badge err"><span className="dot" />⛔ Invalid: {(overview.counts.INVALID ?? 0)}</span>
              <span className="badge err"><span className="dot" />! Failed: {(overview.counts.FAILED ?? 0)}</span>
              <span className="badge mute"><span className="dot" />… Remaining: {Math.max(0, batch.enqueued - ((overview.counts.CORRECTED ?? 0) + (overview.counts.GRADED ?? 0) + (overview.counts.FAILED ?? 0) + (overview.counts.INVALID ?? 0) + (overview.counts.PROCESSING ?? 0)))}</span>
            </div>
          )}
          <div className="row wrap small faint" style={{ marginTop: 8 }}>
            <span>Skipped safely: {batch.skipped_missing} not submitted · {batch.skipped_draft} draft · {batch.invalid} invalid · {batch.enqueue_failed} enqueue failures</span>
          </div>
          {batch.sync_warning && (
            <div className="notice warn small" style={{ marginTop: 8 }}>{batch.sync_warning}</div>
          )}
        </div>
      )}

      {!workId && (
        <div className="card">
          <Empty icon="assignments"
            title={courses.length ? "Choose an assignment" : "No classes yet"}
            hint={courses.length
              ? "Pick a class and an assignment above to see every student's submission state."
              : "Sync Google Classroom to pull your classes and their coursework onto this computer."}
            action={courses.length ? undefined : (
              <Button variant="primary" icon="sync" onClick={runFullSync}
                disabled={!!fullSyncStage || !!syncStage}>
                {fullSyncStage ? "Syncing…" : "Sync Google Classroom"}
              </Button>
            )} />
        </div>
      )}

      {/* ---- tracking table ---- */}
      {workId && (
        <div className="card">
          <div className="row between wrap" style={{ marginBottom: 10, gap: 8 }}>
            <h3 style={{ margin: 0 }}>
              Tracking
              <span className="hint">{overview ? `${overview.total} students` : ""}</span>
            </h3>
            <div className="row wrap">
              <button className="btn sm" onClick={runSync} disabled={!!syncStage}>
                {syncStage ? "Syncing…" : "Sync assignment"}
              </button>
              <button className="btn sm ghost" onClick={loadTracking}>Reload</button>
              <button className="btn sm ghost" onClick={runDiagnostics} disabled={busy === "diag"}>
                {busy === "diag" ? "Running…" : "Diagnostics"}
              </button>
              <ExportMenu base="/classroom/tracking/export"
                params={{ course_id: courseId, coursework_id: workId }}
                onExported={(f) => toast("info", `${f.toUpperCase()} export generated`)} />
            </div>
          </div>

          {/* filter chips with live counts */}
          {overview && (
            <div className="chip-row" style={{ marginBottom: 12 }}>
              {FILTERS.map((f) => {
                let count: number | undefined;
                if (f.key === "all") count = overview.total;
                else if (f.key === "submitted") count = (overview.counts.SUBMITTED ?? 0) + (overview.counts.SUBMITTED_LATE ?? 0);
                else if (f.key === "not_submitted") count = (overview.counts.NOT_STARTED ?? 0) + (overview.counts.CREATED ?? 0) + (overview.counts.MISSING ?? 0);
                else if (f.key === "no_attachment") count = rows.filter((r) =>
                  (r.status === "SUBMITTED" || r.status === "SUBMITTED_LATE") && r.attachments.length === 0).length;
                else if (f.key === "sync_error") count = overview.sync_errors;
                else if (f.key === "stale") count = overview.stale;
                else count = overview.counts[f.key.toUpperCase() as TrackingStatus] ?? 0;
                if (count === 0 && f.key !== "all") return null;
                return (
                  <button key={f.key} className={`chip ${filter === f.key ? "active" : ""}`}
                    onClick={() => setFilter(f.key)}>
                    {f.label}<span className="chip-count">{count}</span>
                  </button>
                );
              })}
            </div>
          )}            <div className="row wrap" style={{ marginBottom: 12 }}>
              <input className="input" style={{ maxWidth: 280 }} placeholder="Search student, email, state…"
                value={search} onChange={(e) => setSearch(e.target.value)}
                aria-label="Search submissions" />
              <Segmented<SortKey> value={sort} onChange={setSort} ariaLabel="Sort submissions"
                options={[
                  { value: "name", label: "Name" },
                  { value: "status", label: "Status" },
                  { value: "recent", label: "Recent" },
                ]} />
              <span className="spacer" />
              {checkedIds.size > 0 && (
                <>
                  <Badge tone="info">{checkedIds.size} selected</Badge>
                  <button className="btn primary sm" disabled={busy === "correct-selected"}
                    onClick={() => correctSelection([...checkedIds])}>
                    {busy === "correct-selected" ? "Queueing…"
                      : `Correct selected (${checkedIds.size})`}
                  </button>
                  <button className="btn sm ghost" onClick={() => setCheckedIds(new Set())}>
                    Clear
                  </button>
                </>
              )}
            </div>

          {filtered.length === 0 ? (
            <Empty icon="inbox" art="inbox"
              title={rows.length === 0 ? "No tracking data yet" : "Nothing matches this filter"}
              hint={rows.length === 0
                ? "Press “Sync Google Classroom” to pull every submission state from Google."
                : "Try a different filter or clear the search."} />
          ) : (
            <DataTable
              head={<tr>
                <th style={{ width: 32 }}>
                  <input type="checkbox" aria-label="Select all correctable"
                    checked={allSelected}
                    onChange={(e) => setCheckedIds(e.target.checked
                      ? new Set(correctableRows.map((r) => r.submission_id))
                      : new Set())} />
                </th>
                <th>Student</th><th>Status</th><th>Attach.</th><th>Quality</th>
                <th className="num">Grade</th><th className="num">V5</th>
                <th>Last sync</th><th>Action</th>
              </tr>}
            >
              {filtered.map((r) => (
                <Fragment key={r.submission_id}>
                  <tr className="clickable" onClick={() => toggleExpand(r)}
                    {...rowActivate(() => toggleExpand(r))}
                    aria-expanded={expanded === r.submission_id}>
                    <td data-label="Select" onClick={(e) => e.stopPropagation()}>
                      <input type="checkbox"
                        aria-label={`Select ${displayName(r)}`}
                        disabled={!correctable(r)}
                        title={correctable(r)
                          ? "Include in the next correction run"
                          : "Nothing to correct: this student has no submitted attachment"}
                        checked={checkedIds.has(r.submission_id)}
                        onChange={(e) => toggleSelect(r.submission_id, e.target.checked)} />
                    </td>
                    <td data-label="Student">
                      <a className="student-link"
                        href={`#/students/${encodeURIComponent(r.student_id)}`}
                        onClick={(e) => e.stopPropagation()}
                        title="Open this student's profile">
                        {displayName(r)}
                      </a>
                      {r.late && <span className="badge warn" style={{ marginLeft: 6, fontSize: 10 }}>late</span>}
                      {r.synthetic && <span className="faint small" style={{ marginLeft: 6 }}>no submission</span>}
                    </td>
                    <td data-label="Status"><StatusChip row={r} /></td>
                    <td data-label="Attachments" className="small">
                      {r.attachments.length === 0
                        ? <span className="faint">none</span>
                        : <span title={r.attachments.map((a) => a.title ?? a.id ?? "").join(", ")}>
                            📎 {r.attachments.length}
                          </span>}
                    </td>
                    <td data-label="Quality"><QualityChip row={r} /></td>
                    <td data-label="Grade" className="num">
                      {r.assigned_grade != null ? `${r.assigned_grade}/${r.max_points ?? "?"}` : "—"}
                    </td>
                    <td data-label="V5" className="num">
                      {r.result
                        ? (r.result.partial
                          ? <span className="small faint">incomplete</span>
                          : `${r.result.earned}/${r.result.available}`)
                        : r.job
                          ? <span className="small faint">{r.job.status.toLowerCase()}</span>
                          : "—"}
                    </td>
                    <td data-label="Last sync" className="small faint">{timeAgo(r.last_synced_at)}</td>
                    <td data-label="Action" onClick={(e) => e.stopPropagation()}>
                      <button className="btn sm" disabled={!correctable(r)
                        || busy === "correct-selected"}
                        title={correctable(r)
                          ? "Correct this student's submission"
                          : r.attachments.length === 0
                            ? "No submitted attachment in Google Classroom"
                            : "Google Classroom shows no submitted work"}
                        onClick={() => correctSelection([r.submission_id])}>
                        Correct
                      </button>
                    </td>
                  </tr>
                  {expanded === r.submission_id && (
                    <ExpandedDetail row={r}
                      detail={detailFor === r.submission_id ? detail : null} />
                  )}
                </Fragment>
              ))}
            </DataTable>
          )}
        </div>
      )}

      {/* ---- diagnostics panel ---- */}
      {diag && (
        <div className="card">
          <h3>
            Tracking diagnostics
            <span className="hint">{diag.passed} passed · {diag.failed} failed · {diag.skipped} skipped</span>
          </h3>
          <DataTable head={<tr><th>Check</th><th>Result</th></tr>}>
            {diag.results.map((r) => (
              <tr key={r.check}>
                <td data-label="Check">
                  <Badge tone={r.status === "pass" ? "ok" : r.status === "fail" ? "err" : "mute"}>
                    {r.status === "pass" ? "✓" : r.status === "fail" ? "✗" : "–"} {r.check}
                  </Badge>
                </td>
                <td data-label="Detail" className="small faint">{r.detail}</td>
              </tr>
            ))}
          </DataTable>
        </div>
      )}
    </>
  );
}


