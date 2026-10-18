/**
 * Browser-driven layout regression: the marking workspace must NEVER scroll
 * horizontally inside a pane.
 *
 * Covers the three ways the result pane overflowed before the container-query
 * fix: narrow panes, long submission filenames, and zoomed submission pages.
 *
 * Requirements:
 *   * a Chrome/Chromium/Edge binary (auto-discovered, or set CHROME_PATH);
 *   * the app running with a seeded result (default http://127.0.0.1:8720,
 *     result id "r-ui-test"; override with WORKSPACE_LAYOUT_URL and
 *     WORKSPACE_RESULT_ID).
 *
 * The check skips with an explicit reason when either is missing — a skip is
 * reported as "not run", never as a pass. Run from `frontend/` with:
 *   node --test "tests/*.test.ts"
 */
import { test } from "node:test";
import assert from "node:assert/strict";
import {
  appReachable, chromiumPath, launchBrowser,
  type PageHandle,
} from "./helpers/browser.ts";

const APP = (process.env.WORKSPACE_LAYOUT_URL ?? "http://127.0.0.1:8720").replace(/\/$/, "");
const RESULT_ID = process.env.WORKSPACE_RESULT_ID ?? "r-ui-test";
const WORKSPACE_URL = `${APP}/#/results/${RESULT_ID}`;
const LONG_FILENAME =
  "Priya Ramanathan - 4MA1 1H June 2023 - submission (final copy) scanned.pdf · 12 page(s) · retained copy";

const PANE_WIDTHS: number[][] = [
  [260, 380], // default
  [500, 520], // squeezed result pane
  [230, 380], // the widths that reproduced the report
];

interface Box { cw: number; sw: number }
interface Overflow { what: string; dx: number }
interface Measure {
  panes: Box[];
  overflows: Overflow[];
  spills: { pane: number; cls: string; dx: number }[];
  pages: { cw: number; sw: number; ox: string } | null;
  zoom: string | null;
}

const MEASURE = String.raw`(() => {
  const panes = [...document.querySelectorAll(".workspace-pane")];
  const overflows = [];
  const panesOut = panes.map((p, i) => {
    const dx = p.scrollWidth - p.clientWidth;
    if (dx > 1) overflows.push({ what: "pane " + i, dx });
    return { cw: p.clientWidth, sw: p.scrollWidth };
  });
  if (document.body.scrollWidth - document.body.clientWidth > 1)
    overflows.push({ what: "body", dx: document.body.scrollWidth - document.body.clientWidth });
  if (document.documentElement.scrollWidth - document.documentElement.clientWidth > 1)
    overflows.push({ what: "html", dx: document.documentElement.scrollWidth - document.documentElement.clientWidth });

  const doc = panes.find((p) => p.innerText.includes("Fit width")) || null;
  let pages = null, zoom = null;
  if (doc) {
    for (const sel of [".doc-head", ".doc-meta"]) {
      const el = doc.querySelector(sel);
      if (el && el.scrollWidth - el.clientWidth > 1)
        overflows.push({ what: sel, dx: el.scrollWidth - el.clientWidth });
    }
    const dp = doc.querySelector(".doc-pages");
    if (dp) pages = { cw: dp.clientWidth, sw: dp.scrollWidth, ox: getComputedStyle(dp).overflowX };
    const z = doc.querySelector("[aria-live='polite']");
    if (z) zoom = (z.textContent || "").trim();
  }

  // Strict check: nothing with VISIBLE horizontal overflow may spill inside a
  // pane. Elements inside a deliberate scroller (.doc-pages, .table-wrap) are
  // excluded — their overflow is the scroller's job (that is what the
  // zoom-level assertions exercise separately).
  const scrollsX = (el) => {
    const ox = getComputedStyle(el).overflowX;
    return ox === "auto" || ox === "scroll";
  };
  const insideScroller = (el, pane) => {
    for (let n = el.parentElement; n && n !== pane; n = n.parentElement) {
      if (scrollsX(n)) return true;
    }
    return false;
  };
  const spills = [];
  panes.forEach((pane, i) => {
    pane.querySelectorAll("*").forEach((el) => {
      if (scrollsX(el) || insideScroller(el, pane)) return;
      const dx = el.scrollWidth - el.clientWidth;
      if (dx > 1 && el.clientWidth > 0)
        spills.push({ pane: i, cls: String(el.className || "").slice(0, 48), dx });
    });
  });
  return { panes: panesOut, overflows, spills, pages, zoom };
})()`;

const STACKED_MEASURE = String.raw`(() => {
  const overflows = [];
  const pane = document.querySelector(".workspace-stacked .workspace-pane");
  if (!pane) return { missing: true, overflows: [{ what: "pane", dx: 1 }], pane: null };
  if (pane.scrollWidth - pane.clientWidth > 1)
    overflows.push({ what: "pane", dx: pane.scrollWidth - pane.clientWidth });
  if (document.body.scrollWidth - document.body.clientWidth > 1)
    overflows.push({ what: "body", dx: document.body.scrollWidth - document.body.clientWidth });
  return { missing: false, overflows, pane: { cw: pane.clientWidth, sw: pane.scrollWidth } };
})()`;

