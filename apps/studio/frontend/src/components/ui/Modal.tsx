import { useEffect, useId, useRef } from "react";
import type { ReactNode } from "react";
import IconButton from "./IconButton";
import { IconClose } from "./icons";

export interface ModalProps {
  title: ReactNode;
  /** Accessible label for the close affordance (localized by the caller). */
  closeLabel: string;
  onClose: () => void;
  children: ReactNode;
  /** Width utility for the dialog card. Spelled out rather than left open so
   *  every class the app can produce is literally present for Tailwind to find. */
  maxWidth?: "max-w-md" | "max-w-lg" | "max-w-xl" | "max-w-2xl" | "max-w-4xl";
  className?: string;
}

/** Overlay dialog — backdrop click and Escape both close. The overlay
 *  scrolls, not the card, so tall forms never trap inner scrollbars. */
export default function Modal({
  title,
  closeLabel,
  onClose,
  children,
  maxWidth = "max-w-lg",
  className,
}: ModalProps) {
  const dialogRef = useRef<HTMLDivElement>(null);
  const onCloseRef = useRef(onClose);
  const titleId = useId();

  useEffect(() => {
    onCloseRef.current = onClose;
  }, [onClose]);

  useEffect(() => {
    const previouslyFocused =
      document.activeElement instanceof HTMLElement ? document.activeElement : null;
    const dialog = dialogRef.current;
    const focusableSelector = [
      "button:not([disabled])",
      "[href]",
      "input:not([disabled])",
      "select:not([disabled])",
      "textarea:not([disabled])",
      '[tabindex]:not([tabindex="-1"])',
    ].join(",");

    const focusableElements = () =>
      Array.from(dialog?.querySelectorAll<HTMLElement>(focusableSelector) ?? []).filter(
        (element) =>
          element.tabIndex >= 0 &&
          !element.hidden &&
          element.getAttribute("aria-hidden") !== "true",
      );

    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        e.preventDefault();
        onCloseRef.current();
        return;
      }
      if (e.key !== "Tab" || !dialog) return;

      const focusable = focusableElements();
      if (focusable.length === 0) {
        e.preventDefault();
        dialog.focus();
        return;
      }

      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      if (
        e.shiftKey &&
        (document.activeElement === first || !dialog.contains(document.activeElement))
      ) {
        e.preventDefault();
        last.focus();
      } else if (!e.shiftKey && document.activeElement === last) {
        e.preventDefault();
        first.focus();
      }
    };

    (focusableElements()[0] ?? dialog)?.focus();
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("keydown", onKey);
      if (previouslyFocused?.isConnected) previouslyFocused.focus();
    };
  }, []);

  return (
    // eslint-disable-next-line jsx-a11y/no-static-element-interactions, jsx-a11y/click-events-have-key-events -- modal backdrop dismiss; keyboard Escape handled above
    <div
      className="fixed inset-0 z-50 flex items-start justify-center overflow-y-auto bg-black/50 py-8"
      onClick={(e) => {
        if (e.target === e.currentTarget) onClose();
      }}
    >
      <div
        ref={dialogRef}
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        tabIndex={-1}
        className={[
          "mx-4 w-full rounded-lg border border-edge bg-surface-raised shadow-card",
          maxWidth,
          className,
        ]
          .filter(Boolean)
          .join(" ")}
      >
        <div className="flex items-center justify-between border-b border-edge px-5 py-4">
          <h2 id={titleId} className="font-data text-label font-semibold text-content-primary">
            {title}
          </h2>
          <IconButton aria-label={closeLabel} onClick={onClose}>
            <IconClose size={12} strokeWidth={2} />
          </IconButton>
        </div>
        {children}
      </div>
    </div>
  );
}
