// Unit tests for the topic-group math + question triage the marking workspace
// shows (Stage B §testing: "Node tests cover useSort / topic-group math").
// Run from `frontend/` with `node --test "tests/*.test.ts"`.
import { test } from "node:test";
import assert from "node:assert/strict";
import {
  MASTERY_LABEL, TRIAGE_LABEL, TRIAGE_RANK, UNKNOWN_TOPIC,
  buildTopicGroups, masteryBand, questionTriage,
  type GroupableQuestion,
} from "../src/topicGroups.ts";

function q(partial: Partial<GroupableQuestion> & { question_number: string }): GroupableQuestion {
  return {
    awarded_marks: 0,
    max_marks: 4,
    topic: "Algebra",
    student_answer: "x = 3",
    working_summary: "solved by inspection",
    ...partial,
  };
}

/* ------------------------------------------------------------- triage */

test("an unmarked (REVIEW) question needs checking first", () => {
  assert.equal(questionTriage(q({ question_number: "1", status: "REVIEW" })), "check");
});

test("unmarked sub-parts need checking whether declared or inline", () => {
  assert.equal(
    questionTriage(q({ question_number: "2", unmarked_subquestions: ["2c"] })), "check");
  assert.equal(
    questionTriage(q({ question_number: "3",
      subquestions: [{ status: "MARKED" }, { status: "UNMARKED" }] })), "check");
  assert.equal(
    questionTriage(q({ question_number: "4",
      subquestions: [{ status: "MARKED" }, { status: "MARKED" }] })), "ok");
});

test("a justified scheme disagreement that differs from the award needs checking", () => {
  assert.equal(questionTriage(q({ question_number: "5", awarded_marks: 1,
    scheme_check: { disagrees_with_scheme: true, student_answer_valid: true,
      justified_marks: 2 } })), "check");
});

test("an informational scheme check is worth a look, not a panic", () => {
  assert.equal(questionTriage(q({ question_number: "6",
    scheme_check: { disagrees_with_scheme: true, student_answer_valid: false,
      justified_marks: null } })), "watch");
  assert.equal(questionTriage(q({ question_number: "7",
    scheme_check: { disagrees_with_scheme: false, student_answer_valid: true,
      justified_marks: 3 } })), "watch");
});

test("nothing readable extracted is worth a look; ordinary questions have no flags", () => {
  assert.equal(questionTriage(q({ question_number: "8",
    student_answer: "", working_summary: "  " })), "watch");
  assert.equal(questionTriage(q({ question_number: "9" })), "ok");
  assert.equal(questionTriage(q({ question_number: "10",
    student_answer: "", working_summary: "attempted trial and improvement" })), "ok");
});

test("triage labels and ranks are consistent", () => {
  for (const t of ["check", "watch", "ok"] as const) {
    assert.ok(TRIAGE_LABEL[t].length > 0);
  }
  assert.ok(TRIAGE_RANK.check < TRIAGE_RANK.watch);
  assert.ok(TRIAGE_RANK.watch < TRIAGE_RANK.ok);
});

/* --------------------------------------------------------- topic groups */

test("groups sum earned/available and mirror the reference percentage math", () => {
  const groups = buildTopicGroups([
    q({ question_number: "1", awarded_marks: 3, max_marks: 4, topic: "Algebra" }),
    q({ question_number: "2", awarded_marks: 4, max_marks: 4, topic: "Algebra" }),
    q({ question_number: "3", awarded_marks: 0, max_marks: 6, topic: "Geometry" }),
    q({ question_number: "4", awarded_marks: 2, max_marks: 4, topic: "Geometry" }),
  ]);
  assert.deepEqual(groups.map((g) => g.topic), ["Algebra", "Geometry"]);
  const [alg, geo] = groups;
  assert.equal(alg.earned, 7);
  assert.equal(alg.available, 8);
  assert.equal(alg.percentage, (7 / 8) * 100);
  assert.equal(geo.earned, 2);
  assert.equal(geo.available, 10);
  assert.equal(geo.percentage, 20);
});

test("a group with nothing available reports 0.0 percent, never NaN", () => {
  const [g] = buildTopicGroups([q({ question_number: "1", awarded_marks: 0, max_marks: 0 })]);
  assert.equal(g.percentage, 0.0);
  assert.equal(Number.isNaN(g.percentage), false);
});

test("groups keep first-appearance (paper) order and blank topics fall back", () => {
  const groups = buildTopicGroups([
    q({ question_number: "1", topic: "Ratio" }),
    q({ question_number: "2", topic: "  " }),
    q({ question_number: "3", topic: null }),
    q({ question_number: "4", topic: "Ratio" }),
  ]);
  assert.deepEqual(groups.map((g) => g.topic), ["Ratio", UNKNOWN_TOPIC]);
  assert.equal(groups[1].questions.length, 2);
  assert.equal(groups[0].questions.length, 2);
});

test("group rollup counts questions needing eyes", () => {
  const [g] = buildTopicGroups([
    q({ question_number: "1", status: "REVIEW" }),
    q({ question_number: "2", scheme_check: { disagrees_with_scheme: true,
      student_answer_valid: true, justified_marks: null } }),
    q({ question_number: "3" }),
  ]);
  assert.equal(g.checkCount, 1);
  assert.equal(g.watchCount, 1);
  assert.equal(g.questions.length, 3);
});

test("mastery bands are a derived reading with stable thresholds", () => {
  assert.equal(masteryBand(100), "secure");
  assert.equal(masteryBand(75), "secure");
  assert.equal(masteryBand(74.999), "developing");
  assert.equal(masteryBand(50), "developing");
  assert.equal(masteryBand(49.999), "needs-work");
  assert.equal(masteryBand(0), "needs-work");
  for (const band of ["secure", "developing", "needs-work"] as const) {
    assert.ok(MASTERY_LABEL[band].length > 0);
  }
});
