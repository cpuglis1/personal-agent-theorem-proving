/**
 * highlight.ts — Lean 4 syntax highlighting via Shiki, with a total fallback.
 *
 * Shiki ships a TextMate "lean4" grammar; we lazily spin up a single highlighter
 * (the WASM + grammar load is async and costly, so it is created once and reused).
 * Everything is defensive: if Shiki fails to load or the grammar can't tokenize,
 * we return null and the <LeanCode> component renders a styled <pre> instead, so
 * the Run view never breaks on the rendering layer.
 */
import type { HighlighterCore } from "shiki/core";

/** Theme used for all Lean code blocks (matches the console's dark palette). */
const THEME = "github-dark";

let highlighterPromise: Promise<HighlighterCore | null> | null = null;

async function getHighlighter(): Promise<HighlighterCore | null> {
  if (!highlighterPromise) {
    highlighterPromise = (async () => {
      try {
        // Fine-grained core API: bundle ONLY the lean4 grammar + github-dark theme
        // + the oniguruma wasm engine, instead of shiki's all-languages bundle.
        const [{ createHighlighterCore }, { createOnigurumaEngine }] =
          await Promise.all([
            import("shiki/core"),
            import("shiki/engine/oniguruma"),
          ]);
        return await createHighlighterCore({
          themes: [import("shiki/themes/github-dark.mjs")],
          langs: [import("shiki/langs/lean4.mjs")],
          engine: createOnigurumaEngine(import("shiki/wasm")),
        });
      } catch (e) {
        console.warn("[lean] Shiki unavailable — falling back to plain <pre>.", e);
        return null;
      }
    })();
  }
  return highlighterPromise;
}

/** The theme background color, so the fallback <pre> matches highlighted blocks. */
export async function leanThemeBackground(): Promise<string | null> {
  const hl = await getHighlighter();
  try {
    return hl?.getTheme(THEME).bg ?? null;
  } catch {
    return null;
  }
}

/**
 * Highlight Lean source to themed HTML. Returns null on any failure so the caller
 * can fall back to a plain <pre>.
 */
export async function highlightLean(code: string): Promise<string | null> {
  const hl = await getHighlighter();
  if (!hl) return null;
  try {
    return hl.codeToHtml(code, { lang: "lean4", theme: THEME });
  } catch (e) {
    console.warn("[lean] highlight failed — falling back to plain <pre>.", e);
    return null;
  }
}
