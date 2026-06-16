import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

// Dev server proxies API + websocket to the FastAPI backend on :8000 so the
// browser talks to a single origin (matches the production single-origin serve).
export default defineConfig({
  plugins: [react()],
  server: {
    host: "127.0.0.1",
    port: 5173,
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
