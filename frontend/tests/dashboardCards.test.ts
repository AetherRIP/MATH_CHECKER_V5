/**
 * Dashboard coverage against SEEDED data — every populated card loop.
 *
 * Why: each Dashboard card has an empty state and a populated one, and a stock
 * install only ever renders the empty ones. The loops that draw real work (queue
 * rows with their badges, the throughput line, the pipeline donut, live job
 * rows, the usage gauges, the per-assignment bars, the failed-corrections
 * notice) therefore went untested — and so did the layout hazard that only the
 * populated branches create (the charts' hidden a11y tables, which are the
 * absolutely positioned elements that used to stretch the whole document).
 *
 * The app is booted against a seeded throwaway data directory by
 * `tests/helpers/dashboardApp.ts`; every expectation is read back from the
 * app's own API, so the assertions compare the DOM with the real data instead
 * of with hard-coded numbers.
 *
 * Requirements: a Chrome/Chromium/Edge binary (CHROME_PATH) and a Python with
 * uvicorn+fastapi (the repo `.venv`, or MC5_PYTHON). Set DASHBOARD_FIXTURE_URL
 * to reuse an app that already runs against seeded data. Skips with an explicit
 * reason when neither is available — a skip is never a pass.
 *
 * Run from `frontend/` with:
 *   node --test "tests/*.test.ts"
 */
import { test } from "node:test";
import assert from "node:assert/strict";
import { chromiumPath, launchBrowser, type PageHandle } from "./helpers/browser.ts";
import { fixtureRuntime, startDashboardApp } from "./helpers/dashboardApp.ts";

const ACTIVE_STATUSES = ["QUEUED", "PREPARING", "DOWNLOADING", "UPLOADING", "MARKING",
  "VERIFYING", "SAVING", "WAITING_FOR_API", "RETRYING"];

interface DashboardPayload {
  corrections_today: number; pending: number; processing: number;
  completed: number; failed: number;
  jobs: Record<string, number>;
  api_health: { healthy: number; total: number; has_keys: boolean };
  classroom: { configured: boolean; connected: boolean };
  first_run: boolean;
}

interface AssignmentRow {
  id: string; title: string; checked: number; submitted: number;
  needs_review: number; failed: number; processing: number;
  average: number | null;
}

interface ActivityPayload {
  days: { day: string; results: number }[];
  totals: { results: number; needs_review: number; jobs_started: number };
  window_days: number;
}

interface JobRow {
  id: string; status: string; student_name: string | null;
  stage_detail: string | null; progress: number;
  attempts: number; max_attempts: number;
}

interface UsagePayload {
  hour: { total: number }; day: { total: number };
  limits: { hourly_mb: number; daily_mb: number };
}

interface AttentionPayload { total: number; items: { type: string; title: string }[] }

interface Snapshot {
  hero: { value: string | null; stats: { label: string; n: string; color: string | null }[] };
  queue: {
    hint: string | null;
    rows: { text: string; badges: string[]; progressPct: number | null }[];
  } | null;
  attention: { header: string | null; items: { title: string; detail: string }[] } | null;
  throughput: { svg: boolean; circles: number; rows: [string, string][] } | null;
  pipeline: { legend: string[]; center: string | null; rows: [string, string][] } | null;
  live: {
    empty: boolean;
    rows: { name: string; status: string; stage: string; attempts: string; progressPct: number | null }[];
  } | null;
  health: {
    keys: string | null; classroom: string | null;
    gauges: { detail: string; aria: string | null }[];
  } | null;
  performance: { bars: string[]; rows: [string, string][] } | null;
  failedNotice: { text: string; button: string | null } | null;
  scroll: { extra: number; escapees: { what: string; bottom: number }[] };
}

