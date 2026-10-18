import {
  ComponentType, Suspense, lazy, useCallback, useEffect, useMemo, useState,
} from "react";
import {
  api, authStatus, setCsrfToken, timeAgo,
  type AttentionFeed, type AuthStatus, type Dashboard,
} from "./api";
import {
  Badge, Drawer, Dropdown, IconButton, Icon, Loading,
  ToastProvider, useToast, type IconName,
} from "./components";
import SignIn from "./SignIn";
import { AttentionCtx, AttentionPanel } from "./attention";
import { ThemeProvider, useTheme } from "./theme";
import SearchBox from "./SearchBox";
import { TooltipLayer } from "./TooltipLayer";
import { ServerLifecycleBanner } from "./lifecycle";
import AiPage from "./pages/AiPage";
import SetupWizard from "./pages/SetupWizard";
import { storedSession } from "./distributed/api";

/**
 * Route-level code splitting.
 *
 * The shell (navigation, sign-in, theme, shared components) loads eagerly; a
 * page's code is fetched the first time that page is opened. There is no
 * behaviour change: every page is still mounted by the same route switch.
 *
 * The retry exists because the app supports in-place upgrades. A tab left
 * open across an upgrade still references the OLD hashed chunk names, which
 * the new build no longer serves; without this the teacher would see a blank
 * page after clicking a navigation item. One reload (rate-limited by
 * sessionStorage) fetches the new index and its current chunks.
 */
const CHUNK_RELOAD_KEY = "mc5.chunk-reload";

function lazyRoute<T extends ComponentType<any>>(
  load: () => Promise<{ default: T }>,
) {
  return lazy(async () => {
    try {
      return await load();
    } catch (error) {
      try {
        const last = Number(window.sessionStorage.getItem(CHUNK_RELOAD_KEY) || 0);
        if (Date.now() - last > 60_000) {
          window.sessionStorage.setItem(CHUNK_RELOAD_KEY, String(Date.now()));
          window.location.reload();
          return await new Promise<never>(() => { /* reloading */ });
        }
      } catch { /* storage unavailable: report the real error */ }
      throw error;
    }
  });
}

const DashboardPage = lazyRoute(() => import("./pages/DashboardPage"));
const ClassesPage = lazyRoute(() => import("./pages/ClassesPage"));
const AssignmentsPage = lazyRoute(() => import("./pages/AssignmentsPage"));
const ClassroomPage = lazyRoute(() => import("./pages/ClassroomPage"));
const StudentsPage = lazyRoute(() => import("./pages/StudentsPage"));
const AuditPage = lazyRoute(() => import("./pages/AuditPage"));
const CorrectionsPage = lazyRoute(() => import("./pages/CorrectionsPage"));
const AutomationPage = lazyRoute(() => import("./pages/AutomationPage"));
const LargePdfWorkerPage = lazyRoute(() => import("./pages/LargePdfWorkerPage"));
const TaskManagerPage = lazyRoute(() => import("./pages/TaskManagerPage"));
const PdfCorrectionPage = lazyRoute(() => import("./pages/PdfCorrectionPage"));
const UsagePage = lazyRoute(() => import("./pages/UsagePage"));
const ResultsPage = lazyRoute(() => import("./pages/ResultsPage"));
const ReviewPage = lazyRoute(() => import("./pages/ReviewPage"));
const ReportPage = lazyRoute(() => import("./pages/ReportPage"));
const SettingsPage = lazyRoute(() => import("./pages/SettingsPage"));
const StoragePage = lazyRoute(() => import("./pages/StoragePage"));
const PrivacyPage = lazyRoute(() => import("./pages/PrivacyPage"));
const DiagnosticsPage = lazyRoute(() => import("./pages/DiagnosticsPage"));
const PromptsPage = lazyRoute(() => import("./pages/PromptsPage"));
const DistributedPage = lazyRoute(() => import("./distributed/DistributedPage"));
const UpdatesPage = lazyRoute(() => import("./distributed/UpdatesPage"));
const SchemeReviewPage = lazyRoute(() => import("./pages/SchemeReviewPage"));

