import type { ReactNode } from "react";

export interface ErrorBannerProps {
  children: ReactNode;
  /** Reduce text size from `text-body` to `text-meta`. Defaults to `body`. */
  size?: "body" | "meta";
  className?: string;
}

/** Inline error banner — red border + tinted background, no icon. */
export default function ErrorBanner({ children, size = "body", className }: ErrorBannerProps) {
  return (
    <div
      className={[
        "rounded border border-status-error/30 bg-status-error-bg px-3 py-2 text-status-error",
        size === "meta" ? "text-meta" : "text-body",
        className,
      ]
        .filter(Boolean)
        .join(" ")}
    >
      {children}
    </div>
  );
}