const SNAPSHOT = String.raw`(() => {
  const text = (el) => (el ? (el.textContent || "").trim() : null);
  const cardFor = (needle) => [...document.querySelectorAll(".main-inner .card")]
    .find((c) => ((c.querySelector("h3") || {}).innerText || "").includes(needle)) || null;
  const tableRows = (root) => [...root.querySelectorAll("table.visually-hidden tr")]
    .map((tr) => [text(tr.querySelector("th")), text(tr.querySelector("td"))]);

  const heroCard = document.querySelector(".hero-band");
  const hero = {
    value: text(heroCard && heroCard.querySelector(".hero-value")),
    stats: heroCard ? [...heroCard.querySelectorAll(".hero-stat")].map((s) => ({
      label: text(s.querySelector(".l")),
      n: text(s.querySelector(".n")),
      color: (s.querySelector(".n") || {}).style ? s.querySelector(".n").style.color || null : null,
    })) : [],
  };

  const queueCard = cardFor("Marking queue");
  const queue = queueCard ? {
    hint: text(queueCard.querySelector("h3 .hint")),
    rows: [...queueCard.querySelectorAll(".attention-item")].map((row) => {
      const bar = row.querySelector("[role=progressbar]");
      return {
        text: (row.innerText || "").replace(/\s+/g, " ").trim(),
        badges: [...row.querySelectorAll(".badge")].map((b) => text(b)),
        progressPct: bar ? Number(bar.getAttribute("aria-valuenow")) : null,
      };
    }),
  } : null;

  const attentionCard = cardFor("What needs my attention?");
  const attention = attentionCard ? {
    header: text(attentionCard.querySelector(".small.muted")),
    items: [...attentionCard.querySelectorAll(".attention-item")].map((i) => ({
      title: text(i.querySelector(".ai-title")),
      detail: text(i.querySelector(".ai-detail")),
    })),
  } : null;

  const throughputCard = cardFor("Marking throughput");
  const throughput = throughputCard ? {
    svg: !!throughputCard.querySelector("svg.chart-svg"),
    circles: throughputCard.querySelectorAll("svg.chart-svg circle").length,
    rows: tableRows(throughputCard),
  } : null;

  const pipelineCard = cardFor("Pipeline right now");
  const pipeline = pipelineCard ? {
    legend: [...pipelineCard.querySelectorAll(".chart-legend .key")].map((k) => text(k)),
    center: (() => {
      const svg = pipelineCard.querySelector("svg.chart-svg");
      if (!svg) return null;
      const label = [...svg.querySelectorAll("text")].map((t) => text(t))
        .find((t) => t !== null && /^[0-9]+$/.test(t));
      return label === undefined ? null : label;
    })(),
    rows: tableRows(pipelineCard),
  } : null;

  const liveCard = cardFor("Live job activity");
  const live = liveCard ? {
    empty: !!liveCard.querySelector(".empty"),
    rows: [...liveCard.querySelectorAll(".card > .stack > .stack")].map((row) => {
      const bar = row.querySelector("[role=progressbar]");
      const lines = row.querySelectorAll(".row");
      const foot = lines.length ? lines[lines.length - 1] : null;
      return {
        name: text(lines.length ? lines[0].querySelector("span") : null),
        status: text(row.querySelector(".badge")),
        stage: text(foot && foot.children[0]),
        attempts: text(foot && foot.children[1]),
        progressPct: bar ? Number(bar.getAttribute("aria-valuenow")) : null,
      };
    }),
  } : null;

  const healthCard = cardFor("System health");
  const statValue = (label) => {
    if (!healthCard) return null;
    // textContent, not innerText: the label is upper-cased by CSS.
    const stat = [...healthCard.querySelectorAll(".stat")].find((s) =>
      text(s.querySelector(".label")) === label);
    if (!stat) return null;
    const badge = stat.querySelector(".badge");
    return text(badge) || text(stat.querySelector(".value"));
  };
  const health = healthCard ? {
    keys: statValue("AI keys healthy"),
    classroom: statValue("Classroom"),
    gauges: [...healthCard.querySelectorAll(".chart-card")].map((c) => ({
      detail: text(c.querySelector(".faint")),
      aria: c.querySelector("[role=img]") ? c.querySelector("[role=img]").getAttribute("aria-label") : null,
    })),
  } : null;

  const perfCard = cardFor("Class performance");
  const performance = perfCard ? {
    bars: [...perfCard.querySelectorAll("svg.chart-svg rect")].map((r) => text(r.querySelector("title"))),
    rows: tableRows(perfCard),
  } : null;

  const notice = liveCard ? liveCard.querySelector(".notice.err") : null;
  const failedNotice = notice ? {
    text: (notice.innerText || "").replace(/\s+/g, " ").trim(),
    button: text(notice.querySelector("button")),
  } : null;

  const se = document.scrollingElement;
  const shellHeight = se.clientHeight;
  const escapees = [];
  document.querySelectorAll(".app-shell *").forEach((el) => {
    const cs = getComputedStyle(el);
    if (cs.position !== "absolute" && cs.position !== "fixed") return;
    const op = el.offsetParent;
    if (op && op !== document.body) return;      // still clipped by the shell
    const box = el.getBoundingClientRect();
    if (box.bottom > shellHeight + 1 || box.right > se.clientWidth + 1) {
      escapees.push({ what: el.tagName + "." + String(el.className || "").slice(0, 32), bottom: Math.round(box.bottom) });
    }
  });

  return {
    hero, queue, attention, throughput, pipeline, live, health, performance, failedNotice,
    scroll: { extra: se.scrollHeight - shellHeight, escapees },
  };
})()`;

