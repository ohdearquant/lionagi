import type { ReactNode } from "react";

export interface SectionLabelProps {
  children: ReactNode;
  /** Optional count rendered in mono after the label. */
  count?: number | string;
  /** Optional right-aligned slot (actions, toggles). */
  trailing?: ReactNode;
  className?: string;
}

/** Uppercase section heading — the one tracking/weight used everywhere. */
export default function SectionLabel({ children, count, trailing, className }: SectionLabelProps) {
  return (
    <div className={["flex items-center gap-2", className].filter(Boolean).join(" ")}>
      <span className="ui-uppercase font-ui text-[length:var(--t-xs)] font-semibold text-content-muted">
        {children}
      </span>
      {count != null && (
        <span className="font-data text-[length:var(--t-xs)] text-content-muted">{count}</span>
      )}
      {trailing != null && <span className="ml-auto flex items-center gap-1">{trailing}</span>}
    </div>
  );
}
