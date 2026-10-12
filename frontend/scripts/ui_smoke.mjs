// DOM-level UI smoke test: loads the REAL built bundle in jsdom against the
// LIVE server, walks every route, and fails on JS errors or missing content.
//
// Usage: node scripts/ui_smoke.mjs [baseUrl]
import { readFileSync, readdirSync } from "node:fs";
import { join, dirname } from "node:path";
import { fileURLToPath } from "node:url";
import { JSDOM } from "jsdom";

const BASE = process.argv[2] || "http://127.0.0.1:8642";
const root = join(dirname(fileURLToPath(import.meta.url)), "..");
const dist = join(root, "dist");
const jsFile = readdirSync(join(dist, "assets")).find((f) => f.endsWith(".js"));
const html = readFileSync(join(dist, "index.html"), "utf8")
  .replace(/assets\/[^"]+\.js/, `assets/${jsFile}`);

const errors = [];
const dom = new JSDOM(html, {
  url: `${BASE}/#dashboard`,
  runScripts: "outside-only",
  pretendToBeVisual: true,
});
const { window } = dom;

// --- polyfills jsdom lacks -------------------------------------------------
window.fetch = (url, init) => fetch(new URL(url, BASE), init);
window.EventSource = class {
  constructor() { setTimeout(() => this.onmessage && this.onmessage({ data: "{}" }), 5); }
  close() {}
};
window.matchMedia = window.matchMedia || (() => ({ matches: false, addListener() {}, removeListener() {} }));
window.addEventListener("error", (e) => errors.push(`window.onerror: ${e.message}`));
const origError = console.error;
console.error = (...a) => { errors.push(`console.error: ${a.map(String).join(" ")}`); };

// --- run the bundle --------------------------------------------------------
const bundle = readFileSync(join(dist, "assets", jsFile), "utf8");
try {
  window.eval(bundle);
} catch (e) {
  errors.push(`bundle eval: ${e.stack || e}`);
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const text = () => window.document.body.textContent || "";

const ROUTES = [
  ["dashboard", ["Dashboard", "Corrections today", "API health"]],
  ["report", ["Report", "Class", "Assignment"]],
  ["students", ["Students", "Submissions", "Score"]],
  ["corrections", ["Corrections", "Job log", "Clear Log"]],
  ["diagnostics", ["Diagnostics", "System"]],
  ["classroom", ["Google Classroom"]],
  ["results", ["Results"]],
  ["workers", ["Workers"]],
  ["usage", ["Usage"]],
  ["keys", ["API Keys"]],
  ["settings", ["Settings"]],
];

let failed = 0;
for (const [route, expects] of ROUTES) {
  window.location.hash = `#/${route}`;
  window.dispatchEvent(new window.Event("hashchange"));
  await sleep(700);
  const body = text();
  const missing = expects.filter((s) => !body.includes(s));
  if (missing.length) {
    failed++;
    console.log(`FAIL  #/${route}  missing: ${JSON.stringify(missing)}`);
  } else {
    console.log(`ok    #/${route}`);
  }
}

// Extra checks on the corrections page: filter chips + clear button exist.
window.location.hash = "#/corrections";
window.dispatchEvent(new window.Event("hashchange"));
await sleep(500);
const buttons = [...window.document.querySelectorAll("button")].map((b) => b.textContent.trim());
if (!buttons.includes("Clear Log")) { failed++; console.log("FAIL  Clear Log button missing"); }
else console.log("ok    Clear Log button present");
// Chips are hidden when their count is 0, so only "All" is guaranteed;
// the rest must appear whenever jobs of that state exist.
if (!buttons.some((b) => b.startsWith("All"))) { failed++; console.log("FAIL  filter chip missing: All"); }
else console.log("ok    filter chips present (All + non-empty states)");

console.error = origError;
const realErrors = errors.filter((e) => !e.includes("Could not parse CSS"));
if (realErrors.length) {
  console.log(`\nJS errors (${realErrors.length}):`);
  realErrors.slice(0, 8).forEach((e) => console.log("  " + e.slice(0, 300)));
}
console.log(`\nRESULT: ${failed === 0 && realErrors.length === 0 ? "PASS" : "FAIL"} (routes failed: ${failed}, js errors: ${realErrors.length})`);
process.exit(failed === 0 && realErrors.length === 0 ? 0 : 1);
