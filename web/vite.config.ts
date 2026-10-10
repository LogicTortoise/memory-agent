import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import { fileURLToPath, URL } from "node:url";

// P7/T15: build a static dist that the Python `memory ui` command
// can serve. The base path is `./` because the page is served at the
// root of the loopback HTTP server; nothing assumes a sub-path.
export default defineConfig({
  plugins: [react()],
  base: "./",
  resolve: {
    alias: {
      "@": fileURLToPath(new URL("./src", import.meta.url)),
    },
  },
  build: {
    outDir: "dist",
    emptyOutDir: true,
    sourcemap: false,
    target: "es2022",
  },
  server: {
    host: "127.0.0.1",
    port: 5173,
    strictPort: true,
    proxy: {
      "/api": {
        target: "http://127.0.0.1:8420",
        changeOrigin: false,
      },
    },
  },
  preview: {
    host: "127.0.0.1",
    port: 8421,
    strictPort: true,
  },
});