/** Wait for the lazy-loaded workspace, all three panes and the document pane. */
async function waitForWorkspace(page: PageHandle): Promise<void> {
  await page.waitFor(
    "document.readyState === 'complete' && document.querySelectorAll('.workspace-pane').length === 3",
    25_000,
    "the marking workspace (3 panes)",
  );
  // The centre pane fetches the stored pages after mount; its metadata line
  // (or the honest "not stored" empty state) appears once that resolves.
  await page.waitFor(
    "!!document.querySelector('.doc-meta') || !!document.querySelector('.workspace-pane .empty')",
    20_000,
    "the document pane (result with retained pages)",
  );
}

/**
 * Wait until the three panes have stopped changing width.
 *
 * Pane widths come from two sources: the stored values, and the ResizeObserver
 * that re-fits them to the real container. Between mount and that first fit the
 * result pane can still be holding the raw stored layout, so measuring the
 * instant the workspace appears catches an intermediate frame and reports a
 * false overflow. This waits for two consecutive identical frames instead.
 */
async function waitForStableLayout(page: PageHandle, label: string): Promise<void> {
  const settled = await page.evaluate<boolean>(`(async () => {
    const snapshot = () => [...document.querySelectorAll(".workspace-pane")]
      .map((p) => Math.round(p.getBoundingClientRect().width)).join(",");
    if (document.fonts && document.fonts.ready) await document.fonts.ready;
    let previous = snapshot();
    for (let i = 0; i < 30; i += 1) {
      await new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve)));
      const current = snapshot();
      if (current === previous) return true;
      previous = current;
    }
    return false;
  })()`);
  assert.ok(settled, `pane widths settle after ${label}`);
}

/**
 * Store pane widths and reload. A nonce is set first so the wait cannot
 * mistake the OLD document (still mounted) for the reloaded one.
 */
async function applyWidths(page: PageHandle, widths: number[]): Promise<void> {
  const stored = JSON.stringify(JSON.stringify(widths));
  const nonce = `n${Date.now()}`;
  try {
    await page.evaluate(
      `window.__mc5LayoutNonce = ${JSON.stringify(nonce)};`
      + `localStorage.setItem("mc5.workspace.results", ${stored});`
      + "location.reload(); true");
  } catch { /* navigation destroys the evaluation context — expected */ }
  await page.waitFor(
    `window.__mc5LayoutNonce === undefined && document.readyState === 'complete'`,
    20_000,
    `reload with pane widths ${widths.join("/")}`);
  await waitForWorkspace(page);
  await waitForStableLayout(page, `widths ${widths.join("/")}`);
}

async function setLongFilename(page: PageHandle): Promise<void> {
  const ok = await page.evaluate<boolean>(`(() => {
    const meta = document.querySelector(".doc-meta");
    if (!meta) return false;
    meta.textContent = ${JSON.stringify(LONG_FILENAME)};
    return true;
  })()`);
  assert.ok(ok, "the document pane metadata line exists (long-filename case)");
}

async function measure(page: PageHandle): Promise<Measure> {
  return page.evaluate<Measure>(MEASURE);
}

function assertNoOverflow(m: Measure, context: string): void {
  assert.deepEqual(m.overflows, [],
    `${context}: horizontal overflow — ${JSON.stringify(m.overflows)}`);
  assert.deepEqual(m.spills, [],
    `${context}: elements visibly spilling — ${JSON.stringify(m.spills)}`);
  for (const [i, pane] of m.panes.entries()) {
    assert.ok(pane.sw <= pane.cw + 1,
      `${context}: pane ${i} scrolls horizontally (${pane.sw} > ${pane.cw})`);
  }
}

async function click(page: PageHandle, finder: string, label: string): Promise<void> {
  const ok = await page.evaluate<boolean>(`(() => {
    const b = ${finder};
    if (!b) return false;
    b.click();
    return true;
  })()`);
  assert.ok(ok, `${label} exists`);
}

async function clickZoom(page: PageHandle, dir: "in" | "out"): Promise<void> {
  await click(page,
    `[...document.querySelectorAll("button")].find((x) => x.getAttribute("aria-label") === `
      + `${JSON.stringify(dir === "in" ? "Zoom in" : "Zoom out")})`,
    `the ${dir === "in" ? "zoom-in" : "zoom-out"} button`);
}

async function clickFitWidth(page: PageHandle): Promise<void> {
  await click(page,
    `[...document.querySelectorAll("button")].find((x) => x.innerText.trim() === "Fit width")`,
    "the Fit width button");
}

const executable = chromiumPath();
const appUp = await appReachable(APP);
const skipReason = !executable
  ? "no Chrome/Chromium/Edge binary found (set CHROME_PATH to enable)"
  : !appUp
    ? `app not reachable at ${APP} (start it, or set WORKSPACE_LAYOUT_URL)`
    : false;

