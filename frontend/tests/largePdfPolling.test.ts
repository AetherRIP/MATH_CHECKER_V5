// Regression tests for the large-PDF job poller.
// Run from `frontend/` with `node --test "tests/*.test.ts"` (the glob is
// resolved by Node, so the same command works in cmd.exe and in a POSIX
// shell). A bare directory argument is NOT accepted by Node's runner: this
// Node version reports "Cannot find module .../tests" and the suite silently
// looks like a failure. The single-file form is in the root README.
// Node runs these TypeScript sources directly; no test framework is added.
import { test } from "node:test";
import assert from "node:assert/strict";
import {
  isLargeJobTerminal,
  LARGE_POLL_INTERVAL_MS,
  LARGE_POLL_MAX_FAILURES,
  nextLargePollDelay,
  startLargeJobPoll,
} from "../src/largePdfPolling.ts";

type Job = { status: string };

/** Manual clock: callbacks run only when the test advances time. */
function fakeTimers() {
  let now = 0;
  let nextId = 1;
  const queue = new Map<number, { at: number; cb: () => void }>();
  return {
    timers: {
      setTimeout(cb: () => void, ms: number): unknown {
        const id = nextId++;
        queue.set(id, { at: now + ms, cb });
        return id;
      },
      clearTimeout(handle: unknown): void {
        queue.delete(handle as number);
      },
    },
    pending: () => queue.size,
    advance(ms: number): void {
      now += ms;
      const due = [...queue.entries()]
        .filter(([, t]) => t.at <= now)
        .sort((a, b) => a[1].at - b[1].at);
      for (const [id, t] of due) {
        if (!queue.has(id)) continue;
        queue.delete(id);
        t.cb();
      }
    },
  };
}

/** Let pending promise callbacks run. */
const flush = () => new Promise<void>((resolve) => setImmediate(resolve));

test("terminal statuses stop polling and active statuses do not", () => {
  for (const s of ["COMPLETED", "AWAITING_REVIEW", "FAILED", "CANCELLED", "ERROR"]) {
    assert.equal(isLargeJobTerminal(s), true, s);
  }
  for (const s of ["QUEUED", "INSPECTING", "PLANNING", "READY", "PROCESSING",
                   "VALIDATING", "MERGING"]) {
    assert.equal(isLargeJobTerminal(s), false, s);
  }
  assert.equal(isLargeJobTerminal(null), false);
  assert.equal(isLargeJobTerminal(undefined), false);
});

test("backoff is bounded and gives up after the configured failures", () => {
  assert.equal(nextLargePollDelay(0), LARGE_POLL_INTERVAL_MS);
  assert.equal(nextLargePollDelay(1), 5000);
  assert.equal(nextLargePollDelay(2), 10_000);
  assert.equal(nextLargePollDelay(3), 20_000);
  assert.equal(nextLargePollDelay(4), 30_000);          // capped
  assert.equal(nextLargePollDelay(LARGE_POLL_MAX_FAILURES), null);
  assert.equal(nextLargePollDelay(LARGE_POLL_MAX_FAILURES + 1), null);
});

test("polls until a terminal status, delivers it, then schedules nothing", async () => {
  const ft = fakeTimers();
  const script: Job[] = [{ status: "PROCESSING" }, { status: "MERGING" },
                         { status: "COMPLETED" }];
  const seen: string[] = [];
  let calls = 0;
  startLargeJobPoll<Job>({
    fetchJob: () => Promise.resolve(script[calls++]),
    onJob: (j) => seen.push(j.status),
    timers: ft.timers,
  });
  await flush();
  ft.advance(LARGE_POLL_INTERVAL_MS);
  await flush();
  ft.advance(LARGE_POLL_INTERVAL_MS);
  await flush();
  ft.advance(LARGE_POLL_INTERVAL_MS * 10);
  await flush();
  assert.equal(calls, 3);
  assert.deepEqual(seen, ["PROCESSING", "MERGING", "COMPLETED"]);
  assert.equal(ft.pending(), 0, "no timer may remain after a terminal status");
});

