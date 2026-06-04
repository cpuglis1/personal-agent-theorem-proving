/**
 * InfoTip.tsx
 *
 * A tiny, dependency-free "info" affordance for the Hyperion UI (React + Tailwind
 * web console at :4102). It renders a small circled "i" glyph that reveals a
 * tooltip bubble on hover, used throughout the console to attach inline,
 * contextual help to labels, table headers, form fields, etc.
 *
 * Role in the system:
 *   This is a presentational leaf component with no state, no effects, and no
 *   external dependencies. It exists so help text can be added next to any UI
 *   element with a single `<InfoTip text="..." />` rather than hand-rolling
 *   hover/positioning markup each time.
 *
 * Design decisions / non-obvious context:
 *   - Pure CSS hover, no JS state. Visibility is driven entirely by Tailwind's
 *     `group` / `group-hover` pattern: the outer `<span>` is the `group`, and the
 *     tooltip's `group-hover:opacity-100` reacts to hover anywhere on it. This
 *     keeps the component stateless and avoids re-renders.
 *   - The tooltip uses `opacity` transitions (not `display`) so it can animate;
 *     `pointer-events-none` ensures the (always-rendered) tooltip never blocks
 *     clicks or interferes with hover hit-testing on underlying elements.
 *   - Positioning: the tooltip is absolutely positioned above the glyph
 *     (`bottom-full`) and horizontally centered via `left-1/2` +
 *     `-translate-x-1/2`. `z-20` keeps it above neighboring content.
 *   - Accessibility: the glyph is `aria-hidden` (decorative), while the tooltip
 *     carries `role="tooltip"` so assistive tech can associate it as help text.
 *   - `cursor-help` on the glyph signals to mouse users that hovering reveals help.
 *   - `align-middle` / `inline-flex` let the tip sit inline next to text without
 *     disrupting baseline alignment.
 */

/**
 * Renders an inline info glyph with a hover-revealed tooltip.
 *
 * @param props.text - The help text shown inside the tooltip bubble on hover.
 * @returns A self-contained inline element (the "i" glyph plus its tooltip).
 */
export default function InfoTip({ text }: { text: string }) {
  return (
    // Outer wrapper acts as the Tailwind hover `group` and keeps the tip inline.
    <span className="group relative ml-1 inline-flex align-middle">
      <span
        className="flex h-3.5 w-3.5 cursor-help items-center justify-center rounded-full border border-slate-500 text-[10px] font-semibold leading-none text-slate-400"
        aria-hidden="true"
      >
        i
      </span>
      {/*
        Tooltip bubble: always in the DOM but visually hidden (opacity-0) until
        the parent group is hovered (group-hover:opacity-100). pointer-events-none
        prevents it from intercepting clicks/hover on underlying content.
      */}
      <span
        role="tooltip"
        className="pointer-events-none absolute bottom-full left-1/2 z-20 mb-1.5 w-64 -translate-x-1/2 rounded border border-edge bg-slate-900 p-2 text-xs font-normal leading-snug text-slate-300 opacity-0 shadow-lg transition-opacity duration-150 group-hover:opacity-100"
      >
        {text}
      </span>
    </span>
  );
}
