import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import { fileURLToPath } from "node:url";

// Корень renderer-а — отдельная папка, чтобы main/preload собирались обычным tsc
// и не попадали в бандл Vite.
const root = fileURLToPath(new URL("./src/renderer", import.meta.url));

export default defineConfig({
  root,
  // В продакшене окно грузится по file:// — абсолютные пути там ломаются.
  base: "./",
  plugins: [react()],
  server: {
    host: "127.0.0.1",
    port: 5273,
    strictPort: true,
  },
  build: {
    outDir: fileURLToPath(new URL("./dist/renderer", import.meta.url)),
    emptyOutDir: true,
    target: "chrome130",
    sourcemap: false,
  },
});
