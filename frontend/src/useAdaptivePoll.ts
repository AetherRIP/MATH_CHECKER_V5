/**
 * Adaptive polling — one cadence rule for every page that has to watch
 * background work.
 *
 * The pages that show live job state used to poll on a fixed 2.5s/5s timer that
 * ran for as long as the page was open, busy or not. That is exactly the
 * continuous background chatter §17 asks us not to create: an idle Task Manager
 * still hit the API ~1,700 times an hour for a list that had not changed, on
 * machines that are also doing the actual correction work.
 *
 * This hook keeps the responsive feel where it is real and removes the noise
 * where it is not:
 *
 *   * busy   → the fast interval (default 2.5s), so progress still moves
 *   * idle   → the slow interval (default 20s), so a teacher leaving the page
 *              open all afternoon costs almost nothing
 *   * hidden → the window is not even on screen, so nothing is polled; the
 *              first tick after it comes back is immediate, which is why the
 *              page still looks current the moment you switch to it
 *
 * The next timeout is scheduled *after* each run and with the cadence that run
 * reported, so a page that goes quiet slows down on its own without any extra
 * bookkeeping, and a component that unmounts cancels its pending timeout.
 */
import { useEffect, useRef } from "react";

export interface AdaptivePollOptions {
  /** Is real work in flight right now? Decides fast vs idle cadence. */
  active: boolean;
  /** Milliseconds between ticks while work is in flight. */
  fast?: number;
  /** Milliseconds between ticks when nothing is happening. */
  idle?: number;
  /** Set false to stop the poll entirely (e.g. permission not granted). */
  enabled?: boolean;
  /** Run once on mount instead of waiting for the first interval. */
  immediate?: boolean;
}

export function useAdaptivePoll(
  run: () => void | Promise<void>,
  { active, fast = 2500, idle = 20_000, enabled = true, immediate = true }: AdaptivePollOptions,
): void {
  const runRef = useRef(run);
  const activeRef = useRef(active);
  runRef.current = run;
  activeRef.current = active;

  useEffect(() => {
    if (!enabled) return;
    let cancelled = false;
    let timer: number | undefined;

    const tick = async () => {
      if (cancelled) return;
      if (!document.hidden) {
        try {
          await runRef.current();
        } catch {
          // A failed refresh is not a reason to stop watching: the next tick
          // reports the problem again, and every one of these loaders already
          // surfaces its own error state.
        }
      }
      if (cancelled) return;
      schedule();
    };

    const schedule = () => {
      if (cancelled) return;
      const wait = document.hidden ? idle : (activeRef.current ? fast : idle);
      timer = window.setTimeout(() => { void tick(); }, wait);
    };

    // Coming back to a hidden tab catches up straight away instead of waiting
    // out an idle interval the user can see.
    const onVisible = () => {
      if (cancelled || document.hidden) return;
      if (timer !== undefined) window.clearTimeout(timer);
      void tick();
    };
    document.addEventListener("visibilitychange", onVisible);

    if (immediate) void tick();
    else schedule();

    return () => {
      cancelled = true;
      if (timer !== undefined) window.clearTimeout(timer);
      document.removeEventListener("visibilitychange", onVisible);
    };
  }, [enabled, fast, idle, immediate]);
}

export default useAdaptivePoll;
