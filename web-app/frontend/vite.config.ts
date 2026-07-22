import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// UI is always served from FastAPI on :8080 (see scripts/dev-local.sh and docker compose).
// This config is only used for `npm run build` / `npm run watch`.
export default defineConfig({
  plugins: [react()],
  build: {
    outDir: "dist",
    emptyOutDir: true,
  },
});
