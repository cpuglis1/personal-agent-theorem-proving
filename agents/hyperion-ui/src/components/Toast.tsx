/**
 * Toast notification system for the Hyperion UI web console.
 *
 * Provides a lightweight, self-contained toast (transient notification)
 * mechanism built on React context. It exposes two things:
 *   - `ToastProvider`: a context provider that renders the floating stack of
 *     toasts in the bottom-right corner and owns the toast state.
 *   - `useToast`: a hook any descendant component calls to imperatively push
 *     a notification (e.g. "Run started", "Failed to save workflow").
 *
 * Role in the system: this is the app-wide feedback channel for one-off
 * status/success/error messages triggered by user actions and API calls
 * across the console (run launches, edits, errors from the Hyperion API).
 * `ToastProvider` is expected to wrap the app near the root so that
 * `useToast` is available everywhere.
 *
 * Design notes:
 *   - There is no external state library; toasts live in local component
 *     state and the only public surface is the `push` function.
 *   - Each toast auto-dismisses after 5 seconds and can also be dismissed
 *     early by clicking it.
 *   - Styling is Tailwind-based and keyed by toast kind (see `KIND_STYLE`).
 */
import { createContext, useCallback, useContext, useState, type ReactNode } from "react";

/** Severity/category of a toast, which selects its color styling. */
type ToastKind = "info" | "success" | "error";

/** A single active toast notification held in provider state. */
interface Toast {
  /** Unique identifier used as the React key and for targeted removal. */
  id: number;
  /** Severity category that drives the visual style. */
  kind: ToastKind;
  /** The text shown to the user. */
  message: string;
}

/** Public API exposed via context and returned by {@link useToast}. */
interface ToastApi {
  /**
   * Show a new toast.
   * @param message - Text to display.
   * @param kind - Severity/style; defaults to "info".
   */
  push: (message: string, kind?: ToastKind) => void;
}

/**
 * React context carrying the toast API. `null` until a {@link ToastProvider}
 * is mounted above the consumer, which lets {@link useToast} detect misuse.
 */
const ToastCtx = createContext<ToastApi | null>(null);

/**
 * Hook to access the toast API from any component rendered inside a
 * {@link ToastProvider}.
 *
 * @returns The {@link ToastApi} (currently just `push`).
 * @throws Error if called outside of a `<ToastProvider>` (context is null),
 *   surfacing a clear developer error instead of a silent no-op.
 */
export function useToast(): ToastApi {
  const ctx = useContext(ToastCtx);
  if (!ctx) throw new Error("useToast must be used inside <ToastProvider>");
  return ctx;
}

/**
 * Tailwind class strings per toast kind (border/background/text colors).
 * Centralized here so the visual language stays consistent across the app.
 */
const KIND_STYLE: Record<ToastKind, string> = {
  info: "border-sky-500/40 bg-sky-600/20 text-sky-100",
  success: "border-emerald-500/40 bg-emerald-600/20 text-emerald-100",
  error: "border-rose-500/40 bg-rose-600/20 text-rose-100",
};

/**
 * Context provider that owns toast state and renders the floating toast stack.
 *
 * Wrap the application (or the relevant subtree) in this so descendants can
 * call {@link useToast}. The provider renders `children` followed by a fixed,
 * bottom-right overlay containing the current toasts.
 *
 * @param props.children - The subtree that gains access to the toast API.
 * @returns The provider element including the rendered toast overlay.
 */
export function ToastProvider({ children }: { children: ReactNode }) {
  // List of currently visible toasts; rendered newest-last in the stack.
  const [toasts, setToasts] = useState<Toast[]>([]);

  /**
   * Append a toast and schedule its automatic removal.
   *
   * Memoized with `useCallback` so the context value stays referentially
   * stable across renders, avoiding needless re-renders of consumers.
   *
   * Side effects: updates state immediately and starts a 5s timer that
   * removes the toast by id. The id combines `Date.now()` with a random
   * fraction to stay unique even when multiple toasts are pushed within the
   * same millisecond.
   */
  const push = useCallback((message: string, kind: ToastKind = "info") => {
    const id = Date.now() + Math.random();
    setToasts((t) => [...t, { id, kind, message }]);
    // Auto-dismiss after 5 seconds; filter by id so concurrent toasts are unaffected.
    setTimeout(() => setToasts((t) => t.filter((x) => x.id !== id)), 5000);
  }, []);

  return (
    <ToastCtx.Provider value={{ push }}>
      {children}
      {/*
        Floating overlay anchored bottom-right. `pointer-events-none` on the
        container lets clicks pass through empty space to the app, while each
        toast re-enables `pointer-events-auto` so it remains clickable.
      */}
      <div className="pointer-events-none fixed bottom-4 right-4 z-50 flex max-w-sm flex-col gap-2">
        {toasts.map((t) => (
          // Click a toast to dismiss it immediately (in addition to auto-dismiss).
          <div
            key={t.id}
            className={`pointer-events-auto cursor-pointer rounded-lg border px-4 py-2 text-sm shadow-lg ${KIND_STYLE[t.kind]}`}
            onClick={() => setToasts((cur) => cur.filter((x) => x.id !== t.id))}
          >
            {t.message}
          </div>
        ))}
      </div>
    </ToastCtx.Provider>
  );
}
