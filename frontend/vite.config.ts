import path from "node:path"
import tailwindcss from "@tailwindcss/vite"
import react from "@vitejs/plugin-react"
import { defineConfig } from "vite"

import { stripJsTrailingWhitespace } from "./src/vite-plugins"

export default defineConfig({
  plugins: [react(), tailwindcss(), stripJsTrailingWhitespace()],
  resolve: {
    alias: { "@": path.resolve(__dirname, "./src") },
  },
  server: {
    host: "127.0.0.1",
    port: 5173,
    proxy: { "/api": "http://127.0.0.1:8765" },
  },
  build: {
    outDir: "../src/stock_recommender/web",
    emptyOutDir: true,
    rollupOptions: {
      input: {
        index: path.resolve(__dirname, "./index.html"),
        performance: path.resolve(__dirname, "./performance.html"),
      },
    },
  },
})