test("AWAITING_REVIEW is terminal for polling too", async () => {
  const ft = fakeTimers();
  let calls = 0;
  startLargeJobPoll<Job>({
    fetchJob: () => { calls++; return Promise.resolve({ status: "AWAITING_REVIEW" }); },
    onJob: () => {},
    timers: ft.timers,
  });
  await flush();
  ft.advance(60_000);
  await flush();
  assert.equal(calls, 1);
  assert.equal(ft.pending(), 0);
});

test("persistent errors back off and then stop instead of running forever", async () => {
  const ft = fakeTimers();
  let calls = 0;
  startLargeJobPoll<Job>({
    fetchJob: () => { calls++; return Promise.reject(new Error("404 not found")); },
    onJob: () => assert.fail("no job should be delivered"),
    timers: ft.timers,
  });
  for (let i = 0; i < 20; i++) {
    await flush();
    ft.advance(60_000);
  }
  await flush();
  assert.equal(calls, LARGE_POLL_MAX_FAILURES);
  assert.equal(ft.pending(), 0, "polling must give up with no pending timer");
});

test("a transient error recovers and resets the failure count", async () => {
  const ft = fakeTimers();
  const outcomes: Array<Job | Error> = [
    new Error("network"), { status: "PROCESSING" }, { status: "COMPLETED" },
  ];
  let calls = 0;
  const seen: string[] = [];
  startLargeJobPoll<Job>({
    fetchJob: () => {
      const next = outcomes[calls++];
      return next instanceof Error ? Promise.reject(next) : Promise.resolve(next);
    },
    onJob: (j) => seen.push(j.status),
    timers: ft.timers,
  });
  await flush();
  ft.advance(5000);       // backoff after the first failure
  await flush();
  ft.advance(LARGE_POLL_INTERVAL_MS);
  await flush();
  assert.equal(calls, 3);
  assert.deepEqual(seen, ["PROCESSING", "COMPLETED"]);
  assert.equal(ft.pending(), 0);
});

test("stopping cancels the pending timer", async () => {
  const ft = fakeTimers();
  let calls = 0;
  const stop = startLargeJobPoll<Job>({
    fetchJob: () => { calls++; return Promise.resolve({ status: "PROCESSING" }); },
    onJob: () => {},
    timers: ft.timers,
  });
  await flush();
  assert.equal(ft.pending(), 1);
  stop();
  assert.equal(ft.pending(), 0);
  ft.advance(60_000);
  await flush();
  assert.equal(calls, 1, "no request may be sent after stop");
});

test("a response arriving after stop is discarded and schedules nothing", async () => {
  const ft = fakeTimers();
  let resolveFetch: (job: Job) => void = () => {};
  const delivered: string[] = [];
  const stop = startLargeJobPoll<Job>({
    fetchJob: () => new Promise<Job>((resolve) => { resolveFetch = resolve; }),
    onJob: (j) => delivered.push(j.status),
    timers: ft.timers,
  });
  stop();                                   // unmount while the request is in flight
  resolveFetch({ status: "PROCESSING" });
  await flush();
  assert.deepEqual(delivered, []);
  assert.equal(ft.pending(), 0);
});

test("a fresh poll after stop works independently (restart after user action)", async () => {
  const ft = fakeTimers();
  const delivered: string[] = [];
  const stop = startLargeJobPoll<Job>({
    fetchJob: () => Promise.resolve({ status: "FAILED" }),
    onJob: (j) => delivered.push(j.status),
    timers: ft.timers,
  });
  await flush();
  stop();
  startLargeJobPoll<Job>({
    fetchJob: () => Promise.resolve({ status: "QUEUED" }),
    onJob: (j) => delivered.push(j.status),
    timers: ft.timers,
  });
  await flush();
  assert.deepEqual(delivered, ["FAILED", "QUEUED"]);
  assert.equal(ft.pending(), 1);
});
