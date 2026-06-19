import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

// Dev server proxies API + websocket to the FastAPI backend on :8000 so the
// browser talks to a single origin (matches the production single-origin serve).
//
// The server stays bound to loopback (§5 "bind loopback"). To view the dev app
// from another tailnet device, front it with `tailscale serve` (never Funnel)
// and set, for that run only:
//   VITE_ALLOWED_HOSTS=.ts.net   — let Vite accept the proxied MagicDNS Host
//   VITE_HMR_CLIENT_PORT=443     — point HMR's ws at the Serve HTTPS port
// Both are env-driven so no tailnet-specific value is ever committed.
const allowedHosts = process.env["VITE_ALLOWED_HOSTS"]
  ?.split(",")
  .map((h) => h.trim())
  .filter(Boolean);
const hmrClientPort = process.env["VITE_HMR_CLIENT_PORT"]
  ? Number(process.env["VITE_HMR_CLIENT_PORT"])
  : undefined;

export default defineConfig({
  plugins: [react()],
  server: {
    host: "127.0.0.1",
    port: 5173,
    ...(allowedHosts ? { allowedHosts } : {}),
    ...(hmrClientPort ? { hmr: { clientPort: hmrClientPort } } : {}),
    proxy: {
      "/api": { target: "http://127.0.0.1:8000", changeOrigin: true },
      "/ws": { target: "ws://127.0.0.1:8000", ws: true },
    },
  },
  test: {
    environment: "jsdom",
    include: ["src/**/*.test.ts", "src/**/*.test.tsx"],
  },
});
