import { useEffect, useState } from "react";
import { api, type SelfCheck, type SetupStatus } from "../api";
import { Badge, useToast } from "../components";

/**
 * First-run setup wizard (§3/§41).
 *
 * A teacher who has never seen the code should reach a working app without
 * editing any file. This walks through the essentials — who you are, your AI
 * keys, Google Classroom, and how the app behaves — and never blocks: every
 * step can be skipped and completed later from Settings.
 */
export default function SetupWizard({ onDone }: { onDone: () => void }) {
  const toast = useToast();
  const [status, setStatus] = useState<SetupStatus | null>(null);
  const [step, setStep] = useState(0);
  const [teacherName, setTeacherName] = useState("");
  const [schoolName, setSchoolName] = useState("");
  const [keys, setKeys] = useState("");
  const [savingKeys, setSavingKeys] = useState(false);
  const [accessMode, setAccessMode] = useState<"local" | "lan">("local");
  const [retention, setRetention] = useState("until_complete");
  const [sessionLabel, setSessionLabel] = useState("");
  const [selfCheck, setSelfCheck] = useState<SelfCheck | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    api<SetupStatus>("/setup/status").then((s) => {
      setStatus(s);
      setTeacherName(s.teacher.teacher_name || "");
      setSchoolName(s.teacher.school_name || "");
    }).catch(() => {});
  }, []);

  async function refresh() {
    try { setStatus(await api<SetupStatus>("/setup/status")); } catch { /* ignore */ }
  }

  async function saveKeys() {
    const list = keys.split(/\r?\n|,/).map((k) => k.trim()).filter(Boolean);
    if (!list.length) { toast("warn", "Paste at least one key, or skip this step."); return; }
    setSavingKeys(true);
    try {
      const res = await api<{ added: number; activated: number }>(
        "/settings/gemini-keys", { method: "POST", body: JSON.stringify({ keys: list }) });
      toast("ok", `${res.activated} key(s) verified and added.`);
      setKeys("");
      await refresh();
    } catch (e) {
      toast("err", (e as Error).message);
    } finally {
      setSavingKeys(false);
    }
  }

  async function connectClassroom() {
    setBusy(true);
    try {
      await api("/classroom/connect", { method: "POST" });
      toast("ok", "Google Classroom connected.");
      await refresh();
    } catch (e) {
      toast("err", (e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  async function runSelfCheck() {
    setBusy(true);
    try {
      setSelfCheck(await api<SelfCheck>("/diagnostics/selfcheck"));
    } catch (e) {
      toast("err", (e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  async function finish() {
    setBusy(true);
    try {
      await api<SetupStatus>("/setup/complete", {
        method: "POST",
        body: JSON.stringify({
          teacher_name: teacherName,
          school_name: schoolName,
          network: { access_mode: accessMode },
          retention: { keep_submissions: retention },
          grades: { session_label: sessionLabel },
        }),
      });
      toast("ok", "Setup complete — welcome to Math Checker.");
      onDone();
    } catch (e) {
      toast("err", (e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  if (!status) {
    return <div className="skeleton" style={{ height: 320, margin: 40 }} />;
  }

  const steps = ["Welcome", "Your details", "AI keys", "Classroom", "Behaviour", "Finish"];
  const dot = (ok: boolean) => ok
    ? <Badge tone="ok">Ready</Badge>
    : <Badge tone="warn">Not set</Badge>;

  return (
    <div style={{ maxWidth: 760, margin: "0 auto", padding: "32px 20px" }}>
      <div className="brand" style={{ marginBottom: 8 }}>
        <div className="brand-badge">∑</div>
        <div>{status.app_name}<small>First-time setup · v{status.version}</small></div>
      </div>

      <div className="row wrap" style={{ gap: 8, margin: "14px 0 22px" }}>
        {steps.map((label, i) => (
          <button key={label}
            className={`btn sm ${i === step ? "primary" : i < step ? "" : "ghost"}`}
            onClick={() => setStep(i)}>{i + 1}. {label}</button>
        ))}
      </div>

      <div className="card">
        {step === 0 && (
          <>
            <h3>Welcome</h3>
            <p>
              Math Checker marks Edexcel IGCSE maths homework using your own
              Gemini API keys, and can pull submissions straight from Google
              Classroom. This short setup gets everything connected. You can
              change any of it later in Settings, and you never need to edit
              any files.
            </p>
            <div className="row wrap" style={{ gap: 10 }}>
              <span>{dot(status.checks.teacher_profile)} Teacher details</span>
              <span>{dot(status.checks.ai_keys)} AI keys</span>
              <span>{dot(status.checks.classroom_oauth_client)} Google client</span>
            </div>
          </>
        )}

        {step === 1 && (
          <>
            <h3>Your details</h3>
            <p className="faint small">
              Used on reports so they are clearly yours. Nothing here is shared.
            </p>
            <label className="field">
              <span>Teacher name</span>
              <input value={teacherName} placeholder="e.g. Ms A. Smith"
                onChange={(e) => setTeacherName(e.target.value)} />
            </label>
            <label className="field">
              <span>School / institution (optional)</span>
              <input value={schoolName} placeholder="e.g. Example Academy"
                onChange={(e) => setSchoolName(e.target.value)} />
            </label>
          </>
        )}

        {step === 2 && (
          <>
            <h3>AI keys</h3>
            <p>
              Paste one or more Google Gemini API keys, one per line. Each key
              is tested with a tiny probe before it is used — never with a
              student's work. Add as many as you like; the app rotates between
              them automatically.
            </p>
            {status.checks.ai_keys
              ? <div className="notice ok">{status.checks.ai_key_count} key(s) configured.</div>
              : <div className="notice warn">No keys configured yet.</div>}
            <label className="field">
              <span>New API keys</span>
              <textarea rows={3} value={keys} placeholder="AIza..."
                onChange={(e) => setKeys(e.target.value)} />
            </label>
            <button className="btn primary" disabled={savingKeys} onClick={saveKeys}>
              {savingKeys ? "Testing…" : "Add and test keys"}
            </button>
          </>
        )}

        {step === 3 && (
          <>
            <h3>Google Classroom</h3>
            <p>
              Connect the Google account you teach with. The app will ask for
              read access to your classes and coursework, and permission to
              manage coursework it creates. You choose classes and assignments
              later — nothing is imported automatically.
            </p>
            {!status.checks.classroom_oauth_client && (
              <div className="notice warn">
                The Google OAuth client file is not installed yet. Ask whoever
                set the app up to add it, then come back to this step.
              </div>
            )}
            {status.checks.classroom_connected
              ? <div className="notice ok">Connected to Google Classroom.</div>
              : <button className="btn primary" disabled={busy || !status.checks.classroom_oauth_client}
                  onClick={connectClassroom}>
                  {busy ? "Waiting for browser…" : "Connect Google Classroom"}
                </button>}
          </>
        )}

        {step === 4 && (
          <>
            <h3>How should the app behave?</h3>
            <label className="field">
              <span>Who can reach the app?</span>
              <select value={accessMode} onChange={(e) => setAccessMode(e.target.value as "local" | "lan")}>
                <option value="local">This computer only (recommended)</option>
                <option value="lan">Other devices on my network</option>
              </select>
            </label>
            <p className="faint small">
              Public internet access is deliberately not offered — it would need
              authentication and encryption this tool does not provide.
            </p>
            <label className="field">
              <span>Keep original student files</span>
              <select value={retention} onChange={(e) => setRetention(e.target.value)}>
                <option value="never">Never keep (delete after marking)</option>
                <option value="until_complete">Until the correction finishes (recommended)</option>
                <option value="days">Keep for a set number of days</option>
                <option value="always">Always keep</option>
              </select>
            </label>
            <label className="field">
              <span>Grade boundary session (optional)</span>
              <input value={sessionLabel} placeholder="e.g. June 2026"
                onChange={(e) => setSessionLabel(e.target.value)} />
            </label>
            <p className="faint small" style={{ marginBottom: 0 }}>
              Exact grade boundaries can be entered later in Settings. Until
              then, sensible default percentage bands are used.
            </p>
          </>
        )}

        {step === 5 && (
          <>
            <h3>Ready to check</h3>
            <p>Run a quick health check, then finish setup.</p>
            <button className="btn" disabled={busy} onClick={runSelfCheck}>
              {busy ? "Checking…" : "Run health check"}
            </button>
            {selfCheck && (
              <div className="stack" style={{ marginTop: 12 }}>
                {selfCheck.checks.map((c) => (
                  <div key={c.name} className="row between wrap"
                    style={{ gap: 8, padding: "6px 0", borderBottom: "1px solid var(--line)" }}>
                    <div>
                      <strong>{c.name}</strong>
                      <div className="faint small">{c.detail}</div>
                      {c.fix && <div className="small" style={{ color: "var(--warn)" }}>{c.fix}</div>}
                    </div>
                    <Badge tone={c.status === "pass" ? "ok" : c.status === "warn" ? "warn" : "err"}>
                      {c.status.toUpperCase()}
                    </Badge>
                  </div>
                ))}
              </div>
            )}
          </>
        )}

        <div className="row between" style={{ marginTop: 20 }}>
          <button className="btn ghost" disabled={step === 0} onClick={() => setStep(step - 1)}>
            Back
          </button>
          {step < steps.length - 1
            ? <button className="btn primary" onClick={() => setStep(step + 1)}>Next</button>
            : <button className="btn primary" disabled={busy} onClick={finish}>
                {busy ? "Saving…" : "Finish setup"}
              </button>}
        </div>
      </div>

      <div className="row" style={{ marginTop: 12 }}>
        <button className="btn ghost sm" onClick={finish} disabled={busy}>
          Skip setup — I'll configure later
        </button>
      </div>
    </div>
  );
}