export type Route =
  | "dashboard" | "classes" | "assignments" | "students" | "reports" | "results" | "corrections" | "review" | "automation" | "largepdf" | "classroom" | "ai" | "settings" | "storage"
  | "privacy" | "diagnostics" | "prompts" | "distributed" | "audit" | "updates"
  | "tasks" | "correct" | "usage" | "scheme-review"
  // legacy aliases kept so old deep links (search, bookmarks) still resolve
  | "report" | "keys" | "workers" | "resultsPage";

interface NavItem {
  id: Route;
  icon: IconName;
  label: string;
  developerOnly?: boolean;
}

interface NavGroup {
  section: string;
  items: NavItem[];
}

/**
 * Main navigation (§6): every page has its own entry again.
 *
 * One job per entry, and one page per job:
 *   * AI            — providers/keys, the Normal and Large PDF workers, network
 *   * Task Manager  — the real jobs: correction, batched, Classroom, sync
 *   * Correct a PDF — the unified correction entry point (routes to either worker)
 *   * Assignments   — assignments, their submissions and their Classroom actions
 *   * everything else keeps the page it always had (results, review, classes,
 *     students, reports, Classroom, automation, storage, usage, privacy,
 *     settings) instead of being embedded in the AI page.
 *
 * Nothing was deleted to get here; the pages and their data are the same, they
 * are simply presented where they belong.
 */
const NAV_GROUPS: NavGroup[] = [
  { section: "Main", items: [
    { id: "dashboard", icon: "dashboard", label: "Dashboard" },
    { id: "ai", icon: "ai", label: "AI" },
    { id: "tasks", icon: "cpu", label: "Task Manager" },
    { id: "correct", icon: "wand", label: "Correct a PDF" },
    { id: "assignments", icon: "assignments", label: "Assignments" },
  ] },
  { section: "Corrections", items: [
    { id: "results", icon: "chart", label: "Results" },
    { id: "review", icon: "review", label: "Review Queue" },
    { id: "scheme-review", icon: "alert", label: "Mark-Scheme Review" },
    { id: "largepdf", icon: "grid", label: "Large AI Worker" },
    { id: "automation", icon: "activity", label: "Automatic Correction" },
  ] },
  { section: "Google Classroom", items: [
    { id: "classroom", icon: "classroom", label: "Google Classroom" },
    { id: "classes", icon: "class", label: "Classes" },
    { id: "students", icon: "students", label: "Students" },
    { id: "reports", icon: "reports", label: "Reports" },
  ] },
  { section: "Data & system", items: [
    { id: "storage", icon: "database", label: "Storage" },
    { id: "usage", icon: "trending", label: "Internet Usage" },
    { id: "privacy", icon: "shield", label: "Privacy & Data" },
    { id: "settings", icon: "settings", label: "Settings" },
  ] },
  { section: "Developer", items: [
    { id: "diagnostics", icon: "gear", label: "Diagnostics", developerOnly: true },
    { id: "prompts", icon: "sparkles", label: "Gemini Prompts", developerOnly: true },
    { id: "audit", icon: "check-circle", label: "Accuracy Audit", developerOnly: true },
    { id: "distributed", icon: "cloud", label: "Multi-School · WIP" },
    { id: "updates", icon: "download", label: "Application Updates", developerOnly: true },
  ] },
];

const ALL_NAV = NAV_GROUPS.flatMap((g) => g.items);

/**
 * Titles for routes that are reachable but hidden from the navigation.
 *
 * Without this the top bar fell back to "Dashboard" for any deep-linked page,
 * which is exactly the kind of small lie the simplification is meant to remove.
 */
const ROUTE_TITLES: Partial<Record<Route, string>> = {
  tasks: "Task Manager",
  correct: "Correct a PDF",
  usage: "Internet Usage",
  dashboard: "Dashboard",
  classes: "Classes",
  assignments: "Assignments",
  students: "Students",
  reports: "Reports",
  results: "Results",
  corrections: "Queue a correction",
  review: "Review Queue",
  "scheme-review": "Mark-Scheme Review",
  automation: "Automation",
  largepdf: "Large PDF Worker",
  classroom: "Google Classroom",
  ai: "AI",
  settings: "Settings",
  storage: "Storage",
  privacy: "Privacy & Data",
  diagnostics: "Diagnostics",
  prompts: "Gemini Prompts",
  distributed: "Multi-School",
  audit: "Accuracy Audit",
  updates: "Application Updates",
};

