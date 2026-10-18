// Unit tests for the workspace pane width math (Stage B §11 + the result-pane
// overflow fix). Run from `frontend/` with `node --test "tests/*.test.ts"`.
import { test } from "node:test";
import assert from "node:assert/strict";
import { DIVIDER_WIDTH, fitPaneWidths } from "../src/paneWidths.ts";

const MIN = 230;

test("wide workspaces keep the teacher's widths untouched", () => {
  // 1280-ish workspace: 260 + 380 + dividers + last pane still fits.
  const out = fitPaneWidths([260, 380], 3, 1278, MIN);
  assert.deepEqual(out, [260, 380]);
});

test("a narrow window shrinks the widest leading pane, not the result pane", () => {
  // The reproduced case: 777px workspace, stored [230, 380].
  // Budget for leading panes = 777 - 2*16 - 230 = 515.
  const out = fitPaneWidths([230, 380], 3, 777, MIN);
  assert.equal(out[0], 230, "pane at its floor is not shrunk further");
  assert.equal(out[1], 285, "widest pane absorbs the shortfall");
  const last = 777 - 2 * DIVIDER_WIDTH - out[0] - out[1];
  assert.equal(last, 230, "last pane keeps exactly its minimum");
});

test("no leading pane is ever pushed below its minimum", () => {
  const out = fitPaneWidths([900, 900], 3, 700, MIN);
  assert.ok(out.every((w) => w >= MIN), `widths ${out} must respect the floor`);
  // Even at the floors the layout no longer fits; the stacked rule handles it.
  const last = 700 - 2 * DIVIDER_WIDTH - out[0] - out[1];
  assert.ok(last < MIN);
});

test("missing stored widths fall back to the minimum before fitting", () => {
  const out = fitPaneWidths([320], 3, 900, MIN);
  assert.equal(out[0], 320);
  assert.equal(out[1], MIN);
});

test("a zero/unknown container width leaves the widths alone", () => {
  assert.deepEqual(fitPaneWidths([260, 380], 3, 0, MIN), [260, 380]);
});

test("single-pane workspaces are untouched", () => {
  assert.deepEqual(fitPaneWidths([260], 1, 400, MIN), [260]);
});

test("fitting is idempotent", () => {
  const once = fitPaneWidths([500, 520], 3, 1278, MIN);
  const twice = fitPaneWidths(once, 3, 1278, MIN);
  assert.deepEqual(twice, once);
});

test("the last pane's remainder is computed with the divider total", () => {
  const count = 4;
  const widths = [300, 400, 500];
  const container = 1600;
  const out = fitPaneWidths(widths, count, container, MIN);
  const last = container - (count - 1) * DIVIDER_WIDTH - out.reduce((a, b) => a + b, 0);
  assert.ok(last >= MIN);
});
