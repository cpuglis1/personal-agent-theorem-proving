export default function InfoTip({ text }: { text: string }) {
  return (
    <span className="group relative ml-1 inline-flex align-middle">
      <span
        className="flex h-3.5 w-3.5 cursor-help items-center justify-center rounded-full border border-slate-500 text-[10px] font-semibold leading-none text-slate-400"
        aria-hidden="true"
      >
        i
      </span>
      <span
        role="tooltip"
        className="pointer-events-none absolute bottom-full left-1/2 z-20 mb-1.5 w-64 -translate-x-1/2 rounded border border-edge bg-slate-900 p-2 text-xs font-normal leading-snug text-slate-300 opacity-0 shadow-lg transition-opacity duration-150 group-hover:opacity-100"
      >
        {text}
      </span>
    </span>
  );
}
