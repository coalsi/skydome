import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";

// `npm run dev` talks to the server on the Jetson; set SKYDOME to point elsewhere
const target = process.env.SKYDOME ?? "http://skydome.local";

export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: { proxy: { "/api": target } },
  build: { outDir: "dist", emptyOutDir: true },
});
