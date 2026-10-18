import { useCallback, useEffect, useState } from "react";
import { api, displayName, timeAgo, type ReviewItem } from "../api";
import {
  Badge, Button, DataTable, Empty, Modal, PageHeader, useToast,
} from "../components";

export default function ReviewPage({ onNavigate }: { onNavigate: (r: string) => void }) {
  const toast = useToast();
  const [items, setItems] = useState<(ReviewItem & { automation_hold?: boolean })[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [overrideFor, setOverrideFor] = useState<ReviewItem | null>(null);
  const [mark, setMark] = useState("");
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState<string | null>(null);
  const [actionError, setActionError] =
    useState<{ item: ReviewItem; message: string } | null>(null);

  const load = useCallback(() => {
    api<{ items: (ReviewItem & { automation_hold?: boolean })[]; total: number }>("/review?limit=500")
      .then((r) => setItems(r.items)).catch((e) => setError(e.message));
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
      toast("err", (e as Error).message);
    } finally { setBusy(null); }
  }

  async function accept(item: ReviewItem) {
    setBusy(item.id);
    setActionError(null);
    try {
      await api(`/results/${item.id}/override`, {
        method: "POST", body: JSON.stringify({ note: "Accepted by teacher" }),
      });
      // Optimistically drop the row so the queue visibly reacts; load() then
      // reconciles against the database (source of truth).
      setItems((prev) => (prev ?? []).filter((r) => r.id !== item.id));
      toast("ok", "\u2713 Result approved");
      load();
    } catch (e) {
      setActionError({ item, message: (e as Error).message });
    } finally { setBusy(null); }
  }

  if (!items) {
    return (
      <>
        <PageHeader title="Review Queue" icon="review"
          sub="Results the engine flagged for a human decision." />
        {error ? <div className="notice err">{error}</div> : <div className="skeleton" style={{ height: 160 }} />}
      </>
    );
  }

  return (
    <>
      <PageHeader title="Review Queue" icon="review"
        sub="Flagged or low-confidence results. Accept them or set your own mark — nothing is published to Classroom without your confirmation.">
        <Button variant="ghost" icon="refresh" onClick={load}>Refresh</Button>
        {items.length > 0 && (
          <Badge tone="review" icon="review">{items.length} awaiting review</Badge>
        )}
      </PageHeader>

      {error && <div className="notice err" style={{ marginBottom: 12 }}>{error}</div>}

      {actionError && (
        <div className="notice err" style={{ marginBottom: 12 }}>
          <strong>Approval failed.</strong> The result could not be saved.
          <div className="small" style={{ marginTop: 4 }}>{actionError.message}</div>
          <div className="row" style={{ gap: 8, marginTop: 8 }}>
            <Button size="sm" variant="secondary" icon="refresh"
              loading={busy === actionError.item.id}
              onClick={() => accept(actionError.item)}>Retry</Button>
            <Button size="sm" variant="ghost"
              onClick={() => onNavigate("#/diagnostics")}>View Diagnostics</Button>
            <Button size="sm" variant="ghost"
              onClick={() => setActionError(null)}>Dismiss</Button>
          </div>
        </div>
      )}

      {items.length === 0 ? (
        <div className="card">
          <Empty icon="check-circle" art="check" title="Nothing needs review"
            hint="No corrections are flagged right now. Results with low confidence, validation failures or large score changes appear here."
            action={<Button variant="secondary" icon="assignments"
              onClick={() => onNavigate("#/assignments")}>Go to assignments</Button>} />
        </div>
      ) : (
        <div className="card">
          <DataTable head={
            <tr>
              <th>Student</th><th>Assignment</th>
              <th className="num">Mark</th><th>Confidence</th><th>Reason</th>
              <th>Flagged</th><th>Action</th>
            </tr>
          }>
            {items.map((r) => (
              <tr key={r.id}>
                <td data-label="Student">
                  {r.student_id ? (
                    <a className="student-link" href={`#/students/${encodeURIComponent(r.student_id)}`}>
                      {displayName(r)}
                    </a>
                  ) : displayName(r)}
                </td>
                <td data-label="Assignment" className="wrap-cell">
                  {r.assignment_title ?? "—"}
                </td>
                <td data-label="Mark" className="num">
                  {r.earned}/{r.available}
                  <span className="faint small"> ({Math.round(r.percentage)}%)</span>
                </td>
                <td data-label="Confidence">
                  <Badge tone={
                    r.confidence === "LOW" ? "warn"
                      : r.confidence === "VALIDATION_FAILED" ? "err"
                      : r.confidence === "LARGE_SCORE_CHANGE" ? "warn" : "mute"}>
                    {r.confidence ?? "—"}
                  </Badge>
                </td>
                <td data-label="Reason" className="wrap-cell small muted">
                  {r.confidence_reasons?.length ? (
                    <ul style={{ margin: 0, paddingLeft: 16 }}>
                      {r.confidence_reasons.slice(0, 3).map((reason, i) => (
                        <li key={i}>{reason}</li>
                      ))}
                      {r.confidence_reasons.length > 3 && (
                        <li className="faint">+{r.confidence_reasons.length - 3} more</li>
                      )}
                    </ul>
                  ) : "Flagged for review"}
                </td>
                <td data-label="Flagged" className="small faint">{timeAgo(r.created_at)}</td>
                <td data-label="Action">
                  {r.automation_hold ? <Button size="sm" onClick={() => onNavigate("#/automation")}>
                    Review delivery in Automation</Button> : <div className="row" style={{ gap: 6, flexWrap: "nowrap" }}>
                    <Button size="sm" variant="success" icon="check"
                      loading={busy === r.id} onClick={() => accept(r)}>
                      Accept
                    </Button>
                    <Button size="sm" variant="secondary" icon="edit"
                      onClick={() => { setOverrideFor(r); setMark(String(r.earned)); setNote(""); }}>
                      Override
                    </Button>
                  </div>}
                </td>
              </tr>
            ))}
          </DataTable>
        </div>
      )}

      {overrideFor && (
        <Modal title="Set your own mark" icon="edit" onClose={() => setOverrideFor(null)}
          footer={
            <>
              <Button variant="ghost" onClick={() => setOverrideFor(null)}>Cancel</Button>
              <Button variant="primary" icon="check" loading={busy === "override"}
                onClick={submitOverride}>Save override</Button>
            </>
          }>
          <p className="muted" style={{ marginTop: 0 }}>
            {displayName(overrideFor)} · {overrideFor.assignment_title ?? "Assignment"}
          </p>
          <label className="field">
            <span>Mark (out of {overrideFor.available})</span>
            <input className="input" type="number" min={0} max={overrideFor.available}
              value={mark} onChange={(e) => setMark(e.target.value)} />
          </label>
          <label className="field">
            <span>Note (optional)</span>
            <input className="input" value={note} placeholder="Why you changed the mark"
              onChange={(e) => setNote(e.target.value)} />
          </label>
          <div className="notice info small">
            An override is preserved permanently — no later automated run can replace it.
          </div>
        </Modal>
      )}
    </>
  );
}
