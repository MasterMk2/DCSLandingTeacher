/// <reference types="vitest" />
import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// Backend (FastAPI) has no CORS middleware, so the dev server proxies
// REST (/api) and WebSocket (/ws) traffic to the backend origin.
const BACKEND = process.env.DLT_BACKEND_URL ?? "http://localhost:8000";

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      "/api/ws": { target: BACKEND, ws: true, changeOrigin: true },
      "/api": { target: BACKEND, changeOrigin: true },
    },
  },
  test: {
    environment: "node",
    include: ["src/**/*.test.ts", "src/**/*.test.tsx"],
  },
});
