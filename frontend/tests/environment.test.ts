// Environment identity shown by the shell. The backend is the authority on
// demo mode; the bundle only knows whether it was built for the dev server.
import { test } from "node:test";
import assert from "node:assert/strict";
import {
  backendOriginLabel, environmentDetail, environmentKind, environmentLabel,
  environmentTone, offlineMessage,
} from "../src/environment.ts";

test("the backend's demo_mode decides DEMO, not the port", () => {
  assert.equal(environmentKind({ demo_mode: true }, false, true), "demo");
  assert.equal(environmentKind({ demo_mode: true }, false, false), "demo");
  assert.equal(environmentKind({ demo_mode: false }, false, true), "dev");
  assert.equal(environmentKind({ mock_mode: true }, false, true), "dev");
  assert.equal(environmentKind({}, false, false), "live");
  // mock_mode alone (no demo data folder) is not DEMO.
  assert.equal(environmentKind({ demo_mode: false, mock_mode: true }, false, false), "live");
});

test("an unreachable backend is its own state, not empty data", () => {
  assert.equal(environmentKind(null, true, true), "offline");
  assert.equal(environmentKind(null, false, false), "live");
  assert.equal(environmentLabel("offline"), "No backend");
  assert.equal(environmentTone("offline"), "err");
  assert.match(offlineMessage(), /Cannot reach the Math Checker backend/);
  assert.match(offlineMessage(), /No data is being shown/);
});

test("labels and tones are distinct per environment", () => {
  assert.equal(environmentLabel("demo"), "DEMO");
  assert.equal(environmentLabel("dev"), "DEV");
  assert.equal(environmentLabel("live"), "LIVE");
  assert.equal(environmentTone("demo"), "warn");
  assert.equal(environmentTone("dev"), "info");
  assert.equal(environmentTone("live"), "ok");
  assert.match(environmentDetail("demo"), /synthetic/i);
  assert.match(environmentDetail("demo"), /Nothing here is your real class/i);
});

test("messages name an origin, never an internal path", () => {
  const label = backendOriginLabel();
  assert.ok(label.length > 0);
  assert.ok(!label.includes("/api"));
});
