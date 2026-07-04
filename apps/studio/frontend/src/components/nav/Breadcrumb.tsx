import { useLocation } from "@tanstack/react-router";
import { useTranslations } from "use-intl";
import { NAV_ITEMS, isRouteActive } from "./types";

// Pages that still live at their own route in phase A (ADR-0093 §Delivery A)
// even though the rail no longer links to them directly — Library and
// System link out to them, and old bookmarks keep working. Ordered longest
// prefix first so a more specific match (e.g. /admin/health) wins over a
// shorter one (/admin).
const STANDALONE_PAGES: Array<{
  prefix: string;
  surfaceKey: string;
  label: string;
}> = [
  { prefix: "/admin/health", surfaceKey: "surfaces.system", label: "Health" },
  { prefix: "/admin/maintenance", surfaceKey: "surfaces.system", label: "Maintenance" },
  { prefix: "/admin", surfaceKey: "surfaces.system", label: "Overview" },
  { prefix: "/projects", surfaceKey: "surfaces.system", label: "Projects" },
  { prefix: "/playbooks", surfaceKey: "surfaces.library", label: "Scripts" },
  { prefix: "/agents", surfaceKey: "surfaces.library", label: "Agents" },
  { prefix: "/schedules", surfaceKey: "surfaces.library", label: "Schedules" },
  { prefix: "/skills", surfaceKey: "surfaces.library", label: "Skills" },
  { prefix: "/plugins", surfaceKey: "surfaces.library", label: "Plugins" },
  { prefix: "/engines", surfaceKey: "surfaces.library", label: "Engines" },
  { prefix: "/teams", surfaceKey: "surfaces.library", label: "Teams" },
  { prefix: "/runs", surfaceKey: "surfaces.operations", label: "Run" },
  { prefix: "/invocations", surfaceKey: "surfaces.operations", label: "Run" },
  { prefix: "/shows", surfaceKey: "surfaces.operations", label: "Script run" },
].sort((a, b) => b.prefix.length - a.prefix.length);

function detailSegmentFor(pathname: string, prefix: string): string | null {
  if (pathname === prefix || !pathname.startsWith(`${prefix}/`)) return null;
  const remainder = pathname.slice(prefix.length + 1);
  const segment = remainder.split("/")[0];
  if (!segment) return null;
  const decoded = decodeURIComponent(segment);
  return decoded.length > 12 ? decoded.slice(0, 12) : decoded;
}

export default function Breadcrumb() {
  const t = useTranslations("nav");
  const pathname = useLocation().pathname ?? "/";

  const surface = NAV_ITEMS.find((item) => isRouteActive(item, pathname));
  if (surface) {
    return (
      <nav
        aria-label={t("breadcrumb.ariaLabel")}
        className="flex h-6 items-center gap-1 border-b border-edge bg-surface-base px-4 text-meta text-content-muted"
      >
        <span>{t(surface.i18nKey as Parameters<typeof t>[0])}</span>
      </nav>
    );
  }

  const standalone = STANDALONE_PAGES.find(
    (page) => pathname === page.prefix || pathname.startsWith(`${page.prefix}/`),
  );

  if (!standalone) {
    const firstSegment = pathname.split("/").filter(Boolean)[0] ?? "";
    return (
      <nav
        aria-label={t("breadcrumb.ariaLabel")}
        className="flex h-6 items-center gap-1 border-b border-edge bg-surface-base px-4 text-meta text-content-muted"
      >
        {firstSegment && <span>{decodeURIComponent(firstSegment)}</span>}
      </nav>
    );
  }

  const detailSegment = detailSegmentFor(pathname, standalone.prefix);

  return (
    <nav
      aria-label={t("breadcrumb.ariaLabel")}
      className="flex h-6 items-center gap-1 border-b border-edge bg-surface-base px-4 text-meta text-content-muted"
    >
      <span>{t(standalone.surfaceKey as Parameters<typeof t>[0])}</span>
      <span aria-hidden="true">›</span>
      <span>{standalone.label}</span>
      {detailSegment && (
        <>
          <span aria-hidden="true">›</span>
          <span>{detailSegment}</span>
        </>
      )}
    </nav>
  );
}
