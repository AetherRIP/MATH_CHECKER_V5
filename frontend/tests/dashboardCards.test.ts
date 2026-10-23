/**
 * Dashboard coverage against SEEDED data — every populated card.
 *
 * The Desk dashboard is built from API aggregates, and a stock install only
 * ever renders the empty states. This test boots the app against a seeded
 * throwaway data directory (`tests/helpers/dashboardApp.ts`) and compares the
 * rendered metrics, waiting queue, charts, live jobs and system strip with the
 * same API responses the page consumed.
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
  classroom: { configured: boolean; connected: boolean; last_sync: number | null };
  first_run: boolean;
}

interface AssignmentRow {
  id: string; title: string; checked: number; submitted: number;
  needs_review: number; failed: number; processing: number;
  students_total: number; average: number | null;
}

interface ActivityPayload {
  days: { day: string; results: number }[];
  totals: { results: number; needs_review: number; jobs_started: number };
  window_days: number;
}

interface JobRow {
  id: string; status: string; student_name: string | null;
  display_name?: string | null; stage_detail: string | null;
  progress: number; attempts: number; max_attempts: number;
}

interface UsagePayload {
  hour: { total: number }; day: { total: number };
  limits: { hourly_mb: number; daily_mb: number };
}

interface AttentionPayload {
  items: { title: string; detail?: string }[];
  total: number;
}

interface Snapshot {
  metrics: { label: string | null; value: string | null; scope: string | null; tone: string | null }[];
  waiting: {
    button: string | null;
    rows: { name: string | null; progress: number | null; outstanding: string | null }[];
  } | null;
  attention: { header: string | null; items: { title: string | null; detail: string | null }[] } | null;
  throughput: { svg: boolean; circles: number; rows: [string, string][]; hint: string | null } | null;
  pipeline: {
    legend: string[]; center: string | null; rows: [string, string][];
    system: Record<string, string>;
    gauges: { detail: string | null; aria: string | null }[];
  } | null;
  live: { empty: boolean; rows: { name: string | null; status: string | null; stage: string | null; progress: number | null }[] } | null;
  performance: { bars: string[]; rows: [string, string][] } | null;
  scroll: { extra: number; escapees: { what: string; bottom: number }[]; topbarTop: number | null };
}

const SNAPSHOT = String.raw`(() => {
  const text = (el) => (el ? (el.textContent || "").trim() : null);
  const cards = [...document.querySelectorAll(".main-inner section.card")];
  const cardFor = (needle) => cards.find((c) =>
    ((c.querySelector(".card-title") || {}).textContent || "").includes(needle)) || null;
  const tableRows = (root) => [...root.querySelectorAll("table.visually-hidden tbody tr")]
    .map((tr) => [text(tr.querySelector("th")), text(tr.querySelector("td"))]);

  const metrics = [...document.querySelectorAll(".metric-row .metric")].map((m) => ({
    label: text(m.querySelector(".metric-label")),
    value: text(m.querySelector(".metric-value")),
    scope: text(m.querySelector(".metric-scope")),
    tone: [...m.classList].find((c) => c.startsWith("tone-")) || null,
  }));

  const waitingCard = cardFor("Waiting on you");
  const waiting = waitingCard ? {
    button: text(waitingCard.querySelector("button")),
    rows: [...waitingCard.querySelectorAll("tbody tr")].map((tr) => {
      const bar = tr.querySelector("[role=progressbar]");
      return {
        name: text(tr.querySelector(".cell-strong")),
        progress: bar ? Number(bar.getAttribute("aria-valuenow")) : null,
        outstanding: text(tr.querySelector("td.num")),
      };
    }),
  } : null;

  const attentionCard = cardFor("Needs your attention");
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
    hint: text(throughputCard.querySelector(".chart-title .hint")),
  } : null;

  const systemCard = cardFor("System");
  const donut = systemCard
    ? [...systemCard.querySelectorAll(".chart-card")].find((c) =>
        ((c.querySelector(".chart-title") || {}).textContent || "").startsWith("Job states")) || null
    : null;
  const pipeline = systemCard ? {
    legend: donut ? [...donut.querySelectorAll(".chart-legend .key")].map((k) => text(k)) : [],
    center: (() => {
      const svg = donut && donut.querySelector("svg.chart-svg");
      if (!svg) return null;
      return [...svg.querySelectorAll("text")].map((t) => text(t))
        .find((t) => t !== null && /^[0-9]+$/.test(t)) ?? null;
    })(),
    rows: donut ? tableRows(donut) : [],
    system: Object.fromEntries([...systemCard.querySelectorAll(".kv")].map((row) => [
      text(row.querySelector(".k")), text(row.querySelector(".v")),
    ])),
    gauges: [...systemCard.querySelectorAll(".chart-card")]
      .filter((c) => c.querySelector(".progress[role=img]"))
      .map((c) => ({
        detail: text(c.querySelector(".faint")),
        aria: c.querySelector(".progress[role=img]").getAttribute("aria-label"),
      })),
  } : null;

  const liveCard = cardFor("Running now");
  const live = liveCard ? {
    empty: !!liveCard.querySelector(".empty"),
    rows: [...liveCard.querySelectorAll(":scope > .stack > .stack")].map((row) => {
      const bar = row.querySelector("[role=progressbar]");
      const lines = row.querySelectorAll(".row");
      const foot = lines.length ? lines[lines.length - 1] : null;
      return {
        name: text(lines.length ? lines[0].querySelector("span") : null),
        status: text(row.querySelector(".badge-text")),
        stage: text(foot && foot.children[0]),
        progress: bar ? Number(bar.getAttribute("aria-valuenow")) : null,
      };
    }),
  } : null;

  const perfCard = cardFor("Class averages");
  const performance = perfCard ? {
    bars: [...perfCard.querySelectorAll("svg.chart-svg rect title")].map((t) => text(t)),
    rows: tableRows(perfCard),
  } : null;

  const se = document.scrollingElement;
  const shellHeight = se.clientHeight;
  const escapees = [];
  document.querySelectorAll(".app-shell *").forEach((el) => {
    const cs = getComputedStyle(el);
    if (cs.position !== "absolute" && cs.position !== "fixed") return;
    const op = el.offsetParent;
    if (op && op !== document.body) return;
    const box = el.getBoundingClientRect();
    if (box.bottom > shellHeight + 1 || box.right > se.clientWidth + 1) {
      escapees.push({ what: el.tagName + "." + String(el.className || "").slice(0, 32), bottom: Math.round(box.bottom) });
    }
  });
  const topbar = document.querySelector(".topbar");

  return {
    metrics, waiting, attention, throughput, pipeline, live, performance,
    scroll: {
      extra: se.scrollHeight - shellHeight,
      escapees,
      topbarTop: topbar ? Math.round(topbar.getBoundingClientRect().top) : null,
    },
  };
})()`;

async function getJson<T>(base: string, path: string): Promise<T> {
  const res = await fetch(base + path, { signal: AbortSignal.timeout(15_000) });
  assert.ok(res.ok, `${path} answered HTTP ${res.status}`);
  return (await res.json()) as T;
}

const outstanding = (a: AssignmentRow) =>
  Math.max(0, a.submitted - a.checked) + a.needs_review + a.failed + a.processing;

const rank = (a: AssignmentRow) =>
  (a.processing > 0 ? 0 : 1) * 1000 + (a.failed + a.needs_review) * 10 +
  Math.max(0, a.submitted - a.checked);

const executable = chromiumPath();
const runtime = fixtureRuntime();
const skipReason: string | false = !executable
  ? "no Chrome/Chromium/Edge binary found (set CHROME_PATH to enable)"
  : runtime.reason ?? false;

test("the populated Dashboard renders every card's real data", { skip: skipReason }, async (t) => {
  const app = await startDashboardApp();
  const browser = await launchBrowser(executable as string);
  try {
    const [dash, assignmentsPayload, activity, jobsPayload, usage, attention, review] = await Promise.all([
      getJson<DashboardPayload>(app.url, "/api/dashboard"),
      getJson<{ assignments: AssignmentRow[] }>(app.url, "/api/assignments"),
      getJson<ActivityPayload>(app.url, "/api/analytics/activity?days=30"),
      getJson<{ jobs: JobRow[] }>(app.url, "/api/jobs?limit=20"),
      getJson<UsagePayload>(app.url, "/api/usage"),
      getJson<AttentionPayload>(app.url, "/api/attention"),
      getJson<{ total: number }>(app.url, "/api/review?limit=1"),
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
      "!!document.querySelector('.metric-row') && !document.querySelector('.skeleton') "
      + "&& !document.querySelector('.skeleton-lines') && !!document.querySelector('.chart-svg')",
      30_000, "the dashboard cards to load");
    // The corrected-today number counts up on first paint: wait for the API value.
    await page.waitFor(
      `[...document.querySelectorAll('.metric-value')][0].textContent === "${dash.corrections_today}"`,
      15_000, "the corrected-today counter to settle");
    if (active.length > 0) {
      await page.waitFor(
        "!!document.querySelector('[role=progressbar][aria-label^=\"Correcting\"]')",
        20_000, "the live job rows (SSE handshake + /jobs)");
    }
    const snap = await page.evaluate<Snapshot>(SNAPSHOT);

    await t.test("the metric strip is the day's real aggregates", () => {
      assert.ok(dash.processing > 0 && dash.failed > 0,
        "the fixture must exercise the running/failed tones");
      const checked = assignments.reduce((n, a) => n + a.checked, 0);
      const submitted = assignments.reduce((n, a) => n + a.submitted, 0);
      const weighted = assignments.reduce((n, a) => n + (a.average ?? 0) * a.checked, 0);
      const expected = [
        ["Corrected today", String(dash.corrections_today), "finished correction jobs"],
        ["Submitted, not marked", String(Math.max(0, submitted - checked)), "from Classroom"],
        ["Waiting on a decision", String(review.total), "review queue"],
        ["Failed", String(dash.failed), "needs a retry"],
        ["Class average", checked > 0 ? `${Math.round(weighted / checked)}%` : "—",
          checked > 0 ? `${checked} marked` : "nothing marked yet"],
      ];
      assert.deepEqual(snap.metrics.map((m) => [m.label, m.value, m.scope]), expected,
        "every metric is its API field, with its provenance");
      assert.equal(snap.metrics[0].tone, "tone-ok", "finished work is marked as good");
      assert.equal(snap.metrics[2].tone, "tone-warn", "a non-empty review queue is highlighted");
      assert.equal(snap.metrics[3].tone, "tone-mark", "failures are shown in the mark tone");
    });

    await t.test("waiting on you lists exactly the outstanding work, most urgent first", () => {
      assert.ok(snap.waiting, "the waiting card rendered");
      const rows = assignments.filter((a) => outstanding(a) > 0)
        .sort((a, b) => rank(a) - rank(b)).slice(0, 6);
      assert.ok(rows.length > 0, "the fixture must leave work outstanding");
      assert.equal(snap.waiting!.rows.length, rows.length,
        `queue rows ${snap.waiting!.rows.length} != outstanding assignments ${rows.length}`);
      assert.equal(snap.waiting!.button,
        rows.length > 0 ? `All ${rows.length} assignments` : "All assignments",
        "the card's link counts the same queue");
      rows.forEach((a, i) => {
        const row = snap.waiting!.rows[i];
        assert.equal(row.name, a.title, `row ${i} is the most urgent outstanding assignment`);
        const pct = a.students_total > 0
          ? Math.round(Math.min(1, a.checked / a.students_total) * 100) : 0;
        assert.equal(row.progress, pct, `the progress bar on "${a.title}"`);
        const todo = Math.max(0, a.submitted - a.checked);
        for (const fragment of [
          a.processing > 0 ? `${a.processing} running` : null,
          a.needs_review > 0 ? `${a.needs_review} to decide` : null,
          a.failed > 0 ? `${a.failed} failed` : null,
          todo > 0 ? `${todo} to mark` : null,
        ]) {
          if (fragment) assert.ok(row.outstanding?.includes(fragment),
            `"${a.title}" shows ${fragment} (saw "${row.outstanding}")`);
        }
      });
      // Nothing outstanding must not be listed at all.
      for (const a of assignments.filter((x) => outstanding(x) === 0)) {
        assert.ok(!snap.waiting!.rows.some((r) => r.name === a.title),
          `"${a.title}" has nothing outstanding and must be absent from the queue`);
      }
    });

    await t.test("the attention card lists the real feed", () => {
      assert.ok(snap.attention, "the attention card rendered");
      assert.ok(attention.items.length > 0, "the fixture must raise attention items");
      assert.equal(snap.attention!.items.length, attention.items.length,
        "one row per /api/attention item");
      assert.equal(snap.attention!.header,
        `${attention.items.length} item${attention.items.length === 1 ? "" : "s"} need you`);
      for (const item of snap.attention!.items) {
        assert.ok(item.title && item.title.length > 0, "every attention row has a title");
      }
    });

    await t.test("throughput chart draws the API's window", () => {
      assert.ok(snap.throughput, "the throughput card rendered");
      assert.equal(snap.throughput!.svg, true, "the line chart drew an svg");
      assert.equal(snap.throughput!.hint, `${activity.totals.results} in 30 days`,
        "the chart hint names the API total");
      assert.deepEqual(snap.throughput!.rows.map((r) => Number(r[1])),
        activity.days.map((d) => d.results),
        "the hidden data table carries one row per day, in order");
      assert.equal(snap.throughput!.circles, activity.days.length,
        "one point per day on the line");
      assert.equal(snap.throughput!.rows.reduce((n, r) => n + Number(r[1]), 0),
        activity.totals.results, "the charted total is the API's total");
      assert.ok(activity.days.filter((d) => d.results > 0).length >= 3,
        "the fixture must spread results over several days");
    });

    await t.test("the job-state donut matches /api/dashboard", () => {
      assert.ok(snap.pipeline, "the system card rendered");
      const expected = Object.entries(dash.jobs).filter(([, n]) => n > 0)
        .map(([status, n]) => [status.replace(/_/g, " "), String(n)] as [string, string]);
      assert.deepEqual(snap.pipeline!.rows, expected, "hidden table = /api/dashboard jobs map");
      assert.deepEqual(snap.pipeline!.legend,
        expected.map(([status, n]) => `${status} (${n})`), "one legend key per job state");
      assert.equal(snap.pipeline!.center,
        String(expected.reduce((n, [, v]) => n + Number(v), 0)), "the donut's centre is the total");
    });

    await t.test("live work lists the in-flight jobs", () => {
      assert.ok(snap.live, "the running-now card rendered");
      const expected = active.slice(0, 4);
      assert.equal(snap.live!.empty, false, "in-flight work must not show the empty state");
      assert.equal(snap.live!.rows.length, expected.length, "one row per active job");
      for (const job of expected) {
        const name = job.display_name?.trim() || job.student_name?.trim() || "Student";
        const row = snap.live!.rows.find((r) => r.name === name);
        assert.ok(row, `a live row for ${name}`);
        assert.equal(row!.status, job.status.replace(/_/g, " "), "the status badge");
        assert.equal(row!.stage, job.stage_detail ?? job.status.replace(/_/g, " "),
          "the stage detail line");
        assert.equal(row!.progress, Math.round(job.progress * 100), "the progress bar value");
      }
    });

    await t.test("system health shows the real flags, totals and gauges", () => {
      assert.ok(snap.pipeline, "the system card rendered");
      assert.equal(snap.pipeline!.system["Pipeline"],
        `${dash.pending} queued · ${dash.processing} running`, "the queue/running totals");
      assert.equal(snap.pipeline!.system["AI keys"],
        dash.api_health.has_keys ? `${dash.api_health.healthy}/${dash.api_health.total} healthy` : "not added",
        "the AI key health");
      const classroom = !dash.classroom.configured ? "Not set up"
        : dash.classroom.connected ? "Connected" : "Offline";
      assert.equal(snap.pipeline!.system["Google Classroom"], classroom,
        "the Classroom badge matches the API flags");
      const mb = (bytes: number) => (bytes / 1_048_576).toFixed(1);
      const limit = (value: number, cap: number) => cap > 0 ? `${value} / ${cap} MB` : "no limit";
      assert.deepEqual(snap.pipeline!.gauges.map((g) => g.detail), [
        limit(mb(usage.hour.total), usage.limits.hourly_mb),
        limit(mb(usage.day.total), usage.limits.daily_mb),
      ], "both usage gauges show used vs cap");
      assert.ok(snap.pipeline!.gauges.every((g) => g.aria && g.aria.includes(" of ")),
        "each gauge is announced with its numbers");
      assert.ok(usage.hour.total > 0, "the fixture must meter some traffic");
    });

    await t.test("class-average bars match the assignment averages", () => {
      assert.ok(snap.performance, "the class-averages card rendered");
      const rated = assignments.filter((a) => a.checked > 0).slice(0, 8);
      assert.ok(rated.length > 0, "the fixture must have corrected work");
      assert.deepEqual(snap.performance!.bars.map((b) => b.split(": ").pop()),
        rated.map((a) => `${Math.round(a.average ?? 0)}%`), "one bar per corrected assignment");
      assert.deepEqual(snap.performance!.rows.map((r) => r[1]),
        rated.map((a) => `${Math.round(a.average ?? 0)}%`), "the hidden table carries the averages");
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
  } finally {
    await browser.close();
    await app.stop();
  }
});
