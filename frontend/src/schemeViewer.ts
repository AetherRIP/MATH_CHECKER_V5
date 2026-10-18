// Source-page viewer state for the mark-scheme review surface.
//
// The viewer shows the ORIGINAL uploaded page next to the questions read from
// it. Almost everything that goes wrong in such a viewer is state, not layout:
// the page jumps back to 1 while the teacher is reading, selecting a question
// silently drops the page, clicking the already-selected question re-fetches
// the image and blanks it. So the state lives here, as pure functions with no
// React in sight, and the component is a thin shell over them.
//
// Types are erasable-only: the frontend tests run this file directly under
// `node --test`, which strips types without a build step.

/** One question as the page-scoped read returned it. */
export interface SchemePageItem {
  page?: number | null;
  source_index?: number | null;
  label?: string | null;
  max_marks?: number | null;
}

/** One page of the uploaded mark scheme, as `/mark-scheme/pages` reports it. */
export interface SchemePageSummary {
  page: number;
  has_items: boolean;
  item_count: number;
  indices: number[];
  needs_review: boolean;
}

export interface SchemePagesPayload {
  page_count: number;
  pages?: SchemePageSummary[];
  items?: SchemePageItem[];
  page_url_template?: string;
  dpi?: number;
  completeness?: Record<string, unknown>;
  verification_state?: string;
  conflicts?: SchemeConflict[];
  label_variants?: SchemeLabelVariant[];
}

/** One read of a disputed printed index, as that read transcribed it. */
export interface SchemeConflictVariant {
  chunk?: string | null;
  label?: string | null;
  max_marks?: number | null;
  parts?: number;
  parts_detail?: [string, number][];
  page?: number | null;
}

/** Two overlapping page reads that disagree about the marks of one index. */
export interface SchemeConflict {
  source_index: number;
  kind?: string;
  chunks?: string[];
  variants?: SchemeConflictVariant[];
  provisional_from_chunk?: string | null;
}

/** Labels transcribed differently while every mark agreed (information only). */
export interface SchemeLabelVariant {
  source_index: number;
  kind?: "label" | "parts" | "both";
  labels?: string[];
  part_labels?: string[][];
  chunks?: string[];
  page?: number | null;
}

/** What the viewer currently shows. */
export interface ViewerSelection {
  page: number;
  /** The selected question's printed label/index, or null. */
  question: string | null;
}

export interface ViewerModel {
  pageCount: number;
  pages: SchemePageSummary[];
  /** Printed index -> the page it was read from. */
  indexToPage: Map<number, number>;
  /** Printed label ("Q3", "3") -> the page it was read from. */
  labelToPage: Map<string, number>;
  /** Pages that returned nothing; the ones a reviewer should open. */
  emptyPages: number[];
}

export function labelKey(value: string | number | null | undefined): string {
  return String(value ?? "").trim().toLowerCase();
}

/**
 * Normalise the API payload into the maps the viewer needs.
 *
 * Missing pages are filled in rather than trusted: a document with 9 pages but
 * only 8 reported must still let the teacher open page 9.
 */
export function buildViewerModel(payload: SchemePagesPayload | null | undefined): ViewerModel {
  const pageCount = Math.max(0, Math.floor(Number(payload?.page_count) || 0));
  const byNumber = new Map<number, SchemePageSummary>();
  for (const page of payload?.pages ?? []) {
    const number = Math.floor(Number(page.page));
    if (!Number.isFinite(number) || number < 1) continue;
    byNumber.set(number, {
      page: number,
      has_items: Boolean(page.has_items),
      item_count: Number(page.item_count) || 0,
      indices: Array.isArray(page.indices) ? page.indices.map(Number) : [],
      needs_review: Boolean(page.needs_review),
    });
  }

  const pages: SchemePageSummary[] = [];
  for (let page = 1; page <= pageCount; page += 1) {
    pages.push(byNumber.get(page) ?? {
      page, has_items: false, item_count: 0, indices: [], needs_review: false,
    });
  }

  const indexToPage = new Map<number, number>();
  const labelToPage = new Map<string, number>();
  for (const item of payload?.items ?? []) {
    const page = Math.floor(Number(item.page));
    if (!Number.isFinite(page) || page < 1 || page > pageCount) continue;
    const index = Number(item.source_index);
    if (Number.isFinite(index) && !indexToPage.has(index)) indexToPage.set(index, page);
    const key = labelKey(item.label);
    if (key && !labelToPage.has(key)) labelToPage.set(key, page);
  }
  // The per-page index lists are authoritative when the flat item list is not
  // present (a cached record written before items were persisted).
  for (const page of pages) {
    for (const index of page.indices) {
      const value = Number(index);
      if (Number.isFinite(value) && !indexToPage.has(value)) indexToPage.set(value, page.page);
    }
  }

  const emptyPages = pages.filter((p) => !p.has_items).map((p) => p.page);
  return { pageCount, pages, indexToPage, labelToPage, emptyPages };
}

