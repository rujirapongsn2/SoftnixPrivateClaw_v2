import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

export default defineConfig({
  plugins: [react()],
  server: {
    // Listen on all interfaces so both 127.0.0.1 and ::1 (localhost) reach the dev server.
    host: true,
    port: Number(process.env.PORT) || 5173,
    // Allow access through the Cloudflare tunnel hostname(s). A leading dot
    // matches the domain and all its subdomains (claw2.softnix.ai, etc.).
    // Extra hosts can be added via CLAW_ALLOWED_HOSTS (comma-separated).
    allowedHosts: [
      ".softnix.ai",
      ...(process.env.CLAW_ALLOWED_HOSTS?.split(",").map((h) => h.trim()).filter(Boolean) ?? []),
    ],
    proxy: {
      "/api": "http://127.0.0.1:8700",
      "/ws": { target: "ws://127.0.0.1:8700", ws: true },
    },
  },
});
