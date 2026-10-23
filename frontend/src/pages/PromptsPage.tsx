import { useCallback, useEffect, useState } from "react";
import {
  devPrompts, saveDevPrompts, type DevPrompt, type DevPrompts,
} from "../api";
import { Badge, Loading, PageHeader, useToast } from "../components";

/**
 * Developer-only prompt inspection/editor.
 *
 * Every text on this page comes from the engine itself
 * (app/engine/legacy_marking.py) through GET /api/dev/prompts — it is the
 * EXACT system instruction and user prompt a correction sends to Gemini, for
 * manual AND automatic marking alike. Editing saves an override to
 * config.toml, which every process (parent, worker child, automation
 * scheduler) reads, so the whole app marks with one prompt.
 */

const TITLES: Record<string, string> = {
  marking_system: "System instruction",
  marking_user: "User prompt",
  blueprint_block: "Blueprint block (appended)",
  audit_user: "Audit second pass (legacy, off by default)",
};

type OverrideKey = "system_prompt_override" | "user_prompt_override";

const EDITABLE: Record<string, OverrideKey> = {
  marking_system: "system_prompt_override",
  marking_user: "user_prompt_override",
};

const PRE_STYLE: React.CSSProperties = {
  whiteSpace: "pre-wrap",
  maxHeight: "40vh",
  overflowY: "auto",
  background: "var(--bg)",
  border: "1px solid var(--line)",
  borderRadius: 8,
  padding: 12,
  margin: "8px 0 0",
  fontSize: 12,
  lineHeight: 1.55,
};

function CodeBlock({ text }: { text: string }) {
  return <pre className="mono" style={PRE_STYLE}>{text}</pre>;
}

function PromptCard({ p, onSaved }: { p: DevPrompt; onSaved: (d: DevPrompts) => void }) {
  const toast = useToast();
  const key = EDITABLE[p.id];
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(p.text);
  const [saving, setSaving] = useState(false);
  const [showBuiltin, setShowBuiltin] = useState(false);
  const [showBlueprint, setShowBlueprint] = useState(false);

  useEffect(() => { setDraft(p.text); setEditing(false); }, [p.text, p.edited]);

  async function save(next: string) {
    if (!key) return;
    setSaving(true);
    try {
      const payload = key === "system_prompt_override"
        ? { system_prompt_override: next }
        : { user_prompt_override: next };
      const updated = await saveDevPrompts(payload);
      onSaved(updated);
      toast("ok", next.trim()
        ? "Override saved — the next correction (manual or automatic) uses it"
        : "Built-in legacy prompt restored");
      setEditing(false);
    } catch (e) {
      toast("err", (e as Error).message);
    } finally {
      setSaving(false);
    }
  }

  async function copy() {
    try {
      await navigator.clipboard.writeText(p.text);
      toast("info", "Prompt copied");
    } catch {
      toast("err", "Could not copy to the clipboard");
    }
  }

  return (
    <div className="card" style={{ marginBottom: 14 }}>
      <div className="row between wrap" style={{ gap: 8, marginBottom: 6 }}>
        <h3 style={{ margin: 0, display: "flex", alignItems: "center", gap: 8 }}>
          {TITLES[p.id] ?? p.id}
          {p.edited && <Badge tone="warn">overridden</Badge>}
        </h3>
        <div className="row wrap" style={{ gap: 6 }}>
          {key && !editing && (
            <button className="btn sm" disabled={saving}
              onClick={() => { setDraft(p.text); setEditing(true); }}>
              Edit
            </button>
          )}
          {key && (
            <button className="btn sm ghost" disabled={saving}
              onClick={() => setShowBuiltin((v) => !v)}>
              {showBuiltin ? "Hide built-in" : "Compare built-in"}
            </button>
          )}
          <button className="btn sm ghost" onClick={copy}>Copy</button>
        </div>
      </div>

      <div className="small faint" style={{ marginBottom: 6 }}>
        {p.when} · sent via <span className="mono">{p.transport}</span>
      </div>

      {editing && key ? (
        <div>
          <textarea
            className="mono"
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            rows={16}
            style={{ width: "100%", fontSize: 12, lineHeight: 1.55, padding: 12 }}
          />
          <div className="row wrap" style={{ gap: 8, marginTop: 8 }}>
            <button className="btn sm" disabled={saving} onClick={() => save(draft)}>
              {saving ? "Saving…" : "Save override"}
            </button>
            <button className="btn sm ghost" disabled={saving}
              onClick={() => save("")}>
              Restore built-in
            </button>
            <button className="btn sm ghost" disabled={saving}
              onClick={() => setEditing(false)}>
              Cancel
            </button>
          </div>
        </div>
      ) : (
        <CodeBlock text={p.text} />
      )}

      {showBuiltin && p.builtin && (
        <div style={{ marginTop: 10 }}>
          <div className="small faint">Built-in text (what runs with no override)</div>
          <CodeBlock text={p.builtin} />
        </div>
      )}

      {p.id === "marking_user" && (
        <div style={{ marginTop: 10 }}>
          <button className="btn sm ghost" onClick={() => setShowBlueprint((v) => !v)}>
            {showBlueprint ? "Hide" : "Show"} with blueprint block appended
          </button>
          {showBlueprint && <CodeBlock text={p.text_with_blueprint ?? p.text} />}
        </div>
      )}
    </div>
  );
}

