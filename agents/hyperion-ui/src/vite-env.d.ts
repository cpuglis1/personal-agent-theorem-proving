/**
 * Ambient TypeScript declarations for the Hyperion UI (Vite + React) frontend.
 *
 * Purpose:
 *   This `.d.ts` file is type-only and emits no runtime JavaScript. It exists to:
 *     1. Pull in Vite's client-side type definitions (the triple-slash reference
 *        below), which describe asset imports (`*.svg`, `*.css?inline`, etc.),
 *        HMR APIs, and the base `import.meta.env` shape.
 *     2. Augment the global `ImportMeta` / `ImportMetaEnv` interfaces with the
 *        project-specific environment variables this app reads at build time,
 *        so `import.meta.env.VITE_*` accesses are strongly typed.
 *
 * Role in the system:
 *   The Hyperion UI is the React/TypeScript web console (dev server :4102) for
 *   the Hyperion multi-agent orchestrator. At runtime the UI talks to the
 *   Hyperion FastAPI backend (default http://localhost:4100); the API base URL
 *   is supplied via the `VITE_HYPERION_API` env var (see `src/api/client.ts`).
 *
 * Key conventions / non-obvious context:
 *   - Vite only exposes env vars prefixed with `VITE_` to client code; that is
 *     why the variable is named `VITE_HYPERION_API` rather than `HYPERION_API`.
 *   - These are `interface` declarations (not `import`/`export`), so this file
 *     participates in global declaration merging — it intentionally has no
 *     top-level imports/exports to keep it an ambient (global) module.
 *   - Editing this file changes only the compile-time types, never behavior.
 */

/// <reference types="vite/client" />

/**
 * Build-time environment variables exposed to the client via `import.meta.env`.
 *
 * Merges with Vite's built-in `ImportMetaEnv` (MODE, DEV, PROD, BASE_URL, ...).
 * All members must be `VITE_`-prefixed to be injected by Vite.
 */
interface ImportMetaEnv {
  /**
   * Base URL of the Hyperion FastAPI backend (e.g. "http://localhost:4100").
   * Optional: when unset, the API client falls back to its default base URL.
   */
  readonly VITE_HYPERION_API?: string;
}

/**
 * Augments the global `ImportMeta` type so `import.meta.env` is typed as the
 * project-specific {@link ImportMetaEnv} above.
 */
interface ImportMeta {
  readonly env: ImportMetaEnv;
}
