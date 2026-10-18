/**
 * End-to-end pane drag regression (marking workspace §11).
 *
 * Dragging a divider must resize panes LIVE while the clamping keeps the
 * result pane usable: it never drops below its floor, no pane scrolls
 * horizontally mid-drag, and the dragged width survives a reload.
 *
 * Uses real CDP mouse input (`Input.dispatchMouseEvent`); Chrome derives the
 * pointer events the divider's handler relies on, including pointer capture.
 *
 * Requirements (same as workspaceLayout.test.ts): a Chrome/Chromium/Edge
 * binary and the app running with a seeded result. Skips with a reason when
 * either is missing. Run from `frontend/` with:
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
const STORAGE_KEY = "mc5.workspace.results";
const PANE_MIN = 230;
const DIVIDER_WIDTH = 16; // 8px divider + 4px margin on each side

const sleep = (ms: number) => new Promise<void>((resolve) => setTimeout(resolve, ms));

interface PaneBox { i: number; cw: number; sw: number; w: number; left: number }
interface Snapshot {
  panes: PaneBox[];
  workspace: { cw: number; sw: number } | null;
  body: { cw: number; sw: number };
}

const SNAPSHOT = String.raw`(() => {
  const ws = document.querySelector(".workspace");
  const panes = [...document.querySelectorAll(".workspace-pane")];
  return {
    panes: panes.map((p, i) => ({
      i, cw: p.clientWidth, sw: p.scrollWidth,
      w: Math.round(p.getBoundingClientRect().width),
      left: Math.round(p.getBoundingClientRect().left),
    })),
    workspace: ws ? { cw: ws.clientWidth, sw: ws.scrollWidth } : null,
    body: { cw: document.body.clientWidth, sw: document.body.scrollWidth },
  };
})()`;

async function waitForWorkspace(page: PageHandle): Promise<void> {
  await page.waitFor(
    "document.readyState === 'complete' && document.querySelectorAll('.workspace-pane').length === 3",
    25_000,
    "the marking workspace (3 panes)",
  );
  await page.waitFor(
    "!!document.querySelector('.doc-meta') || !!document.querySelector('.workspace-pane .empty')",
    20_000,
    "the document pane",
  );
}

/** Store widths and reload; the nonce prevents accepting the old document. */
async function applyWidths(page: PageHandle, widths: number[]): Promise<void> {
  const stored = JSON.stringify(JSON.stringify(widths));
  const nonce = `d${Date.now()}`;
  try {
    await page.evaluate(
      `window.__mc5DragNonce = ${JSON.stringify(nonce)};`
      + `localStorage.setItem(${JSON.stringify(STORAGE_KEY)}, ${stored});`
      + "location.reload(); true");
  } catch { /* navigation destroys the evaluation context — expected */ }
  await page.waitFor(
    "window.__mc5DragNonce === undefined && document.readyState === 'complete'",
    20_000,
    "reload with the requested pane widths");
  await waitForWorkspace(page);
}

async function snapshot(page: PageHandle): Promise<Snapshot> {
  return page.evaluate<Snapshot>(SNAPSHOT);
}

/** Centre of the divider (and inside the viewport). */
async function dividerPoint(page: PageHandle, index: number): Promise<{ x: number; y: number }> {
  const point = await page.evaluate<{ x: number; y: number } | null>(`(() => {
    const d = document.querySelectorAll(".wp-divider")[${index}];
    if (!d) return null;
    const r = d.getBoundingClientRect();
    return { x: r.left + r.width / 2, y: r.top + Math.min(80, r.height / 2) };
  })()`);
  assert.ok(point, `divider ${index} exists`);
  return point as { x: number; y: number };
}

function assertUsable(snap: Snapshot, context: string): void {
  assert.equal(snap.panes.length, 3, `${context}: three panes are mounted`);
  assert.ok(snap.workspace, `${context}: the workspace is present`);
  for (const pane of snap.panes) {
    assert.ok(pane.sw <= pane.cw + 1,
      `${context}: pane ${pane.i} scrolls horizontally (${pane.sw} > ${pane.cw})`);
  }
  assert.ok((snap.workspace?.sw ?? 0) <= (snap.workspace?.cw ?? 0) + 1,
    `${context}: the workspace scrolls horizontally`);
  assert.ok(snap.body.sw <= snap.body.cw + 1, `${context}: the page scrolls horizontally`);
  // Pane widths are border-box (1px borders are inside the CSS width), so the
  // floor is asserted on the rect width, not clientWidth.
  const last = snap.panes[2];
  assert.ok(last.w >= PANE_MIN - 1,
    `${context}: the result pane keeps its ${PANE_MIN}px floor (got ${last.w}px)`);
}

