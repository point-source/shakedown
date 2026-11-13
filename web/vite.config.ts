import path from "node:path"
import react from "@vitejs/plugin-react"
import tailwindcss from "@tailwindcss/vite"
import { defineConfig } from "vite"

// base "./": every asset is addressed relative to the page, so the dashboard
// works at a host's root or under a reverse-proxy path prefix (§spec:dashboard).
export default defineConfig({
  base: "./",
  plugins: [react(), tailwindcss()],
  resolve: { alias: { "@": path.resolve(import.meta.dirname, "./src") } },
  // One ~670 kB script (~200 kB gzip) served by serve on the operator's own
  // network with immutable caching (§spec:dashboard-page): splitting it would
  // not shorten any load that matters, so the default 500 kB advisory is raised.
  build: { chunkSizeWarningLimit: 800 },
})