const sleep = (ms: number) => new Promise<void>((resolve) => setTimeout(resolve, ms));

async function getJson<T>(base: string, path: string): Promise<T> {
  const res = await fetch(base + path, { signal: AbortSignal.timeout(15_000) });
  assert.ok(res.ok, `${path} answered HTTP ${res.status}`);
  return (await res.json()) as T;
}

/** Filters the same rule the component uses, so the DOM can be compared. */
const outstanding = (a: AssignmentRow) =>
  Math.max(0, a.submitted - a.checked) + a.needs_review + a.failed + a.processing;

const expectedBadges = (a: AssignmentRow) => [
  a.processing > 0 ? `${a.processing} processing` : null,
  a.failed > 0 ? `${a.failed} failed` : null,
  a.needs_review > 0 ? `${a.needs_review} review` : null,
  Math.max(0, a.submitted - a.checked) > 0 ? `${Math.max(0, a.submitted - a.checked)} to mark` : null,
].filter((b): b is string => b !== null);

const executable = chromiumPath();
const runtime = fixtureRuntime();
// `false` (not null): Node's runner reads a null skip option as "skip" and
// would report a fully executed test as skipped.
const skipReason: string | false = !executable
  ? "no Chrome/Chromium/Edge binary found (set CHROME_PATH to enable)"
  : runtime.reason ?? false;

