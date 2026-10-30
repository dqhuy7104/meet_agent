import { defineConfig, type Connect } from "vite";
import react from "@vitejs/plugin-react";

const authPageFallback: Connect.NextHandleFunction = (
  request,
  _response,
  next,
) => {
  if (/^\/(login|signup)(\?|$)/.test(request.url ?? "")) {
    request.url = "/app/index.html";
  }
  next();
};

export default defineConfig({
  plugins: [
    react(),
    {
      name: "auth-page-fallback",
      configureServer(server) {
        server.middlewares.use(authPageFallback);
      },
      configurePreviewServer(server) {
        server.middlewares.use(authPageFallback);
      },
    },
  ],
  base: "/app/",
  server: { proxy: { "/api": "http://127.0.0.1:8000" } },
});
