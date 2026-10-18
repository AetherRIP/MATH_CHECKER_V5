// Mark-scheme review: suspected errors, evidence, and approval-gated bulk
// corrections. Staging a proposal changes nothing; only the explicit approval
// action below can change previously graded marks.
import { useCallback, useEffect, useState } from "react";
import {
  api, approveSchemeProposal, decideSchemeDiscrepancy,
  fetchSchemeDiscrepancies, fetchSchemeDiscrepancy,
  proposeSchemeCorrection, rejectSchemeProposal, timeAgo,
  type ProposalItem, type SchemeDiscrepancy, type SchemeProposal,
} from "../api";
import {
  Badge, Button, DataTable, Empty, Modal, PageHeader, Section, useToast,
} from "../components";

const STATE_TONE: Record<string, "ok" | "warn" | "err" | "info"> = {
  CONFIRMED_DISCREPANCY: "err",
  POSSIBLE_DISCREPANCY: "warn",
  VALID_ALTERNATIVE: "info",
  UNCERTAIN: "warn",
  HUMAN_REVIEWED: "ok",
};

const STATE_LABEL: Record<string, string> = {
  CONFIRMED_DISCREPANCY: "Confirmed discrepancy",
  POSSIBLE_DISCREPANCY: "Possible discrepancy",
  VALID_ALTERNATIVE: "Valid alternative answer",
  UNCERTAIN: "Uncertain — needs review",
  HUMAN_REVIEWED: "Human review completed",
};

const DECISIONS: [string, string][] = [
  ["CONFIRM_SCHEME_INCORRECT", "Confirm mark scheme is incorrect"],
  ["ACCEPT_ALTERNATIVE_ANSWER", "Accept the student's alternative answer"],
  ["AWARD_PARTIAL_CREDIT", "Award partial credit"],
  ["CONFIRM_SCHEME_CORRECT", "Confirm the mark scheme is correct"],
  ["REQUEST_FURTHER_INVESTIGATION", "Request further investigation"],
];

