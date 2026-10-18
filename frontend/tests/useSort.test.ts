// Unit tests for the sortable-table math behind `useSort` (Stage B §testing).
// Run from `frontend/` with `node --test "tests/*.test.ts"`.
import { test } from "node:test";
import assert from "node:assert/strict";
import {
  ariaSortFor, compareValues, initialSort, naturalCompare, nextSort, sortRows,
  type SortColumns,
} from "../src/useSort.ts";

interface Row { q: string; marks: number; topic: string | null; lost?: string }

const cols: SortColumns<Row> = {
  q: { value: (r) => r.q },
  marks: { value: (r) => r.marks, numeric: true },
  topic: { value: (r) => r.topic },
  lost: { value: (r) => r.lost },
};

const rows: Row[] = [
  { q: "10", marks: 3, topic: "Algebra", lost: "" },
  { q: "2", marks: 5, topic: "Number", lost: "arithmetic slip" },
  { q: "1a", marks: 0, topic: "Algebra", lost: "method error" },
  { q: "1", marks: null as unknown as number, topic: null, lost: undefined },
];

test("naturalCompare orders numbers numerically, not lexically", () => {
  assert.ok(naturalCompare("2", "10") < 0);
  assert.ok(naturalCompare("10", "2") > 0);
  assert.ok(naturalCompare("1", "1a") < 0);
  assert.ok(naturalCompare("1a", "2") < 0);
  assert.equal(naturalCompare("7", "7"), 0);
  assert.ok(naturalCompare("Q3", "Q20") < 0);
});

test("compareValues puts blanks last in both directions (handled by sortRows sign)", () => {
  assert.ok(compareValues(null, 3) > 0);
  assert.ok(compareValues("", "x") > 0);
  assert.ok(compareValues(3, null) < 0);
  assert.equal(compareValues(null, undefined), 0);
  assert.ok(compareValues(2, 10) < 0);
});

test("nextSort cycles a column asc → desc → off, and switches reset to asc", () => {
  let s = initialSort<keyof Row & string>();
  assert.deepEqual(s, { key: null, dir: "asc" });
  s = nextSort(s, "q");
  assert.deepEqual(s, { key: "q", dir: "asc" });
  s = nextSort(s, "q");
  assert.deepEqual(s, { key: "q", dir: "desc" });
  s = nextSort(s, "q");
  assert.deepEqual(s, { key: null, dir: "asc" });
  s = nextSort(nextSort(s, "marks"), "topic");
  assert.deepEqual(s, { key: "topic", dir: "asc" });
});

test("ariaSortFor reports the header state", () => {
  const s = { key: "q", dir: "desc" as const };
  assert.equal(ariaSortFor(s, "q"), "descending");
  assert.equal(ariaSortFor(s, "marks"), "none");
});

test("sortRows ascending by question number uses natural order", () => {
  const out = sortRows(rows, { key: "q", dir: "asc" }, cols);
  assert.deepEqual(out.map((r) => r.q), ["1", "1a", "2", "10"]);
});

test("sortRows descending by marks, blanks last in both directions", () => {
  const out = sortRows(rows, { key: "marks", dir: "desc" }, cols);
  assert.deepEqual(out.map((r) => r.marks), [5, 3, 0, null]);
  const back = sortRows(rows, { key: "marks", dir: "asc" }, cols);
  // Ascending: lowest first, the blank still last (never "the biggest number").
  assert.deepEqual(back.map((r) => r.marks), [0, 3, 5, null]);
});

test("sortRows is stable for equal values and never mutates the input", () => {
  const input: Row[] = [
    { q: "1", marks: 2, topic: "B" },
    { q: "2", marks: 2, topic: "A" },
    { q: "3", marks: 2, topic: "C" },
  ];
  const snapshot = input.slice();
  const out = sortRows(input, { key: "marks", dir: "asc" }, cols);
  assert.deepEqual(out.map((r) => r.q), ["1", "2", "3"]);
  assert.deepEqual(input, snapshot, "input array must not be mutated");
  assert.notEqual(out, input, "sorting must return a fresh array");
});

test("unsorted state returns the original order in a fresh array", () => {
  const out = sortRows(rows, { key: null, dir: "asc" }, cols);
  assert.deepEqual(out.map((r) => r.q), rows.map((r) => r.q));
  assert.notEqual(out, rows);
});

test("unknown column key leaves the order untouched", () => {
  const out = sortRows(rows, { key: "nope", dir: "asc" }, cols);
  assert.deepEqual(out.map((r) => r.q), rows.map((r) => r.q));
});

test("topic sorting puts blanks last and compares text naturally", () => {
  const out = sortRows(rows, { key: "topic", dir: "asc" }, cols);
  assert.deepEqual(out.map((r) => r.topic), ["Algebra", "Algebra", "Number", null]);
});
