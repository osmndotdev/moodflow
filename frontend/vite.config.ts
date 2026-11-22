import { fileURLToPath } from "node:url";
import { defineConfig, loadEnv } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig(({ mode }) => {
  // backend port lives in the repo-root .env; only read here for the proxy, never exposed to the client
  const env = loadEnv(mode, "..", "MOODFLOW_");
  const apiPort = env.MOODFLOW_PORT || "8000";
  return {
    plugins: [react()],
    build: {
      rollupOptions: {
        // extra pages: privacy policy at /privacy/, terms of service at /terms/
        input: {
          main: fileURLToPath(new URL("./index.html", import.meta.url)),
          privacy: fileURLToPath(new URL("./privacy/index.html", import.meta.url)),
          terms: fileURLToPath(new URL("./terms/index.html", import.meta.url)),
        },
      },
    },
    server: {
      port: 5173,
      proxy: { "/api": `http://127.0.0.1:${apiPort}` },
    },
  };
});
