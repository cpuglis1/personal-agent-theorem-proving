/**
 * PostCSS configuration for the Hyperion UI (React + Vite + Tailwind console, :4102).
 *
 * Vite automatically discovers this file and runs the listed PostCSS plugins over
 * every CSS asset in the build/dev pipeline (the order of plugins below matters).
 *
 * Plugin pipeline:
 *   1. tailwindcss  — expands Tailwind's `@tailwind` directives and utility classes
 *                     into real CSS, driven by `tailwind.config.js`.
 *   2. autoprefixer — adds vendor prefixes (e.g. `-webkit-`) to the generated CSS
 *                     based on the project's Browserslist targets.
 *
 * Both plugins are configured with empty option objects (`{}`), meaning each uses
 * its own defaults; per-plugin tuning lives in their dedicated config files
 * (tailwind.config.js / .browserslistrc / package.json "browserslist") rather than here.
 *
 * Note: this is an ESM module (`export default`), required because the UI package
 * is type:module; do not convert to `module.exports`.
 */
export default {
  plugins: {
    tailwindcss: {},
    autoprefixer: {},
  },
};
