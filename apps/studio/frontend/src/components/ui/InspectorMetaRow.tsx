import type { ReactNode } from "react";

export interface InspectorMetaRowProps {
  label: string;
  children: ReactNode;
}

/**
 * InspectorMetaRow — stacked label-above-value layout. Used in floating
 * inspector panels (edge inspector) where horizontal space is tighter than
 * the docked node inspector.
 */
export default function InspectorMetaRow({ label, children }: InspectorMetaRowProps) {
  return (
    <div className="flex flex-col gap-0.5">
      <div className="font-ui text-[length:var(--t-xs)] font-semibold uppercase tracking-[0.11em] text-content-muted">
        {label}
      </div>
      <div className="break-words font-data text-[length:var(--t-xs)] leading-relaxed text-content-secondary">
        {children}
      </div>
    </div>
  );
}
