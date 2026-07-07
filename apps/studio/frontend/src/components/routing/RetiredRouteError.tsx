import { Link } from "@tanstack/react-router";
import { formatRetiredRouteError } from "@/lib/retiredRoutes";

const FALLBACK_LABEL: Record<string, string> = {
  "/fleet": "Fleet",
  "/library": "Library",
  "/schedules": "Schedules",
  "/system": "System",
};

export interface RetiredRouteErrorProps {
  error: unknown;
  fallbackTo?: "/fleet" | "/library" | "/schedules" | "/system";
}

/**
 * Rendered by a retired route's errorComponent when resolving the redirect
 * target fails (e.g. an invocation fetch). Shown instead of auto-redirecting
 * so the operator sees the real failure detail rather than a silent bounce.
 */
export default function RetiredRouteError({
  error,
  fallbackTo = "/fleet",
}: RetiredRouteErrorProps) {
  const target = fallbackTo ?? "/fleet";
  return (
    <div className="flex h-full flex-col items-center justify-center gap-3 p-6 text-center">
      <p className="max-w-sm text-body text-status-error">{formatRetiredRouteError(error)}</p>
      <Link to={target} className="text-[length:var(--t-sm)] text-accent hover:underline">
        Back to {FALLBACK_LABEL[target] ?? target}
      </Link>
    </div>
  );
}
