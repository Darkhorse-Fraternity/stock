import path from "node:path"
import tailwindcss from "@tailwindcss/vite"
import react from "@vitejs/plugin-react"
import { defineConfig, type Plugin } from "vite"

function stripOutputTrailingWhitespace(): Plugin {
  return {
    name: "strip-output-trailing-whitespace",
    generateBundle(_options, bundle) {
      for (const output of Object.values(bundle)) {
        if (output.type === "chunk") {
          output.code = output.code.replace(/[\t ]+$/gm, "")
        } else if (typeof output.source === "string") {
          output.source = output.source.replace(/[\t ]+$/gm, "")
        }
      }
    },
  }
}

export default defineConfig({
  plugins: [react(), tailwindcss(), stripOutputTrailingWhitespace()],
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
  },
})
