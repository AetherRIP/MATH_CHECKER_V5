/**
 * Boots a throwaway, seeded Math Checker app for the Dashboard browser tests.
 *
 * The Dashboard's cards only render their populated branches once real rows
 * exist (work outstanding, results over time, jobs in flight, metered traffic,
 * assignment averages). `backend/scripts/seed_dashboard_fixture.py` writes those
 * rows into a fresh temp data directory with the app's own schema and config,
 * then serves the real app against it — on its own free port, so a teacher's
 * running install is never touched.
 *
 * The seeder prints exactly one machine-readable line when it is ready:
 *
 *     FIXTURE_READY {"data_dir": "...", "url": "http://127.0.0.1:53121", ...}
 *
 * Set DASHBOARD_FIXTURE_URL to reuse an app that is already running against
 * seeded data instead of starting one (the tests then never own its lifetime).
 */
import { spawn, spawnSync, type ChildProcess } from "node:child_process";
import { existsSync } from "node:fs";
import { mkdtemp, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { fileURLToPath } from "node:url";

const ROOT = fileURLToPath(new URL("../../../", import.meta.url));
const SEEDER = join(ROOT, "backend", "scripts", "seed_dashboard_fixture.py");
const READY = "FIXTURE_READY ";
const REFUSED = "FIXTURE_FAILED ";
const START_TIMEOUT_MS = 240_000;

const sleep = (ms: number) => new Promise<void>((resolve) => setTimeout(resolve, ms));

export interface FixtureSummary {
  data_dir: string;
  courses: number;
  assignments: number;
  students: number;
  results: number;
  jobs: number;
  api_keys: number;
}

export interface DashboardApp {
  url: string;
  /** True when this test started (and must stop) the app. */
  owned: boolean;
  summary: FixtureSummary | null;
  stop(): Promise<void>;
}

function pythonCandidates(): string[] {
  const candidates: string[] = [];
  if (process.env.MC5_PYTHON) candidates.push(process.env.MC5_PYTHON);
  candidates.push(process.platform === "win32"
    ? join(ROOT, ".venv", "Scripts", "python.exe")
    : join(ROOT, ".venv", "bin", "python"));
  candidates.push(process.platform === "win32" ? "python" : "python3", "python");
  return candidates;
}

/**
 * The interpreter that can run the app, or a reason why the fixture cannot
 * start at all (the tests then skip instead of pretending to have passed).
 */
export function fixtureRuntime(): { python: string | null; reason: string | null } {
  if (process.env.DASHBOARD_FIXTURE_URL) return { python: null, reason: null };
  if (!existsSync(SEEDER)) {
    return { python: null, reason: `fixture seeder not found at ${SEEDER}` };
  }
  for (const candidate of pythonCandidates()) {
    const probe = spawnSync(candidate, ["-c", "import uvicorn, fastapi"], {
      cwd: ROOT, timeout: 60_000, stdio: "ignore",
    });
    if (probe.status === 0) return { python: candidate, reason: null };
  }
  return {
    python: null,
    reason: "no Python with uvicorn+fastapi (set MC5_PYTHON to the app's interpreter)",
  };
}

/** Kill the whole tree: the app re-execs itself, so the child holds the port. */
function killTree(child: ChildProcess): void {
  if (child.exitCode !== null || child.pid == null) return;
  if (process.platform === "win32") {
    spawnSync("taskkill", ["/PID", String(child.pid), "/T", "/F"], { stdio: "ignore" });
  } else {
    try { process.kill(-child.pid, "SIGKILL"); }
    catch { try { child.kill("SIGKILL"); } catch { /* already gone */ } }
  }
}

async function removeDir(path: string): Promise<void> {
  for (let attempt = 0; attempt < 12; attempt++) {
    try {
      await rm(path, { recursive: true, force: true });
      return;
    } catch {
      // Windows briefly keeps the SQLite files locked by the killed process.
      await sleep(250);
    }
  }
}

export async function startDashboardApp(): Promise<DashboardApp> {
  const reused = process.env.DASHBOARD_FIXTURE_URL;
  if (reused) {
    return {
      url: reused.replace(/\/$/, ""),
      owned: false,
      summary: null,
      stop: async () => { /* not ours to stop */ },
    };
  }

  const runtime = fixtureRuntime();
  if (!runtime.python) throw new Error(runtime.reason ?? "no fixture runtime");

  const dataDir = await mkdtemp(join(tmpdir(), "mc5-dashboard-fixture-"));
  const env = { ...process.env, MC5_DATA_DIR: dataDir, PYTHONUNBUFFERED: "1", PYTHONIOENCODING: "utf-8" };
  delete env.MC5_DEMO;   // a plain seeded directory, never demo mode

  const child = spawn(runtime.python, [SEEDER, "--data-dir", dataDir, "--serve"], {
    cwd: ROOT,
    env,
    detached: process.platform !== "win32",
    stdio: ["ignore", "pipe", "pipe"],
  });

  const tail: string[] = [];
  let settled = false;

  const ready = new Promise<{ url: string; summary: FixtureSummary }>((resolve, reject) => {
    const consume = (chunk: Buffer) => {
      for (const line of chunk.toString("utf8").split(/\r?\n/)) {
        if (!line.trim()) continue;
        tail.push(line);
        if (tail.length > 40) tail.shift();
        if (line.startsWith(READY)) {
          settled = true;
          try {
            const payload = JSON.parse(line.slice(READY.length)) as {
              url?: string; summary?: FixtureSummary;
            };
            if (!payload.url) throw new Error("the fixture reported no url");
            resolve({ url: payload.url, summary: payload.summary as FixtureSummary });
          } catch (error) {
            reject(new Error(`unreadable fixture line: ${line} (${String(error)})`));
          }
        } else if (line.startsWith(REFUSED)) {
          settled = true;
          reject(new Error(`fixture refused to start: ${line.slice(REFUSED.length)}`));
        }
      }
    };
    child.stdout?.on("data", consume);
    child.stderr?.on("data", consume);
    child.once("exit", (code) => {
      if (settled) return;
      settled = true;
      reject(new Error(`fixture exited with code ${code}\n${tail.join("\n")}`));
    });
    setTimeout(() => {
      if (settled) return;
      settled = true;
      reject(new Error(`fixture did not become ready in ${START_TIMEOUT_MS} ms\n${tail.join("\n")}`));
    }, START_TIMEOUT_MS).unref();
  });

  let payload: { url: string; summary: FixtureSummary };
  try {
    payload = await ready;
  } catch (error) {
    killTree(child);
    await removeDir(dataDir);
    throw error;
  }

  // Last-ditch cleanup if the test process itself dies before stop().
  const onExit = () => killTree(child);
  process.once("exit", onExit);

  return {
    url: payload.url,
    owned: true,
    summary: payload.summary ?? null,
    async stop() {
      process.removeListener("exit", onExit);
      killTree(child);
      const deadline = Date.now() + 6000;
      while (child.exitCode === null && Date.now() < deadline) await sleep(100);
      await removeDir(dataDir);
    },
  };
}
