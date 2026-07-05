import type { ReactNode } from "react";
import IconButton from "./IconButton";
import { IconClose } from "./icons";

export interface InspectorPanelProps {
  /** Title text shown in the header strip. */
  title: ReactNode;
  /** Optional trailing slot in the header (e.g. kind badge). */
  trailing?: ReactNode;
  /** aria-label for the close button. */
  closeLabel: string;
  onClose: () => void;
  /** Optional footer pinned below the scroll area. */
  footer?: ReactNode;
  children: ReactNode;
  className?: string;
}

/**
 * Inspector panel shell — header strip with title + close, scrollable body,
 * optional pinned footer. Used by node and edge inspectors on the designer
 * canvas.
 */
export default function InspectorPanel({
  title,
  trailing,
  closeLabel,
  onClose,
  footer,
  children,
  className,
}: InspectorPanelProps) {
  return (
    <div
      className={[
        "flex h-full flex-col overflow-hidden border-l border-edge bg-surface-raised",
        className,
      ]
        .filter(Boolean)
        .join(" ")}
    >
      <div className="flex h-10 shrink-0 items-center gap-2 border-b border-edge px-3.5">
        <span className="min-w-0 flex-1 truncate font-data text-[length:var(--t-base)] font-semibold text-content-primary">
          {title}
        </span>
        {trailing}
        <IconButton aria-label={closeLabel} onClick={onClose}>
          <IconClose size={12} />
        </IconButton>
      </div>

      <div className="flex min-h-0 flex-1 flex-col overflow-y-auto">{children}</div>

      {footer && <div className="shrink-0 border-t border-edge">{footer}</div>}
    </div>
  );
}
