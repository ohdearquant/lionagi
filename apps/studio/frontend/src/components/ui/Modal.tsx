import { useId, useRef } from "react";
import type { ReactNode } from "react";
import { useOverlayFocus } from "../../lib/useOverlayFocus";
import IconButton from "./IconButton";
import { IconClose } from "./icons";

export interface ModalProps {
  title: ReactNode;
  /** Accessible label for the close affordance (localized by the caller). */
  closeLabel: string;
  onClose: () => void;
  children: ReactNode;
  /** Spelled out, not open, so every class the app can produce is present for Tailwind. */
  maxWidth?: "max-w-md" | "max-w-lg" | "max-w-xl" | "max-w-2xl" | "max-w-4xl";
  className?: string;
}

/** Overlay dialog; backdrop click and Escape close. The overlay scrolls, not the card. */
export default function Modal({
  title,
  closeLabel,
  onClose,
  children,
  maxWidth = "max-w-lg",
  className,
}: ModalProps) {
  const dialogRef = useRef<HTMLDivElement>(null);
  const titleId = useId();
  useOverlayFocus({ description: "Modal", dialogRef, onEscape: onClose });

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
