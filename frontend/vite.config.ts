import { defineConfig, loadEnv } from "vite";
import react from "@vitejs/plugin-react";
import {
  DEV_BACKEND_ENV,
  isLoopbackOrigin,
  resolveDevBackend,
} from "./scripts/dev-backend.mjs";

// Backend selection is explicit (see scripts/dev-backend.mjs).
//
//  * `serve` (browser development) REQUIRES MC5_DEV_API and refuses to start
//    without it. The proxy target is the configured origin, never a guess.
//  * `build` produces the bundle the Python backend serves itself; it has no
//    proxy and always talks to its own origin, so it needs no configuration.
export default defineConfig(({ command, mode }) => {
  const env = loadEnv(mode, process.cwd(), "");

  let devBackend = "";
  if (command === "serve") {
    devBackend = resolveDevBackend(env[DEV_BACKEND_ENV]);
    const where = isLoopbackOrigin(devBackend)
      ? "this machine"
      : "ANOTHER MACHINE — real class data would cross the network";
    console.log(`  Math Checker backend: ${devBackend} (${where})`);
    console.log(`  switch it with ${DEV_BACKEND_ENV} in frontend/.env.local`);
  }

  const proxy = devBackend
    ? { "/api": { target: devBackend, changeOrigin: false } }
    : undefined;

  return {
    plugins: [react()],
    // Compile-time identity of the environment the bundle runs in. The UI shows
    // it, so a browser window can never quietly present demo data as real work.
    define: {
      __MC5_DEV_BACKEND__: JSON.stringify(devBackend),
      __MC5_DEV_SERVER__: JSON.stringify(command === "serve"),
    },
    server: {
      port: 5173,
      proxy,
    },
    // `vite preview` serves a production build; keep the same explicit target
    // so a preview can be pointed at a chosen backend instead of 404-ing.
    preview: {
      proxy,
    },
    build: {
      outDir: "dist",
      chunkSizeWarningLimit: 900,
    },
  };
});
