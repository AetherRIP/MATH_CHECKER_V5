// Source-page viewer: the ACTUAL uploaded mark-scheme page, next to the
// questions that were read from it.
//
// This is the answer to "where did this question come from?". The backend
// renders the real page (never a reconstruction) and reports which printed
// indices each page contributed; the state rules that keep the page and the
// selected question consistent live in ../schemeViewer.ts and are tested
// independently, so this component only renders them.
import { useCallback, useEffect, useMemo, useState } from "react";
import { api } from "../api";
import { Badge, Button, Empty, Icon, Section } from "../components";
import {
  buildViewerModel,
  conflictSummary,
  conflictsOnPage,
  itemsOnPage,
  pageImageUrl,
  pageLabel,
  selectPage,
  selectQuestion,
  stepPage,
  type SchemePagesPayload,
  type ViewerSelection,
} from "../schemeViewer";

export interface ViewerQuestion {
  question_number: string;
  max_marks: number;
  source_index?: number;
  page?: number;
  /** The descriptive header the document prints, if it prints one. */
  printed_label?: string;
}

const STATE_TONE: Record<string, "ok" | "warn" | "err" | "info" | "mute"> = {
  VERIFIED: "ok",
  NEEDS_REPAIR: "warn",
  REPAIRING: "info",
  REVERIFYING: "info",
  VERIFYING: "info",
  NEEDS_MANUAL_REVIEW: "warn",
  NOT_VERIFIED: "mute",
};

