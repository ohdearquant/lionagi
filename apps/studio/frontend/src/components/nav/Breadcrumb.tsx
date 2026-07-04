import { Link, useLocation } from "@tanstack/react-router";
import type { LinkProps } from "@tanstack/react-router";
import { useTranslations } from "use-intl";
import { GROUP_I18N_KEY, ITEM_I18N_KEY, NAV_GROUPS, isRouteActive } from "./types";

export default function Breadcrumb() {
  const t = useTranslations("nav");
  const pathname = useLocation().pathname ?? "/";

  const tGroup = (label: string) => {
    const key = GROUP_I18N_KEY[label];
    return key ? t(key as Parameters<typeof t>[0]) : label;
  };

  const tItem = (label: string) => {
    const key = ITEM_I18N_KEY[label];
    return key ? t(key as Parameters<typeof t>[0]) : label;
  };

  // Home root
  if (pathname === "/") {
    return (
      <nav
        aria-label={t("breadcrumb.ariaLabel")}
        className="flex h-6 items-center gap-1 border-b border-edge bg-surface-base px-4 text-meta text-content-muted"
      >
        <span>{tItem("Home")}</span>
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
