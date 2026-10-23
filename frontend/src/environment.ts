// Which backend is this browser window talking to?
//
// Three environments exist and they must never be confused (docs/
// development-environments.md):
//
//   production desktop  -> its own local backend, real data
//   browser development -> the backend named by MC5_DEV_API, demo only if that
//                          backend says so
//   isolated test       -> a separate data folder, no Classroom or Gemini
//
// The backend is the source of truth for demo mode (`demo_mode` on /api/
// dashboard): a data folder can be copied, an environment variable can be
// inherited, but the process actually answering the request knows whether it
// is the demo process. Everything here is pure so it can be unit-tested
// without a browser.

export type EnvironmentKind = "demo" | "dev" | "live" | "offline";

export interface EnvironmentDashboard {
  demo_mode?: boolean;
  mock_mode?: boolean;
}

/** Origins are injected by vite.config.ts at build/dev time. `typeof` keeps
 *  this module importable from node (tests) where the globals are absent. */
export function devBackendOrigin(): string {
  return typeof __MC5_DEV_BACKEND__ === "string" ? __MC5_DEV_BACKEND__ : "";
}

export function isDevServer(): boolean {
  return typeof __MC5_DEV_SERVER__ === "boolean" ? __MC5_DEV_SERVER__ : false;
}

/** The origin this bundle talks to, for messages. Never includes a path. */
export function backendOriginLabel(): string {
  const configured = devBackendOrigin();
  if (configured) {
    return configured;
  }
  if (typeof window !== "undefined" && window.location) {
    return window.location.origin;
  }
  return "the Math Checker backend";
}

export function environmentKind(
  dashboard: EnvironmentDashboard | null,
  unreachable = false,
  devServer: boolean = isDevServer(),
): EnvironmentKind {
  if (dashboard) {
    return dashboard.demo_mode ? "demo" : devServer ? "dev" : "live";
  }
  return unreachable ? "offline" : devServer ? "dev" : "live";
}


export function environmentLabel(kind: EnvironmentKind): string {
  switch (kind) {
    case "demo":
      return "DEMO";
    case "dev":
      return "DEV";
    case "live":
      return "LIVE";
    case "offline":
      return "No backend";
  }
}

export function environmentDetail(kind: EnvironmentKind): string {
  switch (kind) {
    case "demo":
      return (
        "DEMO backend — synthetic students and assignments only. Nothing here is " +
        "your real class, and no change made here reaches real student data."
      );
    case "dev":
      return (
        `Development build talking to ${backendOriginLabel()}. This is a real ` +
        "backend; it is labelled DEMO when it is one."
      );
    case "live":
      return `Served by the Math Checker backend at ${backendOriginLabel()}.`;
    case "offline":
      return `No answer from ${backendOriginLabel()}. No student data is being shown.`;
  }
}

export function environmentTone(kind: EnvironmentKind): "ok" | "warn" | "info" | "err" | "mute" {
  switch (kind) {
    case "demo":
      return "warn";
    case "dev":
      return "info";
    case "live":
      return "ok";
    case "offline":
      return "err";
  }
}

/** The sentence shown when the backend cannot be reached at all: a
 *  configuration/availability error, never a page of plausible zeroes. */
export function offlineMessage(): string {
  const origin = backendOriginLabel();
  const hint = devBackendOrigin()
    ? "Start that backend (python main.py 8642) or change MC5_DEV_API in frontend/.env.local."
    : "Start Math Checker's backend, then reload this page.";
  return `Cannot reach the Math Checker backend at ${origin}. No data is being shown. ${hint}`;
}
