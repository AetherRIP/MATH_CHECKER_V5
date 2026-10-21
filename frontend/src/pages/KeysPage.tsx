import { useEffect, useRef, useState } from "react";
import { api, timeAgo, type ApiKeyStatus, type KeyInfo } from "../api";
import {
  Badge, DataTable, Empty, Modal, PageHeader, Progress, Stat, useToast,
} from "../components";

interface StageEvent {
  type: string; index: number; fingerprint: string; stage?: string; message?: string;
  status?: string; auth_ok?: boolean; generate_ok?: boolean; response_time_ms?: number | null;
  error_kind?: string | null; error_message?: string | null;
  summary?: { total: number; tested: number; passed: number; failed: number; skipped: number; elapsed_s?: number };
}

interface TestLine { index: number; fingerprint: string; message: string; done: boolean }

interface SaveResult {
  fingerprint: string; duplicate?: boolean; accepted: boolean;
  message?: string; test?: {
    status: string; auth_ok: boolean | null; generate_ok: boolean | null;
    response_time_ms: number | null; error_kind: string | null; message: string;
  };
}

function KeyStatusBadge({ status }: { status: KeyInfo["status"] }) {
  const meta: Record<KeyInfo["status"], { tone: "ok" | "warn" | "err" | "mute"; icon: string }> = {
    HEALTHY: { tone: "ok", icon: "✓" },
    COOLDOWN: { tone: "warn", icon: "⏳" },
    INVALID: { tone: "err", icon: "✗" },
    DISABLED: { tone: "mute", icon: "⏸" },
  };
  const m = meta[status] ?? { tone: "mute" as const, icon: "?" };
  return <Badge tone={m.tone}>{m.icon} {status}</Badge>;
}

