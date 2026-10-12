import { useCallback, useEffect, useRef, useState } from "react";
import {
  syncClassroomProgress, syncClassroomStart, timeAgo,
  type SyncSnapshot,
} from "./api";
import { Badge, Button, Icon, type ButtonVariant } from "./components";

/**
 * ONE Google Classroom sync for the whole application (§7).
 *
 * Every page that offers "Sync Classroom" uses this hook, so there is a single
 * implementation and a single source of truth: the backend's own run registry
 * (`/api/classroom/sync/progress`, written by the same worker the Google
 * Classroom page streams). No page guesses, counts down, or fabricates
 * progress — the stage, message and percentage come from real sync frames.
 *
 * Guarantees the page can rely on:
 *
 *   * starting twice is impossible — the button is disabled while a run is in
 *     flight AND the backend refuses to start a second run, so a double click
 *     can never duplicate Classroom reads, grades or reports;
 *   * success is only reported once the run has actually finished without an
 *     error, and the summary comes from the run itself;
 *   * a sync started from another page is picked up on mount, so the same
 *     operation is never started twice from two tabs.
 */
export function useClassroomSync(onSynced?: (run: SyncSnapshot) => void) {
  const [run, setRun] = useState<SyncSnapshot | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [joined, setJoined] = useState(false);
  const done = useRef(onSynced);
  done.current = onSynced;

  const load = useCallback(() => syncClassroomProgress()
    .then(setRun).catch(() => { /* the run is advisory; failures are reported
                                   by the action that started it */ }), []);

  // Reflect a sync already in flight (another tab, or this page reloaded).
  useEffect(() => {
    let cancelled = false;
    const tick = () => {
      if (cancelled) return;
      load();
    };
    syncClassroomProgress().then((snapshot) => {
      if (cancelled || !snapshot?.running) return;
      setRun(snapshot);
      setBusy(true);
      setJoined(true);
    }).catch(() => {});
    // A slow discovery poll only: it exists so a sync started from ANOTHER
    // page or tab is reflected here instead of being started a second time.
    // The sync itself is followed at 800 ms while it runs, and this costs one
    // tiny snapshot read. (Settings already polls two status endpoints on a
    // similar cadence, so this adds no meaningful load.)
    const id = setInterval(tick, 15_000);
    return () => { cancelled = true; clearInterval(id); };
  }, [load]);

  /** Start (or join) the single sync and follow it to a real conclusion. */
  const sync = useCallback(async (courseId?: string, courseworkId?: string) => {
    if (busy) return;
    setBusy(true);
    setError(null);
    try {
      const started = await syncClassroomStart(courseId, courseworkId);
      setJoined(!started.started);
      setRun(started.snapshot);
      // Follow the REAL run to its end. A full class sync legitimately takes a
      // minute or more; polling keeps working through a proxy that would buffer
      // an event stream.
      let snapshot = started.snapshot;
      const deadline = Date.now() + 15 * 60 * 1000;
      while (snapshot.running && Date.now() < deadline) {
        await new Promise((resolve) => setTimeout(resolve, 800));
        snapshot = await syncClassroomProgress();
        setRun(snapshot);
      }
      if (snapshot.running) {
        throw new Error(
          "The sync is still running after 15 minutes and was left to finish "
          + "on its own. Progress stays on the Google Classroom page.");
      }
      setRun(snapshot);
      if (snapshot.error) throw new Error(snapshot.error);
      done.current?.(snapshot);
      return snapshot;
    } catch (e) {
      setError((e as Error).message);
      throw e;
    } finally {
      setBusy(false);
    }
  }, [busy]);

  return { run, busy, error, joined, sync, refresh: load };
}

/** One-line status of the current/last sync, straight from the backend. */
export function SyncStatusLine({ run }: { run: SyncSnapshot | null }) {
  if (!run) return null;
  if (run.running) {
    return (
      <span className="row small faint" style={{ gap: 6, alignItems: "center" }}>
        <Icon name="refresh" size={13} />
        <span style={{ minWidth: 0, overflow: "hidden", textOverflow: "ellipsis",
                       whiteSpace: "nowrap", maxWidth: 320 }}
          title={run.message ?? undefined}>
          {run.message ?? "Syncing…"}
        </span>
        <Badge tone="info">{Math.round((run.progress ?? 0) * 100)}%</Badge>
      </span>
    );
  }
  if (run.error) return <Badge tone="err">Last sync failed</Badge>;
  if (run.finished_at) {
    return (
      <span className="small faint">
        Last sync {timeAgo(run.finished_at)}
      </span>
    );
  }
  return null;
}

/**
 * The shared button. Drop it into any page header.
 *
 * ``onSynced`` runs only after a genuine successful sync, which is where a page
 * reloads its own students/assignments/results — so the page never refreshes
 * on a failure and never claims success early.
 */
export function SyncClassroomButton({
  onSynced, size = "sm", variant = "secondary", label = "Sync Classroom",
  showStatus = true, disabled,
}: {
  onSynced?: (run: SyncSnapshot) => void;
  size?: "sm" | "lg";
  variant?: ButtonVariant;
  label?: string;
  showStatus?: boolean;
  disabled?: boolean;
}) {
  const [failed, setFailed] = useState<string | null>(null);
  const sync = useClassroomSync(onSynced);

  return (
    <span className="row wrap" style={{ gap: 8, alignItems: "center", minWidth: 0 }}>
      <Button size={size} variant={variant} icon="refresh"
        loading={sync.busy} disabled={Boolean(disabled) || sync.busy}
        tooltip={"Fetch the latest courses, assignments, students and "
          + "submissions from Google Classroom using the current connection"}
        onClick={() => { setFailed(null); sync.sync().catch((e) => setFailed((e as Error).message)); }}>
        {sync.busy ? "Syncing…" : label}
      </Button>
      {showStatus && <SyncStatusLine run={sync.run} />}
      {(failed ?? sync.error) && (
        <Badge tone="err" icon="alert">
          {failed ?? sync.error}
        </Badge>
      )}
      {!failed && !sync.error && sync.joined && sync.busy && (
        <span className="faint small">joined a sync already running</span>
      )}
    </span>
  );
}
