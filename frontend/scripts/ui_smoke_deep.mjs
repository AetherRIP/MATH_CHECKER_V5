// One-off deep-link DOM check (mirrors ui_smoke.mjs): verifies
// #/students/{id} and #/report/{course}/{work} actually render data.
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

window.fetch = (url, init) => fetch(new URL(url, BASE), init);
window.EventSource = class {
  constructor() { setTimeout(() => this.onmessage && this.onmessage({ data: "{}" }), 5); }
  close() {}
};
window.matchMedia = window.matchMedia || (() => ({ matches: false, addListener() {}, removeListener() {} }));
window.addEventListener("error", (e) => errors.push(`window.onerror: ${e.message}`));
const origError = console.error;
console.error = (...a) => { errors.push(`console.error: ${a.map(String).join(" ")}`); };

const bundle = readFileSync(join(dist, "assets", jsFile), "utf8");
try { window.eval(bundle); } catch (e) { errors.push(`bundle eval: ${e.stack || e}`); }

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const text = () => window.document.body.textContent || "";

const ROUTES = [
  ["#/students/108589983533697858396",
    ["Abdulrahman Ahmed", "Nickname", "Assignment history", "Current status", "Google Classroom ID", "Performance"]],
  ["#/report/877926720424/887289637510",
    ["Report", "Class information", "Assignment information", "Submission & marking statistics", "Students"]],
  ["#/classroom/877926720424/887289637510",
    ["Google Classroom", "Tracking", "Correct Entire Assignment"]],
];

let failed = 0;
for (const [route, expects] of ROUTES) {
  window.location.hash = route;
  window.dispatchEvent(new window.Event("hashchange"));
  await sleep(2500); // data fetches are async (77-row tables)
  const body = text();
  const missing = expects.filter((s) => !body.includes(s));
  if (missing.length) {
    failed++;
    console.log(`FAIL  ${route}  missing: ${JSON.stringify(missing)}`);
  } else {
    console.log(`ok    ${route}`);
  }
}

// report deep link: verify real row count made it into the DOM
window.location.hash = "#/report/877926720424/887289637510";
window.dispatchEvent(new window.Event("hashchange"));
await sleep(2500);
const body = text();
const m = body.match(/(\d+) of (\d+) shown/);
if (m && Number(m[2]) >= 70) console.log(`ok    report rows: "${m[0]}"`);
else { failed++; console.log(`FAIL  report rows: ${m ? m[0] : "not found"}`); }

console.error = origError;
const realErrors = errors.filter((e) => !e.includes("Could not parse CSS"));
if (realErrors.length) {
  console.log(`JS errors (${realErrors.length}):`);
  realErrors.slice(0, 8).forEach((e) => console.log("  " + e.slice(0, 300)));
}
console.log(`\nRESULT: ${failed === 0 && realErrors.length === 0 ? "PASS" : "FAIL"} (failed: ${failed}, js errors: ${realErrors.length})`);
process.exit(failed === 0 && realErrors.length === 0 ? 0 : 1);
