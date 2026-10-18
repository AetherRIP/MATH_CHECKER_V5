/**
 * Minimal Chrome DevTools Protocol driver for layout regression tests.
 *
 * Why this exists: overflow is a *rendered* property — jsdom cannot measure
 * it. The project has no browser test dependency, so this drives the local
 * Chrome/Edge headless over CDP using Node's built-in `WebSocket` (Node 22+).
 * Nothing is installed and no Playwright/Puppeteer download is needed.
 *
 * The tests that use it skip (with a reason) when no browser binary or no app
 * server is available; `CHROME_PATH` and `WORKSPACE_LAYOUT_URL` override the
 * discovery below.
 */
import { spawn, type ChildProcess } from "node:child_process";
import { existsSync } from "node:fs";
import { mkdtemp, readFile, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join, delimiter } from "node:path";

const sleep = (ms: number) => new Promise<void>((resolve) => setTimeout(resolve, ms));

/** First existing Chrome/Chromium/Edge binary, or null. */
export function chromiumPath(): string | null {
  const override = process.env.CHROME_PATH;
  if (override) return existsSync(override) ? override : null;

  const candidates: string[] = [];
  if (process.platform === "win32") {
    const pf = process.env.ProgramFiles ?? "C:\\Program Files";
    const pf86 = process.env["ProgramFiles(x86)"] ?? "C:\\Program Files (x86)";
    const local = process.env.LOCALAPPDATA ?? "";
    candidates.push(
      join(pf, "Google", "Chrome", "Application", "chrome.exe"),
      join(pf86, "Google", "Chrome", "Application", "chrome.exe"),
      join(local, "Google", "Chrome", "Application", "chrome.exe"),
      join(pf86, "Microsoft", "Edge", "Application", "msedge.exe"),
      join(pf, "Microsoft", "Edge", "Application", "msedge.exe"),
    );
  } else if (process.platform === "darwin") {
    candidates.push(
      "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
      "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
      "/Applications/Chromium.app/Contents/MacOS/Chromium",
    );
  } else {
    const names = ["google-chrome", "google-chrome-stable", "chromium", "chromium-browser", "microsoft-edge"];
    for (const dir of (process.env.PATH ?? "").split(delimiter)) {
      if (!dir) continue;
      for (const name of names) candidates.push(join(dir, name));
    }
  }
  return candidates.find((p) => p && existsSync(p)) ?? null;
}

/** True when the app answers its health endpoint (any non-5xx response). */
export async function appReachable(baseUrl: string, timeoutMs = 1500, attempts = 3): Promise<boolean> {
  // Retried on purpose: the live-UI suites skip when this probe fails, and a
  // single 1.5s request during a busy test run (a build or several browsers on
  // the same machine) timed out often enough to report running checks as
  // "not run". A genuinely stopped app still fails every attempt.
  for (let attempt = 1; attempt <= attempts; attempt++) {
    try {
      const res = await fetch(`${baseUrl.replace(/\/$/, "")}/api/health`, {
        signal: AbortSignal.timeout(timeoutMs),
      });
      return res.status < 500;
    } catch {
      if (attempt < attempts) await new Promise((r) => setTimeout(r, 250 * attempt));
    }
  }
  return false;
}

export interface PageHandle {
  /** Evaluate an expression in the page (promises are awaited). */
  evaluate<T = unknown>(expression: string): Promise<T>;
  /** Poll until `expression` is truthy. */
  waitFor(expression: string, timeoutMs?: number, label?: string): Promise<void>;
  /** Emulate a viewport size (CSS pixels). */
  setViewport(width: number, height: number): Promise<void>;
  clearViewport(): Promise<void>;
  /** Real mouse input; Chrome derives pointer events from it (drag tests). */
  mouse(
    type: "mousePressed" | "mouseMoved" | "mouseReleased",
    x: number, y: number, buttons: number,
  ): Promise<void>;
}

