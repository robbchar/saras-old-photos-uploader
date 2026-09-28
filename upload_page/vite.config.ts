import { defineConfig, type ProxyOptions } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";

// The Python upload_server.py process this page is served by/proxies to
// during development. Hardcoded here rather than read from an env var
// because the dev proxy target never varies across machines.
const UPLOAD_SERVER_ORIGIN = "http://127.0.0.1:5277";

// `changeOrigin` rewrites the outgoing Host header to the target, but NOT the
// Origin header - so a POST/SSE from the Vite dev origin (e.g. localhost:5173)
// still carries that Origin and the server's same-origin guard rejects it 403.
// Rewrite Origin to the upload server's own here, so the guard stays strict in
// production (served directly, no Vite) yet passes for the dev proxy.
const uploadServerProxy: ProxyOptions = {
  target: UPLOAD_SERVER_ORIGIN,
  changeOrigin: true,
  configure: (proxy) => {
    proxy.on("proxyReq", (proxyReq) => {
      proxyReq.setHeader("origin", UPLOAD_SERVER_ORIGIN);
    });
  },
};

export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: {
    proxy: {
      "/api": uploadServerProxy,
      "/assets": uploadServerProxy,
    },
  },
});
