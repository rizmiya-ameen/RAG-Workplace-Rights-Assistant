/// <reference types="vitest/config" />
import tailwindcss from "@tailwindcss/vite";
import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// Dev: the page calls /api/chat and Vite forwards it to the FastAPI app, so the API needs no
// CORS. 127.0.0.1, not localhost: Windows tries ::1 first and uvicorn listens on IPv4.
// Production (step 21) sets VITE_API_BASE to the deployed API instead.
export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: {
    port: 5173,
    proxy: {
      "/api": {
        target: "http://127.0.0.1:8000",
        rewrite: (path) => path.replace(/^\/api/, ""),
      },
    },
  },
  test: {
    environment: "jsdom",
    setupFiles: ["./src/test-setup.ts"],
  },
});