test("marking workspace has zero horizontal overflow", { skip: skipReason }, async (t) => {
  const browser = await launchBrowser(executable as string);
  try {
    const page = await browser.newPage(WORKSPACE_URL);
    await page.waitFor("document.readyState === 'complete'", 25_000, "initial load");
    await waitForWorkspace(page);

    await t.test("the detector itself catches a deliberately overflowing element", async () => {
      const added = await page.evaluate<boolean>(`(() => {
        const pane = document.querySelectorAll(".workspace-pane")[2];
        if (!pane) return false;
        const probe = document.createElement("div");
        probe.id = "mc5-layout-probe";
        probe.style.cssText = "width:4000px;height:2px";
        pane.appendChild(probe);
        return true;
      })()`);
      assert.ok(added, "the result pane exists for the probe");
      const bad = await measure(page);
      assert.ok(bad.overflows.length > 0 || bad.spills.length > 0,
        "a 4000px probe inside a pane must be reported as overflow");
      await page.evaluate(
        "(() => { document.getElementById('mc5-layout-probe')?.remove(); return true; })()");
      const clean = await measure(page);
      assert.deepEqual(clean.overflows, [],
        `removing the probe restores a clean layout — ${JSON.stringify(clean.overflows)}`);
    });

    await t.test("pane widths and long filenames", async () => {
      for (const widths of PANE_WIDTHS) {
        await applyWidths(page, widths);

        const shortName = await measure(page);
        assert.equal(shortName.panes.length, 3, `widths ${widths}: three panes are visible`);
        assertNoOverflow(shortName, `widths ${widths.join("/")}, short filename`);
        for (const [i, pane] of shortName.panes.entries()) {
          assert.ok(pane.cw >= 200, `widths ${widths}: pane ${i} stays usable (${pane.cw}px)`);
        }

        await setLongFilename(page);
        const longName = await measure(page);
        assertNoOverflow(longName, `widths ${widths.join("/")}, long filename`);
      }
    });

    await t.test("zoom levels keep pages scrolling inside their pane", async () => {
      await applyWidths(page, [260, 380]);
      await waitForWorkspace(page);

      const base = await measure(page);
      assertNoOverflow(base, "zoom 100%");
      assert.equal(base.zoom, "100%");
      assert.ok(base.pages, "the document pane reports its page scroller");
      assert.equal(base.pages?.ox, "auto", ".doc-pages owns the horizontal scroll");
      assert.ok((base.pages?.sw ?? 0) <= (base.pages?.cw ?? 0) + 1, "pages fit at 100%");

      for (let i = 0; i < 3; i += 1) await clickZoom(page, "in");
      await new Promise((r) => setTimeout(r, 150));
      const zoomed = await measure(page);
      assert.equal(zoomed.zoom, "175%");
      assertNoOverflow(zoomed, "zoom 175%");
      assert.ok((zoomed.pages?.sw ?? 0) > (zoomed.pages?.cw ?? 0) + 1,
        "zoom 175%: pages scroll inside .doc-pages");

      // Long filename at a zoomed level must not change the header's fit.
      await setLongFilename(page);
      const longAt175 = await measure(page);
      assertNoOverflow(longAt175, "zoom 175%, long filename");

      for (let i = 0; i < 5; i += 1) await clickZoom(page, "in");
      await new Promise((r) => setTimeout(r, 150));
      const max = await measure(page);
      assert.equal(max.zoom, "300%");
      assertNoOverflow(max, "zoom 300%");

      for (let i = 0; i < 10; i += 1) await clickZoom(page, "out");
      await new Promise((r) => setTimeout(r, 150));
      const min = await measure(page);
      assert.equal(min.zoom, "50%");
      assertNoOverflow(min, "zoom 50%");

      // Fit width restores 100% AND clears any sideways scroll.
      await page.evaluate("(() => { const p = document.querySelector('.doc-pages'); if (p) p.scrollLeft = 120; return true; })()");
      for (let i = 0; i < 4; i += 1) await clickZoom(page, "in");
      await clickFitWidth(page);
      await new Promise((r) => setTimeout(r, 200));
      const fitted = await measure(page);
      assert.equal(fitted.zoom, "100%");
      assertNoOverflow(fitted, "after Fit width");
      const scrollLeft = await page.evaluate<number>(
        "document.querySelector('.doc-pages')?.scrollLeft ?? -1");
      assert.equal(scrollLeft, 0, "Fit width resets the horizontal scroll");
    });

    await t.test("narrow viewport collapses to tabs without overflow", async () => {
      await page.setViewport(500, 900);
      await page.waitFor("!!document.querySelector('.workspace-stacked')", 10_000,
        "the stacked (tabbed) workspace");
      const stacked = await page.evaluate<{
        missing: boolean; overflows: Overflow[]; pane: Box | null;
      }>(STACKED_MEASURE);
      assert.equal(stacked.missing, false, "a single stacked pane is mounted");
      assert.deepEqual(stacked.overflows, [],
        `stacked layout: horizontal overflow — ${JSON.stringify(stacked.overflows)}`);
      assert.ok((stacked.pane?.cw ?? 0) > 300, "the stacked pane uses the available width");
      await page.clearViewport();
    });
  } finally {
    await browser.close();
  }
});
