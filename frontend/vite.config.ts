import { defineConfig, loadEnv } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig(({ mode }) => {
  // backend port lives in the repo-root .env; only read here for the proxy, never exposed to the client
  const env = loadEnv(mode, "..", "MOODFLOW_");
  const apiPort = env.MOODFLOW_PORT || "8000";
  return {
    plugins: [react()],
    server: {
      port: 5173,
      proxy: { "/api": `http://127.0.0.1:${apiPort}` },
    },
  };
});
