"use client";

import React, { createContext, useCallback, useContext, useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";

// ─── Types ────────────────────────────────────────────────────────────────────

type ToastType = "success" | "error" | "info";

interface ToastItem {
  id: number;
  message: string;
  type: ToastType;
  /** true once the auto-dismiss timer fires — triggers slide-out animation */
  dismissing: boolean;
}

interface ToastContextValue {
  toast: (message: string, type?: ToastType) => void;
}

// ─── Context ──────────────────────────────────────────────────────────────────

const ToastContext = createContext<ToastContextValue | null>(null);

let nextId = 1;
const DURATION_MS = 3000;
const MAX_VISIBLE = 3;

// ─── Left-border accent per type ─────────────────────────────────────────────

function borderColor(type: ToastType): string {
  if (type === "success") return "border-l-4 border-l-status-success";
  if (type === "error") return "border-l-4 border-l-status-error";
  return "border-l-4 border-l-status-running";
}

function iconColor(type: ToastType): string {
  if (type === "success") return "text-status-success";
  if (type === "error") return "text-status-error";
  return "text-status-running";
}

function iconGlyph(type: ToastType): string {
  if (type === "success") return "✓";
  if (type === "error") return "✕";
  return "ℹ";
}

// ─── Single toast item ────────────────────────────────────────────────────────

function Toast({ item, onRemove }: { item: ToastItem; onRemove: (id: number) => void }) {
  return (
    <div
      role="alert"
      aria-live="polite"
      className={[
        "flex items-start gap-2.5 rounded border border-edge bg-surface-raised px-3 py-2.5",
        "text-body text-content-primary",
        "min-w-[220px] max-w-[360px]",
        borderColor(item.type),
        // slide in from right; slide out when dismissing
        "transition-all duration-200 ease-out",
        item.dismissing
          ? "translate-x-6 opacity-0 scale-95"
          : "translate-x-0 opacity-100 scale-100",
      ].join(" ")}
      style={{ boxShadow: "var(--shadow-card-hover)" }}
    >
      <span className={`shrink-0 font-bold text-[11px] mt-0.5 ${iconColor(item.type)}`}>
        {iconGlyph(item.type)}
      </span>
      <span className="flex-1 break-words leading-relaxed">{item.message}</span>
      <button
        type="button"
        aria-label="Dismiss"
        onClick={() => onRemove(item.id)}
        className="ml-0.5 shrink-0 text-content-muted hover:text-content-primary transition-colors duration-100 text-[10px] leading-none mt-0.5"
      >
        ✕
      </button>
    </div>
  );
}

// ─── Provider ────────────────────────────────────────────────────────────────

export function ToastProvider({ children }: { children: React.ReactNode }) {
  const [toasts, setToasts] = useState<ToastItem[]>([]);
  // track dismiss timers so we can clear them on manual dismiss
  const timers = useRef<Map<number, ReturnType<typeof setTimeout>>>(new Map());

  const remove = useCallback((id: number) => {
    clearTimeout(timers.current.get(id));
    timers.current.delete(id);
    setToasts((prev) => prev.filter((t) => t.id !== id));
  }, []);

  const startDismiss = useCallback(
    (id: number) => {
      // mark as dismissing (triggers CSS slide-out)
      setToasts((prev) => prev.map((t) => (t.id === id ? { ...t, dismissing: true } : t)));
      // remove from DOM after transition completes
      const cleanupTimer = setTimeout(() => remove(id), 320);
      timers.current.set(id, cleanupTimer);
    },
    [remove],
  );

  const toast = useCallback(
    (message: string, type: ToastType = "info") => {
      const id = nextId++;
      setToasts((prev) => {
        const next = [...prev, { id, message, type, dismissing: false }];
        // if over the cap, immediately dismiss the oldest
        if (next.length > MAX_VISIBLE) {
          const oldest = next[0];
          // schedule removal of oldest on next tick to avoid state-in-state
          setTimeout(() => startDismiss(oldest.id), 0);
        }
        return next;
      });

      // auto-dismiss after DURATION_MS
      const autoDismissTimer = setTimeout(() => startDismiss(id), DURATION_MS);
      timers.current.set(id, autoDismissTimer);
    },
    [startDismiss],
  );

  // clean up all timers on unmount
  useEffect(() => {
    const t = timers.current;
    return () => {
      Array.from(t.values()).forEach(clearTimeout);
    };
  }, []);

  const [mounted, setMounted] = useState(false);
  useEffect(() => setMounted(true), []);

  return (
    <ToastContext.Provider value={{ toast }}>
      {children}
      {mounted &&
        createPortal(
          <div
            aria-label="Notifications"
            className="fixed bottom-4 right-4 z-[9999] flex flex-col-reverse gap-2"
          >
            {toasts.map((item) => (
              <Toast key={item.id} item={item} onRemove={remove} />
            ))}
          </div>,
          document.body,
        )}
    </ToastContext.Provider>
  );
}

// ─── Hook ─────────────────────────────────────────────────────────────────────

export function useToast(): ToastContextValue {
  const ctx = useContext(ToastContext);
  if (!ctx) {
    throw new Error("useToast must be used within a ToastProvider");
  }
  return ctx;
}
