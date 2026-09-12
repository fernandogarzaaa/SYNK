import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Tauri dev server must be strict-port 1420 (see src-tauri/tauri.conf.json devPath).
export default defineConfig({
  plugins: [react()],
  clearScreen: false,
  server: {
    port: 1420,
    strictPort: true,
  },
  build: {
    outDir: "dist",
    target: "es2021",
  },
});
