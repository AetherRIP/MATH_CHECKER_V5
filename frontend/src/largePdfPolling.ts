/**
 * Polling policy for batched (large-PDF) jobs.
 *
 * A job that has reached a terminal status will not change on its own, so a
 * page must stop asking for it. The backend's authoritative terminal set is
 * `large_pdf/states.py` (TERMINAL = COMPLETED, AWAITING_REVIEW, FAILED,
 * CANCELLED). `ERROR` is kept for compatibility with older payloads.
 *
 * Errors are bounded: a transient failure is retried with exponential backoff,
 * and after a fixed number of consecutive failures polling gives up instead of
 * running forever against a job that no longer exists.
 */

export const LARGE_POLL_INTERVAL_MS = 2500;
export const LARGE_POLL_MAX_BACKOFF_MS = 30_000;
export const LARGE_POLL_MAX_FAILURES = 5;

const TERMINAL_STATUSES: ReadonlySet<string> = new Set([
  "COMPLETED",
  "AWAITING_REVIEW",
  "FAILED",
  "CANCELLED",
  "ERROR",
]);

/** True when the job will not change without a user action. */
export function isLargeJobTerminal(status: string | null | undefined): boolean {
  return status ? TERMINAL_STATUSES.has(status) : false;
}

/**
 * Delay before the next poll after `consecutiveFailures` failed requests in a
 * row, or `null` when polling should stop. Zero failures uses the normal
 * interval.
 */
export function nextLargePollDelay(consecutiveFailures: number): number | null {
  if (consecutiveFailures <= 0) return LARGE_POLL_INTERVAL_MS;
  if (consecutiveFailures >= LARGE_POLL_MAX_FAILURES) return null;
  return Math.min(LARGE_POLL_INTERVAL_MS * 2 ** consecutiveFailures,
    LARGE_POLL_MAX_BACKOFF_MS);
}

export interface PollTimers {
  setTimeout(callback: () => void, ms: number): unknown;
  clearTimeout(handle: unknown): void;
}

export interface LargeJobPollOptions<T extends { status: string }> {
  /** Fetches the current job state (one request per call). */
  fetchJob: () => Promise<T>;
  /** Receives every successful job snapshot, including the terminal one. */
  onJob: (job: T) => void;
  /** Timer source; defaults to the global timers. */
  timers?: PollTimers;
}

/**
 * Poll one batched job until it reaches a terminal status, a bounded number
 * of consecutive errors, or the returned stop function is called.
 *
 * Stopping cancels any pending timer and discards a response that arrives
 * after the stop, so an unmounted component is never updated.
 * Returns the stop function.
 */
export function startLargeJobPoll<T extends { status: string }>(
  options: LargeJobPollOptions<T>,
): () => void {
  const timers: PollTimers = options.timers ?? {
    setTimeout: (callback, ms) => globalThis.setTimeout(callback, ms),
    clearTimeout: (handle) => globalThis.clearTimeout(handle as ReturnType<typeof setTimeout>),
  };
  let stopped = false;
  let pending: unknown = undefined;
  let failures = 0;

  const schedule = (delay: number) => {
    pending = timers.setTimeout(poll, delay);
  };

  const poll = () => {
    pending = undefined;
    if (stopped) return;
    options.fetchJob().then(
      (job) => {
        if (stopped) return;
        failures = 0;
        options.onJob(job);
        if (isLargeJobTerminal(job.status)) return;
        schedule(nextLargePollDelay(0) ?? LARGE_POLL_INTERVAL_MS);
      },
      () => {
        if (stopped) return;
        failures += 1;
        const delay = nextLargePollDelay(failures);
        if (delay !== null) schedule(delay);
      },
    );
  };

  poll();

  return () => {
    stopped = true;
    if (pending !== undefined) timers.clearTimeout(pending);
    pending = undefined;
  };
}