export interface BrowserHandle {
  newPage(url: string): Promise<PageHandle>;
  close(): Promise<void>;
}

interface CdpResult {
  result?: { value?: unknown };
  exceptionDetails?: { text?: string; exception?: { description?: string } };
}

class Cdp {
  private readonly ws: WebSocket;
  private nextId = 0;
  private readonly pending = new Map<number, {
    resolve: (value: unknown) => void;
    reject: (error: Error) => void;
    timer: ReturnType<typeof setTimeout>;
  }>();

  constructor(ws: WebSocket) {
    this.ws = ws;
    ws.addEventListener("message", (event: MessageEvent) => {
      let message: { id?: number; result?: unknown; error?: { message?: string; code?: number } };
      try {
        message = JSON.parse(String(event.data));
      } catch {
        return;
      }
      if (typeof message.id !== "number") return;
      const waiter = this.pending.get(message.id);
      if (!waiter) return;
      this.pending.delete(message.id);
      clearTimeout(waiter.timer);
      if (message.error) {
        waiter.reject(new Error(`${message.error.message ?? "CDP error"} (${message.error.code ?? "?"})`));
      } else {
        waiter.resolve(message.result);
      }
    });
  }

  send<T = unknown>(method: string, params: Record<string, unknown> = {}, sessionId?: string): Promise<T> {
    const id = ++this.nextId;
    return new Promise<T>((resolve, reject) => {
      const timer = setTimeout(() => {
        this.pending.delete(id);
        reject(new Error(`CDP timeout after 10s: ${method}`));
      }, 10_000);
      this.pending.set(id, {
        resolve: resolve as (value: unknown) => void,
        reject,
        timer,
      });
      try {
        this.ws.send(JSON.stringify({ id, method, params, ...(sessionId ? { sessionId } : {}) }));
      } catch (error) {
        clearTimeout(timer);
        this.pending.delete(id);
        reject(error instanceof Error ? error : new Error(String(error)));
      }
    });
  }

  close(): void {
    for (const waiter of this.pending.values()) {
      clearTimeout(waiter.timer);
      waiter.reject(new Error("CDP connection closed"));
    }
    this.pending.clear();
    try { this.ws.close(); } catch { /* already closing */ }
  }
}

class Page implements PageHandle {
  private readonly cdp: Cdp;
  private readonly sessionId: string;

  constructor(cdp: Cdp, sessionId: string) {
    this.cdp = cdp;
    this.sessionId = sessionId;
  }

  async evaluate<T = unknown>(expression: string): Promise<T> {
    const response = await this.cdp.send<CdpResult>("Runtime.evaluate", {
      expression,
      awaitPromise: true,
      returnByValue: true,
      userGesture: true,
    }, this.sessionId);
    if (response.exceptionDetails) {
      const detail = response.exceptionDetails.exception?.description
        ?? response.exceptionDetails.text ?? "evaluation failed";
      throw new Error(detail);
    }
    return response.result?.value as T;
  }

  async waitFor(expression: string, timeoutMs = 15_000, label = expression): Promise<void> {
    const start = Date.now();
    let lastError: string | null = null;
    while (Date.now() - start < timeoutMs) {
      try {
        const ok = await this.evaluate<boolean>(`Boolean(${expression})`);
        if (ok) return;
      } catch (error) {
        lastError = error instanceof Error ? error.message : String(error);
      }
      await sleep(150);
    }
    throw new Error(`Timed out waiting for ${label}${lastError ? ` (${lastError})` : ""}`);
  }

  async setViewport(width: number, height: number): Promise<void> {
    await this.cdp.send("Emulation.setDeviceMetricsOverride", {
      width, height, deviceScaleFactor: 1, mobile: false,
    }, this.sessionId);
  }

  async clearViewport(): Promise<void> {
    await this.cdp.send("Emulation.clearDeviceMetricsOverride", {}, this.sessionId);
  }

