import tailwindcss from "@tailwindcss/vite";
import react from "@vitejs/plugin-react";
import { fileURLToPath } from "node:url";
import { readFileSync } from "node:fs";
import { defineConfig } from "vite";

function fromConfig(relativePath: string): string {
  return fileURLToPath(new URL(relativePath, import.meta.url));
}

export default defineConfig({
  define: {
    __SENDSPIN_VERSION__: JSON.stringify(JSON.parse(readFileSync(
      fromConfig("package-lock.json"), "utf8",
    )).packages["node_modules/@sendspin/sendspin-js"].version),
  },
  base: process.env.VITE_PLAYER_BASE ?? "/",
  plugins: [react(), tailwindcss()],
  resolve: {
    alias: {
      "@": fromConfig("src/"),
    },
  },
  build: {
    outDir: fromConfig("../custom_plugins/race_voice/player"),
    emptyOutDir: true,
    sourcemap: true,
    rollupOptions: {
      output: {
        entryFileNames: "assets/[name]-[hash].js",
        chunkFileNames: "assets/[name]-[hash].js",
        assetFileNames: "assets/[name]-[hash][extname]",
      },
    },
  },
  server: {
    strictPort: false,
  },
});