export default function PromptsPage() {
  const toast = useToast();
  const [data, setData] = useState<DevPrompts | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(() => {
    setError(null);
    devPrompts().then(setData)
      .catch((e) => setError((e as Error).message));
  }, []);
  useEffect(load, [load]);

  async function resetAll() {
    try {
      const updated = await saveDevPrompts({
        system_prompt_override: "", user_prompt_override: "",
      });
      setData(updated);
      toast("ok", "Both prompts restored to the built-in legacy text");
    } catch (e) {
      toast("err", (e as Error).message);
    }
  }

  if (error) {
    // Still a real page: a heading, what is wrong, and the way back.
    return (
      <>
        <PageHeader kicker="System · Prompts" title="Gemini Prompts" icon="ai"
          sub="Developer only — the exact texts every correction sends to Gemini" />
        <div className="notice err" role="alert">
          <span>
            <strong>Developer-only page.</strong>{" "}
            Prompts are readable from the local Developer role only.
          </span>
        </div>
      </>
    );
  }
  if (!data) {
    return (
      <>
        <PageHeader kicker="System · Prompts" title="Gemini Prompts" icon="ai"
          sub="Developer only — the exact texts every correction sends to Gemini" />
        <Loading label="Loading prompts…" />
      </>
    );
  }

  return (
    <div className="stack">
      <PageHeader
        title="Gemini Prompts"
        sub="Developer only — the exact texts every correction sends to Gemini"
        icon="ai"
      >
        <button className="btn sm ghost" onClick={load}>Refresh</button>
        {data.overrides.active && (
          <button className="btn sm" style={{ color: "var(--warn)" }}
            onClick={resetAll}>
            Restore both built-in prompts
          </button>
        )}
      </PageHeader>

      {data.overrides.active && (
        <div className="notice warn">
          <strong>An override is active.</strong>
          <div className="small" style={{ marginTop: 6 }}>
            Marking — manual and automatic alike — is using your edited text
            until you restore the built-in prompt. Grades and reports are
            unaffected by merely viewing this page.
          </div>
        </div>
      )}

      <div className="card">
        <div className="row between wrap" style={{ marginBottom: 10 }}>
          <strong>What Gemini receives</strong>
          <span className="hint">{data.engine_source}</span>
        </div>
        <div className="small faint" style={{ marginBottom: 6 }}>
          Model chain — first healthy model wins
        </div>
        <div className="row wrap" style={{ gap: 6, marginBottom: 12 }}>
          {data.models.chain.map((m) => <Badge key={m} tone="info">{m}</Badge>)}
        </div>
        <div className="small faint" style={{ marginBottom: 6 }}>
          High-score double-check — a DIFFERENT model leads the second pass
        </div>
        <div className="row wrap" style={{ gap: 6, marginBottom: 12 }}>
          {data.models.double_check_second_pass.map((m) => (
            <Badge key={m} tone="mute">{m}</Badge>
          ))}
        </div>
        <div className="small faint" style={{ marginBottom: 4 }}>Sampling</div>
        <CodeBlock text={JSON.stringify(data.sampling, null, 2)} />
      </div>

      {data.prompts.map((p) => (
        <PromptCard key={p.id} p={p} onSaved={setData} />
      ))}

      {data.response_schema && (
        <details className="card">
          <summary style={{ cursor: "pointer" }}>
            <strong>Response schema</strong>
            <span className="hint">
              {" "}— Gemini is forced into this exact JSON shape
            </span>
          </summary>
          <CodeBlock text={JSON.stringify(data.response_schema, null, 2)} />
        </details>
      )}
    </div>
  );
}
