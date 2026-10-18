// Regression tests for the source-page viewer state.
// Run from `frontend/` with `node --test "tests/*.test.ts"`.
//
// The bugs these pin are the ones a page viewer actually had:
//   * the image reset to page 1 whenever a question was selected;
//   * the header kept claiming "Q7" while a different page was displayed;
//   * clicking a page re-rendered an identical image;
//   * a document whose page list was truncated could not reach its last page.
import { test } from "node:test";
import assert from "node:assert/strict";
import {
  buildViewerModel,
  conflictSummary,
  conflictsOnPage,
  itemsOnPage,
  labelKey,
  pageForQuestion,
  pageImageUrl,
  pageLabel,
  selectPage,
  selectQuestion,
  selectionChanged,
  stepPage,
} from "../src/schemeViewer.ts";
import type { SchemePagesPayload } from "../src/schemeViewer.ts";

function payload(over: Partial<SchemePagesPayload> = {}): SchemePagesPayload {
  return {
    page_count: 3,
    pages: [
      { page: 1, has_items: true, item_count: 1, indices: [1], needs_review: false },
      { page: 2, has_items: true, item_count: 1, indices: [2], needs_review: false },
      { page: 3, has_items: false, item_count: 0, indices: [], needs_review: true },
    ],
    items: [
      { page: 1, source_index: 1, label: "June 05/4H/Q1", max_marks: 4 },
      { page: 2, source_index: 2, label: "June 05/4H/Q2", max_marks: 5 },
    ],
    page_url_template: "/api/assignments/cw/mark-scheme/page/{page}.png",
    dpi: 150,
    ...over,
  };
}

test("the model reports every page of the document", () => {
  const model = buildViewerModel(payload());
  assert.equal(model.pageCount, 3);
  assert.deepEqual(model.pages.map((p) => p.page), [1, 2, 3]);
  assert.deepEqual(model.emptyPages, [3]);
});

test("a truncated page list still lets the reviewer open the last page", () => {
  const model = buildViewerModel(payload({
    page_count: 5,
    pages: [{ page: 1, has_items: true, item_count: 1, indices: [1], needs_review: false }],
  }));
  assert.deepEqual(model.pages.map((p) => p.page), [1, 2, 3, 4, 5]);
  assert.equal(model.pages[4].has_items, false);
});

test("questions map to the page they were read from", () => {
  const model = buildViewerModel(payload());
  assert.equal(pageForQuestion(model, { source_index: 2 }), 2);
  assert.equal(pageForQuestion(model, { question_number: "June 05/4H/Q1" }), 1);
});

test("a sub-part resolves to its parent question's page", () => {
  const model = buildViewerModel(payload());
  assert.equal(pageForQuestion(model, { question_number: "1(a)" }), 1);
  assert.equal(pageForQuestion(model, { question_number: "2(b)(ii)" }), 2);
});

test("per-page indices are used when the flat item list is missing", () => {
  const model = buildViewerModel(payload({ items: [] }));
  assert.equal(pageForQuestion(model, { source_index: 2 }), 2);
  assert.deepEqual(itemsOnPage(model, 1), [1]);
});

test("an unrelated question has no page rather than page 1", () => {
  const model = buildViewerModel(payload());
  assert.equal(pageForQuestion(model, { question_number: "77" }), null);
});

test("selecting a question moves the page to its source", () => {
  const model = buildViewerModel(payload());
  const next = selectQuestion(model, { page: 1, question: null },
    { source_index: 2, question_number: "2" });
  assert.deepEqual(next, { page: 2, question: "2" });
});

test("selecting a question with no page evidence keeps the current page", () => {
  const model = buildViewerModel(payload());
  const next = selectQuestion(model, { page: 3, question: null },
    { question_number: "unindexed" });
  assert.deepEqual(next, { page: 3, question: "unindexed" },
    "the viewer must not jump back to page 1");
});

test("selecting the same question twice is a stable no-op", () => {
  const model = buildViewerModel(payload());
  const once = selectQuestion(model, { page: 1, question: null },
    { source_index: 2, question_number: "2" });
  const twice = selectQuestion(model, once, { source_index: 2, question_number: "2" });
  assert.deepEqual(twice, once);
  assert.equal(selectionChanged(once, twice), false,
    "an identical selection must not re-fetch the image");
});

