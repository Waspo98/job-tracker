import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      "/api": "http://127.0.0.1:5055",
      "/health": "http://127.0.0.1:5055",
      "/static": "http://127.0.0.1:5055",
      "/manifest.webmanifest": "http://127.0.0.1:5055",
      "/sw.js": "http://127.0.0.1:5055",
      "/offline": "http://127.0.0.1:5055"
    }
  },
  build: {
    outDir: "dist",
    emptyOutDir: true
  }
});
