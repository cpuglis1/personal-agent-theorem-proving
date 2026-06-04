/**
 * Vite build/dev-server configuration for the Hyperion UI (React + TypeScript).
 *
 * Role in the system:
 *   This file configures the development server and production bundler for the
 *   Hyperion web console — the React/Tailwind front end that talks to the
 *   Hyperion FastAPI orchestrator (typically exposed on :4100). The console
 *   itself is documented in the project map as running on :4102; that public
 *   port is normally provided by the run/Docker layer that proxies or maps to
 *   the dev server defined here. This file controls only how Vite itself binds.
 *
 * Key design decisions / non-obvious context:
 *   - `@vitejs/plugin-react` enables React Fast Refresh (HMR) and JSX/TSX
 *     transform during development and production builds.
 *   - `server.host: true` makes the dev server listen on all network
 *     interfaces (0.0.0.0) rather than localhost only. This is required so the
 *     server is reachable from outside the process's own loopback — e.g. from a
 *     Docker container, another host on the LAN, or a reverse proxy. (See the
 *     "0.0.0.0 binding" gotcha noted for the ai-router stack.)
 *   - `server.port: 5173` pins Vite's default dev port so external proxies /
 *     compose mappings can rely on a stable internal port.
 *
 * @see https://vitejs.dev/config/ for the full configuration reference.
 */
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// `defineConfig` is an identity helper that only provides editor type
// inference for the config object; it has no runtime effect.
export default defineConfig({
  plugins: [react()],
  server: { host: true, port: 5173 },
});
