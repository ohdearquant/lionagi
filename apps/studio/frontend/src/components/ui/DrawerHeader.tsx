import type { ReactNode } from "react";

export interface DrawerHeaderProps {
  /** Primary name displayed in the header. */
  name: ReactNode;
  /** Optional badge pill shown after the name (kind label, version tag, etc.). */
  badge?: ReactNode;
  /** Override the text color of the badge. Defaults to `text-content-muted`. */
  badgeColor?: string;
  /** Right-aligned slot for action buttons. */
  trailing?: ReactNode;
}

/**
 * Standard header chrome for detail drawers: left-aligned truncated name,
 * optional badge pill, and an optional right-aligned action slot.
 */
export default function DrawerHeader({ name, badge, badgeColor, trailing }: DrawerHeaderProps) {
  return (
    <div className="flex shrink-0 items-center gap-3 border-b border-edge px-4 py-3">
      <span className="truncate font-data font-medium text-[length:var(--t-lg)] text-content-primary">
        {name}
      </span>
      {badge != null && (
        <span
          className={[
            "shrink-0 rounded border border-edge bg-surface-overlay px-1.5 py-0.5 text-[length:var(--t-xs)] uppercase tracking-[0.08em]",
            badgeColor ?? "text-content-muted",
          ].join(" ")}
        >
          {badge}
        </span>
      )}
      {trailing != null && <div className="ml-auto flex items-center gap-2">{trailing}</div>}
    </div>
  );
}
