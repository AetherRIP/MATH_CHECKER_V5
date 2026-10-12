import { useCallback, useEffect, useState } from "react";
import { api, timeAgo, type ClassOverview } from "../api";
import {
  Badge, Button, Empty, Icon, Progress, PageHeader,
} from "../components";

export default function ClassesPage({ onNavigate }: { onNavigate: (hash: string) => void }) {
  const [classes, setClasses] = useState<ClassOverview[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [q, setQ] = useState("");

  const load = useCallback(() => {
    setError(null);
    api<{ classes: ClassOverview[] }>("/classes")
      .then((r) => setClasses(r.classes)).catch((e) => setError(e.message));
  }, []);
  useEffect(() => { load(); }, [load]);

  const filtered = (classes ?? []).filter((c) =>
    !q.trim() || c.name.toLowerCase().includes(q.trim().toLowerCase())
    || (c.section ?? "").toLowerCase().includes(q.trim().toLowerCase()));

  return (
    <>
      <PageHeader title="Classes" icon="class"
        sub="Your Google Classroom classes and local assignments, with real submission counts.">
        <input className="input" style={{ maxWidth: 240 }} placeholder="Search classes…"
          value={q} onChange={(e) => setQ(e.target.value)} aria-label="Search classes" />
        <Button variant="ghost" icon="refresh" onClick={load}>Refresh</Button>
        <Button variant="secondary" icon="sync" onClick={() => onNavigate("#/classroom")}>
          Sync Classroom
        </Button>
      </PageHeader>

      {error && <div className="notice err" style={{ marginBottom: 12 }}>{error}</div>}

      {!classes ? (
        <div className="grid grid-auto">
          {[0, 1, 2].map((i) => <div key={i} className="skeleton" style={{ height: 180 }} />)}
        </div>
      ) : filtered.length === 0 ? (
        <div className="card">
          <Empty icon="class" title={classes.length === 0 ? "No classes yet" : "No matches"}
            hint={classes.length === 0
              ? "Connect Google Classroom and sync to pull in your classes."
              : "Try a different search."}
            action={<Button variant="primary" icon="classroom"
              onClick={() => onNavigate("#/classroom")}>Connect &amp; sync Classroom</Button>} />
        </div>
      ) : (
        <div className="grid grid-auto">
          {filtered.map((c) => {
            const completion = c.submissions > 0
              ? Math.min(1, c.submitted / c.submissions) : 0;
            const corrected = c.submissions > 0
              ? Math.min(1, c.checked / c.submissions) : 0;
            return (
              <button key={c.id} className="card interactive" style={{ textAlign: "left", cursor: "pointer" }}
                onClick={() => onNavigate(`#/assignments?course=${encodeURIComponent(c.id)}`)}>
                <div className="row between" style={{ alignItems: "flex-start" }}>
                  <div className="row" style={{ gap: 10, minWidth: 0 }}>
                    <div className="status-card tone-info" style={{ padding: 0, border: "none", background: "transparent", width: "auto" }}>
                      <div className="sc-icon"><Icon name={c.local ? "database" : "class"} size={18} /></div>
                    </div>
                    <div style={{ minWidth: 0 }}>
                      <div style={{ fontWeight: 700, fontSize: "var(--fs-lg)" }} className="truncate">
                        {c.name}
                      </div>
                      <div className="faint small">{c.section ?? "—"}</div>
                    </div>
                  </div>
                  {c.local
                    ? <Badge tone="mute" icon="database">Local</Badge>
                    : c.external_assignments && c.external_assignments === c.assignments
                      ? <Badge tone="warn" icon="external">External</Badge>
                      : <Badge tone="ok" icon="classroom">Classroom</Badge>}
                </div>

                <div className="grid grid-2" style={{ gap: 8, marginTop: 14 }}>
                  <div className="stat compact" style={{ padding: 10 }}>
                    <div className="label">Students</div>
                    <div className="value" style={{ fontSize: "var(--fs-lg)" }}>{c.students}</div>
                  </div>
                  <div className="stat compact" style={{ padding: 10 }}>
                    <div className="label">Assignments</div>
                    <div className="value" style={{ fontSize: "var(--fs-lg)" }}>{c.assignments}</div>
                  </div>
                </div>

                <div style={{ marginTop: 14 }} className="stack">
                  <div>
                    <div className="row between small">
                      <span className="muted">Submitted</span>
                      <span className="mono">{c.submitted}/{c.submissions}</span>
                    </div>
                    <Progress value={completion} thin />
                  </div>
                  <div>
                    <div className="row between small">
                      <span className="muted">Checked</span>
                      <span className="mono">{c.checked}</span>
                    </div>
                    <Progress value={corrected} thin tone="ok" />
                  </div>
                </div>

                <div className="row between" style={{ marginTop: 12 }}>
                  <span className="faint small">
                    {c.missing > 0 ? `${c.missing} outstanding` : "all handed in"}
                    {c.review > 0 ? ` · ${c.review} to review` : ""}
                  </span>
                  <span className="faint small">
                    {c.average != null ? `${c.average}% avg` : "no results"}
                  </span>
                </div>
                <div className="faint small" style={{ marginTop: 6 }}>
                  Last synced {timeAgo(c.last_sync)}
                </div>
              </button>
            );
          })}
        </div>
      )}
    </>
  );
}
