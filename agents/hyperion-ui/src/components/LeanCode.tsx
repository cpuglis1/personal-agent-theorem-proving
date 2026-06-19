/**
 * LeanCode — a Lean 4 source block, syntax-highlighted via Shiki with a copy
 * button. Falls back to a styled <pre> when Shiki/grammar is unavailable so a
 * block always renders. Code never wraps; it scrolls horizontally instead.
 */
import { useEffect, useState } from "react";

import { highlightLean } from "../lean/highlight";

interface LeanCodeProps {
  code: string;
  /** Small caption shown above the block (e.g. "scaffold", "result.lean"). */
  label?: string;
}

export default function LeanCode({ code, label }: LeanCodeProps) {
  const [html, setHtml] = useState<string | null>(null);
  const [copied, setCopied] = useState(false);

  useEffect(() => {
    let alive = true;
    setHtml(null);
    highlightLean(code).then((h) => {
      if (alive) setHtml(h);
    });
    return () => {
      alive = false;
    };
  }, [code]);

  const copy = async () => {
    try {
      await navigator.clipboard.writeText(code);
      setCopied(true);
      setTimeout(() => setCopied(false), 1200);
    } catch {
      /* clipboard blocked (e.g. insecure context) — ignore */
    }
  };

  return (
    <div className="lean-code">
      {label && <div className="lean-code__label">{label}</div>}
      <div className="lean-code__body">
        <button className="lean-code__copy" onClick={copy} type="button">
          {copied ? "copied ✓" : "copy"}
        </button>
        {html ? (
          // Shiki returns a full <pre class="shiki">…</pre>; we style it via .lean-code.
          <div className="lean-code__shiki" dangerouslySetInnerHTML={{ __html: html }} />
        ) : (
          <pre className="lean-code__fallback">
            <code>{code}</code>
          </pre>
        )}
      </div>
    </div>
  );
}
