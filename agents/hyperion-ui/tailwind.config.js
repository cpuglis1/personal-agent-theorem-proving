/**
 * Tailwind CSS configuration for the Hyperion UI web console.
 *
 * Hyperion UI is the React + TypeScript + Vite front end (served on :4102)
 * for the Hyperion multi-agent orchestrator. Tailwind is the styling engine;
 * this file drives how Tailwind scans the source tree and which custom design
 * tokens are available as utility classes.
 *
 * Role in the system:
 *  - `content` tells Tailwind's JIT compiler which files to scan for class
 *    names so unused utilities are tree-shaken out of the production bundle.
 *    Missing a path here means classes used in that file silently disappear
 *    from the build, so keep this glob in sync with the source layout.
 *  - `theme.extend.colors` adds the project's custom dark-theme palette on top
 *    of (not replacing) Tailwind's defaults. These tokens back the console's
 *    dark "panel" aesthetic and are referenced as utilities like `bg-ink`,
 *    `bg-panel`, and `border-edge` throughout the components/pages.
 *
 * Key design decisions / non-obvious context:
 *  - ESM module syntax (`export default`) is used because the UI's Vite +
 *    package.json setup treats `.js` as ES modules; do not switch to
 *    `module.exports`.
 *  - Custom colors are intentionally placed under `extend` rather than at the
 *    top level of `theme` so they augment the stock Tailwind palette instead
 *    of overriding it.
 *  - `plugins` is intentionally empty; no Tailwind plugins are in use yet.
 */
/** @type {import('tailwindcss').Config} */
export default {
  // Files Tailwind scans for class names (JIT). Covers the HTML entry point
  // plus every TypeScript/TSX source file under src/.
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      // Custom dark-theme palette (hex values). Exposed as Tailwind color
      // utilities, e.g. `bg-ink` / `text-panel` / `border-edge`.
      colors: {
        ink: "#0b0e14", // near-black base background
        panel: "#131826", // raised surface / card background
        edge: "#222a3d", // subtle borders and dividers
      },
    },
  },
  plugins: [], // no Tailwind plugins configured
};
