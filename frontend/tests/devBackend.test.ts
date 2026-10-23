// The dev server must never guess which backend to use.
// Run from `frontend/` with `node --test "tests/*.test.ts"`.
import { test } from "node:test";
import assert from "node:assert/strict";
import {
  DEFAULT_DEV_BACKEND, DevBackendConfigError, isLoopbackOrigin, resolveDevBackend,
} from "../scripts/dev-backend.mjs";

test("a configured backend origin is accepted and normalised", () => {
  assert.equal(resolveDevBackend("http://127.0.0.1:8642"), "http://127.0.0.1:8642");
  assert.equal(resolveDevBackend("  http://127.0.0.1:8642/  "), "http://127.0.0.1:8642");
  assert.equal(resolveDevBackend("https://math.example.ts.net:8443"),
    "https://math.example.ts.net:8443");
});

test("a missing configuration is an error, never an implicit 8642", () => {
  for (const value of [undefined, null, "", "   "]) {
    assert.throws(() => resolveDevBackend(value), DevBackendConfigError);
  }
  // The old hardcoded target is documented, not applied.
  assert.equal(DEFAULT_DEV_BACKEND, "http://127.0.0.1:8642");
});

test("a malformed or partial value is rejected with guidance", () => {
  assert.throws(() => resolveDevBackend("127.0.0.1:8642"), /must start with http:\/\/ or https:\/\//);
  assert.throws(() => resolveDevBackend("localhost:8642"), /must start with http:\/\/ or https:\/\//);
  assert.throws(() => resolveDevBackend("ftp://127.0.0.1:8642"), /must start with http:\/\/ or https:\/\//);
  assert.throws(() => resolveDevBackend("http://"), /is not a URL|has no host/);
});

test("paths, queries, fragments and embedded credentials are rejected", () => {
  assert.throws(() => resolveDevBackend("http://127.0.0.1:8642/api"), /origin only/);
  assert.throws(() => resolveDevBackend("http://127.0.0.1:8642/?x=1"), /origin only/);
  assert.throws(() => resolveDevBackend("http://127.0.0.1:8642/#demo"), /origin only/);
  assert.throws(() => resolveDevBackend("http://user:secret@127.0.0.1:8642"), /credentials/);
});

test("loopback detection distinguishes this machine from a remote backend", () => {
  assert.equal(isLoopbackOrigin("http://127.0.0.1:8642"), true);
  assert.equal(isLoopbackOrigin("http://localhost:8642"), true);
  assert.equal(isLoopbackOrigin("http://[::1]:8642"), true);
  assert.equal(isLoopbackOrigin("http://192.168.1.20:8642"), false);
  assert.equal(isLoopbackOrigin("https://math.example.ts.net"), false);
  assert.equal(isLoopbackOrigin("not a url"), false);
});
