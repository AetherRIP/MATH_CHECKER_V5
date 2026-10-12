import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// TEMPORARY preview-only config for inspecting the Accuracy Audit page against
// an isolated audit-view backend. Not part of the app; deleted after review.
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5219,
    proxy: { "/api": "http://127.0.0.1:8791" },
  },
});
