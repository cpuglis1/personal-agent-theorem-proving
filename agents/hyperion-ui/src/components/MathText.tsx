/**
 * MathText — render a description string that may contain LaTeX, using KaTeX for
 * `$…$` (inline) and `$$…$$` (display) spans. Non-math text is HTML-escaped and
 * passed through. KaTeX runs with throwOnError:false so a malformed formula shows
 * as a red error fragment rather than crashing the view.
 */
import { useMemo } from "react";
import katex from "katex";
import "katex/dist/katex.min.css";

function escapeHtml(s: string): string {
  return s
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;");
}

function renderKatex(tex: string, displayMode: boolean): string {
  try {
    return katex.renderToString(tex, { displayMode, throwOnError: false });
  } catch {
    return escapeHtml(displayMode ? `$$${tex}$$` : `$${tex}$`);
  }
}

/** Tokenize text into plain / inline-math ($…$) / display-math ($$…$$) → HTML. */
function renderMath(src: string): string {
  let out = "";
  let i = 0;
  while (i < src.length) {
    if (src.startsWith("$$", i)) {
      const end = src.indexOf("$$", i + 2);
      if (end >= 0) {
        out += renderKatex(src.slice(i + 2, end), true);
        i = end + 2;
        continue;
      }
    }
    if (src[i] === "$") {
      const end = src.indexOf("$", i + 1);
      if (end >= 0) {
        out += renderKatex(src.slice(i + 1, end), false);
        i = end + 1;
        continue;
      }
    }
    // Plain run up to the next '$' (or end of string).
    const next = src.indexOf("$", i);
    const chunk = next < 0 ? src.slice(i) : src.slice(i, next);
    if (chunk.length > 0) {
      out += escapeHtml(chunk);
      i += chunk.length;
    } else {
      // Lone trailing '$' with no closing delimiter — emit literally.
      out += "$";
      i += 1;
    }
  }
  return out;
}

export default function MathText({ children }: { children: string }) {
  const html = useMemo(() => renderMath(children), [children]);
  return <span className="math-text" dangerouslySetInnerHTML={{ __html: html }} />;
}
