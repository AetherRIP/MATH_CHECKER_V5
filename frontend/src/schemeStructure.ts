// Presentation policy for the mark scheme's SUBQUESTION STRUCTURE block.
//
// The block exists to be reviewed, not to be read from top to bottom: a scheme
// whose marking points were all mapped is information the teacher skims once,
// while a scheme with a part that did not come back is the thing they came for.
// So the block folds away when it is complete and opens itself when there is
// something to look at — and folded-or-open is decided HERE, as a pure function
// with no React in sight, so the rule can be tested directly (the same reason
// `schemeViewer.ts` exists).
//
// Types are erasable-only: the frontend tests run this file directly under
// `node --test`, which strips types without a build step.

/** One declared marking point, as much of it as this policy needs. */
export interface StructurePart {
  question: string | number;
  label?: string | null;
  /** ``"OK"`` unless the extraction flagged this part (missing/uncertain). */
  validation?: string | null;
}

/** The parts the teacher should look at: everything not marked ``"OK"``. */
export function flaggedParts<T extends StructurePart>(parts: T[]): T[] {
  return parts.filter((p) => (p.validation ?? "OK") !== "OK");
}

/** Should the structure block start expanded?
 *
 * Open when the structure needs attention — the completeness flag is `false`,
 * or a part is flagged, or there is an anomaly — because that is the reason the
 * block is on the page at all. A fully mapped structure starts folded, so the
 * verification verdict is what the teacher reads first. `null`/`undefined`
 * completeness (a flat scheme that declares no subquestions) is not a problem
 * and must not force the block open.
 *
 * A click on the header always wins over this default.
 */
export function structureStartsOpen(input: {
  complete?: boolean | null;
  flagged?: number | null;
  anomalies?: number | null;
}): boolean {
  return input.complete === false
    || (input.flagged ?? 0) > 0
    || (input.anomalies ?? 0) > 0;
}

/** A one-line "which parts need you" summary for the folded header.
 *
 * Folding the detail away must not hide WHAT is wrong: the header keeps naming
 * the flagged parts (``Q4(b)``) so the teacher can decide to open it without
 * opening it. Returns the labels to show and how many were left out.
 */
export function flaggedSummary(parts: StructurePart[], limit = 4): {
  labels: string[]; more: number;
} {
  const flagged = flaggedParts(parts);
  if (flagged.length <= limit) return { labels: flagged.map(partLabel), more: 0 };
  return { labels: flagged.slice(0, limit).map(partLabel),
           more: flagged.length - limit };
}

/** ``Q4 (b)`` for a part, or plain ``Q4`` when the read had no label for it.
 *
 * Deliberately the same shape the structure table prints, so the folded
 * summary and the expanded table never name the same part differently.
 */
export function partLabel(part: StructurePart): string {
  const label = (part.label ?? "").trim();
  return label ? `Q${part.question} ${label}` : `Q${part.question}`;
}