  async mouse(
    type: "mousePressed" | "mouseMoved" | "mouseReleased",
    x: number, y: number, buttons: number,
  ): Promise<void> {
    await this.cdp.send("Input.dispatchMouseEvent", {
      type, x, y, button: "left", buttons, clickCount: 1,
    }, this.sessionId);
  }
}

async function readDevToolsPort(file: string, timeoutMs = 20_000): Promise<{ port: number; path: string }> {
  const start = Date.now();
  while (Date.now() - start < timeoutMs) {
    try {
      const raw = await readFile(file, "utf8");
      const [portLine, pathLine] = raw.trim().split(/\r?\n/);
      const port = Number(portLine);
      if (Number.isInteger(port) && port > 0 && pathLine?.startsWith("/")) {
        return { port, path: pathLine.trim() };
      }
    } catch { /* file not created yet */ }
    await sleep(100);
  }
  throw new Error("Chromium did not expose a DevTools port");
}

function connect(url: string): Promise<WebSocket> {
  return new Promise((resolve, reject) => {
    const ws = new WebSocket(url);
    const timer = setTimeout(() => {
      try { ws.close(); } catch { /* ignore */ }
      reject(new Error(`Timed out connecting to ${url}`));
    }, 10_000);
    ws.addEventListener("open", () => { clearTimeout(timer); resolve(ws); }, { once: true });
    ws.addEventListener("error", () => {
      clearTimeout(timer);
      reject(new Error(`Could not connect to ${url}`));
    }, { once: true });
  });
}

/** Launch headless Chromium with a throw-away profile and connect over CDP. */
export async function launchBrowser(executable: string): Promise<BrowserHandle> {
  const profile = await mkdtemp(join(tmpdir(), "mc5-layout-"));
  const args = [
    "--headless=new",
    "--disable-gpu",
    "--disable-extensions",
    "--no-first-run",
    "--no-default-browser-check",
    "--disable-background-networking",
    "--disable-sync",
    "--metrics-recording-only",
    "--mute-audio",
    "--hide-scrollbars",
    `--user-data-dir=${profile}`,
    "--remote-debugging-port=0",
    "--window-size=1280,900",
    "about:blank",
  ];
  const child: ChildProcess = spawn(executable, args, { stdio: "ignore", windowsHide: true });
  let ws: WebSocket | null = null;
  let cdp: Cdp | null = null;

  const cleanup = async () => {
    try { cdp?.close(); } catch { /* ignore */ }
    if (child.exitCode === null && !child.killed) child.kill();
    await sleep(200);
    await rm(profile, { recursive: true, force: true }).catch(() => { /* Windows may hold locks briefly */ });
  };

  try {
    const { port, path } = await readDevToolsPort(join(profile, "DevToolsActivePort"));
    ws = await connect(`ws://127.0.0.1:${port}${path}`);
    cdp = new Cdp(ws);

    return {
      async newPage(url: string): Promise<PageHandle> {
        const { targetId } = await cdp!.send<{ targetId: string }>("Target.createTarget", { url: "about:blank" });
        const { sessionId } = await cdp!.send<{ sessionId: string }>("Target.attachToTarget", {
          targetId, flatten: true,
        });
        await cdp!.send("Page.enable", {}, sessionId);
        await cdp!.send("Runtime.enable", {}, sessionId);
        await cdp!.send("Page.navigate", { url }, sessionId);
        return new Page(cdp!, sessionId);
      },
      async close(): Promise<void> {
        try { await cdp!.send("Browser.close"); } catch { /* already gone */ }
        const exited = new Promise<void>((resolve) => {
          const timer = setTimeout(() => { child.kill(); resolve(); }, 3000);
          child.once("exit", () => { clearTimeout(timer); resolve(); });
          if (child.exitCode !== null) { clearTimeout(timer); resolve(); }
        });
        await exited;
        await cleanup();
      },
    };
  } catch (error) {
    await cleanup();
    throw error;
  }
}