test("changing page clears a question that is not on that page", () => {
  const model = buildViewerModel(payload());
  const at = { page: 2, question: "2" };
  assert.deepEqual(selectPage(model, at, 1), { page: 1, question: null });
});

test("changing page keeps a question that IS on that page", () => {
  const model = buildViewerModel(payload());
  const at = { page: 1, question: "1" };
  assert.deepEqual(selectPage(model, at, 1), { page: 1, question: "1" });
});

test("stepping stays inside the document", () => {
  const model = buildViewerModel(payload());
  assert.equal(stepPage(model, { page: 1, question: null }, -1).page, 1);
  assert.equal(stepPage(model, { page: 3, question: null }, 1).page, 3);
  assert.equal(stepPage(model, { page: 1, question: null }, 1).page, 2);
});

test("an empty document cannot produce page 0", () => {
  const model = buildViewerModel({ page_count: 0 });
  assert.equal(model.pageCount, 0);
  assert.equal(selectPage(model, { page: 1, question: null }, 5).page, 1);
});

test("the image URL comes from the template the API reported", () => {
  assert.equal(pageImageUrl("/x/page/{page}.png", 3),
    "/x/page/3.png");
  assert.equal(pageImageUrl("/x/page/{page}.png", 3, 200),
    "/x/page/3.png?dpi=200");
  assert.equal(pageImageUrl("/x/page/{page}.png?v=1", 2, 150),
    "/x/page/2.png?v=1&dpi=150");
});

test("a missing template still yields a usable relative URL", () => {
  assert.equal(pageImageUrl(undefined, 4),
    "/api/assignments/{courseworkId}/mark-scheme/page/4.png");
});

test("page labels say what the page contributed", () => {
  const model = buildViewerModel(payload());
  assert.equal(pageLabel(model.pages[1]), "Page 2 — 1 question");
  assert.equal(pageLabel(model.pages[2]), "Page 3 — nothing read, open it");
  assert.equal(pageLabel({ page: 4, has_items: false, item_count: 0, indices: [], needs_review: false }),
    "Page 4 — no questions");
});

test("a conflict is shown on the page it concerns", () => {
  const model = buildViewerModel(payload());
  const conflicts = [{
    source_index: 2,
    chunks: ["p1-2", "p2-3"],
    variants: [
      { chunk: "p1-2", max_marks: 4, parts_detail: [["(a)", 2]] as [string, number][], page: 2 },
      { chunk: "p2-3", max_marks: 5, parts_detail: [["(a)", 3]] as [string, number][], page: 2 },
    ],
  }];
  assert.equal(conflictsOnPage(model, conflicts, 2).length, 1);
  assert.equal(conflictsOnPage(model, conflicts, 1).length, 0);
});

test("a conflict is found from the index a page contributed", () => {
  const model = buildViewerModel(payload());
  const conflicts = [{ source_index: 1, variants: [{ chunk: "p1-6" }] }];
  assert.equal(conflictsOnPage(model, conflicts, 1).length, 1);
});

test("a conflict summary names both readings", () => {
  const text = conflictSummary({
    source_index: 61,
    variants: [
      { chunk: "p21-26", max_marks: 5, parts_detail: [["(b)(ii)", 2]] as [string, number][] },
      { chunk: "p26-29", max_marks: 5, parts_detail: [["(ii)", 1]] as [string, number][] },
    ],
  });
  assert.match(text, /index 61/);
  assert.match(text, /p21-26/);
  assert.match(text, /p26-29/);
  assert.match(text, /vs/);
});

test("an empty conflict list renders nothing", () => {
  const model = buildViewerModel(payload());
  assert.deepEqual(conflictsOnPage(model, [], 1), []);
  assert.deepEqual(conflictsOnPage(model, undefined, 1), []);
});

test("labels are compared case- and space-insensitively", () => {
  assert.equal(labelKey("  June 05/4H/Q1 "), "june 05/4h/q1");
});
