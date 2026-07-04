import type { ReactNode } from "react";

export interface StackedListProps {
  children: ReactNode;
  className?: string;
}

export interface StackedListRowProps {
  children: ReactNode;
  /** Index within the list — determines whether the top hairline is shown. */
  index: number;
  className?: string;
}

/**
 * StackedList — rounded bordered container for hairline-separated rows.
 * Each row uses StackedListRow which adds the inter-row hairline.
 *
 * Used by AttentionQueue and RecentRuns in mission control.
 */
export function StackedList({ children, className }: StackedListProps) {
  return (
    <div
      className={["overflow-hidden rounded border border-edge", className]
        .filter(Boolean)
        .join(" ")}
    >
      {children}
    </div>
  );
}

export function StackedListRow({ children, index, className }: StackedListRowProps) {
  return (
    <div
      className={className}
      style={index > 0 ? { borderTop: "1px solid var(--edge-hairline)" } : undefined}
    >
      {children}
    </div>
  );
}
