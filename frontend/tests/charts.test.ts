// Unit tests for the chart module's pure math (chart inventory §10).
// Run from `frontend/` with `node --test "tests/*.test.ts"`.
import { test } from "node:test";
import assert from "node:assert/strict";
import {
  areaPath, compactNumber, donutSegments, formatDayLabel, formatPercent,
  histogramBins, linePath, niceCeil, niceTicks, scalePoints, stackSegments,
} from "../src/charts/format.ts";

test("niceCeil rounds up to human axis bounds", () => {
  assert.equal(niceCeil(0), 1);
  assert.equal(niceCeil(-5), 1);
  assert.equal(niceCeil(0.7), 1);
  assert.equal(niceCeil(1), 1);
  assert.equal(niceCeil(1.2), 2);
  assert.equal(niceCeil(2.1), 2.5);
  assert.equal(niceCeil(3), 5);
  assert.equal(niceCeil(7), 10);
  assert.equal(niceCeil(42), 50);
  assert.equal(niceCeil(120), 200);
  assert.equal(niceCeil(1200), 2000);
});

test("niceTicks produce even steps ending at the bound", () => {
  assert.deepEqual(niceTicks(10, 2), [0, 5, 10]);
  const ticks = niceTicks(7);
  assert.equal(ticks[0], 0);
  assert.equal(ticks.length, 5);
  assert.ok(ticks.every((t, i) => i === 0 || t > ticks[i - 1]));
  assert.equal(ticks[4], niceCeil(7));
});

test("formatters never invent values", () => {
  assert.equal(formatPercent(null), "—");
  assert.equal(formatPercent(72.4), "72%");
  assert.equal(formatPercent(72.456, 1), "72.5%");
  assert.equal(compactNumber(999), "999");
  assert.equal(compactNumber(1500), "1.5k");
  assert.equal(compactNumber(15000), "15k");
  assert.equal(compactNumber(2_400_000), "2.4M");
  assert.equal(formatDayLabel("2026-03-04"), "Mar 4");
  assert.equal(formatDayLabel("not-a-date"), "not-a-date");
});

test("histogramBins keep the axis continuous and clamp outliers", () => {
  const bins = histogramBins([5, 5, 9, 100, -3], 2, 0, 10);
  assert.equal(bins.length, 2);
  assert.equal(bins[0].from, 0);
  assert.equal(bins[1].to, 10);
  // Bins are half-open [from, to): both 5s and 9 land in bin 1, 100 clamps
  // to bin 1, and -3 clamps to bin 0.
  assert.equal(bins[0].count, 1);
  assert.equal(bins[1].count, 4);
  // Empty bins are preserved.
  const empty = histogramBins([], 3, 0, 6);
  assert.equal(empty.length, 3);
  assert.ok(empty.every((b) => b.count === 0));
  // Degenerate range is safe.
  const degenerate = histogramBins([1], 2, 5, 5);
  assert.equal(degenerate.length, 2);
});

test("donutSegments cover the circle exactly once", () => {
  const segs = donutSegments([1, 1, 2]);
  assert.equal(segs.length, 3);
  assert.equal(segs[0].start, 0);
  const last = segs[segs.length - 1];
  assert.ok(Math.abs(last.end - Math.PI * 2) < 1e-9);
  assert.equal(donutSegments([0, 0]).length, 0);
  // Zero-value slices get no arc but do not shift later ones.
  const skip = donutSegments([0, 4, 0, 4]);
  assert.equal(skip.length, 2);
  assert.ok(Math.abs(skip[1].end - Math.PI * 2) < 1e-9);
});

test("line/area/scale helpers map values into the box", () => {
  const pts = scalePoints([0, 5, 10], { x: 10, y: 0, w: 100, h: 50 }, 10);
  assert.deepEqual(pts[0], [10, 50]);
  assert.deepEqual(pts[2], [110, 0]);
  assert.ok(linePath(pts).startsWith("M10.00,50.00"));
  assert.ok(areaPath(pts, 50).endsWith("L10.00,50.00 Z"));
  // Single value sits in the middle, not at the edge.
  const one = scalePoints([3], { x: 0, y: 0, w: 100, h: 50 }, 6);
  assert.deepEqual(one[0], [50, 25]);
});

test("stackSegments fill the track in order", () => {
  const segs = stackSegments([1, 1, 2], 100);
  assert.equal(segs[0].x, 0);
  assert.ok(Math.abs(segs[0].w - 25) < 1e-9);
  assert.ok(Math.abs(segs[2].x - 50) < 1e-9);
  assert.ok(Math.abs(segs[2].w - 50) < 1e-9);
  const empty = stackSegments([0, 0], 100);
  assert.ok(empty.every((s) => s.w === 0));
});