// Legacy route aliases → canonical destination.
const ALIASES: Record<string, Route> = {
  report: "reports",
  // "keys" and "workers" used to be pages of their own; both are AI sections on
  // the same page now (KeysPage, and the worker panels), so an old bookmark
  // still lands on the right surface.
  keys: "ai",
  workers: "ai",
  // Old links opened the results list as "resultsPage"; without it in the
  // valid set (and here) the deep link silently fell back to AI.
  resultsPage: "results",
};

const VALID_ROUTES = new Set<Route>([
  "dashboard", "classes", "assignments", "students", "reports",
  "results", "corrections", "review", "scheme-review", "automation", "largepdf", "classroom", "ai", "settings", "storage",
  "privacy", "diagnostics", "prompts", "distributed", "audit", "updates", "tasks", "correct", "usage",
  "report", "keys", "workers", "resultsPage",
]);

interface ParsedHash { route: Route; params: string[] }

function parseHash(): ParsedHash {
  const raw = window.location.hash.replace(/^#\/?/, "");
  const parts = raw.split("/").map(decodeURIComponent);
  // AI is the landing page: with three active pages there is no separate
  // dashboard to open, and an unknown hash must not silently land on a page
  // the navigation no longer offers.
  const routeRaw = parts[0] || "ai";
  if (!VALID_ROUTES.has(routeRaw as Route)) return { route: "ai", params: [] };
  const route = (ALIASES[routeRaw] ?? routeRaw) as Route;
  // Legacy 'report' deep links carried course/work ids → forward to Reports.
  return { route, params: routeRaw === route ? parts.slice(1) : parts.slice(1) };
}

const SIDEBAR_KEY = "mc5.sidebar.collapsed";

function Shell() {
  const toast = useToast();
  const { choice, resolved, setChoice, cycle } = useTheme();
  const [nav, setNav] = useState<ParsedHash>(parseHash);
  const [role, setRole] = useState<string>("teacher");
  const [mockMode, setMockMode] = useState(false);
  const [drawerOpen, setDrawerOpen] = useState(false);
  const [collapsed, setCollapsed] = useState<boolean>(() =>
    typeof window !== "undefined" && window.localStorage.getItem(SIDEBAR_KEY) === "1");
  const [attentionOpen, setAttentionOpen] = useState(false);
  const [attention, setAttention] = useState<AttentionFeed | null>(null);
  const [reviewCount, setReviewCount] = useState(0);
  const [dash, setDash] = useState<Dashboard | null>(null);
  const [needsSetup, setNeedsSetup] = useState(false);
  const [setupChecked, setSetupChecked] = useState(false);
  // Public-mode session: null = still checking, never a silent allow.
  const [auth, setAuth] = useState<AuthStatus | null>(null);
  // Bumped when the backend comes back after being away (an idle stop that was
  // followed by a page load, or a restart). The loaders below re-run at once
  // instead of waiting out their interval against a server that has changed.
  const [serverEpoch, setServerEpoch] = useState(0);

  useEffect(() => {
    const onBack = () => setServerEpoch((n) => n + 1);
    window.addEventListener("mc5:server-back", onBack);
    return () => window.removeEventListener("mc5:server-back", onBack);
  }, []);

  useEffect(() => {
    let cancelled = false;
    const check = () => authStatus()
      .then((s) => { if (!cancelled) { setCsrfToken(s.csrf); setAuth(s); } })
      .catch(() => { /* server briefly unreachable; retried by the interval */ });
    check();
    const t = setInterval(check, 30_000);
    const onRequired = () => setAuth((prev) => prev && {
      ...prev, required: true, authenticated: false, csrf: null,
    });
    window.addEventListener("mc5:auth-required", onRequired);
    return () => {
      cancelled = true;
      clearInterval(t);
      window.removeEventListener("mc5:auth-required", onRequired);
    };
  }, [serverEpoch]);

  const route = nav.route;

  useEffect(() => {
    const onHash = () => setNav(parseHash());
    window.addEventListener("hashchange", onHash);
    return () => window.removeEventListener("hashchange", onHash);
  }, []);

  useEffect(() => {
    const load = () => api<Dashboard>("/dashboard")
      .then((d) => {
        setRole(d.role); setMockMode(d.mock_mode); setDash(d);
        setNeedsSetup(Boolean(d.first_run));
      })
      .catch(() => {})
      .finally(() => setSetupChecked(true));
    load();
    // One shared dashboard loader for the shell AND the Dashboard page (§17):
    // two independent pollers of the same endpoint were two pollers too many.
    const t = setInterval(load, 15_000);
    return () => clearInterval(t);
  }, [route, serverEpoch]);

  const loadAttention = useCallback(() => {
    api<AttentionFeed>("/attention").then(setAttention).catch(() => {});
    api<{ total: number }>("/review?limit=500").then(r => setReviewCount(r.total)).catch(() => {});
  }, []);
  useEffect(() => {
    loadAttention();
    const t = setInterval(loadAttention, 30_000);
    return () => clearInterval(t);
  }, [loadAttention, route]);

  function go(r: Route) {
    if (window.location.hash !== `#/${r}`) window.location.hash = `#/${r}`;
    else setNav(parseHash());
    setDrawerOpen(false);
  }

  function navigate(hash: string) {
    if (window.location.hash !== hash) window.location.hash = hash;
    else setNav(parseHash());
    setDrawerOpen(false);
    setAttentionOpen(false);
  }

  function toggleSidebar() {
    setCollapsed((c) => {
      const next = !c;
      try { window.localStorage.setItem(SIDEBAR_KEY, next ? "1" : "0"); } catch { /* ignore */ }
      return next;
    });
  }

  // Release management is a DEVELOPER-only surface that talks to the
  // Multi-School console, so its nav item is shown when EITHER the local app
  // role is developer OR the signed-in distributed account is a Developer.
  // (The page and the backend still re-check the role — this is only about
  // not hiding a page from the person entitled to use it.)
  const distIsDeveloper = storedSession()?.role === "DEVELOPER";
  const showDeveloperItems = role === "developer" || distIsDeveloper;
  const visibleGroups = useMemo(() => NAV_GROUPS.map((g) => ({
    ...g,
    items: g.items.filter((item) => !item.developerOnly || showDeveloperItems),
  })).filter((g) => g.items.length > 0), [showDeveloperItems]);

  const current = ALL_NAV.find((n) => n.id === route)
    ?? { id: route, label: ROUTE_TITLES[route] ?? "AI" } as NavItem;
  const attentionCount = attention?.items.length ?? 0;
  const ctx = useMemo(() => ({ feed: attention, reload: loadAttention }),
    [attention, loadAttention]);

  const apiWarn = dash
    ? (dash.api_health.has_keys && dash.api_health.healthy === 0)
    : false;

  const connectionTone = dash?.classroom.connected ? "ok"
    : dash && !dash.classroom.configured ? "err" : "warn";

  if (auth === null) {
    // Still establishing whether a session is required (public mode).
    return (
      <div className="skeleton-lines" aria-busy="true" style={{ padding: 32 }}>
        <div className="skeleton" style={{ height: 36, maxWidth: 320 }} />
        <div className="skeleton" style={{ height: 160 }} />
      </div>
    );
  }

  if (auth.required && !auth.authenticated) {
    // The Multi-School page has its own bearer-token login (§59) and must be
    // reachable by Assistants on their own machines without the teacher's
    // app session. It talks only to /api/distributed/*, which the backend
    // gate exempts from the session requirement.
    if (route === "distributed") return <div className="card" style={{ margin: 24 }}>
      <h1>Multi-School — Work in Progress</h1>
      <p>This integration is preserved but is not approved for ordinary release use.
        Sign in to the local application to continue. Developer diagnostics remain available.</p>
      <a href="#/dashboard">Back to sign in</a>
    </div>;
    return (
      <SignIn status={auth}
        onSignedIn={(fresh) => { setCsrfToken(fresh.csrf); setAuth(fresh); }} />
    );
  }

  if (setupChecked && needsSetup) {
    return <SetupWizard onDone={() => setNeedsSetup(false)} />;
  }

  // Contextual top-bar sub-line: what is the teacher looking at? Drill-downs
  // get a real breadcrumb (§7): the parent is clickable, never a dead label.
  const crumbParent = ((): { label: string; hash: string; current: string } | null => {
    if (route === "assignments" && nav.params[0])
      return { label: "Assignments", hash: "#/assignments", current: "Assignment detail" };
    if (route === "students" && nav.params[0])
      return { label: "Students", hash: "#/students", current: "Student profile" };
    if (route === "results" && nav.params[0])
      return { label: "Results", hash: "#/results", current: "Correction detail" };
    if (route === "scheme-review" && nav.params[0])
      return { label: "Mark-Scheme Review", hash: "#/scheme-review", current: "Discrepancy" };
    return null;
  })();
  const contextLine = (() => {
    if (crumbParent) return crumbParent.current;
    if (dash) {
      const bits: string[] = [];
      if (dash.classroom.last_sync) bits.push(`synced ${timeAgo(dash.classroom.last_sync)}`);
      if (dash.processing > 0) bits.push(`${dash.processing} correcting`);
      if (mockMode) bits.push("mock mode");
      return bits.join(" · ");
    }
    return "";
  })();

  return (
    <AttentionCtx.Provider value={ctx}>
      <div className="app-shell">
        <a className="skip-link" href="#main-content">Skip to main content</a>
        <aside className={`sidebar ${collapsed ? "collapsed" : ""} ${drawerOpen ? "open" : ""}`}
          aria-label="Main navigation">
          <div className="brand">
            <div className="brand-badge" aria-hidden="true">
              <Icon name="sparkles" size={18} />
            </div>
            <div>
              Math Checker
              <small>
                {dash?.version ? `v${dash.version}` : "V5"}
                {dash?.build_number ? ` · b${dash.build_number}` : ""}
              </small>
            </div>
          </div>

          {visibleGroups.map((group) => (
            <div key={group.section}>
              <div className="nav-section">{group.section}</div>
              {group.items.map((item) => (
                <button
                  key={item.id}
                  className={`nav-item ${route === item.id ? "active" : ""}`}
                  onClick={() => go(item.id)}
                  aria-current={route === item.id ? "page" : undefined}
                  data-tooltip={collapsed ? item.label : undefined}
                  data-tooltip-side="right"
                >
                  <span className="icon"><Icon name={item.icon} size={18} /></span>
                  <span className="label-text">{item.label}</span>
                  {item.id === "review" && reviewCount > 0 && (
                    <span className="nav-count label-text">{reviewCount}</span>
                  )}
                </button>
              ))}
            </div>
          ))}

          <div className="spacer" />
          <button className="nav-item" onClick={toggleSidebar}
            aria-label={collapsed ? "Expand sidebar" : "Collapse sidebar"}
            data-tooltip={collapsed ? "Expand" : undefined} data-tooltip-side="right">
            <span className="icon">
              <Icon name={collapsed ? "chevron-right" : "chevron-left"} size={18} />
            </span>
            <span className="label-text">Collapse</span>
          </button>
          <div className="sidebar-foot faint small" style={{ padding: "6px 10px 2px" }}>
            {role === "developer" ? "Developer" : role === "coteacher" ? "Co-teacher" : "Teacher"} view
          </div>
        </aside>

        {drawerOpen && (
          <button className="scrim" aria-label="Close navigation"
            onClick={() => setDrawerOpen(false)} />
        )}

        <div className="main">
          <header className="topbar">
            <button className="btn ghost icon-only hamburger" aria-label="Open navigation"
              onClick={() => setDrawerOpen(true)}>
              <Icon name="menu" size={18} />
            </button>

            <div className="topbar-title">
              <span className="t">{current?.label ?? "Dashboard"}</span>
              {crumbParent ? (
                <span className="c chip-hide-sm">
                  <a className="crumb-link" href={crumbParent.hash}
                    onClick={(e) => { e.preventDefault(); navigate(crumbParent.hash); }}>
                    {crumbParent.label}
                  </a>
                  <span aria-hidden="true"> / </span>
                  {crumbParent.current}
                </span>
              ) : contextLine && <span className="c chip-hide-sm">{contextLine}</span>}
            </div>

            <div className="topbar-spacer" />
            <SearchBox onNavigate={navigate} />

            <div className="topbar-actions">
              {/* Live background-work indicator: the same data the jobs page
                  shows, always visible so work is never hidden behind
                  navigation. Opens the attention drawer. */}
              <button
                className={`activity-pill ${dash && (dash.processing > 0 || dash.pending > 0) ? "busy" : ""}`}
                onClick={() => setAttentionOpen(true)}
                data-tooltip="Background jobs — click for what needs attention"
                aria-label={dash && dash.processing > 0
                  ? `${dash.processing} corrections running`
                  : "No corrections running"}>
                <span className="led-dot" aria-hidden="true" />
                {dash && dash.processing > 0 ? `${dash.processing} correcting`
                  : dash && dash.pending > 0 ? `${dash.pending} queued`
                  : "Idle"}
              </button>
              <div className="topbar-chips chip-hide-sm">
                <Badge tone={connectionTone} icon="classroom">
                  {dash?.classroom.connected ? "Classroom"
                    : dash && !dash.classroom.configured ? "Not configured" : "Offline"}
                </Badge>
                {dash?.api_health.has_keys && (
                  <Badge tone={apiWarn ? "err" : "ok"} icon="ai">
                    {dash.api_health.healthy}/{dash.api_health.total}
                  </Badge>
                )}
              </div>

              <IconButton
                icon="alert" label="What needs my attention"
                onClick={() => setAttentionOpen(true)}
                style={attentionCount > 0 ? { color: "var(--warn)" } : undefined}
              />

              <IconButton
                icon={resolved === "dark" ? "moon" : "sun"}
                label={`Theme: ${choice} — click for ${choice === "light" ? "dark"
                  : choice === "dark" ? "system" : "light"}`}
                onClick={cycle}
              />

              <Dropdown align="right" icon="user" label=""
                >
                {(close) => (
                  <>
                    <div className="menu-label">Account</div>
                    <div style={{ padding: "0 10px 8px" }} className="small">
                      <div style={{ fontWeight: 650 }}>
                        {auth?.display_name || auth?.username || dash?.classroom.email || "Not connected"}
                      </div>
                      <div className="faint">
                        {role === "developer" ? "Developer" : role === "coteacher" ? "Co-teacher"
                          : role === "assistant" ? "Assistant" : "Teacher"}
                        {dash?.version ? ` · v${dash.version}` : ""}
                      </div>
                    </div>
                    <div className="menu-label">Theme</div>
                    {(["light", "dark", "system"] as const).map((c) => (
                      <button key={c} onClick={() => { close(); setChoice(c); }}>
                        <Icon name={c === "light" ? "sun" : c === "dark" ? "moon" : "gear"} size={15} />
                        {c === "light" ? "Light" : c === "dark" ? "Dark" : "Follow system"}
                        {choice === c && <span style={{ marginLeft: "auto" }}>✓</span>}
                      </button>
                    ))}
                    <div className="menu-sep" />
                    <button onClick={() => { close(); go("classroom"); }}>
                      <Icon name="classroom" size={15} /> Classroom connection
                    </button>
                    <button onClick={() => { close(); go("ai"); }}>
                      <Icon name="ai" size={15} /> AI keys
                    </button>
                    <button onClick={() => { close(); go("privacy"); }}>
                      <Icon name="shield" size={15} /> Privacy & data
                    </button>
                    {role === "developer" && (
                      <button onClick={() => { close(); go("diagnostics"); }}>
                        <Icon name="gear" size={15} /> Diagnostics
                      </button>
                    )}
                    <div className="menu-sep" />
                    <button onClick={async () => {
                      close();
                      try {
                        await api("/auth/logout", { method: "POST" });
                      } catch { /* cookie removal is best-effort */ }
                      setCsrfToken(null);
                      // Back to the main sign-in page: the next API call will
                      // 401 immediately because the cookie is gone.
                      authStatus().then((fresh) => {
                        setCsrfToken(fresh.csrf);
                        setAuth(fresh);
                      }).catch(() => setAuth((prev) => prev && {
                        ...prev, authenticated: false, csrf: null,
                        role: null, username: null,
                      }));
                    }}>
                      <Icon name="logout" size={15} /> Sign out
                    </button>
                    <button onClick={async () => {
                      close();
                      try {
                        await api("/classroom/disconnect", { method: "POST" });
                        toast("info", "Google Classroom disconnected");
                      } catch (e) { toast("err", (e as Error).message); }
                    }} disabled={!dash?.classroom.connected}>
                      <Icon name="logout" size={15} /> Disconnect Classroom
                    </button>
                  </>
                )}
              </Dropdown>
            </div>
          </header>

          <div className="main-scroll" id="main-content" tabIndex={-1}>
            <div className="main-inner">
              {dash?.demo_mode && (
                <div className="notice info" style={{ marginBottom: 14 }}>
                  <Icon name="party" size={16} />
                  <span>
                    <strong>Demo Mode</strong> — synthetic students and assignments only.
                    Google Classroom is disabled in this session.
                  </span>
                </div>
              )}
              {/* Page code is fetched on first visit; the shell never blanks. */}
              <Suspense fallback={<Loading label="Loading page…" />}>
              {route === "dashboard" && <DashboardPage onNavigate={go} data={dash} />}
              {route === "classes" && (
                <ClassesPage onNavigate={navigate} />
              )}
              {route === "assignments" && (
                <AssignmentsPage key={`a:${nav.params.join("/")}`}
                  assignmentId={nav.params[0] ?? null} onNavigate={navigate} />
              )}
              {route === "students" && (
                <StudentsPage key={`s:${nav.params[0] ?? ""}`}
                  studentId={nav.params[0] ?? null} onNavigate={navigate} />
              )}
              {route === "reports" && (
                <ReportPage key={`r:${nav.params.join("/")}`}
                  deepLink={{ courseId: nav.params[0], workId: nav.params[1] }} />
              )}
              {route === "results" && (
                <ResultsPage key={`x:${nav.params[0] ?? ""}`}
                  resultId={nav.params[0] ?? null} onNavigate={navigate} />
              )}
              {route === "corrections" && <CorrectionsPage />}
              {route === "tasks" && <TaskManagerPage onNavigate={navigate} />}
              {route === "correct" && (
                <PdfCorrectionPage onOpenResult={(id) => navigate(`#/results/${id}`)} />
              )}
              {route === "usage" && <UsagePage />}
              {route === "automation" && <AutomationPage />}
              {route === "largepdf" && <LargePdfWorkerPage onNavigate={navigate} />}
              {route === "review" && <ReviewPage onNavigate={navigate} />}
              {route === "scheme-review" && (
                <SchemeReviewPage
                  key={`sr:${nav.params[0] ?? ""}`}
                  discrepancyId={nav.params[0]} />
              )}
              {route === "classroom" && (
                <ClassroomPage key={`c:${nav.params.join("/")}`}
                  deepLink={{ courseId: nav.params[0], workId: nav.params[1] }} />
              )}
              {route === "ai" && <AiPage onNavigate={navigate} />}
              {route === "settings" && <SettingsPage />}
              {route === "storage" && <StoragePage />}
              {route === "privacy" && <PrivacyPage />}
              {route === "diagnostics" && <DiagnosticsPage />}
              {route === "prompts" && <PromptsPage />}
              {route === "distributed" && (showDeveloperItems ? <DistributedPage /> :
                <div className="card"><h2>Multi-School — Work in Progress</h2>
                  <p>The account, school, class and synchronization implementation is preserved.
                    Cross-school use has not completed release verification. Unfinished controls
                    are unavailable here; developers retain diagnostics access.</p></div>)}
              {route === "updates" && <UpdatesPage />}
              {route === "audit" && <AuditPage />}
              </Suspense>
            </div>
          </div>
        </div>
      </div>

      {attentionOpen && (
        <Drawer title="What needs my attention" onClose={() => setAttentionOpen(false)}>
          <AttentionPanel onNavigate={navigate} />
        </Drawer>
      )}
    </AttentionCtx.Provider>
  );
}

export default function App() {
  return (
    <ThemeProvider>
      <ToastProvider>
        <Shell />
        {/* One heartbeat for the whole app, above the sign-in gate: the login
            screen is a browser session too, so the app must stay up while a
            teacher is sitting on it. The banner is the only thing it renders,
            and only when the backend is genuinely unreachable. */}
        <ServerLifecycleBanner />
        <TooltipLayer />
      </ToastProvider>
    </ThemeProvider>
  );
}