/** Press the divider, move in `steps`, release; measure after every move. */
async function dragDivider(
  page: PageHandle,
  dividerIndex: number,
  dx: number,
  steps: number,
  onStep: (snap: Snapshot, step: number) => void | Promise<void>,
): Promise<Snapshot> {
  const start = await dividerPoint(page, dividerIndex);
  await page.mouse("mousePressed", start.x, start.y, 1);
  for (let step = 1; step <= steps; step += 1) {
    await page.mouse("mouseMoved", start.x + (dx * step) / steps, start.y, 1);
    await sleep(60);
    await onStep(await snapshot(page), step);
  }
  await page.mouse("mouseReleased", start.x + dx, start.y, 0);
  await sleep(150);
  return snapshot(page);
}

const executable = chromiumPath();
const appUp = await appReachable(APP);
const skipReason = !executable
  ? "no Chrome/Chromium/Edge binary found (set CHROME_PATH to enable)"
  : !appUp
    ? `app not reachable at ${APP} (start it, or set WORKSPACE_LAYOUT_URL)`
    : false;

test("dragging a workspace divider keeps the result pane usable", { skip: skipReason }, async (t) => {
  const browser = await launchBrowser(executable as string);
  try {
    const page = await browser.newPage(WORKSPACE_URL);
    await page.waitFor("document.readyState === 'complete'", 25_000, "initial load");
    await waitForWorkspace(page);

    await t.test("dragging right grows the middle pane and clamps at the budget", async () => {
      await applyWidths(page, [260, 380]);
      const before = await snapshot(page);
      assertUsable(before, "before the right drag");

      const observed: number[] = [];
      const after = await dragDivider(page, 1, 700, 8, async (snap, step) => {
        assertUsable(snap, `right drag, step ${step}`);
        observed.push(snap.panes[1].w);
      });
      assertUsable(after, "after the right drag");

      assert.ok(after.panes[1].w > before.panes[1].w + 40,
        `the drag resized the pane (${before.panes[1].w}px → ${after.panes[1].w}px)`);
      for (let i = 1; i < observed.length; i += 1) {
        assert.ok(observed[i] >= observed[i - 1] - 2,
          `the dragged pane only grows while dragging right (${observed.join(", ")})`);
      }

      const budget = (after.workspace?.cw ?? 0) - 2 * DIVIDER_WIDTH - PANE_MIN;
      const leading = after.panes[0].w + after.panes[1].w;
      // The +700 drag exceeds the budget, so the clamp must have engaged: the
      // leading panes fill the budget but do not take the result pane's floor.
      assert.ok(leading <= budget + 3,
        `leading panes stay within the budget that protects the result pane `
        + `(${leading} vs ${budget})`);
      assert.ok(leading >= budget - 8,
        `the clamp engaged and the leading panes fill the budget (${leading} vs ${budget})`);

      const stored = await page.evaluate<string>(
        `localStorage.getItem(${JSON.stringify(STORAGE_KEY)}) || "[]"`);
      const parsed = JSON.parse(stored) as number[];
      assert.ok(Array.isArray(parsed) && parsed[0] >= PANE_MIN && parsed[1] >= PANE_MIN,
        `persisted widths stay above the floor: ${stored}`);
    });

    await t.test("dragging left clamps the middle pane at its floor", async () => {
      await applyWidths(page, [260, 380]);
      const before = await snapshot(page);
      assertUsable(before, "before the left drag");

      const after = await dragDivider(page, 1, -900, 8, async (snap, step) => {
        assertUsable(snap, `left drag, step ${step}`);
      });
      assertUsable(after, "after the left drag");
      assert.ok(Math.abs(after.panes[1].w - PANE_MIN) <= 1,
        `the middle pane clamps at its floor (${after.panes[1].w}px)`);
      assert.ok(after.panes[2].w > before.panes[2].w + 40,
        `the result pane takes the freed width (${before.panes[2].w}px → ${after.panes[2].w}px)`);
    });

    await t.test("the dragged layout survives a reload", async () => {
      const before = await snapshot(page);
      const stored = JSON.parse(await page.evaluate<string>(
        `localStorage.getItem(${JSON.stringify(STORAGE_KEY)}) || "[]"`)) as number[];
      assert.ok(Array.isArray(stored) && stored.length >= 2, "widths were persisted");
      await applyWidths(page, stored);
      const after = await snapshot(page);
      assertUsable(after, "after reload");
      assert.ok(Math.abs(after.panes[1].w - before.panes[1].w) <= 2,
        `the dragged pane width is restored (${before.panes[1].w}px → ${after.panes[1].w}px)`);
    });
  } finally {
    await browser.close();
  }
});
