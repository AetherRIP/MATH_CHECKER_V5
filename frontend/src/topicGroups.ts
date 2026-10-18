/**
 * Topic-group math + question triage for the marking workspace.
 *
 * Pure and dependency-free so `frontend/tests/topicGroups.test.ts` covers the
 * exact arithmetic the workspace shows (§ testing: "Node tests cover useSort /
 * topic-group math").
 *
 * Honesty rules (§ microcopy):
 *  * triage is DERIVED from recorded verification flags — unmarked parts,
 *    mark-scheme disagreements, unreadable work. It is never presented as a
 *    model confidence score. (Per-question confidence would need engine-side
 *    data; the engine records confidence at result level only.)
 *  * a 0 with no marking evidence is "unmarked", not "wrong";
 *  * mastery bands are a reading of the awarded marks, clearly labelled as
 *    derived — nothing here changes a stored mark.
 */

/** The per-question fields the grouping/triage needs (structurally typed so
 *  the workspace's `Question` interface satisfies it without adaptation). */
export interface GroupableQuestion {
  question_number: string;
  awarded_marks: number;
  max_marks: number;
  topic?: string | null;
  status?: string | null;
  loss_reason?: string | null;
  student_answer?: string | null;
  working_summary?: string | null;
  scheme_check?: {
    disagrees_with_scheme?: boolean;
    student_answer_valid?: boolean;
    justified_marks?: number | null;
  } | null;
  subquestions?: { status?: string | null; max_marks?: number; awarded_marks?: number }[] | null;
  unmarked_subquestions?: string[] | null;
}

export type Triage = "check" | "watch" | "ok";

export const TRIAGE_LABEL: Record<Triage, string> = {
  check: "Check first",
  watch: "Worth a look",
  ok: "No flags",
};

export const TRIAGE_RANK: Record<Triage, number> = { check: 0, watch: 1, ok: 2 };

/**
 * Triage for ONE question, from fields the engine actually records:
 *
 *   check  — the question could not be fully marked (REVIEW / UNMARKED parts),
 *            or the engine mathematically disagrees with the mark scheme;
 *   watch  — a scheme check exists but is informational, or nothing readable
 *            was extracted to judge the work against;
 *   ok     — nothing recorded against this question.
 */
export function questionTriage(q: GroupableQuestion): Triage {
  const sub = q.subquestions ?? [];
  const unmarkedPart = (q.unmarked_subquestions?.length ?? 0) > 0
    || sub.some((s) => (s.status ?? "MARKED").toUpperCase() !== "MARKED");
  if ((q.status ?? "").toUpperCase() === "REVIEW" || unmarkedPart) return "check";
  const flag = q.scheme_check;
  if (flag?.disagrees_with_scheme && flag.student_answer_valid
      && flag.justified_marks != null && flag.justified_marks !== q.awarded_marks) {
    return "check";
  }
  if (flag) return "watch";
  const nothingExtracted = !(q.student_answer ?? "").trim()
    && !(q.working_summary ?? "").trim();
  if (nothingExtracted) return "watch";
  return "ok";
}

export interface TopicGroup<T extends GroupableQuestion = GroupableQuestion> {
  topic: string;
  earned: number;
  available: number;
  /** earned / available * 100, or 0.0 when nothing is available. */
  percentage: number;
  questions: T[];
  /** Per-question triage rollup. */
  checkCount: number;
  watchCount: number;
  mastery: MasteryBand;
}

export type MasteryBand = "secure" | "developing" | "needs-work";

export const MASTERY_LABEL: Record<MasteryBand, string> = {
  secure: "Secure",
  developing: "Developing",
  "needs-work": "Needs work",
};

/** Derived reading of a percentage — labelling, never a grade change. */
export function masteryBand(percentage: number): MasteryBand {
  if (percentage >= 75) return "secure";
  if (percentage >= 50) return "developing";
  return "needs-work";
}

/** Fallback topic label — matches the engine's normaliser default. */
export const UNKNOWN_TOPIC = "Unclassified";

/**
 * Group questions by topic, preserving first-appearance (paper) order.
 *
 * Arithmetic mirrors the reference app's `make_group_rows` shape (earned /
 * available / percentage per group), but grouped by TOPIC rather than fixed
 * question chunks, with a triage rollup and a derived mastery band.
 */
export function buildTopicGroups<T extends GroupableQuestion>(
  questions: readonly T[],
): TopicGroup<T>[] {
  const order: string[] = [];
  const byTopic = new Map<string, T[]>();
  for (const q of questions) {
    const topic = (q.topic ?? "").trim() || UNKNOWN_TOPIC;
    if (!byTopic.has(topic)) {
      byTopic.set(topic, []);
      order.push(topic);
    }
    byTopic.get(topic)!.push(q);
  }
  return order.map((topic) => {
    const qs = byTopic.get(topic)!;
    const earned = qs.reduce((sum, q) => sum + (q.awarded_marks || 0), 0);
    const available = qs.reduce((sum, q) => sum + (q.max_marks || 0), 0);
    const percentage = available > 0 ? (earned / available) * 100 : 0.0;
    const triage = qs.map(questionTriage);
    return {
      topic,
      earned,
      available,
      percentage,
      questions: qs,
      checkCount: triage.filter((t) => t === "check").length,
      watchCount: triage.filter((t) => t === "watch").length,
      mastery: masteryBand(percentage),
    };
  });
}
