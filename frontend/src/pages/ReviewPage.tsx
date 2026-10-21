import { useCallback, useEffect, useMemo, useState } from "react";
import { api, displayName, timeAgo, type ReviewItem } from "../api";
import {
  Badge, Button, Empty, Field, FilterBar, Modal, PageHeader, useToast,
  type Tone,
} from "../components";
import { friendlyError } from "../errors";

type Item = ReviewItem & { automation_hold?: boolean };

const CONFIDENCE_TONE: Record<string, Tone> = {
  LOW: "warn",
  VALIDATION_FAILED: "err",
  LARGE_SCORE_CHANGE: "warn",
};

const CONFIDENCE_LABEL: Record<string, string> = {
  LOW: "Low confidence",
  MEDIUM: "Medium confidence",
  HIGH: "High confidence",
  VALIDATION_FAILED: "Check failed",
  LARGE_SCORE_CHANGE: "Big score change",
};
const isConfidence = (key: string) => key === "LOW" || key === "MEDIUM" || key === "HIGH";
// Badges read "Confidence · medium": the prefix says what the value is, the
// level is not repeated as the words "medium confidence" again.
const CONFIDENCE_LEVEL: Record<string, string> = {
  LOW: "low", MEDIUM: "medium", HIGH: "high",
};

export default function ReviewPage({ onNavigate }: { onNavigate: (r: string) => void }) {
  const toast = useToast();
  const [items, setItems] = useState<Item[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [overrideFor, setOverrideFor] = useState<Item | null>(null);
  const [mark, setMark] = useState("");
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState<string | null>(null);
  const [actionError, setActionError] =
    useState<{ item: Item; message: string } | null>(null);
  const [query, setQuery] = useState("");
  const [filter, setFilter] = useState<string>("all");

  const load = useCallback(() => {
    api<{ items: Item[]; total: number }>("/review?limit=500")
      .then((r) => setItems(r.items))
      .catch((e) => setError(friendlyError(e).summary));
  }, []);
  useEffect(() => { load(); }, [load]);

  async function submitOverride() {
    if (!overrideFor) return;
    setBusy("override");
    try {
      const body: Record<string, unknown> = { note };
      if (mark.trim() !== "") body.earned = Number(mark);
      await api(`/results/${overrideFor.id}/override`, {
        method: "POST", body: JSON.stringify(body),
      });
      toast("ok", "Result accepted with your override");
      setOverrideFor(null); setMark(""); setNote("");
      load();
    } catch (e) {
      toast("err", friendlyError(e).summary);
    } finally { setBusy(null); }
  }

  async function accept(item: Item) {
    setBusy(item.id);
    setActionError(null);
    try {
      await api(`/results/${item.id}/override`, {
        method: "POST", body: JSON.stringify({ note: "Accepted by teacher" }),
      });
      // Optimistically drop the row so the queue visibly reacts; load() then
      // reconciles against the database (source of truth).
      setItems((prev) => (prev ?? []).filter((r) => r.id !== item.id));
      toast("ok", "Result approved");
      load();
    } catch (e) {
      setActionError({ item, message: friendlyError(e).summary });
    } finally { setBusy(null); }
  }

  const counts = useMemo(() => {
    const list = items ?? [];
    const by: Record<string, number> = {};
    for (const r of list) by[r.confidence ?? "OTHER"] = (by[r.confidence ?? "OTHER"] ?? 0) + 1;
    return by;
  }, [items]);

  const visible = useMemo(() => {
    const q = query.trim().toLowerCase();
    return (items ?? []).filter((r) => {
      if (filter !== "all" && (r.confidence ?? "OTHER") !== filter) return false;
      if (!q) return true;
      return [displayName(r), r.assignment_title ?? "", ...(r.confidence_reasons ?? [])]
        .join(" ").toLowerCase().includes(q);
    });
  }, [items, filter, query]);

  const oldest = useMemo(() => {
    const list = items ?? [];
    if (!list.length) return null;
    return list.reduce((a, b) => (a.created_at < b.created_at ? a : b));
  }, [items]);

  return (
    <>
      <PageHeader
        title="Review queue"
        sub="Results the engine would not sign off on its own. Accept the evidence, or set the mark yourself — nothing reaches Google Classroom until you decide."
        meta={
          items === null ? <span>Loading…</span> : (
            <>
              <span>{items.length} waiting</span>
              {oldest && (
                <>
                  <span aria-hidden="true">·</span>
                  <span>oldest flagged {timeAgo(oldest.created_at)}</span>
                </>
              )}
              <span aria-hidden="true">·</span>
              <span>your decision is final and cannot be overwritten by a later run</span>
            </>
          )
        }
      >
        <Button variant="ghost" icon="refresh" onClick={load}>Refresh</Button>
      </PageHeader>

      {error && (
        <div className="notice err" role="alert" style={{ marginBottom: "var(--s3)" }}>
          <span>
            <strong>The queue could not be loaded.</strong> {error}
          </span>
        </div>
      )}

      {actionError && (
        <div className="notice err" role="alert" style={{ marginBottom: "var(--s3)" }}>
          <div style={{ flex: 1 }}>
            <strong>That mark was not saved.</strong> {actionError.message}
            <div className="small" style={{ marginTop: 6 }}>
              The result is unchanged in the database.
            </div>
          </div>
          <div className="row" style={{ gap: 6, flexShrink: 0 }}>
            <Button size="sm" variant="secondary" icon="refresh"
              loading={busy === actionError.item.id}
              onClick={() => accept(actionError.item)}>Retry</Button>
            <Button size="sm" variant="ghost"
              onClick={() => setActionError(null)}>Dismiss</Button>
          </div>
        </div>
      )}

      {items !== null && items.length > 0 && (
        <>
          <FilterBar search={query} onSearch={setQuery}
            placeholder="Student, assignment or reason"
            ariaLabel="Filter the review queue">
            <div className="chip-row" role="group" aria-label="Reason">
              <button type="button" className={`chip ${filter === "all" ? "active" : ""}`}
                aria-pressed={filter === "all"} onClick={() => setFilter("all")}>
                All <span className="chip-count">{items.length}</span>
              </button>
              {Object.entries(counts).map(([key, n]) => (
                <button key={key} type="button"
                  className={`chip ${filter === key ? "active" : ""}`}
                  aria-pressed={filter === key}
                  onClick={() => setFilter(key)}>
                  {CONFIDENCE_LABEL[key] ?? key} <span className="chip-count">{n}</span>
                </button>
              ))}
            </div>
          </FilterBar>

          {visible.length === 0 ? (
            <div className="card">
              <Empty icon="search" title="No results match that filter"
                hint="Clear the search or choose All to see the whole queue." />
            </div>
          ) : (
            <div className="table-wrap">
              <table className="table table-stack">
                <thead>
                  <tr>
                    <th scope="col">Student</th>
                    <th scope="col">Assignment</th>
                    <th scope="col" className="num">Mark</th>
                    <th scope="col">Why it was flagged</th>
                    <th scope="col">Flagged</th>
                    <th scope="col"><span className="visually-hidden">Decision</span></th>
                  </tr>
                </thead>
                <tbody>
                  {visible.map((r, rowIndex) => (
                    <tr key={`${r.id}:${rowIndex}`}>
                      <td data-label="Student" className="cell-strong">
                        {r.student_id ? (
                          <a className="student-link"
                            href={`#/students/${encodeURIComponent(r.student_id)}`}>
                            {displayName(r)}
                          </a>
                        ) : displayName(r)}
                      </td>
                      <td data-label="Assignment" className="wrap-cell">
                        {r.assignment_title ?? "—"}
                      </td>
                      <td data-label="Mark" className="num">
                        {r.partial ? (
                          // Never show 22/22 for a paper whose 45 other
                          // questions were never marked: that reads as full
                          // marks. Show the coverage that is missing instead.
                          <>
                            <Badge tone="err">Incomplete</Badge>
                            <span className="cell-sub">
                              {r.partial.marked_questions} of{" "}
                              {r.partial.expected_questions} questions marked
                            </span>
                            <span className="cell-sub">
                              {r.partial.unresolved_questions} unmarked
                              {r.partial.paper_marks
                                ? ` · ${r.partial.unresolved_marks} of ${r.partial.paper_marks} marks`
                                : ""}
                            </span>
                          </>
                        ) : (
                          <>
                            <span className="markglyph">
                              <span className="n">{r.earned}</span>
                              <span className="d">/{r.available}</span>
                            </span>
                            <span className="cell-sub">{Math.round(r.percentage)}%</span>
                          </>
                        )}
                      </td>
                      <td data-label="Why flagged" className="wrap-cell">
                        <div className="row wrap" style={{ gap: 6 }}>
                          <Badge tone={CONFIDENCE_TONE[r.confidence ?? ""] ?? "mute"}>
                            {isConfidence(r.confidence ?? "")
                              ? `Confidence · ${CONFIDENCE_LEVEL[r.confidence!]}`
                              : CONFIDENCE_LABEL[r.confidence ?? ""] ?? r.confidence ?? "Flagged"}
                          </Badge>
                          {r.confidence_reasons?.length ? (
                            <span className="small muted">
                              {r.confidence_reasons.slice(0, 2).join(" · ")}
                              {r.confidence_reasons.length > 2
                                ? ` · +${r.confidence_reasons.length - 2} more` : ""}
                            </span>
                          ) : (
                            <span className="small muted">Flagged for review</span>
                          )}
                        </div>
                      </td>
                      <td data-label="Flagged" className="small faint nowrap">
                        {timeAgo(r.created_at)}
                      </td>
                      <td data-label="Decision">
                        {r.automation_hold ? (
                          <Button size="sm" variant="secondary"
                            onClick={() => onNavigate("#/automation")}>
                            Open in Automation
                          </Button>
                        ) : (
                          <div className="row" style={{ gap: 6, justifyContent: "flex-end" }}>
                            {!r.partial && (
                              // "Accept" would publish the marked-subset
                              // fraction as the score. A partial correction can
                              // only be settled with a real whole-paper mark.
                              <Button size="sm" variant="success" icon="check"
                                loading={busy === r.id} onClick={() => accept(r)}>
                                Accept
                              </Button>
                            )}
                            <Button size="sm" variant="secondary"
                              onClick={() => {
                                setOverrideFor(r);
                                setMark(String(r.earned));
                                setNote("");
                              }}>
                              Change mark
                            </Button>
                          </div>
                        )}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </>
      )}

      {items !== null && items.length === 0 && (
        <div className="card">
          <Empty icon="check-circle" art="check" title="Nothing needs a decision"
            hint="No result is flagged right now. Low-confidence marks, failed validation and unusually large score changes appear here."
            action={<Button variant="secondary" icon="assignments"
              onClick={() => onNavigate("#/assignments")}>Go to assignments</Button>} />
        </div>
      )}

      {overrideFor && (
        <Modal title="Set the mark yourself" onClose={() => setOverrideFor(null)}
          footer={
            <>
              <Button variant="ghost" onClick={() => setOverrideFor(null)}>Cancel</Button>
              <Button variant="primary" icon="check" loading={busy === "override"}
                onClick={submitOverride}>Save mark</Button>
            </>
          }>
          <p className="muted" style={{ marginTop: 0 }}>
            {displayName(overrideFor)} · {overrideFor.assignment_title ?? "Assignment"}
            {" "}· {overrideFor.partial
              ? `only ${overrideFor.partial.marked_questions} of ${overrideFor.partial.expected_questions} questions were marked (${overrideFor.partial.unresolved_questions} unmarked)`
              : `the engine awarded ${overrideFor.earned} of ${overrideFor.available}`}
          </p>
          <Field label={`Your mark (out of ${overrideFor.partial?.paper_marks ?? overrideFor.available})`}
            hint={overrideFor.partial
              ? "This correction is incomplete, so a mark here is the mark for the whole paper as you judge it."
              : "Leave the number as it is to accept the engine's mark with your note."}>
            <input className="input" type="number" min={0}
              max={overrideFor.partial?.paper_marks ?? overrideFor.available}
              value={mark} onChange={(e) => setMark(e.target.value)} />
          </Field>
          <Field label="Note" hint="Stored with the result as the reason for your decision.">
            <input className="input" value={note}
              placeholder="e.g. method mark allowed, working shown on page 2"
              onChange={(e) => setNote(e.target.value)} />
          </Field>
          <div className="notice info small">
            <span>
              An override is preserved permanently — no later automated run can
              replace it, and the original mark stays in the audit trail.
            </span>
          </div>
        </Modal>
      )}
    </>
  );
}
