import type { ReactNode } from "react";

export interface PageHeaderProps {
  // Breadcrumb pieces — rendered as "/ a / b / c" with the last being the
  // current page. The list can include react nodes (e.g. Links).
  breadcrumb?: ReactNode[];
  // The visible page title (large, content-primary)
  title: ReactNode;
  // Inline elements rendered immediately right of the title — typically
  // status pills or version chips.
  badges?: ReactNode;
  // Subtitle line beneath the title (short description).
  subtitle?: ReactNode;
  // Right-aligned action area: buttons, toggles, time-range chips.
  actions?: ReactNode;
  // Set to "tight" for inline detail-page headers; defaults to "loose" for
  // list pages with breathing room.
  density?: "tight" | "loose";
  className?: string;
}

export default function PageHeader({
  breadcrumb,
  title,
  badges,
  subtitle,
  actions,
  density = "loose",
  className,
}: PageHeaderProps) {
  const pad = density === "tight" ? "pb-3" : "pb-4";

  return (
    <header className={["flex flex-col gap-2 border-b border-edge", pad, className].filter(Boolean).join(" ")}>
      {breadcrumb && breadcrumb.length > 0 ? (
        <nav className="flex items-center gap-1 text-meta text-content-muted">
          <span>/</span>
          {breadcrumb.map((piece, i) => (
            <span key={i} className="flex items-center gap-1 truncate">
              {piece}
              {i < breadcrumb.length - 1 ? <span className="text-content-muted">/</span> : null}
            </span>
          ))}
        </nav>
      ) : null}

      <div className="flex flex-wrap items-center gap-3">
        <div className="flex min-w-0 items-center gap-3">
          <h1 className="font-mono text-xl font-semibold text-content-primary truncate">
            {title}
          </h1>
          {badges ? <div className="flex items-center gap-2">{badges}</div> : null}
        </div>
        {actions ? (
          <div className="ml-auto flex items-center gap-2 flex-wrap">{actions}</div>
        ) : null}
      </div>

      {subtitle ? (
        <p className="text-body text-content-secondary truncate">{subtitle}</p>
      ) : null}
    </header>
  );
}
