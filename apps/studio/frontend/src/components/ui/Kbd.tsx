import type { ReactNode } from "react";

export default function Kbd({ children, className }: { children: ReactNode; className?: string }) {
  return (
    <kbd
      className={[
        "rounded border border-edge bg-surface-overlay px-1 font-data text-[length:var(--t-xs)] leading-[1.5] text-content-muted",
        className,
      ]
        .filter(Boolean)
        .join(" ")}
    >
      {children}
    </kbd>
  );
}
