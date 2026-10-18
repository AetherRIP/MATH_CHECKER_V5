/**
 * Browser-driven regression: the app chrome must never scroll out of view.
 *
 * The shell clamps the app to the viewport (`.app-shell { height: 100dvh;
 * overflow: hidden }`). That clip only holds while the shell is a containing
 * block for absolutely positioned descendants. When it was not, elements like
 * the charts' `.visually-hidden` a11y table and the `.switch` checkbox took the
 * initial containing block as theirs, escaped the clip and stretched the
 * DOCUMENT — so the whole site scrolled up over the topbar and sidebar and left
 * blank page background below them. Two pages showed it on a stock install:
 * `#/settings` (7 switches, +1842px) and `#/usage` (chart tables, +771px).
 *
 * Requirements:
 *   * a Chrome/Chromium/Edge binary (auto-discovered, or set CHROME_PATH);
 *   * the app reachable (default http://127.0.0.1:8720, override with
 *     PAGE_SCROLL_URL) and not gated behind a sign-in page.
 *
 * Skips with an explicit reason when either is missing — a skip is reported as
 * "not run", never as a pass. Run from `frontend/` with:
 *   node --test "tests/*.test.ts"
 */
import { test } from "node:test";
import assert from "node:assert/strict";
import {
  appReachable, chromiumPath, launchBrowser,
  type PageHandle,
} from "./helpers/browser.ts";

const APP = (process.env.PAGE_SCROLL_URL ?? "http://127.0.0.1:8720").replace(/\/$/, "");

/** Pages that carried escaping content on a stock install, plus the Dashboard. */
const ROUTES = ["dashboard", "settings", "usage", "results", "ai"];

/** Markup shaped exactly like the charts' hidden data table. */
const CHART_TABLE = `<table class="visually-hidden"><caption>probe</caption><tbody>${
  Array.from({ length: 6 }, (_, i) => `<tr><th>Ch${i + 1}L1</th><td>${70 + i}%</td></tr>`).join("")
}</tbody></table>`;

interface ScrollState {
  extra: number;
  scrollTop: number;
  topbarTop: number | null;
  beyondViewport: { what: string; bottom: number; offsetParent: string | null }[];
}

const MEASURE = String.raw`(() => {
  const se = document.scrollingElement;
  const H = se.clientHeight;
  const beyondViewport = [];
  document.querySelectorAll(".app-shell *").forEach((el) => {
    const cs = getComputedStyle(el);
    if (cs.position !== "absolute" && cs.position !== "fixed") return;
    const op = el.offsetParent;
    if (op && op !== document.body) return;   // still inside the shell's clip
    const box = el.getBoundingClientRect();
    if (box.bottom > H + 1 || box.right > se.clientWidth + 1)
      beyondViewport.push({
        what: el.tagName + "." + String(el.className || "").slice(0, 32),
        bottom: Math.round(box.bottom),
        offsetParent: op ? op.tagName : null,
      });
  });
  const topbar = document.querySelector(".topbar");
  return {
    extra: se.scrollHeight - H,
    scrollTop: se.scrollTop,
    topbarTop: topbar ? Math.round(topbar.getBoundingClientRect().top) : null,
    beyondViewport,
  };
})()`;

function assertChromeFixed(state: ScrollState, context: string): void {
  assert.deepEqual(state.beyondViewport, [],
    `${context}: positioned content escaped the app shell — ${JSON.stringify(state.beyondViewport)}`);
  assert.ok(state.extra <= 1,
    `${context}: the document scrolls by ${state.extra}px — the whole site can scroll up past its chrome`);
}

const executable = chromiumPath();
const appUp = await appReachable(APP);
const skipReason = !executable
  ? "no Chrome/Chromium/Edge binary found (set CHROME_PATH to enable)"
  : !appUp
    ? `app not reachable at ${APP} (start it, or set PAGE_SCROLL_URL)`
    : false;

test("the page chrome never scrolls out of view", { skip: skipReason }, async (t) => {
  const browser = await launchBrowser(executable as string);
  try {
    const page = await browser.newPage(`${APP}/#/dashboard`);
    await page.waitFor("document.readyState === 'complete' && !!document.querySelector('.app-shell')",
      25_000, "the app shell");
    // A sign-in wall would make every measurement meaningless.
    const signedOut = await page.evaluate<boolean>("!!document.querySelector('.signin-wrap')");
    if (signedOut) {
      t.skip("the app requires sign-in, so its pages cannot be measured");
      return;
    }

    await t.test("no page extends the document", async () => {
      for (const route of ROUTES) {
        await page.evaluate(`location.hash = ${JSON.stringify(`#/${route}`)}; true`);
        await page.waitFor(
          "!!document.querySelector('.main-inner') && !document.querySelector('.skeleton-lines')",
          20_000,
          `#/${route} to render`);
        // Let lazy page chunks and their charts mount.
        await new Promise((resolve) => setTimeout(resolve, 700));
        await page.evaluate("window.scrollTo(0, 5000); true");
        const state = await page.evaluate<ScrollState>(MEASURE);
        assertChromeFixed(state, `#/${route}`);
        assert.equal(state.scrollTop, 0, `#/${route}: the window must not scroll at all`);
        assert.equal(state.topbarTop, 0, `#/${route}: the topbar must stay pinned`);
      }
    });

    await t.test("a chart's hidden a11y table cannot escape its card", async () => {
      await page.evaluate("location.hash = '#/dashboard'; true");
      await page.waitFor("!!document.querySelector('.main-inner')", 20_000, "the Dashboard");
      await new Promise((resolve) => setTimeout(resolve, 700));

      const before = await page.evaluate<ScrollState>(MEASURE);
      const injected = await page.evaluate<boolean>(`(() => {
        const card = [...document.querySelectorAll(".main-inner > .card")].pop();
        if (!card) return false;
        card.insertAdjacentHTML("beforeend", ${JSON.stringify(CHART_TABLE)});
        return true;
      })()`);
      assert.ok(injected, "the Dashboard renders a plain trailing card to probe");
      const after = await page.evaluate<ScrollState>(MEASURE);
      assertChromeFixed(after, "the Dashboard with a chart table injected");
      assert.equal(after.extra, before.extra,
        `the hidden table grew the document by ${after.extra - before.extra}px`);

      // The switch checkbox is the same hazard on the Settings page.
      await page.evaluate("location.hash = '#/settings'; true");
      await page.waitFor("!!document.querySelector('.switch input')", 20_000, "the Settings switches");
      const anchored = await page.evaluate<boolean>(`(() => {
        const shell = document.querySelector(".app-shell");
        const inputs = [...document.querySelectorAll(".switch input")];
        return inputs.length > 0 && inputs.every((el) => shell.contains(el.offsetParent));
      })()`);
      assert.ok(anchored, "every switch checkbox stays anchored inside the app shell");

      await page.evaluate(
        "(() => { document.querySelector('.main-inner > .card .visually-hidden')?.remove(); return true; })()");
    });
  } finally {
    await browser.close();
  }
});
