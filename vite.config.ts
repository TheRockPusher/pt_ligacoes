import { fileURLToPath, URL } from "node:url";
import tailwindcss from "@tailwindcss/vite";
import { defineConfig } from "vite";

export default defineConfig({
  root: fileURLToPath(new URL("./frontend", import.meta.url)),
  base: "/static/",
  plugins: [tailwindcss()],
  build: {
    outDir: "dist",
    emptyOutDir: true,
    manifest: true,
    sourcemap: false,
    target: "es2022",
    rolldownOptions: {
      input: fileURLToPath(new URL("./frontend/src/main.ts", import.meta.url)),
    },
  },
});