/** Where a question came from: printed index first, then label, then any hint. */
export function pageForQuestion(
  model: ViewerModel,
  question: { question_number?: string | number | null; source_index?: number | null; page?: number | null },
): number | null {
  const index = Number(question.source_index);
  if (Number.isFinite(index) && model.indexToPage.has(index)) {
    return model.indexToPage.get(index) ?? null;
  }
  const key = labelKey(question.question_number);
  if (key && model.labelToPage.has(key)) return model.labelToPage.get(key) ?? null;
  // "3(a)" belongs to question 3's page.
  const leading = /^(\d+)/.exec(key);
  if (leading) {
    const number = Number(leading[1]);
    if (model.indexToPage.has(number)) return model.indexToPage.get(number) ?? null;
  }
  const page = Number(question.page);
  if (Number.isFinite(page) && page >= 1 && page <= model.pageCount) return page;
  return null;
}

/** The questions the page-scoped read attributed to ONE page. */
export function itemsOnPage(model: ViewerModel, page: number): number[] {
  const found = model.pages.find((p) => p.page === page);
  return found ? [...found.indices] : [];
}

function clampPage(model: ViewerModel, page: number): number {
  if (model.pageCount < 1) return 1;
  return Math.min(Math.max(1, Math.floor(page) || 1), model.pageCount);
}

/**
 * Select a question. The page follows the question's own evidence; when the
 * evidence does not say which page it came from, the page the teacher is
 * already looking at is KEPT — never reset to 1, which is what made the viewer
 * feel like it was fighting the reader.
 */
export function selectQuestion(
  model: ViewerModel,
  selection: ViewerSelection,
  question: { question_number?: string | number | null; source_index?: number | null; page?: number | null },
  identity?: string,
): ViewerSelection {
  const key = identity ?? String(question.question_number ?? question.source_index ?? "");
  const page = pageForQuestion(model, question);
  return {
    question: key || null,
    page: page != null ? clampPage(model, page) : clampPage(model, selection.page),
  };
}

/**
 * Move to a page the teacher chose. The selected question survives only while
 * it is actually on that page, so the header can never claim "Q7 — page 2"
 * while page 2 shows Q3.
 */
export function selectPage(
  model: ViewerModel,
  selection: ViewerSelection,
  page: number,
  questionPage?: number | null,
): ViewerSelection {
  const next = clampPage(model, page);
  const onPage = (questionPage != null ? questionPage : pageForSelection(model, selection)) === next;
  return { page: next, question: onPage ? selection.question : null };
}

function pageForSelection(model: ViewerModel, selection: ViewerSelection): number | null {
  if (!selection.question) return null;
  const key = labelKey(selection.question);
  if (model.labelToPage.has(key)) return model.labelToPage.get(key) ?? null;
  const index = Number(selection.question);
  if (Number.isFinite(index) && model.indexToPage.has(index)) {
    return model.indexToPage.get(index) ?? null;
  }
  const leading = /^(\d+)/.exec(key);
  if (leading) {
    const number = Number(leading[1]);
    if (model.indexToPage.has(number)) return model.indexToPage.get(number) ?? null;
  }
  return null;
}

/** Previous/next page, keeping the selection rule above. */
export function stepPage(
  model: ViewerModel,
  selection: ViewerSelection,
  delta: number,
): ViewerSelection {
  return selectPage(model, selection, selection.page + (delta < 0 ? -1 : 1));
}

/**
 * The real disagreements that concern one page, for the page being shown.
 *
 * A conflict belongs to a page when either read placed it there, or when the
 * index it concerns is one this page contributed — the reviewer is looking at
 * the page and must be told, not left to cross-reference elsewhere.
 */
export function conflictsOnPage(
  model: ViewerModel,
  conflicts: SchemeConflict[] | null | undefined,
  page: number,
): SchemeConflict[] {
  const indices = new Set(itemsOnPage(model, page));
  const found: SchemeConflict[] = [];
  for (const conflict of conflicts ?? []) {
    const onPage = (conflict.variants ?? []).some((v) => Number(v.page) === page)
      || indices.has(Number(conflict.source_index));
    if (onPage) found.push(conflict);
  }
  return found;
}

/** A one-line description of a disputed index, naming both readings. */
export function conflictSummary(conflict: SchemeConflict): string {
  const readings = (conflict.variants ?? []).map((v) => {
    const parts = (v.parts_detail ?? [])
      .map(([number, marks]) => `${number}=${marks}`)
      .join(", ");
    return `${v.chunk ?? "?"}: ${v.max_marks ?? "?"} mark(s)`
      + (parts ? ` [${parts}]` : "");
  });
  return `index ${conflict.source_index} — ` + readings.join("  vs  ");
}

/** The URL of one page, from the template the API reports. */
export function pageImageUrl(
  template: string | null | undefined,
  page: number,
  dpi?: number | null,
): string {
  const base = template && template.includes("{page}")
    ? template
    : "/api/assignments/{courseworkId}/mark-scheme/page/{page}.png";
  let url = base.replace("{page}", String(Math.max(1, Math.floor(page) || 1)));
  if (dpi) url += `${url.includes("?") ? "&" : "?"}dpi=${Math.floor(dpi)}`;
  return url;
}

/** A one-line, honest description of a page button. */
export function pageLabel(page: SchemePageSummary): string {
  if (page.needs_review) return `Page ${page.page} — nothing read, open it`;
  if (!page.has_items) return `Page ${page.page} — no questions`;
  const n = page.item_count;
  return `Page ${page.page} — ${n} question${n === 1 ? "" : "s"}`;
}

/** True when moving to this page would change the image the user is looking at. */
export function selectionChanged(a: ViewerSelection, b: ViewerSelection): boolean {
  return a.page !== b.page || a.question !== b.question;
}
