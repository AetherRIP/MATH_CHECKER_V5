import { useState } from "react";
import { PageHeader, Tabs } from "../components";
import { LargeWorkerPanel, NormalWorkerPanel } from "../workers/panels";
import KeysPage from "./KeysPage";
import NetworkPanel from "./NetworkPanel";

type Tab = "api" | "normal" | "large" | "network";

/**
 * AI — the AI system, its two PDF workers, and the network.
 *
 * Exactly four things live here, and nothing else:
 *
 *   * API & providers  — the Gemini providers, their keys, rotation, health
 *                        and the connectivity test (KeysPage).
 *   * Normal PDF worker — the single-pass worker pool: status, worker count,
 *                        throughput, queue (configured, not simulated).
 *   * Large PDF worker  — the batched long-document worker: config, batches,
 *                        per-job progress and recovery.
 *   * Network           — the backend this window talks to, and the separate
 *                        Public Address switch.
 *
 * Job execution, history and monitoring are the Task Manager's responsibility,
 * assignments belong to the Assignments page, Classroom to its own page, and
 * results, review, usage and settings all keep their own pages. Those routes
 * were never deleted, but they are no longer hosted inside this page: that is
 * what made the AI page unusable.
 */
export default function AiPage({ onNavigate }: {
  onNavigate: (hash: string) => void;
}) {
  const [tab, setTab] = useState<Tab>("api");

  return (
    <>
      <PageHeader title="AI" icon="ai"
        sub="Providers and keys, the two PDF correction workers, and the network this installation uses." />

      <Tabs value={tab} onChange={setTab} ariaLabel="AI sections" options={[
        { value: "api", label: "API & providers", icon: "key" },
        { value: "normal", label: "Normal PDF worker", icon: "cpu" },
        { value: "large", label: "Large PDF worker", icon: "grid" },
        { value: "network", label: "Network", icon: "wifi" },
      ]} />

      {tab === "api" && <KeysPage />}
      {tab === "normal" && <NormalWorkerPanel onNavigate={onNavigate} />}
      {tab === "large" && (
        <LargeWorkerPanel onOpen={() => onNavigate("#/largepdf")} />
      )}
      {tab === "network" && <NetworkPanel onNavigate={onNavigate} />}
    </>
  );
}