test("the populated Dashboard renders every card's real data", { skip: skipReason }, async (t) => {
  const app = await startDashboardApp();
  const browser = await launchBrowser(executable as string);
  try {
    const [dash, assignmentsPayload, activity, jobsPayload, usage, attention] = await Promise.all([
      getJson<DashboardPayload>(app.url, "/api/dashboard"),
      getJson<{ assignments: AssignmentRow[] }>(app.url, "/api/assignments"),
      getJson<ActivityPayload>(app.url, "/api/analytics/activity?days=30"),
      getJson<{ jobs: JobRow[] }>(app.url, "/api/jobs?limit=20"),
      getJson<UsagePayload>(app.url, "/api/usage"),
      getJson<AttentionPayload>(app.url, "/api/attention"),
    ]);
    const assignments = assignmentsPayload.assignments;
    const active = jobsPayload.jobs.filter((j) => ACTIVE_STATUSES.includes(j.status));

    const seeded = activity.totals.results > 0 && jobsPayload.jobs.length > 0
      && assignments.some((a) => a.checked > 0) && active.length > 0;
    if (!seeded) {
      const how = app.owned
        ? "the fixture did not seed the dashboard"
        : `the app at ${app.url} carries no dashboard data (start the fixture instead)`;
      if (!app.owned) { t.skip(how); return; }
      assert.fail(how);
    }

    const page: PageHandle = await browser.newPage(`${app.url}/#/dashboard`);
    await page.waitFor("document.readyState === 'complete' && !!document.querySelector('.app-shell')",
      30_000, "the app shell");
    assert.equal(await page.evaluate<boolean>("!!document.querySelector('.signin-wrap')"), false,
      "the fixture app must not be behind sign-in");
    assert.equal(await page.evaluate<boolean>("!!document.querySelector('.setup-wizard, .wizard')"), false,
      "the fixture app must be past the first-run wizard (config.toml onboarded = true)");

    // Every lazy card must have resolved (skeletons gone) before measuring.
    await page.waitFor(
      "!!document.querySelector('.hero-band') && !document.querySelector('.skeleton') "
      + "&& !document.querySelector('.skeleton-lines') && !!document.querySelector('.chart-svg')",
      30_000, "the dashboard cards to load");
    // The hero number counts up on first paint: wait for it to land on the API value.
    await page.waitFor(
      `(document.querySelector('.hero-value') || {}).textContent === "${dash.corrections_today}"`,
      15_000, "the hero counter to settle");
    if (active.length > 0) {
      await page.waitFor(
        "!!document.querySelector('[role=progressbar][aria-label^=\"Correcting\"]')",
        20_000, "the live job rows (SSE handshake + /jobs)");
    }
    const snap = await page.evaluate<Snapshot>(SNAPSHOT);

    await t.test("hero band shows the live counters", () => {
      assert.equal(snap.hero.value, String(dash.corrections_today),
        "the hero value is the API's corrections-today");
      const expected = [
        ["Pending", dash.pending],
        ["Processing", dash.processing],
        ["Completed", dash.completed],
        ["Failed", dash.failed],
      ] as const;
      assert.deepEqual(snap.hero.stats.map((s) => [s.label, Number(s.n)]), expected.map((e) => [e[0], e[1]]),
        "the hero stats match /api/dashboard");
      // Both conditional colour branches only render when the count is non-zero.
      assert.ok(dash.processing > 0 && dash.failed > 0,
        "the fixture must exercise the processing/failed colour branches");
      assert.equal(snap.hero.stats[1].color, "var(--accent)", "processing is highlighted");
      assert.equal(snap.hero.stats[3].color, "var(--err)", "failures are shown in the error tone");
    });

    await t.test("marking queue lists exactly the work outstanding, with its badges", () => {
      assert.ok(snap.queue, "the marking queue card rendered");
      const rows = assignments.filter((a) => outstanding(a) > 0).slice(0, 6);
      assert.ok(rows.length > 0, "the fixture must leave work outstanding");
      assert.equal(snap.queue!.rows.length, rows.length,
        `queue rows ${snap.queue!.rows.length} != outstanding assignments ${rows.length}`);
      assert.equal(snap.queue!.hint, `${rows.length} assignment(s) with work outstanding`);
      for (const a of rows) {
        const row = snap.queue!.rows.find((r) => r.text.includes(a.title));
        assert.ok(row, `a queue row for "${a.title}"`);
        assert.deepEqual(row!.badges, expectedBadges(a),
          `badges on the "${a.title}" row (review/failed/to mark)`);
        assert.notEqual(row!.progressPct, null, `a progress bar on the "${a.title}" row`);
      }
      // An assignment with nothing outstanding must not be listed at all.
      for (const a of assignments.filter((x) => outstanding(x) === 0)) {
        assert.ok(!snap.queue!.rows.some((r) => r.text.includes(a.title)),
          `"${a.title}" has nothing outstanding and must be absent from the queue`);
      }
    });

    await t.test("the attention card lists the real feed", () => {
      assert.ok(snap.attention, "the attention card rendered");
      assert.ok(attention.total > 0, "the fixture must raise attention items");
      assert.equal(snap.attention!.items.length, attention.total,
        "one row per /api/attention item");
      assert.equal(snap.attention!.header,
        `${attention.total} item${attention.total === 1 ? "" : "s"} need you`);
      for (const item of snap.attention!.items) {
        assert.ok(item.title && item.title.length > 0, "every attention row has a title");
        assert.ok(item.detail && item.detail.length > 0, "every attention row has a detail line");
      }
    });

    await t.test("throughput chart draws the API's window", () => {
      assert.ok(snap.throughput, "the throughput card rendered");
      assert.equal(snap.throughput!.svg, true, "the line chart drew an svg");
      assert.deepEqual(snap.throughput!.rows.map((r) => Number(r[1])),
        activity.days.map((d) => d.results),
        "the hidden data table carries one row per day, in order");
      assert.equal(snap.throughput!.circles, activity.days.length,
        "one point per day on the line");
      assert.equal(
        snap.throughput!.rows.reduce((n, r) => n + Number(r[1]), 0), activity.totals.results,
        "the charted total is the API's total");
      assert.ok(activity.days.filter((d) => d.results > 0).length >= 3,
        "the fixture must spread results over several days");
    });

    await t.test("pipeline donut matches the job counts", () => {
      assert.ok(snap.pipeline, "the pipeline card rendered");
      const expected = Object.entries(dash.jobs).filter(([, n]) => n > 0)
        .map(([status, n]) => [status, String(n)] as [string, string]);
      assert.deepEqual(snap.pipeline!.rows, expected, "hidden table = /api/dashboard jobs map");
      assert.deepEqual(snap.pipeline!.legend,
        expected.map(([status, n]) => `${status} (${n})`), "one legend key per job state");
      assert.equal(snap.pipeline!.center,
        String(expected.reduce((n, [, v]) => n + Number(v), 0)), "the donut's centre is the total");
    });

    await t.test("live job activity lists the in-flight jobs", () => {
      assert.ok(snap.live, "the live activity card rendered");
      const expected = active.slice(0, 5);
      assert.equal(snap.live!.empty, false, "in-flight work must not show the empty state");
      assert.equal(snap.live!.rows.length, expected.length, "one row per active job");
      for (const job of expected) {
        const row = snap.live!.rows.find((r) => r.name === (job.student_name ?? "Student"));
        assert.ok(row, `a live row for ${job.student_name}`);
        assert.equal(row!.status, job.status.replace(/_/g, " "), "the status badge");
        assert.equal(row!.stage, job.stage_detail, "the stage detail line");
        assert.ok(row!.attempts.includes(`attempt ${job.attempts}/${job.max_attempts}`),
          `the attempt counter (saw "${row!.attempts}")`);
        assert.ok(row!.attempts.includes("·"), "the elapsed-to-attempt separator is present");
        assert.equal(row!.progressPct, Math.round(job.progress * 100), "the progress bar value");
      }
    });

    await t.test("system health shows the gauges and key health", () => {
      assert.ok(snap.health, "the system health card rendered");
      assert.equal(snap.health!.keys, `${dash.api_health.healthy}/${dash.api_health.total}`,
        "the AI key badge counts the configured keys");
      const classroom = !dash.classroom.configured ? "Not set up"
        : dash.classroom.connected ? "Connected" : "Offline";
      assert.equal(snap.health!.classroom, classroom, "the Classroom badge matches the API flags");
      const mb = (bytes: number) => (bytes / 1_048_576).toFixed(1);
      assert.deepEqual(snap.health!.gauges.map((g) => g.detail), [
        `${mb(usage.hour.total)} / ${usage.limits.hourly_mb} MB`,
        `${mb(usage.day.total)} / ${usage.limits.daily_mb} MB`,
      ], "both usage gauges show used vs cap");
      assert.ok(snap.health!.gauges.every((g) => g.aria && g.aria.includes(" of ")),
        "each gauge is announced with its numbers");
      assert.ok(usage.hour.total > 0, "the fixture must meter some traffic");
    });

    await t.test("class performance bars match the assignment averages", () => {
      assert.ok(snap.performance, "the class performance card rendered");
      const rated = assignments.filter((a) => a.checked > 0).slice(0, 8);
      assert.ok(rated.length > 0, "the fixture must have corrected work");
      assert.deepEqual(snap.performance!.bars.map((b) => b.split(": ").pop()),
        rated.map((a) => `${Math.round(a.average ?? 0)}%`), "one bar per corrected assignment");
      assert.deepEqual(snap.performance!.rows.map((r) => r[1]),
        rated.map((a) => `${Math.round(a.average ?? 0)}%`), "the hidden table carries the averages");
    });

    await t.test("the failed-corrections notice offers the failure route", () => {
      const failed = assignments.reduce((n, a) => n + a.failed, 0);
      if (failed === 0) { assert.equal(snap.failedNotice, null, "no notice without failures"); return; }
      assert.ok(snap.failedNotice, "failures must raise the notice");
      assert.match(snap.failedNotice!.text, new RegExp(`${failed} correction\\(s\\) failed`));
      assert.equal(snap.failedNotice!.button, "Review failures");
    });

    await t.test("the populated page still cannot scroll the whole site", async () => {
      assert.deepEqual(snap.scroll.escapees, [],
        `positioned content escaped the app shell: ${JSON.stringify(snap.scroll.escapees)}`);
      assert.ok(snap.scroll.extra <= 1,
        `the document scrolls by ${snap.scroll.extra}px with every chart present`);
      await page.evaluate("window.scrollTo(0, 5000); true");
      const scrolled = await page.evaluate<{ top: number; topbarTop: number | null }>(
        "(() => { const bar = document.querySelector('.topbar');"
        + " return { top: document.scrollingElement.scrollTop,"
        + " topbarTop: bar ? Math.round(bar.getBoundingClientRect().top) : null }; })()");
      assert.equal(scrolled.top, 0, "the window must not scroll at all");
      assert.equal(scrolled.topbarTop, 0, "the topbar stays pinned");
    });

    await t.test("the failure button navigates to the task manager", async () => {
      if (assignments.reduce((n, a) => n + a.failed, 0) === 0) return;
      const clicked = await page.evaluate<boolean>(
        "(() => { const card = [...document.querySelectorAll('.main-inner .card')]"
        + ".find((c) => ((c.querySelector('h3') || {}).innerText || '').includes('Live job activity'));"
        + " const btn = card && [...card.querySelectorAll('button')]"
        + ".find((b) => b.innerText.trim() === 'Review failures');"
        + " if (!btn) return false; btn.click(); return true; })()");
      assert.ok(clicked, "the Review failures button exists");
      await sleep(400);
      assert.equal(await page.evaluate<string>("location.hash"), "#/tasks",
        "the button routes to the task manager");
    });
  } finally {
    await browser.close();
    await app.stop();
  }
});