export default function KeysPage({ embedded = false }: { embedded?: boolean } = {}) {
  const toast = useToast();
  const [status, setStatus] = useState<ApiKeyStatus | null>(null);
  const [keysInput, setKeysInput] = useState("");
  const [showAdd, setShowAdd] = useState(false);
  const [testing, setTesting] = useState(false);
  const [lines, setLines] = useState<TestLine[]>([]);
  const [summary, setSummary] = useState<StageEvent["summary"] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [saved, setSaved] = useState<string | null>(null);
  const [saveResults, setSaveResults] = useState<SaveResult[] | null>(null);
  const [saving, setSaving] = useState(false);
  const [testingKey, setTestingKey] = useState<string | null>(null);
  const [detail, setDetail] = useState<KeyInfo | null>(null);
  const sourceRef = useRef<EventSource | null>(null);
  const [startedAt, setStartedAt] = useState<number | null>(null);
  const [elapsedSec, setElapsedSec] = useState(0);

  const load = () => api<ApiKeyStatus>("/keys").then(setStatus).catch(() => {});
  useEffect(() => { load(); }, []);
  useEffect(() => () => { sourceRef.current?.close(); }, []);

  // elapsed timer while testing (real time, not animation)
  useEffect(() => {
    if (!testing) return;
    const t = setInterval(() => {
      if (startedAt) setElapsedSec(Math.round(Date.now() / 1000 - startedAt));
    }, 1000);
    return () => clearInterval(t);
  }, [testing, startedAt]);

  async function saveKeys() {
    setError(null); setSaved(null); setSaveResults(null);
    const keys = keysInput.split("\n").map((k) => k.trim()).filter(Boolean);
    if (keys.length === 0) {
      setError("Paste at least one API key.");
      return;
    }
    setSaving(true);
    try {
      const res = await api<{
        count: number; added: number; activated: number; skipped_blank: number;
        results: SaveResult[];
      }>("/settings/gemini-keys", { method: "POST", body: JSON.stringify({ keys }) });
      setSaveResults(res.results);
      setSaved(`${res.count} key${res.count === 1 ? "" : "s"} in rotation — ${res.activated} newly activated.`);
      toast("ok", `${res.added} key(s) added, ${res.activated} activated`);
      setKeysInput(""); load();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setSaving(false);
    }
  }

  async function testKey(fp: string) {
    setTestingKey(fp);
    try {
      await api(`/keys/${fp}/test`, { method: "POST" });
      toast("ok", "Key test complete");
      load();
    } catch (e) {
      toast("err", (e as Error).message);
    } finally {
      setTestingKey(null);
    }
  }

  async function removeKey(fp: string) {
    try {
      await api(`/keys/${fp}/remove`, { method: "POST" });
      toast("info", "Key removed from rotation (stats kept)");
      load();
    } catch (e) {
      toast("err", (e as Error).message);
    }
  }

  async function toggleDisable(k: KeyInfo) {
    const path = k.status === "DISABLED" ? "re-enable" : "disable";
    await api(`/keys/${k.fingerprint}/${path}`, { method: "POST" });
    load();
  }

  function testAll() {
    setTesting(true); setLines([]); setSummary(null); setError(null);
    setStartedAt(Date.now() / 1000); setElapsedSec(0);
    const source = new EventSource("/api/keys/test/stream");
    sourceRef.current = source;
    source.onmessage = (event) => {
      const payload = JSON.parse(event.data) as StageEvent;
      if (payload.type === "start") return;
      if (payload.type === "key_stage") {
        const { index, fingerprint: fp, message } = payload;
        setLines((prev) => {
          const last = prev[prev.length - 1];
          if (last && last.index === index && !last.done) {
            return [...prev.slice(0, -1), { index, fingerprint: fp!, message: message!, done: false }];
          }
          return [...prev, { index, fingerprint: fp!, message: message!, done: false }];
        });
        return;
      }
      if (payload.type === "key_result") {
        const { index, fingerprint: fp, status: keyStatus, response_time_ms, error_message } = payload;
        setLines((prev) => [
          ...prev.map((l) => (l.index === index && !l.done ? { ...l, done: true } : l)),
          {
            index, fingerprint: fp!, done: true,
            message: `Status: ${keyStatus}`
              + (response_time_ms ? ` · ${response_time_ms} ms` : "")
              + (error_message ? ` · ${error_message}` : ""),
          },
        ]);
        if (payload.summary) setSummary(payload.summary);
        load();
        return;
      }
      if (payload.type === "done") {
        if (payload.summary) setSummary(payload.summary);
        setTesting(false);
        source.close();
        load();
        toast("ok", `Key test finished — ${payload.summary?.passed ?? 0} healthy of ${payload.summary?.total ?? 0}`);
      }
    };
    source.onerror = () => { setTesting(false); source.close(); };
  }

  const hasKeys = status?.has_keys;

  const actions = (
    <>
      <button className="btn sm" onClick={() => setShowAdd((v) => !v)}>{showAdd ? "Close" : "Add keys"}</button>
      <button className="btn primary sm" onClick={testAll} disabled={testing || !hasKeys}>
        {testing ? `Testing ${summary?.tested ?? 0}/${summary?.total ?? "…"}…` : "Test all keys"}
      </button>
    </>
  );

  return (
    <>
      {/* Inside the AI page this is a section, not a second page: one <h1>
          per screen, and the tab already names it. */}
      {embedded ? (
        <div className="row between wrap" style={{ marginBottom: "var(--s4)", gap: "var(--s3)" }}>
          <p className="page-sub" style={{ margin: 0 }}>
            Gemini keys are stored only on this machine and are never displayed
            again after saving.
          </p>
          <div className="row wrap" style={{ gap: 6 }}>{actions}</div>
        </div>
      ) : (
        <PageHeader title="API Keys"
          sub="Gemini keys are stored only on this machine and are never displayed again after saving.">
          {actions}
        </PageHeader>
      )}

      {error && <div className="notice err" style={{ marginBottom: 12 }}>{error}</div>}
      {saved && <div className="notice ok" style={{ marginBottom: 12 }}>{saved}</div>}

      {/* ---- stats ---- */}
      {status && hasKeys && (
        <div className="grid grid-4" style={{ marginBottom: 14 }}>
          <Stat label="Total keys" value={status.total} />
          <Stat label="Healthy" value={status.healthy}
            tone={status.healthy > 0 ? "ok" : "err"} />
          <Stat label="Cooling down" value={status.cooldown}
            tone={status.cooldown > 0 ? "warn" : undefined} />
          <Stat label="Invalid" value={status.invalid}
            tone={status.invalid > 0 ? "err" : undefined} />
        </div>
      )}

      {/* ---- save & test results ---- */}
      {saveResults && saveResults.length > 0 && (
        <div className="card" style={{ marginBottom: 14 }}>
          <h3>Save &amp; test results</h3>
          {saveResults.map((r) => (
            <div key={r.fingerprint} className="row wrap" style={{ alignItems: "flex-start", gap: 8, marginBottom: 8 }}>
              {r.duplicate ? (
                <>
                  <Badge tone="mute">DUPLICATE</Badge>
                  <span className="small">{r.message}</span>
                </>
              ) : r.accepted ? (
                <>
                  <Badge tone="ok">✓ ADDED · {r.test?.status}</Badge>
                  <span className="small">
                    {r.test?.response_time_ms != null ? `${(r.test.response_time_ms / 1000).toFixed(1)}s` : "—"}
                    {r.test?.message ? ` · ${r.test.message}` : ""}
                  </span>
                </>
              ) : (
                <>
                  <Badge tone="err">✗ NOT ACTIVATED</Badge>
                  <span className="small">
                    {r.test?.message ?? r.message ?? "Validation failed."} The key was stored but kept out of rotation.
                  </span>
                </>
              )}
              <span className="faint small mono">{r.fingerprint}</span>
            </div>
          ))}
        </div>
      )}

      {/* ---- add keys ---- */}
      {showAdd && (
        <div className="card" style={{ marginBottom: 14 }}>
          <h3>Add Gemini API keys
            <span className="hint">one per line — new keys join the ones already saved</span>
          </h3>
          <textarea className="input" rows={4} value={keysInput}
            placeholder={"Paste your API key here\n(one key per line — anything you paste is treated as an opaque credential)"}
            onChange={(e) => setKeysInput(e.target.value)} />
          <div className="row wrap" style={{ marginTop: 10 }}>
            <button className="btn primary sm" onClick={saveKeys} disabled={saving}>
              {saving ? "Saving & testing…" : "Save & test"}
            </button>
            <span className="faint small">
              Keys are tested before activation; invalid keys are stored but not enabled. Stored in data/secrets.toml (never displayed again).
            </span>
          </div>
        </div>
      )}

      {!status || !hasKeys ? (
        <div className="card">
          <Empty icon="🔑" title="No keys configured"
            hint="Add at least one Gemini API key to start correcting." />
        </div>
      ) : (
        <>
          <div className="card">
            <h3>Key health
              <span className="hint">rotation balances load; failing keys are benched automatically</span>
            </h3>
            <DataTable head={<tr>
              <th>Key</th><th>Status</th><th className="num">Req</th><th className="num">OK</th>
              <th className="num">Fail</th><th className="num">429s</th>
              <th>Last used</th><th>Cooldown</th><th></th>
            </tr>}>
              {status.keys.map((k) => (
                <tr key={k.fingerprint}>
                  <td data-label="Key">
                    <button className="linklike" onClick={() => setDetail(k)}
                      title="Show key details">
                      {k.label ?? k.fingerprint}
                    </button>
                  </td>
                  <td data-label="Status"><KeyStatusBadge status={k.status} /></td>
                  <td data-label="Requests" className="num">{k.requests}</td>
                  <td data-label="OK" className="num">{k.successes}</td>
                  <td data-label="Fail" className="num">{k.failures}</td>
                  <td data-label="429s" className="num">{k.rate_limit_events}</td>
                  <td data-label="Last used" className="muted small">{timeAgo(k.last_used)}</td>
                  <td data-label="Cooldown" className="muted small">
                    {k.cooldown_remaining > 0 ? `${k.cooldown_remaining}s` : "—"}
                  </td>
                  <td data-label="Actions">
                    <div className="row wrap" style={{ gap: 6 }}>
                      {k.status === "INVALID" || k.status === "DISABLED" ? (
                        <button className="btn sm ghost" onClick={() => toggleDisable(k)}>Re-enable</button>
                      ) : (
                        <button className="btn sm ghost" onClick={() => toggleDisable(k)}>Disable</button>
                      )}
                      <button className="btn sm ghost" onClick={() => testKey(k.fingerprint)}
                        disabled={testingKey === k.fingerprint}>
                        {testingKey === k.fingerprint ? "…" : "Test"}
                      </button>
                      <button className="btn sm ghost" onClick={() => removeKey(k.fingerprint)}>Remove</button>
                    </div>
                  </td>
                </tr>
              ))}
            </DataTable>
          </div>

          {/* ---- test-all panel ---- */}
          {(testing || lines.length > 0) && (
            <div className="card">
              <div className="row between wrap">
                <h3 style={{ margin: 0 }}>
                  Testing API keys {testing && <span className="hint">running…</span>}
                </h3>
                {summary && (
                  <div className="row wrap small">
                    <span className="badge info">{summary.tested}/{summary.total} tested</span>
                    <span className="badge ok">{summary.passed} healthy</span>
                    <span className="badge err">{summary.failed} failed</span>
                    {elapsedSec > 0 && <span className="faint mono">{elapsedSec}s</span>}
                  </div>
                )}
              </div>
              {testing && (
                <Progress
                  value={summary && summary.total > 0 ? summary.tested / summary.total : 0.04}
                  thin />
              )}
              <div className="stack" style={{ marginTop: 12 }}>
                {lines.filter((l) => l.done).map((line, i) => {
                  const tone = line.message.includes("HEALTHY")
                    ? "ok" : line.message.includes("COOLDOWN") ? "warn"
                    : line.message.includes("INVALID") ? "err" : "mute";
                  return (
                    <div key={i} className="row between" style={{ padding: "4px 0" }}>
                      <span className="small">
                        <span className="faint mono">Key {line.index}</span>{" "}
                        <span className="mono faint">{line.fingerprint}</span>
                      </span>
                      <Badge tone={tone as "ok" | "warn" | "err" | "mute"}>{line.message.replace("Status: ", "")}</Badge>
                    </div>
                  );
                })}
                {testing && (
                  <div className="small faint">
                    Testing key {summary ? summary.tested + 1 : 1} of {summary?.total ?? "…"}…
                  </div>
                )}
              </div>
            </div>
          )}
        </>
      )}

      {/* ---- key detail modal ---- */}
      {detail && (
        <Modal title={detail.label ?? "Key details"} onClose={() => setDetail(null)}>
          <div className="stack">
            <div className="row between">
              <span className="muted">Fingerprint</span>
              <span className="mono">{detail.fingerprint}</span>
            </div>
            <div className="row between">
              <span className="muted">Status</span>
              <KeyStatusBadge status={detail.status} />
            </div>
            <div className="row between"><span className="muted">Requests</span><span>{detail.requests}</span></div>
            <div className="row between"><span className="muted">Successes</span><span>{detail.successes}</span></div>
            <div className="row between"><span className="muted">Failures</span><span>{detail.failures}</span></div>
            <div className="row between"><span className="muted">Rate-limit events</span><span>{detail.rate_limit_events}</span></div>
            <div className="row between"><span className="muted">Quota errors</span><span>{detail.quota_errors}</span></div>
            <div className="row between"><span className="muted">Auth errors</span><span>{detail.auth_errors}</span></div>
            <div className="row between"><span className="muted">Timeouts</span><span>{detail.timeout_errors}</span></div>
            <div className="row between"><span className="muted">Last used</span><span>{timeAgo(detail.last_used)}</span></div>
            {detail.last_error && (
              <div className="notice err small"><strong>Last error:</strong> {detail.last_error}</div>
            )}
            <p className="faint small" style={{ margin: 0 }}>
              The secret itself is never stored in or shown by the interface — only this fingerprint.
            </p>
          </div>
        </Modal>
      )}
    </>
  );
}
