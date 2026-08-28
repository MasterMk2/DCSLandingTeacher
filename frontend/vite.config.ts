/// <reference types="vitest" />
import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// Backend (FastAPI) has no CORS middleware, so the dev server proxies
// REST (/api) and WebSocket (/ws) traffic to the backend origin.
const BACKEND = process.env.DLT_BACKEND_URL ?? "http://localhost:8000";

export default defineConfig({
  // Relative asset URLs so the same build works both at the domain root and
  // behind a reverse-proxy subpath (e.g. "/landing-teacher/"); see
  // src/api/client.ts / src/api/ws.ts for the matching runtime API base.
  base: "./",
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      // Versioned WebSocket (Issue #38) plus the legacy /api/ws alias. The
      // plain "/api" proxy below already forwards /api/v1 REST traffic.
      "/api/v1/ws": { target: BACKEND, ws: true, changeOrigin: true },
      "/api/ws": { target: BACKEND, ws: true, changeOrigin: true },
      "/api": { target: BACKEND, changeOrigin: true },
    },
  },
  build: {
    // Issue #39: split heavy / rarely-changing vendor code into its own
    // chunks so the app shell stays small and recharts (the Largest
    // dependency) is cached independently of application code.
    chunkSizeWarningLimit: 900,
    rollupOptions: {
      output: {
        manualChunks: {
          react: ["react", "react-dom"],
          recharts: ["recharts"],
        },
      },
    },
  },
  test: {
    environment: "node",
    include: ["src/**/*.test.ts", "src/**/*.test.tsx"],
  },
});
