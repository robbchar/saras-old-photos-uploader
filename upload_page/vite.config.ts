import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";

// The Python upload_server.py process this page is served by/proxies to
// during development. Hardcoded here rather than read from an env var
// because the dev proxy target never varies across machines.
const UPLOAD_SERVER_ORIGIN = "http://127.0.0.1:5277";

export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: {
    proxy: {
      // changeOrigin rewrites the dev server's Origin header to the
      // upload_server's own, so its same-origin guard stays strict instead
      // of having to allow the Vite dev origin as a special case.
      "/api": {
        target: UPLOAD_SERVER_ORIGIN,
        changeOrigin: true,
      },
      "/assets": {
        target: UPLOAD_SERVER_ORIGIN,
        changeOrigin: true,
      },
    },
  },
});
