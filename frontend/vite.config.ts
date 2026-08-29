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
    // chunks so the app shell stays small and recharts (the largest
    // dependency) is cached independently of application code.
    chunkSizeWarningLimit: 900,
    rollupOptions: {
      output: {
        // Matched by module id rather than by the object form
        // ({react: ["react", "react-dom"], recharts: ["recharts"]}). The
        // object form names *entry* modules, and since recharts imports React
        // itself, Rollup pulled react/react-dom into the recharts chunk and
        // emitted a 32-byte "react" chunk that only re-exported it -- so a
        // recharts upgrade still invalidated React's cache entry, which is the
        // whole point of splitting. Matching ids puts React in its own chunk
        // whoever imports it. React must be tested first: recharts' own
        // dependency subtree contains react modules too.
        manualChunks(id: string) {
          if (!id.includes("node_modules")) return undefined;
          if (/[\\/]node_modules[\\/](react|react-dom|scheduler)[\\/]/.test(id)) {
            return "react";
          }
          if (
            /[\\/]node_modules[\\/](recharts|d3-[a-z]+|victory-vendor|internmap|decimal\.js-light|eventemitter3|fast-equals)[\\/]/.test(
              id,
            )
          ) {
            return "recharts";
          }
          return undefined;
        },
      },
    },
  },
  test: {
    environment: "node",
    include: ["src/**/*.test.ts", "src/**/*.test.tsx"],
  },
});
