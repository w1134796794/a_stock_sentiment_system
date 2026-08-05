import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import { fileURLToPath } from "node:url";

const outputDirectory = fileURLToPath(
  new URL("../web/static/workbench", import.meta.url)
);

export default defineConfig({
  plugins: [react()],
  base: "/static/workbench/",
  build: {
    outDir: outputDirectory,
    emptyOutDir: true,
    rollupOptions: {
      input: fileURLToPath(new URL("./src/main.tsx", import.meta.url)),
      output: {
        entryFileNames: "workspace.js",
        assetFileNames: "workspace.[ext]",
        chunkFileNames: "chunks/[name]-[hash].js"
      }
    }
  }
});
