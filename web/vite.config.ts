import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

export default defineConfig({
  plugins: [react()],
  server: {
    // Listen on all interfaces so both 127.0.0.1 and ::1 (localhost) reach the dev server.
    host: true,
    port: Number(process.env.PORT) || 5173,
    proxy: {
      "/api": "http://127.0.0.1:8700",
      "/ws": { target: "ws://127.0.0.1:8700", ws: true },
    },
  },
});
