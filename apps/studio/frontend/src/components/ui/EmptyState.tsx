import type { ReactNode } from "react";

export interface EmptyStateProps {
  /** Single glyph or small icon shown in the circle. */
  glyph?: ReactNode;
  title: ReactNode;
  body?: ReactNode;
  /** Call-to-action slot, typically a Button. */
  action?: ReactNode;
  className?: string;
}

/** Inviting zero state — circle glyph, short title, one-line body, one CTA. */
export default function EmptyState({ glyph, title, body, action, className }: EmptyStateProps) {
  return (
    <div
      className={["flex flex-1 flex-col items-center justify-center gap-2 text-center", className]
        .filter(Boolean)
        .join(" ")}
    >
      {glyph != null && (
        <div className="mb-2 flex h-12 w-12 items-center justify-center rounded-full border border-edge bg-surface-raised text-xl text-content-muted">
          {glyph}
        </div>
      )}
      <p className="text-body font-medium text-content-secondary">{title}</p>
      {body != null && <p className="max-w-sm text-meta text-content-muted">{body}</p>}
      {action != null && <div className="mt-2">{action}</div>}
    </div>
  );
}
