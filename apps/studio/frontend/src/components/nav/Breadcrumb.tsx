import { Link, useLocation } from "@tanstack/react-router";
import type { LinkProps } from "@tanstack/react-router";
import { useTranslations } from "use-intl";
import { NAV_GROUPS, isRouteActive } from "./types";

const GROUP_KEY: Record<string, string> = {
  Dashboard: "groups.dashboard",
  Work: "groups.work",
  Library: "groups.library",
  Admin: "groups.admin",
};

const ITEM_KEY: Record<string, string> = {
  Dashboard: "items.dashboard",
  Shows: "items.shows",
  Runs: "items.runs",
  Projects: "items.projects",
  Teams: "items.teams",
  Invocations: "items.invocations",
  Schedules: "items.schedules",
  Playbooks: "items.playbooks",
  Agents: "items.agents",
  Plugins: "items.plugins",
  Skills: "items.skills",
  Health: "items.health",
  Maintenance: "items.maintenance",
};

export default function Breadcrumb() {
  const t = useTranslations("nav");
  const pathname = useLocation().pathname ?? "/";

  const tGroup = (label: string) => {
    const key = GROUP_KEY[label];
    return key ? t(key as Parameters<typeof t>[0]) : label;
  };

  const tItem = (label: string) => {
    const key = ITEM_KEY[label];
    return key ? t(key as Parameters<typeof t>[0]) : label;
  };

  // Dashboard root
  if (pathname === "/") {
    return (
      <nav
        aria-label={t("breadcrumb.ariaLabel")}
        className="flex h-6 items-center gap-1 border-b border-edge bg-surface-base px-4 text-meta text-content-muted"
      >
        <span>{t("items.dashboard")}</span>
      </nav>
    );
  }

  // Find matching group and item
  let groupLabel: string | null = null;
  let itemLabel: string | null = null;
  let itemHref: LinkProps["to"] | null = null;

  outer: for (const group of NAV_GROUPS) {
    for (const item of group.items) {
      if (isRouteActive(item, pathname)) {
        groupLabel = group.label;
        itemLabel = item.label;
        itemHref = item.href;
        break outer;
      }
    }
  }

  if (!groupLabel || !itemLabel || !itemHref) {
    // Unknown route: show first decoded segment
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

  // Determine detail segment beyond the section route
  let detailSegment: string | null = null;
  if (
    typeof itemHref === "string" &&
    pathname !== itemHref &&
    pathname.startsWith(`${itemHref}/`)
  ) {
    const remainder = pathname.slice(itemHref.length + 1);
    const segment = remainder.split("/")[0];
    if (segment) {
      const decoded = decodeURIComponent(segment);
      detailSegment = decoded.length > 12 ? decoded.slice(0, 12) : decoded;
    }
  }

  return (
    <nav
      aria-label={t("breadcrumb.ariaLabel")}
      className="flex h-6 items-center gap-1 border-b border-edge bg-surface-base px-4 text-meta text-content-muted"
    >
      <span>{tGroup(groupLabel)}</span>
      <span aria-hidden="true">›</span>
      <Link to={itemHref} className="transition-colors duration-150 hover:text-content-secondary">
        {tItem(itemLabel)}
      </Link>
      {detailSegment && (
        <>
          <span aria-hidden="true">›</span>
          <span>{detailSegment}</span>
        </>
      )}
    </nav>
  );
}
