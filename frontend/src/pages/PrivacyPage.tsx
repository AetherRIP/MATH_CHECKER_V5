import { useCallback, useEffect, useState } from "react";
import { api, type Dashboard, type PrivacyInfo } from "../api";
import {
  Badge, Button, Icon, Modal, PageHeader, useToast,
} from "../components";

export default function PrivacyPage() {
  const toast = useToast();
  const [info, setInfo] = useState<PrivacyInfo | null>(null);
  const [dash, setDash] = useState<Dashboard | null>(null);
  const [confirmOpen, setConfirmOpen] = useState(false);
  const [typed, setTyped] = useState("");
  const [busy, setBusy] = useState<string | null>(null);

  const load = useCallback(() => {
    api<PrivacyInfo>("/privacy/info").then(setInfo).catch(() => {});
    api<Dashboard>("/dashboard").then(setDash).catch(() => {});
  }, []);
  useEffect(() => { load(); }, [load]);

  async function doReset() {
    setBusy("reset");
    try {
      const r = await api<{ removed: string[]; failures: string[] }>(
        "/privacy/factory-reset", { method: "POST", body: JSON.stringify({ confirm: "RESET" }) });
      setConfirmOpen(false); setTyped("");
      if (r.failures?.length) {
        // A partial reset (e.g. a locked file) must be visible, never silent.
        toast("err", `Reset finished with ${r.failures.length} item(s) not removed.`);
      } else {
        toast("ok", `Factory reset complete — removed ${r.removed.length} item(s).`);
      }
      // Reload so the app returns to a clean first-run state (setup wizard)
      // instead of showing pages backed by data that no longer exists.
      window.setTimeout(() => window.location.reload(), 700);
    } catch (e) {
      toast("err", (e as Error).message);
    } finally { setBusy(null); }
  }

  function exportDiagnostics() {
    // Sanitized JSON download produced by the backend.
    window.location.href = "/api/privacy/diagnostic-export";
    toast("info", "Diagnostic report downloaded (no personal data included)");
  }

  return (
    <>
      <PageHeader title="Privacy & Data" icon="shield"
        sub="What is stored locally, how to remove it, and how to share a safe support file.">
        <Button variant="secondary" icon="download" onClick={exportDiagnostics}>
          Export Diagnostics
        </Button>
      </PageHeader>

      {dash?.demo_mode && (
        <div className="notice info" style={{ marginBottom: 12 }}>
          <Icon name="party" size={16} />
          <span><strong>Demo Mode.</strong> Synthetic data only — Google Classroom is disabled.</span>
        </div>
      )}

      <div className="grid grid-2" style={{ alignItems: "start" }}>
        <div className="card">
          <h3><Icon name="database" size={16} style={{ color: "var(--accent)" }} />Your local data</h3>
          <p className="small muted" style={{ marginTop: 0 }}>
            Application files and your data are kept apart, so updating or reinstalling
            never removes your work.
          </p>
          {!info ? <div className="skeleton" style={{ height: 120 }} />
            : (
              <>
                {info.items.map((item) => (
                  <div className="kv" key={item.key}>
                    <span className="k">{item.label}</span>
                    <span className="v">
                      {item.present
                        ? <Badge tone="ok" icon="check">present</Badge>
                        : <Badge tone="mute">none</Badge>}
                    </span>
                  </div>
                ))}
                <div className="faint small mono" style={{ marginTop: 10 }}>
                  {info.data_directory}
                </div>
                <div className="notice info small" style={{ marginTop: 10 }}>{info.note}</div>
              </>
            )}
        </div>

        <div className="card">
          <h3><Icon name="trash" size={16} style={{ color: "var(--err)" }} />Factory Reset</h3>
          <p className="small muted" style={{ marginTop: 0 }}>
            Permanently removes your local API keys, Google connection, configuration,
            database, cache and generated reports. The application stays installed.
          </p>
          <Button variant="danger" icon="trash" onClick={() => setConfirmOpen(true)}>
            Factory Reset…
          </Button>
        </div>
      </div>

      <div className="card">
        <h3><Icon name="info" size={16} style={{ color: "var(--accent)" }} />About</h3>
        <div className="grid grid-2" style={{ gap: 8 }}>
          <div className="kv"><span className="k">Application</span>
            <span className="v">Math Checker</span></div>
          <div className="kv"><span className="k">Version</span>
            <span className="v">v{dash?.version ?? "—"}</span></div>
          <div className="kv"><span className="k">Build</span>
            <span className="v">{dash?.build_number ?? "—"}</span></div>
          <div className="kv"><span className="k">Build date</span>
            <span className="v">{dash?.build_date ?? "—"}</span></div>
          <div className="kv"><span className="k">Mode</span>
            <span className="v">{dash?.demo_mode ? "Demo (synthetic)" : dash?.mock_mode ? "Mock" : "Live"}</span></div>
          <div className="kv"><span className="k">Google Classroom</span>
            <span className="v">{dash?.classroom.connected ? "Connected" : "Not connected"}</span></div>
        </div>
      </div>

      {confirmOpen && (
        <Modal title="Factory Reset" icon="alert" onClose={() => setConfirmOpen(false)}
          footer={
            <>
              <Button variant="ghost" onClick={() => setConfirmOpen(false)}>Cancel</Button>
              <Button variant="danger" icon="trash" loading={busy === "reset"}
                disabled={typed !== "RESET"} onClick={doReset}>
                Reset Everything
              </Button>
            </>
          }>
          <div className="notice err">
            <Icon name="alert" size={16} />
            <span>
              This will permanently remove your local Math Checker data and credentials:
              API keys, Google connection, configuration, database, cache and reports.
              <br /><strong>Your application installation will remain.</strong>
            </span>
          </div>
          <p className="small muted">
            This cannot be undone. Type <strong>RESET</strong> to confirm.
          </p>
          <input className="input" value={typed} onChange={(e) => setTyped(e.target.value)}
            placeholder="RESET" aria-label="Type RESET to confirm" />
        </Modal>
      )}
    </>
  );
}
