/**
 * Browser side of the server lifecycle: "I am here", "I am leaving", and the
 * honest state of the link.
 *
 * The backend decides when to stop — a page cannot stop a server, and must not
 * be able to. What the page does is TELL the backend it is still there (a small
 * heartbeat) and SAY GOODBYE when it is being closed, so a tab that closes is
 * noticed at once instead of after the session timeout.
 *
 * Design decisions that matter:
 *
 *   * one id per TAB (``sessionStorage``), so two tabs are two sessions and the
 *     app stays up for as long as any one of them is open;
 *   * a GET heartbeat every ``heartbeat_seconds`` (default 30s) — no body, no
 *     database write, no CSRF token, nothing that a status request should not
 *     be;
 *   * a HIDDEN tab keeps beating, just slower (never slower than the session
 *     timeout, or the app could shut down under a teacher who is coming back to
 *     it), which is the honest reading of "reduce requests while hidden";
 *   * ``pagehide`` says goodbye through ``keepalive``, and is treated as a
 *     bonus: browsers do not always deliver it, so the timeout is the real
 *     safety net;
 *   * a failed heartbeat is retried quickly a couple of times before the UI
 *     admits the server is away, because one dropped request is not evidence.
 */
import { useEffect, useRef, useState } from "react";
import { lifecycleHeartbeat, lifecycleLeave, type LifecycleStatus } from "./api";

const SID_KEY = "mc5.tab.session";

/** Where the link to the backend stands, as far as this tab can tell. */
export type LinkState = "connecting" | "live" | "away";

/** Per-tab session id. Survives a reload (same tab), dies with the tab. */
export function tabSessionId(): string {
  try {
    const existing = window.sessionStorage.getItem(SID_KEY);
    if (existing) return existing;
    const fresh = typeof crypto !== "undefined" && "randomUUID" in crypto
      ? crypto.randomUUID()
      : `tab-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}`;
    window.sessionStorage.setItem(SID_KEY, fresh);
    return fresh;
  } catch {
    // Private mode with storage disabled: still a session, just not persisted.
    return `tab-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}`;
  }
}

export interface ServerSession {
  status: LifecycleStatus | null;
  link: LinkState;
  /** The id this tab is beating with (useful for diagnostics and tests). */
  sid: string;
}

export function useServerSession(): ServerSession {
  const sidRef = useRef<string>("");
  if (!sidRef.current) sidRef.current = tabSessionId();
  const [status, setStatus] = useState<LifecycleStatus | null>(null);
  const [link, setLink] = useState<LinkState>("connecting");
  const failures = useRef(0);
  const cadence = useRef({ heartbeat: 30, timeout: 90 });

  useEffect(() => {
    const sid = sidRef.current;
    let cancelled = false;
    let timer: number | undefined;
    let left = false;

    const schedule = () => {
      if (cancelled) return;
      const { heartbeat, timeout } = cadence.current;
      let wait: number;
      if (failures.current > 0) {
        // The backend is away or restarting: retry quickly, but back off from
        // 3s to 10s so a machine that is genuinely down is not hammered.
        wait = Math.min(3000 * failures.current, 10_000);
      } else if (document.hidden) {
        // A background tab is still a session; it just beats less often — up to
        // twice the normal gap, but never later than 60% of the session timeout
        // and never sooner than the configured heartbeat. Slowing it down past
        // the timeout would let the app stop under a user who is about to come
        // back to the tab.
        const gap = Math.max(heartbeat, Math.min(heartbeat * 2, timeout * 0.6));
        wait = gap * 1000;
      } else {
        wait = heartbeat * 1000;
      }
      timer = window.setTimeout(() => { void beat(); }, wait);
    };

    const beat = async () => {
      if (cancelled || left) return;
      try {
        const next = await lifecycleHeartbeat(sid, document.hidden);
        if (cancelled) return;
        const reconnected = failures.current >= 2;
        failures.current = 0;
        cadence.current = {
          heartbeat: next.heartbeat_seconds || 30,
          timeout: next.session_timeout_seconds || 90,
        };
        setStatus(next);
        setLink("live");
        if (reconnected) {
          // The app came back (an idle stop, a restart, a crashed backend).
          // Tell the shell so it re-reads the session and the dashboard now
          // instead of waiting out its own interval.
          window.dispatchEvent(new CustomEvent("mc5:server-back"));
        }
      } catch {
        if (cancelled) return;
        failures.current += 1;
        if (failures.current >= 2) setLink("away");
      }
      schedule();
    };

    const onVisibility = () => {
      if (cancelled || left) return;
      if (timer !== undefined) window.clearTimeout(timer);
      void beat();
    };

    const onPageHide = () => {
      // The tab is going away: say so. Nothing else is sent afterwards.
      left = true;
      if (timer !== undefined) window.clearTimeout(timer);
      lifecycleLeave(sid);
    };

    document.addEventListener("visibilitychange", onVisibility);
    window.addEventListener("pagehide", onPageHide);
    void beat();

    return () => {
      cancelled = true;
      if (timer !== undefined) window.clearTimeout(timer);
      document.removeEventListener("visibilitychange", onVisibility);
      window.removeEventListener("pagehide", onPageHide);
    };
  }, []);

  return { status, link, sid: sidRef.current };
}

/**
 * A slim banner that appears only when the backend is genuinely unreachable.
 *
 * It replaces a page full of failing requests with one honest sentence, and it
 * says what the app is doing about it. While the relay behind the app is
 * starting the backend again, this is the only thing a teacher ever sees.
 */
export function ServerLifecycleBanner() {
  const { link } = useServerSession();
  if (link !== "away") return null;
  return (
    <div className="notice warn lifecycle-away" role="status">
      <span>
        <strong>Reconnecting to Math Checker…</strong> the app is not answering
        right now. It starts again by itself when nobody is using it, and this
        page will carry on as soon as it is back. Nothing you had queued is
        lost — a correction in flight resumes from the step it reached.
      </span>
      <button className="btn secondary sm" type="button"
        onClick={() => window.location.reload()}>
        Reload now
      </button>
    </div>
  );
}