export default function SchemeReviewPage({ discrepancyId }: { discrepancyId?: string }) {
  const toast = useToast();
  const [items, setItems] = useState<SchemeDiscrepancy[] | null>(null);
  const [selected, setSelected] = useState<SchemeDiscrepancy | null>(null);
  const [history, setHistory] = useState<Awaited<
    ReturnType<typeof fetchSchemeDiscrepancy>>["review_history"]>([]);
  const [proposal, setProposal] = useState<SchemeProposal | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [approveOpen, setApproveOpen] = useState(false);
  const [confirmedReview, setConfirmedReview] = useState(false);
  const [publish, setPublish] = useState(false);

  const loadList = useCallback(() => {
    fetchSchemeDiscrepancies()
      .then((r) => setItems(r.items))
      .catch((e) => setError((e as Error).message));
  }, []);

  const loadDetail = useCallback((id: string) => {
    fetchSchemeDiscrepancy(id)
      .then((r) => {
        setSelected(r.discrepancy);
        setHistory(r.review_history);
        const latest = r.proposals.sort((a, b) => b.created_at - a.created_at)[0];
        setProposal(latest ?? null);
      })
      .catch((e) => setError((e as Error).message));
  }, []);

  useEffect(() => { loadList(); }, [loadList]);
  useEffect(() => { if (discrepancyId) loadDetail(discrepancyId); }, [discrepancyId, loadDetail]);

  async function prepare() {
    if (!selected) return;
    setBusy("propose"); setError(null);
    try {
      const p = await proposeSchemeCorrection(selected.id);
      setProposal(p);
      toast("ok", p.changed_count > 0
        ? `Proposed ${p.changed_count} change(s) — staged, nothing applied yet.`
        : "Proposal prepared — no submission has a verified change yet.");
    } catch (e) { setError((e as Error).message); }
    finally { setBusy(null); }
  }

  async function doApprove() {
    if (!proposal) return;
    setBusy("approve");
    try {
      const r = await approveSchemeProposal(proposal.id, {
        publish, affectedCount: proposal.affected_count,
      });
      setProposal(r);
      setApproveOpen(false); setConfirmedReview(false);
      toast(r.status === "PARTIAL" ? "warn" : "ok",
        r.status === "PARTIAL"
          ? `Applied ${r.applied_count}, ${r.failed_count} failed — recoverable per item.`
          : `Approved and applied to ${r.applied_count} submission(s).`);
      loadList();
    } catch (e) { toast("err", (e as Error).message); }
    finally { setBusy(null); }
  }

  async function recover() {
    if (!proposal) return;
    setBusy("retry");
    try {
      const r = await api<SchemeProposal>(`/scheme/proposals/${encodeURIComponent(proposal.id)}/retry`,
        { method: "POST", body: JSON.stringify({ confirm: true }) });
      setProposal(r);
      toast(r.failed_count ? "warn" : "ok", `Applied ${r.applied_count}; ${r.failed_count} still failed.`);
    } catch (e) { toast("err", (e as Error).message); }
    finally { setBusy(null); }
  }

  async function doReject() {
    if (!proposal) return;
    setBusy("reject");
    try {
      setProposal(await rejectSchemeProposal(proposal.id, "Rejected by teacher"));
      toast("ok", "Proposal rejected — grades unchanged.");
      loadList();
    } catch (e) { toast("err", (e as Error).message); }
    finally { setBusy(null); }
  }

  async function decide(decision: string) {
    if (!selected) return;
    try {
      const r = await decideSchemeDiscrepancy(selected.id, decision);
      setSelected(r.discrepancy);
      toast("ok", "Decision recorded.");
      loadList();
      loadDetail(selected.id);
    } catch (e) { toast("err", (e as Error).message); }
  }

  return (
    <>
      <PageHeader title="Mark-Scheme Review" icon="alert"
        sub="Suspect a supplied mark scheme is wrong? The engine verifies independently and records the disagreement. No existing grade changes without your explicit approval.">
        <Button variant="ghost" icon="refresh" onClick={loadList}>Refresh</Button>
      </PageHeader>

      {error && <div className="notice err" style={{ marginBottom: 12 }}>{error}</div>}

      {!items ? (
        <div className="skeleton" style={{ height: 140 }} />
      ) : items.length === 0 ? (
        <Empty icon="check-circle" title="No suspected mark-scheme errors"
          hint="Ordinary corrections produce no entries here. A record only appears when the engine has a defensible mathematical reason to disagree with the mark scheme." />
      ) : (
        <Section title="Open discrepancies" hint={`${items.length}`}>
          <DataTable head={<tr><th>Assignment</th><th>Question</th><th>Type</th>
            <th>State</th><th>Awarded</th><th>Updated</th><th /></tr>}>
            {items.map((d) => (
              <tr key={d.id} onClick={() => loadDetail(d.id)} className="clickable">
                <td>{d.assignment_title || "—"}</td>
                <td>Q{d.question_number}</td>
                <td>{d.discrepancy_type.replace(/_/g, " ")}</td>
                <td><Badge tone={STATE_TONE[d.state] ?? "warn"}>
                  {STATE_LABEL[d.state] ?? d.state}</Badge></td>
                <td>{d.awarded_marks ?? 0}{d.max_marks ? ` / ${d.max_marks}` : ""}</td>
                <td className="muted small">{timeAgo(d.updated_at)}</td>
                <td><Button size="sm" variant="ghost" icon="chevron-right" /></td>
              </tr>
            ))}
          </DataTable>
        </Section>
      )}

      {selected && (
        <Section title={`Q${selected.question_number} — evidence`}
          icon="alert"
          actions={<Button variant="ghost" onClick={() => setSelected(null)}>Close</Button>}>
          <div className="notice" style={{ marginBottom: 12 }}>
            <strong>{STATE_LABEL[selected.state] ?? selected.state}.</strong>{" "}
            {selected.suspicion}
          </div>
          <DataTable head={<tr><th>Field</th><th>Value</th></tr>}>
            <tr><td>Supplied mark-scheme answer</td>
              <td>{selected.scheme_answer || "—"}</td></tr>
            <tr><td>Independently verified answer</td>
              <td>{selected.verified_answer || "—"}</td></tr>
            <tr><td>Reasoning / evidence</td>
              <td>{selected.verified_reasoning || "—"}</td></tr>
            <tr><td>Student</td>
              <td>{selected.student_name || "—"}</td></tr>
            <tr><td>Marks awarded</td>
              <td>{selected.awarded_marks ?? 0}{selected.max_marks ? ` / ${selected.max_marks}` : ""}
                {selected.justified_marks != null
                  ? ` — mathematics justifies ${selected.justified_marks}` : ""}</td></tr>
            <tr><td>Confidence</td>
              <td>{selected.confidence != null ? Math.round(selected.confidence * 100) + "%" : "—"}</td></tr>
            <tr><td>Review status</td><td>{selected.review_status.replace(/_/g, " ")}</td></tr>
          </DataTable>

          <div className="row wrap" style={{ gap: 8, marginTop: 12 }}>
            {DECISIONS.map(([value, label]) => (
              <Button key={value} size="sm" onClick={() => decide(value)}
                disabled={selected.review_status === "REVIEW_COMPLETED"}>{label}</Button>
            ))}
          </div>

          {history.length > 0 && (
            <>
              <h4 style={{ marginTop: 16 }}>Review history</h4>
              <DataTable head={<tr><th>Decision</th><th>By</th><th>AI recommendation</th><th>When</th></tr>}>
                {history.map((h, i) => (
                  <tr key={i}>
                    <td>{h.decision.replace(/_/g, " ")}</td>
                    <td>{h.decided_by || "—"}</td>
                    <td className="muted small">{h.ai_recommendation || "—"}</td>
                    <td className="muted small">{timeAgo(h.created_at)}</td>
                  </tr>
                ))}
              </DataTable>
            </>
          )}

          <div className="row wrap" style={{ gap: 8, marginTop: 16 }}>
            <Button variant="primary" icon="wand" onClick={prepare}
              loading={busy === "propose"}>
              Prepare bulk correction
            </Button>
            <span className="muted small">
              Prepares proposed changes for every affected submission. Nothing is applied.
            </span>
          </div>
        </Section>
      )}

      {proposal && (
        <Section title="Proposed bulk correction" icon="review"
          actions={<Badge tone={proposal.status === "PROPOSED" ? "warn" : "ok"}>
            {proposal.status}</Badge>}>
          <div className="notice" style={{ marginBottom: 12 }}>
            This change would affect <strong>{proposal.affected_count}</strong> submission(s);
            <strong> {proposal.changed_count}</strong> have a verified proposed mark.
            Approving updates those results and preserves the original marks for auditing.
            {proposal.classroom_publish_state &&
              <> Classroom write-back: <strong>{proposal.classroom_publish_state}</strong>.</>}
          </div>
          <DataTable head={<tr><th>Student</th><th>Original</th><th>Proposed</th>
            <th>Question mark</th><th>Status</th></tr>}>
            {proposal.items.map((it: ProposalItem) => (
              <tr key={it.id}>
                <td>{it.student_name || "—"}</td>
                <td>{it.original_earned ?? "—"}</td>
                <td>{it.proposed_earned ?? "needs re-mark"}</td>
                <td>{it.question_awarded_before ?? "—"} → {it.question_awarded_after ?? "—"}</td>
                <td>{it.status}{it.error ? ` — ${it.error}` : ""}</td>
              </tr>
            ))}
          </DataTable>

          {proposal.status === "PARTIAL" && (
            <Button variant="secondary" loading={busy === "retry"} onClick={recover}>
              Retry failed items only ({proposal.failed_count})
            </Button>
          )}
          {proposal.status === "PROPOSED" && (
            <div className="row wrap" style={{ gap: 8, marginTop: 12 }}>
              <Button variant="primary" icon="check-circle"
                onClick={() => setApproveOpen(true)}>
                Approve and apply changes
              </Button>
              <Button variant="danger" icon="x" onClick={doReject}
                loading={busy === "reject"}>Reject proposed correction</Button>
            </div>
          )}
        </Section>
      )}

      {approveOpen && proposal && (
        <Modal title="Approve bulk grade changes" icon="alert"
          onClose={() => { setApproveOpen(false); setConfirmedReview(false); }}
          footer={<>
            <Button variant="ghost" onClick={() => setApproveOpen(false)}>Cancel</Button>
            <Button variant="primary" icon="check-circle" disabled={!confirmedReview}
              loading={busy === "approve"} onClick={doApprove}>
              Approve {proposal.changed_count} change(s)
            </Button>
          </>}>
          <p>
            You are about to change <strong>{proposal.changed_count}</strong> previously
            graded submission(s) out of <strong>{proposal.affected_count}</strong> affected.
            The original marks are preserved in the correction history.
          </p>
          <label className="row" style={{ gap: 8, marginTop: 8 }}>
            <input type="checkbox" checked={confirmedReview}
              onChange={(e) => setConfirmedReview(e.target.checked)} />
            <span>I have reviewed the evidence and confirm these changes.</span>
          </label>
          <label className="row" style={{ gap: 8, marginTop: 8 }}>
            <input type="checkbox" checked={publish}
              onChange={(e) => setPublish(e.target.checked)} />
            <span>Also return the corrected grades to Google Classroom (only if authorised).</span>
          </label>
          <p className="muted small" style={{ marginTop: 8 }}>
            Opening this screen is not approval. Only the deliberate confirmation above applies changes.
          </p>
        </Modal>
      )}
    </>
  );
}
