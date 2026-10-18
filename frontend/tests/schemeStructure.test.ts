// Regression tests for the mark scheme structure block's collapse policy.
// Run from `frontend/` with `node --test "tests/*.test.ts"`.
//
// Why these exist: folding the structure away is only safe if folding can never
// HIDE a problem. The rules pinned here are
//   * the block opens itself when a marking point is missing or flagged,
//   * a complete scheme starts folded (the verdict is what leads the page),
//   * a flat scheme (no subquestions declared) is not a problem,
//   * folded, the header still names the parts that need looking at,
//   * the summary and the table name the same part the same way.
import { test } from "node:test";
import assert from "node:assert/strict";
import {
  flaggedParts,
  flaggedSummary,
  partLabel,
  structureStartsOpen,
} from "../src/schemeStructure.ts";

const part = (question: string | number, label?: string, validation = "OK") =>
  ({ question, label, validation });

test("a complete structure starts folded", () => {
  assert.equal(structureStartsOpen({ complete: true, flagged: 0, anomalies: 0 }),
    false);
  // The counts are what matter, not the flag alone: a part the checker flagged
  // means review, even while completeness still reads true.
  assert.equal(structureStartsOpen({ complete: true, flagged: 1, anomalies: 0 }),
    true);
});

test("an incomplete structure opens itself", () => {
  assert.equal(structureStartsOpen({ complete: false, flagged: 0, anomalies: 0 }),
    true);
});

test("an anomaly opens the block even when every part is accounted for", () => {
  assert.equal(structureStartsOpen({ complete: true, flagged: 0, anomalies: 2 }),
    true);
});

test("a flat scheme (no subquestions declared) does not force the block open", () => {
  // ``complete`` is null/undefined when the scheme declares no subquestions at
  // all; that is information, not a problem, and must not look like one.
  assert.equal(structureStartsOpen({ complete: null, flagged: 0, anomalies: 0 }),
    false);
  assert.equal(structureStartsOpen({}), false);
  assert.equal(structureStartsOpen({ complete: undefined, flagged: null,
    anomalies: null }), false);
});

test("flaggedParts keeps only the parts that are not OK", () => {
  const subs = [part(1, "(a)"), part(2, "(a)", "MISSING"),
                part(2, "(b)", "UNCERTAIN"), part(3, "", "OK")];
  assert.deepEqual(flaggedParts(subs).map((s) => `${s.question}${s.label}`),
    ["2(a)", "2(b)"]);
  // An absent validation means the same as "OK" — a scheme read before the
  // checker had an opinion must not be reported as uncertain.
  assert.equal(flaggedParts([{ question: 1, label: "(a)" }]).length, 0);
});

test("the folded summary names the flagged parts, not just how many", () => {
  const subs = [part(1, "(a)"), part(2, "(b)", "MISSING"),
                part(4, "(c)", "MISSING")];
  assert.deepEqual(flaggedSummary(subs), { labels: ["Q2 (b)", "Q4 (c)"],
                                           more: 0 });
});

test("the folded summary stays one line and counts what it left out", () => {
  const subs = [1, 2, 3, 4, 5, 6].map((n) => part(n, "(a)", "MISSING"));
  const summary = flaggedSummary(subs, 4);
  assert.equal(summary.labels.length, 4);
  assert.equal(summary.more, 2);
  // Nothing was left out here, so there is no "+n more" to show.
  assert.equal(flaggedSummary(subs, 6).more, 0);
  assert.equal(flaggedSummary([]).labels.length, 0);
});

test("a part is named the way the table names it", () => {
  assert.equal(partLabel(part(4, "(b)")), "Q4 (b)");
  assert.equal(partLabel(part(4, "")), "Q4");
  assert.equal(partLabel(part("4", "   ")), "Q4");
  assert.equal(partLabel({ question: 7, label: null }), "Q7");
});
