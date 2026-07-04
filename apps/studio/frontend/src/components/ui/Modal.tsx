import { useEffect } from "react";
import type { ReactNode } from "react";
import IconButton from "./IconButton";
import { IconClose } from "./icons";

export interface ModalProps {
  title: ReactNode;
  /** Accessible label for the close affordance (localized by the caller). */
  closeLabel: string;
  onClose: () => void;
  children: ReactNode;
  /** Width utility for the dialog card. */
  maxWidth?: "max-w-md" | "max-w-lg" | "max-w-xl" | "max-w-2xl";
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
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  return (
    // eslint-disable-next-line jsx-a11y/no-static-element-interactions, jsx-a11y/click-events-have-key-events -- modal backdrop dismiss; keyboard Escape handled above
    <div
      className="fixed inset-0 z-50 flex items-start justify-center overflow-y-auto bg-black/50 py-8"
      onClick={(e) => {
        if (e.target === e.currentTarget) onClose();
      }}
    >
      <div
        role="dialog"
        aria-modal="true"
        className={[
          "mx-4 w-full rounded-lg border border-edge bg-surface-raised shadow-card",
          maxWidth,
          className,
        ]
          .filter(Boolean)
          .join(" ")}
      >
        <div className="flex items-center justify-between border-b border-edge px-5 py-4">
          <h2 className="font-data text-label font-semibold text-content-primary">{title}</h2>
          <IconButton aria-label={closeLabel} onClick={onClose}>
            <IconClose size={12} strokeWidth={2} />
          </IconButton>
        </div>
        {children}
      </div>
    </div>
  );
}
