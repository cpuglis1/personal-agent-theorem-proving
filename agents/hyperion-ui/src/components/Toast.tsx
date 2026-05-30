import { createContext, useCallback, useContext, useState, type ReactNode } from "react";

type ToastKind = "info" | "success" | "error";
interface Toast {
  id: number;
  kind: ToastKind;
  message: string;
}

interface ToastApi {
  push: (message: string, kind?: ToastKind) => void;
}

const ToastCtx = createContext<ToastApi | null>(null);

export function useToast(): ToastApi {
  const ctx = useContext(ToastCtx);
  if (!ctx) throw new Error("useToast must be used inside <ToastProvider>");
  return ctx;
}

const KIND_STYLE: Record<ToastKind, string> = {
  info: "border-sky-500/40 bg-sky-600/20 text-sky-100",
  success: "border-emerald-500/40 bg-emerald-600/20 text-emerald-100",
  error: "border-rose-500/40 bg-rose-600/20 text-rose-100",
};

export function ToastProvider({ children }: { children: ReactNode }) {
  const [toasts, setToasts] = useState<Toast[]>([]);

  const push = useCallback((message: string, kind: ToastKind = "info") => {
    const id = Date.now() + Math.random();
    setToasts((t) => [...t, { id, kind, message }]);
    setTimeout(() => setToasts((t) => t.filter((x) => x.id !== id)), 5000);
  }, []);

  return (
    <ToastCtx.Provider value={{ push }}>
      {children}
      <div className="pointer-events-none fixed bottom-4 right-4 z-50 flex max-w-sm flex-col gap-2">
        {toasts.map((t) => (
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
