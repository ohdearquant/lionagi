import type { HTMLAttributes, ReactNode } from "react";

export interface ChipProps extends Omit<HTMLAttributes<HTMLSpanElement>, "className"> {
  children: ReactNode;
  /** When true, renders with uppercase tracking — for kind/type labels. */
  mono?: boolean;
  className?: string;
}

/**
 * Chip — small rounded metadata tag. Used for kind labels, role tags, and
 * other compact inline identifiers throughout the designer and mission views.
 */
export default function Chip({ children, mono, className, ...rest }: ChipProps) {
  return (
    <span
      {...rest}
      className={[
        "inline-flex items-center rounded border border-edge bg-surface-overlay px-1 font-data text-[length:var(--t-xs)] text-content-muted",
        mono ? "py-0.5 uppercase tracking-wider" : "leading-none",
        className,
      ]
        .filter(Boolean)
        .join(" ")}
    >
      {children}
    </span>
  );
}
