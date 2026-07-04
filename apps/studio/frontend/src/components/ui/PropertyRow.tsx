import type { ReactNode } from "react";

export interface PropertyRowProps {
  label: string;
  children: ReactNode;
}

/**
 * PropertyRow — fixed-width label beside a value. Used in inspector panels
 * wherever a key:value fact grid is needed (node execution details, etc.).
 */
export default function PropertyRow({ label, children }: PropertyRowProps) {
  return (
    <div className="flex items-baseline gap-2">
      <span className="w-[76px] shrink-0 font-ui text-[length:var(--t-xs)] text-content-muted">
        {label}
      </span>
      <span className="min-w-0 flex-1 break-words font-data text-[length:var(--t-sm)] text-content-secondary">
        {children}
      </span>
    </div>
  );
}