export default function SchemeSourceViewer({ courseworkId, questions,
  focusQuestion, onClose }: {
  courseworkId: string;
  questions: ViewerQuestion[];
  focusQuestion?: string | null;
  onClose?: () => void;
}) {
  const [payload, setPayload] = useState<SchemePagesPayload | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [selection, setSelection] = useState<ViewerSelection>({ page: 1, question: null });
  const [zoom, setZoom] = useState(100);

  useEffect(() => {
    let live = true;
    setError(null);
    api<SchemePagesPayload>(`/assignments/${encodeURIComponent(courseworkId)}/mark-scheme/pages`)
      .then((res) => { if (live) setPayload(res); })
      .catch((e) => { if (live) setError((e as Error).message); });
    return () => { live = false; };
  }, [courseworkId]);

  const model = useMemo(() => buildViewerModel(payload), [payload]);

  // A question opened from elsewhere (an anomaly row, a review link) jumps the
  // viewer straight to its source page — without resetting the page if the
  // structure has no page evidence for it.
  useEffect(() => {
    if (!focusQuestion || !payload) return;
    const target = questions.find((q) => q.question_number === focusQuestion);
    setSelection((current) => selectQuestion(model, current,
      target ?? { question_number: focusQuestion }, focusQuestion));
  }, [focusQuestion, payload, model, questions]);

  const questionPage = useCallback((key: string | null) => {
    if (!key) return null;
    const found = questions.find((q) => q.question_number === key);
    if (!found) return null;
    const fromModel = model.labelToPage.get(key.trim().toLowerCase());
    if (fromModel) return fromModel;
    return found.page ?? null;
  }, [questions, model]);

  const goto = (page: number) =>
    setSelection((current) => selectPage(model, current, page, questionPage(current.question)));

  if (error) {
    return (
      <Section title="Source pages" icon="file"
        actions={onClose ? <Button size="sm" variant="ghost" onClick={onClose}>Close</Button> : undefined}>
        <div className="notice err"><Icon name="alert" size={16} />
          <span>{error}</span></div>
      </Section>
    );
  }
  if (!payload) return <div className="skeleton" style={{ height: 220 }} />;
  if (model.pageCount === 0) {
    return (
      <Section title="Source pages" icon="file">
        <Empty icon="file" title="No readable pages"
          hint="The uploaded mark scheme reports no pages, so there is nothing to show. Re-upload it or check the file." />
      </Section>
    );
  }

  const shown = payload.verification_state ?? null;
  const disputed = conflictsOnPage(model, payload.conflicts, selection.page);
  const labelVariants = (payload.label_variants ?? []).filter((v) =>
    (v.page ?? null) === selection.page
    || model.indexToPage.get(Number(v.source_index)) === selection.page);
  const selected = selection.question;
  const indicesHere = itemsOnPage(model, selection.page);
  const image = pageImageUrl(payload.page_url_template, selection.page, payload.dpi);
  const empty = model.emptyPages.length;

  return (
    <Section title="Source pages" icon="file"
      hint={`${model.pageCount} page(s)${empty ? ` · ${empty} with nothing read` : ""}`}
      actions={
        <div className="row wrap" style={{ gap: 8 }}>
          {shown && <Badge tone={STATE_TONE[shown] ?? "mute"}>{shown.replace(/_/g, " ")}</Badge>}
          {onClose && <Button size="sm" variant="ghost" onClick={onClose}>Close</Button>}
        </div>
      }>
      <div style={{ display: "grid", gridTemplateColumns: "minmax(150px, 200px) 1fr", gap: 12 }}>
        <div>
          <div className="small muted" style={{ marginBottom: 4 }}>Pages</div>
          <div style={{ maxHeight: 340, overflowY: "auto", display: "flex",
            flexDirection: "column", gap: 4 }}>
            {model.pages.map((page) => (
              <button key={page.page} type="button"
                className={`btn ${page.page === selection.page ? "" : "ghost"} sm`}
                onClick={() => goto(page.page)}
                title={pageLabel(page)}
                style={{ justifyContent: "flex-start",
                  borderColor: page.page === selection.page ? "var(--accent)" : undefined }}>
                <span className="num">{page.page}</span>
                <span className="small" style={{ marginLeft: 8 }}>
                  {page.has_items ? `${page.item_count} question(s)` : "nothing read"}
                </span>
                {page.needs_review && (
                  <Icon name="alert" size={12}
                    style={{ marginLeft: "auto", color: "var(--warn)" }} />
                )}
              </button>
            ))}
          </div>
          <div className="row wrap" style={{ gap: 6, marginTop: 8 }}>
            <Button size="sm" variant="ghost" icon="chevron-left"
              disabled={selection.page <= 1}
              onClick={() => setSelection((s) => stepPage(model, s, -1))}>Prev</Button>
            <Button size="sm" variant="ghost" icon="chevron-right"
              disabled={selection.page >= model.pageCount}
              onClick={() => setSelection((s) => stepPage(model, s, 1))}>Next</Button>
          </div>
          <div className="row wrap" style={{ gap: 6, marginTop: 8 }}>
            <Button size="sm" variant="ghost"
              onClick={() => setZoom((z) => Math.max(60, z - 20))}>−</Button>
            <span className="small muted" style={{ minWidth: 40, textAlign: "center" }}>
              {zoom}%</span>
            <Button size="sm" variant="ghost"
              onClick={() => setZoom((z) => Math.min(220, z + 20))}>+</Button>
          </div>
        </div>

        <div>
          <div className="row between wrap" style={{ gap: 8, marginBottom: 6 }}>
            <div className="small">
              <strong>Page {selection.page} of {model.pageCount}</strong>
              {indicesHere.length > 0
                ? <span className="muted"> · printed index/indices {indicesHere.join(", ")}</span>
                : <span className="muted"> · no question was read from this page</span>}
            </div>
            {selected && <Badge tone="info">Q{selected}</Badge>}
          </div>
          <div style={{ maxHeight: 460, overflow: "auto", border: "1px solid var(--line)",
            borderRadius: 8, background: "var(--surface-2)" }}>
            {/* width is what makes the page legible; the browser keeps the
                cached image, so changing zoom does not re-request it. */}
            <img src={image} alt={`Mark scheme page ${selection.page}`}
              style={{ display: "block", width: `${zoom}%`, maxWidth: "none",
                height: "auto", margin: "0 auto" }} />
          </div>
          {/* The disagreement itself, on the page it concerns: "flagged for
              review" is only useful if both readings are visible. */}
          {disputed.map((conflict) => (
            <div key={conflict.source_index} className="notice warn"
              style={{ marginTop: 8 }}>
              <Icon name="alert" size={14} />
              <div>
                <strong>Index {conflict.source_index} was read two ways.</strong>
                <div className="small" style={{ marginTop: 2 }}>
                  {conflictSummary(conflict)}
                </div>
                <div className="small muted">
                  Both readings are kept. Correct the structure by hand if you
                  disagree with the retained one.
                </div>
              </div>
            </div>
          ))}
          {labelVariants.length > 0 && (
            <div className="small muted" style={{ marginTop: 8 }}>
              Transcribed differently but with identical marks (no action needed):{" "}
              {labelVariants.map((v) => `index ${v.source_index}`).join(", ")}.
            </div>
          )}

          <div className="row wrap" style={{ gap: 8, marginTop: 8 }}>
            {questions.slice(0, 400).map((q) => {
              const page = model.labelToPage.get(q.question_number.trim().toLowerCase())
                ?? q.page ?? null;
              if (page == null) return null;
              return (
                <Button key={q.question_number} size="sm"
                  variant={q.question_number === selected ? "secondary" : "ghost"}
                  onClick={() => setSelection((s) => selectQuestion(model, s, q, q.question_number))}
                  title={[q.printed_label, `Read from page ${page}`]
                    .filter(Boolean).join(" · ")}>
                  Q{q.question_number}
                  {q.max_marks ? <span className="muted small"> · {q.max_marks}</span> : null}
                </Button>
              );
            })}
          </div>
        </div>
      </div>
      <div className="small muted" style={{ marginTop: 10 }}>
        These are the pages of the file you uploaded, rendered as they are. A page with
        nothing read is either a cover, or a page whose read failed — it is flagged rather
        than assumed empty.
      </div>
    </Section>
  );
}
